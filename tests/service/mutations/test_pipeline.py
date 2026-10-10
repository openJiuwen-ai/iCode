# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the mutation pipeline: snapshot reads, finalize_mutation_tracking, and the per-file lock."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from chrys.service.mutations.pipeline import (
    MutationContext,
    _read_file_snapshot,
    _read_shell_snapshots,
    finalize_mutation_tracking,
    prepare_mutation_tracking,
)
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import (
    FileHashDiff,
    FileMutation,
    FileMutationTextSnapshot,
    MutationOp,
    MutationSource,
)

# ──────────────── _read_file_snapshot ─────────────────────────────────


def _make_mutation(
    path: str = "/tmp/f.py",
    op: MutationOp = MutationOp.MODIFY,
    before_hash: str | None = "aaa",
    after_hash: str | None = "bbb",
    source: MutationSource = MutationSource.SHELL,
) -> FileMutation:
    return FileMutation(
        path=path,
        operation=op,
        source=source,
        tool_call_id="call-1",
        timestamp=0.0,
        before_hash=before_hash,
        after_hash=after_hash,
    )


def _make_tracker(blobs: dict[str, bytes]) -> SimpleNamespace:
    """Return a minimal tracker-like object with a fake store."""
    store = SimpleNamespace(read_blob=blobs.get)
    return SimpleNamespace(store=store)


def test_read_file_snapshot_basic() -> None:
    tracker = _make_tracker({"aaa": b"before content", "bbb": b"after content"})
    mutation = _make_mutation()
    result = _read_file_snapshot(tracker, mutation)
    assert result == ("before content", "after content")


def test_read_file_snapshot_no_before_hash() -> None:
    tracker = _make_tracker({"bbb": b"after"})
    mutation = _make_mutation(before_hash=None)
    result = _read_file_snapshot(tracker, mutation)
    assert result == ("", "after")


def test_read_file_snapshot_no_after_hash() -> None:
    tracker = _make_tracker({"aaa": b"before"})
    mutation = _make_mutation(after_hash=None)
    result = _read_file_snapshot(tracker, mutation)
    assert result == ("before", "")


def test_read_file_snapshot_both_none() -> None:
    tracker = _make_tracker({})
    mutation = _make_mutation(before_hash=None, after_hash=None)
    result = _read_file_snapshot(tracker, mutation)
    assert result == ("", "")


def test_read_file_snapshot_blob_missing_returns_empty() -> None:
    tracker = _make_tracker({})  # no blobs at all
    mutation = _make_mutation(before_hash="missing", after_hash="also_missing")
    result = _read_file_snapshot(tracker, mutation)
    assert result == ("", "")


