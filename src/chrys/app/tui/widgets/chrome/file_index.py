# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""In-memory search index for project path suggestions.

``ProjectPathIndex`` is one immutable, pre-normalized snapshot of a scan.  Its
query returns a bounded top-k result with the same ordering semantics as
``fuzzy_filter``: match position, original path length, then original path text.

``WorkspacePathIndex`` owns that snapshot's lifecycle for one root: the ``@``
popup answers every keystroke from the installed snapshot, while incremental
folds and full rebuilds both derive off the event loop and only publish a
finished result.  ``dirty`` therefore means "the snapshot may be behind
the filesystem", never "block the popup until it is current".
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
from bisect import bisect_left
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, NamedTuple

from chrys.app.tui.widgets.chrome.file_scanner import (
    ProjectPathScanResult,
    ProjectPathSuggestion,
    _has_non_referenceable_file_extension,
    _include_ignored_visible_path,
    _normalize_for_search,
)

if TYPE_CHECKING:
    from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathOrdering

logger = logging.getLogger(__name__)

_DEFAULT_QUERY_LIMIT = 30

type ScanProjectPaths = Callable[[str], Awaitable[ProjectPathScanResult]]


def _default_scan(root: str) -> Awaitable[ProjectPathScanResult]:
    """Resolve the scanner at call time so tests and callers can substitute it."""
    from chrys.app.tui.widgets.chrome import file_scanner

    return file_scanner.scan_project_paths(root)


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


def _ancestor_directories(relative_path: str) -> list[str]:
    """Return the ``@`` directory rows a file row implies, outermost first."""
    parts = relative_path.split("/")
    return ["/".join(parts[:index]) + "/" for index in range(1, len(parts))]


def _path_identity(relative_path: str) -> str:
    """Return the key a root-relative path is deduplicated by.

    Windows treats ``src/app.py`` and ``src/App.py`` as one file, so the visible
    set must too; darwin's default volume is case-insensitive as well.  The
    spelling a row is displayed with stays in the row itself, so folding never
    rewrites how a path is shown.
    """
    identity = os.path.normcase(relative_path)
    if sys.platform == "darwin":
        identity = identity.casefold()
    return identity


def _directory_reference_counts(files: Iterable[str]) -> dict[str, int]:
    """Count the files under each directory, so a directory row lives iff its count is positive."""
    counts: dict[str, int] = {}
    for relative_path in files:
        for directory in _ancestor_directories(relative_path):
            counts[directory] = counts.get(directory, 0) + 1
    return counts


def _materialize_rows(files: Iterable[str], directory_counts: dict[str, int]) -> list[ProjectPathSuggestion]:
    """Return file rows plus one row per live directory, in insertion order."""
    rows: list[ProjectPathSuggestion] = []
    emitted: set[str] = set()
    for relative_path in files:
        for directory in _ancestor_directories(relative_path):
            if directory in emitted or directory_counts.get(directory, 0) <= 0:
                continue
            emitted.add(directory)
            rows.append(ProjectPathSuggestion(path=directory, kind="directory"))
        rows.append(ProjectPathSuggestion(path=relative_path, kind="file"))
    return rows


def _append_file(
    files: list[str],
    identities: dict[str, str],
    directory_counts: dict[str, int],
    relative_path: str,
) -> None:
    """Append a folded file row; never inserted in path order.

    ``head()`` returns a prefix of the last full scan's order, not a
    lexicographic prefix — ``rg --sort path`` is a per-directory depth-first
    order and the supplemental sources append after the primary block.  A
    folded row therefore goes to the tail of ``files`` (its ancestor rows stay
    next to it), so post-fold empty-query order is not guaranteed to match a
    fresh full scan until the next full rebuild.  Frozen regardless: the visible
    set, the match ordering key, the ``source_truncations`` key set, and the
    budget constants.  Do not turn this into a sorted insert.
    """
    files.append(relative_path)
    identities[_path_identity(relative_path)] = relative_path
    for directory in _ancestor_directories(relative_path):
        directory_counts[directory] = directory_counts.get(directory, 0) + 1


def _drop_file(
    files: list[str],
    identities: dict[str, str],
    directory_counts: dict[str, int],
    identity: str,
) -> None:
    """Drop a folded file row and the directory references it held."""
    relative_path = identities.pop(identity, None)
    if relative_path is None:
        return
    with contextlib.suppress(ValueError):
        files.remove(relative_path)
    for directory in _ancestor_directories(relative_path):
        remaining = directory_counts.get(directory, 0) - 1
        if remaining > 0:
            directory_counts[directory] = remaining
        else:
            directory_counts.pop(directory, None)


