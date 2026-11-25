# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for TUI git branch discovery helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from chrys.app.tui.util.git_branch import (
    GitBranchMonitor,
    GitBranchSnapshot,
    _event_matches_watch_target,
    _watch_target_for_snapshot,
    format_workspace_subtitle,
    read_git_branch_snapshot,
)


class _Event:
    def __init__(self, src_path: Path) -> None:
        self.src_path = str(src_path)
        self.dest_path = ""


def _write_head(repo: Path, value: str) -> None:
    git_dir = repo / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text(value, encoding="utf-8")


def test_read_git_branch_snapshot_reads_normal_repo_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_head(repo, "ref: refs/heads/main\n")

    snapshot = read_git_branch_snapshot(str(repo))

    assert snapshot.branch == "main"
    assert snapshot.head_path == repo / ".git" / "HEAD"
    assert format_workspace_subtitle(str(repo), snapshot.branch, width=1000).plain == f"{repo} (main)"


def test_read_git_branch_snapshot_finds_parent_repo_from_nested_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    _write_head(repo, "ref: refs/heads/feat/abc\n")

    snapshot = read_git_branch_snapshot(str(nested))

    assert snapshot.branch == "feat/abc"
    assert snapshot.repo_root == repo


def test_read_git_branch_snapshot_reads_gitdir_file(tmp_path: Path) -> None:
    repo = tmp_path / "worktree"
    git_dir = tmp_path / "real-git-dir"
    repo.mkdir()
    git_dir.mkdir()
    (repo / ".git").write_text("gitdir: ../real-git-dir\n", encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/worktree-branch\n", encoding="utf-8")

    snapshot = read_git_branch_snapshot(str(repo))

    assert snapshot.branch == "worktree-branch"
    assert snapshot.head_path == git_dir / "HEAD"


def test_read_git_branch_snapshot_handles_detached_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_head(repo, "0123456789abcdef0123456789abcdef01234567\n")

    snapshot = read_git_branch_snapshot(str(repo))

    assert snapshot.branch == "detached 0123456"


def test_read_git_branch_snapshot_handles_missing_head(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)

    snapshot = read_git_branch_snapshot(str(repo))

    assert snapshot.branch == ""
    assert snapshot.head_path == repo / ".git" / "HEAD"


def test_read_git_branch_snapshot_handles_non_git_cwd(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    snapshot = read_git_branch_snapshot(str(workspace))

    assert snapshot.branch == ""
    assert snapshot.head_path is None
    assert format_workspace_subtitle(str(workspace), snapshot.branch, width=1000).plain == str(workspace)


def test_non_git_watch_target_catches_git_init_marker_creation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    snapshot = GitBranchSnapshot(cwd=str(workspace))

    target = _watch_target_for_snapshot(snapshot)

    assert target is not None
    assert target.watch_dirs == (workspace,)
    assert _event_matches_watch_target(_Event(workspace / ".git"), target)


def test_git_watch_target_catches_git_dir_deletion_for_recreate(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = repo / ".git" / "HEAD"
    snapshot = GitBranchSnapshot(cwd=str(repo), branch="main", head_path=head, git_marker_path=repo / ".git")

    target = _watch_target_for_snapshot(snapshot)

    assert target is not None
    assert repo in target.watch_dirs
    assert _event_matches_watch_target(_Event(repo / ".git"), target)


def test_pending_gitdir_watch_target_catches_late_gitdir_parent_creation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    external_git_parent = tmp_path / "external"
    repo.mkdir()
    external_git_parent.mkdir()
    pending_gitdir = external_git_parent / "worktrees" / "repo"
    snapshot = GitBranchSnapshot(
        cwd=str(repo),
        branch="",
        head_path=pending_gitdir / "HEAD",
        git_marker_path=repo / ".git",
    )

    target = _watch_target_for_snapshot(snapshot)

    assert target is not None
    assert external_git_parent in target.watch_dirs
    assert _event_matches_watch_target(_Event(external_git_parent / "worktrees"), target)


def test_git_branch_monitor_state_machine_rearms_same_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_head(repo, "ref: refs/heads/main\n")
    starts: list[tuple[Path, ...]] = []
    stops: list[object] = []

    def start_observer(self: GitBranchMonitor, target: Any) -> None:
        starts.append(target.watch_dirs)
        self._observer = object()

    def stop_observer(self: GitBranchMonitor) -> None:
        if self._observer is not None:
            stops.append(self._observer)
        self._observer = None

    monkeypatch.setattr(GitBranchMonitor, "_start_observer", start_observer)
    monkeypatch.setattr(GitBranchMonitor, "_stop_observer", stop_observer)
    monitor = GitBranchMonitor(lambda: None)

    snapshot = monitor.start(str(repo))

    assert snapshot.branch == "main"
    assert monitor.active is True
    assert monitor.watching is True
    assert len(starts) == 1

    monitor.configure(str(repo))

    assert len(starts) == 1

    monitor._observer = None
    monitor.configure(str(repo))

    assert monitor.watching is True
    assert len(starts) == 2

    _write_head(repo, "ref: refs/heads/next\n")
    snapshot = monitor.refresh()

    assert snapshot.branch == "next"
    assert len(starts) == 2

    monitor.stop()

    assert monitor.active is False
    assert monitor.watching is False
    assert len(stops) == 1


def test_git_branch_monitor_exposes_poll_fallback_when_observer_does_not_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _self, _target: None)
    monitor = GitBranchMonitor(lambda: None)

    snapshot = monitor.start(str(workspace))

    assert snapshot.branch == ""
    assert monitor.active is True
    assert monitor.watching is False

    monitor.stop()

    assert monitor.active is False
