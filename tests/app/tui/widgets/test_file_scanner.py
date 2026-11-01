# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the ``@``-menu project file scanner."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from chrys.app.tui.widgets.chrome import file_scanner as file_scanner_module
from chrys.app.tui.widgets.chrome.file_scanner import (
    DEFAULT_FILE_DISCOVERY_BUDGET,
    DEFAULT_SUGGESTION_BUDGET,
    ProjectPathScanResult,
    ProjectPathSuggestion,
    _append_unseen_dot_entries_from_walk,
    _ChunkedLineReader,
    _is_referenceable_walk_name,
    _probe_ignored_visible_files,
    _project_path_suggestions,
    _scan_dot_entries,
    _scan_os_walk_files,
    _select_ignored_visible_files,
    _top_level_dot_entries,
    _walk_relative_path,
    fuzzy_filter,
    scan_project_paths,
)
from chrys.foundation.platform import get_platform
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT

GIT = shutil.which("git")


def _git(repo: Path, *args: str) -> None:
    if GIT is None:
        pytest.skip("git is required for repository-backed file scanner tests")
    subprocess.run(
        [GIT, *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _paths(entries: list[ProjectPathSuggestion]) -> list[str]:
    return [entry.path for entry in entries]


def _scan_paths(scan: ProjectPathScanResult) -> list[str]:
    return _paths(scan.paths)


def _windows_extended_path(path: Path) -> Path:
    """Return an extended-length path that bypasses Win32 DOS-device parsing."""
    absolute_parent = path.parent.resolve()
    return Path("\\\\?\\" + str(absolute_parent / path.name))


class TestFuzzyFilter:
    def test_top_n_ordering_for_mixed_file_and_directory_paths(self) -> None:
        """Matches are ordered by match position, path length, then path text."""
        candidates = [
            "src/chrys/app/tui/widgets/chrome/file_scanner.py",
            "docs/chrome.md",
            "README.md",
            "chrome/",
            "src/chrome/",
            "src/chrome/readme.md",
            "src/app/chrome.py",
        ]

        assert fuzzy_filter("chrome", candidates, max_results=5) == [
            "chrome/",
            "src/chrome/",
            "src/chrome/readme.md",
            "docs/chrome.md",
            "src/app/chrome.py",
        ]

    def test_full_relative_path_substring_matches_directory_segments(self) -> None:
        """A query can match any segment of the full relative path."""
        candidates = [
            "src/chrys/app/tui/widgets/chrome/file_scanner.py",
            "src/chrys/orchestration/engine/engine.py",
        ]

        assert fuzzy_filter("chrome", candidates) == ["src/chrys/app/tui/widgets/chrome/file_scanner.py"]

    def test_query_casefolding_matches_lowercase_paths(self) -> None:
        candidates = ["docs/x.md", "src/chrome/file.py"]

        assert fuzzy_filter("CHROME", candidates) == ["src/chrome/file.py"]

    def test_unicode_normalization_matches_decomposed_filesystem_paths(self) -> None:
        """NFC queries still match decomposed path strings returned by filesystems."""
        candidates = ["docs/cafe\u0301.md", "docs/plain.md"]

        assert fuzzy_filter("caf\u00e9", candidates) == ["docs/cafe\u0301.md"]

    def test_non_ascii_scoring_uses_normalized_match_and_original_path_tiebreaks(self) -> None:
        """Lock current mixed normalized/original scoring tuple behavior."""
        assert fuzzy_filter("x", ["abc/x.py", "a/e\u0301x_long.py"]) == ["a/e\u0301x_long.py", "abc/x.py"]
        assert fuzzy_filter("x", ["e\u0301x.py", "\u00e9x.py"]) == ["\u00e9x.py", "e\u0301x.py"]
        assert fuzzy_filter("x", ["\u00e4/x.py", "\u00c5/x.py"]) == ["\u00c5/x.py", "\u00e4/x.py"]

    def test_empty_query_returns_candidate_head(self) -> None:
        candidates = ["b.py", "a.py", "src/", "src/a.py"]

        assert fuzzy_filter("", candidates, max_results=3) == candidates[:3]


@pytest.fixture
def repo(tmp_path: Path, git_repo_factory: Callable[[Path], Path]) -> Path:
    if GIT is None:
        pytest.skip("git is required for repository-backed file scanner tests")
    return git_repo_factory(tmp_path)


class TestScanProjectPaths:
    def test_missing_root_returns_empty_scan_without_subprocess(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Downloaded sessions can reference a workspace path that does not exist locally."""
        root = "Z:\\Fake\\MissingWorkspace"

        def fail_subprocess(*_args: object, **_kwargs: object) -> object:
            raise AssertionError("missing roots should not start file discovery subprocesses")

        monkeypatch.setattr(file_scanner_module.os.path, "isdir", lambda _path: False)
        monkeypatch.setattr(file_scanner_module, "managed_subprocess", fail_subprocess)

        scan = asyncio.run(scan_project_paths(root, file_budget=5, suggestion_budget=7))

        assert scan == ProjectPathScanResult(
            root=root,
            paths=[],
            file_count=0,
            suggestion_count=0,
            truncated=False,
            file_budget=5,
            suggestion_budget=7,
            source_truncations={},
        )

    def test_invalid_subprocess_cwd_returns_empty_scan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Windows reports a missing cwd to subprocesses as ``NotADirectoryError`` / WinError 267."""
        root = "Z:\\Fake\\MissingWorkspace"

        class _InvalidCwdProcess:
            async def __aenter__(self) -> object:
                raise NotADirectoryError(267, "The directory name is invalid", root)

            async def __aexit__(self, *_args: object) -> None:
                return None

        def invalid_cwd_subprocess(*_args: object, **kwargs: object) -> _InvalidCwdProcess:
            assert kwargs.get("cwd") == root
            return _InvalidCwdProcess()

        original_walk = file_scanner_module.os.walk
        original_listdir = file_scanner_module.os.listdir

        def missing_walk(top: str, *args: object, **kwargs: object):
            # The fabricated drive must never be walked on a host where it could exist.
            return iter(()) if top == root else original_walk(top, *args, **kwargs)

        def missing_listdir(path: str = "."):
            if path == root:
                raise FileNotFoundError(2, "injected missing workspace", path)
            return original_listdir(path)

        monkeypatch.setattr(file_scanner_module.os.path, "isdir", lambda path: path == root)
        monkeypatch.setattr(file_scanner_module, "managed_subprocess", invalid_cwd_subprocess)
        monkeypatch.setattr(file_scanner_module.os, "walk", missing_walk)
        monkeypatch.setattr(file_scanner_module.os, "listdir", missing_listdir)

        scan = asyncio.run(scan_project_paths(root, file_budget=5, suggestion_budget=7))

        assert scan == ProjectPathScanResult(
            root=root,
            paths=[],
            file_count=0,
            suggestion_count=0,
            truncated=False,
            file_budget=5,
            suggestion_budget=7,
            source_truncations={},
            ordering="discovery",
        )

    def test_directory_entries_are_derived_from_referenceable_files(self, repo: Path) -> None:
        """Parent directories are included, sorted before their children."""
        (repo / "src" / "chrys" / "app" / "tui").mkdir(parents=True)
        (repo / "src" / "chrys" / "app" / "tui" / "input_bar.py").write_text("py\n")
        (repo / "src" / "chrys" / "orchestration" / "engine").mkdir(parents=True)
        (repo / "src" / "chrys" / "orchestration" / "engine" / "engine.py").write_text("py\n")

        scan = asyncio.run(scan_project_paths(str(repo)))
        entries = scan.paths
        paths = _paths(entries)
        by_path = {entry.path: entry for entry in entries}

        assert scan.file_budget == DEFAULT_FILE_DISCOVERY_BUDGET
        assert scan.suggestion_budget == DEFAULT_SUGGESTION_BUDGET
        assert scan.truncated is False
        assert "." not in paths
        assert by_path["src/"].kind == "directory"
        assert by_path["src/chrys/"].kind == "directory"
        assert by_path["src/chrys/app/tui/"].kind == "directory"
        assert by_path["src/chrys/app/tui/input_bar.py"].kind == "file"
        assert paths.index("src/chrys/app/tui/") < paths.index("src/chrys/app/tui/input_bar.py")

    def test_backslash_in_posix_filename_is_not_treated_as_separator(self) -> None:
        """Backslash is a valid POSIX filename character, not a path separator."""
        result = _project_path_suggestions(["notes\\draft.md"], max_paths=10)

        assert result.suggestions == [ProjectPathSuggestion(path="notes\\draft.md", kind="file")]
        assert result.truncated is False

    def test_walk_relative_path_is_lexical(self, tmp_path: Path) -> None:
        """Walk-relative display paths do not resolve or stat the final component."""
        root = str(tmp_path)
        dirpath = str(tmp_path / "cache" / "pkg")

        assert _walk_relative_path(root, dirpath, "nul") == "cache/pkg/nul"

    @pytest.mark.skipif(get_platform().is_windows, reason="POSIX permits nul as a regular filename")
    def test_posix_walk_keeps_nul_file_referenceable(self, tmp_path: Path) -> None:
        """Windows device-name filtering must not change valid POSIX filenames."""
        (tmp_path / "nul").write_text("valid on POSIX\n")

        result = _scan_os_walk_files(str(tmp_path), max_files=10)

        assert result.paths == ["nul"]
        assert _is_referenceable_walk_name("nul") is True

    @pytest.mark.skipif(not get_platform().is_windows, reason="requires Win32 reserved-name semantics")
    def test_windows_walk_skips_real_nul_file(self, tmp_path: Path) -> None:
        """A real reserved-name file must not reach Win32 path normalization."""
        reserved = _windows_extended_path(tmp_path / "nul")
        reserved.write_text("created through the extended namespace\n")
        (tmp_path / "keep.txt").write_text("keep\n")
        try:
            result = _scan_os_walk_files(str(tmp_path), max_files=10)
        finally:
            reserved.unlink(missing_ok=True)

        assert result.paths == ["keep.txt"]
        assert _is_referenceable_walk_name("nul") is False

    @pytest.mark.parametrize(
        "name",
        ["nul", "NUL.txt", "con", "aux.js", "com1", "trailing.", "trailing ", "bad:name"],
    )
    def test_reserved_walk_name_filter_is_windows_only(self, name: str) -> None:
        """DOS device and reserved spellings remain valid suggestion names on POSIX."""
        assert _is_referenceable_walk_name(name) is not get_platform().is_windows
        assert _is_referenceable_walk_name("regular.txt") is True

    def test_visible_walk_prunes_unreferenceable_directories(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The visible fallback removes rejected directories before descent."""
        root = str(tmp_path)
        observed_dirnames: list[str] = []

        def fake_walk(_root: str):
            dirnames = ["reserved", "src"]
            yield root, dirnames, ["reserved", "keep.txt"]
            observed_dirnames.extend(dirnames)
            if "reserved" in dirnames:
                raise AssertionError("reserved directory was not pruned")
            yield str(tmp_path / "src"), [], ["nested.txt"]

        monkeypatch.setattr(file_scanner_module.os, "walk", fake_walk)
        monkeypatch.setattr(
            file_scanner_module,
            "_is_referenceable_walk_name",
            lambda name: name != "reserved",
        )

        result = _scan_os_walk_files(root, max_files=10)

        assert observed_dirnames == ["src"]
        assert result.paths == ["keep.txt", "src/nested.txt"]
        assert result.truncated is False

    def test_derived_directories_preserve_input_tier_order(self) -> None:
        """Supplemental directory parents should not sort ahead of primary files."""
        result = _project_path_suggestions(["README.md", ".github/workflows/ci.yml"], max_paths=10)

        assert result.suggestions == [
            ProjectPathSuggestion(path="README.md", kind="file"),
            ProjectPathSuggestion(path=".github/", kind="directory"),
            ProjectPathSuggestion(path=".github/workflows/", kind="directory"),
            ProjectPathSuggestion(path=".github/workflows/ci.yml", kind="file"),
        ]
        assert result.truncated is False

    def test_dot_folder_contents_included(self, repo: Path) -> None:
        """Files under a non-ignored dot-folder show up in suggestions."""
        (repo / ".github" / "workflows").mkdir(parents=True)
        (repo / ".github" / "workflows" / "ci.yml").write_text("yaml\n")
        (repo / ".claude").mkdir()
        (repo / ".claude" / "skill.md").write_text("md\n")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert ".github/workflows/ci.yml" in paths
        assert ".claude/skill.md" in paths
        assert "README.md" in paths
        assert paths.index("README.md") < paths.index(".github/")

    def test_git_folder_never_included(self, repo: Path) -> None:
        """``.git/`` internals must never leak into @ suggestions."""
        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))
        assert not any(p.startswith(".git/") for p in paths), (
            f"unexpected .git/ leak: {[p for p in paths if p.startswith('.git/')]}"
        )

    def test_gitignored_dot_folder_excluded(self, repo: Path) -> None:
        """Dot-folders listed in ``.gitignore`` stay out of results."""
        (repo / ".gitignore").write_text(".secret/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore")
        (repo / ".secret").mkdir()
        (repo / ".secret" / "token.txt").write_text("nope\n")
        (repo / ".github").mkdir()
        (repo / ".github" / "ok.yml").write_text("ok\n")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert ".github/ok.yml" in paths
        assert not any(p.startswith(".secret/") for p in paths)

    def test_gitignored_visible_folder_contents_included(self, repo: Path) -> None:
        """Ignored visible docs remain referenceable from the ``@`` menu."""
        (repo / ".gitignore").write_text("drafts/\nnotes/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore docs")
        (repo / "drafts").mkdir()
        (repo / "drafts" / "proposal.md").write_text("draft\n")
        (repo / "notes").mkdir()
        (repo / "notes" / "private.md").write_text("notes\n")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert "drafts/proposal.md" in paths
        assert "notes/private.md" in paths
        assert paths.index("README.md") < paths.index("drafts/proposal.md")
        assert paths.index("README.md") < paths.index("drafts/")

    def test_gitignored_visible_media_and_docs_included(self, repo: Path) -> None:
        """Ignored visible images and documents remain referenceable."""
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")
        (repo / "drafts").mkdir()
        (repo / "drafts" / "notes.md").write_text("notes\n")
        (repo / "drafts" / "diagram.PNG").write_bytes(b"\x89PNG\r\n\x1a\n")
        (repo / "drafts" / "brief.pdf").write_bytes(b"%PDF-")
        (repo / "drafts" / "sheet.xlsx").write_bytes(b"PK\x03\x04")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert "drafts/notes.md" in paths
        assert "drafts/diagram.PNG" in paths
        assert "drafts/brief.pdf" in paths
        assert "drafts/sheet.xlsx" in paths

    def test_gitignored_visible_non_referenceable_extensions_excluded(self, repo: Path) -> None:
        """Ignored archives and compiled artifacts stay out of ``@`` suggestions."""
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")
        (repo / "drafts").mkdir()
        (repo / "drafts" / "notes.md").write_text("notes\n")
        (repo / "drafts" / "archive.zip").write_bytes(b"PK\x03\x04")
        (repo / "drafts" / "module.pyc").write_bytes(b"\0\0\0\0")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert "drafts/notes.md" in paths
        assert "drafts/archive.zip" not in paths
        assert "drafts/module.pyc" not in paths

    def test_gitignored_noise_dirs_excluded(self, repo: Path) -> None:
        """High-noise ignored directories stay out of ``@`` suggestions."""
        (repo / ".gitignore").write_text("node_modules/\ndist/\nbuild/\n__pycache__/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore noise")
        for rel in ("node_modules/pkg/index.js", "dist/app.js", "build/out.js", "__pycache__/mod.pyc"):
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        (repo / "drafts").mkdir()
        (repo / "drafts" / "keep.md").write_text("keep\n")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert "drafts/keep.md" in paths
        assert not any(p.startswith("node_modules/") for p in paths)
        assert not any(p.startswith("dist/") for p in paths)
        assert not any(p.startswith("build/") for p in paths)
        assert not any(p.startswith("__pycache__/") for p in paths)

    def test_tracked_file_under_now_ignored_folder_included_once(self, repo: Path) -> None:
        """Tracked files remain visible if their folder is ignored later."""
        (repo / "drafts").mkdir()
        (repo / "drafts" / "old.md").write_text("old\n")
        _git(repo, "add", "drafts/old.md")
        _git(repo, "commit", "-q", "-m", "track draft")
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")
        (repo / "drafts" / "new.md").write_text("new\n")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert paths.count("drafts/old.md") == 1
        assert paths.count("drafts/new.md") == 1

    def test_tracked_files_hidden_by_ripgrep_ignore_files_are_included(self, repo: Path) -> None:
        """Tracked files skipped by .ignore/.rgignore remain referenceable."""
        (repo / ".ignore").write_text("kept_by_git.txt\n")
        (repo / ".rgignore").write_text("rg_only.txt\n")
        (repo / "kept_by_git.txt").write_text("tracked\n")
        (repo / "rg_only.txt").write_text("tracked\n")
        _git(repo, "add", ".ignore", ".rgignore", "kept_by_git.txt", "rg_only.txt")
        _git(repo, "commit", "-q", "-m", "track rg ignored")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert "kept_by_git.txt" in paths
        assert "rg_only.txt" in paths

    def test_tracked_nested_dot_paths_included_when_rg_omits_hidden_paths(self, repo: Path) -> None:
        """Tracked dot files and hidden-dir descendants below visible dirs are not lost."""
        (repo / "src" / ".config").mkdir(parents=True)
        (repo / "src" / ".env.example").write_text("KEY=value\n")
        (repo / "src" / ".config" / "settings.toml").write_text("theme = 'dark'\n")
        _git(repo, "add", "src/.env.example", "src/.config/settings.toml")
        _git(repo, "commit", "-q", "-m", "track nested dot paths")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        assert paths.count("src/.env.example") == 1
        assert paths.count("src/.config/settings.toml") == 1

    def test_git_cached_probe_reports_truncation_when_rg_fills_file_budget(self, repo: Path) -> None:
        """Tracked ignored files are not silently omitted when rg exactly fills the file budget."""
        (repo / "drafts").mkdir()
        (repo / "drafts" / "old.md").write_text("old\n")
        _git(repo, "add", "drafts/old.md")
        _git(repo, "commit", "-q", "-m", "track draft")
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=1, suggestion_budget=10))

        assert scan.file_count == 1
        assert scan.truncated is True
        assert scan.source_truncations["git_cached"] is True

    def test_truncated_sources_are_keyed_and_merged_in_priority_order(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every concurrently gathered source keeps its own truncation key without stealing rows."""

        async def fake_probes(_root: str, *, file_budget: int):
            assert file_budget == 1
            return file_scanner_module._PathProbes(
                rg=file_scanner_module._PathSourceResult(["README.md"], truncated=True),
                git_cached=file_scanner_module._PathSourceResult([".gitignore"]),
                dot_entries=file_scanner_module._PathSourceResult([".github/ci.yml"]),
                ignored_visible=file_scanner_module._IgnoredVisibleProbe(["drafts/notes.md"]),
            )

        monkeypatch.setattr(file_scanner_module, "_run_probes", fake_probes)

        scan = asyncio.run(scan_project_paths(str(tmp_path), file_budget=1, suggestion_budget=10))

        # A truncated rg result at the budget leaves no capacity for the other tiers.
        assert _paths(scan.paths) == ["README.md"]
        assert scan.file_count == 1
        assert scan.source_truncations == {"rg": True, "dot_entries": True, "ignored_visible": True}

    def test_git_cached_probe_appends_tracked_ignored_files_with_remaining_budget(self, repo: Path) -> None:
        """The cached-git supplemental pass targets tracked files that are now ignored."""
        (repo / "drafts").mkdir()
        (repo / "drafts" / "old.md").write_text("old\n")
        _git(repo, "add", "drafts/old.md")
        _git(repo, "commit", "-q", "-m", "track draft")
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=3, suggestion_budget=10))
        paths = _paths(scan.paths)

        assert scan.file_count == 3
        assert "drafts/old.md" in paths
        assert scan.source_truncations.get("git_cached") is None

    def test_dedup_preserves_primary_order(self, repo: Path) -> None:
        """Dot entries already surfaced by git ls-files must not duplicate."""
        (repo / ".github").mkdir()
        (repo / ".github" / "ci.yml").write_text("x\n")
        _git(repo, "add", ".github/ci.yml")
        _git(repo, "commit", "-q", "-m", "ci")

        paths = _scan_paths(asyncio.run(scan_project_paths(str(repo))))

        # Appears exactly once regardless of which tier picked it up first.
        assert paths.count(".github/ci.yml") == 1

    def test_dot_entries_stream_past_duplicate_raw_lines(self, repo: Path) -> None:
        """Duplicate dot-entry rows do not consume remaining file budget."""
        (repo / ".a").mkdir()
        (repo / ".a" / "tracked.md").write_text("tracked\n")
        _git(repo, "add", ".a/tracked.md")
        _git(repo, "commit", "-q", "-m", "track dot")
        (repo / ".b").mkdir()
        (repo / ".b" / "new.md").write_text("new\n")

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=3, suggestion_budget=10))
        paths = _paths(scan.paths)

        assert scan.file_count == 3
        assert ".a/tracked.md" in paths
        assert ".b/new.md" in paths
        assert scan.source_truncations.get("dot_entries") is None

    def test_max_paths_honoured_with_dot_entries(self, repo: Path) -> None:
        """``max_paths`` caps the combined result, not just the primary pass."""
        for i in range(5):
            (repo / f"f{i}.txt").write_text("x\n")
        (repo / ".github").mkdir()
        for i in range(5):
            (repo / ".github" / f"g{i}.yml").write_text("x\n")

        scan = asyncio.run(scan_project_paths(str(repo), max_paths=3))
        paths = _paths(scan.paths)
        assert len(paths) == 3
        assert scan.truncated is True
        assert scan.source_truncations

    def test_max_paths_honoured_with_ignored_visible_files(self, repo: Path) -> None:
        """``max_paths`` caps the combined primary + supplemental ignored result."""
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")
        (repo / "drafts").mkdir()
        (repo / "drafts" / "hidden.md").write_text("hidden\n")

        scan = asyncio.run(scan_project_paths(str(repo), max_paths=1))
        paths = _paths(scan.paths)

        assert len(paths) == 1
        assert "drafts/hidden.md" not in paths
        assert scan.truncated is True

    def test_file_budget_counts_files_before_derived_directories(self, repo: Path) -> None:
        """The file budget is separate from the combined suggestion budget."""
        (repo / "src" / "app").mkdir(parents=True)
        (repo / "src" / "app" / "main.py").write_text("py\n")

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=2, suggestion_budget=10))
        paths = _paths(scan.paths)

        assert scan.file_count == 2
        assert len([path for path in scan.paths if path.kind == "file"]) == 2
        assert len(scan.paths) > scan.file_count
        assert "src/" in paths
        assert scan.file_budget == 2
        assert scan.suggestion_budget == 10

    def test_suggestion_budget_reports_truncation(self) -> None:
        """Directory generation is bounded separately from file discovery."""
        result = _project_path_suggestions(["src/chrys/app/tui/input_bar.py"], suggestion_budget=2)

        assert result.suggestions == [
            ProjectPathSuggestion(path="src/", kind="directory"),
            ProjectPathSuggestion(path="src/chrys/", kind="directory"),
        ]
        assert result.truncated is True

    def test_scan_result_reports_suggestion_budget_truncation(self, repo: Path) -> None:
        (repo / "src" / "chrys").mkdir(parents=True)
        (repo / "src" / "chrys" / "app.py").write_text("py\n")

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=10, suggestion_budget=2))

        assert scan.truncated is True
        assert scan.source_truncations == {"suggestions": True}
        assert scan.suggestion_count == 2

    def test_ignored_visible_source_cap_reports_truncation(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Independent supplemental source caps are surfaced in scan metadata."""
        (repo / ".gitignore").write_text("drafts/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore drafts")
        (repo / "drafts").mkdir()
        (repo / "drafts" / "one.md").write_text("one\n")
        (repo / "drafts" / "two.md").write_text("two\n")
        monkeypatch.setattr(
            "chrys.app.tui.widgets.chrome.file_scanner._MAX_SUPPLEMENTAL_IGNORED_FILES",
            1,
        )

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=100, suggestion_budget=100))

        assert scan.truncated is True
        assert scan.source_truncations["ignored_visible"] is True

    @pytest.mark.asyncio
    async def test_suggestion_generation_does_not_starve_event_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CPU-heavy suggestion construction runs off the event loop."""
        original_project_path_suggestions = file_scanner_module._project_path_suggestions
        loop_turned = threading.Event()

        async def fake_probes(_root: str, *, file_budget: int):
            assert file_budget == 20
            return file_scanner_module._PathProbes(
                rg=file_scanner_module._PathSourceResult([f"src/path_{index:03d}/file.py" for index in range(20)]),
                git_cached=file_scanner_module._PathSourceResult([]),
                dot_entries=file_scanner_module._PathSourceResult([]),
                ignored_visible=file_scanner_module._IgnoredVisibleProbe([]),
            )

        def slow_project_path_suggestions(
            paths: list[str],
            *,
            suggestion_budget: int | None = None,
            max_paths: int | None = None,
        ):
            loop_turned.wait(DEFAULT_WAIT_TIMEOUT)
            return original_project_path_suggestions(
                paths,
                suggestion_budget=suggestion_budget,
                max_paths=max_paths,
            )

        monkeypatch.setattr(file_scanner_module, "_run_probes", fake_probes)
        monkeypatch.setattr(file_scanner_module, "_project_path_suggestions", slow_project_path_suggestions)

        scan_task = asyncio.create_task(scan_project_paths(str(tmp_path), file_budget=20, suggestion_budget=100))
        ticks = 0
        # Count event-loop iterations (bare yields), not wall-clock slices, which
        # Windows floors to ~15.6ms. The work waits for the loop's fourth turn, so
        # it cannot finish before ``to_thread`` awaits it (``to_thread`` would then
        # return without yielding); work that blocks the loop waits out the gate
        # while the count stays low.
        while not scan_task.done():
            ticks += 1
            if ticks > 3:
                loop_turned.set()
            await asyncio.sleep(0)
        scan = await scan_task

        assert ticks > 3
        assert scan.file_count == 20

    @pytest.mark.asyncio
    async def test_dot_entry_fallback_walk_does_not_starve_event_loop(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Dot-entry fallback walking runs off the event loop when git is unavailable."""
        loop_turned = threading.Event()
        (tmp_path / ".cache").mkdir()

        async def fake_scan_rg_files(_root: str, _max_files: int):
            return file_scanner_module._PathSourceResult(["README.md"])

        def fail_dot_git_ls_files(*_args, **_kwargs):
            raise FileNotFoundError

        def slow_dot_walk(
            _root: str,
            _dot_entries: list[str],
            paths: list[str],
            seen: set[str],
            *,
            file_budget: int,
        ) -> bool:
            loop_turned.wait(DEFAULT_WAIT_TIMEOUT)
            path = ".cache/data.txt"
            if path not in seen and len(paths) < file_budget:
                seen.add(path)
                paths.append(path)
            return False

        monkeypatch.setattr(file_scanner_module, "_scan_rg_files", fake_scan_rg_files)
        monkeypatch.setattr(file_scanner_module, "managed_subprocess", fail_dot_git_ls_files)
        monkeypatch.setattr(file_scanner_module, "_append_unseen_dot_entries_from_walk", slow_dot_walk)

        scan_task = asyncio.create_task(scan_project_paths(str(tmp_path), file_budget=5, suggestion_budget=10))
        ticks = 0
        # Count event-loop iterations (bare yields), not wall-clock slices, which
        # Windows floors to ~15.6ms. The work waits for the loop's fourth turn, so
        # it cannot finish before ``to_thread`` awaits it (``to_thread`` would then
        # return without yielding); work that blocks the loop waits out the gate
        # while the count stays low.
        while not scan_task.done():
            ticks += 1
            if ticks > 3:
                loop_turned.set()
            await asyncio.sleep(0)
        scan = await scan_task

        assert ticks > 3
        assert ".cache/data.txt" in _paths(scan.paths)


class TestScanDotEntries:
    def test_empty_root_returns_empty(self, tmp_path: Path) -> None:
        result = asyncio.run(_scan_dot_entries(str(tmp_path), _top_level_dot_entries(str(tmp_path)), max_files=100))

        assert result.paths == []
        assert result.truncated is False

    def test_zero_budget_returns_empty(self, repo: Path) -> None:
        (repo / ".github").mkdir()
        (repo / ".github" / "x.yml").write_text("x\n")
        result = asyncio.run(_scan_dot_entries(str(repo), [".github"], max_files=0))

        assert result.paths == []
        assert result.truncated is False

    def test_dot_file_at_root(self, repo: Path) -> None:
        """Top-level dot-files (not just directories) are surfaced."""
        (repo / ".env.example").write_text("KEY=val\n")
        _git(repo, "add", ".env.example")
        _git(repo, "commit", "-q", "-m", "env")
        result = asyncio.run(_scan_dot_entries(str(repo), _top_level_dot_entries(str(repo)), max_files=100))

        assert ".env.example" in result.paths
        assert result.truncated is False

    def test_probe_reports_rows_beyond_the_budget(self, repo: Path) -> None:
        """A probe bounded at the file budget still reports the rows it did not read."""
        (repo / ".github").mkdir()
        for name in ("a.yml", "b.yml", "c.yml"):
            (repo / ".github" / name).write_text("x\n")
        _git(repo, "add", ".github")
        _git(repo, "commit", "-q", "-m", "ci")

        result = asyncio.run(_scan_dot_entries(str(repo), _top_level_dot_entries(str(repo)), max_files=2))

        assert result.paths == [".github/a.yml", ".github/b.yml"]
        assert result.truncated is True

    @pytest.mark.skipif(not get_platform().is_windows, reason="requires Win32 reserved-name semantics")
    def test_fallback_walk_skips_real_nul_file_in_dot_cache(self, tmp_path: Path) -> None:
        """The dot-entry fallback survives reserved files such as ``.bun/**/nul``."""
        cache = tmp_path / ".bun" / "install" / "cache" / "pkg"
        cache.mkdir(parents=True)
        reserved = _windows_extended_path(cache / "nul")
        reserved.write_text("created through the extended namespace\n")
        (cache / "keep.txt").write_text("keep\n")
        paths: list[str] = []
        try:
            truncated = _append_unseen_dot_entries_from_walk(
                str(tmp_path),
                [".bun"],
                paths,
                set(),
                file_budget=100,
            )
        finally:
            reserved.unlink(missing_ok=True)

        assert paths == [".bun/install/cache/pkg/keep.txt"]
        assert truncated is False

    def test_fallback_walk_prunes_unreferenceable_directories(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Rejected directory components are removed before ``os.walk`` descends."""
        (tmp_path / ".cache").mkdir()
        dot_root = str(tmp_path / ".cache")
        observed_dirnames: list[str] = []

        def fake_walk(_root: str):
            dirnames = ["reserved", "src"]
            yield dot_root, dirnames, ["reserved", "keep.txt"]
            observed_dirnames.extend(dirnames)
            if "reserved" in dirnames:
                raise AssertionError("reserved directory was not pruned")
            yield str(tmp_path / ".cache" / "src"), [], ["nested.txt"]

        monkeypatch.setattr(file_scanner_module.os, "walk", fake_walk)
        monkeypatch.setattr(
            file_scanner_module,
            "_is_referenceable_walk_name",
            lambda name: name != "reserved",
        )
        paths: list[str] = []

        truncated = _append_unseen_dot_entries_from_walk(
            str(tmp_path),
            [".cache"],
            paths,
            set(),
            file_budget=100,
        )

        assert observed_dirnames == ["src"]
        assert paths == [".cache/keep.txt", ".cache/src/nested.txt"]
        assert truncated is False


class TestParallelProbeScheduling:
    def test_rejected_rows_after_the_last_kept_path_still_truncate(self, repo: Path) -> None:
        """A budget cut at the final kept path must still report the rows git printed after it."""
        (repo / ".gitignore").write_text("gen/\n")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-q", "-m", "ignore gen")
        (repo / "gen").mkdir()
        (repo / "gen" / "a.md").write_text("x\n")
        (repo / "gen" / "b.zip").write_bytes(b"PK\x03\x04")

        probe = asyncio.run(_probe_ignored_visible_files(str(repo)))

        assert probe.paths == ["gen/a.md"]
        assert probe.lines_after_last == 1
        assert _select_ignored_visible_files(probe, 1).truncated is True
        assert _select_ignored_visible_files(probe, 2).truncated is False

    @pytest.mark.asyncio
    async def test_every_probe_starts_before_any_result_is_awaited(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """All four discovery sources are in flight at once, so a cold scan costs the longest probe."""
        (tmp_path / ".cache").mkdir()
        started: set[str] = set()
        all_started = asyncio.Event()

        def gate(name: str, result: object):
            async def probe(*_args: object, **_kwargs: object) -> object:
                started.add(name)
                if len(started) == 4:
                    all_started.set()
                await asyncio.wait_for(all_started.wait(), DEFAULT_WAIT_TIMEOUT)
                return result

            return probe

        monkeypatch.setattr(
            file_scanner_module, "_scan_rg_files", gate("rg", file_scanner_module._PathSourceResult(["a.py"]))
        )
        monkeypatch.setattr(
            file_scanner_module,
            "_scan_git_ls_files",
            gate("git_cached", file_scanner_module._PathSourceResult([])),
        )
        monkeypatch.setattr(
            file_scanner_module,
            "_probe_ignored_visible_files",
            gate("ignored_visible", file_scanner_module._IgnoredVisibleProbe([])),
        )
        monkeypatch.setattr(
            file_scanner_module,
            "_scan_dot_entries",
            gate("dot_entries", file_scanner_module._PathSourceResult([])),
        )

        scan = await asyncio.wait_for(
            scan_project_paths(str(tmp_path), file_budget=10, suggestion_budget=10),
            DEFAULT_WAIT_TIMEOUT,
        )

        assert started == {"rg", "git_cached", "ignored_visible", "dot_entries"}
        assert _paths(scan.paths) == ["a.py"]

    def test_source_unavailable_still_returns_other_sources(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A missing git does not drop the files ripgrep already proved exist."""
        (repo / "src" / "app").mkdir(parents=True)
        (repo / "src" / "app" / "main.py").write_text("py\n")

        async def git_unavailable(_root: str, *_args: object, **_kwargs: object):
            return file_scanner_module._PathSourceResult([])

        async def no_ignored_visible(_root: str):
            return file_scanner_module._IgnoredVisibleProbe([])

        monkeypatch.setattr(file_scanner_module, "_scan_git_ls_files", git_unavailable)
        monkeypatch.setattr(file_scanner_module, "_probe_ignored_visible_files", no_ignored_visible)

        scan = asyncio.run(scan_project_paths(str(repo), file_budget=100, suggestion_budget=100))
        paths = _paths(scan.paths)

        assert "src/app/main.py" in paths
        assert "src/" in paths
        assert scan.source_truncations == {}

    def test_pinned_path_set_for_mixed_source_fixture(self, repo: Path) -> None:
        """Characterization pin: the visible set and its order must match the pre-parallel scanner."""
        for directory in ("src/app", ".github/workflows", ".claude", "drafts", "node_modules/pkg", "src/.config"):
            (repo / directory).mkdir(parents=True, exist_ok=True)
        tracked = {
            "README.md": "x",
            "src/app/main.py": "x",
            "src/app/util.py": "x",
            "src/.env.example": "x",
            "src/.config/settings.toml": "x",
            ".github/workflows/ci.yml": "x",
            ".claude/skill.md": "x",
            "drafts/tracked.md": "x",
            "docs/guide.md": "x",
        }
        for rel, body in tracked.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body)
        (repo / ".gitignore").write_text("drafts/\nnode_modules/\ndocs/local.md\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "seed")
        (repo / "drafts" / "local.md").write_text("x\n")
        (repo / "docs" / "local.md").write_text("x\n")
        (repo / "node_modules" / "pkg" / "index.js").write_text("x\n")
        (repo / "drafts" / "archive.zip").write_bytes(b"PK\x03\x04")

        scan = asyncio.run(scan_project_paths(str(repo)))

        assert [(entry.path, entry.kind) for entry in scan.paths] == [
            ("README.md", "file"),
            ("docs/", "directory"),
            ("docs/guide.md", "file"),
            ("src/", "directory"),
            ("src/app/", "directory"),
            ("src/app/main.py", "file"),
            ("src/app/util.py", "file"),
            (".claude/", "directory"),
            (".claude/skill.md", "file"),
            (".github/", "directory"),
            (".github/workflows/", "directory"),
            (".github/workflows/ci.yml", "file"),
            (".gitignore", "file"),
            ("src/.config/", "directory"),
            ("src/.config/settings.toml", "file"),
            ("src/.env.example", "file"),
            ("docs/local.md", "file"),
            ("drafts/", "directory"),
            ("drafts/local.md", "file"),
            ("drafts/tracked.md", "file"),
        ]
        assert scan.file_count == 12
        assert scan.ordering == "path-sorted"
        assert scan.source_truncations == {}


class TestDerivedDirectoryReuse:
    def test_directory_rows_are_derived_once_per_chained_prefix(self) -> None:
        """A path-sorted input reuses the previous directory chain instead of re-joining it."""
        paths = ["src/a.py", "src/b.py", "src/pkg/c.py", "src/pkg/d.py", "zzz/e.py"]

        result = _project_path_suggestions(paths, suggestion_budget=1000)

        assert [(entry.path, entry.kind) for entry in result.suggestions] == [
            ("src/", "directory"),
            ("src/a.py", "file"),
            ("src/b.py", "file"),
            ("src/pkg/", "directory"),
            ("src/pkg/c.py", "file"),
            ("src/pkg/d.py", "file"),
            ("zzz/", "directory"),
            ("zzz/e.py", "file"),
        ]
        assert result.truncated is False

    def test_unsorted_input_still_emits_each_ancestor_before_its_file(self) -> None:
        """Input tier order wins over path sorting: later tiers keep their own directory rows."""
        paths = ["README.md", ".github/workflows/ci.yml", "src/app/main.py", "src/app/util.py", ".github/ISSUE.md"]

        result = _project_path_suggestions(paths, suggestion_budget=1000)

        assert [entry.path for entry in result.suggestions] == [
            "README.md",
            ".github/",
            ".github/workflows/",
            ".github/workflows/ci.yml",
            "src/",
            "src/app/",
            "src/app/main.py",
            "src/app/util.py",
            ".github/ISSUE.md",
        ]

    def test_budget_cut_before_a_reused_prefix_reports_truncation(self) -> None:
        """The budget still cuts on the row that would have been appended, reused prefix or not."""
        paths = ["a/one.py", "a/two.py", "a/three.py"]

        result = _project_path_suggestions(paths, suggestion_budget=3)

        assert [entry.path for entry in result.suggestions] == ["a/", "a/one.py", "a/two.py"]
        assert result.truncated is True


class _FakeStdout:
    """Minimal ``asyncio.StreamReader`` stand-in that answers ``read`` in fixed blocks."""

    def __init__(self, payload: bytes, block: int) -> None:
        self._payload = payload
        self._offset = 0
        self._block = block

    async def read(self, n: int = -1) -> bytes:
        size = self._block if n < 0 else min(n, self._block)
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def _read_lines(payload: bytes, block: int) -> list[bytes]:
    async def collect() -> list[bytes]:
        reader = _ChunkedLineReader(_FakeStdout(payload, block), chunk_size=8)
        lines: list[bytes] = []
        while line := await reader.line():
            lines.append(line)
        return lines

    return asyncio.run(collect())


class TestChunkedLineReader:
    """The block reader must hand back exactly the lines ``StreamReader.readline`` returns."""

    @pytest.mark.parametrize("block", [1, 2, 3, 4, 5, 7, 8, 64, 4096])
    def test_matches_readline_across_block_boundaries(self, block: int) -> None:
        payload = b"a.py\nsrc/b.py\r\nlonger/name/here.py\n"

        assert _read_lines(payload, block) == [b"a.py\n", b"src/b.py\r\n", b"longer/name/here.py\n"]

    def test_trailing_fragment_at_eof_is_a_line(self) -> None:
        """Output without a final newline still yields its last path."""
        assert _read_lines(b"a.py\nb.py", 3) == [b"a.py\n", b"b.py"]

    def test_empty_stream_returns_no_lines(self) -> None:
        assert _read_lines(b"", 4) == []

    def test_blank_line_is_preserved(self) -> None:
        assert _read_lines(b"a\n\nb\n", 2) == [b"a\n", b"\n", b"b\n"]

    def test_line_ending_exactly_on_chunk_boundary(self) -> None:
        payload = b"12345678\nnext\n"

        assert _read_lines(payload, 9) == [b"12345678\n", b"next\n"]
