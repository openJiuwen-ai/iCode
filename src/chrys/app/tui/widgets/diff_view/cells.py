# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What stands beside a line of code: its number and its annotation.

These are plain functions of a row and the look of the app. The gutter columns of the scrolling
diff and the single-widget diff of the chat both draw with them, so the two cannot drift apart.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.content import Content

from chrys.app.tui.widgets import HATCH_GLYPH
from chrys.app.tui.widgets.diff_view.rows import RowKind

if TYPE_CHECKING:
    from chrys.app.tui.widgets.diff_view.palette import DiffLook
    from chrys.app.tui.widgets.diff_view.rows import DiffRow, Side

EDGE = "▎"
COMMENT_ICON = "💬"
"""Marks a line that has a comment, in place of its number or, in a quote, ahead of it."""
MARKER_WIDTH = 2
"""The cells ahead of a line's number where a quote marks its quoted line."""
ANNOTATIONS = {RowKind.ADDED: "+", RowKind.REMOVED: "-"}
ANNOTATION_WIDTH = 3
"""A ``+`` or ``-`` with a space on either side."""
COLLAPSED_ANNOTATION_WIDTH = 1
"""What is left of the cell when annotations are switched off: the space ahead of the code."""


def number_cell_width(digits: int) -> int:
    """The width of a number cell: the edge or a space, the digits, and a space."""
    return digits + 2


def number_cell(row: DiffRow, look: DiffLook, *, side: Side, digits: int, edge: bool) -> Content:
    """The line number of ``row`` in the text of ``side``, right-aligned in ``digits`` columns.

    With ``edge`` the cell opens with a bar in the color of the row's kind, which also shows on a
    row that has no number on this side.
    """
    if row.is_placeholder:
        return Content.styled(HATCH_GLYPH * number_cell_width(digits), look.hatch)
    number = row.number(side)
    cell = Content(f"{EDGE if edge else ' '}{'' if number is None else number:>{digits}} ")
    if number is not None:
        cell = cell.stylize(look.palette.number[row.kind], 1 if edge else 0)
    if edge:
        cell = cell.stylize(look.palette.edge[row.kind], 0, 1)
    return cell


def marker_cell(row: DiffRow, look: DiffLook, *, marked: bool) -> Content:
    """The comment icon ahead of a quoted line, or blank, on the background of the line."""
    return Content(COMMENT_ICON if marked else " " * MARKER_WIDTH).stylize_before(look.line_style(row.kind))


def annotation_cell(row: DiffRow, look: DiffLook) -> Content:
    """The ``+`` or ``-`` of a changed row on the background of its line; blank for any other row."""
    if row.is_placeholder:
        return Content.styled(HATCH_GLYPH * ANNOTATION_WIDTH, look.hatch)
    annotation = ANNOTATIONS.get(row.kind)
    if annotation is None:
        return Content(" " * ANNOTATION_WIDTH)
    return Content(f" {annotation} ").stylize(look.line_style(row.kind)).stylize("bold")