class _InstallBundle(NamedTuple):
    """Everything one install needs, derived off the event loop.

    Publishing this is reference assignment only: no list or dict derivation, no
    path normalization and no ``os.path`` batch run happen on the event loop.
    """

    files: list[str]
    identities: dict[str, str]
    directory_counts: dict[str, int]
    snapshot: ProjectPathIndex
    source_truncations: dict[str, bool]
    file_budget: int
    suggestion_budget: int
    ordering: ProjectPathOrdering


def _derive_from_scan(scan: ProjectPathScanResult) -> _InstallBundle:
    """Derive a scan's snapshot and bookkeeping — the thread half of an install."""
    files = [suggestion.path for suggestion in scan.paths if suggestion.kind == "file"]
    return _InstallBundle(
        files=files,
        identities={_path_identity(relative_path): relative_path for relative_path in files},
        directory_counts=_directory_reference_counts(files),
        snapshot=ProjectPathIndex.build(scan),
        source_truncations=dict(scan.source_truncations),
        file_budget=scan.file_budget,
        suggestion_budget=scan.suggestion_budget,
        ordering=scan.ordering,
    )


class WorkspacePathIndex:
    """Resident ``@`` path snapshot for one workspace root.

    The popup only ever reads :attr:`snapshot`; nothing here scans on the input
    path.  ``dirty`` says the snapshot may be behind the filesystem and
    ``delta`` holds the changed absolute paths observed since it was installed,
    so ``mark_dirty()`` with no paths means the change set is unknown and a full
    rescan is required.  :meth:`refresh` is the single-flight rebuild: it
    replaces the snapshot only when it finishes for the same :attr:`epoch`,
    which is what makes a rebuild that outlives a workspace switch harmless.
    """

    def __init__(self, root: str) -> None:
        self.root = root
        self.epoch = 0
        self.dirty = False
        self.snapshot: ProjectPathIndex | None = None
        self.build_task: asyncio.Task[bool] | None = None
        self.delta: set[str] = set()
        self._files: list[str] = []
        self._identities: dict[str, str] = {}
        self._directory_counts: dict[str, int] = {}
        self._source_truncations: dict[str, bool] = {}
        self._file_budget = 0
        self._suggestion_budget = 0
        self._ordering: ProjectPathOrdering = "path-sorted"
        # Bumped on every recorded change, so a rebuild that straddled one knows
        # its scan predates the change and leaves the snapshot dirty.
        self._change_seq = 0
        # Bumped on every change set we could not enumerate, so a rebuild that
        # was already running when one landed knows its scan does not cover it —
        # a boolean cannot tell "covered by this scan" from "arrived after it".
        self._unknown_seq = 0
        # A change set we could not enumerate (shell tools, truncated detection)
        # rules out folding known paths in: only a rescan can cover it.
        self._unknown_changes = False

    @property
    def installed(self) -> bool:
        """Whether a snapshot is available for queries."""
        return self.snapshot is not None

    def query(self, query: str, *, limit: int = _DEFAULT_QUERY_LIMIT) -> list[ProjectPathSuggestion]:
        """Answer from the installed snapshot only; never scans."""
        snapshot = self.snapshot
        if snapshot is None:
            return []
        return snapshot.query(query, limit=limit)

    def head(self, *, limit: int = _DEFAULT_QUERY_LIMIT) -> list[ProjectPathSuggestion]:
        """Answer the empty query from the installed snapshot only."""
        snapshot = self.snapshot
        if snapshot is None:
            return []
        return snapshot.head(limit=limit)

    def install(self, scan: ProjectPathScanResult) -> None:
        """Install *scan* as the snapshot of this root."""
        self._install(scan)

    def mark_dirty(self, changed: Iterable[str] | None = None) -> None:
        """Record that the snapshot may be stale.

        ``changed`` is the set of absolute paths the change came from; pass
        ``None`` when the change set is unknown, which forces a full rebuild.
        An empty set means the caller observed no change, so nothing is recorded.
        """
        if changed is None:
            self.dirty = True
            self._change_seq += 1
            self._unknown_seq += 1
            self._unknown_changes = True
            self.delta.clear()
            return
        paths = {os.path.abspath(path) for path in changed}
        if not paths:
            return
        self.dirty = True
        self._change_seq += 1
        self.delta.update(paths)

    async def apply_changes(self, paths: Iterable[str] = ()) -> bool:
        """Fold *paths* and every pending delta into the installed snapshot, off the event loop.

        The derive runs in a worker thread and the event loop only
        publishes the result by reference assignment, so ``True`` means "derived
        and published" rather than "finished synchronously".  A caller that
        awaits this sees the same state the old synchronous fold left behind;
        what changed is that :meth:`mark_dirty` alone no longer guarantees an
        installed result.  While the fold is in flight ``dirty`` stays ``True``
        and queries keep answering from the old snapshot.

        Nothing is written until the whole batch is known to be representable, so
        a pending delta from an earlier failed batch is folded with this one
        instead of being dropped.

        Returns ``False`` when this root cannot be kept correct in memory — no
        snapshot, a truncated scan, an unenumerable change set, a rebuild already
        in flight, a directory, a path whose visibility needs git evidence, a
        file-budget overrun, or an ``epoch``/``_change_seq`` that moved while
        deriving — so the caller upgrades to a full rebuild instead of guessing.
        """
        snapshot = self.snapshot
        if snapshot is None or snapshot.truncated or self._unknown_changes or self._building():
            return False

        pending = set(self.delta)
        pending.update(os.path.abspath(path) for path in paths)
        if not pending:
            # Stale with nothing pending to fold: only a rescan can prove the
            # snapshot current, so do not report it clean.
            return not self.dirty

        removals: list[str] = []
        additions: list[str] = []
        for path in pending:
            relative = self._relative_path(path)
            if relative is None:
                continue
            identity = _path_identity(relative)
            if os.path.isdir(path):
                return False
            if os.path.exists(path):
                if identity in self._identities:
                    continue
                if not self._addition_is_visible(relative):
                    return False
                additions.append(relative)
            elif identity in self._identities:
                removals.append(identity)

        if self._file_budget > 0 and len(self._files) + len(additions) > self._file_budget:
            # The scan caps file discovery; adding past it belongs to a rescan.
            return False

        if not removals and not additions:
            # Nothing representable changed, so this batch leaves the snapshot current.
            self.dirty = False
            self.delta.clear()
            return True

        epoch = self.epoch
        change_seq = self._change_seq
        bundle = await asyncio.to_thread(self._derive_fold, removals, additions)
        if self.epoch != epoch or self._change_seq != change_seq:
            # A rebuild installed, or a change landed while deriving: this bundle
            # does not cover the current state, so leave it pending for the next
            # fold or rescan instead of publishing a stale answer as current.
            return False
        self._publish(bundle)
        return True

    def discard(self) -> None:
        """Drop the snapshot: the next ``@`` cold-builds for the new context."""
        self.epoch += 1
        self.snapshot = None
        self.dirty = False
        self.delta.clear()
        self._unknown_changes = False
        self._files = []
        self._identities = {}
        self._directory_counts = {}
        self._source_truncations = {}
        self._file_budget = 0
        self._suggestion_budget = 0
        self._ordering = "path-sorted"

    async def refresh(self, scan: ScanProjectPaths | None = None) -> bool:
        """Rebuild the snapshot in the background; ``True`` when one was installed.

        Single-flight: concurrent callers await the same rebuild.  A failure
        keeps the installed snapshot (queries keep answering) and leaves
        ``dirty`` set so the next lifecycle event retries.
        """
        task = self.build_task
        if task is None or task.done():
            task = asyncio.ensure_future(self._rebuild(scan or _default_scan))
            self.build_task = task
        return await task

    def _building(self) -> bool:
        task = self.build_task
        return task is not None and not task.done()

    async def _rebuild(self, scan: ScanProjectPaths) -> bool:
        epoch = self.epoch
        change_seq = self._change_seq
        # Everything recorded before the scan starts is in its result; only what
        # arrives while it runs has to survive the install.  Unknown change sets
        # need their own sequence: a flag cannot tell "already covered by this
        # scan" from "landed after it started", and folding an enumerable delta
        # over a snapshot no scan covered would report a stale index current.
        covered_delta = set(self.delta)
        covered_unknown_seq = self._unknown_seq
        try:
            result = await scan(self.root)
            bundle = await asyncio.to_thread(_derive_from_scan, result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Workspace path index rebuild failed for %s", self.root, exc_info=True)
            self.dirty = True
            return False
        if self.epoch != epoch:
            return False
        arrived_delta = set(self.delta) - covered_delta
        arrived_unknown = self._unknown_seq != covered_unknown_seq
        self._publish(bundle)
        if self._change_seq != change_seq:
            # The scan may predate these changes, so the snapshot is not their
            # clean baseline: keep them pending for the next fold or rescan.
            self.delta.update(arrived_delta)
            self._unknown_changes = arrived_unknown
            self.dirty = True
        return True

    def _install(self, scan: ProjectPathScanResult) -> None:
        """Install a scan result on the calling thread.

        Production derives a scan's bookkeeping in a worker thread
        (:meth:`_rebuild`) and only publishes it here.  This synchronous form
        serves callers that already hold a scan, and must not grow derivation the
        event loop would otherwise repeat.
        """
        self._publish(_derive_from_scan(scan))

    def _derive_fold(self, removals: list[str], additions: list[str]) -> _InstallBundle:
        """Derive the folded rows on private copies — the thread half of a fold.

        A fold costs time proportional to the installed rows, not to the
        number of changed files, so it runs here, off the event loop, on copies
        of ``_files``, ``_identities`` and ``_directory_counts``.  The loop only
        publishes the returned bundle, so a large workspace no longer blocks
        input while its rows are re-derived.
        """
        files = list(self._files)
        identities = dict(self._identities)
        directory_counts = dict(self._directory_counts)
        for identity in removals:
            _drop_file(files, identities, directory_counts, identity)
        for relative_path in additions:
            _append_file(files, identities, directory_counts, relative_path)

        rows = _materialize_rows(files, directory_counts)
        suggestions_truncated = len(rows) > self._suggestion_budget
        if suggestions_truncated:
            rows = rows[: self._suggestion_budget]
        source_truncations = dict(self._source_truncations)
        if suggestions_truncated:
            source_truncations["suggestions"] = True
        scan = ProjectPathScanResult(
            root=self.root,
            paths=rows,
            file_count=sum(1 for row in rows if row.kind == "file"),
            suggestion_count=len(rows),
            truncated=any(source_truncations.values()),
            file_budget=self._file_budget,
            suggestion_budget=self._suggestion_budget,
            source_truncations=source_truncations,
            ordering=self._ordering,
        )
        return _InstallBundle(
            files=files,
            identities=identities,
            directory_counts=directory_counts,
            snapshot=ProjectPathIndex.build(scan),
            source_truncations=dict(scan.source_truncations),
            file_budget=scan.file_budget,
            suggestion_budget=scan.suggestion_budget,
            ordering=scan.ordering,
        )

    def _publish(self, bundle: _InstallBundle) -> None:
        """Install a derived bundle: reference assignments only.

        Nothing proportional to the row count may run here — everything
        was derived in :meth:`_derive_fold` or :func:`_derive_from_scan`.  Bumping
        :attr:`epoch` is what makes a scan or fold that started earlier drop its
        result instead of overwriting this snapshot.
        """
        self._files = bundle.files
        self._identities = bundle.identities
        self._directory_counts = bundle.directory_counts
        self.snapshot = bundle.snapshot
        self._source_truncations = bundle.source_truncations
        self._file_budget = bundle.file_budget
        self._suggestion_budget = bundle.suggestion_budget
        self._ordering = bundle.ordering
        self.epoch += 1
        self.delta.clear()
        self._unknown_changes = False
        self.dirty = False

    def _relative_path(self, path: str) -> str | None:
        """Return *path* relative to this root in ``/`` form, or ``None`` when outside it."""
        try:
            relative = os.path.relpath(os.path.abspath(path), os.path.abspath(self.root))
        except ValueError:
            return None
        relative = relative.replace(os.sep, "/")
        if relative == ".." or relative.startswith("../"):
            return None
        return relative

    @staticmethod
    def _addition_is_visible(relative_path: str) -> bool:
        """Whether a new path is in the visible set whatever git says.

        The visible set is the union of the ripgrep, tracked, dot-entry and
        ignored-visible passes, so a non-dot path outside the noise top-level
        directories with a referenceable extension is listed in every case.
        Dot paths need git evidence and noise/blacklisted paths are listed only
        when they are not ignored — both are undeterminable here, so they fall
        back to a rescan instead of being guessed.
        """
        return _include_ignored_visible_path(relative_path) and not _has_non_referenceable_file_extension(relative_path)
