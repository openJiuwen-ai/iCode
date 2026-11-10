# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the tool-call detailed view modal and its builders."""

from __future__ import annotations

import base64
import json
from io import StringIO

import pytest
from rich.console import Console, Group
from rich.syntax import Syntax
from textual import on
from textual.app import App, ComposeResult
from textual.events import MouseDown, MouseUp
from textual.geometry import Offset
from textual.pilot import Pilot
from textual.selection import Selection
from textual.widgets import Static, TabPane

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.tool_view import _COPY_HINT, _NO_INPUT, ToolDetailModal
from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptJournal,
    AgentTranscriptSurface,
    TranscriptAssistantOp,
    TranscriptToolResultOp,
    TranscriptToolStartOp,
)
from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.renderers.ask_user import AskUserToolCall
from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
from chrys.app.tui.widgets.chat.renderers.file_edit import EditFileToolCall, WriteFileToolCall
from chrys.app.tui.widgets.chat.renderers.read_file import ReadFileToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.scroll_controller import scroll_gc_paused
from chrys.app.tui.widgets.chat.tool_call import (
    BaseToolCard,
    ToolCall,
    ToolCardHeader,
    ToolGroup,
    ToolViewButton,
    ToolViewRequested,
)
from chrys.app.tui.widgets.chat.tool_view_builders import (
    ToolViewContent,
    ToolViewDiff,
    ToolViewImage,
    build_code_view,
    build_output_views,
    build_params_view,
)
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.app.tui.widgets.diff_view.code import CodeColumn
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.i18n import Localizer
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ
from chrys.kernel import Content
from tests.support.tui_helpers import make_wheel
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until


def test_tool_view_chrome_keeps_english_and_localizes_chinese() -> None:
    assert format_message(_COPY_HINT.bind()) == "Press c to copy"
    assert format_message(_NO_INPUT.bind()) == "(no input)"
    assert Localizer("zh-Hans").render(_COPY_HINT.bind()) == "按 c 复制"
    assert Localizer("zh-Hans").render(_NO_INPUT.bind()) == "（无输入）"  # noqa: RUF001


def test_build_code_view_routes_by_language() -> None:
    assert isinstance(build_code_view("markdown", "# hi"), VirtualizedMarkdown)
    assert isinstance(build_code_view("md", "# hi"), VirtualizedMarkdown)
    assert isinstance(build_code_view("text", "plain"), Static)
    assert isinstance(build_code_view("", "plain"), Static)
    assert isinstance(build_code_view("json", "{}"), Static)


@pytest.mark.asyncio
async def test_build_params_view_tables_scalars_and_breaks_out_long_values() -> None:
    long_value = "x" * 500
    async with App().run_test():
        widgets = build_params_view(
            {"timeout": 30, "enabled": True, "missing": None, "blob": long_value},
            dark=True,
        )
        # One table for the scalars, plus a label + code block for the long value.
        assert len(widgets) == 3
        table_text = _static_text(widgets[0])
        assert "timeout" in table_text
        assert "30" in table_text
        assert "true" in table_text
        assert "null" in table_text
        assert "blob" in _static_text(widgets[1])
        assert long_value in _static_text(widgets[2])


@pytest.mark.asyncio
async def test_build_params_view_renders_scalar_list_inline() -> None:
    async with App().run_test():
        widgets = build_params_view({"options": ["yes", "no"]}, dark=True)
        assert len(widgets) == 1
        text = _static_text(widgets[0])
        assert "options" in text
        assert "yes" in text
        assert "no" in text


def test_build_output_views_adds_headings_only_when_multiple() -> None:
    single = build_output_views([("Output", "text", "body")])
    assert len(single) == 1

    multi = build_output_views([("Output", "text", "a"), ("Diff", "diff", "b")])
    # One heading + one widget per section.
    assert len(multi) == 4


def _static_text(static: Static) -> str:
    """Best-effort plain text from a Static's renderable (Text, Syntax, or Table)."""
    from rich.console import Console

    rendered = static.render()
    # Plain text content renders to a Content/Text visual exposing .plain.
    plain = getattr(rendered, "plain", None)
    if isinstance(plain, str):
        return plain
    # Syntax/Table render to a RichVisual wrapping the original renderable.
    renderable = getattr(rendered, "_renderable", rendered)
    console = Console(width=200, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _modal_section_text(modal: ToolDetailModal, pane_id: str) -> str:
    pane = modal.query_one(f"#{pane_id}", TabPane)
    return "\n".join(_static_text(static) for static in pane.query(Static))


async def _wait_for_tool_view(pilot: Pilot[None], *, timeout: float = 5.0) -> ToolDetailModal:
    """Return the open tool-detail modal once it and its content are mounted.

    A single ``pilot.pause()`` after opening races the ``push_screen`` → compose
    → ``TabPane`` → ``ToolViewContent`` mount chain on slow CI (notably Windows),
    where the queried content is not yet attached. Poll the asserted condition
    instead of trusting one pause.
    """

    def content_ready() -> bool:
        screen = pilot.app.screen
        if not isinstance(screen, ToolDetailModal) or not screen.is_mounted:
            return False
        views = screen.query(ToolViewContent)
        return bool(views) and all(view.children for view in views)

    await wait_for(content_ready, timeout=timeout, pilot=pilot, description="tool detail modal content is mounted")
    modal = pilot.app.screen
    assert isinstance(modal, ToolDetailModal)
    return modal


async def _open_tool_view(pilot: Pilot[None], tool: BaseToolCard, *, timeout: float = 5.0) -> ToolDetailModal:
    """Open *tool*'s detail modal and wait until its content is mounted."""
    tool.open_tool_view()
    return await _wait_for_tool_view(pilot, timeout=timeout)


async def _wait_for_markdown_source(
    pilot: Pilot[None],
    markdown: VirtualizedMarkdown,
    expected: str,
    *,
    timeout: float = 5.0,
) -> None:
    """Wait until a VirtualizedMarkdown initial/update parse has settled."""
    await wait_for(
        lambda: markdown.source == expected,
        pilot=pilot,
        timeout=timeout,
        description="markdown source matches the requested content",
    )


def _tiny_png_bytes() -> bytes:
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lG9n1wAAAABJRU5ErkJggg=="
    )


