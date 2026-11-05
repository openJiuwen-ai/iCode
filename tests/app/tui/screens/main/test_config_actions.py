# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for main-screen runtime/config action controller."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml

from chrys.app.tui.screens.agents.config import AgentsConfigScreen
from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
from chrys.app.tui.screens.main.config_actions import (
    RuntimeConfigCallbacks,
    RuntimeConfigController,
    _canonical_active_model_profile_id,
)
from chrys.app.tui.screens.main.model_indicator import compute_model_indicator_state
from chrys.app.tui.screens.main.session_handlers import SessionCallbacks, SessionHandler
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.view_adapter import MainScreenViewAdapter
from chrys.app.tui.screens.models.picker import ModelPickerAction, ModelsScreen
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.config.settings import DEFAULT_LOCALE
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    ProfileSwitched,
    RuntimeModelDetails,
    SetApprovalMode,
    SettingsReload,
)
from chrys.foundation.i18n import Localizer
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile, is_model_profile_selectable
from tests.app.tui.screens._model_config_support import _model_config_result_events
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_helpers import discard_worker


@pytest.fixture(autouse=True)
def _isolate_chrys_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep model-pointer reads/writes away from the developer's real config."""
    fake_platform = type("P", (), {"config_dir": tmp_path})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    monkeypatch.delenv("CHRYS_MODEL_PROFILE")


class _View:
    def __init__(self) -> None:
        self.chat_profiles: list[str] = []
        self.status_profiles: list[tuple[str, str]] = []
        self.pushed: list[tuple[object, object | None]] = []
        self.notifications: list[tuple[object, object, str, float | None]] = []
        self.focus_requests = 0
        self.worker_tasks: list[asyncio.Task[Any]] = []
        self.worker_groups: list[str] = []

    def set_chat_profile(self, profile: str) -> None:
        self.chat_profiles.append(profile)

    def set_status_profile(self, profile: str, *, description: str = "") -> None:
        self.status_profiles.append((profile, description))

    def push_screen(self, screen: object, callback: object | None = None) -> None:
        self.pushed.append((screen, callback))

    def focus_input(self) -> None:
        self.focus_requests += 1

    def run_worker(self, awaitable: Awaitable[Any], *, group: str) -> None:
        self.worker_groups.append(group)
        self.worker_tasks.append(asyncio.create_task(awaitable))

    async def drain_workers(self) -> None:
        tasks, self.worker_tasks = self.worker_tasks, []
        await asyncio.gather(*tasks)

    def notify(
        self,
        message: object,
        *,
        title: object,
        severity: str = "information",
        timeout: float | None = 3,
    ) -> None:
        self.notifications.append((message, title, severity, timeout))


class _ProfileDescriptions:
    def get_profile_description(self, profile_name: str) -> str:
        return f"description for {profile_name}"


def _notification_service() -> object:
    return object()


def _settings_coordinator() -> object:
    return object()


def _callbacks() -> RuntimeConfigCallbacks:
    return RuntimeConfigCallbacks(
        start_worker=discard_worker,
        set_profile_display=lambda _profile: None,
        update_subtitle=lambda: None,
        debug=lambda _key, _message="": None,
        notification_service=_notification_service,
        settings_coordinator=_settings_coordinator,
        user_picked=lambda: None,
    )


def test_agent_config_saved_uses_profile_description_port() -> None:
    view = _View()
    state = MainScreenState()
    state.runtime.profile = "Code"
    subtitle_updates: list[bool] = []
    profile_updates: list[str] = []
    controller = RuntimeConfigController(
        state=state,
        services=MainScreenServices(bus=EventBus()),
        view=cast(Any, view),
        callbacks=replace(
            _callbacks(),
            set_profile_display=profile_updates.append,
            update_subtitle=lambda: subtitle_updates.append(True),
        ),
        profile_descriptions=_ProfileDescriptions(),
    )

    controller.on_agent_config_saved("Code Renamed", "CodeProfile")

    assert state.runtime.profile == "Code Renamed"
    assert state.runtime.pending_active_switch == "CodeProfile"
    assert profile_updates == ["Code Renamed"]
    assert subtitle_updates == [True]
    assert view.chat_profiles == ["Code Renamed"]
    assert view.status_profiles == [("Code Renamed", "description for CodeProfile")]


