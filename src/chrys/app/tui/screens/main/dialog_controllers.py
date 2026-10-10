# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Dialog queue controllers for the main TUI screen."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any, ClassVar, Protocol

from chrys.app.tui.screens.dialogs.agent_load import load_count_failed_message, map_load_progress_prose
from chrys.app.tui.screens.main.ports import StatusMessage
from chrys.app.tui.widgets import PromptDraft
from chrys.foundation.config.settings import DEFAULT_APPROVAL_TIMEOUT_SECONDS
from chrys.foundation.events.types import (
    AGENT_LOAD_PHASE_SESSION,
    AGENT_LOAD_STATUS_DONE,
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    ApprovalCancelled,
    ApprovalRequest,
    ApprovalReviewed,
    AskUserTimedOut,
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
    QuestionToUser,
)
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.approval_reuse import ReuseChoice
from chrys.foundation.models.ask_user import AskUserAnswer

_LOAD_TITLE_INITIALIZING = msg("tui.agent_load.title.initializing", fallback="Initializing Agent")
_LOAD_TITLE_SWITCHING = msg("tui.agent_load.title.switching", fallback="Switching Agent")
_LOAD_TITLE_RELOADING = msg("tui.agent_load.title.reloading", fallback="Reloading Agent")
_LOAD_TITLE_WORKSPACE = msg("tui.agent_load.title.workspace", fallback="Loading Workspace")
_LOAD_TITLE_STARTING_SESSION = msg("tui.agent_load.title.starting_session", fallback="Starting Session")
_LOAD_TITLE_RESTORING_SESSION = msg("tui.agent_load.title.restoring_session", fallback="Restoring Session")
_LOAD_TITLE_RESETTING_SESSION = msg("tui.agent_load.title.resetting_session", fallback="Resetting Session")
_LOAD_TITLE_LOADING = msg("tui.agent_load.title.loading", fallback="Loading Agent")

_LOAD_CHECKING_SESSION = msg(
    "tui.agent_load.flow.checking_session_availability",
    fallback="Checking session availability",
)
_LOAD_SESSION_CHECKED = msg(
    "tui.agent_load.flow.session_availability_checked",
    fallback="Session availability checked",
)
_LOAD_PREPARING_AGENT = msg("tui.agent_load.flow.preparing_agent", fallback="Preparing agent")
_LOAD_LOADING_AGENT = msg("tui.agent_load.flow.loading_agent", fallback="Loading agent")
_LOAD_RESTORING_HISTORY = msg(
    "tui.agent_load.flow.restoring_session_history",
    fallback="Restoring session history",
)
_LOAD_PREPARING_SESSION = msg("tui.agent_load.flow.preparing_session", fallback="Preparing session")
_LOAD_APPLYING_CHANGES = msg("tui.agent_load.flow.applying_agent_changes", fallback="Applying agent changes")
_LOAD_RESTORING_HISTORY_PERCENT = msg(
    "tui.agent_load.flow.restoring_session_history_percent",
    fallback="Restoring session history ({percent}%)",
)
_LOAD_FAILED = msg("tui.agent_load.flow.failed", fallback="Agent failed to load.")
_IMAGE_COMPRESSION_TITLE = msg(
    "tui.image_compression.title",
    fallback="Preparing Image",
    plural_fallback="Preparing Images",
)
_APPROVAL_AUTO_APPROVED_JUDGE = msg(
    "tui.approval.judge.auto_approved",
    fallback="auto-approved, judge",
)
_APPROVAL_FLAGGED = msg("tui.approval.judge.flagged", fallback="flagged: {reason}")

_LOAD_TITLES = {
    "startup": _LOAD_TITLE_INITIALIZING,
    "switch": _LOAD_TITLE_SWITCHING,
    "settings_reload": _LOAD_TITLE_RELOADING,
    "workspace_change": _LOAD_TITLE_WORKSPACE,
    "new_session": _LOAD_TITLE_STARTING_SESSION,
    "restore": _LOAD_TITLE_RESTORING_SESSION,
    "reset": _LOAD_TITLE_RESETTING_SESSION,
}


@dataclass(frozen=True)
class ApprovalBypassDecision:
    """Immediate approval response produced by a dialog body builder."""

    approved: bool
    reason: str = ""
    debug_reason: str = ""


