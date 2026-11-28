# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Workspace column of the sessions browser: display, search, sorting and its absence in workflows."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.widgets import DataTable, Input

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.sessions.presenter import WORKFLOW_COLUMNS, column_by_key
from chrys.app.tui.screens.sessions.screen import SessionsScreen, _elide_middle_whole, _SessionTable
from chrys.app.tui.util.formatting import elide_end
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.foundation.config.settings import Settings
from chrys.service.state.store import WorkflowSessionMeta
from chrys.service.workflows.history import WorkflowRunMeta
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.pilot_barrier import screen_is_settled
from tests.support.sessions_browser import (
    FakeSessionStore,
    GatedPageStore,
    SessionsHostApp,
    wait_for_blocked_loads,
    wait_for_load_idle,
    wait_for_row_count,
)
from tests.support.toasts import shown_toasts
from tests.support.tui_helpers import resize_when_settled
from tests.support.waiting import wait_for
from tests.support.workspace_registry import make_workdirs
from tests.support.workspace_registry import registry as registry

_WIDE = (160, 40)
"""A screen where the table leaves the Workspace column its whole 16-cell cap."""


def _assigned(registry: WorkspaceRegistry, store: FakeSessionStore, index: int, workspace_id: str | None) -> None:
    registry.set_session_workspace(store.sessions[index].session_id, workspace_id)


def _click_header(screen: SessionsScreen, column_index: int) -> None:
    """Click a column header the way DataTable reports it."""
    table = screen.query_one("#sessions", _SessionTable)
    column = table.ordered_columns[column_index]
    table.post_message(DataTable.HeaderSelected(table, column.key, column_index, label=column.label))


@pytest.mark.asyncio
async def test_the_workspace_column_leads_the_table_and_shows_the_registered_name(
    registry: WorkspaceRegistry, tmp_path: Path
) -> None:
    main, former = make_workdirs(tmp_path, "main", "former")
    workspace = registry.create("Lunar Base", str(main))
    removed = registry.create("Removed", str(former))
    store = FakeSessionStore(3)
    _assigned(registry, store, 0, workspace.id)
    _assigned(registry, store, 1, removed.id)
    registry.delete(removed.id)
    screen = SessionsScreen(store, workspaces=registry)

    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_load_idle(screen, pilot)

        assert table.ordered_columns[0].key.value == "workspace"
        assert table.ordered_columns[0].label.plain == "Workspace"
        # A session with no saved record is Unspecified, as on the workspaces page.
        assert [table.get_row_at(index)[0].plain for index in range(3)] == [
            "Lunar Base",
            "Unspecified",
            "Unspecified",
        ]
        assert screen.query_one("#search", Input).placeholder == (
            "Search all sessions… (workspaces, IDs, titles, directories & your prompts)"
        )


@pytest.mark.asyncio
async def test_an_unreadable_registry_hides_the_workspace_column_and_warns_once(
    registry: WorkspaceRegistry, tmp_path: Path
) -> None:
    workspace = registry.create("Hidden", str(tmp_path))
    store = FakeSessionStore(1)
    _assigned(registry, store, 0, workspace.id)
    registry.path.write_text("damaged", encoding="utf-8")
    screen = SessionsScreen(store, workspaces=registry)

    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_load_idle(screen, pilot)

        assert "workspace" not in {str(column.key.value) for column in table.ordered_columns}
        error = registry.snapshot().error
        assert error is not None
        warnings = [(str(toast.message), toast.severity, toast.markup) for toast in shown_toasts(pilot.app)]
        assert warnings == [(screen._render_message(error.display_message), "warning", False)]
        screen._request_page(1)
        await wait_for_load_idle(screen, pilot)
        assert len(shown_toasts(pilot.app)) == 1


