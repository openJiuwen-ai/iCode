# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-internal companion module for ``AgentEngine`` build and restart orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.config.errors import ConfigError
from chrys.foundation.errors import clean_error_message
from chrys.foundation.errors.display import display_fields
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadProgress,
    AgentLoadStarted,
    ApprovalModeUpdated,
    ProfileSwitched,
    SessionReady,
    Warning,
)
from chrys.foundation.i18n import msg
from chrys.foundation.models.workspace import Workspace, session_workdirs, workspace_scope_fingerprint
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.trajectory.event_types import ProfileKind
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.engine.build import construction
from chrys.orchestration.engine.build.construction import BuildAgentFn, StagedBuild
from chrys.orchestration.engine.build.loaded import AgentManifest, CompletedBuild, ReplacedBuild
from chrys.orchestration.engine.hook_reporting import wire_hook_reporting
from chrys.orchestration.engine.state.machine import Trigger
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.orchestration.engine.workspaces import (
    WORKSPACE_CHANGED,
    MembershipWrite,
    SessionWorkspaces,
    StaleWorkspacePlan,
    WorkspacePlan,
    record_workspace_use,
    workspace_updated_event,
)
from chrys.orchestration.invoker.resources import PreparedAgent
from chrys.orchestration.session_hooks import SessionHookFactory, hooks_unchanged
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES
from chrys.service.profiles.reload import ProfileReload
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.trajectory.session import SessionStartInfo
from chrys.service.workspaces.registry import RegistrySnapshot

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import RuntimeSkillDetails
    from chrys.orchestration.engine.reload_resources import ReloadResources
    from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits, RebuildControlToken
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.state.session_writer import RecoveryCheckpoints
    from chrys.orchestration.engine.usage import UsagePublisher
    from chrys.orchestration.session_hooks import StrictHooks
    from chrys.service.approval.turn_context import TurnContextHolder
    from chrys.service.hooks.confirmation import HookConfirmationService
    from chrys.service.hooks.manager import HookManager
    from chrys.service.hooks.run_log import HookRunLog
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile
    from chrys.service.session.history import SessionHistoryManager
    from chrys.service.session.persistence import SessionPersistence
    from chrys.service.trajectory.session import SessionTrajectory


logger = logging.getLogger(__name__)


_CONSTRUCTION_SERVICE_SESSION_INCOMPATIBLE = msg(
    "construction.service_session_incompatible",
    fallback=(
        "The previous OpenAI Responses service session is not compatible with the active agent profile, workspace, "
        "model profile, service endpoint, or storage is disabled. {app_name} will continue from local history only."
    ),
)


def _is_openai_responses_profile(profile: ModelProfile | None) -> bool:
    """Return True for profiles that use OpenAI Responses transport."""
    return profile is not None and profile.provider == "openai" and profile.api_style == API_STYLE_RESPONSES


def _responses_service_session_profiles_match(
    old_profile: ModelProfile | None, new_profile: ModelProfile | None
) -> bool:
    """Return True when two profiles can safely share a Responses service session id."""
    return (
        _is_openai_responses_profile(old_profile)
        and _is_openai_responses_profile(new_profile)
        and old_profile is not None
        and new_profile is not None
        and old_profile.model_id == new_profile.model_id
    )


def _workspace_signature(workspace: Workspace | None) -> str | tuple[str, tuple[str, ...]]:
    """Return the session-relevant workspace identity for service-session reuse."""
    if workspace is None:
        return ("", ())
    return workspace_scope_fingerprint(workspace) or (
        workspace.primary_cwd,
        tuple(working_dir.path for working_dir in workspace.working_dirs),
    )


def _workspace_primary_cwd(workspace: Workspace | None) -> str:
    """Return the workspace primary cwd used for directory-scoped settings."""
    return workspace.primary_cwd if workspace is not None else ""


def _can_reuse_responses_service_session(
    *,
    old_agent_profile_fingerprint: str,
    new_agent_profile_fingerprint: str,
    old_model_profile_fingerprint: str,
    new_model_profile_fingerprint: str,
    old_model_profile: ModelProfile | None,
    new_model_profile: ModelProfile | None,
    old_model_base_url: str,
    new_model_base_url: str,
    old_workspace: Workspace | None,
    new_workspace: Workspace | None,
    old_storage_enabled: bool,
    new_storage_enabled: bool,
) -> bool:
    """Return True when a Responses service session can safely survive a rebuild."""
    return bool(
        _responses_service_session_profiles_match(old_model_profile, new_model_profile)
        and old_agent_profile_fingerprint
        and old_agent_profile_fingerprint == new_agent_profile_fingerprint
        and old_model_profile_fingerprint
        and old_model_profile_fingerprint == new_model_profile_fingerprint
        and old_model_base_url
        and old_model_base_url == new_model_base_url
        and _workspace_signature(old_workspace) == _workspace_signature(new_workspace)
        and old_storage_enabled
        and new_storage_enabled
    )


async def _close_replaced_hook_manager(manager: HookManager | None) -> None:
    """Close a hook manager that no longer belongs to the live executor."""
    if manager is None:
        return
    try:
        await manager.drain_session()
    except Exception:
        logger.exception("Error draining replaced hook manager")


def _preserved_history_state(raw: dict | None) -> dict | None:
    """Deep copy of the predecessor's history state, installed as the successor's live history.

    The copy owns every layer (messages, contents lists, content objects) so the
    successor's registries and anchors can never alias the predecessor's live
    objects. Falsy/empty history returns ``None`` — no carryover to preserve.
    """
    return copy.deepcopy(raw) if raw else None


