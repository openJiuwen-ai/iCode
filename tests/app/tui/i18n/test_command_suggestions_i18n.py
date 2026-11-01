# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Locale coverage for slash-command descriptions and completion labels."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from textual.content import Content

from chrys.app.features.buddy import actions as buddy_actions
from chrys.app.features.buddy import commands as buddy_commands
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs import man_page as man_page_module
from chrys.app.tui.screens.dialogs.man_page import ManPageDialog
from chrys.app.tui.screens.main.buddy_command import BuddyCommandController
from chrys.app.tui.screens.main.commands import MainSlashCommandRegistry
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.main.suggestions import SuggestionCallbacks, SuggestionHandler
from chrys.app.tui.widgets.chrome.commands import ManPageSpec
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.i18n.formatting import format_message


def _suggestion_handler(locale_controller: LocaleController | None = None) -> SuggestionHandler:
    return SuggestionHandler(
        state=MainScreenState(),
        services=MainScreenServices(bus=EventBus()),
        view=MagicMock(),
        command_actions=MagicMock(),
        callbacks=SuggestionCallbacks(
            notify_warning=MagicMock(),
            start_worker=MagicMock(),
            submit_user_text=MagicMock(),
            start_agent_profile_switch=MagicMock(),
            start_model_profile_switch=MagicMock(),
        ),
        buddy_view=MagicMock(),
        locale_controller=locale_controller,
    )


def _label_text(label: str | Content) -> str:
    return label if isinstance(label, str) else label.plain


def test_command_description_suggestions_render_current_locale() -> None:
    english_handler = _suggestion_handler()
    english_command = english_handler.build_slash_commands()[0]
    english_item = english_handler._command_suggestion_items([english_command], set())[0]
    assert english_item.label.plain == "/new  Start a new session"

    controller = LocaleController(Settings(locale="en"))
    localized_handler = _suggestion_handler(controller)
    localized_command = localized_handler.build_slash_commands()[0]
    controller.localizer.switch_locale("zh-Hans")
    localized_item = localized_handler._command_suggestion_items([localized_command], set())[0]
    assert localized_item.label.plain == "/new  开始新会话"
    assert format_message(localized_command.description) == "Start a new session"


def test_completion_labels_localize_without_changing_values() -> None:
    localizer = Localizer("zh-Hans")
    actions = MagicMock()
    actions.current_approval_mode.return_value = "manual"
    buddy = BuddyCommandController(MagicMock(), render_message=localizer.render)
    commands = MainSlashCommandRegistry(
        actions=actions,
        buddy=buddy,
        render_message=localizer.render,
    ).build()

    approval = next(command for command in commands if command.name == "approval")
    assert approval.subcommands is not None
    approval_items = approval.subcommands()
    assert [value for value, _label in approval_items] == ["manual", "auto", "bypass"]
    assert [label.plain for _value, label in approval_items] == [
        "● Manual  逐一批准需要审批的调用",
        "  Auto  自动批准安全调用，并标记可疑调用",  # noqa: RUF001
        "  Bypass  所有工具调用均无需审批即可运行",
    ]

    agents = next(command for command in commands if command.name == "agents")
    assert agents.subcommands is not None
    agent_items = agents.subcommands()
    assert [value for value, _label in agent_items] == [
        "basic",
        "instructions",
        "tools",
        "sub-agents",
        "skills",
        "mcp",
        "memory",
        "compaction",
    ]
    assert [_label_text(label) for _value, label in agent_items] == [
        "打开基本设置",
        "打开提示词编辑器",
        "打开工具设置",
        "打开子智能体设置",
        "打开 Skills 设置",
        "打开 MCP 服务器设置",
        "打开记忆设置",
        "打开压缩设置",
    ]

    man = next(command for command in commands if command.name == "man")
    assert man.subcommands is not None
    man_items = man.subcommands()
    assert [value for value, _label in man_items] == [command.name for command in commands]
    assert _label_text(man_items[0][1]) == "显示 /new 的帮助"

    buddy_command = next(command for command in commands if command.name == "buddy")
    assert buddy_command.subcommands is not None
    assert buddy_command.subcommands() == [("hatch", "孵化新伙伴")]
    buddy_actions.hatch()
    assert buddy_command.subcommands() == [
        ("info", "显示伙伴信息"),
        ("pet", "抚摸你的伙伴"),
        ("mute", "切换伙伴通知"),
        ("name", "重命名伙伴"),
    ]


