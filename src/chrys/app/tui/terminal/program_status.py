# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OSC 7501 (Program Status Protocol) reporting for native TUI runs.

Spec: https://www.superlogical.com/rex/docs/build/program-status (rev 0.4).
The TUI maintains one root record in the host terminal, derived from facts
instead of the last event: ``idle`` at rest, ``working`` while a turn or
workflow run holds the execution lease, ``blocked`` while any human decision
is pending, ``done`` when a run finished and ``error`` when it failed. The
pending decisions are tracked per id (approval and question request ids,
paused sub-agent invocation ids, workflow node retries), so answering one of
several waits keeps the record blocked until the last one is answered, and
another invocation's traffic cannot mask a wait that is still open. Emission
is unconditional: feature detection is optional per the spec and benign
terminals ignore unknown OSC sequences, so no capability query is sent and no
reply is read. Report text is English protocol prose throughout: model and
user text goes out as written, iCode's own fragments stay untranslated.
"""

from __future__ import annotations

import base64
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from chrys.app.tui.terminal.title import (
    app_terminal_title_writer,
    running_under_textual_web_driver,
    title_safe_text,
)
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.events.types import (
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalResponse,
    ApprovalReviewed,
    AskUserResponse,
    AskUserTimedOut,
    Error,
    ExecutionChanged,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationMessage,
    InvocationPaused,
    InvocationResumed,
    QuestionToUser,
    SessionReady,
    SessionRestored,
    UserInterrupt,
    WorkflowNodeAnswered,
    WorkflowNodeAskUser,
    WorkflowNodeStateChanged,
    WorkflowRunFinished,
)
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.service.workflows.outcomes import run_status

if TYPE_CHECKING:
    from collections.abc import Awaitable

    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Event

type ProgramState = Literal["idle", "working", "done", "blocked", "error"]

_MESSAGE_LIMIT_CHARS = 200
_OSC_INTRO = "\x1b]7501;"
_OSC_ST = "\x1b\\"
# Errors that rejected one operation (a blocked injection, a submit the
# admission rules refused) without changing the running turn's outcome. The
# same meaning lives in BackendEventHandler's submit-blocking code set; an
# error here must never read as the run failing.
_REJECTED_OPERATION_ERROR_CODES = frozenset(
    {
        "hook_blocked",
        "image_attachment_error",
        "manual_settings_reload_denied",
        "manual_settings_reload_failed",
        "not_ready",
        "prompt_admission_conflict",
        "retry_missing_user_anchor",
        "vision_unsupported",
        "working_dir_missing",
    }
)


def program_status_message(text: str) -> str:
    """Return one protocol-safe message line for ``msg``.

    The spec discards a whole report whose decoded message carries control
    characters, and messages must be a single human-readable line, so take
    the first non-blank line, strip terminal sequences and control
    characters, and cap the length well under the 2048-byte decoded limit.
    """
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    return title_safe_text(first_line)[:_MESSAGE_LIMIT_CHARS]


def build_program_status_report(
    state: ProgramState,
    *,
    kind: str = "",
    message: str = "",
    app: str = APP_DISPLAY_NAME,
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


def emit_program_status_report(app: object, state: ProgramState, *, kind: str = "", message: str = "") -> bool:
    """Write one OSC 7501 report through the app's terminal driver.

    Returns whether the write happened: native runs only (textual-serve
    captures stderr and has no OSC terminal), before the driver exists there
    is nothing to write to, and a failed write must be retried by the next
    report rather than remembered as sent.
    """
    if running_under_textual_web_driver():
        return False
    writer = app_terminal_title_writer(app)
    if writer is None:
        return False
    with contextlib.suppress(Exception):
        writer(build_program_status_report(state, kind=kind, message=message))
        return True
    return False


@dataclass(frozen=True)
class _ProgramStatusRecord:
    """The full body of one root record; equal records are not re-sent."""

    state: ProgramState
    kind: str = ""
    message: str = ""

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        return build_program_status_report(self.state, kind=self.kind, message=self.message)


@dataclass(frozen=True)
class _WorkflowAsk:
    """One open workflow ``ctx.ask`` question, bound to its node attempt."""

    run_id: str
    activation_id: str
    attempt: int
    message: str


def _question_message(questions: tuple[AskUserQuestion, ...]) -> str:
    return program_status_message(questions[0].question) if questions else ""


def _pause_message(event: InvocationPaused) -> str:
    name = event.agent_name or "sub-agent"
    return program_status_message(f"{name} failed - retry or abort")


class ProgramStatusReporter:
    """Derive the host terminal's root OSC 7501 record from observed facts.

    Waits are collected per id, so concurrent approvals, questions, paused
    sub-agents and workflow retries coexist: the record stays ``blocked``
    until the last one clears, and the message always names the same wait as
    the kind. Between waits, the execution lease says ``working``; after it
    releases, a run outcome (``done``/``error``) stands until the session
    changes or a user interrupt replaces it with ``idle``. Derived records
    are compared with the last one that was successfully written.
    """

    def __init__(self, bus: EventBus, *, app_provider: Callable[[], object]) -> None:
        self._bus = bus
        self._app_provider = app_provider
        self._last: _ProgramStatusRecord | None = None
        self._subscribed = False
        # Pending human decisions, each keyed by the id that resolves it.
        self._approvals: dict[str, str] = {}
        self._judging: dict[str, str] = {}
        self._questions: dict[str, str] = {}
        self._paused_agents: dict[str, str] = {}
        self._workflow_asks: dict[str, _WorkflowAsk] = {}
        self._retry_nodes: dict[tuple[str, str], str] = {}
        # What the lease holds, what the finished run left, and whether the
        # user cut a run short (an interrupt outranks any leftover outcome).
        self._lease: Literal["idle", "turn", "workflow"] = "idle"
        self._outcome: _ProgramStatusRecord | None = None
        self._interrupted = False
        self._session_id = ""
        # Bound methods are held once so unsubscribe gets the same handlers
        # subscribe registered. The handler parameter stays ``Any`` so one
        # heterogeneous table serves every event type, exactly like the main
        # screen's subscription table.
        self._entries: tuple[tuple[type[Event], Callable[[Any], Awaitable[None]]], ...] = (
            (ExecutionChanged, self.on_execution_changed),
            (UserInterrupt, self.on_user_interrupt),
            (InvocationMessage, self.on_invocation_message),
            (Error, self.on_error),
            (ApprovalRequest, self.on_approval_request),
            (ApprovalReviewed, self.on_approval_reviewed),
            (ApprovalResponse, self.on_approval_response),
            (ApprovalCancelled, self.on_approval_cancelled),
            (QuestionToUser, self.on_question_to_user),
            (AskUserResponse, self.on_ask_user_response),
            (AskUserTimedOut, self.on_ask_user_timed_out),
            (InvocationPaused, self.on_invocation_paused),
            (InvocationResumed, self.on_invocation_resumed),
            (InvocationAborted, self.on_invocation_aborted),
            (InvocationCascadeAborted, self.on_invocation_cascade_aborted),
            (WorkflowNodeAskUser, self.on_workflow_node_ask_user),
            (WorkflowNodeAnswered, self.on_workflow_node_answered),
            (WorkflowNodeStateChanged, self.on_workflow_node_state_changed),
            (WorkflowRunFinished, self.on_workflow_run_finished),
            (SessionReady, self.on_session_ready),
            (SessionRestored, self.on_session_restored),
        )

    async def subscribe(self) -> None:
        """Register every handler, then establish the idle root record."""
        if self._subscribed:
            return
        for event_type, handler in self._entries:
            await self._bus.subscribe(event_type, handler)
        self._subscribed = True
        self._refresh()

    async def unsubscribe(self) -> None:
        """Remove every registered handler."""
        if not self._subscribed:
            return
        for event_type, handler in self._entries:
            await self._bus.unsubscribe(event_type, handler)
        self._subscribed = False

    async def close(self) -> None:
        """Report idle for a quit-in-progress, then stop reporting.

        Leaving mid-run means the user has seen the screen, so idle replaces
        any outcome; this runs while the app can still write to the terminal
        (Unmount precedes the driver close on Textual 8.2.7), and the
        unsubscribe right after keeps the shutdown's own events from
        overwriting it. A bare clear is never used: without an id it would
        erase every program's records on the terminal.
        """
        self._report(_ProgramStatusRecord("idle"))
        await self.unsubscribe()

    # ------------------------------------------------------------------ #
    # Fact updates
    # ------------------------------------------------------------------ #

    async def on_execution_changed(self, event: ExecutionChanged) -> None:
        kind = event.snapshot.kind
        if kind not in ("turn", "workflow"):
            kind = "idle"
        self._lease = kind
        if kind != "idle":
            # A new run outranks the previous run's interrupt, and its outcome
            # must not leak into it: a retry starting from a failed turn
            # reports working again until it reaches its own terminal.
            self._interrupted = False
            self._outcome = None
        self._refresh()

    async def on_user_interrupt(self, _event: UserInterrupt) -> None:
        # The user cut the run short: the spec wants idle, and any outcome
        # from before the cut no longer describes what the user is looking at.
        self._interrupted = True
        self._outcome = None
        self._refresh()

    async def on_invocation_message(self, event: InvocationMessage) -> None:
        # Only the chat turn's own final message completes the root record.
        # Sub-agent finals are fragments of the parent's working turn, and
        # workflow-node traffic belongs to the workflow's record.
        if not event.is_final:
            return
        if event.origin.kind != "turn" or event.origin.root.kind != "turn":
            return
        if self._interrupted:
            # The user already cut this run short; a final message that raced
            # the interrupt must not claim a result was delivered.
            return
        self._outcome = _ProgramStatusRecord("done", message=program_status_message(event.text))
        self._refresh()

    async def on_error(self, event: Error) -> None:
        # An error while a run is live ends it — the tab title earns its
        # cross on the same evidence. A rejected operation (a blocked
        # injection, a refused submit) leaves the running turn alone, and
        # errors outside a run (submit validation, agent load) change
        # nothing here.
        if self._lease == "idle" or event.code in _REJECTED_OPERATION_ERROR_CODES:
            return
        if self._interrupted:
            # The interrupt already settled this run as idle; a stale error
            # from the cancelled run must not override the user's cut.
            return
        self._outcome = _ProgramStatusRecord("error", message=program_status_message(event.message))
        self._refresh()

    async def on_approval_request(self, event: ApprovalRequest) -> None:
        if event.judging:
            # The judge may still approve it without the user; only a flagged
            # verdict opens a wait, via ApprovalReviewed. Remember the tool
            # name so that wait can carry it.
            self._judging[event.request_id] = program_status_message(event.tool_name)
            return
        # The middleware leaves intent_summary empty for ordinary tool
        # approvals, and the user's own prompt says nothing about this call,
        # so the tool name alone names the wait.
        self._approvals[event.request_id] = program_status_message(event.tool_name)
        self._refresh()

    async def on_approval_reviewed(self, event: ApprovalReviewed) -> None:
        tool_name = self._judging.pop(event.request_id, None)
        if event.approved:
            # Auto-fulfilment proceeds without the user.
            return
        if tool_name is None:
            # The verdict races the user's own response or a cancellation
            # that already resolved this request: it must not reopen the
            # wait, which nothing would ever answer.
            return
        self._approvals[event.request_id] = tool_name
        self._refresh()

    async def on_approval_response(self, event: ApprovalResponse) -> None:
        self._approvals.pop(event.request_id, None)
        self._judging.pop(event.request_id, None)
        self._refresh()

    async def on_approval_cancelled(self, event: ApprovalCancelled) -> None:
        self._approvals.pop(event.request_id, None)
        self._judging.pop(event.request_id, None)
        self._refresh()

    async def on_question_to_user(self, event: QuestionToUser) -> None:
        self._questions[event.request_id] = _question_message(event.questions)
        self._refresh()

    async def on_ask_user_response(self, event: AskUserResponse) -> None:
        self._questions.pop(event.request_id, None)
        self._refresh()

    async def on_ask_user_timed_out(self, event: AskUserTimedOut) -> None:
        self._questions.pop(event.request_id, None)
        self._refresh()

    async def on_invocation_paused(self, event: InvocationPaused) -> None:
        # The sub-agent's parent tool call waits with it, so the whole run
        # blocks on this decision; kind stays absent because the protocol's
        # ``question`` means the user must type an answer, which this is not.
        self._paused_agents[event.origin.invocation_id] = _pause_message(event)
        self._refresh()

    async def on_invocation_resumed(self, event: InvocationResumed) -> None:
        self._paused_agents.pop(event.origin.invocation_id, None)
        self._refresh()

    async def on_invocation_aborted(self, event: InvocationAborted) -> None:
        self._paused_agents.pop(event.origin.invocation_id, None)
        self._refresh()

    async def on_invocation_cascade_aborted(self, event: InvocationCascadeAborted) -> None:
        self._paused_agents.pop(event.origin.invocation_id, None)
        self._refresh()

    async def on_workflow_node_ask_user(self, event: WorkflowNodeAskUser) -> None:
        self._workflow_asks[event.request_id] = _WorkflowAsk(
            event.run_id, event.activation_id, event.attempt, _question_message(event.questions)
        )
        self._refresh()

    async def on_workflow_node_answered(self, event: WorkflowNodeAnswered) -> None:
        self._workflow_asks.pop(event.request_id, None)
        self._refresh()

    async def on_workflow_node_state_changed(self, event: WorkflowNodeStateChanged) -> None:
        # awaiting_retry is an activation-level wait: the retry that resolves
        # it starts a new attempt of the same activation, so the key carries
        # no attempt number.
        key = (event.run_id, event.activation_id)
        if event.state == "awaiting_retry":
            self._retry_nodes[key] = event.node_id
        else:
            self._retry_nodes.pop(key, None)
        if event.state != "running":
            # The attempt settled or was superseded: its open question dies
            # with it (a timeout cancels it without an answer event), exactly
            # where the workflow projector retires it.
            attempt_key = (event.run_id, event.activation_id, event.attempt)
            for request_id, ask in list(self._workflow_asks.items()):
                if (ask.run_id, ask.activation_id, ask.attempt) == attempt_key:
                    del self._workflow_asks[request_id]
        self._refresh()

    async def on_workflow_run_finished(self, event: WorkflowRunFinished) -> None:
        for request_id, ask in list(self._workflow_asks.items()):
            if ask.run_id == event.run_id:
                del self._workflow_asks[request_id]
        for key in list(self._retry_nodes):
            if key[0] == event.run_id:
                del self._retry_nodes[key]
        status = run_status(event.outcome, event.reason, active=False)
        if status == "completed":
            self._outcome = _ProgramStatusRecord("done")
        elif status == "failed":
            self._outcome = _ProgramStatusRecord("error", message=program_status_message(event.error))
        else:
            # A user cancellation or shutdown cut the run short: idle, like a
            # chat interrupt, rather than announcing a result.
            self._interrupted = True
            self._outcome = None
        self._refresh()

    async def on_session_ready(self, event: SessionReady) -> None:
        session_id = event.session_id or ""
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            self._forget_session_facts()
        self._refresh()

    async def on_session_restored(self, event: SessionRestored) -> None:
        self._session_id = event.session_id or self._session_id
        self._forget_session_facts()
        self._refresh()

    def reset_context(self) -> None:
        """Acknowledge the previous view's result on a UI context switch.

        Workflow session changes and chat/workflow mode switches do not
        rebuild the chat engine or emit its session lifecycle events.
        """
        self._forget_session_facts()
        self._refresh()

    # ------------------------------------------------------------------ #
    # Derivation
    # ------------------------------------------------------------------ #

    def _derive(self) -> _ProgramStatusRecord:
        """Fold the facts into one root record.

        Wait priority is fixed — a question the user must answer outranks a
        permission, which outranks retry decisions — so which wait a report
        names never depends on event arrival order. Messages use protocol
        English; see the module docstring for the language rule.
        """
        if self._questions:
            return _ProgramStatusRecord("blocked", kind="question", message=next(iter(self._questions.values())))
        if self._workflow_asks:
            ask = next(iter(self._workflow_asks.values()))
            return _ProgramStatusRecord("blocked", kind="question", message=ask.message)
        if self._approvals:
            return _ProgramStatusRecord("blocked", kind="permission", message=next(iter(self._approvals.values())))
        if self._paused_agents:
            return _ProgramStatusRecord("blocked", message=next(iter(self._paused_agents.values())))
        if self._retry_nodes:
            node_id = next(iter(self._retry_nodes.values()))
            return _ProgramStatusRecord("blocked", message=program_status_message(f"{node_id} awaits manual retry"))
        # An outcome outranks the lease: the final message, a terminal error
        # and WorkflowRunFinished all land while the lease is still held, and
        # the result is already readable then. An interrupt outranks both:
        # the user cut the run short, so what it left behind no longer shows.
        if self._outcome is not None:
            return self._outcome
        if self._interrupted:
            return _ProgramStatusRecord("idle")
        if self._lease != "idle":
            return _ProgramStatusRecord("working")
        return _ProgramStatusRecord("idle")

    def _forget_session_facts(self) -> None:
        """Drop the previous session's waits and outcome on a session switch."""
        self._approvals.clear()
        self._judging.clear()
        self._questions.clear()
        self._paused_agents.clear()
        self._workflow_asks.clear()
        self._retry_nodes.clear()
        self._outcome = None
        self._interrupted = False

    def _refresh(self) -> None:
        self._report(self._derive())

    def _report(self, record: _ProgramStatusRecord) -> None:
        if record == self._last:
            return
        if emit_program_status_report(self._app_provider(), record.state, kind=record.kind, message=record.message):
            # Only a write that reached the driver counts as sent; a failed
            # write leaves the record unrecorded so the next report resends.
            self._last = record
