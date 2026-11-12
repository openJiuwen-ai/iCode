# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Text layout shared by the profile pickers."""

from __future__ import annotations

from rich.cells import cell_len

from chrys.app.tui.util.formatting import grapheme_clusters

_DESC_PREFIX = "  - "
"""Prefix of the first description line."""

_DESC_INDENT = "    "
"""Indent for description lines (matches width of '  - ')."""


def _wrap_cells(text: str, width: int) -> list[str]:
    """Greedily wrap *text* measured in terminal cells, breaking runs at graphemes.

    Words are kept whole while they fit; a run with no spaces (CJK, a bare URL)
    is broken between grapheme clusters, so a wide glyph is never cut in half,
    a combining mark stays with the character it modifies, and a flag emoji
    stays one glyph.
    """
    lines: list[str] = []
    current, used = "", 0
    for word in text.split():
        word_cells = cell_len(word)
        if current and used + 1 + word_cells <= width:
            current, used = f"{current} {word}", used + 1 + word_cells
            continue
        if current:
            lines.append(current)
            current, used = "", 0
        for cluster, cells in grapheme_clusters(word):
            if current and used + cells > width:
                lines.append(current)
                current, used = "", 0
            current, used = current + cluster, used + cells
    if current:
        lines.append(current)
    return lines


def wrap_description(desc: str, width: int) -> str:
    """Wrap description text so continuation lines align after '  - '.

    Lines are measured in terminal cells, so double-width text stays inside
    *width*; a budget too narrow for the indent is honoured rather than
    widened, and one grapheme wider than the whole budget keeps a line to
    itself.
    """
    if not desc.strip():
        return ""
    text_width = max(width - cell_len(_DESC_INDENT), 1)
    lines = _wrap_cells(desc, text_width)
    if not lines:
        return ""
    return _DESC_PREFIX + ("\n" + _DESC_INDENT).join(lines)
