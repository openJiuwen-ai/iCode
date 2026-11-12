# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A comment quotes one original line with its neighbours, complete provenance and bounded fences, and reads back."""

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
from chrys.app.tui.widgets.diff_view.quote import DiffQuote, build_diff_quote, parse_diff_quote, quote_scope
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
from tests.support.diff_quote_text import diff_quote_text
from tests.support.waiting import wait_for


def _quote(row: DiffRow, before: str, after: str, path: str, scope: str, **limits: int) -> DiffQuote | None:
    return build_diff_quote(
        row, (compared_lines(before), compared_lines(after)), path, scope, hunks=compute_hunks(before, after), **limits
    )


def test_the_model_reads_the_quoted_line_between_its_neighbours() -> None:
    quote = _quote(
        DiffRow(RowKind.REMOVED, Content("shown"), before=2), "a\n\told\nc\n", "a\n\tnew\nc\n", "src/a.py", "turn 4"
    )
    assert quote is not None
    assert quote.content == (
        "File: src/a.py (as it was before the changes made in turn 4; line numbers count in that version)\n"
        'Quoted line: 2, marked with ">"; the lines around it are context\n'
        "```\n"
        "  1   a\n"
        "> 2 - \told\n"
        "  3   c\n"
        "```"
    )
    assert not quote.truncated and not quote.context_omitted


@pytest.mark.parametrize(
    ("row", "version", "quoted", "lines"),
    [
        (
            DiffRow(RowKind.REMOVED, Content("shown"), before=2),
            "before",
            2,
            [(1, " ", "head"), (2, "-", "\told\vx")],
        ),
        (DiffRow(RowKind.ADDED, Content("shown"), after=2), "after", 2, [(1, " ", "head"), (2, "+", "\tnew")]),
        (DiffRow(RowKind.CONTEXT, Content("shown"), 1, 1), "after", 1, [(1, " ", "head"), (2, "+", "\tnew")]),
    ],
)
def test_lines_come_from_the_quoted_version_with_crlf_and_no_trailing_newline(row, version, quoted, lines) -> None:
    quote = _quote(row, "head\r\n\told\vx\r", "head\n\tnew", "src/a.py", "turn 4")
    assert quote is not None
    assert quote.content == diff_quote_text("src/a.py", version, "turn 4", quoted, lines)


@pytest.mark.parametrize(("raw", "fence"), [("`", "```"), ("```", "````"), ("```````", "````````")])
def test_a_one_line_file_quotes_one_line_with_a_fence_longer_than_its_backticks(raw, fence) -> None:
    quote = _quote(DiffRow(RowKind.ADDED, Content("shown"), after=1), "", raw, "/outside/a", "this session")
    assert quote is not None
    assert quote.content == diff_quote_text("/outside/a", "after", "this session", 1, [(1, "+", raw)], fence)
    assert not quote.context_omitted


def test_the_fence_is_longer_than_backticks_in_a_neighbour() -> None:
    quote = _quote(DiffRow(RowKind.ADDED, Content("x"), after=2), "", "a ````` b\nx\n", "a.md", "this session")
    assert quote is not None
    assert quote.content == diff_quote_text(
        "a.md", "after", "this session", 2, [(1, "+", "a ````` b"), (2, "+", "x")], "``````"
    )


_FILE = "".join(f"line {n}\n" for n in range(1, 21))


