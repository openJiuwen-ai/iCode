# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locale regressions for transient TUI notifications and desktop toasts."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController, render_str
from chrys.app.tui.notifications.drivers import NotificationDeliveryResult, NotificationPayload
from chrys.app.tui.notifications.service import NotificationService
from chrys.app.tui.notifications.settings import NotificationSettings
from chrys.app.tui.screens.main.commands import MainSlashCommandRegistry, SlashCommandActions
from chrys.app.tui.screens.main.copy_actions import CopyActionController
from chrys.app.tui.screens.main.rollback_controller import RollbackController
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.screens.settings.panes.notifications import NotificationsPane
from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.widgets import Checkbox
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import ApprovalModeUpdated, Error, RollbackResult, Warning
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.orchestration.engine.session_lifecycle import _FORK_TURN_ACTIVE
from chrys.orchestration.engine.state.controls import _CONTROLS_NO_REGISTRY
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations.types import RestoreOutcome, RestoreResult
from tests.support.tui_helpers import make_backend_handler


def _controller(locale: str) -> LocaleController:
    return LocaleController(Settings(locale=locale))


def _display(controller: LocaleController | None, message: MessageRef | str) -> str:
    if isinstance(message, str):
        return message
    return format_message(message) if controller is None else render_str(controller.localizer, message)


class _CopyView:
    def __init__(self, controller: LocaleController | None) -> None:
        self.controller = controller
        self.messages = [("assistant", "first"), ("assistant", "second")]
        self.notifications: list[tuple[str, str]] = []

    def notify(self, message: MessageRef | str, *, title: MessageRef | str, **_kwargs: object) -> None:
        self.notifications.append((_display(self.controller, message), _display(self.controller, title)))

    def chat_copy_messages(self, _target: str) -> list[tuple[str, str]]:
        return list(self.messages)

    def copy_text(self, _text: str) -> None:
        return

    def toggle_chat_fold_all(self) -> bool:
        return False


def test_copy_toast_plural_renders_english_and_chinese_through_controller() -> None:
    english_view = _CopyView(None)
    english = CopyActionController(view=cast(Any, english_view), debug=lambda *_args: None)
    english.copy_agent_responses("1")
    english.copy_agent_responses("2")
    assert english_view.notifications == [
        ("Copied 1 response", "Copied"),
        ("Copied 2 responses", "Copied"),
    ]

    chinese_view = _CopyView(_controller("zh-Hans"))
    chinese = CopyActionController(view=cast(Any, chinese_view), debug=lambda *_args: None)
    chinese.copy_agent_responses("1")
    chinese.copy_agent_responses("2")
    assert chinese_view.notifications == [
        ("已复制 1 条回复", "已复制"),
        ("已复制 2 条回复", "已复制"),
    ]


def _slash_actions(warn: Any) -> SlashCommandActions:
    def noop(*_args: object, **_kwargs: object) -> None:
        return

    return SlashCommandActions(
        list_themes=list,
        get_theme=str,
        apply_theme=noop,
        pick_theme=noop,
        list_languages=list,
        get_language=str,
        apply_language=noop,
        pick_language=noop,
        render_unknown_language_warning=str,
        debug_event=noop,
        new_session=noop,
        clear_session=noop,
        quit_app=noop,
        resume_session=noop,
        fork_session=noop,
        ask_side_question=lambda _question: None,
        workflow_selection=lambda: None,
        open_guide=lambda: None,
        browse_session_list=noop,
        edit_session_title=noop,
        apply_session_title=noop,
        change_directory=noop,
        copy_conversation=noop,
        fold_tools=noop,
        open_diff=noop,
        open_rollback=noop,
        get_approval_mode=lambda: "manual",
        change_approval_mode=noop,
        configure_model=noop,
        configure_agent=noop,
        configure_agent_tab=noop,
        show_runtime_details=noop,
        show_usage=noop,
        configure_settings=lambda _tab: None,
        show_manual_pages=noop,
        warn=warn,
    )


