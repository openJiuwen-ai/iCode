# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preview totals use every net snapshot and reuse statistics while endpoints stay unchanged."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from chrys.app.tui.screens.diff.screen import load_diff_entries_by_period
from chrys.app.tui.util.diff_content import PreviewCountCache, compute_preview_counts, read_diff_content
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.service.mutations.store import SnapshotPolicy, SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore


def counted_entries(
    cache: PreviewCountCache, entries: list[DiffFileEntry], directory: Path | None
) -> tuple[DiffFileEntry, ...]:
    cache.fill(entries, directory)
    while (entry := cache.next_pending()) is not None:
        assert cache.accept(compute_preview_counts(entry, directory), directory)
    return cache.entries


def snapshot_entry(
    store: SnapshotStore,
    path: Path,
    before: str | None,
    after: str | None,
    operation: MutationOp = MutationOp.MODIFY,
) -> DiffFileEntry:
    return DiffFileEntry(
        path=str(path),
        rel_path=path.name,
        operation=operation,
        old_path=None,
        before_text="",
        after_text="",
        is_binary=False,
        encoding="utf-8",
        bytes_changed=True,
        before_hash=store.save_data_as_blob(before.encode("utf-8")).content_hash if before is not None else None,
        after_hash=store.save_data_as_blob(after.encode("utf-8")).content_hash if after is not None else None,
    )


@pytest.mark.parametrize(
    ("operation", "before", "after", "expected"),
    [
        (MutationOp.CREATE, None, "one\ntwo\n", (2, 0)),
        (MutationOp.DELETE, "one\ntwo\n", None, (0, 2)),
        (MutationOp.MODIFY, "one\ntwo\nthree\n", "one\nchanged\nthree\nfour\n", (2, 1)),
        (MutationOp.MODIFY, "one\r\ntwo\r\n", "one\ntwo\n", (0, 0)),
        (MutationOp.MODIFY, "one\u2028before\n", "one\u2028after\n", (1, 1)),
        (MutationOp.CREATE, None, "", (0, 0)),
        (MutationOp.MOVE, "one\n", "one\n", (0, 0)),
        (MutationOp.MOVE, None, "one\ntwo\n", (2, 0)),
        (MutationOp.MOVE, "before\n", "after\n", (1, 1)),
    ],
    ids=[
        "create",
        "delete",
        "modify",
        "line-endings-only",
        "unicode-within-one-line",
        "empty-file",
        "move-identical-content",
        "move-new-target",
        "move-and-edit",
    ],
)
def test_snapshot_counts_match_the_diff_view_without_git(
    tmp_path: Path,
    operation: MutationOp,
    before: str | None,
    after: str | None,
    expected: tuple[int, int],
) -> None:
    directory = tmp_path / "session"
    store = SnapshotStore(directory)
    entry = snapshot_entry(store, tmp_path / "file.txt", before, after, operation)

    counted = counted_entries(PreviewCountCache(), [entry], directory)[0]
    content = read_diff_content(store, entry.before_hash, entry.after_hash)
    viewer = DiffView(entry.path, entry.path, content.before_text, content.after_text)

    assert counted.line_counts == viewer.counts == expected
    assert content.available and not content.is_binary
    assert not (tmp_path / ".git").exists()


def test_all_files_contribute_to_totals_and_unchanged_hashes_reuse_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "session"
    store = SnapshotStore(directory)
    entries = [
        snapshot_entry(store, tmp_path / f"file{index}.txt", f"before {index}\n", f"after {index}\n")
        for index in range(11)
    ]
    reads: list[str] = []
    read_blob = SnapshotStore.read_blob

    def track_read(self: SnapshotStore, digest: str) -> bytes | None:
        reads.append(digest)
        return read_blob(self, digest)

    monkeypatch.setattr(SnapshotStore, "read_blob", track_read)
    cache = PreviewCountCache()
    first = counted_entries(cache, entries, directory)
    assert [entry.line_counts for entry in first] == [(1, 1)] * 11
    assert len(reads) == 22
    assert set(reads) == {digest for entry in entries for digest in (entry.before_hash, entry.after_hash)}
    assert sum(entry.line_counts[0] for entry in first if entry.line_counts is not None) == 11
    assert sum(entry.line_counts[1] for entry in first if entry.line_counts is not None) == 11

    reads.clear()
    second = counted_entries(cache, [replace(entry, last_changed_at=50) for entry in entries], directory)
    assert [entry.line_counts for entry in second] == [entry.line_counts for entry in first]
    assert reads == []

    changed = snapshot_entry(store, tmp_path / "file0.txt", "before 0\n", "replacement\nextra\n")
    third = counted_entries(cache, [changed, *entries[1:]], directory)
    assert third[0].line_counts == (2, 1)
    assert [entry.line_counts for entry in third[1:]] == [(1, 1)] * 10
    assert sum(entry.line_counts[0] for entry in third if entry.line_counts is not None) == 12
    assert sum(entry.line_counts[1] for entry in third if entry.line_counts is not None) == 11
    assert set(reads) == {changed.before_hash, changed.after_hash}
    assert len(reads) == 2