@pytest.mark.parametrize(
    ("before", "after", "row", "version", "quoted", "lines"),
    [
        # A replace block: the old lines are removed, the new ones added, each in its own version.
        (
            "a\nb\nc\nd\n",
            "a\nB\nC\nd\n",
            DiffRow(RowKind.REMOVED, Content(""), before=2),
            "before",
            2,
            [(1, " ", "a"), (2, "-", "b"), (3, "-", "c")],
        ),
        (
            "a\nb\nc\nd\n",
            "a\nB\nC\nd\n",
            DiffRow(RowKind.ADDED, Content(""), after=3),
            "after",
            3,
            [(2, "+", "B"), (3, "+", "C"), (4, " ", "d")],
        ),
        # A context line numbered differently before and after is quoted by its number after.
        (
            "a\nb\nc\n",
            "x\ny\na\nb\nc\n",
            DiffRow(RowKind.CONTEXT, Content(""), 1, 3),
            "after",
            3,
            [(2, "+", "y"), (3, " ", "a"), (4, " ", "b")],
        ),
        # The first line of a file has only the line after it; both lines of the removed block are marked.
        (
            "a\nold\nc\n",
            "new\nc\n",
            DiffRow(RowKind.REMOVED, Content(""), before=1),
            "before",
            1,
            [(1, "-", "a"), (2, "-", "old")],
        ),
        # The last line has only the line before it; blank lines are lines.
        (
            "\nb\n",
            "\nb\n\n",
            DiffRow(RowKind.ADDED, Content(""), after=3),
            "after",
            3,
            [(2, " ", "b"), (3, "+", "")],
        ),
        # A neighbour outside every hunk, in the folded context, is unchanged.
        (
            _FILE,
            _FILE.replace("line 1\n", "line one\n"),
            DiffRow(RowKind.CONTEXT, Content(""), 4, 4),
            "after",
            4,
            [(3, " ", "line 3"), (4, " ", "line 4"), (5, " ", "line 5")],
        ),
        # Numbers align to the widest of the three.
        (
            _FILE,
            _FILE.replace("line 9\n", "line nine\n"),
            DiffRow(RowKind.ADDED, Content(""), after=9),
            "after",
            9,
            [(8, " ", "line 8"), (9, "+", "line nine"), (10, " ", "line 10")],
        ),
    ],
)
def test_neighbours_are_marked_by_where_they_fall_in_the_hunks(before, after, row, version, quoted, lines) -> None:
    quote = _quote(row, before, after, "f", "this session")
    assert quote is not None
    assert quote.content == diff_quote_text("f", version, "this session", quoted, lines)


def test_line_number_width_follows_the_widest_line_in_the_quote() -> None:
    quote = _quote(DiffRow(RowKind.CONTEXT, Content(""), 9, 9), _FILE, _FILE, "f", "this session")
    assert quote is not None
    assert quote.content.split("\n")[3:6] == ["   8   line 8", ">  9   line 9", "  10   line 10"]


@pytest.mark.parametrize(
    ("turn", "prompt", "scope"),
    [
        (None, "ignored", "this session"),
        (3, None, "turn 3"),
        (3, " \n ", "turn 3"),
        (3, "  fix\n the \t bug ", 'turn 3, the reply to the user message "fix the bug"'),
        (12, "x" * 40, f'turn 12, the reply to the user message "{"x" * 40}"'),
        (12, "x" * 41, f'turn 12, the reply to the user message "{"x" * 39}…"'),
        (2, "修复 这个\n问题", 'turn 2, the reply to the user message "修复 这个 问题"'),
    ],
)
def test_scope_names_the_turn_by_the_start_of_its_user_message(turn, prompt, scope) -> None:
    assert quote_scope(turn, prompt) == scope


def test_a_prompt_that_is_not_utf8_still_encodes_for_the_model() -> None:
    quote_scope(1, "bad \udcff").encode("utf-8")


@pytest.mark.parametrize("placeholder", [FILLER_ROW, BREAK_ROW])
def test_placeholders_cannot_be_quoted(placeholder) -> None:
    assert _quote(placeholder, "", "", "a", "this session") is None
    assert line_identity(placeholder) is None


def _single(body: str, max_scope: str = "turn 1") -> str:
    fence = "`" * max(3, max((len(run) for run in re.findall(r"`+", body)), default=0) + 1)
    return diff_quote_text("a.py", "after", max_scope, 1, [(1, "+", body)], fence)


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
    quote = _quote(row, "", raw, "a.py", "turn 1", max_tokens=max_tokens)
    longest = max(n for n in range(len(raw)) if tokenizer.count_tokens(_single(raw[:n] + "…")) <= max_tokens)
    assert quote is not None and quote.truncated
    assert quote.content == _single(raw[:longest] + "…")


