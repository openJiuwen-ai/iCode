# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The sidebar stays on screen when the terminal is too narrow for its usual width."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.events import Resize, Show
from textual.geometry import Offset, Size
from textual.screen import Screen
from textual.selection import Selection
from textual.widget import Widget
from textual.widgets import Static, TabbedContent, Tabs
from textual.widgets._tabs import Underline

from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.terminal.widget import Terminal
from chrys.app.tui.theme import CHRYS_LEGACY_THEME
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import UserThemeStore
from chrys.app.tui.widgets.btw import ChatColumn
from chrys.app.tui.widgets.chat.messages import AgentMessage, MessageHeaderRow, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel, TabOverflowMark
from chrys.foundation.config.settings import Settings
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_sidebar_tab, resize_when_settled
from tests.support.waiting import wait_for


def _right_mark_shown(sidebar: SidebarPanel, screen_width: int) -> bool:
    mark = sidebar.query_one("#sidebar-tabs-overflow-right", TabOverflowMark)
    return mark.active and mark.region.width == 1 and mark.region.right <= screen_width - 1


def _active_tab_is_underlined(tabs: Tabs) -> bool:
    active = tabs.active_tab
    if active is None or not active.content_region.width:
        return False
    underline = tabs.query_one(Underline)
    animator = tabs.app.animator
    span = active.virtual_region.shrink(active.styles.gutter).column_span
    return (
        not animator.is_being_animated(underline, "highlight_start")
        and not animator.is_being_animated(underline, "highlight_end")
        and (underline.highlight_start, underline.highlight_end) == span
    )


@pytest.mark.asyncio
async def test_a_narrow_terminal_shrinks_the_sidebar_to_the_room_beside_the_chat(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(60, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatColumn)
        # The chat keeps its 35 cells; the sidebar takes the 25 left, border and right mark included.
        await wait_for(lambda: sidebar.region.width == 25 and sidebar.region.right == 60, pilot=pilot)
        assert chat.region.width == 35
        await wait_for(lambda: _right_mark_shown(sidebar, 60), pilot=pilot)

        await resize_when_settled(pilot, 120, 30)
        await wait_for(lambda: sidebar.region.width == 42, pilot=pilot, description="the usual minimum is back")

        await resize_when_settled(pilot, 70, 24)
        await wait_for(lambda: sidebar.region.width == 35 and sidebar.region.right == 70, pilot=pilot)
        await wait_for(lambda: _right_mark_shown(sidebar, 70), pilot=pilot)

        # Narrower still, the sidebar's 30% alone would outgrow the 10 cells left.
        await resize_when_settled(pilot, 45, 24)
        await wait_for(lambda: sidebar.region.width == 10 and sidebar.region.right == 45, pilot=pilot)
        await wait_for(lambda: _right_mark_shown(sidebar, 45), pilot=pilot)
        assert chat.region.width == 35


@pytest.mark.asyncio
async def test_the_sidebar_hides_where_one_character_and_the_marks_no_longer_fit(tmp_path: Path) -> None:
    """The chat's 35 cells, two for the marks, two for the border and one letter fit in 40 columns."""
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(40, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatColumn)
        await wait_for(lambda: sidebar.display and sidebar.region.width == 5, pilot=pilot)

        await resize_when_settled(pilot, 39, 24)
        await wait_for(lambda: not sidebar.display and chat.region.width == 39, pilot=pilot)
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert not sidebar.display, "the sidebar cannot be opened where it does not fit"

        await resize_when_settled(pilot, 40, 24)
        await wait_for(lambda: sidebar.display and sidebar.region.width == 5, pilot=pilot)

        # A sidebar the user closed stays closed when the terminal widens again.
        await pilot.press("ctrl+g")
        await wait_for(lambda: not sidebar.display, pilot=pilot)
        await resize_when_settled(pilot, 39, 24)
        await resize_when_settled(pilot, 120, 30)
        await pilot.pause()
        assert not sidebar.display


@pytest.mark.asyncio
async def test_the_sidebar_follows_the_chat_column_s_minimum_width(tmp_path: Path) -> None:
    """The room is what the chat column's own ``min-width`` leaves, not a copy of it."""
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatColumn)
        chat.styles.min_width = 40

        await resize_when_settled(pilot, 60, 24)
        await wait_for(lambda: sidebar.region.width == 20 and sidebar.region.right == 60, pilot=pilot)
        assert chat.region.width == 40


