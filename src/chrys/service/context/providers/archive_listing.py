# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Paged text listing of compressed blocks and fold markers.

A long session holds more blocks than a tool result can carry, so a listing
is cut into pages that each fit one tool result.  A page opens with the
totals and the cursor of the next page, where a cut at the end cannot lose
them, and holds whole items: one line per block (or its full summary, on
request) and one line per fold marker.  Only an item that cannot fit a page
on its own, a long full summary, is split, and the cursor then names the
character to go on from.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chrys.foundation.text.tokenizer import MixedLanguageTokenizer

_PAGE_TARGET_TOKENS = 8_000
"""What a page aims for when the tool-result ceiling leaves more room."""

_PAGE_MARGIN = 0.9
"""Share of the page budget a page fills: the kernel counts the result once more with its own joins."""

_MARKER_HINT_EACH_END = 10
"""Markers a short hint names from each end of a long marker list."""

_CONTINUED = "    … "
"""Opens the rest of a summary the previous page cut."""

# The counter the kernel's tool-result ceiling truncates by.
_tokenizer = MixedLanguageTokenizer()


class ListingError(ValueError):
    """A listing request the caller has to correct; the message is safe to show the model."""


@dataclass(frozen=True, slots=True)
class _Item:
    heading: str
    """Section heading written before the first item of its section on a page."""
    text: str
    splittable: bool = False


def page_budget_tokens(tool_result_ceiling_tokens: int | None) -> int:
    """Tokens one page may take: the page target, or less under a lower tool-result ceiling."""
    ceiling = tool_result_ceiling_tokens or 0
    budget = _PAGE_TARGET_TOKENS if ceiling <= 0 else min(_PAGE_TARGET_TOKENS, ceiling)
    return max(1, int(budget * _PAGE_MARGIN))


def block_for_turn(blocks: list[dict[str, Any]], turn: int) -> dict[str, Any] | None:
    """The block whose turn range holds *turn*."""
    for block in blocks:
        turn_from, turn_to = block["turn_range"]
        if turn_from <= turn <= turn_to:
            return block
    return None


def marker_hint(markers: list[dict[str, Any]]) -> str:
    """Fold markers for an error hint: all of a short list, both ends of a long one."""
    if not markers:
        return "No fold markers available — markers are inserted after each agent turn."
    lines = [_marker_line(marker) for marker in markers]
    if len(lines) > 2 * _MARKER_HINT_EACH_END:
        omitted = len(lines) - 2 * _MARKER_HINT_EACH_END
        lines = [
            *lines[:_MARKER_HINT_EACH_END],
            f"  … {omitted} more (list_compressed_contexts shows them all)",
            *lines[-_MARKER_HINT_EACH_END:],
        ]
    return "Available fold markers (use the marker_id value exactly as shown):\n" + "\n".join(lines)


