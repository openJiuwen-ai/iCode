# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shell backups must preserve the last copy without unbounded child reads."""

from __future__ import annotations

import errno
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.platform import get_platform
from chrys.service.mutations import pipeline
from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking
from chrys.service.mutations.scanner import WorkspaceScanner
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import RollbackExclusionReason, SnapshotSkipReason


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, stdin=subprocess.DEVNULL, capture_output=True, check=True)


@pytest.mark.parametrize("payload", [b"oversized text\n" * 30, b"\x89PNG\r\n\x1a\n"], ids=["oversized", "binary"])
async def test_unreadable_moved_source_keeps_destination_out_of_rollback(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    source = repo / "source.dat"
    destination = repo / "destination.dat"
    source.write_bytes(payload)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "source")
    store = SnapshotStore(tmp_path / "session", policy=SnapshotPolicy(max_file_bytes=64))
    tracker = MutationTracker(store)
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": "opaque-launcher", "workdir": str(repo)}, "move", True, str(repo)
    )
    source.rename(destination)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "move source")
    original = os.lstat

    def lstat(path, *, dir_fd=None):
        if os.path.normcase(os.fsdecode(path)) == os.path.normcase(str(source)):
            raise PermissionError(errno.EACCES, "injected source access failure", str(source))
        return original(path, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "lstat", create_autospec(original, side_effect=lstat))
        await finalize_mutation_tracking(tracker, context, "move")
    tracker = MutationTracker.deserialize(tracker.serialize(), store)
    summary = tracker.get_turn_file_summary(1)
    assert summary[str(source)].after_skip is SnapshotSkipReason.UNREADABLE
    assert dict(tracker.get_rollback_plan().exclusions) == {
        str(source): RollbackExclusionReason.UNRESTORABLE,
        str(destination): RollbackExclusionReason.MOVE_POISONED,
    }
    tracker.rollback_turns({1})
    assert destination.read_bytes() == payload


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
@pytest.mark.parametrize("action", ["unlink", "edit_child", "remove_both", "retarget"])
async def test_directory_alias_children_keep_physical_identity_across_shell(
    tmp_path: Path, git_repo_factory: Callable[[Path], Path], action: str
) -> None:
    repo = git_repo_factory(tmp_path / "repo")
    real = repo / "real"
    real.mkdir()
    child = real / "file.txt"
    child.write_bytes(b"original child")
    alias = repo / "alias"
    alias.symlink_to("real", target_is_directory=True)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "directory alias")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    command = "rm alias/file.txt alias" if action == "remove_both" else "rm alias"
    context = await prepare_mutation_tracking(
        tracker, "shell", {"command": command, "workdir": str(repo)}, "shell", True, str(repo)
    )
    if action == "unlink":
        alias.unlink()
    elif action == "remove_both":
        (alias / "file.txt").unlink()
        alias.unlink()
    elif action == "retarget":
        alias.unlink()
        alias.symlink_to("missing-directory", target_is_directory=True)
    else:
        (alias / "file.txt").write_bytes(b"modified through alias")
    await finalize_mutation_tracking(tracker, context, "shell")
    expected = {str(child)} if action == "edit_child" else {str(alias)}
    if action == "remove_both":
        expected.add(str(child))
    assert set(tracker.get_turn_file_summary(1)) == expected
    tracker.rollback_turns({1})
    assert alias.is_symlink()
    assert os.readlink(alias) == "real"
    assert child.read_bytes() == b"original child"


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file"])
async def test_invalid_nul_path_does_not_abort_mutation_preparation(tmp_path: Path, tool_name: str) -> None:
    invalid = str(tmp_path / "invalid\0.txt")
    store = SnapshotStore(tmp_path / "session")
    assert not store.save(invalid, 1).existed
    assert store.save_blob(invalid).skip_reason is None
    assert WorkspaceScanner(str(tmp_path)).scan_paths([invalid]) == {}
    tracker = MutationTracker(store)
    tracker.start_turn(1)
    context = await prepare_mutation_tracking(tracker, tool_name, {"path": invalid}, "invalid", True, str(tmp_path))
    assert context.file_mutation is not None
    await finalize_mutation_tracking(tracker, context, "invalid")


@pytest.mark.parametrize("budget", ["files", "bytes", "exact"])
async def test_child_backup_budget_keeps_explicit_files_and_drops_unbacked_observations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    repo = tmp_path / "workspace"
    directory = repo / "data"
    directory.mkdir(parents=True)
    children = [directory / "a.txt", directory / "b.txt"]
    for child in children:
        child.write_bytes(b"original")
    explicit = repo / "explicit.txt"
    explicit.write_bytes(b"explicit before")
    monkeypatch.setattr(pipeline, "_MAX_SHELL_CHILD_SNAPSHOT_FILES", 1 if budget == "files" else 2)
    monkeypatch.setattr(pipeline, "_MAX_SHELL_CHILD_SNAPSHOT_BYTES", 15 if budget == "bytes" else 16)
    original = Path.read_bytes
    child_reads: list[Path] = []

    def read(path: Path) -> bytes:
        if path in children:
            child_reads.append(path)
        return original(path)

    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", create_autospec(original, side_effect=read))
        context = await prepare_mutation_tracking(
            tracker,
            "shell",
            {"command": "chmod +x data explicit.txt", "workdir": str(repo)},
            "shell",
            True,
            str(repo),
        )
    assert bool(child_reads) is (budget == "exact")
    children[0].write_bytes(b"modified child")
    children[1].unlink()
    created = directory / "new.txt"
    created.write_bytes(b"new child")
    explicit.write_bytes(b"modified explicit")
    await finalize_mutation_tracking(tracker, context, "shell")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None and turn.detection_truncated is (budget != "exact")
    expected_paths = {str(explicit)}
    if budget == "exact":
        expected_paths.update(str(path) for path in [*children, created])
    assert set(tracker.get_turn_file_summary(1)) == expected_paths
    tracker.rollback_turns({1})
    assert explicit.read_bytes() == b"explicit before"
    assert children[0].read_bytes() == (b"original" if budget == "exact" else b"modified child")
    assert children[1].exists() is (budget == "exact")
    assert created.exists() is (budget != "exact")
