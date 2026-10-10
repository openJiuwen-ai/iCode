# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for TUI notification settings and triggers."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalGroup
from textual.widgets import Checkbox

from chrys.app.tui.notifications import drivers as notification_drivers
from chrys.app.tui.notifications.drivers import MacOSNotificationDriver, NotificationDeliveryResult, NotificationPayload
from chrys.app.tui.notifications.service import NotificationService
from chrys.app.tui.notifications.settings import NOTIFICATIONS_TITLE, NotificationEvent, NotificationSettings
from chrys.app.tui.screens.main.state import MainScreenState, RunState
from chrys.app.tui.screens.settings.panes.notifications import (
    _SUPPRESS_WHILE_FOCUSED,
    _TEST_BUTTON,
    NotificationsPane,
)
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.events.types import ApprovalRequest, ApprovalReviewed, Error, InvocationMessage, QuestionToUser
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.foundation.models.invocations import InvocationOrigin
from tests.support.notifications import RecordingNotificationDriver
from tests.support.tui_helpers import fake_session_title, make_backend_handler
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.app.tui.screens.main.event_handlers import BackendEventHandler


def test_notification_settings_chrome_keeps_english_and_localizes_chinese() -> None:
    reference = _SUPPRESS_WHILE_FOCUSED.bind(app=APP_DISPLAY_NAME)

    assert format_message(reference) == f"Suppress while {APP_DISPLAY_NAME} is focused"
    assert format_message(_TEST_BUTTON.bind()) == "Test"
    assert Localizer("zh-Hans").render(NOTIFICATIONS_TITLE.bind()) == "通知"
    assert Localizer("zh-Hans").render(reference) == f"{APP_DISPLAY_NAME} 获得焦点时暂停通知"
    assert Localizer("zh-Hans").render(_TEST_BUTTON.bind()) == "测试"


class _PanePorts:
    """Notification ports for a pane under test: records saves, answers tests."""

    def __init__(self, settings: NotificationSettings | None = None) -> None:
        self.settings = settings or NotificationSettings()
        self.saved: list[NotificationSettings] = []
        self.tested: list[NotificationSettings] = []
        self.test_result = True
        self.save_error: Exception | None = None

    def current(self) -> NotificationSettings:
        return self.settings

    def save(self, settings: NotificationSettings) -> bool:
        if self.save_error is not None:
            raise self.save_error
        self.saved.append(settings)
        self.settings = settings
        return True

    async def test(self, settings: NotificationSettings) -> bool:
        self.tested.append(settings)
        return self.test_result


class _PaneHost(TuiVariableDefaultsMixin, App):
    def __init__(self, ports: _PanePorts) -> None:
        super().__init__()
        self.pane = NotificationsPane(ports)
        self.toasts: list[tuple[str, str | None]] = []

    def compose(self) -> ComposeResult:
        yield self.pane

    def notify(self, message, *, title="", severity="information", timeout=None, markup=True):
        self.toasts.append((str(message), severity))


class _FakeNotificationDriver:
    def __init__(self, result: NotificationDeliveryResult | None = None) -> None:
        self.result = result or NotificationDeliveryResult(desktop_sent=True, sound_sent=True)
        self.payloads: list[NotificationPayload] = []

    async def send(self, payload: NotificationPayload) -> NotificationDeliveryResult:
        self.payloads.append(payload)
        return self.result


class _FakeNotificationService:
    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def notify(self, event: NotificationEvent) -> bool:
        self.events.append(event)
        return True


class _FakeApp:
    def __init__(self) -> None:
        self.notification_service = _FakeNotificationService()
        self.pushed: list[tuple[object, object | None]] = []

    def push_screen(self, screen: object, callback: object | None = None) -> None:
        self.pushed.append((screen, callback))


class _FakeApprovalDialog:
    def __init__(self, **kwargs: object) -> None:
        self.tool_name = str(kwargs.get("tool_name", ""))
        self._dismissed = False
        self._user_decision_submitted = False
        self.verdicts: list[object] = []

    @property
    def is_dismissed(self) -> bool:
        return self._dismissed

    @property
    def user_decision_submitted(self) -> bool:
        return self._user_decision_submitted

    def receive_verdict(self, verdict: object) -> None:
        self.verdicts.append(verdict)


