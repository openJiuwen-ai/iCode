# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for slash command suggestions and dispatch."""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual.content import Content

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.main.suggestions import SuggestionHandler
from chrys.app.tui.widgets.chrome.commands import (
    ManPageProseBlock,
    ManPageVerbatimBlock,
    is_slash_command_candidate,
)
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathSuggestion
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionItem
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import AgentRuntimeDetails, RuntimeSkillDetails
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.tui_helpers import (
    AgentProfileStub,
    AgentRegistryStub,
    SuggestionScreen,
    make_suggestion_handler,
    make_suggestion_screen,
    scan_result,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT


def test_slash_command_candidate_rejects_code_comments_paths_and_multiline_pastes() -> None:
    rejected = [
        "// comment",
        "/// doc comment",
        "/* block comment */",
        "/** jsdoc */",
        "/",
        "/ path-ish text",
        "/123",
        "/-flag",
        "/path/to/file",
        "/Users/foo",
        "/regex/i",
        "/help\nmore text",
        "  /help",
    ]

    for text in rejected:
        assert not is_slash_command_candidate(text)


def test_slash_command_candidate_accepts_command_shaped_input() -> None:
    assert is_slash_command_candidate("/help")
    assert is_slash_command_candidate("/help arg")
    assert is_slash_command_candidate("/chdir /Users/foo")
    assert is_slash_command_candidate("\uff0fhelp")


def test_suggestion_labels_style_second_separator_space_dim() -> None:
    """The dim description span starts at the second separator space, so a
    highlighted row's bright-to-gray reverse-video boundary sits between the
    two spaces instead of hugging the description's first letter."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    items = handler._command_suggestion_items(handler._visible_slash_commands(), set())
    label = items[0].label
    assert isinstance(label, Content)
    name_length = len(f"/{items[0].value}")
    assert label.plain[name_length : name_length + 2] == "  "
    assert any(span.start == name_length + 1 and "dim" in str(span.style) for span in label.spans)
    assert items[0].marquee_start == len(f"/{items[0].value}  ")

    skill_label = SuggestionHandler._runtime_skill_label("review", "Review changes")
    assert skill_label.plain == "/review  Review changes"
    assert any(span.start == len("/review ") and "dim" in str(span.style) for span in skill_label.spans)

    screen.services.agent_registry = AgentRegistryStub(
        [AgentProfileStub(name="QA", display_name="Q&A Agent", description="Read-only assistant")]
    )
    agent_items, _disabled = handler._get_agent_items()
    agent_label = agent_items[0].label
    assert isinstance(agent_label, Content)
    assert agent_label.plain == "  Q&A Agent  Read-only assistant"
    assert any(span.start == len("  Q&A Agent ") and "dim" in str(span.style) for span in agent_label.spans)
    assert agent_items[0].marquee_start == len("  Q&A Agent  ")

    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="fast", name="Fast Model", model_id="vendor/fast"))
    screen.services.model_registry = model_registry
    model_items, _disabled = handler._get_model_items()
    model_label = model_items[0].label
    assert isinstance(model_label, Content)
    assert model_label.plain == "  Fast Model  vendor/fast"
    assert any(span.start == len("  Fast Model ") and "dim" in str(span.style) for span in model_label.spans)
    assert model_items[0].marquee_start == len("  Fast Model  ")


def test_slash_suggestions_dismiss_for_non_command_paste() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()
    handler._suggestion_mode = "commands"

    handler.on_text_changed("// comment")

    assert handler.suggestion_mode is None


def test_multiline_side_question_dismisses_command_suggestions() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()
    handler._suggestion_mode = "commands"
    handler.on_text_changed("/btw\nquestion")
    assert handler.suggestion_mode is None
    assert is_slash_command_candidate("/btw\nquestion")


def test_build_slash_commands_uses_agents_and_no_legacy_entries() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    commands = handler.build_slash_commands()
    names = [c.name for c in commands]

    assert "agents" in names
    assert "runtime" in names
    assert "settings" in names
    assert "notifications" not in names
    assert "fork" in names
    assert "clear" in names
    assert "rename" in names
    assert "agent" not in names  # "agent" is now an alias, not a primary name
    assert "agent_config" not in names
    assert "mcp" not in names
    assert "skills" not in names
    language = next(command for command in commands if command.name == "language")
    assert language.synopsis == "/language [locale]"


def test_rename_command_opens_session_title_editor() -> None:
    """Bare /rename (or whitespace-only) is a second entry point to the
    border-click title dialog; /rename <title> applies directly."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    commands = handler.build_slash_commands()
    rename = next(command for command in commands if command.name == "rename")

    rename.action("")
    rename.action("   ")
    assert screen.title_editor_requests == 2
    assert screen.applied_titles == []

    rename.action("  Login bug fix  ")
    assert screen.applied_titles == ["Login bug fix"]
    assert screen.title_editor_requests == 2
    assert rename.man_page is not None


def test_runtime_config_commands_stay_available_but_settings_are_disabled_while_agent_runs() -> None:
    screen = make_suggestion_screen()
    screen.state.run.agent_running = True
    handler = make_suggestion_handler(screen)

    handler.build_slash_commands()

    assert "agents" not in handler._disabled_commands()
    assert "models" not in handler._disabled_commands()
    assert "settings" in handler._disabled_commands()


def test_copy_man_page_documents_role_filters() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    commands = handler.build_slash_commands()
    copy_command = next(command for command in commands if command.name == "copy")

    assert format_message(copy_command.description) == "Copy agent, user, or all turns to clipboard"
    assert copy_command.man_page is not None
    assert copy_command.synopsis is not None
    assert copy_command.options_help is not None
    assert isinstance(copy_command.man_page, tuple)
    copy_body = next(
        format_message(segment.message) for segment in copy_command.man_page if isinstance(segment, ManPageProseBlock)
    )
    examples = "\n".join(segment.text for segment in copy_command.man_page if isinstance(segment, ManPageVerbatimBlock))
    assert '"/copy agent N" is equivalent to "/copy N"' in copy_body
    assert '"/copy user N" copies the last N user turns' in copy_body
    assert "/copy agent all" in examples
    assert "/copy user all" in examples
    assert "/copy all" in examples
    assert "/copy agent [N|all]" in copy_command.synopsis
    assert [prefix for prefix, _reference in copy_command.options_help] == [
        "agent [N|all]  ",
        "user [N|all]   ",
        "all            ",
        "N              ",
    ]
    assert "Positive integer count" in format_message(copy_command.options_help[-1][1])


def test_rollback_man_page_documents_direct_relative_and_absolute_forms() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    commands = handler.build_slash_commands()
    rollback = next(command for command in commands if command.name == "rollback")

    assert format_message(rollback.description) == "Discard recent turns or return to a specific turn"
    assert rollback.subcommands is None
    assert rollback.synopsis is not None
    assert rollback.options_help is not None
    assert rollback.man_page is not None
    assert isinstance(rollback.man_page, MessageRef)
    man_page = " ".join(format_message(rollback.man_page).split())
    assert "/rollback N" in rollback.synopsis
    assert "/rollback to N" in rollback.synopsis
    assert [prefix for prefix, _reference in rollback.options_help] == ["N       ", "to N    "]
    assert "Positive number of most recent turns" in format_message(rollback.options_help[0][1])
    assert '"/rollback 1" discards the last turn' in man_page
    assert '"/rollback to 1"' in man_page
    assert "valid explicit count executes immediately" in man_page
    assert "restore eligible file changes by default" in man_page
    assert "discard conversation without restoring file changes" in man_page
    assert "non-dismissible loading" in man_page


def test_dispatch_agents_subcommands_route_to_expected_tabs() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/agents") is True
    assert handler.dispatch_slash_command("/agents basic") is True
    assert handler.dispatch_slash_command("/agents instructions") is True
    assert handler.dispatch_slash_command("/agents tools") is True
    assert handler.dispatch_slash_command("/agents sub-agents") is True
    assert handler.dispatch_slash_command("/agents subagents") is True
    assert handler.dispatch_slash_command("/agents mcp") is True
    assert handler.dispatch_slash_command("/agents memory") is True
    assert handler.dispatch_slash_command("/agents skill") is True
    assert handler.dispatch_slash_command("/agents skills") is True

    assert screen.opened == [
        "agent",
        "basic",
        "instructions",
        "tools",
        "sub-agents",
        "sub-agents",
        "mcp",
        "memory",
        "skills",
        "skills",
    ]


def test_agents_command_includes_memory_target_in_suggestions_and_man_page() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)

    commands = handler.build_slash_commands()
    agents_command = next(command for command in commands if command.name == "agents")

    assert agents_command.subcommands is not None
    assert ("memory", "Open Memory settings") in agents_command.subcommands()
    assert agents_command.man_page is not None
    assert isinstance(agents_command.man_page, MessageRef)
    assert "memory       - Configure memory files and folders" in format_message(agents_command.man_page)


