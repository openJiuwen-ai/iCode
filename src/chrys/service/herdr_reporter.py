# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Report agent state to the Herdr pane owner (https://herdr.dev).

Herdr, a terminal multiplexer for coding agents, injects ``HERDR_ENV``,
``HERDR_PANE_ID`` and ``HERDR_BIN_PATH`` into the processes a pane runs.
Inside such a pane this reporter tells Herdr when a turn is running, waiting
on the user, or idle, and which session the pane should resume after a
Herdr restart, through Herdr's self-service ``pane report-agent`` CLI.
Outside Herdr ``HerdrReporter.from_env`` returns ``None`` and nothing in
this module runs.

Reporting stays out of the way by construction: events arrive through an
EventBus stream (publishers never wait on this consumer), the CLI calls run
in worker threads with a short timeout, and every failure is silently
ignored. While a report is in flight newer events keep folding into the
pending state, so only the newest state is ever sent; identical consecutive
states are skipped. ``--seq`` is ``time.time_ns()``, which stays monotonic
across process restarts as Herdr requires.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import subprocess
import time
from typing import TYPE_CHECKING, Literal

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalAutoFulfillBlocked,
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    AskUserTimedOut,
    Event,
    ExecutionChanged,
    QuestionToUser,
    SessionReady,
    SessionRestored,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
)
from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

if TYPE_CHECKING:
    from chrys.foundation.models.ask_user import AskUserQuestion

logger = logging.getLogger(__name__)

_SOURCE = "icode"
"""Identity of this integration; Herdr reserves the ``herdr:`` prefix for its own."""

_AGENT = "icode"
"""Agent name Herdr shows in its sidebar and ``herdr agent list``."""

_RESUME_COMMAND = "icode"
"""Resume argv head; Herdr requires a bare PATH command name, never a path."""

_REPORT_TIMEOUT_SECONDS = 1.0
_MAX_MESSAGE_CHARS = 200

_State = Literal["idle", "working", "blocked"]


def _clip(text: str) -> str:
    """Collapse to one line and cap the length of a ``--message`` value."""
    return " ".join(text.split())[:_MAX_MESSAGE_CHARS]


def _approval_message(event: ApprovalRequest) -> str:
    parts = [part for part in (event.tool_name, event.intent_summary) if part]
    return _clip(": ".join(parts))


def _question_message(questions: tuple[AskUserQuestion, ...]) -> str:
    for question in questions:
        return _clip(question.header or question.question)
    return ""


def _invoke_herdr_cli(argv: list[str]) -> None:
    """Run one Herdr CLI call; never raises and never touches the console."""
    try:
        subprocess.run(  # noqa: S603 - argv head is the operator's Herdr binary from HERDR_BIN_PATH.
            argv,
            check=False,
            timeout=_REPORT_TIMEOUT_SECONDS,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **windows_hidden_subprocess_kwargs(),
        )
    except Exception:
        # Herdr may be gone, hung or too old; reporting is best-effort by
        # design and must never surface in the session.
        logger.debug("Herdr CLI call failed: %r", argv, exc_info=True)