def test_notification_settings_project_from_settings() -> None:
    from chrys.foundation.config.settings import Settings

    settings = Settings(
        notifications_enabled=False,
        notifications_sound=False,
        notifications_suppress_when_focused=False,
        notifications_event_ask_user=False,
        notifications_event_turn_error=False,
    )

    view = NotificationSettings.from_settings(settings)

    assert view.enabled is False
    assert view.desktop is True
    assert view.sound is False
    assert view.suppress_when_focused is False
    assert view.event_enabled(NotificationEvent.APPROVAL_REQUIRED) is True
    assert view.event_enabled(NotificationEvent.ASK_USER) is False
    assert view.event_enabled(NotificationEvent.TURN_COMPLETE) is True
    assert view.event_enabled(NotificationEvent.TURN_ERROR) is False


def test_notification_settings_patch_targets_persistable_settings_keys() -> None:
    """Every patch key must be a spec key the store will accept, or panel
    saves would start raising ``TypeError`` the moment a key drifted."""
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import specs_by_key

    view = NotificationSettings(
        enabled=False,
        sound=False,
        events={NotificationEvent.ASK_USER: False},
    )

    patch = view.to_settings_patch()

    assert patch == {
        "notifications.enabled": False,
        "notifications.delivery.desktop": True,
        "notifications.delivery.sound": False,
        "notifications.suppress_when_focused": True,
        # Events absent from the mapping count as enabled, same as the
        # delivery service reads them.
        "notifications.events.approval_required": True,
        "notifications.events.ask_user": False,
        "notifications.events.turn_complete": True,
        "notifications.events.turn_error": True,
    }
    specs = specs_by_key(Settings)
    for key in patch:
        assert specs[key].persist is True


def test_notification_settings_patch_round_trips_through_the_projection() -> None:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.config.spec import specs_by_field

    view = NotificationSettings(
        enabled=False,
        desktop=False,
        suppress_when_focused=False,
        events={NotificationEvent.TURN_COMPLETE: False},
    )

    patch = view.to_settings_patch()
    fields_by_key = {entry.key: name for name, entry in specs_by_field(Settings).items()}
    rebuilt = NotificationSettings.from_settings(
        Settings(**{fields_by_key[key]: value for key, value in patch.items()})
    )

    assert rebuilt == NotificationSettings(
        enabled=False,
        desktop=False,
        suppress_when_focused=False,
        events={
            NotificationEvent.APPROVAL_REQUIRED: True,
            NotificationEvent.ASK_USER: True,
            NotificationEvent.TURN_COMPLETE: False,
            NotificationEvent.TURN_ERROR: True,
        },
    )


@pytest.mark.asyncio
async def test_notifications_pane_auto_saves_checkbox_changes() -> None:
    ports = _PanePorts()
    app = _PaneHost(ports)

    async with app.run_test() as pilot:
        await pilot.pause()
        # Mount-time checkbox events are not edits.
        assert ports.saved == []

        sound = app.pane.query_one("#notifications-sound", Checkbox)
        sound.value = False
        await pilot.pause()

    assert [saved.sound for saved in ports.saved] == [False]
    assert ports.saved[-1].enabled is True


@pytest.mark.asyncio
async def test_notifications_pane_master_toggle_hides_detail_sections() -> None:
    ports = _PanePorts()
    app = _PaneHost(ports)

    async with app.run_test() as pilot:
        await pilot.pause()
        section = app.pane.query_one("#notifications-enabled-settings", VerticalGroup)
        enabled = app.pane.query_one("#notifications-enabled", Checkbox)
        assert section.display is True

        enabled.value = False
        await pilot.pause()
        assert section.display is False
        assert ports.saved[-1].enabled is False

        enabled.value = True
        await pilot.pause()
        assert section.display is True


