# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Archive index: one message that stands in for the oldest fold summaries.

Each fold leaves a summary message at the head of the history, and nothing
ever removes one, so a long session would end up with a head made of
summaries.  The head is kept to two bounded parts instead::

    [archive index]         one message, oldest
    [summary ctx_a]
    [summary ctx_b]         in order
    ...
    [history not folded]

When the summaries outgrow their budget, the oldest half of them is merged
into the index, one line per block; when the index outgrows its own budget,
its oldest lines are merged into one range line.  Only the head messages
change: the archived blocks, their messages and their summary texts stay in
``state["compressed_msgs"]``, and every index line names the turns it covers,
which is enough to look a block up again.

The index is a summary message with ``HistoryMarkerKind.ARCHIVE_INDEX_KEY``
holding its entries; a merged summary stays in ``state["messages"]`` flagged
excluded, so the transcript still shows every fold and the fold start is where
it was.  Each merge writes a new index message rather than editing the old
one: a history rollback then restores the old head as a whole.

A merge rewrites the prompt from its first history message on, so it runs only
where a fold has just rewritten everything after the summaries anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.metadata import ensure_analytics_item_id
from chrys.kernel import EXCLUDE_REASON_KEY, EXCLUDED_KEY, Message, set_excluded
from chrys.service.context.providers.fold_summary import first_user_text

if TYPE_CHECKING:
    from collections.abc import Callable

ARCHIVE_INDEX_KEY: Final[str] = HistoryMarkerKind.ARCHIVE_INDEX_KEY

REASON_ARCHIVED: Final[str] = "archive_index"
"""Exclusion reason of a fold summary whose block the archive index lists instead."""

_DETAIL_WINDOW_SHARE = 0.08
"""Share of the context window the fold summaries may take together before the oldest half is merged."""

_INDEX_WINDOW_SHARE = 0.02
"""Share of the context window the archive index may take before its oldest lines are merged."""

_LABEL_CHARS = 80
_BLOCK_ID_KEY = "_block_id"

