# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Git observation failures must not create destructive phantom mutation records."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.platform import get_platform
from chrys.service.mutations import git_calibrator, git_state
from chrys.service.mutations.git_calibrator import GitDiffCalibrator
from chrys.service.mutations.git_state import GitDeltaResult
from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, capture_output=True, check=True).stdout


@pytest.mark.parametrize("failure_at", [1, 2])
async def test_failed_head_probe_does_not_pollute_diff_or_delete_tracked_files_on_rollback(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch, failure_at: int
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    for name, data in {
        "AGENTS.md": b"committed instructions",
        "pom.xml": b"<project/>",
        "scanner.exe": b"\x00\xffbinary",
    }.items():
        (repo / name).write_bytes(data)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tracked project")
    head = _git(repo, "rev-parse", "HEAD")
    agents = repo / "AGENTS.md"
    agents.write_bytes(b"pre-existing dirty instructions")
    before = {path: path.read_bytes() for path in repo.iterdir() if path.is_file()}
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    original = git_state._run_git
    head_reads = 0

    def run(root: str, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes] | None:
        nonlocal head_reads
        if args[:2] == ["rev-parse", "--verify"]:
            head_reads += 1
            if head_reads == failure_at:
                return subprocess.CompletedProcess(args, 128, b"", b"fatal: cannot read HEAD")
        return original(root, args, timeout=timeout)

    monkeypatch.setattr(git_state, "_run_git", create_autospec(original, side_effect=run))
    # Exercise the production observation/recording boundary around an opaque
    # shell command with no filesystem effects. Git calls other than the single
    # injected HEAD failure are real. The repository's HEAD remains intact.
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": "opaque-launcher", "workdir": str(repo)}, "launcher", True, str(repo)
    )
    result = await finalize_mutation_tracking(tracker, context, "launcher")
    assert head_reads >= failure_at
    assert _git(repo, "rev-parse", "HEAD") == head
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert turn.detection_truncated is True
    assert turn.mutations == []
    assert not result.shell_snapshots
    assert tracker.get_turn_file_summary(1) == {}

    # A later real edit still owns its normal before/after endpoints. Rolling
    # the turn back must restore only that edit and leave every tracked file.
    edit = tracker.record(str(agents), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit-agents")
    assert edit is not None
    agents.write_bytes(b"updated by edit_file")
    tracker.record_after(edit)
    summary = tracker.get_turn_file_summary(1)
    assert set(summary) == {str(agents)}
    assert summary[str(agents)].before == SnapshotStore.content_hash(before[agents])
    assert summary[str(agents)].after == SnapshotStore.content_hash(b"updated by edit_file")
    tracker.rollback_turns({1})
    assert {path: path.read_bytes() for path in before} == before


def test_unknown_old_head_never_enumerates_an_unborn_delta(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    monkeypatch.setattr(
        git_calibrator,
        "read_git_head_oid",
        create_autospec(git_calibrator.read_git_head_oid, side_effect=[OSError("unreadable HEAD"), head]),
    )
    delta = create_autospec(git_calibrator.read_git_head_delta, return_value=GitDeltaResult(()))
    monkeypatch.setattr(git_calibrator, "read_git_head_delta", delta)
    calibrator = GitDiffCalibrator(str(repo))
    calibrator.capture_before()
    assert calibrator.detect_implicit_changes(set()) == []
    assert calibrator.detection_truncated is True
    delta.assert_not_called()


def test_missing_before_capture_is_not_a_confirmed_unborn_head(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path]
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    calibrator = GitDiffCalibrator(str(repo))
    assert calibrator.detect_implicit_changes(set()) == []
    assert calibrator.detection_truncated is True


async def test_real_initial_commit_keeps_new_file_detection_and_rollback(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path]
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    _git(repo, "checkout", "--orphan", "unborn")
    _git(repo, "rm", "-rf", "--", ".")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": "opaque-generator", "workdir": str(repo)}, "initial-commit", True, str(repo)
    )
    created = repo / "created.txt"
    created.write_bytes(b"created and committed during the command")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "first commit")
    await finalize_mutation_tracking(tracker, context, "initial-commit")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert turn.detection_truncated is False
    assert [(row.path, row.operation, row.before_hash) for row in turn.mutations] == [
        (str(created), MutationOp.CREATE, None)
    ]
    assert turn.mutations[0].after_hash == SnapshotStore.content_hash(created.read_bytes())
    tracker.rollback_turns({1})
    assert not created.exists()