def test_single_line_is_shortened_inside_line_and_fence_is_recomputed() -> None:
    raw = "word " * 500 + "`````"
    quote = _quote(DiffRow(RowKind.ADDED, Content(raw), after=1), "", raw, "a.py", "turn 2", max_tokens=80)
    assert quote is not None and quote.truncated
    assert quote.original_tokens > 80
    tokenizer = MixedLanguageTokenizer()
    assert tokenizer.count_tokens(quote.content) <= 80
    shortened = quote.content.split("\n")[3].removeprefix("> 1 + ")
    assert shortened.endswith("…") and raw.startswith(shortened[:-1])
    assert quote.content == _single(shortened, "turn 2")
    # One more character of the line would not fit.
    assert tokenizer.count_tokens(_single(raw[: len(shortened)] + "…", "turn 2")) > 80


def test_neighbours_that_do_not_fit_are_left_out_without_being_counted() -> None:
    huge = "y" * 2_000_000
    counted: list[int] = []
    original = MixedLanguageTokenizer.count_token_units

    def counting(self, text: str) -> int:
        counted.append(len(text))
        return original(self, text)

    with patch.object(MixedLanguageTokenizer, "count_token_units", counting):
        quote = _quote(DiffRow(RowKind.ADDED, Content("x"), after=2), "", f"{huge}\nx\n{huge}\n", "a.py", "turn 1")
    assert quote is not None
    assert quote.content == _single("x").replace("Quoted line: 1", "Quoted line: 2").replace("> 1 +", "> 2 +")
    assert quote.context_omitted and not quote.truncated
    assert max(counted) < 1_000


def test_neighbours_whose_exact_count_does_not_fit_are_left_out() -> None:
    # Each CJK character costs more than the per-character floor, so the quick bound passes and the count decides.
    around = "中" * 500
    quote = _quote(
        DiffRow(RowKind.ADDED, Content("x"), after=2), "", f"{around}\nx\n", "a.py", "turn 1", max_tokens=300
    )
    assert quote is not None
    assert quote.content == diff_quote_text("a.py", "after", "turn 1", 2, [(2, "+", "x")])
    assert quote.context_omitted and not quote.truncated


def test_a_quoted_line_too_long_alone_is_shortened_and_its_neighbours_left_out() -> None:
    raw = "word " * 500
    quote = _quote(DiffRow(RowKind.ADDED, Content(raw), after=2), "", f"a\n{raw}\nb\n", "a.py", "turn 1", max_tokens=80)
    assert quote is not None and quote.truncated and quote.context_omitted
    lines = quote.content.split("\n")
    assert lines[1] == 'Quoted line: 2, marked with ">"'
    assert len(lines) == 5 and lines[3].startswith("> 2 + word") and lines[3].endswith("…")