class _ToolViewHostApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    @on(ToolViewRequested)
    def _on_tool_view_requested(self, event: ToolViewRequested) -> None:
        event.stop()
        self.push_screen(ToolDetailModal.from_request(event))


def _at_bottom(surface: AgentTranscriptSurface) -> bool:
    return surface.max_scroll_y > 0 and surface.scroll_y == surface.max_scroll_y


@pytest.mark.asyncio
async def test_view_button_revealed_and_opens_modal() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "some_tool", args={"foo": "bar"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ToolCall)
        assert tool.query_one(ToolCardHeader).actions_visible is False

        tool.set_complete("the result body", duration_ms=10)
        await pilot.pause()
        assert tool.query_one(ToolCardHeader).actions_visible is True

        modal = await _open_tool_view(pilot, tool)
        assert modal.query_one("#tool-view-input", TabPane) is not None
        assert modal.query_one("#tool-view-output", TabPane) is not None
        assert "the result body" in _modal_section_text(modal, "tool-view-output")
        assert "foo" in _modal_section_text(modal, "tool-view-input")


@pytest.mark.asyncio
async def test_tool_view_modal_stacks_nested_tool_detail_and_returns_to_parent() -> None:
    class ToolApp(App):
        locale_controller = LocaleController(Settings(locale="en"))

    nested_tool = ToolCall("nested", "nested_tool", args={"path": "README.md"})
    nested_tool.set_complete("nested result", duration_ms=10)

    async with ToolApp().run_test() as pilot:
        outer = ToolDetailModal(
            title="outer_tool",
            input_widgets=[],
            output_widgets=[nested_tool],
            initial_tab="output",
        )
        pilot.app.push_screen(outer)
        await wait_for(
            lambda: pilot.app.screen is outer and nested_tool.is_attached,
            pilot=pilot,
            description="outer tool detail with nested card",
        )

        nested_tool.open_tool_view()
        nested_modals: list[ToolDetailModal] = []

        def nested_modal_mounted() -> bool:
            screen = pilot.app.screen
            if screen is outer or not isinstance(screen, ToolDetailModal) or not screen.is_mounted:
                return False
            if len(screen.query(ToolViewContent)) != 2:
                return False
            nested_modals[:] = [screen]
            return True

        await wait_for(
            nested_modal_mounted,
            pilot=pilot,
            description="nested tool detail modal",
        )
        assert nested_modals[0]._title == "nested_tool"

        await pilot.press("escape")
        await wait_for(
            lambda: pilot.app.screen is outer,
            pilot=pilot,
            description="return to parent tool detail modal",
        )


@pytest.mark.asyncio
async def test_sub_agent_detail_uses_one_scroll_owner_and_pauses_gc_during_scroll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    journal = AgentTranscriptJournal()
    for index in range(80):
        journal.record(TranscriptAssistantOp(f"Transcript line {index}: " + ("content " * 12)))
    surface = AgentTranscriptSurface(journal)
    modal = ToolDetailModal(
        title="Explore",
        input_widgets=[],
        output_widgets=[surface],
        initial_tab="output",
    )

    async with _ToolViewHostApp().run_test(size=(100, 30)) as pilot:
        pilot.app.push_screen(modal)
        await wait_for(
            lambda: _at_bottom(surface),
            pilot=pilot,
            description="scrollable sub-agent detail transcript at its newest content",
        )
        # Its own moves to the bottom are no scroll gesture.
        assert not scroll_gc_paused()

        output_pane = modal.query_one("#tool-view-output", TabPane)
        assert surface.parent is output_pane

        gc_states_during_scroll: list[bool] = []
        original_scroll_up = surface._scroll_up_for_pointer

        def scroll_up_spy(**kwargs: object) -> bool:
            gc_states_during_scroll.append(scroll_gc_paused())
            return original_scroll_up(**kwargs)

        monkeypatch.setattr(surface, "_scroll_up_for_pointer", scroll_up_spy)
        await surface._on_message(make_wheel(surface, up=True))
        await wait_for(
            lambda: surface.scroll_y < surface.max_scroll_y and surface._scroll_gc_guard.paused,
            pilot=pilot,
            description="detail scroll with GC paused",
        )
        assert gc_states_during_scroll == [True]
        assert scroll_gc_paused()

        await pilot.press("escape")
        await wait_for(
            lambda: not scroll_gc_paused(),
            pilot=pilot,
            description="detail scroll GC pause released on close",
        )