@pytest.mark.asyncio
async def test_notifications_pane_project_repaints_without_saving() -> None:
    ports = _PanePorts()
    app = _PaneHost(ports)

    async with app.run_test() as pilot:
        await pilot.pause()
        ports.settings = NotificationSettings(enabled=False, desktop=False)
        app.pane.project()
        await pilot.pause()

        assert app.pane.query_one("#notifications-enabled", Checkbox).value is False
        assert app.pane.query_one("#notifications-desktop", Checkbox).value is False
        assert app.pane.query_one("#notifications-enabled-settings", VerticalGroup).display is False
        assert ports.saved == []


@pytest.mark.asyncio
async def test_notifications_pane_save_failure_is_nonfatal() -> None:
    ports = _PanePorts()
    ports.save_error = RuntimeError("save failed")
    app = _PaneHost(ports)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.pane._save_current_settings() is False

    assert app.toasts == [("Could not save notification settings", "error")]


@pytest.mark.asyncio
async def test_notifications_pane_awaits_async_test_callback() -> None:
    ports = _PanePorts()
    ports.test_result = False
    app = _PaneHost(ports)

    async with app.run_test() as pilot:
        await pilot.pause()
        await app.pane._on_test_pressed(SimpleNamespace(stop=lambda: None))  # type: ignore[arg-type]

    assert ports.tested == [NotificationSettings()]
    assert app.toasts == [("Test notification could not be delivered", "warning")]


def test_main_screen_exposes_f10_settings_binding() -> None:
    from chrys.app.tui.screens.main.screen import MainScreen

    bindings = {binding.key: binding for binding in MainScreen.BINDINGS}

    assert bindings["f10"].action == "settings"
    assert bindings["f10"].description == "Settings"
    assert bindings["f10"].show is True


def _stub_settings_persist(monkeypatch: pytest.MonkeyPatch) -> list[tuple[dict, tuple, float | None]]:
    from chrys.app.tui.screens.main import settings_persistence as persistence_module
    from chrys.foundation.config.settings_store import PersistResult

    calls: list[tuple[dict, tuple, float | None]] = []

    def persist(values, *, remove=(), lock_timeout=None):
        calls.append((dict(values), tuple(remove), lock_timeout))
        return PersistResult(written=dict(values), rejected={})

    monkeypatch.setattr(persistence_module, "persist", persist)
    return calls


def _stub_overlay_app(monkeypatch: pytest.MonkeyPatch, screen_module: Any) -> list[NotificationSettings]:
    """Give a bare ``MainScreen`` the app that owns the shared settings handle.

    These tests instantiate the screen without a running app, and scheduling a
    save also records the live choice — so the app has to answer for something.
    """
    overrides: list[NotificationSettings] = []
    app = SimpleNamespace(override_notification_settings=overrides.append)
    monkeypatch.setattr(screen_module.MainScreen, "app", property(lambda _self: app))
    return overrides


@pytest.mark.asyncio
async def test_main_screen_debounces_notification_settings_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui.screens.main import screen as screen_module

    calls = _stub_settings_persist(monkeypatch)
    overrides = _stub_overlay_app(monkeypatch, screen_module)
    monkeypatch.setattr(screen_module, "SETTINGS_SAVE_DELAY_SECONDS", 0)

    screen = object.__new__(screen_module.MainScreen)

    first = NotificationSettings(enabled=False)
    second = NotificationSettings(enabled=True)

    screen._schedule_notification_settings_save(first)
    screen._schedule_notification_settings_save(second)
    task = screen._settings_persistence().save_task
    assert task is not None

    await task

    # Both edits merged into one write carrying the later values.
    assert calls == [(second.to_settings_patch(), (), None)]
    # The overlay is not debounced: every edit is live the moment it is made,
    # which is what a reload landing inside the window reapplies.
    assert overrides == [first, second]


@pytest.mark.asyncio
async def test_main_screen_flushes_pending_notification_settings_without_debounce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.app.tui.screens.main import screen as screen_module

    calls = _stub_settings_persist(monkeypatch)
    _stub_overlay_app(monkeypatch, screen_module)
    monkeypatch.setattr(screen_module, "SETTINGS_SAVE_DELAY_SECONDS", 60)

    screen = object.__new__(screen_module.MainScreen)

    settings = NotificationSettings(enabled=False)
    screen._schedule_notification_settings_save(settings)

    await screen._flush_settings_save()

    assert calls == [
        (settings.to_settings_patch(), (), screen_module.SETTINGS_FLUSH_LOCK_TIMEOUT_SECONDS),
    ]


