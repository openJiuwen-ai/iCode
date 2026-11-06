# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the active runtime details dialog."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.markup import escape
from textual.widgets import Static, Tab, TabbedContent, TabPane

from chrys.app.tui.screens.dialogs.runtime_details import (
    _BASE_URL,
    _INLINE_SKILLS_SOURCE,
    RuntimeDetailsDialog,
    _api_style_label,
    _bool_label,
    _context_label,
    _provider_label,
)
from chrys.foundation.events.types import (
    AgentRuntimeDetails,
    RuntimeHookDetails,
    RuntimeHookSourceDetails,
    RuntimeModelDetails,
)
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message


def test_runtime_details_values_follow_localization_contract() -> None:
    assert format_message(_INLINE_SKILLS_SOURCE.bind()) == "Inline profile skills"
    assert _api_style_label("chat_completions") == "Chat Completions"

    chinese = Localizer("zh-Hans")
    assert chinese.render(_INLINE_SKILLS_SOURCE.bind()) == "配置文件内联的 Skills"
    assert _api_style_label("chat_completions", chinese.render) == "Chat Completions"
    assert _api_style_label("responses", chinese.render) == "Responses"
    assert chinese.render(_BASE_URL.bind()) == "服务地址"
    assert _bool_label(True, chinese.render) == "是"
    assert _bool_label(False, chinese.render) == "否"


class _RuntimeDetailsDialogApp(App):
    CSS = "#text-probe { color: $text; }"

    def compose(self) -> ComposeResult:
        yield Static("placeholder", id="text-probe")
        with TabbedContent(id="baseline-tabs"):
            with TabPane("Active", id="baseline-active"):
                yield Static("active")
            with TabPane("Inactive", id="baseline-inactive"):
                yield Static("inactive")


def _section_text(section) -> str:
    return section.content.plain


def test_runtime_details_dialog_builtin_titles_are_category_names() -> None:
    details = AgentRuntimeDetails(builtin_tools={"filesystem.read": ["read_file"]})

    sections = RuntimeDetailsDialog(details)._tool_sections()

    assert str(sections[0].border_title) == "filesystem.read"


def test_runtime_details_dialog_does_not_render_base_url_placeholder() -> None:
    details = AgentRuntimeDetails(model=RuntimeModelDetails(name="GPT", provider="openai", model_id="gpt-test"))

    sections = RuntimeDetailsDialog(details)._model_sections(details.model)

    text = _section_text(sections[0])
    assert "provider default" not in text
    assert "Base URL" in text


def test_runtime_details_dialog_model_profile_id_first_and_streaming_uppercase() -> None:
    details = AgentRuntimeDetails(
        model=RuntimeModelDetails(
            profile_id="profile-123",
            name="GPT",
            provider="openai",
            model_id="gpt-test",
            stream=True,
            vision=True,
        )
    )

    sections = RuntimeDetailsDialog(details)._model_sections(details.model)
    lines = _section_text(sections[0]).splitlines()

    assert lines[0].startswith("Profile ID")
    assert "profile-123" in lines[0]
    assert any(line.startswith("Streaming") and "ON" in line for line in lines)
    assert any(line.startswith("Vision") and "ON" in line for line in lines)


def test_runtime_details_dialog_shows_chat_completions_api_style() -> None:
    details = AgentRuntimeDetails(
        model=RuntimeModelDetails(
            name="DeepSeek",
            provider="deepseek-openai",
            api_style="chat_completions",
            model_id="deepseek-chat",
        )
    )

    sections = RuntimeDetailsDialog(details)._model_sections(details.model)
    text = _section_text(sections[0])

    assert "API Style" in text
    assert "Chat Completions" in text


def test_runtime_details_dialog_shows_deepseek_responses_api_style() -> None:
    details = AgentRuntimeDetails(
        model=RuntimeModelDetails(
            name="DeepSeek",
            provider="deepseek-openai",
            api_style="responses",
            model_id="deepseek-reasoner",
        )
    )

    sections = RuntimeDetailsDialog(details)._model_sections(details.model)
    text = _section_text(sections[0])

    assert "API Style" in text
    assert "Responses" in text


@pytest.mark.parametrize(
    ("tokens", "label"),
    [
        (500, "500 tokens"),
        (200_000, "200k tokens"),
        (1_000_000, "1m tokens"),
    ],
)
def test_runtime_details_dialog_formats_context_units(tokens: int, label: str) -> None:
    assert _context_label(tokens) == label


@pytest.mark.parametrize(
    ("provider", "label"),
    [
        ("openai", "OpenAI"),
        ("anthropic", "Anthropic"),
        ("deepseek-openai", "DeepSeek (OpenAI)"),
        ("glm-openai", "GLM (OpenAI)"),
    ],
)
def test_runtime_details_dialog_formats_provider_display_names(provider: str, label: str) -> None:
    assert _provider_label(provider) == label


