# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-internal companion module for ``AgentEngine`` build and restart orchestration."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from chrys.foundation.branding import APP_DISPLAY_NAME
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
    WorkspaceUpdated,
)
from chrys.foundation.i18n import msg
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.trajectory.event_types import ProfileKind
from chrys.orchestration.engine.build import construction
from chrys.orchestration.engine.build.construction import BuildAgentFn, StagedBuild
from chrys.orchestration.engine.build.loaded import AgentManifest, CompletedBuild, ReplacedBuild
from chrys.orchestration.engine.hook_reporting import wire_hook_reporting
from chrys.orchestration.engine.state.machine import Trigger
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.orchestration.invoker.resources import PreparedAgent
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.service.agent_middleware.reminders.archive_pointer import CATALOG_POINTER_RECORD_COUNT_STATE_KEY
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.trajectory.session import SessionStartInfo

if TYPE_CHECKING:
    from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import RuntimeSkillDetails
    from chrys.orchestration.engine.run.turn_hooks import TurnHookDispatcher
    from chrys.orchestration.engine.run.turn_state import TurnRuntimeState
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.engine.state.machine import EngineStateMachine
    from chrys.orchestration.engine.state.session_writer import RecoveryCheckpoints
    from chrys.orchestration.engine.usage import UsagePublisher
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


def _workspace_signature(workspace: Workspace | None) -> tuple[str, tuple[str, ...]]:
    """Return the session-relevant workspace identity for service-session reuse."""
    if workspace is None:
        return ("", ())
    return (workspace.primary_cwd, tuple(working_dir.path for working_dir in workspace.working_dirs))