def test_a_settings_reload_cannot_revert_a_pending_notification_edit() -> None:
    """The write is debounced, so the handle is the only record of a fresh edit."""
    from chrys.app.tui.app import ChrysApp
    from chrys.foundation.config.settings_store import SettingsHandle, load_settings

    projected: list[NotificationSettings] = []
    before = load_settings()
    app = object.__new__(ChrysApp)
    app._settings_handle = SettingsHandle(before)
    app._notification_service = SimpleNamespace(update_settings=projected.append)

    edited = NotificationSettings(enabled=False, desktop=False)
    app.override_notification_settings(edited)
    # A reload lands inside the debounce window and installs the pre-edit
    # document; re-projecting it would otherwise put the old values straight
    # back over the delivery service.
    app._settings_handle.install(before)
    app.refresh_notification_settings()

    assert projected[-1].enabled is False
    assert projected[-1].desktop is False


def test_notification_service_scheduling_failure_returns_false() -> None:
    app = SimpleNamespace(bells=0)
    app.bell = lambda: setattr(app, "bells", app.bells + 1)
    settings = NotificationSettings()
    settings.events = None  # type: ignore[assignment]
    service = NotificationService(app, settings, _FakeNotificationDriver())

    assert service.notify(NotificationEvent.TURN_COMPLETE) is False


@pytest.mark.asyncio
async def test_notification_service_test_reports_delivery_failure() -> None:
    app = SimpleNamespace(bells=0)
    app.bell = lambda: setattr(app, "bells", app.bells + 1)
    driver = _FakeNotificationDriver(NotificationDeliveryResult(desktop_sent=False, sound_sent=False))
    service = NotificationService(app, NotificationSettings(), driver)
    settings = NotificationSettings(desktop=True, sound=False)

    assert await service.test(settings) is False
    assert app.bells == 0


@pytest.mark.asyncio
async def test_notification_service_test_reports_bell_fallback_success() -> None:
    app = SimpleNamespace(bells=0)
    app.bell = lambda: setattr(app, "bells", app.bells + 1)
    driver = _FakeNotificationDriver(NotificationDeliveryResult(desktop_sent=False, sound_sent=False))
    service = NotificationService(app, NotificationSettings(), driver)
    settings = NotificationSettings(desktop=False, sound=True)

    assert await service.test(settings) is True
    assert app.bells == 1


@pytest.mark.asyncio
async def test_notification_service_focus_gate_and_unknown_focus() -> None:
    app = SimpleNamespace(bells=0)
    app.bell = lambda: setattr(app, "bells", app.bells + 1)
    driver = _FakeNotificationDriver()
    service = NotificationService(app, NotificationSettings(), driver)

    assert service.notify(NotificationEvent.TURN_COMPLETE) is True
    await wait_for(lambda: len(driver.payloads) == 1, interval=0, description="first payload delivered")
    assert [p.body for p in driver.payloads] == ["Agent finished"]

    service.set_focused(True)
    assert service.notify(NotificationEvent.TURN_COMPLETE) is False
    await asyncio.sleep(0)
    assert len(driver.payloads) == 1

    service.set_focused(False)
    assert service.notify(NotificationEvent.TURN_COMPLETE) is True
    await wait_for(lambda: len(driver.payloads) == 2, interval=0, description="second payload delivered")
    assert len(driver.payloads) == 2


@pytest.mark.asyncio
async def test_notification_service_uses_app_bell_fallback() -> None:
    app = SimpleNamespace(bells=0)
    app.bell = lambda: setattr(app, "bells", app.bells + 1)
    driver = _FakeNotificationDriver(NotificationDeliveryResult(desktop_sent=True, sound_sent=False))
    service = NotificationService(app, NotificationSettings(), driver)

    assert service.notify(NotificationEvent.TURN_ERROR) is True
    await wait_for(lambda: app.bells == 1, interval=0, description="bell fallback rang")
    assert app.bells == 1