def test_unknown_command_warning_renders_chinese_title_and_message() -> None:
    controller = _controller("zh-Hans")
    notifications: list[tuple[str, str]] = []

    def warn(message: MessageRef | str, title: MessageRef | str, _timeout: float | None) -> None:
        notifications.append((_display(controller, message), _display(controller, title)))

    buddy = SimpleNamespace(subcommands=list, handle=lambda _arg: None)
    commands = MainSlashCommandRegistry(actions=_slash_actions(warn), buddy=cast(Any, buddy)).build()
    next(command for command in commands if command.name == "agents").action("missing")
    next(command for command in commands if command.name == "man").action("missing")

    assert notifications == [
        ("未知的 /agents 目标：missing", "无效命令"),  # noqa: RUF001
        ("未知命令：/missing", "手册页"),  # noqa: RUF001
    ]


class _NotificationDriver:
    def __init__(self) -> None:
        self.payloads: list[NotificationPayload] = []

    async def send(self, payload: NotificationPayload) -> NotificationDeliveryResult:
        self.payloads.append(payload)
        return NotificationDeliveryResult(desktop_sent=True, sound_sent=False)


@pytest.mark.asyncio
async def test_notification_service_renders_at_send_time_with_optional_controller() -> None:
    settings = NotificationSettings(desktop=True, sound=False)
    app = SimpleNamespace(bell=lambda: None)

    chinese_driver = _NotificationDriver()
    chinese = NotificationService(
        cast(Any, app),
        settings,
        chinese_driver,
        locale_controller=_controller("zh-Hans"),
    )
    assert await chinese.test(settings) is True
    assert chinese_driver.payloads[-1].body == "智能体已完成"

    english_driver = _NotificationDriver()
    english = NotificationService(cast(Any, app), settings, english_driver)
    assert await english.test(settings) is True
    assert english_driver.payloads[-1].body == "Agent finished"


class _PanePorts:
    def current(self) -> NotificationSettings:
        return NotificationSettings()

    def save(self, settings: NotificationSettings) -> bool:
        return True

    async def test(self, settings: NotificationSettings) -> bool:
        return True


class _PaneHost(TuiVariableDefaultsMixin, App[None]):
    def __init__(self, controller: LocaleController) -> None:
        super().__init__()
        self.locale_controller = controller
        self.pane = NotificationsPane(_PanePorts())

    def compose(self) -> ComposeResult:
        yield self.pane


@pytest.mark.asyncio
async def test_notification_event_labels_and_descriptions_paint_in_chinese() -> None:
    app = _PaneHost(_controller("zh-Hans"))
    async with app.run_test() as pilot:
        await pilot.pause()
        pane = app.pane

        assert pane.query_one("#notifications-event-approval_required", Checkbox).label.plain == "需要审批"
        assert pane.query_one("#notifications-event-ask_user", Checkbox).label.plain == "智能体需要输入"
        descriptions = [
            widget.content.plain for widget in pane.query(".notification-event-description").results(Static)
        ]
        assert descriptions[:2] == ["当工具审批需要你决定时通知", "当智能体提出问题时通知"]


class _RollbackView:
    def __init__(self, controller: LocaleController | None) -> None:
        self.controller = controller
        self.notifications: list[str] = []

    def notify(self, message: MessageRef | str, *, title: MessageRef | str, **_kwargs: object) -> None:
        self.notifications.append(_display(self.controller, message))

    async def restore_welcome_rollback(self, **_kwargs: object) -> None:
        return

    def restore_input_text(self, _text: str) -> None:
        return


