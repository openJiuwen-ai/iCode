# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the ``@``-menu project path index."""

from __future__ import annotations

import asyncio
import os
import threading

import pytest

from chrys.app.tui.widgets.chrome import file_scanner as file_scanner_module
from chrys.app.tui.widgets.chrome.file_index import ProjectPathIndex, WorkspacePathIndex
from chrys.app.tui.widgets.chrome.file_scanner import ProjectPathScanResult, ProjectPathSuggestion, fuzzy_filter


def _index(suggestions: list[ProjectPathSuggestion]) -> ProjectPathIndex:
    return ProjectPathIndex.build(ProjectPathScanResult.from_suggestions(root="/repo", paths=suggestions))


def _paths(suggestions: list[ProjectPathSuggestion]) -> list[str]:
    return [suggestion.path for suggestion in suggestions]


def _expected_fuzzy_paths(query: str, suggestions: list[ProjectPathSuggestion], *, limit: int) -> list[str]:
    return fuzzy_filter(query, _paths(suggestions), max_results=limit)


class TestProjectPathScanResult:
    def test_from_suggestions_wraps_current_bare_list_shape(self) -> None:
        suggestions = [
            ProjectPathSuggestion(path="src/", kind="directory"),
            ProjectPathSuggestion(path="src/app.py", kind="file"),
            ProjectPathSuggestion(path="README.md", kind="file"),
        ]

        scan = ProjectPathScanResult.from_suggestions(root="/repo", paths=suggestions)

        assert scan.root == "/repo"
        assert scan.paths is suggestions
        assert scan.file_count == 2
        assert scan.suggestion_count == 3
        assert scan.truncated is False
        assert scan.file_budget == 2
        assert scan.suggestion_budget == 3
        assert scan.source_truncations == {}
        assert scan.ordering == "path-sorted"


class TestProjectPathIndex:
    def test_query_matches_fuzzy_filter_top_n_for_mixed_file_and_directory_paths(self) -> None:
        suggestions = [
            ProjectPathSuggestion(path="src/chrys/app/tui/widgets/chrome/file_scanner.py", kind="file"),
            ProjectPathSuggestion(path="docs/chrome.md", kind="file"),
            ProjectPathSuggestion(path="README.md", kind="file"),
            ProjectPathSuggestion(path="chrome/", kind="directory"),
            ProjectPathSuggestion(path="src/chrome/", kind="directory"),
            ProjectPathSuggestion(path="src/chrome/readme.md", kind="file"),
            ProjectPathSuggestion(path="src/app/chrome.py", kind="file"),
        ]

        assert _paths(_index(suggestions).query("chrome", limit=5)) == _expected_fuzzy_paths(
            "chrome", suggestions, limit=5
        )

    @pytest.mark.parametrize("query", ["chrome", "CHROME", "caf\u00e9", "x", ""])
    def test_query_matches_fuzzy_filter_fixture(self, query: str) -> None:
        suggestions = [
            ProjectPathSuggestion(path="src/chrys/app/tui/widgets/chrome/", kind="directory"),
            ProjectPathSuggestion(path="src/chrys/app/tui/widgets/chrome/file_scanner.py", kind="file"),
            ProjectPathSuggestion(path="docs/cafe\u0301.md", kind="file"),
            ProjectPathSuggestion(path="abc/x.py", kind="file"),
            ProjectPathSuggestion(path="a/e\u0301x_long.py", kind="file"),
            ProjectPathSuggestion(path="\u00c5/x.py", kind="file"),
        ]

        assert _paths(_index(suggestions).query(query, limit=4)) == _expected_fuzzy_paths(query, suggestions, limit=4)

    def test_head_preserves_insertion_order(self) -> None:
        suggestions = [
            ProjectPathSuggestion(path="b.py", kind="file"),
            ProjectPathSuggestion(path="a.py", kind="file"),
            ProjectPathSuggestion(path="src/", kind="directory"),
            ProjectPathSuggestion(path="src/a.py", kind="file"),
        ]

        assert _index(suggestions).head(limit=3) == suggestions[:3]

    def test_non_ascii_scoring_uses_normalized_match_and_original_path_tiebreaks(self) -> None:
        assert _paths(
            _index(
                [
                    ProjectPathSuggestion(path="abc/x.py", kind="file"),
                    ProjectPathSuggestion(path="a/e\u0301x_long.py", kind="file"),
                ]
            ).query("x")
        ) == ["a/e\u0301x_long.py", "abc/x.py"]
        assert _paths(
            _index(
                [
                    ProjectPathSuggestion(path="e\u0301x.py", kind="file"),
                    ProjectPathSuggestion(path="\u00e9x.py", kind="file"),
                ]
            ).query("x")
        ) == ["\u00e9x.py", "e\u0301x.py"]
        assert _paths(
            _index(
                [
                    ProjectPathSuggestion(path="\u00e4/x.py", kind="file"),
                    ProjectPathSuggestion(path="\u00c5/x.py", kind="file"),
                ]
            ).query("x")
        ) == ["\u00c5/x.py", "\u00e4/x.py"]

    def test_query_uses_precomputed_candidate_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        suggestions = [ProjectPathSuggestion(path=f"src/path_{index:03d}/file.py", kind="file") for index in range(200)]
        index = _index(suggestions)
        expected = _expected_fuzzy_paths("FILE", suggestions, limit=7)
        calls: list[str] = []
        original_normalize = file_scanner_module.unicodedata.normalize

        def normalize(form: str, value: str) -> str:
            calls.append(value)
            return original_normalize(form, value)

        monkeypatch.setattr(file_scanner_module.unicodedata, "normalize", normalize)

        assert _paths(index.query("FILE", limit=7)) == expected
        assert calls == ["FILE"]


