# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Markdown table renderer for document parsers."""

from __future__ import annotations

from itertools import groupby
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


def mark_struck(segments: Iterable[tuple[str, bool]]) -> str:
    """Join ``(text, struck)`` segments, wrapping struck-through text in ``~~`` markers.

    Authors strike text out to void it while keeping it readable, so the
    Markdown keeps that text, marked, instead of presenting it as current.
    Adjacent struck segments share one marker pair, and whitespace at either
    end of a struck run stays outside the markers: Markdown does not read
    ``~~ text~~`` as strikethrough.
    """
    parts: list[str] = []
    for struck, group in groupby(segments, key=lambda segment: segment[1]):
        text = "".join(segment[0] for segment in group)
        start = len(text) - len(text.lstrip())
        end = len(text.rstrip())
        if not struck or start >= end:
            parts.append(text)
        else:
            parts.append(f"{text[:start]}~~{text[start:end]}~~{text[end:]}")
    return "".join(parts)


def _escape_cell(value: object) -> str:
    """Stringify and escape a cell value for use inside a Markdown table."""
    text = str(value) if value is not None else ""
    # A table row is a single line: a raw line break would split the row and
    # leave its remaining cells outside the table.  <br> keeps the line break
    # visible inside the cell.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")
    return text.replace("|", "\\|")


def rows_to_markdown_table(rows: list[tuple]) -> str:
    """Convert a list of row tuples to a Markdown table.

    The first row is used as the header.  Pipe characters in cell values
    are escaped and line breaks become ``<br>`` so neither breaks the table
    structure.
    """
    if not rows:
        return ""
    header = rows[0]
    col_count = len(header)
    header_cells = [_escape_cell(c) for c in header]
    lines = [
        "| " + " | ".join(header_cells) + " |",
        "| " + " | ".join(["---"] * col_count) + " |",
    ]
    for row in rows[1:]:
        cells = [_escape_cell(c) for c in row]
        # Pad or truncate to match header column count
        cells = (cells + [""] * col_count)[:col_count]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