def test_runtime_details_dialog_preserves_list_order() -> None:
    details = AgentRuntimeDetails(
        builtin_tools={"filesystem.write": ["write_file", "edit_file", "write_file"]},
        mcp_tools={"server": ["z_tool", "a_tool"]},
    )

    tool_sections = RuntimeDetailsDialog(details)._tool_sections()
    mcp_sections = RuntimeDetailsDialog(details)._mcp_sections()

    assert _section_text(tool_sections[0]).splitlines() == ["write_file", "edit_file"]
    assert _section_text(mcp_sections[0]).splitlines() == ["z_tool", "a_tool"]


def test_runtime_details_dialog_merges_web_tool_and_provider_details() -> None:
    details = AgentRuntimeDetails(
        builtin_tools={"web_search": ["web_search"]},
        web_search_providers={"corp": "https://search.example.com"},
    )

    sections = RuntimeDetailsDialog(details)._tool_sections()

    assert [str(section.border_title) for section in sections] == ["web_search"]
    assert _section_text(sections[0]).splitlines() == ["web_search", "corp: https://search.example.com"]


def test_runtime_details_dialog_lists_context_tools_before_sub_agent_tools() -> None:
    details = AgentRuntimeDetails(
        builtin_tools={"sleep": ["sleep"]},
        context_tools=["compress_context", "recall_context", "list_compressed_contexts"],
        sub_agent_tools=["Explore"],
    )

    sections = RuntimeDetailsDialog(details)._tool_sections()

    assert [str(section.border_title) for section in sections] == [
        "sleep",
        "Context management tools",
        "Sub-agent tools",
    ]
    assert _section_text(sections[1]).splitlines() == ["compress_context", "recall_context", "list_compressed_contexts"]


def test_runtime_details_dialog_lists_each_extension_after_icode_s_tools_and_why_any_is_missing() -> None:
    details = AgentRuntimeDetails(
        builtin_tools={"sleep": ["sleep"]},
        extension_tools={"acme_release": ["acme_project_name", "acme_add_release_note"]},
        extension_failures={"acme_gone": "it is not installed"},
        context_tools=["compress_context"],
    )

    sections = RuntimeDetailsDialog(details)._tool_sections()

    assert [str(section.border_title) for section in sections] == [
        "sleep",
        "acme_release",
        "Unavailable extensions",
        "Context management tools",
    ]
    assert _section_text(sections[1]).splitlines() == ["acme_project_name", "acme_add_release_note"]
    assert _section_text(sections[2]) == "acme_gone: it is not installed"


def test_runtime_details_dialog_shows_an_extension_s_import_error_without_its_control_characters() -> None:
    details = AgentRuntimeDetails(
        extension_failures={"acme_bad": "importing it failed: RuntimeError: \x1b[2Jgone\nsecond line"},
    )

    (section,) = RuntimeDetailsDialog(details)._tool_sections()

    text = _section_text(section)
    assert "\x1b" not in text
    assert text.startswith("acme_bad: importing it failed: RuntimeError: ")
    assert text.splitlines()[-1] == "second line"


def test_runtime_details_dialog_shows_an_extension_s_undecodable_path_without_lone_surrogates() -> None:
    details = AgentRuntimeDetails(
        extension_failures={"acme_twice": "it is installed more than once (acme 1.0; /home/caf\udce9/acme_twice.py)"},
    )

    (section,) = RuntimeDetailsDialog(details)._tool_sections()

    text = _section_text(section)
    assert text.encode("utf-8")
    assert text.startswith("acme_twice: it is installed more than once (acme 1.0; /home/caf")


def test_runtime_details_dialog_groups_mcp_tools_and_failures() -> None:
    details = AgentRuntimeDetails(
        mcp_tools={
            "filesystem": ["read_file", "write_file"],
        },
        mcp_failures={"broken": "connection timed out"},
    )

    dialog = RuntimeDetailsDialog(details)
    sections = dialog._mcp_sections()

    assert str(sections[0].border_title) == "filesystem"
    assert "read_file" in _section_text(sections[0])
    assert "write_file" in _section_text(sections[0])
    assert str(sections[-1].border_title) == "Failed MCP servers"
    assert "broken: connection timed out" in _section_text(sections[-1])


def test_runtime_details_dialog_distinguishes_empty_and_failed_mcp_servers() -> None:
    details = AgentRuntimeDetails(
        mcp_tools={"empty": [], "broken": []},
        mcp_failures={"broken": "connection timed out"},
    )

    sections = RuntimeDetailsDialog(details)._mcp_sections()

    assert str(sections[0].border_title) == "empty"
    assert "Connected, but no tools are available to the model." in _section_text(sections[0])
    assert str(sections[1].border_title) == "Failed MCP servers"
    assert "broken: connection timed out" in _section_text(sections[1])
    assert all(str(section.border_title) != "broken" for section in sections)