@pytest.mark.asyncio
async def test_sub_agent_detail_follows_new_tools_only_while_scrolled_to_the_bottom() -> None:
    journal = AgentTranscriptJournal()
    for index in range(40):
        journal.record(TranscriptAssistantOp(f"Transcript line {index}: " + ("content " * 12)))
    surface = AgentTranscriptSurface(journal)
    modal = ToolDetailModal(title="Explore", input_widgets=[], output_widgets=[surface], initial_tab="output")
    tools = 0

    def add_tool() -> int:
        nonlocal tools
        tools += 1
        call_id = f"read-{tools}"
        # Text first, so the tool opens a group of its own rather than joining the last one.
        journal.record(TranscriptAssistantOp(f"Reading, step {tools}.", segment_id=f"step-{tools}"))
        journal.record(TranscriptToolStartOp(call_id, "read_file", KIND_FILESYSTEM_READ, {"path": "README.md"}))
        journal.record(TranscriptToolResultOp(call_id, "read_file", "contents"))
        return surface.max_scroll_y

    async with _ToolViewHostApp().run_test(size=(100, 30)) as pilot:
        pilot.app.push_screen(modal)
        await wait_for(lambda: _at_bottom(surface), pilot=pilot, description="the detail opens at its newest content")

        bottom = add_tool()
        await wait_for(
            lambda: surface.max_scroll_y > bottom and _at_bottom(surface),
            pilot=pilot,
            description="a new tool keeps a view at the bottom there",
        )

        surface.post_message(make_wheel(surface, up=True))
        await wait_for(lambda: 0 < surface.scroll_y < surface.max_scroll_y, pilot=pilot, description="scrolled up")
        reading = surface.scroll_y
        bottom = add_tool()
        await wait_for(lambda: surface.max_scroll_y > bottom, pilot=pilot, description="the second tool lands")
        assert not await wait_until(lambda: surface.scroll_y != reading, pilot=pilot, timeout=0.5)

        # A scroll heading to the bottom follows again from the first frame of its move.
        surface.scroll_page_down(duration=1.0)
        await wait_for(
            lambda: pilot.app.animator.is_being_animated(surface, "scroll_y"),
            pilot=pilot,
            description="Page Down heading to the bottom",
        )
        bottom = add_tool()
        await wait_for(
            lambda: surface.max_scroll_y > bottom and _at_bottom(surface),
            pilot=pilot,
            description="back at the bottom, a new tool is followed again",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_kind", ["page-down", "down"])
async def test_a_scroll_down_asked_as_new_content_lands_keeps_a_following_detail_following(
    monkeypatch: pytest.MonkeyPatch, request_kind: str
) -> None:
    """Asked for as new content lands, a scroll down heads on from where the view was, and the view follows.

    Page Down (the key, or a click on the scrollbar's track below its thumb) and Down work out their rows as they
    are asked for; Textual scrolls after the next refresh, when the follow would take the view to the new content.
    """
    journal = AgentTranscriptJournal()
    for index in range(40):
        journal.record(TranscriptAssistantOp(f"Transcript line {index}: " + ("content " * 12)))
    surface = AgentTranscriptSurface(journal)
    modal = ToolDetailModal(title="Explore", input_widgets=[], output_widgets=[surface], initial_tab="output")

    async with _ToolViewHostApp().run_test(size=(100, 30)) as pilot:
        pilot.app.push_screen(modal)
        follow = surface._bottom_follow
        # The settle the opening queued has run: one still queued would pin the view before the seam.
        await wait_for(
            lambda: _at_bottom(surface) and not follow._settle_scheduled,
            pilot=pilot,
            description="the detail opens at its newest content",
        )
        bottom = surface.max_scroll_y
        settle = follow._settle
        asked: list[bool] = []

        def page_down_then_settle() -> None:
            if not asked and surface.max_scroll_y > bottom:
                asked.append(True)
                if request_kind == "page-down":
                    surface.scroll_page_down(duration=ENGINE_TURN_TIMEOUT, easing="linear")
                else:
                    surface.scroll_down(duration=ENGINE_TURN_TIMEOUT, easing="linear")
            settle()

        monkeypatch.setattr(follow, "_settle", page_down_then_settle)
        # Far more than a page, in one answer that lands at once.
        journal.record(
            TranscriptAssistantOp("\n\n".join(f"New line {index}." for index in range(60)), segment_id="new")
        )
        await wait_for(
            lambda: (
                bool(asked) and _at_bottom(surface) and not pilot.app.animator.is_being_animated(surface, "scroll_y")
            ),
            pilot=pilot,
            description="the view is at the new content's bottom",
        )
        assert follow.following


@pytest.mark.asyncio
async def test_home_asked_as_content_outgrows_a_detail_that_fit_keeps_it_at_the_top(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The view that fit its content was at its top and bottom at once: Home there is no scroll to the bottom."""
    journal = AgentTranscriptJournal()
    journal.record(TranscriptAssistantOp("A short opening line."))
    surface = AgentTranscriptSurface(journal)
    modal = ToolDetailModal(title="Explore", input_widgets=[], output_widgets=[surface], initial_tab="output")

    follow = surface._bottom_follow
    settle = follow._settle
    asked: list[bool] = []

    def home_then_settle() -> None:
        if not asked and surface.max_scroll_y > 0:
            asked.append(True)
            surface.scroll_home(animate=False)
        settle()

    # Before the detail opens: a settle the opening queued would otherwise pin the view first.
    monkeypatch.setattr(follow, "_settle", home_then_settle)
    async with _ToolViewHostApp().run_test(size=(100, 30)) as pilot:
        pilot.app.push_screen(modal)
        await wait_for(lambda: surface.is_mounted, pilot=pilot, description="the detail opens")
        assert surface.max_scroll_y == 0
        journal.record(
            TranscriptAssistantOp("\n\n".join(f"New line {index}." for index in range(60)), segment_id="new")
        )
        await wait_for(lambda: bool(asked), pilot=pilot, description="Home asked for as the content outgrew the view")
        assert not await wait_until(lambda: surface.scroll_y != 0, pilot=pilot, timeout=0.5)
        assert not follow.following


@pytest.mark.asyncio
async def test_image_tool_view_output_renders_image_without_caption() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "view_image", args={"path": "/tmp/pixel.png"})

    async with ToolApp().run_test(size=(120, 40)) as pilot:
        tool = pilot.app.query_one(ToolCall)
        image = Content.from_data(
            _tiny_png_bytes(),
            "image/png",
            additional_properties={"width": 1, "height": 1, "media_type": "image/png"},
        )
        tool.set_complete(
            "[image/png image]",
            duration_ms=10,
            image_contents=[image],
        )
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert len(widgets) == 1
        assert isinstance(widgets[0], ToolViewImage)

        modal = await _open_tool_view(pilot, tool)
        assert modal.query_one(ToolViewImage) is not None
        output_text = _modal_section_text(modal, "tool-view-output")
        assert "Image:" not in output_text
        assert "[image/png image]" not in output_text


@pytest.mark.asyncio
async def test_tool_view_copy_key_copies_raw_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    copy_kwargs: list[dict[str, object]] = []

    def fake_copy(app: App, text: str, **kwargs: object) -> bool:
        copied.append(text)
        copy_kwargs.append(kwargs)
        return True

    monkeypatch.setattr(
        "chrys.app.tui.screens.dialogs.tool_view.copy_text_to_clipboards",
        fake_copy,
    )

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "some_tool", args={"foo": "bar"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ToolCall)
        tool.set_complete("the raw result body", duration_ms=10)
        await pilot.pause()

        modal = await _open_tool_view(pilot, tool)
        assert modal.query_one("#tool-view-container").border_subtitle == "Press c to copy"

        await pilot.press("c")
        await pilot.pause()
        assert copied == ['{\n  "foo": "bar"\n}']
        assert copy_kwargs[-1]["max_terminal_bytes"] == 64 * 1024

        modal.query_one("TabbedContent").active = "tool-view-output"
        await pilot.pause()
        await pilot.press("c")
        await pilot.pause()
        assert copied[-1] == "the raw result body"
        assert copy_kwargs[-1]["max_terminal_bytes"] == 64 * 1024


@pytest.mark.asyncio
async def test_running_sub_agent_tool_view_disables_whole_payload_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []

    def fake_copy(_app: App, text: str, **_kwargs: object) -> bool:
        copied.append(text)
        return True

    monkeypatch.setattr(
        "chrys.app.tui.screens.dialogs.tool_view.copy_text_to_clipboards",
        fake_copy,
    )

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("c1", "Explore", args={"prompt": "investigate"})

    async with ToolApp().run_test() as pilot:
        card = pilot.app.query_one(SubAgentToolCall)
        card.open_tool_view()
        mounted: list[ToolDetailModal] = []

        def modal_mounted() -> bool:
            screen = pilot.app.screen
            if not isinstance(screen, ToolDetailModal) or not screen.is_mounted:
                return False
            mounted[:] = [screen]
            return bool(screen.query(AgentTranscriptSurface))

        await wait_for(
            modal_mounted,
            pilot=pilot,
            description="running sub-agent tool detail modal",
        )
        modal = mounted[0]
        assert modal._copy_enabled is False
        assert modal.query_one("#tool-view-container").border_subtitle is None

        await pilot.press("c")
        await pilot.pause()

        assert copied == []


def test_tool_view_raw_input_parses_replay_args_summary() -> None:
    tool = ToolCall("c1", "some_tool", args_summary='{"foo": "bar", "count": 2}')

    assert json.loads(tool._tool_view_raw_input()) == {"foo": "bar", "count": 2}


@pytest.mark.asyncio
async def test_specialized_tool_view_raw_input_includes_visible_extra_args() -> None:
    prompt = "Inspect the workspace"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Pick one", "options": ["a", "b"]})
            yield SubAgentToolCall("c2", "explore_agent", args={"prompt": prompt, "profile": "Explore"})

    async with ToolApp().run_test() as pilot:
        ask = pilot.app.query_one(AskUserToolCall)
        sub_agent = pilot.app.query_one(SubAgentToolCall)
        ask.set_complete("User response: a", duration_ms=10)
        sub_agent.set_complete("done", duration_ms=10)
        await pilot.pause()

        assert json.loads(ask._tool_view_raw_input()) == {"question": "Pick one", "options": ["a", "b"]}
        assert json.loads(sub_agent._tool_view_raw_input()) == {"prompt": prompt, "profile": "Explore"}


@pytest.mark.asyncio
async def test_sub_agent_view_labels_prompt_as_task_prompt_without_top_margin() -> None:
    prompt = "## Explore\n\n- inspect tests"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("c1", "explore_agent", args={"prompt": prompt})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.set_complete("done", duration_ms=10)
        await pilot.pause()

        modal = await _open_tool_view(pilot, tool)
        pane = modal.query_one("#tool-view-input", TabPane)
        label = pane.query_one(".tool-view-section-title", Static)
        assert _static_text(label).strip() == "Task Prompt"
        assert label.has_class("tool-view-section-title-first")

        task_prompt = pane.query_one(VirtualizedMarkdown)
        await _wait_for_markdown_source(pilot, task_prompt, prompt)
        assert task_prompt.source == prompt
        assert "prompt:" not in _modal_section_text(modal, "tool-view-input")


@pytest.mark.asyncio
async def test_sub_agent_running_header_view_opens_live_collapsed_transcript() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("c1", "explore_agent", args={"prompt": "inspect"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.add_answer("plan", "I will inspect the file.")
        await tool.add_inner_tool_start(
            "inner-1",
            "read_file",
            {"path": "README.md"},
            tool_kind=KIND_FILESYSTEM_READ,
        )

        header = tool.query_one(ToolCardHeader)
        assert header.actions_visible is True
        assert header.copy_action_visible is False
        header.post_message(ToolViewButton.Clicked())
        modal = await _wait_for_tool_view(pilot)
        surface = modal.query_one(AgentTranscriptSurface)
        await wait_for(
            lambda: len(surface.query(ToolGroup)) == 1,
            pilot=pilot,
            description="sub-agent modal transcript group",
        )
        group = surface.query_one(ToolGroup)
        assert group.collapsed is True

        group.collapsed = False
        await wait_for(
            lambda: group.get_tool("inner-1") is not None,
            pilot=pilot,
            description="expanded nested modal tool",
        )


@pytest.mark.asyncio
async def test_sub_agent_terminal_header_view_opens_full_transcript() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("c1", "explore_agent", args={"prompt": "inspect"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)
        tool.add_answer("plan", "I will inspect the file.")
        await tool.add_inner_tool_start(
            "inner-1",
            "read_file",
            {"path": "README.md"},
            tool_kind=KIND_FILESYSTEM_READ,
        )
        tool.complete_inner_tool("inner-1", "file contents", 10)
        tool.add_answer("answer", "Inspection complete.")
        tool.set_complete("Inspection complete.", 20)
        await pilot.pause()

        header = tool.query_one(ToolCardHeader)
        assert header.copy_action_visible is True
        assert header._actions_text().plain.strip() == "view / copy"
        header.post_message(ToolViewButton.Clicked())

        modal = await _wait_for_tool_view(pilot)
        surface = modal.query_one(AgentTranscriptSurface)
        await wait_for(
            lambda: len(surface.query(AgentMessage)) == 2 and len(surface.query(ToolGroup)) == 1,
            pilot=pilot,
            description="terminal sub-agent full transcript",
        )
        assert [message.text for message in surface.query(AgentMessage)] == [
            "I will inspect the file.",
            "Inspection complete.",
        ]
        assert surface.query_one(ToolGroup).collapsed is True


@pytest.mark.asyncio
async def test_execute_view_shows_full_untruncated_output() -> None:
    long_output = "\n".join(f"line{i}" for i in range(20)) + "\n[exit_code: 0]"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ExecuteToolCall("c1", "bash", args={"command": "run.sh"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ExecuteToolCall)
        tool.set_complete(long_output, duration_ms=10)
        await pilot.pause()

        modal = await _open_tool_view(pilot, tool)

        output = _modal_section_text(modal, "tool-view-output")
        # Inline panel caps at 5 lines; the modal must show every line.
        assert "line0" in output
        assert "line19" in output
        # The command appears as the input.
        assert "run.sh" in _modal_section_text(modal, "tool-view-input")


def test_read_file_and_execute_renderers_use_base_tool_card_contract() -> None:
    read_tool = ReadFileToolCall("c1", "read_file", args={"path": "/tmp/app.py"})
    execute_tool = ExecuteToolCall("c2", "bash", args={"command": "echo hi"})

    assert isinstance(read_tool, BaseToolCard)
    assert isinstance(execute_tool, BaseToolCard)
    assert read_tool.status == "running"
    assert execute_tool.result_text == ""
    assert read_tool.args == {"path": "/tmp/app.py"}
    assert execute_tool.args == {"command": "echo hi"}


@pytest.mark.asyncio
async def test_read_file_renderer_records_rejected_completion_approval() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": "/tmp/app.py"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)

        tool.set_complete("Error: rejected", duration_ms=10, approval="user_rejected")
        await pilot.pause()

        assert tool.approval == "user_rejected"
        assert tool.has_class("-rejected")
        assert tool.query_one("#rf-panel").border_subtitle == "Rejected"


@pytest.mark.asyncio
async def test_execute_renderer_records_rejected_completion_approval() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ExecuteToolCall("c1", "bash", args={"command": "echo hi"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ExecuteToolCall)

        tool.set_complete("Error: rejected", duration_ms=10, approval="user_rejected")
        await pilot.pause()

        assert tool.approval == "user_rejected"
        assert tool.has_class("-rejected")
        assert str(tool.query_one("#exec-panel").border_subtitle) == "Rejected"


@pytest.mark.asyncio
async def test_edit_file_view_renders_full_diff() -> None:
    before = "alpha\nbeta\ngamma\n"
    after = "alpha\nBETA\ngamma\n"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield EditFileToolCall("c1", "edit_file", args={"path": "/tmp/x.txt"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(EditFileToolCall)
        tool.set_complete("Replaced 1 occurrence", duration_ms=10, file_snapshot=(before, after))
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert len(widgets) == 1
        assert isinstance(widgets[0], ToolViewDiff)


@pytest.mark.asyncio
async def test_diff_tool_view_output_keeps_horizontal_scrollbar() -> None:
    long_line = "const snakeState = " + "x" * 180 + ";\n"
    modal = ToolDetailModal(
        title="edit_file",
        input_widgets=[],
        output_widgets=[ToolViewDiff("/tmp/snake.html", "old\n", long_line)],
        initial_tab="output",
    )

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield Static("placeholder")

    async with ToolApp().run_test() as pilot:
        await pilot.app.push_screen(modal)
        await wait_for(
            lambda: len(modal.query(CodeColumn)),
            pilot=pilot,
            description="tool detail code column is mounted",
        )

        diff = modal.query_one(DiffView)
        code_column = modal.query_one(CodeColumn)

        assert diff.show_scrollbars is True
        assert code_column.code_width > code_column.size.width
        assert code_column.styles.overflow_x == "auto"
        assert code_column.styles.scrollbar_size_horizontal > 0


@pytest.mark.asyncio
async def test_read_file_view_shows_all_lines() -> None:
    lines = [f"{i}|content_{i}" for i in range(1, 21)]
    result = "File: /tmp/big.py (20 lines, 200 chars)\n" + "\n".join(lines)

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": "/tmp/big.py"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)
        tool.set_complete(result, duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert len(widgets) == 1
        assert isinstance(widgets[0], Static)
        code = _static_text(widgets[0])
        assert "content_1" in code
        assert "content_20" in code


@pytest.mark.parametrize("line_count", [3, 8])
@pytest.mark.parametrize("theme", ["textual-dark", "textual-light"])
async def test_read_file_card_and_detail_neutralize_controls_without_changing_raw_result(
    line_count: int, theme: str
) -> None:
    raw_lines = [f'\tvalue_{i} = "\x1b[24Dmasked\x07\x9b"' for i in range(line_count)]
    expected_lines = [f'    value_{i} = "�[24Dmasked��"' for i in range(line_count)]
    result = f"File: /tmp/source.py ({line_count} lines, 200 chars)\n" + "\n".join(
        f"{41 + i}|{line}" for i, line in enumerate(raw_lines)
    )

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": "/tmp/source.py"})

    app = ToolApp()
    app.theme = theme
    async with app.run_test(size=(100, 30)) as pilot:
        tool = app.query_one(ReadFileToolCall)
        tool.set_complete(result)
        panel = tool.query_one("#rf-panel", Static)
        card = panel.content
        if line_count > 5:
            assert isinstance(card, Group)
            card = card.renderables[0]
        assert isinstance(card, Syntax)
        assert card.code == "\n".join(expected_lines[:5])

        modal = await _open_tool_view(pilot, tool)
        detail = modal.query_one("#tool-view-output", TabPane).query_one(".tool-view-code", Static).content
        assert isinstance(detail, Syntax)
        assert detail.code == "\n".join(expected_lines)
        for syntax in (card, detail):
            assert syntax.line_numbers and syntax.start_line == 41 and syntax.tab_size == 4
            output = StringIO()
            Console(file=output, color_system=None, width=100).print(syntax)
            assert (
                "\x1b" not in output.getvalue() and "\x07" not in output.getvalue() and "\x9b" not in output.getvalue()
            )
        assert tool.result_text == result
        assert modal._raw_output == result


@pytest.mark.asyncio
async def test_header_renders_view_slash_copy_affordances() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "some_tool", args={"foo": "bar"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ToolCall)
        header = tool.query_one(ToolCardHeader)
        # Hidden until the tool reaches a terminal state, then revealed with view/copy.
        assert header.actions_visible is False
        actions_left = header.content_size.width - ToolCardHeader._ACTIONS_WIDTH
        assert header._zone_at(Offset(actions_left + 2, 0)) is None

        tool.set_complete("body", duration_ms=10)
        await pilot.pause()
        assert header.actions_visible is True
        actions = header._actions_text()
        assert actions.plain == " view / copy "

        # Zone styling must resolve through component classes: theme variables
        # like "auto 60%" are not Rich-parsable, so a raw style string would be
        # silently dropped and the text would inherit ancestor colors. The
        # blended (non-partial) foreground must be used — partial resolution
        # loses the alpha and renders brighter than MessageCopyButton — while the
        # background color stays off the spans for ANSI passthrough themes.
        full_style = header.get_component_rich_style("toolcardheader--action")
        base_style = header._zone_style("toolcardheader--action")
        assert base_style.dim is True
        assert base_style.color == full_style.color
        assert base_style.bgcolor is None
        assert all(span.style == base_style for span in actions.spans)
        hover_style = header._zone_style("toolcardheader--action-hover")
        assert hover_style.underline is True
        assert hover_style.bgcolor is None

        # View zone sits left of the copy zone; the separator between them is inert.
        assert header._zone_at(Offset(actions_left + 2, 0)) == "view"
        assert header._zone_at(Offset(actions_left + 6, 0)) is None
        assert header._zone_at(Offset(actions_left + 9, 0)) == "copy"
        # Clicks on the label area (outside the actions cell) hit no zone.
        assert header._zone_at(Offset(0, 0)) is None

        # Hovering a zone shows its tooltip and a pointer cursor.
        assert header.tooltip is None
        header._set_hover_zone("view")
        assert header.tooltip == "View full tool input and output"
        assert str(header.styles.pointer) == "pointer"
        header._set_hover_zone(None)
        assert header.tooltip is None


@pytest.mark.asyncio
async def test_modal_right_click_copies_selected_text(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "some_tool", args={"foo": "bar"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ToolCall)
        tool.set_complete("the result body", duration_ms=10)
        await pilot.pause()

        modal = await _open_tool_view(pilot, tool)

        pane = modal.query_one("#tool-view-output", TabPane)
        target = pane.query(Static).first()
        modal.selections = {target: Selection(Offset(0, 0), Offset(len("the result"), 0))}
        click_x = target.region.x + 1
        click_y = target.region.y
        for event_cls in (MouseDown, MouseUp):
            pilot.app.post_message(
                event_cls(
                    target,
                    x=click_x,
                    y=click_y,
                    delta_x=0,
                    delta_y=0,
                    button=3,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=click_x,
                    screen_y=click_y,
                )
            )
            await pilot.pause()

        assert copied == ["the result"]
        assert pilot.app.clipboard == "the result"
        assert modal.get_selected_text() is None


@pytest.mark.asyncio
async def test_view_button_click_opens_modal() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ToolCall("c1", "some_tool", args={"foo": "bar"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ToolCall)
        tool.set_complete("body", duration_ms=10)
        await pilot.pause()

        header = tool.query_one(ToolCardHeader)
        view_x = header.content_size.width - ToolCardHeader._ACTIONS_WIDTH + 2
        await pilot.click(ToolCardHeader, offset=(view_x, 0))
        assert isinstance(await _wait_for_tool_view(pilot), ToolDetailModal)


@pytest.mark.asyncio
async def test_read_file_view_preserves_truncation_notice() -> None:
    result = "File: /tmp/big.py (5 lines, 50 chars)\n" + "\n".join(f"{i}|c_{i}" for i in range(1, 6))
    result += "\n[Truncated at line 5 of 999]"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": "/tmp/big.py"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)
        tool.set_complete(result, duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert "[Truncated at line 5 of 999]" in joined


@pytest.mark.asyncio
async def test_read_file_view_preserves_long_line_notice() -> None:
    result = "File: /tmp/big.py (3 lines, 50 chars)\n" + "\n".join(f"{i}|c_{i}" for i in range(1, 4))
    result += "\n[Long lines truncated to 1000 chars: line 2]"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": "/tmp/big.py"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)
        tool.set_complete(result, duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert "[Long lines truncated to 1000 chars: line 2]" in joined


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, 6])
async def test_read_file_pagination_notice_preserved_without_false_truncation(tmp_path, limit: int) -> None:
    from chrys.service.tools.builtins.filesystem import read_file

    path = tmp_path / "pages.txt"
    path.write_text("\n".join(f"content_{i}" for i in range(1, 51)) + "\n", encoding="utf-8")
    result = read_file(str(path), offset=10, limit=limit)

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": str(path), "offset": 10, "limit": limit})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)
        tool.set_complete(result)
        card = tool.query_one("#rf-panel", Static).content
        if limit > 5:
            assert isinstance(card, Group)
            card = card.renderables[0]
        assert isinstance(card, Syntax)
        assert card.start_line == 10
        assert card.code == "\n".join(f"content_{i}" for i in range(10, 10 + min(limit, 5)))

        widgets = tool._tool_view_output_widgets()
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert (
            f"[Showing lines 10-{9 + limit} of 50. "
            f"Use offset={10 + limit}, limit={limit} to continue reading if needed.]"
        ) in joined
        assert "[Truncated" not in joined


@pytest.mark.asyncio
@pytest.mark.parametrize("max_tokens", [1, 100])
@pytest.mark.parametrize("limit", [1, 3])
async def test_read_file_view_preserves_actual_token_budget_notice(tmp_path, max_tokens: int, limit: int) -> None:
    from chrys.service.tools.builtins.filesystem import read_file

    path = tmp_path / "large-line.txt"
    path.write_text("x" * 50_000 + "\n", encoding="utf-8")
    result = read_file(str(path), limit=limit, max_tokens=max_tokens)
    notice = next(line for line in result.splitlines() if line.startswith("[Truncated"))
    assert "Source line 1 has 50000 chars." in notice
    assert "[Long lines" not in result
    if limit != 1:
        assert "To read this line in full, use offset=1, limit=1" in notice

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ReadFileToolCall("c1", "read_file", args={"path": str(path)})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ReadFileToolCall)
        tool.set_complete(result, duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert notice in joined


@pytest.mark.asyncio
async def test_ask_user_view_input_includes_options() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args={"question": "Pick one", "options": ["a", "b"]})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_complete("User response: a", duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_input_widgets()
        assert len(widgets) == 2
        extra = _static_text(widgets[1])
        assert "options" in extra
        assert "question" not in extra


@pytest.mark.asyncio
async def test_ask_user_view_input_options_from_args_summary_replay() -> None:
    summary = '{"question": "Pick one", "options": ["a", "b"]}'

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield AskUserToolCall("c1", "ask_user", args_summary=summary)

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(AskUserToolCall)
        tool.set_complete("User response: a", duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_input_widgets()
        assert len(widgets) == 2
        assert "options" in _static_text(widgets[1])


@pytest.mark.asyncio
async def test_execute_view_input_includes_extra_args() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ExecuteToolCall("c1", "bash", args={"command": "ls", "timeout": 30, "reason": "list files"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ExecuteToolCall)
        tool.set_complete("out\n[exit_code: 0]", duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_input_widgets()
        assert len(widgets) == 2
        extra = _static_text(widgets[1])
        assert "timeout" in extra
        assert "list files" in extra
        assert "command" not in extra


@pytest.mark.asyncio
async def test_execute_view_input_from_args_summary_replay() -> None:
    summary = '{"command": "ls -la", "timeout": 30, "reason": "list files"}'

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ExecuteToolCall("c1", "bash", args_summary=summary)

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ExecuteToolCall)
        tool.set_complete("out\n[exit_code: 0]", duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_input_widgets()
        assert len(widgets) == 2
        assert "ls -la" in _static_text(widgets[0])
        extra = _static_text(widgets[1])
        assert "timeout" in extra
        assert "list files" in extra


@pytest.mark.asyncio
async def test_execute_view_input_malformed_summary_falls_back_to_text() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield ExecuteToolCall("c1", "bash", args_summary="not json at all")

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(ExecuteToolCall)
        tool.set_complete("out\n[exit_code: 0]", duration_ms=10)
        await pilot.pause()

        widgets = tool._tool_view_input_widgets()
        assert len(widgets) == 1
        assert "not json at all" in _static_text(widgets[0])


@pytest.mark.asyncio
async def test_empty_write_file_view_falls_back_without_diff() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield WriteFileToolCall("c1", "write_file", args={"path": "/tmp/empty.txt"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)
        tool.set_complete("Wrote /tmp/empty.txt (0 lines)", duration_ms=10, file_snapshot=("", ""))
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert all(not isinstance(w, ToolViewDiff) for w in widgets)
        assert any("0 lines" in _static_text(w) for w in widgets if isinstance(w, Static))


@pytest.mark.asyncio
async def test_oversized_write_file_view_uses_bounded_preview() -> None:
    # > 64KB snapshot: must not build a full rich DiffView (stall risk).
    big_after = "\n".join(f"line {i} value" for i in range(20000))

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield WriteFileToolCall("c1", "write_file", args={"path": "/tmp/big.txt"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)
        tool.set_complete("Wrote /tmp/big.txt (20000 lines)", duration_ms=10, file_snapshot=("", big_after))
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert all(not isinstance(w, ToolViewDiff) for w in widgets)
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert "Full diff omitted" in joined


@pytest.mark.asyncio
async def test_oversized_eol_only_change_uses_bounded_preview() -> None:
    # > 64KB line-ending-only rewrite must also be bounded, not a full diff.
    lines = [f"line {i} value here" for i in range(14000)]
    before = "\n".join(lines) + "\n"
    after = "\r\n".join(lines) + "\r\n"

    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield EditFileToolCall("c1", "edit_file", args={"path": "/tmp/eol.txt"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(EditFileToolCall)
        tool.set_complete("Normalized line endings", duration_ms=10, file_snapshot=(before, after))
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert all(not isinstance(w, ToolViewDiff) for w in widgets)
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert "Full diff omitted" in joined


@pytest.mark.asyncio
async def test_noop_mutation_view_falls_back_to_result_text() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield EditFileToolCall("c1", "edit_file", args={"path": "/tmp/x.txt"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(EditFileToolCall)
        tool.set_complete("No changes made", duration_ms=10, file_snapshot=("same\n", "same\n"))
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert all(not isinstance(w, ToolViewDiff) for w in widgets)
        assert any("No changes made" in _static_text(w) for w in widgets if isinstance(w, Static))


@pytest.mark.asyncio
async def test_eol_only_change_view_shows_marked_diff() -> None:
    class ToolApp(_ToolViewHostApp):
        def compose(self) -> ComposeResult:
            yield EditFileToolCall("c1", "edit_file", args={"path": "/tmp/x.txt"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(EditFileToolCall)
        tool.set_complete("Replaced 1 occurrence", duration_ms=10, file_snapshot=("line", "line\n"))
        await pilot.pause()

        widgets = tool._tool_view_output_widgets()
        assert all(not isinstance(w, ToolViewDiff) for w in widgets)
        joined = "\n".join(_static_text(w) for w in widgets if isinstance(w, Static))
        assert "EOL" in joined or "newline" in joined
