# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session startup, restore, reset, fork, deletion, and shutdown orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.process_settings import reattribute_command_line, route_restart_settings
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.events.types import (
    BackgroundTasksChanged,
    Error,
    SessionClear,
    SessionDelete,
    SessionDeleted,
    SessionFork,
    SessionForked,
    SessionNew,
    SessionRestore,
    SessionRestored,
    Warning,
)
from chrys.foundation.i18n import DisplayBlock, DisplayPath, DisplaySequence, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import SUB_AGENT_STATE_DISCARDED_MESSAGE, HistoryMarkerKind
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.trajectory.event_types import RuntimeFinishReason as TrajectoryRuntimeFinishReason
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.engine.state import lifecycle_permits
from chrys.orchestration.engine.state.active_session import session_pin_overrides
from chrys.orchestration.engine.state.machine import Trigger
from chrys.orchestration.invoker.contracts import AbortCause
from chrys.orchestration.invoker.runtime import restore_phase4_state
from chrys.service.context.compaction.spill import SpillReconciliationResult, reconcile_spill_storage
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.profiles.agents.registry import resolve_launch_profile, resolve_profile_selector
from chrys.service.profiles.models.resolver import active_model_profile_fields
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES, is_model_profile_selectable
from chrys.service.session.history import stamp_history_item_ids
from chrys.service.session.persistence import has_real_messages
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.session.side_questions import BTW_FILE_NAME
from chrys.service.session.sub_agent_logs import SubAgentSessionArtifactService
from chrys.service.state.store import (
    SESSION_BACKUP_FILE_NAME,
    SESSION_FILE_NAME,
    SESSION_RECOVERY_FILE_NAME,
    SESSION_WRITE_LOCK_TIMEOUT_SECONDS,
    ChatSessionMeta,
    SessionForkError,
    SessionNotFoundError,
    parse_snapshot_turn,
)
from chrys.service.todos.tracker import TodoTracker
from chrys.service.trajectory.preparation import PreparationOutcome

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.loader import AgentLoader
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.side_questions import SideQuestions
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.state.session_writer import SessionWriter
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.orchestration.engine.usage import UsagePublisher
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.session.history import SessionHistoryManager
    from chrys.service.session.persistence import SessionPersistence


logger = logging.getLogger(__name__)
_SHUTDOWN_POST_RUN_TIMEOUT_SECONDS = 2.0
_SESSION_FORK_AGENT_LOAD_TIMEOUT_SECONDS = 5.0

_FORK_AGENT_LOADING = msg(
    "engine.fork_agent_loading",
    fallback="Cannot fork while the agent is still loading.",
)
_FORK_NOT_READY = msg(
    "engine.fork_not_ready",
    fallback="Cannot fork before a session is ready.",
)
_FORK_SESSION_CHANGED = msg(
    "engine.fork_session_changed",
    fallback="Cannot fork because the active session changed.",
)
_FORK_TURN_ACTIVE = msg(
    "engine.fork_turn_active",
    fallback="Cannot fork while a turn is running.",
)
_FORK_STATE_STORE_MISSING = msg(
    "engine.fork_state_store_missing",
    fallback="State store not configured.",
)
_FORK_RESTORING = msg(
    "engine.fork_restoring",
    fallback="Cannot fork while session state is being restored.",
)
_FORK_LOCK_NOT_OWNED = msg(
    "engine.fork_lock_not_owned",
    fallback="Cannot fork because this window does not own the active session lock.",
)
_FORK_EMPTY_SESSION = msg(
    "engine.fork_empty_session",
    fallback="Cannot fork an empty session.",
)
_FORK_PREPARE_TIMEOUT = msg(
    "engine.fork_prepare_timeout",
    fallback="Timed out preparing session for fork: {detail}",
    multiline=True,
)
_FORK_PREPARE_FAILED = msg(
    "engine.fork_prepare_failed",
    fallback="Failed to prepare session for fork: {detail}",
    multiline=True,
)
_FORK_SESSION_NOT_FOUND = msg(
    "engine.fork_session_not_found",
    fallback="Session '{session_id}' not found.",
)
_FORK_FAILED = msg(
    "engine.fork_failed",
    fallback="Failed to fork session: {detail}",
    multiline=True,
)


_CONSTRUCTION_TRAJECTORY_ACTIVATION_FAILED = msg(
    "construction.trajectory_activation_failed",
    fallback="Detailed trajectory recording is off for this session; your task can continue.",
)


_RESTORE_SERVICE_SESSION_INCOMPATIBLE = msg(
    "restore.service_session_incompatible",
    fallback=(
        "This session was saved with an OpenAI Responses service session. "
        "The active agent/model profile, workspace, service endpoint, or storage mode "
        "is not compatible, so {app} will continue from local history only."
    ),
)
_RESTORE_SUB_AGENTS_DISCARDED = msg(
    "restore.sub_agents_discarded",
    fallback="{discarded} paused sub-agent(s) from a previous session were discarded: {names}",
)
_RESTORE_AGENT_PROFILE_UNRESOLVED_USING_CURRENT = msg(
    "restore.agent_profile_unresolved_using_current",
    fallback=(
        "The saved agent profile {saved} could not be uniquely resolved. Continuing with the current agent {current}."
    ),
)
_RESTORE_AGENT_PROFILE_UNRESOLVED = msg(
    "restore.agent_profile_unresolved",
    fallback="The saved agent profile {saved} could not be uniquely resolved. Session restore was stopped.",
)
_RESTORE_SESSION_CWD_MISSING = msg(
    "restore.session_cwd_missing",
    fallback="The working directory of this session no longer exists: {path}",
)
_RESTORE_REQUESTED_AGENT_PROFILE_UNRESOLVED = msg(
    "restore.requested_agent_profile_unresolved",
    fallback="The requested agent profile {profile} could not be found. Session restore was stopped.",
)


def _working_dirs_from_paths(paths: list[str], primary_cwd: str) -> list[WorkingDir]:
    return [WorkingDir(path=path, is_primary=path == primary_cwd) for path in paths]


def _restore_profile_override_switch(
    state: dict | None,
    *,
    from_profile: str,
    from_display: str,
    to_profile: str,
    to_display: str,
) -> bool:
    """Record an explicit restore-time profile override in session state."""
    if state is None or not from_profile or from_profile == to_profile:
        return False
    switches = state.setdefault("agent_profile_switches", [])
    switches.append(
        {
            "from": from_profile,
            "to": to_profile,
            "from_display": from_display or from_profile,
            "to_display": to_display or to_profile,
            "at_message_index": len(state.get("messages", [])),
            "timestamp": datetime.now(UTC).isoformat(),
        },
    )
    return True


def _extra_roots(paths: list[str], primary_cwd: str) -> list[str]:
    """Working-dir paths with the primary cwd removed (effective extra roots).

    Sessions persist ``working_dirs`` inconsistently: a no-extra session stores
    ``[]`` while a multi-root session stores ``[primary, *extras]``. Restore also
    always re-inserts the primary for explicit ``additionalDirectories``. Service-
    session identity only cares about the *effective* extra roots, so strip the
    primary from both sides before comparing — otherwise loading a no-extra session
    with ``additionalDirectories: []`` would discard a still-valid service session.
    """
    return [path for path in paths if path != primary_cwd]


def _reset_restore_history_state(history_state: dict) -> dict:
    """Deep copy of the live history captured before a reset deletes session files.

    A failed reset restarts the engine from this copy, so it must own every
    layer (messages, contents lists, content objects) — aliasing the live
    objects of the session being shut down would let the restarted session
    share identities with a torn-down state.
    """
    return copy.deepcopy(history_state)


def cleanup_empty_session_dir_path(session_dir: Path | None, *, path_reused: bool = False) -> None:
    """Remove *session_dir* when it has no restorable session files.

    ``path_reused`` is for a caller that restarts on this very directory (a
    reset keeps the session id).
    """
    if session_dir is None or not session_dir.is_dir():
        return
    if (session_dir / SESSION_FILE_NAME).exists():
        return
    if (session_dir / SESSION_BACKUP_FILE_NAME).exists():
        return
    if (session_dir / SESSION_RECOVERY_FILE_NAME).exists():
        return
    if _has_restorable_rollback_snapshots(session_dir):
        return
    if _has_committed_sub_agent_artifacts(session_dir):
        return
    if _has_recorded_trajectory(session_dir):
        return
    from chrys.service.trajectory.tombstone import delete_session_directory

    try:
        # Same disposal as an explicit delete: a trajectory writer that is
        # still alive (a stuck worker, a writer in another process) turns this
        # into a logical delete instead of a half-removed folder.
        delete_session_directory(session_dir, sessions_root=session_dir.parent, path_reused=path_reused)
    except OSError:
        logger.debug("Failed to remove empty session directory %s", session_dir, exc_info=True)


def _delete_reset_session_files(session_dir: Path) -> None:
    for filename in (SESSION_FILE_NAME, SESSION_BACKUP_FILE_NAME, SESSION_RECOVERY_FILE_NAME, BTW_FILE_NAME):
        path = session_dir / filename
        if path.exists():
            path.unlink()


def _acquire_session_write_lock(lock_path: Path, session_id: str) -> FileLock | None:
    lock = FileLock(lock_path, timeout=SESSION_WRITE_LOCK_TIMEOUT_SECONDS)
    try:
        lock.acquire()
        return lock
    except TimeoutError:
        logger.warning("Timed out acquiring session lock for reset %s", session_id, exc_info=True)
        return None
    except OSError:
        logger.warning("Failed to acquire session lock for reset %s", session_id, exc_info=True)
        return None


def _has_restorable_rollback_snapshots(session_dir: Path) -> bool:
    snap_dir = session_dir / "snapshots"
    if not snap_dir.is_dir():
        return False
    return any(parse_snapshot_turn(path) >= 1 for path in snap_dir.glob("*.json"))


