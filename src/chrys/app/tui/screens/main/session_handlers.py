# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session lifecycle handlers — create, restore, delete, profile/workspace switching."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from functools import partial
from typing import TYPE_CHECKING, Any

from chrys.app.tui.i18n import render_str
from chrys.app.tui.screens.main.main_screen_presenter import MainScreenPresenter
from chrys.app.tui.screens.main.ports import (
    AgentLoadUiPort,
    BackendEventView,
    NotificationSeverity,
    ProfileDescriptionProvider,
    RuntimeInfoProvider,
    StatusMessage,
    StatusTrail,
)
from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider, enabled_hook_count
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.sessions import WorkflowSessionPick
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.support.workspace_mru import schedule_workspace_mru_touches
from chrys.app.tui.widgets.chrome.input_bar import INPUT_CONTINUE, INPUT_RETRY
from chrys.app.tui.widgets.chrome.status_bar import (
    STATUS_CUSTOM_TITLE_CLEARED,
    STATUS_FORK_CREATED,
    STATUS_FORK_NOTICE,
    STATUS_OPENED_FORK,
    STATUS_SESSION_RESTORED,
    STATUS_SESSION_TITLE_UPDATED,
)
from chrys.app.tui.widgets.sidebar.context import ContextUsageState
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    Error,
    ProfileSwitched,
    SessionFork,
    SessionForked,
    SessionRestored,
    SessionSaved,
    SessionTitleUpdated,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import DisplayPath, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.todos import parse_todo_items
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.util.session_ids import session_short_id
from chrys.service.session.persistence import has_real_messages

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog
    from chrys.app.tui.screens.main.ports import StartWorker
    from chrys.app.tui.widgets.chat.file_snapshot import FileSnapshotPayload
    from chrys.foundation.events.bus import EventBus
    from chrys.service.context.providers.history import CompressedBlock
    from chrys.service.state.store import StateStore


_FINISH_SESSION_RESTORED = msg(
    "tui.agent_load.finish.session_restored",
    fallback="Session restored: {session_id}",
)
_FINISH_AGENT_READY = msg("tui.agent_load.finish.agent_ready", fallback="Agent ready: {agent}")
_FINISH_PROFILE_SWITCHED = msg(
    "tui.agent_load.finish.profile_switched",
    fallback="Profile switched: {from_label} -> {to_label}",
)
_RESUME_TITLE = msg("tui.session.title.resume", fallback="Resume")
_FORK_TITLE = msg("tui.session.title.fork", fallback="Fork")
CLEAR_SESSION_TITLE = msg("tui.main.confirm.clear_title", fallback="Clear Session")
_CLEAR_FAILED = msg(
    "tui.session.clear.failed",
    fallback="The current session was kept: {message}",
)
_SESSION_TITLE_TITLE = msg("tui.session.title.session_title", fallback="Session Title")
_NO_SAVED_SESSIONS = msg("tui.session.resume.none", fallback="No saved sessions found in this mode")
_FORK_TURN_RUNNING = msg(
    "tui.session.fork.turn_running",
    fallback="Cannot fork while a turn is running",
)
_FORK_NO_ACTIVE_SESSION = msg(
    "tui.session.fork.no_active_session",
    fallback="No active session to fork",
)
_FORK_DIALOG_FAILED = msg(
    "tui.session.fork.dialog_failed",
    fallback="Could not open fork dialog",
)
_SESSION_TITLE_SAVE_FAILED = msg(
    "tui.session.title.save_failed",
    fallback="Failed to save session title",
)
_FORK_CREATED = msg("tui.session.fork.created", fallback="Created fork {fork_id}")
_PROFILE_SWITCH_INDICATOR = msg(
    "tui.session.profile_switch_indicator",
    fallback="Agent profile switched: {from_label} → {to_label}",
)
_WORKING_DIRECTORY_INDICATOR = msg(
    "tui.session.working_directory_indicator",
    fallback="Working directory → {path}",
)


class RestoreRequest(Enum):
    """What :meth:`SessionHandler.do_session_restore` did."""

    REQUESTED = "requested"
    """A restore was published; ``SessionRestored`` or an ``Error`` follows."""
    DECLINED = "declined"
    """The session's folder is gone and the user chose no other one, or kept its background commands running."""
    SKIPPED = "skipped"
    """Nothing was published (busy, or the loading UI could not open)."""


async def _decline_missing_working_dir(_missing: str) -> str | None:
    return None


async def _stop_background_tasks_unasked() -> bool:
    return True


