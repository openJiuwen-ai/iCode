# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-memory search index for project path suggestions.

``ProjectPathIndex`` is one immutable, pre-normalized snapshot of a scan.  Its
query returns a bounded top-k result with the same ordering semantics as
``fuzzy_filter``: match position, original path length, then original path text.

``WorkspacePathIndex`` keeps that snapshot resident for one workspace root, so
the ``@`` popup answers at once, even from a stale snapshot, while a rescan
builds its replacement in the background.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_left
from collections.abc import Awaitable, Callable

from chrys.app.tui.widgets.chrome import file_scanner
from chrys.app.tui.widgets.chrome.file_scanner import (
    ProjectPathScanResult,
    ProjectPathSuggestion,
    _normalize_for_search,
)

logger = logging.getLogger(__name__)

_DEFAULT_QUERY_LIMIT = 30

type ScanProjectPaths = Callable[[str], Awaitable[ProjectPathScanResult]]


class ProjectPathIndex:
    """Pre-normalized substring index for ``@`` file suggestions."""

    def __init__(self, scan: ProjectPathScanResult) -> None:
        self.root = scan.root
        self.file_count = scan.file_count
        self.suggestion_count = scan.suggestion_count
        self.truncated = scan.truncated
        self.file_budget = scan.file_budget
        self.suggestion_budget = scan.suggestion_budget
        self.source_truncations = dict(scan.source_truncations)
        self.ordering = scan.ordering
        self._suggestions = list(scan.paths)
        self._normalized = [_normalize_for_search(suggestion.path) for suggestion in self._suggestions]

    @classmethod
    def build(cls, scan: ProjectPathScanResult) -> ProjectPathIndex:
        """Build an index from a scan result."""
        return cls(scan)

    def head(self, *, limit: int = _DEFAULT_QUERY_LIMIT) -> list[ProjectPathSuggestion]:
        """Return the first suggestions in insertion order."""
        if limit <= 0:
            return []
        return self._suggestions[:limit]

    def query(self, query: str, *, limit: int = _DEFAULT_QUERY_LIMIT) -> list[ProjectPathSuggestion]:
        """Return exact current fuzzy-filter ordering for the best file matches."""
        if limit <= 0:
            return []
        if not query:
            return self.head(limit=limit)

        normalized_query = _normalize_for_search(query)
        best: list[tuple[tuple[int, int, str], int]] = []
        for index, normalized_path in enumerate(self._normalized):
            match_index = normalized_path.find(normalized_query)
            if match_index < 0:
                continue
            suggestion = self._suggestions[index]
            entry = ((match_index, len(suggestion.path), suggestion.path), index)
            if len(best) < limit:
                best.insert(bisect_left(best, entry), entry)
            elif entry < best[-1]:
                best.insert(bisect_left(best, entry), entry)
                best.pop()
        return [self._suggestions[index] for _score, index in best]


class WorkspacePathIndex:
    """Resident ``@`` path snapshot for one workspace root.

    A change is always answered by a full rescan, never by patching the
    snapshot: the mutation tracker misses edits made outside the agent, and
    whether a new path is listed depends on ignore rules only a scan applies.
    :meth:`mark_stale` bumps a generation, so a rescan that was already running
    when the change happened still installs its result but leaves the snapshot
    stale for the next one.  Callers run at most one :meth:`rescan` per index at
    a time.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self.snapshot: ProjectPathIndex | None = None
        self.stale = False
        self._generation = 0

    @property
    def current(self) -> bool:
        """Whether an installed snapshot covers every change marked so far."""
        return self.snapshot is not None and not self.stale

    def install(self, scan: ProjectPathScanResult) -> None:
        """Install *scan* as this root's current snapshot."""
        self.snapshot = ProjectPathIndex.build(scan)
        self.stale = False

    def mark_stale(self) -> None:
        """Record that the workspace may have changed since the snapshot's scan began."""
        self.stale = True
        self._generation += 1

    async def rescan(self, scan: ScanProjectPaths | None = None) -> bool:
        """Scan the root and install the result; ``True`` when a snapshot was installed.

        The installed snapshot keeps answering while the scan runs, and a failed
        scan keeps it (and its staleness), so the next ``@`` tries again.
        """
        generation = self._generation
        try:
            result = await (scan or file_scanner.scan_project_paths)(self.root)
            snapshot = await asyncio.to_thread(ProjectPathIndex.build, result)
        except Exception:
            logger.warning("Scanning %s for @ file suggestions failed", self.root, exc_info=True)
            return False
        self.snapshot = snapshot
        self.stale = generation != self._generation
        return True
