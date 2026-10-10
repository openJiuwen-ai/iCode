# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OSC 7501 (Program Status Protocol) reporting for native TUI runs.

Spec: https://www.superlogical.com/rex/docs/build/program-status (rev 0.3).
The TUI maintains one root record in the host terminal: ``idle`` at rest,
``working`` while a turn or workflow run holds the execution lease,
``blocked`` while a human decision is pending (approval or question, with
``kind=permission`` / ``kind=question``), ``done`` when a turn finished and
``error`` when it failed. Emission is unconditional: feature detection is
optional per the spec and benign terminals ignore unknown OSC sequences, so
no capability query is sent and no reply is read.
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from chrys.app.tui.terminal.title import (
    _app_terminal_title_writer,
    _title_safe_text,
    running_under_textual_web_driver,
)
from chrys.foundation.events.types import (
    ApprovalAutoFulfillBlocked,
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    AskUserTimedOut,
    Error,
    ExecutionChanged,
    InvocationMessage,
    QuestionToUser,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
)
from chrys.foundation.models.ask_user import AskUserQuestion

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Event

type ProgramState = Literal["idle", "working", "done", "blocked", "error"]

_PROGRAM_STATUS_APP = "icode"
_MESSAGE_LIMIT_CHARS = 200
_OSC_INTRO = "\x1b]7501;"
_OSC_ST = "\x1b\\"


def program_status_message(text: str) -> str:
    """Return one protocol-safe message line for ``msg``.

    The spec discards a whole report whose decoded message carries control
    characters, and messages must be a single human-readable line, so strip
    terminal sequences and control characters, collapse whitespace and cap
    the length well under the 2048-byte decoded limit.
    """
    return _title_safe_text(text)[:_MESSAGE_LIMIT_CHARS]


def build_program_status_report(
    state: ProgramState,
    *,
    kind: str = "",
    message: str = "",
    app: str = _PROGRAM_STATUS_APP,
) -> str:
    """Build one OSC 7501 report sequence.

    Every report replaces the terminal's record for us completely, so the
    stable ``app`` name rides every report, and empty optional keys are
    omitted rather than sent empty.
    """
    pairs = [f"state={state}"]
    if kind:
        pairs.append(f"kind={kind}")
    if app:
        pairs.append(f"app={app}")
    if message:
        encoded = base64.b64encode(message.encode("utf-8")).decode("ascii")
        pairs.append(f"msg={encoded}")
    return f"{_OSC_INTRO}{':'.join(pairs)}{_OSC_ST}"


def emit_program_status_report(app: object, state: ProgramState, *, kind: str = "", message: str = "") -> None:
    """Write one OSC 7501 report through the app's terminal driver, fail-soft.

    Native runs only: textual-serve captures stderr and has no OSC terminal,
    and before the driver exists there is nothing to write to.
    """
    if running_under_textual_web_driver():
        return
    writer = _app_terminal_title_writer(app)
    if writer is None:
        return
    with contextlib.suppress(Exception):
        writer(build_program_status_report(state, kind=kind, message=message))


@dataclass(frozen=True)
class _ProgramStatusRecord:
    """The full body of one root record; equal records are not re-sent."""

    state: ProgramState
    kind: str = ""
    message: str = ""


def _question_message(questions: tuple[AskUserQuestion, ...]) -> str:
    return program_status_message(questions[0].question) if questions else ""


def _approval_message(tool_name: str, summary: str) -> str:
    return program_status_message(f"{tool_name}: {summary}" if summary else tool_name)