def test_stats_describe_session_endpoints_instead_of_summing_turns(tmp_path: Path) -> None:
    state = JsonFileStateStore(tmp_path / "sessions")
    session_id = "non-git-counts"
    directory = state.session_dir(session_id)
    tracker = MutationTracker(SnapshotStore(directory))
    target = tmp_path / "file.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")

    for turn, text in [(1, "one\nchanged\n"), (2, "one\ntwo\nthree\n")]:
        tracker.start_turn(turn)
        mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, f"edit{turn}")
        assert mutation is not None
        target.write_text(text, encoding="utf-8")
        tracker.record_after(mutation)
    (directory / "session.json").write_text(
        json.dumps({"state": {"chrys_mutations": tracker.serialize()}}), encoding="utf-8"
    )

    data = load_diff_entries_by_period(state, session_id, str(tmp_path), include_content=False)
    counted = counted_entries(PreviewCountCache(), data.all_entries, directory)
    assert len(counted) == 1
    assert counted[0].line_counts == (1, 0)
    full = load_diff_entries_by_period(state, session_id, str(tmp_path))
    per_turn = [
        DiffView(entry.path, entry.path, entry.before_text, entry.after_text).counts
        for entries in full.per_period_entries.values()
        for entry in entries
    ]
    assert per_turn == [(1, 1), (2, 1)]


@pytest.mark.parametrize("kind", ["no-backup", "known-binary"])
def test_non_diffable_entries_have_no_numbers_and_read_no_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = tmp_path / "session"
    entry = snapshot_entry(SnapshotStore(directory), tmp_path / "file.txt", "before\n", "after\n")
    entry = replace(entry, content_omitted="too_large") if kind == "no-backup" else replace(entry, is_binary=True)

    def unexpected_read(self: SnapshotStore, digest: str) -> bytes | None:
        raise AssertionError("non-diffable content must not be read to invent statistics")

    monkeypatch.setattr(SnapshotStore, "read_blob", unexpected_read)
    assert counted_entries(PreviewCountCache(), [entry], directory)[0].line_counts is None