@dataclass(frozen=True, slots=True)
class SessionCallbacks:
    """Screen-owned effects required by session handlers."""

    set_agent_loading: Callable[[bool], None]
    set_has_messages: Callable[[bool], None]
    set_creating_new_session: Callable[[bool], None]
    set_restoring_session: Callable[[bool], None]
    set_profile_display: Callable[[str], None]
    set_active_model_profile_id: Callable[[str], None]
    set_workspace_cwd: Callable[[str], None]
    update_subtitle: Callable[[], None]
    update_toc: Callable[[], None]
    clear_suggestion_file_cache: Callable[[], None]
    start_worker: StartWorker
    post_gc_message: Callable[[GcAbsorbRequested | GcReclaimRequested], object]
    debug: Callable[[str, str], None]
    refresh_model_indicator: Callable[[], None]
    choose_missing_working_dir: Callable[[str], Awaitable[str | None]] = _decline_missing_working_dir
    confirm_stopping_background_tasks: Callable[[], Awaitable[bool]] = _stop_background_tasks_unasked
    """Whether the user lets the running background commands stop as the session ends."""


def _samepath(a: str, b: str) -> bool:
    """Compare paths using OS semantics (handles case-insensitive FS, symlinks)."""
    try:
        return os.path.samefile(a, b)
    except OSError, ValueError:
        return a == b


