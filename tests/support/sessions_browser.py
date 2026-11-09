# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host app, in-memory stores and waits for the sessions browser tests."""

from __future__ import annotations

import asyncio
from collections.abc import Collection, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from textual.app import App, ComposeResult
from textual.pilot import Pilot
from textual.widgets import Button, DataTable, Static

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.foundation.config.settings import Settings
from chrys.foundation.models.session_surface import SessionSurface
from chrys.service.state._session_meta import copy_session_meta
from chrys.service.state.session_listing import (
    SESSION_PAGE_SIZE,
    SessionListing,
    SessionListingEntry,
    SessionPage,
    page_slice,
)
from chrys.service.state.store import ChatSessionMeta, SessionMeta
from tests.support.waiting import wait_for


class SessionsHostApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def compose(self) -> ComposeResult:
        yield Static("placeholder")


class FakeSessionStore:
    """Chat sessions ``session-0`` (newest) … ``session-<count-1>``, listed and paged like the real store.

    The listing is a snapshot of :attr:`sessions` when opened, each entry with
    its own copy of the meta (editing :attr:`sessions` afterwards makes the
    snapshot stale, as another process would); each page load returns the
    current meta objects of the snapshot's rows, dropping those deleted since. *surfaces* gives each session's recorded surface
    (``None``: saved before surfaces were recorded); the rest are TUI.
    """

    def __init__(self, count: int, *, surfaces: Sequence[SessionSurface | None] = ()) -> None:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        recorded = [*surfaces, *[SessionSurface.TUI] * (count - len(surfaces))]
        self.sessions: list[SessionMeta] = [
            ChatSessionMeta(
                session_id=f"session-{index}",
                agent_profile="Code",
                agent_display_name="Code",
                created_at=base - timedelta(minutes=index),
                updated_at=base - timedelta(minutes=index),
                message_count=1,
                title=f"Session {index}",
                last_surface=recorded[index],
            )
            for index in range(count)
        ]
        self.listings_opened: list[str] = []
        """The kind of each listing opened."""
        self.pages_requested: list[tuple[frozenset[SessionSurface], int]] = []
        self.loaded_ids: list[tuple[str, ...]] = []
        """The sessions each page load read, by id."""

    async def list_sessions(self) -> list[SessionMeta]:
        return list(self.sessions)

    async def open_session_listing(self, *, kind: Literal["chat", "workflow"]) -> SessionListing:
        self.listings_opened.append(kind)
        listed = sorted(
            (meta for meta in self.sessions if meta.kind == kind),
            key=lambda meta: (meta.updated_at, meta.session_id),
            reverse=True,
        )
        return SessionListing(
            kind,
            tuple(
                SessionListingEntry(
                    meta.session_id,
                    meta.updated_at,
                    meta.last_surface or SessionSurface.TUI,
                    Path(meta.session_id),
                    meta=copy_session_meta(meta),
                )
                for meta in listed
            ),
        )

    async def load_session_page(
        self,
        listing: SessionListing,
        *,
        surfaces: Collection[SessionSurface],
        page: int = 1,
        page_size: int = SESSION_PAGE_SIZE,
    ) -> SessionPage:
        self.pages_requested.append((frozenset(surfaces), page))
        return self.page(listing, surfaces, page, page_size)

    def page(
        self, listing: SessionListing, surfaces: Collection[SessionSurface], page: int, page_size: int
    ) -> SessionPage:
        selected, current, page_count, total = page_slice(listing, surfaces, page, page_size=page_size)
        self.loaded_ids.append(tuple(entry.session_id for entry in selected))
        live = {meta.session_id: meta for meta in self.sessions}
        metas = tuple(live[entry.session_id] for entry in selected if entry.session_id in live)
        return SessionPage(metas, current, page_count, total)

    async def delete_session(self, session_id: str, *, allow_active: bool = False) -> None:
        self.sessions = [session for session in self.sessions if session.session_id != session_id]


class GatedPageStore(FakeSessionStore):
    """Every page load after the first *free_loads* waits for its own gate in :attr:`gates`."""

    def __init__(self, count: int, *, free_loads: int = 1, surfaces: Sequence[SessionSurface | None] = ()) -> None:
        super().__init__(count, surfaces=surfaces)
        self._free_loads = free_loads
        self.gates: list[asyncio.Event] = []
        self.cancelled_loads = 0
        self._loads_running = 0
        self.most_loads_running = 0
        """The most page loads that ever ran at once."""

    async def load_session_page(
        self,
        listing: SessionListing,
        *,
        surfaces: Collection[SessionSurface],
        page: int = 1,
        page_size: int = SESSION_PAGE_SIZE,
    ) -> SessionPage:
        self.pages_requested.append((frozenset(surfaces), page))
        self._loads_running += 1
        self.most_loads_running = max(self.most_loads_running, self._loads_running)
        try:
            if len(self.pages_requested) > self._free_loads:
                gate = asyncio.Event()
                self.gates.append(gate)
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.cancelled_loads += 1
                    raise
            return self.page(listing, surfaces, page, page_size)
        finally:
            self._loads_running -= 1

    def release_all(self) -> None:
        for gate in self.gates:
            gate.set()


async def wait_for_row_count(table: DataTable, pilot: Pilot, count: int) -> None:
    await wait_for(
        lambda: table.row_count == count,
        pilot=pilot,
        description=f"sessions table reaches {count} rows",
    )


async def wait_for_load_idle(screen: SessionsScreen, pilot: Pilot) -> None:
    await wait_for(
        # A load is asked for before its worker starts loading.
        lambda: not screen._loading and screen._loading_request is None,
        pilot=pilot,
        description="Expected sessions load to become idle",
    )


async def wait_for_blocked_loads(store: GatedPageStore, pilot: Pilot, count: int) -> None:
    await wait_for(
        lambda: len(store.gates) == count,
        pilot=pilot,
        description=f"{count} page loads wait on their gates",
    )


async def open_delete_dialog(screen: SessionsScreen, pilot: Pilot) -> ConfirmDialog:
    screen.action_delete_session()
    await pilot.pause()
    dialog = pilot.app.screen
    assert isinstance(dialog, ConfirmDialog)
    return dialog


async def confirm_delete(screen: SessionsScreen, pilot: Pilot) -> None:
    dialog = await open_delete_dialog(screen, pilot)
    dialog.query_one("#confirm-yes", Button).press()
    await pilot.pause()
