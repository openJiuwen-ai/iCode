# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A fixed, ordered snapshot of the sessions a browser pages through.

The store takes the snapshot once when a browser opens; paging, filtering by
surface and deleting then work on it without rescanning, so a session saved
meanwhile never moves between pages. Page contents are loaded fresh. Each
entry keeps the metadata the snapshot read, which the browser searches every
session by without reading any session again.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from chrys.foundation.models.session_surface import SessionSurface
from chrys.service.state._session_meta import SessionMeta

SESSION_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class SessionListingEntry:
    """One listed session: its ordering time, its surface, where it is stored and what the snapshot read of it."""

    session_id: str
    listed_at: datetime
    """Chat: the last update. Workflow: the latest run's finish, else start."""
    surface: SessionSurface
    """The recorded surface; sessions saved before it was recorded count as TUI."""
    source: Path
    """The session folder, or the file of a legacy flat-file session."""
    legacy: bool = False
    meta: SessionMeta = field(kw_only=True, hash=False)
    """The session as the snapshot read it, a copy no cache shares: ``size_bytes`` 0 for a folder session, and a
    workflow session's ``latest_run`` read when the snapshot was taken."""


@dataclass(frozen=True, slots=True)
class SessionListing:
    """Displayable sessions of one kind, newest first (ties by session id, descending)."""

    kind: Literal["chat", "workflow"]
    entries: tuple[SessionListingEntry, ...]

    def filtered(self, surfaces: Collection[SessionSurface]) -> tuple[SessionListingEntry, ...]:
        return tuple(entry for entry in self.entries if entry.surface in surfaces)

    def page_count(self, surfaces: Collection[SessionSurface], *, page_size: int = SESSION_PAGE_SIZE) -> int:
        """Pages of the filtered listing; an empty listing still has one (empty) page."""
        return max(1, -(-len(self.filtered(surfaces)) // _checked_page_size(page_size)))

    def without(self, session_id: str) -> SessionListing:
        """The same snapshot minus a deleted session."""
        return SessionListing(self.kind, tuple(entry for entry in self.entries if entry.session_id != session_id))


@dataclass(frozen=True, slots=True)
class SessionPage:
    """One loaded page: its sessions in listing order, with sizes and workflow run summaries."""

    metas: tuple[SessionMeta, ...]
    page: int
    """1-based, clamped into ``1..page_count``."""
    page_count: int
    total: int
    """Sessions in the filtered listing, across all pages."""


def page_slice(
    listing: SessionListing, surfaces: Collection[SessionSurface], page: int, *, page_size: int = SESSION_PAGE_SIZE
) -> tuple[tuple[SessionListingEntry, ...], int, int, int]:
    """The entries of one page, with the clamped page number, the page count and the filtered total."""
    size = _checked_page_size(page_size)
    selected = listing.filtered(surfaces)
    page_count = max(1, -(-len(selected) // size))
    current = min(max(1, page), page_count)
    start = (current - 1) * size
    return selected[start : start + size], current, page_count, len(selected)


def _checked_page_size(page_size: int) -> int:
    if page_size < 1:
        raise ValueError("page_size must be positive.")
    return page_size
