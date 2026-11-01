# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Runtime, model, agent, approval, and notification actions for MainScreen."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from chrys.app.tui.screens.main.model_indicator import model_lock_notice
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.foundation.events.types import SetApprovalMode
from chrys.foundation.i18n import msg
from chrys.service.approval.policy import ApprovalMode

_BUSY_TITLE = msg("tui.config.title.busy", fallback="Busy")
_MODEL_LOCKED_TITLE = msg("tui.config.title.model_locked", fallback="Model Locked")
_MODEL_SETTINGS_TITLE = msg("tui.config.title.model_settings", fallback="Model Settings")
_AGENT_TITLE = msg("tui.config.title.agent", fallback="Agent")
_SETTINGS_TITLE = msg("tui.config.title.settings", fallback="Settings")
_MODEL_CONFIG_LOADING = msg(
    "tui.config.model.loading",
    fallback="Cannot open model config while agent is loading",
)
_MODEL_PROFILE_UPDATED = msg("tui.config.model.updated", fallback="Model profile updated")
_AGENT_CONFIG_LOADING = msg(
    "tui.config.agent.loading",
    fallback="Cannot open config while agent is loading",
)
_AGENT_SWITCHED = msg("tui.config.agent.switched", fallback="Switched to {label}")
_AGENT_SWITCH_BUSY = msg(
    "tui.config.agent.switch_busy",
    fallback="Cannot switch agents while the agent is busy",
)
_MODEL_SWITCH_BUSY = msg(
    "tui.config.model.switch_busy",
    fallback="Cannot switch models while the agent is busy",
)
_NO_MAIN_AGENTS = msg(
    "tui.config.agent.no_main_profiles",
    fallback="No main agent profiles available — add one to continue.",
)
_CONFIGURATION_UPDATED = msg("tui.config.updated", fallback="Configuration updated")

if TYPE_CHECKING:
    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.notifications import NotificationService
    from chrys.app.tui.screens.main.ports import ProfileDescriptionProvider, RuntimeConfigView, StartWorker
    from chrys.app.tui.screens.main.settings_coordinator import SettingsCoordinator
    from chrys.service.profiles.models.registry import ModelProfileRegistry


@dataclass(frozen=True, slots=True)
class RuntimeConfigCallbacks:
    """Screen-owned effects required by runtime/config actions."""

    start_worker: StartWorker
    set_profile_display: Callable[[str], None]
    update_subtitle: Callable[[], None]
    debug: Callable[[str, str], None]
    notification_service: Callable[[], NotificationService]
    settings_coordinator: Callable[[], SettingsCoordinator]
    user_picked: Callable[[], None]
    """The user chose an agent or model; a prompt shortcut's pending switch gives way to it.

    Only the ``#`` and ``$`` pickers choose one. A settings dialog's ``"switched"`` result is not a
    choice: both dialogs return it only when they fall back after the session's profile was deleted,
    renamed or (agents) retired to sub-agent use, so no dialog result calls this."""


def resolve_approval_mode(arg: str, current: ApprovalMode) -> ApprovalMode:
    """Resolve a named mode, or cycle from the currently displayed mode."""
    arg = arg.strip().lower()
    if arg in ("manual", "auto", "bypass"):
        return ApprovalMode(arg)
    return {
        ApprovalMode.MANUAL: ApprovalMode.AUTO,
        ApprovalMode.AUTO: ApprovalMode.BYPASS,
        ApprovalMode.BYPASS: ApprovalMode.MANUAL,
    }[current]


def _canonical_active_model_profile_id(
    model_registry: ModelProfileRegistry | None,
    profile_selector: str = "",
) -> str:
    """Canonicalize an explicit model-profile selector without consulting other state."""
    from chrys.service.profiles.models.resolver import resolve_profile_selector

    if not profile_selector or model_registry is None:
        return profile_selector
    profile = resolve_profile_selector(model_registry, profile_selector)
    if profile is None:
        return profile_selector
    return profile.id


