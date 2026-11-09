# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""All mutation producers share stable entry identities and earliest backups."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from chrys.foundation.platform import get_platform
from chrys.service.mutations.pipeline import finalize_mutation_tracking, prepare_mutation_tracking
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationLog, MutationOp, MutationSource


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
@pytest.mark.parametrize("shell_first", [False, True])
@pytest.mark.parametrize("shell_mode", ["targeted", "git"])
@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("file_tool", ["edit_file", "write_file"])
async def test_file_tool_and_shell_alias_edits_restore_the_earliest_content(
    tmp_path: Path,
    git_repo_factory: Callable[[Path], Path],
    shell_first: bool,
    shell_mode: str,
    resume: bool,
    file_tool: str,
) -> None:
    if shell_mode == "git":
        real = git_repo_factory(tmp_path / "real")
        child = real / "README.md"
    else:
        real = tmp_path / "real"
        real.mkdir()
        child = real / "file.txt"
        child.write_bytes(b"original")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    aliased = alias / child.name
    original = child.read_bytes()
    store = SnapshotStore(tmp_path / "session")
    tracker = MutationTracker(store)
    tracker.start_turn(1)
    cwd = str(real if shell_mode == "git" else tmp_path)
    for index, is_shell in enumerate([shell_first, not shell_first]):
        command = "opaque-launcher" if shell_mode == "git" else f"touch alias/{child.name}"
        context = await prepare_mutation_tracking(
            tracker,
            "shell" if is_shell else file_tool,
            {"command": command, "workdir": cwd} if is_shell else {"path": str(aliased)},
            str(index),
            True,
            cwd,
        )
        aliased.write_bytes(f"edit {index}".encode())
        await finalize_mutation_tracking(tracker, context, str(index))
        if resume:
            tracker = MutationTracker.deserialize(tracker.serialize(), store)
    summary = tracker.get_turn_file_summary(1)
    assert set(summary) == {str(child)}
    assert summary[str(child)].before == SnapshotStore.content_hash(original)
    assert summary[str(child)].after == SnapshotStore.content_hash(b"edit 1")
    assert len(tracker.log.snapshots) == 1
    assert tracker.get_snapshot(str(aliased), 1) is tracker.get_snapshot(str(child), 1)
    results = tracker.rollback_turns({1})
    assert results and all(result.ok for result in results)
    assert child.read_bytes() == original
    assert alias.is_symlink()


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
def test_tracker_entry_points_share_keys_but_keep_the_final_link_distinct(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    child = real / "file.txt"
    child.write_bytes(b"original")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    aliased = alias / child.name
    link = real / "file-link"
    link.symlink_to("file.txt")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    tracker.pre_snapshot([str(aliased), str(child)])
    assert len(tracker.log.snapshots) == 1
    assert tracker.get_file_lock(str(aliased)) is tracker.get_file_lock(str(child))
    assert tracker.get_file_lock(str(link)) is not tracker.get_file_lock(str(child))
    child.write_bytes(b"modified")
    row = tracker.record_calibrated(str(aliased), MutationOp.MODIFY, b"original")
    assert row is not None and row.path == str(child)
    assert tracker.get_original_snapshot(str(aliased)) is tracker.get_original_snapshot(str(child))
    destination = real / "moved.txt"
    tracker.pre_snapshot([str(destination)])
    child.rename(destination)
    row = tracker.record(
        str(alias / destination.name), MutationOp.MOVE, MutationSource.SHELL, "move", old_path=str(aliased)
    )
    assert row is not None and row.old_path == str(child)
    assert row.path == str(destination)
    tracker.rollback_turns({1})
    assert child.read_bytes() == b"original"
    assert not destination.exists()
    assert link.is_symlink() and os.readlink(link) == "file.txt"


@pytest.mark.parametrize("tool_name", ["shell", "write_file", "edit_file"])
@pytest.mark.parametrize("component", ["parent", "leaf"])
async def test_nul_path_normalization_cannot_abort_tool_tracking(
    tmp_path: Path, tool_name: str, component: str
) -> None:
    invalid = str(tmp_path / ("bad\0parent/file.txt" if component == "parent" else "bad\0file.txt"))
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    args = (
        {"command": f'echo data > "{invalid}"', "workdir": str(tmp_path)} if tool_name == "shell" else {"path": invalid}
    )
    context = await prepare_mutation_tracking(tracker, tool_name, args, "invalid", True, str(tmp_path))
    await finalize_mutation_tracking(tracker, context, "invalid")
    if tool_name == "shell":
        assert tracker.get_turn_file_summary(1) == {}
    else:
        row = context.file_mutation
        assert row is not None and row.before_hash is None and row.after_hash is None
        assert row.before_skip is None and row.after_skip is None


@pytest.mark.skipif(get_platform().is_windows, reason="symlinks require privileges on Windows")
def test_saved_legacy_alias_keys_are_not_reinterpreted_after_retarget(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "file.txt").write_bytes(b"original")
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    path = str(alias / "file.txt")
    store = SnapshotStore(tmp_path / "session")
    # Earlier releases persisted lexical aliases. Loading and querying these
    # snapshots must preserve their captured identities, even if links changed.
    snapshot = store.save(path, 1)
    key = MutationLog.snapshot_key(path, 1)
    tracker = MutationTracker.deserialize({"turns": [], "snapshots": {key: snapshot.to_dict()}}, store)
    alias.unlink()
    alias.symlink_to("different-directory", target_is_directory=True)
    saved = tracker.get_snapshot(path, 1)
    assert saved is not None and saved.content_hash == SnapshotStore.content_hash(b"original")
    assert tracker.get_original_snapshot(path) is saved
    assert list(tracker.serialize()["snapshots"]) == [key]