class SessionHandler:
    """Handles session lifecycle and profile/workspace switching.

    Owns chain-tracking state for profile switches and chdir operations.
    Constructed once by the main-screen owner.
    """

    def __init__(
        self,
        *,
        state: MainScreenState,
        services: MainScreenServices,
        view: BackendEventView,
        callbacks: SessionCallbacks,
        agent_load: AgentLoadUiPort,
        runtime_info: RuntimeInfoProvider | None = None,
        profile_descriptions: ProfileDescriptionProvider | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._callbacks = callbacks
        self._presenter = MainScreenPresenter(view, state)
        runtime_info = runtime_info or RegistryRuntimeInfoProvider(services)
        self._agent_load = agent_load
        self._runtime_info = runtime_info
        self._profile_descriptions = profile_descriptions or runtime_info
        self._locale_controller = locale_controller
        self._fork_dialog: ForkSessionDialog | None = None
        self._fork_session_id = ""
        self._fork_new_session_id = ""
        # Serializes custom-title saves: FileLock acquisition is not FIFO,
        # so two rapid dialog saves could otherwise land out of order.
        self._custom_title_apply_lock = asyncio.Lock()

    def _ui(self) -> MainScreenPresenter:
        """Return the screen presenter."""
        return self._presenter

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def _format_tool_info(
        self,
        tool_names: list[str],
        skill_names: list[str],
        *,
        memory_files: list[str] | None = None,
        runtime_details: AgentRuntimeDetails | None = None,
    ) -> StatusTrail:
        return self._runtime_info.format_tool_info(
            tool_names,
            skill_names,
            memory_files=memory_files,
            runtime_details=runtime_details,
        )

    def _get_profile_description(self, profile_name: str) -> str:
        return self._profile_descriptions.get_profile_description(profile_name)

    @property
    def bus(self) -> EventBus:
        return self._services.bus

    @property
    def state_store(self) -> StateStore | None:
        return self._services.state_store

    @property
    def agent_running(self) -> bool:
        return self._state.run.agent_running

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
        configured = enabled_hook_count(self._state.runtime.details)
        self._state.runtime.details = value
        # The Hooks tab's empty state says whether any hook is configured.
        if enabled_hook_count(value) != configured:
            self._ui().hook_runs_changed()

    @property
    def main_usage_source_id(self) -> str:
        return self._state.runtime.main_usage_source_id

    @main_usage_source_id.setter
    def main_usage_source_id(self, value: str) -> None:
        self._state.runtime.main_usage_source_id = value

    @property
    def last_usage_tokens(self) -> int:
        return self._state.usage.last_usage_tokens

    @last_usage_tokens.setter
    def last_usage_tokens(self, value: int) -> None:
        self._state.usage.last_usage_tokens = value

    @property
    def last_total_session_tokens(self) -> int:
        return self._state.usage.last_total_session_tokens

    @last_total_session_tokens.setter
    def last_total_session_tokens(self, value: int) -> None:
        self._state.usage.last_total_session_tokens = value

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

    @property
    def profile_switch_from(self) -> str | None:
        return self._state.profile_marker.from_profile

    @profile_switch_from.setter
    def profile_switch_from(self, value: str | None) -> None:
        self._state.profile_marker.from_profile = value

    @property
    def profile_switch_to(self) -> str | None:
        return self._state.profile_marker.to_profile

    @profile_switch_to.setter
    def profile_switch_to(self, value: str | None) -> None:
        self._state.profile_marker.to_profile = value

    @property
    def profile_switch_seq(self) -> int:
        return self._state.profile_marker.seq

    @profile_switch_seq.setter
    def profile_switch_seq(self, value: int) -> None:
        self._state.profile_marker.seq = value

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

    def start_session_restore(self, session_id: str) -> object:
        """Run :meth:`do_session_restore` in a screen worker."""
        return self._callbacks.start_worker(partial(self.do_session_restore, session_id))

    def workspace_cwd(self) -> str:
        return self.chdir_current_cwd or self._state.workspace.current_cwd or safe_getcwd()

    def update_subtitle(self) -> None:
        self._callbacks.update_subtitle()

    def update_toc(self) -> None:
        self._callbacks.update_toc()

    def clear_suggestion_file_cache(self) -> None:
        self._callbacks.clear_suggestion_file_cache()

    def debug(self, key: str, message: str = "") -> None:
        self._callbacks.debug(key, message)

    # -------------------------------------------------------------- #
    # Session lifecycle
    # -------------------------------------------------------------- #

    async def delete_current_and_new(self, session_id: str) -> None:
        """Delete the current session and start fresh as ONE fenced backend operation.

        Input is blocked for the whole operation (typed text is kept), and the
        fresh session only starts once the backend acknowledged the delete with
        ``SessionDeleted``. A failed delete keeps the current session: the
        backend reports ``session_clear_failed`` (toast via
        :meth:`on_session_clear_error`) and never starts a new session.
        """
        s = self
        if not await self._callbacks.confirm_stopping_background_tasks():
            return
        # Guard and claim the UI synchronously — no await between them — so
        # the /clear preflight and "input blocked" are one step: a submit that
        # is still being admitted (``submit.active``; ``agent_running`` flips
        # only afterwards) vetoes the clear, and nothing can be admitted once
        # ``agent_loading`` is set.
        if s.agent_loading or s.agent_running or self._state.submit.active or self._services.execution_busy():
            return
        s.set_agent_loading(True)
        s.creating_new_session = True
        from chrys.foundation.events.types import SessionClear, SessionDeleted

        deleted = False

        async def _on_deleted(event: SessionDeleted) -> None:
            nonlocal deleted
            if event.session_id == session_id:
                deleted = True

        await s.bus.subscribe(SessionDeleted, _on_deleted)
        try:
            await s.bus.publish(SessionClear(session_id=session_id))
        finally:
            await s.bus.unsubscribe(SessionDeleted, _on_deleted)
            if not deleted:
                # Nothing was deleted, so no fresh session was started: undo the
                # optimistic new-session/loading state (idempotent with the
                # ``session_clear_failed`` toast path).
                s.creating_new_session = False
                s.set_agent_loading(False)

    async def create_new_session(self) -> None:
        s = self
        if not await self._callbacks.confirm_stopping_background_tasks():
            return
        if s.agent_loading or s.agent_running or self._services.execution_busy():
            return
        from chrys.foundation.events.types import SessionNew

        s.creating_new_session = True
        await s.bus.publish(SessionNew())

    async def resume_last_session(self) -> None:
        """Resume the most recent chat session.

        The restore modal opens before the lookup so a slow first-time index
        backfill still shows progress.
        """
        s = self
        if not s.state_store or s.agent_running or s.agent_loading or self._services.execution_busy():
            return
        s.restoring_session = True
        try:
            # Empty id: the modal opens with a blank subtitle and the resolved
            # id fills it in via ``do_session_restore`` (same dialog).
            await self._agent_load.begin_session_restore_load("")
            selection = await s.state_store.load_latest_session_id(chat_only=True)
        except Exception as exc:
            s.restoring_session = False
            with contextlib.suppress(Exception):
                self._agent_load.cancel_agent_load()
            s.debug("SessionRestore", f"failed to look up latest session: {exc}")
            return
        if selection is None:
            s.restoring_session = False
            self._agent_load.cancel_agent_load()
            s.notify(_NO_SAVED_SESSIONS.bind(), title=_RESUME_TITLE.bind(), severity="warning", timeout=3)
            return
        # The lookup modal already marked the screen as loading.
        await self.do_session_restore(selection, allow_while_loading=True)

    async def fork_current_session(self) -> None:
        """Request a backend-managed fork of the current saved session."""
        s = self
        ui = self._ui()

        if s.agent_loading:
            return
        if s.agent_running or self._services.execution_busy():
            s.notify(_FORK_TURN_RUNNING.bind(), title=_FORK_TITLE.bind(), severity="warning", timeout=3)
            return
        # The backend owns emptiness: a session can contain saved side answers without chat messages.
        session_id = ui.chat_session_id()
        if not session_id:
            s.notify(_FORK_NO_ACTIVE_SESSION.bind(), title=_FORK_TITLE.bind(), severity="warning", timeout=3)
            return
        try:
            await self._open_session_fork_dialog(session_id)
        except Exception as exc:
            self._reset_session_fork_dialog()
            s.set_agent_loading(False)
            s.notify(_FORK_DIALOG_FAILED.bind(), title=_FORK_TITLE.bind(), severity="error", timeout=5)
            s.debug("SessionFork", f"failed to open dialog: {exc}")
            return
        try:
            await s.bus.publish(SessionFork(session_id=session_id))
        except Exception as exc:
            self.on_session_fork_error(
                Error(session_id=session_id, code="session_fork_failed", message=str(exc)),
                message=str(exc),
                severity="error",
            )

    async def _open_session_fork_dialog(self, session_id: str) -> None:
        """Open the non-dismissible fork progress dialog and mark the UI busy."""
        s = self
        from chrys.app.tui.screens.dialogs.fork_session import ForkSessionDialog
        from chrys.app.tui.terminal.launcher import can_open_new_chrys_window

        if self._fork_dialog is not None:
            return
        dialog = ForkSessionDialog(
            show_new_window=can_open_new_chrys_window(),
            locale_controller=self._locale_controller,
        )
        self._fork_dialog = dialog
        self._fork_session_id = session_id
        self._fork_new_session_id = ""
        s.set_agent_loading(True)
        pushed = s.push_screen(dialog, callback=self._handle_session_fork_result)
        if inspect.isawaitable(pushed):
            await pushed

    def _handle_session_fork_result(self, result: object) -> None:
        """Route the resolved fork dialog action."""
        s = self
        ui = self._ui()

        new_session_id = self._fork_new_session_id
        fork_short_id = session_short_id(new_session_id) if new_session_id else ""
        self._reset_session_fork_dialog()
        s.set_agent_loading(False)

        if result == "switch" and new_session_id:
            s.start_session_restore(new_session_id)
            return
        if result == "new_window" and new_session_id:
            from chrys.app.tui.terminal.launcher import TerminalLaunchError, launch_new_chrys_window

            try:
                launch_new_chrys_window(new_session_id, cwd=s.workspace_cwd())
            except TerminalLaunchError as exc:
                message = str(exc) if exc.display_message is None else self._render_message(exc.display_message)
                s.notify(message, title=_FORK_TITLE.bind(), severity="warning", timeout=5)
                return
            ui.flash_status(STATUS_OPENED_FORK.bind(fork_id=fork_short_id))
            return
        if result == "stay" and fork_short_id:
            ui.flash_status(STATUS_FORK_CREATED.bind(fork_id=fork_short_id))

    def _reset_session_fork_dialog(self) -> None:
        self._fork_dialog = None
        self._fork_session_id = ""
        self._fork_new_session_id = ""

    async def do_session_restore(
        self,
        session_id: str | WorkflowSessionPick,
        *,
        allow_while_loading: bool = False,
        primary_cwd: str = "",
    ) -> RestoreRequest:
        """Restore *session_id*, in *primary_cwd* when given instead of its saved folder.

        A session whose saved folder is gone is restored only in a folder the
        user picks; declining leaves the current session as it was.
        """
        s = self
        if s.agent_running or self._services.execution_busy() or (s.agent_loading and not allow_while_loading):
            return RestoreRequest.SKIPPED
        if isinstance(session_id, WorkflowSessionPick):
            await self._restore_workflow_session(session_id)
            return RestoreRequest.REQUESTED
        from chrys.foundation.events.types import SessionRestore

        if not primary_cwd and (missing := await self._missing_saved_cwd(session_id)) is not None:
            # /resume and startup open their loading UI first; the prompt replaces it.
            if s.agent_loading:
                with contextlib.suppress(Exception):
                    self._agent_load.cancel_agent_load()
            s.restoring_session = False
            chosen = await self._callbacks.choose_missing_working_dir(missing)
            if chosen is None:
                return RestoreRequest.DECLINED
            return await self.do_session_restore(
                session_id, allow_while_loading=allow_while_loading, primary_cwd=chosen
            )
        # Restoring the current session keeps its background commands.
        if session_id != s.main_usage_source_id and not await self._callbacks.confirm_stopping_background_tasks():
            if s.agent_loading:
                with contextlib.suppress(Exception):
                    self._agent_load.cancel_agent_load()
            s.restoring_session = False
            return RestoreRequest.DECLINED

        s.restoring_session = True
        try:
            await self._agent_load.begin_session_restore_load(session_id)
        except Exception as exc:
            s.restoring_session = False
            with contextlib.suppress(Exception):
                self._agent_load.cancel_agent_load()
            s.debug("SessionRestore", f"failed to open loading UI: {exc}")
            return RestoreRequest.SKIPPED
        await s.bus.publish(
            SessionRestore(
                session_id=session_id,
                primary_cwd=primary_cwd,
                apply_saved_model=self._services.apply_saved_model_on_restore,
            )
        )
        return RestoreRequest.REQUESTED

    async def _missing_saved_cwd(self, session_id: str) -> str | None:
        """The saved folder of *session_id* when it no longer exists, read the way restore reads it.

        Restoring another session prefers its crash-recovery copy, so this
        does too. Read failures are left for the restore itself to report.
        """
        store = self.state_store
        if store is None:
            return None
        try:
            meta = await store.load_session_meta(session_id, prefer_recovery=True)
        except Exception:
            return None
        saved = meta.primary_cwd if meta is not None else ""
        return saved if saved and not os.path.isdir(saved) else None

    async def _restore_workflow_session(self, selection: WorkflowSessionPick) -> None:
        self.restoring_session = True
        try:
            await self._view.restore_workflow_session(selection.session_id)
        finally:
            self.restoring_session = False

    def load_file_edit_snapshots(
        self,
        session_id: str,
        messages: list[dict],
        state: dict[str, Any] | None,
    ) -> dict[str, list[FileSnapshotPayload]]:
        """Build ``{fw_call_id: [snapshot_payload, ...]}`` from MutationTracker data.

        Returns a list per ``call_id`` so duplicate call_ids (observed when
        some LLMs reuse ids across retries after approval rejection) keep
        distinct snapshots.  ``replay_history`` consumes these as available
        file-tool snapshot cursors independent of tool-result pairing.
        """
        s = self
        if s.state_store is None:
            return {}
        try:
            from chrys.app.tui.widgets.chat.file_snapshot import snapshot_payload_from_hashes
            from chrys.service.mutations.store import SnapshotStore
            from chrys.service.mutations.tracker import MutationTracker

            mutations_data = state.get("chrys_mutations") if state is not None else None
            if not mutations_data:
                return {}

            # Route through the store so the folder derivation matches
            # wherever the session was actually saved (handles short/long
            # ids and alternate store roots, e.g. in tests).
            session_dir = s.state_store.session_dir(session_id)
            store = SnapshotStore(session_dir)
            tracker = MutationTracker.deserialize(mutations_data, store)
            edit_snapshot_refs = tracker.get_file_edit_snapshot_refs()
            if not edit_snapshot_refs:
                return {}

            _file_tools = {"edit_file", "write_file"}
            fw_call_ids: list[str] = []
            for message_item in messages:
                if message_item.get("role") != "assistant":
                    continue
                additional = message_item.get("additional_properties")
                if isinstance(additional, dict) and HistoryMarkerKind.KEY in additional:
                    # Marker payloads are chrome that replay never renders;
                    # the tracker records snapshots only for executed calls,
                    # so a marker-carried call must not shift the zip.
                    continue
                for c in message_item.get("contents", []):
                    if isinstance(c, dict) and c.get("type") == "function_call":
                        name = c.get("name", "")
                        cid = c.get("call_id", "")
                        if name in _file_tools and cid:
                            fw_call_ids.append(cid)

            # Positional pairing: fw_call_ids[i] ↔ edit_snapshots[i].  Bucket
            # into per-call_id lists so duplicate call_ids each keep their
            # own snapshot.  ``zip(..., strict=False)`` silently truncates if
            # the lists disagree in length — preserves existing fail-soft
            # semantics when history and tracker data are out of sync.
            buckets: dict[str, list[FileSnapshotPayload]] = {}
            for cid, snap_ref in zip(fw_call_ids, edit_snapshot_refs, strict=False):
                buckets.setdefault(cid, []).append(
                    snapshot_payload_from_hashes(store.mutations_dir, snap_ref.before, snap_ref.after)
                )
            return buckets
        except Exception:
            return {}

    # -------------------------------------------------------------- #
    # EventBus callbacks
    # -------------------------------------------------------------- #

    async def on_session_restored(self, event: SessionRestored) -> None:
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        s = self
        ui = self._ui()

        self._state.workspace.roots = [event.primary_cwd, *event.working_dirs]
        s.restoring_session = False
        restored_has_messages = event.message_count > 0
        s.chdir_original_cwd = None
        cwd = event.primary_cwd or safe_getcwd()
        s.chdir_current_cwd = cwd
        ui.set_terminal_title_for_cwd(cwd)
        if os.path.isdir(cwd):
            await ui.change_shell_directory(cwd)
        ui.set_input_retry_mode(False)
        ui.set_input_paste_cwd(cwd)
        ui.set_input_clipboard_dir(event.session_id)
        display = event.display_name or event.agent_profile
        # The previous session's hook runs ended with its manager. The restored
        # session's own may already be stored: its SessionReady, and the runs
        # its build started, usually come before this event.
        if self._state.runtime.hook_snapshots:
            self._state.runtime.keep_hook_snapshot_of(event.session_id)
            restored_hooks = self._state.runtime.hook_snapshots.get(event.session_id)
            ui.set_hook_executions(restored_hooks.executions if restored_hooks is not None else ())
        s.main_usage_source_id = event.session_id
        await ui.clear_chat()
        ui.set_chat_workspace_cwd(cwd)
        ui.update_welcome(profile=display, cwd=cwd)
        ui.set_chat_session_id(event.session_id)
        await self._restore_session_title_state(
            event.session_id,
            prefer_recovery=event.recovered_from_sidecar,
        )

        # Load compressed blocks (needed for both replay and Context Panel)
        compressed_blocks_map: dict[str, CompressedBlock] = {}
        loaded_state: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            ui.clear_compressed_blocks()
        prefer_recovery = event.recovered_from_sidecar
        if s.state_store:
            try:
                loaded = await s.state_store.load_session(event.session_id, prefer_recovery=prefer_recovery)
                if loaded:
                    loaded_state = loaded
                    restored_has_messages = has_real_messages(loaded_state)
                    for block in loaded_state.get("compressed_msgs", []):
                        ctx_id = block.compressed_context_id
                        if ctx_id:
                            compressed_blocks_map[ctx_id] = block
                            ui.add_compressed_block(
                                ctx_id,
                                block.summary_text,
                                len(block.messages),
                                block.turn_range,
                            )
            except Exception:
                pass
        s.set_has_messages(restored_has_messages)

        # Reseed the Tasks panel from the restored state (covers session
        # switch AND rollback-to-N) via the shared tolerant decoder — an
        # ad-hoc parse could crash the sidebar on malformed/legacy state.
        ui.set_todo_state(parse_todo_items(loaded_state.get("chrys_todos") if loaded_state else None))

        # Replay conversation history from saved state
        messages: list[dict[str, Any]] | None = None
        if s.state_store:
            messages = await s.state_store.load_session_raw(event.session_id, prefer_recovery=prefer_recovery)
            if messages:
                file_snapshots = self.load_file_edit_snapshots(event.session_id, messages, loaded_state)

                await ui.replay_history(
                    messages,
                    initial_profile=event.initial_agent_profile or display,
                    file_snapshots=file_snapshots or None,
                    compressed_blocks=compressed_blocks_map or None,
                    recovered_from_sidecar=event.recovered_from_sidecar,
                )
                s.update_toc()

        # Check if the session ended with an error/interrupt — enable retry
        if messages:
            for m in reversed(messages):
                ct = m.get("additional_properties", {}).get(HistoryMarkerKind.KEY)
                if ct == HistoryMarkerKind.TURN:
                    continue
                if ct == HistoryMarkerKind.INTERRUPTED:
                    source = m.get("additional_properties", {}).get("_interrupted_by", "user")
                    ui.set_input_retry(INPUT_RETRY.bind() if source == "error" else INPUT_CONTINUE.bind())
                break

        # Restored workspace roots are backend-confirmed — record them in the
        # MRU as a detached background task (never inline in the handler).
        schedule_workspace_mru_touches(
            [cwd, *event.working_dirs],
            max_entries=self._services.workspace_mru_max_entries,
            session_id=event.session_id or "",
            used_at=event.timestamp,
        )
        short_id = session_short_id(event.session_id)
        info = STATUS_SESSION_RESTORED.bind(session_id=short_id)
        ui.flash_status(info)
        self._agent_load.finish_agent_load(_FINISH_SESSION_RESTORED.bind(session_id=short_id))
        s.debug("SessionRestored", short_id)
        self._callbacks.post_gc_message(GcReclaimRequested(GcReclaimReason.SESSION_RESTORED, prompt=True))

    async def on_session_saved(self, event: SessionSaved) -> None:
        self.debug("SessionSaved", session_short_id(event.session_id or ""))

    async def _restore_session_title_state(self, session_id: str, *, prefer_recovery: bool = False) -> None:
        """Reload title overlays from the session meta into the UI.

        A crash-recovered session's history comes from the recovery
        sidecar; its meta (title patches are mirrored into a live sidecar,
        and a never-saved session has no primary at all) must come from
        the same source.
        """
        s = self
        ui = self._ui()
        ui.reset_session_title_state()
        if not s.state_store:
            return
        try:
            meta = await s.state_store.load_session_meta(session_id, prefer_recovery=prefer_recovery)
        except Exception:
            return
        if meta is None:
            return
        ui.set_session_title_state(
            custom=meta.custom_title,
            generated=meta.generated_title,
            fallback=meta.title,
        )

    async def apply_custom_session_title(self, custom_title: str, session_id: str) -> None:
        """Persist a user-edited custom title (empty clears it) and refresh the UI.

        ``session_id`` is the session the edit dialog was opened for; the
        title is persisted there even if the UI has since moved to another
        session, but the on-screen title only updates while it still shows
        that session.
        """
        s = self
        ui = self._ui()
        if not session_id or not s.state_store:
            return
        try:
            async with self._custom_title_apply_lock:
                meta = await s.state_store.update_session_titles(session_id, custom_title=custom_title)
        except Exception:
            s.debug("SessionTitle", "custom title save failed")
            s.notify(
                _SESSION_TITLE_SAVE_FAILED.bind(),
                title=_SESSION_TITLE_TITLE.bind(),
                severity="error",
                timeout=3,
            )
            return
        with contextlib.suppress(Exception):
            await s.bus.publish(
                SessionTitleUpdated(
                    session_id=session_id,
                    title=custom_title,
                    custom=True,
                    # A clear falls back to the generated/first-message
                    # title; ``meta`` is None only for unsaved sessions,
                    # which have no persisted fallback to fall back to.
                    display_title=meta.display_title if meta is not None else custom_title,
                ),
            )
        if ui.chat_session_id() != session_id:
            return
        if meta is not None:
            ui.set_session_title_state(
                custom=meta.custom_title,
                generated=meta.generated_title,
                fallback=meta.title,
            )
        else:
            ui.set_session_title_state(custom=custom_title)
        definition = STATUS_SESSION_TITLE_UPDATED if custom_title else STATUS_CUSTOM_TITLE_CLEARED
        ui.flash_status(definition.bind())

    async def on_session_title_updated(self, event: SessionTitleUpdated) -> None:
        """Reflect a persisted auto-generated title for the active session."""
        ui = self._ui()
        if event.custom or event.session_id != ui.chat_session_id():
            return
        if ui.session_custom_title():
            return
        ui.set_session_title_state(generated=event.title)
        self.debug("SessionTitleUpdated", event.title)

    async def on_session_forked(self, event: SessionForked) -> None:
        s = self
        ui = self._ui()

        current_session_id = ui.chat_session_id()
        if event.session_id != current_session_id:
            return

        fork_short_id = session_short_id(event.new_session_id)
        if self._fork_dialog is None:
            try:
                await self._open_session_fork_dialog(event.session_id)
            except Exception as exc:
                s.set_agent_loading(False)
                s.notify(
                    _FORK_CREATED.bind(fork_id=fork_short_id),
                    title=_FORK_TITLE.bind(),
                    severity="information",
                    timeout=3,
                )
                s.debug("SessionForked", f"{fork_short_id}; dialog failed: {exc}")
                return

        self._fork_new_session_id = event.new_session_id
        fork_dialog = self._fork_dialog
        if fork_dialog is not None:
            fork_dialog.set_success(fork_short_id)
        s.set_agent_loading(False)
        s.notify(
            _FORK_CREATED.bind(fork_id=fork_short_id),
            title=_FORK_TITLE.bind(),
            severity="information",
            timeout=3,
        )
        s.debug("SessionForked", fork_short_id)

    def on_session_clear_error(self, event: Error, *, message: str) -> None:
        """A /clear failed before anything was deleted: the current session stays."""
        s = self
        ui = self._ui()

        s.creating_new_session = False
        s.set_agent_loading(False)
        ui.unlock_input_keep_if_locked()
        s.notify(
            _CLEAR_FAILED.bind(message=message),
            title=CLEAR_SESSION_TITLE.bind(),
            severity="error",
            timeout=5,
        )

    def on_session_fork_error(self, event: Error, *, message: str, severity: NotificationSeverity) -> None:
        """Show a fork failure in the active fork dialog, if present."""
        s = self
        ui = self._ui()

        if event.session_id and self._fork_session_id and event.session_id != self._fork_session_id:
            return

        if self._fork_dialog is not None:
            self._fork_dialog.set_error(message)
        else:
            self._reset_session_fork_dialog()
        s.set_agent_loading(False)
        ui.flash_status(STATUS_FORK_NOTICE.bind(message=message), error=severity == "error")
        ui.unlock_input_keep_if_locked()
        s.notify(message, title=_FORK_TITLE.bind(), severity=severity, timeout=5)

    async def on_profile_switched(self, event: ProfileSwitched) -> None:
        """Handle profile switch — update UI and show switch marker in chat."""
        s = self
        ui = self._ui()

        to_label = event.to_display_name or event.to_profile
        from_label = event.from_display_name or event.from_profile
        self._state.runtime.profile_name = event.to_profile
        s.runtime_metadata = event.runtime_details
        self._state.runtime.details_confirmed = True
        if event.runtime_details.model.selection_source == "active":
            self._callbacks.set_active_model_profile_id(event.runtime_details.model.profile_id)

        # Same profile (settings reload / workspace change) — update tool info
        # and context panel but skip profile switch UI.
        if event.from_profile == event.to_profile:
            self._callbacks.refresh_model_indicator()
            trail = self._format_tool_info(
                event.tool_names,
                event.skill_names,
                memory_files=event.memory_files,
                runtime_details=event.runtime_details,
            )
            ui.set_tool_info(trail)
            ui.clear_status()
            if event.max_context_tokens:
                current_context_usage = s.context_usage_state
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
            self._agent_load.finish_agent_load(_FINISH_AGENT_READY.bind(agent=to_label))
            self._callbacks.post_gc_message(GcAbsorbRequested(GcAbsorbReason.PROFILE_UI_UPDATED))
            return

        s.profile = to_label
        self._callbacks.refresh_model_indicator()
        s.update_subtitle()
        ui.set_chat_profile(to_label)
        desc = self._get_profile_description(event.to_profile)
        ui.set_status_profile(to_label, description=desc)

        if s.has_messages:
            # Each fresh switch (after messages) gets a unique key so committed
            # switch indicators stay permanently in the chat history.
            existing_from_label = s.profile_switch_from
            if existing_from_label is None:
                # Fresh switch — new unique key
                s.profile_switch_seq += 1
                key = f"profile-switch-{s.profile_switch_seq}"
                s.profile_switch_from = from_label
                s.profile_switch_to = to_label
                await ui.add_system(
                    self._render_message(_PROFILE_SWITCH_INDICATOR.bind(from_label=from_label, to_label=to_label)),
                    key=key,
                )
            elif s.profile_switch_to == from_label:
                # Consecutive switch (no messages since last switch)
                key = f"profile-switch-{s.profile_switch_seq}"
                s.profile_switch_to = to_label
                if existing_from_label == to_label:
                    await ui.remove_system(key)
                    s.profile_switch_from = None
                    s.profile_switch_to = None
                else:
                    await ui.update_system(
                        key,
                        self._render_message(
                            _PROFILE_SWITCH_INDICATOR.bind(
                                from_label=existing_from_label,
                                to_label=to_label,
                            )
                        ),
                    )
            else:
                # Chain broken (e.g. A→B then C→D where C != B) — remove old
                key = f"profile-switch-{s.profile_switch_seq}"
                await ui.remove_system(key)
                s.profile_switch_from = None
                s.profile_switch_to = None
        else:
            ui.update_welcome(profile=to_label, cwd=s.workspace_cwd())

        trail = self._format_tool_info(
            event.tool_names,
            event.skill_names,
            memory_files=event.memory_files,
            runtime_details=event.runtime_details,
        )
        ui.set_tool_info(trail)
        ui.clear_status()
        if event.sub_agent_tool_names:
            from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
            from chrys.app.tui.widgets.chat.tool_renderers import register_dynamic_renderer

            for name in event.sub_agent_tool_names:
                register_dynamic_renderer(name, SubAgentToolCall)
        s.debug("ProfileSwitched", f"{from_label} -> {to_label}")
        self._agent_load.finish_agent_load(_FINISH_PROFILE_SWITCHED.bind(from_label=from_label, to_label=to_label))
        self._callbacks.post_gc_message(GcAbsorbRequested(GcAbsorbReason.PROFILE_UI_UPDATED))

    async def on_workspace_updated(self, event: WorkspaceUpdated) -> None:
        """Handle workspace update — refresh all cwd-dependent TUI state."""
        s = self
        ui = self._ui()

        self._state.workspace.roots = [event.primary_cwd, *event.working_dirs]
        s.clear_suggestion_file_cache()
        ui.set_input_paste_cwd(event.primary_cwd)
        ui.set_chat_workspace_cwd(event.primary_cwd)
        if os.path.isdir(event.primary_cwd):
            await ui.change_shell_directory(event.primary_cwd)
        # Always track the latest cwd so callers (e.g. agent config screen)
        # can see the live workspace, even before the first message.
        previous_cwd = s.chdir_current_cwd
        s.chdir_current_cwd = event.primary_cwd
        ui.set_terminal_title_for_cwd(event.primary_cwd)

        panel_key = "chdir"
        if s.has_messages:
            if s.chdir_original_cwd is None:
                await ui.remove_system(panel_key)
                s.chdir_original_cwd = previous_cwd
                await ui.add_system(
                    self._render_message(_WORKING_DIRECTORY_INDICATOR.bind(path=DisplayPath(event.primary_cwd))),
                    key=panel_key,
                )
            else:
                if _samepath(s.chdir_original_cwd, s.chdir_current_cwd):
                    await ui.remove_system(panel_key)
                    s.chdir_original_cwd = None
                else:
                    await ui.update_system(
                        panel_key,
                        self._render_message(_WORKING_DIRECTORY_INDICATOR.bind(path=DisplayPath(event.primary_cwd))),
                    )
        else:
            ui.update_welcome(profile=s.profile, cwd=event.primary_cwd)

        # The workspace change is backend-confirmed at this point — record it
        # in the MRU as a detached background task (never inline).
        schedule_workspace_mru_touches(
            [event.primary_cwd, *event.working_dirs],
            max_entries=self._services.workspace_mru_max_entries,
            session_id=event.session_id or "",
            used_at=event.timestamp,
        )
        s.debug("WorkspaceUpdated", event.primary_cwd)
        self._callbacks.post_gc_message(GcAbsorbRequested(GcAbsorbReason.WORKSPACE_UI_UPDATED))
