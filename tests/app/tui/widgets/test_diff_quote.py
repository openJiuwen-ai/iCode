# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A comment quotes exactly one original line, with complete provenance and bounded fences."""

from __future__ import annotations

import re
from functools import partial
from unittest.mock import call, patch

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.content import Content
from textual.geometry import Region

from chrys.app.tui.widgets.diff_view import CodeColumn, DiffView
from chrys.app.tui.widgets.diff_view.cells import number_cell, number_cell_width
from chrys.app.tui.widgets.diff_view.compute import compared_lines, compute_hunks
from chrys.app.tui.widgets.diff_view.gutter import GutterColumn
from chrys.app.tui.widgets.diff_view.quote import DiffQuote, build_diff_quote
from chrys.app.tui.widgets.diff_view.rows import (
    BREAK_ROW,
    FILLER_ROW,
    DiffRow,
    RowKind,
    Side,
    line_identity,
    split_rows,
)
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from tests.support.waiting import wait_for


def _quote(row: DiffRow, before: str, after: str, path: str, source: str, **limits: int) -> DiffQuote | None:
    return build_diff_quote(row, (compared_lines(before), compared_lines(after)), path, source, **limits)


@pytest.mark.parametrize(
    ("row", "before", "after", "path", "source", "position", "body", "fence"),
    [
        (row, "head\r\n\told\vx\r", "head\n\tnew\n", "src/a.py", "turn 4", position, body, "```")
        for row, position, body in [
            (DiffRow(RowKind.REMOVED, Content("shown"), before=2), "2", "- \told\vx"),
            (DiffRow(RowKind.ADDED, Content("shown"), after=2), "2", "+ \tnew"),
            (DiffRow(RowKind.CONTEXT, Content("shown"), 1, 1), "1", "  head"),
        ]
    ]
    + [
        (
            DiffRow(RowKind.ADDED, Content("shown"), after=1),
            "",
            raw,
            "/outside/a",
            "session",
            "1",
            "+ " + raw,
            fence,
        )
        for raw, fence in [("`", "```"), ("```", "````"), ("```````", "````````")]
    ],
)
def test_one_raw_line_and_english_provenance(row, before, after, path, source, position, body, fence) -> None:
    quote = _quote(row, before, after, path, source)
    assert quote is not None
    assert (
        quote.content
        == f"File: {path} ({'before' if body.startswith('-') else 'after'}) | Source: {source}\n{fence}\n{position} {body}\n{fence}"
    )
    assert not quote.truncated


@pytest.mark.parametrize("row", [FILLER_ROW, BREAK_ROW])
def test_placeholders_cannot_be_quoted(row) -> None:
    assert _quote(row, "", "", "a", "session") is None
    assert line_identity(row) is None


@pytest.mark.parametrize(
    ("raw", "max_tokens"),
    [
        ("word " * 400, 80),
        ("中文混合 text " * 200, 60),
        ("a`b``c" * 150 + "````" + "tail " * 100, 70),
        ("x" * 2_000 + "`" * 9 + "y" * 2_000, 300),
    ],
)
def test_truncation_keeps_the_longest_prefix_that_fits(raw, max_tokens) -> None:
    tokenizer = MixedLanguageTokenizer()
    row = DiffRow(RowKind.ADDED, Content(raw), after=1)

    def fenced(body: str) -> str:
        fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", body)), default=0) + 1)
        return f"File: a.py (after) | Source: turn 1\n{fence}\n1 + {body}\n{fence}"

    quote = _quote(row, "", raw, "a.py", "turn 1", max_tokens=max_tokens)
    longest = max(n for n in range(len(raw)) if tokenizer.count_tokens(fenced(raw[:n] + "…")) <= max_tokens)
    assert quote is not None and quote.truncated
    assert quote.content == fenced(raw[:longest] + "…")


def test_single_line_is_shortened_inside_line_and_fence_is_recomputed() -> None:
    raw = "word " * 500 + "`````"
    quote = _quote(DiffRow(RowKind.ADDED, Content(raw), after=1), "", raw, "a.py", "turn 2", max_tokens=80)
    assert quote is not None and quote.truncated
    assert quote.original_tokens > 80
    assert MixedLanguageTokenizer().count_tokens(quote.content) <= 80
    assert quote.content == "File: a.py (after) | Source: turn 2\n```\n1 + " + "word " * 54 + "word…\n```"


def test_header_that_cannot_fit_is_not_partially_quoted() -> None:
    assert (
        _quote(DiffRow(RowKind.ADDED, Content("a"), after=1), "", "a", "long/path" * 100, "session", max_tokens=5)
        is None
    )