def test_dispatch_agents_backward_compat_alias() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/agent") is True
    assert handler.dispatch_slash_command("/agent mcp") is True
    assert screen.opened == ["agent", "mcp"]


def test_dispatch_runtime_opens_details_for_agent_without_status_trail() -> None:
    from chrys.app.tui.screens.main.runtime_info import RegistryRuntimeInfoProvider

    screen = make_suggestion_screen()
    runtime_info = RegistryRuntimeInfoProvider(screen.services)
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert runtime_info.format_tool_info([], []) == ()
    assert handler.dispatch_slash_command("/runtime") is True
    assert handler.dispatch_slash_command("/details") is True
    assert screen.opened == ["runtime", "runtime"]


def test_dispatch_usage_opens_the_usage_dialog() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/usage") is True
    assert screen.opened == ["usage"]


def test_dispatch_fork_requests_session_fork() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/fork") is True
    assert screen.fork_requests == 1


def test_dispatch_clear_requests_confirmed_session_clear() -> None:
    """/clear routes to the screen's confirm-then-delete flow, distinct from /new."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/clear") is True
    assert screen.clear_requests == 1


def test_dispatch_settings_opens_the_panel_on_the_requested_tab() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/settings") is True
    assert handler.dispatch_slash_command("/settings sessions") is True
    assert handler.dispatch_slash_command("/settings notifications") is True
    assert screen.opened == ["settings:general", "settings:sessions", "settings:notifications"]

    assert handler.dispatch_slash_command("/settings bogus") is True
    assert screen.opened[-1] == "settings:notifications"
    assert screen.notifications[-1] == "Unknown /settings tab: bogus"


def test_dispatch_settings_is_rejected_while_agent_runs() -> None:
    screen = make_suggestion_screen()
    screen.state.run.agent_running = True
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/settings") is True
    assert screen.opened == []
    assert screen.notifications == ["/settings is not available while agent is running"]


def test_removed_notifications_commands_are_not_dispatched() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/notifications") is False
    assert handler.dispatch_slash_command("/notify") is False
    assert screen.opened == []


def test_dispatch_unknown_agents_subcommand_notifies_warning() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/agents unknown") is True
    assert screen.notifications == ["Unknown /agents target: unknown"]


def test_legacy_mcp_and_skills_commands_are_not_dispatched() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/mcp") is False
    assert handler.dispatch_slash_command("/skills") is False


def test_typing_space_switches_to_subcommand_mode_for_theme_and_agents() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    # Simulate active slash suggestion mode while user types command + space.
    handler._suggestion_mode = "commands"
    handler.on_text_changed("/theme ")
    assert handler.suggestion_mode == "subcommands"
    assert screen.suggestion_list.last_mode == "subcommands"

    handler._suggestion_mode = "commands"
    handler.on_text_changed("/agents ")
    assert handler.suggestion_mode == "subcommands"
    assert screen.suggestion_list.last_mode == "subcommands"


async def _finish_workers(screen: SuggestionScreen) -> None:
    """Await the shortcut reads opening or filtering the list started; a started worker is the test's to finish."""
    assert screen.workers
    for worker in screen.workers:
        await asyncio.wait_for(worker.wait(), ENGINE_TURN_TIMEOUT)