def test_header_that_cannot_fit_is_not_partially_quoted() -> None:
    assert (
        _quote(DiffRow(RowKind.ADDED, Content("a"), after=1), "", "a", "long/path" * 100, "this session", max_tokens=5)
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
        assert _quote(row, before, after, "a", "this session").content == diff_quote_text(
            "a", "after", "this session", 2, [(1, "+", "new"), (2, " ", "a"), (3, " ", "b")]
        )


_ROUND_TRIP = [
    ("before", "\told", "-", "this session"),
    ("after", "中文 `code` ``` x", "+", "turn 3"),
    ("after", "> 1 - not a marker", " ", 'turn 12, the reply to the user message "fix \\"it\\" now"'),
    ("before", "trailing  \t", " ", "turn 1"),
    ("after", "line\u2028separator\x85next", "+", "this session"),
    ("after", "", "+", "turn 2"),
]


@pytest.mark.parametrize(("version", "text", "mark", "scope"), _ROUND_TRIP)
@pytest.mark.parametrize("shape", ["alone", "first", "middle", "last"])
def test_every_quote_reads_back_as_written(version, text, mark, scope, shape) -> None:
    neighbour = (" ", "ctx  ")
    lines = {
        "alone": [(1, mark, text)],
        "first": [(1, mark, text), (2, *neighbour)],
        "middle": [(9, *neighbour), (10, mark, text), (11, *neighbour)],
        "last": [(9, *neighbour), (10, mark, text)],
    }[shape]
    quoted = 1 if shape in ("alone", "first") else 10
    content = diff_quote_text("dir/a b.py", version, scope, quoted, lines, _fence_for(text))
    parsed = parse_diff_quote(content)
    assert parsed is not None
    assert (parsed.path, parsed.version) == ("dir/a b.py", version)
    assert parsed.turn == (None if scope == "this session" else int(scope.split()[1].rstrip(",")))
    assert [(line.number, line.text, line.quoted) for line in parsed.lines] == [
        (number, line_text, number == quoted) for number, _mark, line_text in lines
    ]
    assert [line.kind for line in parsed.lines] == [
        {"-": RowKind.REMOVED, "+": RowKind.ADDED, " ": RowKind.CONTEXT}[line_mark] for _n, line_mark, _t in lines
    ]


def _fence_for(text: str) -> str:
    return "`" * max(3, max((len(run) for run in re.findall(r"`+", text)), default=0) + 1)


def test_quotes_built_from_a_diff_read_back() -> None:
    for row, before, after in [
        (DiffRow(RowKind.REMOVED, Content(""), before=2), "a\n\told\nc\n", "a\n\tnew\nc\n"),
        (DiffRow(RowKind.ADDED, Content(""), after=1), "", "```\n"),
        (DiffRow(RowKind.ADDED, Content("x"), after=2), "", "y" * 2_000_000 + "\nx\n"),
    ]:
        quote = _quote(row, before, after, "p.py", quote_scope(2, "fix it"))
        assert quote is not None and parse_diff_quote(quote.content) is not None
    truncated = _quote(DiffRow(RowKind.ADDED, Content(""), after=1), "", "word " * 500, "p.py", "turn 1", max_tokens=80)
    assert truncated is not None and parse_diff_quote(truncated.content) is not None


_VALID = diff_quote_text("a.py", "after", "turn 2", 2, [(1, " ", "a"), (2, "+", "b"), (3, " ", "c")])


@pytest.mark.parametrize(
    "content",
    [
        # The one-line form quotes had before they carried context, and text that only looks like a quote.
        "File: a.py (after) | Source: turn 2\n```\n1 + x\n```",
        "plain selected text",
        "",
        _VALID + "\n",
        _VALID.replace("\n", "\r\n"),
        _VALID.replace("```", "````", 1),
        _VALID.replace("as it was after", "as it was later"),
        _VALID.replace("turn 2", "turn two"),
        _VALID.replace("turn 2", "the session"),
        _VALID.replace("Quoted line: 2", "Quoted line: 3"),
        _VALID.replace("; the lines around it are context", ""),
        _VALID.replace("> 2 +", "  2 +"),
        _VALID.replace("  3   c", "> 3   c"),
        _VALID.replace("  3   c", "  4   c"),
        _VALID.replace("> 2 +", "> 2 -"),
        _VALID.replace("> 2 +", "> 2 *"),
        _VALID.replace("> 2 + b", "> 2 +b"),
        _VALID.replace("  1   a", " 1   a"),
        # The quoted line is not between its neighbours.
        diff_quote_text("a.py", "after", "turn 2", 1, [(1, "+", "a"), (2, " ", "b"), (3, " ", "c")]),
        # Line 5 has a line before it, so it cannot be quoted with only the line after.
        diff_quote_text("a.py", "after", "turn 2", 5, [(5, "+", "a"), (6, " ", "b")]),
        # Four lines, or a header that does not end where the form does.
        _VALID.replace("  3   c", "  3   c\n  4   d"),
        _VALID.replace("line numbers count in that version)", "line numbers count in that version) extra"),
        # Numbers no file or session reaches, too long for int() to read.
        _VALID.replace("Quoted line: 2", "Quoted line: " + "9" * 5000),
        _VALID.replace("> 2 +", "> " + "9" * 5000 + " +"),
        _VALID.replace("turn 2", "turn " + "9" * 5000),
    ],
)
def test_anything_but_the_whole_form_is_not_a_diff_quote(content) -> None:
    assert parse_diff_quote(content) is None


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


@pytest.mark.parametrize(("code", "count"), [("a = 1", 1), ("a = 1\nb = 2", 2), ("a = 1\n", 2), ("x\n\n", 3), ("", 1)])
def test_a_highlighted_snippet_has_one_line_per_line_of_code_blank_last_lines_included(code, count) -> None:
    from chrys.app.tui.widgets.diff_view.compute import highlight_snippet

    lines = highlight_snippet(code, "a.py")
    assert [line.plain for line in lines] == code.split("\n")
    assert len(lines) == count