@pytest.mark.parametrize(
    ("registry_state", "assigned"),
    [("readable", True), ("readable", False), ("unreadable", True), ("missing", True)],
)
async def test_the_running_session_uses_its_effective_workspace_for_display_search_and_sort(
    registry: WorkspaceRegistry, tmp_path: Path, registry_state: str, assigned: bool
) -> None:
    """J₀ wins over disk membership, including an empty J₀ and an unavailable registry."""
    workspace = registry.create("Alpha", str(tmp_path))
    store = FakeSessionStore(2)
    # Make disk disagree with the running session in both directions.
    _assigned(registry, store, 0, None if assigned else workspace.id)
    _assigned(registry, store, 1, workspace.id)
    if registry_state == "unreadable":
        registry.path.write_text("broken", encoding="utf-8")
    elif registry_state == "missing":
        registry.path.unlink()
    expected = "Alpha" if assigned else "Unspecified"
    screen = SessionsScreen(
        store, current_session_id="session-0", workspaces=registry, current_workspace_name="Alpha" if assigned else None
    )
    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        table = screen.query_one("#sessions", _SessionTable)
        if registry_state != "readable":
            assert table.ordered_columns[0].label.plain == "Session ID"
            return
        assert table.get_row("session-0")[0].plain == expected
        assert table.get_row("session-1")[0].plain == "Alpha"
        _click_header(screen, 0)
        await wait_for(
            lambda: (
                [table.get_row_at(index)[0].plain for index in range(2)]
                == sorted([table.get_row("session-0")[0].plain, table.get_row("session-1")[0].plain], key=str.casefold)
            ),
            pilot=pilot,
        )
        screen.query_one("#search", Input).value = expected
        await wait_for(lambda: bool(table.get_row("session-0")[0].spans), pilot=pilot)
        assert table.get_row("session-0")[0].plain == expected


async def test_the_directory_column_shows_the_folder_a_restore_opens(
    registry: WorkspaceRegistry, tmp_path: Path
) -> None:
    """A workspace session lists its workspace's registered primary folder, the running one too."""
    primary = tmp_path / "team-main"
    primary.mkdir()
    workspace = registry.create("Team", str(primary))
    store = FakeSessionStore(3)
    for session, folder in zip(store.sessions, ("/old/saved-dir", "/old/saved-too", "/plain/own-dir"), strict=True):
        session.primary_cwd = folder
    _assigned(registry, store, 0, workspace.id)
    _assigned(registry, store, 1, workspace.id)
    screen = SessionsScreen(store, current_session_id="session-1", workspaces=registry, current_workspace_name="Team")
    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 3)
        directory = table.get_column_index("directory")
        shown = {key: table.get_row(key)[directory].plain for key in ("session-0", "session-1", "session-2")}
        assert shown == {"session-0": "team-main", "session-1": "team-main", "session-2": "own-dir"}
        tooltip = screen._row_tooltip(next(row for row in screen._rows if row.key == "session-0")).plain
        assert "team-main" in tooltip and "saved-dir" not in tooltip

        screen.query_one("#search", Input).value = "saved-dir"
        await wait_for(lambda: screen._shown_query == "saved-dir" and not screen._loading, pilot=pilot)
        assert table.row_count == 0
        screen.query_one("#search", Input).value = "team-main"
        await wait_for_row_count(table, pilot, 2)
        assert sorted(table.rows) == ["session-0", "session-1"]


async def test_a_browser_keeps_its_membership_snapshot_until_the_next_open(
    registry: WorkspaceRegistry, tmp_path: Path
) -> None:
    """Search uses the opening read; opening F1 again sees a later membership change."""
    first, second = make_workdirs(tmp_path, "first", "second")
    alpha = registry.create("Alpha", str(first))
    beta = registry.create("Beta", str(second))
    store = FakeSessionStore(1)
    _assigned(registry, store, 0, alpha.id)
    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        screen = SessionsScreen(store, workspaces=registry)
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        registry.set_session_workspace("session-0", beta.id)
        screen.query_one("#search", Input).value = "Alpha"
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for(lambda: bool(table.get_row_at(0)[0].spans), pilot=pilot)
        assert table.get_row_at(0)[0].plain == "Alpha"
        await pilot.press("escape")
        reopened = SessionsScreen(store, workspaces=registry)
        await pilot.app.push_screen(reopened)
        await wait_for_load_idle(reopened, pilot)
        assert reopened.query_one("#sessions", _SessionTable).get_row_at(0)[0].plain == "Beta"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "query", "size", "shown"),
    [
        ("Lunar Base", "lunar", _WIDE, "Lunar Base"),
        ("Lazy-Theta-with-optimization", "optimization", _WIDE, "Lazy-Theta-with…"),
    ],
    ids=["whole-name", "truncated-name"],
)
async def test_searching_a_workspace_name_finds_its_sessions_and_highlights_the_cell(
    registry: WorkspaceRegistry, tmp_path: Path, name: str, query: str, size: tuple[int, int], shown: str
) -> None:
    """Search reads the whole name even when the visible cell ends in an ellipsis."""
    workspace = registry.create(name, str(tmp_path))
    store = FakeSessionStore(3)
    _assigned(registry, store, 1, workspace.id)
    app = SessionsHostApp()
    screen = SessionsScreen(store, workspaces=registry)

    async with app.run_test(size=size) as pilot:
        await app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 3)

        screen.query_one("#search", Input).value = query
        await wait_for_row_count(table, pilot, 1)

        assert list(table.rows) == ["session-1"]
        assert "session-0" not in table.rows and "session-2" not in table.rows
        workspace_cell = table.get_row_at(0)[0]
        assert workspace_cell.plain == shown
        if shown == name:
            match_style = rich_style_from_textual_color(app.theme_variables["warning"], reverse=True)
            assert [span for span in workspace_cell.spans if span.style == match_style]
        else:
            assert query not in workspace_cell.plain


