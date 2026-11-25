# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Golden render tests for :class:`VirtualizedMarkdown`.

Pins the visible output for a representative markdown document so the Tier 1
fence pre-render and the Tier 3 block-style cache cannot silently change what
appears on screen.

The assertions intentionally compare *rstripped plain text per row* rather
than full style tuples — style attributes resolve through the theme
machinery and are not deterministic across CI configurations.  Plain text
captures every visible character (including padding, bullets, fence body)
and is the user-observable shape of the output.
"""

from __future__ import annotations

from textual._cells import cell_len
from textual.app import App, ComposeResult
from textual.geometry import Region

from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.markdown.parser import (
    _cell_min_width,
    _cell_prefix_length,
    _cell_wrapped_height,
    _wrap_cell_text,
)
from tests.support.waiting import wait_for


def _row_text(strip) -> str:
    """Visible characters of a row, with trailing padding stripped."""
    return "".join(segment.text for segment in strip._segments).rstrip()


def _visual_segments(strip) -> list[tuple[str, object]]:
    """Segment text and visual style, excluding Textual selection metadata."""
    return [
        (segment.text, None if segment.style is None else segment.style.clear_meta_and_links())
        for segment in strip._segments
    ]


def _bar_columns(text: str) -> tuple[int, ...]:
    """Display columns (not char indices) of each ``│`` separator in a row."""
    return tuple(cell_len(text[:i]) for i, ch in enumerate(text) if ch == "│")


def _assert_table_columns_aligned(rows: list[str], *, col_count: int, width: int) -> None:
    bar_rows = [row for row in rows if row.count("│") >= 2]
    assert bar_rows, f"width={width}: no table rows rendered"
    positions = {_bar_columns(row) for row in bar_rows}
    assert len(positions) == 1, f"width={width}: mismatched separator columns: {sorted(positions)}"
    counts = {row.count("│") for row in bar_rows}
    assert counts == {col_count + 1}, f"width={width}: cropped/missing borders, separator counts: {counts}"


def test_cell_prefix_length_fits_emoji_clusters_within_display_width() -> None:
    """Variation selectors and ZWJ clusters must not leak extra table cells.

    The table renderer pads the kept prefix up to the column, so a prefix that
    never exceeds the column is what keeps every row exactly column-wide.
    """
    cases = (
        ("☑️", 1),
        ("☑️", 2),
        ("👍🏽", 2),
        ("🏳️‍🌈", 2),
        ("👩‍💻", 2),
        ("á", 1),
        ("中", 1),
    )

    for text, width in cases:
        assert cell_len(text[: _cell_prefix_length(text, width)]) <= width

    assert _cell_prefix_length("☑️", 2) == len("☑️")
    assert _cell_prefix_length("👩‍💻", 2) == len("👩‍💻")
    assert _cell_prefix_length("中", 1) == 0


def test_cell_min_width_tracks_unbreakable_clusters() -> None:
    """Table column floors should match the markdown cell wrapping model."""
    assert _cell_min_width("abc") == 1
    assert _cell_min_width("行数") == 2
    assert _cell_min_width("☑️") == 2
    assert _cell_min_width("👩‍💻") == 2
    assert _cell_min_width("👍🏽") == 2
    assert _cell_min_width("á") == 1


def test_wrap_cell_text_keeps_zero_width_clusters_together() -> None:
    """Wrapping should not split modifiers away from their base character."""
    assert _wrap_cell_text("☑️x", 1) == ["☑️", "x"]
    assert _wrap_cell_text("👩‍💻x", 1) == ["👩‍💻", "x"]
    assert _wrap_cell_text("áb", 1) == ["á", "b"]


def test_cell_wrapped_height_matches_wrap_cell_text() -> None:
    """The table height fast path must match the cell wrapping model exactly."""
    cases = (
        "plain ascii words",
        "line one\nline two",
        "中文内容 wraps",
        "☑️x 👩‍💻x 👍🏽 ok",
        "áb zero width mark",
    )
    for text in cases:
        for width in range(1, 12):
            assert _cell_wrapped_height(text, width) == len(_wrap_cell_text(text, width))


class _MdApp(App):
    CSS = """
    VirtualizedMarkdown { padding: 0; }
    """

    def __init__(self, markdown: str) -> None:
        super().__init__()
        self._md = markdown
        self.widget: VirtualizedMarkdown | None = None

    def compose(self) -> ComposeResult:
        self.widget = VirtualizedMarkdown(self._md)
        yield self.widget


async def _render_all_rows(markdown: str, *, width: int = 40, height: int = 40) -> list[str]:
    """Mount a markdown widget, force layout, return per-row rstripped text."""
    app = _MdApp(markdown)
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.pause()
        md = app.widget
        assert md is not None
        await wait_for(lambda: md.region.width == width, pilot=pilot, description="markdown initial width")

        strips = md.render_lines(Region(0, 0, width, height))
        return [_row_text(s) for s in strips]


async def _render_rows_across_widths(markdown: str, *, widths: range, height: int) -> dict[int, list[str]]:
    """Mount once, resize through *widths*, and capture every rendered row.

    Table-width regressions are resize bugs. Exercising the real terminal
    resize path both models that behavior more closely and avoids rebuilding
    an otherwise identical Textual app for every width.
    """
    width_values = tuple(widths)
    assert width_values
    app = _MdApp(markdown)
    rendered: dict[int, list[str]] = {}
    async with app.run_test(size=(width_values[0], height)) as pilot:
        await pilot.pause()
        md = app.widget
        assert md is not None
        await wait_for(lambda: md.region.width == width_values[0], pilot=pilot, description="markdown initial width")

        for index, width in enumerate(width_values):
            if index:
                await pilot.resize_terminal(width, height)
                # The resize layout lands on a later message-pump cycle;
                # poll for the new region instead of asserting immediately.
                await wait_for(
                    lambda expected=width: md.region.width == expected,
                    pilot=pilot,
                    description=f"markdown region resized to {width} cells",
                )
            assert md.region.width == width
            strips = md.render_lines(Region(0, 0, width, height))
            rendered[width] = [_row_text(strip) for strip in strips]
    return rendered


async def test_paragraph_only_render() -> None:
    """A paragraph's visible content is exactly its source text (modulo wrap)."""
    rows = await _render_all_rows("This is a paragraph that should not wrap at 40 cells.\n")
    visible = [r for r in rows if r]
    joined = " ".join(visible)
    assert joined == "This is a paragraph that should not wrap at 40 cells."


