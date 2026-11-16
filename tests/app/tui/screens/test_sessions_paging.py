# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Surface filters, paging, the search and the empty states of the sessions browser."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from textual.message import Message
from textual.widgets import Button, DataTable, Input, Static

from chrys.app.tui.screens.sessions import screen as sessions_screen
from chrys.app.tui.screens.sessions.screen import SessionsScreen, _SessionTable
from chrys.app.tui.widgets import Checkbox, HatchedEmptyState, PageNavigator
from chrys.foundation.models.session_surface import SessionSurface
from chrys.service.state.session_listing import SESSION_PAGE_SIZE
from chrys.service.state.store import SessionMeta, WorkflowSessionMeta
from chrys.service.workflows.history import WorkflowRunMeta
from chrys.service.workflows.outcomes import RunStatus
from tests.support.sessions_browser import (
    FakeSessionStore,
    GatedPageStore,
    SessionsHostApp,
    confirm_delete,
    wait_for_blocked_loads,
    wait_for_load_idle,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

TUI, CLI, ACP = SessionSurface.TUI, SessionSurface.CLI, SessionSurface.ACP


def _checked(screen: SessionsScreen) -> dict[str, bool]:
    return {surface.value: screen.query_one(f"#surface-{surface.value}", Checkbox).value for surface in SessionSurface}


def _pager(screen: SessionsScreen) -> tuple[bool, str, bool]:
    """Previous disabled, the page label, Next disabled."""
    navigator = screen.query_one("#session-pages", PageNavigator)
    return (
        navigator.query_one("#previous-page", Button).disabled,
        str(navigator.query_one("#page-number", Static).content),
        navigator.query_one("#next-page", Button).disabled,
    )


def _subtitle(screen: SessionsScreen) -> str:
    return str(screen.query_one("#container").border_subtitle)


def _note(screen: SessionsScreen) -> str | None:
    """The empty note's text while it shows."""
    note = screen.query_one("#empty-note", HatchedEmptyState)
    return note.label if note.display else None


def _controls_shown(screen: SessionsScreen) -> bool:
    return bool(screen.query_one("#filters").display) and bool(screen.query_one("#footer").display)


async def _click_pager(pilot, screen: SessionsScreen, button: str) -> None:
    """Click Previous or Next: Textual ignores a click while the button still shows its last press."""
    target = screen.query_one(f"#session-pages #{button}", Button)
    await wait_for(lambda: not target.has_class("-active"), pilot=pilot, description=f"{button} takes a click")
    await click_when_settled(pilot, target)


async def _wait_for_page(screen: SessionsScreen, pilot, page: int) -> None:
    await wait_for(
        lambda: screen._page == page and not screen._loading and screen._loading_request is None,
        pilot=pilot,
        description=f"page {page} is loaded",
    )


def _search_box(screen: SessionsScreen) -> Input:
    return screen.query_one("#search", Input)


async def _search(screen: SessionsScreen, pilot, query: str) -> None:
    """Type *query* and wait until its results show."""
    _search_box(screen).value = query
    await _wait_for_results(screen, pilot, query)


async def _submit_search(screen: SessionsScreen, pilot, query: str) -> None:
    """Type *query*, press Enter and wait until its results show."""
    search = _search_box(screen)
    search.focus()
    await wait_for(lambda: search.has_focus, pilot=pilot, description="the search box has focus")
    search.value = query
    await pilot.press("enter")
    await _wait_for_results(screen, pilot, query)


async def _wait_for_results(screen: SessionsScreen, pilot, query: str) -> None:
    await wait_for(
        lambda: screen._shown_query == query.strip() and not screen._loading and screen._loading_request is None,
        pilot=pilot,
        description=f"the results for {query!r} show",
    )


def _titled(store: FakeSessionStore, indexes: range, title: str) -> None:
    for index in indexes:
        store.sessions[index].title = f"{title} {index}"


def _workflow_session(index: int, *, status: RunStatus = "completed", input_text: str = "routine") -> SessionMeta:
    updated = datetime(2026, 1, 1, tzinfo=UTC) - timedelta(minutes=index)
    return WorkflowSessionMeta(
        session_id=f"flow-{index}",
        created_at=updated,
        updated_at=updated,
        run_count=1,
        latest_run_id="run",
        latest_run=WorkflowRunMeta("run", "review", "Review", status, updated, input_text),
    )


@pytest.mark.asyncio
async def test_opens_on_the_first_page_of_tui_sessions_counting_unrecorded_ones_as_tui() -> None:
    store = FakeSessionStore(4, surfaces=[CLI, TUI, None, ACP])
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        assert _checked(screen) == {"tui": True, "cli": False, "acp": False}
        assert store.pages_requested == [(frozenset({TUI}), 1)]
        assert screen._session_ids == ["session-1", "session-2"]
        assert _subtitle(screen) == "2 sessions"
        assert _pager(screen) == (True, "Page 1 of 1", True)
        tooltips = screen.query_one("#sessions", _SessionTable)._row_tooltips
        assert tooltips[0].plain.endswith("\nLast used in: TUI")
        assert tooltips[1].plain.endswith("\nLast used in: Unknown")


@pytest.mark.asyncio
async def test_opens_with_the_surfaces_it_is_given() -> None:
    store = FakeSessionStore(3, surfaces=[CLI, TUI, ACP])
    screen = SessionsScreen(store, surfaces={CLI, ACP})

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        assert _checked(screen) == {"tui": False, "cli": True, "acp": True}
        assert screen._session_ids == ["session-0", "session-2"]
        assert screen.query_one("#sessions", _SessionTable)._row_tooltips[1].plain.endswith("\nLast used in: ACP")


@pytest.mark.asyncio
async def test_workflow_sessions_offer_only_the_surfaces_that_start_runs() -> None:
    store = FakeSessionStore(0)
    reported: list[frozenset[SessionSurface]] = []
    screen = SessionsScreen(store, workflow_mode=True, surfaces={CLI, ACP}, on_surfaces_changed=reported.append)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        assert [checkbox.id for checkbox in screen.query("#surface-filters Checkbox")] == [
            "surface-tui",
            "surface-cli",
        ]
        # ACP has no checkbox here, so it could never be unchecked.
        assert store.pages_requested == [(frozenset({CLI}), 1)]
        screen.query_one("#surface-tui", Checkbox).value = True
        await wait_for(lambda: reported == [frozenset({TUI, CLI})], pilot=pilot, description="TUI is added")


@pytest.mark.asyncio
async def test_toggling_a_surface_lists_the_first_page_of_the_new_selection() -> None:
    store = FakeSessionStore(SESSION_PAGE_SIZE + 20, surfaces=[CLI] * 5)
    reported: list[frozenset[SessionSurface]] = []
    screen = SessionsScreen(store, on_surfaces_changed=reported.append)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        assert _subtitle(screen) == "115 sessions"
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        assert screen._session_ids == [f"session-{index}" for index in range(105, 120)]

        await click_when_settled(pilot, "#surface-cli")
        await wait_for(
            lambda: store.pages_requested[-1] == (frozenset({TUI, CLI}), 1) and not screen._loading,
            pilot=pilot,
            description="the first page of TUI and CLI sessions is loaded",
        )

        assert reported == [frozenset({TUI, CLI})]
        assert _pager(screen) == (True, "Page 1 of 2", False)
        assert screen._session_ids[:6] == [f"session-{index}" for index in range(6)]
        assert _subtitle(screen) == "120 sessions"
        # Every load pages through the listing taken when the browser opened.
        assert store.listings_opened == ["chat"]


@pytest.mark.asyncio
async def test_filters_that_match_nothing_keep_the_controls_that_undo_them() -> None:
    store = FakeSessionStore(3, surfaces=[CLI] * 3)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        assert _note(screen) == "No sessions match the selected filters."
        assert not screen.query_one("#sessions").display
        assert _controls_shown(screen)
        assert not screen.query_one("#container").has_class("-empty")
        assert _subtitle(screen) == "0 sessions"
        assert _pager(screen) == (True, "Page 1 of 1", True)
        assert screen.query_one("#resume", Button).disabled
        assert not screen.query_one("#cancel", Button).disabled

        await click_when_settled(pilot, "#surface-cli")
        await wait_for(lambda: len(screen._session_ids) == 3, pilot=pilot, description="CLI sessions are listed")
        assert _note(screen) is None
        assert screen.query_one("#sessions").display


@pytest.mark.asyncio
async def test_without_saved_sessions_only_the_note_shows() -> None:
    screen = SessionsScreen(FakeSessionStore(0))

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        assert _note(screen) == "No saved sessions."
        assert not screen.query_one("#filters").display
        assert not screen.query_one("#footer").display
        assert screen.query_one("#container").has_class("-empty")
        assert _subtitle(screen) == ""


@pytest.mark.asyncio
async def test_the_search_finds_sessions_on_every_page() -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 10)
    store.sessions[2 * SESSION_PAGE_SIZE + 3].title = "needle on page three"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        await _search(screen, pilot, "needle")
        assert screen._session_ids == [f"session-{2 * SESSION_PAGE_SIZE + 3}"]
        assert _subtitle(screen) == "1/210 sessions"
        assert _pager(screen) == (True, "Page 1 of 1", True)
        assert _note(screen) is None
        # Past the first page, the search read only its match.
        assert store.loaded_ids[1:] == [(f"session-{2 * SESSION_PAGE_SIZE + 3}",)]