@pytest.mark.asyncio
async def test_select_model_tag_opens_picker_and_routes_results(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="current-id", name="Current", model_id="current-wire"))
    registry.register(ModelProfile(id="picked-id", name="Picked", model_id="picked-wire"))
    state = MainScreenState()
    state.runtime.details_confirmed = True
    state.runtime.details.model = RuntimeModelDetails(profile_id="current-id")
    view = _View()
    controller = RuntimeConfigController(
        state=state,
        services=MainScreenServices(bus=EventBus(), model_registry=registry),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )
    picked: list[str] = []
    managed: list[None] = []

    async def record_pick(profile_id: str) -> None:
        picked.append(profile_id)

    monkeypatch.setattr(controller, "on_model_picked", record_pick)
    monkeypatch.setattr(controller, "open_model_config", lambda: managed.append(None))

    controller.on_model_tag_clicked("select")

    assert len(view.pushed) == 1
    picker, callback = view.pushed[0]
    assert isinstance(picker, ModelsScreen)
    assert picker._current_profile_id == "current-id"
    assert callable(callback)

    await cast(Any, callback)("picked-id")
    await cast(Any, callback)(ModelPickerAction.MANAGE)
    await cast(Any, callback)(None)

    assert picked == ["picked-id"]
    assert managed == [None]
    # Pick and dismissal hand focus back; MANAGE defers to the config screen.
    assert view.focus_requests == 2


@pytest.mark.asyncio
async def test_model_pick_activates_then_publishes_one_settings_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    published: list[SettingsReload] = []

    async def capture(event: SettingsReload) -> None:
        published.append(event)

    await bus.subscribe(SettingsReload, capture)
    activated: list[tuple[str, int]] = []

    def record_activation(profile_id: str) -> None:
        activated.append((profile_id, threading.get_ident()))

    monkeypatch.setattr("chrys.service.profiles.models.env_bridge.activate_model_profile", record_activation)
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=bus),
        view=cast(Any, _View()),
        callbacks=_callbacks(),
    )

    await controller.on_model_picked("picked-id")

    assert activated == [("picked-id", activated[0][1])]
    assert activated[0][1] != threading.get_ident()
    assert len(published) == 1


@pytest.mark.asyncio
async def test_model_pick_write_failure_notifies_error_without_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = EventBus()
    published: list[SettingsReload] = []

    async def capture(event: SettingsReload) -> None:
        published.append(event)

    await bus.subscribe(SettingsReload, capture)

    def fail_activation(_profile_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("chrys.service.profiles.models.env_bridge.activate_model_profile", fail_activation)
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=bus),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    await controller.on_model_picked("picked-id")

    assert published == []
    assert len(view.notifications) == 1
    assert view.notifications[0][0] == "disk full"
    assert view.notifications[0][2] == "error"