SESSION_START_OPERATIONS: Final = frozenset({"startup", "new_session", "reset"})
"""The loads that start a session, as opposed to replacing one under it.

A restore is not one of them: ``session_restored`` already says the session came
back, and firing ``session_start`` too would change what an existing hook sees
then, including a rollback to an earlier turn. Profile switches and rebuilds are
not either: the session was already running.
"""


def fires_session_start(operation: str) -> bool:
    """Whether a load of *operation* starts a session, so its hooks fire."""
    return operation in SESSION_START_OPERATIONS


class AgentLoader:
    """Construct candidates, install their values, and release displaced build resources."""

    def __init__(
        self,
        *,
        bus: EventBus,
        persistence: SessionPersistence,
        agent_registry: AgentProfileRegistry | None,
        model_registry: ModelProfileRegistry | None,
        settings_handle: SettingsHandle,
        session: ActiveSession,
        current: CurrentAgent,
        permits: LifecyclePermits,
        checkpoints: RecoveryCheckpoints,
        usage_publisher: UsagePublisher,
        hooks: TurnHookDispatcher,
        history: SessionHistoryManager,
        turn_state: TurnRuntimeState,
        workspace_change_tracker: WorkspaceChangeTracker,
        trajectory_recorder: TrajectoryRecorder,
        fsm: EngineStateMachine,
        turn_context: TurnContextHolder,
        mcp_cache: MCPConnectionCache,
        mcp_overlay: list[MCPServerConfig] | None,
        allow_user_interaction: bool,
        build_agent_fn: BuildAgentFn,
        register_current_engine: Callable[[], None],
        hook_runs: HookRunLog | None = None,
        hook_confirmations: HookConfirmationService | None = None,
    ) -> None:
        self._bus = bus
        self._hook_runs = hook_runs
        self._hook_confirmations = hook_confirmations
        self._persistence = persistence
        self._agent_registry = agent_registry
        self._model_registry = model_registry
        self._settings_handle = settings_handle
        self._session = session
        self._current = current
        self._permits = permits
        self._checkpoints = checkpoints
        self._usage_publisher = usage_publisher
        self._hooks = hooks
        self._history = history
        self._turn_state = turn_state
        self._workspace_change_tracker = workspace_change_tracker
        self._trajectory_recorder = trajectory_recorder
        self._fsm = fsm
        self._mcp_cache = mcp_cache
        self._turn_context = turn_context
        self._injection_notify_tasks: set[asyncio.Task[None]] = set()
        self._mcp_overlay = list(mcp_overlay or [])
        self._allow_user_interaction = allow_user_interaction
        self._build_agent_fn = build_agent_fn
        self._register_current_engine = register_current_engine

    def stage(
        self,
        *,
        loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        agent_profile: AgentProfile,
        workspace_plan: WorkspacePlan,
        hook_manager: HookManager | None,
        restore_session: ActiveSession | None = None,
        restore_tracker: WorkspaceChangeTracker | None = None,
        workspace_transaction: bool = False,
    ) -> StagedBuild:
        """Assemble the settings, workspace, and coordinator candidate.

        *loaded* is the layered load to build on (the installed one when None:
        never the effective value, which already carries the session's own
        choices); *session* the session's own choices to build with (the
        installed ones when None). The build reads both composed, and its commit
        installs them.
        """
        base = loaded if loaded is not None else self._settings_handle.base
        return construction.stage_build(
            session=self._session,
            persistence=self._persistence,
            prepared=self._settings_handle.prepare(base, session=session),
            agent_profile=agent_profile,
            workspace_plan=workspace_plan,
            hook_manager=hook_manager,
            restore_session=restore_session,
            restore_tracker=restore_tracker,
            workspace_transaction=workspace_transaction,
        )

    async def stage_hook_manager(
        self,
        *,
        operation: str,
        staged_loaded: LoadedSettings | None,
        workspace: Workspace | None,
        restore_session: ActiveSession | None = None,
    ) -> HookManager | None:
        """Construct hooks for the session that will own the build."""
        resources = self._permits.staged_reload_resources() if restore_session is None else None
        target = restore_session or self._session
        hook_manager = target.hook_manager
        replace_hooks = hook_manager is None or operation == "workspace_change" or self._settings_moved(staged_loaded)
        if resources is not None:
            hook_manager = self._reloaded_hook_manager(resources.hooks)
        elif replace_hooks:
            build_settings = (staged_loaded if staged_loaded is not None else self._settings_handle.base).settings
            hook_manager = await self.build_hook_manager(
                workdir=_workspace_primary_cwd(workspace),
                workdir_hooks_enabled=build_settings.workdir_hooks_enabled,
                session_id=target.session_id,
            )
        if replace_hooks and hook_manager is not None:
            hook_manager.trajectory_context_provider = self._trajectory_recorder.context
        return hook_manager

    def _settings_moved(self, staged_loaded: LoadedSettings | None) -> bool:
        """Whether *staged_loaded* belongs to another directory than the installed settings.

        Working directory hooks are read from that directory, so they follow it
        even when the cwd string is unchanged (a link pointed elsewhere).
        """
        if staged_loaded is None:
            return False
        installed = self._settings_handle.base.workdir_identity
        staged = staged_loaded.workdir_identity
        return installed is not None and staged is not None and installed != staged

    async def build(
        self,
        profile: AgentProfile,
        staged: StagedBuild,
        *,
        preserved_history: dict | None = None,
        restore_profiles: ProfileReload | None = None,
    ) -> CompletedBuild:
        """Construct a complete candidate without installing it."""
        profiles = restore_profiles or self._permits.staged_profiles()
        progress = self.publish_load_progress
        if staged.restore_session is not None:
            progress = partial(progress, session_id=staged.restore_session.session_id)
        return await construction.build_agent(
            self._profile_with_mcp_overlay(profile),
            staged=staged,
            preserved_history=preserved_history,
            build_agent_fn=self._build_agent_fn,
            session=self._session,
            persistence=self._persistence,
            bus=self._bus,
            agent_registry=profiles.agents if profiles is not None else self._agent_registry,
            model_registry=profiles.models if profiles is not None else self._model_registry,
            workspace_change_tracker=self._workspace_change_tracker,
            turn_state=self._turn_state,
            turn_context=self._turn_context,
            mcp_cache=self._mcp_cache,
            allow_user_interaction=self._allow_user_interaction,
            checkpoints=self._checkpoints,
            usage_publisher=self._usage_publisher,
            fire_pre_compact=self._hooks.fire_pre_compact,
            publish_load_progress=progress,
            hold_injection_notification=self.hold_injection_notification,
        )

    def _workspace_cwd(self) -> str:
        """Return the engine workspace cwd, falling back only for legacy unstarted engines."""
        if self._session.workspace is not None:
            return self._session.workspace.primary_cwd
        return safe_getcwd()

    def trajectory_session_start_info(self) -> SessionStartInfo:
        """Resolved lazily, at the recorder's first event — after the build set the fingerprints."""
        return SessionStartInfo(
            primary_cwd=_workspace_primary_cwd(self._session.workspace),
            agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
            model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
        )

    async def build_hook_manager(
        self, *, workdir: str, workdir_hooks_enabled: bool, session_id: str | None = None
    ) -> HookManager | None:
        resolved_session_id = session_id or self._session.session_id
        manager = await SessionHookFactory(self._bus, confirmations=self._hook_confirmations)(
            workdir=workdir,
            workdir_hooks_enabled=workdir_hooks_enabled,
            session_id=resolved_session_id,
        )
        # Every main-session manager reports to the TUI; workflow managers are
        # built by their own factory and stay unreported.
        if manager is not None:
            wire_hook_reporting(manager, self._bus, session_id=resolved_session_id or "", run_log=self._hook_runs)
        return manager

    def _reloaded_hook_manager(self, hooks: StrictHooks) -> HookManager | None:
        """The manager a manual reload runs on: the live one unless a complete read says otherwise.

        Keeping the same manager is what lets running asynchronous hooks and
        in-flight checks finish: retiring one interrupts them.
        """
        old = self._session.hook_manager
        # With nothing running (a failed first build) no rule is at stake, and what could be read still applies.
        if (hooks.problems and old is not None) or hooks_unchanged(old, hooks.merged):
            return old
        factory = SessionHookFactory(self._bus, confirmations=self._hook_confirmations)
        manager = factory.manager_for(hooks.merged)
        if manager is not None:
            manager.trajectory_context_provider = self._trajectory_recorder.context
            wire_hook_reporting(manager, self._bus, session_id=self._session.session_id or "", run_log=self._hook_runs)
        return manager

    async def _commit_mcp_refresh(self) -> None:
        """Settle a manual reload's connections; a cancelled waiter never leaves them half settled."""
        resources = self._permits.staged_reload_resources()
        if resources is None or not self._mcp_cache.refreshing:
            return

        async def commit(into: ReloadResources) -> None:
            into.mcp = await self._mcp_cache.commit_refresh()

        await finish_close(asyncio.create_task(commit(resources)))

    async def _settle_failed_build(
        self, *, old: HookManager | None, staged: HookManager | None, retire_staged_hooks: bool = True
    ) -> None:
        """Drop what a failed build opened, leaving the connections and hooks the live runtime holds."""
        try:
            if self._mcp_cache.refreshing:
                await finish_close(asyncio.create_task(self._mcp_cache.abort_refresh()))
        finally:
            try:
                if retire_staged_hooks:
                    await self.settle_staged_hook_manager(old=old, staged=staged)
            finally:
                self._permits.finish_agent_load()

    def start_outbox_recovery(self) -> None:
        """Start durable outbox recovery for the current hook manager if needed."""
        if self._session.hook_manager is None or self._session.outbox_recovery_task is not None:
            return
        # Background-recover the outbox only after the session guard is held.
        # That prevents a failed/conflicting startup from spawning work, and
        # shutdown explicitly observes/cancels this task before draining hooks.
        self._session.outbox_recovery_task = asyncio.create_task(self._session.hook_manager.recover_outbox())

    async def cancel_outbox_recovery(self) -> None:
        """Observe or cancel the active outbox recovery task before replacing managers."""
        task = self._session.outbox_recovery_task
        if task is None:
            return
        if task.done():
            try:
                task.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Outbox recovery task failed")
        else:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._session.outbox_recovery_task = None

    async def commit_hook_manager_replacement(
        self,
        old_hook_manager: HookManager | None,
    ) -> None:
        """Retire the old hook manager after a replacement executor is installed."""
        await self.cancel_outbox_recovery()
        await _close_replaced_hook_manager(old_hook_manager)
        # A manual reload replaces a manager this process is still running: its durable jobs
        # are in flight, and recovery would take them for a dead process's and start them again.
        if old_hook_manager is None or self._permits.staged_reload_resources() is None:
            self.start_outbox_recovery()

    async def settle_staged_hook_manager(
        self,
        *,
        old: HookManager | None,
        staged: HookManager | None,
    ) -> None:
        """After a failed build, retire whichever hook manager lost.

        Pre-commit failure: the staged candidate never went live — drain it and
        leave the old manager, its outbox and its recovery task untouched.
        Post-commit failure: the candidate is installed and the build it belongs
        to is what the engine keeps, so retire the old manager exactly as the
        success path would have.
        """
        if staged is old:
            return
        if self._session.hook_manager is staged:
            await self.commit_hook_manager_replacement(old)
        else:
            await _close_replaced_hook_manager(staged)

    async def load(
        self,
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        workspace_plan: WorkspacePlan,
        hook_manager: HookManager | None,
        trajectory: SessionTrajectory,
        old_hook_manager: HookManager | None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Build and install the agent from a profile.

        ``staged_loaded`` is a settings candidate the build should be configured
        from instead of the live handle — a settings reload or workspace change
        retrying after a failed first build passes the re-derived load here, and
        it goes live only when the build commits; ``session`` is the same for the
        session's own choices (:meth:`stage`). ``workspace`` is the same thing
        for the workspace: a no-executor workspace change passes the new root here
        rather than mutating the live one, so a failed build keeps the workspace
        the live settings and hooks still describe.
        """
        try:
            await self.begin_load(profile, operation=operation, session_id=self._session.session_id)

            staged = self.stage(
                loaded=staged_loaded,
                session=session,
                agent_profile=profile,
                workspace_plan=workspace_plan,
                hook_manager=hook_manager,
                workspace_transaction=commit_workspace is not None,
            )
            if self._permits.staged_reload_resources() is not None:
                self._mcp_cache.begin_refresh()
            owner = self._permits.capture_control_token()
            completed = await self.build(profile, staged)
            announce = operation == "workspace_change" and (
                self._session.workspace_id is not None or staged.workspace_plan.workspace_id is not None
            )
            replaced = await self._install_candidate(completed, owner=owner, commit_workspace=commit_workspace)
            await self._finish_workspace_install(
                replaced, announce=announce, commit=commit_workspace if operation != "restore" else None
            )
        except Exception as exc:
            await self._settle_failed_build(old=old_hook_manager, staged=hook_manager)
            report_error = commit_workspace is None or commit_workspace.report_error
            if not report_error or operation != "workspace_change" or not isinstance(exc, StaleWorkspacePlan):
                await self.publish_load_failed(operation=operation, profile=profile, exc=exc, report_error=report_error)
            raise
        except BaseException:
            await self._settle_failed_build(old=old_hook_manager, staged=hook_manager)
            raise

        await self.finish_load(profile, operation=operation, trajectory=trajectory, old_hook_manager=old_hook_manager)

    async def finish_load(
        self,
        profile: AgentProfile,
        *,
        operation: str,
        trajectory: SessionTrajectory,
        old_hook_manager: HookManager | None = None,
    ) -> None:
        """Publish readiness for an installed initial build."""
        self._fsm.try_transition(Trigger.START)
        self._register_current_engine()
        # The build committed, so the profiles this session opened with are the
        # ones it keeps: pin them before a switch can rewrite what the recorder
        # reads at its first event.
        trajectory.pin_session_start_info()

        if self._session.hook_manager is not old_hook_manager:
            await self.commit_hook_manager_replacement(old_hook_manager)
        else:
            self.start_outbox_recovery()

        if operation == "restore" and not self._session.suppress_save:
            store = self._persistence.state_store
            await record_workspace_use(store.workspace_registry if store is not None else None, self._session.workspace)

        self._permits.finish_agent_load()
        await self._bus.publish(
            AgentLoadFinished(
                operation=operation,
                agent_profile=profile.name,
                display_name=profile.display_name or profile.name,
                session_id=self._session.session_id,
            ),
        )

        sub_agent_tool_names = (
            self._current.loaded.sub_agent_tools.tool_names()
            if self._current.loaded is not None and self._current.loaded.sub_agent_tools
            else []
        )
        await self._bus.publish(
            SessionReady(
                workspace=self._session.session_workspace,
                agent_profile=profile.name,
                display_name=profile.display_name,
                model_profile_id=self._current.manifest.active_profile.id
                if self._current.manifest.active_profile
                else "",
                max_context_tokens=self._current.manifest.active_profile.max_context_tokens
                if self._current.manifest.active_profile
                else 0,
                session_id=self._session.session_id,
                tool_names=list(self._current.manifest.tool_names),
                tool_kinds=dict(self._current.manifest.tool_kinds),
                skill_names=list(self._current.manifest.skill_names),
                sub_agent_tool_names=sub_agent_tool_names,
                memory_files=list(self._current.manifest.memory_files),
                runtime_details=copy.deepcopy(self._current.manifest.runtime_details),
                primary_cwd=self._workspace_cwd(),
                working_dirs=[directory.path for directory in session_workdirs(self._session.workspace)],
            ),
        )
        await self._bus.publish(
            ApprovalModeUpdated(mode=self._session.approval_mode.value, session_id=self._session.session_id)
        )

        # Fire ``session_start`` hooks after the engine is fully ready and
        # the UI has its ``SessionReady`` event.  Hooks are observers here:
        # they cannot deny startup or mutate engine state, although a
        # blocking hook still runs inline at this post-ready boundary.
        # Profile-switch/rebuild operations skip this so users don't see a
        # hook fire every time the same session reloads infrastructure.
        # A restore does not fire it: ``session_restored`` already tells a hook
        # the session came back, and firing this too would change what an
        # existing ``session_start`` hook sees on a restore, including a
        # rollback to an earlier turn.
        if self._session.hook_manager is not None and fires_session_start(operation):
            from chrys.service.hooks.events import HookEvent

            await self._session.hook_manager.fire(
                HookEvent.SESSION_START,
                {"session_id": self._session.session_id, "profile": profile.name, "cwd": self._workspace_cwd()},
                scope="session",
            )

    async def publish_load_progress(
        self,
        *,
        phase: str,
        message: str,
        server_name: str = "",
        current: int = 0,
        total: int = 0,
        failed: int = 0,
        status: str = "",
        subject: str = "",
        detail: str = "",
        stage: str = "",
        stage_subject: str = "",
        stage_status: str = "",
        session_id: str | None = None,
    ) -> None:
        """Publish an agent load progress event."""
        await self._bus.publish(
            AgentLoadProgress(
                phase=phase,
                message=message,
                server_name=server_name,
                current=current,
                total=total,
                failed=failed,
                status=status,
                subject=subject,
                detail=detail,
                stage=stage,
                stage_subject=stage_subject,
                stage_status=stage_status,
                session_id=session_id if session_id is not None else self._session.session_id,
            ),
        )

    async def begin_load(self, profile: AgentProfile, *, operation: str, session_id: str | None) -> None:
        self._permits.begin_agent_load()
        await self._bus.publish(
            AgentLoadStarted(
                operation=operation,
                to_profile=profile.name,
                to_display_name=profile.display_name or profile.name,
                session_id=session_id,
            )
        )

    async def publish_load_failed(
        self,
        *,
        operation: str,
        profile: AgentProfile,
        exc: Exception,
        session_id: str | None = None,
        report_error: bool = True,
    ) -> None:
        """Publish an agent load failure event."""
        display_message, display_hint = (
            (exc.display, exc.display_hint) if isinstance(exc, ConfigError) else display_fields(exc)
        )
        await self._bus.publish(
            AgentLoadFailed(
                operation=operation,
                agent_profile=profile.name,
                display_name=profile.display_name or profile.name,
                message=clean_error_message(exc),
                session_id=session_id if session_id is not None else self._session.session_id,
                display_message=display_message,
                display_hint=display_hint,
                report_error=report_error,
            ),
        )

    async def reload(
        self,
        new_profile: AgentProfile,
        workspace_plan: WorkspacePlan | None = None,
        *,
        operation: str = "switch",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
        commit_workspace: MembershipWrite | None = None,
    ) -> None:
        """Restart the agent with a new profile/workspace while preserving history.

        ``staged_loaded`` is a settings candidate the build should be configured
        from instead of the live handle — a settings reload or workspace change
        passes the re-derived load here, and it goes live only when the build
        commits; ``session`` is the same for the session's own choices
        (:meth:`stage`), which a shortcut's switch, a ``$`` pick or a deleted
        model change. A pre-commit failure therefore leaves the settings the previous
        executor was built from untouched; a post-commit failure keeps the new
        ones, because they are what the installed executor runs with.
        """
        old_profile_name = self._session.agent_profile.name if self._session.agent_profile else ""
        old_display_name = self._session.agent_profile.display_name if self._session.agent_profile else ""
        old_model_profile = self._current.manifest.active_profile
        old_model_base_url = self._current.manifest.runtime_details.model.base_url
        old_agent_profile_fingerprint = self._current.manifest.agent_profile_fingerprint
        old_model_profile_fingerprint = self._current.manifest.model_profile_fingerprint
        old_service_session_id = (
            self._current.loaded.bindings.backend.service_session_id if self._current.loaded is not None else ""
        )
        old_service_session_storage_enabled = (
            self._current.loaded.bindings.backend.service_session_storage_enabled
            if self._current.loaded is not None
            else False
        )
        old_workspace = self._session.workspace
        target_plan = workspace_plan if workspace_plan is not None else self._session.workspace_plan
        if target_plan is None:
            target_plan = SessionWorkspaces.plan(workspace=Workspace.from_cwd(), snapshot=RegistrySnapshot())
        workspace = workspace_plan.workspace if workspace_plan is not None else None
        announce_workspace = workspace is not None and (
            self._session.workspace_id is not None or target_plan.workspace_id is not None
        )
        old_hook_manager = self._session.hook_manager
        staged_hook_manager = old_hook_manager
        new_loaded = staged_loaded if staged_loaded is not None else self._settings_handle.base
        resources = self._permits.staged_reload_resources()
        # Working directory hooks are primary-cwd scoped and gated by ``workdir.hooks_enabled``:
        # a new primary cwd, settings loaded for another directory or a flipped gate rebuilds the manager for the root
        # the build is about to install (the live one when the workspace stays).
        reload_hook_manager = (
            (workspace is not None and _workspace_primary_cwd(old_workspace) != _workspace_primary_cwd(workspace))
            or new_loaded.settings.workdir_hooks_enabled != self._settings_handle.base.settings.workdir_hooks_enabled
            or self._settings_moved(staged_loaded)
        )
        hook_manager_root = _workspace_primary_cwd(workspace if workspace is not None else old_workspace)
        hook_manager_replacement_committed = False

        preserved_state: dict | None = None
        self._permits.begin_agent_load()
        try:
            await self._bus.publish(
                AgentLoadStarted(
                    operation=operation,
                    from_profile=old_profile_name,
                    to_profile=new_profile.name,
                    from_display_name=old_display_name or old_profile_name,
                    to_display_name=new_profile.display_name or new_profile.name,
                    session_id=self._session.session_id,
                ),
            )

            if self._current.loaded is not None:
                raw = self._current.loaded.bindings.backend.history_state
                preserved_state = _preserved_history_state(raw)
                if self._current.loaded is not None:
                    catalog_pointer_record_count = (
                        self._current.loaded.reminder_middleware.sources.archive_pointer.record_count_state()
                    )
                    if catalog_pointer_record_count is not None:
                        preserved_state = preserved_state or {}
                        preserved_state[CATALOG_POINTER_RECORD_COUNT_STATE_KEY] = catalog_pointer_record_count

            old_pending_switch = (
                self._current.loaded.reminder_middleware.sources.profile_switch.snapshot_pending_switch()
                if self._current.loaded is not None
                else None
            )
            is_consecutive = old_pending_switch is not None

            if preserved_state is not None and new_profile.name != old_profile_name:
                switches = preserved_state.setdefault("agent_profile_switches", [])
                new_label = new_profile.display_name or new_profile.name
                if is_consecutive and switches:
                    switches[-1]["to"] = new_profile.name
                    switches[-1]["to_display"] = new_label
                    switches[-1]["timestamp"] = datetime.now(UTC).isoformat()
                    if switches[-1]["from"] == new_profile.name:
                        switches.pop()
                else:
                    switches.append(
                        {
                            "from": old_profile_name,
                            "to": new_profile.name,
                            "from_display": old_display_name or old_profile_name,
                            "to_display": new_label,
                            "at_message_index": len(preserved_state.get("messages", [])),
                            "timestamp": datetime.now(UTC).isoformat(),
                        },
                    )

            if resources is not None:
                staged_hook_manager = self._reloaded_hook_manager(resources.hooks)
                self._mcp_cache.begin_refresh()
            elif reload_hook_manager:
                staged_hook_manager = await self.build_hook_manager(
                    workdir=hook_manager_root,
                    workdir_hooks_enabled=new_loaded.settings.workdir_hooks_enabled,
                )

            staged = self.stage(
                loaded=staged_loaded,
                session=session,
                agent_profile=new_profile,
                workspace_plan=target_plan,
                hook_manager=staged_hook_manager,
                workspace_transaction=commit_workspace is not None,
            )
            # The commit inside ``_build_agent`` records ``new_profile`` with the
            # executor it configured — and installs ``preserved_state`` into that
            # executor in the same synchronous block. Doing either here, after the
            # awaited post-commit steps, would let a cancellation strand the new
            # executor under the old profile's name or publish it with an empty
            # history for the next save to persist.
            owner = self._permits.capture_control_token()
            completed = await self.build(
                new_profile,
                staged,
                preserved_history=preserved_state,
            )
            replaced = await self._install_candidate(completed, owner=owner, commit_workspace=commit_workspace)
            await self._finish_workspace_install(replaced, announce=announce_workspace, commit=commit_workspace)
            if staged_hook_manager is not old_hook_manager:
                await self.commit_hook_manager_replacement(old_hook_manager)
                hook_manager_replacement_committed = True
            if old_service_session_id and self._current.loaded is not None:
                if _can_reuse_responses_service_session(
                    old_agent_profile_fingerprint=old_agent_profile_fingerprint,
                    new_agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
                    old_model_profile_fingerprint=old_model_profile_fingerprint,
                    new_model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
                    old_model_profile=old_model_profile,
                    new_model_profile=self._current.manifest.active_profile,
                    old_model_base_url=old_model_base_url,
                    new_model_base_url=self._current.manifest.runtime_details.model.base_url,
                    old_workspace=old_workspace,
                    new_workspace=self._session.workspace,
                    old_storage_enabled=old_service_session_storage_enabled,
                    new_storage_enabled=self._current.loaded.bindings.backend.service_session_storage_enabled,
                ):
                    self._current.loaded.bindings.backend.service_session_id = old_service_session_id
                else:
                    self._current.loaded.bindings.backend.service_session_id = ""
                    await self._bus.publish(
                        Warning(
                            code="service_session_incompatible",
                            message=(
                                "The previous OpenAI Responses service session is not compatible with "
                                "the active agent profile, workspace, model profile, service endpoint, "
                                "or storage is disabled. "
                                f"{APP_DISPLAY_NAME} will continue from local history only."
                            ),
                            display_message=_CONSTRUCTION_SERVICE_SESSION_INCOMPATIBLE.bind(
                                app_name=APP_DISPLAY_NAME,
                            ),
                            session_id=self._session.session_id,
                        )
                    )
            if new_profile.name != old_profile_name and self._current.loaded is not None:
                new_label = new_profile.display_name or new_profile.name
                set_profile_switch = self._current.loaded.reminder_middleware.sources.profile_switch.set_profile_switch
                if is_consecutive and old_pending_switch is not None:
                    if old_pending_switch["from"] != new_label:
                        set_profile_switch(old_pending_switch["from"], new_label)
                else:
                    old_label = old_display_name or old_profile_name
                    set_profile_switch(old_label, new_label)

            if preserved_state is not None:
                last_usage = preserved_state.get("last_usage")
                if isinstance(last_usage, dict):
                    preserved_meta = SessionRuntimeMetadata.from_state_dict(preserved_state)
                    self._session.runtime_meta.last_usage_details = preserved_meta.last_usage_details
                    # Route through the ordered chain so a sub-agent UsageUpdate
                    # still queued from the prior build can't overtake this
                    # post-restart snapshot.
                    self._usage_publisher.enqueue_usage_event(self._usage_publisher.make_usage_event())
        except Exception as exc:
            # Nothing was staged onto the engine, so there is nothing to roll
            # back: a pre-commit failure left every live field untouched, and a
            # post-commit failure keeps the committed build. Only the losing
            # hook manager still needs retiring.
            await self._settle_failed_build(
                old=old_hook_manager,
                staged=staged_hook_manager,
                retire_staged_hooks=not hook_manager_replacement_committed,
            )
            report_error = commit_workspace is None or commit_workspace.report_error
            if not report_error or operation != "workspace_change" or not isinstance(exc, StaleWorkspacePlan):
                await self.publish_load_failed(
                    operation=operation, profile=new_profile, exc=exc, report_error=report_error
                )
            raise
        except BaseException:
            await self._settle_failed_build(
                old=old_hook_manager,
                staged=staged_hook_manager,
                retire_staged_hooks=not hook_manager_replacement_committed,
            )
            raise

        message_count = len(preserved_state.get("messages", [])) if preserved_state else 0
        switched_sub_agent_names = (
            self._current.loaded.sub_agent_tools.tool_names()
            if self._current.loaded is not None and self._current.loaded.sub_agent_tools
            else []
        )
        self._permits.finish_agent_load()
        await self._bus.publish(
            AgentLoadFinished(
                operation=operation,
                agent_profile=new_profile.name,
                display_name=new_profile.display_name or new_profile.name,
                session_id=self._session.session_id,
            ),
        )
        await self._bus.publish(
            ProfileSwitched(
                from_profile=old_profile_name,
                to_profile=new_profile.name,
                from_display_name=old_display_name or old_profile_name,
                to_display_name=new_profile.display_name or new_profile.name,
                message_count=message_count,
                model_profile_id=self._current.manifest.active_profile.id
                if self._current.manifest.active_profile
                else "",
                max_context_tokens=self._current.manifest.active_profile.max_context_tokens
                if self._current.manifest.active_profile
                else 0,
                session_id=self._session.session_id,
                tool_names=list(self._current.manifest.tool_names),
                skill_names=list(self._current.manifest.skill_names),
                sub_agent_tool_names=switched_sub_agent_names,
                memory_files=list(self._current.manifest.memory_files),
                runtime_details=copy.deepcopy(self._current.manifest.runtime_details),
            ),
        )
        await self._trajectory_recorder.profile_switched(
            kind=ProfileKind.AGENT,
            from_fingerprint=old_agent_profile_fingerprint,
            to_fingerprint=self._current.manifest.agent_profile_fingerprint,
        )
        await self._trajectory_recorder.profile_switched(
            kind=ProfileKind.MODEL,
            from_fingerprint=old_model_profile_fingerprint,
            to_fingerprint=self._current.manifest.model_profile_fingerprint,
        )
        if workspace is not None and not announce_workspace:
            await self._publish_workspace_update()

    async def _publish_workspace_update(self) -> None:
        await self._bus.publish(
            workspace_updated_event(self._session, session_id=self._session.session_id),
        )

    async def _finish_workspace_install(
        self, replaced: ReplacedBuild, *, announce: bool, commit: MembershipWrite | None
    ) -> None:
        try:
            try:
                await self._commit_mcp_refresh()
            finally:
                await self.release(replaced)
        finally:
            try:
                # Recording the use stays cancellable, so exit never waits on a primary that stopped answering;
                # a finish that is already being cancelled does not start the read at all.
                task = asyncio.current_task()
                if commit is not None and (task is None or not task.cancelling()):
                    await commit.record_use()
            finally:
                # The sidebar reads the use time in the same update that changes its highlight.
                if announce:
                    await finish_close(asyncio.create_task(self._publish_workspace_update()))

    async def stage_restore_profiles(self) -> ProfileReload:
        """Capture the disk inputs before a strict restore builds from them."""
        profiles = ProfileReload(self._agent_registry, self._model_registry)
        await asyncio.to_thread(profiles.scan)
        profiles.check_versions()
        return profiles

    @staticmethod
    async def normalize_candidate_profiles(profiles: ProfileReload) -> None:
        profiles.check_versions()
        await finish_close(asyncio.create_task(asyncio.to_thread(profiles.normalize)))

    def _install_candidate_profiles(self, profiles: ProfileReload, launch_agent: str) -> None:
        self._session.new_session_agent = launch_agent
        profiles.install()

    async def _install_candidate(
        self,
        completed: CompletedBuild,
        *,
        owner: RebuildControlToken | None = None,
        commit_workspace: MembershipWrite | None = None,
        restore_profiles: ProfileReload | None = None,
    ) -> ReplacedBuild:
        """Install a candidate, or a restore already validated before the session transition."""
        profiles = restore_profiles if restore_profiles is not None else self._permits.staged_profiles()
        profiles_before_install = commit_workspace is not None or restore_profiles is not None
        launch_agent = None

        def validate_candidate() -> str | None:
            nonlocal launch_agent
            if profiles is not None:
                profiles.check_versions()
                launch_agent = profiles.updated_launch_agent(
                    self._session.new_session_agent, completed.settings.effective.settings.default_agent
                )
            if restore_profiles is None and (
                commit_workspace is not None or completed.workspace_retarget.source_generation is not None
            ):
                if self._session.shutting_down or self._permits.capture_control_token() != owner:
                    raise RuntimeError(
                        "Cannot install agent build because the active session changed or the build was superseded."
                    )
                if not self._workspace_change_tracker.retarget_is_current(completed.workspace_retarget):
                    raise StaleWorkspacePlan("Workspace changes changed during preparation.", WORKSPACE_CHANGED.bind())
            if commit_workspace is None:
                return None
            self._settings_handle.check_prepared(completed.settings)
            session_id = self._session.membership_session_id
            if session_id is None:
                raise RuntimeError("Saving workspace membership requires a session ID.")
            return session_id

        try:
            if profiles is not None and restore_profiles is None:
                await self.normalize_candidate_profiles(profiles)
            if commit_workspace is not None and not commit_workspace.saved:
                await commit_workspace.save_async(validate_candidate)
            else:
                validate_candidate()
            if profiles_before_install and profiles is not None and launch_agent is not None:
                self._install_candidate_profiles(profiles, launch_agent)
            replaced = self.install(completed)
        except BaseException as exc:
            if restore_profiles is None and self._current.loaded is not completed.loaded:
                if isinstance(exc, ConfigError) and profiles is not None:
                    exc.display_hint = profiles.changes_hint()
                await self.discard_candidate(completed)
            raise
        if not profiles_before_install and profiles is not None and launch_agent is not None:
            self._install_candidate_profiles(profiles, launch_agent)
        return replaced

    async def discard_candidate(self, completed: CompletedBuild) -> None:
        coordinator = completed.staged.mutation_coordinator
        await self.release(
            ReplacedBuild(
                loaded=completed.loaded,
                coordinator=coordinator if coordinator is not self._session.mutation_coordinator else None,
            )
        )

    def install(self, completed: CompletedBuild) -> ReplacedBuild:
        """Install the completed candidate before releasing the previous owner."""
        old_loaded = self._current.loaded
        old_coordinator = self._session.mutation_coordinator
        staged = completed.staged
        # The commit: everything the build was configured from goes live in one
        # synchronous block together with the executor it produced — no await
        # between the first assignment and the last, so a concurrent reader sees
        # the old build entirely or the new one entirely, never a mixture.
        self._settings_handle.install_prepared(completed.settings)
        self._session.install_build(
            staged, mutation_tracker=completed.mutation_tracker, todo_tracker=completed.todo_tracker
        )
        self._current.loaded = completed.loaded
        self._current.manifest = completed.manifest
        self._history.bind(completed.loaded.bindings.backend.history_state)
        self._workspace_change_tracker.apply_retarget(completed.workspace_retarget)
        self._permits.advance_build_generation()
        return ReplacedBuild(
            loaded=old_loaded,
            coordinator=old_coordinator if old_coordinator is not staged.mutation_coordinator else None,
        )

    async def release(self, replaced: ReplacedBuild) -> None:
        old_prepared = replaced.loaded.prepared if replaced.loaded is not None else None
        old_coordinator = replaced.coordinator
        cancelled: asyncio.CancelledError | None = None
        if old_coordinator is not None:
            # Settings turned coordination off (or the session re-targeted): the
            # replaced registry file is stamped closed only now that the build is
            # committed, so peers keep warning about us until we truly stop.
            try:
                await construction._close_coordinator(old_coordinator, reason="replaced")
            except asyncio.CancelledError as exc:
                cancelled = exc
        try:
            await self._cleanup_replaced_build_resources(old_prepared)
        except asyncio.CancelledError as exc:
            cancelled = exc
        if cancelled is not None:
            raise cancelled

    def apply_skill_refresh(
        self, *, skill_names: list[str], skill_sources: dict[str, list[str]], skill_details: list[RuntimeSkillDetails]
    ) -> AgentManifest:
        """Install a new manifest containing the refreshed skill catalog."""
        manifest = self._current.manifest.with_skill_refresh(
            skill_names=skill_names, skill_sources=skill_sources, skill_details=skill_details
        )
        self._current.manifest = manifest
        return manifest

    def hold_injection_notification(self, task: asyncio.Task[None]) -> None:
        """Hold the latest detached injection delivery task."""
        self._injection_notify_tasks.add(task)
        task.add_done_callback(self._injection_notify_tasks.discard)

    def _profile_with_mcp_overlay(self, profile: AgentProfile) -> AgentProfile:
        """Return a per-session profile copy with ephemeral MCP servers appended."""
        if not self._mcp_overlay:
            return profile
        effective = copy.deepcopy(profile)
        effective.tools.mcp.extend(copy.deepcopy(self._mcp_overlay))
        return effective

    async def _cleanup_replaced_build_resources(self, old_prepared: PreparedAgent | None) -> None:
        """Drain the displaced owner; borrowed engine fields have no close authority.

        Prepared keeps its cleanup task reachable and completes every release even
        if this post-install waiter is cancelled repeatedly.
        """
        if old_prepared is not None and old_prepared is not (
            self._current.loaded.prepared if self._current.loaded is not None else None
        ):
            await old_prepared.aclose()

    async def release_current(self) -> None:
        """Close the installed owner and then clear the live resource slot."""
        loaded = self._current.loaded
        if loaded is not None:
            await loaded.aclose()
            self._current.loaded = None

    async def close(self) -> None:
        """Close and replace the engine-level MCP connection cache."""
        await self._mcp_cache.close_all()
        self._mcp_cache = MCPConnectionCache()