@pytest.mark.asyncio
async def test_the_workspace_header_sorts_by_name_ignoring_case(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    first, second, third = make_workdirs(tmp_path, "first", "second", "third")
    # Case-sensitive order would be Zulu, alpha, beta.
    zulu = registry.create("Zulu", str(first))
    alpha = registry.create("alpha", str(second))
    beta = registry.create("beta", str(third))
    store = FakeSessionStore(3)
    _assigned(registry, store, 0, zulu.id)
    _assigned(registry, store, 1, alpha.id)
    _assigned(registry, store, 2, beta.id)
    screen = SessionsScreen(store, workspaces=registry)

    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 3)

        _click_header(screen, 0)  # Workspace — ascending first.
        await pilot.pause()
        assert [table.get_row_at(index)[0].plain for index in range(3)] == ["alpha", "beta", "Zulu"]
        assert "↑" in str(table.ordered_columns[0].label)

        _click_header(screen, 0)
        await pilot.pause()
        assert [table.get_row_at(index)[0].plain for index in range(3)] == ["Zulu", "beta", "alpha"]
        assert "↓" in str(table.ordered_columns[0].label)


@pytest.mark.asyncio
async def test_the_workflow_tab_has_no_workspace_column(registry: WorkspaceRegistry, tmp_path: Path) -> None:
    registry.create("Team", str(tmp_path))
    store = FakeSessionStore(0)
    updated = datetime(2026, 1, 1, tzinfo=UTC)
    store.sessions = [
        WorkflowSessionMeta(
            session_id="flow-0",
            created_at=updated,
            updated_at=updated,
            run_count=1,
            latest_run_id="run",
            latest_run=WorkflowRunMeta("run", "review", "Review", "completed", updated, "do it"),
        )
    ]
    screen = SessionsScreen(store, workflow_mode=True, workspaces=registry)

    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_load_idle(screen, pilot)

        assert "workspace" not in [column.key.value for column in table.ordered_columns]
        assert [column.key.value for column in table.ordered_columns] == [column.key for column in WORKFLOW_COLUMNS]
        assert len(table.get_row_at(0)) == len(WORKFLOW_COLUMNS)


class _ChineseSessionsHost(SessionsHostApp):
    locale_controller = LocaleController(Settings(locale="zh-Hans"))


@pytest.mark.asyncio
async def test_the_workspace_header_and_unspecified_are_localized(registry: WorkspaceRegistry, tmp_path) -> None:
    registry.create("Team", str(tmp_path))
    screen = SessionsScreen(FakeSessionStore(1), workspaces=registry)

    async with _ChineseSessionsHost().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_load_idle(screen, pilot)

        assert table.ordered_columns[0].label.plain == "工作区"
        assert table.get_row_at(0)[0].plain == "未指定"


@pytest.mark.parametrize(
    ("host", "query", "placeholder"),
    [
        (SessionsHostApp, "spec", "Search all sessions… (IDs, titles, directories & your prompts)"),
        (_ChineseSessionsHost, "指定", "搜索全部会话…（匹配 ID、标题、目录和你的提示词）"),  # noqa: RUF001
    ],
)
async def test_empty_registry_uses_the_original_search_fields(registry, host, query, placeholder):
    store = FakeSessionStore(1)
    store.sessions[0].title = "Fix parser"
    screen = SessionsScreen(store, workspaces=registry, current_session_id=store.sessions[0].session_id)
    async with host().run_test() as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one(_SessionTable)
        await wait_for_row_count(table, pilot, 1)
        search = screen.query_one("#search", Input)
        assert search.placeholder == placeholder
        search.value = query
        await wait_for_row_count(table, pilot, 0)


