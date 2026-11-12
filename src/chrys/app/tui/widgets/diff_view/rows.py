# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The rows of a diff: what each line on display stands for.

A diff is computed as hunks of line opcodes and shown as rows. A row says what kind of line it is,
which line of the old and of the new text it shows, and carries the highlighted code. Everything a
renderer needs to draw a line is on its row, so the widgets share one list of rows and nothing has
to be kept in step with it.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from itertools import zip_longest
from typing import TYPE_CHECKING

from textual.content import Content

if TYPE_CHECKING:
    from chrys.app.tui.util.diff_comments import LineIdentity

type Opcode = tuple[str, int, int, int, int]
"""One `difflib` opcode: what happened to lines ``[i1, i2)`` of the old text and ``[j1, j2)`` of the new."""
type Hunk = Sequence[Opcode]
"""A run of changes with the unchanged lines around it."""


class Side(Enum):
    """One of the two texts a diff compares."""

    BEFORE = "before"
    AFTER = "after"


class RowKind(Enum):
    """What a row shows."""

    CONTEXT = "context"
    ADDED = "added"
    REMOVED = "removed"
    FILLER = "filler"
    """Nothing on this side of a split diff: the line across from it has no counterpart."""
    BREAK = "break"
    """Stands for the unchanged lines left out between two hunks, across the whole width of the diff."""


@dataclass(frozen=True, slots=True)
class DiffRow:
    """One line of a diff on display."""

    kind: RowKind
    code: Content | None = None
    before: int | None = None
    """The line's number in the old text, counted from one."""
    after: int | None = None
    """The line's number in the new text, counted from one."""

    def number(self, side: Side) -> int | None:
        return self.before if side is Side.BEFORE else self.after

    @property
    def is_placeholder(self) -> bool:
        """Whether the row stands where no line is. Every column draws such a row as hatching."""
        return self.kind in _PLACEHOLDERS


def line_identity(row: DiffRow) -> LineIdentity | None:
    if row.is_placeholder:
        return None
    if row.kind is RowKind.REMOVED:
        return ("before", row.before) if row.before is not None else None
    return ("after", row.after) if row.after is not None else None


def comment_side(row: DiffRow) -> Side:
    """The number column a comment on ``row`` shows in: the old one for a removed line, else the new one."""
    return Side.BEFORE if row.kind is RowKind.REMOVED else Side.AFTER


_PLACEHOLDERS = frozenset({RowKind.FILLER, RowKind.BREAK})

BREAK_ROW = DiffRow(RowKind.BREAK)
FILLER_ROW = DiffRow(RowKind.FILLER)


def _removed_rows(before: Sequence[Content], start: int, end: int) -> list[DiffRow]:
    return [DiffRow(RowKind.REMOVED, before[index], before=index + 1) for index in range(start, end)]


def _added_rows(after: Sequence[Content], start: int, end: int) -> list[DiffRow]:
    return [DiffRow(RowKind.ADDED, after[index], after=index + 1) for index in range(start, end)]


def unified_rows(hunks: Iterable[Hunk], before: Sequence[Content], after: Sequence[Content]) -> list[DiffRow]:
    """Lay hunks out in one column: within a change, the removed lines and then the added ones."""
    rows: list[DiffRow] = []
    for hunk in hunks:
        if rows:
            rows.append(BREAK_ROW)
        for tag, i1, i2, j1, j2 in hunk:
            if tag == "equal":
                rows.extend(
                    DiffRow(RowKind.CONTEXT, before[i1 + offset], i1 + offset + 1, j1 + offset + 1)
                    for offset in range(i2 - i1)
                )
            else:
                rows.extend(_removed_rows(before, i1, i2))
                rows.extend(_added_rows(after, j1, j2))
    return rows


def split_rows(
    hunks: Iterable[Hunk], before: Sequence[Content], after: Sequence[Content]
) -> tuple[list[DiffRow], list[DiffRow]]:
    """Lay hunks out in two columns of equal length, the old text on the left and the new on the right.

    Within a change the removed and the added lines pair off from the top, and the side that runs
    out first is filled up. Each side shows context in its own highlighting: the same text may
    read differently once a change further up opened a string or a comment.
    """
    left: list[DiffRow] = []
    right: list[DiffRow] = []
    for hunk in hunks:
        if left:
            left.append(BREAK_ROW)
            right.append(BREAK_ROW)
        for tag, i1, i2, j1, j2 in hunk:
            if tag == "equal":
                left.extend(
                    DiffRow(RowKind.CONTEXT, before[i1 + offset], i1 + offset + 1, j1 + offset + 1)
                    for offset in range(i2 - i1)
                )
                right.extend(
                    DiffRow(RowKind.CONTEXT, after[j1 + offset], i1 + offset + 1, j1 + offset + 1)
                    for offset in range(j2 - j1)
                )
            else:
                pairs = zip_longest(_removed_rows(before, i1, i2), _added_rows(after, j1, j2), fillvalue=FILLER_ROW)
                for removed, added in pairs:
                    left.append(removed)
                    right.append(added)
    return left, right


def change_counts(hunks: Iterable[Hunk]) -> tuple[int, int]:
    """How many lines the hunks add and how many they remove."""
    changes = [opcode for hunk in hunks for opcode in hunk if opcode[0] != "equal"]
    return sum(j2 - j1 for _, _, _, j1, j2 in changes), sum(i2 - i1 for _, i1, i2, _, _ in changes)


def number_width(*row_lists: Sequence[DiffRow]) -> int:
    """The digits needed for the largest line number among the rows."""
    numbers = (number for rows in row_lists for row in rows for number in (row.before, row.after) if number)
    return len(str(max(numbers, default=0)))


def code_width(*row_lists: Sequence[DiffRow]) -> int:
    """The width in cells of the longest line of code among the rows, and never less than one."""
    return max((row.code.cell_length for rows in row_lists for row in rows if row.code is not None), default=1) or 1
