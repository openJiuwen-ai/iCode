# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Backend event handlers — routes EventBus callbacks to TUI widgets."""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.app.tui.i18n import render_str
from chrys.app.tui.notifications import NotificationEvent
from chrys.app.tui.screens.main.dialog_controllers import (
    AgentLoadDialogController,
    ApprovalDialogHandle,
    ApprovalQueueController,
    ApprovalResponseCallback,
    ImageCompressionDialogController,
    QuestionDialogHandle,
    QuestionQueueController,
)
from chrys.app.tui.screens.main.dialog_gateway import UiGateway, UiGatewayCallbacks
from chrys.app.tui.screens.main.diff_controller import LiveDiffTracker
from chrys.app.tui.screens.main.main_screen_presenter import MainScreenPresenter
from chrys.app.tui.screens.main.ports import (
    BackendEventView,
    InputRestore,
    NotificationSeverity,
    StatusMessage,
    StatusTrail,
)
from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.support.workspace_mru import schedule_workspace_mru_touches
from chrys.app.tui.util.context_pressure import context_pressure_message
from chrys.app.tui.widgets import PromptDraft
from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotPayload, FileSnapshotRef, should_externalize_snapshot
from chrys.app.tui.widgets.chrome.app_header import APPROVAL_MODE_MESSAGES
from chrys.app.tui.widgets.chrome.input_bar import INPUT_RETRY
from chrys.app.tui.widgets.chrome.status_bar import (
    STATUS_COMPACTING,
    STATUS_ERROR,
    STATUS_STREAMING,
    STATUS_THINKING,
)
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.foundation.errors.display import DISPLAY_WITH_HINT
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    AgentRuntimeDetails,
    AgentRuntimeUpdated,
    AgentThinking,
    ApprovalAutoFulfillBlocked,
    ApprovalCancelled,
    ApprovalModeUpdated,
    ApprovalRequest,
    ApprovalReviewed,
    AskUserTimedOut,
    CompactionFinished,
    CompactionStarted,
    ContextCompressed,
    Error,
    ImageAttachmentCompressionFinished,
    ImageAttachmentCompressionStarted,
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationCompactionCommitted,
    InvocationCompactionFinished,
    InvocationCompactionStarted,
    InvocationContextPressure,
    InvocationMessage,
    InvocationPaused,
    InvocationPresentationAttemptAccepted,
    InvocationPresentationAttemptRejected,
    InvocationProgress,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationStarted,
    InvocationToolCallArgsUpdated,
    InvocationToolCallProgress,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    QuestionToUser,
    SessionReady,
    SettingsReloaded,
    TodoListUpdated,
    ToolCompacted,
    UsageUpdate,
    UserInjectResult,
    Warning,
)
from chrys.foundation.hosted_tools import HostedToolStatus, normalize_hosted_tool_status
from chrys.foundation.i18n import DisplayBlock, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.foundation.tool_result_metadata import canonical_tool_result_status
from chrys.orchestration.engine.run.attachments import replace_image_mentions_with_paths
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations.types import FileHashDiff
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS
from chrys.service.state.store import StateStore

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.foundation.models.invocations import InvocationOrigin

_IMAGE_ERROR_CODES = {"image_attachment_error", "vision_unsupported"}
_IMAGE_WARNING_CODES = {
    "image_attachment_error",
    "image_attachment_retry_unsupported",
    "image_attachment_while_running",
}
_SUBMIT_BLOCKING_WARNING_CODES = _IMAGE_WARNING_CODES | {"sub_agent_paused"}
_SUBMIT_BLOCKING_ERROR_CODES = _IMAGE_ERROR_CODES | {
    "hook_blocked",
    "not_ready",
    "prompt_admission_conflict",
    "retry_missing_user_anchor",
}
_STALE_RUN_FILTERED_ERROR_CODES = {"executor_error", "retry_missing_user_anchor"}


_SOFT_AGENT_LOAD_OPERATIONS = {"switch", "settings_reload", "workspace_change", "model_switch"}

_APPROVAL_TITLE = msg("tui.approval.title", fallback="Approval")
_APPROVAL_MODE_CHANGED = msg("tui.approval.mode_changed", fallback="Approval mode: {mode}")
_WARNING_TITLE = msg("tui.warning.title", fallback="Warning")
_UNKNOWN_ERROR = msg("tui.error.unknown", fallback="Unknown error")
_RETRY_NOT_STARTED = msg("tui.retry.not_started", fallback="Retry not started")
_AGENT_FAILED_TO_LOAD = msg("tui.agent_load.failed", fallback="Agent failed to load.")
_NON_TURN_NOTIFICATION_ERROR_CODES = {
    "hook_blocked",
    "image_attachment_error",
    "no_state_store",
    "not_ready",
    "session_in_use",
    "vision_unsupported",
}
_SESSION_FORK_ERROR_PREFIX = "session_fork_"
_SESSION_CLEAR_FAILED_CODE = "session_clear_failed"

logger = logging.getLogger(__name__)

_FINISH_SESSION_READY = msg("tui.agent_load.finish.session_ready", fallback="Session ready: {profile}")
_SESSION_IN_USE_TITLE = msg("tui.main.session_in_use.title", fallback="Session In Use")
_SESSION_IN_USE_MESSAGE = msg(
    "tui.main.session_in_use.message",
    fallback="Session Already Open\n\n{message}",
    multiline=True,
)
_SESSION_IN_USE_OK = msg("tui.main.session_in_use.ok", fallback="OK")
_HOSTED_WEB_TOOLS_OK = msg("tui.main.hosted_web_tools.ok", fallback="OK")
_IMAGE_NOT_ATTACHED = msg("tui.vision_unsupported.title.not_attached", fallback="Image Not Attached")


def _chat_sub_agent(origin: InvocationOrigin) -> bool:
    """A sub-agent with a card in the chat transcript: a workflow node's children have none."""
    return origin.kind == "sub_agent" and origin.root.kind != "workflow_node"


@dataclass(frozen=True, slots=True)
class _WarningDedupeKey:
    """Session-scoped identity for one screen-lifetime warning toast."""

    session_id: str | None
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class BackendEventCallbacks:
    """Screen-owned effects required by backend event handlers."""

    set_agent_running: Callable[[bool], None]
    set_agent_loading: Callable[[bool], None]
    set_has_messages: Callable[[bool], None]
    set_profile_display: Callable[[str], None]
    set_runtime_details: Callable[[AgentRuntimeDetails], None]
    set_active_model_profile_id: Callable[[str], None]
    set_main_usage_source_id: Callable[[str], None]
    set_last_usage_tokens: Callable[[int], None]
    set_last_total_session_tokens: Callable[[int], None]
    set_creating_new_session: Callable[[bool], None]
    set_restoring_session: Callable[[bool], None]
    set_workspace_cwd: Callable[[str], None]
    set_workspace_original_cwd: Callable[[str | None], None]
    refresh_git_branch: Callable[[], None]
    update_subtitle: Callable[[], None]
    update_toc: Callable[[], None]
    on_session_fork_error: Callable[[Error, str, NotificationSeverity], None]
    on_session_clear_error: Callable[[Error, str], None]
    block_pending_user_submit: Callable[[], None]
    handle_approval_response: ApprovalResponseCallback
    handle_ask_user_response: Callable[[str, tuple[AskUserAnswer, ...]], object]
    question_inline_preferred: Callable[[], bool]
    post_gc_message: Callable[[GcAbsorbRequested | GcReclaimRequested], object]
    debug: Callable[[str, str], None]
    refresh_model_indicator: Callable[[], None]
    refresh_notification_settings: Callable[[], None]
    refresh_trajectory_verify_commands: Callable[[], None]
    settings_reloaded: Callable[[], None]
    accept_approval_update: Callable[[ApprovalModeUpdated], bool] = lambda _event: True
    route_session_error: Callable[[Error], bool] = lambda _event: False


def _usage_source_debug_suffix(source_id: str, *, main_source_id: str) -> str:
    """Return a short source marker for non-main usage debug entries."""
    if not source_id or source_id == main_source_id:
        return ""
    short_source = source_id if len(source_id) <= 12 else f"{source_id[:6]}…{source_id[-4:]}"
    return f" source={short_source}"


def _metadata_hashes(value: object) -> FileHashDiff:
    """Read before/after hashes from event metadata."""
    if isinstance(value, FileHashDiff):
        return value
    if value is not None:
        logger.debug("dropping unexpected file_mutation_hashes payload: %r", value)
    return FileHashDiff(before=None, after=None)


def _chat_file_snapshot_payload(
    snapshot: object,
    hashes: FileHashDiff,
    state_store: StateStore | None,
    session_id: str,
) -> FileSnapshotPayload | None:
    """Return the snapshot payload retained by chat widgets for a file tool."""
    if not isinstance(snapshot, tuple) or len(snapshot) != 2:
        return None
    before, after = snapshot
    if not isinstance(before, str) or not isinstance(after, str):
        return None
    text_snapshot = (before, after)
    if not should_externalize_snapshot(text_snapshot):
        return text_snapshot
    if state_store is None or not session_id or (hashes.before is None and hashes.after is None):
        return text_snapshot
    try:
        from chrys.service.mutations.store import SnapshotStore

        mutations_dir = SnapshotStore(state_store.session_dir(session_id)).mutations_dir
    except Exception:
        return text_snapshot
    return FileSnapshotRef(mutations_dir=mutations_dir, before_hash=hashes.before, after_hash=hashes.after)