async def test_finalize_mutation_tracking_reports_file_operation(tmp_path) -> None:
    file_path = tmp_path / "empty.txt"
    file_path.write_text("", encoding="utf-8")
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(1)
    mutation = tracker.record(str(file_path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call-1")
    assert mutation is not None

    file_path.write_text("new content", encoding="utf-8")
    result = await finalize_mutation_tracking(tracker, MutationContext(file_mutation=mutation), "call-1")

    assert result.file_snapshot == ("", "new content")
    assert result.file_operation == "modify"
    assert result.file_bytes_changed is True
    assert result.file_hashes == FileHashDiff(before=mutation.before_hash, after=mutation.after_hash)


# ──────────────── _read_shell_snapshots ───────────────────────────────


def test_read_shell_snapshots_single_mutation() -> None:
    tracker = _make_tracker({"aaa": b"old", "bbb": b"new"})
    mutations = [_make_mutation(path="/tmp/x.py")]
    result = _read_shell_snapshots(tracker, mutations)
    assert result == {
        "/tmp/x.py": FileMutationTextSnapshot(
            before_text="old",
            after_text="new",
            operation="modify",
            bytes_changed=True,
            source="shell",
            before_hash="aaa",
            after_hash="bbb",
            provenance="assumed",
        )
    }


def test_read_shell_snapshots_collapse_multi_mutations() -> None:
    """Multiple mutations to the same path should keep the first before_text."""
    tracker = _make_tracker({"h1": b"original", "h2": b"middle", "h3": b"final"})
    mutations = [
        _make_mutation(path="/tmp/x.py", before_hash="h1", after_hash="h2"),
        _make_mutation(path="/tmp/x.py", before_hash="h2", after_hash="h3"),
    ]
    result = _read_shell_snapshots(tracker, mutations)
    assert result["/tmp/x.py"] == FileMutationTextSnapshot(
        before_text="original",
        after_text="final",
        operation="modify",
        bytes_changed=True,
        source="shell",
        before_hash="h1",
        after_hash="h3",
        provenance="assumed",
    )


def test_read_shell_snapshots_preserves_first_op() -> None:
    """Collapsed mutations should keep the operation from the first mutation."""
    tracker = _make_tracker({"h1": b"", "h2": b"v1", "h3": b"v2"})
    mutations = [
        _make_mutation(path="/tmp/x.py", op=MutationOp.CREATE, before_hash="h1", after_hash="h2"),
        _make_mutation(path="/tmp/x.py", op=MutationOp.MODIFY, before_hash="h2", after_hash="h3"),
    ]
    result = _read_shell_snapshots(tracker, mutations)
    assert result["/tmp/x.py"].operation == "create"


def test_read_shell_snapshots_collapse_preserves_implicit_source() -> None:
    """A collapsed path should stay marked implicit if any mutation was implicit."""
    tracker = _make_tracker({"h1": b"original", "h2": b"middle", "h3": b"final"})
    mutations = [
        _make_mutation(path="/tmp/x.py", before_hash="h1", after_hash="h2", source=MutationSource.SHELL),
        _make_mutation(path="/tmp/x.py", before_hash="h2", after_hash="h3", source=MutationSource.IMPLICIT),
    ]
    result = _read_shell_snapshots(tracker, mutations)
    assert result["/tmp/x.py"].source == "implicit"


def test_read_shell_snapshots_multiple_paths() -> None:
    tracker = _make_tracker({"a1": b"A-old", "a2": b"A-new", "b1": b"B-old", "b2": b"B-new"})
    mutations = [
        _make_mutation(path="/tmp/a.py", before_hash="a1", after_hash="a2"),
        _make_mutation(path="/tmp/b.py", before_hash="b1", after_hash="b2"),
    ]
    result = _read_shell_snapshots(tracker, mutations)
    assert len(result) == 2
    assert result["/tmp/a.py"] == FileMutationTextSnapshot(
        before_text="A-old",
        after_text="A-new",
        operation="modify",
        bytes_changed=True,
        source="shell",
        before_hash="a1",
        after_hash="a2",
        provenance="assumed",
    )
    assert result["/tmp/b.py"] == FileMutationTextSnapshot(
        before_text="B-old",
        after_text="B-new",
        operation="modify",
        bytes_changed=True,
        source="shell",
        before_hash="b1",
        after_hash="b2",
        provenance="assumed",
    )


def test_read_shell_snapshots_empty_list() -> None:
    tracker = _make_tracker({})
    result = _read_shell_snapshots(tracker, [])
    assert result == {}


# ──────────────── File lock serialization ───────────────────────────────


async def test_file_lock_serializes_concurrent_edits(tmp_path) -> None:
    """Concurrent edit_file calls to the same file are serialized by file lock.

    Without the lock, asyncio.gather dispatches both record() calls before
    either record_after() runs, breaking the _last_known_hash chain so
    mutation 2 gets the wrong before_hash.  The lock ensures sequential
    execution: record+edit+record_after for edit 1 completes before edit 2
    starts.
    """

    f = tmp_path / "test.py"
    f.write_text("original", encoding="utf-8")

    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(1)

    async def simulate_edit(content: str, call_id: str) -> None:
        """Simulate an edit_file tool call under the file lock."""
        async with tracker.get_file_lock(str(f)):
            m = tracker.record(str(f), MutationOp.MODIFY, MutationSource.EDIT_FILE, call_id)
            # Simulate async tool execution with a yield point
            await asyncio.sleep(0)
            f.write_text(content, encoding="utf-8")
            tracker.record_after(m)

    # Concurrent edits must serialize through MutationTracker.get_file_lock.
    await asyncio.gather(
        simulate_edit("v2", "c1"),
        simulate_edit("v3", "c2"),
    )

    turn = tracker.current_turn
    assert len(turn.mutations) == 2
    m1, m2 = turn.mutations

    # Key assertion: the incremental before_hash chain is correct
    # m1.after == m2.before (not m1.before == m2.before which is the bug)
    assert m1.after_hash == m2.before_hash, "Lock should serialize mutations so m2.before_hash reflects m1's result"


def test_shell_and_calibration_mutations_keep_the_tool_operation(tmp_path) -> None:
    from chrys.service.mutations.pipeline import _record_shell_observations, _ShellObservation

    tracker = MutationTracker(SnapshotStore(tmp_path / "snapshots"))
    tracker.start_turn(1)
    paths = [tmp_path / "shell.txt", tmp_path / "calibrated.txt"]
    for path in paths:
        path.write_text("after\n", encoding="utf-8")
    operation_id = "a" * 32
    mutations = _record_shell_observations(
        tracker,
        [
            _ShellObservation(str(paths[0]), MutationOp.CREATE, None),
            _ShellObservation(str(paths[1]), MutationOp.CREATE, None, calibrated=True),
        ],
        "short-id",
        operation_id,
    )
    assert len(mutations) == 2
    assert {mutation.tool_operation_id for mutation in mutations} == {operation_id}
    assert {mutation.tool_call_id for mutation in mutations} == {"short-id"}
    assert all(mutation.to_dict()["tool_operation_id"] == operation_id for mutation in mutations)


async def test_shell_detected_mutations_keep_the_tool_operation_from_prepare_to_finalize(tmp_path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    target = repo / "file.txt"
    target.write_bytes(b"before")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    operation_id = "b" * 32
    context = await prepare_mutation_tracking(
        tracker,
        "shell",
        {"command": "chmod +x .", "working_dir": str(repo)},
        "shell",
        True,
        str(repo),
        tool_operation_id=operation_id,
    )
    target.write_bytes(b"changed")
    await finalize_mutation_tracking(tracker, context, "shell")
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    assert str(target) in {m.path for m in turn.mutations}
    assert {m.tool_operation_id for m in turn.mutations} == {operation_id}
