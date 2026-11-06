# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-internal profile, settings, workspace, and approval controls."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.config.context import EvalContext
from chrys.foundation.config.errors import RELOAD_SESSION_CHANGED, ConfigError, ConfigValueError
from chrys.foundation.config.process_settings import reattribute_command_line, route_restart_settings
from chrys.foundation.config.runtime_pointer import ModelFileChange
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle, load_settings
from chrys.foundation.config.spec import Apply, SettingOrigin, Source, specs_by_field
from chrys.foundation.config.warnings import settings_warning_events
from chrys.foundation.events.types import (
    AgentProfileSwitch,
    ApprovalModeUpdated,
    Error,
    ModelProfileSwitched,
    ProfileSwitched,
    SessionOnlySwitch,
    SetApprovalMode,
    SetModelProfile,
    SettingsReload,
    SettingsReloaded,
    Warning,
    WorkspaceChange,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import DisplayPath, DisplaySequence, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.util.once_close import finish_close
from chrys.orchestration.engine.reload_resources import (
    ReloadResources,
    hook_trust_failure,
    hooks_kept_report,
    mcp_refresh_report,
)
from chrys.orchestration.engine.state.active_session import session_pin_overrides
from chrys.orchestration.engine.state.lifecycle_permits import RebuildControlToken, RebuildPermit, RebuildPermitDenied
from chrys.orchestration.session_hooks import read_hooks_strict
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.agents.registry import resolve_launch_profile
from chrys.service.profiles.models.resolver import (
    active_model_profile_fields,
    model_for_agent,
    resolve_for_agent,
    resolve_selectable_profile,
)
from chrys.service.profiles.models.schema import is_model_profile_selectable
from chrys.service.profiles.reload import ProfileReload

if TYPE_CHECKING:
    from chrys.foundation.events.bus import EventBus
    from chrys.orchestration.engine.session_lifecycle import SessionLifecycle
    from chrys.orchestration.engine.state.active_session import ActiveSession
    from chrys.orchestration.engine.state.current_agent import CurrentAgent
    from chrys.orchestration.engine.state.lifecycle_permits import LifecyclePermits
    from chrys.orchestration.workflows.coordinator import WorkflowCoordinator
    from chrys.service.mutations.workspace_changes import WorkspaceChangeTracker
    from chrys.service.profiles.agents.registry import AgentProfileRegistry
    from chrys.service.profiles.agents.schema import AgentProfile
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile


logger = logging.getLogger(__name__)

_RELOAD_DONE = msg(
    "controls.reload_done",
    fallback="Reloaded settings, profiles, memory, skills, hooks, MCP servers, themes and prompt shortcuts.",
)
_RELOAD_SETTINGS_ONLY = msg("controls.reload_settings_only", fallback="Reloaded settings. No agent is active.")
_RELOAD_UNKNOWN_THEME = msg("controls.reload_unknown_theme", fallback="Theme '{theme}' is unavailable in this session.")
_RELOAD_NEXT_LAUNCH = msg("controls.reload_next_launch", fallback="Applies next launch: {keys}")
_RELOAD_KEPT = msg(
    "controls.reload_kept_model",
    fallback="Kept the current model: '{model}' is unavailable. Check the model profile file and run /reload again.",
)
_PROFILE_FALLBACK = msg(
    "controls.reload_agent_fallback", fallback="Agent '{old}' is no longer available. Using '{new}'."
)
_PROFILE_RENAMED = msg("controls.reload_agent_renamed", fallback="Agent '{old}' is now named '{new}'.")
_NO_AGENT = msg("controls.reload_no_agent", fallback="No main agent is available. Kept the previous runtime.")
_MODEL_FALLBACK = msg(
    "controls.reload_model_fallback", fallback="Model '{old}' is no longer selectable. Using '{new}' as the default."
)
_UNAVAILABLE_CHILD = msg(
    "controls.reload_unavailable_child",
    fallback="Agent '{parent}' references '{child}'. Its tool '{tool}' is unavailable.",
)
_MISSING_BOUND_MODEL = msg(
    "controls.reload_missing_bound_model",
    fallback="Agent '{agent}' references missing model '{missing}'. Using '{actual}'.",
)
# What a caller's model pin puts into every load (``session_pin_overrides``).
_MODEL_PIN_KEYS = frozenset({"model_profile", "model_profile_override", "model_profile_override_sub_agents"})

_SESSION_MODEL_REMOVED = msg(
    "controls.reload_session_model_removed",
    fallback="Session model '{model}' is no longer selectable. The session now uses its default model.",
)
_RELOAD_CHANGED = msg("controls.reload_applied", fallback="Applied: {keys}")
_RELOAD_SHADOWED = msg("controls.reload_shadowed", fallback="Overridden by a higher-priority choice: {keys}")

_RELOAD_BUSY = msg("controls.reload_busy", fallback="Reload when the current turn or workflow finishes.")
_RELOAD_LOADING = msg(
    "controls.reload_loading", fallback="Wait for the agent to finish loading, then run /reload again."
)
_RELOAD_SUPERSEDED = msg(
    "controls.reload_superseded", fallback="A newer agent load took precedence. Run /reload again."
)
_RELOAD_SHUTDOWN = msg("controls.reload_shutdown", fallback="Settings cannot reload during shutdown.")
_RELOAD_UNAVAILABLE = msg("controls.reload_unavailable_model", fallback="Model profile is unavailable: {model}")
_RELOAD_DENIALS = {
    "busy": _RELOAD_BUSY,
    "load_active": _RELOAD_LOADING,
    "not_ready": _RELOAD_LOADING,
    "session_changed": RELOAD_SESSION_CHANGED,
    "superseded": _RELOAD_SUPERSEDED,
    "shutdown": _RELOAD_SHUTDOWN,
}

# These settings only decide what the next launch starts with.
_NEXT_LAUNCH_KEYS = {"agent.default_profile", "approval.default_mode"}

_CONTROLS_NO_REGISTRY = msg(
    "controls.no_registry",
    fallback="No profile registry configured — cannot switch profiles",
)
_CONTROLS_PROFILE_NOT_FOUND = msg(
    "controls.profile_not_found",
    fallback="Profile '{profile_name}' not found",
)
_CONTROLS_MODEL_PROFILE_NOT_FOUND = msg(
    "controls.model_profile_not_found",
    fallback="Model profile '{profile_id}' not found",
)
_CONTROLS_MODEL_SWITCH_NOT_READY = msg(
    "controls.model_switch_not_ready",
    fallback="No active agent — cannot switch model",
)
_CONTROLS_WORKSPACE_SWITCH_NOT_READY = msg(
    "controls.workspace_switch_not_ready",
    fallback="No active agent — cannot change workspace",
)
_CONTROLS_WORKSPACE_MISSING = msg(
    "controls.workspace_missing",
    fallback="Cannot change the working directory: {path} does not exist.",
)
_CONTROLS_SETTINGS_RESTART_REQUIRED = msg(
    "controls.settings_restart_required",
    fallback="Restart to apply these settings: {keys}.",
)

PersistApprovalModeFn = Callable[[str], Exception | None]


def _denial_allows_already_satisfied_success(denied: RebuildPermitDenied) -> bool:
    """Return whether a denial can be converted to an already-satisfied success."""
    return denied.reason == "superseded"


def _workspace_primary_matches(workspace: Workspace | None, primary_cwd: str) -> bool:
    """Return whether *primary_cwd* names the live workspace primary cwd."""
    if workspace is None:
        return False
    if workspace.primary_cwd == primary_cwd:
        return True
    return workspace.primary_cwd == Workspace.from_cwd(primary_cwd).primary_cwd


class RuntimeControls:
    """Applies profile, model, settings, workspace, and approval changes."""

    def __init__(
        self,
        *,
        session: ActiveSession,
        current: CurrentAgent,
        permits: LifecyclePermits,
        lifecycle: SessionLifecycle,
        bus: EventBus,
        settings_handle: SettingsHandle,
        agent_registry: AgentProfileRegistry | None,
        model_registry: ModelProfileRegistry | None,
        workspace_change_tracker: WorkspaceChangeTracker,
        workflows: WorkflowCoordinator,
    ) -> None:
        self._session = session
        self._current = current
        self._permits = permits
        self._lifecycle = lifecycle
        self._bus = bus
        self._settings_handle = settings_handle
        self._launch_defaults = {
            "default_agent": settings_handle.base.settings.default_agent,
            "default_approval_mode": settings_handle.base.settings.default_approval_mode,
        }
        self._agent_registry = agent_registry
        self._model_registry = model_registry
        self._workspace_change_tracker = workspace_change_tracker
        self._workflows = workflows

    def current_profile_snapshot(self) -> ProfileSwitched:
        """Build a no-op ``ProfileSwitched`` reflecting the live runtime.

        A frontend reselecting the already-active agent performs no backend
        switch and emits no event, so callers that still owe the client the
        standard runtime envelope read it here instead of fabricating blank
        fields. ``from``/``to`` are identical because nothing changed. Field
        sourcing mirrors the real switch event so the two cannot drift.
        """
        profile = self._session.agent_profile
        name = profile.name if profile else ""
        display = (profile.display_name or profile.name) if profile else ""
        messages = (
            self._current.loaded.bindings.backend.history_state.get("messages", [])
            if self._current.loaded is not None
            else []
        )
        return ProfileSwitched(
            from_profile=name,
            to_profile=name,
            from_display_name=display,
            to_display_name=display,
            message_count=len(messages),
            model_profile_id=self._current.manifest.active_profile.id if self._current.manifest.active_profile else "",
            max_context_tokens=self._current.manifest.active_profile.max_context_tokens
            if self._current.manifest.active_profile
            else 0,
            session_id=self._session.session_id,
            tool_names=list(self._current.manifest.tool_names),
            skill_names=list(self._current.manifest.skill_names),
            sub_agent_tool_names=self._current.loaded.sub_agent_tools.tool_names()
            if self._current.loaded is not None and self._current.loaded.sub_agent_tools
            else [],
            memory_files=list(self._current.manifest.memory_files),
            runtime_details=copy.deepcopy(self._current.manifest.runtime_details),
        )

    async def on_set_approval_mode(
        self,
        event: SetApprovalMode,
        *,
        persist_approval_mode_fn: PersistApprovalModeFn,
    ) -> None:
        """Update the active approval mode on the running middleware."""
        try:
            mode = ApprovalMode(event.mode)
        except ValueError:
            logger.warning("Unknown approval mode: %r", event.mode)
            return
        self._session.approval_mode = mode
        if self._current.loaded is not None:
            self._current.loaded.bindings.approval.set_approval_mode(mode)
        # Propagate to sub-agent tools so each fresh sub-agent invocation
        # constructs its ApprovalMiddleware with the live mode.
        if self._current.loaded is not None and self._current.loaded.sub_agent_tools is not None:
            self._current.loaded.sub_agent_tools.set_approval_mode(mode)
        self._workflows.set_approval_mode(mode)
        if event.persist:
            # Persist the user's choice so it survives restart.  BYPASS is
            # downgraded to AUTO inside ``persist_approval_mode`` to avoid
            # booting into unattended auto-approval on next launch — mirror
            # that same downgrade onto the in-memory Settings so a later
            # ``SettingsReload`` stays consistent with what's on disk.
            #
            # Through the overlay rather than onto the field: this key is
            # ``Risk.DANGEROUS``, so a rejected value seals it at ``manual``, and
            # an in-place write would leave that seal on top of the mode the user
            # just chose.
            try:
                self._settings_handle.persist_approval_default(mode.value, persist=persist_approval_mode_fn)
            except (OSError, ValueError) as exc:
                await self._bus.publish(
                    Warning(
                        code="approval_default_save_failed",
                        message=str(exc),
                        display_message=exc.display if isinstance(exc, ConfigError) else None,
                        session_id=self._session.session_id,
                    )
                )
        await self._bus.publish(ApprovalModeUpdated(mode=mode.value, session_id=self._session.session_id))

    async def on_profile_switch(self, event: AgentProfileSwitch) -> None:
        """Handle agent profile switch — preserves conversation history."""
        token = self._permits.capture_control_token()
        if self._agent_registry is None:
            await self._bus.publish(
                Error(
                    code="no_registry",
                    message="No profile registry configured — cannot switch profiles",
                    display_message=_CONTROLS_NO_REGISTRY.bind(),
                    session_id=self._session.session_id,
                )
            )
            return

        new_profile = self._agent_registry.get(event.profile_name)
        if new_profile is None:
            await self._bus.publish(
                Error(
                    code="profile_not_found",
                    message=f"Profile '{event.profile_name}' not found",
                    display_message=_CONTROLS_PROFILE_NOT_FOUND.bind(profile_name=event.profile_name),
                    session_id=self._session.session_id,
                )
            )
            return

        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, RebuildPermitDenied):
            if await self._publish_profile_satisfied_or_denied(event.profile_name, permit, token.session_id):
                self._record_new_session_agent(event, new_profile)
            return
        try:
            if (
                self._current.loaded is not None
                and self._session.agent_profile
                and new_profile.name == self._session.agent_profile.name
            ):
                await self._bus.publish(self._profile_switched_snapshot(permit.token.session_id))
                return
            if self._current.loaded is None:
                await self._lifecycle.start_with_rebuild_permit(permit, new_profile, operation="switch")
                await self._bus.publish(self._profile_switched_snapshot(permit.token.session_id))
                return
            await self._lifecycle.reload_with_rebuild_permit(permit, new_profile, operation="switch")
        finally:
            try:
                # Whenever the requested agent is in force, also when it already
                # was or a step after the commit failed: the pick stands.
                current = self._session.agent_profile
                if current is not None and current.name == new_profile.name:
                    self._record_new_session_agent(event, new_profile)
            finally:
                self._permits.release_rebuild_permit(permit)

    def _record_new_session_agent(self, event: AgentProfileSwitch, profile: AgentProfile) -> None:
        """Make the user's own pick the agent new sessions start on; a fallback only repairs a dead one."""
        if event.source == "user":
            self._session.new_session_agent = profile.name
            return
        registry = self._agent_registry
        kept = registry.get(self._session.new_session_agent) if registry is not None else None
        if kept is None or kept.sub_agent_only:
            self._session.new_session_agent = profile.name

    async def on_session_only_switch(self, event: SessionOnlySwitch) -> None:
        """Switch this session's own agent and/or model in one rebuild, or neither (a prompt shortcut's).

        The agent and model new sessions start on do not move: the agent is
        this session's ``agent_profile`` only, the model rides the settings
        handle's session layer, and both end when the session closes. Both
        halves are staged into one build under one permit, so a build that
        fails before its commit changes nothing. The model is measured on the
        agent the switch runs on (``model_for_agent``): one that agent would not
        run (it binds its own, or an override is set) is left out, and
        ``event.model_left_out`` says so; one it runs anyway is not asked for.
        ``event.applied`` is set before the publish returns: True when what was
        asked is in force.
        """
        agent: AgentProfile | None = None
        if event.agent:
            agent = self._agent_registry.get(event.agent) if self._agent_registry is not None else None
            if agent is None or agent.sub_agent_only:
                await self._bus.publish(
                    Error(
                        code="profile_not_found",
                        message=f"Profile '{event.agent}' not found",
                        display_message=_CONTROLS_PROFILE_NOT_FOUND.bind(profile_name=event.agent),
                        session_id=self._session.session_id,
                    )
                )
                return
        model: ModelProfile | None = None
        if event.model_profile:
            model = self._model_registry.get(event.model_profile) if self._model_registry is not None else None
            if model is None or not is_model_profile_selectable(model):
                await self._bus.publish(
                    Error(
                        code="model_profile_not_found",
                        message=f"Model profile '{event.model_profile}' not found",
                        display_message=_CONTROLS_MODEL_PROFILE_NOT_FOUND.bind(profile_id=event.model_profile),
                        session_id=self._session.session_id,
                    )
                )
                return
        current = self._session.agent_profile
        target = agent or current
        if target is None:
            await self._bus.publish(
                Error(
                    code="runtime_mutation_not_ready",
                    message="No active agent — cannot switch model",
                    display_message=_CONTROLS_MODEL_SWITCH_NOT_READY.bind(),
                    session_id=self._session.session_id,
                )
            )
            return
        if model is not None:
            settings = self._settings_handle.settings
            if model_for_agent(self._model_registry, settings, target, session_model=model.id) != model.id:
                # The target agent runs a model of its own (or an override) over the session's: asking
                # for it changes nothing now, and held in the session it would surface on a later agent.
                event.model_left_out = True
                model = None
            elif model_for_agent(self._model_registry, settings, target) == model.id:
                # It runs there anyway (the agent's own, or the session's already): nothing to ask for.
                model = None
        wants_agent = agent is not None and (current is None or agent.name != current.name)
        wants_model = model is not None and model.id != self._settings_handle.settings.model_profile
        if not wants_agent and not wants_model:
            event.applied = True
            return
        token = self._permits.capture_control_token()
        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, RebuildPermitDenied):
            await self._publish_rebuild_denied(permit, token.session_id)
            return
        generation = self._permits.build_generation
        started = self._current.loaded is None
        session = (
            {**self._settings_handle.session_values, **active_model_profile_fields(model)}
            if wants_model and model is not None
            else None
        )
        try:
            await self._start_or_restart_with_permit(
                permit, target, operation="switch" if wants_agent else "model_switch", session=session
            )
        finally:
            try:
                committed = self._permits.build_generation != generation
                if started and not committed:
                    # A start names its agent before it builds; one that failed leaves neither half behind.
                    self._session.agent_profile = current
                in_force = self._session.agent_profile
                event.applied = (
                    committed
                    and in_force is not None
                    and in_force.name == target.name
                    and (model is None or self._settings_handle.settings.model_profile == model.id)
                )
            finally:
                self._permits.release_rebuild_permit(permit)
        if started:
            await self._bus.publish(self._profile_switched_snapshot(token.session_id))
        if wants_model and model is not None:
            await self._publish_model_switched_snapshot(model.id, token.session_id)

    async def _publish_profile_satisfied_or_denied(
        self,
        profile_name: str,
        denied: RebuildPermitDenied,
        session_id: str | None,
    ) -> bool:
        if _denial_allows_already_satisfied_success(denied) and (
            self._session.agent_profile is not None and self._session.agent_profile.name == profile_name
        ):
            await self._bus.publish(self._profile_switched_snapshot(session_id))
            return True
        await self._publish_rebuild_denied(denied, session_id)
        return False

    def _profile_switched_snapshot(self, session_id: str | None) -> ProfileSwitched:
        return dataclasses.replace(self.current_profile_snapshot(), session_id=session_id)

    async def _publish_rebuild_denied(
        self,
        denied: RebuildPermitDenied,
        session_id: str | None,
    ) -> None:
        await self._bus.publish(Error(code=denied.code, message=denied.message, session_id=session_id))

    async def _publish_model_switched_snapshot(
        self,
        profile_id: str,
        session_id: str | None,
    ) -> None:
        active = self._current.manifest.active_profile
        await self._bus.publish(
            ModelProfileSwitched(
                model_profile_id=active.id if active else profile_id,
                max_context_tokens=active.max_context_tokens if active else 0,
                runtime_details=copy.deepcopy(self._current.manifest.runtime_details),
                session_id=session_id,
            )
        )

    def _pin_session_model_profile(self, profile_id: str) -> None:
        # Into the layered load, where a caller-owned pin lives (``session_pin_overrides``).
        self._settings_handle.install(
            self._settings_handle.base.overlay(
                Source.SESSION,
                model_profile=profile_id,
                model_profile_override=profile_id,
                model_profile_override_sub_agents=False,
            ),
        )
        self._session.model_profile_pinned = True

    async def _publish_workspace_updated_snapshot(
        self,
        session_id: str | None,
        *,
        primary_cwd: str | None = None,
    ) -> None:
        workspace = self._session.workspace
        resolved_primary_cwd = primary_cwd if primary_cwd is not None else (workspace.primary_cwd if workspace else "")
        await self._bus.publish(
            WorkspaceUpdated(
                primary_cwd=resolved_primary_cwd,
                working_dirs=[d.path for d in workspace.working_dirs] if workspace is not None else [],
                reference_files=list(workspace.reference_files) if workspace is not None else [],
                session_id=session_id,
            )
        )

    async def _publish_model_satisfied_or_denied(
        self,
        profile_id: str,
        denied: RebuildPermitDenied,
        session_id: str | None,
    ) -> None:
        if _denial_allows_already_satisfied_success(denied) and (
            self._current.manifest.active_profile is not None and self._current.manifest.active_profile.id == profile_id
        ):
            self._pin_session_model_profile(profile_id)
            await self._publish_model_switched_snapshot(profile_id, session_id)
            return
        await self._publish_rebuild_denied(denied, session_id)

    async def _publish_workspace_satisfied_or_denied(
        self,
        primary_cwd: str,
        denied: RebuildPermitDenied,
        session_id: str | None,
    ) -> None:
        if _denial_allows_already_satisfied_success(denied) and _workspace_primary_matches(
            self._session.workspace, primary_cwd
        ):
            await self._publish_workspace_updated_snapshot(session_id, primary_cwd=primary_cwd)
            return
        await self._publish_rebuild_denied(denied, session_id)

    async def _acquire_rebuild_permit_or_publish_error(
        self,
        token: RebuildControlToken,
        *,
        manual: bool = False,
    ) -> RebuildPermit | None:
        permit = await self._permits.acquire_rebuild_permit(token, reject_running=manual)
        if isinstance(permit, RebuildPermitDenied):
            if manual:
                await self._bus.publish(
                    Error(
                        code="manual_settings_reload_denied",
                        message=permit.message,
                        display_message=permit.display_message or _RELOAD_DENIALS[permit.reason].bind(),
                        session_id=token.session_id,
                    )
                )
            else:
                await self._publish_rebuild_denied(permit, token.session_id)
            return None
        return permit

    async def _start_or_restart_with_permit(
        self,
        permit: RebuildPermit,
        profile: AgentProfile,
        *,
        operation: str,
        workspace: Workspace | None = None,
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        if self._current.loaded is None:
            # The workspace rides as staged input, exactly like the settings: a
            # build that fails before committing must leave the live root — the
            # one the live settings and hooks were derived from — untouched.
            await self._lifecycle.start_with_rebuild_permit(
                permit, profile, operation=operation, staged_loaded=staged_loaded, session=session, workspace=workspace
            )
            return
        await self._lifecycle.reload_with_rebuild_permit(
            permit, profile, workspace=workspace, operation=operation, staged_loaded=staged_loaded, session=session
        )

    async def on_set_model_profile(self, event: SetModelProfile) -> None:
        """Switch the active model profile for this session, preserving the saved default.

        Swaps the in-memory model selector and explicit override, then rebuilds;
        the build path resolves the model from ``settings`` against the registry,
        and credentials come from the profile itself, so sessions stay isolated.
        """
        if self._session.agent_profile is None:
            await self._bus.publish(
                Error(
                    code="runtime_mutation_not_ready",
                    message="No active agent — cannot switch model",
                    display_message=_CONTROLS_MODEL_SWITCH_NOT_READY.bind(),
                    session_id=self._session.session_id,
                )
            )
            return
        token = self._permits.capture_control_token()
        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, RebuildPermitDenied):
            await self._publish_model_satisfied_or_denied(event.profile_id, permit, token.session_id)
            return

        old_loaded = self._settings_handle.base
        old_pinned = self._session.model_profile_pinned
        old_runtime = self._current.loaded

        try:
            try:
                self._pin_session_model_profile(event.profile_id)
                # The pin is the session's model: a model of the session's own would outrank it.
                await self._start_or_restart_with_permit(
                    permit, self._session.agent_profile, operation="model_switch", session={}
                )
            except BaseException:
                # The rebuild failed or was cancelled (a failure already published AgentLoadFailed).
                # While no new runtime went live, restore the pin and settings so the running
                # executor stays consistent with session state. One that did was built on the new
                # model: restoring then would name a model the executor was not built with.
                if self._current.loaded is old_runtime:
                    self._settings_handle.install(old_loaded)
                    self._session.model_profile_pinned = old_pinned
                raise
            await self._publish_model_switched_snapshot(event.profile_id, token.session_id)
        finally:
            self._permits.release_rebuild_permit(permit)

    def _session_pin_overrides(self) -> dict[str, Any]:
        """Snapshot the per-session pins every re-load must carry (:func:`session_pin_overrides`)."""
        return session_pin_overrides(self._session, self._settings_handle.base)

    def _reload_eval_context(self) -> EvalContext:
        """The launch mode's retry policy, passed *into* the load.

        An input to the load rather than an override: the project layer's
        tighten/loosen verdicts are evaluated against it, and every re-load must
        use the same one the initial load did.
        """
        return EvalContext(
            frontend_default_max_transient_retries=self._settings_handle.settings.frontend_default_max_transient_retries
        )

    def _session_project_root(self) -> Path:
        """The workspace root whose project trust domain this session lives under."""
        workspace = self._session.workspace
        return Path(workspace.primary_cwd) if workspace is not None else Path(safe_getcwd())

    async def on_settings_reload(self, event: SettingsReload) -> None:
        """Handle settings reload — reload Settings from disk and env, then rebuild.

        The session's own model rides along unless the user just chose one for
        this session and the default together (``model_chosen``, the ``$``
        pick) or its profile no longer exists: the rebuild then commits the
        session without it.
        """
        if event.manual and event.session_id != self._session.session_id:
            message = RELOAD_SESSION_CHANGED.bind()
            await self._bus.publish(
                Error(
                    code="manual_settings_reload_denied",
                    message=format_message(message),
                    display_message=message,
                    session_id=event.session_id,
                )
            )
            return
        token = self._permits.capture_control_token()
        # Taken even with nothing built yet. A host that has not built — startup
        # before the first build, a first build that failed, a host that only
        # subscribed — must still genuinely reload; echoing completion without
        # reloading would report success and then hand the first build the old
        # configuration. The permit is also what serializes the load against a
        # build, since ``start`` takes this same boundary: the values installed
        # below cannot land halfway through one.
        permit = await self._acquire_rebuild_permit_or_publish_error(token, manual=event.manual)
        if permit is None:
            return

        report: list[MessageRef] = []
        profiles = None
        resources: ReloadResources | None = None
        kept_model = False
        release_pin = False
        old_effective = self._settings_handle.loaded
        old_loaded = self._settings_handle.base
        session = (
            self._settings_handle.session_values
            if event.manual
            else self._session_choice_after_reload(model_chosen=event.model_chosen)
        )
        try:
            try:
                # Re-read settings from env (matches TUI behavior, which persists
                # changes before triggering reload), for this session's workspace
                # root — the project trust domain is root-derived, so the reload
                # must re-derive it from where the session actually lives.
                #
                # Off-thread because the load reads config files and waits on their
                # lock, and this handler runs inline on the bus: a synchronous load
                # here would stall every other event for as long as the disk takes.
                # The pins are snapshotted before the hand-off, so the load works
                # from the state that asked for it.
                pins = self._session_pin_overrides()
                if event.follow_process_model:
                    pins.pop("session_default_model", None)
                if event.manual:
                    pins["manual_reload"] = True
                    pins["session_scoped_model"] = event.session_scoped_model
                # Before the read: a choice confirmed after it may be newer than what was read.
                confirmed = self._settings_handle.confirmed_overrides() if event.manual else {}
                candidate = await asyncio.to_thread(
                    load_settings,
                    project_root=self._session_project_root(),
                    eval_context=self._reload_eval_context(),
                    **pins,
                )
                if event.manual:
                    candidate = dataclasses.replace(candidate, confirmed_overrides=confirmed)
                    profiles = ProfileReload(self._agent_registry, self._model_registry)
                    await asyncio.to_thread(profiles.scan)
                    profiles.check_versions()
                    report.extend(profiles.report)
                    own_model = self._settings_handle.session_model
                    if own_model and resolve_selectable_profile(profiles.models, own_model) is None:
                        session = {}
                        report.append(_SESSION_MODEL_REMOVED.bind(model=own_model))
                    pinned = pins.get("model_profile_override") if self._session.model_profile_pinned else None
                    if (
                        pinned
                        and profiles.models is not None
                        and resolve_selectable_profile(profiles.models, pinned) is None
                    ):
                        # A caller's pin (an ACP model switch) names a model whose file is gone:
                        # read again without it, and the pin ends with the install.
                        release_pin = True
                        unpinned = {name: value for name, value in pins.items() if name not in _MODEL_PIN_KEYS}
                        candidate = dataclasses.replace(
                            await asyncio.to_thread(
                                load_settings,
                                project_root=self._session_project_root(),
                                eval_context=self._reload_eval_context(),
                                **unpinned,
                            ),
                            confirmed_overrides=confirmed,
                        )
                        report.append(_SESSION_MODEL_REMOVED.bind(model=pinned))
                    report.append(
                        (_RELOAD_DONE if self._session.agent_profile is not None else _RELOAD_SETTINGS_ONLY).bind()
                    )
                    candidate, kept_model = self._reload_model_default(candidate, profiles, report)
                # Routed after the load, not excluded from it: the reload still has
                # to report a RESTART value the user typed wrong, it just must not
                # claim the good ones took effect. The snapshot fields keep the
                # bootstrap values their readers hold; the rest of the RESTART tier
                # is held at the values already in force, so the rebuild below
                # cannot apply what only a restart may.
                loaded, deferred_keys = route_restart_settings(
                    reattribute_command_line(candidate, old_loaded),
                    old_loaded,
                )
                # A reload is the moment a user finds out their edit did not take.
                # Startup already reports these; dropping them here would make the
                # same bad value silent from the second read onwards.
                for warning in settings_warning_events(loaded):
                    await self._bus.publish(dataclasses.replace(warning, session_id=self._session.session_id))
                if deferred_keys and not event.manual:
                    display_message = _CONTROLS_SETTINGS_RESTART_REQUIRED.bind(keys=DisplaySequence(deferred_keys))
                    await self._bus.publish(
                        Warning(
                            code="settings_restart_required",
                            message=format_message(display_message),
                            display_message=display_message,
                            session_id=self._session.session_id,
                        )
                    )

                # Pull the latest active profile instance from the registry so edits
                # saved to user YAML (e.g. MCP enabled/disabled) take effect immediately.
                profile_for_restart = self._session.agent_profile
                if profile_for_restart is not None and profiles is not None and profiles.agents is not None:
                    current = profile_for_restart
                    view = profiles.agents
                    selected = profiles.updated_agent(current)
                    if selected is None or selected not in view.list_profiles():
                        selected = resolve_launch_profile(view, loaded.settings.default_agent)
                        if selected is None:
                            raise ConfigValueError(_NO_AGENT.bind())
                        report.append(_PROFILE_FALLBACK.bind(old=current.name, new=selected.name))
                    elif selected.name != current.name:
                        report.append(_PROFILE_RENAMED.bind(old=current.name, new=selected.name))
                    previous_model = self._current.manifest.active_profile
                    if previous_model and is_model_profile_selectable(previous_model):
                        effective = self._settings_handle.prepare(loaded, session=session).effective.settings
                        resolved = resolve_for_agent(profiles.models, effective, selected)
                        # Removing just the default setting intentionally clears its pointer,
                        # even if the unconfigured builtin will then prompt for a model.
                        cleared_default = (
                            candidate.model_edit is not None
                            and not candidate.model_edit.value
                            and not effective.model_profile_override
                            and not selected.model.profile_id
                            and resolve_selectable_profile(profiles.models, previous_model.id) is not None
                        )
                        if not is_model_profile_selectable(resolved) and not cleared_default:
                            raise ConfigValueError(_RELOAD_UNAVAILABLE.bind(model=previous_model.name))
                    profile_for_restart = selected
                elif profile_for_restart is not None and self._agent_registry is not None:
                    refreshed = self._agent_registry.get(profile_for_restart.name)
                    if refreshed is not None:
                        profile_for_restart = refreshed
                    else:
                        logger.warning(
                            "Active profile '%s' missing from registry during settings reload; reusing current profile.",
                            profile_for_restart.name,
                        )
                if event.manual and profile_for_restart is not None:
                    hooks = await asyncio.to_thread(
                        read_hooks_strict,
                        project_root=str(self._session_project_root()),
                        project_hooks_enabled=loaded.settings.project_hooks_enabled,
                    )
                    if hooks.problems:
                        # Rules read under the old trust setting cannot stand in for the new
                        # one, so this reload as a whole changes nothing.
                        rules_in_force = self._session.hook_manager is not None
                        was_trusted = old_loaded.settings.project_hooks_enabled
                        if loaded.settings.project_hooks_enabled != was_trusted:
                            raise ConfigValueError(hook_trust_failure(hooks.problems, project_hooks_loaded=was_trusted))
                        report.append(hooks_kept_report(hooks.problems, rules_in_force=rules_in_force))
                    resources = ReloadResources(hooks)
            except Exception as exc:
                # A failing load (an unreadable settings file, say) aborts *before*
                # the rebuild publishes any completion event. The bus swallows
                # handler exceptions, so without an explicit failure here a caller
                # awaiting the reload would hang until its timeout. Nothing was
                # installed yet — the candidate stays staged until the rebuild
                # commits it — so surfacing the error is all there is to do.
                #
                # Individually invalid values no longer land here: they are rejected
                # by their coercer and reported as warnings, so one bad variable can
                # no longer take a whole reload down.
                await self._bus.publish(
                    Error(
                        code="manual_settings_reload_failed" if event.manual else "settings_reload_failed",
                        message=str(exc),
                        display_message=exc.display if isinstance(exc, ConfigError) else None,
                        session_id=token.session_id,
                    )
                )
                raise

            # Only the rebuild is conditional: with nothing built there is no
            # runtime to replace, so the candidate is installed directly and the
            # first build will read it. With a runtime, the candidate travels as
            # the rebuild's staged input and is committed by the build itself,
            # together with the executor it configured: a rebuild that fails
            # before installing leaves the live settings untouched, and one that
            # fails after keeps the settings its executor was actually built from
            # — restoring the old ones there would desynchronize the two.
            if profile_for_restart is not None:
                if profiles is not None:
                    self._permits.stage_profiles(permit, profiles)
                if resources is not None:
                    self._permits.stage_reload_resources(permit, resources)
                try:
                    await self._start_or_restart_with_permit(
                        permit,
                        profile_for_restart,
                        operation="settings_reload",
                        staged_loaded=loaded,
                        session=session,
                    )
                finally:
                    if release_pin and self._settings_handle.base is not old_loaded:
                        self._session.model_profile_pinned = False
                if resources is not None:
                    report.extend(mcp_refresh_report(resources.mcp))
                if (
                    old_loaded.settings.workspace_change_notice
                    and not self._settings_handle.settings.workspace_change_notice
                ):
                    # Drop the baseline only once the off-state rebuild has committed —
                    # a failed rebuild keeps the old enabled settings AND the baseline.
                    self._workspace_change_tracker.invalidate()
            else:
                try:
                    if profiles is not None:
                        await finish_close(asyncio.create_task(asyncio.to_thread(profiles.normalize)))
                        profiles.check_versions()
                    self._settings_handle.install_prepared(self._settings_handle.prepare(loaded, session=session))
                    if release_pin:
                        self._session.model_profile_pinned = False
                    if profiles is not None:
                        profiles.install()
                except ConfigError as exc:
                    await self._bus.publish(
                        Error(
                            code="manual_settings_reload_failed",
                            message=str(exc),
                            display_message=exc.display,
                            display_hint=exc.display_hint
                            or (profiles.changes_hint() if profiles is not None else None),
                            session_id=token.session_id,
                        )
                    )
                    raise
            if event.manual:
                if profile_for_restart is not None:
                    report.extend(self._profile_reference_report(profile_for_restart))
                new = self._settings_handle.loaded
                before = dataclasses.asdict(old_effective.settings) | self._launch_defaults | event.tui_projection
                after = dataclasses.asdict(new.settings)
                changed = {
                    entry.key
                    for name, entry in specs_by_field(Settings).items()
                    if before[name] != after[name] and entry.apply in (Apply.LIVE, Apply.RELOAD)
                }
                # Also when the name is unchanged: its file may just have been deleted.
                if (
                    event.available_themes is not None
                    and new.settings.theme
                    and new.settings.theme not in event.available_themes
                ):
                    changed.discard("ui.theme")
                    report.append(_RELOAD_UNKNOWN_THEME.bind(theme=new.settings.theme))
                for definition, keys in (
                    (_RELOAD_CHANGED, changed - _NEXT_LAUNCH_KEYS),
                    (_RELOAD_NEXT_LAUNCH, changed & _NEXT_LAUNCH_KEYS),
                ):
                    if keys:
                        report.append(definition.bind(keys=DisplaySequence(tuple(sorted(keys)))))
                if deferred_keys:
                    report.append(_CONTROLS_SETTINGS_RESTART_REQUIRED.bind(keys=DisplaySequence(deferred_keys)))
                upper_layers = {Source.ENV, Source.CLI, Source.PROCESS_RUNTIME, Source.SESSION, Source.RUNTIME}
                shadowed = sorted(
                    entry.key
                    for name, entry in specs_by_field(Settings).items()
                    if not (name == "model_profile" and kept_model)
                    and name in new.file_values
                    and new.file_values[name] != after[name]
                    and new.source_for(entry.key).layer in upper_layers
                )
                if shadowed:
                    report.append(_RELOAD_SHADOWED.bind(keys=DisplaySequence(tuple(shadowed))))
            await self._publish_settings_reloaded(tuple(report), manual=event.manual)
        finally:
            self._permits.release_rebuild_permit(permit)

    def _profile_reference_report(self, profile: AgentProfile) -> list[MessageRef]:
        """Describe unresolved references on the installed agent and the children it actually exposes."""
        manifest = self._current.manifest
        active = manifest.active_profile
        report: list[MessageRef] = []
        models = self._model_registry
        bound = profile.model.profile_id
        if bound and active is not None and models is not None and models.get(bound) is None:
            report.append(_MISSING_BOUND_MODEL.bind(agent=profile.name, missing=bound, actual=active.id))
        for ref in profile.sub_agents.agents:
            tool = ref.tool_name or ref.profile
            child = self._agent_registry.get(ref.profile) if self._agent_registry is not None else None
            if child is None or tool not in manifest.runtime_details.sub_agent_tools:
                report.append(_UNAVAILABLE_CHILD.bind(parent=profile.name, child=ref.profile, tool=tool))
            elif (
                child.acp is None
                and (bound := child.model.profile_id)
                and models is not None
                and models.get(bound) is None
            ):
                actual = resolve_for_agent(models, self._settings_handle.settings, child, fallback=active)
                report.append(_MISSING_BOUND_MODEL.bind(agent=child.name, missing=bound, actual=actual.id))
        return report

    def _reload_model_default(
        self, candidate: LoadedSettings, profiles: ProfileReload, report: list[MessageRef]
    ) -> tuple[LoadedSettings, bool]:
        """Validate an external selection or replace a default that names no selectable model."""
        requested = candidate.settings.model_profile
        old = self._settings_handle.base
        previous = old.settings.model_profile
        key = "model.profile.active"
        if candidate.session_scoped_model and requested == previous:
            # Decided afresh every time, but only a different answer is an edit to judge.
            candidate = dataclasses.replace(candidate, model_edit=None)
        old_model = resolve_selectable_profile(self._model_registry, previous)
        selected = resolve_selectable_profile(profiles.models, requested)
        # A saved selector may be a display name. Renaming or reusing that
        # name must keep the same model when the user hasn't changed selection.
        if not candidate.model_edit and requested == previous and old_model and profiles.models:
            same_id = profiles.models.get(old_model.id)
            if same_id is not None and is_model_profile_selectable(same_id) and same_id is not selected:
                revision = candidate.manual_model_revision
                if revision is None:
                    raise ConfigValueError(_RELOAD_UNAVAILABLE.bind(model=requested))
                return dataclasses.replace(
                    candidate,
                    model_edit=ModelFileChange(candidate.file_model, same_id.id, candidate.source_for(key), revision),
                    settings=dataclasses.replace(candidate.settings, model_profile=same_id.id),
                ), False
        if not requested or selected is not None:
            return candidate, False
        if candidate.model_edit and (not previous or resolve_selectable_profile(profiles.models, previous) is not None):
            if self._current.loaded is None:
                raise ConfigValueError(_RELOAD_UNAVAILABLE.bind(model=requested))
            report.append(_RELOAD_KEPT.bind(model=requested))
            return dataclasses.replace(
                candidate,
                model_edit=None,
                settings=dataclasses.replace(candidate.settings, model_profile=previous),
                provenance={**candidate.provenance, key: old.source_for(key)},
            ), True
        if candidate.model_edit and requested == candidate.file_model != old.file_model:
            # The user just wrote it and no usable model is left to keep: report it, never a stand-in.
            raise ConfigValueError(_RELOAD_UNAVAILABLE.bind(model=requested))
        # Nobody just named it: whether its file went in this reload, in another session's
        # (the definitions are shared) or never existed, the model next in line takes over.
        replacement = resolve_selectable_profile(profiles.models, candidate.file_model)
        if replacement is None and profiles.models is not None:
            replacement = next(
                (
                    p
                    for p in sorted(profiles.models.list_profiles(), key=lambda p: (p.name.casefold(), p.id))
                    if is_model_profile_selectable(p)
                ),
                None,
            )
        if replacement is None:
            # Nothing to fall back to: the build decides whether the session can go on.
            return candidate, False
        if candidate.manual_model_revision is None:
            raise ConfigValueError(_RELOAD_UNAVAILABLE.bind(model=requested))
        origin = SettingOrigin(layer=Source.PROCESS_RUNTIME)
        report.append(_MODEL_FALLBACK.bind(old=previous, new=replacement.name))
        return dataclasses.replace(
            candidate,
            model_edit=ModelFileChange(candidate.file_model, replacement.id, origin, candidate.manual_model_revision),
            settings=dataclasses.replace(candidate.settings, model_profile=replacement.id),
            provenance={**candidate.provenance, key: origin},
        ), False

    def _session_choice_after_reload(self, *, model_chosen: bool) -> dict[str, Any]:
        """The session's own choices a reload commits: without its model once the user chose one or it is gone."""
        session = self._settings_handle.session_values
        model_id = self._settings_handle.session_model
        if model_id and (model_chosen or not self._model_selectable(model_id)):
            return {}
        return session

    def _model_selectable(self, profile_id: str) -> bool:
        registry = self._model_registry
        profile = registry.get(profile_id) if registry is not None else None
        return profile is not None and is_model_profile_selectable(profile)

    async def _publish_settings_reloaded(self, report: tuple[MessageRef, ...] = (), *, manual: bool = False) -> None:
        await self._bus.publish(
            SettingsReloaded(
                manual=manual,
                report=report,
                runtime_details=copy.deepcopy(self._current.manifest.runtime_details),
                session_id=self._session.session_id,
            )
        )

    async def on_workspace_change(self, event: WorkspaceChange) -> None:
        """Handle workspace/cwd change — rebuild agent with new workspace."""
        if self._session.agent_profile is None:
            await self._bus.publish(
                Error(
                    code="runtime_mutation_not_ready",
                    message="No active agent — cannot change workspace",
                    display_message=_CONTROLS_WORKSPACE_SWITCH_NOT_READY.bind(),
                    session_id=self._session.session_id,
                )
            )
            return
        if event.primary_cwd and (missing := Workspace.from_cwd(event.primary_cwd).missing_primary()) is not None:
            await self._bus.publish(
                Error(
                    code="workspace_change_failed",
                    message=f"Working directory does not exist: {surrogate_safe_text(missing)}",
                    display_message=_CONTROLS_WORKSPACE_MISSING.bind(path=DisplayPath(missing)),
                    session_id=self._session.session_id,
                )
            )
            return
        token = self._permits.capture_control_token()
        permit = await self._permits.acquire_rebuild_permit(token)
        if isinstance(permit, RebuildPermitDenied):
            await self._publish_workspace_satisfied_or_denied(event.primary_cwd, permit, token.session_id)
            return

        new_workspace = Workspace.from_cwd(event.primary_cwd)
        starting_without_executor = self._current.loaded is None
        try:
            # A workspace change is a settings reload in disguise: the project
            # trust domain is root-derived, so the new root's layers must be
            # audited and applied — and the old root's dropped — by the same
            # rebuild that installs the new workspace. Same routing as a reload
            # (RESTART values stay in force, the command line keeps its credit),
            # minus the restart-required warning: nothing was edited here, so any
            # deferred value was already reported by the reload that deferred it.
            try:
                old_loaded = self._settings_handle.base
                candidate = await asyncio.to_thread(
                    load_settings,
                    project_root=Path(new_workspace.primary_cwd),
                    eval_context=self._reload_eval_context(),
                    **self._session_pin_overrides(),
                )
                staged_loaded, _ = route_restart_settings(reattribute_command_line(candidate, old_loaded), old_loaded)
            except Exception as exc:
                # Same shape as a reload's load failure: it aborts before the
                # rebuild publishes any completion event, and the bus swallows
                # handler exceptions — without an explicit terminal event here a
                # caller awaiting the change would hang until its timeout.
                await self._bus.publish(
                    Error(code="workspace_change_failed", message=str(exc), session_id=self._session.session_id)
                )
                raise
            await self._start_or_restart_with_permit(
                permit,
                self._session.agent_profile,
                operation="workspace_change",
                workspace=new_workspace,
                staged_loaded=staged_loaded,
            )
            # After the commit, not before: these verdicts describe the new
            # root's files, and a failed rebuild keeps the old root's settings.
            for warning in settings_warning_events(staged_loaded):
                await self._bus.publish(dataclasses.replace(warning, session_id=self._session.session_id))
            if starting_without_executor:
                await self._publish_workspace_updated_snapshot(token.session_id)
        finally:
            self._permits.release_rebuild_permit(permit)
