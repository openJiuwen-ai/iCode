# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for responsive sidebar layout."""

from __future__ import annotations

from functools import partial

import pytest
from rich.cells import cell_len
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Static, Tab, TabbedContent, Tabs

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.theme import CHRYS_ANSI_THEME, CHRYS_THEME
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.sidebar import buddy as buddy_module
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel, TabOverflowMark
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.foundation.config.settings import Settings
from chrys.foundation.models.todos import TodoItem
from tests.support.buddies import a_buddy
from tests.support.paths import SRC_ROOT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for, wait_until


class SidebarSizingApp(App):
    CSS = """
    Horizontal {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield ChatPanel()
            yield SidebarPanel()


class LocalizedSidebarApp(App):
    def __init__(self, controller: LocaleController) -> None:
        super().__init__()
        self._controller = controller

    def compose(self) -> ComposeResult:
        yield SidebarPanel(locale_controller=self._controller)


def _tab_labels(sidebar: SidebarPanel) -> list[str]:
    tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
    return [tab.label_text for tab in tabs.query(Tab).results(Tab)]


def _value_column(row: str, value: str) -> int:
    assert row.endswith(value)
    return cell_len(row) - 8


async def _wait_for_sidebar_width(pilot, sidebar: SidebarPanel, expected: int) -> None:
    """Wait for Textual's debounced terminal resize layout to settle."""
    await wait_for(
        lambda: sidebar.region.width == expected,
        pilot=pilot,
        description=f"sidebar reaches {expected} columns",
    )


async def _wait_for_overflow(pilot, sidebar: SidebarPanel) -> None:
    """Wait until the tabs overflow and the strip is laid out with the mark cells reserved."""
    tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
    strip = tabs.query_one("#tabs-scroll")
    app = pilot.app
    await wait_for(
        lambda: (
            any(mark.active for mark in _marks(sidebar))
            and strip.max_scroll_x > 0
            and screen_is_settled(app, app.screen)
        ),
        pilot=pilot,
        description="the tabs overflow the sidebar",
    )


@pytest.mark.asyncio
async def test_sidebar_width_is_bounded_across_terminal_resizes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)

    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)

        assert sidebar.region.width == 42

        await pilot.resize_terminal(160, 30)
        await _wait_for_sidebar_width(pilot, sidebar, 48)

        await pilot.resize_terminal(240, 30)
        await _wait_for_sidebar_width(pilot, sidebar, 60)


@pytest.mark.asyncio
async def test_main_panels_fit_without_horizontal_overflow_at_80_columns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)

    async with SidebarSizingApp().run_test(size=(80, 30)) as pilot:
        chat = pilot.app.query_one(ChatPanel)
        sidebar = pilot.app.query_one(SidebarPanel)
        container = pilot.app.query_one(Horizontal)

        assert chat.region.width >= 35
        assert sidebar.region.width == 42
        assert sidebar.region.right <= container.content_region.right
        assert container.max_scroll_x == 0


@pytest.mark.asyncio
async def test_main_panels_keep_three_row_minimum_height(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)

    async with SidebarSizingApp().run_test(size=(80, 2)) as pilot:
        chat = pilot.app.query_one(ChatPanel)
        sidebar = pilot.app.query_one(SidebarPanel)

        assert chat.region.height == 3
        assert sidebar.region.height == 3


