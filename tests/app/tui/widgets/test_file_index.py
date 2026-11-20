# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the ``@``-menu workdir path index."""

from __future__ import annotations

import asyncio

import pytest

from chrys.app.tui.widgets.chrome import file_scanner as file_scanner_module
from chrys.app.tui.widgets.chrome.file_index import WorkdirPathIndex, WorkspacePathIndex
from chrys.app.tui.widgets.chrome.file_scanner import WorkdirPathScanResult, WorkdirPathSuggestion, fuzzy_filter
from tests.support.waiting import wait_for


def _index(suggestions: list[WorkdirPathSuggestion]) -> WorkdirPathIndex:
    return WorkdirPathIndex.build(WorkdirPathScanResult.from_suggestions(root="/repo", paths=suggestions))


def _paths(suggestions: list[WorkdirPathSuggestion]) -> list[str]:
    return [suggestion.path for suggestion in suggestions]


def _expected_fuzzy_paths(query: str, suggestions: list[WorkdirPathSuggestion], *, limit: int) -> list[str]:
    return fuzzy_filter(query, _paths(suggestions), max_results=limit)


class TestWorkdirPathScanResult:
    def test_from_suggestions_wraps_current_bare_list_shape(self) -> None:
        suggestions = [
            WorkdirPathSuggestion(path="src/", kind="directory"),
            WorkdirPathSuggestion(path="src/app.py", kind="file"),
            WorkdirPathSuggestion(path="README.md", kind="file"),
        ]

        scan = WorkdirPathScanResult.from_suggestions(root="/repo", paths=suggestions)

        assert scan.root == "/repo"
        assert scan.paths is suggestions
        assert scan.file_count == 2
        assert scan.suggestion_count == 3
        assert scan.truncated is False
        assert scan.file_budget == 2
        assert scan.suggestion_budget == 3
        assert scan.source_truncations == {}
        assert scan.ordering == "path-sorted"


class TestWorkdirPathIndex:
    def test_query_matches_fuzzy_filter_top_n_for_mixed_file_and_directory_paths(self) -> None:
        suggestions = [
            WorkdirPathSuggestion(path="src/chrys/app/tui/widgets/chrome/file_scanner.py", kind="file"),
            WorkdirPathSuggestion(path="docs/chrome.md", kind="file"),
            WorkdirPathSuggestion(path="README.md", kind="file"),
            WorkdirPathSuggestion(path="chrome/", kind="directory"),
            WorkdirPathSuggestion(path="src/chrome/", kind="directory"),
            WorkdirPathSuggestion(path="src/chrome/readme.md", kind="file"),
            WorkdirPathSuggestion(path="src/app/chrome.py", kind="file"),
        ]

        assert _paths(_index(suggestions).query("chrome", limit=5)) == _expected_fuzzy_paths(
            "chrome", suggestions, limit=5
        )

    @pytest.mark.parametrize("query", ["chrome", "CHROME", "caf\u00e9", "x", ""])
    def test_query_matches_fuzzy_filter_fixture(self, query: str) -> None:
        suggestions = [
            WorkdirPathSuggestion(path="src/chrys/app/tui/widgets/chrome/", kind="directory"),
            WorkdirPathSuggestion(path="src/chrys/app/tui/widgets/chrome/file_scanner.py", kind="file"),
            WorkdirPathSuggestion(path="docs/cafe\u0301.md", kind="file"),
            WorkdirPathSuggestion(path="abc/x.py", kind="file"),
            WorkdirPathSuggestion(path="a/e\u0301x_long.py", kind="file"),
            WorkdirPathSuggestion(path="\u00c5/x.py", kind="file"),
        ]

        assert _paths(_index(suggestions).query(query, limit=4)) == _expected_fuzzy_paths(query, suggestions, limit=4)

    def test_head_preserves_insertion_order(self) -> None:
        suggestions = [
            WorkdirPathSuggestion(path="b.py", kind="file"),
            WorkdirPathSuggestion(path="a.py", kind="file"),
            WorkdirPathSuggestion(path="src/", kind="directory"),
            WorkdirPathSuggestion(path="src/a.py", kind="file"),
        ]

        assert _index(suggestions).head(limit=3) == suggestions[:3]

    def test_non_ascii_scoring_uses_normalized_match_and_original_path_tiebreaks(self) -> None:
        assert _paths(
            _index(
                [
                    WorkdirPathSuggestion(path="abc/x.py", kind="file"),
                    WorkdirPathSuggestion(path="a/e\u0301x_long.py", kind="file"),
                ]
            ).query("x")
        ) == ["a/e\u0301x_long.py", "abc/x.py"]
        assert _paths(
            _index(
                [
                    WorkdirPathSuggestion(path="e\u0301x.py", kind="file"),
                    WorkdirPathSuggestion(path="\u00e9x.py", kind="file"),
                ]
            ).query("x")
        ) == ["\u00e9x.py", "e\u0301x.py"]
        assert _paths(
            _index(
                [
                    WorkdirPathSuggestion(path="\u00e4/x.py", kind="file"),
                    WorkdirPathSuggestion(path="\u00c5/x.py", kind="file"),
                ]
            ).query("x")
        ) == ["\u00c5/x.py", "\u00e4/x.py"]

    def test_query_uses_precomputed_candidate_normalization(self, monkeypatch: pytest.MonkeyPatch) -> None:
        suggestions = [WorkdirPathSuggestion(path=f"src/path_{index:03d}/file.py", kind="file") for index in range(200)]
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


def _scan(root: str, *files: str) -> WorkdirPathScanResult:
    return WorkdirPathScanResult.from_suggestions(
        root=root,
        paths=[WorkdirPathSuggestion(path=path, kind="file") for path in files],
    )


class TestWorkspacePathIndex:
    async def test_rescan_installs_the_scan_and_clears_staleness(self) -> None:
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", "old.py"))
        index.mark_stale()

        async def scan(root: str) -> WorkdirPathScanResult:
            return _scan(root, "new.py")

        assert await index.rescan(scan) is True
        assert index.current is True
        assert index.snapshot is not None
        assert _paths(index.snapshot.head()) == ["new.py"]

    async def test_a_change_marked_during_a_rescan_leaves_the_new_snapshot_stale(self) -> None:
        index = WorkspacePathIndex("/repo")
        started = asyncio.Event()
        release = asyncio.Event()

        async def scan(root: str) -> WorkdirPathScanResult:
            started.set()
            await release.wait()
            return _scan(root, "a.py")

        rescan = asyncio.create_task(index.rescan(scan))
        await wait_for(lambda: started.is_set() or rescan.done(), description="rescan started")
        assert not rescan.done()
        index.mark_stale()
        release.set()

        assert await rescan is True
        assert index.snapshot is not None
        assert index.stale is True
        assert index.current is False

    async def test_a_failed_rescan_keeps_the_snapshot_and_its_staleness(self) -> None:
        index = WorkspacePathIndex("/repo")
        index.install(_scan("/repo", "a.py"))
        snapshot = index.snapshot
        index.mark_stale()

        async def scan(_root: str) -> WorkdirPathScanResult:
            raise OSError("scan failed")

        assert await index.rescan(scan) is False
        assert index.snapshot is snapshot
        assert index.stale is True
