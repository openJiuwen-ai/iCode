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
from chrys.foundation.config.errors import ConfigError, ConfigValueError
from chrys.foundation.config.process_settings import reattribute_command_line, route_restart_settings
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.warnings import settings_warning_events, workdir_config_read_failure
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
    WorkspaceOperation,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import DisplayBlock, DisplayPath, DisplaySequence, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.history_markers import SUB_AGENT_STATE_DISCARDED_MESSAGE, HistoryMarkerKind
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import (
    WorkingDir,
    Workspace,
    path_identity,
    session_workdirs,
    workspace_scope_fingerprint,
)
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.platform.files import surrogate_safe_text, unlink_with_retry
from chrys.foundation.trajectory.event_types import RuntimeFinishReason as TrajectoryRuntimeFinishReason
from chrys.foundation.util.lock import FileLock
from chrys.foundation.util.once_close import finish_close
from chrys.foundation.util.session_ids import session_short_id
from chrys.orchestration.engine.state import lifecycle_permits
from chrys.orchestration.engine.state.active_session import ActiveSession, session_pin_overrides
from chrys.orchestration.engine.state.machine import Trigger
from chrys.orchestration.engine.workspaces import (
    FORK_MEMBERSHIP_WRITE_FAILED,
    LEAVE_MEMBERSHIP_WRITE_FAILED,
    NEW_MEMBERSHIP_WRITE_FAILED,
    WORKSPACE_CHANGED,
    LeaveConfirmationRequired,
    MembershipWrite,
    SessionWorkspaces,
    StaleWorkspacePlan,
    WorkspacePlan,
    validate_workspace_primary,
    workspace_error_display,
    workspace_left_warning,
    workspace_selected_warning,
)
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
from chrys.service.state._session_files import session_dir_has_artifacts
from chrys.service.state.locks import SESSION_RESTORE_ACTIVE_LOCK_TIMEOUT_SECONDS
from chrys.service.state.store import (
    SESSION_BACKUP_FILE_NAME,
    SESSION_FILE_NAME,
    SESSION_RECOVERY_FILE_NAME,
    SESSION_WRITE_LOCK_TIMEOUT_SECONDS,
    ChatRestoreData,
    ChatRestoreKindError,
    ChatSessionMeta,
    JsonFileStateStore,
    SessionForkError,
    SessionNotFoundError,
    SessionReplay,
    StartupSessionInUseError,
    parse_snapshot_turn,
)
from chrys.service.todos.tracker import TodoTracker
from chrys.service.trajectory.preparation import PreparationOutcome
from chrys.service.workspaces.paths import MissingWorkspacePrimary, resolve_workspace_directory
from chrys.service.workspaces.registry import RegistrySnapshot, WorkspaceRegistryError

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import LoadedSettings, PreparedSettings, SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.build.loaded import CompletedBuild, LoadedAgent
    from chrys.orchestration.engine.loader import AgentLoader
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.side_questions import SideQuestions
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.state.session_writer import SessionWriter
    from chrys.orchestration.engine.trajectory import TrajectoryRecorder
    from chrys.orchestration.engine.usage import UsagePublisher
    from chrys.service.mutations.workspace_changes import WorkspaceRetarget
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.reload import ProfileReload
    from chrys.service.session.history import SessionHistoryManager
    from chrys.service.session.persistence import SessionPersistence
    from chrys.service.state.store import StateStore
    from chrys.service.trajectory.session import SessionTrajectory


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
_RESTORE_NO_CHECKPOINT = msg("restore.no_checkpoint", fallback="Session {session_id} has no readable chat checkpoint.")
_RESTORE_SESSION_CHANGED = msg(
    "restore.session_changed", fallback="Cannot restore because the active session changed before installation."
)
_NEW_SESSION_FAILED = msg("engine.new_session_failed", fallback="Could not start a new session: {detail}")
_NEW_SESSION_NO_AGENT = msg(
    "engine.new_session_no_agent", fallback="Cannot start a new session without an active agent."
)
_NEW_SESSION_CHOOSE_CWD = msg(
    "engine.new_session_choose_cwd", fallback="Start an Unspecified session to choose a working directory."
)
_NEW_SESSION_CHOOSE_WORKSPACE = msg(
    "engine.new_session_choose_workspace",
    fallback="Choose a workspace or Unspecified. With Unspecified, you can choose a working directory.",
)
_CLEAR_SESSION_FAILED = msg("engine.clear_session_failed", fallback="Could not clear the session: {detail}")


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


def cleanup_empty_session_dir_path(
    session_dir: Path | None,
    *,
    session_id: str | None = None,
    state_store: StateStore | None = None,
    path_reused: bool = False,
) -> None:
    """Remove *session_dir* when it has no restorable session files.

    Also forget its membership in the store's paired registry, even if no
    directory was created. A caller that restarts on this same directory sets
    ``path_reused`` to keep membership.
    """
    if session_dir is None:
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
    from chrys.service.trajectory.tombstone import DeleteOutcome, delete_session_directory

    try:
        # Same disposal as an explicit delete: a trajectory writer that is
        # still alive (a stuck worker, a writer in another process) turns this
        # into a logical delete instead of a half-removed folder.
        result = delete_session_directory(session_dir, sessions_root=session_dir.parent, path_reused=path_reused)
    except OSError:
        logger.debug("Failed to remove empty session directory %s", session_dir, exc_info=True)
        return
    if result.outcome in (DeleteOutcome.INTENT_FAILED, DeleteOutcome.MISSING) and session_dir.exists():
        return
    if not path_reused and session_id is not None and isinstance(state_store, JsonFileStateStore):
        state_store.forget_workspace_membership(session_id)


def _delete_reset_session_files(session_dir: Path) -> None:
    for filename in (SESSION_FILE_NAME, SESSION_BACKUP_FILE_NAME, SESSION_RECOVERY_FILE_NAME, BTW_FILE_NAME):
        path = session_dir / filename
        if path.exists():
            # A reader can hold the file for a moment (the sidebar's change
            # preview rereads session.json in a worker thread), and Windows
            # refuses to delete a file that is open: a plain unlink would fail
            # the reset, and with it a rollback to the start.
            unlink_with_retry(path)


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


@dataclass(frozen=True, slots=True)
class _PreparedRestore:
    session: ActiveSession
    tracker: WorkspaceChangeTracker
    settings: PreparedSettings
    retarget: WorkspaceRetarget
    build: CompletedBuild | None
    profiles: ProfileReload | None


