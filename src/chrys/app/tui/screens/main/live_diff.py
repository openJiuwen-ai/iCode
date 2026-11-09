# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live /diff model helpers for the main TUI screen."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from chrys.app.tui.util.mutation_sources import merge_mutation_source
from chrys.foundation.platform.paths import display_relpath
from chrys.service.mutations.types import MutationOp

if TYPE_CHECKING:
    from chrys.app.tui.util.diff_entries import DiffFileEntry


LiveMutationOp = Literal["create", "modify", "delete", "move"]

_OP_STR_TO_MUTATION_OP: dict[str, MutationOp] = {
    "create": MutationOp.CREATE,
    "modify": MutationOp.MODIFY,
    "delete": MutationOp.DELETE,
    "move": MutationOp.MOVE,
}


@dataclass(frozen=True)
class LiveFileMutation:
    """Accumulated live /diff state for one file path."""

    before_text: str
    after_text: str
    operation: LiveMutationOp
    bytes_changed: bool
    before_hash: str | None = None
    after_hash: str | None = None
    source: str = ""
    # SnapshotSkipReason value ("too_large" / "binary") when a side's
    # content backup was withheld by SnapshotPolicy — texts/hashes are
    # then empty/None without meaning "no content".  "" = content
    # available.
    content_omitted: str = ""
    # MutationProvenance value of the underlying mutation(s); "" for
    # legacy metadata.  Foreign rows are filtered before reaching the
    # live model, so this is "proven" / "assumed" in practice.
    provenance: str = ""
    contested: bool = False
    last_changed_at: float = 0


def merge_provenance(current: str, latest: str) -> str:
    """Merge provenance values across collapsed live rows, sticky-assumed."""
    from chrys.service.mutations.types import MutationProvenance

    assumed = MutationProvenance.ASSUMED.value
    if current == assumed or latest == assumed:
        return assumed
    return latest or current


def infer_live_op(before_text: str, op_str: str | None) -> LiveMutationOp:
    """Infer the mutation op used by live /diff accumulation."""
    match op_str:
        case "create" | "modify" | "delete" | "move":
            return op_str
        case _:
            return "create" if not before_text else "modify"


def mutation_op_for_live_op(operation: str | None) -> MutationOp:
    """Return the persisted diff operation for a live operation string."""
    return _OP_STR_TO_MUTATION_OP.get(operation or "", MutationOp.MODIFY)


def merge_live_mutation(
    original: LiveFileMutation,
    latest: LiveFileMutation,
) -> LiveFileMutation | None:
    """Return the net live mutation, or ``None`` when it cancels out."""
    existed_at_start = original.operation != "create"
    exists_at_end = latest.operation != "delete"

    if not existed_at_start and not exists_at_end:
        return None

    if not existed_at_start and exists_at_end:
        net_op = "create"
    elif existed_at_start and not exists_at_end:
        net_op = "delete"
    else:
        # Once a MOVE is followed by another live mutation, the net row
        # is treated as a content/state change at the destination path.
        net_op = "modify"

    if original.before_hash is not None or latest.after_hash is not None:
        net_bytes_changed = original.before_hash != latest.after_hash
    elif original.before_text != latest.after_text:
        net_bytes_changed = True
    elif latest.before_text != latest.after_text:
        # Text changed during this mutation but returned to the original
        # decoded content. Without hashes, preserve the historical behavior:
        # treat it as a net-zero live edit.
        net_bytes_changed = False
    else:
        net_bytes_changed = original.bytes_changed or latest.bytes_changed

    if original.before_text == latest.after_text and not net_bytes_changed:
        return None

    return LiveFileMutation(
        before_text=original.before_text,
        after_text=latest.after_text,
        operation=net_op,
        bytes_changed=net_bytes_changed,
        before_hash=original.before_hash,
        after_hash=latest.after_hash,
        source=merge_mutation_source(original.source, latest.source),
        # Sticky: once either side of the net view relies on withheld
        # content, the merged texts stay untruthful.
        content_omitted=latest.content_omitted or original.content_omitted,
        # Sticky-assumed: once the net view includes an inferred delta,
        # the merged row stays inferred.  Contested is sticky-True.
        provenance=merge_provenance(original.provenance, latest.provenance),
        contested=original.contested or latest.contested,
        last_changed_at=max(original.last_changed_at, latest.last_changed_at),
    )