@pytest.mark.asyncio
async def test_clearing_the_search_returns_to_the_page_browsed() -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 10)
    store.sessions[5].title = "needle on page one"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)

        await _search(screen, pilot, "needle")
        assert screen._session_ids == ["session-5"]
        assert _pager(screen) == (True, "Page 1 of 1", True)
        # A changed search still returns to that page.
        await _search(screen, pilot, "needle on")
        assert screen._session_ids == ["session-5"]

        await _search(screen, pilot, "")
        assert screen._session_ids[0] == f"session-{SESSION_PAGE_SIZE}"
        assert _pager(screen) == (False, "Page 2 of 3", False)
        assert _subtitle(screen) == "210 sessions"


@pytest.mark.asyncio
async def test_clearing_a_search_begun_while_a_page_loaded_returns_to_that_page() -> None:
    store = GatedPageStore(2 * SESSION_PAGE_SIZE + 10)
    store.sessions[5].title = "needle on page one"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _click_pager(pilot, screen, "next-page")
        await wait_for_blocked_loads(store, pilot, 1)
        _search_box(screen).value = "needle"
        await wait_for(
            lambda: screen._loading_request == (1, "needle"), pilot=pilot, description="the search is asked for"
        )
        store.release_all()
        await wait_for_blocked_loads(store, pilot, 2)
        store.release_all()
        await _wait_for_results(screen, pilot, "needle")
        assert screen._session_ids == ["session-5"]

        _search_box(screen).value = ""
        await wait_for_blocked_loads(store, pilot, 3)
        store.release_all()
        await _wait_for_results(screen, pilot, "")
        assert _pager(screen) == (False, "Page 2 of 3", False)