async def test_heading_render() -> None:
    """``# Heading`` renders as one styled row of the heading text."""
    rows = await _render_all_rows("# Hello World\n")
    visible = [r for r in rows if r]
    # Heading 1 uses content-align center, so the text may include leading
    # padding even after rstrip — assert containment rather than equality.
    assert any("Hello World" in r for r in visible)


async def test_fence_render_preserves_code_text() -> None:
    """Every code line of a fence appears in the rendered output."""
    code_lines = [f"x_{i} = {i}" for i in range(5)]
    body = "\n".join(code_lines)
    rows = await _render_all_rows(f"```python\n{body}\n```\n")
    visible = [r for r in rows if r]
    for code in code_lines:
        assert any(code in row for row in visible), f"missing {code!r} in {visible!r}"


async def test_fence_row_count_is_stable() -> None:
    """An N-line fence produces exactly N visible code rows (no per-row drift)."""
    code_lines = [f"line_{i}" for i in range(8)]
    body = "\n".join(code_lines)
    rows = await _render_all_rows(f"```python\n{body}\n```\n", height=20)
    code_row_indices = [i for i, r in enumerate(rows) if r.strip().startswith("line_")]
    assert len(code_row_indices) == len(code_lines)
    # And they are contiguous (no blank row interleaved into the fence body).
    assert code_row_indices == list(range(code_row_indices[0], code_row_indices[0] + len(code_lines)))


