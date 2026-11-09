# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The lightweight preview loader uses the diff viewer's exact display rules."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.app.tui.screens.diff.screen import load_diff_entries_by_period
from chrys.app.tui.screens.main.diff_controller import sorted_preview_entries
from chrys.app.tui.util.diff_labels import (
    entry_display_key,
    entry_label_parts,
    entry_source_suffix,
    filter_and_dedupe_entries,
)
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore


def test_metadata_loading_matches_full_diff_without_reading_snapshot_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    sid = "preview"
    tracker = MutationTracker(SnapshotStore(store.session_dir(sid)))
    target = tmp_path / "modify.py"
    target.write_text("before\n", encoding="utf-8")
    tracker.start_turn(1)
    for index, (path, content) in enumerate([(target, "after\n"), (tmp_path / "create.py", "created\n")]):
        mutation = tracker.record(
            str(path), MutationOp.MODIFY if path.exists() else MutationOp.CREATE, MutationSource.WRITE_FILE, str(path)
        )
        assert mutation is not None
        path.write_text(content, encoding="utf-8")
        tracker.record_after(mutation)
        mutation.timestamp = 10.0 + index * 10
        mutation.t_end = mutation.timestamp + 1
    tracker.start_turn(2)
    mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "again")
    assert mutation is not None
    target.write_text("latest\n", encoding="utf-8")
    tracker.record_after(mutation)
    mutation.timestamp = 30.0
    mutation.t_end = 40.0
    folder = store.session_dir(sid)
    (folder / "session.json").write_text(
        json.dumps({"state": {"chrys_mutations": tracker.serialize()}}), encoding="utf-8"
    )
    full = load_diff_entries_by_period(store, sid, str(tmp_path))

    def no_blobs(self, digest):
        raise AssertionError("preview must not read blob content")

    monkeypatch.setattr(SnapshotStore, "read_blob", no_blobs)
    lightweight = load_diff_entries_by_period(store, sid, str(tmp_path), include_content=False)

    def displayed(entries):
        return [
            (
                entry_display_key(row),
                entry_label_parts(row)[0].plain,
                entry_label_parts(row)[1].plain,
                entry_source_suffix(row),
                row.before_hash,
                row.after_hash,
                row.last_changed_at,
            )
            for row in filter_and_dedupe_entries(entries)
        ]

    assert lightweight.total_periods == full.total_periods == 2
    assert {row.rel_path: row.last_changed_at for row in lightweight.all_entries} == {
        "create.py": 21.0,
        "modify.py": 40.0,
    }
    assert displayed(lightweight.all_entries) == displayed(full.all_entries)
    assert {period: displayed(rows) for period, rows in lightweight.per_period_entries.items()} == {
        period: displayed(rows) for period, rows in full.per_period_entries.items()
    }


@pytest.mark.parametrize("include_content", [False, True])
def test_no_change_row_does_not_make_an_older_file_the_most_recent(tmp_path: Path, include_content: bool) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    session_id = "neutral-row-order"
    tracker = MutationTracker(SnapshotStore(store.session_dir(session_id)))
    paths = [tmp_path / "a.py", tmp_path / "b.py"]
    for path in paths:
        path.write_text("before\n", encoding="utf-8")

    tracker.start_turn(1)
    for path, changed_at in zip(paths, [11.0, 21.0], strict=True):
        mutation = tracker.record(str(path), MutationOp.MODIFY, MutationSource.EDIT_FILE, path.name)
        assert mutation is not None
        path.write_text("after\n", encoding="utf-8")
        tracker.record_after(mutation)
        mutation.timestamp = changed_at - 1
        mutation.t_end = changed_at

    neutral = tracker.record(str(paths[0]), MutationOp.MODIFY, MutationSource.WRITE_FILE, "unexecuted")
    assert neutral is not None
    neutral.after_hash = neutral.before_hash
    neutral.after_skip = neutral.before_skip
    neutral.timestamp = 30.0
    neutral.t_end = 31.0
    (store.session_dir(session_id) / "session.json").write_text(
        json.dumps({"state": {"chrys_mutations": tracker.serialize()}}), encoding="utf-8"
    )

    data = load_diff_entries_by_period(store, session_id, str(tmp_path), include_content=include_content)
    assert [entry.rel_path for entry in sorted_preview_entries(data)] == ["b.py", "a.py"]
    expected_times = {"a.py": 11.0, "b.py": 21.0}
    assert {entry.rel_path: entry.last_changed_at for entry in data.all_entries} == expected_times
    assert {entry.rel_path: entry.last_changed_at for entry in data.per_period_entries[1]} == expected_times
    # Neutral rows still contribute display metadata; only their time is ignored.
    assert next(entry for entry in data.all_entries if entry.rel_path == "a.py").source == "write_file"
    assert next(entry for entry in data.per_period_entries[1] if entry.rel_path == "a.py").source == "write_file"