@pytest.mark.asyncio
async def test_failed_settings_reload_keeps_confirmed_runtime_and_cache_without_success_toast(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_platform = SimpleNamespace(config_dir=tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.delenv("CHRYS_MODEL_PROFILE", raising=False)
    (tmp_path / ".env").write_text("CHRYS_MODEL_PROFILE=old-id\n", encoding="utf-8")
    bus = EventBus()

    async def fail_reload(_event: SettingsReload) -> None:
        raise RuntimeError("backend rebuild failed")

    await bus.subscribe(SettingsReload, fail_reload)
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="old-id", name="Old Model", model_id="old-wire"))
    registry.register(ModelProfile(id="new-id", name="New Model", model_id="new-wire"))
    state = MainScreenState()
    state.runtime.details_confirmed = True
    state.runtime.details.model = RuntimeModelDetails(profile_id="old-id", name="Old Model", selection_source="active")
    services = MainScreenServices(bus=bus, model_registry=registry, active_model_profile_id="old-id")
    view = _View()
    controller = RuntimeConfigController(
        state=state,
        services=services,
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    await controller.on_model_picked("new-id")

    # The durable pointer landed in the document; the user's dotenv is not touched.
    doc = yaml.safe_load((tmp_path / "settings.yaml").read_text(encoding="utf-8"))
    assert doc["model"]["profile"]["active"] == "new-id"
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "CHRYS_MODEL_PROFILE=old-id\n"
    assert state.runtime.details.model.profile_id == "old-id"
    assert services.active_model_profile_id == "old-id"
    assert view.notifications == []
    # No confirmation event arrived, so the screen's indicator read path
    # (confirmed details + registry selectability) must still render the
    # old model's label.
    assert state.runtime.details_confirmed is True
    indicator = compute_model_indicator_state(
        state.runtime.details.model if state.runtime.details_confirmed else None,
        any(is_model_profile_selectable(profile) for profile in registry.list_profiles()),
        state.runtime.profile,
        Localizer(DEFAULT_LOCALE),
    )
    assert indicator.label == "Old Model"
    assert indicator.profile_id == "old-id"
    assert indicator.mode == "select"
    os.environ.pop("CHRYS_MODEL_PROFILE", None)


@pytest.mark.asyncio
async def test_model_config_result_requests_reload_without_preconfirm_cache_or_success_toast() -> None:
    bus = EventBus()
    published: list[SettingsReload] = []

    async def capture(event: SettingsReload) -> None:
        published.append(event)

    await bus.subscribe(SettingsReload, capture)
    services = MainScreenServices(bus=bus, active_model_profile_id="confirmed-id")
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=services,
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    await controller.on_model_config_result("deleted")

    assert len(published) == 1
    assert services.active_model_profile_id == "confirmed-id"
    assert view.notifications == []


@pytest.mark.asyncio
async def test_model_config_result_updated_still_notifies_profile_updated() -> None:
    bus = EventBus()
    published: list[SettingsReload] = []

    async def capture(event: SettingsReload) -> None:
        published.append(event)

    await bus.subscribe(SettingsReload, capture)
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=bus),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    await controller.on_model_config_result("updated")

    assert len(published) == 1
    assert len(view.notifications) == 1
    message, _title, severity, _timeout = view.notifications[0]
    assert message.definition.key == "tui.config.model.updated"
    assert severity == "information"


@pytest.mark.asyncio
async def test_config_screens_receive_file_default_and_runtime_effective_pointers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "chrys.foundation.platform.get_platform",
        lambda: SimpleNamespace(config_dir=tmp_path),
    )
    (tmp_path / "settings.yaml").write_text(
        yaml.safe_dump({"model": {"profile": {"active": "model-a"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "model-b")
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="model-a", name="Model A", model_id="global-wire"))
    model_registry.register(ModelProfile(id="model-b", name="Model B", model_id="runtime-wire"))
    services = MainScreenServices(
        bus=EventBus(),
        agent_registry=AgentProfileRegistry(),
        model_registry=model_registry,
        active_model_profile_id="model-b",
    )
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=services,
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    controller.open_model_config()
    await view.drain_workers()
    controller.open_agent_config()

    model_screen, _model_callback = view.pushed[0]
    agent_screen, _agent_callback = view.pushed[1]
    assert isinstance(model_screen, ModelConfigScreen)
    assert model_screen._global_default_profile_id == "model-a"
    assert isinstance(agent_screen, AgentsConfigScreen)
    assert agent_screen._active_model_profile_id == "model-b"


@pytest.mark.asyncio
async def test_open_model_config_keeps_the_event_loop_responsive_while_the_pointer_read_waits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Model A", model_id="wire-a"))
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=EventBus(), model_registry=registry),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )
    read_started = threading.Event()
    release_read = threading.Event()
    read_threads: list[int] = []

    def blocked_read() -> str:
        read_threads.append(threading.get_ident())
        read_started.set()
        assert release_read.wait(10)
        return "model-a"

    monkeypatch.setattr(
        "chrys.service.profiles.models.env_bridge.get_global_default_profile_id",
        blocked_read,
    )

    controller.open_model_config()
    try:
        assert await asyncio.to_thread(read_started.wait, 2)
        loop_advanced = asyncio.Event()
        asyncio.get_running_loop().call_soon(loop_advanced.set)
        await asyncio.wait_for(loop_advanced.wait(), timeout=1)
        assert view.pushed == []
    finally:
        release_read.set()
        await view.drain_workers()

    assert read_threads and threading.get_ident() not in read_threads
    assert len(view.pushed) == 1