async def _rollback_message(count: int, locale: str | None) -> str:
    controller = None if locale is None else _controller(locale)
    view = _RollbackView(controller)
    rollback = RollbackController(
        services=MainScreenServices(bus=EventBus()),
        view=cast(Any, view),
        workspace_cwd=lambda: "/workspace",
        is_agent_busy=lambda: False,
        current_session_id=lambda: "session",
        session_generation=lambda: 1,
        turn_lifecycle_task=lambda: None,
        profile_name=lambda: "Code",
        reset_welcome_workspace_marker=lambda _cwd: None,
        set_has_messages=lambda _value: None,
        post_gc_message=lambda _message: None,
        debug=lambda *_args: None,
        locale_controller=controller,
    )
    results = [RestoreResult(path=f"/workspace/{index}.txt", outcome=RestoreOutcome.APPLIED) for index in range(count)]
    await rollback.on_result(
        RollbackResult(
            session_id="session",
            target_turn=1,
            files_reverted=count,
            restore_results=results,
        )
    )
    return view.notifications[-1]


@pytest.mark.asyncio
async def test_rollback_file_count_plural_renders_english_and_chinese() -> None:
    assert "1 file restored" in await _rollback_message(1, None)
    assert "2 files restored" in await _rollback_message(2, None)
    assert "已还原 1 个文件" in await _rollback_message(1, "zh-Hans")
    assert "已还原 2 个文件" in await _rollback_message(2, "zh-Hans")


class _SessionForkCapture:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def on_session_fork_error(self, _event: object, *, message: str, severity: str) -> None:
        self.calls.append((message, severity))


def _backend_screen(notifications: list[tuple[str, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        _debug=lambda *_args: None,
        header_approval_mode=ApprovalMode.MANUAL,
        _sessions=_SessionForkCapture(),
        notify=lambda message, *, title, severity="information", timeout=3, markup=False: notifications.append(
            (message, title)
        ),
    )


def test_approval_mode_toast_renders_localized_mode_label() -> None:
    notifications: list[tuple[str, str]] = []
    screen = _backend_screen(notifications)
    handler = make_backend_handler(screen, locale_controller=_controller("zh-Hans"))

    asyncio.run(handler.on_approval_mode_updated(ApprovalModeUpdated(mode=ApprovalMode.AUTO.value)))

    assert notifications == [("审批模式：自动", "审批")]  # noqa: RUF001


def test_backend_warning_toast_prefers_display_message() -> None:
    notifications: list[tuple[str, str]] = []
    screen = _backend_screen(notifications)
    handler = make_backend_handler(screen, locale_controller=_controller("zh-Hans"))

    asyncio.run(
        handler.on_warning(
            Warning(
                code="profile_switch_unavailable",
                message="No profile registry configured — cannot switch profiles",
                display_message=_CONTROLS_NO_REGISTRY.bind(),
            )
        )
    )

    assert notifications == [("未配置智能体配置注册表，无法切换智能体配置", "警告")]  # noqa: RUF001


def test_backend_warning_deduplication_is_scoped_to_the_session() -> None:
    notifications: list[tuple[str, str]] = []
    screen = _backend_screen(notifications)
    handler = make_backend_handler(screen)
    warning = {
        "code": "trajectory_activation_failed",
        "message": "Trajectory recording could not start and has been disabled for this session.",
    }

    asyncio.run(handler.on_warning(Warning(session_id="session-a", **warning)))
    asyncio.run(handler.on_warning(Warning(session_id="session-a", **warning)))
    asyncio.run(handler.on_warning(Warning(session_id="session-b", **warning)))

    assert notifications == [
        (warning["message"], "Warning"),
        (warning["message"], "Warning"),
    ]


def test_session_fork_error_renders_display_message() -> None:
    notifications: list[tuple[str, str]] = []
    screen = _backend_screen(notifications)
    handler = make_backend_handler(screen, locale_controller=_controller("zh-Hans"))

    asyncio.run(
        handler.on_error(
            Error(
                code="session_fork_turn_active",
                message="Cannot fork while a turn is running.",
                display_message=_FORK_TURN_ACTIVE.bind(),
            )
        )
    )

    assert screen._sessions.calls == [("轮次进行中无法派生会话。", "warning")]