@pytest.mark.asyncio
async def test_closing_the_compact_theme_editor_fits_the_sidebar_beside_the_chat_again(tmp_path: Path) -> None:
    """The compact editor drops the chat column's minimum; closing it restores the room beside the chat."""
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        store = UserThemeStore(tmp_path / "themes")
        store.save(copy_theme(CHRYS_LEGACY_THEME, name="custom"), None)
        theme, revision = store.load("custom")
        await main.open_theme_editor(theme, store, revision)
        await resize_when_settled(pilot, 60, 24)
        await wait_for(lambda: main.has_class("--theme-editor-compact"), pilot=pilot)

        editor = main.theme_editor
        assert editor is not None
        editor.action_close()
        await wait_for(lambda: main.theme_editor is None and sidebar.display, pilot=pilot)
        await wait_for(lambda: sidebar.region.width == 25 and sidebar.region.right == 60, pilot=pilot)


@pytest.mark.asyncio
@pytest.mark.parametrize(("locale", "narrow"), [("en", 40), ("zh-Hans", 42)])
async def test_active_sidebar_tab_stays_in_view_and_underlined_across_padding_changes(
    tmp_path: Path, locale: str, narrow: int
) -> None:
    app = make_chrys_app(tmp_path / "sessions", settings=Settings(locale=locale))
    async with app.run_test(size=(narrow, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        content = sidebar.query_one(TabbedContent)
        tabs = content.query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")

        def active_is_aligned() -> bool:
            active = tabs.active_tab
            return (
                active is not None
                and screen_is_settled(app, main)
                and not app.animator.is_being_animated(strip, "scroll_x")
                and _active_tab_is_underlined(tabs)
                and strip.content_region.overlaps(active.content_region)
            )

        await wait_for(lambda: tabs.has_class("-tight"), pilot=pilot)
        await pilot.wait_for_scheduled_animations()
        await wait_for(active_is_aligned, pilot=pilot, description="Chat underlined after narrow startup")
        await click_sidebar_tab(pilot, sidebar, content.get_tab("tab-debug"))
        await wait_for(lambda: content.active == "tab-debug" and active_is_aligned(), pilot=pilot)
        for width in (120, narrow, 55):
            await resize_when_settled(pilot, width, 24)
            await wait_for(
                active_is_aligned, pilot=pilot, description=f"Debug visible and underlined at {width} columns"
            )
            assert content.active == "tab-debug"


@pytest.mark.asyncio
@pytest.mark.parametrize(("locale", "width"), [("en", 39), ("zh-Hans", 41)])
async def test_restoring_the_sidebar_during_startup_keeps_the_active_tab_underlined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str, width: int
) -> None:
    app = make_chrys_app(tmp_path / "sessions", settings=Settings(locale=locale))
    app.animation_level = "none"
    async with app.run_test(size=(width, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        tabs = sidebar.query_one(Tabs)
        underline = tabs.query_one(Underline)
        active = tabs.active_tab
        assert active is not None
        await wait_for(
            lambda: not sidebar.display and active.virtual_region.width == 0,
            pilot=pilot,
            description="the sidebar and its active tab are hidden",
        )

        # Start the stale animation explicitly. Depending on startup layout order, Textual
        # may already have positioned the underline at the hidden tab and skip animating.
        hidden_end = active.virtual_region.shrink(active.styles.gutter).column_span[1]
        before = hidden_end + active.label.cell_length
        underline.highlight_end = before
        app.animation_level = "full"
        clock = app.animator._get_time
        now = clock()
        monkeypatch.setattr(app.animator, "_get_time", lambda: now)
        underline.animate("highlight_end", hidden_end, duration=0.3)
        now += 0.1
        await wait_for(lambda: hidden_end < underline.highlight_end < before, pilot=pilot)
        assert app.animator.is_being_animated(underline, "highlight_end")
        # If repainting the hidden underline invalidated the compositor's map, rebuilding it
        # during display restoration can leave no new widget for Textual to send Show to.
        sidebar.disable_messages(Show)
        await resize_when_settled(pilot, 120, 24)
        monkeypatch.setattr(app.animator, "_get_time", lambda: max(now, clock()))
        await wait_for(lambda: sidebar.display and _active_tab_is_underlined(tabs), pilot=pilot)


@pytest.mark.asyncio
async def test_restoring_the_sidebar_refits_the_tab_strip_after_a_hidden_resize(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(190, 24)) as pilot:
        sidebar = app.screen.query_one(SidebarPanel)
        tabs = sidebar.query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        right = sidebar.query_one("#sidebar-tabs-overflow-right", TabOverflowMark)

        def strip_fits(overflow: bool, tight: bool) -> bool:
            return (
                sidebar.display
                and tabs.has_class("-tight") == tight
                and right.visible == right.active == overflow
                and strip.region.x == sidebar.content_region.x
                and strip.region.right == sidebar.content_region.right - int(overflow)
            )

        # With the Workspace tab every tab fits from a 57-cell sidebar (30% of 190 columns).
        await wait_for(lambda: sidebar.region.width == 57 and not right.visible, pilot=pilot)
        for width, overflow, tight in ((120, True, False), (40, True, True), (55, True, False), (190, False, False)):
            await pilot.press("ctrl+g")
            await wait_for(lambda: not sidebar.display, pilot=pilot)
            await resize_when_settled(pilot, width, 24)
            sidebar.context_panel.update_session_totals(total_session_tokens=width)
            # A hidden repaint can leave the restored widget absent from both layout event lists.
            sidebar.disable_messages(Show, Resize)
            await pilot.press("ctrl+g")
            await wait_for(
                partial(strip_fits, overflow, tight),
                pilot=pilot,
                description=f"restored tab padding and overflow mark at {width} columns",
            )


@pytest.mark.asyncio
async def test_restoring_tab_padding_during_a_scroll_keeps_the_selected_tab_underlined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    app.animation_level = "full"
    async with app.run_test(size=(40, 24)) as pilot:
        content = app.screen.query_one(SidebarPanel).query_one(TabbedContent)
        tabs = content.query_one(Tabs)
        strip = tabs.query_one("#tabs-scroll")
        underline = tabs.query_one(Underline)
        await wait_for(lambda: tabs.has_class("-tight") and _active_tab_is_underlined(tabs), pilot=pilot)
        # Textual's scroll reflow updates sizes with layout=False, without firing virtual_size watchers.
        monkeypatch.setattr(strip, "_size_updated", partial(strip._size_updated, layout=False))
        with patch.object(app.animator, "_get_time", return_value=app.animator._get_time()):
            content.active = "tab-buddy"
            await wait_for(lambda: app.animator.is_being_animated(underline, "highlight_start"), pilot=pilot)
            await resize_when_settled(pilot, 55, 24)
        await wait_for(lambda: not tabs.has_class("-tight") and _active_tab_is_underlined(tabs), pilot=pilot)
        assert content.active == "tab-buddy"


@pytest.mark.asyncio
async def test_hiding_the_focused_sidebar_returns_keyboard_input_to_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(80, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        shell = main.query_one(ShellPanel)
        monkeypatch.setattr(shell, "_start_shell", lambda _cwd: None)
        main.shell_mode_state = True
        terminal = shell.query_one(Terminal)
        writes: list[str] = []

        async def record_input(data: str) -> None:
            writes.append(data)

        terminal.set_write_to_stdin(record_input)
        sidebar = main.query_one(SidebarPanel)
        content = sidebar.query_one(TabbedContent)
        await click_sidebar_tab(pilot, sidebar, content.get_tab("tab-tasks"))
        await wait_for(lambda: sidebar.has_focus_within, pilot=pilot)
        await resize_when_settled(pilot, 39, 30)
        await wait_for(lambda: not sidebar.display, pilot=pilot)
        assert main.focused is terminal
        await pilot.press("x")
        assert writes == ["x"]


@pytest.mark.asyncio
async def test_wide_or_hidden_sidebar_does_not_request_layout_on_terminal_resize(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        await main.query_one(ChatPanel).mount(*(Static(f"Transcript row {index}") for index in range(100)))

        async def resize_without_sidebar_layout(width: int, height: int) -> None:
            await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)
            with patch.object(sidebar, "refresh", wraps=sidebar.refresh) as refresh:
                await resize_when_settled(pilot, width, height)
                await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)
            assert not any(call.kwargs.get("layout") for call in refresh.call_args_list)

        # The 42-cell sidebar keeps its width across this resize.
        await resize_without_sidebar_layout(121, 30)
        await pilot.press("ctrl+g")
        await wait_for(lambda: not sidebar.display, pilot=pilot)
        await resize_without_sidebar_layout(60, 30)
        await pilot.press("ctrl+g")
        await wait_for(lambda: sidebar.region.width == 25 and sidebar.region.right == 60, pilot=pilot)


async def test_transcript_height_updates_do_not_requeue_layout_during_resize_or_sidebar_toggle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        sidebar = main.query_one(SidebarPanel)
        answer = "## Check\n\n" + "A long reply with CJK 中文 and an emoji 🫠. " * 28
        prompt = "A long request. " * 20
        entries = [entry for _ in range(6) for entry in (UserMessage(prompt), AgentMessage(answer))]
        await panel.mount(*entries)
        bodies = list(panel.query(VirtualizedMarkdown))
        await wait_for(
            lambda: len(bodies) == 12 and all(body.shown_source for body in bodies) and screen_is_settled(app, main),
            pilot=pilot,
            description="the transcript has parsed and laid out",
        )
        initial_height = panel.virtual_size.height
        panel.scroll_end(animate=False, immediate=True)
        await wait_for(lambda: panel.scroll_y == panel.max_scroll_y, pilot=pilot, description="at the latest message")

        inside_update: set[Widget] = set()
        committed: set[Widget] = set()
        requeued: list[Widget] = []
        vertical_scrollbars_shown: list[Widget] = []
        virtual_size_reactive = Widget.virtual_size
        vertical_scrollbar_reactive = Widget.show_vertical_scrollbar
        reactive_type = type(virtual_size_reactive)
        set_reactive = reactive_type._set
        size_updated = Widget._size_updated

        def observe_reactive(self, widget, value, always=False):
            if self is virtual_size_reactive and widget in inside_update and widget.virtual_size != value:
                requeued.append(widget)
            if self is vertical_scrollbar_reactive and value is True and widget in bodies:
                vertical_scrollbars_shown.append(widget)
            return set_reactive(self, widget, value, always=always)

        def observe_size(self, size, virtual_size, container_size, layout=True):
            if not isinstance(self, (UserMessage, MessageHeaderRow, SidebarPanel)) or self.virtual_size == virtual_size:
                return size_updated(self, size, virtual_size, container_size, layout=layout)
            committed.add(self)
            inside_update.add(self)
            try:
                return size_updated(self, size, virtual_size, container_size, layout=layout)
            finally:
                inside_update.remove(self)

        monkeypatch.setattr(reactive_type, "_set", observe_reactive)
        monkeypatch.setattr(Widget, "_size_updated", observe_size)

        await resize_when_settled(pilot, 64, 24)
        await wait_for(
            lambda: panel.virtual_size.height > initial_height and panel.scroll_y == panel.max_scroll_y,
            pilot=pilot,
            description="wrapping at the narrow width preserves the bottom anchor",
        )
        narrow_height = panel.virtual_size.height
        await pilot.press("ctrl+g")
        await wait_for(
            lambda: (
                not sidebar.display
                and panel.virtual_size.height < narrow_height
                and panel.scroll_y == panel.max_scroll_y
                and screen_is_settled(app, main)
            ),
            pilot=pilot,
            description="the transcript wraps and stays anchored when the sidebar hides",
        )
        assert {type(widget) for widget in committed} >= {UserMessage, MessageHeaderRow, SidebarPanel}
        assert not requeued, [type(widget).__name__ for widget in requeued]
        assert not vertical_scrollbars_shown


async def test_resize_layout_consumes_a_pending_request_and_retains_a_new_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot, description="the initial layout settled")
        pending_at_reflow: list[bool] = []
        new_request_survived: list[bool] = []
        layout = Screen._refresh_layout
        screen_resize = type(main)._screen_resized

        def observe_layout(self, size: Size | None = None, scroll: bool = False) -> None:
            if self is main and size == Size(80, 24):
                pending_at_reflow.append(main._layout_required)
                layout(self, size, scroll)
                main.refresh(layout=True)
            else:
                layout(self, size, scroll)

        def request_layout_before_resize(self, size: Size) -> None:
            if self is main and size == Size(80, 24) and self.outer_size != size:
                main.refresh(layout=True)
                assert main._layout_required
                screen_resize(self, size)
                new_request_survived.append(main._layout_required)
            else:
                screen_resize(self, size)

        monkeypatch.setattr(Screen, "_refresh_layout", observe_layout)
        monkeypatch.setattr(type(main), "_screen_resized", request_layout_before_resize)
        await resize_when_settled(pilot, 80, 24)
        assert pending_at_reflow == [False]
        assert new_request_survived == [True]


async def test_reopening_sidebar_restores_offscreen_markdown_layout_before_selection(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        sidebar = main.query_one(SidebarPanel)
        reply = "## Check\n\n" + "A long reply with CJK 中文 and an emoji 🫠. " * 28
        entries = [entry for _ in range(6) for entry in (UserMessage("A long request. " * 20), AgentMessage(reply))]
        await panel.mount(*entries)
        bodies = list(panel.query(VirtualizedMarkdown))
        await wait_for(
            lambda: len(bodies) == 12 and all(body.shown_source for body in bodies) and screen_is_settled(app, main),
            pilot=pilot,
            description="history has settled at the original width",
        )
        original_sizes = [body.virtual_size for body in bodies]
        first_answer = bodies[1]
        selection = Selection(Offset(0, 15), Offset(20, 19))
        selected = first_answer.get_selection(selection)
        assert selected is not None

        panel.scroll_end(animate=False, immediate=True)
        await wait_for(lambda: panel.scroll_y == panel.max_scroll_y, pilot=pilot)
        await pilot.press("ctrl+g")
        await wait_for(
            lambda: (
                not sidebar.display
                and all(body.virtual_size.width > size.width for body, size in zip(bodies, original_sizes, strict=True))
                and screen_is_settled(app, main)
            ),
            pilot=pilot,
            description="sidebar hidden and history wrapped wider",
        )

        # A geometry read can rebuild the full map before the queued full reflow
        # sends Resize. Offscreen markdown must already be measured by then.
        sidebar.display = True
        main._compositor._full_map_invalidated = True
        assert first_answer.region.width == original_sizes[1].width
        assert [body.virtual_size for body in bodies] == original_sizes
        assert first_answer.get_selection(selection) == selected
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)


async def test_sidebar_restores_wrapping_on_the_first_frame_after_same_height_stream_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        sidebar = main.query_one(SidebarPanel)
        source = "\n\n".join(f"Line {index} short" for index in range(5))
        answer = AgentMessage(source, is_final=False)
        await panel.mount(answer, *[UserMessage("A filler request. " * 30) for _ in range(6)])
        body = answer.query_one(VirtualizedMarkdown)
        await wait_for(
            lambda: body.shown_source == source and screen_is_settled(app, main),
            pilot=pilot,
            description="streaming message initially placed with the sidebar",
        )
        original_size = body.virtual_size
        await pilot.press("ctrl+g")
        await wait_for(
            lambda: body.virtual_size.width > original_size.width and screen_is_settled(app, main),
            pilot=pilot,
            description="message placed with the sidebar hidden",
        )
        wide_size = body.virtual_size

        expanded = "\n\n".join(f"Line {index} " + ("word " * 17).strip() for index in range(5))
        answer.stream_update(expanded, is_final=False)
        await wait_for(
            lambda: body.shown_source == expanded and screen_is_settled(app, main),
            pilot=pilot,
            description="longer stream content settled at the same wide height",
        )
        assert body.virtual_size == wide_size
        frames: list[tuple[int, int]] = []
        paint = type(main)._compositor_refresh

        def observe_paint(self) -> None:
            if self is main and sidebar.display and body._size.width == original_size.width:
                frames.append((body._size.height, body._width_at_last_layout))
            paint(self)

        monkeypatch.setattr(type(main), "_compositor_refresh", observe_paint)
        await pilot.press("ctrl+g")
        await wait_for(
            lambda: bool(frames) and screen_is_settled(app, main),
            pilot=pilot,
            description="first frame with the sidebar shown",
        )
        assert body.virtual_size.height > original_size.height
        assert frames[0] == (body.virtual_size.height, body.virtual_size.width)