def test_canonical_model_profile_id_does_not_fall_back_to_process_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.models.env_bridge import get_global_default_profile_id

    monkeypatch.setattr(
        "chrys.foundation.platform.get_platform",
        lambda: SimpleNamespace(config_dir=tmp_path),
    )
    (tmp_path / ".env").write_text("CHRYS_THEME=dark\n", encoding="utf-8")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "model-b")
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-b", name="Model B", model_id="runtime-wire"))

    file_selector = get_global_default_profile_id()

    assert file_selector == ""
    assert _canonical_active_model_profile_id(registry, file_selector) == ""


def test_tui_canonical_active_model_profile_id_resolves_explicit_profile_name() -> None:
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-id", name="Friendly Model"))

    assert _canonical_active_model_profile_id(registry, "friendly model") == "model-id"


def test_tui_canonical_active_model_profile_id_prefers_explicit_active_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.service.profiles.models.registry import ModelProfileRegistry
    from chrys.service.profiles.models.schema import ModelProfile

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="env-model", name="Env Model"))
    registry.register(ModelProfile(id="active-model", name="Active Model"))
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "env-model")

    assert _canonical_active_model_profile_id(registry, "active-model") == "active-model"


def test_select_model_tag_ignores_unconfirmed_runtime_profile() -> None:
    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="stale-id", name="Stale", model_id="stale-wire"))
    state = MainScreenState()
    state.runtime.details.model = RuntimeModelDetails(profile_id="stale-id")
    view = _View()
    controller = RuntimeConfigController(
        state=state,
        services=MainScreenServices(bus=EventBus(), model_registry=registry),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    controller.on_model_tag_clicked("select")

    picker, _callback = view.pushed[0]
    assert isinstance(picker, ModelsScreen)
    assert picker._current_profile_id == ""


class _BusyEngine:
    def execution_busy(self) -> bool:
        return True


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "agent",
            (
                "Code [Agent] is bound to the model Bound [模型], so the model cannot be switched here. "
                "To change it, press F2 and edit the agent's model on its Basic tab."
            ),
        ),
        ("override", "This session is pinned to this model."),
        ("inherited", "Model selection is locked."),
    ],
)
def test_locked_model_tag_explains_its_lock_even_while_a_run_is_busy(source: str, expected: str) -> None:
    state = MainScreenState()
    state.runtime.profile = "Code [Agent]"
    state.runtime.details_confirmed = True
    state.runtime.details.model = RuntimeModelDetails(
        profile_id="bound", name="Bound [模型]", selection_source=cast(Any, source)
    )
    view = _View()
    controller = RuntimeConfigController(
        state=state,
        services=MainScreenServices(bus=EventBus(), engine_provider=cast(Any, _BusyEngine)),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    controller.on_model_tag_clicked("locked")

    assert view.pushed == []
    assert len(view.notifications) == 1
    message, title, severity, timeout = view.notifications[0]
    assert Localizer("en").render(cast(Any, message)) == expected
    assert title.definition.key == "tui.config.title.model_locked"
    assert severity == "warning"
    assert timeout == 6


def test_agent_model_lock_notice_is_translated() -> None:
    state = MainScreenState()
    state.runtime.profile = "Code"
    state.runtime.details_confirmed = True
    state.runtime.details.model = RuntimeModelDetails(profile_id="bound", name="Bound", selection_source="agent")
    view = _View()
    controller = RuntimeConfigController(
        state=state,
        services=MainScreenServices(bus=EventBus()),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    controller.on_model_tag_clicked("locked")

    message = view.notifications[0][0]
    expected = (
        "智能体“Code”绑定了模型“Bound”，无法在此切换模型。"  # noqa: RUF001
        "如需更改，请按 F2，在该智能体的“基本”标签页中修改模型配置。"  # noqa: RUF001
    )
    assert Localizer("zh-Hans").render(cast(Any, message)) == expected


@pytest.mark.parametrize(
    ("selector", "expected"),
    [
        ("profile", "Cannot switch agents while the agent is busy"),
        ("model", "Cannot switch models while the agent is busy"),
    ],
)
def test_busy_selector_click_names_the_selector_it_blocked(selector: str, expected: str) -> None:
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=EventBus()),
        view=cast(Any, view),
        callbacks=_callbacks(),
    )

    controller.on_selector_busy(cast(Any, selector))

    assert view.pushed == []
    assert len(view.notifications) == 1
    message, title, severity, _timeout = view.notifications[0]
    assert Localizer("en").render(cast(Any, message)) == expected
    assert Localizer("en").render(cast(Any, title)) == "Busy"
    assert severity == "warning"