@pytest.mark.asyncio
async def test_the_pager_walks_the_results_shown_until_the_search_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Typing never settles here, so only Enter runs a search.
    monkeypatch.setattr(sessions_screen, "_SEARCH_DEBOUNCE_SECONDS", 60)
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 10)
    _titled(store, range(0, 2 * SESSION_PAGE_SIZE + 10, 2), "even needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        await _submit_search(screen, pilot, "even needle")
        assert _subtitle(screen) == "105/210 sessions"
        assert _pager(screen) == (True, "Page 1 of 2", False)
        assert pilot.app.focused is screen.query_one("#sessions")

        _search_box(screen).value = "something else"
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        assert screen._session_ids == [f"session-{index}" for index in range(2 * SESSION_PAGE_SIZE, 210, 2)]
        assert _subtitle(screen) == "105/210 sessions"

        await _submit_search(screen, pilot, "something else")
        assert _note(screen) == "No sessions match your search."
        assert _subtitle(screen) == "0/210 sessions"
        assert _pager(screen) == (True, "Page 1 of 1", True)
        assert _controls_shown(screen)


@pytest.mark.asyncio
async def test_a_search_reads_only_the_result_page_it_shows() -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 10)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        await _search(screen, pilot, "session")
        assert _subtitle(screen) == "210/210 sessions"
        assert _pager(screen) == (True, "Page 1 of 3", False)
        assert store.loaded_ids[-1] == tuple(f"session-{index}" for index in range(SESSION_PAGE_SIZE))
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        assert store.loaded_ids[-1] == tuple(
            f"session-{index}" for index in range(SESSION_PAGE_SIZE, 2 * SESSION_PAGE_SIZE)
        )

        # Enter on the search shown runs nothing again: the result page stays.
        _search_box(screen).focus()
        await wait_for(lambda: _search_box(screen).has_focus, pilot=pilot, description="the search box has focus")
        await pilot.press("enter")
        assert screen._loading_request is None
        assert screen._page == 2

        # A search matching nothing reads no session.
        loads = len(store.loaded_ids)
        await _search(screen, pilot, "nothing like it")
        assert len(store.loaded_ids) == loads
        assert _note(screen) == "No sessions match your search."
        assert _subtitle(screen) == "0/210 sessions"
        assert _pager(screen) == (True, "Page 1 of 1", True)


@pytest.mark.asyncio
async def test_toggling_a_surface_during_a_search_searches_the_new_selection() -> None:
    store = FakeSessionStore(SESSION_PAGE_SIZE + 10, surfaces=[CLI] * 5)
    store.sessions[1].title = "needle in the CLI"
    store.sessions[50].title = "needle in the TUI"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        await _search(screen, pilot, "needle")
        assert screen._session_ids == ["session-50"]
        assert _subtitle(screen) == "1/105 sessions"

        await click_when_settled(pilot, "#surface-cli")
        await wait_for(
            lambda: screen._session_ids == ["session-1", "session-50"] and screen._loading_request is None,
            pilot=pilot,
            description="the search covers CLI sessions too",
        )
        assert _subtitle(screen) == "2/110 sessions"

        # The new selection is browsed from its first page.
        await _search(screen, pilot, "")
        assert _pager(screen) == (True, "Page 1 of 2", False)

        await _search(screen, pilot, "needle")
        await click_when_settled(pilot, "#surface-cli")
        await wait_for(
            lambda: screen._session_ids == ["session-50"] and screen._loading_request is None,
            pilot=pilot,
            description="the search covers TUI sessions only",
        )
        loads = len(store.loaded_ids)
        await click_when_settled(pilot, "#surface-tui")
        await wait_for(
            lambda: not screen._session_ids and screen._loading_request is None,
            pilot=pilot,
            description="no surface is searched",
        )
        assert len(store.loaded_ids) == loads
        assert _note(screen) == "No sessions match the selected filters."
        assert _subtitle(screen) == "0/0 sessions"