_HEADER = (
    "[Archive index]\n"
    "Older compressed blocks, oldest first. Their full summaries and messages are kept: "
    "`list_compressed_contexts` with `turn_from`/`turn_to` shows the blocks of a turn range, "
    "and `recall_context` with `turn` queries the block holding that turn."
)


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One index line: a single block, or a run of blocks merged into a range."""

    first_id: str
    last_id: str
    turn_from: int
    turn_to: int
    blocks: int
    label: str = ""

    def line(self) -> str:
        if self.blocks == 1:
            suffix = f" · {self.label}" if self.label else ""
            return f"{self.first_id} · turns {self.turn_from}-{self.turn_to}{suffix}"
        return f"{self.first_id} … {self.last_id} · turns {self.turn_from}-{self.turn_to} · {self.blocks} blocks"

    def to_json(self) -> dict[str, Any]:
        return {
            "first_id": self.first_id,
            "last_id": self.last_id,
            "turn_from": self.turn_from,
            "turn_to": self.turn_to,
            "blocks": self.blocks,
            "label": self.label,
        }

    @classmethod
    def from_json(cls, raw: object) -> ArchiveEntry | None:
        if not isinstance(raw, dict):
            return None
        first_id, last_id, label = raw.get("first_id"), raw.get("last_id"), raw.get("label", "")
        if not (isinstance(first_id, str) and isinstance(last_id, str) and isinstance(label, str)):
            return None
        numbers: list[int] = []
        for key in ("turn_from", "turn_to", "blocks"):
            value = raw.get(key)
            if not isinstance(value, int) or isinstance(value, bool):
                return None
            numbers.append(value)
        return cls(first_id, last_id, numbers[0], numbers[1], numbers[2], label)

    def joined_with(self, later: ArchiveEntry) -> ArchiveEntry:
        return ArchiveEntry(
            first_id=self.first_id,
            last_id=later.last_id,
            turn_from=min(self.turn_from, later.turn_from),
            turn_to=max(self.turn_to, later.turn_to),
            blocks=self.blocks + later.blocks,
        )


@dataclass(frozen=True, slots=True)
class ArchiveMerge:
    """What one merge changed at the head of the history."""

    index: Message
    """The new index message, already in ``state["messages"]``."""
    previous_index: Message | None
    """The index it replaced, no longer in ``state["messages"]``."""
    merged_summaries: tuple[Message, ...]
    """The fold summaries now listed by the index, flagged excluded."""


def is_archive_index(message: Message) -> bool:
    """Whether *message* is the archive index."""
    return isinstance(message.additional_properties.get(ARCHIVE_INDEX_KEY), list)


def is_archived_summary(message: Message) -> bool:
    """Whether *message* is a fold summary the archive index lists instead."""
    return (
        _is_summary(message)
        and not _visible(message)
        and message.additional_properties.get(EXCLUDE_REASON_KEY) == REASON_ARCHIVED
    )


def archive_entries(message: Message) -> list[ArchiveEntry]:
    """The entries of the index *message*; malformed ones are skipped."""
    raw = message.additional_properties.get(ARCHIVE_INDEX_KEY)
    if not isinstance(raw, list):
        return []
    return [entry for item in raw if (entry := ArchiveEntry.from_json(item)) is not None]


def render_archive_index(entries: list[ArchiveEntry]) -> str:
    return "\n".join([_HEADER, *(entry.line() for entry in entries)])


def _is_summary(message: Message) -> bool:
    return message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY


def _visible(message: Message) -> bool:
    return not message.additional_properties.get(EXCLUDED_KEY, False)


def _entry_for(summary: Message, state: dict[str, Any]) -> ArchiveEntry | None:
    """The index line of the block *summary* stands for, or None when the block is gone."""
    block_id = summary.additional_properties.get(_BLOCK_ID_KEY)
    for block in state.get("compressed_msgs", []):
        if block.compressed_context_id == block_id:
            turn_from, turn_to = block.turn_range
            return ArchiveEntry(
                first_id=block.compressed_context_id,
                last_id=block.compressed_context_id,
                turn_from=turn_from,
                turn_to=turn_to,
                blocks=1,
                label=first_user_text(block.messages, _LABEL_CHARS),
            )
    return None


def _fit_index(
    entries: list[ArchiveEntry], budget_tokens: int, count_tokens: Callable[[str], int]
) -> list[ArchiveEntry]:
    """Merge the oldest lines into one range line until the index fits or is a single line."""
    while len(entries) > 1 and count_tokens(render_archive_index(entries)) > budget_tokens:
        # Half of the lines at a time, so the head is not rewritten again on the next merge.
        take = max(2, len(entries) // 2)
        merged = entries[0]
        for entry in entries[1:take]:
            merged = merged.joined_with(entry)
        entries = [merged, *entries[take:]]
    return entries


def merge_fold_summaries(
    state: dict[str, Any],
    *,
    max_context_tokens: int,
    count_tokens: Callable[[str], int],
) -> ArchiveMerge | None:
    """Merge the oldest fold summaries into the archive index when they outgrow their share of the window.

    Rewrites ``state["messages"]`` and returns what changed, or None when
    the summaries fit or the window is unknown.  Call it only where a fold
    already rewrites the head of the history: on its own it would cost a
    prompt-cache miss.
    """
    if max_context_tokens <= 0:
        return None
    detail_budget_tokens = int(max_context_tokens * _DETAIL_WINDOW_SHARE)
    index_budget_tokens = int(max_context_tokens * _INDEX_WINDOW_SHARE)
    messages: list[Message] = state.get("messages", [])
    previous_index = next((m for m in messages if is_archive_index(m) and _visible(m)), None)
    detailed = [m for m in messages if _is_summary(m) and not is_archive_index(m) and _visible(m)]
    sizes = [count_tokens(m.text or "") for m in detailed]
    total = sum(sizes)
    if total <= detail_budget_tokens or not detailed:
        return None

    # The oldest half by size, at least one: merging only the excess would
    # bring the next merge, and its rewritten head, one fold later.  The
    # newest summary always stays: it is the fold the model has just seen.
    merged: list[Message] = []
    merged_tokens = 0
    new_entries: list[ArchiveEntry] = []
    for summary, size in zip(detailed[:-1], sizes[:-1], strict=True):
        if merged and merged_tokens * 2 >= total:
            break
        entry = _entry_for(summary, state)
        if entry is None:
            # No block to point at: keep the summary, it is all that is left of the fold.
            continue
        merged.append(summary)
        merged_tokens += size
        new_entries.append(entry)
    if not merged:
        return None

    entries = archive_entries(previous_index) if previous_index is not None else []
    entries = _fit_index([*entries, *new_entries], index_budget_tokens, count_tokens)

    index = Message("assistant", [render_archive_index(entries)])
    index.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.SUMMARY
    index.additional_properties[ARCHIVE_INDEX_KEY] = [entry.to_json() for entry in entries]
    # A new version is a new item: its text differs from the one it replaces.
    ensure_analytics_item_id(index.additional_properties)

    for summary in merged:
        set_excluded(summary, excluded=True, reason=REASON_ARCHIVED)

    kept = [m for m in messages if m is not previous_index]
    position = next(index_ for index_, m in enumerate(kept) if _is_summary(m))
    state["messages"] = [*kept[:position], index, *kept[position:]]
    return ArchiveMerge(index=index, previous_index=previous_index, merged_summaries=tuple(merged))


def project_archive(state: dict[str, Any], messages: list[Message]) -> bool:
    """Make the request list *messages* show the head ``state["messages"]`` holds; return whether it changed.

    A request list built before a merge still carries the summaries the
    index now lists and, possibly, an older index: those are flagged
    excluded and the current index is put where the head starts.  A list
    that already matches is left alone, and so is one that carries no
    summary at all: it replays no local history (the provider holds the
    conversation), and an index added to every such request would pile up
    there.
    """
    state_messages: list[Message] = state.get("messages", [])
    index = next((m for m in state_messages if is_archive_index(m) and _visible(m)), None)
    if index is None:
        return False
    entries = index.additional_properties[ARCHIVE_INDEX_KEY]
    archived_block_ids = {
        block_id
        for m in state_messages
        if is_archived_summary(m) and isinstance(block_id := m.additional_properties.get(_BLOCK_ID_KEY), str)
    }

    changed = False
    present = False
    head_start: int | None = None
    for position, message in enumerate(messages):
        if not _is_summary(message):
            continue
        if head_start is None:
            head_start = position
        if is_archive_index(message):
            # Compared by what it lists: a request may carry a copy of the message.
            if message.additional_properties[ARCHIVE_INDEX_KEY] == entries and _visible(message):
                present = True
                continue
        elif message.additional_properties.get(_BLOCK_ID_KEY) not in archived_block_ids:
            continue
        changed |= set_excluded(message, excluded=True, reason=REASON_ARCHIVED)
    if not present and head_start is not None:
        messages.insert(head_start, index)
        changed = True
    return changed
