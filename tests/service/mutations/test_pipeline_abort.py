# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Aborted calls settle file endpoints and close their observation windows."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.mutations.pipeline import MutationContext, abort_mutation_tracking
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource


@pytest.mark.parametrize("may_have_run", [False, True])
async def test_abort_file_captures_only_the_appropriate_endpoint(tmp_path: Path, may_have_run: bool) -> None:
    target = tmp_path / "file.txt"
    await asyncio.to_thread(target.write_text, "original", encoding="utf-8")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    row = await asyncio.to_thread(tracker.record, str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call")
    assert row is not None
    await asyncio.to_thread(target.write_text, "disk now", encoding="utf-8")
    coordinator = create_autospec(MutationCoordinator, instance=True)

    abort_mutation_tracking(
        MutationContext(file_mutation=row, window_id="window"), coordinator, tracker=tracker, may_have_run=may_have_run
    )

    assert tracker.store.read_blob(row.after_hash) == (b"disk now" if may_have_run else b"original")
    assert row.after_skip is None
    coordinator.close_window.assert_called_once_with("window")
    assert tracker.get_turn_mutations(1).detection_truncated is False


@pytest.mark.parametrize("may_have_run", [False, True])
def test_abort_shell_only_marks_truncation_if_it_may_have_run(tmp_path: Path, may_have_run: bool) -> None:
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    coordinator = create_autospec(MutationCoordinator, instance=True)

    abort_mutation_tracking(
        MutationContext(implicit_detection=True, window_id="window"),
        coordinator,
        tracker=tracker,
        may_have_run=may_have_run,
    )

    assert tracker.get_turn_mutations(1).detection_truncated is may_have_run
    coordinator.close_window.assert_called_once_with("window")


def test_failed_endpoint_capture_does_not_prevent_window_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    target.write_text("original", encoding="utf-8")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    row = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "call")
    coordinator = create_autospec(MutationCoordinator, instance=True)

    def fail_capture(mutation):
        raise OSError("unavailable")

    monkeypatch.setattr(tracker, "record_after", fail_capture)

    abort_mutation_tracking(MutationContext(file_mutation=row, window_id="window"), coordinator, tracker=tracker)

    coordinator.close_window.assert_called_once_with("window")
