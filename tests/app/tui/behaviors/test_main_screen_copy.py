# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for main-screen clipboard interactions."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.errors import NoWidget
from textual.events import Click, MouseDown, MouseMove, MouseUp
from textual.geometry import Offset, Region
from textual.pilot import Pilot
from textual.selection import SELECT_ALL, SelectEnd, Selection, SelectStart, SelectState
from textual.widget import Widget
from textual.widgets import Static
from textual.widgets.text_area import Selection as TextAreaSelection

from chrys.app.tui.behaviors import right_click_copy
from chrys.app.tui.screens.main.copy_actions import parse_copy_arguments
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    MessageCopyButton,
    MessageHeaderRow,
    UserMessage,
    _UserImagePreview,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, is_tool_copy_excluded
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from chrys.foundation.events.bus import EventBus
from chrys.kernel import Content
from tests.support.tui_helpers import png_bytes
from tests.support.waiting import wait_for


class _MainScreenApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.main_screen = MainScreen(EventBus(), engine_provider=None)

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)


class _ChatPanelProbeApp(App):
    """Bare ChatPanel host for widget-tree marker checks.

    Copy-exclusion markers resolve by nearest-ancestor class walking
    (``is_tool_copy_excluded``), independent of any screen, so tests that
    only assert marker state skip the full MainScreen chrome mount.
    """

    def compose(self) -> ComposeResult:
        yield ChatPanel()


async def _click_coords_when_settled(pilot: Pilot[None], target: Widget) -> tuple[int, int]:
    """Return (region.x+1, region.y) once the compositor hit-tests to ``target``.

    The screen-level right-click interceptor resolves which widget owns the
    click via ``screen.get_widget_at(screen_x, screen_y)``. When a layout
    cascade has not fully settled, ``target.region`` can report a stale
    coordinate and the constructed click lands on a sibling — routing the
    event through the screen-copy path instead of the text-area path the
    test means to exercise. Polling until hit-test matches removes the race.
    """
    screen = target.screen

    def hit_target() -> tuple[int, int] | None:
        region = target.region
        if region.width <= 0 or region.height <= 0:
            return None
        x, y = region.x + 1, region.y
        try:
            widget_at, _ = screen.get_widget_at(x, y)
        except NoWidget:
            return None
        if widget_at is target or target in widget_at.ancestors_with_self:
            return x, y
        return None

    await wait_for(hit_target, pilot=pilot, description=f"{target!r} is hit-testable at its click coordinates")
    coordinates = hit_target()
    assert coordinates is not None
    return coordinates


async def _maximize_chat_panel_for_selection_test(app: _MainScreenApp, pilot: Pilot[None]) -> None:
    """Hide surrounding chrome so ChatPanel has a realistic viewport in unit tests."""
    for selector in ("AppHeader", "SidebarPanel", "InputBar", "Footer"):
        app.main_screen.query_one(selector).display = False
    await pilot.pause()


async def _rendered_user_body(pilot: Pilot[None], message: UserMessage) -> VirtualizedMarkdown:
    body = message.query_one(VirtualizedMarkdown)
    await wait_for(
        lambda: body.get_selection(SELECT_ALL) is not None,
        pilot=pilot,
        description="the user message body has rendered",
    )
    return body


def _select_all_in(widget: Widget) -> dict[Widget, Selection]:
    """What Textual's select-all records: the widget and every descendant."""
    return dict.fromkeys([widget, *widget.query("*")], SELECT_ALL)


def _click_copy_button(copy_button: MessageCopyButton) -> None:
    copy_button.on_click(
        Click(
            copy_button,
            x=0,
            y=0,
            delta_x=0,
            delta_y=0,
            button=1,
            shift=False,
            meta=False,
            ctrl=False,
            screen_x=copy_button.region.x,
            screen_y=copy_button.region.y,
        )
    )


