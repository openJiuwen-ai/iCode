# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Empty-turn retirement and durable file-tool occurrence references."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import FileSnapshot, MutationLog, MutationOp, MutationSource


def test_replacing_an_empty_turn_leaves_one_ledger_entry_for_its_ordinal(tmp_path: Path) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(1)
    tracker.start_turn(2)

    assert tracker.has_turn_work(2) is False
    tracker.discard_empty_turn(2)
    tracker.start_turn(2)

    assert [turn.turn_id for turn in tracker.get_all_turns()] == [1, 2]
    assert [turn["turn_id"] for turn in tracker.serialize()["turns"]] == [1, 2]


def test_empty_turn_retirement_cannot_discard_file_work(tmp_path: Path) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(1)
    path = tmp_path / "file.txt"
    path.write_text("before", encoding="utf-8")
    mutation = tracker.record(
        str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, "provider-call", tool_operation_id="operation-one"
    )
    assert mutation is not None
    path.write_text("after", encoding="utf-8")
    tracker.record_after(mutation)

    assert tracker.has_turn_work(1) is True
    with pytest.raises(ValueError, match="file work"):
        tracker.discard_empty_turn(1)

    refs = tracker.get_file_edit_snapshot_refs()
    assert len(refs) == 1
    assert refs[0].tool_operation_id == "operation-one"
    assert tracker.store.read_blob(refs[0].before) == b"before"
    assert tracker.store.read_blob(refs[0].after) == b"after"


def test_empty_turn_retirement_cannot_discard_incomplete_detection(tmp_path: Path) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path))
    tracker.start_turn(1)
    tracker.mark_detection_truncated()

    assert tracker.has_turn_work(1) is True
    with pytest.raises(ValueError, match="file work"):
        tracker.discard_empty_turn(1)

    assert tracker.current_turn is not None
    assert tracker.current_turn.detection_truncated is True


def test_snapshot_without_mutation_still_counts_as_turn_work(tmp_path: Path) -> None:
    path = str(tmp_path / "pending.txt")
    snapshot = FileSnapshot(path=path, period_index=2, existed=False)
    tracker = MutationTracker.deserialize(
        {
            "turns": [{"turn_id": 1}, {"turn_id": 2}],
            "snapshots": {MutationLog.snapshot_key(path, 2): snapshot.to_dict()},
        },
        SnapshotStore(tmp_path),
    )

    assert tracker.has_turn_work(1) is False
    assert tracker.has_turn_work(2) is True
    with pytest.raises(ValueError, match="file work"):
        tracker.discard_empty_turn(2)
    assert [turn.turn_id for turn in tracker.get_all_turns()] == [1, 2]
