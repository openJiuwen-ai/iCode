# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for sessions screen utility functions."""

from __future__ import annotations

import errno
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from rich.style import Style
from rich.text import Text
from textual.events import MouseMove
from textual.widgets import Button, DataTable, Static

from chrys.app.tui.screens.sessions.presenter import format_size
from chrys.app.tui.screens.sessions.screen import (
    SessionsScreen,
    _next_cursor_row_after_delete,
    _next_session_id_after_delete,
    _SessionTable,
)
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.service.state.store import ChatSessionMeta
from tests.support.sessions_browser import (
    FakeSessionStore,
    GatedPageStore,
    SessionsHostApp,
    confirm_delete,
    open_delete_dialog,
    wait_for_blocked_loads,
    wait_for_load_idle,
    wait_for_row_count,
)
from tests.support.waiting import wait_for


async def _wait_for_results(results: list[str | None], pilot, count: int) -> None:
    await wait_for(
        lambda: len(results) == count,
        pilot=pilot,
        description=f"session dialog produces {count} dismiss results",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("workflow_mode", [False, True])
async def test_session_browser_lists_only_its_mode_kind(workflow_mode: bool) -> None:
    store = FakeSessionStore(3)
    app = SessionsHostApp()
    async with app.run_test() as pilot:
        screen = SessionsScreen(store, workflow_mode=workflow_mode)
        await app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        assert store.listings_opened == ["workflow" if workflow_mode else "chat"]


@pytest.mark.asyncio
async def test_session_table_mouse_move_dispatch_invokes_data_table_base_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session table hover customizations should not double-run DataTable mouse handling."""

    class _MouseMoveProbe(_SessionTable):
        pass

    base_calls: list[MouseMove] = []

    def data_table_mouse_move_spy(_table: DataTable, event: MouseMove) -> None:
        base_calls.append(event)

    monkeypatch.setattr(DataTable, "_on_mouse_move", data_table_mouse_move_spy)
    table = _MouseMoveProbe()
    event = MouseMove(
        table,
        x=0,
        y=0,
        delta_x=0,
        delta_y=0,
        button=0,
        shift=False,
        meta=False,
        ctrl=False,
        style=Style(),
    )

    await table._on_message(event)

    assert base_calls == [event]


class TestFormatSize:
    def test_zero_bytes(self) -> None:
        assert format_size(0) == "0 B"

    def test_small_bytes(self) -> None:
        assert format_size(512) == "512 B"

    def test_one_byte_below_kb(self) -> None:
        assert format_size(1023) == "1023 B"

    def test_exactly_one_kb(self) -> None:
        assert format_size(1024) == "1.0 KB"

    def test_kilobytes(self) -> None:
        assert format_size(1536) == "1.5 KB"

    def test_one_byte_below_mb(self) -> None:
        assert format_size(1024 * 1024 - 1) == "1.0 MB"

    def test_exactly_one_mb(self) -> None:
        assert format_size(1024 * 1024) == "1.0 MB"

    def test_megabytes(self) -> None:
        assert format_size(int(2.5 * 1024 * 1024)) == "2.5 MB"

    def test_exactly_one_gb(self) -> None:
        assert format_size(1024 * 1024 * 1024) == "1.0 GB"

    def test_gigabytes(self) -> None:
        assert format_size(int(3.7 * 1024 * 1024 * 1024)) == "3.7 GB"


class TestNextCursorRowAfterDelete:
    def test_keeps_same_visible_index_when_later_rows_remain(self) -> None:
        assert _next_cursor_row_after_delete(deleted_row=4, remaining_count=8) == 4

    def test_moves_to_previous_row_when_deleted_row_was_last(self) -> None:
        assert _next_cursor_row_after_delete(deleted_row=4, remaining_count=4) == 3

    def test_returns_none_when_no_rows_remain(self) -> None:
        assert _next_cursor_row_after_delete(deleted_row=0, remaining_count=0) is None


class TestNextSessionIdAfterDelete:
    def test_prefers_following_visible_session(self) -> None:
        assert _next_session_id_after_delete(["a", "b", "c"], 1) == "c"

    def test_falls_back_to_previous_for_last_row(self) -> None:
        assert _next_session_id_after_delete(["a", "b", "c"], 2) == "b"

    def test_returns_none_for_only_row(self) -> None:
        assert _next_session_id_after_delete(["a"], 0) is None

    def test_returns_none_for_invalid_row(self) -> None:
        assert _next_session_id_after_delete(["a"], 5) is None


@pytest.mark.asyncio
async def test_initial_load_shows_loading_until_the_page_is_ready() -> None:
    store = GatedPageStore(4, free_loads=0)
    store.sessions[3].updated_at = datetime(2026, 1, 2, tzinfo=UTC)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_blocked_loads(store, pilot, 1)

        assert table.row_count == 0
        assert not bool(table.display)
        assert bool(screen.query_one("#sessions-loading-state").display)
        assert isinstance(screen.query_one("#sessions-loading"), ChrysLoadingIndicator)
        assert not bool(screen.query_one("#footer").display)
        assert not bool(screen.query_one("#filters").display)
        assert not bool(screen.query_one("#empty-note").display)
        assert screen.query_one("#container").border_subtitle == "Loading sessions"

        store.release_all()
        await wait_for_row_count(table, pilot, 4)

        assert not bool(screen.query_one("#sessions-loading-state").display)
        assert bool(table.display)
        assert bool(screen.query_one("#footer").display)
        assert screen.query_one("#container").border_subtitle == "4 sessions"
        assert table.get_row_at(0)[0].plain == "session3"
        assert screen._get_selected_session_id() == "session-3"


@pytest.mark.asyncio
async def test_delete_keeps_same_visible_row_when_following_rows_remain() -> None:
    store = FakeSessionStore(8)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 8)

        table.move_cursor(row=4)
        await pilot.pause()
        await confirm_delete(screen, pilot)
        await wait_for_row_count(table, pilot, 7)

        assert table.cursor_coordinate.row == 4
        assert screen._get_selected_session_id() == "session-5"


@pytest.mark.asyncio
async def test_delete_selects_previous_row_when_deleted_row_was_last() -> None:
    store = FakeSessionStore(5)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 5)

        table.move_cursor(row=4)
        await pilot.pause()
        await confirm_delete(screen, pilot)
        await wait_for_row_count(table, pilot, 4)

        assert table.cursor_coordinate.row == 3
        assert screen._get_selected_session_id() == "session-3"


@pytest.mark.asyncio
async def test_delete_leaves_no_selection_when_no_rows_remain() -> None:
    store = FakeSessionStore(1)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 1)

        await confirm_delete(screen, pilot)
        await wait_for_row_count(table, pilot, 0)

        assert screen._get_selected_session_id() is None
        assert bool(screen.query_one("#empty-note").display)
        assert screen.query_one("#resume", Button).disabled
        assert screen.query_one("#delete", Button).disabled


@pytest.mark.asyncio
async def test_delete_other_session_does_not_yank_cursor_to_current() -> None:
    """Post-delete the cursor lands on the neighbor, not the loaded session."""
    store = FakeSessionStore(5)
    screen = SessionsScreen(store, current_session_id="session-0")

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 5)
        assert screen._get_selected_session_id() == "session-0"

        table.move_cursor(row=3)
        await pilot.pause()
        await confirm_delete(screen, pilot)
        await wait_for_row_count(table, pilot, 4)

        assert table.cursor_coordinate.row == 3
        assert screen._get_selected_session_id() == "session-4"


@pytest.mark.asyncio
async def test_delete_after_sort_selects_visible_neighbor_by_session_id() -> None:
    store = FakeSessionStore(4)
    for index, turns in enumerate((40, 10, 20, 30)):
        store.sessions[index].turn_count = turns
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 4)

        _click_header(screen, 4)  # Turns — descending first.
        await pilot.pause()
        assert [table.get_row_at(i)[4].plain for i in range(4)] == ["40", "30", "20", "10"]

        table.move_cursor(row=1)
        await pilot.pause()
        assert screen._get_selected_session_id() == "session-3"

        await confirm_delete(screen, pilot)
        await wait_for_row_count(table, pilot, 3)

        assert [table.get_row_at(i)[4].plain for i in range(3)] == ["40", "20", "10"]
        assert table.cursor_coordinate.row == 1
        assert screen._get_selected_session_id() == "session-2"


@pytest.mark.asyncio
async def test_delete_reload_final_render_preserves_user_cursor_move() -> None:
    store = GatedPageStore(4)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 4)

        table.move_cursor(row=1)
        await pilot.pause()
        await confirm_delete(screen, pilot)
        await wait_for_blocked_loads(store, pilot, 1)
        await wait_for_row_count(table, pilot, 3)
        assert screen._get_selected_session_id() == "session-2"
        # Counted before the reload lands, too.
        assert screen.query_one("#container").border_subtitle == "3 sessions"

        table.move_cursor(row=2)
        await pilot.pause()
        assert screen._get_selected_session_id() == "session-3"

        store.release_all()
        await wait_for_load_idle(screen, pilot)

        assert screen._get_selected_session_id() == "session-3"


@pytest.mark.asyncio
async def test_rapid_delete_while_reload_is_pending_renders_from_the_last_render() -> None:
    store = GatedPageStore(4)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 4)

        table.move_cursor(row=0)
        await pilot.pause()
        await confirm_delete(screen, pilot)
        await wait_for_blocked_loads(store, pilot, 1)
        await wait_for_row_count(table, pilot, 3)
        assert [table.get_row_at(i)[0].plain for i in range(3)] == ["session1", "session2", "session3"]

        table.move_cursor(row=1)
        await pilot.pause()
        await confirm_delete(screen, pilot)
        await wait_for_row_count(table, pilot, 2)

        assert [table.get_row_at(i)[0].plain for i in range(2)] == ["session1", "session3"]
        assert screen._get_selected_session_id() == "session-3"

        # The second delete's reload replaced the first, and reads only once the first's read is done.
        store.release_all()
        await wait_for_blocked_loads(store, pilot, 2)
        store.release_all()
        await wait_for_load_idle(screen, pilot)
        assert [table.get_row_at(i)[0].plain for i in range(2)] == ["session1", "session3"]
        assert screen._get_selected_session_id() == "session-3"
        assert store.most_loads_running == 1
        assert store.cancelled_loads == 0


@pytest.mark.asyncio
async def test_delete_confirmation_uses_neighbor_from_dialog_open_order() -> None:
    store = FakeSessionStore(4)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 4)

        table.move_cursor(row=1)
        await pilot.pause()
        assert screen._get_selected_session_id() == "session-1"
        dialog = await open_delete_dialog(screen, pilot)

        store.sessions[3].updated_at = datetime(2026, 1, 3, tzinfo=UTC)
        screen._render_table()
        await pilot.pause()
        assert [table.get_row_at(i)[0].plain for i in range(4)] == ["session3", "session0", "session1", "session2"]

        dialog.query_one("#confirm-yes", Button).press()
        await wait_for_row_count(table, pilot, 3)

        assert screen._get_selected_session_id() == "session-2"


@pytest.mark.asyncio
async def test_delete_optimistic_render_uses_current_stable_source_after_dialog_refresh() -> None:
    store = GatedPageStore(3)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 3)

        table.move_cursor(row=1)
        await pilot.pause()
        assert screen._get_selected_session_id() == "session-1"
        dialog = await open_delete_dialog(screen, pilot)

        discovered_session = ChatSessionMeta(
            session_id="session-new",
            agent_profile="Code",
            agent_display_name="Code",
            created_at=datetime(2026, 1, 3, tzinfo=UTC),
            updated_at=datetime(2026, 1, 3, tzinfo=UTC),
            message_count=1,
            title="Session New",
        )
        store.sessions.append(discovered_session)
        screen._render_table(source_sessions=list(store.sessions))
        await pilot.pause()
        assert screen._session_ids == ["session-new", "session-0", "session-1", "session-2"]

        dialog.query_one("#confirm-yes", Button).press()
        await wait_for_blocked_loads(store, pilot, 1)
        await wait_for_row_count(table, pilot, 3)

        assert screen._session_ids == ["session-new", "session-0", "session-2"]
        assert screen._get_selected_session_id() == "session-2"

        store.release_all()
        await wait_for_load_idle(screen, pilot)


@pytest.mark.asyncio
async def test_delete_session_cancel_leaves_row_intact() -> None:
    store = FakeSessionStore(2)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 2)

        dialog = await open_delete_dialog(screen, pilot)
        message = dialog.query_one("#confirm-message", Static)
        confirm = dialog.query_one("#confirm-yes", Button)
        assert message.render().plain == 'Delete session\n"session0"?\n\nThis cannot be undone.'
        assert str(confirm.label) == "Delete"
        assert confirm.variant == "error"

        dialog.query_one("#confirm-no", Button).press()
        await pilot.pause()

        assert table.row_count == 2
        assert [session.session_id for session in store.sessions] == ["session-0", "session-1"]


class _UndeletableSessionStore(FakeSessionStore):
    async def delete_session(self, session_id: str, *, allow_active: bool = False) -> None:
        raise OSError(errno.ENOTEMPTY, "The directory is not empty", f"sessions/{session_id}/trajectory")


@pytest.mark.asyncio
async def test_a_session_that_cannot_be_deleted_stays_listed_with_an_error() -> None:
    store = _UndeletableSessionStore(2)
    screen = SessionsScreen(store)
    notified: list[tuple[str, str, str, bool]] = []

    def notify(
        message: str, *, title: str = "", severity: str = "information", timeout: float | None = None, markup: bool
    ) -> None:
        notified.append((severity, title, message, markup))

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 2)
        screen.notify = notify  # type: ignore[method-assign]

        await confirm_delete(screen, pilot)
        # A failure the screen does not handle ends the App instead.
        await wait_for(lambda: notified or not pilot.app.is_running, pilot=pilot, description="delete failure reported")

        assert pilot.app.is_running
        assert notified == [
            (
                "error",
                "Delete Session",
                "The session could not be fully deleted. Close any program that may have its files open, then try again.",
                False,
            )
        ]
        assert table.row_count == 2
        assert screen._get_selected_session_id() == "session-0"


@pytest.mark.asyncio
async def test_delete_session_escape_cancels_confirmation() -> None:
    store = FakeSessionStore(2)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 2)

        await open_delete_dialog(screen, pilot)
        await pilot.press("escape")
        await pilot.pause()

        assert table.row_count == 2
        assert [session.session_id for session in store.sessions] == ["session-0", "session-1"]


@pytest.mark.asyncio
async def test_delete_current_session_requires_confirmation_before_dismiss() -> None:
    store = FakeSessionStore(2)
    screen = SessionsScreen(store, current_session_id="session-0")
    results: list[str | None] = []

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen, callback=results.append)
        table = screen.query_one("#sessions", DataTable)
        await wait_for_row_count(table, pilot, 2)

        dialog = await open_delete_dialog(screen, pilot)

        assert results == []
        dialog.query_one("#confirm-yes", Button).press()

        await _wait_for_results(results, pilot, 1)

    assert results == [f"{SessionsScreen.DELETE_AND_NEW_PREFIX}session-0"]
    assert [session.session_id for session in store.sessions] == ["session-0", "session-1"]


@pytest.mark.asyncio
async def test_sessions_table_treats_session_metadata_as_plain_text() -> None:
    store = FakeSessionStore(1)
    store.sessions[0].agent_profile_history = ["Code [/home/jack]"]
    store.sessions[0].primary_cwd = "/tmp/[/project]"
    store.sessions[0].title = "Review crash from [/home/jack]"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 1)

        cells = table.get_row_at(0)
        assert all(isinstance(cell, Text) for cell in cells)
        assert cells[1].plain == "Review crash from [/home/jack]"
        assert cells[2].plain == "project]"
        assert isinstance(table._row_tooltips[0], Text)
        last_interaction = store.sessions[0].updated_at.astimezone().strftime("%Y/%m/%d %H:%M")
        assert table._row_tooltips[0].plain == (
            "Title: Review crash from [/home/jack]\n"
            "Agent: Code [/home/jack]\n"
            "Directory: /tmp/[/project]\n"
            "Turns: 0\n"
            "Total tokens: 0\n"
            f"Last interaction: {last_interaction}\n"
            "Last used in: TUI"
        )


def _click_header(screen: SessionsScreen, column_index: int) -> None:
    """Simulate a header click by posting the DataTable message."""
    table = screen.query_one("#sessions", _SessionTable)
    column = table.ordered_columns[column_index]
    table.post_message(DataTable.HeaderSelected(table, column.key, column_index, label=column.label))


@pytest.mark.asyncio
async def test_header_click_sorts_and_click_again_reverses() -> None:
    store = FakeSessionStore(3)
    store.sessions[0].turn_count = 5
    store.sessions[1].turn_count = 20
    store.sessions[2].turn_count = 1
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 3)

        _click_header(screen, 4)  # Turns — numeric, descending first
        await pilot.pause()
        assert [table.get_row_at(i)[4].plain for i in range(3)] == ["20", "5", "1"]
        assert "↓" in str(table.ordered_columns[4].label)

        _click_header(screen, 4)
        await pilot.pause()
        assert [table.get_row_at(i)[4].plain for i in range(3)] == ["1", "5", "20"]
        assert "↑" in str(table.ordered_columns[4].label)


@pytest.mark.asyncio
async def test_header_click_keeps_selected_session() -> None:
    store = FakeSessionStore(4)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 4)

        table.move_cursor(row=2)
        await pilot.pause()
        selected = screen._get_selected_session_id()

        _click_header(screen, 0)  # Session ID ascending
        await pilot.pause()
        assert screen._get_selected_session_id() == selected


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["textual-dark", "ansi-dark", "ansi-light"])
async def test_search_filters_rows_and_highlights_matches(theme: str) -> None:
    from textual.widgets import Input

    store = FakeSessionStore(5)
    store.sessions[3].title = "unique needle title"
    screen = SessionsScreen(store)

    app = SessionsHostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        app.theme = theme
        await app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 5)

        screen.query_one("#search", Input).value = "needle"
        await wait_for_row_count(table, pilot, 1)

        title_cell = table.get_row_at(0)[1]
        assert title_cell.plain == "unique needle title"
        assert screen.query_one("#container").border_subtitle == "1/5 sessions"
        # Match style is reverse-video in the theme's warning color.
        warning = app.theme_variables["warning"]
        expected_style = rich_style_from_textual_color(warning, reverse=True)
        match_spans = [span for span in title_cell.spans if span.style == expected_style]
        assert match_spans
        assert screen._get_selected_session_id() == "session-3"

        screen.query_one("#search", Input).value = ""
        await wait_for_row_count(table, pilot, 5)


@pytest.mark.asyncio
async def test_search_highlights_the_query_only_where_it_searches() -> None:
    from textual.widgets import Input

    store = FakeSessionStore(3)
    store.sessions[1] = replace(store.sessions[1], title="mb converter", size_bytes=3 * 1024 * 1024)
    screen = SessionsScreen(store)

    app = SessionsHostApp()
    async with app.run_test(size=(120, 40)) as pilot:
        await app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 3)

        screen.query_one("#search", Input).value = "mb"
        await wait_for_row_count(table, pilot, 1)

        cells = dict(zip((column.key for column in screen._columns), table.get_row_at(0), strict=True))
        match_style = rich_style_from_textual_color(app.theme_variables["warning"], reverse=True)
        assert (cells["title"].plain, cells["size"].plain) == ("mb converter", "3.0 MB")
        assert [span for span in cells["title"].spans if span.style == match_style]
        # Size is not searched, so its "MB" is no match.
        assert not [span for span in cells["size"].spans if span.style == match_style]


@pytest.mark.asyncio
async def test_search_escape_clears_then_returns_focus_to_table() -> None:
    from textual.widgets import Input

    store = FakeSessionStore(2)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 2)

        search = screen.query_one("#search", Input)
        search.focus()
        await wait_for(lambda: search.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()
        search.value = "session"
        await pilot.pause()

        await pilot.press("escape")
        assert search.value == ""
        assert screen.app.focused is search
        assert isinstance(screen.app.screen, SessionsScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert screen.app.focused is table
        assert isinstance(screen.app.screen, SessionsScreen)


@pytest.mark.asyncio
async def test_forked_sessions_nest_under_parent_with_tree_guides() -> None:
    store = FakeSessionStore(3)
    # session-2 is a fork of session-0; forks carry the parent's full id.
    store.sessions[2].parent_session_id = "session-0"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 3)

        # Default sort: newest first — session-0 root, its fork right below.
        first_column = [table.get_row_at(i)[0].plain for i in range(3)]
        assert first_column == ["session0", "└ session2", "session1"]
        assert "Forked from: session0" in table._row_tooltips[1].plain


async def test_session_titles_are_shown_without_terminal_controls() -> None:
    store = FakeSessionStore(1)
    store.sessions[0] = replace(store.sessions[0], title="Bad\x1b[2Jtitle\udcff")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        cells = [str(cell) for cell in screen.query_one("#sessions", DataTable).get_row_at(0)]

    # The control is replaced; the undecodable byte shows as its escape.
    assert any(cell.endswith("Bad\ufffd[2Jtitle\\udcff") for cell in cells)