def test_view_adapter_open_runtime_details_pushes_runtime_details_dialog() -> None:
    pushed: list[object] = []

    def push_screen(dialog: object) -> None:
        pushed.append(dialog)

    def load_shortcuts() -> list[object]:
        return []

    def shortcut_model_text(spec: str) -> str:
        return spec

    screen = SimpleNamespace(
        app=SimpleNamespace(push_screen=push_screen),
        prompt_shortcut_sources_loader=lambda: load_shortcuts,
        shortcut_model_text=shortcut_model_text,
    )
    details = AgentRuntimeDetails()

    MainScreenViewAdapter(cast(Any, screen), state=MainScreenState()).open_runtime_details(details)

    assert len(pushed) == 1
    assert isinstance(pushed[0], RuntimeDetailsDialog)
    assert pushed[0]._details is details
    assert pushed[0]._load_shortcuts is load_shortcuts
    assert pushed[0]._shortcut_model_text is shortcut_model_text


class _CoordinatorStub:
    def __init__(self) -> None:
        self.attached: list[object] = []
        self.external_writes: list[tuple[str, object]] = []

    def attach_dialog(self, dialog: object) -> None:
        self.attached.append(dialog)

    def note_external_write(self, key: str, value: object) -> None:
        self.external_writes.append((key, value))


def test_open_settings_pushes_the_dialog_on_the_requested_tab_and_attaches_it() -> None:
    from chrys.app.tui.screens.settings import NOTIFICATIONS_TAB_ID, SettingsDialog

    coordinator = _CoordinatorStub()
    view = _View()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=EventBus()),
        view=cast(Any, view),
        callbacks=replace(_callbacks(), settings_coordinator=lambda: cast(Any, coordinator)),
    )

    controller.open_settings(NOTIFICATIONS_TAB_ID)

    assert len(view.pushed) == 1
    dialog = view.pushed[0][0]
    assert isinstance(dialog, SettingsDialog)
    assert dialog._initial_tab == NOTIFICATIONS_TAB_ID
    assert coordinator.attached == [dialog]