def test_split_context_retains_both_numbers_and_has_same_identity_on_both_sides() -> None:
    before, after = "a\nb\nc", "new\na\nb\nc"
    left, right = split_rows(
        compute_hunks(before, after), list(map(Content, before.splitlines())), list(map(Content, after.splitlines()))
    )
    context_left = next(row for row in left if row.kind is RowKind.CONTEXT)
    context_right = next(row for row in right if row.kind is RowKind.CONTEXT)
    assert (context_left.before, context_left.after) == (1, 2)
    assert (context_right.before, context_right.after) == (1, 2)
    assert line_identity(context_left) == line_identity(context_right) == ("after", 2)
    for row in (context_left, context_right):
        assert _quote(row, before, after, "a", "session").content == (
            "File: a (after) | Source: session\n```\n2   a\n```"
        )


class _CommentDiffApp(App):
    CSS = "DiffView { height: 1fr; }"

    def __init__(self, *, split: bool = True, before_mount: bool = False) -> None:
        super().__init__()
        self.split = split
        self.before_mount = before_mount

    def compose(self) -> ComposeResult:
        view = DiffView("a.py", "a.py", "old\nsame", "new\nsame")
        view.split = self.split
        view.comments_enabled = True
        if self.before_mount:
            view.set_comment_lines(frozenset({("before", 1), ("after", 1), ("after", 2)}))
        yield view


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("before_mount", [False, True])
async def test_comment_lines_replace_only_the_correct_numeric_gutter_cells(split, before_mount) -> None:
    app = _CommentDiffApp(split=split, before_mount=before_mount)
    async with app.run_test(size=(100, 24)) as pilot:
        view = app.query_one(DiffView)
        await wait_for(lambda: bool(view.query(CodeColumn)), pilot=pilot, description="comment diff mounted")
        await pilot.pause()
        gutters = list(view.query(GutterColumn))
        original = {g: [g.render_line(y).text for y in range(len(g.rows))] for g in gutters}
        view.set_comment_lines(frozenset({("before", 1), ("after", 1), ("after", 2)}))
        for gutter in gutters:
            if gutter.side is None:
                expected = frozenset()
            elif split:
                expected = frozenset({0}) if gutter.side is Side.BEFORE else frozenset({0, 1})
            else:
                expected = frozenset({0}) if gutter.side is Side.BEFORE else frozenset({1, 2})
            assert gutter.comment_rows == expected
            crop = Region(0, 0, gutter.size.width, len(gutter.rows))
            for y, strip in enumerate(gutter.render_lines(crop)):
                assert ("💬" in strip.text) is (y in expected)
        for column in view.query(CodeColumn):
            assert all("💬" not in column.render_line(y).text for y in range(len(column.rows)))
        view.set_comment_lines(frozenset())
        for gutter in gutters:
            assert not gutter.comment_rows
            assert all("💬" not in gutter.render_line(y).text for y in range(len(gutter.rows)))
            if not before_mount:
                assert [gutter.render_line(y).text for y in range(len(gutter.rows))] == original[gutter]


async def test_code_hover_repaints_only_changed_number_cells_and_preserves_other_cached_rows() -> None:
    app = _CommentDiffApp()
    async with app.run_test(size=(100, 24)) as pilot:
        await wait_for(lambda: bool(app.query(CodeColumn)), pilot=pilot, description="comment diff mounted")
        await pilot.pause()
        right = app.query(CodeColumn).last()
        gutter = next(g for g in right.gutters if g.side is Side.AFTER)
        crop = Region(0, 0, gutter.size.width, 2)
        initial = gutter.render_lines(crop)
        unchanged = gutter.render_line(1)
        code = right.render_line(0)
        with patch.object(gutter, "refresh", wraps=gutter.refresh) as refresh:
            await pilot.hover(right, offset=(4, 0))
            refresh.assert_called_once_with(Region(0, 0, gutter.size.width, 1))
            assert "💬" in gutter.render_lines(crop)[0].text
            assert gutter.render_line(1) is unchanged
            assert right.render_line(0) is code
            refresh.reset_mock()
            await pilot.hover(right, offset=(5, 0))
            refresh.assert_not_called()
            await pilot.hover(right, offset=(4, 1))
            assert refresh.call_args_list == [
                call(Region(0, 0, gutter.size.width, 1)),
                call(Region(0, 1, gutter.size.width, 1)),
            ]
            assert gutter.render_lines(crop)[0].text == initial[0].text
            assert "💬" in gutter.render_lines(crop)[1].text
            refresh.reset_mock()
            await pilot.hover(app.query_one(DiffView), offset=(0, 0))
            refresh.assert_called_once_with(Region(0, 1, gutter.size.width, 1))
            assert [strip.text for strip in gutter.render_lines(crop)] == [strip.text for strip in initial]