def test_runtime_details_dialog_groups_skills_and_files_by_source() -> None:
    details = AgentRuntimeDetails(
        skill_sources={
            "/tmp/chrys-skills/review": ["review"],
            "Inline profile skills": ["inline-a"],
        },
        memory_sources={
            "AGENTS.md": ["AGENTS.md"],
            "docs/": ["docs/design.md", "docs/notes.txt"],
        },
    )

    dialog = RuntimeDetailsDialog(details)
    skill_sections = dialog._skill_sections()
    file_sections = dialog._file_sections()

    assert str(skill_sections[0].border_title) == "/tmp/chrys-skills/review"
    assert "review" in _section_text(skill_sections[0])
    assert str(skill_sections[1].border_title) == "Inline skills"
    assert "inline-a" in _section_text(skill_sections[1])
    assert str(file_sections[0].border_title) == "AGENTS.md"
    assert "AGENTS.md" in _section_text(file_sections[0])
    assert str(file_sections[1].border_title) == "docs/"
    assert "docs/design.md" in _section_text(file_sections[1])


def test_runtime_details_dialog_groups_project_and_global_hooks_with_runtime_metadata() -> None:
    details = AgentRuntimeDetails(
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="project",
                source_path="/repo/[literal]/.chrys/hooks/hooks.yaml",
                hooks=[
                    RuntimeHookDetails(
                        id="project[guard]",
                        event="before_tool_call",
                        execution_mode="blocking",
                        enabled=True,
                        description="Protect [literal] writes",
                    )
                ],
            ),
            RuntimeHookSourceDetails(
                scope="global",
                source_path="/config/hooks/hooks.yaml",
                hooks=[
                    RuntimeHookDetails(
                        id="audit",
                        event="after_turn",
                        execution_mode="fire_and_forget",
                        enabled=False,
                        description="Global audit",
                    )
                ],
            ),
        ]
    )

    sections = RuntimeDetailsDialog(details)._hook_sections()

    assert [str(section.border_title) for section in sections] == ["Project hooks", "Global hooks"]
    project_text = _section_text(sections[0])
    assert "Source" not in project_text
    assert "/repo/[literal]/.chrys/hooks/hooks.yaml" not in project_text
    assert str(sections[0].border_subtitle) == escape("/repo/[literal]/.chrys/hooks/hooks.yaml")
    assert "project[guard]" in project_text
    assert "before_tool_call" in project_text
    assert "blocking" in project_text
    assert "ON" in project_text
    assert "Protect [literal] writes" in project_text
    global_text = _section_text(sections[1])
    assert str(sections[1].border_subtitle) == "/config/hooks/hooks.yaml"
    assert "audit" in global_text
    assert "after_turn" in global_text
    assert "fire_and_forget" in global_text
    assert "OFF" in global_text


def test_runtime_details_dialog_distinguishes_no_sources_from_empty_source() -> None:
    empty = RuntimeDetailsDialog(AgentRuntimeDetails())._hook_sections()
    configured = RuntimeDetailsDialog(
        AgentRuntimeDetails(
            hook_sources=[
                RuntimeHookSourceDetails(
                    scope="global",
                    source_path="/config/hooks/hooks.yaml",
                )
            ]
        )
    )._hook_sections()

    assert str(empty[0].border_title) == "Hooks"
    assert _section_text(empty[0]) == "No hooks loaded."
    assert str(configured[0].border_title) == "Global hooks"
    assert str(configured[0].border_subtitle) == "/config/hooks/hooks.yaml"
    assert "No hooks configured in this source." in _section_text(configured[0])


@pytest.mark.asyncio
async def test_runtime_details_dialog_uses_theme_text_without_overriding_tabs() -> None:
    details = AgentRuntimeDetails(
        model=RuntimeModelDetails(profile_id="profile-123", name="GPT"),
        hook_sources=[
            RuntimeHookSourceDetails(
                scope="global",
                source_path="/config/hooks/hooks.yaml",
                hooks=[RuntimeHookDetails(id="audit", event="after_turn")],
            )
        ],
    )
    app = _RuntimeDetailsDialogApp()
    async with app.run_test() as pilot:
        app.theme = "textual-light"
        await app.push_screen(RuntimeDetailsDialog(details))
        await pilot.pause()

        expected = app.query_one("#text-probe", Static).styles.color
        screen = app.screen

        assert screen.query_one(".runtime-detail-section").styles.color == expected

        baseline_tabs = list(app.query_one("#baseline-tabs").query(Tab))
        runtime_tabs = list(screen.query_one("#runtime-details-tabs").query(Tab))
        hook_section = screen.query_one("#runtime-hooks", TabPane).query_one(".runtime-detail-section", Static)

        assert runtime_tabs[0].styles.color == baseline_tabs[0].styles.color
        assert runtime_tabs[1].styles.color == baseline_tabs[1].styles.color
        assert hook_section.styles.border_subtitle_align == "right"
        assert str(hook_section.border_subtitle) == "/config/hooks/hooks.yaml"
