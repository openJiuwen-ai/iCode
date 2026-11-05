# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Simplified-Chinese regressions for transient modal presentation surfaces."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from rich.cells import cell_len
from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.i18n import LocaleController, render_str
from chrys.app.tui.screens.dialogs.approval.bodies.file_edit import _REPLACEMENTS
from chrys.app.tui.screens.dialogs.approval.dialog import ApprovalDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.man_page import ManPageDialog
from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
from chrys.app.tui.screens.main.commands import MainSlashCommandRegistry
from chrys.app.tui.screens.main.rollback_controller import _TARGET_SESSION_START, _TARGET_TURN, RollbackController
from chrys.app.tui.screens.main.state import MainScreenServices
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.app.tui.screens.themes.picker import _THEMES
from chrys.app.tui.widgets.chrome.commands import ManPageSpec, ManPageVerbatimBlock
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeDetails, RollbackResult, RuntimeModelDetails
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.tool_kinds import KIND_SHELL
from tests.support.sessions_browser import FakeSessionStore


def _controller(locale: str = "zh-Hans") -> LocaleController:
    return LocaleController(Settings(locale=locale))


def test_theme_picker_title_keeps_english_and_localizes_chinese() -> None:
    assert format_message(_THEMES.bind()) == "Themes"
    assert Localizer("zh-Hans").render(_THEMES.bind()) == "主题"


def test_rollback_debug_targets_keep_english_and_localize_chinese() -> None:
    assert format_message(_TARGET_SESSION_START.bind()) == "Session start"
    assert format_message(_TARGET_TURN.bind(turn=7)) == "Turn 7"
    chinese = Localizer("zh-Hans")
    assert chinese.render(_TARGET_SESSION_START.bind()) == "会话开始"
    assert chinese.render(_TARGET_TURN.bind(turn=7)) == "第 7 轮"


class _LocalizedHost(App[None]):
    def __init__(self, controller: LocaleController) -> None:
        self.locale_controller = controller
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Static("host")


class _RollbackView:
    def __init__(self, controller: LocaleController) -> None:
        self.controller = controller
        self.notifications: list[str] = []

    def notify(self, message: MessageRef | str, **_kwargs: object) -> None:
        self.notifications.append(
            message if isinstance(message, str) else render_str(self.controller.localizer, message)
        )


@pytest.mark.asyncio
async def test_rollback_exclusion_label_reaches_localized_toast_preview() -> None:
    controller = _controller()
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
        invalidate_file_index=lambda: None,
        locale_controller=controller,
    )

    await rollback.on_result(
        RollbackResult(
            session_id="session",
            target_turn=1,
            exclusions=[("/workspace/file.txt", "contested")],
        )
    )

    assert "file.txt（内部和外部均有修改）" in view.notifications[-1]  # noqa: RUF001


@pytest.mark.asyncio
async def test_runtime_details_localizes_labels_with_cell_aligned_values() -> None:
    controller = _controller()
    details = AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id="profile-1",
            name="Model Name",
            provider="openai",
            model_id="model-1",
            stream=True,
        )
    )
    dialog = RuntimeDetailsDialog(details)
    app = _LocalizedHost(controller)

    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await pilot.pause()

        section = dialog.query_one(".runtime-detail-section", Static)
        lines = section.content.plain.splitlines()
        profile_line = next(line for line in lines if "profile-1" in line)
        name_line = next(line for line in lines if "Model Name" in line)
        assert profile_line.startswith("配置 ID")
        assert name_line.startswith("名称")
        assert cell_len(profile_line[: profile_line.index("profile-1")]) == cell_len(
            name_line[: name_line.index("Model Name")]
        )


class _BracketLabelLocalizer(Localizer):
    def render(self, reference: MessageRef) -> str:
        if reference.definition.key == "tui.approval.presentation.run_command":
            return "运行[命令]"
        return super().render(reference)


@pytest.mark.asyncio
async def test_approval_presentation_label_keeps_localized_brackets_literal() -> None:
    controller = LocaleController(
        Settings(locale="zh-Hans"),
        localizer=_BracketLabelLocalizer("zh-Hans"),
    )
    dialog = ApprovalDialog(
        caller_name="",
        tool_name="shell",
        presentation_kind=KIND_SHELL,
    )
    app = _LocalizedHost(controller)

    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await pilot.pause()

        assert dialog.query_one("#approval-tool", Static).render().plain == " 运行[命令] "