class ApprovalResponseWorker(Protocol):
    """Worker handle returned by the approval response publisher."""

    async def wait(self) -> object: ...


class ApprovalTimer(Protocol):
    """A pending human approval deadline."""

    def stop(self) -> None: ...


class ApprovalDialogHandle(Protocol):
    """Small approval-dialog surface used by the controller."""

    @property
    def user_decision_submitted(self) -> bool: ...

    remember_choice: ReuseChoice

    def when_ready(self, callback: Callable[[], None]) -> None: ...

    @property
    def is_dismissed(self) -> bool: ...


class ApprovalDialogPort(Protocol):
    """Textual side effects required by :class:`ApprovalQueueController`."""

    async def build_approval_body(self, event: ApprovalRequest) -> object | None: ...

    def approval_body_bypass(self, body: object | None) -> ApprovalBypassDecision | None: ...

    def show_approval_dialog(
        self,
        event: ApprovalRequest,
        approval_body: object | None,
        on_result: Callable[[tuple[bool, str, dict[str, Any] | None] | None], None],
        *,
        verdict: ApprovalReviewed | None,
    ) -> ApprovalDialogHandle: ...

    def deliver_approval_verdict(self, dialog: ApprovalDialogHandle, event: ApprovalReviewed) -> None: ...

    def dismiss_approval_dialog(self, dialog: ApprovalDialogHandle) -> None: ...

    def approval_dialog_tool_name(self, dialog: ApprovalDialogHandle) -> str: ...

    def approval_defer_while_judging(self) -> bool: ...

    def set_auto_review_count(self, count: int) -> None: ...

    def debug(self, key: str, message: str = "") -> None: ...

    def notify_approval_required(self) -> None: ...

    def update_tool_args(self, call_id: str, args: dict[str, Any]) -> None: ...

    def handle_approval_response(
        self,
        request_id: str,
        approved: bool,
        reason: str,
        modified_args: dict[str, Any] | None = None,
        remember_choice: ReuseChoice = "",
    ) -> ApprovalResponseWorker | None: ...

    def run_worker(self, awaitable: Awaitable[Any], *, group: str) -> None: ...

    def set_timer(self, delay: float, callback: Callable[[], None]) -> ApprovalTimer: ...

    async def publish_auto_fulfill_blocked(self, event: ApprovalReviewed) -> None: ...


