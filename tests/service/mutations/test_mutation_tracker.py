# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for MutationTracker: snapshot capture, cleanup, cross-tool turns, rollback, and middleware capture."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from chrys.foundation.models.invocations import InvocationOrigin
from chrys.service.agent_middleware import ToolEventMiddleware
from chrys.service.mutations.scanner import WorkspaceScanner
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource, RestoreOutcome

if TYPE_CHECKING:
    from pathlib import Path


# ===========================================================================
# MutationTracker — before/after snapshot capture
# ===========================================================================


class TestMutationTrackerSnapshots:
    """MutationTracker captures before/after content hashes on file mutations."""

    def test_record_sets_before_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("original", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mutation = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call_1")
        assert mutation is not None
        assert mutation.before_hash is not None

    def test_record_after_sets_after_hash(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("original", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mutation = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call_1")

        f.write_text("modified", encoding="utf-8")
        tracker.record_after(mutation)

        assert mutation.after_hash is not None
        assert mutation.before_hash != mutation.after_hash

    def test_get_file_edit_snapshots(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("before text", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call_1")
        f.write_text("after text", encoding="utf-8")
        tracker.record_after(m)

        snapshots = tracker.get_file_edit_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0] == ("before text", "after text")

    def test_new_file_has_empty_before(self, tmp_path: Path) -> None:
        f = tmp_path / "new.txt"

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.CREATE, MutationSource.WRITE_FILE, "call_1")
        f.write_text("created!", encoding="utf-8")
        tracker.record_after(m)

        snapshots = tracker.get_file_edit_snapshots()
        assert snapshots[0] == ("", "created!")

    def test_multiple_edits_ordered(self, tmp_path: Path) -> None:
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("a_before", encoding="utf-8")
        f2.write_text("b_before", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        m1 = tracker.record(str(f1), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f1.write_text("a_after", encoding="utf-8")
        tracker.record_after(m1)

        m2 = tracker.record(str(f2), MutationOp.MODIFY, MutationSource.WRITE_FILE, "c2")
        f2.write_text("b_after", encoding="utf-8")
        tracker.record_after(m2)

        snapshots = tracker.get_file_edit_snapshots()
        assert len(snapshots) == 2
        assert snapshots[0] == ("a_before", "a_after")
        assert snapshots[1] == ("b_before", "b_after")

    def test_serialize_deserialize_preserves_hashes(self, tmp_path: Path) -> None:
        f = tmp_path / "test.py"
        f.write_text("v1", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m)

        data = tracker.serialize()
        restored = MutationTracker.deserialize(data, SnapshotStore(tmp_path))
        snapshots = restored.get_file_edit_snapshots()
        assert snapshots == [("v1", "v2")]

    def test_shell_mutations_excluded_from_file_edit_snapshots(self, tmp_path: Path) -> None:
        """SHELL mutations are excluded even when both hashes are set."""
        f = tmp_path / "test.py"
        f.write_text("before", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.SHELL, "c1")
        f.write_text("after", encoding="utf-8")
        tracker.record_after(m)

        # Both hashes are set, but source is SHELL -> excluded
        assert m.before_hash is not None
        assert m.after_hash is not None
        assert tracker.get_file_edit_snapshots() == []


# ===========================================================================
# MutationTracker — edge cases and lifecycle
# ===========================================================================


class TestMutationTrackerEdgeCases:
    """Edge cases: deduplication, no-turn guard, pre_snapshot, binary files."""

    def test_record_no_active_turn_returns_none(self, tmp_path: Path) -> None:
        """record() returns None and logs a warning when no turn is active."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        result = tracker.record("/tmp/x.py", MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        assert result is None

    def test_same_file_multiple_edits_accurate_before_hash(self, tmp_path: Path) -> None:
        """Multiple edits: each mutation's before_hash reflects actual state before that call."""
        f = tmp_path / "test.py"
        f.write_text("original", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m1)

        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("v3", encoding="utf-8")
        tracker.record_after(m2)

        # m1 before = original (first mutation, no prior state)
        # m2 before = v2 (the result of m1, not the turn-start state)
        assert m1.before_hash != m2.before_hash
        assert m1.after_hash != m2.after_hash
        # m1.after == m2.before (incremental chain)
        assert m1.after_hash == m2.before_hash

        # DiffView shows incremental diffs, not diffs against turn start
        snapshots = tracker.get_file_edit_snapshots()
        assert len(snapshots) == 2
        assert snapshots[0] == ("original", "v2")
        assert snapshots[1] == ("v2", "v3")

    def test_get_file_lock_returns_same_lock_for_same_path(self, tmp_path: Path) -> None:
        """get_file_lock returns the same asyncio.Lock for the same normalized path."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        f = tmp_path / "test.py"
        lock1 = tracker.get_file_lock(str(f))
        lock2 = tracker.get_file_lock(str(f))
        assert lock1 is lock2

    def test_get_file_lock_returns_different_lock_for_different_path(self, tmp_path: Path) -> None:
        """get_file_lock returns different locks for different files."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        lock1 = tracker.get_file_lock(str(tmp_path / "a.py"))
        lock2 = tracker.get_file_lock(str(tmp_path / "b.py"))
        assert lock1 is not lock2

    def test_pre_snapshot_captures_before_state(self, tmp_path: Path) -> None:
        """pre_snapshot creates a snapshot usable by subsequent record()."""
        f = tmp_path / "target.txt"
        f.write_text("pre-snapshot content", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # Pre-snapshot (as shell tool would do)
        tracker.pre_snapshot([str(f)])

        # Now modify and record
        f.write_text("post content", encoding="utf-8")
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.SHELL, "c1")

        # before_hash comes from the pre-snapshot
        assert m is not None
        assert m.before_hash is not None
        before_bytes = tracker.store.read_blob(m.before_hash)
        assert before_bytes == b"pre-snapshot content"

    def test_pre_snapshot_no_active_turn_is_noop(self, tmp_path: Path) -> None:
        """pre_snapshot is a no-op when no turn is active."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        # Should not raise
        tracker.pre_snapshot(["/tmp/nonexistent"])

    def test_blob_deduplication(self, tmp_path: Path) -> None:
        """Identical files share the same blob on disk."""
        f1 = tmp_path / "a.txt"
        f2 = tmp_path / "b.txt"
        f1.write_text("same content", encoding="utf-8")
        f2.write_text("same content", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m1 = tracker.record(str(f1), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        m2 = tracker.record(str(f2), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")

        assert m1.before_hash == m2.before_hash
        # Only one blob file should exist for that hash
        blobs = list((tmp_path / "mutations").iterdir())
        assert len(blobs) == 1

    def test_nonexistent_file_snapshot(self, tmp_path: Path) -> None:
        """Recording a mutation on a non-existent file sets existed=False, before_hash=None."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(tmp_path / "ghost.txt"), MutationOp.CREATE, MutationSource.WRITE_FILE, "c1")
        assert m is not None
        assert m.before_hash is None

        snap = tracker.get_snapshot(str(tmp_path / "ghost.txt"), 1)
        assert snap is not None
        assert snap.existed is False

    def test_binary_file_in_get_file_edit_snapshots(self, tmp_path: Path) -> None:
        """Binary content is decoded without raising."""
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x80\x81\xff\xfe binary stuff")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_bytes(b"\x00\x01\x02")
        tracker.record_after(m)

        snapshots = tracker.get_file_edit_snapshots()
        assert len(snapshots) == 1
        # Should not raise; encoding detector may decode as a legacy
        # encoding (e.g. Windows-1251) or fall back to UTF-8 with replacement.
        assert isinstance(snapshots[0][0], str)
        assert "binary stuff" in snapshots[0][0]

    def test_get_changed_files(self, tmp_path: Path) -> None:
        """get_changed_files returns unique paths in first-seen order."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        (tmp_path / "a.py").write_text("a", encoding="utf-8")
        (tmp_path / "b.py").write_text("b", encoding="utf-8")
        tracker.record(str(tmp_path / "a.py"), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        tracker.record(str(tmp_path / "b.py"), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        tracker.record(str(tmp_path / "a.py"), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c3")

        changed = tracker.get_changed_files()
        assert len(changed) == 2
        # a.py first (seen first), b.py second
        assert changed[0].endswith("a.py")
        assert changed[1].endswith("b.py")

    def test_get_original_snapshot(self, tmp_path: Path) -> None:
        """get_original_snapshot returns the earliest snapshot across turns."""
        f = tmp_path / "evolving.py"
        f.write_text("v0", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v1", encoding="utf-8")
        tracker.record_after(m1)

        tracker.start_turn(2)
        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m2)

        snap = tracker.get_original_snapshot(str(f))
        assert snap is not None
        assert snap.period_index == 1
        content = tracker.store.read_content(snap)
        assert content == b"v0"


# ===========================================================================
# MutationTracker — cleanup unused snapshots
# ===========================================================================


class TestCleanupUnusedSnapshots:
    """cleanup_unused_snapshots removes pre-snapshots with no corresponding mutations."""

    def test_removes_orphan_snapshots_and_blobs(self, tmp_path: Path) -> None:
        """Pre-snapshotted files with no mutations get cleaned up."""
        dirty1 = tmp_path / "dirty1.py"
        dirty2 = tmp_path / "dirty2.py"
        target = tmp_path / "target.py"
        dirty1.write_text("unchanged1", encoding="utf-8")
        dirty2.write_text("unchanged2", encoding="utf-8")
        target.write_text("before", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # Pre-snapshot all three (as GitDiffCalibrator would)
        tracker.pre_snapshot([str(dirty1), str(dirty2), str(target)])

        # Only target.py actually gets a mutation
        target.write_text("after", encoding="utf-8")
        m = tracker.record(str(target), MutationOp.MODIFY, MutationSource.SHELL, "c1")
        assert m is not None

        # Before cleanup: 3 snapshots exist, blobs for all 3 files
        assert len(tracker.log.snapshots) == 3
        mutations_dir = tmp_path / "mutations"
        blobs_before = {f.name for f in mutations_dir.iterdir()} if mutations_dir.exists() else set()

        removed = tracker.cleanup_unused_snapshots()

        assert removed == 2
        assert len(tracker.log.snapshots) == 1
        # Only blobs referenced by the remaining snapshot + mutation survive
        blobs_after = {f.name for f in mutations_dir.iterdir()}
        assert len(blobs_after) < len(blobs_before)

    def test_no_mutations_all_snapshots_removed(self, tmp_path: Path) -> None:
        """When a turn has pre-snapshots but zero mutations, all snapshots are removed."""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("aaa", encoding="utf-8")
        f2.write_text("bbb", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        tracker.pre_snapshot([str(f1), str(f2)])

        assert len(tracker.log.snapshots) == 2

        removed = tracker.cleanup_unused_snapshots()

        assert removed == 2
        assert len(tracker.log.snapshots) == 0
        # All blobs should be gone
        mutations_dir = tmp_path / "mutations"
        if mutations_dir.exists():
            assert len(list(mutations_dir.iterdir())) == 0

    def test_noop_when_no_orphans(self, tmp_path: Path) -> None:
        """No removal when every snapshot has a corresponding mutation."""
        f = tmp_path / "test.py"
        f.write_text("content", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        tracker.pre_snapshot([str(f)])
        f.write_text("modified", encoding="utf-8")
        tracker.record(str(f), MutationOp.MODIFY, MutationSource.SHELL, "c1")

        removed = tracker.cleanup_unused_snapshots()
        assert removed == 0
        assert len(tracker.log.snapshots) == 1

    def test_noop_when_no_active_turn(self, tmp_path: Path) -> None:
        """Returns 0 when no turn has been started."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        assert tracker.cleanup_unused_snapshots() == 0

    def test_shared_blob_preserved_when_referenced_elsewhere(self, tmp_path: Path) -> None:
        """A blob shared between an orphan and a used snapshot is NOT deleted."""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("same content", encoding="utf-8")
        f2.write_text("same content", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # Both files have the same content → same blob hash
        tracker.pre_snapshot([str(f1), str(f2)])

        # Only f1 gets a mutation
        f1.write_text("changed", encoding="utf-8")
        tracker.record(str(f1), MutationOp.MODIFY, MutationSource.SHELL, "c1")

        # f2's snapshot is orphaned, but its blob hash == f1's snapshot hash
        removed = tracker.cleanup_unused_snapshots()

        assert removed == 1  # f2 snapshot removed
        # Blob is preserved because f1's snapshot still references it
        mutations_dir = tmp_path / "mutations"
        blobs = list(mutations_dir.iterdir())
        assert len(blobs) >= 1  # shared blob survives

    def test_only_cleans_current_turn(self, tmp_path: Path) -> None:
        """Snapshots from prior turns are not touched."""
        f = tmp_path / "dirty.py"
        f.write_text("dirty", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        # Turn 1: pre-snapshot with a mutation
        tracker.start_turn(1)
        tracker.pre_snapshot([str(f)])
        f.write_text("v1", encoding="utf-8")
        tracker.record(str(f), MutationOp.MODIFY, MutationSource.SHELL, "c1")

        # Turn 2: pre-snapshot without mutation (orphan)
        tracker.start_turn(2)
        tracker.pre_snapshot([str(f)])

        assert len(tracker.log.snapshots) == 2  # one per turn

        removed = tracker.cleanup_unused_snapshots()

        assert removed == 1  # only turn 2's orphan
        # Turn 1's snapshot still exists
        remaining_turn_ids = [s.period_index for s in tracker.log.snapshots.values()]
        assert 1 in remaining_turn_ids
        assert 2 not in remaining_turn_ids


# ===========================================================================
# MutationTracker — cross-tool scenarios within a turn
# ===========================================================================


class TestMutationTrackerCrossToolScenarios:
    """Scenarios: edit→edit, edit→shell rm, write→shell mv, failed edit."""

    def test_edit_then_edit_incremental_before_hash(self, tmp_path: Path) -> None:
        """Two edits to same file: each mutation's before is the previous after."""
        f = tmp_path / "code.py"
        f.write_text("v0", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v1", encoding="utf-8")
        tracker.record_after(m1)

        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m2)

        m3 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c3")
        f.write_text("v3", encoding="utf-8")
        tracker.record_after(m3)

        # Chain: v0→v1→v2→v3
        assert m1.after_hash == m2.before_hash  # v1
        assert m2.after_hash == m3.before_hash  # v2

        snaps = tracker.get_file_edit_snapshots()
        assert snaps == [("v0", "v1"), ("v1", "v2"), ("v2", "v3")]

    def test_edit_then_shell_delete(self, tmp_path: Path) -> None:
        """Edit a file, then shell-delete it: delete's before = edit's after."""
        f = tmp_path / "doomed.py"
        f.write_text("original", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # 1. edit_file
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("edited", encoding="utf-8")
        tracker.record_after(m1)

        # 2. shell: rm doomed.py (pre_snapshot, then execute, then record)
        tracker.pre_snapshot([str(f)])
        f.unlink()
        m2 = tracker.record(str(f), MutationOp.DELETE, MutationSource.SHELL, "c2")

        # delete's before = the edited content, not the turn-start original
        assert m2.before_hash == m1.after_hash
        # DiffView: only edit_file shows (shell excluded)
        assert tracker.get_file_edit_snapshots() == [("original", "edited")]

        # Turn summary: before=original, after=None (deleted)
        summary = tracker.get_turn_file_summary(1)
        norm = os.path.normpath(os.path.abspath(str(f)))
        assert summary[norm].before is not None  # had content before turn
        assert summary[norm].after is None  # deleted (no record_after for shell)

        # Rollback restores to pre-turn state (original)
        f.write_text("should be overwritten", encoding="utf-8")  # simulate something
        tracker.rollback(1)
        assert f.read_text(encoding="utf-8") == "original"

    def test_write_new_file_then_shell_move(self, tmp_path: Path) -> None:
        """Create file via write_file, then mv via shell."""
        src = tmp_path / "src.py"
        dst = tmp_path / "dst.py"

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # 1. write_file creates src.py
        m1 = tracker.record(str(src), MutationOp.CREATE, MutationSource.WRITE_FILE, "c1")
        assert m1.before_hash is None  # file didn't exist
        src.write_text("new content", encoding="utf-8")
        tracker.record_after(m1)

        # 2. shell: mv src.py dst.py
        tracker.pre_snapshot([str(src), str(dst)])
        src.rename(dst)
        # Scanner would detect: src=DELETE, dst=CREATE
        m_del = tracker.record(str(src), MutationOp.DELETE, MutationSource.SHELL, "c2")
        m_create = tracker.record(str(dst), MutationOp.CREATE, MutationSource.SHELL, "c2")

        # delete's before = hash of "new content" (from write_file)
        assert m_del.before_hash == m1.after_hash
        # create's before = None (dst didn't exist before the mv)
        assert m_create.before_hash is None

        # DiffView: only write_file shows
        assert tracker.get_file_edit_snapshots() == [("", "new content")]

    def test_failed_edit_then_successful_edit(self, tmp_path: Path) -> None:
        """Failed edit doesn't change file; next edit sees the unchanged state."""
        f = tmp_path / "code.py"
        f.write_text("stable", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # 1. edit_file that fails (file unchanged on disk)
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        # Tool raises but finally block still calls record_after
        # File wasn't modified, so after_hash == before_hash
        tracker.record_after(m1)
        assert m1.before_hash == m1.after_hash

        # 2. Successful edit
        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("fixed", encoding="utf-8")
        tracker.record_after(m2)

        # m2's before = stable (unchanged by the failed edit)
        assert m2.before_hash == m1.after_hash

        snaps = tracker.get_file_edit_snapshots()
        assert len(snaps) == 2
        assert snaps[0] == ("stable", "stable")  # no-op edit
        assert snaps[1] == ("stable", "fixed")  # actual change

    def test_shell_modify_then_edit(self, tmp_path: Path) -> None:
        """Shell modifies file, then edit_file: edit sees post-shell state."""
        f = tmp_path / "config.yaml"
        f.write_text("key: old", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # 1. shell: sed modifies the file
        tracker.pre_snapshot([str(f)])
        f.write_text("key: shell-modified", encoding="utf-8")
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.SHELL, "c1")
        # m1.before_hash = hash("key: old") via _last_known_hash from pre_snapshot

        # 2. edit_file refines it further
        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("key: final", encoding="utf-8")
        tracker.record_after(m2)

        # edit's before = the shell-modified content, not the turn start
        assert m2.before_hash == m1.after_hash

        snaps = tracker.get_file_edit_snapshots()
        assert len(snaps) == 1
        assert snaps[0] == ("key: shell-modified", "key: final")

    def test_turn_file_summary_collapses_multiple_edits(self, tmp_path: Path) -> None:
        """get_turn_file_summary: before=turn-start, after=last mutation."""
        f = tmp_path / "multi.py"
        f.write_text("v0", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v1", encoding="utf-8")
        tracker.record_after(m1)

        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m2)

        summary = tracker.get_turn_file_summary(1)
        norm = os.path.normpath(os.path.abspath(str(f)))
        diff = summary[norm]

        # before = turn start (v0), after = last mutation result (v2)
        assert diff.before == m1.before_hash
        assert diff.after == m2.after_hash
        assert diff.before != diff.after

    def test_turn_file_summary_new_file_created(self, tmp_path: Path) -> None:
        """get_turn_file_summary: new file has before=None."""
        f = tmp_path / "brand_new.py"

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        m = tracker.record(str(f), MutationOp.CREATE, MutationSource.WRITE_FILE, "c1")
        f.write_text("hello", encoding="utf-8")
        tracker.record_after(m)

        summary = tracker.get_turn_file_summary(1)
        norm = os.path.normpath(os.path.abspath(str(f)))
        assert summary[norm].before is None  # didn't exist before turn
        assert summary[norm].after is not None  # exists after

    def test_rollback_after_multiple_edits_restores_pre_turn_state(self, tmp_path: Path) -> None:
        """Rollback with 3 edits in one turn restores to pre-turn state (not intermediate)."""
        f = tmp_path / "many_edits.py"
        f.write_text("v0", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        for i in range(1, 4):
            m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, f"c{i}")
            f.write_text(f"v{i}", encoding="utf-8")
            tracker.record_after(m)

        assert f.read_text(encoding="utf-8") == "v3"
        tracker.rollback(1)
        assert f.read_text(encoding="utf-8") == "v0"


# ===========================================================================
# MutationTracker + WorkspaceScanner — failed shell operations
# ===========================================================================


class TestFailedShellOperations:
    """Failed rm/mv/cp: no false mutations when disk is unchanged."""

    def test_failed_rm_records_nothing(self, tmp_path: Path) -> None:
        """rm fails → file still on disk → diff empty → no mutation recorded."""
        f = tmp_path / "protected.py"
        f.write_text("important code", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # Pre-scan (middleware would do this)
        scanner = WorkspaceScanner(str(tmp_path))
        tracker.pre_snapshot([str(f)])
        before = scanner.scan_paths([str(f)])

        # Shell runs "rm protected.py" but fails (permission denied).
        # File is still on disk — we simulate by doing nothing.

        # Post-scan: detect actual changes
        after = scanner.scan_paths([str(f)])
        changes = WorkspaceScanner.diff(before, after)

        # No changes detected → no mutations recorded
        assert changes == []
        turn = tracker.get_turn_mutations(1)
        assert turn is not None
        assert turn.mutations == []

        # last_known_hash correctly reflects the unchanged file
        norm = os.path.normpath(os.path.abspath(str(f)))
        assert tracker._last_known_hash[norm] is not None

    def test_failed_mv_records_nothing(self, tmp_path: Path) -> None:
        """mv fails → both src and dst unchanged → no mutation recorded."""
        src = tmp_path / "src.py"
        src.write_text("source", encoding="utf-8")
        # dst doesn't exist

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        scanner = WorkspaceScanner(str(tmp_path))
        tracker.pre_snapshot([str(src), str(tmp_path / "dst.py")])
        before = scanner.scan_paths([str(src), str(tmp_path / "dst.py")])

        # mv fails → nothing changes on disk

        after = scanner.scan_paths([str(src), str(tmp_path / "dst.py")])
        changes = WorkspaceScanner.diff(before, after)

        assert changes == []
        assert tracker.get_turn_mutations(1).mutations == []

    def test_partial_rm_records_only_deleted_files(self, tmp_path: Path) -> None:
        """rm file1 file2: file1 deleted, file2 permission denied."""
        f1 = tmp_path / "file1.txt"
        f2 = tmp_path / "file2.txt"
        f1.write_text("data1", encoding="utf-8")
        f2.write_text("data2", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        scanner = WorkspaceScanner(str(tmp_path))
        targets = [str(f1), str(f2)]
        tracker.pre_snapshot(targets)
        before = scanner.scan_paths(targets)

        # Simulate partial success: only file1 gets deleted
        f1.unlink()

        after = scanner.scan_paths(targets)
        changes = WorkspaceScanner.diff(before, after)

        for path, op in changes:
            tracker.record(path, op, MutationSource.SHELL, "c1")

        # Only file1 DELETE recorded
        turn = tracker.get_turn_mutations(1)
        assert len(turn.mutations) == 1
        assert turn.mutations[0].operation == MutationOp.DELETE
        assert turn.mutations[0].path.endswith("file1.txt")

        # file2 state untouched in last_known_hash
        norm_f2 = os.path.normpath(os.path.abspath(str(f2)))
        assert tracker._last_known_hash[norm_f2] is not None

    def test_failed_shell_then_edit_has_correct_before(self, tmp_path: Path) -> None:
        """Failed rm, then edit_file: edit sees the original (not deleted) state."""
        f = tmp_path / "code.py"
        f.write_text("original", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        # Shell rm fails
        scanner = WorkspaceScanner(str(tmp_path))
        tracker.pre_snapshot([str(f)])
        before = scanner.scan_paths([str(f)])
        # rm fails → file unchanged
        after = scanner.scan_paths([str(f)])
        changes = WorkspaceScanner.diff(before, after)
        assert changes == []

        # Now edit_file: should see the original content as before
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("edited", encoding="utf-8")
        tracker.record_after(m)

        # before_hash = hash("original"), set by pre_snapshot
        assert m.before_hash is not None
        before_bytes = tracker.store.read_blob(m.before_hash)
        assert before_bytes == b"original"

        snaps = tracker.get_file_edit_snapshots()
        assert snaps == [("original", "edited")]

    def test_interrupted_shell_no_false_state(self, tmp_path: Path) -> None:
        """Shell never runs (interrupted) → diff empty → state clean."""
        f = tmp_path / "safe.py"
        f.write_text("untouched", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)

        scanner = WorkspaceScanner(str(tmp_path))
        tracker.pre_snapshot([str(f)])
        before = scanner.scan_paths([str(f)])

        # Interrupted before shell executes → disk unchanged
        after = scanner.scan_paths([str(f)])
        changes = WorkspaceScanner.diff(before, after)

        assert changes == []
        assert tracker.get_turn_mutations(1).mutations == []

        # File is still pristine
        assert f.read_text(encoding="utf-8") == "untouched"


# ===========================================================================
# MutationTracker — rollback and cleanup
# ===========================================================================


class TestMutationTrackerRollback:
    """Rollback, remove_turn, and clear operations."""

    def test_rollback_restores_modified_file(self, tmp_path: Path) -> None:
        """Rolling back one turn restores the file to its pre-turn state."""
        f = tmp_path / "code.py"
        f.write_text("original", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("changed", encoding="utf-8")
        tracker.record_after(m)

        assert f.read_text(encoding="utf-8") == "changed"
        restored = tracker.rollback(1)
        assert restored and restored[0].path == str(f) and restored[0].changed
        assert f.read_text(encoding="utf-8") == "original"

    def test_rollback_deletes_created_file(self, tmp_path: Path) -> None:
        """Rolling back a CREATE operation deletes the file."""
        f = tmp_path / "new_file.py"

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.CREATE, MutationSource.WRITE_FILE, "c1")
        f.write_text("created!", encoding="utf-8")
        tracker.record_after(m)

        assert f.exists()
        restored = tracker.rollback(1)
        assert len(restored) == 1
        assert not f.exists()

    def test_rollback_multi_turn(self, tmp_path: Path) -> None:
        """Rolling back multiple turns restores to the pre-window state."""
        f = tmp_path / "multi.py"
        f.write_text("v0", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))

        tracker.start_turn(1)
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v1", encoding="utf-8")
        tracker.record_after(m1)

        tracker.start_turn(2)
        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m2)

        # Roll back 2 turns -> restore to v0
        tracker.rollback(2)
        assert f.read_text(encoding="utf-8") == "v0"
        assert len(tracker.get_all_turns()) == 0

    def test_rollback_explicit_turn_ids_ignores_log_insertion_order(self, tmp_path: Path) -> None:
        """Rolling back by turn ID must not treat the log suffix as the target."""
        f_turn_3 = tmp_path / "turn3.py"
        f_turn_2 = tmp_path / "turn2.py"
        f_turn_3.write_text("v0-3", encoding="utf-8")
        f_turn_2.write_text("v0-2", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))

        tracker.start_turn(1)

        tracker.start_turn(3)
        m3 = tracker.record(str(f_turn_3), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c3")
        f_turn_3.write_text("v3", encoding="utf-8")
        tracker.record_after(m3)

        tracker.start_turn(2)
        m2 = tracker.record(str(f_turn_2), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f_turn_2.write_text("v2", encoding="utf-8")
        tracker.record_after(m2)

        restored = tracker.rollback_turns({3})

        assert [result.path for result in restored] == [str(f_turn_3)]
        assert f_turn_3.read_text(encoding="utf-8") == "v0-3"
        assert f_turn_2.read_text(encoding="utf-8") == "v2"
        assert [turn.turn_id for turn in tracker.get_all_turns()] == [1, 2]

    def test_rollback_empty_turns(self, tmp_path: Path) -> None:
        """Rollback with no turns is a no-op."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        assert tracker.rollback(1) == []

    def test_remove_turn_cleans_orphaned_blobs(self, tmp_path: Path) -> None:
        """remove_turn deletes blobs not referenced by remaining turns."""
        f = tmp_path / "test.py"
        f.write_text("content-a", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("content-b", encoding="utf-8")
        tracker.record_after(m)

        blob_dir = tmp_path / "mutations"
        assert blob_dir.exists()
        blobs_before = set(blob_dir.iterdir())
        assert len(blobs_before) == 2  # before + after blobs

        orphaned = tracker.remove_turn(1)
        assert len(orphaned) == 2

        # Blobs should be deleted
        remaining = set(blob_dir.iterdir()) if blob_dir.exists() else set()
        assert remaining == set()

    def test_remove_turn_preserves_shared_blobs(self, tmp_path: Path) -> None:
        """remove_turn keeps blobs still referenced by other turns."""
        f = tmp_path / "shared.py"
        f.write_text("shared-content", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))

        # Turn 1: snapshot the file
        tracker.start_turn(1)
        m1 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("v1", encoding="utf-8")
        tracker.record_after(m1)

        # Turn 2: snapshot the file again (now "v1")
        tracker.start_turn(2)
        m2 = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c2")
        f.write_text("v2", encoding="utf-8")
        tracker.record_after(m2)

        # Remove turn 1 — "shared-content" blob is only in turn 1
        # but "v1" blob is referenced as turn-2 snapshot AND as turn-1 after_hash
        orphaned = tracker.remove_turn(1)
        # "shared-content" hash should be orphaned
        assert len(orphaned) >= 1

        # Turn 2 snapshots should still be readable
        snap = tracker.get_file_edit_snapshots()
        assert len(snap) == 1
        assert snap[0][0] == "v1"
        assert snap[0][1] == "v2"

    def test_clear_removes_everything(self, tmp_path: Path) -> None:
        """clear() removes all turns, snapshots, and blob files."""
        f = tmp_path / "test.py"
        f.write_text("content", encoding="utf-8")

        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")
        f.write_text("new", encoding="utf-8")
        tracker.record_after(m)

        blob_dir = tmp_path / "mutations"
        assert blob_dir.exists()

        tracker.clear()
        assert tracker.get_all_turns() == []
        assert tracker.get_changed_files() == []
        assert tracker.get_file_edit_snapshots() == []
        assert not blob_dir.exists()

    def test_get_rollback_plan_caps_at_available_turns(self, tmp_path: Path) -> None:
        """Requesting more turns than available rolls back all turns."""
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        f = tmp_path / "x.py"
        f.write_text("hi", encoding="utf-8")
        tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, "c1")

        plan = tracker.get_rollback_plan(100)  # way more than 1 turn
        assert len(plan.entries) == 1

    def test_rollback_continues_past_one_file_failing(self, tmp_path: Path) -> None:
        """Per-file best-effort: one restore raising must not abort the batch.

        ``SnapshotStore.restore`` is already defensive, but we also want
        the tracker's outer loop to survive a truly unexpected exception
        (e.g. a disk IO bug surfacing as something other than OSError).
        This test swaps in a store whose first ``restore()`` raises, then
        confirms the second file still gets attempted and reported.
        """

        class _FlakyStore(SnapshotStore):
            def __init__(self, base: Path) -> None:
                super().__init__(base)
                self.calls = 0

            def restore(self, snapshot):  # type: ignore[override]
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("simulated unexpected failure")
                return super().restore(snapshot)

        store = _FlakyStore(tmp_path)
        tracker = MutationTracker(store)
        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        file_a.write_text("a-orig", encoding="utf-8")
        file_b.write_text("b-orig", encoding="utf-8")
        tracker.start_turn(1)
        mut_a = tracker.record(str(file_a), MutationOp.MODIFY, MutationSource.EDIT_FILE, "ca")
        assert mut_a is not None
        file_a.write_text("a-changed", encoding="utf-8")
        tracker.record_after(mut_a)
        mut_b = tracker.record(str(file_b), MutationOp.MODIFY, MutationSource.EDIT_FILE, "cb")
        assert mut_b is not None
        file_b.write_text("b-changed", encoding="utf-8")
        tracker.record_after(mut_b)

        results = tracker.rollback(1)
        # Both files reported, even though the first one raised mid-flight.
        assert len(results) == 2
        outcomes = {r.path: r.outcome for r in results}
        assert any(o is RestoreOutcome.FAILED for o in outcomes.values())
        # One of them succeeded (the non-flaky one) — disk matches pre-turn.
        applied_paths = [r.path for r in results if r.changed]
        assert applied_paths, "expected at least one file to be restored"

    def test_rollback_with_only_paths_restores_subset(self, tmp_path: Path) -> None:
        """``only_paths`` restricts the file restore to the given subset.

        Mirrors the path the rollback modal takes when the user
        un-checks some files before clicking "Rollback & Revert
        Changes": turns are popped normally, but un-selected files
        keep their mutated content.

        Paths in the filter must match the tracker's canonical form
        (``os.path.normpath(os.path.abspath(...))``) — this is what the
        real UI passes too, because ``DiffFileEntry.path`` is populated
        from ``Mutation.path`` which the tracker already normalized.
        """
        store = SnapshotStore(tmp_path)
        tracker = MutationTracker(store)

        file_a = tmp_path / "a.txt"
        file_b = tmp_path / "b.txt"
        file_a.write_text("a-original", encoding="utf-8")
        file_b.write_text("b-original", encoding="utf-8")

        tracker.start_turn(1)
        mut_a = tracker.record(str(file_a), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-a")
        assert mut_a is not None
        file_a.write_text("a-changed", encoding="utf-8")
        tracker.record_after(mut_a)

        mut_b = tracker.record(str(file_b), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-b")
        assert mut_b is not None
        file_b.write_text("b-changed", encoding="utf-8")
        tracker.record_after(mut_b)

        # Use the same normalization the tracker applies, so this test
        # is stable on both Windows and POSIX regardless of whatever
        # form pytest's ``tmp_path`` produced.
        norm_a = os.path.normpath(os.path.abspath(str(file_a)))
        norm_b = os.path.normpath(os.path.abspath(str(file_b)))

        # Only restore file_a; file_b stays at its mutated content.
        restored = tracker.rollback(1, only_paths={norm_a})

        paths = [r.path for r in restored]
        assert paths == [norm_a]
        assert norm_b not in paths
        assert all(r.changed for r in restored)
        assert file_a.read_text(encoding="utf-8") == "a-original"
        assert file_b.read_text(encoding="utf-8") == "b-changed"
        # Turn log is still fully popped regardless of the filter.
        assert tracker.get_all_turns() == []

    def test_rollback_only_paths_requires_canonical_form(self, tmp_path: Path) -> None:
        """A relative (or otherwise non-canonical) path in ``only_paths`` silently skips.

        Documents the contract: ``only_paths`` is matched against the
        tracker's canonical absolute/normpath form, not whatever string
        the caller happened to pass to ``record()``.  Callers that
        build the filter from ``DiffFileEntry.path`` (the UI path) get
        this right automatically; callers passing raw relative strings
        would find nothing restored — which this test locks in.
        """
        store = SnapshotStore(tmp_path)
        tracker = MutationTracker(store)

        file_a = tmp_path / "a.txt"
        file_a.write_text("a-original", encoding="utf-8")

        tracker.start_turn(1)
        mut = tracker.record(str(file_a), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call")
        assert mut is not None
        file_a.write_text("a-changed", encoding="utf-8")
        tracker.record_after(mut)

        # "a.txt" alone is a relative name — not what the tracker stored.
        restored = tracker.rollback(1, only_paths={"a.txt"})

        assert restored == []
        assert file_a.read_text(encoding="utf-8") == "a-changed"

    # NOTE: the "restored" list from rollback() now holds RestoreResult
    # entries, not plain paths.  Use ``r.path`` / ``r.changed`` / ``r.ok``
    # on each entry; empty list means the filter matched nothing.

    def test_rollback_only_paths_case_sensitive_match(self, tmp_path: Path) -> None:
        """``only_paths`` uses byte-exact matching — no ``normcase`` folding.

        The tracker does ``normpath(abspath(...))`` (which on Windows
        also normalises the drive letter), but it does **not** call
        ``normcase`` — so case is preserved as stored.  Matching in
        ``only_paths`` therefore treats ``/Foo/bar.txt`` and
        ``/foo/bar.txt`` as distinct entries.  On POSIX this is the
        correct behaviour; on Windows (where filesystems are typically
        case-insensitive) callers should either pass paths that match
        how the tracker stored them or pre-apply ``os.path.normcase``
        themselves before building the set.
        """
        store = SnapshotStore(tmp_path)
        tracker = MutationTracker(store)

        file_a = tmp_path / "a.txt"
        file_a.write_text("orig", encoding="utf-8")

        tracker.start_turn(1)
        mut = tracker.record(str(file_a), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call")
        assert mut is not None
        file_a.write_text("changed", encoding="utf-8")
        tracker.record_after(mut)

        norm_a = os.path.normpath(os.path.abspath(str(file_a)))
        # Upper-case the filename portion only.  This produces a string
        # that never exists in the tracker's snapshot map (on POSIX) —
        # and on Windows where ``abspath`` already canonicalises the
        # drive letter but leaves the rest alone, the mismatch survives.
        scrambled = os.path.join(os.path.dirname(norm_a), os.path.basename(norm_a).upper())

        if scrambled == norm_a:
            # All-uppercase filename == stored form (e.g. an ALL-CAPS
            # filename from the start) — skip the assertion for this
            # degenerate input since the test would be vacuous.
            pytest.skip("filename is already uppercase, case comparison would be trivial")

        restored = tracker.rollback(1, only_paths={scrambled})
        assert [r.path for r in restored] == []
        assert file_a.read_text(encoding="utf-8") == "changed"

    def test_rollback_with_empty_only_paths_restores_nothing(self, tmp_path: Path) -> None:
        """Empty ``only_paths`` set → no file is restored, but the turn is still popped."""
        store = SnapshotStore(tmp_path)
        tracker = MutationTracker(store)

        file_a = tmp_path / "a.txt"
        file_a.write_text("a-original", encoding="utf-8")

        tracker.start_turn(1)
        mut = tracker.record(str(file_a), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call")
        assert mut is not None
        file_a.write_text("a-changed", encoding="utf-8")
        tracker.record_after(mut)

        restored = tracker.rollback(1, only_paths=set())

        assert [r.path for r in restored] == []
        assert file_a.read_text(encoding="utf-8") == "a-changed"
        assert tracker.get_all_turns() == []


# ===========================================================================
# Middleware — mutation tracking via MutationTracker
# ===========================================================================


class TestMiddlewareMutationTracking:
    """ToolEventMiddleware records mutations via MutationTracker."""

    async def test_captures_edit_file_mutation(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        target = tmp_path / "test.py"
        target.write_text("original content", encoding="utf-8")

        ctx = MagicMock()
        ctx.function.name = "edit_file"
        ctx.arguments = {"path": str(target)}
        ctx.result = "Success"

        async def fake_call_next():
            target.write_text("modified content", encoding="utf-8")

        await mw.process(ctx, fake_call_next)

        snapshots = tracker.get_file_edit_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0] == ("original content", "modified content")

    async def test_captures_write_file_mutation(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        target = tmp_path / "new.txt"

        ctx = MagicMock()
        ctx.function.name = "write_file"
        ctx.arguments = {"path": str(target)}
        ctx.result = "Success"

        async def fake_call_next():
            target.write_text("new file content", encoding="utf-8")

        await mw.process(ctx, fake_call_next)

        snapshots = tracker.get_file_edit_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0] == ("", "new file content")

    async def test_no_mutation_for_read_file(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        ctx = MagicMock()
        ctx.function.name = "read_file"
        ctx.arguments = {"path": "/some/file.py"}
        ctx.result = "file content"

        await mw.process(ctx, AsyncMock())

        assert tracker.get_file_edit_snapshots() == []

    async def test_no_mutation_for_non_file_tools(self, tmp_path: Path) -> None:
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        for tool_name in ("grep", "glob"):
            ctx = MagicMock()
            ctx.function.name = tool_name
            ctx.arguments = {"pattern": "*.py"}
            ctx.result = "output"
            await mw.process(ctx, AsyncMock())

        assert tracker.get_file_edit_snapshots() == []

    async def test_shell_tool_triggers_pre_post_scan(self, tmp_path: Path) -> None:
        """Shell tool calls trigger pre/post scanning and record detected mutations."""
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        target = tmp_path / "victim.txt"
        target.write_text("before content", encoding="utf-8")

        ctx = MagicMock()
        ctx.function.name = "zsh"
        ctx.function.chrys_kind = "shell"
        ctx.arguments = {"command": "rm victim.txt", "workdir": str(tmp_path)}
        ctx.result = "ok"

        async def fake_shell():
            target.unlink()

        await mw.process(ctx, fake_shell)

        # The heuristic scanner should have detected the DELETE
        changed = tracker.get_changed_files()
        assert any(p.endswith("victim.txt") for p in changed)

    async def test_shell_tool_no_changes_records_nothing(self, tmp_path: Path) -> None:
        """Shell tool that doesn't modify files records no mutations."""
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        ctx = MagicMock()
        ctx.function.name = "bash"
        ctx.function.chrys_kind = "shell"
        ctx.arguments = {"command": "echo hello"}
        ctx.result = "hello"

        await mw.process(ctx, AsyncMock())

        # "echo hello" has no file targets — no mutations
        assert tracker.get_changed_files() == []

    async def test_shell_tool_modifies_file(self, tmp_path: Path) -> None:
        """Shell tool modifying a file records the mutation with correct before hash."""
        bus = AsyncMock()
        bus.publish = AsyncMock()
        tracker = MutationTracker(SnapshotStore(tmp_path))
        tracker.start_turn(1)
        mw = ToolEventMiddleware(bus, mutation_tracker=tracker, origin=InvocationOrigin("turn", "", "turn-test", None))

        target = tmp_path / "output.txt"
        target.write_text("original", encoding="utf-8")

        ctx = MagicMock()
        ctx.function.name = "bash"
        ctx.function.chrys_kind = "shell"
        ctx.arguments = {"command": "echo 'new' > output.txt", "workdir": str(tmp_path)}
        ctx.result = "ok"

        async def fake_shell():
            target.write_text("new\n", encoding="utf-8")

        await mw.process(ctx, fake_shell)

        changed = tracker.get_changed_files()
        norm = os.path.normpath(os.path.abspath(str(target)))
        assert norm in changed

    async def test_no_tracker_shell_runs_without_error(self, tmp_path: Path) -> None:
        """Shell tools work fine when no mutation tracker is configured."""
        bus = AsyncMock()
        bus.publish = AsyncMock()
        mw = ToolEventMiddleware(bus, mutation_tracker=None, origin=InvocationOrigin("turn", "", "turn-test", None))

        ctx = MagicMock()
        ctx.function.name = "bash"
        ctx.function.chrys_kind = "shell"
        ctx.arguments = {"command": "echo hello"}
        ctx.result = "hello"

        # Should not raise
        await mw.process(ctx, AsyncMock())