class ProgramStatusReporter:
    """Fold backend events into the host terminal's root OSC 7501 record.

    The fold mirrors what the TUI already shows: ``ExecutionChanged`` drives
    working/idle, the chat turn's own final message and a live-run ``Error``
    drive done/error exactly where the tab title earns its check/cross, and
    the approval/question events drive blocked. Sub-agent and workflow-node
    traffic stays inside the parent's working record: only a pending human
    decision (whose card blocks the whole app) or the root invocation itself
    changes the root state.
    """

    def __init__(self, bus: EventBus, *, app_provider: Callable[[], object]) -> None:
        self._bus = bus
        self._app_provider = app_provider
        self._last: _ProgramStatusRecord | None = None
        self._subscribed = False
        # Bound methods are held once so unsubscribe gets the same handlers
        # subscribe registered. The handler parameter stays ``Any`` so one
        # heterogeneous table serves every event type, exactly like the main
        # screen's subscription table.
        self._entries: tuple[tuple[type[Event], Callable[[Any], Awaitable[None]]], ...] = (
            (ExecutionChanged, self.on_execution_changed),
            (InvocationMessage, self.on_invocation_message),
            (Error, self.on_error),
            (ApprovalRequest, self.on_approval_request),
            (ApprovalReviewed, self.on_approval_reviewed),
            (ApprovalResponse, self.on_approval_response),
            (ApprovalCancelled, self.on_approval_cancelled),
            (ApprovalAutoFulfillBlocked, self.on_approval_auto_fulfill_blocked),
            (QuestionToUser, self.on_question_to_user),
            (AskUserResponse, self.on_ask_user_response),
            (AskUserTimedOut, self.on_ask_user_timed_out),
            (WorkflowNodeAskUser, self.on_workflow_node_ask_user),
            (WorkflowNodeAnswered, self.on_workflow_node_answered),
        )

    async def subscribe(self) -> None:
        """Register every handler, then establish the idle root record."""
        if self._subscribed:
            return
        for event_type, handler in self._entries:
            await self._bus.subscribe(event_type, handler)
        self._subscribed = True
        self._report(_ProgramStatusRecord("idle"))

    async def unsubscribe(self) -> None:
        """Remove every registered handler."""
        if not self._subscribed:
            return
        for event_type, handler in self._entries:
            await self._bus.unsubscribe(event_type, handler)
        self._subscribed = False

    # ------------------------------------------------------------------ #
    # Event fold
    # ------------------------------------------------------------------ #

    async def on_execution_changed(self, event: ExecutionChanged) -> None:
        if event.snapshot.kind in ("turn", "workflow"):
            self._report(_ProgramStatusRecord("working"))
        elif self._last is None or self._last.state in ("idle", "working", "blocked"):
            # A lease release right after a terminal outcome must not erase
            # it: every report replaces the record, so idle is only sent when
            # no done/error is on record. An interrupt releases the lease
            # straight from working/blocked and does report idle.
            self._report(_ProgramStatusRecord("idle"))

    async def on_invocation_message(self, event: InvocationMessage) -> None:
        # Only the chat turn's own final message completes the root record.
        # Sub-agent finals are fragments of the parent's working turn, and
        # workflow-node traffic belongs to the workflow's working record.
        if not event.is_final or event.is_intermediate:
            return
        if event.origin.kind != "turn" or event.origin.root.kind != "turn":
            return
        self._report(_ProgramStatusRecord("done", message=program_status_message(event.text)))

    async def on_error(self, event: Error) -> None:
        # Mirror the tab title's cross: an error while a run is live ends it.
        # Errors outside a run (submit validation, agent load) leave the
        # current record alone.
        if self._last is None or self._last.state not in ("working", "blocked"):
            return
        self._report(_ProgramStatusRecord("error", message=program_status_message(event.message)))

    async def on_approval_request(self, event: ApprovalRequest) -> None:
        if event.judging:
            # The judge may still approve it without the user; wait for the
            # verdict instead of flashing blocked.
            return
        self._report(
            _ProgramStatusRecord(
                "blocked",
                kind="permission",
                message=_approval_message(event.tool_name, event.intent_summary or event.user_message),
            )
        )

    async def on_approval_reviewed(self, event: ApprovalReviewed) -> None:
        if event.approved:
            self._report(_ProgramStatusRecord("working"))
        else:
            self._report(
                _ProgramStatusRecord("blocked", kind="permission", message=program_status_message(event.reason))
            )

    async def on_approval_response(self, _event: ApprovalResponse) -> None:
        self._report(_ProgramStatusRecord("working"))

    async def on_approval_cancelled(self, _event: ApprovalCancelled) -> None:
        self._report(_ProgramStatusRecord("working"))

    async def on_approval_auto_fulfill_blocked(self, _event: ApprovalAutoFulfillBlocked) -> None:
        # The approved verdict needs the user after all: their decision is
        # now the pending one.
        self._report(_ProgramStatusRecord("blocked", kind="permission"))

    async def on_question_to_user(self, event: QuestionToUser) -> None:
        self._report(_ProgramStatusRecord("blocked", kind="question", message=_question_message(event.questions)))

    async def on_ask_user_response(self, _event: AskUserResponse) -> None:
        self._report(_ProgramStatusRecord("working"))

    async def on_ask_user_timed_out(self, _event: AskUserTimedOut) -> None:
        # The expired question fails into the still-running turn.
        self._report(_ProgramStatusRecord("working"))

    async def on_workflow_node_ask_user(self, event: WorkflowNodeAskUser) -> None:
        self._report(_ProgramStatusRecord("blocked", kind="question", message=_question_message(event.questions)))

    async def on_workflow_node_answered(self, _event: WorkflowNodeAnswered) -> None:
        self._report(_ProgramStatusRecord("working"))

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _report(self, record: _ProgramStatusRecord) -> None:
        if record == self._last:
            return
        self._last = record
        emit_program_status_report(self._app_provider(), record.state, kind=record.kind, message=record.message)