@dataclass(frozen=True, slots=True)
class _RestoreResume:
    """The folder a failed restore began from, and that folder's identity."""

    cwd: str
    identity: tuple[int, int] | str


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
        self._startup_cwd = safe_getcwd()
        self._current = current
        self._loader = loader
        self._permits = permits
        self._writer = writer
        self._turn_state = turn_state
        self._usage_publisher = usage_publisher
        self._bus = bus
        store = persistence.state_store
        self.workspaces = SessionWorkspaces(bus, store.workspace_registry if store is not None else None)
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
        workspace_plan: WorkspacePlan | None = None,
        workspace_id: str | None = None,
        workspace_snapshot: RegistrySnapshot | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Build and install the agent from a profile under a lifecycle permit.

        *session* is the session's own choices to build with (the installed
        ones when None), committed with the build (``AgentLoader.stage``).
        """
        permit = None
        if not (
            self._permits.current_task_owns_rebuild_permit()
            or self._permits.current_task_owns_session_transition_permit()
        ):
            token = self._permits.capture_control_token()
            permit = await self._permits.acquire_rebuild_permit(token)
            if isinstance(permit, lifecycle_permits.RebuildPermitDenied):
                raise RuntimeError(permit.message)
        try:
            await self._start_locked(
                profile,
                operation=operation,
                staged_loaded=staged_loaded,
                session=session,
                workspace=workspace,
                workspace_plan=workspace_plan,
                workspace_id=workspace_id,
                workspace_snapshot=workspace_snapshot,
                commit_workspace=commit_workspace,
            )
        finally:
            if permit is not None:
                self._permits.release_rebuild_permit(permit)

    async def _start_locked(
        self,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace: Workspace | None = None,
        workspace_plan: WorkspacePlan | None = None,
        workspace_id: str | None = None,
        workspace_snapshot: RegistrySnapshot | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Start the engine while the caller owns the rebuild boundary."""
        # Every session (fresh, restored, reset) starts here: re-arm the
        # once-per-session ``session_end`` hook fired by shutdown/delete.
        opening_fresh_identity = self._session.session_id is None
        self._session.begin(agent_profile=profile)
        if not self._session.new_session_agent and operation == "startup":
            self._session.new_session_agent = profile.name
        session_id = self._session.session_id
        if session_id is None:
            raise RuntimeError("Session begin did not establish a session id.")
        if operation == "startup" and opening_fresh_identity:
            self.workspaces.begin_session()
        snapshot = workspace_snapshot
        if snapshot is None:
            snapshot = self.workspaces.snapshot() if operation == "startup" else RegistrySnapshot()
        if workspace_id is not None:
            workspace_plan = self.workspaces.plan(
                workspace=workspace or self._session.workspace or Workspace.from_cwd(),
                opened_cwd=workspace.primary_cwd if workspace is not None else "",
                snapshot=snapshot,
                operation=WorkspaceOperation.SELECT,
                workspace_id=workspace_id,
            )

            commit_workspace = self.workspaces.membership_write(
                workspace_plan, WorkspaceOperation.SELECT, source_id=None
            )
        elif workspace_plan is None:
            workspace_plan = self._session.workspace_plan if workspace is None else None
            if workspace_plan is None:
                workspace_plan = self.workspaces.plan(workspace=workspace or Workspace.from_cwd(), snapshot=snapshot)
                if workspace is None:
                    self._session.install_workspace(workspace_plan)
        staged_workspace = workspace_plan.workspace

        if not self._session.guard.ensure(session_id):
            message = self._session.guard.conflict_message(session_id)
            await self._bus.publish(Error(code="session_in_use", message=message, session_id=self._session.session_id))
            raise RuntimeError(message)

        reloaded_settings = None
        if staged_loaded is None:
            # Whatever this build is for — a model switch, an implicit new
            # session, a start after a failed restore — it runs in this
            # directory, so the installed settings are only usable if they
            # were loaded for the same one.
            staged_loaded = reloaded_settings = await self._settings_for_build(staged_workspace.primary_cwd)

        try:
            if workspace_id is not None and commit_workspace is not None:
                settings = self._settings_handle.prepare(staged_loaded or self._settings_handle.base, session=session)
                self._settings_handle.check_prepared(settings)
                profiles = await self._loader.stage_restore_profiles()
                profile = profiles.updated_agent(profile) or profile
                commit_workspace.save(session_id)
                await self._loader.normalize_candidate_profiles(profiles)
                profiles.install()

            # Build the hook manager once per engine instance when a hook config
            # file exists.  With no config file, hooks stay a true no-op: no
            # manager, no outbox/log/tmp directories, and no recovery task.
            #
            # The manager survives profile switches within the same session —
            # hooks are global, not profile-scoped. A no-executor workspace-change
            # retry can happen after a startup failure, so reload there because
            # working directory hooks are primary-cwd scoped. The candidate stays staged: it
            # goes live with the build's commit, and until then the old manager
            # keeps running untouched.
            old_hook_manager = self._session.hook_manager
            hook_manager = await self._loader.stage_hook_manager(
                operation=operation, staged_loaded=staged_loaded, workspace=staged_workspace
            )
            self._prepare_background_tasks(self._session, session_id)
            session_dir = self._session.session_dir if self._persistence.state_store is not None else None
            write_lock_path = self._session.session_write_lock_path(session_id) if session_dir is not None else None
            trajectory = self._bind_trajectory(session_id, session_dir, write_lock_path)

            await self._loader.load(
                profile,
                operation=operation,
                staged_loaded=staged_loaded,
                session=session,
                workspace_plan=workspace_plan,
                hook_manager=hook_manager,
                trajectory=trajectory,
                old_hook_manager=old_hook_manager,
                commit_workspace=commit_workspace,
            )
        except Exception as exc:
            if workspace_id is not None and commit_workspace is not None and commit_workspace.saved:
                current = self._session.workspace if self._current.loaded is not None else None
                raise commit_workspace.open_failure(current, exc) from exc
            raise
        if (
            workspace_plan.workspace_id is not None
            and (workspace_id is not None or operation == "new_session")
            and (commit_workspace is None or commit_workspace.failure is None)
        ):
            await self.touch_opened_workspace(staged_workspace, staged_workspace.primary_cwd)
        await self._publish_settings_warnings(reloaded_settings)

    def _prepare_background_tasks(self, session: ActiveSession, session_id: str) -> None:
        session_dir = session.session_dir if self._persistence.state_store is not None else None
        if session_dir is not None and session.background_tasks is None:
            from chrys.service.tools.background import BackgroundTasks

            async def _publish_background_tasks(tasks: BackgroundTasks) -> None:
                await self._bus.publish(
                    BackgroundTasksChanged(session_id=session_id, tasks=tasks.views(), full=tasks.full)
                )

            session.background_tasks = BackgroundTasks(
                session_dir,
                turn=lambda: self._session.turn_number,
                trajectory=self._trajectory_recorder.context,
                on_change=_publish_background_tasks,
                surface=session.surface or "",
            )

    def _bind_trajectory(
        self, session_id: str, session_dir: Path | None, write_lock_path: Path | None
    ) -> SessionTrajectory:
        from chrys.foundation.observability.sink import get_otel_sink

        otel_sink = get_otel_sink()
        if otel_sink is not None:
            otel_sink.activate(session_id, session_dir)
        # The trajectory recorder is bound here and activates (opens the log,
        # takes the writer lease) on its first event, so a session that never
        # records anything never creates ``trajectory/``.
        event_loop = asyncio.get_running_loop()
        trajectory_session_id = session_id
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

        return self._trajectory_recorder.bind_session(
            session_id=trajectory_session_id,
            session_dir=session_dir,
            write_lock_path=write_lock_path,
            session_start_info=self._loader.trajectory_session_start_info,
            on_activation_failed=_report_trajectory_activation_failure,
        )

    async def touch_opened_workspace(
        self, workspace: Workspace, cwd: str, *, token: lifecycle_permits.RebuildControlToken | None = None
    ) -> None:
        owner = token if token is not None else self._permits.capture_control_token()
        await self.workspaces.touch(workspace, cwd, is_current=lambda: owner == self._permits.capture_control_token())

    async def start_with_rebuild_permit(
        self,
        permit: lifecycle_permits.RebuildPermit,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace_plan: WorkspacePlan | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Start using an already-acquired rebuild permit."""
        self._permits.ensure_rebuild_permit(permit)
        await self.start(
            profile,
            operation=operation,
            staged_loaded=staged_loaded,
            session=session,
            workspace_plan=workspace_plan,
            commit_workspace=commit_workspace,
        )

    async def reload(
        self,
        new_profile: AgentProfile,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace_plan: WorkspacePlan | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Restart the agent with a new profile/workspace while preserving history."""
        if (
            self._permits.current_task_owns_rebuild_permit()
            or self._permits.current_task_owns_session_transition_permit()
        ):
            await self._reload_locked(
                new_profile,
                operation=operation,
                staged_loaded=staged_loaded,
                session=session,
                workspace_plan=workspace_plan,
                commit_workspace=commit_workspace,
            )
            return
        token = self._permits.capture_control_token()
        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, lifecycle_permits.RebuildPermitDenied):
            raise RuntimeError(permit.message)
        try:
            await self._reload_locked(
                new_profile,
                operation=operation,
                staged_loaded=staged_loaded,
                session=session,
                workspace_plan=workspace_plan,
                commit_workspace=commit_workspace,
            )
        finally:
            self._permits.release_rebuild_permit(permit)

    async def _reload_locked(
        self,
        new_profile: AgentProfile,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace_plan: WorkspacePlan | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Soft-restart while the caller owns the rebuild boundary."""
        reloaded_settings = None
        if staged_loaded is None:
            plan = workspace_plan if workspace_plan is not None else self._session.workspace_plan
            cwd = plan.workspace.primary_cwd if plan is not None else Workspace.from_cwd().primary_cwd
            staged_loaded = reloaded_settings = await self._settings_for_build(cwd)
        await self._loader.reload(
            new_profile,
            workspace_plan,
            operation=operation,
            staged_loaded=staged_loaded,
            session=session,
            commit_workspace=commit_workspace,
        )
        await self._publish_settings_warnings(reloaded_settings)

    async def reload_with_rebuild_permit(
        self,
        permit: lifecycle_permits.RebuildPermit,
        new_profile: AgentProfile,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace_plan: WorkspacePlan | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Soft-restart using an already-acquired rebuild permit."""
        self._permits.ensure_rebuild_permit(permit)
        await self.reload(
            new_profile,
            operation=operation,
            staged_loaded=staged_loaded,
            session=session,
            workspace_plan=workspace_plan,
            commit_workspace=commit_workspace,
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
        self.cleanup_empty_session_dir(path_reused=not release_lock)
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

        installed_workspace = self._session.session_workspace

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

        async def copy_session() -> str:
            # Forking is work on the new session, so it starts out on this launch's surface.
            return await asyncio.to_thread(
                state_store.fork_session, active_session_id, last_surface=self._session.surface
            )

        async def fork_trajectory(forked: str) -> None:
            # The fork gets its own closed opening runtime pointing back at the parent.
            await self._trajectory_recorder.fork(
                origin_session_id=active_session_id,
                fork_session_id=forked,
                fork_session_dir=self._session.session_dir_for(forked),
                fork_write_lock_path=self._session.session_write_lock_path(forked),
                session_start_info=self._loader.trajectory_session_start_info(),
            )

        async def fork_with_membership(installed: Workspace) -> str:
            forked = await copy_session()
            await fork_trajectory(forked)
            session_workspace = await self.workspaces.inherited_workspace(installed, active_session_id)
            fork_registry = state_store.workspace_registry
            if session_workspace is not None and fork_registry is not None:
                try:
                    fork_registry.set_session_workspace(forked, session_workspace.id, if_absent=True)
                except WorkspaceRegistryError as error:
                    await self.workspaces.warn_membership_write(
                        active_session_id,
                        FORK_MEMBERSHIP_WRITE_FAILED.bind(name=session_workspace.name),
                        error,
                        request_id=forked,
                    )
            return forked

        try:
            # Forks in a workspace must settle their best-effort membership even if the caller is cancelled.
            if installed_workspace is None:
                new_session_id = await copy_session()
            else:
                new_session_id = await finish_close(asyncio.create_task(fork_with_membership(installed_workspace)))
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

        if installed_workspace is None:
            await fork_trajectory(new_session_id)
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

    async def _reconcile_existing_spill_storage(
        self, *, target: ActiveSession | None = None
    ) -> SpillReconciliationResult:
        """Rebuild and account for retained spill storage without creating a session directory."""
        session = target if target is not None else self._session
        session_dir = session.session_dir
        if session_dir is None or not session_dir.is_dir():
            return SpillReconciliationResult(0, 0, frozenset())
        try:
            return await asyncio.to_thread(reconcile_spill_storage, session_dir, session.spill_quota)
        except OSError, RuntimeError, UnicodeError:
            # Spill records are auxiliary context. Filesystem damage or permissions
            # must not prevent the authoritative session state from hydrating.
            logger.warning("Unable to reconcile spill storage under %s", session_dir, exc_info=True)
            session.spill_quota.disable_storage()
            return SpillReconciliationResult(0, 0, frozenset())

    def _workspace_cwd(self) -> str:
        """Return the engine workspace cwd, falling back only for legacy unstarted engines."""
        if self._session.workspace is not None:
            return self._session.workspace.primary_cwd
        return safe_getcwd()

    def _can_restore_service_session(self, meta: ChatSessionMeta, *, saved_workspace: bool) -> bool:
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
                [working_dir.path for working_dir in session_workdirs(self._session.workspace)],
                self._session.workspace.primary_cwd,
            )
        ):
            return False
        scope = workspace_scope_fingerprint(self._session.workspace)
        saved_scope = meta.service_session_workspace_fingerprint
        if scope != saved_scope or (saved_workspace and not saved_scope):
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
        return session_pin_overrides(self._session, self._settings_handle.base)

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
        *,
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """The restored session's own choices: its saved model, when selectable and not bound by its agent.

        Its own, not the default: the selection rides the settings handle's
        session layer, goes live with the build's commit and ends when the
        session closes, so restoring a session never changes the model new
        sessions start on. Restoring the current session keeps the choice it
        has, and a saved model already in force adds nothing.
        """
        if current is None:
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

    async def reset_after_failed_startup_restore(
        self, *, reload_settings: bool = True, resume_at: _RestoreResume | None = None
    ) -> None:
        """Discard a partially opened session before admitting another operation.

        Restore can fail after installing the target session id, workspace, active
        lock, or a partial runtime. Suppress persistence while tearing those down so
        fallback startup cannot overwrite the saved target session.
        A caller retrying a fresh start can keep its launch settings. ``resume_at``
        is the folder a failed restore began from, and that folder's identity:
        what starts next runs there, Unspecified, while the same directory is
        still there.
        """
        with self._session.saves_suppressed():
            try:
                await self.close_session()
            except BaseException:
                await self._loader.release_current()
                raise
            finally:
                self._session.workspace_plan = None
                self._history.bind({})
                try:
                    self.reset_for_restart(None)
                finally:
                    self._session.guard.release()
                self.reset_turn_runtime_after_session_shutdown()
                self._end_session_choice()
                process_cwd = self._session.workspace_cwd()
                cwd = process_cwd
                if (
                    resume_at is not None
                    and (resumed_cwd := resolve_workspace_directory(resume_at.cwd)) is not None
                    and path_identity(resumed_cwd) == resume_at.identity
                ):
                    # The same directory the restore began in. A failed restore may
                    # have installed the target directory's settings, and everything
                    # read from now until the next build would see them, so reload
                    # for this one. A load that fails keeps them and says so; their
                    # identity still differs, so the next build tries again.
                    cwd = resumed_cwd
                    if (resumed_loaded := await self._settings_for_build(cwd)) is not None:
                        self._settings_handle.install(resumed_loaded)
                elif reload_settings:
                    if os.path.isdir(self._startup_cwd):
                        cwd = self._startup_cwd
                    try:
                        self._settings_handle.install(await self.stage_workspace_settings(cwd))
                    except Exception:
                        logger.warning("Settings re-derivation for the fallback startup root failed", exc_info=True)
                        await self._publish_settings_read_failure(cwd)
                if resume_at is not None or cwd != process_cwd:
                    self._session.install_workspace(
                        self.workspaces.plan(workspace=Workspace(primary_cwd=cwd), snapshot=RegistrySnapshot())
                    )
                await self._bus.publish(WorkspaceUpdated(primary_cwd=cwd, session_id=self._session.session_id))

    def reset_for_restart(self, session_id: str | None) -> None:
        """Reset per-session engine state in preparation for a fresh ``start()``.

        The approval mode is not per-session state: the mode the user last chose stays in
        force for every session of this launch, and only the next launch reads the saved default.
        """
        self._fsm.reset()
        self._session.reset(session_id=session_id)
        installed = self._session.workspace_plan
        if installed is not None and installed.departure is not None:
            self._session.install_workspace(
                self.workspaces.plan(workspace=installed.workspace, snapshot=RegistrySnapshot())
            )
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

    def cleanup_empty_session_dir(self, *, path_reused: bool = False) -> None:
        """Dispose of an unsaved directory and forget membership unless restarting on it."""
        cleanup_empty_session_dir_path(
            self._session.session_dir,
            session_id=self._session.session_id,
            state_store=self._persistence.state_store,
            path_reused=path_reused,
        )

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
        # This restart restores live memory after a partially failed delete,
        # rather than hydrating a verified checkpoint.
        self.reset_for_restart(session_id)
        if state is not None and has_real_messages(state):
            self._session.pending_conversation_save = object()
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
            plan = self._session.workspace_plan
            self._workspace_change_tracker.restore(
                state.get("chrys_workspace_baseline"),
                plan.workspace if plan is not None else None,
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

    async def on_new_session(self, event: SessionNew) -> None:
        """Stage the requested workspace before committing the fresh-session transition."""
        explicit = (
            event.workspace_id is not None
            or event.start_unspecified
            or bool(event.primary_cwd)
            or event.confirmation is not None
        )
        owner = await self._permits.prepare_session_transition("new_session")
        if owner is None:
            raise RuntimeError("Session transition acquisition unexpectedly failed")
        if not explicit and self._session.workspace_id is None:
            try:
                self._permits.commit_session_transition(owner)
                await self._start_fresh_session(None, None)
            finally:
                self._permits.finish_session_transition(owner)
            return
        outgoing_id = self._session.session_id
        committed = False
        error: Exception | None = None
        try:
            if explicit:
                plan, staged_loaded = await self._prepare_explicit_new_session(event)
            elif self._session.agent_profile is not None:
                plan = await self._plan_implicit_new_session(outgoing_id)
                staged_loaded = None
            else:
                plan, staged_loaded = None, None
            self._permits.commit_session_transition(owner)
            committed = True
            await self._start_fresh_session(plan, staged_loaded)
        except Exception as exc:
            error = exc
        finally:
            if committed:
                self._permits.finish_session_transition(owner)
            else:
                self._permits.abort_prepared_session_transition(owner)
        if error is None:
            return
        await self._bus.publish(
            Error(
                code="session_new_failed",
                message=str(error),
                display_message=workspace_error_display(error) or _NEW_SESSION_FAILED.bind(detail=str(error)),
                session_id=self._session.session_id if committed else outgoing_id,
            )
        )
        raise error

    async def _prepare_explicit_new_session(self, event: SessionNew) -> tuple[WorkspacePlan, LoadedSettings]:
        if self._session.agent_profile is None:
            raise ConfigError(_NEW_SESSION_NO_AGENT.bind())
        if event.primary_cwd and not event.start_unspecified:
            raise ConfigValueError(_NEW_SESSION_CHOOSE_CWD.bind())
        if event.workspace_id is not None and event.start_unspecified:
            raise ConfigValueError(_NEW_SESSION_CHOOSE_WORKSPACE.bind())
        workspace = self._session.workspace or Workspace.from_cwd()
        workspaces = self.workspaces
        source_id = self._session.workspace_id
        confirmation = event.confirmation
        snapshot = workspaces.snapshot()
        if event.workspace_id is not None:
            plan = workspaces.plan(
                workspace=workspace,
                live_plan=self._session.workspace_plan,
                snapshot=snapshot,
                operation=WorkspaceOperation.SELECT,
                workspace_id=event.workspace_id,
                confirmation=confirmation,
            )
        else:
            if event.primary_cwd or workspace.id:
                workspace = Workspace.from_cwd(event.primary_cwd or workspace.primary_cwd)
            if missing := workspace.missing_primary():
                raise MissingWorkspacePrimary(missing)
            if confirmation is not None:
                if not confirmation.matches(source_id, workspace.primary_cwd):
                    raise LeaveConfirmationRequired()
                workspace = Workspace.from_cwd(confirmation.primary_cwd)
            plan = workspaces.plan(workspace=workspace, snapshot=snapshot)
        staged_loaded = await self.stage_workspace_settings(plan.workspace.primary_cwd)
        return plan, staged_loaded

    async def _plan_implicit_new_session(self, session_id: str | None) -> WorkspacePlan | None:
        installed = self._session.session_workspace
        if installed is None:
            return self._session.workspace_plan
        if missing := installed.missing_primary():
            raise MissingWorkspacePrimary(missing)
        latest = await self.workspaces.inherited_workspace(installed, session_id)
        if latest is installed:
            # A failed registry read preserves the installed plan, including its missing entries.
            return self._session.workspace_plan
        return self.workspaces.plan(
            workspace=latest if latest is not None else Workspace.from_cwd(installed.primary_cwd),
            snapshot=RegistrySnapshot(),
        )

    async def stage_workspace_settings(
        self, cwd: str, *, settings_loader: Callable[..., LoadedSettings] | None = None
    ) -> LoadedSettings:
        """Resolve and route settings under the target root while holding its lifecycle permit."""
        old_loaded = self._settings_handle.base
        candidate = await asyncio.to_thread(
            settings_loader or load_settings,
            workdir=Path(cwd),
            eval_context=self._reload_eval_context(),
            **self._session_pin_overrides(),
        )
        staged, _ = route_restart_settings(reattribute_command_line(candidate, old_loaded), old_loaded)
        return staged

    async def _settings_for_build(self, cwd: str) -> LoadedSettings | None:
        """Stage settings for *cwd* when the installed ones belong to another directory.

        ``None`` means keep the installed settings: they were loaded for this
        directory already, or they belong to no directory at all and there is
        nothing to check (``LoadedSettings.workdir_identity``). A load that
        fails does not abort the build — the old configuration stays in use and
        the user is told — and because the identity still names the other
        directory, the next build tries again.
        """
        installed = self._settings_handle.base.workdir_identity
        if installed is None or installed == path_identity(cwd):
            return None
        try:
            return await self.stage_workspace_settings(cwd)
        except Exception:
            logger.warning("Settings re-derivation for %s failed; keeping the installed settings", cwd, exc_info=True)
            await self._publish_settings_read_failure(cwd)
            return None

    async def _publish_settings_read_failure(self, cwd: str) -> None:
        await self._bus.publish(replace(workdir_config_read_failure(cwd), session_id=self._session.session_id))

    async def _publish_settings_warnings(self, loaded: LoadedSettings | None) -> None:
        """Report the warnings of settings loaded for a directory, once they are in use."""
        if loaded is None:
            return
        for warning in settings_warning_events(loaded):
            await self._bus.publish(replace(warning, session_id=self._session.session_id))

    async def _start_fresh_session(
        self,
        workspace_plan: WorkspacePlan | None,
        staged_loaded: LoadedSettings | None,
        *,
        profile: AgentProfile | None = None,
    ) -> None:
        """Close the previous conversation after the transition has committed."""
        if profile is None:
            profile = self._session.agent_profile
        if profile is None:
            return
        if workspace_plan is not None and workspace_plan.departure is not None:
            workspace_plan = self.workspaces.plan(workspace=workspace_plan.workspace, snapshot=RegistrySnapshot())
        session_workspace = (
            workspace_plan.workspace if workspace_plan is not None and workspace_plan.workspace_id else None
        )
        await self.close_session()
        self.reset_turn_runtime_after_session_shutdown()
        self.reset_for_restart(None)
        self.workspaces.begin_session()

        registry = self.workspaces.registry
        commit = (
            MembershipWrite(registry, session_workspace, best_effort=True)
            if registry is not None and session_workspace is not None
            else None
        )
        try:
            await self.start(
                self._session.agent_profile or profile,
                operation="new_session",
                staged_loaded=staged_loaded,
                workspace_plan=workspace_plan,
                commit_workspace=commit,
            )
        finally:
            # Recorded at the commit point and reported here, so a cancel since cannot drop it.
            if commit is not None and commit.failure is not None and session_workspace is not None:
                await self.workspaces.warn_membership_write(
                    self._session.session_id,
                    NEW_MEMBERSHIP_WRITE_FAILED.bind(name=session_workspace.name),
                    commit.failure,
                )
        await self._publish_settings_warnings(staged_loaded)

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
        failure_display: MessageRef | None = None
        try:
            # Sample under the fence: a queued restore/new/switch cannot move the
            # active session while we hold the gate.
            profile = self._session.agent_profile
            if not self._session.guard.owns(event.session_id):
                failure_message = "Only the active session can be cleared"
            elif profile is None:
                failure_message = "No agent profile is active"
            else:
                has_workspace = self._session.workspace_id is not None
                try:
                    plan = await self._plan_implicit_new_session(event.session_id) if has_workspace else None
                except Exception as exc:
                    failure_message = str(exc)
                    failure_display = workspace_error_display(exc) or _CLEAR_SESSION_FAILED.bind(detail=failure_message)
                else:
                    failure = await self._delete_session_reporting(event.session_id)
                    if failure is not None:
                        failure_message = failure.message
                    else:
                        self._permits.commit_session_transition(transition_owner)
                        await self._start_fresh_session(plan, None, profile=None if has_workspace else profile)
        finally:
            self._permits.finish_session_transition(transition_owner)
        # Published after the fence is released — an Error handler must not find
        # the gate still held by the failed clear.
        if failure_message is not None:
            await self._bus.publish(
                Error(
                    code="session_clear_failed",
                    message=failure_message,
                    display_message=failure_display,
                    session_id=event.session_id,
                ),
            )

    async def on_session_restore(self, event: SessionRestore, *, for_rollback: bool = False) -> None:
        """Handle session restore by loading saved state and rebuilding the agent."""
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        requested_session_id = event.session_id
        state_store = self._persistence.state_store
        if state_store is None:
            await self._bus.publish(
                Error(code="no_state_store", message="State store not configured", session_id=event.session_id)
            )
            return

        strict = event.workspace_operation in (WorkspaceOperation.SELECT, WorkspaceOperation.UNSPECIFY)
        confirmation = event.confirmation
        # A workspace restore leaves the target's files untouched until it commits.
        defer_file_repairs = strict or confirmation is not None
        snapshot = event.workspace_snapshot
        if snapshot is None:
            snapshot = RegistrySnapshot() if for_rollback else self.workspaces.snapshot()
        target_lock: FileLock | None = None
        transition_owner: str | None = None
        restore_incomplete = False

        def release_restore_boundaries() -> None:
            nonlocal target_lock, transition_owner
            try:
                if target_lock is not None:
                    lock, target_lock = target_lock, None
                    lock.release()
            finally:
                if transition_owner is not None:
                    owner, transition_owner = transition_owner, None
                    self._permits.finish_session_transition(owner)

        async def reject_restore(error: Error) -> None:
            # EventBus handlers may request another transition inline.
            release_restore_boundaries()
            await self._bus.publish(error)

        try:
            # Fence session changes before deciding whether to borrow the current
            # lock. In particular, SessionNew cannot release it during file repair.
            # Keep the transition uncommitted until checkpoint and settings validation.
            if not self._permits.current_task_owns_session_transition_permit():
                transition_owner = await self._permits.prepare_session_transition("restore")
                if transition_owner is None:  # No owner filter was given, so this cannot happen.
                    raise RuntimeError("Session transition acquisition unexpectedly failed")
            restoring_current = self._session.guard.owns(event.session_id)
            if not restoring_current:
                try:
                    target_lock = await self._session.guard.acquire_for_restore_async(
                        event.session_id, timeout=SESSION_RESTORE_ACTIVE_LOCK_TIMEOUT_SECONDS
                    )
                except TimeoutError:
                    message, generation = self._session.guard.conflict_details(event.session_id)
                    await reject_restore(
                        Error(
                            code="session_in_use",
                            message=message,
                            session_owner_generation=generation,
                            session_id=event.session_id,
                        ),
                    )
                    return

            prefer_recovery = not event.ignore_recovery and (
                not restoring_current or self._session.recovered_from_sidecar
            )

            try:
                checkpoint = await self._persistence.load_chat_for_restore(
                    event.session_id,
                    prefer_recovery=prefer_recovery,
                    ignore_recovery=event.ignore_recovery,
                    repair=not defer_file_repairs,
                )
            except StartupSessionInUseError as exc:
                await reject_restore(Error(code="session_in_use", message=str(exc), session_id=event.session_id))
                return
            except ChatRestoreKindError as exc:
                await reject_restore(Error(code="session_kind_mismatch", message=str(exc), session_id=event.session_id))
                return
            if checkpoint is None:
                if strict and await asyncio.to_thread(
                    session_dir_has_artifacts, state_store.session_dir(event.session_id)
                ):
                    # A checkpoint can become unreadable between selection and the active lock.
                    raise ConfigValueError(_RESTORE_NO_CHECKPOINT.bind(session_id=repr(event.session_id)))
                # A workspace restore that fails leaves the target's files as they were.
                if event.ignore_recovery and not restoring_current and not defer_file_repairs:
                    cleanup_empty_session_dir_path(
                        state_store.session_dir(event.session_id), session_id=event.session_id, state_store=state_store
                    )
                await reject_restore(
                    Error(
                        code="session_not_found",
                        message=f"Session '{event.session_id}' not found",
                        session_id=event.session_id,
                    )
                )
                return

            state, meta = checkpoint.state, checkpoint.meta
            membership_session_id = meta.session_id if meta is not None else event.session_id

            cwd_warning = ""

            async def check_restore_cwd(path: str) -> bool:
                nonlocal cwd_warning
                if not path or os.path.isdir(path):
                    return True
                if restoring_current:
                    cwd_warning = f"Working directory no longer exists: {surrogate_safe_text(path)}"
                    return True
                await refuse_missing_cwd(path)
                return False

            async def refuse_missing_cwd(path: str) -> None:
                await reject_restore(
                    Error(
                        code="session_cwd_missing",
                        message=(
                            f"Working directory of session {session_short_id(requested_session_id)} "
                            f"no longer exists: {surrogate_safe_text(path)}"
                        ),
                        display_message=_RESTORE_SESSION_CWD_MISSING.bind(path=DisplayPath(path)),
                        session_id=event.session_id,
                    )
                )

            saved_cwd = meta.primary_cwd if meta and meta.primary_cwd else ""
            early_workspace = snapshot.session_workspace(membership_session_id)
            saved_workspace_membership = snapshot.maps_session(membership_session_id) or (
                for_rollback and self._session.workspace_id is not None
            )
            ordinary_unspecified = (
                not strict
                and confirmation is None
                and early_workspace is None
                and (not for_rollback or self._session.workspace_id is None)
            )
            registry_error = snapshot.error
            warned_registry = bool(registry_error) and not for_rollback
            if warned_registry and registry_error is not None:
                await self.workspaces.publish_unreadable(event.session_id, registry_error)
            opened_cwd = event.opened_cwd
            requested_cwd = None
            if ordinary_unspecified:
                requested_cwd = event.primary_cwd or saved_cwd or self._workspace_cwd()
                if not await check_restore_cwd(requested_cwd):
                    return
            elif (
                early_workspace is not None
                and not strict
                and not event.primary_cwd
                and not opened_cwd
                and confirmation is None
            ):
                try:
                    opened_cwd = validate_workspace_primary(early_workspace.primary_cwd)
                except MissingWorkspacePrimary:
                    await refuse_missing_cwd(early_workspace.primary_cwd)
                    return

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
                        await reject_restore(
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
                        await reject_restore(
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

            def restore_workspace(cwd: str) -> Workspace:
                if event.working_dirs is not None:
                    paths = list(dict.fromkeys((cwd, *(path for path in event.working_dirs if path))))
                elif event.primary_cwd and event.primary_cwd != saved_cwd:
                    paths = []
                else:
                    paths = list(meta.working_dirs) if meta is not None else []
                return Workspace(cwd, [WorkingDir(path=path, is_primary=path == cwd) for path in paths])

            ordinary_workspace = restore_workspace(requested_cwd) if requested_cwd is not None else None
            # Taken inside the fence, so a reload that finished first is what a failure returns to.
            previous_cwd = self._session.workspace_cwd()
            resume_at = (
                _RestoreResume(previous_cwd, path_identity(previous_cwd)) if os.path.isdir(previous_cwd) else None
            )

            commit_workspace = None
            left_name = None
            if confirmation is not None:
                # A picker snapshot describes what the user saw. Check its confirmation
                # against membership under the restore lock and transition fence.
                snapshot = self.workspaces.snapshot()
                early_workspace = snapshot.session_workspace(membership_session_id)
                saved_workspace_membership = snapshot.maps_session(membership_session_id)
            if ordinary_workspace is not None:
                workspace_plan = self.workspaces.plan(workspace=ordinary_workspace, snapshot=RegistrySnapshot())
                initial_plan = workspace_plan
            else:
                baseline_cwd = saved_cwd or self._workspace_cwd()
                live_plan = self._session.workspace_plan if for_rollback else None
                requested_workspace = restore_workspace(event.primary_cwd or baseline_cwd)
                prevalidated_cwd = (
                    event.workspace_operation is WorkspaceOperation.SELECT
                    and self._session.session_id is None
                    and self._session.surface in (SessionSurface.CLI, SessionSurface.TUI)
                ) or (
                    early_workspace is not None
                    and event.workspace_snapshot is not None
                    and self._session.surface is SessionSurface.ACP
                )
                try:
                    workspace_plan = (
                        live_plan
                        if for_rollback and live_plan is not None and live_plan.workspace_id
                        else self.workspaces.plan(
                            workspace=requested_workspace,
                            snapshot=snapshot,
                            live_plan=live_plan,
                            operation=event.workspace_operation,
                            session_id=membership_session_id,
                            restoring=True,
                            workspace_id=event.workspace_id,
                            target_cwd="" if prevalidated_cwd else event.primary_cwd,
                            require_leave_confirmation=event.require_leave_confirmation,
                            confirmation=confirmation,
                            opened_cwd=(event.primary_cwd if prevalidated_cwd else "") or opened_cwd,
                            use_target_cwd=self._session.surface is SessionSurface.CLI,
                        )
                    )
                except LeaveConfirmationRequired as exc:
                    if strict:
                        raise
                    await reject_restore(
                        Error(
                            code="workspace_leave_confirmation_required",
                            message=str(exc),
                            display_message=exc.display_message,
                            session_id=event.session_id,
                        )
                    )
                    return
                source_workspace = live_plan.workspace if live_plan is not None else early_workspace
                left_name = (
                    source_workspace.name if workspace_plan.left_workspace and source_workspace is not None else None
                )
                primary_validated = workspace_plan.workspace_id or (
                    early_workspace is not None
                    and (confirmation is not None or event.workspace_operation is WorkspaceOperation.UNSPECIFY)
                )
                if not primary_validated and not await check_restore_cwd(workspace_plan.workspace.primary_cwd):
                    return

                commit_workspace = (
                    self.workspaces.membership_write(
                        workspace_plan,
                        event.workspace_operation,
                        source_id=source_workspace.id if source_workspace is not None else None,
                    )
                    if not for_rollback
                    else None
                )
                initial_plan = workspace_plan
                if not strict and workspace_plan.left_workspace and source_workspace is not None:
                    # Until the membership change commits, a failed build recovers
                    # the workspace that an ordinary restore of this target resolves.
                    initial_plan = self.workspaces.plan(workspace=source_workspace, snapshot=RegistrySnapshot())

            # The restore crosses into the target session's working directory trust domain:
            # its settings are re-derived for that root so the build below reads
            # them, exactly as a workspace change would. Loaded here — before
            # anything is torn down — so an unreadable config file aborts the
            # restore with the current session intact.
            staged_loaded = await self.stage_workspace_settings(workspace_plan.workspace.primary_cwd)
            if profile is None:
                # Sampled after the transition fence, like the live agent was:
                # a switch that won the gate meanwhile is the one in force.
                profile = self._restore_fallback_profile(restoring_current=restoring_current)
                if not profile_name and profile is not None:
                    profile_name = profile.name
            prepared = None
            load_started = False

            async def open_restored_session() -> None:
                nonlocal restore_incomplete, target_lock
                if restoring_current:
                    await self.close_session_in_place(keep_side_answers=True)
                else:
                    previous_id = self._session.session_id
                    try:
                        await self.close_session()
                    except BaseException:
                        restore_incomplete = self._current.loaded is None and (
                            previous_id is None or not self._session.guard.owns(previous_id)
                        )
                        raise
                restore_incomplete = True
                self.reset_turn_runtime_after_session_shutdown()
                if defer_file_repairs:
                    await state_store.repair_chat_restore(
                        membership_session_id, prefer_recovery=prefer_recovery, ignore_recovery=event.ignore_recovery
                    )

                if prepared is not None:
                    # Workers can still append spill records while the target builds.
                    spill = await self._reconcile_existing_spill_storage(target=prepared.session)
                    if state and prepared.build is not None:
                        self._restore_history_state(prepared.build.loaded, state, spill)
                        stamp_history_item_ids(prepared.build.loaded.bindings.backend.history_state)

                if target_lock is not None:
                    self._session.guard.install(requested_session_id, target_lock)
                    target_lock = None

                if not for_rollback:
                    self.workspaces.begin_session(warned=warned_registry)
                self._fsm.reset()
                self._session.adopt_restore_identity(
                    session_id=requested_session_id,
                    recovered_from_sidecar=checkpoint.recovered_from_sidecar,
                    membership_session_id=membership_session_id,
                )
                await self._hydrate_restored_session(
                    event=event,
                    membership_session_id=membership_session_id,
                    saved_workspace_membership=saved_workspace_membership,
                    checkpoint=checkpoint,
                    profile=profile,
                    profile_name=profile_name,
                    profile_switch=profile_switch,
                    profile_resolution_warning=profile_resolution_warning,
                    cwd_warning=cwd_warning,
                    workspace_plan=workspace_plan,
                    staged_loaded=staged_loaded,
                    initial_plan=initial_plan,
                    commit_workspace=commit_workspace,
                    prepared_restore=prepared,
                )
                restore_incomplete = False

            try:
                try:
                    if defer_file_repairs and commit_workspace is not None:
                        choices = (
                            self._restored_session_choice(meta, profile, staged_loaded, current={})
                            if event.apply_saved_model and profile is not None
                            else {}
                        )
                        self._settings_handle.check_prepared(
                            self._settings_handle.prepare(staged_loaded, session=choices)
                        )
                        profiles = await self._loader.stage_restore_profiles() if profile is not None else None
                        if strict:
                            commit_workspace.save(membership_session_id)
                        if profile is not None:
                            load_started = True
                            await self._loader.begin_load(profile, operation="restore", session_id=event.session_id)
                        prepared = await self._prepare_strict_restore(
                            requested_session_id, state, profile, workspace_plan, staged_loaded, choices, profiles
                        )
                        if prepared.build is not None:
                            profile = prepared.build.staged.agent_profile
                        if not strict:
                            commit_workspace.save(membership_session_id)
                    if transition_owner is not None:
                        self._permits.commit_session_transition(transition_owner)
                    if prepared is None:
                        await open_restored_session()
                    else:
                        await self._permits.finish_committed_session_step(open_restored_session)
                finally:
                    if (
                        prepared is not None
                        and prepared.build is not None
                        and self._current.loaded is not prepared.build.loaded
                    ):
                        await self._discard_restore_build(prepared.build, prepared.session)
            except Exception as exc:
                if load_started and self._permits.agent_loading and profile is not None:
                    self._permits.finish_agent_load()
                    await self._loader.publish_load_failed(
                        operation="restore", profile=profile, exc=exc, session_id=event.session_id
                    )
                if commit_workspace is not None and commit_workspace.saved:
                    current = (
                        self._session.workspace if not restore_incomplete and self._current.loaded is not None else None
                    )
                    raise commit_workspace.open_failure(current, exc) from exc
                raise
            finally:
                if load_started and self._permits.agent_loading:
                    self._permits.finish_agent_load()
                # Recorded at the commit point and reported here, so a cancel since cannot drop it.
                if commit_workspace is not None and commit_workspace.failure is not None and left_name is not None:
                    await self.workspaces.warn_membership_write(
                        membership_session_id,
                        LEAVE_MEMBERSHIP_WRITE_FAILED.bind(name=left_name),
                        commit_workspace.failure,
                    )
            if left_name is not None and self._session.surface is SessionSurface.CLI:
                await self._bus.publish(workspace_left_warning(event.session_id, left_name))
            if (
                event.workspace_operation is WorkspaceOperation.SELECT
                and early_workspace is not None
                and early_workspace.id
                and early_workspace.id != workspace_plan.workspace_id
            ):
                await self._bus.publish(
                    workspace_selected_warning(event.session_id, early_workspace, workspace_plan.workspace)
                )
        except BaseException as exc:
            if restore_incomplete:
                try:
                    await self.reset_after_failed_startup_restore(resume_at=resume_at)
                except Exception:
                    logger.warning("Failed to close the incomplete session restore", exc_info=True)
            release_restore_boundaries()
            if isinstance(exc, Exception):
                # The interactive callers publish this event with the bus's
                # default swallow-and-log delivery: without a terminal event the
                # restore loading UI has nothing to clear it. Published after the
                # fence is released — an Error handler must not find the gate
                # still held by the failed restore.
                await self._bus.publish(
                    Error(
                        code=(
                            "workspace_leave_confirmation_required"
                            if isinstance(exc, LeaveConfirmationRequired)
                            else exc.code
                            if isinstance(exc, WorkspaceRegistryError)
                            and exc.code == "workspace_membership_saved_open_failed"
                            else "session_restore_failed"
                        ),
                        message=str(exc),
                        display_message=workspace_error_display(exc),
                        session_id=event.session_id,
                    )
                )
            raise
        finally:
            release_restore_boundaries()

    async def _restore_resources(
        self, target: ActiveSession, state: dict[str, Any], staged_loaded: LoadedSettings
    ) -> None:
        snapshot_store = SnapshotStore(
            cast("Path", target.session_dir), policy=SnapshotPolicy.from_settings(staged_loaded.settings)
        )
        mutations = state.get("chrys_mutations")
        target.mutation_tracker = (
            MutationTracker.deserialize(mutations, snapshot_store) if mutations else MutationTracker(snapshot_store)
        )
        target.todo_tracker = TodoTracker()
        if state:
            await target.todo_tracker.restore(state.get("chrys_todos"))
        target.mutation_coordinator = None
        target.runtime_meta = SessionRuntimeMetadata.from_state_dict(state)

    async def _prepare_strict_restore(
        self,
        session_id: str,
        state: dict[str, Any],
        profile: AgentProfile | None,
        plan: WorkspacePlan,
        loaded: LoadedSettings,
        choices: Mapping[str, Any],
        profiles: ProfileReload | None,
    ) -> _PreparedRestore:
        """Build the target before closing the outgoing session."""
        owner = self._permits.capture_control_token()
        target = ActiveSession(
            persistence=self._persistence,
            workspace_plan=plan,
            approval_mode=self._session.approval_mode,
            surface=self._session.surface,
            pause_failed_sub_agents=self._session.pause_failed_sub_agents,
        )
        target.session_id = session_id
        if self._session.guard.owns(session_id):
            target.background_tasks = self._session.background_tasks
        tracker = WorkspaceChangeTracker()
        tracker.restore(state.get("chrys_workspace_baseline"), plan.workspace, resolve_scope=False)
        await self._restore_resources(target, state, loaded)
        settings = self._settings_handle.prepare(loaded, session=choices)
        build = None
        try:
            if profile is not None and profiles is not None:
                profile = profiles.updated_agent(profile) or profile
                target.hook_manager = await self._loader.stage_hook_manager(
                    operation="restore", staged_loaded=loaded, workspace=plan.workspace, restore_session=target
                )
                self._prepare_background_tasks(target, session_id)
                staged = self._loader.stage(
                    loaded=loaded,
                    session=choices,
                    agent_profile=profile,
                    workspace_plan=plan,
                    hook_manager=target.hook_manager,
                    restore_session=target,
                    restore_tracker=tracker,
                )
                build = await self._loader.build(profile, staged, restore_profiles=profiles)
                await self._loader.normalize_candidate_profiles(profiles)
                settings = build.settings
                retarget = build.workspace_retarget
            else:
                retarget = await asyncio.to_thread(
                    tracker.resolve_retarget,
                    plan.workspace,
                    resolve_scope=settings.effective.settings.workspace_change_notice,
                )
            if (
                self._session.shutting_down and self._current.loaded is not None
            ) or owner != self._permits.capture_control_token():
                raise ConfigError(_RESTORE_SESSION_CHANGED.bind())
            self._settings_handle.check_prepared(settings)
            return _PreparedRestore(target, tracker, settings, retarget, build, profiles)
        except BaseException:
            await self._discard_restore_build(build, target)
            raise

    async def _discard_restore_build(self, build: CompletedBuild | None, target: ActiveSession) -> None:
        try:
            if build is not None:
                await self._loader.discard_candidate(build)
        finally:
            await self._loader.settle_staged_hook_manager(old=None, staged=target.hook_manager)

    @staticmethod
    def _restore_history_state(loaded: LoadedAgent, state: dict[str, Any], spill: SpillReconciliationResult) -> None:
        loaded.bindings.backend.history_state = state
        restore_phase4_state(
            loaded.reminder_middleware,
            loaded.last_words,
            state,
            available_relative_paths=spill.available_relative_paths,
        )

    async def _hydrate_restored_session(
        self,
        *,
        event: SessionRestore,
        membership_session_id: str,
        saved_workspace_membership: bool,
        checkpoint: ChatRestoreData,
        profile: AgentProfile | None,
        profile_name: str,
        profile_switch: tuple[str, str] | None,
        profile_resolution_warning: Warning | None,
        cwd_warning: str,
        workspace_plan: WorkspacePlan,
        staged_loaded: LoadedSettings,
        initial_plan: WorkspacePlan,
        commit_workspace: MembershipWrite | None = None,
        prepared_restore: _PreparedRestore | None = None,
    ) -> None:
        """Hydrate live engine state after restore shutdown while admission remains closed."""
        if event.session_id is None:
            raise ValueError("This session event requires a session ID.")
        state, meta = checkpoint.state, checkpoint.meta
        recovered_from_sidecar = checkpoint.recovered_from_sidecar
        self._session.install_workspace(initial_plan)
        spill_reconciliation = None
        if prepared_restore is None:
            self._workspace_change_tracker.restore(
                state.get("chrys_workspace_baseline"),
                initial_plan.workspace,
                resolve_scope=staged_loaded.settings.workspace_change_notice,
            )
            spill_reconciliation = await self._reconcile_existing_spill_storage()
            await self._restore_resources(self._session, state, staged_loaded)
        else:
            self._workspace_change_tracker.install_restored_state(prepared_restore.tracker)
            target = prepared_restore.session
            self._session.spill_quota = target.spill_quota
            self._session.mutation_tracker = target.mutation_tracker
            self._session.todo_tracker = target.todo_tracker
            self._session.runtime_meta = target.runtime_meta
            self._session.background_tasks = target.background_tasks
            self._session.mutation_coordinator = None

        prepared_build = prepared_restore.build if prepared_restore is not None else None
        if profile is not None:
            if prepared_restore is not None and prepared_build is not None:
                self._session.begin(agent_profile=profile)
                trajectory = self._bind_trajectory(
                    event.session_id, self._session.session_dir, self._session.session_write_lock_path(event.session_id)
                )
                replaced = await self._loader._install_candidate(
                    prepared_build, restore_profiles=prepared_restore.profiles
                )
                await self._loader.release(replaced)
                await self._loader.finish_load(profile, operation="restore", trajectory=trajectory)
            else:
                choices = (
                    self._restored_session_choice(meta, profile, staged_loaded)
                    if event.apply_saved_model
                    else self._settings_handle.session_values
                )
                await self.start(
                    profile,
                    operation="restore",
                    workspace_plan=workspace_plan,
                    staged_loaded=staged_loaded,
                    session=choices,
                    commit_workspace=commit_workspace,
                )
            if profile_switch is not None and self._current.loaded is not None:
                self._current.loaded.reminder_middleware.sources.profile_switch.set_profile_switch(*profile_switch)
        elif prepared_restore is None and workspace_plan.workspace_id is None and commit_workspace is None:
            self._settings_handle.install(staged_loaded)
        else:
            if prepared_restore is not None:
                prepared, retarget = prepared_restore.settings, prepared_restore.retarget
            else:
                owner = self._permits.capture_control_token()
                prepared = self._settings_handle.prepare(staged_loaded)
                retarget = await asyncio.to_thread(
                    self._workspace_change_tracker.resolve_retarget,
                    workspace_plan.workspace,
                    resolve_scope=staged_loaded.settings.workspace_change_notice,
                    freeze_baseline=True,
                )
                if self._session.shutting_down or owner != self._permits.capture_control_token():
                    raise ConfigError(_RESTORE_SESSION_CHANGED.bind())
                if commit_workspace is not None:
                    self._settings_handle.check_prepared(prepared)
                if not self._workspace_change_tracker.retarget_is_current(retarget):
                    raise StaleWorkspacePlan("Workspace changes changed during preparation.", WORKSPACE_CHANGED.bind())
                if commit_workspace is not None:
                    commit_workspace.save(membership_session_id)
            self._session.install_workspace(workspace_plan)
            self._settings_handle.install_prepared(prepared)
            self._workspace_change_tracker.apply_retarget(retarget)

        # The verdicts describe the target root's files — the same report a
        # reload or workspace change makes — published only now that the staged
        # load is in force (committed by the build above, or installed by the
        # degenerate path), under the restored session's id.
        restore_warnings = settings_warning_events(staged_loaded)
        if profile_resolution_warning is not None:
            restore_warnings.insert(0, profile_resolution_warning)
        for warning in restore_warnings:
            await self._bus.publish(replace(warning, session_id=self._session.session_id))

        if (loaded := self._current.loaded) is not None and state:
            if spill_reconciliation is not None:
                self._restore_history_state(loaded, state, spill_reconciliation)
            if meta and meta.service_session_id:
                if self._can_restore_service_session(meta, saved_workspace=saved_workspace_membership):
                    loaded.bindings.backend.service_session_id = meta.service_session_id
                else:
                    loaded.bindings.backend.service_session_id = ""
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
            backend = self._current.require_loaded().bindings.backend
            if prepared_build is None:
                stamp_history_item_ids(backend.history_state)
                self._history.bind(backend.history_state)

            # ``engine.start`` above already hydrated the fresh strategy through
            # ``_publish_results``; this covers the no-rebuild path (``profile is
            # None``) where the surviving executor's strategy is the only target.
            # The provenance gate makes the repeat call idempotent.
            if (
                self._session.runtime_meta.context_calibration is not None
                and (strategy := backend.compaction_strategy) is not None
            ):
                self._session.runtime_meta.restore_context_calibration(
                    strategy,
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

            restored_history_changed = self._history.close_unfinished_turn() or restored_history_changed
            self._restore_terminal_fsm_from_history()

        # Interrupted recovery can append the missing final boundary above.
        restored_state = self._history.state if self._history.is_bound else state or {}
        self._session.turn_number = restored_state.get("turn_counter", 0)

        saved = False
        if restored_history_changed:
            try:
                saved = await self._writer.save_current_session(raise_on_error=bool(paused_records))
            except Exception:
                logger.warning("Session restore: failed to save repaired session; preserving paused sub-agent records")
            if saved and paused_records and artifact_service is not None:
                artifact_service.finalize_restored_paused_records(paused_records)
        elif paused_records and artifact_service is not None:
            artifact_service.archive_unconsumed_restored_paused_records(paused_records)

        replay = None
        if self._session.surface == SessionSurface.TUI:
            # A linked source may change owners while the agent is being built.
            # Only a successful local repair supersedes the checkpoint we accepted.
            if saved and (store := self._persistence.state_store) is not None:
                replay = await store.load_session_for_replay(event.session_id)
            if replay is None:
                replay = await asyncio.to_thread(SessionReplay.from_saved_state, checkpoint.saved_state, meta)

        target_cwd = workspace_plan.workspace.primary_cwd
        session_workspace = self._session.session_workspace
        token = (
            self._permits.capture_control_token()
            if session_workspace is not None and not self._session.suppress_save
            else None
        )
        await self._bus.publish(
            SessionRestored(
                workspace=session_workspace,
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
                replay=replay,
                working_dirs=[working_dir.path for working_dir in session_workdirs(self._session.workspace)],
            ),
        )
        # Rollback restores with saves suppressed and does not record another workspace use.
        if token is not None and session_workspace is not None:
            await self.touch_opened_workspace(
                session_workspace,
                target_cwd,
                token=token,
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