@pytest.mark.asyncio
async def test_man_page_footer_paints_in_chinese() -> None:
    dialog = ManPageDialog(
        [
            ManPageSpec("help", (ManPageVerbatimBlock("body", indent=0),)),
            ManPageSpec("models", (ManPageVerbatimBlock("body", indent=0),)),
        ]
    )
    app = _LocalizedHost(_controller())

    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await pilot.pause()

        footer = dialog.query_one("#man-footer", Static)
        assert footer.render().plain == "第 1/2 页  ↑ 上一页 ↓ 下一页  Esc 关闭"


@pytest.mark.asyncio
async def test_man_examples_localize_explanations_with_verbatim_syntax(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui import i18n as tui_i18n

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = _controller("en")
    actions = MagicMock()
    actions.current_approval_mode.return_value = "manual"
    commands = MainSlashCommandRegistry(actions=actions, buddy=MagicMock()).build()
    next(command for command in commands if command.name == "man").action("man")
    pages = actions.show_man_pages.call_args.args[0]
    start_index = actions.show_man_pages.call_args.kwargs["start_index"]
    dialog = ManPageDialog(pages, start_index=start_index, locale_controller=controller)
    app = _LocalizedHost(controller)

    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await pilot.pause()

        content = dialog.query_one("#man-content", Static)
        english = content.render().plain
        assert "/man              Show all commands" in english
        assert "/man theme        Show help for /theme" in english
        assert "/man diff         Show help for /diff" in english

        controller.switch_locale("zh-Hans")
        await pilot.pause()

        chinese = content.render().plain
        assert "/man              显示全部命令" in chinese
        assert "/man theme        显示 /theme 的帮助" in chinese
        assert "/man diff         显示 /diff 的帮助" in chinese


@pytest.mark.asyncio
async def test_man_page_retranslates_in_place_and_preserves_page(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.app.tui import i18n as tui_i18n

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = _controller("en")
    actions = MagicMock()
    actions.current_approval_mode.return_value = "manual"
    commands = MainSlashCommandRegistry(actions=actions, buddy=MagicMock()).build()
    next(command for command in commands if command.name == "man").action("exit")
    pages = actions.show_man_pages.call_args.args[0]
    start_index = actions.show_man_pages.call_args.kwargs["start_index"]
    assert start_index > 0
    dialog = ManPageDialog(pages, start_index=start_index, locale_controller=controller)
    app = _LocalizedHost(controller)

    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await pilot.pause()

        content = dialog.query_one("#man-content", Static)
        english = content.render().plain
        assert "Exit iCode and return to the terminal." in english
        assert str(dialog.query_one("#man-container").border_title) == "/exit"

        controller.switch_locale("zh-Hans")
        await pilot.pause()

        assert app.screen is dialog
        assert dialog._index == start_index
        assert "退出 iCode 并返回终端。" in content.render().plain
        footer = dialog.query_one("#man-footer", Static).render().plain
        assert footer.startswith(f"第 {start_index + 1}/{len(pages)} 页")
        assert str(dialog.query_one("#man-container").border_title) == "/exit"

        controller.switch_locale("en")
        await pilot.pause()

        assert app.screen is dialog
        assert dialog._index == start_index
        assert content.render().plain == english
        assert str(dialog.query_one("#man-container").border_title) == "/exit"


@pytest.mark.asyncio
async def test_sessions_delete_confirmation_reuses_localized_title_and_action() -> None:
    controller = _controller()
    screen = SessionsScreen(cast(Any, FakeSessionStore(0)), locale_controller=controller)
    screen._get_selected_session_id = lambda: "session-abc"  # type: ignore[method-assign]
    app = _LocalizedHost(controller)

    async with app.run_test() as pilot:
        await app.push_screen(screen)
        await pilot.pause()
        screen.action_delete_session()
        await pilot.pause()

        dialog = cast(ConfirmDialog, app.screen)
        assert str(dialog.query_one("#confirm-container").border_title) == "删除会话"
        assert dialog.query_one("#confirm-message", Static).render().plain == (
            "删除会话\n“sessionabc”？\n\n此操作无法撤销。"  # noqa: RUF001
        )
        assert dialog.query_one("#confirm-yes").label.plain == "删除"
        assert dialog.query_one("#confirm-no").label.plain == "取消"


def test_file_edit_replacement_plural_renders_english_and_chinese() -> None:
    english = Localizer("en")
    chinese = Localizer("zh-Hans")

    assert render_str(english, _REPLACEMENTS.bind(count=1)) == "1 replacement"
    assert render_str(english, _REPLACEMENTS.bind(count=2)) == "2 replacements"
    assert render_str(chinese, _REPLACEMENTS.bind(count=1)) == "1 处替换"
    assert render_str(chinese, _REPLACEMENTS.bind(count=2)) == "2 处替换"