async def test_list_render_includes_bullets() -> None:
    """Unordered list items are prefixed with the bullet glyph."""
    rows = await _render_all_rows("- alpha\n- beta\n- gamma\n", height=10)
    visible = [r for r in rows if r]
    assert any("alpha" in r and "•" in r for r in visible)
    assert any("beta" in r and "•" in r for r in visible)
    assert any("gamma" in r and "•" in r for r in visible)


async def test_full_document_row_text_is_stable() -> None:
    """Mixed document renders to a stable, deterministic per-row text sequence.

    This is the strongest regression test: if any optimization touches the
    rendered output (style cache, fence cache, hoisted color-system lookup),
    the row sequence here must be byte-identical or the test fails.
    """
    md = "# Title\n\npara one.\n\n```python\na = 1\nb = 2\n```\n\n- alpha\n- beta\n"
    rows = await _render_all_rows(md, width=40, height=25)

    # Strip trailing blank rows (viewport tail) for stability.
    while rows and not rows[-1]:
        rows.pop()

    # Render twice and confirm output is deterministic (no flakiness in the
    # render pipeline itself).
    rows_again = await _render_all_rows(md, width=40, height=25)
    while rows_again and not rows_again[-1]:
        rows_again.pop()
    assert rows == rows_again, "two renders of identical markdown diverged"

    assert rows == [
        "                 Title",
        "",
        "para one.",
        "",
        "",
        "  a = 1",
        "  b = 2",
        "",
        "",
        "  • alpha",
        "  • beta",
    ]


async def test_fence_render_byte_identical_with_and_without_line_cache() -> None:
    """Toggling the lazy fence line cache must not change visible output.

    This covers cache determinism: render once with the per-source-line caches
    populated and once after clearing them, then assert identical rows.
    """
    md = "```python\n" + "\n".join(f"x_{i} = {i}" for i in range(20)) + "\n```\n"
    app = _MdApp(md)
    async with app.run_test(size=(60, 30)) as pilot:
        widget = app.widget
        assert widget is not None
        await wait_for(lambda: widget.region.width == 60, pilot=pilot, description="markdown initial width")

        crop = Region(0, 0, 60, 30)

        # Pass 1: lazy fence line caches are populated by visible fence rows.
        widget._line_cache.clear()
        widget._lines_cache_result = None
        cached_strips = widget._render_lines_direct(crop, None)

        # Pass 2: drop fence line caches and per-row strip cache; rendering
        # rebuilds the same rows through the cold lazy path.
        widget._fence_line_strips.clear()
        widget._fence_source_line_strips.clear()
        widget._block_style_cache.clear()
        widget._line_cache.clear()
        widget._lines_cache_result = None
        fallback_strips = widget._render_lines_direct(crop, None)

        assert len(cached_strips) == len(fallback_strips)
        for y, (a, b) in enumerate(zip(cached_strips, fallback_strips, strict=True)):
            assert a.cell_length == b.cell_length, f"row {y} cell_length differs"
            a_segs = [(s.text, s.style) for s in a._segments]
            b_segs = [(s.text, s.style) for s in b._segments]
            assert a_segs == b_segs, f"row {y} segments differ:\n  cached={a_segs}\n  fallback={b_segs}"