def _scan(
    root: str,
    files: list[str],
    *,
    truncated: bool = False,
    source_truncations: dict[str, bool] | None = None,
) -> ProjectPathScanResult:
    """Build a scan result shaped like the scanner's: directory rows derived from files."""
    rows: list[ProjectPathSuggestion] = []
    seen: set[str] = set()
    for path in files:
        parts = path.split("/")
        for index in range(1, len(parts)):
            directory = "/".join(parts[:index]) + "/"
            if directory in seen:
                continue
            seen.add(directory)
            rows.append(ProjectPathSuggestion(path=directory, kind="directory"))
        rows.append(ProjectPathSuggestion(path=path, kind="file"))
    return ProjectPathScanResult.from_suggestions(
        root=root,
        paths=rows,
        file_budget=1_000_000,
        suggestion_budget=2_000_000,
        truncated=truncated,
        source_truncations=source_truncations,
    )


class TestWorkspacePathIndexLifecycle:
    def test_queries_answer_from_the_installed_snapshot_only(self) -> None:
        index = WorkspacePathIndex("/repo")

        assert index.query("a") == []
        assert index.head() == []
        assert index.installed is False

        index.install(_scan("/repo", ["src/a.py", "src/b.py"]))

        assert index.installed is True
        assert _paths(index.query("b", limit=5)) == ["src/b.py"]
        assert _paths(index.head(limit=2)) == ["src/", "src/a.py"]

    def test_mark_dirty_keeps_answering_from_the_old_snapshot(self) -> None:
        """A dirty snapshot is still the answer: only a finished rebuild replaces it."""
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", ["src/a.py"]))
        installed = index.snapshot
        epoch = index.epoch

        index.mark_dirty({"/repo/src/a.py"})

        assert index.dirty is True
        assert index.snapshot is installed
        assert index.epoch == epoch
        assert index.delta == {os.path.abspath("/repo/src/a.py")}
        assert _paths(index.query("a", limit=5)) == ["src/a.py"]

    def test_mark_dirty_without_paths_drops_the_known_delta(self) -> None:
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", ["src/a.py"]))
        index.mark_dirty({"/repo/src/a.py"})

        index.mark_dirty()

        assert index.dirty is True
        assert index.delta == set()

    async def test_refresh_installs_a_new_snapshot_and_bumps_the_epoch(self) -> None:
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", ["old.py"]))
        before = index.epoch
        scans: list[str] = []

        async def scan(root: str) -> ProjectPathScanResult:
            scans.append(root)
            return _scan(root, ["new.py"])

        index.mark_dirty()

        assert await index.refresh(scan) is True
        assert scans == ["/repo"]
        assert index.epoch == before + 1
        assert index.dirty is False
        assert index.delta == set()
        assert _paths(index.query("", limit=5)) == ["new.py"]

    async def test_refresh_failure_keeps_the_old_snapshot_and_stays_dirty(self) -> None:
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", ["old.py"]))
        installed = index.snapshot

        async def failing_scan(root: str) -> ProjectPathScanResult:
            raise OSError(f"cannot scan {root}")

        index.mark_dirty()

        assert await index.refresh(failing_scan) is False
        assert index.snapshot is installed
        assert index.dirty is True
        assert _paths(index.query("old", limit=5)) == ["old.py"]

    async def test_refresh_is_single_flight(self) -> None:
        index = WorkspacePathIndex("/repo")
        scans: list[str] = []
        release = asyncio.Event()

        async def slow_scan(root: str) -> ProjectPathScanResult:
            scans.append(root)
            await release.wait()
            return _scan(root, ["a.py"])

        first = asyncio.ensure_future(index.refresh(slow_scan))
        await asyncio.sleep(0)
        second = asyncio.ensure_future(index.refresh(slow_scan))
        await asyncio.sleep(0)
        release.set()

        assert await first is True
        assert await second is True
        assert scans == ["/repo"]

    async def test_rebuild_that_outlives_a_discard_is_dropped(self) -> None:
        """A scan that finishes after the workspace changed must not install."""
        index = WorkspacePathIndex("/repo")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan(root: str) -> ProjectPathScanResult:
            started.set()
            await release.wait()
            return _scan(root, ["stale.py"])

        rebuild = asyncio.ensure_future(index.refresh(slow_scan))
        await started.wait()
        index.discard()
        release.set()

        assert await rebuild is False
        assert index.snapshot is None

    async def test_change_recorded_while_rebuilding_survives_the_install(self) -> None:
        """A change that lands mid-scan is not in the scan, so it stays pending.

        Dropping it would install the scan as a clean baseline and the file would
        never appear: the next ``apply_changes`` would fold nothing and report the
        snapshot current.
        """
        index = WorkspacePathIndex("/repo")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan(root: str) -> ProjectPathScanResult:
            started.set()
            await release.wait()
            return _scan(root, ["a.py"])

        rebuild = asyncio.ensure_future(index.refresh(slow_scan))
        await started.wait()
        index.mark_dirty({"/repo/b.py"})
        release.set()

        assert await rebuild is True
        assert index.dirty is True
        assert index.delta == {os.path.abspath("/repo/b.py")}

    async def test_a_change_recorded_during_a_rebuild_is_folded_by_the_next_turn(
        self,
        tmp_path,
    ) -> None:
        root = str(tmp_path)
        created = tmp_path / "b.py"
        created.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan(scan_root: str) -> ProjectPathScanResult:
            started.set()
            await release.wait()
            return _scan(scan_root, ["a.py"])

        rebuild = asyncio.ensure_future(index.refresh(slow_scan))
        await started.wait()
        assert await index.apply_changes([str(created)]) is False
        index.mark_dirty([str(created)])
        release.set()
        assert await rebuild is True

        # A later turn that changed nothing still folds what was left pending.
        assert await index.apply_changes() is True

        assert index.dirty is False
        assert "b.py" in _paths(index.query("", limit=10))

    async def test_an_unknown_change_that_lands_mid_rebuild_is_not_washed_clean_by_a_fold(
        self,
        tmp_path,
    ) -> None:
        """An unknowable change during a scan is not in that scan, so a later fold cannot clear it.

        ``_install`` dropped the pending unknown set whenever one was already
        pending when the scan started, so the next enumerable turn end folded a
        path over a snapshot no scan had covered and reported it current.
        """
        root = str(tmp_path)
        old = tmp_path / "old.py"
        old.write_text("", encoding="utf-8")
        new = tmp_path / "new.py"
        new.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_scan(scan_root: str) -> ProjectPathScanResult:
            started.set()
            await release.wait()
            return _scan(scan_root, ["old.py"])

        index.mark_dirty()  # turn 1: change set unknowable, so a rescan starts
        rebuild = asyncio.ensure_future(index.refresh(slow_scan))
        await started.wait()
        old.unlink()
        index.mark_dirty()  # turn 2: another unknowable change, after the scan began
        release.set()

        assert await rebuild is True
        assert index.dirty is True
        # The enumerable delta must not be folded over a scan that never covered turn 2.
        assert await index.apply_changes([str(new)]) is False
        assert index.dirty is True

        async def rescan(scan_root: str) -> ProjectPathScanResult:
            return _scan(scan_root, ["new.py"])

        assert await index.refresh(rescan) is True
        assert index.dirty is False
        assert _paths(index.head(limit=10)) == ["new.py"]

    async def test_apply_changes_refuses_when_stale_with_nothing_pending(self, tmp_path) -> None:
        """A stale snapshot with no known delta can only be cleared by a rescan."""
        root = str(tmp_path)
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))
        index.dirty = True

        assert await index.apply_changes() is False
        assert index.dirty is True

    def test_mark_dirty_with_no_changed_paths_records_nothing(self) -> None:
        """An empty change set is the caller saying it observed no change."""
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", ["a.py"]))

        index.mark_dirty([])

        assert index.dirty is False
        assert index.delta == set()