async def test_failed_old_tree_probe_cannot_turn_a_tracked_edit_into_create(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": "opaque-generator", "workdir": str(repo)}, "generator", True, str(repo)
    )
    target = repo / "README.md"
    target.write_bytes(b"changed during command")
    original = git_state._run_git
    injected = False

    def run(root: str, args: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes] | None:
        nonlocal injected
        if args[0] in {"cat-file", "ls-tree"}:
            injected = True
            return subprocess.CompletedProcess(args, 128, b"", b"fatal: cannot read tree")
        return original(root, args, timeout=timeout)

    monkeypatch.setattr(git_state, "_run_git", create_autospec(original, side_effect=run))
    await finalize_mutation_tracking(tracker, context, "generator")
    assert injected
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert turn.detection_truncated is True
    assert turn.mutations == []
    tracker.rollback_turns({1})
    assert target.read_bytes() == b"changed during command"


@pytest.mark.parametrize("head_move", ["symbolic-ref", "checkout-orphan"])
@pytest.mark.parametrize("file_action", ["unchanged", "modified", "deleted"])
async def test_head_tree_deletion_uses_actual_worktree_endpoint(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], head_move: str, file_action: str
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    target = repo / "README.md"
    before = target.read_bytes()
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": "opaque-git-wrapper", "workdir": str(repo)}, "orphan", True, str(repo)
    )
    if head_move == "symbolic-ref":
        _git(repo, "symbolic-ref", "HEAD", "refs/heads/unborn")
    else:
        _git(repo, "checkout", "--orphan", "unborn")
    if file_action == "modified":
        target.write_bytes(b"changed after HEAD move")
    elif file_action == "deleted":
        target.unlink()
    await finalize_mutation_tracking(tracker, context, "orphan")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert len(turn.mutations) == 1
    row = turn.mutations[0]
    expected_operation = MutationOp.DELETE if file_action == "deleted" else MutationOp.MODIFY
    assert row.operation is expected_operation
    assert row.before_hash == SnapshotStore.content_hash(before)
    summary = tracker.get_turn_file_summary(1)[str(target)]
    assert summary.is_net_zero is (file_action == "unchanged")
    assert summary.after_exists is (file_action != "deleted")
    tracker.rollback_turns({1})
    assert target.read_bytes() == before


@pytest.mark.parametrize(
    "name",
    [
        "中文.txt",
        "has space.txt",
        "[literal].txt",
        pytest.param("tab\tline\n.txt", marks=pytest.mark.skipif(get_platform().is_windows, reason="Windows filename")),
    ],
)
def test_git_nul_names_preserve_dirty_and_new_paths(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], name: str
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    target = repo / name
    target.write_bytes(b"committed")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "tracked filename")
    calibrator = GitDiffCalibrator(str(repo))
    calibrator.capture_before()
    target.write_bytes(b"changed")
    changes = calibrator.detect_implicit_changes(set())
    assert [(row.path, row.operation, row.before_data) for row in changes] == [
        (str(target), MutationOp.MODIFY, b"committed")
    ]
    assert set(calibrator.capture_before()) == {str(target)}
    new_path = repo / f"new-{name}"
    new_path.write_bytes(b"new")
    changes = calibrator.detect_implicit_changes(set())
    assert [(row.path, row.operation) for row in changes] == [(str(new_path), MutationOp.CREATE)]
    assert not calibrator.detection_truncated