class HerdrReporter:
    """Fold bus events into Herdr agent-state reports for one pane.

    Two event sources that never fight: the execution lease
    (``ExecutionChanged``) owns working/idle — sub-agent and workflow-node
    traffic never holds the lease, so no origin filtering is needed — while
    approval and ask-user events overlay ``blocked`` for as long as the
    turn stays live waiting on the user.
    """

    def __init__(self, *, pane_id: str, bin_path: str) -> None:
        self._pane_id = pane_id
        self._bin_path = bin_path
        self._consume_task: asyncio.Task[None] | None = None
        self._send_task: asyncio.Task[None] | None = None
        self._started = asyncio.Event()
        self._wake = asyncio.Event()
        self._state: _State = "idle"
        self._message = ""
        self._session_id: str | None = None
        self._session_report_pending = False
        self._last_sent: tuple[str, str] | None = None
        self._last_approval_message = ""

    @classmethod
    def from_env(cls) -> HerdrReporter | None:
        """Build a reporter inside a Herdr pane; ``None`` everywhere else."""
        if os.environ.get("HERDR_ENV") != "1":
            return None
        pane_id = os.environ.get("HERDR_PANE_ID", "").strip()
        bin_path = os.environ.get("HERDR_BIN_PATH", "").strip()
        if not pane_id or not bin_path:
            return None
        return cls(pane_id=pane_id, bin_path=bin_path)

    async def start(self, bus: EventBus) -> None:
        """Start consuming; returns once the stream is registered on the bus.

        Registering before the caller's next await guarantees the opening
        ``SessionReady`` (and any early turn) is seen.
        """
        if self._consume_task is not None:
            return
        self._consume_task = asyncio.create_task(self._consume(bus), name="chrys.herdr.reporter.events")
        self._send_task = asyncio.create_task(self._send_loop(), name="chrys.herdr.reporter.sender")
        await self._started.wait()

    async def close(self) -> None:
        """Stop reporting and release the pane; safe to call more than once.

        A release raced by one last in-flight state report is self-healing:
        Herdr clears a self-reported agent once its pane returns to a shell.
        """
        for task in (self._consume_task, self._send_task):
            if task is not None:
                task.cancel()
        for task in (self._consume_task, self._send_task):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._consume_task = None
        self._send_task = None
        await asyncio.to_thread(_invoke_herdr_cli, self._release_argv())

    async def _consume(self, bus: EventBus) -> None:
        async with bus.stream(
            ExecutionChanged,
            ApprovalRequest,
            ApprovalReviewed,
            ApprovalResponse,
            ApprovalCancelled,
            ApprovalAutoFulfillBlocked,
            QuestionToUser,
            AskUserResponse,
            AskUserTimedOut,
            WorkflowNodeAskUser,
            WorkflowNodeAnswered,
            SessionReady,
            SessionRestored,
        ) as events:
            self._started.set()
            async for event in events:
                self._apply(event)
                self._wake.set()

    async def _send_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            key = (self._state, self._message)
            session_id = self._session_id if self._session_report_pending else None
            if session_id is None and key == self._last_sent:
                continue
            try:
                await asyncio.to_thread(_invoke_herdr_cli, self._report_argv(session_id=session_id))
            except Exception:
                # The invoker is responsible for its own errors; this guard
                # only keeps the sender alive if one escapes anyway. The
                # report stays unsent, so the next state change retries it.
                logger.debug("Herdr report send failed", exc_info=True)
                continue
            self._last_sent = key
            self._session_report_pending = False

    def _report_argv(self, *, session_id: str | None) -> list[str]:
        argv = [
            self._bin_path,
            "pane",
            "report-agent",
            self._pane_id,
            "--source",
            _SOURCE,
            "--agent",
            _AGENT,
            "--state",
            self._state,
            "--seq",
            str(time.time_ns()),
        ]
        if self._message:
            argv += ["--message", self._message]
        if session_id is not None:
            argv += ["--agent-session-id", session_id, "--", _RESUME_COMMAND, "--session", session_id]
        return argv

    def _release_argv(self) -> list[str]:
        return [
            self._bin_path,
            "pane",
            "release-agent",
            self._pane_id,
            "--source",
            _SOURCE,
            "--agent",
            _AGENT,
            "--seq",
            str(time.time_ns()),
        ]

    def _apply(self, event: Event) -> None:
        """Fold one event into the pending state; sending happens elsewhere."""
        if isinstance(event, ExecutionChanged):
            # Any lease owner (a turn or a workflow run) means the pane is
            # working; a released lease ends every overlay below as idle.
            self._set_state("working" if event.snapshot.kind != "idle" else "idle")
        elif isinstance(event, ApprovalRequest):
            message = _approval_message(event)
            self._last_approval_message = message
            # With the LLM judge still evaluating, the pane is not blocked on
            # the user yet; a flagged verdict or a shown dialog reports it.
            if not event.judging:
                self._set_state("blocked", message)
        elif isinstance(event, ApprovalReviewed):
            if event.approved:
                self._set_state("working")
            else:
                self._set_state("blocked", _clip(event.reason) or self._last_approval_message)
        elif isinstance(event, (ApprovalResponse, ApprovalCancelled)):
            self._set_state("working")
        elif isinstance(event, ApprovalAutoFulfillBlocked):
            # The frontend is asking the user after all, judge verdict or not.
            self._set_state("blocked", self._last_approval_message)
        elif isinstance(event, QuestionToUser):
            self._set_state("blocked", _question_message(event.questions))
        elif isinstance(event, (AskUserResponse, AskUserTimedOut)):
            # A timed-out question hands the agent a timeout result; the
            # turn keeps running either way.
            self._set_state("working")
        elif isinstance(event, WorkflowNodeAskUser):
            self._set_state("blocked", _question_message(event.questions))
        elif isinstance(event, WorkflowNodeAnswered):
            self._set_state("working")
        elif isinstance(event, (SessionReady, SessionRestored)):
            # SessionReady refires on every rebuild (a profile switch too),
            # so only a genuinely new session id re-arms the resume report.
            session_id = event.session_id or ""
            if session_id and session_id != self._session_id:
                self._session_id = session_id
                self._session_report_pending = True

    def _set_state(self, state: _State, message: str = "") -> None:
        self._state = state
        self._message = message
