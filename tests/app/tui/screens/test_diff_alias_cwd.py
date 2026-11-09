# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Diff and rollback views retain local labels for an aliased workspace cwd."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.app.tui.screens.diff.rollback_modal import _build_cumulative_entries, _exclusion_note_text
from chrys.app.tui.screens.diff.screen import _build_period_entries, _build_session_entries
from chrys.app.tui.screens.main.live_diff import LiveFileMutation, build_live_diff_entries
from chrys.app.tui.util.diff_labels import is_within_cwd
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource, RollbackExclusionReason


@pytest.mark.parametrize("deleted", [False, True])
def test_diff_and_rollback_keep_physical_entries_inside_alias_cwd(tmp_path: Path, deleted: bool) -> None:
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks unavailable")
    target = real / "file.txt"
    target.write_bytes(b"original")
    tracker = MutationTracker(SnapshotStore(tmp_path / "session"))
    tracker.start_turn(1)
    mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "edit")
    assert mutation is not None
    if deleted:
        target.unlink()
    else:
        target.write_bytes(b"modified")
    tracker.record_after(mutation)
    turn = tracker.get_turn_mutations(1)
    assert turn is not None
    live = LiveFileMutation("original", "" if deleted else "modified", "delete" if deleted else "modify", True)
    views = [
        _build_period_entries(tracker, turn, str(alias)),
        _build_session_entries(tracker, str(alias)),
        _build_cumulative_entries(tracker, 1, str(alias)),
        build_live_diff_entries({str(target): live}, str(alias)),
    ]
    for entries in views:
        assert len(entries) == 1
        assert entries[0].path == str(target)
        assert entries[0].rel_path == "file.txt"
        assert is_within_cwd(entries[0].rel_path)
    note = _exclusion_note_text([(str(target), RollbackExclusionReason.UNRESTORABLE)], str(alias))
    assert "file.txt" in note and "../" not in note and str(real) not in note