async def test_default_test_notifications_are_recorded_without_native_delivery_or_bells(
    notification_driver: RecordingNotificationDriver, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from unittest.mock import create_autospec

    from tests.support.tui_app_harness import make_chrys_app

    app = make_chrys_app(tmp_path / "sessions")
    bell = create_autospec(app.bell, side_effect=AssertionError("test notifications must not ring the bell"))
    monkeypatch.setattr(app, "bell", bell)
    service = app.notification_service
    service.update_settings(NotificationSettings(enabled=True, desktop=True, sound=True, suppress_when_focused=False))
    assert service.notify(NotificationEvent.TURN_COMPLETE)
    await wait_for(lambda: len(notification_driver.payloads) == 1)
    payload = notification_driver.payloads[0]
    assert payload.body == "Agent finished"
    assert payload.desktop and payload.sound
    assert await service.test()
    assert len(notification_driver.payloads) == 2
    bell.assert_not_called()


@pytest.mark.asyncio
async def test_macos_sound_only_uses_system_sound_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    calls: list[list[str]] = []
    sound_file = tmp_path / "Glass.aiff"
    sound_file.write_bytes(b"sound")
    monkeypatch.setattr(notification_drivers, "_MACOS_SOUND_PATH", sound_file)

    async def run_command(argv: list[str], *, timeout: float = 3.0) -> bool:
        calls.append(argv)
        return True

    monkeypatch.setattr(notification_drivers, "_run_command", run_command)

    result = await MacOSNotificationDriver().send(
        NotificationPayload(title=APP_DISPLAY_NAME, body="Agent finished", desktop=False)
    )

    assert result.desktop_sent is False
    assert result.sound_sent is True
    assert calls == [["/usr/bin/afplay", str(sound_file)]]


def _make_approval_handler(monkeypatch: pytest.MonkeyPatch) -> tuple[BackendEventHandler, _FakeApp]:
    import chrys.app.tui.screens.dialogs.approval as approval_module

    monkeypatch.setattr(approval_module, "ApprovalDialog", _FakeApprovalDialog)

    app = _FakeApp()
    screen = SimpleNamespace(
        app=app,
        _debug=lambda *_args: None,
        _handle_approval_response=lambda *_args: None,
        run_worker=lambda *_args, **_kwargs: None,
    )
    handler = make_backend_handler(screen)
    handler._approval_queue = deque()
    handler._approval_dialog_open = False
    handler._approval_bodies = {}
    handler._open_approval_dialogs = {}
    handler._pending_verdicts = {}
    handler._dismissed_approval_requests = set()
    handler._reviewed_dismissed_approval_requests = set()
    return handler, app


def _approval_request(request_id: str, *, judging: bool) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=request_id,
        caller_name="",
        tool_name="zsh",
        tool_kind="shell",
        args={"command": "rm file"},
        judging=judging,
    )


async def test_manual_approval_notifies_when_dialog_is_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, app = _make_approval_handler(monkeypatch)
    handler._approval_queue.append(_approval_request("r1", judging=False))

    handler._show_next_approval()

    assert app.notification_service.events == [NotificationEvent.APPROVAL_REQUIRED]