async def test_lazy_fence_rows_match_whole_fence_render_strips() -> None:
    """Per-source-line fence rendering must match whole-fence Content rendering."""
    long_line = "wrapped_value = " + " + ".join(f"compute_{i}()" for i in range(20))
    md = f"```python\nx = 1\n\n{long_line}\nif x:\n    print(x)\n```\n"
    app = _MdApp(md)
    async with app.run_test(size=(42, 30)) as pilot:
        widget = app.widget
        assert widget is not None
        await wait_for(lambda: widget.region.width == 42, pilot=pilot, description="markdown initial width")

        fence_idx = next(i for i, block in enumerate(widget._blocks) if block.block_type == "fence")
        block = widget._blocks[fence_idx]
        # Mirror VirtualizedMarkdown._render_block_line's content-width formula
        # so the whole-fence comparison uses the same inner code width.
        border_width = len(block.border_left) if block.border_left else 0
        content_width = (
            widget.scrollable_content_region.width
            - block.indent
            - block.padding_left
            - block.padding_right
            - border_width
            - 1
        )
        content_width = max(1, content_width)
        block_style = widget._get_block_style_cached(fence_idx, block)

        whole_fence = widget._build_fence_strips(block, content_width, block_style)
        layout = widget._fence_layouts[fence_idx]
        lazy_rows = [
            widget._render_fence_line(block, fence_idx, actual_line, content_width, block_style)
            for actual_line in range(layout.total_height)
        ]

        assert len(lazy_rows) == len(whole_fence)
        for y, (lazy, whole) in enumerate(zip(lazy_rows, whole_fence, strict=True)):
            assert lazy.cell_length == whole.cell_length, f"row {y} cell_length differs"
            # Whole-fence rendering tags rows with source offsets from the
            # entire fence, while lazy row rendering starts from the sliced
            # source line. VirtualizedMarkdown.render_line() later rebases
            # offsets for selection, so this equivalence is visual.
            assert _visual_segments(lazy) == _visual_segments(whole), (
                f"row {y} differs between lazy and whole-fence rendering"
            )


async def test_cjk_table_columns_stay_aligned_across_widths() -> None:
    """A table with CJK headers must stay aligned and intact at every width.

    Regression for the misaligned-bars bug: proportional column scaling could
    squeeze a column holding double-width CJK glyphs down to 1 cell.  A
    width-2 glyph cannot fit a 1-cell column, so it overflowed and pushed every
    separator to its right one column over — but only on rows whose cell in
    that column was CJK (the header ``行数``), not ASCII (the data ``1180``),
    so header and data rows drifted apart.  Resizing made it come and go as the
    layout swept through trigger widths.

    Two invariants must hold at every width down to where the table can still
    physically fit (N columns need N content cells + borders/padding):

    1. every row places its ``│`` separators at identical display columns, and
    2. every row keeps all ``col_count + 1`` separators — the min-width floor
       must never push the row past the content area, where it would be cropped
       and lose its right border/data.

    Two tables: the realistic one exercises the original CJK-header / ASCII-data
    asymmetry where a squeezed column lands on width 1 (≈ width 44-50); the
    compact one stays scrollbar-free at the narrowest widths so the min-width
    floor's fit-fallback is exercised without hitting the fundamental
    can't-fit-a-scrollbar-and-N-columns limit.
    """
    realistic = (
        "| 文件 | 行数 | 测试目标 | 核心内容 |\n"
        "|---|---|---|---|\n"
        "| test_clients_factory.py | 1180 | clients.py — create_client() 工厂 | "
        "从 ModelProfile key 解析、base_URL/代理)、provider(DeepSeek/Mock)、headed 变体 |\n"
        "| test_cache.py | 22 | 验证缓存行为 | 核心内容很长一段话用于测试换行 |\n"
    )
    compact = (
        "| 文件 | 行数 | 状态 |\n"
        "|---|---|---|\n"
        "| client_factory.py | 1180 | 验证缓存行为是否正确 |\n"
        "| 测试器.py | 22 | ok |\n"
    )
    # Together the ranges cover a contiguous 16-60: compact stays scrollbar-free
    # down low, realistic takes over where it no longer scrolls in this viewport.
    for md, widths in ((compact, range(16, 30)), (realistic, range(30, 61))):
        col_count = md.splitlines()[0].count("|") - 1
        rows_by_width = await _render_rows_across_widths(md, widths=widths, height=60)
        for width, rows in rows_by_width.items():
            _assert_table_columns_aligned(rows, col_count=col_count, width=width)