def _has_recorded_trajectory(session_dir: Path) -> bool:
    """Whether this directory holds a log that outlives the conversation in it.

    A rollback only ever appends to the log — including the reset to welcome,
    which discards the session's messages but restarts on this very directory
    and reads its branch from the last line written. Only an explicit clear or
    delete takes a session's trajectory with it, and neither comes through
    here.
    """
    from chrys.service.trajectory.session import trajectory_events_path

    events = trajectory_events_path(session_dir)
    try:
        return events.stat().st_size > 0
    except FileNotFoundError:
        return False
    except OSError:
        # This is the guard that keeps an audit log from being deleted, so a
        # probe that cannot answer keeps the directory: "I could not look" is
        # not "there is nothing there".
        return True


def _has_committed_sub_agent_artifacts(session_dir: Path) -> bool:
    sub_agents = session_dir / "sub_agents"
    if not sub_agents.is_dir():
        return False
    for path in sub_agents.rglob("*.json"):
        if path.name.endswith(".tmp"):
            continue
        if path.is_file():
            return True
    return False


@dataclass(frozen=True, slots=True)
class _SessionDeleteFailure:
    """Why a session delete did not happen; the current session is intact."""

    code: str
    message: str


class SessionLifecycle:
    """Orchestrates session startup, reload, restore, reset, fork, deletion, and shutdown."""

    def __init__(
        self,
        *,
        session: ActiveSession,
        current: CurrentAgent,
        loader: AgentLoader,
        permits: LifecyclePermits,
        writer: SessionWriter,
        turn_state: TurnRuntimeState,
        usage_publisher: UsagePublisher,
        bus: EventBus,
        fsm: EngineStateMachine,
        history: SessionHistoryManager,
        persistence: SessionPersistence,
        settings_handle: SettingsHandle,
        agent_registry: AgentProfileRegistry | None,
        model_registry: ModelProfileRegistry | None,
        trajectory_recorder: TrajectoryRecorder,
        workspace_change_tracker: WorkspaceChangeTracker,
        unregister_current_engine: Callable[[], None],
        side_questions: SideQuestions,
    ) -> None:
        self._session = session
        self._current = current
        self._loader = loader
        self._permits = permits
        self._writer = writer
        self._turn_state = turn_state
        self._usage_publisher = usage_publisher
        self._bus = bus
        self._fsm = fsm
        self._history = history
        self._persistence = persistence
        self._settings_handle = settings_handle
        self._agent_registry = agent_registry
        self._model_registry = model_registry
        self._trajectory_recorder = trajectory_recorder
        self._workspace_change_tracker = workspace_change_tracker
        self._unregister_current_engine = unregister_current_engine
        self._side_questions = side_questions

    def reset_turn_runtime_after_session_shutdown(self) -> None:
        """Clear turn runtime state after shutdown has observed the old task."""
        prompt_admission_owner = self._permits.prompt_admission_owner_for_current_task()
        self._turn_state.reset_after_session_shutdown(
            prompt_admission_owner=prompt_admission_owner,
        )

    async def fire_session_end_hooks(self) -> None:
        """Fire ``session_end`` for the live session and wait for it to finish.

        Fires at most once per session.  ``shutdown()`` is the usual caller;
        deleting the ACTIVE session calls it first, while ``_session_id`` and
        the session files still exist, and the later shutdown then skips the
        duplicate.  ``fire()`` only *spawns* ``async``-mode hooks, so this
        also drains them (without closing the manager) — otherwise a delete
        could remove the files before such a hook ran.  The next ``start()``
        re-arms it for the session it brings up.
        """
        hook_manager = self._session.hook_manager
        if hook_manager is None or self._session.session_end_fired:
            return
        from chrys.service.hooks.events import HookEvent

        self._session.mark_session_end_fired()
        profile_name = self._session.agent_profile.name if self._session.agent_profile is not None else ""
        await hook_manager.fire(
            HookEvent.SESSION_END,
            {"session_id": self._session.session_id, "profile": profile_name, "cwd": self._session.workspace_cwd()},
            scope="session",
        )
        await hook_manager.drain_session(close=False)

    async def close_trajectory_log(self) -> None:
        """Close the trajectory writer so the session directory can be removed.

        Deleting the ACTIVE session removes its folder while this runtime is
        still up. A live writer holds the log's lease, and a leased directory
        is only tombstoned — swept at the next store startup, so the files a
        user asked to delete would outlive the request for the rest of the
        run. Closing first makes the delete physical.

        A delete that then fails (the lock is busy) leaves the session
        running unrecorded until the next runtime resumes its log. That is
        the recorder's standing contract — recording never holds a session
        up — and the log it leaves behind is closed, not torn.
        """
        await self._trajectory_recorder.close(reason=TrajectoryRuntimeFinishReason.SESSION_SWITCH)

    def prepare(self, profile: AgentProfile | None) -> None:
        """Seed the fallback profile used by session restores, and the agent new sessions start on.

        Seeded here, where the launch profile is known, rather than by the first
        start: with ``-s`` that start is a restore, whose agent is that
        session's own.
        """
        if profile is not None:
            self._session.agent_profile = profile
            self._session.new_session_agent = profile.name

    async def start(
        self,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        """Build and install the agent from a profile under a lifecycle permit.

        *session* is the session's own choices to build with (the installed
        ones when None), committed with the build (``AgentLoader.stage``).
        """
        if (
            self._permits.current_task_owns_rebuild_permit()
            or self._permits.current_task_owns_session_transition_permit()
        ):
            await self._start_locked(
                profile, operation=operation, staged_loaded=staged_loaded, session=session, workspace=workspace
            )
            return
        token = self._permits.capture_control_token()
        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, lifecycle_permits.RebuildPermitDenied):
            raise RuntimeError(permit.message)
        try:
            await self._start_locked(
                profile, operation=operation, staged_loaded=staged_loaded, session=session, workspace=workspace
            )
        finally:
            self._permits.release_rebuild_permit(permit)

    async def _start_locked(
        self,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        """Start the engine while the caller owns the rebuild boundary."""
        # Every session (fresh, restored, reset) starts here: re-arm the
        # once-per-session ``session_end`` hook fired by shutdown/delete.
        self._session.begin(agent_profile=profile, workspace=workspace)
        if not self._session.new_session_agent and operation == "startup":
            # A host that never named a launch profile (ACP, headless): the agent
            # it starts on is the one new sessions go back to. Every other start
            # runs on a session's agent (a restore's, a switch's, the live one a
            # rebuild keeps), which never becomes the default.
            self._session.new_session_agent = profile.name
        if self._session.session_id is None:
            raise RuntimeError("Session begin did not establish a session id.")
        staged_workspace = workspace if workspace is not None else self._session.workspace

        if not self._session.guard.ensure(self._session.session_id):
            message = self._session.guard.conflict_message(self._session.session_id)
            await self._bus.publish(Error(code="session_in_use", message=message, session_id=self._session.session_id))
            raise RuntimeError(message)

        # Build the hook manager once per engine instance when a hook config
        # file exists.  With no config file, hooks stay a true no-op: no
        # manager, no outbox/log/tmp directories, and no recovery task.
        #
        # The manager survives profile switches within the same session —
        # hooks are global, not profile-scoped. A no-executor workspace-change
        # retry can happen after a startup failure, so reload there because
        # project hooks are primary-cwd scoped. The candidate stays staged: it
        # goes live with the build's commit, and until then the old manager
        # keeps running untouched.
        old_hook_manager = self._session.hook_manager
        hook_manager = await self._loader.stage_hook_manager(
            operation=operation, staged_loaded=staged_loaded, workspace=staged_workspace
        )
        if (old_hook_manager is None or operation == "workspace_change") and hook_manager is not None:
            # Session / turn lifecycle hooks fire outside any model run, so
            # they record under the recorder's current scope instead.
            hook_manager.trajectory_context_provider = self._trajectory_recorder.context

        session_dir = self._session.session_dir if self._persistence.state_store is not None else None
        if session_dir is not None and self._session.background_tasks is None:
            from chrys.service.tools.background import BackgroundTasks

            session_id = self._session.session_id

            async def _publish_background_tasks(tasks: BackgroundTasks) -> None:
                await self._bus.publish(
                    BackgroundTasksChanged(session_id=session_id, tasks=tasks.views(), full=tasks.full)
                )

            self._session.background_tasks = BackgroundTasks(
                session_dir,
                turn=lambda: self._session.turn_number,
                trajectory=self._trajectory_recorder.context,
                on_change=_publish_background_tasks,
                surface=self._session.surface or "",
            )

        from chrys.foundation.observability.sink import get_otel_sink

        otel_sink = get_otel_sink()
        if otel_sink is not None:
            otel_sink.activate(self._session.session_id, session_dir)
        # The trajectory recorder is bound here and activates (opens the log,
        # takes the writer lease) on its first event, so a session that never
        # records anything never creates ``trajectory/``.
        event_loop = asyncio.get_running_loop()
        trajectory_session_id = self._session.session_id
        trajectory_warning_tasks: set[asyncio.Task[None]] = set()

        def _report_trajectory_activation_failure(reason: str) -> None:
            def _publish_warning() -> None:
                task = event_loop.create_task(
                    self._bus.publish(
                        Warning(
                            code="trajectory_activation_failed",
                            message="Trajectory recording could not start and has been disabled for this session.",
                            display_message=_CONSTRUCTION_TRAJECTORY_ACTIVATION_FAILED.bind(),
                            session_id=trajectory_session_id,
                            reason=reason,
                        )
                    )
                )
                trajectory_warning_tasks.add(task)
                task.add_done_callback(trajectory_warning_tasks.discard)

            try:
                event_loop.call_soon_threadsafe(_publish_warning)
            except RuntimeError:
                logger.debug("Trajectory activation failure could not be published because the event loop is closed")

        trajectory = self._trajectory_recorder.bind_session(
            session_id=trajectory_session_id,
            session_dir=session_dir,
            write_lock_path=self._session.session_write_lock_path(trajectory_session_id)
            if session_dir is not None
            else None,
            session_start_info=self._loader.trajectory_session_start_info,
            on_activation_failed=_report_trajectory_activation_failure,
        )

        await self._loader.load(
            profile,
            operation=operation,
            staged_loaded=staged_loaded,
            session=session,
            workspace=staged_workspace,
            hook_manager=hook_manager,
            trajectory=trajectory,
            old_hook_manager=old_hook_manager,
        )

    async def start_with_rebuild_permit(
        self,
        permit: lifecycle_permits.RebuildPermit,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
    ) -> None:
        """Start using an already-acquired rebuild permit."""
        self._permits.ensure_rebuild_permit(permit)
        await self.start(
            profile, operation=operation, staged_loaded=staged_loaded, session=session, workspace=workspace
        )

    async def reload(
        self,
        new_profile: AgentProfile,
        workspace: Workspace | None = None,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        """Restart the agent with a new profile/workspace while preserving history."""
        if (
            self._permits.current_task_owns_rebuild_permit()
            or self._permits.current_task_owns_session_transition_permit()
        ):
            await self._reload_locked(
                new_profile, workspace, operation=operation, staged_loaded=staged_loaded, session=session
            )
            return
        token = self._permits.capture_control_token()
        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, lifecycle_permits.RebuildPermitDenied):
            raise RuntimeError(permit.message)
        try:
            await self._reload_locked(
                new_profile, workspace, operation=operation, staged_loaded=staged_loaded, session=session
            )
        finally:
            self._permits.release_rebuild_permit(permit)

    async def _reload_locked(
        self,
        new_profile: AgentProfile,
        workspace: Workspace | None = None,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        """Soft-restart while the caller owns the rebuild boundary."""
        await self._loader.reload(
            new_profile, workspace, operation=operation, staged_loaded=staged_loaded, session=session
        )

    async def reload_with_rebuild_permit(
        self,
        permit: lifecycle_permits.RebuildPermit,
        new_profile: AgentProfile,
        workspace: Workspace | None = None,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        """Soft-restart using an already-acquired rebuild permit."""
        self._permits.ensure_rebuild_permit(permit)
        await self.reload(
            new_profile, workspace=workspace, operation=operation, staged_loaded=staged_loaded, session=session
        )

    async def close_session(self) -> None:
        """Close the session and release its lock, retaining process resources.

        The session's own agent and model end with it (a new session, a clear, a
        restore of another): the next start begins on the agent new sessions
        start on and the default model. A shutdown leaves them, since nothing
        starts on an engine after it. A teardown that raised before it released the runtime leaves the
        session running on the build both were configured for, so both stay.
        Closing in place (:meth:`close_session_in_place`) keeps them.
        """
        try:
            await self._teardown(release_lock=True, close_mcp=False)
        finally:
            if self._current.loaded is None:
                self._end_session_choice()

    def _end_session_choice(self) -> None:
        """Put the agent and model a new session starts on in force for whatever starts next.

        Synchronous on purpose: it runs in ``finally`` blocks a cancel passes
        through.
        """
        profile = self.new_session_profile()
        if profile is not None:
            self._session.agent_profile = profile
        self._settings_handle.install_prepared(self._settings_handle.prepare(self._settings_handle.base, session={}))

    def new_session_profile(self) -> AgentProfile | None:
        """The agent a new session starts on, as the registry has it now; None when none was named.

        ``ActiveSession.new_session_agent`` while it is still a main agent, else
        the launch chain from the configured default agent
        (:func:`resolve_launch_profile`). None while no agent was named (a host
        that has not started yet, or started only by restoring a session):
        callers keep the agent they have.
        """
        registry = self._agent_registry
        name = self._session.new_session_agent
        if registry is None or not name:
            return None
        kept = resolve_profile_selector(registry.list_profiles(), name)
        return kept or resolve_launch_profile(registry, self._settings_handle.settings.default_agent)

    def _restore_fallback_profile(self, *, restoring_current: bool) -> AgentProfile | None:
        """The agent a restore uses when the saved one does not resolve.

        The current one when restoring the current session; else the one new
        sessions start on — never another session's own (a prompt shortcut's).
        """
        if restoring_current:
            return self._session.agent_profile
        return self.new_session_profile() or self._session.agent_profile

    async def close_session_in_place(self, *, keep_side_answers: bool = False) -> None:
        """Close the runtime while retaining its lock and process resources.

        Restoring the same session keeps its side answers and queued saves, but stops running questions.
        """
        await self._teardown(release_lock=False, close_mcp=False, keep_side_answers=keep_side_answers)

    async def shutdown(self) -> None:
        """Close the session and process resources."""
        await self._teardown(release_lock=True, close_mcp=True)

    async def _stop_background_tasks(self) -> None:
        """Stop the session's background commands for good; the next start of a session takes new ones."""
        tasks, self._session.background_tasks = self._session.background_tasks, None
        if tasks is not None:
            await tasks.close()

    async def _teardown(self, *, release_lock: bool, close_mcp: bool, keep_side_answers: bool = False) -> None:
        """Gracefully shut down the engine."""
        self._session.mark_closing()
        if keep_side_answers:
            await self._side_questions.stop_running()
        else:
            await self._side_questions.quiesce()
        self._turn_state.shutdown_used_cancel_fallback = False
        suppress_trailing_save = False
        self._unregister_current_engine()
        self._fsm.try_transition(Trigger.SHUTDOWN)
        if self._current.loaded is not None and self._current.loaded.bindings.state.running:
            handle = self._current.loaded.bindings.backend.active_handle
            if handle is not None:
                await self._current.loaded.bindings.backend.abort(handle, AbortCause.OWNER_CLOSE)
        run_task = self._turn_state.lease.run_task
        if run_task is not None and not run_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(run_task),
                    timeout=_SHUTDOWN_POST_RUN_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                self._turn_state.shutdown_used_cancel_fallback = True
                suppress_trailing_save = True
                run_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await run_task
            except asyncio.CancelledError:
                pass
        self._turn_state.lease.release_run_task()
        await self._turn_state.lease.settle_notifications()
        # A cancelled/timed-out worker can leave a queued retry or active
        # admission behind. Terminalize their preparations while the trajectory
        # writer is still open; the post-shutdown reset is too late to emit.
        self._turn_state.lease.clear_pending_retry(outcome=PreparationOutcome.DROPPED)
        self._turn_state.lease.clear_active_admissions(outcome=PreparationOutcome.OWNER_CHANGED)
        self._turn_state.lease.clear_pre_admission_preparations()
        if self._current.loaded is not None:
            for injection in self._current.loaded.injection.drain_pending():
                if injection.preparation is not None:
                    injection.preparation.finished_soon(
                        outcome=PreparationOutcome.TARGET_STALE,
                        target_turn_id=injection.target_turn_id,
                    )
        await self._loader.cancel_outbox_recovery()
        # Background commands never outlive their session; an in-place close
        # (rollback) keeps them.
        if release_lock:
            await self._stop_background_tasks()
        # Drain any queued UsageUpdate publish tasks before the bus is torn
        # down — orphaned tasks otherwise either deliver a stale UsageUpdate
        # into the next session's subscriber set (engine instance is reused
        # across session restore / new-session) or get garbage-collected mid-
        # await, producing "Task was destroyed but it is pending" warnings on
        # process exit.
        await self._usage_publisher.settle()
        # Fire ``session_end`` hooks BEFORE we tear down anything — they
        # may want a live session to inspect.  Then drain any in-flight
        # async hooks within the configured shutdown grace.  Detached
        # hooks survive this call by design.
        if self._session.hook_manager is not None:
            await self.fire_session_end_hooks()
            await self._session.hook_manager.drain_session()
            # ``drain_session()`` closes the manager.  The same engine
            # instance is reused for restore/new-session flows, so force
            # the next ``start()`` to reload config and create a fresh
            # manager instead of carrying a closed no-op instance forward.
            self._session.hook_manager = None
        # Auto-save before shutdown
        with self._session.saves_suppressed() if suppress_trailing_save else contextlib.nullcontext():
            await self._writer.save_current_session()
        # Stamp the coordination registry closed (peers stop warning
        # about us); files are kept for peers' late rollbacks — GC only.
        if self._session.mutation_coordinator is not None:
            try:
                await asyncio.get_running_loop().run_in_executor(None, self._session.mutation_coordinator.close)
            except Exception:
                logger.debug("Mutation coordinator close failed", exc_info=True)
        # The trajectory writer must be closed before the directory it writes
        # into can be judged empty. ``close_mcp=False`` is how the
        # session-switch flows (new/restore/clear/reset) shut the engine down
        # while the process keeps running; every other caller is exiting.
        await self._trajectory_recorder.close(
            reason=(
                TrajectoryRuntimeFinishReason.GRACEFUL_SHUTDOWN
                if close_mcp
                else TrajectoryRuntimeFinishReason.SESSION_SWITCH
            ),
        )
        # Clean up empty session directory (no messages were sent)
        self.cleanup_empty_session_dir()
        await self._loader.release_current()
        self._turn_state.paused_sub_agents.clear()
        if close_mcp:
            await self._loader.close()
        if release_lock:
            self._session.guard.release()

    async def refresh_mutation_attribution(self, *, force: bool = False) -> bool:
        """Reclassify the live mutation log against peer registry claims.

        The engine-level read-side entry point for cross-session
        coordination: display
        surfaces call it before building net summaries; the rollback
        path calls it with ``force=True`` as the authoritative last
        check.  Returns True when any row changed — the change is
        persisted via the normal session-state save so serialized
        consumers (TUI /diff) observe it too.
        """
        coordinator = self._session.mutation_coordinator
        tracker = self._session.mutation_tracker
        if coordinator is None or tracker is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            changed = await loop.run_in_executor(
                None,
                lambda: coordinator.reclassify(tracker, force=force, fallback_root=self._session.workspace_cwd()),
            )
        except Exception:
            logger.debug("Mutation attribution refresh failed", exc_info=True)
            return False
        # The coordinator's flag, not this call's return value: a
        # finalize-time reclassify may have changed rows earlier with no
        # saver, and its signature update makes this very call report
        # "unchanged".  While a run is active the save must not happen here:
        # a primary save deletes the recovery sidecar, which mid-turn is the
        # only durable copy of committed-but-unmerged tool exchanges.  The
        # flag stays set, and the turn-end save persists the reclassification.
        if (
            not (self._current.loaded is not None and self._current.loaded.bindings.state.running)
            and self._session.mutation_coordinator is not None
            and self._session.mutation_coordinator.consume_unsaved_reclassification()
        ):
            await self._writer.save_current_session()
        return changed

    async def on_session_fork(self, event: SessionFork) -> None:
        """Handle session fork requests for the active saved session."""
        try:
            await asyncio.wait_for(
                self._permits.wait_for_agent_load_idle(),
                timeout=_SESSION_FORK_AGENT_LOAD_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            await self._publish_session_fork_error(
                "session_fork_busy",
                "Cannot fork while the agent is still loading.",
                session_id=event.session_id or self._session.session_id or "",
                display_message=_FORK_AGENT_LOADING.bind(),
            )
            return

        # Side-question history reads may yield to a rebuild or a new turn.
        # All fork admission checks below must observe the state after those waits.
        has_side_questions = False
        if (
            event.session_id == self._session.session_id
            and self._current.loaded is not None
            and not self._fsm.is_running()
        ):
            has_side_questions = await self._side_questions.has_history()
        active_session_id = self._session.session_id
        state_store = self._persistence.state_store
        if active_session_id is None or self._current.loaded is None:
            await self._publish_session_fork_error(
                "session_fork_not_ready",
                "Cannot fork before a session is ready.",
                session_id=active_session_id or event.session_id or "",
                display_message=_FORK_NOT_READY.bind(),
            )
            return
        if event.session_id != active_session_id:
            await self._publish_session_fork_error(
                "session_fork_stale",
                "Cannot fork because the active session changed.",
                session_id=event.session_id or active_session_id,
                display_message=_FORK_SESSION_CHANGED.bind(),
            )
            return
        if self._fsm.is_running():
            await self._publish_session_fork_error(
                "session_fork_busy",
                "Cannot fork while a turn is running.",
                session_id=active_session_id,
                display_message=_FORK_TURN_ACTIVE.bind(),
            )
            return
        if state_store is None:
            await self._publish_session_fork_error(
                "session_fork_failed",
                "State store not configured.",
                session_id=active_session_id,
                display_message=_FORK_STATE_STORE_MISSING.bind(),
            )
            return
        if self._session.suppress_save:
            await self._publish_session_fork_error(
                "session_fork_busy",
                "Cannot fork while session state is being restored.",
                session_id=active_session_id,
                display_message=_FORK_RESTORING.bind(),
            )
            return
        if not self._session.guard.owns(active_session_id):
            await self._publish_session_fork_error(
                "session_fork_busy",
                "Cannot fork because this window does not own the active session lock.",
                session_id=active_session_id,
                display_message=_FORK_LOCK_NOT_OWNED.bind(),
            )
            return
        if not has_real_messages(self._current.loaded.bindings.backend.history_state) and not has_side_questions:
            await self._publish_session_fork_error(
                "session_fork_empty",
                "Cannot fork an empty session.",
                session_id=active_session_id,
                display_message=_FORK_EMPTY_SESSION.bind(),
            )
            return

        try:
            await self._writer.save_current_session(raise_on_error=True)
            fresh_state = await state_store.load_session(active_session_id)
        except TimeoutError as exc:
            logger.warning("Timed out preparing session %s for fork", active_session_id, exc_info=True)
            await self._publish_session_fork_error(
                "session_fork_busy",
                f"Timed out preparing session for fork: {exc}",
                session_id=active_session_id,
                display_message=_FORK_PREPARE_TIMEOUT.bind(detail=DisplayBlock(str(exc))),
            )
            return
        except Exception as exc:
            logger.warning("Failed to prepare session %s for fork", active_session_id, exc_info=True)
            await self._publish_session_fork_error(
                "session_fork_failed",
                f"Failed to prepare session for fork: {exc}",
                session_id=active_session_id,
                display_message=_FORK_PREPARE_FAILED.bind(detail=DisplayBlock(str(exc))),
            )
            return
        if fresh_state is None or (not has_real_messages(fresh_state) and not has_side_questions):
            await self._publish_session_fork_error(
                "session_fork_empty",
                "Cannot fork an empty session.",
                session_id=active_session_id,
                display_message=_FORK_EMPTY_SESSION.bind(),
            )
            return

        try:
            # Forking is work on the new session, so it starts out on this launch's surface.
            new_session_id = await asyncio.to_thread(
                state_store.fork_session, active_session_id, last_surface=self._session.surface
            )
        except SessionNotFoundError:
            await self._publish_session_fork_error(
                "session_fork_not_found",
                f"Session '{active_session_id}' not found.",
                session_id=active_session_id,
                display_message=_FORK_SESSION_NOT_FOUND.bind(session_id=active_session_id),
            )
            return
        except SessionForkError as exc:
            logger.warning("Failed to fork session %s", active_session_id, exc_info=True)
            await self._publish_session_fork_error(
                "session_fork_failed",
                f"Failed to fork session: {exc}",
                session_id=active_session_id,
                display_message=_FORK_FAILED.bind(detail=DisplayBlock(str(exc))),
            )
            return

        # The fork copies the conversation, not the parent's trajectory; it
        # gets its own closed opening runtime pointing back at the parent.
        await self._trajectory_recorder.fork(
            origin_session_id=active_session_id,
            fork_session_id=new_session_id,
            fork_session_dir=self._session.session_dir_for(new_session_id),
            fork_write_lock_path=self._session.session_write_lock_path(new_session_id),
            session_start_info=self._loader.trajectory_session_start_info(),
        )
        await self._bus.publish(
            SessionForked(
                session_id=active_session_id,
                parent_session_id=active_session_id,
                new_session_id=new_session_id,
            )
        )

    async def _publish_session_fork_error(
        self, code: str, message: str, *, session_id: str, display_message: MessageRef | None = None
    ) -> None:
        await self._bus.publish(
            Error(code=code, message=message, session_id=session_id, display_message=display_message)
        )

    async def _reconcile_existing_spill_storage(self) -> SpillReconciliationResult:
        """Rebuild and account for retained spill storage without creating a session directory."""
        session_dir = self._session.session_dir
        if session_dir is None or not session_dir.is_dir():
            return SpillReconciliationResult(0, 0, frozenset())
        try:
            return await asyncio.to_thread(reconcile_spill_storage, session_dir, self._session.spill_quota)
        except OSError, RuntimeError, UnicodeError:
            # Spill records are auxiliary context. Filesystem damage or permissions
            # must not prevent the authoritative session state from hydrating.
            logger.warning("Unable to reconcile spill storage under %s", session_dir, exc_info=True)
            self._session.spill_quota.disable_storage()
            return SpillReconciliationResult(0, 0, frozenset())

    def _workspace_cwd(self) -> str:
        """Return the engine workspace cwd, falling back only for legacy unstarted engines."""
        if self._session.workspace is not None:
            return self._session.workspace.primary_cwd
        return safe_getcwd()

    def _can_restore_service_session(self, meta: ChatSessionMeta) -> bool:
        """Return True when the active model can continue a saved service session id."""
        profile = self._current.manifest.active_profile
        if (
            profile is None
            or self._current.loaded is None
            or meta.model_provider != "openai"
            or meta.model_api_style != API_STYLE_RESPONSES
            or profile.provider != "openai"
            or profile.api_style != API_STYLE_RESPONSES
            or meta.model_id != profile.model_id
            or not meta.model_profile_fingerprint
            or meta.model_profile_fingerprint != self._current.manifest.model_profile_fingerprint
            or not meta.agent_profile_fingerprint
            or meta.agent_profile_fingerprint != self._current.manifest.agent_profile_fingerprint
            or self._session.workspace is None
            or meta.primary_cwd != self._session.workspace.primary_cwd
            or _extra_roots(meta.working_dirs, meta.primary_cwd)
            != _extra_roots(
                [working_dir.path for working_dir in self._session.workspace.working_dirs],
                self._session.workspace.primary_cwd,
            )
        ):
            return False
        from chrys.service.llm.clients import effective_model_base_url

        if meta.model_base_url != effective_model_base_url(profile):
            return False
        return self._current.loaded.bindings.backend.service_session_storage_enabled

    def _restore_terminal_fsm_from_history(self) -> None:
        """Rebuild runtime terminal state from persisted history markers."""
        if not self._history.is_bound:
            return
        marker = self._history.trailing_status_marker()
        if marker is None:
            return
        kind, source = marker
        if kind != HistoryMarkerKind.INTERRUPTED:
            return
        self._fsm.restore_terminal_state(failed=source == "error")

    def _session_pin_overrides(self) -> dict[str, Any]:
        """Snapshot the per-session pins every re-load must carry (:func:`session_pin_overrides`)."""
        return session_pin_overrides(self._session, self._settings_handle.base.settings)

    def _reload_eval_context(self) -> EvalContext:
        """The launch mode's retry policy, passed *into* the load (as every re-load must)."""
        return EvalContext(
            frontend_default_max_transient_retries=self._settings_handle.settings.frontend_default_max_transient_retries
        )

    def _restored_session_choice(
        self,
        meta: ChatSessionMeta | None,
        profile: AgentProfile,
        staged_loaded: LoadedSettings,
    ) -> dict[str, Any]:
        """The restored session's own choices: its saved model, when selectable and not bound by its agent.

        Its own, not the default: the selection rides the settings handle's
        session layer, goes live with the build's commit and ends when the
        session closes, so restoring a session never changes the model new
        sessions start on. Restoring the current session keeps the choice it
        has, and a saved model already in force adds nothing.
        """
        current = self._settings_handle.session_values
        registry = self._model_registry
        if meta is None or not meta.model_profile_id or registry is None:
            return current
        saved = registry.get(meta.model_profile_id)
        if saved is None or not is_model_profile_selectable(saved):
            return current
        bound_profile_id = profile.model.profile_id
        if bound_profile_id and registry.get(bound_profile_id) is not None:
            return current
        if saved.id == self._settings_handle.prepare(staged_loaded, session=current).effective.settings.model_profile:
            return current
        return {**current, **active_model_profile_fields(saved)}

    async def reset_after_failed_startup_restore(self) -> None:
        """Discard every partially installed restore field before fresh startup.

        Restore can fail after installing the target session id, workspace, active
        lock, or a partial runtime. Suppress persistence while tearing those down so
        fallback startup cannot overwrite the saved target session.
        """
        with self._session.saves_suppressed():
            try:
                await self.close_session()
            finally:
                # ``shutdown`` normally releases this; keep the failure path idempotent
                # if teardown raised after acquiring/installing the restore lock.
                self._session.guard.release()
                self.reset_turn_runtime_after_session_shutdown()
                self._session.workspace = None
                self.reset_for_restart(None)
                # The fallback start is a new session whatever the teardown got through. A
                # teardown that raised before releasing the runtime leaves it running on the
                # session's model meanwhile, under settings that name the default: the
                # fallback start's build replaces it.
                self._end_session_choice()
        # Fallback startup runs in the process cwd, not the failed target's root,
        # so the settings the next build reads must be re-derived for that root —
        # whatever the failed restore left installed described the wrong project
        # trust domain. Best-effort: the reset must never mask the original
        # failure, and the live settings remain a usable baseline without it.
        try:
            old_loaded = self._settings_handle.base
            candidate = await asyncio.to_thread(
                load_settings,
                project_root=Path(safe_getcwd()),
                eval_context=self._reload_eval_context(),
                **self._session_pin_overrides(),
            )
            routed, _ = route_restart_settings(reattribute_command_line(candidate, old_loaded), old_loaded)
            self._settings_handle.install(routed)
        except Exception:
            logger.warning("Settings re-derivation for the fallback startup root failed", exc_info=True)

    def reset_for_restart(self, session_id: str | None) -> None:
        """Reset per-session engine state in preparation for a fresh ``start()``.

        The approval mode is not per-session state: the mode the user last chose stays in
        force for every session of this launch, and only the next launch reads the saved default.
        """
        self._fsm.reset()
        self._session.reset(session_id=session_id, workspace=self._session.workspace)
        self._workspace_change_tracker.reset_for_restart()

    async def reset_session_to_welcome(
        self,
        session_id: str,
        *,
        write_lock_held: bool = False,
        after_delete: Callable[[], Awaitable[None]] | None = None,
        before_restart: Callable[[], None] | None = None,
    ) -> bool:
        """Delete ``session.json`` and snapshots, then reload to a welcome state."""
        transition_owner = (
            None
            if self._permits.current_task_owns_session_transition_permit()
            else await self._permits.begin_session_transition("reset")
        )
        reset_lock: FileLock | None = None
        try:
            session_dir = None
            lock_path = None
            if self._persistence.state_store is not None:
                session_dir = self._persistence.state_store.session_dir(session_id)
            if session_dir is not None and session_dir.is_dir():
                lock_path = self._session.session_write_lock_path(session_id)
                if not write_lock_held:
                    await self._writer.flush()
                    if lock_path is not None:
                        reset_lock = _acquire_session_write_lock(lock_path, session_id)
                        if reset_lock is None:
                            return False

            profile = self._session.agent_profile
            restore_state = (
                _reset_restore_history_state(self._current.loaded.bindings.backend.history_state)
                if self._current.loaded is not None
                else None
            )
            restore_mutations = (
                copy.deepcopy(self._session.mutation_tracker.serialize())
                if self._session.mutation_tracker is not None
                else None
            )
            if restore_state is not None and restore_mutations is not None:
                restore_state["chrys_mutations"] = copy.deepcopy(restore_mutations)
            restore_todos = self._session.todo_tracker.serialize() if self._session.todo_tracker is not None else None
            if restore_state is not None and restore_todos:
                restore_state["chrys_todos"] = copy.deepcopy(restore_todos)
            restore_turn_number = self._session.turn_number
            restore_runtime_meta = copy.deepcopy(self._session.runtime_meta)
            with self._session.saves_suppressed():
                await self.close_session_in_place()
                self.reset_turn_runtime_after_session_shutdown()

                if session_dir is not None and session_dir.is_dir():
                    try:
                        _delete_reset_session_files(session_dir)
                        snap_dir = session_dir / "snapshots"
                        if snap_dir.is_dir():
                            shutil.rmtree(snap_dir, ignore_errors=True)
                    except OSError:
                        logger.warning("Failed to delete session files for reset %s", session_id, exc_info=True)
                        if reset_lock is not None:
                            reset_lock.release()
                            reset_lock = None
                        if before_restart is not None:
                            before_restart()
                        await self._restart_after_failed_reset(
                            session_id=session_id,
                            profile=profile,
                            state=restore_state,
                            mutations=restore_mutations,
                            todos=restore_todos,
                            turn_number=restore_turn_number,
                            runtime_meta=restore_runtime_meta,
                        )
                        return False

                if after_delete is not None:
                    await after_delete()
                if session_dir is not None and session_dir.is_dir():
                    # The restart below keeps this session id, so the directory is
                    # written to again: a delete that cannot finish now must leave
                    # no intent naming the path the new run lands in.
                    cleanup_empty_session_dir_path(session_dir, path_reused=True)

                # Both the normal directory path and a concurrently removed
                # directory reach the restart below.  The new runtime can activate
                # trajectory recording from a session_start hook, which reacquires
                # this same path-keyed lock, so ownership must end before restart
                # regardless of what happened to the directory entry.
                if reset_lock is not None:
                    reset_lock.release()
                    reset_lock = None
                if before_restart is not None:
                    before_restart()

                self.reset_for_restart(session_id)
                await self._reconcile_existing_spill_storage()
                if profile is None:
                    await self._bus.publish(
                        Error(
                            code="no_agent_profile",
                            message="Agent profile not configured.",
                            session_id=session_id,
                        ),
                    )
                    return True
                await self.start(profile, operation="reset")
                return True
        finally:
            if reset_lock is not None:
                reset_lock.release()
            if transition_owner is not None:
                self._permits.finish_session_transition(transition_owner)

    def cleanup_empty_session_dir(self) -> None:
        """Remove the current session directory if it was never saved."""
        cleanup_empty_session_dir_path(self._session.session_dir)

    async def _restart_after_failed_reset(
        self,
        *,
        session_id: str,
        profile: AgentProfile | None,
        state: dict | None,
        mutations: dict[str, Any] | None,
        todos: list[dict[str, str]] | None,
        turn_number: int,
        runtime_meta: SessionRuntimeMetadata,
    ) -> None:
        self.reset_for_restart(session_id)
        await self._reconcile_existing_spill_storage()
        self._session.restore_position(runtime_meta=runtime_meta, turn_number=turn_number)
        mutation_state = (
            mutations if mutations is not None else state.get("chrys_mutations") if state is not None else None
        )
        if mutation_state is not None:
            session_dir = self._session.session_dir
            if session_dir is not None:
                snapshot_store = SnapshotStore(
                    session_dir, policy=SnapshotPolicy.from_settings(self._settings_handle.settings)
                )
                self._session.mutation_tracker = MutationTracker.deserialize(mutation_state, snapshot_store)
        if state is not None:
            self._workspace_change_tracker.restore(
                state.get("chrys_workspace_baseline"),
                self._session.workspace,
                resolve_scope=self._settings_handle.settings.workspace_change_notice,
            )
        # Rehydrate the todo tracker from the pre-shutdown snapshot: reattaching
        # ``history_state`` alone is not enough — save reads the TRACKER, so an
        # empty one would pop ``chrys_todos`` on the next save.
        todo_state = todos if todos is not None else state.get("chrys_todos") if state is not None else None
        if todo_state:
            self._session.todo_tracker = TodoTracker()
            await self._session.todo_tracker.restore(todo_state)
        if profile is None:
            return
        try:
            await self.start(profile, operation="reset_failed")
        except Exception:
            logger.warning("Failed to restart session after reset failure %s", session_id, exc_info=True)
            return
        if state is not None and self._current.loaded is not None:
            if mutation_state is not None:
                state["chrys_mutations"] = copy.deepcopy(mutation_state)
            if todo_state:
                state["chrys_todos"] = copy.deepcopy(todo_state)
            self._current.loaded.bindings.backend.history_state = state
            if self._current.loaded is not None:
                loaded = self._current.loaded
                restore_phase4_state(loaded.reminder_middleware, loaded.last_words, state)
            stamp_history_item_ids(self._current.loaded.bindings.backend.history_state)
            self._history.bind(self._current.loaded.bindings.backend.history_state)

    async def on_new_session(self, _event: SessionNew) -> None:
        """Handle new session request: save current session, then start fresh."""
        transition_owner = await self._permits.begin_session_transition("new_session")
        try:
            # Sample the live profile after the transition fence so a queued
            # profile switch that won the rebuild gate determines the new session.
            profile = self._session.agent_profile
            if profile is None:
                return
            await self._start_fresh_session(profile)
        finally:
            self._permits.finish_session_transition(transition_owner)

    async def _start_fresh_session(self, profile: AgentProfile) -> None:
        """Shut the current session down and start an empty one on the agent new sessions start on.

        *profile*, the live agent, is used only when none resolves: closing the
        session puts the new-session agent in force (:meth:`close_session`).
        Callers hold a COMMITTED session transition (``_begin_session_transition``
        or prepare + ``_commit_session_transition``); the shutdown here relies on
        the fence to have already invalidated the old session's turn state.
        """
        await self.close_session()
        self.reset_turn_runtime_after_session_shutdown()
        self.cleanup_empty_session_dir()
        self.reset_for_restart(None)
        await self.start(self._session.agent_profile or profile, operation="new_session")

    async def on_session_clear(self, event: SessionClear) -> None:
        """Delete the ACTIVE session and start a fresh one as one fenced transition.

        Prompt admission is closed before the deletion starts and stays closed
        until the fresh session is ready, so no prompt can be admitted against the
        detached session in between.  The transition is committed only after the
        delete succeeded: a failed delete leaves the current session and its turn
        state intact (admission simply reopens), reports
        ``Error(code="session_clear_failed")``, and never starts a new session.
        """
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        transition_owner = await self._permits.prepare_session_transition("clear")
        if transition_owner is None:  # No owner filter was given, so this cannot happen.
            raise RuntimeError("Session transition acquisition unexpectedly failed")
        failure_message: str | None = None
        try:
            # Sample under the fence: a queued restore/new/switch cannot move the
            # active session while we hold the gate.
            profile = self._session.agent_profile
            if not self._session.guard.owns(event.session_id):
                failure_message = "Only the active session can be cleared"
            elif profile is None:
                failure_message = "No agent profile is active"
            else:
                failure = await self._delete_session_reporting(event.session_id)
                if failure is not None:
                    failure_message = failure.message
                else:
                    self._permits.commit_session_transition(transition_owner)
                    await self._start_fresh_session(profile)
        finally:
            self._permits.finish_session_transition(transition_owner)
        # Published after the fence is released — an Error handler must not find
        # the gate still held by the failed clear.
        if failure_message is not None:
            await self._bus.publish(
                Error(code="session_clear_failed", message=failure_message, session_id=event.session_id),
            )

    async def on_session_restore(self, event: SessionRestore) -> None:
        """Handle session restore by loading saved state and rebuilding the agent."""
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        from chrys.service.state.locks import SESSION_RESTORE_ACTIVE_LOCK_TIMEOUT_SECONDS

        if self._persistence.state_store is None:
            await self._bus.publish(
                Error(code="no_state_store", message="State store not configured", session_id=event.session_id)
            )
            return

        restoring_current = self._session.guard.owns(event.session_id)
        target_lock: FileLock | None = None
        transition_owner: str | None = None
        if not restoring_current:
            try:
                target_lock = await asyncio.to_thread(
                    self._session.guard.acquire_for_restore,
                    event.session_id,
                    timeout=SESSION_RESTORE_ACTIVE_LOCK_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                await self._bus.publish(
                    Error(
                        code="session_in_use",
                        message=self._session.guard.conflict_message(event.session_id),
                        session_id=event.session_id,
                    ),
                )
                return

        try:
            if event.ignore_recovery:
                try:
                    await asyncio.to_thread(self._persistence.state_store.delete_recovery_session, event.session_id)
                except Exception:
                    logger.debug("Failed to delete ignored recovery sidecar for %s", event.session_id, exc_info=True)

            prefer_recovery = not event.ignore_recovery and (
                not restoring_current or self._session.recovered_from_sidecar
            )

            state = await self._persistence.load_session(event.session_id, prefer_recovery=prefer_recovery)
            if state is None:
                if event.ignore_recovery and not restoring_current:
                    cleanup_empty_session_dir_path(self._persistence.state_store.session_dir(event.session_id))
                await self._bus.publish(
                    Error(
                        code="session_not_found",
                        message=f"Session '{event.session_id}' not found",
                        session_id=event.session_id,
                    )
                )
                return

            # Resolve meta by id directly. ``list_sessions`` relies on
            # ``iterdir`` which can briefly miss a just-written session
            # folder on Windows; a direct envelope read avoids that race.
            meta = await self._persistence.load_session_meta(event.session_id, prefer_recovery=prefer_recovery)
            if meta is not None and meta.kind == "workflow":
                await self._bus.publish(
                    Error(
                        code="session_kind_mismatch",
                        message="Open this session in Workflow mode.",
                        session_id=event.session_id,
                    )
                )
                return
            recovered_from_sidecar = (
                await self._persistence.recovery_session_wins(event.session_id) if prefer_recovery else False
            )

            cwd_warning = ""
            saved_cwd = meta.primary_cwd if meta and meta.primary_cwd else ""
            cwd_overridden = bool(event.primary_cwd) and event.primary_cwd != saved_cwd
            target_cwd = event.primary_cwd or saved_cwd or self._workspace_cwd()
            target_cwd_exists = os.path.isdir(target_cwd)
            if target_cwd and not target_cwd_exists:
                if not restoring_current:
                    # Never switch to a session whose directory is gone; the
                    # frontend lets the user pick another one (``primary_cwd``).
                    # Rolling back the current session keeps it and only warns.
                    await self._bus.publish(
                        Error(
                            code="session_cwd_missing",
                            message=(
                                f"Working directory of session {session_short_id(event.session_id)} "
                                f"no longer exists: {surrogate_safe_text(target_cwd)}"
                            ),
                            display_message=_RESTORE_SESSION_CWD_MISSING.bind(path=DisplayPath(target_cwd)),
                            session_id=event.session_id,
                        )
                    )
                    return
                cwd_warning = f"Working directory no longer exists: {surrogate_safe_text(target_cwd)}"

            profile_name = event.profile_name or (meta.agent_profile if meta else "")
            saved_profile_id = meta.agent_profile_id if meta else ""
            profile_selector = event.profile_name or saved_profile_id or profile_name
            profile: AgentProfile | None = None
            profile_switch: tuple[str, str] | None = None
            profile_resolution_warning: Warning | None = None
            if profile_selector and self._agent_registry:
                if event.profile_name:
                    resolved = self._agent_registry.resolve_selector(event.profile_name)
                elif saved_profile_id:
                    resolved = self._agent_registry.get_by_id(
                        saved_profile_id,
                        disambiguating_name=profile_name,
                    )
                else:
                    resolved = self._agent_registry.get(profile_name)
                if resolved is not None:
                    profile = resolved
                    profile_name = resolved.name
                    if event.profile_name and meta and meta.agent_profile and meta.agent_profile != resolved.name:
                        from_display = meta.agent_display_name or meta.agent_profile
                        to_display = resolved.display_name or resolved.name
                        if _restore_profile_override_switch(
                            state,
                            from_profile=meta.agent_profile,
                            from_display=from_display,
                            to_profile=resolved.name,
                            to_display=to_display,
                        ):
                            profile_switch = (from_display, to_display)
                else:
                    saved_profile_name = profile_name
                    if event.profile_name:
                        logger.warning(
                            "Requested agent profile '%s' could not be resolved; stopping session restore",
                            event.profile_name,
                        )
                        await self._bus.publish(
                            Error(
                                code="requested_agent_profile_unresolved",
                                message=(
                                    f"Requested agent profile '{event.profile_name}' could not be resolved; "
                                    "session restore was stopped."
                                ),
                                display_message=_RESTORE_REQUESTED_AGENT_PROFILE_UNRESOLVED.bind(
                                    profile=event.profile_name
                                ),
                                session_id=event.session_id,
                            )
                        )
                        return
                    unresolved_saved_identity = bool(saved_profile_id)
                    saved_display = (meta.agent_display_name if meta else "") or saved_profile_name or saved_profile_id
                    fallback_profile = self._restore_fallback_profile(restoring_current=restoring_current)
                    if fallback_profile is not None:
                        profile_name = fallback_profile.name
                        logger.warning(
                            "Agent profile '%s' not found, keeping current profile '%s'",
                            saved_profile_name,
                            profile_name,
                        )
                        if unresolved_saved_identity:
                            current_display = fallback_profile.display_name or profile_name
                            profile_resolution_warning = Warning(
                                code="saved_agent_profile_unresolved",
                                message=(
                                    f"Saved agent profile id '{saved_profile_id}' for '{saved_profile_name}' "
                                    f"could not be uniquely resolved; keeping current profile '{profile_name}'."
                                ),
                                display_message=_RESTORE_AGENT_PROFILE_UNRESOLVED_USING_CURRENT.bind(
                                    saved=saved_display,
                                    current=current_display,
                                ),
                                session_id=event.session_id,
                            )
                    elif unresolved_saved_identity:
                        logger.warning(
                            "Agent profile id '%s' for saved profile '%s' could not be uniquely resolved; "
                            "stopping restore because no current profile is active",
                            saved_profile_id,
                            saved_profile_name,
                        )
                        await self._bus.publish(
                            Error(
                                code="saved_agent_profile_unresolved",
                                message=(
                                    f"Saved agent profile id '{saved_profile_id}' for '{saved_profile_name}' "
                                    "could not be uniquely resolved, and no current profile is active."
                                ),
                                display_message=_RESTORE_AGENT_PROFILE_UNRESOLVED.bind(saved=saved_display),
                                session_id=event.session_id,
                            )
                        )
                        return
                    else:
                        available = self._agent_registry.list_profiles()
                        if available:
                            profile = available[0]
                            logger.warning(
                                "Agent profile '%s' not found, falling back to '%s'",
                                saved_profile_name,
                                profile.name,
                            )
                            profile_name = profile.name

            if event.working_dirs is not None:
                # Client-supplied additionalDirectories are authoritative (ACP load
                # semantics): the list REPLACES the saved roots so a client can narrow,
                # swap, or clear (empty list) workspace scope. Keep the primary first to
                # mirror new-session storage; service-session compatibility canonicalizes
                # away the primary (see _extra_roots), so its inclusion here is cosmetic.
                merged_dirs = [target_cwd]
                seen_dirs: set[str] = {target_cwd}
                for path in event.working_dirs:
                    if path and path not in seen_dirs:
                        seen_dirs.add(path)
                        merged_dirs.append(path)
            else:
                # Caller did not specify roots: keep the saved working_dirs verbatim (it may
                # include the primary cwd, and _can_restore_service_session compares the path
                # list exactly). A moved cwd invalidates the saved layout, so drop it.
                merged_dirs = [] if cwd_overridden else list(meta.working_dirs if meta else [])
            session_ws = Workspace(
                primary_cwd=target_cwd,
                working_dirs=_working_dirs_from_paths(merged_dirs, target_cwd),
            )

            # The transition boundary is taken in two phases. The *prepared* fence
            # comes first: it shares the rebuild gate with settings reloads and
            # model switches, so everything read below — loaded settings, eval
            # context, session pins — is a committed state no concurrent rebuild
            # can move. Snapshotting before the fence would let a reload commit
            # during the load's thread hop and then be silently overwritten by a
            # staged load routed against the stale copy. But the *commit* — which
            # bumps the session generation and invalidates the old session's turn
            # state, retries and injection — waits until the load has succeeded:
            # an unreadable config file must abort the restore with the current
            # session genuinely intact, not admission-open but generation-dead.
            if not self._permits.current_task_owns_session_transition_permit():
                transition_owner = await self._permits.prepare_session_transition("restore")
                if transition_owner is None:  # No owner filter was given, so this cannot happen.
                    raise RuntimeError("Session transition acquisition unexpectedly failed")

            # The restore crosses into the target session's project trust domain:
            # its settings are re-derived for that root so the build below reads
            # them, exactly as a workspace change would. Loaded here — before
            # anything is torn down — so an unreadable config file aborts the
            # restore with the current session intact.
            old_loaded = self._settings_handle.base
            candidate = await asyncio.to_thread(
                load_settings,
                project_root=Path(target_cwd),
                eval_context=self._reload_eval_context(),
                **self._session_pin_overrides(),
            )
            staged_loaded, _ = route_restart_settings(reattribute_command_line(candidate, old_loaded), old_loaded)
            if profile is None:
                # Sampled after the transition fence, like the live agent was:
                # a switch that won the gate meanwhile is the one in force.
                profile = self._restore_fallback_profile(restoring_current=restoring_current)
                if not profile_name and profile is not None:
                    profile_name = profile.name
            if transition_owner is not None:
                self._permits.commit_session_transition(transition_owner)
            if restoring_current:
                await self.close_session_in_place(keep_side_answers=True)
            else:
                await self.close_session()
            self.reset_turn_runtime_after_session_shutdown()
            self.cleanup_empty_session_dir()

            self._session.workspace = session_ws

            if target_lock is not None:
                self._session.guard.install(event.session_id, target_lock)
                target_lock = None

            await self._hydrate_restored_session(
                event=event,
                state=state,
                meta=meta,
                profile=profile,
                profile_name=profile_name,
                profile_switch=profile_switch,
                profile_resolution_warning=profile_resolution_warning,
                recovered_from_sidecar=recovered_from_sidecar,
                cwd_warning=cwd_warning,
                target_cwd=target_cwd,
                staged_loaded=staged_loaded,
            )
        except BaseException as exc:
            if transition_owner is not None:
                self._permits.finish_session_transition(transition_owner)
                transition_owner = None
            if isinstance(exc, Exception):
                # The interactive callers publish this event with the bus's
                # default swallow-and-log delivery: without a terminal event the
                # restore loading UI has nothing to clear it. Published after the
                # fence is released — an Error handler must not find the gate
                # still held by the failed restore.
                await self._bus.publish(
                    Error(code="session_restore_failed", message=str(exc), session_id=event.session_id)
                )
            raise
        finally:
            if target_lock is not None:
                target_lock.release()
            if transition_owner is not None:
                self._permits.finish_session_transition(transition_owner)

    async def _hydrate_restored_session(
        self,
        *,
        event: SessionRestore,
        state: dict[str, Any],
        meta: ChatSessionMeta | None,
        profile: AgentProfile | None,
        profile_name: str,
        profile_switch: tuple[str, str] | None,
        profile_resolution_warning: Warning | None,
        recovered_from_sidecar: bool,
        cwd_warning: str,
        target_cwd: str,
        staged_loaded: LoadedSettings,
    ) -> None:
        """Hydrate live engine state after restore shutdown while admission remains closed."""
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        self._fsm.reset()
        self._session.adopt_restore_identity(session_id=event.session_id, recovered_from_sidecar=recovered_from_sidecar)
        session_dir = self._session.session_dir
        # The staged settings describe the target session; the live ones still
        # belong to the previous session until the build below commits them.
        self._workspace_change_tracker.restore(
            state.get("chrys_workspace_baseline"),
            self._session.workspace,
            resolve_scope=staged_loaded.settings.workspace_change_notice,
        )

        spill_reconciliation = await self._reconcile_existing_spill_storage()

        # Installing ``event.session_id`` above makes the engine session directory
        # available for the remainder of successful hydration. The policy reads
        # the staged settings: they are what the build below commits, and the
        # tracker hydrated here outlives that commit.
        snapshot_store = SnapshotStore(
            cast("Path", session_dir), policy=SnapshotPolicy.from_settings(staged_loaded.settings)
        )
        if state and state.get("chrys_mutations"):
            self._session.mutation_tracker = MutationTracker.deserialize(state["chrys_mutations"], snapshot_store)
        else:
            self._session.mutation_tracker = MutationTracker(snapshot_store)
        # Hydrate todos before ``restore_phase4_state`` below:
        # ``LastWordsState.restore_last_words`` re-captures the restored note's
        # todo section from the tracker.
        self._session.todo_tracker = TodoTracker()
        if state:
            await self._session.todo_tracker.restore(state.get("chrys_todos"))
        # The restored session gets its own coordinator (session id changed;
        # the previous one was closed during shutdown).  Construction builds
        # it during ``start()`` below.
        self._session.mutation_coordinator = None

        # Install the target session's runtime metadata BEFORE the build:
        # construction's ``_publish_results`` hydrates the fresh strategy from
        # ``engine.session.runtime_meta``, so leaving the previous session's metadata in
        # place would leak its calibration record into this session's strategy
        # whenever the build fingerprints happen to match.
        self._session.runtime_meta = SessionRuntimeMetadata.from_state_dict(state)

        if profile is not None:
            session = (
                self._restored_session_choice(meta, profile, staged_loaded)
                if event.apply_saved_model
                else self._settings_handle.session_values
            )
            await self.start(profile, operation="restore", staged_loaded=staged_loaded, session=session)
            if profile_switch is not None and self._current.loaded is not None:
                self._current.loaded.reminder_middleware.sources.profile_switch.set_profile_switch(*profile_switch)
        else:
            # Nothing to build, so no commit will install the staged load; this
            # degenerate path installs it directly, like a reload with nothing
            # built.
            self._settings_handle.install(staged_loaded)

        # The verdicts describe the target root's files — the same report a
        # reload or workspace change makes — published only now that the staged
        # load is in force (committed by the build above, or installed by the
        # degenerate path), under the restored session's id.
        restore_warnings = settings_warning_events(staged_loaded)
        if profile_resolution_warning is not None:
            restore_warnings.insert(0, profile_resolution_warning)
        for warning in restore_warnings:
            await self._bus.publish(replace(warning, session_id=self._session.session_id))

        self._session.turn_number = state.get("turn_counter", 0) if state else 0

        if self._current.loaded is not None and state:
            self._current.loaded.bindings.backend.history_state = state
            if self._current.loaded is not None:
                loaded = self._current.loaded
                restore_phase4_state(
                    loaded.reminder_middleware,
                    loaded.last_words,
                    state,
                    available_relative_paths=spill_reconciliation.available_relative_paths,
                )
            if meta and meta.service_session_id:
                if self._can_restore_service_session(meta):
                    self._current.loaded.bindings.backend.service_session_id = meta.service_session_id
                else:
                    self._current.loaded.bindings.backend.service_session_id = ""
                    await self._bus.publish(
                        Warning(
                            code="service_session_incompatible",
                            message=(
                                "This session was saved with an OpenAI Responses service session. "
                                "The active agent/model profile, workspace, service endpoint, or storage mode "
                                "is not compatible, "
                                f"so {APP_DISPLAY_NAME} will continue from local history only."
                            ),
                            display_message=_RESTORE_SERVICE_SESSION_INCOMPATIBLE.bind(app=APP_DISPLAY_NAME),
                            session_id=event.session_id,
                        )
                    )
            stamp_history_item_ids(self._current.loaded.bindings.backend.history_state)
            self._history.bind(self._current.loaded.bindings.backend.history_state)

            # ``engine.start`` above already hydrated the fresh strategy through
            # ``_publish_results``; this covers the no-rebuild path (``profile is
            # None``) where the surviving executor's strategy is the only target.
            # The provenance gate makes the repeat call idempotent.
            if (
                self._session.runtime_meta.context_calibration is not None
                and self._current.loaded.bindings.backend.compaction_strategy is not None
            ):
                self._session.runtime_meta.restore_context_calibration(
                    self._current.loaded.bindings.backend.compaction_strategy,
                    model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
                    agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
                )

        restored_history_changed = profile_switch is not None
        paused_records: list[dict[str, Any]] = []
        injected_paused_results = 0
        artifact_service = (
            SubAgentSessionArtifactService(self._session.session_dir)
            if self._session.session_dir is not None and self._current.loaded is not None
            else None
        )
        if artifact_service is not None and self._current.loaded is not None:
            paused_records = artifact_service.drain_paused_records()
        # Repair dangling sub-agent function_calls even when drain dropped or
        # quarantined every record (over-cap/corrupt): otherwise the parent
        # assistant call is left with no tool_result and the next turn is
        # provider-invalid. The live tool registry resolves dangling calls whose
        # record did not survive drain — records only enrich the error text.
        registry_sub_agent_names = (
            set(self._current.loaded.sub_agent_tools.tool_names())
            if (self._current.loaded is not None and self._current.loaded.sub_agent_tools is not None)
            else set()
        )
        record_sub_agent_names = {
            tool_name
            for record in paused_records
            if isinstance(tool_name := record.get("tool_name"), str) and tool_name
        }
        # Always attempt injection — never gate it on records/registry being
        # non-empty. A persisted sub-agent function_call self-identifies via its
        # ``_chrys_tool_kind`` marker, so a dangling call must be repaired even
        # when drain lost every record AND the restored profile no longer registers
        # the tool (removed/disabled/depth-skipped). The method self-gates on the
        # history being bound and does nothing when there is no dangling call.
        injected_paused_results = self._history.inject_error_results_for_sub_agents(
            paused_records, registry_sub_agent_names | record_sub_agent_names
        )
        restored_history_changed = restored_history_changed or injected_paused_results > 0
        if paused_records:
            logger.info(
                "Session restore: loaded %d paused sub-agent record(s), injected %d error tool_result(s)",
                len(paused_records),
                injected_paused_results,
            )
            discarded_tools = sorted({r.get("tool_name", "?") for r in paused_records})
            tool_names = ", ".join(discarded_tools)
            await self._bus.publish(
                Warning(
                    code="sub_agents_reload_discarded",
                    message=(
                        f"{len(paused_records)} paused sub-agent(s) from a previous session were discarded: {tool_names}"
                    ),
                    display_message=_RESTORE_SUB_AGENTS_DISCARDED.bind(
                        discarded=len(paused_records),
                        names=DisplaySequence(tuple(discarded_tools)),
                    ),
                    session_id=event.session_id,
                ),
            )

        if self._history.is_bound:
            awaiting_count = sum(
                1
                for message in self._history.messages
                if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.AWAITING_SUB_AGENTS
            )
            self._history.remove_awaiting_sub_agents_marker()
            restored_history_changed = restored_history_changed or awaiting_count > 0
            if (awaiting_count > 0 or injected_paused_results > 0) and self._history.trailing_status_marker() is None:
                self._history.insert_interrupted_marker(
                    reason=format_message(SUB_AGENT_STATE_DISCARDED_MESSAGE.bind()),
                    source="error",
                    status_code=HistoryMarkerKind.STATUS_SUB_AGENT_STATE_DISCARDED,
                )
                restored_history_changed = True
            if artifact_service is not None:
                orphaned = artifact_service.reconcile_orphaned_running_logs(self._history.messages, paused_records)
                if orphaned:
                    logger.info("Session restore: marked %d sub-agent audit log(s) orphaned", orphaned)

            self._restore_terminal_fsm_from_history()

        if restored_history_changed:
            saved = False
            try:
                saved = await self._writer.save_current_session(raise_on_error=bool(paused_records))
            except Exception:
                logger.warning("Session restore: failed to save repaired session; preserving paused sub-agent records")
            if saved and paused_records and artifact_service is not None:
                artifact_service.finalize_restored_paused_records(paused_records)
        elif paused_records and artifact_service is not None:
            artifact_service.archive_unconsumed_restored_paused_records(paused_records)

        await self._bus.publish(
            SessionRestored(
                session_id=event.session_id,
                agent_profile=profile_name,
                display_name=profile.display_name if profile else "",
                initial_agent_profile=(
                    meta.agent_profile_history[0]
                    if meta is not None and meta.agent_profile_history
                    else profile.display_name
                    if profile
                    else profile_name
                ),
                message_count=meta.message_count if meta else 0,
                cwd_warning=cwd_warning,
                primary_cwd=target_cwd,
                recovered_from_sidecar=recovered_from_sidecar,
                working_dirs=(
                    [working_dir.path for working_dir in self._session.workspace.working_dirs]
                    if self._session.workspace is not None
                    else []
                ),
            ),
        )
        # Restore-time UsageUpdate must follow SessionRestored so the TUI has
        # already bound the new ``main_usage_source_id`` before classifying it as
        # the session window — otherwise the chat panel keeps stale window tokens
        # from the previous session.  Route through the ordered chain so any
        # pending sub-agent UsageUpdate that was in-flight before the switch can't
        # overtake this restored snapshot.
        self._usage_publisher.enqueue_usage_event(self._usage_publisher.make_usage_event(session_id=event.session_id))
        await self._fire_session_restored_hook(restored_session_id=event.session_id, profile_name=profile_name)

    async def _fire_session_restored_hook(
        self,
        *,
        restored_session_id: str,
        profile_name: str,
    ) -> None:
        if self._session.hook_manager is None:
            return
        from chrys.service.hooks.events import HookEvent

        if not self._session.hook_manager.has_hooks_for(HookEvent.SESSION_RESTORED):
            return
        await self._session.hook_manager.fire(
            HookEvent.SESSION_RESTORED,
            {
                "session_id": self._session.session_id,
                "profile": profile_name,
                "cwd": self._workspace_cwd(),
                "restored_session_id": restored_session_id,
            },
            scope="session",
        )

    async def on_session_delete(self, event: SessionDelete) -> None:
        """Handle session deletion."""
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        failure = await self._delete_session_reporting(event.session_id)
        if failure is not None:
            await self._bus.publish(
                Error(code=failure.code, message=failure.message, session_id=event.session_id),
            )

    async def _delete_session_reporting(self, session_id: str) -> _SessionDeleteFailure | None:
        """Delete *session_id* from disk, detaching the engine first when it is the active session.

        Publishes ``SessionDeleted`` on success and returns ``None``; on failure
        returns the reason with the engine's session ownership restored, leaving
        the caller to report it (``on_session_delete`` publishes the raw code,
        ``on_session_clear`` folds it into ``session_clear_failed``).
        """
        if self._persistence.state_store is None:
            return _SessionDeleteFailure(code="no_state_store", message="State store not configured")

        release_current_lock = self._session.guard.owns(session_id)
        detached_session_id = self._session.session_id if release_current_lock else None
        otel_sink = None
        if release_current_lock:
            from chrys.foundation.observability.sink import get_otel_sink

            await self._side_questions.quiesce()
            # Deleting the live session is its end: fire ``session_end`` now and
            # wait for it (async hooks included), while the id and the files still
            # exist, so hooks keep the "before teardown" contract; the shutdown
            # that follows (clear / delete-current -> new) skips the duplicate.  A
            # failed delete re-arms it below — the session then lives on and hooks
            # see one early ``session_end`` plus the real one at shutdown.  This
            # duplicate is accepted by design: exactly-once would need the hook to
            # run only after the delete is known to succeed, i.e. under the store's
            # write lock from a thread (rejected — user hooks may touch the store),
            # and NOT re-arming would instead drop the real ``session_end`` of a
            # session that keeps going, which loses more than a repeat costs.
            await self.fire_session_end_hooks()
            # Their logs are in the directory, and their ends belong in the trajectory.
            # The registry stays open for a session a failed delete keeps; the
            # shutdown after a delete closes it.
            if (tasks := self._session.background_tasks) is not None:
                await tasks.stop_all()
            # The trajectory writer holds the log's lease, and a leased directory
            # is tombstoned instead of removed — close it so the delete below is
            # physical.
            await self.close_trajectory_log()
            otel_sink = get_otel_sink()
            self._session.detach_for_delete()

        delete_succeeded = False
        # Not cancellation-safe on purpose: the delete runs in a thread and keeps
        # going if this task is cancelled, and ``CancelledError`` skips the restore
        # branches below, leaving the engine detached with the guard held.  The
        # only publisher of ``SessionClear``/``SessionDelete`` is a MainScreen
        # worker, which Textual cancels solely on app exit — and there a detached
        # engine is exactly right: ``shutdown()`` must not save (resurrect) a
        # session whose deletion may have just completed.  Restoring ``_session_id``
        # blindly on cancel would do precisely that; shield-and-reconcile would only
        # release a guard and publish an ack inside an app that is going away.
        try:
            await self._persistence.delete_session(session_id, allow_active=release_current_lock)
            delete_succeeded = True
        except TimeoutError as exc:
            if release_current_lock:
                self._session.reattach_after_failed_delete(detached_session_id)
                return _SessionDeleteFailure(
                    code="session_busy",
                    message=f"Timed out waiting for session write lock: {exc}",
                )
            return _SessionDeleteFailure(
                code="session_in_use",
                message=self._session.guard.conflict_message(session_id),
            )
        except Exception as exc:
            logger.exception("Failed to delete session %s", session_id)
            if release_current_lock:
                self._session.reattach_after_failed_delete(detached_session_id)
            return _SessionDeleteFailure(code="session_delete_failed", message=f"Failed to delete session: {exc}")
        finally:
            if release_current_lock and delete_succeeded:
                self._session.guard.release()

        # Post-delete cleanup runs only after the delete has committed. Keep it out
        # of the try above: a failure here must not reach the delete-failure handler
        # (which would wrongly restore engine.session.session_id to a now-deleted session)
        # nor be reported as a delete failure.
        if release_current_lock and otel_sink is not None:
            try:
                otel_sink.deactivate()
            except Exception:
                logger.exception("Failed to deactivate OTel sink after deleting session %s", session_id)
        await self._bus.publish(SessionDeleted(session_id=session_id))
        return None