async def test_slash_suggestions_include_runtime_skills_in_separate_section() -> None:
    screen = make_suggestion_screen()
    screen.state.runtime.details = AgentRuntimeDetails(
        skill_details=[RuntimeSkillDetails(name="review", description="Review code and identify issues")]
    )
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    handler.on_slash_triggered()
    # Opening the list rereads prompt shortcut files on a worker: let it finish before reading the list.
    await _finish_workers(screen)

    skill_items = [
        item for item in screen.suggestion_list.last_items if isinstance(item, SuggestionItem) and item.kind == "skill"
    ]
    assert len(skill_items) == 1
    assert skill_items[0].value == "review"
    assert skill_items[0].section == "Loaded Skills"
    assert skill_items[0].marquee_start == len("/review  ")


async def test_slash_filter_matches_runtime_skills() -> None:
    screen = make_suggestion_screen()
    screen.state.runtime.details = AgentRuntimeDetails(
        skill_details=[RuntimeSkillDetails(name="review", description="Review code")]
    )
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()
    handler._suggestion_mode = "commands"

    handler.on_text_changed("/r")

    assert any(
        isinstance(item, SuggestionItem) and item.kind == "skill" and item.value == "review"
        for item in screen.suggestion_list.last_items
    )
    # Filtering before any shortcut read started one: finish it.
    await _finish_workers(screen)


async def test_shadowed_runtime_skill_is_visible_but_disabled() -> None:
    screen = make_suggestion_screen()
    screen.state.runtime.details = AgentRuntimeDetails(
        skill_details=[RuntimeSkillDetails(name="runtime", description="Runtime-like skill")]
    )
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    handler.on_slash_triggered()
    # Opening the list rereads prompt shortcut files on a worker: let it finish before reading the list.
    await _finish_workers(screen)

    skill = next(
        item
        for item in screen.suggestion_list.last_items
        if isinstance(item, SuggestionItem) and item.kind == "skill" and item.value == "runtime"
    )
    assert skill.disabled is True
    assert skill.disabled_reason == "shadowed by /runtime"