def test_manual_pages_render_english_byte_identically_and_translate_at_display(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    localizer = Localizer("zh-Hans")
    actions = MagicMock()
    captured: list[tuple[list[ManPageSpec], int]] = []

    def capture_pages(pages: list[ManPageSpec], *, start_index: int = 0) -> None:
        captured.append((pages, start_index))

    actions.show_man_pages.side_effect = capture_pages
    registry = MainSlashCommandRegistry(
        actions=actions,
        buddy=BuddyCommandController(MagicMock(), render_message=localizer.render),
        render_message=localizer.render,
    )
    commands = registry.build()
    man = next(command for command in commands if command.name == "man")

    man.action("")
    index_page = captured[-1][0][0]
    monkeypatch.setattr(man_page_module, "widget_localizer", lambda _widget: Localizer("en"))
    index_content = ManPageDialog([index_page])._render_page(index_page)
    assert index_content == (
        "NAME\n"
        "    iCode - AI-powered code assistant\n"
        "\n"
        "DESCRIPTION\n"
        "    iCode is a terminal-based AI assistant for code exploration,\n"
        "    analysis, and understanding.\n"
        "\n"
        "AVAILABLE COMMANDS\n"
        "  /new          - Start a new session\n"
        "  /clear        - Delete the current session and start a new one\n"
        "  /exit         - Exit iCode\n"
        "  /resume       - Resume the most recent session in this mode\n"
        "  /fork         - Fork the current session\n"
        "  /rename       - Set or clear a custom session title\n"
        "  /sessions     - Browse saved sessions\n"
        "  /theme        - Set color theme\n"
        "  /language     - Set display language\n"
        "  /chdir        - Change working directory\n"
        "  /copy         - Copy agent, user, or all turns to clipboard\n"
        "  /fold         - Toggle collapse on activity, tool groups and thinking\n"
        "  /diff         - View file changes for the current session\n"
        "  /rollback     - Discard recent turns or return to a specific turn\n"
        "  /approval     - Switch approval mode: manual → auto → bypass\n"
        "  /models       - Configure model provider and settings\n"
        "  /buddy        - Hatch, pet and look after your buddy\n"
        "  /agents       - Manage agent configs\n"
        "  /runtime      - Show active model, tools, skills, and files\n"
        "  /usage        - Show token usage and speed across all sessions\n"
        "  /settings     - Open the Settings panel\n"
        "  /workflow     - Open Workflow mode and select a workflow\n"
        "  /help         - Open the user guide\n"
        "  /man          - Show manual page for a command\n"
        "\n"
        "SEE ALSO\n"
        "    /man <command>  Show detailed help for a specific command\n"
    )
    assert index_content.splitlines()[8] == "  /new          - Start a new session"

    man.action("new")
    pages, start_index = captured[-1]
    new_page = pages[start_index]
    new_content = ManPageDialog(pages, start_index=start_index)._render_page(new_page)
    assert new_content == (
        "NAME\n"
        "    /new - Start a new session\n"
        "\n"
        "SYNOPSIS\n"
        "    /new\n"
        "\n"
        "DESCRIPTION\n"
        "    Start a completely new iCode session.\n"
        "\n"
        "    This clears the current conversation context and begins fresh.\n"
        "    Use this when you want to work on a new task without\n"
        "    carrying over previous context.\n"
        "\n"
        "ALIASES\n"
        "    none\n"
        "\n"
        "OPTIONS\n"
        "    This command does not take additional options.\n"
    )
    assert new_content.splitlines()[:2] == ["NAME", "    /new - Start a new session"]

    monkeypatch.setattr(man_page_module, "widget_localizer", lambda _widget: localizer)
    localized_index = ManPageDialog([index_page])._render_page(index_page)
    localized_new = ManPageDialog(pages, start_index=start_index)._render_page(new_page)
    assert "可用命令" in localized_index
    assert "  /new          - 开始新会话" in localized_index
    assert "开始一个全新的 iCode 会话。" in localized_new


def test_buddy_command_messages_render_localized_with_legacy_english_fallback() -> None:
    localizer = Localizer("zh-Hans")

    intro, intro_severity = buddy_commands.handle_buddy_command(None)
    assert isinstance(intro, MessageRef)
    assert intro_severity == "information"
    assert format_message(intro) == (
        "🥚 There is an egg here, and nobody knows what is in it.\n\n/buddy hatch finds out."
    )
    assert localizer.render(intro) != format_message(intro)

    hatched, hatch_severity = buddy_commands.handle_buddy_command("hatch")
    buddy = buddy_actions.current_buddy()
    assert buddy is not None
    assert isinstance(hatched, MessageRef)
    assert hatch_severity == "information"
    info = buddy_commands.buddy_card(buddy)
    assert format_message(hatched) == (
        f"🐣 Out of the egg:\n\n{info}\n\nFrom now on it lives in the sidebar, on the Buddy tab."
    )
    assert info in localizer.render(hatched)
    assert localizer.render(hatched) != format_message(hatched)

    with_buddy, _severity = buddy_commands.handle_buddy_command(None)
    assert isinstance(with_buddy, MessageRef)
    assert format_message(with_buddy) == (
        f"🐾 {buddy.name} is keeping you company.\n\n/buddy info shows how it is doing, /buddy pet gives it a pat."
    )
    assert buddy.name in localizer.render(with_buddy)
    assert localizer.render(with_buddy) != format_message(with_buddy)