class BackendEventHandler:
    """Handles all EventBus backend → TUI event routing.

    Constructed once by the main-screen owner and receives a semantic host
    for shared state, dialog controllers, and presenter-mediated UI.
    """

    def __init__(
        self,
        *,
        state: MainScreenState,
        services: MainScreenServices,
        view: BackendEventView,
        callbacks: BackendEventCallbacks,
        runtime_info: RegistryRuntimeInfoProvider | None = None,
        live_diff: LiveDiffTracker | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._callbacks = callbacks
        self._locale_controller = locale_controller
        self._dialog_gateway = UiGateway(
            view,
            UiGatewayCallbacks(
                debug=callbacks.debug,
                handle_approval_response=callbacks.handle_approval_response,
                publish_auto_fulfill_blocked=self._publish_auto_fulfill_blocked,
                handle_ask_user_response=callbacks.handle_ask_user_response,
                question_inline_preferred=callbacks.question_inline_preferred,
                set_agent_loading=self.set_agent_loading,
            ),
        )
        self._approval_controller = ApprovalQueueController(
            self._dialog_gateway,
            render_message=self._render_display,
        )
        self._question_controller = QuestionQueueController(self._dialog_gateway)
        self._agent_load_controller = AgentLoadDialogController(self._dialog_gateway)
        self._image_compression_controller = ImageCompressionDialogController(self._dialog_gateway)
        self._presenter = MainScreenPresenter(view, state)
        self._runtime_info = runtime_info or RegistryRuntimeInfoProvider(services)
        self._live_diff = live_diff or LiveDiffTracker()
        self._seen_warnings: set[_WarningDedupeKey] = set()

    def _render_display(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def _render_display_with_hint(self, message: MessageRef, hint: MessageRef | None) -> str:
        """Render an error's display message and its hint in the current locale, joined as that locale joins them."""
        rendered = self._render_display(message)
        if hint is None:
            return rendered
        return self._render_display(DISPLAY_WITH_HINT.bind(message=rendered, hint=self._render_display(hint)))

    def _gateway(self) -> UiGateway:
        """Return the Textual dialog gateway."""
        return self._dialog_gateway

    def _ui(self) -> MainScreenPresenter:
        """Return the screen presenter."""
        return self._presenter

    def _approval(self) -> ApprovalQueueController:
        return self._approval_controller

    def _questions(self) -> QuestionQueueController:
        return self._question_controller

    def _agent_load(self) -> AgentLoadDialogController:
        return self._agent_load_controller

    def _image_compression(self) -> ImageCompressionDialogController:
        return self._image_compression_controller

    def _predates_active_run(self, event: InvocationMessage | Error) -> bool:
        """Return whether *event* was sourced before the current UI run."""
        started_at = self._state.run.started_at
        return self._state.run.agent_running and started_at is not None and event.timestamp < started_at

    @property
    def state_store(self) -> StateStore | None:
        return self._services.state_store

    @property
    def agent_running(self) -> bool:
        return self._state.run.agent_running

    def set_agent_running(self, running: bool) -> None:
        self._state.run.agent_running = running
        self._callbacks.set_agent_running(running)

    @property
    def agent_loading(self) -> bool:
        return self._state.run.agent_loading

    def set_agent_loading(self, loading: bool) -> None:
        self._state.run.agent_loading = loading
        self._callbacks.set_agent_loading(loading)

    @property
    def has_messages(self) -> bool:
        return self._state.run.has_messages

    def set_has_messages(self, has_messages: bool) -> None:
        self._state.run.has_messages = has_messages
        self._callbacks.set_has_messages(has_messages)

    @property
    def profile(self) -> str:
        return self._state.runtime.profile

    @profile.setter
    def profile(self, value: str) -> None:
        self._state.runtime.profile = value
        self._callbacks.set_profile_display(value)

    @property
    def runtime_metadata(self) -> AgentRuntimeDetails:
        return self._state.runtime.details

    @runtime_metadata.setter
    def runtime_metadata(self, value: AgentRuntimeDetails) -> None:
        self._state.runtime.details = value
        self._callbacks.set_runtime_details(value)

    @property
    def approval_mode(self) -> ApprovalMode:
        return self._state.runtime.approval_mode

    @approval_mode.setter
    def approval_mode(self, value: ApprovalMode) -> None:
        self._state.runtime.approval_mode = value

    @property
    def main_usage_source_id(self) -> str:
        return self._state.runtime.main_usage_source_id

    @main_usage_source_id.setter
    def main_usage_source_id(self, value: str) -> None:
        self._state.runtime.main_usage_source_id = value
        self._callbacks.set_main_usage_source_id(value)

    @property
    def creating_new_session(self) -> bool:
        return self._state.session.creating_new_session

    @creating_new_session.setter
    def creating_new_session(self, value: bool) -> None:
        self._state.session.creating_new_session = value
        self._callbacks.set_creating_new_session(value)

    @property
    def restoring_session(self) -> bool:
        return self._state.session.restoring_session

    @restoring_session.setter
    def restoring_session(self, value: bool) -> None:
        self._state.session.restoring_session = value
        self._callbacks.set_restoring_session(value)

    @property
    def last_usage_tokens(self) -> int:
        return self._state.usage.last_usage_tokens

    @last_usage_tokens.setter
    def last_usage_tokens(self, value: int) -> None:
        self._state.usage.last_usage_tokens = value
        self._callbacks.set_last_usage_tokens(value)

    @property
    def last_total_session_tokens(self) -> int:
        return self._state.usage.last_total_session_tokens

    @last_total_session_tokens.setter
    def last_total_session_tokens(self, value: int) -> None:
        self._state.usage.last_total_session_tokens = value
        self._callbacks.set_last_total_session_tokens(value)

    @property
    def context_usage_state(self) -> ContextUsageState | None:
        return self._view.context_usage_state

    @property
    def chdir_current_cwd(self) -> str:
        return self._state.workspace_marker.current_cwd

    @chdir_current_cwd.setter
    def chdir_current_cwd(self, value: str) -> None:
        self._state.workspace.current_cwd = value
        self._state.workspace_marker.current_cwd = value
        self._callbacks.set_workspace_cwd(value)

    @property
    def chdir_original_cwd(self) -> str | None:
        return self._state.workspace_marker.original_cwd

    @chdir_original_cwd.setter
    def chdir_original_cwd(self, value: str | None) -> None:
        self._state.workspace_marker.original_cwd = value
        self._callbacks.set_workspace_original_cwd(value)

    @property
    def pending_user_submit_active(self) -> bool:
        return self._state.submit.active

    @property
    def pending_user_submit_text(self) -> str:
        return self._state.submit.text

    @property
    def pending_user_message_render_active(self) -> bool:
        return self._state.render_gate.active

    def defer_agent_message(self, event: InvocationMessage) -> None:
        self._state.render_gate.defer(event)

    def defer_error(self, event: Error) -> None:
        self._state.render_gate.defer(event)

    def block_pending_user_submit(self) -> None:
        self._state.submit.block()
        self._callbacks.block_pending_user_submit()

    def notify(
        self,
        message: StatusMessage,
        *,
        title: StatusMessage,
        severity: NotificationSeverity = "information",
        timeout: float | None = 3,
    ) -> None:
        self._view.notify(message, title=title, severity=severity, timeout=timeout)

    def push_screen(self, screen: object, callback: object | None = None) -> object:
        return self._view.push_screen(screen, callback)

    def update_subtitle(self) -> None:
        self._callbacks.update_subtitle()

    def refresh_git_branch(self) -> None:
        self._callbacks.refresh_git_branch()

    def update_toc(self) -> None:
        self._callbacks.update_toc()

    def on_session_fork_error(self, event: Error, *, message: str, severity: NotificationSeverity) -> None:
        self._callbacks.on_session_fork_error(event, message, severity)

    def on_session_clear_error(self, event: Error, *, message: str) -> None:
        self._callbacks.on_session_clear_error(event, message)

    def debug(self, key: str, message: str = "") -> None:
        self._callbacks.debug(key, message)

    @property
    def _approval_queue(self):
        return self._approval().queue

    @_approval_queue.setter
    def _approval_queue(self, value) -> None:
        self._approval().queue = value

    @property
    def _approval_request_lock(self):
        return self._approval().request_lock

    @_approval_request_lock.setter
    def _approval_request_lock(self, value) -> None:
        self._approval().request_lock = value

    @property
    def _approval_dialog_open(self) -> bool:
        return self._approval().dialog_open

    @_approval_dialog_open.setter
    def _approval_dialog_open(self, value: bool) -> None:
        self._approval().dialog_open = value

    @property
    def _approval_bodies(self) -> dict[str, object]:
        return self._approval().bodies

    @_approval_bodies.setter
    def _approval_bodies(self, value: dict[str, object]) -> None:
        self._approval().bodies = value

    @property
    def _open_approval_dialogs(self) -> dict[str, ApprovalDialogHandle]:
        return self._approval().open_dialogs

    @_open_approval_dialogs.setter
    def _open_approval_dialogs(self, value: dict[str, ApprovalDialogHandle]) -> None:
        self._approval().open_dialogs = value

    @property
    def _pending_verdicts(self) -> dict[str, ApprovalReviewed]:
        return self._approval().pending_verdicts

    @_pending_verdicts.setter
    def _pending_verdicts(self, value: dict[str, ApprovalReviewed]) -> None:
        self._approval().pending_verdicts = value

    @property
    def _dismissed_approval_requests(self) -> set[str]:
        return self._approval().dismissed_requests

    @_dismissed_approval_requests.setter
    def _dismissed_approval_requests(self, value: set[str]) -> None:
        self._approval().dismissed_requests = value

    @property
    def _reviewed_dismissed_approval_requests(self) -> set[str]:
        return self._approval().reviewed_dismissed_requests

    @_reviewed_dismissed_approval_requests.setter
    def _reviewed_dismissed_approval_requests(self, value: set[str]) -> None:
        self._approval().reviewed_dismissed_requests = value

    @property
    def _question_queue(self):
        return self._questions().queue

    @_question_queue.setter
    def _question_queue(self, value) -> None:
        self._questions().queue = value

    @property
    def _question_dialog_open(self) -> bool:
        return self._questions().dialog_open

    @_question_dialog_open.setter
    def _question_dialog_open(self, value: bool) -> None:
        self._questions().dialog_open = value

    @property
    def _open_question_dialogs(self) -> dict[str, QuestionDialogHandle]:
        return self._questions().open_dialogs

    @_open_question_dialogs.setter
    def _open_question_dialogs(self, value: dict[str, QuestionDialogHandle]) -> None:
        self._questions().open_dialogs = value

    @property
    def _inline_question_call_ids(self) -> dict[str, str]:
        return self._questions().inline_call_ids

    @_inline_question_call_ids.setter
    def _inline_question_call_ids(self, value: dict[str, str]) -> None:
        self._questions().inline_call_ids = value

    @property
    def _inline_question_request_ids(self) -> dict[str, str]:
        return self._questions().inline_request_ids

    @_inline_question_request_ids.setter
    def _inline_question_request_ids(self, value: dict[str, str]) -> None:
        self._questions().inline_request_ids = value

    @property
    def _question_drafts(self) -> dict[str, PromptDraft]:
        return self._questions().drafts

    @_question_drafts.setter
    def _question_drafts(self, value: dict[str, PromptDraft]) -> None:
        self._questions().drafts = value

    @property
    def _agent_load_dialog(self):
        return self._agent_load().dialog

    @_agent_load_dialog.setter
    def _agent_load_dialog(self, value) -> None:
        self._agent_load().dialog = value

    @property
    def _agent_load_status_snapshot(self) -> dict | None:
        return self._agent_load().status_snapshot

    @_agent_load_status_snapshot.setter
    def _agent_load_status_snapshot(self, value: dict | None) -> None:
        self._agent_load().status_snapshot = value

    @property
    def _image_compression_dialog(self):
        return self._image_compression().dialog

    @_image_compression_dialog.setter
    def _image_compression_dialog(self, value) -> None:
        self._image_compression().dialog = value

    # -------------------------------------------------------------- #
    # Helpers
    # -------------------------------------------------------------- #

    @staticmethod
    def _extract_error_detail(raw: str) -> str:
        """Extract a readable error message from an API error string.

        API errors look like: ``Error code: 400 - {'error': {'message': '...'}}``
        This extracts the inner ``message`` field for the chat panel while
        keeping the error code prefix.
        """
        idx = raw.find("- {")
        if idx < 0:
            return raw
        prefix = raw[:idx].strip()
        payload = raw[idx + 2 :].strip()
        try:
            data = json.loads(payload.replace("'", '"'))
            # Anthropic / OpenAI style: {'error': {'message': '...'}}
            inner = data.get("error", data)
            if isinstance(inner, dict):
                detail = inner.get("message", "")
                if detail:
                    return f"{prefix}\n{detail}"
        except json.JSONDecodeError, TypeError, AttributeError:
            pass
        return raw

    def _fallback_error_message(self, code: str) -> str:
        """Return a readable fallback when an error event has no message."""
        label = code.replace("_", " ").strip()
        return label.capitalize() if label else self._render_display(_UNKNOWN_ERROR.bind())

    def _notify(self, event: NotificationEvent) -> None:
        """Send a background desktop notification through the active app service."""
        self._view.notify_event(event)

    async def _publish_auto_fulfill_blocked(self, event: ApprovalReviewed) -> None:
        await self._services.bus.publish(
            ApprovalAutoFulfillBlocked(request_id=event.request_id, session_id=event.session_id)
        )

    def format_tool_info(
        self,
        tool_names: list[str],
        skill_names: list[str],
        *,
        memory_files: list[str] | None = None,
        runtime_details: AgentRuntimeDetails | None = None,
    ) -> StatusTrail:
        """Build trail text for tool/skill/hook/file counts.

        ``memory_files`` are auto-loaded reference file display paths from
        :class:`chrys.foundation.events.types.SessionReady` / ``ProfileSwitched``.
        Counted into the trail when non-empty. Detailed metadata is shown in
        the runtime details modal, so the tooltip is only a click hint.
        """
        return self._runtime_info.format_tool_info(
            tool_names,
            skill_names,
            memory_files=memory_files,
            runtime_details=runtime_details,
        )

    def get_profile_description(self, profile_name: str) -> str:
        """Look up a profile's description from the registry."""
        return self._runtime_info.get_profile_description(profile_name)

    @staticmethod
    def _load_title(operation: str) -> StatusMessage:
        return AgentLoadDialogController.load_title(operation)

    @staticmethod
    def _format_load_count(current: int, total: int, failed: int = 0) -> str:
        return AgentLoadDialogController.format_load_count(current, total, failed)

    async def _show_agent_load_dialog(
        self,
        *,
        title: str,
        subtitle: str,
        session_id: str | None,
        initial_message: str,
        initial_phase: str = "",
        update_clipboard_dir: bool = True,
    ) -> None:
        await self._agent_load().show_dialog(
            title=title,
            subtitle=subtitle,
            session_id=session_id,
            initial_message=initial_message,
            initial_phase=initial_phase,
            update_clipboard_dir=update_clipboard_dir,
        )

    async def begin_session_restore_load(self, session_id: str) -> None:
        """Open the restore modal before backend session ownership checks run.

        An empty *session_id* opens the modal for a pending latest-session
        lookup (blank subtitle); a later call with the resolved id reuses the
        same dialog and fills the subtitle in.
        """
        from chrys.foundation.util.session_ids import session_short_id

        await self._agent_load().begin_session_restore_load(session_id, session_short_id(session_id))

    def cancel_agent_load(self) -> None:
        """Dismiss any active agent-load UI without showing a load result."""
        self._agent_load().cancel()

    # -------------------------------------------------------------- #
    # EventBus callbacks
    # -------------------------------------------------------------- #

    async def on_agent_load_started(self, event: AgentLoadStarted) -> None:
        if event.operation not in _SOFT_AGENT_LOAD_OPERATIONS:
            # Fail-closed for guard/picker reads, but don't repaint the tag
            # yet: that flashes the select label between the old and new
            # model names on every restore/new-session load. The terminal
            # confirmation event repaints on success and AgentLoadFailed
            # repaints the unconfirmed action state on failure.
            self._state.runtime.details_confirmed = False
        await self._agent_load().on_started(event)

    async def on_agent_load_progress(self, event: AgentLoadProgress) -> None:
        await self._agent_load().on_progress(event)

    async def on_agent_load_finished(self, event: AgentLoadFinished) -> None:
        self._agent_load().on_finished(event)
        self._callbacks.post_gc_message(GcReclaimRequested(GcReclaimReason.AGENT_REBUILT, prompt=False))

    def update_session_history_progress(self, current: int, total: int) -> None:
        """Forward mounted transcript counts to the active restore dialog."""
        self._agent_load().update_session_history_progress(current, total)

    def finish_agent_load(self, message: StatusMessage = "") -> None:
        """Dismiss any active agent-load UI and unlock the main input."""
        self._agent_load().finish(message)

    async def on_agent_load_failed(self, event: AgentLoadFailed) -> None:
        s = self

        label = event.display_name or event.agent_profile
        if label:
            s.profile = label
            with contextlib.suppress(Exception):
                s.update_subtitle()

        if event.display_message is None:
            self._agent_load().on_failed(event)
        else:
            self._agent_load().on_failed(
                event,
                display=self._render_display_with_hint(event.display_message, event.display_hint),
                summary=self._render_display(event.display_message),
            )
        self._callbacks.refresh_model_indicator()
        if event.operation != "startup":
            self._callbacks.post_gc_message(GcReclaimRequested(GcReclaimReason.AGENT_REBUILD_FAILED, prompt=False))

    async def on_image_attachment_compression_started(self, event: ImageAttachmentCompressionStarted) -> None:
        """Show a loading-only modal while oversized images are prepared."""
        await self._image_compression().on_started(event)

    async def on_image_attachment_compression_finished(self, event: ImageAttachmentCompressionFinished) -> None:
        """Dismiss the image-compression modal after preparation finishes."""
        await self._image_compression().on_finished(event)

    async def on_session_ready(self, event: SessionReady) -> None:
        s = self
        ui = self._ui()

        self._state.workspace.roots = [event.primary_cwd, *event.working_dirs]
        s.main_usage_source_id = event.session_id or ""
        s.profile = event.display_name or event.agent_profile
        s.runtime_metadata = event.runtime_details
        self._state.runtime.details_confirmed = True
        if event.runtime_details.model.selection_source == "active":
            self._callbacks.set_active_model_profile_id(event.runtime_details.model.profile_id)
        self._callbacks.refresh_model_indicator()
        s.update_subtitle()
        ui.set_chat_profile(s.profile)
        ui.set_chat_tool_kinds(event.tool_kinds)
        desc = self.get_profile_description(event.agent_profile)
        ui.set_status_profile(s.profile, description=desc)
        ui.set_input_clipboard_dir(event.session_id)
        trail = self.format_tool_info(
            event.tool_names,
            event.skill_names,
            memory_files=event.memory_files,
            runtime_details=event.runtime_details,
        )
        ui.set_tool_info(trail)
        # During a restore, _on_session_restored handles the chat panel
        if s.creating_new_session:
            from chrys.foundation.platform import safe_getcwd

            s.creating_new_session = False
            s.set_has_messages(False)
            s.last_usage_tokens = 0
            s.last_total_session_tokens = 0
            # Post-success reset point: clearing on the SessionNew publish
            # instead would desync the sidebar if backend startup fails.
            ui.clear_todos()
            ui.set_input_retry_mode(False)
            s.chdir_original_cwd = None
            cwd = event.primary_cwd or safe_getcwd()
            s.chdir_current_cwd = cwd
            ui.set_context_usage_state(
                ContextUsageState.with_window(
                    used_tokens=0,
                    max_context_tokens=event.max_context_tokens or DEFAULT_MAX_CONTEXT_TOKENS,
                )
            )
            ui.set_terminal_title_for_cwd(cwd)
            ui.set_input_paste_cwd(cwd)
            await ui.clear_chat()
            s.update_toc()
            with contextlib.suppress(Exception):
                ui.reset_context_usage(event.max_context_tokens)
            ui.set_chat_workspace_cwd(cwd)
            ui.update_welcome(profile=s.profile, cwd=cwd)
            if event.session_id:
                ui.set_chat_session_id(event.session_id)
            ui.reset_session_title_state()
            ui.clear_status()
        elif not s.restoring_session:
            from chrys.foundation.platform import safe_getcwd

            cwd = event.primary_cwd or safe_getcwd()
            s.chdir_current_cwd = cwd
            ui.set_chat_workspace_cwd(cwd)
            ui.set_input_paste_cwd(cwd)
            ui.update_welcome(profile=s.profile, cwd=cwd)
            if event.session_id:
                ui.set_chat_session_id(event.session_id)
            ui.clear_status()
        # Always propagate max_context_tokens so the Context panel shows the
        # correct ceiling from the moment the session starts (before any LLM call).
        if event.max_context_tokens:
            current_context_usage = s.context_usage_state
            if current_context_usage is None or current_context_usage.max_context_tokens != event.max_context_tokens:
                ui.set_context_usage_state(
                    ContextUsageState.with_window(
                        used_tokens=current_context_usage.used_tokens if current_context_usage else s.last_usage_tokens,
                        max_context_tokens=event.max_context_tokens,
                        total_session_tokens=(
                            current_context_usage.total_session_tokens
                            if current_context_usage
                            else s.last_total_session_tokens
                        ),
                        total_session_input_tokens=(
                            current_context_usage.total_session_input_tokens if current_context_usage else 0
                        ),
                        total_session_output_tokens=(
                            current_context_usage.total_session_output_tokens if current_context_usage else 0
                        ),
                        total_session_cache_hit_tokens=(
                            current_context_usage.total_session_cache_hit_tokens if current_context_usage else None
                        ),
                    )
                )
        if event.sub_agent_tool_names:
            from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
            from chrys.app.tui.widgets.chat.tool_renderers import register_kind_renderer
            from chrys.foundation.tool_kinds import KIND_SUB_AGENT

            register_kind_renderer(KIND_SUB_AGENT, SubAgentToolCall)
        from chrys.foundation.util.session_ids import session_short_id

        # Backend-confirmed workspace roots: record them in the MRU as a
        # detached background task (never inline — publish awaits handlers).
        schedule_workspace_mru_touches(
            [event.primary_cwd, *event.working_dirs],
            max_entries=self._services.workspace_mru_max_entries,
            session_id=event.session_id or "",
            used_at=event.timestamp,
        )
        s.debug("SessionReady", f"{event.agent_profile} [{session_short_id(event.session_id or '')}]")
        if not s.restoring_session:
            self.finish_agent_load(_FINISH_SESSION_READY.bind(profile=s.profile))
            self._callbacks.post_gc_message(GcReclaimRequested(GcReclaimReason.SESSION_READY, prompt=True))

    async def on_settings_reloaded(self, _event: SettingsReloaded) -> None:
        """Route the LIVE tier: re-project what a reload only installed.

        A reload replaces the settings in the shared handle, but a LIVE field
        whose consumer holds a projection — the notification service's view,
        the dashboard's verify-commands word list — stays on the old values
        until someone projects again. RELOAD fields need nothing here (the
        rebuild re-read them), and RESTART fields were held back by the
        reload's own routing.
        """
        self._callbacks.refresh_notification_settings()
        self._callbacks.refresh_trajectory_verify_commands()
        # The Settings panel projects written-but-not-yet-live values; a
        # reload is when the live values catch up (or an unrelated reload
        # arrives), so it re-reads and, for its own reload, takes the ack.
        self._callbacks.settings_reloaded()

    async def on_agent_runtime_updated(self, event: AgentRuntimeUpdated) -> None:
        """Refresh runtime details and the persistent status-bar counts."""
        s = self

        s.runtime_metadata = event.runtime_details
        self._state.runtime.details_confirmed = True
        self._callbacks.refresh_model_indicator()
        trail = self.format_tool_info(
            event.tool_names,
            event.skill_names,
            memory_files=event.memory_files,
            runtime_details=event.runtime_details,
        )
        self._ui().set_tool_info(trail)

    async def on_agent_thinking(self, _event: AgentThinking) -> None:
        s = self
        if not s.agent_running:
            return

        self._ui().show_status(STATUS_THINKING.bind())
        s.debug("AgentThinking")

    async def on_agent_message(self, event: InvocationMessage) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_message(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if self._predates_active_run(event):
            s.debug("InvocationMessage", "ignored stale prior-run event")
            return
        if s.pending_user_message_render_active:
            s.defer_agent_message(event)
            return
        if not s.agent_running:
            return

        ui = self._ui()

        if event.is_intermediate:
            await ui.add_agent_message(
                event.text,
                is_final=True,
                is_intermediate=True,
                presentation=event.presentation,
                created_at=event.timestamp,
            )
            s.debug("InvocationMessage", f"(intermediate, {len(event.text)} chars)")
        elif event.is_final:
            was_live = s.agent_running
            terminal_generation = self._state.run.generation
            terminal_request = GcAbsorbRequested(GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True)
            terminal_owned = False
            try:
                ui.flash_turn_complete()
                await ui.add_agent_message(
                    event.text,
                    is_final=True,
                    structured_output_completed=event.structured_output_completed,
                    created_at=event.timestamp,
                )
            finally:
                if self._state.run.generation == terminal_generation and s.agent_running:
                    ui.mark_terminal_title_completed()
                    s.set_agent_running(False)
                    terminal_owned = True
            if not terminal_owned:
                s.debug("InvocationMessage", "ignored stale terminal completion")
                return
            self._callbacks.post_gc_message(terminal_request)
            if was_live:
                self._notify(NotificationEvent.TURN_COMPLETE)
            s.debug("InvocationMessage", f"({len(event.text)} chars)")
        else:
            ui.show_status(STATUS_STREAMING.bind())
            await ui.add_agent_message(event.text, is_final=False, created_at=event.timestamp)

    async def on_presentation_attempt_accepted(self, event: InvocationPresentationAttemptAccepted) -> None:
        """Commit canonical provisional text and remove rejected siblings."""
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_presentation_accepted(event)
            return
        if event.origin.kind != "turn":
            return
        self._state.render_gate.accept_presentation_attempt(event.attempt_id, event.segment_ids)
        await self._ui().accept_presentation_attempt(event.attempt_id, event.segment_ids)

    async def on_presentation_attempt_rejected(self, event: InvocationPresentationAttemptRejected) -> None:
        """Retract provisional text from a rejected response attempt."""
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_presentation_rejected(event)
            return
        if event.origin.kind != "turn":
            return
        self._state.render_gate.reject_presentation_attempt(event.attempt_id)
        await self._ui().reject_presentation_attempt(event.attempt_id)

    async def on_injection_outcome(self, event: UserInjectResult) -> None:
        s = self
        ui = self._ui()

        # Only the tracked pending injection may touch the input bar. A result
        # for an id the user already cancelled (or replaced with a new submit)
        # must not clear or unlock text they are editing; id-less results come
        # from frontends without pending tracking and keep legacy behavior.
        pending = self._state.pending_injection
        owns_input = event.injection_id is None or pending.matches(event.injection_id)

        if event.consumed:
            try:
                # Render the bubble even when cancelled-too-late: the model
                # did see this text, so the transcript must show it.
                await ui.add_user_injection(event.text, created_at=event.created_at)
                s.update_toc()
                s.debug("UserInjectResult", "consumed" if owns_input else "consumed (stale id)")
            finally:
                if owns_input:
                    pending.clear()
                    ui.unlock_input_after_consumed_injection()
        elif owns_input:
            pending.clear()
            ui.unlock_input_after_abandoned_injection()
            s.debug("UserInjectResult", "abandoned (text preserved)")
        else:
            s.debug("UserInjectResult", "abandoned (stale id, ignored)")

    async def on_tool_start(self, event: InvocationToolCallStart) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_tool_start(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if not s.agent_running:
            return
        ui = self._ui()
        ui.start_tool_status(event.tool_name)
        args_json = json.dumps(event.args, ensure_ascii=False) if event.args else ""
        provider_status = normalize_hosted_tool_status(event.provider_status)
        canonical_status = (
            provider_status.value
            if event.provider_hosted and provider_status in {HostedToolStatus.PENDING, HostedToolStatus.RUNNING}
            else "running"
        )
        await ui.add_tool_start(
            event.call_id,
            event.tool_name,
            event.tool_kind,
            args_json,
            args=event.args,
            provider_hosted=event.provider_hosted,
            hosted_family=event.hosted_family,
            provider=event.provider,
            provider_item_type=event.provider_item_type,
            provider_status=event.provider_status,
            provider_call_id=event.provider_call_id,
            canonical_status=canonical_status,
        )
        self._live_diff.record_tool_start(event.call_id, event.tool_name, event.args)
        s.debug("InvocationToolCallStart", f"{event.tool_name}")

    async def on_tool_progress(self, event: InvocationToolCallProgress) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_tool_progress(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if not s.agent_running:
            return
        self._ui().update_tool_progress(
            event.call_id,
            event.lines,
            image_contents=event.image_contents,
            snapshot_metadata=event.snapshot_metadata,
            provider_status=event.provider_status,
        )

    async def on_tool_args_updated(self, event: InvocationToolCallArgsUpdated) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_tool_args_updated(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if not s.agent_running:
            return
        self._ui().update_tool_args(event.call_id, event.args)
        s.debug("InvocationToolCallArgsUpdated", f"{event.tool_name}")

    async def on_tool_status_updated(self, event: InvocationToolCallStatusUpdated) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_tool_status_updated(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if not s.agent_running:
            return
        self._ui().update_tool_status(
            event.call_id,
            event.status,
            provider_status=event.provider_status,
            metadata=event.metadata,
        )
        s.debug("ToolCallStatusUpdated", f"{event.tool_name}: {event.status}")

    async def on_tool_result(self, event: InvocationToolCallResult) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_tool_result(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if not s.agent_running:
            return
        ui = self._ui()
        file_snapshot = event.metadata.get("file_snapshot")
        hashes = _metadata_hashes(event.metadata.get("file_mutation_hashes"))
        chat_file_snapshot = _chat_file_snapshot_payload(file_snapshot, hashes, s.state_store, ui.chat_session_id())
        approval = event.metadata.get("approval")
        canonical_status = canonical_tool_result_status(event.provider_status, event.metadata)
        await ui.add_tool_result(
            event.call_id,
            event.tool_name,
            event.result or "",
            event.duration_ms,
            image_contents=event.image_contents,
            file_snapshot=chat_file_snapshot,
            approval=approval,
            metadata=event.metadata,
            artifacts=event.artifacts,
            provider_status=event.provider_status,
            canonical_status=canonical_status,
        )
        self._questions().finish_inline_for_tool_result(event.call_id)
        self._live_diff.record_tool_result(event)
        s.refresh_git_branch()
        ui.show_status(STATUS_THINKING.bind())
        from chrys.app.tui.widgets.chat.tool_call import fmt_duration

        s.debug("InvocationToolCallResult", f"{event.tool_name} ({fmt_duration(event.duration_ms)})")

    async def on_sub_agent_invocation_start(self, event: InvocationStarted) -> None:
        """Link the just-started invocation to its mounted widget.

        Fires before the sub-agent's first LLM call so the widget lookup
        by ``invocation_id`` works for every subsequent event — including
        ``InvocationRetryAttempt`` banners that would otherwise be dropped
        when the first LLM call hits transient errors.
        """
        if not _chat_sub_agent(event.origin):
            return
        s = self
        if not s.agent_running:
            return

        self._ui().link_sub_agent_invocation(
            event.parent_call_id,
            event.origin.invocation_id,
            event.agent_name,
            event.sub_agent_log_file,
            tool_name=event.tool_name,
        )

    async def on_sub_agent_message(self, event: InvocationMessage) -> None:
        """Append invocation-scoped assistant prose to the nested transcript."""
        if not _chat_sub_agent(event.origin):
            return
        if not self.agent_running:
            return
        self._ui().add_sub_agent_message(
            event.agent_name,
            event.origin.invocation_id,
            event.text,
            presentation=event.presentation,
        )

    async def on_sub_agent_presentation_accepted(self, event: InvocationPresentationAttemptAccepted) -> None:
        """Commit provisional nested assistant segments."""
        if not _chat_sub_agent(event.origin):
            return
        if not self.agent_running:
            return
        self._ui().accept_sub_agent_presentation(
            event.origin.invocation_id,
            event.attempt_id,
            event.segment_ids,
        )

    async def on_sub_agent_presentation_rejected(self, event: InvocationPresentationAttemptRejected) -> None:
        """Retract provisional nested assistant segments."""
        if not _chat_sub_agent(event.origin):
            return
        if not self.agent_running:
            return
        self._ui().reject_sub_agent_presentation(event.origin.invocation_id, event.attempt_id)

    async def on_sub_agent_tool_start(self, event: InvocationToolCallStart) -> None:
        if not _chat_sub_agent(event.origin):
            return
        s = self
        if not s.agent_running:
            return

        await self._ui().add_sub_agent_tool_start(
            event.agent_name,
            event.origin.invocation_id,
            event.tool_name,
            event.args,
            event.call_id,
            tool_kind=event.tool_kind,
            provider_hosted=event.provider_hosted,
            hosted_family=event.hosted_family,
            provider=event.provider,
            provider_item_type=event.provider_item_type,
            provider_status=event.provider_status,
            provider_call_id=event.provider_call_id,
        )
        self._live_diff.record_tool_start(event.call_id, event.tool_name, event.args)

    async def on_sub_agent_tool_result(self, event: InvocationToolCallResult) -> None:
        if not _chat_sub_agent(event.origin):
            return
        s = self
        if not s.agent_running:
            return

        approval = event.metadata.get("approval")
        canonical_status = canonical_tool_result_status(event.provider_status, event.metadata)
        self._ui().complete_sub_agent_tool(
            event.agent_name,
            event.origin.invocation_id,
            event.call_id,
            event.result or "",
            event.duration_ms,
            image_contents=event.image_contents,
            artifacts=event.artifacts,
            approval=approval,
            metadata=event.metadata,
            provider_status=event.provider_status,
            canonical_status=canonical_status,
        )
        self._live_diff.record_tool_result(event)
        s.refresh_git_branch()

    async def on_sub_agent_tool_args_updated(self, event: InvocationToolCallArgsUpdated) -> None:
        if not _chat_sub_agent(event.origin):
            return
        if not self.agent_running:
            return
        self._ui().update_sub_agent_tool_args(event.origin.invocation_id, event.call_id, event.args)
        self.debug("InvocationToolCallArgsUpdated", event.tool_name)

    async def on_sub_agent_tool_status_updated(self, event: InvocationToolCallStatusUpdated) -> None:
        if not _chat_sub_agent(event.origin):
            return
        if not self.agent_running:
            return
        self._ui().update_sub_agent_tool_status(
            event.origin.invocation_id,
            event.call_id,
            event.status,
            metadata=event.metadata,
            provider_status=event.provider_status,
        )
        self.debug("InvocationToolCallStatusUpdated", f"{event.tool_name}: {event.status}")

    async def on_sub_agent_tool_progress(self, event: InvocationToolCallProgress) -> None:
        if not _chat_sub_agent(event.origin):
            return
        if not self.agent_running:
            return
        self._ui().update_sub_agent_tool_progress(
            event.origin.invocation_id,
            event.call_id,
            event.lines,
            image_contents=event.image_contents,
            snapshot_metadata=event.snapshot_metadata,
            provider_status=event.provider_status,
        )
        self.debug("InvocationToolCallProgress", event.tool_name)

    async def on_sub_agent_progress(self, event: InvocationProgress) -> None:
        if not _chat_sub_agent(event.origin):
            return
        s = self
        if not s.agent_running:
            return

        self._ui().update_sub_agent_progress(
            event.origin.invocation_id,
            event.tool_call_count,
            event.total_tokens,
            event.total_usage_tokens,
            event.usage_unreported_attempts,
        )

    async def on_sub_agent_compaction_started(self, event: InvocationCompactionStarted) -> None:
        """Show a live compaction line inside the owning sub-agent card."""
        if not _chat_sub_agent(event.origin):
            return
        s = self
        if not s.agent_running:
            return

        self._ui().add_sub_agent_compaction_start(event.agent_name, event.origin.invocation_id, event.compaction_id)
        s.debug("InvocationCompactionStarted", f"{event.agent_name} {event.phase}")

    async def on_sub_agent_compaction_finished(self, event: InvocationCompactionFinished) -> None:
        """Flip the sub-agent compaction line to its terminal state.

        Deliberately not gated on ``agent_running``: a cancel-outcome signal
        can land while the run is tearing down, and finalizing a line on an
        already-aborted card is a safe no-op.
        """
        if not _chat_sub_agent(event.origin):
            return
        s = self

        self._ui().complete_sub_agent_compaction(
            event.origin.invocation_id,
            event.compaction_id,
            outcome=event.outcome,
            duration_ms=event.duration_ms,
            format_violation=event.format_violation,
            failure_reason=event.failure_reason,
        )
        from chrys.app.tui.widgets.chat.tool_call import fmt_duration

        format_warning = f" — {event.format_violation}" if event.format_violation else ""
        s.debug(
            "InvocationCompactionFinished",
            f"{event.agent_name} {event.outcome} ({fmt_duration(event.duration_ms)}){format_warning}",
        )

    async def on_sub_agent_compaction_committed(self, event: InvocationCompactionCommitted) -> None:
        """Bump the sub-agent card's compaction counter for a committed round.

        Like the finished handler, deliberately not gated on
        ``agent_running`` — the committed signal trails finished(ok) and
        may land during run teardown; counting on a finalized card is
        harmless.
        """
        if not _chat_sub_agent(event.origin):
            return
        s = self

        self._ui().record_sub_agent_compaction_committed(event.origin.invocation_id, event.compaction_id)
        s.debug("InvocationCompactionCommitted", f"{event.agent_name} {event.compaction_id}")

    async def on_sub_agent_retry_attempt(self, event: InvocationRetryAttempt) -> None:
        """Render an auto-retry banner inside the owning sub-agent card."""
        if not _chat_sub_agent(event.origin):
            return
        message = (
            event.message
            if event.display_message is None
            else self._render_display_with_hint(event.display_message, event.display_hint)
        )
        self._ui().sub_agent_retry_attempt(
            event.origin.invocation_id,
            message,
            event.attempt,
            event.max_attempts,
            event.delay_seconds,
        )
        s = self
        s.debug(
            "Retry",
            f"{event.agent_name}: {event.message} ({event.attempt}/{event.max_attempts})",
        )

    async def on_sub_agent_paused(self, event: InvocationPaused) -> None:
        """Flip the sub-agent card into its paused state with Retry/Abort buttons.

        The parent is normally still running (awaiting the sub-agent's
        tool call result), so in the happy path no gate is needed.  But
        during a user interrupt the frontend flips ``_agent_running`` to
        False BEFORE the backend cascade finishes, so a late
        ``InvocationPaused`` that was already in-flight when interrupt
        fired can arrive after the UI has already torn the run down.
        Skipping it here keeps cards from flickering into a stale paused
        state with live Retry/Abort buttons that point at a controller
        the engine has already dropped.
        """
        if not _chat_sub_agent(event.origin):
            return
        s = self
        if not s.agent_running:
            return

        self._ui().sub_agent_paused(
            event.origin.invocation_id,
            event.reason,
            event.last_error,
            event.retry_attempts,
            event.diagnostic_path,
            last_error_display=(
                None
                if event.last_error_display is None
                else self._render_display_with_hint(event.last_error_display, event.last_error_hint)
            ),
        )

    async def on_sub_agent_resumed_after_pause(self, event: InvocationResumed) -> None:
        """Clear the paused state — the controller is running again."""
        if not _chat_sub_agent(event.origin):
            return
        self._ui().sub_agent_resumed_after_pause(event.origin.invocation_id)

    async def on_sub_agent_cascade_aborted(self, event: InvocationCascadeAborted) -> None:
        """Mark the sub-agent card as cancelled by a global interrupt."""
        if not _chat_sub_agent(event.origin):
            return
        self._ui().sub_agent_cascade_aborted(event.origin.invocation_id)

    async def on_sub_agent_aborted(self, event: InvocationAborted) -> None:
        """Mark the sub-agent card as aborted by the user after a pause.

        Cleared independently of the parent :class:`InvocationToolCallResult` so the
        paused banner disappears the moment the controller publishes
        ``InvocationAborted`` — without this, the card could stay visually
        paused if the parent tool result is delayed or lost.
        """
        if not _chat_sub_agent(event.origin):
            return
        self._ui().sub_agent_aborted(event.origin.invocation_id, event.last_error)

    # -- Live /diff accumulation helpers -----------------------------------

    def _accumulate_live_mutation(
        self,
        path: str,
        before_text: str,
        after_text: str,
        op_str: str | None = None,
        *,
        bytes_changed: bool | None = None,
        before_hash: str | None = None,
        after_hash: str | None = None,
        source: str = "",
    ) -> None:
        """Record a single file mutation for live /diff."""
        self._live_diff.accumulate_live_mutation(
            path,
            before_text,
            after_text,
            op_str,
            bytes_changed=bytes_changed,
            before_hash=before_hash,
            after_hash=after_hash,
            source=source,
        )

    def _accumulate_shell_snapshots(self, metadata: dict[str, object]) -> None:
        """Accumulate shell-detected file mutations from event metadata."""
        self._live_diff.accumulate_shell_snapshots(metadata)

    async def on_approval_request(self, event: ApprovalRequest) -> None:
        """Queue an approval request and (maybe) show its dialog.

        The backend decides the mode: BYPASS never publishes this event at
        all, MANUAL publishes with ``judging=False``, AUTO publishes with
        ``judging=True`` and will later emit an ``ApprovalReviewed`` to
        update the dialog.  The TUI just displays what it's told.
        """
        await self._approval().on_request(event)

    async def on_approval_cancelled(self, event: ApprovalCancelled) -> None:
        """Dismiss an abandoned approval without sending an ApprovalResponse."""
        await self._approval().on_cancelled(event)

    def _show_next_approval(self) -> None:
        """Pop the next queued approval request and show its dialog.

        Drains any cached judge verdict that arrived while the request was
        still queued (parallel tool execution in AUTO mode — see
        ``_pending_verdicts``):

        - Approved pre-mount → skip the dialog entirely and continue to the
          next queued request; the backend already ran the tool.
        - Flagged pre-mount → push the dialog and deliver the verdict after
          mount so the concern is pre-populated instead of showing a stuck
          "Evaluating" spinner.
        """
        self._approval().show_next()

    async def on_approval_reviewed(self, event: ApprovalReviewed) -> None:
        """Deliver a judge verdict from the backend to the open dialog.

        Three cases handled:

        - Matching dialog is live and undismissed → deliver verdict
          immediately (approved auto-dismisses; flagged shows the concern).
        - Dialog not yet pushed but request still queued → stash the verdict;
          it is applied when ``_show_next_approval`` eventually pops the
          request.  Supports parallel tool execution in AUTO mode where
          judges for queued requests finish before their dialog is shown.
        - Late arrival for a request that was already user-dismissed or
          otherwise resolved → drop silently.
        """
        await self._approval().on_reviewed(event)

    async def on_approval_mode_updated(self, event: ApprovalModeUpdated) -> None:
        """Sync the header badge + local cache from the backend's mode."""
        if not self._callbacks.accept_approval_update(event):
            return
        s = self
        try:
            mode = ApprovalMode(event.mode)
        except ValueError:
            return
        changed = s.approval_mode != mode
        s.approval_mode = mode
        self._ui().set_header_approval_mode(mode)
        label = mode.value.upper()
        # Only toast on a genuine change — skip the initial session-start sync.
        if changed:
            mode_label = self._render_display(APPROVAL_MODE_MESSAGES[mode].bind())
            s.notify(_APPROVAL_MODE_CHANGED.bind(mode=mode_label), title=_APPROVAL_TITLE.bind(), timeout=2)
        s.debug("ApprovalMode", label)

    async def on_question_to_user(self, event: QuestionToUser) -> None:
        await self._questions().on_question(event)

    def clear_pending_questions(self) -> None:
        """Drop any ask_user UI state after the backend turn is cancelled."""
        self._questions().clear_pending()

    def _show_next_question(self) -> None:
        """Pop the next queued question and show its dialog."""
        self._questions().show_next()

    def _show_question_inline(self, event: QuestionToUser, draft: PromptDraft | None = None) -> bool:
        """Move an active ask_user prompt from modal UI into its tool renderer."""
        return self._gateway().show_question_inline(event, draft)

    def _question_can_reopen_modal(self, event: QuestionToUser) -> bool:
        """Return whether a failed inline handoff should reopen the modal."""
        return self._gateway().question_can_reopen_modal(event)

    async def on_ask_user_timed_out(self, event: AskUserTimedOut) -> None:
        """Dismiss or dequeue an ask_user dialog after the backend timeout fires."""
        await self._questions().on_timed_out(event)

    async def on_usage_update(self, event: UsageUpdate) -> None:
        s = self
        if event.session_id and s.main_usage_source_id and event.session_id != s.main_usage_source_id:
            return

        # Only treat an event as the main session window when both sides have
        # bound a non-empty source id and they match.  Earlier we accepted any
        # empty event as the main window, but that silently mis-routed any
        # caller that forgot to set ``usage_source_id`` and also misclassified
        # parent usage that arrived before SessionReady set
        # ``_main_usage_source_id``.
        is_session_window = bool(s.main_usage_source_id) and (event.usage_source_id == s.main_usage_source_id)
        if is_session_window:
            s.last_usage_tokens = event.total_tokens
        # Session totals advance with every UsageUpdate (parent or sub-agent) —
        # the cumulative figure is global, not per-source.
        s.last_total_session_tokens = event.total_session_tokens
        if is_session_window:
            # Main session: refresh used/max gauge, sparkline, AND totals row.
            self._ui().set_context_usage_state(
                ContextUsageState.with_window(
                    used_tokens=event.total_tokens,
                    max_context_tokens=event.max_context_tokens or DEFAULT_MAX_CONTEXT_TOKENS,
                    total_session_tokens=event.total_session_tokens,
                    total_session_input_tokens=event.total_session_input_tokens,
                    total_session_output_tokens=event.total_session_output_tokens,
                    total_session_cache_hit_tokens=event.total_session_cache_hit_tokens,
                )
            )
        else:
            # Sub-agent: keep the main session's used/max gauge & sparkline,
            # but let the cumulative totals row advance.  Without this gate the
            # sidebar gauge would flicker between parent and sub-agent windows.
            self._ui().set_context_usage_state(
                ContextUsageState.session_totals_only(
                    s.context_usage_state,
                    fallback_used_tokens=s.last_usage_tokens,
                    total_session_tokens=event.total_session_tokens,
                    total_session_input_tokens=event.total_session_input_tokens,
                    total_session_output_tokens=event.total_session_output_tokens,
                    total_session_cache_hit_tokens=event.total_session_cache_hit_tokens,
                )
            )
        label = f"Usage[{event.agent_profile}]" if event.agent_profile else "UsageUpdate"
        source = _usage_source_debug_suffix(event.usage_source_id, main_source_id=s.main_usage_source_id)
        s.debug(label, f"{event.total_tokens:,} ({event.pct:.1f}%) local={event.local_tokens:,}{source}")

    async def on_todo_list_updated(self, event: TodoListUpdated) -> None:
        s = self

        self._ui().set_todo_state(tuple(event.items))
        done = sum(1 for item in event.items if item.status == "completed")
        s.debug("TodoListUpdated", f"{done}/{len(event.items)} done")

    async def on_compaction_started(self, event: CompactionStarted) -> None:
        """Mount a live "Compacting conversation..." card in the transcript."""
        s = self
        if not s.agent_running:
            return

        await self._ui().add_compaction_start(event.compaction_id)
        self._ui().show_status(STATUS_COMPACTING.bind())
        s.debug("CompactionStarted", event.phase)

    async def on_compaction_finished(self, event: CompactionFinished) -> None:
        """Finalize the live compaction card (summary note, failure, or cancel).

        Deliberately not gated on ``agent_running``: the cancel-outcome
        signal is published while the interrupted run is tearing down, and
        the card must still stop spinning.  With no matching live card the
        update is a no-op.  The status bar is a different story: a canceled
        outcome only ever means an interrupted run, and during teardown it
        can arrive before the frontend flips ``agent_running`` — re-showing
        the spinner then would overwrite the "Interrupted by user" flash
        and leave a stuck "Thinking" bar, so canceled never restores it.
        """
        s = self

        self._ui().complete_compaction(
            event.compaction_id,
            outcome=event.outcome,
            duration_ms=event.duration_ms,
            last_words=event.last_words,
            format_violation=event.format_violation,
            failure_reason=event.failure_reason,
        )
        if s.agent_running and event.outcome != "canceled":
            self._ui().show_status(STATUS_THINKING.bind())
        from chrys.app.tui.widgets.chat.tool_call import fmt_duration

        format_warning = f" — {event.format_violation}" if event.format_violation else ""
        failure_note = f" — {event.failure_reason}" if event.failure_reason else ""
        s.debug(
            "CompactionFinished",
            f"{event.outcome} ({fmt_duration(event.duration_ms)}){failure_note}{format_warning}",
        )

    async def on_tool_compacted(self, event: ToolCompacted) -> None:
        s = self
        freed = event.tokens_before - event.tokens_after
        event_key = f"ToolCompacted:{event.phase}" if event.phase else "ToolCompacted"

        if event.phase in ("phase1", "phase2"):
            turns_str = ", ".join(str(t) for t in event.turn_numbers) if event.turn_numbers else "?"
            action = "summarised" if event.phase == "phase1" else "removed"
            s.debug(
                event_key,
                f"{event.tokens_before:,} \u2192 {event.tokens_after:,} (-{freed:,}) [turns {turns_str} {action}]",
            )
        else:
            action = "removed"
            s.debug(
                event_key,
                f"{event.tokens_before:,} \u2192 {event.tokens_after:,} (-{freed:,}) "
                f"[{event.compacted_groups} tool calls {action}]",
            )

    async def on_context_compressed(self, event: ContextCompressed) -> None:
        s = self

        ui = self._ui()
        if await ui.add_context_fold(
            event.compressed_context_id,
            event.summary,
            event.freed_messages,
            event.turn_range,
        ):
            s.update_toc()
        with contextlib.suppress(Exception):
            ui.add_compressed_block(
                event.compressed_context_id,
                event.summary,
                event.freed_messages,
                event.turn_range,
            )
        source_label = "auto" if event.source == "auto" else "agent"
        event_key = f"ContextCompressed:{event.source}" if event.source == "auto" else "ContextCompressed"
        s.debug(event_key, f"-{event.freed_messages:,} messages ({source_label})")

    async def on_context_pressure(self, event: InvocationContextPressure) -> None:
        """Surface a Phase-4 breaker trip instead of silently losing compaction."""
        if event.origin.root.kind == "workflow_node":
            return
        message = context_pressure_message(event.reason, source=event.source)
        s = self
        # The breaker already deduplicates this event once per logical turn.
        # Do not route it through ``on_warning``: that handler's screen-lifetime
        # cache would suppress the same pressure reason in every later turn or
        # sibling sub-agent invocation.
        s.notify(message, title=_WARNING_TITLE.bind(), severity="warning", timeout=8)
        s.debug(
            "InvocationContextPressure",
            f"{event.source}:{event.reason} attempts={event.attempts} "
            f"side_calls={event.side_call_tokens:,}/{event.side_call_token_budget:,}",
        )

    async def on_warning(self, event: Warning) -> None:
        s = self
        submit_rejected = event.code in _SUBMIT_BLOCKING_WARNING_CODES and s.pending_user_submit_active
        if submit_rejected:
            s.block_pending_user_submit()
            if event.code in _IMAGE_WARNING_CODES:
                display = (
                    event.message if event.display_message is None else self._render_display(event.display_message)
                )
                self._show_image_rejection_dialog(event.code, display)
                s.debug("ImageAttachmentBlocked", event.message.splitlines()[0][:80])
                return
            self._restore_pending_submit_text()
        key = _WarningDedupeKey(
            # Capability ownership is a model configuration fact. Its message
            # already includes the model profile, so /new must not reopen the
            # same modal merely because it allocated another session id.
            session_id=None if event.code == "hosted_web_tools_preferred" else event.session_id,
            code=event.code,
            message=event.message,
        )
        # Each rejected user action needs feedback, even when the reason is
        # unchanged. Only background warnings share the screen-lifetime cache.
        if key in self._seen_warnings and not submit_rejected:
            return
        self._seen_warnings.add(key)
        # A capability handover is worth one modal per configuration, not a toast the
        # user can miss; the dedupe key already scopes it to the producing fingerprint.
        if event.code == "hosted_web_tools_preferred":
            from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

            dialog = ConfirmDialog(
                title=_WARNING_TITLE.bind(),
                message=event.display_message or event.message,
                confirm_label=_HOSTED_WEB_TOOLS_OK.bind(),
                cancel_label=None,
                confirm_variant="warning",
                locale_controller=self._locale_controller,
            )
            dialog.add_class("-warning-border")
            s.push_screen(dialog)
            s.debug("Warning", f"[{event.code}] {event.message[:80]}")
            return
        s.notify(event.display_message or event.message, title=_WARNING_TITLE.bind(), severity="warning", timeout=8)
        s.debug("Warning", f"[{event.code}] {event.message[:80]}")

    async def on_error(self, event: Error) -> None:
        if self._callbacks.route_session_error(event):
            return
        s = self
        if event.code in _STALE_RUN_FILTERED_ERROR_CODES and self._predates_active_run(event):
            s.debug("Error", "ignored stale prior-run error")
            return
        ui = self._ui()
        was_live = s.agent_running
        terminal_generation = self._state.run.generation
        s.restoring_session = False
        # Agent-load failures should arrive as ``AgentLoadFailed``.  Keep
        # this fallback for failures that happen before the load lifecycle is
        # established or arrive from older backend paths.
        raw_msg = event.message.strip()
        if event.code.startswith(_SESSION_FORK_ERROR_PREFIX):
            full_msg = raw_msg or self._fallback_error_message(event.code)
            display_msg = full_msg if event.display_message is None else self._render_display(event.display_message)
            severity: NotificationSeverity = "error" if event.code == "session_fork_failed" else "warning"
            s.on_session_fork_error(event, message=display_msg, severity=severity)
            s.debug("Error", f"[{event.code}] {full_msg[:60]}")
            return
        if event.code == _SESSION_CLEAR_FAILED_CODE:
            # /clear failed before deleting anything: keep the session, toast,
            # and skip the generic chat-error/retry-mode surface.
            full_msg = raw_msg or self._fallback_error_message(event.code)
            display_msg = full_msg if event.display_message is None else self._render_display(event.display_message)
            s.on_session_clear_error(event, message=display_msg)
            s.debug("Error", f"[{event.code}] {full_msg[:60]}")
            return
        full_msg = raw_msg or (
            self._render_display(_AGENT_FAILED_TO_LOAD.bind())
            if s.agent_loading
            else self._fallback_error_message(event.code)
        )
        # UI surfaces show the localized display when the producer attached
        # one; ``full_msg`` keeps the raw protocol text for debug lines.
        display_full = (
            full_msg
            if event.display_message is None
            else self._render_display_with_hint(event.display_message, event.display_hint)
        )
        if event.code == "session_in_use":
            if s.agent_loading:
                self.cancel_agent_load()
            s.set_agent_running(False)
            from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog

            dialog = ConfirmDialog(
                title=_SESSION_IN_USE_TITLE.bind(),
                message=_SESSION_IN_USE_MESSAGE.bind(message=DisplayBlock(display_full)),
                confirm_label=_SESSION_IN_USE_OK.bind(),
                cancel_label=None,
                confirm_variant="warning",
                locale_controller=self._locale_controller,
                bold_message_prefix=True,
            )
            dialog.add_class("-warning-border")
            s.push_screen(dialog)
            s.debug("Error", f"[{event.code}] {full_msg[:60]}")
            return
        if s.pending_user_submit_active and event.code in _SUBMIT_BLOCKING_ERROR_CODES:
            s.set_agent_running(False)
            s.block_pending_user_submit()
            if event.code in _IMAGE_ERROR_CODES:
                self._show_image_rejection_dialog(event.code, display_full)
                s.debug("ImageAttachmentBlocked", full_msg.splitlines()[0][:80])
                return
            await self._show_pending_submit_error(event.code, display_full, raw_message=full_msg)
            return
        if s.pending_user_message_render_active:
            s.defer_error(event)
            return
        # A classified failure shows what went wrong, then the service's own
        # words as evidence; other producers' display is their message in
        # the current locale, so it stands alone.
        with_raw = event.code == "executor_error" and event.display_message is not None and bool(raw_msg)
        if s.agent_loading:
            self._agent_load().fail(f"{display_full}\n{raw_msg}" if with_raw else display_full)
        terminal_request = GcAbsorbRequested(GcAbsorbReason.TURN_TERMINAL, terminal_boundary=True) if was_live else None
        # Build a short version for the status bar (strip JSON payload); its
        # one line has no room for the hint.
        short_msg = full_msg if event.display_message is None else self._render_display(event.display_message)
        if "- {" in short_msg:
            short_msg = short_msg[: short_msg.index("- {")].strip()
        # Build a readable version for the chat panel: extract 'message'
        # from the JSON payload if present, otherwise use full text.
        if with_raw:
            chat_msg = f"{display_full}\n{self._extract_error_detail(raw_msg)}"
        else:
            chat_msg = self._extract_error_detail(display_full)
        terminal_owned = not was_live
        try:
            ui.flash_status(STATUS_ERROR.bind(message=short_msg), error=True)
            await ui.add_error(chat_msg)
        finally:
            if not was_live or (self._state.run.generation == terminal_generation and s.agent_running):
                if was_live:
                    ui.mark_terminal_title_failed()
                s.set_agent_running(False)
                ui.unlock_input_keep_if_locked()
                terminal_owned = True
        if not terminal_owned:
            s.debug("Error", "ignored stale terminal completion")
            return
        if terminal_request is not None:
            self._callbacks.post_gc_message(terminal_request)
        ui.set_input_retry(INPUT_RETRY.bind())
        if was_live and event.code not in _NON_TURN_NOTIFICATION_ERROR_CODES:
            self._notify(NotificationEvent.TURN_ERROR)
        s.debug("Error", f"[{event.code}] {full_msg[:60]}")

    def _restore_pending_submit_text(self) -> InputRestore | None:
        """Restore the prompt text that the input widget cleared on submit."""
        s = self
        return self._ui().restore_input_text(s.pending_user_submit_text)

    def _show_image_rejection_dialog(self, code: str, message: str) -> None:
        """Show the image rejection modal for a backend-validated prompt."""
        text = self.pending_user_submit_text
        converted_text = replace_image_mentions_with_paths(text, Path(self._state.workspace.current_cwd))
        show_path_action = converted_text != text
        restore = self._restore_pending_submit_text()

        from chrys.app.tui.screens.dialogs.vision_unsupported import (
            USE_IMAGE_PATHS_RESULT,
            VISION_UNSUPPORTED_TITLE,
            VisionUnsupportedDialog,
        )

        title = VISION_UNSUPPORTED_TITLE.bind() if code == "vision_unsupported" else _IMAGE_NOT_ATTACHED.bind()

        dialog = VisionUnsupportedDialog(
            message,
            title=title,
            show_path_action=show_path_action,
            locale_controller=self._locale_controller,
        )

        def on_dismiss(result: object) -> None:
            # Only the prompt this handler restored may be rewritten in place;
            # a draft the user typed instead is theirs, dialog or no dialog.
            if result == USE_IMAGE_PATHS_RESULT:
                self._ui().rewrite_restored_input(restore, converted_text)

        self.push_screen(dialog, callback=on_dismiss)

    async def _show_pending_submit_error(self, code: str, message: str, *, raw_message: str | None = None) -> None:
        """Surface a synchronous submit rejection without switching into retry mode.

        ``message`` may be localized display text; the debug stream keeps
        ``raw_message`` (the protocol English) when the caller provides it.
        """
        s = self
        ui = self._ui()

        self._restore_pending_submit_text()
        short_msg = message
        if "- {" in short_msg:
            short_msg = short_msg[: short_msg.index("- {")].strip()
        chat_msg = self._extract_error_detail(message)
        ui.flash_status(STATUS_ERROR.bind(message=short_msg), error=True)
        if self._state.submit.is_retry:
            # Admission did not start a new run. Keep the previous terminal
            # card and its action so the user can retry after resolving this.
            s.notify(chat_msg, title=_RETRY_NOT_STARTED.bind(), severity="error", timeout=8)
        else:
            await ui.add_error(chat_msg, action_label=None)
        s.debug("Error", f"[{code}] {(message if raw_message is None else raw_message)[:60]}")

    async def on_retry_attempt(self, event: InvocationRetryAttempt) -> None:
        if event.origin.kind == "sub_agent":
            await self.on_sub_agent_retry_attempt(event)
            return
        if event.origin.kind != "turn":
            return
        s = self
        if not s.agent_running:
            return  # Stale event after interrupt — ignore
        if event.scope == "compaction":
            # Phase-4 side-call retries recover on their own most of the
            # time — they surface as a quiet warning line inside the live
            # compaction card, never as the transcript-level error banner
            # (which would also wrongly finalize running tool widgets).
            reason = event.detail or event.message
            self._ui().show_compaction_retry(reason, event.attempt, event.max_attempts, event.delay_seconds)
            s.debug("Retry", f"{event.message} ({event.attempt}/{event.max_attempts})")
            return
        # Finalise pending widgets from the failed attempt BEFORE mounting
        # the retry notice.  The backend's ``StreamRetryLoop`` rolls history
        # back before the next attempt, so any still-running tool cards
        # (especially sub-agents cancelled by the stream cleanup hooks) and
        # the open intermediate assistant message would otherwise act as
        # stale attach points for the re-run's tool calls, causing new
        # sub-agent cards to appear under the old assistant block.
        retry_msg = (
            event.message
            if event.display_message is None
            else self._render_display_with_hint(event.display_message, event.display_hint)
        )
        await self._ui().show_retry_attempt(retry_msg, event.attempt, event.max_attempts, event.delay_seconds)
        s.debug("Retry", f"{event.message} ({event.attempt}/{event.max_attempts})")