@pytest.mark.parametrize("split", [False, True])
async def test_hover_only_replaces_digits_and_highlights_preserve_foregrounds(split: bool) -> None:
    app = _CommentDiffApp(split=split)
    async with app.run_test(size=(100, 24)) as pilot:
        from rich.console import ColorSystem

        app.console._color_system = ColorSystem.TRUECOLOR
        await wait_for(lambda: bool(app.query(CodeColumn)), pilot=pilot)
        await pilot.pause()
        for column in app.query(CodeColumn):
            for index, row in enumerate(column.rows):
                side = Side.BEFORE if row.kind is RowKind.REMOVED else Side.AFTER
                gutter = next((g for g in column.gutters if g.side is side), None)
                if gutter is None:
                    continue
                # Strip.crop counts terminal cells, including the two-cell icon.
                original = gutter.render_line(index)
                edge_gutter = column.gutters[0]
                edge = next(iter(edge_gutter.render_line(index).crop(0, 1))).style.color
                original_first = next(iter(original.crop(0, 1))).style.color
                if row.kind is not RowKind.CONTEXT:
                    expected = Content.styled(
                        "▎", "$success on #1f4d2a" if row.kind is RowKind.ADDED else "$error on #5a2628"
                    )
                    assert edge == next(iter(expected.render_segments(gutter.visual_style))).style.color
                column.set_comment_hover(index)
                hovered = gutter.render_line(index)
                assert "💬" in hovered.text
                assert edge_gutter.render_line(index).text[0] == "▎"
                assert next(iter(hovered.crop(0, 1))).style.color == original_first
                assert next(iter(edge_gutter.render_line(index).crop(0, 1))).style.color == edge
                number_style = next(iter(original.crop(1, original.cell_length))).style
                for segment in hovered.crop(1, hovered.cell_length):
                    assert segment.style == number_style
                column.set_comment_hover(None)
                gutter.set_comment_rows(frozenset({index}))
                commented = gutter.render_line(index)
                assert commented.text == hovered.text
                assert list(commented) == list(hovered)
                gutter.set_comment_rows(frozenset())
                if row.kind is RowKind.CONTEXT:
                    continue
                original_code = column.render_line(index)
                original_colours = [segment.style.color for segment in original]
                code_colours = [segment.style.color for segment in original_code]
                column.set_current_comment(index)
                highlighted = gutter.render_line(index)
                highlighted_code = column.render_line(index)
                assert [segment.style.color for segment in highlighted] == original_colours
                assert [segment.style.color for segment in highlighted_code] == code_colours
                assert next(iter(highlighted)).style.bgcolor != next(iter(original)).style.bgcolor
                assert next(iter(highlighted_code)).style.bgcolor != next(iter(original_code)).style.bgcolor
                column.set_current_comment(None)


@pytest.mark.parametrize(("number", "expected"), [(1, "▎💬"), (10, "▎💬 "), (100, "▎ 💬 ")])
async def test_comment_icon_centers_in_number_cell_after_the_edge(number: int, expected: str) -> None:
    class NumberApp(App):
        def compose(self) -> ComposeResult:
            rows = [DiffRow(RowKind.ADDED, Content("new"), after=number)]
            column = CodeColumn(rows, 3)
            column.comments_enabled = True
            digits = len(str(number))
            gutter = GutterColumn(
                rows,
                partial(number_cell, side=Side.AFTER, digits=digits, edge=True),
                number_cell_width(digits),
                column,
                side=Side.AFTER,
            )
            column.gutters = [gutter]
            yield Horizontal(gutter, column)

    app = NumberApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        column = app.query_one(CodeColumn)
        gutter = app.query_one(GutterColumn)
        assert gutter.render_line(0).text == f"▎{number} "
        column.set_comment_hover(0)
        assert gutter.render_line(0).text == expected
        column.set_comment_hover(None)
        gutter.set_comment_rows(frozenset({0}))
        assert gutter.render_line(0).text == expected


def test_a_path_that_is_not_utf8_still_encodes_for_the_model() -> None:
    quote = _quote(DiffRow(RowKind.ADDED, Content("x"), after=1), "", "x", "bad-\udcff.py", "turn 1")
    assert quote is not None
    quote.content.encode("utf-8")
    assert "\udcff" not in quote.content