@pytest.mark.asyncio
async def test_sidebar_relabels_tabs_and_current_child_chrome_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.BuddyPanel._render_sprite", lambda _self: None)
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale="en"))
    sidebar: SidebarPanel | None = None

    async with LocalizedSidebarApp(controller).run_test(size=(80, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs_widget = sidebar.query_one(TabbedContent).query_one(Tabs)
        tabs_before = tuple(tabs_widget.query(Tab).results(Tab))
        assert sidebar in controller._surfaces
        assert _tab_labels(sidebar) == ["Chat", "Tasks", "Context", "Hooks", "Debug", "Buddy"]
        assert str(sidebar.debug_panel.query_one(".debug-title", Static).render()) == "Event Stream"

        sidebar.todo_state = TodoListState(items=(TodoItem(content="done", status="completed"),))
        sidebar.buddy_panel.buddy = a_buddy(name="Nori", turns=60)
        sidebar.context_panel.update_session_totals(
            total_session_tokens=1_000,
            total_session_input_tokens=200,
            total_session_output_tokens=800,
            total_session_cache_hit_tokens=50,
        )
        await pilot.pause()
        english_rows = {
            "200": str(sidebar.query_one("#ctx-session-input", Static).render()),
            "800": str(sidebar.query_one("#ctx-session-output", Static).render()),
            "1.0k": str(sidebar.query_one("#ctx-session-total", Static).render()),
            "50": str(sidebar.query_one("#ctx-session-cache-hit", Static).render()),
        }
        assert {_value_column(row, value) for value, row in english_rows.items()} == {9}

        assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
        tabs_after = tuple(tabs_widget.query(Tab).results(Tab))
        assert tabs_after == tabs_before
        assert _tab_labels(sidebar) == ["对话", "任务", "上下文", "钩子", "调试", "伙伴"]
        assert sidebar.tasks_panel.query_one("#tasks-todos").border_title == "待办 (1/1)"
        assert str(sidebar.tasks_panel.query_one("#tasks-empty", Static).render()) == "暂无待办"
        assert str(sidebar.toc_panel.query_one(".toc-empty", Static).render()) == "你的对话将显示在这里"
        assert (
            str(sidebar.buddy_panel.query_one(".buddy-empty", Static).render()) == "尚未孵化伙伴。请尝试 /buddy hatch"
        )
        assert str(sidebar.buddy_panel.query_one("#buddy-level", Static).render()) == "等级 7\n0/100 经验"
        assert str(sidebar.buddy_panel.query_one("#buddy-status", Static).render()) == "点击抚摸！"  # noqa: RUF001
        sidebar.buddy_panel._update_status(buddy_module._BUDDY_PETTING.bind(), style=None)
        assert str(sidebar.buddy_panel.query_one("#buddy-status", Static).render()) == "抚摸中..."
        assert str(sidebar.debug_panel.query_one(".debug-title", Static).render()) == "事件流"
        assert str(sidebar.context_panel.query_one("#ctx-usage-label", Static).render()) == "上下文用量"
        assert str(sidebar.context_panel.query_one("#ctx-token-usage-label", Static).render()) == "词元用量"
        assert str(sidebar.context_panel.query_one("#ctx-compressed-label", Static).render()) == "已压缩消息"
        chinese_rows = {
            "200": str(sidebar.query_one("#ctx-session-input", Static).render()),
            "800": str(sidebar.query_one("#ctx-session-output", Static).render()),
            "1.0k": str(sidebar.query_one("#ctx-session-total", Static).render()),
            "50": str(sidebar.query_one("#ctx-session-cache-hit", Static).render()),
        }
        assert {_value_column(row, value) for value, row in chinese_rows.items()} == {10}

    assert sidebar is not None
    assert sidebar not in controller._surfaces


def _wheel(event_type: type[events.MouseEvent], widget: Widget) -> events.MouseEvent:
    """A wheel step over *widget* as the terminal sends it: screen coordinates, no widget named."""
    x, y = widget.region.x + 1, widget.region.y
    return event_type(
        None, x=x, y=y, delta_x=0, delta_y=1, button=0, shift=False, meta=False, ctrl=False, screen_x=x, screen_y=y
    )


async def _scroll(pilot, widget: Widget, event_type: type[events.MouseEvent], strip: Widget, *, steps: int = 1) -> None:
    """Send up to *steps* wheel steps one at a time (queued together, the same event merges into one); stop at an end.

    Through the App, as the driver delivers them: the screen finds the widget under the mouse.
    """
    for _ in range(steps):
        before = strip.scroll_x
        pilot.app.post_message(_wheel(event_type, widget))
        if not await wait_until(lambda before=before: strip.scroll_x != before, timeout=0.5, pilot=pilot):
            return


async def test_the_wheel_over_a_narrow_tab_strip_slides_it_to_the_cut_off_tabs() -> None:
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        content = sidebar.query_one(TabbedContent)
        tabs = content.query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        await _wait_for_overflow(pilot, sidebar)
        *_, buddy = tabs.query(Tab).results(Tab)
        assert buddy.region.right > strip.region.right, "the last tab starts cut off"
        await _scroll(pilot, tabs, events.MouseScrollDown, strip, steps=10)
        await wait_for(lambda: strip.scroll_x == strip.max_scroll_x, pilot=pilot, description="slid to the end")
        await wait_for(lambda: buddy.region.right <= strip.region.right, pilot=pilot, description="the last tab whole")

        await _scroll(pilot, tabs, events.MouseScrollLeft, strip, steps=10)
        await wait_for(lambda: strip.scroll_x == 0, pilot=pilot, description="slid back to the start")

        await _scroll(pilot, tabs, events.MouseScrollRight, strip)
        await wait_for(lambda: strip.scroll_x > 0, pilot=pilot, description="a sideways wheel slides it too")
        await _scroll(pilot, tabs, events.MouseScrollUp, strip)
        await wait_for(lambda: strip.scroll_x == 0, pilot=pilot, description="up slides it back")
        assert content.active == "tab-toc", "sliding the strip picks no tab"


async def test_the_wheel_over_a_tab_pane_leaves_the_tab_strip_alone() -> None:
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        await _wait_for_overflow(pilot, sidebar)
        pane_child = sidebar.toc_panel
        # A positive control first: the same event over the strip does move it.
        await _scroll(pilot, tabs, events.MouseScrollRight, strip)
        await wait_for(lambda: strip.scroll_x > 0, pilot=pilot, description="the strip moved")
        moved = strip.scroll_x
        pilot.app.post_message(_wheel(events.MouseScrollRight, pane_child))
        assert not await wait_until(lambda: strip.scroll_x != moved, timeout=0.3, pilot=pilot)


def _marks(sidebar: SidebarPanel) -> tuple[TabOverflowMark, TabOverflowMark]:
    return (
        sidebar.query_one("#sidebar-tabs-overflow-left", TabOverflowMark),
        sidebar.query_one("#sidebar-tabs-overflow-right", TabOverflowMark),
    )


def _fits_without_marks(sidebar: SidebarPanel) -> bool:
    """Every tab is whole, the marks are gone and the tabs start at the sidebar's edge, as without marks."""
    tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
    strip = tabs.query_one("#tabs-scroll")
    return (
        _clipped_left(tabs, strip) is None
        and _clipped_right(tabs, strip) is None
        and not any(mark.visible for mark in _marks(sidebar))
        and strip.region.x == sidebar.content_region.x
        and strip.region.right == sidebar.content_region.right
    )


def _mark_text(mark: Widget) -> str:
    return mark.render_line(0).text if mark.visible else ""


def _clipped_right(tabs: Tabs, strip: Widget) -> Tab | None:
    """The first tab whose label runs past the strip's right edge."""
    view = strip.content_region
    return next(
        (tab for tab in tabs.query(Tab).results(Tab) if tab.display and tab.content_region.right > view.right),
        None,
    )


def _clipped_left(tabs: Tabs, strip: Widget) -> Tab | None:
    """The last tab whose label starts before the strip's left edge."""
    view = strip.content_region
    return next(
        (tab for tab in reversed(list(tabs.query(Tab).results(Tab))) if tab.display and tab.content_region.x < view.x),
        None,
    )


@pytest.mark.asyncio
async def test_a_narrow_tab_strip_marks_each_side_with_a_hidden_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        await _wait_for_overflow(pilot, sidebar)
        left, right = _marks(sidebar)
        # At 120 columns the baseline sidebar is 42 cells, with 40 inside its border.
        assert sidebar.toc_panel.region.width == sidebar.content_region.width == 40
        assert sidebar.toc_panel.region.x == sidebar.content_region.x
        assert sidebar.toc_panel.region.right == sidebar.content_region.right
        # The left edge keeps its full width when only the right arrow shows.
        assert right.region.width == 1 and right.region.right == sidebar.content_region.right
        assert strip.region.x == sidebar.content_region.x
        assert right.region.x == strip.region.right
        await wait_for(lambda: _mark_text(right) == "\u276f" and _mark_text(left) == "", pilot=pilot)
        assert right.styles.pointer == "pointer"

        strip.scroll_x = strip.max_scroll_x // 2
        await wait_for(lambda: strip.scroll_x == strip.max_scroll_x // 2, pilot=pilot)
        await wait_for(
            lambda: (
                _mark_text(left) == "\u276e"
                and _mark_text(right) == "\u276f"
                and screen_is_settled(pilot.app, pilot.app.screen)
            ),
            pilot=pilot,
        )
        assert left.region.width == 1 and left.region.x == sidebar.content_region.x
        assert left.region.right == strip.region.x
        assert right.region.x == strip.region.right

        strip.scroll_x = strip.max_scroll_x
        await wait_for(lambda: strip.scroll_x == strip.max_scroll_x, pilot=pilot)
        await wait_for(
            lambda: (
                _mark_text(left) == "\u276e" and not right.visible and screen_is_settled(pilot.app, pilot.app.screen)
            ),
            pilot=pilot,
        )
        assert left.region.right == strip.region.x
        assert strip.region.right == sidebar.content_region.right
        assert left.styles.pointer == "pointer"
        assert right.styles.pointer != "pointer"

        # The marks belong to the strip's row: the wheel over one slides the strip too.
        await _scroll(pilot, left, events.MouseScrollUp, strip)
        await wait_for(lambda: strip.scroll_x < strip.max_scroll_x, pilot=pilot, description="the wheel over a mark")


class _StyledSidebarApp(SidebarSizingApp):
    """The sizing host with the app stylesheet, so marks and tabs take their real colours."""

    CSS_PATH = str(SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss")

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.register_theme(theme)
        self.theme = theme.name


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", [CHRYS_THEME, CHRYS_ANSI_THEME], ids=["truecolor", "ansi"])
async def test_a_mark_uses_one_cell_in_the_unselected_tab_colour(monkeypatch: pytest.MonkeyPatch, theme: Theme) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    async with _StyledSidebarApp(theme).run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        await _wait_for_overflow(pilot, sidebar)
        strip.scroll_x = strip.max_scroll_x // 2
        left, right = _marks(sidebar)
        await wait_for(lambda: _mark_text(left) == "\u276e" and _mark_text(right) == "\u276f", pilot=pilot)
        assert str(left.render_line(0).text) == "\u276e"
        assert str(right.render_line(0).text) == "\u276f"
        unselected = next(tab for tab in tabs.query(Tab).results(Tab) if not tab.has_class("-active"))
        assert left.styles.color == unselected.styles.color == right.styles.color


@pytest.mark.asyncio
async def test_clicking_a_mark_reveals_the_next_tab_hidden_on_that_side(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        await _wait_for_overflow(pilot, sidebar)
        left, right = _marks(sidebar)
        await wait_for(lambda: _mark_text(right) == "\u276f" and _mark_text(left) == "", pilot=pilot)

        target = _clipped_right(tabs, strip)
        assert target is not None and target.region.right > strip.content_region.right
        await click_when_settled(pilot, right)
        await wait_for(
            lambda: target.region.right <= strip.content_region.right,
            pilot=pilot,
            description="the clipped tab is whole",
        )
        await wait_for(lambda: strip.scroll_x > 0, pilot=pilot)
        await wait_for(lambda: _mark_text(left) == "\u276e", pilot=pilot)

        back = _clipped_left(tabs, strip)
        assert back is not None and back.region.x < strip.content_region.x
        await click_when_settled(pilot, left)
        await wait_for(
            lambda: back.region.x >= strip.content_region.x,
            pilot=pilot,
            description="the tab clipped on the left is whole",
        )
        await wait_for(lambda: strip.scroll_x == 0, pilot=pilot)
        await wait_for(lambda: _mark_text(left) == "" and _mark_text(right) == "\u276f", pilot=pilot)


@pytest.mark.asyncio
async def test_a_tab_cut_only_in_its_padding_shows_no_mark(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every label is whole while a padding cell is cut: nothing is hidden, so no mark appears."""
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        await _wait_for_overflow(pilot, sidebar)
        left, right = _marks(sidebar)

        strip.scroll_x = strip.max_scroll_x - 1
        await wait_for(lambda: strip.scroll_x == strip.max_scroll_x - 1, pilot=pilot)
        await wait_for(lambda: _clipped_right(tabs, strip) is None, pilot=pilot)
        await wait_for(lambda: _mark_text(left) == "\u276e" and _mark_text(right) == "", pilot=pilot)
        assert right.styles.pointer != "pointer"

        strip.scroll_x = 1
        await wait_for(lambda: strip.scroll_x == 1, pilot=pilot)
        await wait_for(lambda: _clipped_left(tabs, strip) is None, pilot=pilot)
        await wait_for(lambda: _mark_text(left) == "" and _mark_text(right) == "\u276f", pilot=pilot)
        assert left.styles.pointer != "pointer"


@pytest.mark.asyncio
async def test_on_a_narrow_strip_each_click_shows_one_whole_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tab wider than the strip is shown from its label's start, never cut on the other side."""
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        # 11 cells leave 7 with both marks. Context's label (7) fits, its tab (9) does not.
        sidebar.fit_beside(11)
        await wait_for(lambda: sidebar.region.width == 11, pilot=pilot)
        await _wait_for_overflow(pilot, sidebar)
        left, right = _marks(sidebar)
        assert strip.content_region.width == 8

        for direction, mark, clipped in (("right", right, _clipped_right), ("left", left, _clipped_left)):
            seen: list[str] = []
            while (target := clipped(tabs, strip)) is not None:
                await click_when_settled(pilot, mark)
                await wait_for(
                    lambda target=target: (
                        strip.content_region.x <= target.content_region.x
                        and target.content_region.right <= strip.content_region.right
                    ),
                    pilot=pilot,
                    description=f"{target.label_text} whole after a click on the {direction} mark",
                )
                seen.append(str(target.label_text))
            assert len(seen) == 5, seen


_CHINESE_LABELS = ("对话", "任务", "上下文", "钩子", "调试", "伙伴")


def _shown_characters(tabs: Tabs, strip: Widget) -> set[tuple[int, int]]:
    """Which label characters, as (tab, character) indexes, the strip shows whole."""
    view = strip.content_region
    shown: set[tuple[int, int]] = set()
    for tab_index, tab in enumerate(tabs.query(Tab).results(Tab)):
        x = tab.content_region.x
        for char_index, char in enumerate(tab.label.plain):
            if view.x <= x and x + cell_len(char) <= view.right:
                shown.add((tab_index, char_index))
            x += cell_len(char)
    return shown


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "workflow", "room", "view", "tight"),
    [
        (None, False, 5, 1, True),
        (None, False, 6, 2, True),
        (None, False, 7, 3, False),
        (_CHINESE_LABELS, False, 7, 3, True),
        (_CHINESE_LABELS, False, 9, 5, False),
        # Unpadded, the two workflow labels would fit the 8 cells exactly: overflow
        # is still judged with the padding, so dropping it cannot end the overflow.
        (_CHINESE_LABELS, True, 8, 4, True),
    ],
    ids=["english-1", "english-2", "english-3", "chinese-3", "chinese-5", "chinese-workflow-4"],
)
async def test_a_narrow_tab_strip_always_shows_a_whole_character(
    monkeypatch: pytest.MonkeyPatch,
    labels: tuple[str, ...] | None,
    workflow: bool,
    room: int,
    view: int,
    tight: bool,
) -> None:
    """Paging through labels wider than the strip shows a character first and skips none.

    Wherever the strip is scrolled it shows a whole character; where it would not
    with the tabs' padding, they drop it.
    """
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    async with SidebarSizingApp().run_test(size=(120, 30)) as pilot:
        app = pilot.app
        sidebar = app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        if labels is not None:
            for tab, label in zip(tabs.query(Tab).results(Tab), labels, strict=True):
                tab.label = label
        sidebar.set_workflow_mode(workflow)
        sidebar.fit_beside(room)
        await wait_for(lambda: sidebar.region.width == room, pilot=pilot)
        await _wait_for_overflow(pilot, sidebar)
        left, right = _marks(sidebar)
        # Switching mode makes Textual slide the strip back to the selected tab: let that slide end.
        await wait_for(
            lambda: (
                tabs.has_class("-tight") == tight
                and not app.animator.is_being_animated(strip, "scroll_x")
                and screen_is_settled(app, app.screen)
            ),
            pilot=pilot,
        )
        every = {
            (tab_index, char_index)
            for tab_index, tab in enumerate(tabs.query(Tab).results(Tab))
            if tab.display
            for char_index in range(len(tab.label.plain))
        }
        assert _shown_characters(tabs, strip), "the strip opens on a character"

        strip.scroll_to(x=0, animate=False, force=True)
        await wait_for(lambda: strip.scroll_x == 0 and screen_is_settled(app, app.screen), pilot=pilot)
        assert not left.active and right.active
        assert strip.content_region.width == view + 1
        for mark in (right, left):
            seen = _shown_characters(tabs, strip)
            while mark.active:
                before = strip.scroll_x
                await click_when_settled(pilot, mark)
                await wait_for(
                    lambda before=before: strip.scroll_x != before and screen_is_settled(app, app.screen), pilot=pilot
                )
                shown = _shown_characters(tabs, strip)
                assert shown, f"a click leaves no whole character at {strip.scroll_x}"
                if left.active and right.active:
                    assert strip.content_region.width == view
                seen |= shown
            assert seen == every

        for position in range(strip.virtual_size.width - view):
            strip.scroll_to(x=position, animate=False, force=True)
            await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
            assert _shown_characters(tabs, strip), f"scrolled to {position}, the strip shows no whole character"

        # Selecting a tab scrolls it into view: the strip still shows a character.
        for tab in tabs.query(Tab).results(Tab):
            if not tab.display:
                continue
            tabs.active = tab.id or ""
            await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
            assert _shown_characters(tabs, strip), f"selecting {tab.label.plain} leaves no whole character"


@pytest.mark.asyncio
async def test_marks_follow_resize_locale_and_workflow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.current_buddy", lambda: None)
    monkeypatch.setattr("chrys.app.tui.widgets.sidebar.buddy.BuddyPanel._render_sprite", lambda _self: None)
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale="en"))
    async with LocalizedSidebarApp(controller).run_test(size=(120, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent).query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        left, right = _marks(sidebar)
        await wait_for(lambda: sidebar.region.width == 42 and _mark_text(right) == "\u276f", pilot=pilot)

        await pilot.resize_terminal(240, 30)
        await _wait_for_sidebar_width(pilot, sidebar, 60)
        await wait_for(lambda: strip.max_scroll_x == 0, pilot=pilot)
        await wait_for(lambda: _mark_text(left) == "" and _mark_text(right) == "", pilot=pilot)

        # Just wide enough for every English label: no marks, and no cells kept for them.
        await pilot.resize_terminal(150, 30)
        await _wait_for_sidebar_width(pilot, sidebar, 45)
        await wait_for(lambda: _fits_without_marks(sidebar), pilot=pilot)
        # One cell narrower, only Buddy's trailing padding is cut, as on main: still no marks.
        await pilot.resize_terminal(148, 30)
        await _wait_for_sidebar_width(pilot, sidebar, 44)
        await wait_for(lambda: strip.max_scroll_x == 1 and _fits_without_marks(sidebar), pilot=pilot)

        await pilot.resize_terminal(120, 30)
        await _wait_for_sidebar_width(pilot, sidebar, 42)
        await wait_for(lambda: _mark_text(right) == "\u276f", pilot=pilot)

        # Wide enough for the Chinese labels but not the English ones. A scroll reflow
        # can apply the new label widths without notifying the virtual_size watcher.
        with monkeypatch.context() as scroll_reflow:
            scroll_reflow.setattr(strip, "_size_updated", partial(strip._size_updated, layout=False))
            assert controller.switch_locale("zh-Hans").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
            await wait_for(lambda: _fits_without_marks(sidebar), pilot=pilot)

            assert controller.switch_locale("en").status is LocaleSwitchStatus.EFFECTIVE_CHANGED
            await wait_for(lambda: _mark_text(right) == "\u276f", pilot=pilot)

        sidebar.set_workflow_mode(True)
        await wait_for(lambda: _fits_without_marks(sidebar), pilot=pilot)

        sidebar.set_workflow_mode(False)
        await wait_for(lambda: _mark_text(right) == "\u276f", pilot=pilot)