class TestWorkspacePathIndexIncrementalApply:
    async def test_apply_changes_adds_and_removes_files_with_their_directory_rows(self, tmp_path) -> None:
        root = str(tmp_path)
        (tmp_path / "src").mkdir()
        new_file = tmp_path / "src" / "b.py"
        new_file.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/a.py"]))

        assert await index.apply_changes([str(new_file)]) is True

        assert _paths(index.query("b", limit=5)) == ["src/b.py"]
        assert index.snapshot is not None
        assert index.snapshot.file_count == 2
        assert index.dirty is False

        new_file.unlink()

        assert await index.apply_changes([str(new_file)]) is True
        assert index.query("b") == []
        assert index.snapshot is not None
        assert index.snapshot.file_count == 1

    async def test_apply_changes_appends_an_addition_instead_of_inserting_it_in_path_order(
        self,
        tmp_path,
    ) -> None:
        """Folded rows are appended, so head order is not the scan order.

        ``head()`` returns the last full-scan prefix plus the appended tail, not a
        lexicographic prefix: ``src/aaa.py`` sorts ahead of ``src/zzz.py`` yet
        lands after it, until the next full rebuild.
        """
        root = str(tmp_path)
        (tmp_path / "src").mkdir()
        added = tmp_path / "src" / "aaa.py"
        added.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/zzz.py"]))

        assert await index.apply_changes([str(added)]) is True

        assert _paths(index.query("", limit=10)) == ["src/", "src/zzz.py", "src/aaa.py"]

    async def test_apply_changes_drops_a_directory_row_with_its_last_file(self, tmp_path) -> None:
        root = str(tmp_path)
        nested = tmp_path / "src" / "deep"
        nested.mkdir(parents=True)
        only = nested / "only.py"
        only.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/deep/only.py"]))

        only.unlink()

        assert await index.apply_changes([str(only)]) is True
        assert _paths(index.query("", limit=10)) == []

    async def test_apply_changes_keeps_a_directory_row_while_another_file_references_it(self, tmp_path) -> None:
        root = str(tmp_path)
        (tmp_path / "src").mkdir()
        removed = tmp_path / "src" / "a.py"
        removed.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/a.py", "src/b.py"]))

        removed.unlink()

        assert await index.apply_changes([str(removed)]) is True
        assert _paths(index.query("", limit=10)) == ["src/", "src/b.py"]

    @pytest.mark.parametrize(
        "relative",
        [
            ".github/workflows/ci.yml",
            "dist/bundle.js",
            "assets/logo.exe",
        ],
        ids=["dot-directory", "noise-top-level", "referenceable-extension-unknown"],
    )
    async def test_apply_changes_refuses_additions_whose_visibility_needs_git(self, tmp_path, relative: str) -> None:
        """Dot entries, noise top-levels and blacklisted extensions are listed only when git says so."""
        root = str(tmp_path)
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/a.py"]))

        assert await index.apply_changes([str(target)]) is False
        assert index.query("logo") == []
        assert index.query("ci.yml") == []

    async def test_apply_changes_refuses_a_truncated_snapshot(self, tmp_path) -> None:
        root = str(tmp_path)
        added = tmp_path / "b.py"
        added.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"], truncated=True, source_truncations={"rg": True}))

        assert await index.apply_changes([str(added)]) is False

    async def test_apply_changes_ignores_paths_outside_the_root(self, tmp_path) -> None:
        root = str(tmp_path / "repo")
        os.makedirs(root)
        outside = tmp_path / "elsewhere.py"
        outside.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))

        assert await index.apply_changes([str(outside)]) is True
        assert _paths(index.query("", limit=10)) == ["a.py"]

    async def test_apply_changes_does_not_double_list_a_known_file(self, tmp_path) -> None:
        root = str(tmp_path)
        known = tmp_path / "a.py"
        known.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))

        assert await index.apply_changes([str(known)]) is True
        assert _paths(index.query("", limit=10)) == ["a.py"]

    async def test_apply_changes_refuses_while_a_rebuild_is_in_flight(self, tmp_path) -> None:
        root = str(tmp_path)
        added = tmp_path / "b.py"
        added.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))
        index.build_task = asyncio.create_task(asyncio.sleep(3600))

        try:
            assert await index.apply_changes([str(added)]) is False
        finally:
            index.build_task.cancel()

    async def test_discard_drops_the_snapshot_and_its_bookkeeping(self, tmp_path) -> None:
        root = str(tmp_path)
        added = tmp_path / "b.py"
        added.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))
        epoch = index.epoch

        index.discard()

        assert index.snapshot is None
        assert index.dirty is False
        assert index.epoch == epoch + 1
        assert index.query("a") == []
        assert await index.apply_changes([str(added)]) is False

    async def test_apply_changes_folds_a_pending_delta_from_an_earlier_failed_batch(self, tmp_path) -> None:
        """A change that could not be folded earlier is not lost by the next batch."""
        root = str(tmp_path)
        first = tmp_path / "src" / "first.py"
        second = tmp_path / "src" / "second.py"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("", encoding="utf-8")
        second.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/old.py"]))

        blocked = tmp_path / "dist" / "bundle.js"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("", encoding="utf-8")
        assert await index.apply_changes([str(first), str(blocked)]) is False
        index.mark_dirty([str(first), str(blocked)])

        # The retry batch carries only the new path, yet the earlier one is still
        # folded: pending deltas are kept until they are represented.
        pending = set(index.delta)
        assert str(first) in {os.path.abspath(path) for path in pending}

    async def test_apply_changes_folds_every_pending_path_on_retry(self, tmp_path) -> None:
        root = str(tmp_path)
        first = tmp_path / "src" / "first.py"
        second = tmp_path / "src" / "second.py"
        first.parent.mkdir(parents=True, exist_ok=True)
        first.write_text("", encoding="utf-8")
        second.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/old.py"]))
        blocked = tmp_path / "dist" / "bundle.js"
        blocked.parent.mkdir(parents=True, exist_ok=True)
        blocked.write_text("", encoding="utf-8")
        assert await index.apply_changes([str(first), str(blocked)]) is False
        index.mark_dirty([str(first), str(blocked)])
        blocked.unlink()

        assert await index.apply_changes([str(second)]) is True

        paths = _paths(index.query("", limit=10))
        assert "src/first.py" in paths
        assert "src/second.py" in paths
        assert index.dirty is False

    async def test_a_change_recorded_during_a_fold_is_not_published_as_covered(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A change that lands while the rows are derived must stay pending.

        The bundle is derived from the copies taken before ``late.py`` existed,
        so publishing it would clear the delta and report the snapshot current:
        the path would never appear and nothing would ever retry it.  The guard
        drops that bundle and the next fold picks the path up.
        """
        root = str(tmp_path)
        (tmp_path / "src").mkdir()
        early = tmp_path / "src" / "early.py"
        early.write_text("", encoding="utf-8")
        late = tmp_path / "src" / "late.py"
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["src/a.py"]))

        fold_started = threading.Event()
        release_fold = threading.Event()
        derive_fold = WorkspacePathIndex._derive_fold

        def gated_derive(self, removals, additions):
            fold_started.set()
            release_fold.wait(timeout=10)
            return derive_fold(self, removals, additions)

        monkeypatch.setattr(WorkspacePathIndex, "_derive_fold", gated_derive)

        index.mark_dirty([str(early)])
        fold = asyncio.ensure_future(index.apply_changes())
        assert await asyncio.to_thread(fold_started.wait, 5) is True

        late.write_text("", encoding="utf-8")
        index.mark_dirty([str(late)])
        release_fold.set()

        assert await fold is False
        assert index.dirty is True
        assert index.delta == {os.path.abspath(str(early)), os.path.abspath(str(late))}
        assert "src/late.py" not in _paths(index.query("", limit=10))

        assert await index.apply_changes() is True
        assert index.dirty is False
        assert "src/late.py" in _paths(index.query("", limit=10))

    async def test_apply_changes_refuses_after_an_unknown_change_set(self, tmp_path) -> None:
        """An unenumerable change set can only be covered by a rescan."""
        root = str(tmp_path)
        added = tmp_path / "b.py"
        added.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))
        index.mark_dirty()

        assert await index.apply_changes([str(added)]) is False

    async def test_a_rescan_clears_the_unknown_change_set(self, tmp_path) -> None:
        root = str(tmp_path)
        added = tmp_path / "b.py"
        added.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(root)
        index.install(_scan(root, ["a.py"]))
        index.mark_dirty()
        index.install(_scan(root, ["a.py"]))

        assert await index.apply_changes([str(added)]) is True

    @pytest.mark.skipif(os.name != "nt", reason="path identity is case-insensitive only on Windows")
    async def test_apply_changes_does_not_list_a_case_variant_of_a_known_file(self, tmp_path) -> None:
        """Windows is case-insensitive: ``src/App.py`` is the row ``src/app.py``.

        Folding by the literal relative path appended a second row for one file,
        so a turn that touched it through a different spelling duplicated it.
        """
        (tmp_path / "src").mkdir()
        known = tmp_path / "src" / "app.py"
        known.write_text("", encoding="utf-8")
        index = WorkspacePathIndex(str(tmp_path))
        index.install(_scan(str(tmp_path), ["src/app.py"]))

        assert await index.apply_changes([str(tmp_path / "src" / "App.py")]) is True

        assert _paths(index.head(limit=10)) == ["src/", "src/app.py"]