@pytest.mark.asyncio
async def test_deleting_the_only_result_of_the_last_result_page_shows_the_page_before() -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE)
    _titled(store, range(SESSION_PAGE_SIZE + 1), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _search(screen, pilot, "needle")
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        assert screen._session_ids == [f"session-{SESSION_PAGE_SIZE}"]

        await confirm_delete(screen, pilot)
        await _wait_for_page(screen, pilot, 1)

        assert len(screen._session_ids) == SESSION_PAGE_SIZE
        assert _pager(screen) == (True, "Page 1 of 1", True)
        assert _subtitle(screen) == "100/199 sessions"
        assert screen._shown_query == "needle"


@pytest.mark.asyncio
async def test_a_delete_during_a_search_counts_before_the_reload_lands() -> None:
    store = GatedPageStore(5, free_loads=2)
    store.sessions[1].title = "needle one"
    store.sessions[3].title = "needle three"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _search(screen, pilot, "needle")
        assert _subtitle(screen) == "2/5 sessions"

        await confirm_delete(screen, pilot)
        await wait_for_blocked_loads(store, pilot, 1)
        assert _subtitle(screen) == "1/4 sessions"
        store.release_all()
        await wait_for_load_idle(screen, pilot)
        assert _subtitle(screen) == "1/4 sessions"


@pytest.mark.asyncio
async def test_a_delete_while_a_search_loads_reloads_the_search_asked_for() -> None:
    store = GatedPageStore(2 * SESSION_PAGE_SIZE + 10, free_loads=2)
    _titled(store, range(0, 2 * SESSION_PAGE_SIZE + 10, 2), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        _search_box(screen).value = "needle"
        await wait_for_blocked_loads(store, pilot, 1)

        # Browse page 2 still shows: delete one of its sessions while the first result page loads.
        await confirm_delete(screen, pilot)
        await wait_for(
            lambda: screen._filtered_total == 2 * SESSION_PAGE_SIZE + 9, pilot=pilot, description="the delete lands"
        )
        store.release_all()
        await wait_for_blocked_loads(store, pilot, 2)
        store.release_all()
        await _wait_for_results(screen, pilot, "needle")

        # The first result page, not the page number browsed.
        assert _pager(screen) == (True, "Page 1 of 2", False)


@pytest.mark.parametrize("keeps_typing", [False, True], ids=["waits", "keeps-typing"])
@pytest.mark.asyncio
async def test_enter_before_its_results_show_hands_them_focus_unless_typing_goes_on(
    monkeypatch: pytest.MonkeyPatch, keeps_typing: bool
) -> None:
    # Typing never settles here, so only Enter runs a search.
    monkeypatch.setattr(sessions_screen, "_SEARCH_DEBOUNCE_SECONDS", 60)
    store = GatedPageStore(SESSION_PAGE_SIZE + 10)
    store.sessions[5].title = "needle in a haystack"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        search = _search_box(screen)
        await _submit_search(screen, pilot, "needlx")
        assert not screen.query_one("#sessions").display
        assert pilot.app.focused is search

        search.value = "needle"
        await pilot.press("enter")
        await wait_for_blocked_loads(store, pilot, 1)
        # No table shows to take focus yet.
        assert pilot.app.focused is search
        if keeps_typing:
            search.value = "needle in"
        store.release_all()
        await _wait_for_results(screen, pilot, "needle")

        assert screen._session_ids == ["session-5"]
        assert pilot.app.focused is screen.query_one("#search" if keeps_typing else "#sessions")


@pytest.mark.parametrize("move", ["surface", "pager", "retyped"])
@pytest.mark.asyncio
async def test_enter_hands_focus_only_to_the_results_it_asked_for(monkeypatch: pytest.MonkeyPatch, move: str) -> None:
    if move != "retyped":
        # Typing never settles here, so only Enter runs a search.
        monkeypatch.setattr(sessions_screen, "_SEARCH_DEBOUNCE_SECONDS", 60)
    store = GatedPageStore(3 * SESSION_PAGE_SIZE, surfaces=[CLI] * 3)
    _titled(store, range(0, 3 * SESSION_PAGE_SIZE, 2), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        search = _search_box(screen)
        search.focus()
        await wait_for(lambda: search.has_focus, pilot=pilot, description="the search box has focus")
        search.value = "needle"
        await pilot.press("enter")
        await wait_for_blocked_loads(store, pilot, 1)

        # Before the results show, the user moves on, and is back in the search box when they do.
        if move == "surface":
            await click_when_settled(pilot, "#surface-cli")
            await wait_for(lambda: CLI in screen._surfaces, pilot=pilot, description="CLI is selected")
        elif move == "pager":
            # Turns nothing: the pager shows the listing, not the search on its way.
            await _click_pager(pilot, screen, "next-page")
            assert screen._loading_request == (1, "needle")
        else:
            # The search in between has matches, so it waits for the first read rather than showing first.
            for query in ("needle 1", "needle"):
                search.value = query
                await wait_for(
                    lambda query=query: screen._search_query == query,
                    pilot=pilot,
                    description=f"{query!r} is searched",
                )
        if not search.has_focus:
            await click_when_settled(pilot, search)
            await wait_for(lambda: search.has_focus, pilot=pilot, description="the search box has focus again")
        store.release_all()
        if move != "pager":
            await wait_for_blocked_loads(store, pilot, 2)
            store.release_all()
        await _wait_for_results(screen, pilot, "needle")
        await pilot.pause()

        assert screen.query_one("#sessions").display
        assert pilot.app.focused is search


@pytest.mark.asyncio
async def test_the_pager_of_a_search_being_replaced_turns_nothing() -> None:
    store = GatedPageStore(3 * SESSION_PAGE_SIZE + 10, free_loads=4)
    _titled(store, range(0, 3 * SESSION_PAGE_SIZE + 10, 2), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        for page in (2, 3):
            await _click_pager(pilot, screen, "next-page")
            await _wait_for_page(screen, pilot, page)
        await _search(screen, pilot, "needle")
        assert _pager(screen) == (True, "Page 1 of 2", False)

        _search_box(screen).value = ""
        await wait_for_blocked_loads(store, pilot, 1)
        # Its Next is the results' page 2, not a page of the listing it returns to.
        await _click_pager(pilot, screen, "next-page")
        assert screen._loading_request == (3, "")
        store.release_all()
        await _wait_for_page(screen, pilot, 3)
        assert _pager(screen) == (False, "Page 3 of 4", False)


@pytest.mark.asyncio
async def test_the_pager_of_a_selection_being_replaced_turns_nothing() -> None:
    store = GatedPageStore(4 * SESSION_PAGE_SIZE + 10, free_loads=3, surfaces=[CLI] * 10)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        for page in (2, 3):
            await _click_pager(pilot, screen, "next-page")
            await _wait_for_page(screen, pilot, page)
        assert _pager(screen) == (False, "Page 3 of 4", False)

        await click_when_settled(pilot, "#surface-cli")
        await wait_for_blocked_loads(store, pilot, 1)
        # Its Next is page 4 of TUI sessions, not a page of the new selection.
        await _click_pager(pilot, screen, "next-page")
        assert screen._loading_request == (1, "")
        store.release_all()
        await _wait_for_page(screen, pilot, 1)
        assert _pager(screen) == (True, "Page 1 of 5", False)


def _hold_page_turns(monkeypatch: pytest.MonkeyPatch, navigator: PageNavigator) -> list[Message]:
    """Keep the page turns *navigator* asks for from reaching its owner, for as long as *monkeypatch* lasts."""
    post = navigator.post_message
    held: list[Message] = []

    def hold_page_turns(message: Message) -> bool:
        if isinstance(message, PageNavigator.Changed):
            held.append(message)
            return True
        return post(message)

    monkeypatch.setattr(navigator, "post_message", hold_page_turns)
    return held


@pytest.mark.asyncio
async def test_a_page_turn_that_arrives_after_its_search_was_replaced_turns_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeSessionStore(3 * SESSION_PAGE_SIZE + 10)
    _titled(store, range(0, 3 * SESSION_PAGE_SIZE + 10, 2), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        for page in (2, 3):
            await _click_pager(pilot, screen, "next-page")
            await _wait_for_page(screen, pilot, page)
        await _search(screen, pilot, "needle")
        assert _pager(screen) == (True, "Page 1 of 2", False)

        navigator = screen.query_one("#session-pages", PageNavigator)

        # Next is pressed on the results; its page turn reaches the screen once the listing is back.
        with monkeypatch.context() as patch:
            held = _hold_page_turns(patch, navigator)
            await _click_pager(pilot, screen, "next-page")
            await wait_for(lambda: held, pilot=pilot, description="Next asks for a page")
        await _search(screen, pilot, "")
        assert _pager(screen) == (False, "Page 3 of 4", False)
        requested = len(store.pages_requested)
        table = screen.query_one("#sessions", _SessionTable)
        assert not table.has_focus

        navigator.post_message(held[0])
        await wait_for(lambda: table.has_focus, pilot=pilot, description="the screen takes the page turn")
        assert screen._loading_request is None
        assert len(store.pages_requested) == requested
        assert _pager(screen) == (False, "Page 3 of 4", False)


@pytest.mark.asyncio
async def test_a_page_turn_that_arrives_after_an_enter_leaves_it_the_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    store = GatedPageStore(3 * SESSION_PAGE_SIZE, free_loads=2)
    _titled(store, range(0, 3 * SESSION_PAGE_SIZE, 2), "needle")
    _titled(store, range(1, 3 * SESSION_PAGE_SIZE, 2), "pin")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _search(screen, pilot, "needle")
        assert _pager(screen) == (True, "Page 1 of 2", False)
        navigator = screen.query_one("#session-pages", PageNavigator)
        table = screen.query_one("#sessions", _SessionTable)

        # Next is pressed on the results; its page turn reaches the screen only after the user searched again.
        with monkeypatch.context() as patch:
            held = _hold_page_turns(patch, navigator)
            await _click_pager(pilot, screen, "next-page")
            await wait_for(lambda: held, pilot=pilot, description="Next asks for a page")
        search = _search_box(screen)
        await click_when_settled(pilot, search)
        await wait_for(lambda: search.has_focus, pilot=pilot, description="the search box has focus")
        search.value = "pin"
        await pilot.press("enter")
        await wait_for_blocked_loads(store, pilot, 1)

        navigator.post_message(held[0])
        await pilot.pause()
        assert search.has_focus
        assert screen._focus_results_of == "pin"

        store.release_all()
        await _wait_for_results(screen, pilot, "pin")
        await wait_for(lambda: table.has_focus, pilot=pilot, description="the results take focus")
        assert screen._page == 1


@pytest.mark.asyncio
async def test_deleting_the_only_match_leaves_the_search_note() -> None:
    store = FakeSessionStore(5)
    store.sessions[3].title = "needle"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _search(screen, pilot, "needle")
        assert screen._session_ids == ["session-3"]

        await confirm_delete(screen, pilot)
        await wait_for_load_idle(screen, pilot)

        assert "session-3" not in [meta.session_id for meta in store.sessions]
        assert _note(screen) == "No sessions match your search."
        assert _subtitle(screen) == "0/4 sessions"
        assert _controls_shown(screen)


@pytest.mark.asyncio
async def test_a_matching_fork_stays_under_the_parent_it_was_forked_from() -> None:
    # A full page of matches, one of them forked from a session that does not match.
    store = GatedPageStore(SESSION_PAGE_SIZE + 1, free_loads=2)
    _titled(store, range(1, SESSION_PAGE_SIZE + 1), "needle")
    store.sessions[1].parent_session_id = "session-0"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _search(screen, pilot, "needle")

        # The parent anchors the fork, dimmed, besides the page's matches: no match itself, it is not counted.
        table = screen.query_one("#sessions", _SessionTable)
        assert table.row_count == SESSION_PAGE_SIZE + 1
        assert [table.get_row_at(row)[0].plain for row in (0, 1)] == ["session0", "└ session1"]
        assert [row.matched for row in screen._rows[:2]] == [False, True]
        assert _subtitle(screen) == f"{SESSION_PAGE_SIZE}/{SESSION_PAGE_SIZE + 1} sessions"
        assert _pager(screen) == (True, "Page 1 of 1", True)

        # Deleting it takes no match off the count.
        table.move_cursor(row=0)
        await confirm_delete(screen, pilot)
        await wait_for_blocked_loads(store, pilot, 1)
        assert _subtitle(screen) == f"{SESSION_PAGE_SIZE}/{SESSION_PAGE_SIZE} sessions"
        store.release_all()
        await _wait_for_results(screen, pilot, "needle")
        assert table.row_count == SESSION_PAGE_SIZE
        assert table.get_row_at(0)[0].plain == "session1"


@pytest.mark.asyncio
async def test_a_fork_whose_parent_matches_on_another_page_starts_its_own_tree() -> None:
    store = FakeSessionStore(SESSION_PAGE_SIZE + 1)
    _titled(store, range(SESSION_PAGE_SIZE + 1), "needle")
    # The newest match was forked from the oldest, which lists on the next page.
    store.sessions[0].parent_session_id = f"session-{SESSION_PAGE_SIZE}"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _search(screen, pilot, "needle")
        assert screen._session_ids == [f"session-{index}" for index in range(SESSION_PAGE_SIZE)]
        assert screen.query_one("#sessions", _SessionTable).get_row_at(0)[0].plain == "session0"

        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        assert screen._session_ids == [f"session-{SESSION_PAGE_SIZE}"]


@pytest.mark.asyncio
async def test_the_workflow_window_searches_every_run_input_and_status() -> None:
    store = FakeSessionStore(0)
    store.sessions = [_workflow_session(index) for index in range(SESSION_PAGE_SIZE + 5)]
    store.sessions[SESSION_PAGE_SIZE + 2] = _workflow_session(SESSION_PAGE_SIZE + 2, input_text="find the needle")
    store.sessions[SESSION_PAGE_SIZE + 3] = _workflow_session(SESSION_PAGE_SIZE + 3, status="failed")
    screen = SessionsScreen(store, workflow_mode=True)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        assert _pager(screen) == (True, "Page 1 of 2", False)

        await _search(screen, pilot, "needle")
        assert screen._session_ids == [f"flow-{SESSION_PAGE_SIZE + 2}"]
        assert [row.prompt_only for row in screen._rows] == [True]
        assert _subtitle(screen) == "1/105 workflow sessions"

        # The label the Status column shows.
        await _search(screen, pilot, "error")
        assert screen._session_ids == [f"flow-{SESSION_PAGE_SIZE + 3}"]


@pytest.mark.parametrize(
    ("last_page_renamed", "lands_on"),
    [(False, "#sessions"), (True, "#search")],
    ids=["the-last-page-has-matches", "the-last-page-has-none"],
)
@pytest.mark.asyncio
async def test_turning_to_the_last_page_under_a_search_that_emptied_the_table_leaves_the_pager(
    last_page_renamed: bool, lands_on: str
) -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 1)
    _titled(store, range(0, 2 * SESSION_PAGE_SIZE + 1, 2), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        # Renamed since the browser opened: its listing still counts them as matches, but their pages show none.
        _titled(store, range(0, 2 * SESSION_PAGE_SIZE + (1 if last_page_renamed else 0), 2), "renamed")
        await _search(screen, pilot, "needle")
        assert _note(screen) == "No sessions match your search."
        assert _pager(screen) == (True, "Page 1 of 2", False)

        next_page = screen.query_one("#session-pages #next-page", Button)
        next_page.focus()
        await wait_for(lambda: next_page.has_focus, pilot=pilot, description="Next has focus")
        await pilot.press("enter")
        await _wait_for_page(screen, pilot, 2)
        assert _pager(screen) == (False, "Page 2 of 2", True)
        # Not the Previous button, where another Enter would walk back to page one.
        assert pilot.app.focused is screen.query_one(lands_on)


@pytest.mark.asyncio
async def test_a_page_the_search_empties_takes_focus_off_the_hidden_table() -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 1)
    _titled(store, range(0, 2 * SESSION_PAGE_SIZE + 1, 2), "needle")
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        table = screen.query_one("#sessions", _SessionTable)
        # Renamed since the browser opened: its listing still counts it as a match, but its page shows none.
        store.sessions[2 * SESSION_PAGE_SIZE].title = "renamed"
        await _search(screen, pilot, "needle")
        assert len(screen._session_ids) == SESSION_PAGE_SIZE

        next_page = screen.query_one("#session-pages #next-page", Button)
        next_page.focus()
        await wait_for(lambda: next_page.has_focus, pilot=pilot, description="Next has focus")
        await pilot.press("enter")
        await _wait_for_page(screen, pilot, 2)
        assert not table.display
        assert _note(screen) == "No sessions match your search."
        # A hidden table still takes focus; keys would go nowhere visible.
        assert pilot.app.focused is screen.query_one("#search")


@pytest.mark.parametrize("key", ["enter", "escape"])
@pytest.mark.asyncio
async def test_the_search_box_keeps_focus_while_no_table_shows(key: str) -> None:
    store = FakeSessionStore(3, surfaces=[CLI] * 3)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        assert _note(screen) == "No sessions match the selected filters."
        search = screen.query_one("#search", Input)
        search.focus()
        await wait_for(lambda: search.has_focus, pilot=pilot, description="the search box has focus")

        await pilot.press(key)
        assert not screen.query_one("#sessions").display
        assert pilot.app.focused is search

        # With the table shown, both keys hand it focus.
        await click_when_settled(pilot, "#surface-cli")
        await wait_for(lambda: len(screen._session_ids) == 3, pilot=pilot, description="CLI sessions are listed")
        search.focus()
        await wait_for(lambda: search.has_focus, pilot=pilot, description="the search box has focus again")
        await pilot.press(key)
        await wait_for(
            lambda: pilot.app.focused is screen.query_one("#sessions"), pilot=pilot, description="the table has focus"
        )


@pytest.mark.asyncio
async def test_pager_walks_the_pages_and_hands_focus_to_the_table() -> None:
    store = FakeSessionStore(2 * SESSION_PAGE_SIZE + 50)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        table = screen.query_one("#sessions", DataTable)
        assert _pager(screen) == (True, "Page 1 of 3", False)
        assert table.row_count == SESSION_PAGE_SIZE
        assert _subtitle(screen) == "250 sessions"

        next_page = screen.query_one("#session-pages #next-page", Button)
        next_page.focus()
        await wait_for(lambda: next_page.has_focus, pilot=pilot, description="Next has focus")
        await pilot.press("enter")
        await _wait_for_page(screen, pilot, 2)
        assert _pager(screen) == (False, "Page 2 of 3", False)
        assert screen._session_ids[0] == f"session-{SESSION_PAGE_SIZE}"
        assert pilot.app.focused is table

        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 3)
        assert _pager(screen) == (False, "Page 3 of 3", True)
        assert table.row_count == 50
        # Not the Previous button, which Textual would pick when Next is disabled under focus.
        assert pilot.app.focused is table

        await _click_pager(pilot, screen, "previous-page")
        await _wait_for_page(screen, pilot, 2)
        assert _pager(screen) == (False, "Page 2 of 3", False)


@pytest.mark.asyncio
async def test_the_last_requested_load_wins() -> None:
    store = GatedPageStore(2 * SESSION_PAGE_SIZE, surfaces=[CLI] * 10)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        await _click_pager(pilot, screen, "next-page")
        await wait_for_blocked_loads(store, pilot, 1)
        await click_when_settled(pilot, "#surface-cli")
        await click_when_settled(pilot, "#surface-acp")
        # Each replacement waits for the page 2 read, which cancelling its load cannot stop.
        store.release_all()
        await wait_for_blocked_loads(store, pilot, 2)
        store.release_all()
        await wait_for_load_idle(screen, pilot)

        # The selection replaced while it waited read nothing.
        assert store.pages_requested[1:] == [(frozenset({TUI}), 2), (frozenset(SessionSurface), 1)]
        assert store.most_loads_running == 1
        assert store.cancelled_loads == 0
        assert screen._page == 1
        assert screen._session_ids[0] == "session-0"


@pytest.mark.asyncio
async def test_asking_again_for_the_page_on_its_way_leaves_its_load_running() -> None:
    store = GatedPageStore(2 * SESSION_PAGE_SIZE + 1)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        await _click_pager(pilot, screen, "next-page")
        await wait_for_blocked_loads(store, pilot, 1)
        # The pager shows page 1 until page 2 arrives, so Next asks for page 2 again.
        await _click_pager(pilot, screen, "next-page")
        # The click still hands the table focus, or Enter on Next would skip a page once it arrives.
        assert pilot.app.focused is screen.query_one("#sessions")
        store.release_all()
        await _wait_for_page(screen, pilot, 2)

        # Read once: a second request would read it again after the first.
        assert store.pages_requested[1:] == [(frozenset({TUI}), 2)]
        assert store.cancelled_loads == 0
        assert _pager(screen) == (False, "Page 2 of 3", False)


@pytest.mark.asyncio
async def test_a_newer_search_waits_for_the_page_read_in_flight() -> None:
    store = GatedPageStore(SESSION_PAGE_SIZE + 10)
    for index, word in ((1, "first"), (2, "second"), (3, "third")):
        store.sessions[index].title = f"{word} needle"
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)

        _search_box(screen).value = "first"
        await wait_for_blocked_loads(store, pilot, 1)
        for word in ("second", "third"):
            _search_box(screen).value = word
            await wait_for(
                lambda word=word: screen._loading_request == (1, word),
                pilot=pilot,
                description=f"the {word} search is asked for",
            )
        store.release_all()
        await wait_for_blocked_loads(store, pilot, 2)
        store.release_all()
        await _wait_for_results(screen, pilot, "third")

        assert screen._session_ids == ["session-3"]
        # The second search, replaced while it waited, read nothing.
        assert store.loaded_ids[1:] == [("session-1",), ("session-3",)]
        assert store.most_loads_running == 1
        assert store.cancelled_loads == 0


@pytest.mark.asyncio
async def test_deleting_from_a_full_page_pulls_the_next_session_up() -> None:
    store = FakeSessionStore(SESSION_PAGE_SIZE + 1)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        assert _pager(screen) == (True, "Page 1 of 2", False)

        screen.query_one("#sessions", DataTable).move_cursor(row=0)
        await confirm_delete(screen, pilot)
        await wait_for(
            lambda: f"session-{SESSION_PAGE_SIZE}" in screen._session_ids and not screen._loading,
            pilot=pilot,
            description="the next page's first session moves up",
        )

        assert len(screen._session_ids) == SESSION_PAGE_SIZE
        assert _pager(screen) == (True, "Page 1 of 1", True)
        assert _subtitle(screen) == "100 sessions"
        assert screen._get_selected_session_id() == "session-1"


@pytest.mark.asyncio
async def test_deleting_the_only_session_of_the_last_page_shows_the_page_before() -> None:
    store = FakeSessionStore(SESSION_PAGE_SIZE + 1)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _click_pager(pilot, screen, "next-page")
        await _wait_for_page(screen, pilot, 2)
        assert screen._session_ids == [f"session-{SESSION_PAGE_SIZE}"]

        await confirm_delete(screen, pilot)
        await _wait_for_page(screen, pilot, 1)

        assert len(screen._session_ids) == SESSION_PAGE_SIZE
        assert _pager(screen) == (True, "Page 1 of 1", True)
        assert _note(screen) is None


class _GatedDeleteStore(GatedPageStore):
    """A page store whose deletes also wait, for :attr:`delete_gate`."""

    def __init__(self, count: int) -> None:
        super().__init__(count)
        self.delete_gate = asyncio.Event()
        self.deletes_started = 0

    async def delete_session(self, session_id: str, *, allow_active: bool = False) -> None:
        self.deletes_started += 1
        await self.delete_gate.wait()
        await super().delete_session(session_id, allow_active=allow_active)


@pytest.mark.parametrize("pending", ["page-load", "delete"])
@pytest.mark.asyncio
async def test_work_that_finishes_as_the_browser_closes_renders_nothing(pending: str) -> None:
    store = _GatedDeleteStore(SESSION_PAGE_SIZE + 1)
    pruned: list[bool] = []

    class Closing(SessionsScreen):
        # A relative CSS_PATH resolves against the subclass's module.
        CSS_PATH = Path(inspect.getfile(SessionsScreen)).parent / str(SessionsScreen.CSS_PATH)

        async def on_unmount(self) -> None:
            # Removal has pruned every widget, and the browser's work still runs: let it finish now.
            pruned.append(not self.children)
            store.release_all()
            store.delete_gate.set()
            for worker in [worker for worker in self.app.workers if worker.node is self]:
                await worker.wait()

    screen = Closing(store)
    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        if pending == "page-load":
            await _click_pager(pilot, screen, "next-page")
            await wait_for_blocked_loads(store, pilot, 1)
        else:
            await confirm_delete(screen, pilot)
            await wait_for(lambda: store.deletes_started == 1, pilot=pilot, description="the delete waits")
        await pilot.app.pop_screen()
    # Leaving run_test re-raises what the work raised as it finished.
    assert pruned == [True]


@pytest.mark.asyncio
async def test_closing_the_browser_cancels_the_page_load_in_flight() -> None:
    store = GatedPageStore(SESSION_PAGE_SIZE + 1)
    screen = SessionsScreen(store)

    async with SessionsHostApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await wait_for_load_idle(screen, pilot)
        await _click_pager(pilot, screen, "next-page")
        await wait_for_blocked_loads(store, pilot, 1)

        await pilot.app.pop_screen()
        await wait_for(lambda: store.cancelled_loads == 1, pilot=pilot, description="the page load is cancelled")
