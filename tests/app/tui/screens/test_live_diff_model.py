# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the main-screen live /diff model."""

from __future__ import annotations

from dataclasses import replace

from chrys.app.tui.screens.diff import DiffFileEntry
from chrys.app.tui.screens.main.live_diff import (
    LiveFileMutation,
    build_live_diff_entries,
    merge_live_entries_into_all,
    merge_live_mutation,
    mutation_op_for_live_op,
)
from chrys.service.mutations.types import MutationOp, MutationSource


def _live(
    before_text: str,
    after_text: str,
    operation: str,
    *,
    bytes_changed: bool = True,
    before_hash: str | None = None,
    after_hash: str | None = None,
    source: str = "",
    content_omitted: str = "",
) -> LiveFileMutation:
    return LiveFileMutation(
        before_text=before_text,
        after_text=after_text,
        operation=operation,
        bytes_changed=bytes_changed,
        before_hash=before_hash,
        after_hash=after_hash,
        source=source,
        content_omitted=content_omitted,
    )


def _entry(
    path: str,
    before_text: str,
    after_text: str,
    operation: MutationOp = MutationOp.MODIFY,
    *,
    bytes_changed: bool = True,
    before_hash: str | None = None,
    after_hash: str | None = None,
    source: str = "",
    is_binary: bool = False,
    content_omitted: str = "",
) -> DiffFileEntry:
    return DiffFileEntry(
        path=path,
        rel_path=path.removeprefix("/repo/"),
        operation=operation,
        old_path=None,
        before_text=before_text,
        after_text=after_text,
        is_binary=is_binary,
        encoding="utf-8",
        bytes_changed=bytes_changed,
        before_hash=before_hash,
        after_hash=after_hash,
        source=source,
        content_omitted=content_omitted,
    )


def test_merge_live_mutation_removes_create_then_delete() -> None:
    original = _live("", "generated", "create")
    latest = _live("generated", "", "delete")

    assert merge_live_mutation(original, latest) is None


def test_merge_live_mutation_keeps_metadata_only_change() -> None:
    original = _live(
        "same\n",
        "same\n",
        "modify",
        bytes_changed=True,
        before_hash="hash-with-bom",
        after_hash="hash-without-bom",
    )
    latest = _live(
        "same\n",
        "same\n",
        "modify",
        bytes_changed=True,
        before_hash="hash-without-bom",
        after_hash="hash-without-bom-plus-mode",
    )

    merged = merge_live_mutation(original, latest)

    assert merged == _live(
        "same\n",
        "same\n",
        "modify",
        bytes_changed=True,
        before_hash="hash-with-bom",
        after_hash="hash-without-bom-plus-mode",
    )


def test_mutation_op_for_live_op_defaults_unknown_to_modify() -> None:
    assert mutation_op_for_live_op("create") is MutationOp.CREATE
    assert mutation_op_for_live_op("move") is MutationOp.MOVE
    assert mutation_op_for_live_op("unknown") is MutationOp.MODIFY
    assert mutation_op_for_live_op(None) is MutationOp.MODIFY


def test_live_change_time_survives_collapsing_and_session_overlay() -> None:
    first = replace(_live("original", "middle", "modify"), last_changed_at=10)
    second = replace(_live("middle", "final", "modify"), last_changed_at=20)
    merged = merge_live_mutation(first, second)
    assert merged is not None
    assert merged.last_changed_at == 20

    live = build_live_diff_entries({"/repo/a.py": merged}, "/repo")[0]
    assert live.last_changed_at == 20
    for persisted_time in (10, 30):
        persisted = replace(_entry("/repo/a.py", "original", "middle"), last_changed_at=persisted_time)
        net = merge_live_entries_into_all([persisted], [live])[0]
        assert net.last_changed_at == max(persisted_time, 20)
        assert (net.before_text, net.after_text) == ("original", "final")


def test_build_live_diff_entries_filters_invisible_entries_and_sorts() -> None:
    entries = build_live_diff_entries(
        {
            "/repo/z.py": _live("same", "same", "modify", bytes_changed=False),
            "/repo/b.py": _live("old", "old", "move", bytes_changed=False),
            "/repo/a.py": _live("", "new", "create"),
        },
        "/repo",
    )

    assert [entry.rel_path for entry in entries] == ["a.py", "b.py"]
    assert [entry.operation for entry in entries] == [MutationOp.CREATE, MutationOp.MOVE]


def test_merge_live_entries_into_all_drops_entry_reverted_to_session_original() -> None:
    existing = _entry(
        "/repo/Power.yaml",
        "A\n",
        "B\n",
        bytes_changed=True,
        before_hash="hash-A",
        after_hash="hash-B",
    )
    live = _entry(
        "/repo/Power.yaml",
        "B\n",
        "A\n",
        bytes_changed=True,
        before_hash="hash-B",
        after_hash="hash-A",
    )

    assert merge_live_entries_into_all([existing], [live]) == []


def test_merge_live_entries_into_all_preserves_session_before_text_and_latest_source() -> None:
    existing = _entry(
        "/repo/Power.yaml",
        "A\n",
        "B\n",
        source=MutationSource.WRITE_FILE.value,
    )
    live = _entry(
        "/repo/Power.yaml",
        "B\n",
        "C\n",
        source=MutationSource.EDIT_FILE.value,
    )

    merged = merge_live_entries_into_all([existing], [live])

    assert len(merged) == 1
    assert merged[0].before_text == "A\n"
    assert merged[0].after_text == "C\n"
    assert merged[0].source == MutationSource.EDIT_FILE.value