class RuntimeConfigController:
    """Coordinate runtime/config modal actions."""

    def __init__(
        self,
        *,
        state: MainScreenState,
        services: MainScreenServices,
        view: RuntimeConfigView,
        callbacks: RuntimeConfigCallbacks,
        profile_descriptions: ProfileDescriptionProvider | None = None,
        locale_controller: LocaleController | None = None,
    ) -> None:
        self._state = state
        self._services = services
        self._view = view
        self._callbacks = callbacks
        self._profile_descriptions = profile_descriptions
        self._locale_controller = locale_controller
        self._model_config_opening = False

    def on_approval_badge_clicked(self) -> None:
        """Open the approval mode picker modal."""
        from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen

        def _on_result(result: ApprovalMode | None) -> None:
            if result is not None:
                self.start_approval_mode_change(result.value)

        self._view.push_screen(ApprovalModeScreen(self._state.runtime.approval_mode), _on_result)

    def on_profile_tag_clicked(self) -> None:
        """Open the agent picker modal."""
        if (
            self._state.run.agent_running
            or self._state.run.agent_loading
            or self._services.execution_busy()
            or self._services.agent_registry is None
        ):
            return
        from chrys.app.tui.screens.agents.picker import AgentsScreen

        def _on_result(result: str | None) -> None:
            if result:
                self.start_agent_profile_switch(result)

        self._view.push_screen(AgentsScreen(self._services.agent_registry, self._state.runtime.profile), _on_result)

    def on_selector_busy(self, selector: Literal["profile", "model"]) -> None:
        """Tell the user why a status-bar selector ignored a click during a run."""
        message = _AGENT_SWITCH_BUSY if selector == "profile" else _MODEL_SWITCH_BUSY
        self._view.notify(message.bind(), title=_BUSY_TITLE.bind(), severity="warning")

    def on_model_tag_clicked(self, mode: Literal["configure", "select", "locked"]) -> None:
        """Route model-tag actions to configuration or profile selection."""
        if mode == "locked":
            # The lock outlasts any run, so it is explained even while one is busy.
            self._explain_model_lock()
            return
        if self._services.execution_busy():
            return
        match mode:
            case "configure":
                self.open_model_config()
            case "select":
                if self._services.model_registry is None:
                    return
                from chrys.app.tui.screens.models.picker import ModelPickerAction, ModelsScreen

                current_profile_id = (
                    self._state.runtime.details.model.profile_id if self._state.runtime.details_confirmed else ""
                )

                async def _on_result(result: str | ModelPickerAction | None) -> None:
                    if result is ModelPickerAction.MANAGE:
                        # The config screen takes over; its close restores focus.
                        self.open_model_config()
                        return
                    self._view.focus_input()
                    if isinstance(result, str):
                        await self.on_model_picked(result)

                self._view.push_screen(
                    ModelsScreen(self._services.model_registry, current_profile_id),
                    _on_result,
                )

    def _explain_model_lock(self) -> None:
        self._view.notify(
            model_lock_notice(self._state.runtime.details.model, self._state.runtime.profile),
            title=_MODEL_LOCKED_TITLE.bind(),
            severity="warning",
            timeout=6,
        )

    async def on_model_picked(self, profile_id: str) -> None:
        """Persist a picked model profile and request a backend settings reload.

        The pick is for this session and the default together, so it also ends
        the session's own model (a prompt shortcut's, a restored session's).
        """
        if self._services.execution_busy():
            return
        from chrys.foundation.events.types import SettingsReload
        from chrys.service.profiles.models.env_bridge import activate_model_profile

        try:
            await asyncio.to_thread(activate_model_profile, profile_id)
        except Exception as exc:
            self._view.notify(
                str(exc),
                title=_MODEL_SETTINGS_TITLE.bind(),
                severity="error",
                timeout=5,
            )
            return
        await self._services.bus.publish(SettingsReload(model_chosen=True))
        self._callbacks.user_picked()

    def start_agent_profile_switch(self, profile_name: str) -> object:
        """Run :meth:`switch_agent_profile` in a screen worker."""
        return self._callbacks.start_worker(partial(self.switch_agent_profile, profile_name))

    async def switch_agent_profile(self, profile_name: str) -> None:
        """Publish an AgentProfileSwitch event to the backend."""
        if self._state.run.agent_running or self._state.run.agent_loading or self._services.execution_busy():
            return
        from chrys.foundation.events.types import AgentProfileSwitch

        await self._services.bus.publish(AgentProfileSwitch(profile_name=profile_name))
        self._callbacks.user_picked()
        self._callbacks.debug("AgentProfileSwitch", profile_name)

    def _config_read_only(self) -> bool:
        """Whether a config dialog opening now is read-only: a turn runs, or a send in flight will start one.

        A dialog reads the session once, as it opens. A prompt shortcut's send
        holds its claim from its switch until its turn starts: the switch and
        the turn land under whatever opened meanwhile.
        """
        return self._state.run.agent_running or self._state.submit.active or self._services.execution_busy()

    def open_model_config(self) -> None:
        """Schedule opening the model configuration modal."""
        if self._state.run.agent_loading:
            self._view.notify(_MODEL_CONFIG_LOADING.bind(), title=_BUSY_TITLE.bind(), severity="warning")
            return
        if self._services.model_registry is None or self._model_config_opening:
            return
        self._model_config_opening = True
        worker = self._open_model_config()
        try:
            self._view.run_worker(worker, group="model-config-open")
        except BaseException:
            worker.close()
            self._model_config_opening = False
            raise

    async def _open_model_config(self) -> None:
        """Read the locked durable pointer off-loop, then open the modal."""
        from chrys.app.tui.screens.models.screen import ModelConfigScreen
        from chrys.service.profiles.models.env_bridge import get_global_default_profile_id

        try:
            global_default_profile_id = await asyncio.to_thread(get_global_default_profile_id)
            registry = self._services.model_registry
            if registry is None:
                return
            if self._state.run.agent_loading:
                self._view.notify(_MODEL_CONFIG_LOADING.bind(), title=_BUSY_TITLE.bind(), severity="warning")
                return

            def _on_result(result: str) -> None:
                self._callbacks.start_worker(partial(self.on_model_config_result, result))
                self._view.focus_input()

            self._view.push_screen(
                ModelConfigScreen(
                    registry,
                    _canonical_active_model_profile_id(registry, global_default_profile_id),
                    read_only=self._config_read_only(),
                ),
                _on_result,
            )
        except Exception as exc:
            self._view.notify(
                str(exc),
                title=_MODEL_SETTINGS_TITLE.bind(),
                severity="error",
                timeout=5,
            )
        finally:
            self._model_config_opening = False

    async def on_model_config_result(self, result: str) -> None:
        """Handle model config modal result — reload settings if applied.

        ``"deleted"``: a profile was deleted; the reload drops whatever the
        session used that is gone. ``"updated"``: a Save, announced.
        """
        if not result:
            return
        from chrys.foundation.events.types import SettingsReload

        registry = self._services.model_registry
        if registry is not None:
            directory, profiles = await asyncio.to_thread(registry.read_profiles)
            registry.install_profiles(directory, profiles)
        await self._adopt_global_default_when_nothing_active()
        # The engine drops whatever the reload finds gone (the session's own model included).
        await self._services.bus.publish(SettingsReload())
        if result == "updated":
            self._view.notify(_MODEL_PROFILE_UPDATED.bind(), title=_MODEL_SETTINGS_TITLE.bind())
        self._callbacks.debug("ModelConfig", result)

    async def _adopt_global_default_when_nothing_active(self) -> None:
        """Point the runtime at the file default when no usable profile is active.

        Saving a first model profile claims the empty global default; the
        closing reload should then resolve that profile instead of the
        built-in placeholder. A process pointer that still resolves to a
        selectable profile is never overridden — explicit selections keep
        winning. A dangling or hollow pointer (its profile was deleted or
        the models directory changed behind our back) carries no selection
        worth preserving, so it is repointed like the empty case; without
        a registry to validate against, any non-empty pointer is preserved.
        """
        from chrys.service.profiles.models.env_bridge import get_active_profile_id, get_global_default_profile_id
        from chrys.service.profiles.models.resolver import resolve_selectable_profile

        registry = self._services.model_registry
        active_id = get_active_profile_id()
        if active_id and (registry is None or resolve_selectable_profile(registry, active_id) is not None):
            return
        default_id = await asyncio.to_thread(get_global_default_profile_id)
        if default_id and (registry is None or resolve_selectable_profile(registry, default_id) is not None):
            from chrys.foundation.config.runtime_pointer import set_model_pointer
            from chrys.foundation.config.spec import SettingOrigin, Source

            set_model_pointer(default_id, origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))

    def resolve_profile_name(self) -> str:
        """Resolve current display name to canonical profile name."""
        if self._services.agent_registry is not None:
            for profile in self._services.agent_registry.list_profiles(include_sub_agent_only=True):
                if (profile.display_name or profile.name) == self._state.runtime.profile:
                    return profile.name
        return self._state.runtime.profile

    def open_runtime_details(self, *, initial_tab: str | None = None) -> None:
        """Open the active runtime details modal, on *initial_tab* when given."""
        self._view.open_runtime_details(self._state.runtime.details, initial_tab=initial_tab)

    def open_usage(self) -> None:
        """Open the token usage modal."""
        self._view.open_usage()

    def open_settings(self, initial_tab: str) -> None:
        """Open the Settings dialog on *initial_tab*."""
        from chrys.app.tui.screens.settings import SettingsDialog

        coordinator = self._callbacks.settings_coordinator()
        dialog = SettingsDialog(
            coordinator,
            initial_tab=initial_tab,
            locale_controller=self._locale_controller,
        )
        coordinator.attach_dialog(dialog)
        self._view.push_screen(dialog)
        self._callbacks.debug("Settings", f"opened {initial_tab}")

    def open_agent_config(self) -> None:
        """Open the unified agent configuration modal."""
        self._open_agent_config_at_tab(None)

    def open_agent_config_tab(self, tab: str) -> None:
        """Open the agent configuration modal at a specific tab."""
        self._open_agent_config_at_tab(tab)

    def _open_agent_config_at_tab(self, tab: str | None) -> None:
        if self._state.run.agent_loading:
            self._view.notify(_AGENT_CONFIG_LOADING.bind(), title=_BUSY_TITLE.bind(), severity="warning")
            return
        if self._services.agent_registry is None:
            return
        from chrys.app.tui.screens.agents.config import AgentsConfigScreen

        active_name = self.resolve_profile_name()
        kwargs = {"initial_tab": tab} if tab is not None else {}
        self._view.push_screen(
            AgentsConfigScreen(
                self._services.agent_registry,
                current_profile=self._state.runtime.profile,
                initial_profile=active_name,
                active_profile_name=active_name,
                model_registry=self._services.model_registry,
                active_model_profile_id=_canonical_active_model_profile_id(
                    self._services.model_registry,
                    self._services.active_model_profile_id,
                ),
                workspace_cwd=self._state.workspace_marker.current_cwd or None,
                workspace_roots=list(self._state.workspace.roots),
                on_saved=self.on_agent_config_saved,
                read_only=self._config_read_only(),
                **kwargs,
            ),
            self._start_agent_config_result,
        )

    def on_agent_config_saved(self, new_display: str | None, new_registry_name: str | None) -> None:
        """Handle a mid-modal Save from AgentsConfigScreen."""
        if new_display is not None:
            self._state.runtime.profile = new_display
            self._callbacks.set_profile_display(new_display)
            self._callbacks.update_subtitle()
            with contextlib.suppress(Exception):
                self._view.set_chat_profile(new_display)
            lookup_name = new_registry_name or self.resolve_profile_name()
            desc = (
                self._profile_descriptions.get_profile_description(lookup_name)
                if self._profile_descriptions is not None
                else ""
            )
            with contextlib.suppress(Exception):
                self._view.set_status_profile(new_display, description=desc)

        if new_registry_name is not None:
            self._state.runtime.pending_active_switch = new_registry_name

    def _start_agent_config_result(self, result: str) -> None:
        self._callbacks.start_worker(partial(self.on_agent_config_result, result))

    async def on_agent_config_result(self, result: str) -> None:
        """Handle agent config modal result — reload and optionally switch."""
        pending_switch = self._state.runtime.pending_active_switch
        self._state.runtime.pending_active_switch = None

        if not result and pending_switch is None:
            return
        from chrys.foundation.events.types import AgentProfileSwitch, SettingsReload

        if self._services.agent_registry is not None:
            self._services.agent_registry.load_user_profiles()

        new_active_name: str | None = pending_switch
        if new_active_name is None and result == "switched" and self._services.agent_registry is not None:
            profiles = self._services.agent_registry.list_profiles()
            if profiles:
                new_active_name = profiles[0].name

        if new_active_name is not None and self._services.agent_registry is not None:
            new_profile = self._services.agent_registry.get(new_active_name)
            if new_profile is not None:
                label = new_profile.display_name or new_profile.name
                # Not the user's pick: new sessions keep starting on their agent unless it was this one.
                await self._services.bus.publish(AgentProfileSwitch(profile_name=new_active_name, source="fallback"))
                if pending_switch is None:
                    self._view.notify(_AGENT_SWITCHED.bind(label=label), title=_AGENT_TITLE.bind())
                self._callbacks.debug("AgentConfig", f"switched → {new_active_name}")
                return
            if result == "switched":
                self._view.notify(
                    _NO_MAIN_AGENTS.bind(),
                    title=_AGENT_TITLE.bind(),
                    severity="warning",
                    timeout=5,
                )
                self._callbacks.debug(
                    "AgentConfig", "switched but no main-eligible profiles remain — engine state stale"
                )
                return

        await self._services.bus.publish(SettingsReload())
        self._view.notify(_CONFIGURATION_UPDATED.bind(), title=_SETTINGS_TITLE.bind())
        self._callbacks.debug("AgentConfig", result)

    def start_approval_mode_change(self, arg: str) -> object:
        """Run :meth:`set_approval_mode` in a screen worker."""
        return self._callbacks.start_worker(partial(self.set_approval_mode, arg))

    async def set_approval_mode(self, arg: str) -> None:
        """Publish SetApprovalMode for the requested approval mode.

        The engine applies the mode to chat and workflow execution for the whole launch.
        """
        target = resolve_approval_mode(arg, self._state.runtime.approval_mode)
        await self._services.bus.publish(SetApprovalMode(mode=target.value))
        # The engine persists this as the global default and rewrites bypass to
        # auto on the way to disk (a per-launch mode is never saved); the panel
        # projects the saved value, so tell it what was written.
        self._callbacks.settings_coordinator().note_external_write(
            "approval.default_mode",
            ApprovalMode.AUTO.value if target is ApprovalMode.BYPASS else target.value,
        )
