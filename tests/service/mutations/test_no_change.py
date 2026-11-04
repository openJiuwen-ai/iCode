# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Only final, identical endpoints without a move prove that a mutation changed nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import FileMutation, MutationOp, MutationSource, SnapshotSkipReason


@pytest.mark.parametrize(
    ("operation", "before", "after", "old_path", "before_skip", "after_skip", "expected"),
    [
        (MutationOp.MODIFY, "same", "same", None, None, None, True),
        (MutationOp.CREATE, None, None, None, None, None, True),
        (MutationOp.CREATE, None, "created", None, None, None, False),
        (MutationOp.DELETE, "deleted", None, None, None, None, False),
        (MutationOp.MODIFY, "before", "after", None, None, None, False),
        (MutationOp.MOVE, "same", "same", "old.txt", None, None, False),
        (MutationOp.MODIFY, "same", "same", None, SnapshotSkipReason.BINARY, None, False),
        (MutationOp.MODIFY, "same", "same", None, None, SnapshotSkipReason.TOO_LARGE, False),
        (MutationOp.MODIFY, None, None, None, SnapshotSkipReason.UNREADABLE, SnapshotSkipReason.UNREADABLE, False),
    ],
    ids=[
        "identical-content",
        "creation-never-happened",
        "created",
        "deleted",
        "modified",
        "moved-without-content-change",
        "before-backup-withheld",
        "after-backup-withheld",
        "both-endpoints-unknown",
    ],
)
@pytest.mark.parametrize("settled", [True, False], ids=["settled", "unsettled"])
def test_mutation_only_proves_no_change_with_final_identical_endpoints(
    tmp_path: Path,
    operation: MutationOp,
    before: str | None,
    after: str | None,
    old_path: str | None,
    before_skip: SnapshotSkipReason | None,
    after_skip: SnapshotSkipReason | None,
    expected: bool,
    settled: bool,
) -> None:
    mutation = FileMutation(
        path=str(tmp_path / "file.txt"),
        operation=operation,
        source=MutationSource.EDIT_FILE,
        tool_call_id="call",
        timestamp=10,
        old_path=str(tmp_path / old_path) if old_path is not None else None,
        before_hash=before,
        after_hash=after,
        before_skip=before_skip,
        after_skip=after_skip,
        settled=settled,
    )
    assert mutation.endpoints_match is expected
    # An unsettled endpoint (an interrupted writer, an older session) proves nothing.
    assert mutation.proves_no_change is (expected and settled)
    assert FileMutation.from_dict(mutation.to_dict()) == mutation


@pytest.mark.parametrize(
    ("before", "after", "t_end", "expected"),
    [("before", "after", 20.0, 20.0), ("before", "after", None, 10.0), ("same", "same", 30.0, None)],
    ids=["write-interval-end", "legacy-record-time", "no-op-has-no-change-time"],
)
def test_changed_at_only_reports_a_real_or_unresolved_change(
    tmp_path: Path, before: str, after: str, t_end: float | None, expected: float | None
) -> None:
    mutation = FileMutation(
        path=str(tmp_path / "file.txt"),
        operation=MutationOp.MODIFY,
        source=MutationSource.EDIT_FILE,
        tool_call_id="call",
        timestamp=10.0,
        t_end=t_end,
        before_hash=before,
        after_hash=after,
    )
    assert mutation.changed_at == expected


def test_interrupted_create_from_an_older_session_is_still_deleted_by_rollback(tmp_path: Path) -> None:
    target = tmp_path / "new.txt"
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    row = tracker.record(str(target), MutationOp.CREATE, MutationSource.WRITE_FILE, "call")
    assert row is not None
    # Older sessions never read an interrupted call's endpoint, nor marked a row settled.
    target.write_text("created by the interrupted writer", encoding="utf-8")
    data = tracker.serialize()
    (saved,) = data["turns"][0]["mutations"]
    assert "after_hash" not in saved and "settled" not in saved
    restored = MutationTracker.deserialize(data, SnapshotStore(tmp_path / "session"))
    plan = restored.get_rollback_plan(1)
    assert plan.paths == {str(target)}
    ((_, snapshot),) = plan.entries
    assert snapshot.existed is False