def render_listing(
    info: dict[str, Any],
    *,
    budget_tokens: int,
    compressed_context_id: str | None = None,
    turn_from: int | None = None,
    turn_to: int | None = None,
    full_summaries: bool = False,
    cursor: str | None = None,
) -> str:
    """One page of the blocks and markers in *info* (``list_compressed`` output).

    *compressed_context_id* shows that block alone, with its full summary;
    *turn_from*/*turn_to* keep the blocks and markers that touch the turn
    range; *full_summaries* writes every block's summary instead of its one
    line.  *cursor* is what the previous page gave for the next one.
    """
    blocks: list[dict[str, Any]] = info["blocks"]
    markers: list[dict[str, Any]] = info["markers"]
    if turn_from is not None and turn_to is not None and turn_from > turn_to:
        raise ListingError(f"turn_from ({turn_from}) is after turn_to ({turn_to}).")

    if compressed_context_id is not None:
        block = next((b for b in blocks if b["compressed_context_id"] == compressed_context_id), None)
        if block is None:
            raise ListingError(
                f"compressed_context_id '{compressed_context_id}' not found. "
                "Call list_compressed_contexts without it to see the available IDs."
            )
        shown_blocks, shown_markers, full_summaries = [block], [], True
    else:
        shown_blocks = [b for b in blocks if _touches(b["turn_range"], turn_from, turn_to)]
        shown_markers = [m for m in markers if _touches((m["turn_index"], m["turn_index"]), turn_from, turn_to)]

    items = [
        *(
            _Item("Compressed blocks:", _block_text(block, full=full_summaries), splittable=full_summaries)
            for block in shown_blocks
        ),
        *(
            _Item("Available fold markers (use the marker_id value exactly as shown):", _marker_line(marker))
            for marker in shown_markers
        ),
    ]
    start, offset = _parse_cursor(cursor, items)

    totals = _totals_line(blocks, markers, shown_blocks, shown_markers)
    if not items:
        return totals

    def page(taken: int, cut: int = 0) -> str:
        """Items ``start`` to ``start + taken``; with *cut*, only that many characters of the last one."""
        lines: list[str] = []
        heading = ""
        for position in range(start, start + taken):
            item = items[position]
            text = f"{_CONTINUED}{item.text[offset:]}" if position == start and offset else item.text
            if cut and position == start + taken - 1:
                text = text[:cut]
            if item.heading != heading:
                heading = item.heading
                lines.append(heading)
            lines.append(text)
        end = start + taken
        if cut:
            written = cut - (len(_CONTINUED) if taken == 1 and offset else 0)
            where = f"{end - 1}+{(offset if taken == 1 else 0) + max(1, written)}"
        else:
            where = str(end) if end < len(items) else ""
        more = f'More: call again with cursor="{where}".' if where else "End of list."
        return "\n".join([totals, f"Items {start + 1}-{end} of {len(items)}. {more}", *lines])

    # Whole items while they fit.  Counts of parts do not add up to the count
    # of the whole, so the sum only says where to start measuring whole pages.
    room = budget_tokens - _count(page(0))
    taken = 0
    while start + taken < len(items) and (room := room - _count(items[start + taken].text) - 1) >= 0:
        taken += 1
    taken = max(1, taken)
    while taken > 1 and _count(page(taken)) > budget_tokens:
        taken -= 1
    first = items[start]
    if taken > 1 or not first.splittable or _count(page(1)) <= budget_tokens:
        # A one-line item opening the page is written whole whatever it takes: a page holds at least one item.
        return page(taken)

    # A full summary longer than a page: as much of it as fits, the rest on the next page.
    low, high = 1, len(first.text) - offset + (len(_CONTINUED) if offset else 0) - 1
    while low < high:
        middle = (low + high + 1) // 2
        if _count(page(1, middle)) <= budget_tokens:
            low = middle
        else:
            high = middle - 1
    return page(1, low)


def _touches(turn_range: tuple[int, int], turn_from: int | None, turn_to: int | None) -> bool:
    first, last = turn_range
    return (turn_from is None or last >= turn_from) and (turn_to is None or first <= turn_to)


def _block_text(block: dict[str, Any], *, full: bool) -> str:
    turn_from, turn_to = block["turn_range"]
    head = f"  - {block['compressed_context_id']} · turns {turn_from}-{turn_to} · {block['num_messages']} messages"
    if full:
        return f"{head}\n    Summary: {block['summary']}"
    label = block.get("first_user_text") or ""
    return f"{head} · {label}" if label else head


def _marker_line(marker: dict[str, Any]) -> str:
    return f'  - marker_id="{marker["marker_id"]}" (turn {marker["turn_index"]})'


def _totals_line(
    blocks: list[dict[str, Any]],
    markers: list[dict[str, Any]],
    shown_blocks: list[dict[str, Any]],
    shown_markers: list[dict[str, Any]],
) -> str:
    parts: list[str] = []
    if blocks:
        first = min(block["turn_range"][0] for block in blocks)
        last = max(block["turn_range"][1] for block in blocks)
        parts.append(f"{len(blocks)} compressed block(s), turns {first}-{last}")
    else:
        parts.append("No compressed blocks yet")
    if markers:
        parts.append(
            f"{len(markers)} fold marker(s), turns {markers[0]['turn_index']}-{markers[-1]['turn_index']}"
            ' (marker_id="turn_<n>")'
        )
    else:
        parts.append("no fold markers available — markers are inserted after each agent turn")
    line = "; ".join(parts) + "."
    if len(shown_blocks) != len(blocks) or len(shown_markers) != len(markers):
        line += f" Matching this request: {len(shown_blocks)} block(s), {len(shown_markers)} marker(s)."
    return line


def _parse_cursor(cursor: str | None, items: list[_Item]) -> tuple[int, int]:
    """``"<item>"`` or ``"<item>+<character>"``, as a page wrote it."""
    if cursor is None or not cursor.strip():
        return 0, 0
    index_text, _, offset_text = cursor.strip().partition("+")
    if not index_text.isdecimal() or (offset_text and not offset_text.isdecimal()):
        raise ListingError(f"cursor '{cursor}' is not one a list_compressed_contexts page gave.")
    index, offset = int(index_text), int(offset_text or 0)
    if index >= len(items) or (offset and (not items[index].splittable or offset >= len(items[index].text))):
        raise ListingError(
            f"cursor '{cursor}' is past the end of this list ({len(items)} item(s)); "
            "call again without a cursor, with the same filters."
        )
    return index, offset


def _count(text: str) -> int:
    return _tokenizer.count_tokens(text)
