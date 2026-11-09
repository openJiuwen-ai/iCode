# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unreadable filesystem evidence must not become destructive mutation endpoints."""

from __future__ import annotations

import errno
import ntpath
import os
from collections.abc import Callable
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.service.mutations.git_state import canonical_path
from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking
from chrys.service.mutations.scanner import WorkspaceScanner
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource, RollbackExclusionReason, SnapshotSkipReason
from chrys.service.mutations.workspace_changes import (
    BaselineMode,
    DegradedReason,
    WorkspaceChangeTracker,
    _operation_from_summary,
)


def _block_lstat(monkeypatch: pytest.MonkeyPatch, target: Path, error_number: int) -> list[str]:
    original = os.lstat
    target_key = os.path.normcase(str(target))
    blocked: list[str] = []

    def lstat(path, *, dir_fd=None):
        observed = os.fsdecode(path)
        if os.path.normcase(observed) == target_key:
            blocked.append(observed)
            raise OSError(error_number, "injected stat failure", str(target))
        return original(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "lstat", create_autospec(original, side_effect=lstat))
    return blocked


def _block_read(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original = Path.read_bytes

    def read(path: Path) -> bytes:
        if path == target:
            raise PermissionError(errno.EACCES, "injected read failure", str(target))
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", create_autospec(original, side_effect=read))


def _block_scandir(monkeypatch: pytest.MonkeyPatch, target: Path) -> None:
    original = os.scandir
    target_key = os.path.normcase(str(target))

    def scandir(path="."):
        if os.path.normcase(os.fsdecode(path)) == target_key:
            raise PermissionError(errno.EACCES, "injected directory failure", str(target))
        return original(path)

    monkeypatch.setattr(os, "scandir", create_autospec(original, side_effect=scandir))


@pytest.mark.parametrize("operation", ["lstat", "scandir"])
def test_fault_injection_matches_windows_case_and_separators(monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    target = Path(r"C:\Users\RunnerAdmin\Temp\WorkspaceCase\README.md")
    observed = r"c:/users/runneradmin/temp/workspacecase/README.md"
    # Exercise Windows spelling changes on every host without accessing a Windows drive.
    with monkeypatch.context() as patch:
        patch.setattr(os.path, "normcase", create_autospec(os.path.normcase, side_effect=ntpath.normcase))
        if operation == "lstat":
            _block_lstat(patch, target, errno.EIO)
            with pytest.raises(OSError, match="injected stat failure"):
                os.lstat(observed)
        else:
            _block_scandir(patch, target)
            with pytest.raises(PermissionError, match="injected directory failure"):
                os.scandir(observed)


@pytest.mark.parametrize("failure", ["read", "stat"])
def test_unreadable_before_survives_serialization_and_is_excluded_from_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    target = tmp_path / "existing.txt"
    target.write_bytes(b"user content")
    store = SnapshotStore(tmp_path / "session")
    tracker = MutationTracker(store)
    tracker.start_turn(1)
    with monkeypatch.context() as patch:
        if failure == "read":
            _block_read(patch, target)
        else:
            _block_lstat(patch, target, errno.EACCES)
        mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
    assert mutation is not None
    target.write_bytes(b"agent content")
    tracker.record_after(mutation)
    tracker = MutationTracker.deserialize(tracker.serialize(), store)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.before_skip is SnapshotSkipReason.UNREADABLE
    assert summary.content_unavailable
    assert _operation_from_summary(summary) is MutationOp.MODIFY
    turn = tracker.get_turn_mutations(1)
    assert turn is not None and turn.detection_truncated
    plan = tracker.get_rollback_plan()
    assert plan.entries == []
    assert plan.exclusions == [(str(target), RollbackExclusionReason.UNRESTORABLE)]
    tracker.rollback_turns({1})
    assert target.read_bytes() == b"agent content"


def test_unreadable_after_is_not_a_deletion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "existing.txt"
    target.write_bytes(b"user content")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
    assert mutation is not None
    target.write_bytes(b"agent content")
    with monkeypatch.context() as patch:
        _block_read(patch, target)
        tracker.record_after(mutation)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.after_skip is SnapshotSkipReason.UNREADABLE
    assert _operation_from_summary(summary) is MutationOp.MODIFY
    # The known before snapshot remains a valid rollback target.
    tracker.rollback_turns({1})
    assert target.read_bytes() == b"user content"


@pytest.mark.parametrize("error_number", [errno.ENOENT, errno.ENOTDIR, errno.EACCES, errno.EIO])
def test_snapshot_stat_distinguishes_missing_from_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"content")
    _block_lstat(monkeypatch, target, error_number)
    store = SnapshotStore(tmp_path / "session")
    snapshot = store.save(str(target), 1)
    blob = store.save_blob(str(target))
    absent = error_number in (errno.ENOENT, errno.ENOTDIR)
    assert snapshot.existed is not absent
    assert snapshot.restorable is absent
    assert snapshot.skip_reason is (None if absent else SnapshotSkipReason.UNREADABLE)
    assert blob.skip_reason is snapshot.skip_reason


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
def test_unreadable_link_is_not_a_nonexistent_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "link"
    target.symlink_to("missing-target")
    original = os.readlink

    def readlink(path, *, dir_fd=None):
        if os.fsdecode(path) == str(target):
            raise OSError(errno.EIO, "injected readlink failure")
        return original(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "readlink", create_autospec(original, side_effect=readlink))
    store = SnapshotStore(tmp_path / "session")
    snapshot = store.save(str(target), 1)
    assert snapshot.existed and not snapshot.restorable
    assert snapshot.skip_reason is SnapshotSkipReason.UNREADABLE
    assert store.save_blob(str(target)).skip_reason is SnapshotSkipReason.UNREADABLE


@pytest.mark.parametrize("targeted", [False, True])
@pytest.mark.parametrize("failure", ["file_stat", "directory_stat", "directory_listing"])
def test_scans_reject_unreadable_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, targeted: bool, failure: str
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    target = root / "file.txt"
    target.write_bytes(b"content")
    if failure == "directory_listing":
        _block_scandir(monkeypatch, root)
    elif failure == "directory_stat":
        if not targeted:
            pytest.skip("Full walks discover directory errors through scandir")
        _block_lstat(monkeypatch, root, errno.EACCES)
    else:
        _block_lstat(monkeypatch, target, errno.EIO)
    scanner = WorkspaceScanner(str(root))
    with pytest.raises(OSError):
        if targeted:
            scanner.scan_paths([str(root)])
        else:
            scanner.scan()


@pytest.mark.parametrize("phase", ["before", "after"])
@pytest.mark.parametrize("failure", ["file_stat", "directory_listing"])
async def test_incomplete_shell_scans_cannot_override_healthy_git_or_delete_existing_files(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: str,
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    target = repo / "README.md"
    content = target.read_bytes()
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    with monkeypatch.context() as patch:
        if phase == "before":
            if failure == "file_stat":
                _block_lstat(patch, target, errno.EACCES)
            else:
                _block_scandir(patch, repo)
        context = await prepare_mutation_tracking(
            tracker, "shell", {"command": "chmod +x .", "workdir": str(repo)}, "noop", True, str(repo)
        )
    with monkeypatch.context() as patch:
        if phase == "after":
            if failure == "file_stat":
                _block_lstat(patch, target, errno.EACCES)
            else:
                _block_scandir(patch, repo)
        await finalize_mutation_tracking(tracker, context, "noop")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None and turn.detection_truncated
    assert tracker.get_turn_file_summary(1) == {}
    tracker.rollback_turns({1})
    assert target.read_bytes() == content


@pytest.mark.parametrize("phase", ["before", "after"])
async def test_git_stat_failure_does_not_turn_existing_dirty_file_into_delete(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    target = repo / "README.md"
    target.write_bytes(b"preexisting dirty content")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    with monkeypatch.context() as patch:
        if phase == "before":
            _block_lstat(patch, target, errno.EIO)
        context = await prepare_mutation_tracking(
            tracker, "shell", {"command": "opaque-launcher", "workdir": str(repo)}, "noop", True, str(repo)
        )
    with monkeypatch.context() as patch:
        if phase == "after":
            _block_lstat(patch, target, errno.EIO)
        await finalize_mutation_tracking(tracker, context, "noop")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None and turn.detection_truncated
    assert turn.mutations == []
    tracker.rollback_turns({1})
    assert target.read_bytes() == b"preexisting dirty content"


@pytest.mark.parametrize("git_mode", [False, True])
def test_unreadable_workspace_capture_is_degraded_instead_of_recording_absence(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch, git_mode: bool
) -> None:
    root = tmp_path / "WorkspaceCase"
    if git_mode:
        root = git_repo_factory(root)
    else:
        root.mkdir()
    target = root / "README.md"
    target.write_bytes(b"dirty content")
    tracker = WorkspaceChangeTracker()
    tracker.retarget_roots(Workspace.from_cwd(str(root)))
    root_key = canonical_path(str(root))
    healthy = tracker.capture_baseline(1)
    assert healthy.roots[root_key].mode is (BaselineMode.GIT if git_mode else BaselineMode.SCAN)
    with monkeypatch.context() as patch:
        blocked = _block_lstat(patch, target, errno.EIO)
        baseline = tracker.capture_baseline(2)
    assert blocked, "The lstat failure must reach the canonical workspace path"
    assert not baseline.roots
    assert set(baseline.degraded) == {root_key}
    assert baseline.degraded[root_key].reason is DegradedReason.CAPTURE_FAILED
    recovered = tracker.capture_baseline(3)
    assert not recovered.degraded
    assert recovered.roots[root_key].files == healthy.roots[root_key].files


@pytest.mark.parametrize("calibrated", [False, True])
@pytest.mark.parametrize("after", ["unchanged", "modified", "missing", "unreadable"])
def test_delete_candidates_always_observe_the_after_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calibrated: bool, after: str
) -> None:
    target = tmp_path / "file.txt"
    before = b"before"
    target.write_bytes(before)
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    tracker.pre_snapshot([str(target)])
    if after == "modified":
        target.write_bytes(b"after")
    elif after == "missing":
        target.unlink()
    if after == "unreadable":
        _block_read(monkeypatch, target)
    if calibrated:
        row = tracker.record_calibrated(str(target), MutationOp.DELETE, before)
    else:
        row = tracker.record(str(target), MutationOp.DELETE, MutationSource.SHELL, "shell")
    assert row is not None
    assert row.operation is (MutationOp.DELETE if after == "missing" else MutationOp.MODIFY)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.is_net_zero is (after == "unchanged")
    assert summary.after_exists is (after != "missing")
    assert summary.content_unavailable is (after == "unreadable")


async def test_directory_scan_snapshots_child_before_actual_edit(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    target = repo / "file.txt"
    target.write_bytes(b"before")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": "chmod +x .", "workdir": str(repo)}, "shell", True, str(repo)
    )
    target.write_bytes(b"changed inside directory")
    await finalize_mutation_tracking(tracker, context, "shell")
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.before == SnapshotStore.content_hash(b"before")
    assert summary.after == SnapshotStore.content_hash(b"changed inside directory")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None and not turn.detection_truncated
    tracker.rollback_turns({1})
    assert target.read_bytes() == b"before"


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
def test_targeted_scan_preserves_explicit_directory_alias(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "file.txt").write_bytes(b"content")
    alias = tmp_path / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    file_link = tmp_path / "file-link"
    file_link.symlink_to(directory / "file.txt")
    scanner = WorkspaceScanner(str(tmp_path))
    assert set(scanner.scan_paths([str(alias), str(file_link)])) == {str(alias / "file.txt")}


def test_failed_after_blob_persistence_does_not_publish_a_half_calibrated_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_bytes(b"before")
    store = SnapshotStore(tmp_path / "session")
    tracker = MutationTracker(store)
    tracker.start_turn(1)
    tracker.pre_snapshot([str(target)])
    monkeypatch.setattr(
        store, "save_blob", create_autospec(store.save_blob, side_effect=OSError(errno.ENOSPC, "blob storage full"))
    )
    with pytest.raises(OSError, match="blob storage full"):
        tracker.record_calibrated(str(target), MutationOp.DELETE, b"before")
    assert tracker.get_turn_file_summary(1) == {}
    assert tracker.get_rollback_plan().entries == []
