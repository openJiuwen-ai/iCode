# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for responsive sidebar layout."""

from __future__ import annotations

import pytest
from rich.cells import cell_len
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static, Tab, TabbedContent, Tabs

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.sidebar import buddy as buddy_module
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.tasks import TodoListState
from chrys.foundation.config.settings import Settings
from chrys.foundation.models.todos import TodoItem
from tests.support.buddies import a_buddy
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
        await wait_for(lambda: strip.max_scroll_x > 0, pilot=pilot, description="the tabs overflow the sidebar")
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
        await wait_for(lambda: strip.max_scroll_x > 0, pilot=pilot, description="the tabs overflow the sidebar")
        pane_child = sidebar.toc_panel
        # A positive control first: the same event over the strip does move it.
        await _scroll(pilot, tabs, events.MouseScrollRight, strip)
        await wait_for(lambda: strip.scroll_x > 0, pilot=pilot, description="the strip moved")
        moved = strip.scroll_x
        pilot.app.post_message(_wheel(events.MouseScrollRight, pane_child))
        assert not await wait_until(lambda: strip.scroll_x != moved, timeout=0.3, pilot=pilot)