def test_merge_live_entries_into_all_preserves_implicit_source() -> None:
    existing = _entry(
        "/repo/Power.yaml",
        "A\n",
        "B\n",
        source=MutationSource.IMPLICIT.value,
    )
    live = _entry(
        "/repo/Power.yaml",
        "B\n",
        "C\n",
        source=MutationSource.EDIT_FILE.value,
    )

    merged = merge_live_entries_into_all([existing], [live])

    assert len(merged) == 1
    assert merged[0].source == MutationSource.IMPLICIT.value


def test_merge_live_entries_into_all_appends_new_live_entries_and_sorts() -> None:
    existing = _entry("/repo/z.py", "old", "new")
    live = _entry("/repo/a.py", "", "new", MutationOp.CREATE)

    merged = merge_live_entries_into_all([existing], [live])

    assert [entry.rel_path for entry in merged] == ["a.py", "z.py"]


# ---------------------------------------------------------------------------
# SnapshotPolicy-skipped content in the live view
# ---------------------------------------------------------------------------


def test_build_live_diff_entries_keeps_skipped_edit_visible() -> None:
    """A skipped edit has ("", "") texts and (None, None) hashes; the
    skip-aware bytes_changed=True plus content_omitted keep it visible."""
    entries = build_live_diff_entries(
        {"/repo/big.bin": _live("", "", "modify", bytes_changed=True, content_omitted="too_large")},
        "/repo",
    )

    assert [entry.rel_path for entry in entries] == ["big.bin"]
    assert entries[0].content_omitted == "too_large"


def test_merge_live_mutation_content_omitted_is_sticky() -> None:
    """Once either accumulated side relies on withheld content, the net
    view stays marked — and equal empty texts must not cancel it out."""
    original = _live("", "", "modify", bytes_changed=True, content_omitted="binary")
    latest = _live("", "", "modify", bytes_changed=True, content_omitted="")

    merged = merge_live_mutation(original, latest)

    assert merged is not None
    assert merged.content_omitted == "binary"
    assert merged.bytes_changed is True


def test_merge_live_entries_into_all_drops_fully_skipped_modify_only() -> None:
    """A fully-skipped MODIFY has no provable net change — the persisted
    All tab nets it out after the turn completes, so the live merge must
    not add it either.  Skipped creates/deletes flip existence — a real
    net change — and must stay."""
    modify = _entry("/repo/big.bin", "", "", MutationOp.MODIFY, bytes_changed=True, content_omitted="too_large")
    create = _entry("/repo/new.bin", "", "", MutationOp.CREATE, bytes_changed=True, content_omitted="too_large")
    delete = _entry("/repo/old.bin", "", "", MutationOp.DELETE, bytes_changed=True, content_omitted="too_large")

    merged = merge_live_entries_into_all([], [modify, create, delete])

    assert [entry.rel_path for entry in merged] == ["new.bin", "old.bin"]
    assert all(entry.content_omitted == "too_large" for entry in merged)


def test_merge_live_entries_into_all_marks_merged_entry_when_live_side_skipped() -> None:
    """Existing restorable content + live skip-marked update (file grew
    or became binary): the merged row must not be dropped by the
    None-hash comparison, and it carries the skip marker."""
    existing = _entry("/repo/notes.txt", "small", "bigger", before_hash="hash-a", after_hash="hash-b")
    live = _entry("/repo/notes.txt", "bigger", "", before_hash="hash-b", content_omitted="too_large")

    merged = merge_live_entries_into_all([existing], [live])

    assert len(merged) == 1
    assert merged[0].content_omitted == "too_large"
    assert merged[0].bytes_changed is True
    assert merged[0].before_text == "small"


def test_merge_live_entries_into_all_drops_skipped_existence_bounce_back() -> None:
    """Existence returning to the session original nets to zero even
    when every hash is None due to withheld backups: skipped CREATE
    (session) + skipped DELETE (live) is absent → absent; the reverse is
    the fully-skipped MODIFY analogue (exists → exists, nothing
    provable).  The persisted All tab shows neither after the turn
    completes, so the live merge must drop both."""
    created = _entry("/repo/big.bin", "", "", MutationOp.CREATE, bytes_changed=True, content_omitted="too_large")
    deleted = _entry("/repo/big.bin", "", "", MutationOp.DELETE, bytes_changed=True, content_omitted="too_large")

    assert merge_live_entries_into_all([created], [deleted]) == []
    assert merge_live_entries_into_all([deleted], [created]) == []


def test_merge_live_entries_into_all_keeps_skipped_create_then_skipped_modify() -> None:
    """A skipped CREATE followed by a skipped MODIFY still flips
    existence vs the session start — the net CREATE row must survive
    the merge with its skip marker."""
    created = _entry("/repo/big.bin", "", "", MutationOp.CREATE, bytes_changed=True, content_omitted="too_large")
    modified = _entry("/repo/big.bin", "", "", MutationOp.MODIFY, bytes_changed=True, content_omitted="too_large")

    merged = merge_live_entries_into_all([created], [modified])

    assert len(merged) == 1
    assert merged[0].operation is MutationOp.CREATE
    assert merged[0].content_omitted == "too_large"