async def test_suggestion_chrome_renders_chinese_at_show_boundary_without_translating_payloads() -> None:
    """Chrome localizes at the show boundary; payloads stay verbatim.

    Localized descriptions and man-page bodies are covered by
    tests/app/tui/i18n/test_command_suggestions_i18n.py; this pins the other
    half — user-supplied titles, descriptions and paths are never translated
    and never re-parsed as markup.
    """
    screen = make_suggestion_screen()
    screen._set_workspace_cwd("/repo/[literal]")
    screen.services.agent_registry = AgentRegistryStub(
        [AgentProfileStub(name="Explore", display_name="Explorer", description="Profile [red]description")]
    )
    screen.state.runtime.details = AgentRuntimeDetails(
        skill_details=[RuntimeSkillDetails(name="runtime", description="Skill [blue]description")]
    )
    handler = make_suggestion_handler(screen, locale_controller=LocaleController(Settings(locale="zh-Hans")))
    commands = handler.build_slash_commands()

    handler.on_slash_triggered()
    await _finish_workers(screen)

    assert screen.suggestion_list.last_title == "命令"
    command = commands[0]
    command_item = next(
        item
        for item in screen.suggestion_list.last_items
        if isinstance(item, SuggestionItem) and item.kind == "command" and item.value == command.name
    )
    assert command_item.section == "系统命令"
    assert handler._render_message(command.description) in command_item.label.plain
    skill = next(
        item
        for item in screen.suggestion_list.last_items
        if isinstance(item, SuggestionItem) and item.kind == "skill" and item.value == "runtime"
    )
    assert skill.section == "已加载 Skills"
    assert skill.disabled_reason == "被 /runtime 遮蔽"
    assert "Skill [blue]description" in skill.label.plain

    handler.on_agent_triggered()
    assert screen.suggestion_list.last_title == "智能体"
    agent = next(item for item in screen.suggestion_list.last_items if isinstance(item, SuggestionItem))
    assert "Profile [red]description" in agent.label.plain

    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="literal", name="Model [green]name", model_id="vendor/[model]"))
    screen.services.model_registry = model_registry
    handler.on_model_triggered()
    assert screen.suggestion_list.last_title == "模型"
    model = next(item for item in screen.suggestion_list.last_items if isinstance(item, SuggestionItem))
    assert model.label.plain == "  Model [green]name  vendor/[model]"

    handler._show_suggestions("files", [])
    assert screen.suggestion_list.last_title == "/repo/[literal] 下的文件"
    scan = scan_result(
        "/repo/[literal]",
        [ProjectPathSuggestion(path="a.py", kind="file")],
        truncated=True,
        file_budget=1,
        suggestion_budget=1,
        source_truncations={"rg": True},
    )
    index = handler._build_file_index(scan)
    truncation = handler._file_suggestion_items([], index=index)[0]
    assert truncation.label == "此有限索引之外还有更多文件"
    assert truncation.disabled_reason == "已索引 1 个文件 / 1 行"


def test_shadowed_runtime_skill_dispatches_command() -> None:
    screen = make_suggestion_screen()
    screen.state.runtime.details = AgentRuntimeDetails(
        skill_details=[RuntimeSkillDetails(name="runtime", description="Runtime-like skill")]
    )
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    assert handler.dispatch_slash_command("/runtime") is True

    assert screen.opened == ["runtime"]
    assert screen.submitted == []


def test_selecting_runtime_skill_with_enter_submits_slash_reference() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    handler.on_suggestion_selected("commands", "review", execute=True, kind="skill")

    assert screen.submitted == ["/review"]


def test_stale_command_suggestion_selection_is_ignored() -> None:
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    handler.on_suggestion_selected("commands", "missing", execute=True)

    assert screen.opened == []
    assert screen.submitted == []


async def test_opening_the_list_under_new_folders_starts_one_shortcut_read() -> None:
    """A changed workspace starts one read, not one per caller asking for the shortcuts while it is out."""
    screen = make_suggestion_screen()
    handler = make_suggestion_handler(screen)
    handler.build_slash_commands()

    handler.on_slash_triggered()
    await _finish_workers(screen)
    landed = len(screen.workers)

    screen._set_workspace_cwd(str(Path.cwd() / "elsewhere"))
    handler.on_slash_triggered()
    for text in ("/z", "/zz", "/zzx"):
        handler.on_text_changed(text)

    started = screen.workers[landed:]
    for worker in started:
        await asyncio.wait_for(worker.wait(), ENGINE_TURN_TIMEOUT)
    assert len(started) == 1
