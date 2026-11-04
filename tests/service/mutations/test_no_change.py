# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Only known identical endpoints without a move prove that a mutation changed nothing."""

from __future__ import annotations

from pathlib import Path

import pytest

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
def test_mutation_only_proves_no_change_with_known_identical_endpoints(
    tmp_path: Path,
    operation: MutationOp,
    before: str | None,
    after: str | None,
    old_path: str | None,
    before_skip: SnapshotSkipReason | None,
    after_skip: SnapshotSkipReason | None,
    expected: bool,
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
    )
    assert mutation.proves_no_change is expected


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