async def test_narrow_table_shrinks_the_widest_column_before_a_short_one() -> None:
    """A table too wide for the view wraps its long column and leaves short ones whole.

    Scaling every column in proportion to its natural width also took cells from
    the three-glyph ``默认值`` column beside a long description, so the header broke
    into ``默认`` / ``值`` while the description could have wrapped instead.
    """
    markdown = (
        "| 设置 | 默认值 | 说明 |\n"
        "|---|---|---|\n"
        "| `ui.theme` | `chrys` | 字符串 使用 TUI 主题列表中已有的主题名称 未知名称回到默认主题 "
        "修改后立即生效并写入用户设置文件 |\n"
    )
    width = 60
    rows = await _render_all_rows(markdown, width=width, height=30)
    _assert_table_columns_aligned(rows, col_count=3, width=width)
    header = next(row for row in rows if "设置" in row)
    assert "默认值" in header, f"short header column wrapped: {rows}"
    assert "ui.theme" in "\n".join(rows)
    description_rows = [row for row in rows if row.count("│") == 4 and "设置" not in row and row.split("│")[3].strip()]
    assert len(description_rows) > 1, f"long column should wrap instead: {rows}"


async def test_emoji_table_columns_stay_aligned_across_widths() -> None:
    """Emoji clusters must fit their assigned display cells exactly."""
    markdown = "| S | V |\n|---|---|\n| ☑️ | done |\n| x | ok |\n| 👩‍💻 | dev |\n| 👍🏽 | yes |\n"
    col_count = markdown.splitlines()[0].count("|") - 1
    rows_by_width = await _render_rows_across_widths(markdown, widths=range(15, 26), height=40)
    for width, rows in rows_by_width.items():
        _assert_table_columns_aligned(rows, col_count=col_count, width=width)
        rendered = "\n".join(rows)
        assert "☑️" in rendered
        assert "👩‍💻" in rendered
        assert "👍🏽" in rendered


async def test_br_splits_a_table_cell_into_lines_sized_by_the_widest() -> None:
    """``<br>`` in a cell stacks its parts; the column fits the widest part, not all of them in a row."""
    markdown = "| Command | Use |\n|---|---|\n| `/copy`<br>`/copy all` | copy |\n"
    width = 40
    rows = await _render_all_rows(markdown, width=width, height=12)
    _assert_table_columns_aligned(rows, col_count=2, width=width)
    commands = [row.split("│")[1].strip() for row in rows if row.count("│") == 3]
    assert commands == ["Command", "/copy", "/copy all"]
    top = next(row for row in rows if row.startswith("┌"))
    assert top.split("┬")[0] == "┌" + "─" * (len("/copy all") + 2)


async def test_block_style_cached_matches_uncached_render() -> None:
    """The block-style cache must produce the same render as a fresh lookup.

    Parallels the fence equivalence test but targets the Tier 3 block-style
    cache: drop just that cache, render the same crop, and assert bytes match.
    """
    md = "# Heading\n\nA paragraph.\n\n```python\nprint('hi')\n```\n"
    app = _MdApp(md)
    async with app.run_test(size=(50, 20)) as pilot:
        widget = app.widget
        assert widget is not None
        await wait_for(lambda: widget.region.width == 50, pilot=pilot, description="markdown initial width")

        crop = Region(0, 0, 50, 20)

        widget._line_cache.clear()
        widget._lines_cache_result = None
        with_cache = widget._render_lines_direct(crop, None)

        widget._block_style_cache.clear()
        widget._line_cache.clear()
        widget._lines_cache_result = None
        without_cache = widget._render_lines_direct(crop, None)

        for y, (a, b) in enumerate(zip(with_cache, without_cache, strict=True)):
            assert a.cell_length == b.cell_length, f"row {y} differs in cell_length"
            assert [(s.text, s.style) for s in a._segments] == [(s.text, s.style) for s in b._segments], (
                f"row {y} segments differ between cached and uncached block-style paths"
            )