def _workspace_primary_cwd(workspace: Workspace | None) -> str:
    """Return the workspace primary cwd used for project-scoped config."""
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
        workspace: Workspace | None,
        hook_manager: HookManager | None,
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
            workspace=workspace,
            hook_manager=hook_manager,
        )

    async def stage_hook_manager(
        self,
        *,
        operation: str,
        staged_loaded: LoadedSettings | None,
        workspace: Workspace | None,
    ) -> HookManager | None:
        """Construct the hook candidate when the caller's replacement decision requires it."""
        old_hook_manager = self._session.hook_manager
        hook_manager = old_hook_manager
        if old_hook_manager is None or operation == "workspace_change":
            # Project hooks are primary-cwd scoped, so the candidate is built for
            # the root the staged workspace would install, not the live one.
            build_settings = (staged_loaded if staged_loaded is not None else self._settings_handle.base).settings
            hook_manager = await self.build_hook_manager(
                project_root=_workspace_primary_cwd(workspace),
                project_hooks_enabled=build_settings.project_hooks_enabled,
            )
        return hook_manager

    async def build(
        self, profile: AgentProfile, staged: StagedBuild, *, preserved_history: dict | None = None
    ) -> CompletedBuild:
        """Construct a complete candidate without installing it."""
        return await construction.build_agent(
            self._profile_with_mcp_overlay(profile),
            staged=staged,
            preserved_history=preserved_history,
            build_agent_fn=self._build_agent_fn,
            session=self._session,
            persistence=self._persistence,
            bus=self._bus,
            agent_registry=self._agent_registry,
            model_registry=self._model_registry,
            workspace_change_tracker=self._workspace_change_tracker,
            turn_state=self._turn_state,
            turn_context=self._turn_context,
            mcp_cache=self._mcp_cache,
            allow_user_interaction=self._allow_user_interaction,
            checkpoints=self._checkpoints,
            usage_publisher=self._usage_publisher,
            fire_pre_compact=self._hooks.fire_pre_compact,
            publish_load_progress=self.publish_load_progress,
            hold_injection_notification=self.hold_injection_notification,
        )

    def _workspace_cwd(self) -> str:
        """Return the engine workspace cwd, falling back only for legacy unstarted engines."""
        if self._session.workspace is not None:
            return self._session.workspace.primary_cwd
        return safe_getcwd()

    def _workspace_working_dirs(self) -> list[str]:
        """Return all workspace root paths for event payloads."""
        if self._session.workspace is None:
            return []
        return [working_dir.path for working_dir in self._session.workspace.working_dirs]

    def trajectory_session_start_info(self) -> SessionStartInfo:
        """Resolved lazily, at the recorder's first event — after the build set the fingerprints."""
        return SessionStartInfo(
            primary_cwd=_workspace_primary_cwd(self._session.workspace),
            agent_profile_fingerprint=self._current.manifest.agent_profile_fingerprint,
            model_profile_fingerprint=self._current.manifest.model_profile_fingerprint,
        )

    async def build_hook_manager(
        self, *, project_root: str, project_hooks_enabled: bool, session_id: str | None = None
    ) -> HookManager | None:
        resolved_session_id = session_id or self._session.session_id
        manager = await SessionHookFactory(self._bus, confirmations=self._hook_confirmations)(
            project_root=project_root,
            project_hooks_enabled=project_hooks_enabled,
            session_id=resolved_session_id,
        )
        # Every main-session manager reports to the TUI; workflow managers are
        # built by their own factory and stay unreported.
        if manager is not None:
            wire_hook_reporting(manager, self._bus, session_id=resolved_session_id or "", run_log=self._hook_runs)
        return manager

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
        workspace: Workspace | None,
        hook_manager: HookManager | None,
        trajectory: SessionTrajectory,
        old_hook_manager: HookManager | None,
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
        self._permits.begin_agent_load()
        try:
            await self._bus.publish(
                AgentLoadStarted(
                    operation=operation,
                    to_profile=profile.name,
                    to_display_name=profile.display_name or profile.name,
                    session_id=self._session.session_id,
                ),
            )

            staged = self.stage(
                loaded=staged_loaded,
                session=session,
                agent_profile=profile,
                workspace=workspace,
                hook_manager=hook_manager,
            )
            completed = await self.build(profile, staged)
            replaced = self.install(completed)
            await self.release(replaced)
            self._fsm.try_transition(Trigger.START)
            self._register_current_engine()
        except Exception as exc:
            await self.settle_staged_hook_manager(old=old_hook_manager, staged=hook_manager)
            self._permits.finish_agent_load()
            await self.publish_load_failed(operation=operation, profile=profile, exc=exc)
            raise
        except BaseException:
            await self.settle_staged_hook_manager(old=old_hook_manager, staged=hook_manager)
            self._permits.finish_agent_load()
            raise

        # The build committed, so the profiles this session opened with are the
        # ones it keeps: pin them before a switch can rewrite what the recorder
        # reads at its first event.
        trajectory.pin_session_start_info()

        if hook_manager is not old_hook_manager:
            await self.commit_hook_manager_replacement(old_hook_manager)
        else:
            self.start_outbox_recovery()

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
                working_dirs=self._workspace_working_dirs(),
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
        if self._session.hook_manager is not None and operation in {"startup", "new_session", "reset"}:
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
                session_id=self._session.session_id,
            ),
        )

    async def publish_load_failed(
        self,
        *,
        operation: str,
        profile: AgentProfile,
        exc: Exception,
    ) -> None:
        """Publish an agent load failure event."""
        display_message, display_hint = display_fields(exc)
        await self._bus.publish(
            AgentLoadFailed(
                operation=operation,
                agent_profile=profile.name,
                display_name=profile.display_name or profile.name,
                message=clean_error_message(exc),
                session_id=self._session.session_id,
                display_message=display_message,
                display_hint=display_hint,
            ),
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
        old_hook_manager = self._session.hook_manager
        staged_hook_manager = old_hook_manager
        new_loaded = staged_loaded if staged_loaded is not None else self._settings_handle.base
        # Project hooks are primary-cwd scoped and gated by ``project.hooks_enabled``:
        # a new primary cwd or a flipped gate rebuilds the manager for the root
        # the build is about to install (the live one when the workspace stays).
        reload_hook_manager = (
            workspace is not None and _workspace_primary_cwd(old_workspace) != _workspace_primary_cwd(workspace)
        ) or new_loaded.settings.project_hooks_enabled != self._settings_handle.base.settings.project_hooks_enabled
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

            if reload_hook_manager:
                staged_hook_manager = await self.build_hook_manager(
                    project_root=hook_manager_root,
                    project_hooks_enabled=new_loaded.settings.project_hooks_enabled,
                )

            staged = self.stage(
                loaded=staged_loaded,
                session=session,
                agent_profile=new_profile,
                workspace=workspace if workspace is not None else old_workspace,
                hook_manager=staged_hook_manager,
            )
            # The commit inside ``_build_agent`` records ``new_profile`` with the
            # executor it configured — and installs ``preserved_state`` into that
            # executor in the same synchronous block. Doing either here, after the
            # awaited post-commit steps, would let a cancellation strand the new
            # executor under the old profile's name or publish it with an empty
            # history for the next save to persist.
            completed = await self.build(new_profile, staged, preserved_history=preserved_state)
            replaced = self.install(completed)
            await self.release(replaced)
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
            if not hook_manager_replacement_committed:
                await self.settle_staged_hook_manager(old=old_hook_manager, staged=staged_hook_manager)
            self._permits.finish_agent_load()
            await self.publish_load_failed(operation=operation, profile=new_profile, exc=exc)
            raise
        except BaseException:
            if not hook_manager_replacement_committed:
                await self.settle_staged_hook_manager(old=old_hook_manager, staged=staged_hook_manager)
            self._permits.finish_agent_load()
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
        if workspace is not None:
            await self._bus.publish(
                WorkspaceUpdated(
                    primary_cwd=workspace.primary_cwd,
                    working_dirs=[d.path for d in workspace.working_dirs],
                    reference_files=workspace.reference_files,
                    session_id=self._session.session_id,
                ),
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