def test_auto_approval_does_not_notify_while_judging(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, app = _make_approval_handler(monkeypatch)
    handler._approval_queue.append(_approval_request("r1", judging=True))

    handler._show_next_approval()

    assert app.notification_service.events == []


def test_backend_notification_dispatch_failure_is_nonfatal() -> None:
    class _FailingNotificationService:
        def notify(self, _event: NotificationEvent) -> bool:
            raise RuntimeError("notification failed")

    screen = SimpleNamespace(app=SimpleNamespace(notification_service=_FailingNotificationService()))
    handler = make_backend_handler(screen)

    handler._notify(NotificationEvent.TURN_COMPLETE)


@pytest.mark.asyncio
async def test_auto_approval_notifies_when_live_judge_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, app = _make_approval_handler(monkeypatch)
    handler._approval_queue.append(_approval_request("r1", judging=True))
    handler._show_next_approval()

    await handler.on_approval_reviewed(ApprovalReviewed(request_id="r1", approved=False, reason="risky"))

    assert app.notification_service.events == [NotificationEvent.APPROVAL_REQUIRED]


async def test_auto_approval_cached_flagged_verdict_notifies_when_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    handler, app = _make_approval_handler(monkeypatch)
    handler._approval_queue.append(_approval_request("r1", judging=True))
    handler._pending_verdicts["r1"] = ApprovalReviewed(request_id="r1", approved=False, reason="risky")

    handler._show_next_approval()

    assert app.notification_service.events == [NotificationEvent.APPROVAL_REQUIRED]


@pytest.mark.asyncio
async def test_ask_user_notifies_when_dialog_is_shown() -> None:
    app = _FakeApp()
    screen = SimpleNamespace(
        app=app,
        _handle_ask_user_response=lambda *_args: None,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._question_queue = deque()
    handler._question_dialog_open = False
    handler._open_question_dialogs = {}
    handler._inline_question_call_ids = {}
    handler._inline_question_request_ids = {}
    handler._question_drafts = {}

    await handler.on_question_to_user(
        QuestionToUser(request_id="q1", questions=(AskUserQuestion(question="Need input?"),))
    )

    assert app.notification_service.events == [NotificationEvent.ASK_USER]


@pytest.mark.asyncio
async def test_final_agent_message_notifies_only_for_live_run() -> None:
    app = _FakeApp()

    class _Panel:
        async def add_agent_message(self, *_args: object, **_kwargs: object) -> None:
            return

    class _StatusBar:
        def flash_completed(self) -> None:
            self.flash("Completed in 1s")

        def flash(self, *_args: object, **_kwargs: object) -> None:
            return

    def query_one(cls: type) -> object:
        if cls.__name__ == "ChatPanel":
            return _Panel()
        if cls.__name__ == "StatusBar":
            return _StatusBar()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=app,
        _state=MainScreenState(run=RunState(agent_running=True)),
        _session_title=fake_session_title(),
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)

    await handler.on_agent_message(
        InvocationMessage(text="done", is_final=True, origin=InvocationOrigin("turn", "", "turn-test", None))
    )
    await handler.on_agent_message(
        InvocationMessage(text="stale", is_final=True, origin=InvocationOrigin("turn", "", "turn-test", None))
    )

    assert app.notification_service.events == [NotificationEvent.TURN_COMPLETE]


@pytest.mark.asyncio
async def test_error_notifies_only_for_live_turn_errors() -> None:
    app = _FakeApp()

    class _StatusBar:
        def flash(self, *_args: object, **_kwargs: object) -> None:
            return

    class _InputBar:
        locked = False
        _retry_label = ""
        retry_mode = False

    class _ChatPanel:
        async def add_error(self, *_args: object, **_kwargs: object) -> None:
            return

    def query_one(cls: type) -> object:
        if cls.__name__ == "StatusBar":
            return _StatusBar()
        if cls.__name__ == "InputBar":
            return _InputBar()
        if cls.__name__ == "ChatPanel":
            return _ChatPanel()
        raise AssertionError(f"unexpected query_one({cls.__name__})")

    screen = SimpleNamespace(
        app=app,
        _state=MainScreenState(run=RunState(agent_running=True)),
        _session_title=fake_session_title(),
        query_one=query_one,
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None
    handler._agent_load_status_snapshot = None

    await handler.on_error(Error(code="executor_error", message="boom"))
    await handler.on_error(Error(code="executor_error", message="stale"))

    assert app.notification_service.events == [NotificationEvent.TURN_ERROR]


@pytest.mark.asyncio
async def test_session_in_use_error_does_not_notify() -> None:
    app = _FakeApp()
    screen = SimpleNamespace(
        app=app,
        _state=MainScreenState(run=RunState(agent_running=True)),
        _debug=lambda *_args: None,
    )
    handler = make_backend_handler(screen)
    handler._agent_load_dialog = None

    await handler.on_error(Error(code="session_in_use", message="already open"))

    assert app.notification_service.events == []
