# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Folded segments of a persisted chat transcript."""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.service.state.serializers import deserialize_compressed_block

if TYPE_CHECKING:
    from chrys.service.context.providers.history import CompressedBlock

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class FoldItem:
    """A fold card, with its block, stored record and slot when the block is readable."""

    context_id: str
    summary: str
    block_index: int | None = None
    block: CompressedBlock | None = None
    raw_block: dict[str, Any] | None = None


def message_properties(message: dict[str, Any]) -> dict[str, Any]:
    """A raw state message's properties: its dict, or empty when missing or not a dict."""
    properties = message.get("additional_properties")
    return properties if isinstance(properties, dict) else {}


def message_contents(value: object) -> list[object]:
    """A raw message's contents: the list itself, one item for a string, else empty."""
    if isinstance(value, list):
        return value
    return [value] if isinstance(value, str) else []


def fold_summary(contents: object) -> str:
    """The text after ``Summary:`` in a fold summary message, else its last text."""
    fallback = ""
    for content in message_contents(contents):
        text = content.get("text", "") if isinstance(content, dict) else content
        if not isinstance(text, str) or not text:
            continue
        if "Summary:" in text:
            return text.split("Summary:", 1)[1].strip()
        fallback = text
    return fallback


def transcript_folds(
    state: dict[str, Any],
    *,
    decoded_blocks: list[CompressedBlock] | None = None,
) -> list[FoldItem]:
    """Match summaries to slots once and emit each fold item once in summary order.

    Unclaimed readable blocks keep their stored positions among the summaries.

    A slot is readable when its stored record decodes; every reader (TUI
    replay, ACP replay, ``session/history``) uses this one verdict.

    Pass 1 lets each summary, in message order, claim the first unclaimed slot
    with its id. Pass 2 walks the summaries in message order and emits one item
    per summary: the block when its claimed slot is readable, else a
    summary-only card. A readable slot no summary claimed is an orphan; it is
    emitted before the first summary whose claimed slot is later than it, and
    any left over at the end, in stored order. Every slot is emitted at most
    once, so summaries stored out of block order can neither duplicate a slot
    nor leave an empty card behind it.
    """
    slots: list[FoldItem | None] = []
    by_id: dict[str, deque[int]] = defaultdict(deque)
    for index, raw in enumerate(state.get("compressed_msgs", [])):
        context_id = raw.get("compressed_context_id") if isinstance(raw, dict) else None
        if isinstance(context_id, str) and context_id:
            by_id[context_id].append(index)
        try:
            if not isinstance(raw, dict) or not isinstance(context_id, str):
                raise ValueError("Compressed block has no valid id")
            block = decoded_blocks[index] if decoded_blocks is not None else deserialize_compressed_block(raw)
            item = FoldItem(context_id, block.summary_text, index, block=block, raw_block=raw)
        except Exception:
            logger.warning("Failed to read compressed block at slot %d (%s)", index, context_id, exc_info=True)
            item = None
        slots.append(item)

    summaries: list[tuple[str, str]] = []
    for message in state.get("messages", []):
        properties = message_properties(message)
        if (
            properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.SUMMARY
            or HistoryMarkerKind.ARCHIVE_INDEX_KEY in properties
        ):
            continue
        summaries.append((str(properties.get("_block_id", "")), fold_summary(message.get("contents", []))))

    slot_for_summary: dict[int, int] = {}
    for summary_index, (context_id, _summary) in enumerate(summaries):
        candidates = by_id.get(context_id)
        if candidates:
            slot_for_summary[summary_index] = candidates.popleft()
    claimed = set(slot_for_summary.values())

    folds: list[FoldItem] = []
    emitted: set[int] = set()

    def emit_orphans_before(limit: int) -> None:
        for index in range(limit):
            item = slots[index]
            if item is not None and index not in claimed and index not in emitted:
                folds.append(item)
                emitted.add(index)

    for summary_index, (context_id, summary) in enumerate(summaries):
        slot_index = slot_for_summary.get(summary_index)
        if slot_index is not None:
            emit_orphans_before(slot_index)
        item = slots[slot_index] if slot_index is not None else None
        if item is None:
            folds.append(FoldItem(context_id, summary))
        else:
            folds.append(FoldItem(context_id, summary, slot_index, block=item.block, raw_block=item.raw_block))

    folds.extend(
        item for index, item in enumerate(slots) if item is not None and index not in claimed and index not in emitted
    )
    return folds