class ApprovalQueueController:
    """Own the approval dialog queue and AUTO judge race state.

    With ``approval_defer_while_judging`` on (read once per request), a request
    the judge is reviewing waits in ``deferred`` instead of the queue: an
    approval drops it unseen, a flag queues it with its verdict, a cancel
    drops it. The header counts what waits there.
    """

    def __init__(
        self,
        port: ApprovalDialogPort,
        *,
        render_message: Callable[[MessageRef], str] = format_message,
        timeout_seconds: Callable[[], float] = lambda: DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._timeouts: dict[str, ApprovalTimer] = {}
        self._closed = False
        self._port = port
        self._render_message = render_message
        self.queue: deque[ApprovalRequest] = deque()
        self.request_lock = asyncio.Lock()
        self.dialog_open = False
        self.bodies: dict[str, object] = {}
        self.open_dialogs: dict[str, ApprovalDialogHandle] = {}
        self.pending_verdicts: dict[str, ApprovalReviewed] = {}
        self.dismissed_requests: set[str] = set()
        self.reviewed_dismissed_requests: set[str] = set()
        self.cancelled_requests: set[str] = set()
        self.deferred: dict[str, ApprovalRequest] = {}

    def close(self) -> None:
        """Stop UI-owned timers when the main screen is removed."""
        self._closed = True
        for timer in self._timeouts.values():
            timer.stop()
        self._timeouts.clear()

    def _cancel_timeout(self, request_id: str) -> None:
        timer = self._timeouts.pop(request_id, None)
        if timer is not None:
            timer.stop()

    def _start_timeout(self, request_id: str) -> None:
        if self._closed or self._timeout_seconds() == 0:
            return
        dialog = self.open_dialogs.get(request_id)
        if dialog is not None:
            dialog.when_ready(lambda: self._arm_timeout(request_id))

    def _arm_timeout(self, request_id: str) -> None:
        dialog = self.open_dialogs.get(request_id)
        if dialog is None or dialog.is_dismissed or dialog.user_decision_submitted:
            return
        if self._closed or request_id in self._timeouts:
            return
        seconds = self._timeout_seconds()
        # Zero disables the deadline; user responses and cancellation still work.
        if seconds == 0:
            return
        # The screen owns the timer so the expiry runs as the screen. This runs
        # in the dialog's ready callback: an event-loop timer would keep the
        # dialog as Textual's active pump, show_next() would push the next
        # dialog on its behalf, and that dialog's answer would go to this
        # closed dialog and be lost.
        self._timeouts[request_id] = self._port.set_timer(seconds, partial(self._on_timeout, request_id))

    def _on_timeout(self, request_id: str) -> None:
        self._cancel_timeout(request_id)
        dialog = self.open_dialogs.get(request_id)
        if self._closed or dialog is None or dialog.is_dismissed or dialog.user_decision_submitted:
            return
        self.open_dialogs.pop(request_id)
        # Publish the rejection even if another modal covers this one. Its
        # eventual dismissal callback must not publish a second response.
        self.cancelled_requests.add(request_id)
        self._port.dismiss_approval_dialog(dialog)
        self._port.handle_approval_response(request_id, False, "Human approval request timed out.")
        self.dialog_open = False
        self.show_next()

    async def on_request(self, event: ApprovalRequest) -> None:
        """Queue an approval request and show it when the queue is idle."""
        async with self.request_lock:
            approval_body = await self._port.build_approval_body(event)
            bypass = self._port.approval_body_bypass(approval_body)
            if bypass is not None:
                self._port.handle_approval_response(event.request_id, bypass.approved, bypass.reason)
                suffix = f": {bypass.debug_reason}" if bypass.debug_reason else ""
                self._port.debug("ApprovalRequest", f"{event.tool_name} (skipped dialog{suffix})")
                return

            if approval_body is not None:
                self.bodies[event.request_id] = approval_body
            if event.judging and self._port.approval_defer_while_judging():
                # The producer publishes inline and starts its judge only after
                # this returns, so the verdict always finds the entry.
                self.deferred[event.request_id] = event
                self._sync_review_count()
                self._port.debug("ApprovalRequest", f"{event.tool_name} (deferred while judging)")
                return
            self.queue.append(event)
            if not self.dialog_open:
                self.show_next()

    def _sync_review_count(self) -> None:
        self._port.set_auto_review_count(len(self.deferred))

    def show_next(self) -> None:
        """Show the next queued approval, draining cached AUTO verdicts."""
        while self.queue:
            event = self.queue.popleft()
            approval_body = self.bodies.pop(event.request_id, None)
            cached = self.pending_verdicts.pop(event.request_id, None)

            if cached is not None and cached.approved:
                self._port.debug("ApprovalJudge", f"{event.tool_name} (auto-approved pre-mount)")
                continue

            self.dialog_open = True

            def _on_result(
                result: tuple[bool, str, dict[str, Any] | None] | None,
                _req: str = event.request_id,
                _call_id: str = event.call_id,
                _args: dict[str, Any] = event.args,
                _judging: bool = event.judging,
            ) -> None:
                self._cancel_timeout(_req)
                dialog = self.open_dialogs.pop(_req, None)
                if _req in self.cancelled_requests:
                    self.cancelled_requests.discard(_req)
                    return
                if result is None:
                    return
                track_user_decision = False
                if _judging and dialog is not None and dialog.user_decision_submitted:
                    if _req in self.reviewed_dismissed_requests:
                        self.reviewed_dismissed_requests.discard(_req)
                    else:
                        self.dismissed_requests.add(_req)
                        track_user_decision = True
                approved, reason, modified_args = result
                if approved and modified_args and _call_id:
                    self._port.update_tool_args(_call_id, {**_args, **modified_args})
                remember_choice = dialog.remember_choice if approved and dialog is not None else ""
                response_worker = self._port.handle_approval_response(
                    _req, approved, reason, modified_args, remember_choice
                )
                if track_user_decision:

                    async def _drain_marker(
                        _request_id: str = _req,
                        _worker: ApprovalResponseWorker | None = response_worker,
                    ) -> None:
                        if _worker is not None:
                            with contextlib.suppress(Exception):
                                await _worker.wait()
                        self.dismissed_requests.discard(_request_id)

                    self._port.run_worker(_drain_marker(), group="approval-cleanup")
                self.show_next()

            # A flagged verdict that arrived first opens the dialog flagged.
            dialog = self._port.show_approval_dialog(event, approval_body, _on_result, verdict=cached)
            self.open_dialogs[event.request_id] = dialog
            if not event.judging or cached is not None:
                self._start_timeout(event.request_id)
            if not event.judging:
                self._port.notify_approval_required()

            self._port.debug(
                "ApprovalRequest",
                f"{event.tool_name}" + (f" ({event.caller_name})" if event.caller_name else ""),
            )

            if cached is not None:
                self._port.debug(
                    "ApprovalJudge",
                    f"{event.tool_name} (flagged pre-mount: {cached.reason[:60]})",
                )
                self._port.notify_approval_required()
            return

        self.dialog_open = False

    async def on_cancelled(self, event: ApprovalCancelled) -> None:
        """Dismiss or dequeue an abandoned request without publishing a response."""
        self._cancel_timeout(event.request_id)
        async with self.request_lock:
            if self.deferred.pop(event.request_id, None) is not None:
                self.bodies.pop(event.request_id, None)
                self._sync_review_count()
                self._port.debug("ApprovalCancelled", f"{event.request_id} (deferred)")
                return
            queued_before = len(self.queue)
            self.queue = deque(request for request in self.queue if request.request_id != event.request_id)
            self.bodies.pop(event.request_id, None)
            self.pending_verdicts.pop(event.request_id, None)
            self.dismissed_requests.discard(event.request_id)
            self.reviewed_dismissed_requests.discard(event.request_id)
            if len(self.queue) != queued_before:
                self._port.debug("ApprovalCancelled", f"{event.request_id} (queued)")
                return

            dialog = self.open_dialogs.pop(event.request_id, None)
            if dialog is None:
                return
            # Set the marker before dismissing. Textual normally schedules the
            # result callback, but test doubles and future ports may invoke it
            # synchronously.
            self.cancelled_requests.add(event.request_id)
            self._port.dismiss_approval_dialog(dialog)
            self.dialog_open = False
            self.show_next()
            self._port.debug("ApprovalCancelled", event.request_id)

    async def on_reviewed(self, event: ApprovalReviewed) -> None:
        """Deliver or cache an AUTO judge verdict."""
        if event.request_id in self.cancelled_requests:
            return
        label = self._render_message(
            _APPROVAL_AUTO_APPROVED_JUDGE.bind() if event.approved else _APPROVAL_FLAGGED.bind(reason=event.reason[:60])
        )

        # Synchronous to the end: on_request's critical section never sees a
        # half-moved entry.
        deferred = self.deferred.pop(event.request_id, None)
        if deferred is not None:
            self._sync_review_count()
            if event.approved:
                # The backend runs the call; the user never needed to see it.
                self.bodies.pop(event.request_id, None)
                self._port.debug("ApprovalJudge", f"{deferred.tool_name} ({label}, never shown)")
                return
            self.pending_verdicts[event.request_id] = event
            self.queue.append(deferred)
            if not self.dialog_open:
                self.show_next()
            return

        dialog = self.open_dialogs.get(event.request_id)
        user_decision_in_flight = (
            dialog is not None and dialog.user_decision_submitted
        ) or event.request_id in self.dismissed_requests
        if user_decision_in_flight:
            self.dismissed_requests.discard(event.request_id)
            if dialog is not None:
                self.reviewed_dismissed_requests.add(event.request_id)
            if event.approved:
                await self._port.publish_auto_fulfill_blocked(event)
            return

        if dialog is not None and not dialog.is_dismissed:
            self._port.deliver_approval_verdict(dialog, event)
            if event.approved:
                self._cancel_timeout(event.request_id)
            else:
                self._start_timeout(event.request_id)
            self._port.debug("ApprovalJudge", f"{self._port.approval_dialog_tool_name(dialog)} ({label})")
            if not event.approved:
                self._port.notify_approval_required()
            return

        for queued in self.queue:
            if queued.request_id == event.request_id:
                self.pending_verdicts[event.request_id] = event
                self._port.debug("ApprovalJudge", f"{queued.tool_name} ({label}, cached)")
                return


@dataclass(frozen=True)
class InlineQuestionDialogResult:
    """Parsed dialog result requesting inline ask_user rendering."""

    draft: PromptDraft


@dataclass(frozen=True)
class AnswerQuestionDialogResult:
    """Parsed dialog result containing the user's answer."""

    request_id: str
    answers: tuple[AskUserAnswer, ...]


type QuestionDialogResult = InlineQuestionDialogResult | AnswerQuestionDialogResult | None


class QuestionDialogHandle(Protocol):
    """Small ask-user dialog surface used by the controller."""

    def dismiss_due_to_timeout(self) -> None: ...


class QuestionDialogPort(Protocol):
    """Textual side effects required by :class:`QuestionQueueController`."""

    def show_question_dialog(
        self,
        event: QuestionToUser,
        draft: PromptDraft | None,
        on_result: Callable[[object], None],
    ) -> QuestionDialogHandle: ...

    def parse_question_dialog_result(self, result: object) -> QuestionDialogResult: ...

    def show_question_inline(self, event: QuestionToUser, draft: PromptDraft | None = None) -> bool: ...

    def clear_question_inline(self, call_id: str, request_id: str) -> bool: ...

    def question_can_reopen_modal(self, event: QuestionToUser) -> bool: ...

    def question_inline_preferred(self) -> bool: ...

    def handle_ask_user_response(self, request_id: str, answers: tuple[AskUserAnswer, ...]) -> None: ...

    def debug(self, key: str, message: str = "") -> None: ...

    def notify_ask_user(self) -> None: ...

    def focus_input(self) -> None: ...


class QuestionQueueController:
    """Own the ask_user modal queue and inline handoff state."""

    def __init__(self, port: QuestionDialogPort) -> None:
        self._port = port
        self.queue: deque[QuestionToUser] = deque()
        self.dialog_open = False
        self.open_dialogs: dict[str, QuestionDialogHandle] = {}
        self.inline_call_ids: dict[str, str] = {}
        self.inline_request_ids: dict[str, str] = {}
        self.drafts: dict[str, PromptDraft] = {}

    async def on_question(self, event: QuestionToUser) -> None:
        self.queue.append(event)
        if not self.dialog_open:
            self.show_next()

    def clear_pending(self) -> None:
        """Drop all pending ask_user UI state."""
        self.queue.clear()
        dialogs = list(self.open_dialogs.values())
        self.open_dialogs.clear()
        self.inline_call_ids.clear()
        self.inline_request_ids.clear()
        self.drafts.clear()
        self.dialog_open = False
        for dialog in dialogs:
            dialog.dismiss_due_to_timeout()

    def show_next(self) -> None:
        """Show the next queued question."""
        if not self.queue:
            self.dialog_open = False
            return

        self.dialog_open = True
        event = self.queue.popleft()
        draft = self.drafts.pop(event.request_id, None)

        # The setting asks for the transcript first; the modal stays the
        # fallback when the tool card cannot host the prompt (no call id, or
        # the card is not mounted), so a question is never silently dropped.
        if self._port.question_inline_preferred() and self._show_inline(event, draft):
            self._port.notify_ask_user()
            self._debug_question(event)
            self.show_next()
            return

        def _on_result(result: object, _req: str = event.request_id, _event: QuestionToUser = event) -> None:
            self.open_dialogs.pop(_req, None)
            parsed = self._port.parse_question_dialog_result(result)
            if parsed is None:
                self.show_next()
                return
            if isinstance(parsed, InlineQuestionDialogResult):
                if not self._show_inline(_event, parsed.draft) and self._port.question_can_reopen_modal(_event):
                    self.drafts[_event.request_id] = parsed.draft
                    self.queue.appendleft(_event)
                self.show_next()
                return
            self._port.handle_ask_user_response(parsed.request_id, parsed.answers)
            self.show_next()

        dialog = self._port.show_question_dialog(event, draft, _on_result)
        self.open_dialogs[event.request_id] = dialog
        self._port.notify_ask_user()
        self._debug_question(event)

    def _show_inline(self, event: QuestionToUser, draft: PromptDraft | None) -> bool:
        """Hand a question to its tool card; on success remember the pairing."""
        if not self._port.show_question_inline(event, draft):
            return False
        self.inline_call_ids[event.request_id] = event.call_id
        self.inline_request_ids[event.call_id] = event.request_id
        self.drafts.pop(event.request_id, None)
        return True

    def _debug_question(self, event: QuestionToUser) -> None:
        first = event.questions[0].question if event.questions else ""
        self._port.debug(
            "QuestionToUser",
            f"{first[:60]}" + (f" ({event.caller_name})" if event.caller_name else ""),
        )

    async def on_timed_out(self, event: AskUserTimedOut) -> None:
        """Dismiss or dequeue an ask_user dialog after backend timeout."""
        queued_count = len(self.queue)
        self.queue = deque(q for q in self.queue if q.request_id != event.request_id)
        self.drafts.pop(event.request_id, None)
        if len(self.queue) != queued_count:
            self._port.debug("AskUserTimedOut", f"{event.request_id} (queued)")
            return

        if call_id := self.inline_call_ids.pop(event.request_id, ""):
            if self.inline_request_ids.get(call_id) == event.request_id:
                self._port.clear_question_inline(call_id, event.request_id)
                self.inline_request_ids.pop(call_id, None)
            self.drafts.pop(event.request_id, None)
            if not self.inline_request_ids:
                self._port.focus_input()
            self._port.debug("AskUserTimedOut", f"{event.request_id} (inline)")
            return

        dialog = self.open_dialogs.get(event.request_id)
        if dialog is None:
            return
        dialog.dismiss_due_to_timeout()
        self._port.debug("AskUserTimedOut", event.request_id)

    def finish_inline_for_tool_result(self, call_id: str) -> bool:
        """Clear inline ask_user state for a completed tool call."""
        if request_id := self.inline_request_ids.pop(call_id, ""):
            self.inline_call_ids.pop(request_id, None)
            self.drafts.pop(request_id, None)
            # Another card may still be waiting for its answer; only the last
            # one hands the keyboard back to the composer.
            if not self.inline_request_ids:
                self._port.focus_input()
            return True
        return False


class AgentLoadDialogHandle(Protocol):
    """Small agent-load dialog surface used by the controller."""

    COMPLETE_STEP_HOLD_SECONDS: ClassVar[float]

    def update_progress(
        self,
        message: StatusMessage,
        *,
        subtitle: str = "",
        phase: str = "",
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
    ) -> bool: ...

    def update_finish_progress(self, message: StatusMessage) -> None: ...

    def finish(self, message: StatusMessage = "") -> None: ...

    def request_dismiss(self) -> None: ...

    def set_result(self, success: bool, message: StatusMessage, *, allow_esc: bool = False) -> None: ...


class AgentLoadDialogPort(Protocol):
    """Textual side effects required by :class:`AgentLoadDialogController`."""

    def create_agent_load_dialog(self, *, title: StatusMessage, subtitle: str) -> AgentLoadDialogHandle: ...

    def prepare_agent_load_ui(
        self,
        *,
        title: StatusMessage,
        session_id: str | None,
        update_clipboard_dir: bool,
        capture_status_snapshot: bool,
    ) -> dict | None: ...

    async def push_agent_load_dialog(self, dialog: AgentLoadDialogHandle) -> None: ...

    def set_agent_loading(self, value: bool) -> None: ...

    def restore_agent_load_status(self, snapshot: dict) -> None: ...

    def show_load_status(self, message: StatusMessage) -> None: ...

    def render_status_message(self, message: StatusMessage) -> str: ...

    def flash_agent_load_failed(self, message: str) -> None: ...

    def debug(self, key: str, message: str = "") -> None: ...


class AgentLoadDialogController:
    """Own the agent-load dialog handle and status snapshot."""

    def __init__(self, port: AgentLoadDialogPort) -> None:
        self._port = port
        self.dialog: AgentLoadDialogHandle | None = None
        self.status_snapshot: dict | None = None

    @staticmethod
    def load_title(operation: str) -> MessageRef:
        return _LOAD_TITLES.get(operation, _LOAD_TITLE_LOADING).bind()

    @staticmethod
    def format_load_count(current: int, total: int, failed: int = 0) -> str:
        if total <= 0:
            return "-"
        if failed:
            return f"{current}/{total}, failed: {failed}"
        return f"{current}/{total}"

    def _render_load_count(self, current: int, total: int, failed: int = 0) -> str:
        if failed and total > 0:
            return self._port.render_status_message(load_count_failed_message(current, total, failed))
        return self.format_load_count(current, total, failed)

    async def show_dialog(
        self,
        *,
        title: StatusMessage,
        subtitle: str,
        session_id: str | None,
        initial_message: StatusMessage,
        initial_phase: str = "",
        update_clipboard_dir: bool = True,
    ) -> None:
        dialog = self.dialog
        created = dialog is None
        if dialog is None:
            dialog = self._port.create_agent_load_dialog(title=title, subtitle=subtitle)
            self.dialog = dialog
        dialog.update_progress(initial_message, subtitle=subtitle, phase=initial_phase)

        snapshot = self._port.prepare_agent_load_ui(
            title=title,
            session_id=session_id,
            update_clipboard_dir=update_clipboard_dir,
            capture_status_snapshot=created and self.status_snapshot is None,
        )
        if snapshot is not None:
            self.status_snapshot = snapshot

        if created:
            await self._port.push_agent_load_dialog(dialog)

    async def begin_session_restore_load(self, session_id: str, subtitle: str) -> None:
        await self.show_dialog(
            title=self.load_title("restore"),
            subtitle=subtitle,
            session_id=session_id,
            initial_message=_LOAD_CHECKING_SESSION.bind(),
            initial_phase=AGENT_LOAD_PHASE_SESSION,
            update_clipboard_dir=False,
        )

    def cancel(self) -> None:
        dialog = self.dialog
        status_snapshot = self.status_snapshot
        self.dialog = None
        self.status_snapshot = None
        if dialog is not None:
            with contextlib.suppress(Exception):
                dialog.request_dismiss()
        self._port.set_agent_loading(False)
        if status_snapshot is not None:
            self._port.restore_agent_load_status(status_snapshot)

    async def on_started(self, event: AgentLoadStarted) -> None:
        label = event.to_display_name or event.to_profile
        title = self.load_title(event.operation)
        if event.operation == "restore" and self.dialog is not None:
            self.dialog.update_progress(
                _LOAD_SESSION_CHECKED.bind(),
                phase=AGENT_LOAD_PHASE_SESSION,
            )
        await self.show_dialog(
            title=title,
            subtitle=label,
            session_id=event.session_id,
            initial_message=_LOAD_PREPARING_AGENT.bind(),
        )
        self._port.debug("AgentLoadStarted", f"{event.operation}:{label}")

    async def on_progress(self, event: AgentLoadProgress) -> None:
        raw_message: StatusMessage = event.message or _LOAD_LOADING_AGENT.bind()
        display: StatusMessage = raw_message
        if isinstance(raw_message, str):
            # Backend prose is an English protocol; show its localized display
            # definition in the status bar and keep unknown prose verbatim.
            display = map_load_progress_prose(raw_message) or raw_message
        if event.total or event.current or event.failed or event.status == AGENT_LOAD_STATUS_DONE:
            label = self._port.render_status_message(display)
            message: StatusMessage = f"{label} ({self._render_load_count(event.current, event.total, event.failed)})"
        else:
            message = display
        completed = False
        if self.dialog is not None:
            completed = self.dialog.update_progress(
                raw_message,
                phase=event.phase,
                server_name=event.server_name,
                current=event.current,
                total=event.total,
                failed=event.failed,
                status=event.status,
            )
        self._port.show_load_status(message)
        debug_message = message if isinstance(message, str) else format_message(message)
        self._port.debug("AgentLoadProgress", f"{event.phase}:{debug_message}")
        if completed and self.dialog is not None:
            await asyncio.sleep(self.dialog.COMPLETE_STEP_HOLD_SECONDS)

    def on_finished(self, event: AgentLoadFinished) -> bool:
        """Return True when final dismissal should be deferred."""
        if event.operation == "restore":
            if self.dialog is not None:
                self.dialog.update_finish_progress(_LOAD_RESTORING_HISTORY.bind())
            self._port.debug("AgentLoadFinished", f"{event.agent_profile}:restore pending")
            return True
        if event.operation in {"startup", "new_session", "reset"}:
            if self.dialog is not None:
                self.dialog.update_finish_progress(_LOAD_PREPARING_SESSION.bind())
            self._port.debug("AgentLoadFinished", f"{event.agent_profile}:session pending")
            return True
        if event.operation in {"switch", "settings_reload", "workspace_change"}:
            if self.dialog is not None:
                self.dialog.update_finish_progress(_LOAD_APPLYING_CHANGES.bind())
            self._port.debug("AgentLoadFinished", f"{event.agent_profile}:switch pending")
            return True
        self.finish()
        self._port.debug("AgentLoadFinished", event.agent_profile)
        return False

    def update_session_history_progress(self, current: int, total: int) -> None:
        """Update the restore-only final step from transcript mount counts."""
        bounded_total = max(0, total)
        bounded_current = min(max(0, current), bounded_total)
        percent = 100 if bounded_total == 0 else bounded_current * 100 // bounded_total
        message = _LOAD_RESTORING_HISTORY_PERCENT.bind(percent=percent)
        if self.dialog is not None:
            self.dialog.update_finish_progress(message)
        self._port.show_load_status(message)

    def finish(self, message: StatusMessage = "") -> None:
        dialog = self.dialog
        self.dialog = None
        self.status_snapshot = None
        if dialog is not None:
            with contextlib.suppress(Exception):
                dialog.finish(message)
        self._port.set_agent_loading(False)

    def fail(self, message: StatusMessage) -> None:
        dialog = self.dialog
        self.dialog = None
        self.status_snapshot = None
        if dialog is not None:
            with contextlib.suppress(Exception):
                dialog.set_result(False, message or _LOAD_FAILED.bind(), allow_esc=True)
        self._port.set_agent_loading(False)

    def on_failed(self, event: AgentLoadFailed, *, display: str | None = None, summary: str | None = None) -> None:
        """Fail the load; *display* is the event's rendered display message and hint, shown above the raw text.

        The status bar has room for one line: *summary*, the rendered message
        without its hint (*display* when not given).
        """
        if display is None:
            self.fail(event.display_message or event.message or _LOAD_FAILED.bind())
            self._port.flash_agent_load_failed(event.message)
        else:
            self.fail(f"{display}\n{event.message}" if event.message else display)
            self._port.flash_agent_load_failed(display if summary is None else summary)
        self._port.debug("AgentLoadFailed", event.message[:80])


class ImageCompressionDialogHandle(Protocol):
    """Small image-compression dialog surface used by the controller."""

    def finish(self) -> None: ...


class ImageCompressionDialogPort(Protocol):
    """Textual side effects required by :class:`ImageCompressionDialogController`."""

    def create_image_compression_dialog(self, *, title: StatusMessage) -> ImageCompressionDialogHandle: ...

    async def push_image_compression_dialog(self, dialog: ImageCompressionDialogHandle) -> None: ...

    def debug(self, key: str, message: str = "") -> None: ...


class ImageCompressionDialogController:
    """Own the transient image-compression loading dialog."""

    def __init__(self, port: ImageCompressionDialogPort) -> None:
        self._port = port
        self.dialog: ImageCompressionDialogHandle | None = None

    async def on_started(self, event: ImageAttachmentCompressionStarted) -> None:
        if self.dialog is not None:
            return
        title = _IMAGE_COMPRESSION_TITLE.bind(count=event.image_count)
        dialog = self._port.create_image_compression_dialog(title=title)
        self.dialog = dialog
        await self._port.push_image_compression_dialog(dialog)
        self._port.debug("ImageCompressionStarted", str(event.image_count))

    async def on_finished(self, event: ImageAttachmentCompressionFinished) -> None:
        dialog = self.dialog
        self.dialog = None
        if dialog is not None:
            with contextlib.suppress(Exception):
                dialog.finish()
        self._port.debug("ImageCompressionFinished", str(event.image_count))