@pytest.mark.parametrize("rows", [3, 60], ids=["fits", "scrolls-down"])
@pytest.mark.parametrize(
    ("terminal", "inner", "directory", "registered", "workspace_width", "title", "directory_width", "overflow"),
    [
        (80, 66, "abcdefghijklmnopqrstuvwx", True, 11, 30, None, True),
        (120, 100, "work", True, 11, 26, None, False),
        (120, 100, "abcdefghijklmnopqrstuvwx", True, 11, 21, 14, False),
        (160, 118, "abcdefghijklmnopqrstuvwx", True, 11, 30, 23, False),
        (120, 100, "abcdefghijklmnopqrstuvwx", "A very long workspace name", 16, 30, None, True),
        (80, 66, "abcdefghijklmnopqrstuvwx", False, None, 30, None, True),
        (120, 100, "abcdefghijklmnopqrstuvwx", False, None, 30, None, True),
        (160, 118, "abcdefghijklmnopqrstuvwx", False, None, 30, None, False),
    ],
)
async def test_workspace_column_widths_and_horizontal_overflow(
    registry, tmp_path, rows, terminal, inner, directory, registered, workspace_width, title, directory_width, overflow
):
    """Borrow only when every column fits; a slow load and a vertical scrollbar do not change widths."""
    workspace = (
        registry.create(registered if isinstance(registered, str) else "Team", str(tmp_path)) if registered else None
    )
    store = GatedPageStore(rows, free_loads=0)
    if isinstance(registered, str):
        for index in range(rows):
            _assigned(registry, store, index, workspace.id)
    for meta in store.sessions:
        meta.primary_cwd = f"/projects/{directory}"
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=(terminal, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_blocked_loads(store, pilot, 1)
        store.release_all()
        await wait_for_load_idle(screen, pilot)
        await wait_for(lambda: screen_is_settled(pilot.app, screen), pilot=pilot)
        table = screen.query_one(_SessionTable)
        assert screen.query_one("#container").content_region.width == inner
        expected = {"id": 12, "title": title, "directory": directory_width, "last_active": 13, "turns": 5, "size": 9}
        if registered:
            expected = {"workspace": workspace_width, **expected}
        assert screen._column_layout(screen._rows)[1] == screen._column_widths == expected
        assert [column.key.value for column in table.ordered_columns] == list(expected)
        assert (table.max_scroll_x > 0) is overflow
        for column in table.ordered_columns:
            width = expected[column.key.value]
            assert column.auto_width is (width is None)
            if width is not None:
                assert column.width == width
            else:
                assert column.content_width == max(cell_len(column.label.plain), cell_len(directory))
        if registered:
            shown = workspace.name if isinstance(registered, str) else "Unspecified"
            assert table.get_row_at(0)[0].plain == elide_end(shown, workspace_width)
        if overflow:
            table.scroll_to(x=table.max_scroll_x, animate=False, force=True)
            await wait_for(lambda: table.scroll_x > 0, pilot=pilot)


async def test_a_resize_behind_the_delete_confirmation_refits_on_return(registry, tmp_path):
    registry.create("Team", str(tmp_path))
    store = FakeSessionStore(3)
    for meta in store.sessions:
        meta.primary_cwd = "/projects/abcdefghijklmnopqrstuvwx"
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=(160, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await wait_for(lambda: screen._column_widths["title"] == 30, pilot=pilot)
        screen.action_delete_session()
        await wait_for(lambda: pilot.app.screen is not screen, pilot=pilot)
        await resize_when_settled(pilot, 120, 40)
        await wait_for(lambda: screen.query_one("#container").content_region.width == 100, pilot=pilot)
        assert pilot.app.screen is not screen and screen._column_widths["title"] == 30
        await pilot.press("escape")
        await wait_for(lambda: pilot.app.screen is screen, pilot=pilot)
        await wait_for(lambda: screen._column_widths["title"] == 21, pilot=pilot)
        assert screen._column_widths["directory"] == 14
        assert store.sessions and len(screen._session_ids) == 3


async def test_truncated_directory_and_workspace_matches_remain_in_tooltip(registry, tmp_path):
    primary = tmp_path / "prefix-needle-long-tail"
    primary.mkdir()
    workspace = registry.create("Workspace needle suffix", str(primary))
    store = FakeSessionStore(1)
    _assigned(registry, store, 0, workspace.id)
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=(130, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        screen.query_one("#search", Input).value = "needle"
        await wait_for(lambda: screen._shown_query == "needle" and not screen._loading, pilot=pilot)
        table = screen.query_one(_SessionTable)
        directory = table.get_row_at(0)[table.get_column_index("directory")]
        assert "…" in directory.plain and "needle" not in directory.plain
        assert cell_len(directory.plain) == screen._column_widths["directory"]
        for width in (130, 80):
            await resize_when_settled(pilot, width, 40)
            tooltip = screen._row_tooltip(screen._rows[0])
            assert "Workspace needle suffix" in tooltip.plain
            assert primary.name in tooltip.plain
            assert "Session 0" in tooltip.plain
            spans = [span for span in tooltip.spans if span.style == screen._match_style()]
            assert [tooltip.plain[span.start : span.end] for span in spans] == ["needle", "needle"]
        # A query that also spells a label marks only the value.
        screen.query_one("#search", Input).value = "Work"
        await wait_for(lambda: screen._shown_query == "Work" and not screen._loading, pilot=pilot)
        tooltip = screen._row_tooltip(screen._rows[0])
        spans = [span for span in tooltip.spans if span.style == screen._match_style()]
        label = tooltip.plain.index("Workspace: ")
        assert [tooltip.plain[span.start : span.end] for span in spans] == ["Work"]
        assert spans[0].start == label + len("Workspace: ")


async def test_a_refreshed_or_scrolled_list_drops_the_shown_tooltip(registry):
    """The hovered row may hold another session after a search or a scroll: its old tooltip goes."""
    store = FakeSessionStore(40)
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=(130, 24)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        table = screen.query_one(_SessionTable)

        async def hover_first_row() -> str:
            await pilot.hover(table, offset=(30, 1))
            await wait_for(lambda: table.tooltip is not None, pilot=pilot)
            return str(table.tooltip)

        assert "Session 0" in await hover_first_row()
        screen.query_one("#search", Input).value = "Session 7"
        await wait_for(lambda: screen._shown_query == "Session 7" and not screen._loading, pilot=pilot)
        assert table.tooltip is None
        assert "Session 7" in await hover_first_row()

        screen.query_one("#search", Input).value = ""
        await wait_for(lambda: screen._shown_query == "" and not screen._loading, pilot=pilot)
        await hover_first_row()
        table.scroll_to(y=5, animate=False)
        await wait_for(lambda: table.scroll_y == 5, pilot=pilot)
        assert table.tooltip is None


async def test_resize_refits_borrowed_widths_in_place(registry, tmp_path, monkeypatch):
    """A resize changes only the columns whose width changed: no rebuild, and cursor, scroll and sort stay."""
    workspace = registry.create("Zulu", str(tmp_path))
    store = FakeSessionStore(100)
    _assigned(registry, store, 0, workspace.id)
    for meta in store.sessions:
        meta.primary_cwd = "/projects/abcdefghijklmnopqrstuvwx"
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        table = screen.query_one(_SessionTable)
        _click_header(screen, 0)
        await wait_for(lambda: screen._sort_column == "workspace" and screen_is_settled(pilot.app, screen), pilot=pilot)
        table.move_cursor(row=55, scroll=False)
        table.scroll_to(y=45, animate=False, force=True)
        await wait_for(lambda: table.scroll_y == 45 and screen_is_settled(pilot.app, screen), pilot=pilot)
        selected = screen._get_selected_session_id()
        order = list(table.rows)
        title_cell = table.get_cell(selected, "title")
        rebuild = create_autospec(screen._render_table, side_effect=screen._render_table)
        clear = create_autospec(table.clear, side_effect=table.clear)
        update_cell = create_autospec(table.update_cell, side_effect=table.update_cell)
        monkeypatch.setattr(screen, "_render_table", rebuild)
        monkeypatch.setattr(table, "clear", clear)
        monkeypatch.setattr(table, "update_cell", update_cell)
        previous = dict(screen._column_widths)
        for width, title, directory, shown in (
            (120, 23, 14, "abcdefg…stuvwx"),
            (80, 30, None, "abcdefghijklmnopqrstuvwx"),
            (160, 30, None, "abcdefghijklmnopqrstuvwx"),
        ):
            update_cell.reset_mock()
            await resize_when_settled(pilot, width, 40)
            await wait_for(lambda: screen_is_settled(pilot.app, screen), pilot=pilot)
            widths = screen._column_widths
            assert (widths["workspace"], widths["title"], widths["directory"]) == (11, title, directory)
            changed = {key for key, value in widths.items() if value != previous[key]}
            assert {call.args[1] for call in update_cell.call_args_list} <= changed
            previous = dict(widths)
            assert table.get_cell(selected, "directory").plain == shown
            assert table.get_cell(selected, "title") is title_cell
            assert (table.max_scroll_x > 0) is (width == 80)
            assert screen._get_selected_session_id() == selected
            assert table.cursor_row == 55
            assert table.scroll_y == table.scroll_target_y == 45
            assert screen._sort_column == "workspace" and screen._sort_reverse is False
            assert list(table.rows) == order
        rebuild.assert_not_called()
        clear.assert_not_called()


async def test_opening_starts_listing_before_the_registry_read_finishes(registry, monkeypatch):
    import asyncio
    from threading import Event

    entered, release = Event(), Event()
    listing_started = asyncio.Event()
    snapshot = registry.snapshot
    store = FakeSessionStore(1)
    open_listing = store.open_session_listing

    def held_snapshot():
        entered.set()
        if not release.wait(5):
            raise TimeoutError("registry read was not released")
        return snapshot()

    async def listing(*, kind):
        listing_started.set()
        return await open_listing(kind=kind)

    monkeypatch.setattr(registry, "snapshot", held_snapshot)
    monkeypatch.setattr(store, "open_session_listing", listing)
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        try:
            await pilot.app.push_screen(screen)
            await wait_for(entered.is_set, pilot=pilot)
            await wait_for(listing_started.is_set, pilot=pilot)
            assert not release.is_set()
        finally:
            release.set()
        await wait_for_load_idle(screen, pilot)


@pytest.mark.parametrize(
    ("text", "width", "middle"),
    [
        ("client🇨🇳project", 14, "client…roject"),
        ("ab☀️☀️☀️☀️☀️cd", 8, "ab☀️…cd"),
        ("ab👨\u200d👩\u200d👧-👨\u200d👩\u200d👧cd", 7, "ab…cd"),
    ],
)
def test_a_narrowed_directory_keeps_whole_graphemes_within_its_cells(text, width, middle):
    assert _elide_middle_whole(text, width) == middle
    for budget in range(cell_len(text) + 1):
        assert cell_len(_elide_middle_whole(text, budget)) <= budget


async def test_a_narrowed_directory_cuts_the_whole_folder_name(registry, tmp_path):
    """The 24-cell search text can end in half a flag; the narrowed cell starts from the folder name."""
    registry.create("Team", str(tmp_path))
    store = FakeSessionStore(1)
    store.sessions[0].primary_cwd = "/work/workbench-data-🇨🇳-build2026"
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=_WIDE) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        row = screen._rows[0]
        assert row.cells["directory"].count("🇨") != row.cells["directory"].count("🇳")
        screen._column_widths["directory"] = 23
        cell = screen._cell_text(row, column_by_key("directory"), "", screen._match_style()).plain
        assert cell == "workbench-d…-build2026"


@pytest.mark.parametrize("with_workspace", [False, True])
async def test_a_directory_fragment_still_finds_its_session(registry, tmp_path, with_workspace):
    """Narrowing the shown Directory cell must not change which sessions a query finds."""
    if with_workspace:
        registry.create("Team", str(tmp_path))
    store = FakeSessionStore(2)
    store.sessions[0].primary_cwd = "/work/workbench-☀️x-build-output"
    screen = SessionsScreen(store, workspaces=registry)
    async with SessionsHostApp().run_test(size=(130, 40)) as pilot:
        await pilot.app.push_screen(screen)
        table = screen.query_one("#sessions", _SessionTable)
        await wait_for_row_count(table, pilot, 2)
        screen.query_one("#search", Input).value = "☀️x"
        await wait_for_row_count(table, pilot, 1)
        assert list(table.rows) == ["session-0"]
