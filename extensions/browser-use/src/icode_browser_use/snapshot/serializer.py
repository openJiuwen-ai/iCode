# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Budgeted serialization of a fused snapshot into the model-visible stage.

The budget is weighted (ASCII 1, CJK 2) so the rendered text stays below the
host truncation line; text is sanitized on the way out only (lone surrogates and
zero-width/control characters are stripped, the raw snapshot is untouched).
"""

from __future__ import annotations

from ..config import MAX_STAGE_NODES, SNAPSHOT_CHAR_BUDGET
from .fusion import FusedNode

#: Code point above which a character counts as full-width (CJK + full-width punctuation).
_FULL_WIDTH_THRESHOLD = 0x2E7F

_ZERO_WIDTH = frozenset({0xFEFF})
_CONTROL_KEEP = frozenset({0x09, 0x0A})  # tab, newline


def weight(text: str) -> int:
    """Weighted length: full-width characters count 2, everything else 1."""
    return sum(2 if ord(ch) > _FULL_WIDTH_THRESHOLD else 1 for ch in text)


def sanitize_text(text: str) -> str:
    """Remove lone surrogates, zero-width and control characters (keep ``\\n``/``\\t``)."""
    if not text:
        return ""
    out: list[str] = []
    length = len(text)
    index = 0
    while index < length:
        ch = text[index]
        code = ord(ch)
        if 0xD800 <= code <= 0xDBFF:  # high surrogate
            if index + 1 < length and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
                low = ord(text[index + 1])
                out.append(chr(0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)))
                index += 2
                continue
            index += 1
            continue
        if 0xDC00 <= code <= 0xDFFF:  # lone low surrogate
            index += 1
            continue
        if 0x200B <= code <= 0x200F or code in _ZERO_WIDTH:
            index += 1
            continue
        if (code < 0x20 or code == 0x7F or 0x80 <= code <= 0x9F) and code not in _CONTROL_KEEP:
            index += 1
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def _single_line(text: str) -> str:
    """Sanitize and flatten newlines/tabs so page text can never spoof snapshot lines."""
    cleaned = sanitize_text(text)
    if not cleaned:
        return ""
    return " ".join(cleaned.split())


def is_structural(node: FusedNode) -> bool:
    """A node with nothing to name, read, click, edit or scroll: structure only, not worth a line."""
    return (
        not node.interactive
        and not node.editable
        and not node.scrollable
        and not node.name.strip()
        and not node.text_hint.strip()
    )


def _fold(stage: list[FusedNode]) -> list[tuple[FusedNode, int]]:
    """Collapse consecutive same-parent, same-role, same-name text siblings into one line.

    ``parent_backend_id`` is unique per element within a target, so equal parent
    ids mean the same DOM parent. Interactive duplicates are never folded: each
    of them is a target of its own, and a folded line would hide its ref.
    """
    folded: list[tuple[FusedNode, int]] = []
    for node in stage:
        if folded:
            previous, count = folded[-1]
            if (
                node.name
                and not node.interactive
                and not previous.interactive
                and node.parent_backend_id
                and node.frame_id == previous.frame_id
                and node.parent_backend_id == previous.parent_backend_id
                and node.role == previous.role
                and node.name == previous.name
            ):
                folded[-1] = (previous, count + 1)
                continue
        folded.append((node, 1))
    return folded


def _rank(nodes: list[FusedNode]) -> list[FusedNode]:
    """Ordering: in-viewport first, then interactive, then by vertical position."""
    return sorted(nodes, key=lambda node: (not node.in_viewport, not node.interactive, node.bbox[1]))


def render(
    nodes: list[FusedNode],
    *,
    page_epoch: int,
    url: str,
    title: str,
    version: int = 0,
    budget: int = SNAPSHOT_CHAR_BUDGET,
    max_nodes: int = MAX_STAGE_NODES,
    focus: str = "",
) -> str:
    """Render the stage: a header line followed by the budgeted node lines."""
    visible = [node for node in nodes if not is_structural(node)]
    entries = _fold(_rank(visible))
    suffix = f", version {version}" if version else ""
    if focus:
        suffix += f", subtree {focus}"
    header = f"[{_single_line(url)}] {_single_line(title)}  (epoch {page_epoch}{suffix})"
    lines = [header]
    used = weight(header)
    total = sum(count for _, count in entries)
    shown = 0
    capacity = max_nodes
    offscreen = sum(1 for node in visible if not node.in_viewport)

    for node, count in entries:
        if capacity <= 0:
            break
        take = min(count, capacity)
        token = f"@e{node.ref}#{page_epoch}" if node.ref is not None else "-"
        role = _single_line(node.role)
        name = _single_line(node.name)
        if not name and node.text_hint:
            name = _single_line(node.text_hint[:120])
        if len(name) > 240:
            name = name[:239] + "…"
        star = "*" if node.scrollable else ""
        mark = "[iframe] " if node.cdp_session_id else ""
        extra = ""
        if focus:
            tags: list[str] = []
            if node.editable:
                tags.append("editable")
            if node.readonly:
                tags.append("readonly")
            if node.disabled:
                tags.append("disabled")
            if not node.has_layout and (node.bbox[2] <= 0 or node.bbox[3] <= 0):
                tags.append("no-layout")
            if tags:
                extra += f" [{' '.join(tags)}]"
            if node.placeholder and node.placeholder not in name:
                extra += f' placeholder="{_single_line(node.placeholder[:80])}"'
        line = f'  {token} {mark}{role} "{name}"{extra}{star}' + (f" ×{take}" if take > 1 else "")
        if used + weight(line) + 1 > budget:
            break
        lines.append(line)
        used += weight(line) + 1
        shown += take
        capacity -= take

    remaining = total - shown
    if remaining:
        lines.append(f"  … (+{remaining} more interactive, {offscreen} offscreen; scroll or narrow)")
    return "\n".join(lines)