def test_old_binary_backup_is_detected_before_producing_counts(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    store = SnapshotStore(directory, policy=SnapshotPolicy(skip_binary=False))
    before = store.save_data_as_blob(b"before\x00binary")
    after = store.save_data_as_blob(b"after\x00binary")
    entry = replace(
        snapshot_entry(store, tmp_path / "file.bin", None, None),
        before_hash=before.content_hash,
        after_hash=after.content_hash,
    )
    assert counted_entries(PreviewCountCache(), [entry], directory)[0].line_counts is None


def test_missing_backup_does_not_turn_unavailable_text_into_a_deletion(tmp_path: Path) -> None:
    directory = tmp_path / "session"
    store = SnapshotStore(directory)
    entry = snapshot_entry(store, tmp_path / "file.txt", "before\n", "after\n")
    assert entry.after_hash is not None
    store.remove_blobs({entry.after_hash})

    assert not read_diff_content(store, entry.before_hash, entry.after_hash).available
    assert counted_entries(PreviewCountCache(), [entry], directory)[0].line_counts is None


def test_live_text_counts_need_neither_a_repository_nor_a_snapshot_store(tmp_path: Path) -> None:
    entry = DiffFileEntry(
        str(tmp_path / "live.py"),
        "live.py",
        MutationOp.MODIFY,
        None,
        "one\nbefore\n",
        "one\nafter\nextra\n",
        False,
        "utf-8",
        True,
    )
    assert counted_entries(PreviewCountCache(), [entry], None)[0].line_counts == (2, 1)


def test_metadata_fill_is_pending_without_reading_snapshots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "session"
    entry = snapshot_entry(SnapshotStore(directory), tmp_path / "file.txt", "before\n", "after\n")

    def unexpected_read(self: SnapshotStore, digest: str) -> bytes | None:
        pytest.fail("publishing the file list must not read snapshots")

    monkeypatch.setattr(SnapshotStore, "read_blob", unexpected_read)
    rows = PreviewCountCache().fill([entry], directory)
    assert rows[0].line_count_result == "pending"
    assert rows[0].line_counts is None


@pytest.mark.parametrize("side", ["before", "after"])
@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("trailing_break", [False, True])
def test_over_twenty_thousand_lines_never_calls_the_diff_algorithm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str, ending: str, trailing_break: bool
) -> None:
    from chrys.app.tui.widgets.diff_view import compute

    text = ending.join(["line"] * 20001) + (ending if trailing_break else "")
    before, after = (text, "short\n") if side == "before" else ("short\n", text)
    directory = tmp_path / "session"
    entry = snapshot_entry(SnapshotStore(directory), tmp_path / "big.txt", before, after)

    def unexpected_diff(before: str, after: str):
        pytest.fail("a file exceeding 20000 lines must not enter difflib")

    monkeypatch.setattr(compute, "compute_hunks", unexpected_diff)
    cache = PreviewCountCache()
    counted = counted_entries(cache, [entry], directory)[0]
    assert counted.line_counts is None
    assert counted.line_count_result == "large"
    assert cache.fill([entry], directory)[0].line_count_result == "large"
    assert cache.next_pending() is None


@pytest.mark.parametrize("ending", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("trailing_break", [False, True])
def test_exactly_twenty_thousand_lines_are_counted(tmp_path: Path, ending: str, trailing_break: bool) -> None:
    text = ending.join(["line"] * 20000) + (ending if trailing_break else "")
    entry = snapshot_entry(SnapshotStore(tmp_path), tmp_path / "limit.txt", None, text, MutationOp.CREATE)
    result = compute_preview_counts(entry, tmp_path)
    assert result.line_counts == (20000, 0)


def test_unicode_separators_and_long_lines_follow_the_full_viewer_not_splitlines(tmp_path: Path) -> None:
    entry = snapshot_entry(SnapshotStore(tmp_path), tmp_path / "long.txt", None, "word\u2028" * 30000)
    assert compute_preview_counts(entry, tmp_path).line_counts == (1, 0)


def test_live_endpoint_text_is_part_of_cache_identity(tmp_path: Path) -> None:
    entry = replace(snapshot_entry(SnapshotStore(tmp_path), tmp_path / "live.txt", None, None), after_text="one\n")
    cache = PreviewCountCache()
    assert counted_entries(cache, [entry], None)[0].line_counts == (1, 0)
    changed = replace(entry, after_text="one\ntwo\n")
    assert cache.fill([changed], None)[0].line_count_result == "pending"
    assert counted_entries(cache, [changed], None)[0].line_counts == (2, 0)


def test_unavailable_results_are_cached_but_removed_entries_are_forgotten(tmp_path: Path) -> None:
    entry = snapshot_entry(SnapshotStore(tmp_path), tmp_path / "missing.txt", "before", "after")
    store = SnapshotStore(tmp_path)
    assert entry.after_hash is not None
    store.remove_blobs({entry.after_hash})
    cache = PreviewCountCache()
    result = counted_entries(cache, [entry], tmp_path)[0]
    assert result.line_counts is None
    assert result.line_count_result == "unavailable"
    assert cache.fill([entry], tmp_path)[0].line_count_result == "unavailable"
    cache.fill([], tmp_path)
    assert cache.fill([entry], tmp_path)[0].line_count_result == "pending"


def test_same_hash_pair_in_a_different_snapshot_directory_needs_a_new_count(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    a = snapshot_entry(SnapshotStore(first), tmp_path / "file.txt", "before\n", "after\n")
    b = snapshot_entry(SnapshotStore(second), tmp_path / "file.txt", "before\n", "after\n")
    cache = PreviewCountCache()
    counted_entries(cache, [a], first)
    assert cache.fill([b], second)[0].line_count_result == "pending"