@pytest.mark.parametrize(
    ("arg", "expected"),
    [
        ("", ("agent", 1)),
        ("3", ("agent", 3)),
        ("agent", ("agent", 1)),
        ("agent 3", ("agent", 3)),
        ("agent all", ("agent", None)),
        ("user", ("user", 1)),
        ("user 3", ("user", 3)),
        ("user all", ("user", None)),
        ("all", ("all", None)),
        ("USER 2", ("user", 2)),
    ],
)
def test_parse_copy_arguments_accepts_supported_forms(arg: str, expected: tuple[str, int | None]) -> None:
    assert parse_copy_arguments(arg) == expected


@pytest.mark.parametrize(
    "arg",
    [
        "0",
        "-1",
        "agent 0",
        "agent -1",
        "user 0",
        "user -1",
        "all 1",
        "agent all extra",
        "unknown",
    ],
)
def test_parse_copy_arguments_rejects_unsupported_forms(arg: str) -> None:
    assert parse_copy_arguments(arg) is None


@pytest.mark.asyncio
async def test_right_click_copies_chat_selection_then_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel = app.main_screen.query_one(ChatPanel)
        panel.set_session_id("session-should-not-win")
        text = "right click copy"
        await panel.add_user_message(text)
        await pilot.pause()

        message = app.main_screen.query_one(UserMessage)
        body = await _rendered_user_body(pilot, message)
        screen = app.main_screen
        screen.selections = {body: Selection(Offset(0, 0), Offset(len(text), 0))}
        click_x = message.region.x + 1
        click_y = message.region.y + 1

        app.post_message(
            MouseDown(
                message,
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
        app.post_message(
            MouseUp(
                message,
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

        assert app.clipboard == text
        assert copied == [text]
        assert screen.get_selected_text() is None


@pytest.mark.asyncio
async def test_right_click_input_selection_wins_over_stale_chat_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        text_area_copy_calls: list[tuple[EnhancedTextArea, bool, str]] = []
        original_text_area_copy = right_click_copy.copy_text_area_or_screen_selection

        def record_text_area_copy(text_area: EnhancedTextArea, *, clear: bool = False) -> bool:
            text_area_copy_calls.append((text_area, clear, text_area.selected_text))
            return original_text_area_copy(text_area, clear=clear)

        monkeypatch.setattr(right_click_copy, "copy_text_area_or_screen_selection", record_text_area_copy)
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_user_message("chat text")
        input_area = app.main_screen.query_one("#chat-input", EnhancedTextArea)
        input_area.text = "input text"
        input_area.selection = TextAreaSelection((0, 0), (0, 5))

        message = app.main_screen.query_one(UserMessage)
        body = await _rendered_user_body(pilot, message)
        app.main_screen.selections = {body: Selection(Offset(0, 0), Offset(4, 0))}
        click_x, click_y = await _click_coords_when_settled(pilot, input_area)

        app.post_message(
            MouseDown(
                input_area,
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

        assert app.clipboard == "input"
        assert copied == ["input"]
        assert text_area_copy_calls == [(input_area, True, "input")]
        assert input_area.selected_text == ""


@pytest.mark.asyncio
async def test_forward_event_discards_detached_markdown_selection_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_agent_message("stale markdown")
        await pilot.pause()

        markdown = app.main_screen.query_one(VirtualizedMarkdown)
        await markdown.remove()
        await pilot.pause()

        screen = app.main_screen
        screen.selections = {markdown: SELECT_ALL}
        screen._mouse_down_offset = Offset(1, 1)
        screen._selecting = True
        app._mouse_down_widget = markdown

        def stale_content_hit(_x: int, _y: int) -> tuple[Widget | None, Offset | None]:
            return markdown, Offset(0, 0)

        def stale_hit(_x: int, _y: int) -> tuple[Widget, Region]:
            return markdown, Region(1, 1, 10, 1)

        # The layout before the removal answers both, as the screen's one map does.
        monkeypatch.setattr(screen, "get_widget_and_offset_at", stale_content_hit)
        monkeypatch.setattr(screen, "get_widget_at", stale_hit)

        screen._forward_event(
            MouseDown(
                markdown,
                x=1,
                y=1,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=1,
                screen_y=1,
            )
        )

        assert screen.selections == {}
        assert screen._mouse_down_offset is None
        assert screen._selecting is False
        assert app._mouse_down_widget is None


async def _point_on_markdown(pilot: Pilot[None], markdown: VirtualizedMarkdown, *, top: str) -> tuple[int, int]:
    """A point that hits ``markdown`` once the chat is scrolled to its end: mid-screen when its top
    is ``"scrolled_off"`` the screen, else on its top row."""
    screen = markdown.screen

    def point() -> tuple[int, int] | None:
        region = markdown.region
        if (region.y < 0) != (top == "scrolled_off"):
            return None
        x, y = region.x + 1, screen.size.height // 2 if top == "scrolled_off" else region.y
        try:
            widget, _region = screen.get_widget_at(x, y)
        except NoWidget:
            return None
        return (x, y) if widget is markdown else None

    screen.query_one(ChatPanel).scroll_end(animate=False)
    await wait_for(point, pilot=pilot, description=f"the markdown's top is {top}")
    found = point()
    assert found is not None
    return found


@pytest.mark.asyncio
@pytest.mark.parametrize("top", ["scrolled_off", "on_screen"])
async def test_press_on_markdown_removed_since_the_last_layout_is_discarded(top: str) -> None:
    """A press with nothing selected must not reach a widget that is gone.

    Until the next layout, the screen still hits a widget removed since the last one, for longer
    inside ``batch_update()``. Where its top was scrolled off the screen, the hit gives an offset
    in it, and Textual's selection there requires it to have a parent; anywhere, Textual would
    focus it, and then no key binding works.
    """
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        paragraphs = 40 if top == "scrolled_off" else 1
        await panel.add_agent_message("\n\n".join(f"line {n}" for n in range(paragraphs)))
        await _maximize_chat_panel_for_selection_test(app, pilot)
        markdown = panel.query_one(VirtualizedMarkdown)
        screen = app.main_screen
        x, y = await _point_on_markdown(pilot, markdown, top=top)
        focused = screen.focused
        assert screen.selections == {}
        assert not screen._selecting

        with app.batch_update():
            await markdown.remove()
            stale, _region = screen.get_widget_at(x, y)
            assert (stale, markdown.parent) == (markdown, None)
            assert (screen.get_widget_and_offset_at(x, y)[1] is not None) == (top == "scrolled_off")
            screen._forward_event(
                MouseDown(
                    markdown,
                    x=x,
                    y=y,
                    delta_x=0,
                    delta_y=0,
                    button=1,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=x,
                    screen_y=y,
                )
            )

        assert screen._select_state is None
        assert screen._mouse_down_offset is None
        assert screen._selecting is False
        assert screen.focused is focused


@pytest.mark.asyncio
@pytest.mark.parametrize("top", ["scrolled_off", "on_screen"])
async def test_right_click_over_markdown_removed_since_the_last_layout_still_copies(
    monkeypatch: pytest.MonkeyPatch, top: str
) -> None:
    """A right-click copies the selection even over a widget removed since the last layout."""
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
        panel = app.main_screen.query_one(ChatPanel)
        text = "keep this"
        await panel.add_user_message(text)
        body = await _rendered_user_body(pilot, app.main_screen.query_one(UserMessage))
        paragraphs = 40 if top == "scrolled_off" else 1
        await panel.add_agent_message("\n\n".join(f"line {n}" for n in range(paragraphs)))
        await _maximize_chat_panel_for_selection_test(app, pilot)
        markdown = panel.query_one(AgentMessage).query_one(VirtualizedMarkdown)
        screen = app.main_screen
        x, y = await _point_on_markdown(pilot, markdown, top=top)
        screen.selections = {body: Selection(Offset(0, 0), Offset(len(text), 0))}

        with app.batch_update():
            await markdown.remove()
            stale, _region = screen.get_widget_at(x, y)
            assert (stale, markdown.parent) == (markdown, None)
            screen._forward_event(
                MouseDown(
                    markdown,
                    x=x,
                    y=y,
                    delta_x=0,
                    delta_y=0,
                    button=3,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=x,
                    screen_y=y,
                )
            )

        assert copied == [text]
        assert screen.selections == {}
        assert screen._mouse_down_offset is None


@pytest.mark.asyncio
@pytest.mark.parametrize("dragging", [False, True], ids=["finished", "dragging"])
async def test_move_over_markdown_removed_since_the_last_layout_ends_only_a_drag(dragging: bool) -> None:
    """A move over a widget removed since the last layout ends a drag, and leaves a finished
    selection alone: only a drag extends a selection to the widget under the pointer."""
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        text = "keep this"
        await panel.add_user_message(text)
        body = await _rendered_user_body(pilot, app.main_screen.query_one(UserMessage))
        await panel.add_agent_message("\n\n".join(f"line {n}" for n in range(40)))
        await _maximize_chat_panel_for_selection_test(app, pilot)
        markdown = panel.query_one(AgentMessage).query_one(VirtualizedMarkdown)
        screen = app.main_screen
        x, y = await _point_on_markdown(pilot, markdown, top="scrolled_off")
        selection = Selection(Offset(0, 0), Offset(len(text), 0))
        screen.selections = {body: selection}
        screen._selecting = dragging

        with app.batch_update():
            await markdown.remove()
            # Where its top was scrolled off, the hit gives an offset in the removed widget.
            hit, offset = screen.get_widget_and_offset_at(x, y)
            assert (hit, markdown.parent) == (markdown, None)
            assert offset is not None
            screen._forward_event(
                MouseMove(
                    markdown,
                    x=x,
                    y=y,
                    delta_x=0,
                    delta_y=0,
                    button=0,
                    shift=False,
                    meta=False,
                    ctrl=False,
                    screen_x=x,
                    screen_y=y,
                )
            )

        assert screen.selections == ({} if dragging else {body: selection})
        assert screen._selecting is False


@pytest.mark.asyncio
async def test_chat_clear_discards_selection_before_removing_transcript() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_agent_message("selected markdown")
        await pilot.pause()

        markdown = app.main_screen.query_one(VirtualizedMarkdown)
        screen = app.main_screen
        screen.selections = {markdown: SELECT_ALL}
        screen._mouse_down_offset = Offset(1, 1)
        screen._selecting = True
        app._mouse_down_widget = markdown

        await panel.clear()

        assert screen.selections == {}
        assert screen._mouse_down_offset is None
        assert screen._selecting is False
        assert app._mouse_down_widget is None


@pytest.mark.asyncio
async def test_screen_copy_skips_tool_renderers_and_descendants() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        text = "copy the message"
        await panel.add_user_message(text)
        await panel.add_tool_start(
            "call_1",
            "write_file",
            "filesystem.write",
            "{}",
            args={"path": "README_zh.md"},
        )
        await pilot.pause()

        message = app.main_screen.query_one(UserMessage)
        body = await _rendered_user_body(pilot, message)
        tool_group = app.main_screen.query_one(ToolGroup)
        tool_container = app.main_screen.query_one("#ft-content", Widget)
        screen = app.main_screen

        screen.selections = {
            body: Selection(Offset(0, 0), Offset(len(text), 0)),
            tool_group: SELECT_ALL,
            tool_container: SELECT_ALL,
        }

        selected = screen.get_selected_text()
        assert selected == text
        assert selected is not None
        assert "Widget#ft-content" not in selected
        assert "write_file" not in selected


@pytest.mark.asyncio
async def test_chat_panel_visible_drag_selection_skips_textual_full_walk(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await _maximize_chat_panel_for_selection_test(app, pilot)
        panel = app.main_screen.query_one(ChatPanel)
        for index in range(16):
            await panel.add_agent_message(f"visible fast path message {index}")
        await pilot.pause()

        panel.scroll_to(y=panel.max_scroll_y / 2, animate=False, immediate=True)
        await pilot.pause()

        visible_markdown = [
            widget for widget in panel.query(VirtualizedMarkdown) if widget.region.overlaps(panel.region)
        ]
        assert len(visible_markdown) >= 2
        start = visible_markdown[0]
        end = visible_markdown[-1]
        assert start.parent is not None
        assert end.parent is not None

        def fail_full_walk(self: SelectState) -> list[Widget]:
            raise AssertionError("generic Textual selection walker should not run for visible ChatPanel drags")

        monkeypatch.setattr(SelectState, "_walk_selected_widgets", fail_full_walk)

        selection_state = SelectState(
            start.region.offset,
            start=SelectStart(
                start.parent,
                start.region.offset - start.parent.region.offset,
                start.parent.region.offset,
                start.parent.scroll_offset,
                content_widget=start,
                content_offset=Offset(0, 0),
            ),
        ).update_end(
            end.region.offset,
            SelectEnd(end.parent, end, Offset(4, 0)),
        )

        app.main_screen._watch__select_state(selection_state)

        assert start in app.main_screen.selections
        assert end in app.main_screen.selections


@pytest.mark.asyncio
async def test_chat_panel_fast_selection_skips_tool_ui_descendants() -> None:
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await _maximize_chat_panel_for_selection_test(app, pilot)
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_agent_message("before tool card")
        await panel.add_tool_start(
            "call_1",
            "write_file",
            "filesystem.write",
            "{}",
            args={"path": "README_zh.md"},
        )
        await panel.add_tool_result("call_1", "write_file", "Wrote README_zh.md", 12)
        await panel.add_agent_message("after tool card")
        await pilot.pause()

        markdown_widgets = list(panel.query(VirtualizedMarkdown))
        assert len(markdown_widgets) >= 2
        start = markdown_widgets[0]
        end = markdown_widgets[-1]
        assert start.parent is not None
        assert end.parent is not None

        selection_state = SelectState(
            start.region.offset,
            start=SelectStart(
                start.parent,
                start.region.offset - start.parent.region.offset,
                start.parent.region.offset,
                start.parent.scroll_offset,
                content_widget=start,
                content_offset=Offset(0, 0),
            ),
        ).update_end(
            end.region.offset,
            SelectEnd(end.parent, end, Offset(5, 0)),
        )

        app.main_screen._watch__select_state(selection_state)

        assert app.main_screen.selections
        assert all(not is_tool_copy_excluded(widget) for widget in app.main_screen.selections)


def _chat_drag_state(start: Widget, end: Widget, *, end_offset: Offset) -> SelectState:
    assert start.parent is not None
    assert end.parent is not None
    return SelectState(
        start.region.offset,
        start=SelectStart(
            start.parent,
            start.region.offset - start.parent.region.offset,
            start.parent.region.offset,
            start.parent.scroll_offset,
            content_widget=start,
            content_offset=Offset(0, 0),
        ),
    ).update_end(
        end.region.offset + end_offset,
        SelectEnd(end.parent, end, end_offset),
    )


async def _chat_panel_with_messages(app: _MainScreenApp, pilot: Pilot[None], count: int) -> ChatPanel:
    await _maximize_chat_panel_for_selection_test(app, pilot)
    panel = app.main_screen.query_one(ChatPanel)
    for index in range(count):
        await panel.add_agent_message(f"cross viewport message {index} payload")
    await pilot.pause()
    return panel


@pytest.mark.asyncio
async def test_chat_cross_viewport_drag_copies_offscreen_middles_without_full_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drag whose start anchor is scrolled offscreen must stay O(visible).

    ``screen.selections`` holds only the visible slice plus the two anchors,
    while copy extracts the whole anchored range — including messages that
    were never on screen during the drag.
    """
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        panel = await _chat_panel_with_messages(app, pilot, 30)
        panel.scroll_end(animate=False, immediate=True)
        await pilot.pause()

        markdown = list(panel.query(VirtualizedMarkdown))
        start = markdown[0]
        end = markdown[-1]
        assert not start.region.overlaps(panel.region), "start anchor should be scrolled offscreen"
        assert end.region.overlaps(panel.region)

        def fail_full_walk(self: SelectState) -> list[Widget]:
            raise AssertionError("chat drags must never run the generic Textual selection walker")

        monkeypatch.setattr(SelectState, "_walk_selected_widgets", fail_full_walk)

        app.main_screen._watch__select_state(_chat_drag_state(start, end, end_offset=Offset(4, 0)))

        selections = app.main_screen.selections
        assert start in selections
        assert end in selections
        # The map stays bounded to the visible slice plus the two anchors:
        # the generic walk would select 60+ widgets here (30 markdown bodies
        # plus their header rows); the offscreen middles must never be
        # materialized into ``screen.selections``.
        assert len(selections) < 30
        for index in (2, 10, 20):
            assert markdown[index] not in selections

        selected = app.main_screen.get_selected_text()
        assert selected is not None
        for index in range(25):
            assert f"cross viewport message {index} payload" in selected
        assert "Scroll to bottom" not in selected


@pytest.mark.asyncio
async def test_chat_selection_reprojects_highlights_after_scroll() -> None:
    """Scrolling after a drag re-projects highlights onto revealed widgets."""
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        panel = await _chat_panel_with_messages(app, pilot, 30)
        panel.scroll_end(animate=False, immediate=True)
        await pilot.pause()

        markdown = list(panel.query(VirtualizedMarkdown))
        start = markdown[0]
        end = markdown[-1]
        offscreen_middle = markdown[10]
        assert not offscreen_middle.region.overlaps(panel.region)

        app.main_screen._watch__select_state(_chat_drag_state(start, end, end_offset=Offset(4, 0)))
        assert offscreen_middle not in app.main_screen.selections

        # Finish the drag, then scroll a mid-transcript message into view.
        # ``force=True`` + anchor release because the bottom-anchored chat
        # panel ignores plain programmatic scrolls (mirrors a user wheel-up).
        app.main_screen._selecting = False
        panel._anchor_released = True
        middle_virtual_y = offscreen_middle.region.y - panel.region.y + panel.scroll_y
        target_y = middle_virtual_y - panel.scrollable_content_region.height // 2
        panel.scroll_to(y=max(0, target_y), animate=False, immediate=True, force=True)
        await wait_for(
            lambda: offscreen_middle in app.main_screen.selections,
            pilot=pilot,
            description="drag selection reaches the offscreen middle widget",
        )
        assert app.main_screen.selections.get(offscreen_middle) == SELECT_ALL
        # Non-anchor widgets from the old (bottom) viewport dropped out.
        assert markdown[25] not in app.main_screen.selections

        # Copy remains the full range after scrolling.
        selected = app.main_screen.get_selected_text()
        assert selected is not None
        assert "cross viewport message 15 payload" in selected


@pytest.mark.asyncio
async def test_chat_selection_updates_notify_only_changed_widgets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drag updates must not invalidate unchanged widgets' render caches."""
    app = _MainScreenApp()
    async with app.run_test(size=(100, 24)) as pilot:
        panel = await _chat_panel_with_messages(app, pilot, 5)
        markdown = list(panel.query(VirtualizedMarkdown))
        assert len(markdown) >= 3
        start, end = markdown[0], markdown[-1]

        app.main_screen._watch__select_state(_chat_drag_state(start, end, end_offset=Offset(4, 0)))
        await pilot.pause()

        notified: list[Widget] = []
        original = VirtualizedMarkdown.selection_updated

        def counting(self: VirtualizedMarkdown, selection: Selection | None) -> None:
            notified.append(self)
            original(self, selection)

        monkeypatch.setattr(VirtualizedMarkdown, "selection_updated", counting)
        app.main_screen._watch__select_state(_chat_drag_state(start, end, end_offset=Offset(6, 0)))
        await pilot.pause()

        assert notified, "the moving end anchor must be re-rendered"
        assert set(notified) == {end}, "widgets with unchanged selections must not be re-notified"


@pytest.mark.asyncio
async def test_screen_copy_embedded_image_user_message_uses_text_not_preview() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        text = "look at @shot.png"
        contents = [
            text,
            Content.from_data(data=png_bytes((240, 80, 80)), media_type="image/png"),
        ]
        await panel.add_user_message(text, contents=contents)
        await pilot.pause()

        message = app.main_screen.query_one(UserMessage)
        await _rendered_user_body(pilot, message)
        preview = message.query_one(_UserImagePreview)
        screen = app.main_screen

        screen.selections = {preview: SELECT_ALL}
        assert screen.get_selected_text() is None

        screen.selections = _select_all_in(message)
        assert preview in screen.selections
        assert screen.get_selected_text() == f"[You]\n{text}"


@pytest.mark.asyncio
async def test_tool_renderer_descendants_are_marked_copy_excluded() -> None:
    app = _ChatPanelProbeApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_tool_start(
            "call_1",
            "write_file",
            "filesystem.write",
            "{}",
            args={"path": "README_zh.md"},
        )
        await pilot.pause()

        tool_spinner = app.query_one("#ft-spinner", Widget)

        assert is_tool_copy_excluded(tool_spinner)


@pytest.mark.asyncio
async def test_sub_agent_final_result_is_tool_copy_only_not_inline_selection_copy() -> None:
    """Final prose stays out of the inline transcript but remains in tool copy."""
    from chrys.foundation.tool_kinds import KIND_SUB_AGENT

    app = _ChatPanelProbeApp()
    async with app.run_test() as pilot:
        panel = app.query_one(ChatPanel)
        await panel.add_tool_start(
            "call_sa",
            "explore_agent",
            KIND_SUB_AGENT,
            "{}",
            args={"prompt": "investigate"},
        )
        final_result = "The investigation is complete."
        await panel.add_tool_result("call_sa", "explore_agent", final_result, 25)
        await pilot.pause()

        card = app.query_one(SubAgentToolCall)
        inner_feed = app.query_one("#sa-latest", Widget)

        assert is_tool_copy_excluded(inner_feed)
        assert final_result in card.format_tool_execution_copy()
        assert all(message.text != final_result for message in inner_feed.query(AgentMessage))


@pytest.mark.asyncio
async def test_screen_copy_filters_tool_group_when_selection_crosses_it() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        first = "before tool"
        second = "after tool"
        await panel.add_user_message(first)
        await panel.add_tool_start(
            "call_1",
            "write_file",
            "filesystem.write",
            "{}",
            args={"path": "README_zh.md"},
        )
        await panel.add_user_message(second)
        await pilot.pause()

        first_message, second_message = list(app.main_screen.query(UserMessage))[:2]
        first_body = await _rendered_user_body(pilot, first_message)
        second_body = await _rendered_user_body(pilot, second_message)
        tool_group = app.main_screen.query_one(ToolGroup)
        tool_widgets = [tool_group, *list(tool_group.query("*"))]

        app.main_screen.selections = {
            first_body: Selection(Offset(0, 0), None),
            **dict.fromkeys(tool_widgets, SELECT_ALL),
            second_body: Selection(None, Offset(len(second), 0)),
        }

        selected = app.main_screen.get_selected_text()
        assert selected is not None
        assert first in selected
        assert second in selected
        assert "write_file" not in selected
        assert "Widget#ft-content" not in selected


@pytest.mark.asyncio
async def test_screen_copy_formats_chat_speaker_labels() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.add_user_message("hello")
        await panel.add_agent_message("hi there")
        await pilot.pause()

        user = app.main_screen.query_one(UserMessage)
        await _rendered_user_body(pilot, user)
        agent = app.main_screen.query_one(AgentMessage)
        agent_row = agent.query_one(MessageHeaderRow)
        agent_header = agent.query_one(".agent-header", Static)
        agent_copy_button = agent.query_one(MessageCopyButton)
        agent_body = agent.query_one(VirtualizedMarkdown)
        screen = app.main_screen

        screen.selections = {
            **_select_all_in(user),
            agent: SELECT_ALL,
            agent_row: SELECT_ALL,
            agent_header: SELECT_ALL,
            agent_copy_button: SELECT_ALL,
            agent_body: SELECT_ALL,
        }

        assert screen.get_selected_text() == "[You]\nhello\n[Code Agent]\nhi there"


@pytest.mark.asyncio
async def test_slash_copy_omits_colon_after_speaker_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel = app.main_screen.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.add_user_message("hello")
        await panel.add_agent_message("hi there")
        await pilot.pause()

        app.main_screen._copy_agent_responses("all")
        assert app.clipboard == "[You]\nhello\n\n[Code Agent]\nhi there"
        assert copied[-1] == "[You]\nhello\n\n[Code Agent]\nhi there"

        app.main_screen._copy_agent_responses("")
        assert app.clipboard == "[Code Agent]\nhi there"
        assert copied[-1] == "[Code Agent]\nhi there"


@pytest.mark.asyncio
async def test_agent_copy_button_copies_raw_final_response(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel = app.main_screen.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.add_agent_message("hi **there**")
        await pilot.pause()

        button = app.main_screen.query_one(MessageCopyButton)
        assert button.display is True

        _click_copy_button(button)
        await pilot.pause()

        assert app.clipboard == "hi **there**"
        assert copied[-1] == "hi **there**"


@pytest.mark.asyncio
async def test_user_copy_button_copies_the_message_as_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel = app.main_screen.query_one(ChatPanel)
        text = "fix `List<String>` in <file>\n\tthen **ship**"
        await panel.add_user_message(text)
        await pilot.pause()

        message = app.main_screen.query_one(UserMessage)
        await _rendered_user_body(pilot, message)
        button = message.query_one(MessageCopyButton)
        assert button.display is True

        _click_copy_button(button)
        await pilot.pause()

        assert app.clipboard == text
        assert copied[-1] == text


@pytest.mark.asyncio
async def test_agent_copy_button_only_shows_after_streaming_finalizes() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_answer_delta("s1", "partial")
        await pilot.pause()

        button = app.main_screen.query_one(MessageCopyButton)
        assert button.display is False

        await panel.add_answer_delta("s1", " response")
        await panel.add_agent_message("partial response")
        await pilot.pause()

        button = app.main_screen.query_one(MessageCopyButton)
        assert button.display is True


@pytest.mark.asyncio
async def test_agent_copy_button_hidden_for_intermediate_agent_text() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_answer_delta("s1", "working")
        # A tool call after the text makes it intermediate.
        await panel.add_tool_start("c1", "read_file", "", args={"path": "a.py"})
        await pilot.pause()

        button = app.main_screen.query_one(MessageCopyButton)
        assert button.display is False


@pytest.mark.asyncio
async def test_slash_copy_supports_role_filters_and_all_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        panel = app.main_screen.query_one(ChatPanel)
        panel.set_profile("Code Agent")
        await panel.add_user_message("u1")
        await panel.add_agent_message("a1")
        await panel.add_user_message("u2")
        await panel.add_agent_message("a2")
        await panel.add_user_message("u3")
        await panel.add_agent_message("a3")
        await pilot.pause()

        app.main_screen._copy_agent_responses("2")
        assert copied[-1] == "[Code Agent]\na2\n\n[Code Agent]\na3"

        app.main_screen._copy_agent_responses("agent 2")
        assert copied[-1] == "[Code Agent]\na2\n\n[Code Agent]\na3"

        app.main_screen._copy_agent_responses("user 2")
        assert copied[-1] == "[You]\nu2\n\n[You]\nu3"

        app.main_screen._copy_agent_responses("agent all")
        assert copied[-1] == "[Code Agent]\na1\n\n[Code Agent]\na2\n\n[Code Agent]\na3"

        app.main_screen._copy_agent_responses("user all")
        assert copied[-1] == "[You]\nu1\n\n[You]\nu2\n\n[You]\nu3"

        app.main_screen._copy_agent_responses("all")
        assert copied[-1] == (
            "[You]\nu1\n\n[Code Agent]\na1\n\n[You]\nu2\n\n[Code Agent]\na2\n\n[You]\nu3\n\n[Code Agent]\na3"
        )


@pytest.mark.asyncio
async def test_screen_copy_returns_none_when_only_tool_ui_is_selected() -> None:
    app = _MainScreenApp()
    async with app.run_test() as pilot:
        panel = app.main_screen.query_one(ChatPanel)
        await panel.add_tool_start(
            "call_1",
            "write_file",
            "filesystem.write",
            "{}",
            args={"path": "README_zh.md"},
        )
        await pilot.pause()

        screen = app.main_screen
        screen.selections = {app.main_screen.query_one("#ft-content", Widget): SELECT_ALL}

        assert screen.get_selected_text() is None