@pytest.mark.parametrize(
    ("requested", "noted"),
    [("manual", "manual"), ("auto", "auto"), ("bypass", "auto")],
)
async def test_set_approval_mode_tells_the_settings_panel_what_the_engine_persists(
    requested: str,
    noted: str,
) -> None:
    coordinator = _CoordinatorStub()
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=EventBus()),
        view=cast(Any, _View()),
        callbacks=replace(_callbacks(), settings_coordinator=lambda: cast(Any, coordinator)),
    )

    async with capture_event_sequence(controller._services.bus, SetApprovalMode) as events:
        await controller.set_approval_mode(requested)
    # The chat session carries the choice for the whole launch, so it is the one that is saved.
    assert len(events) == 1
    assert (events[0].mode, events[0].session_id, events[0].persist) == (requested, None, True)

    # Bypass is a per-launch posture: the engine writes ``auto`` to disk, so
    # the panel is told ``auto`` too.
    assert coordinator.external_writes == [("approval.default_mode", noted)]


# The close-adoption tests below, the profile-switch confirmation test, and the
# SessionHandler double they share moved here from
# tests/app/tui/screens/test_model_config.py: all four exercise this module's
# controller and handler without ever mounting the model configuration screen.


def _confirmation_session_handler(state: MainScreenState, services: MainScreenServices) -> SessionHandler:
    """Build a SessionHandler wired to sync the process-effective cache on confirmation."""

    def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    def _set_active_model_profile_id(value: str) -> None:
        services.active_model_profile_id = value

    handler = SessionHandler(
        state=state,
        services=services,
        view=cast(Any, SimpleNamespace()),
        callbacks=SessionCallbacks(
            set_agent_loading=_noop,
            set_has_messages=_noop,
            set_creating_new_session=_noop,
            set_restoring_session=_noop,
            set_profile_display=_noop,
            set_active_model_profile_id=_set_active_model_profile_id,
            set_workspace_cwd=_noop,
            update_subtitle=_noop,
            update_toc=_noop,
            clear_suggestion_file_cache=_noop,
            start_worker=discard_worker,
            post_gc_message=_noop,
            debug=_noop,
            refresh_model_indicator=_noop,
        ),
        agent_load=cast(
            Any,
            SimpleNamespace(
                begin_session_restore_load=_noop,
                cancel_agent_load=_noop,
                finish_agent_load=_noop,
            ),
        ),
    )
    handler._presenter = cast(
        Any,
        SimpleNamespace(set_tool_info=_noop, clear_status=_noop, flash_status=_noop),
    )
    return handler


async def test_close_keeps_existing_process_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Model A", model_id="first-wire"))
    registry.register(ModelProfile(id="model-b", name="Model B", model_id="active-wire"))
    set_global_default_profile_id("model-a")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "model-b")

    published = await _model_config_result_events("updated", registry)

    assert len(published) == 1
    assert os.environ["CHRYS_MODEL_PROFILE"] == "model-b"


async def test_close_repoints_dangling_process_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process pointer whose profile no longer exists preserves nothing;
    closing the modal adopts the valid file default instead of leaving the
    runtime stuck on the built-in placeholder."""
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="model-a", name="Model A", model_id="first-wire"))
    set_global_default_profile_id("model-a")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "ghost-id")

    published = await _model_config_result_events("updated", registry)

    assert len(published) == 1
    assert os.environ["CHRYS_MODEL_PROFILE"] == "model-a"


async def test_close_does_not_adopt_unresolvable_file_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the file default is itself hollow, adoption stands down rather
    than swapping one unusable pointer for another."""
    from chrys.service.profiles.models.env_bridge import set_global_default_profile_id

    registry = ModelProfileRegistry()
    registry.register(ModelProfile(id="hollow-id", name="Hollow"))
    set_global_default_profile_id("hollow-id")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "ghost-id")

    published = await _model_config_result_events("updated", registry)

    assert len(published) == 1
    assert os.environ["CHRYS_MODEL_PROFILE"] == "ghost-id"