def build_live_diff_entries(
    live_mutations: Mapping[str, LiveFileMutation],
    cwd: str,
) -> list[DiffFileEntry]:
    """Build visible diff viewer entries from accumulated live mutations."""
    from chrys.app.tui.util.diff_entries import DiffFileEntry, entry_has_visible_change

    entries: list[DiffFileEntry] = []
    for path, mutation in live_mutations.items():
        rel_path = display_relpath(path, cwd)
        entry = DiffFileEntry(
            path=path,
            rel_path=rel_path,
            operation=mutation_op_for_live_op(mutation.operation),
            old_path=None,
            before_text=mutation.before_text,
            after_text=mutation.after_text,
            is_binary=False,
            encoding="utf-8",
            bytes_changed=mutation.bytes_changed,
            before_hash=mutation.before_hash,
            after_hash=mutation.after_hash,
            source=mutation.source,
            content_omitted=mutation.content_omitted,
            contested=mutation.contested,
            inferred=mutation.provenance == "assumed",
            last_changed_at=mutation.last_changed_at,
        )
        if entry_has_visible_change(entry):
            entries.append(entry)
    entries.sort(key=lambda entry: entry.rel_path)
    return entries


def merge_live_entries_into_all(
    all_entries: Sequence[DiffFileEntry],
    live_entries: Sequence[DiffFileEntry],
) -> list[DiffFileEntry]:
    """Merge current-turn live entries into the session-wide diff entries."""
    from chrys.app.tui.util.diff_entries import DiffFileEntry

    merged_entries = list(all_entries)
    by_path = {entry.path: index for index, entry in enumerate(merged_entries)}
    dropped_indices: set[int] = set()
    for live in live_entries:
        # A fully-skipped MODIFY (both backups withheld, existence
        # unchanged) has no provable net change — the tracker's session
        # summary drops exactly this case, so keep the live All view
        # consistent with the persisted All tab after the turn
        # completes.  Skipped creates/deletes stay: their existence
        # change is a real net change even with (None, None) hashes.
        if (
            live.content_omitted
            and live.before_hash is None
            and live.after_hash is None
            and live.operation is MutationOp.MODIFY
        ):
            continue

        idx = by_path.get(live.path)
        if idx is None:
            by_path[live.path] = len(merged_entries)
            merged_entries.append(live)
            continue

        existing = merged_entries[idx]
        existed_at_start = existing.operation != MutationOp.CREATE
        exists_at_end = live.operation != MutationOp.DELETE

        # Mirror the tracker's skip-aware ``is_net_zero``: an existence
        # flip is a provable net change even when withheld backups leave
        # both hashes None; with existence unchanged, hash inequality
        # decides, and the both-hashes-None case (fully-skipped MODIFY,
        # or an existence bounce-back like skipped CREATE → DELETE)
        # falls back to text equality — no provable change nets out,
        # keeping the live All view consistent with the persisted All
        # tab after the turn completes.
        if existed_at_start != exists_at_end:
            net_bytes_changed = True
        elif existing.before_hash is not None or live.after_hash is not None:
            net_bytes_changed = existing.before_hash != live.after_hash
        else:
            net_bytes_changed = existing.before_text != live.after_text

        if not net_bytes_changed:
            dropped_indices.add(idx)
            continue

        if not existed_at_start and exists_at_end:
            net_op = MutationOp.CREATE
        elif existed_at_start and not exists_at_end:
            net_op = MutationOp.DELETE
        else:
            net_op = MutationOp.MODIFY

        merged_entries[idx] = DiffFileEntry(
            path=existing.path,
            rel_path=existing.rel_path,
            operation=net_op,
            old_path=existing.old_path,
            before_text=existing.before_text,
            after_text=live.after_text,
            is_binary=existing.is_binary or live.is_binary,
            encoding=existing.encoding,
            bytes_changed=net_bytes_changed,
            before_hash=existing.before_hash,
            after_hash=live.after_hash,
            source=merge_mutation_source(existing.source, live.source),
            content_omitted=live.content_omitted or existing.content_omitted,
            contested=existing.contested or live.contested,
            inferred=existing.inferred or live.inferred,
            last_changed_at=max(existing.last_changed_at, live.last_changed_at),
        )

    if dropped_indices:
        merged_entries = [entry for index, entry in enumerate(merged_entries) if index not in dropped_indices]
    merged_entries.sort(key=lambda entry: entry.rel_path)
    return merged_entries