async def test_profile_switch_confirmation_lands_promoted_model_on_runtime_cache() -> None:
    """A backend profile-switch confirmation lands runtime details and the process cache on the promoted model."""
    registry = ModelProfileRegistry()
    promoted = ModelProfile(id="model-a", name="Model A", model_id="global-wire")
    registry.register(promoted)

    state = MainScreenState()
    services = MainScreenServices(
        bus=EventBus(),
        model_registry=registry,
        active_model_profile_id="model-b",
    )
    handler = _confirmation_session_handler(state, services)

    await handler.on_profile_switched(
        ProfileSwitched(
            from_profile="Code",
            to_profile="Code",
            runtime_details=AgentRuntimeDetails(
                model=RuntimeModelDetails(
                    profile_id=promoted.id,
                    name=promoted.name,
                    selection_source="active",
                )
            ),
        )
    )

    assert services.active_model_profile_id == promoted.id
    assert state.runtime.details_confirmed is True
    assert state.runtime.details.model.profile_id == promoted.id


# -- a prompt shortcut's pending switch gives way only to the user's own pick --


def _picks_controller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, busy: bool = False, running: bool = False
) -> tuple[RuntimeConfigController, list[str], MainScreenState]:
    monkeypatch.setattr("chrys.service.profiles.models.env_bridge.activate_model_profile", lambda _profile_id: None)
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=tmp_path / "agents")
    state = MainScreenState()
    state.runtime.profile = "Code"
    state.run.agent_running = running
    picks: list[str] = []

    class _Engine:
        def execution_busy(self) -> bool:
            return busy

    controller = RuntimeConfigController(
        state=state,
        services=MainScreenServices(bus=EventBus(), agent_registry=registry, engine_provider=_Engine),
        view=cast(Any, _View()),
        callbacks=replace(_callbacks(), user_picked=lambda: picks.append("picked")),
    )
    return controller, picks, state


@pytest.mark.parametrize("pick", ["agent_picker", "model_picker"])
async def test_users_own_agent_or_model_pick_tells_the_prompt_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, pick: str
) -> None:
    controller, picks, _state = _picks_controller(monkeypatch, tmp_path)

    if pick == "agent_picker":
        await controller.switch_agent_profile("QA")
    else:
        await controller.on_model_picked("picked-id")

    assert picks == ["picked"]


@pytest.mark.parametrize(
    "change",
    [
        "model_settings_updated",
        # A model deleted: the reload drops whatever is gone; nobody picked anything.
        "model_settings_deleted",
        "agent_config_rename",
        "agent_config_fallback",
        "agent_config_saved",
        "agent_picker_while_running",
        "model_picker_while_busy",
        "model_pick_failed",
    ],
)
async def test_changes_that_are_not_the_users_pick_leave_the_prompt_card_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    controller, picks, state = _picks_controller(
        monkeypatch,
        tmp_path,
        busy=change == "model_picker_while_busy",
        running=change == "agent_picker_while_running",
    )
    if change == "model_pick_failed":

        def fail(_profile_id: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("chrys.service.profiles.models.env_bridge.activate_model_profile", fail)

    if change == "model_settings_updated":
        await controller.on_model_config_result("updated")
    elif change == "model_settings_deleted":
        await controller.on_model_config_result("deleted")
    elif change == "agent_config_rename":
        controller.on_agent_config_saved("Code Renamed", "QA")
        await controller.on_agent_config_result("")
    elif change == "agent_config_fallback":
        await controller.on_agent_config_result("switched")
    elif change == "agent_config_saved":
        controller.on_agent_config_saved("Code Renamed", None)
    elif change == "agent_picker_while_running":
        await controller.switch_agent_profile("QA")
    else:
        await controller.on_model_picked("picked-id")

    assert picks == []
    assert state.runtime.pending_active_switch is None or change == "agent_config_saved"
