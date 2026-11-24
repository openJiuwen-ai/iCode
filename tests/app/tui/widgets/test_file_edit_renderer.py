# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for file-edit tool renderers."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from rich.color import Color, ColorSystem
from textual.app import App, ComposeResult
from textual.content import Content
from textual.geometry import Offset, Region
from textual.highlight import HighlightTheme
from textual.selection import Selection
from textual.strip import Strip
from textual.widgets import Static

from chrys.app.tui.support.gc_freeze import GcAbsorbReason, GcAbsorbRequested
from chrys.app.tui.theme import (
    CHRYS_ANSI_THEME,
    CHRYS_THEME,
    TUI_VARIABLE_DEFAULTS,
)
from chrys.app.tui.widgets.chat.file_snapshot import (
    FileSnapshotRef,
    file_snapshot_inline_char_limit,
    should_externalize_snapshot,
)
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.file_edit import (
    _EDIT_FILE_INLINE_MAX_DISPLAY_LINES,
    _FILE_COPY_FULL_DIFF_MAX_CHARS,
    _WRITE_FILE_LINE_COUNT_SCAN_CHARS,
    EditFileToolCall,
    WriteFileToolCall,
)
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolCardHeader, ToolGroup
from chrys.app.tui.widgets.diff_view import CodeColumn, DiffView, GutterColumn
from chrys.app.tui.widgets.diff_view.cells import annotation_cell
from chrys.app.tui.widgets.diff_view.compute import compute_highlighted_lines, compute_hunks
from chrys.app.tui.widgets.diff_view.inline import InlineUnifiedDiffLines
from chrys.app.tui.widgets.diff_view.palette import DARK, LIGHT, DiffLook
from chrys.app.tui.widgets.diff_view.rows import DiffRow, RowKind, unified_rows
from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines
from chrys.app.tui.widgets.file_preview import WRITE_FILE_PREVIEW_MAX_LINE_CHARS
from chrys.foundation.config.process_settings import reset_process_settings
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_WRITE
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY
from chrys.service.mutations.store import SnapshotStore
from tests.support.paths import SRC_ROOT
from tests.support.tui_helpers import ChatPanelApp, open_process_for
from tests.support.waiting import wait_for

_CHRYS_CSS = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"


def _content_bg_rgb(content: Content) -> tuple[int, int, int] | None:
    for _text, style, _control in content.render_segments():
        if style is None or style.bgcolor is None or style.bgcolor.triplet is None:
            continue
        triplet = style.bgcolor.triplet
        return (triplet.red, triplet.green, triplet.blue)
    return None


def _strip_bg_rgbs(strip: Strip) -> set[tuple[int, int, int]]:
    return {
        (triplet.red, triplet.green, triplet.blue)
        for segment in strip
        if segment.style is not None
        and segment.style.bgcolor is not None
        and (triplet := segment.style.bgcolor.triplet) is not None
    }


def _rgb(color: str) -> tuple[int, int, int]:
    triplet = Color.parse(color).triplet
    assert triplet is not None
    return (triplet.red, triplet.green, triplet.blue)


async def _wait_for_inline_diff(pilot, tool: EditFileToolCall | WriteFileToolCall) -> InlineUnifiedDiffLines:
    def first_diff_ready() -> bool:
        matches = list(tool.query(InlineUnifiedDiffLines))
        # Textual can lay out the child before the parent's awaited mount
        # resumes and clears its pending state.
        return (
            not tool._diff_mounting
            and not tool._diff_pending
            and bool(matches)
            and matches[0].is_mounted
            and matches[0].region.width > 0
        )

    await wait_for(
        first_diff_ready,
        pilot=pilot,
        description="inline diff mount has completed and its child is laid out",
    )
    return tool.query_one(InlineUnifiedDiffLines)


class _WriteFileToolApp(App):
    def compose(self) -> ComposeResult:
        yield WriteFileToolCall("c1", "write_file", args={"path": "dist/index.html"})


class _EditFileToolApp(App):
    def compose(self) -> ComposeResult:
        yield EditFileToolCall("c1", "edit_file", args={"path": "src/app.py"})


class _GcEditFileToolApp(_EditFileToolApp):
    def __init__(self) -> None:
        self.gc_messages: list[GcAbsorbRequested] = []
        self.prewarmed_at_request = False
        super().__init__()

    def on_gc_absorb_requested(self, message: GcAbsorbRequested) -> None:
        diffs = list(self.query(InlineUnifiedDiffLines))
        self.prewarmed_at_request = bool(diffs and diffs[0]._strip_cache)
        self.gc_messages.append(message)


class _FileToolInstanceApp(App):
    def __init__(self, tool: EditFileToolCall | WriteFileToolCall) -> None:
        super().__init__()
        self._tool = tool

    def compose(self) -> ComposeResult:
        yield self._tool


class _ThemeSwitchDiffApp(App):
    CSS_PATH = str(_CHRYS_CSS)

    def get_theme_variable_defaults(self) -> dict[str, str]:
        return dict(TUI_VARIABLE_DEFAULTS)

    def __init__(self, *, before: str = "same\n", after: str = "same\n", theme_name: str = "chrys") -> None:
        super().__init__()
        self._before = before
        self._after = after
        self.register_theme(CHRYS_THEME)
        self.register_theme(CHRYS_ANSI_THEME)
        self.theme = theme_name

    def compose(self) -> ComposeResult:
        diff = DiffView("same.py", "same.py", self._before, self._after)
        diff.split = False
        yield diff


class _LargeOneLineHtml(str):
    """Sentinel for minified/generated HTML where full line splitting is unsafe."""

    def splitlines(self, keepends: bool = False) -> list[str]:
        raise AssertionError("write_file renderer split the full HTML snapshot")

    def count(self, sub: str, start: int | None = None, end: int | None = None) -> int:
        if sub == "\n" and (start is None or end is None or end > _WRITE_FILE_LINE_COUNT_SCAN_CHARS):
            raise AssertionError("write_file renderer counted the full HTML snapshot")
        return super().count(sub, 0 if start is None else start, len(self) if end is None else end)


def test_diff_view_changed_gutters_survive_256_color_downgrade() -> None:
    expected_gutter_256_color = {RowKind.ADDED: 22, RowKind.REMOVED: 52}
    for kind, expected in expected_gutter_256_color.items():
        gutter_bg = Color.parse(DARK.number[kind].rsplit(" on ", 1)[1])
        assert gutter_bg.downgrade(ColorSystem.EIGHT_BIT).number == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 128 * 1024), ("4096", 4096), ("-1", 0), ("not-an-int", 128 * 1024)],
)
def test_snapshot_inline_limit_parse_is_fail_soft(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
    expected: int,
) -> None:
    """Same fail-soft grammar as the module-level parser this replaced."""
    if raw is None:
        monkeypatch.delenv("CHRYS_TUI_FILE_SNAPSHOT_INLINE_CHARS", raising=False)
    else:
        monkeypatch.setenv("CHRYS_TUI_FILE_SNAPSHOT_INLINE_CHARS", raw)
    reset_process_settings()

    assert file_snapshot_inline_char_limit() == expected


def test_live_snapshot_externalization_uses_utf8_byte_size() -> None:
    text = "你" * (file_snapshot_inline_char_limit() // len("你".encode()) + 1)

    assert len(text) < file_snapshot_inline_char_limit()
    assert len(text.encode("utf-8")) > file_snapshot_inline_char_limit()
    assert should_externalize_snapshot(("", text)) is True


def test_diff_view_tcss_theme_variables_do_not_render_as_syntax_errors() -> None:
    diff = DiffView(
        "theme.tcss",
        "theme.tcss",
        "",
        "border: round $primary;\nborder-subtitle-color: $text-muted;\n",
    )

    _lines_before, lines_after = diff.highlighted_lines
    assert "$primary" in lines_after[0].plain
    assert "$text-muted" in lines_after[1].plain
    assert all("error-muted" not in str(span.style) for line in lines_after for span in line.spans)


def test_diff_highlighting_uses_path_language_without_content_guess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Known file paths should avoid expensive content-based lexer guessing."""
    from chrys.app.tui.widgets.diff_view import compute as compute_module

    def fail_guess_language(_code: str, _path: str | None) -> str:
        raise AssertionError("diff highlighting should not call content language guessing for .py paths")

    monkeypatch.setattr(compute_module.highlight, "guess_language", fail_guess_language)

    before = "def old() -> int:\n    return 1\n"
    after = "def new() -> int:\n    return 2\n"
    lines_before, lines_after = compute_highlighted_lines(
        before,
        after,
        "src/example.py",
        "src/example.py",
        compute_hunks(before, after),
    )

    assert [line.plain for line in lines_before] == before.splitlines()
    assert [line.plain for line in lines_after] == after.splitlines()


def test_diff_highlighting_fast_path_includes_csharp(monkeypatch: pytest.MonkeyPatch) -> None:
    """C# files should use the path fast path instead of content guessing."""
    from chrys.app.tui.widgets.diff_view import compute as compute_module

    def fail_guess_language(_code: str, _path: str | None) -> str:
        raise AssertionError("diff highlighting should not call content language guessing for .cs paths")

    monkeypatch.setattr(compute_module.highlight, "guess_language", fail_guess_language)

    before = "public sealed class Example { }\n"
    after = "public sealed class Example { public int Value => 1; }\n"
    _lines_before, lines_after = compute_highlighted_lines(
        before,
        after,
        "src/Example.cs",
        "src/Example.cs",
        compute_hunks(before, after),
    )

    assert lines_after[0].plain == after.rstrip("\n")


def test_diff_highlighting_unknown_same_path_guesses_after_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown same-path diffs should not reuse an empty before-side content guess."""
    from chrys.app.tui.widgets.diff_view import compute as compute_module

    guess_calls: list[tuple[str, str | None]] = []

    def fake_guess_language(code: str, path: str | None) -> str:
        guess_calls.append((code, path))
        return "text" if not code else "python"

    monkeypatch.setattr(compute_module.highlight, "guess_language", fake_guess_language)

    before = ""
    after = "def hello() -> int:\n    return 1\n"
    lines_before, lines_after = compute_highlighted_lines(
        before,
        after,
        "notes.unknown",
        "notes.unknown",
        compute_hunks(before, after),
    )

    assert lines_before == []
    assert [line.plain for line in lines_after] == after.splitlines()
    assert guess_calls == [(before, "notes.unknown"), (after, "notes.unknown")]


@pytest.mark.parametrize(
    ("tool_cls", "tool_name", "args"),
    [
        (WriteFileToolCall, "write_file", {"path": "dist/index.html"}),
        (EditFileToolCall, "edit_file", {"path": "src/app.py"}),
    ],
)
def test_file_tool_renderers_use_base_tool_card_contract(
    tool_cls: type[EditFileToolCall] | type[WriteFileToolCall],
    tool_name: str,
    args: dict[str, str],
) -> None:
    tool = tool_cls("c1", tool_name, args=args)

    assert isinstance(tool, BaseToolCard)
    assert tool.call_id == "c1"
    assert tool.tool_name == tool_name
    assert tool.status == "running"
    assert tool.result_text == ""
    assert tool.duration_ms == 0
    assert tool.args == args


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("src/main.cxx", "cpp"),
        ("include/widget.hh", "cpp"),
        ("src/App.mm", "objective-c++"),
        ("src/Script.csx", "csharp"),
        ("src/App.csproj", "xml"),
        ("src/Library.fs", "fsharp"),
        ("src/Library.fsi", "fsharp"),
        ("src/Program.fsx", "fsharp"),
        ("src/App.vb", "vb.net"),
        ("src/Main.scala", "scala"),
        ("src/app.dart", "dart"),
        ("src/pages/Index.ets", "typescript"),
        ("src/pages/Index.arkts", "typescript"),
        ("src/index.mjs", "javascript"),
        ("src/App.vue", "vue"),
        ("src/App.svelte", "html"),
        ("tsconfig.jsonc", "javascript"),
        ("src/config.jsonl", "json"),
        ("setup.cfg", "ini"),
        (".env", "text"),
        (".gitignore", "text"),
        ("src/icon.svg", "xml"),
        ("src/App.xaml", "xml"),
        ("Directory.Build.props", "xml"),
        ("src/script.ps1", "powershell"),
        ("scripts/login.ksh", "bash"),
        ("scripts/build.bat", "batch"),
        ("shader.frag", "glsl"),
        ("shader.vert", "glsl"),
        ("schema.gql", "graphql"),
        ("README.mdx", "markdown"),
        ("CMakeLists.txt", "cmake"),
        ("go.mod", "text"),
        ("Pipfile", "toml"),
        ("Gemfile", "ruby"),
        ("Dockerfile.dev", "docker"),
        ("Makefile.am", "make"),
    ],
)
def test_diff_language_fast_path_covers_common_project_files(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    language: str,
) -> None:
    """Common project file names should avoid content-based language guessing."""
    from chrys.app.tui.widgets.diff_view import compute as compute_module

    def fail_guess_language(_code: str, _path: str | None) -> str:
        raise AssertionError(f"diff highlighting should not content-guess {path}")

    monkeypatch.setattr(compute_module.highlight, "guess_language", fail_guess_language)

    assert compute_module._guess_diff_language("placeholder\n", path) == language


def test_sparse_diff_highlighting_preserves_multiline_lexer_state() -> None:
    """Sparse highlighting should keep lexer state for hunks inside multi-line strings."""
    before = 'x = """\n' + "\n".join(f"line {index}" for index in range(80)) + '\n"""\n'
    after = 'x = """\n' + "\n".join("changed" if index == 40 else f"line {index}" for index in range(80)) + '\n"""\n'
    hunks = compute_hunks(before, after)
    _tag, _i1, _i2, changed_start, _changed_end = next(
        opcode for hunk in hunks for opcode in hunk if opcode[0] == "replace"
    )

    _full_before, full_after = compute_highlighted_lines(
        before,
        after,
        "src/example.py",
        "src/example.py",
        hunks,
        hunks_only=False,
    )
    _sparse_before, sparse_after = compute_highlighted_lines(
        before,
        after,
        "src/example.py",
        "src/example.py",
        hunks,
        hunks_only=True,
    )

    def syntax_spans(content: Content) -> list[tuple[int, int, str]]:
        return [(span.start, span.end, str(span.style)) for span in content.spans if " on " not in str(span.style)]

    assert syntax_spans(sparse_after[changed_start]) == syntax_spans(full_after[changed_start])


def test_sparse_diff_highlighting_preserves_trailing_context_blank_line() -> None:
    """Sparse highlighting must not shrink ranges that end on interior blank lines."""
    before = "a = 1\nb = 2\nc = 3\n\nd = 4\ne = 5\nf = 6\ng = 7\n"
    after = "a = 10\nb = 2\nc = 3\n\nd = 4\ne = 5\nf = 6\ng = 7\n"
    hunks = compute_hunks(before, after)
    lines_before, lines_after = compute_highlighted_lines(
        before,
        after,
        "src/example.py",
        "src/example.py",
        hunks,
        hunks_only=True,
    )
    rows = unified_rows(hunks, lines_before, lines_after)

    assert [line.plain for line in lines_before] == before.splitlines()
    assert [line.plain for line in lines_after] == after.splitlines()
    assert [row.code.plain for row in rows if row.kind is RowKind.CONTEXT and row.code is not None] == [
        "b = 2",
        "c = 3",
        "",
    ]


@pytest.mark.asyncio
async def test_inline_diff_prepare_highlights_no_further_than_the_last_hunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inline previews should not syntax-highlight the unchanged rest of a file."""
    from chrys.app.tui.widgets.diff_view import compute as compute_module

    original_highlight = compute_module.highlight.highlight
    highlighted_line_counts: list[int] = []

    def highlight_spy(code: str, *, language: str, path: str, theme: type[HighlightTheme]) -> Content:
        highlighted_line_counts.append(len(code.splitlines()))
        return original_highlight(code, language=language, path=path, theme=theme)

    monkeypatch.setattr(compute_module.highlight, "highlight", highlight_spy)

    before_lines = [f"VALUE_{index} = {index}" for index in range(300)]
    after_lines = before_lines.copy()
    after_lines[50] = "VALUE_50 = 5000"
    diff = InlineUnifiedDiffLines(
        "src/example.py",
        "src/example.py",
        "\n".join(before_lines) + "\n",
        "\n".join(after_lines) + "\n",
        auto_height=True,
    )

    await diff.prepare()

    # Line 50 changed: each side is highlighted down to the context under it, and no further.
    assert highlighted_line_counts == [54, 54]
    assert [row.code.plain for row in diff.rows if row.kind is not RowKind.CONTEXT and row.code is not None] == [
        "VALUE_50 = 50",
        "VALUE_50 = 5000",
    ]


@pytest.mark.asyncio
async def test_diff_view_uses_explicit_full_line_backgrounds_in_256_color_mode() -> None:
    async with _ThemeSwitchDiffApp(before="", after="added\n").run_test() as pilot:
        await pilot.pause()
        pilot.app.console._color_system = ColorSystem.EIGHT_BIT
        code_column = pilot.app.query_one(CodeColumn)

        assert DARK.line_style(RowKind.ADDED, "256") == "on #005F00"
        assert DARK.line_style(RowKind.REMOVED, "256") == "on #5F0000"
        assert DARK.line_style(RowKind.ADDED, "truecolor") == DARK.line[RowKind.ADDED]

        look = DiffLook.of(code_column)
        assert look.color_system == "256"
        assert _content_bg_rgb(annotation_cell(DiffRow(RowKind.ADDED), look)) == (0, 95, 0)
        assert _content_bg_rgb(annotation_cell(DiffRow(RowKind.REMOVED), look)) == (95, 0, 0)

        assert _rgb("#005F00") in _strip_bg_rgbs(code_column.render_line(0))


@pytest.mark.asyncio
async def test_diff_view_preserves_intraline_highlights_in_256_color_mode() -> None:
    async with _ThemeSwitchDiffApp(before="spam\n", after="span\n").run_test() as pilot:
        await pilot.pause()
        pilot.app.console._color_system = ColorSystem.EIGHT_BIT
        code_column = pilot.app.query_one(CodeColumn)

        strip = code_column.render_line(0)
        row_bg = _rgb("#5F0000")
        emphasized = [segment for segment in strip if segment.text == "m"]

        assert len(emphasized) == 1
        assert row_bg in _strip_bg_rgbs(strip)
        assert _strip_bg_rgbs(Strip(emphasized)) not in (set(), {row_bg})


@pytest.mark.asyncio
async def test_diff_view_uses_light_palette_for_light_themes() -> None:
    async with _ThemeSwitchDiffApp(before="", after="added\n", theme_name="ansi-light").run_test() as pilot:
        await pilot.pause()
        code_column = pilot.app.query_one(CodeColumn)

        assert DiffLook.of(code_column).palette is LIGHT
        assert _rgb(LIGHT.line[RowKind.ADDED].removeprefix("on ")) in _strip_bg_rgbs(code_column.render_line(0))


@pytest.mark.asyncio
async def test_diff_view_repaints_in_place_when_theme_brightness_switches() -> None:
    dark_bg = _rgb(DARK.line[RowKind.ADDED].removeprefix("on "))
    light_bg = _rgb(LIGHT.line[RowKind.ADDED].removeprefix("on "))

    async with _ThemeSwitchDiffApp(before="", after="added\n").run_test() as pilot:
        await pilot.pause()
        # What the host's console makes of color is no part of this: a 256-color one, which a Windows
        # runner has, gets the dark palette's other greens.
        pilot.app.console._color_system = ColorSystem.TRUECOLOR
        code_column = pilot.app.query_one(CodeColumn)
        gutters = list(pilot.app.query(GutterColumn))
        assert dark_bg in _strip_bg_rgbs(code_column.render_line(0))

        pilot.app.theme = "ansi-light"
        await wait_for(
            lambda: light_bg in _strip_bg_rgbs(code_column.render_line(0)),
            pilot=pilot,
            description="the diff's code column paints the light palette",
        )

        # The palette is looked up while painting: the widgets that were mounted are still the ones showing.
        assert pilot.app.query_one(CodeColumn) is code_column
        assert list(pilot.app.query(GutterColumn)) == gutters
        assert dark_bg not in _strip_bg_rgbs(code_column.render_line(0))
        annotation = gutters[-1]
        assert light_bg in _strip_bg_rgbs(annotation.render_line(0))


@pytest.mark.asyncio
async def test_inline_unified_diff_refreshes_palette_when_theme_brightness_switches() -> None:
    dark_bg = _rgb(DARK.line[RowKind.ADDED].removeprefix("on "))
    light_bg = _rgb(LIGHT.line[RowKind.ADDED].removeprefix("on "))

    async with App().run_test(size=(80, 12)) as pilot:
        diff = InlineUnifiedDiffLines("added.py", "added.py", "", "added\n", auto_height=True)
        await diff.prepare()
        await pilot.app.mount(diff)
        await pilot.pause()
        pilot.app.console._color_system = ColorSystem.TRUECOLOR

        assert dark_bg in _strip_bg_rgbs(diff.render_line(0))

        pilot.app.theme = "ansi-light"
        await wait_for(
            lambda: light_bg in _strip_bg_rgbs(diff.render_line(0)),
            pilot=pilot,
            description="the inline diff paints the light palette",
        )

        assert dark_bg not in _strip_bg_rgbs(diff.render_lines(Region(0, 0, diff.size.width, 1))[0])


@pytest.mark.asyncio
async def test_inline_unified_diff_render_lines_after_remove_returns_blank() -> None:
    async with App().run_test(size=(80, 12)) as pilot:
        diff = InlineUnifiedDiffLines("added.py", "added.py", "", "added\n", auto_height=True)
        await diff.prepare()
        await pilot.app.mount(diff)
        await pilot.pause()

        await diff.remove()
        await pilot.pause()

        rendered = diff.render_lines(Region(0, 0, 20, 2))

    assert [strip.cell_length for strip in rendered] == [20, 20]


@pytest.mark.asyncio
async def test_code_column_render_lines_after_remove_returns_blank() -> None:
    async with _ThemeSwitchDiffApp(before="", after="added\n").run_test(size=(80, 12)) as pilot:
        await pilot.pause()
        code_column = pilot.app.query_one(CodeColumn)

        await code_column.remove()
        await pilot.pause()

        rendered = code_column.render_lines(Region(0, 0, 20, 2))

    assert [strip.cell_length for strip in rendered] == [20, 20]


def test_gutter_column_render_lines_unattached_returns_blank() -> None:
    rows = [DiffRow(RowKind.ADDED, Content("added"), after=1)]
    code_column = CodeColumn(rows, 5)
    annotation = GutterColumn(rows, annotation_cell, 3, code_column)

    rendered = annotation.render_lines(Region(0, 0, 20, 2))

    assert [strip.cell_length for strip in rendered] == [20, 20]


@pytest.mark.asyncio
async def test_diff_view_invalidates_render_cache_when_theme_switches() -> None:
    async with _ThemeSwitchDiffApp().run_test() as pilot:
        await pilot.pause()
        code_column = pilot.app.query_one(CodeColumn)
        crop = Region(0, 0, code_column.size.width, 1)

        old_lines = code_column.render_lines(crop)
        # Nothing moved, so a second repaint hands the first one's strips out again.
        assert code_column.render_lines(crop) is old_lines

        pilot.app.theme = "chrys-ansi"

        assert code_column.render_lines(crop) is not old_lines
        await pilot.pause()
        assert code_column.render_lines(crop) is not old_lines


@pytest.mark.asyncio
async def test_diff_view_gutter_cache_tracks_color_system() -> None:
    async with _ThemeSwitchDiffApp(before="", after="added\n").run_test() as pilot:
        await pilot.pause()
        annotation = list(pilot.app.query(GutterColumn))[-1]
        crop = Region(0, 0, annotation.size.width, 1)

        # Force TRUECOLOR explicitly so the assertion is deterministic across
        # platforms (Windows CI defaults to a downsampled color system).
        pilot.app.console._color_system = ColorSystem.TRUECOLOR
        assert _strip_bg_rgbs(annotation.render_lines(crop)[0]) == {(36, 63, 48)}

        pilot.app.console._color_system = ColorSystem.EIGHT_BIT

        assert _strip_bg_rgbs(annotation.render_lines(crop)[0]) == {(0, 95, 0)}


@pytest.mark.asyncio
async def test_unified_diff_without_scrollbars_uses_flat_renderer() -> None:
    class _InlineUnifiedDiffApp(App):
        def compose(self) -> ComposeResult:
            diff = DiffView("added.py", "added.py", "", "added\n")
            diff.split = False
            diff.auto_height = True
            diff.show_scrollbars = False
            yield diff

    async with _InlineUnifiedDiffApp().run_test() as pilot:
        await pilot.pause()
        flat = pilot.app.query_one(UnifiedDiffLines)

        assert not list(pilot.app.query(CodeColumn))
        rendered = flat.render_lines(Region(0, 0, flat.size.width, 1))
        assert "+ added" in "".join(segment.text for segment in rendered[0]._segments)
        assert flat.ALLOW_SELECT
        selected, _separator = flat.get_selection(Selection(Offset(9, 0), Offset(14, 0)))
        assert selected == "added"
        full_row_selected, _separator = flat.get_selection(Selection(Offset(0, 0), Offset(flat.size.width, 0)))
        assert full_row_selected.rstrip() == "added"

        pilot.app.console._color_system = ColorSystem.EIGHT_BIT
        assert _rgb("#005F00") in _strip_bg_rgbs(flat.render_lines(Region(0, 0, flat.size.width, 1))[0])
        assert _rgb("#005F00") in _strip_bg_rgbs(flat.render_line(0))


@pytest.mark.asyncio
async def test_code_column_picks_up_color_system_change_without_being_told() -> None:
    """The line backgrounds are resolved once per look of the app, and a changed color system is a new look.

    Nothing announces that the console's color system changed. If the column kept the rows and
    the palette it resolved for the old one, the 256-color backgrounds would silently not apply.
    """
    truecolor_bg = _rgb(DARK.line[RowKind.ADDED].removeprefix("on "))
    downgraded_bg = _rgb("#005F00")

    async with _ThemeSwitchDiffApp(before="", after="added\n").run_test() as pilot:
        await pilot.pause()
        code_column = pilot.app.query_one(CodeColumn)
        crop = Region(0, 0, code_column.size.width, 1)

        pilot.app.console._color_system = ColorSystem.TRUECOLOR
        backgrounds = _strip_bg_rgbs(code_column.render_lines(crop)[0])
        assert truecolor_bg in backgrounds
        assert downgraded_bg not in backgrounds

        pilot.app.console._color_system = ColorSystem.EIGHT_BIT
        backgrounds = _strip_bg_rgbs(code_column.render_lines(crop)[0])
        assert downgraded_bg in backgrounds
        assert truecolor_bg not in backgrounds
        assert downgraded_bg in _strip_bg_rgbs(code_column.render_line(0))


@pytest.mark.asyncio
async def test_flat_unified_diff_annotations_toggle_updates_after_mount() -> None:
    class _InlineUnifiedDiffApp(App):
        def compose(self) -> ComposeResult:
            diff = DiffView("added.py", "added.py", "", "added\n")
            diff.split = False
            diff.auto_height = True
            diff.show_scrollbars = False
            yield diff

    async with _InlineUnifiedDiffApp().run_test() as pilot:
        await pilot.pause()
        diff = pilot.app.query_one(DiffView)
        flat = pilot.app.query_one(UnifiedDiffLines)
        assert flat.annotations is True

        rendered = flat.render_lines(Region(0, 0, flat.size.width, 1))
        plain = "".join(segment.text for segment in rendered[0]._segments)

        assert "+ added" in plain

        diff.annotations = False
        await pilot.pause()
        assert flat.annotations is False
        rendered = flat.render_lines(Region(0, 0, flat.size.width, 1))
        plain = "".join(segment.text for segment in rendered[0]._segments)

        assert "added" in plain
        assert "+ added" not in plain

        diff.annotations = True
        await pilot.pause()
        assert flat.annotations is True
        rendered = flat.render_lines(Region(0, 0, flat.size.width, 1))
        plain = "".join(segment.text for segment in rendered[0]._segments)

        assert "+ added" in plain


@pytest.mark.asyncio
async def test_flat_unified_diff_multi_row_selection_copies_code_only() -> None:
    class _InlineUnifiedDiffApp(App):
        def compose(self) -> ComposeResult:
            diff = DiffView("added.py", "added.py", "", "first\nsecond\n")
            diff.split = False
            diff.auto_height = True
            diff.show_scrollbars = False
            yield diff

    async with _InlineUnifiedDiffApp().run_test() as pilot:
        await pilot.pause()
        flat = pilot.app.query_one(UnifiedDiffLines)

        selected, _separator = flat.get_selection(Selection(Offset(0, 0), Offset(flat.size.width, 1)))

        assert selected == "first\nsecond"


@pytest.mark.asyncio
async def test_flat_unified_diff_selection_preserves_blank_context_break_row() -> None:
    before = "\n".join(f"line {index}" for index in range(30)) + "\n"
    after_lines = [f"line {index}" for index in range(30)]
    after_lines[0] = "changed 0"
    after_lines[29] = "changed 29"
    after = "\n".join(after_lines) + "\n"

    class _InlineUnifiedDiffApp(App):
        def compose(self) -> ComposeResult:
            diff = DiffView("changed.py", "changed.py", before, after)
            diff.split = False
            diff.auto_height = True
            diff.show_scrollbars = False
            yield diff

    async with _InlineUnifiedDiffApp().run_test() as pilot:
        await pilot.pause()
        flat = pilot.app.query_one(UnifiedDiffLines)
        break_index = next(index for index, row in enumerate(flat.rows) if row.kind is RowKind.BREAK)

        selected, _separator = flat.get_selection(
            Selection(Offset(0, break_index - 1), Offset(flat.size.width, break_index + 1))
        )

        assert selected == "line 3\n\nline 26"


@pytest.mark.asyncio
async def test_write_file_large_one_line_html_result_does_not_scan_full_snapshot() -> None:
    """Reproduce the frozen `Running: write_file` path with minified HTML.

    The live ToolCallResult handler cannot reset the status bar until
    WriteFileToolCall.set_complete() returns.  For generated HTML, line-count
    and preview code must be bounded; calling splitlines() on the full snapshot
    can stall or corrupt the TUI without an exception.
    """
    html = _LargeOneLineHtml("<!doctype html><html><body>" + ("x" * 1_000_000) + "</body></html>")

    async with _WriteFileToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)
        header = tool.query_one(ToolCardHeader)
        assert header.actions_visible is False

        tool.set_complete(
            "Written 1000039 chars (1 lines) to /repo/dist/index.html.",
            25,
            file_snapshot=("", html),
        )
        assert header.actions_visible is True
        diff = await _wait_for_inline_diff(pilot, tool)

        assert tool.status == "complete"
        assert diff.code_after.endswith("...")
        assert len(diff.code_after) <= WRITE_FILE_PREVIEW_MAX_LINE_CHARS + len("...")


@pytest.mark.asyncio
async def test_write_file_line_count_fallback_is_bounded() -> None:
    html = _LargeOneLineHtml("<!doctype html><html><body>" + ("x" * 1_000_000) + "</body></html>")

    async with _WriteFileToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)

        tool.set_complete(
            "Successfully wrote dist/index.html.",
            25,
            file_snapshot=("", html),
        )
        await pilot.pause()

        panel = tool.query_one("#ft-panel")
        assert panel.border_subtitle == "1+ lines written"


@pytest.mark.parametrize(
    ("tool_cls", "tool_name", "args"),
    [
        (WriteFileToolCall, "write_file", {"path": "dist/index.html"}),
        (EditFileToolCall, "edit_file", {"path": "src/app.py"}),
    ],
)
@pytest.mark.asyncio
async def test_file_tool_renderers_record_rejected_completion_approval(
    tool_cls: type[EditFileToolCall] | type[WriteFileToolCall],
    tool_name: str,
    args: dict[str, str],
) -> None:
    tool = tool_cls("c1", tool_name, args=args)

    async with _FileToolInstanceApp(tool).run_test() as pilot:
        mounted = pilot.app.query_one(tool_cls)

        mounted.set_complete("Error: rejected", duration_ms=10, approval="user_rejected")
        await pilot.pause()

        assert mounted.approval == "user_rejected"
        assert mounted.has_class("-rejected")
        assert mounted.query_one("#ft-panel").border_subtitle == "Rejected"
        assert mounted.query_one("#ft-content").query_one(Static).render().plain == "Error: rejected"


@pytest.mark.parametrize(
    ("tool_cls", "tool_name", "args"),
    [
        (WriteFileToolCall, "write_file", {"path": "dist/index.html"}),
        (EditFileToolCall, "edit_file", {"path": "src/app.py"}),
    ],
)
@pytest.mark.asyncio
async def test_file_tool_renderers_use_structured_failure_metadata(
    tool_cls: type[EditFileToolCall] | type[WriteFileToolCall],
    tool_name: str,
    args: dict[str, str],
) -> None:
    tool = tool_cls("c1", tool_name, args=args)

    async with _FileToolInstanceApp(tool).run_test() as pilot:
        mounted = pilot.app.query_one(tool_cls)

        mounted.set_complete(
            "Error: literal file contents",
            duration_ms=10,
            metadata={TOOL_FAILED_METADATA_KEY: False},
            file_snapshot=("before\n", "after\n"),
        )
        await pilot.pause()

        assert mounted.has_class("-success")
        assert not mounted.has_class("-error")


@pytest.mark.parametrize(
    ("tool_cls", "tool_name", "args"),
    [
        (WriteFileToolCall, "write_file", {"path": "dist/index.html"}),
        (EditFileToolCall, "edit_file", {"path": "src/app.py"}),
    ],
)
@pytest.mark.asyncio
async def test_file_tool_renderers_set_error_status_for_failed_results(
    tool_cls: type[EditFileToolCall] | type[WriteFileToolCall],
    tool_name: str,
    args: dict[str, str],
) -> None:
    tool = tool_cls("c1", tool_name, args=args)

    async with _FileToolInstanceApp(tool).run_test() as pilot:
        mounted = pilot.app.query_one(tool_cls)

        mounted.set_complete("Error: failed", duration_ms=10, metadata={TOOL_FAILED_METADATA_KEY: True})
        await pilot.pause()

        assert mounted.status == "error"
        assert mounted.has_class("-error")


@pytest.mark.parametrize(
    ("tool_cls", "tool_name"), [(EditFileToolCall, "edit_file"), (WriteFileToolCall, "write_file")]
)
@pytest.mark.parametrize("mode", ["live", "lazy", "collapsed"])
@pytest.mark.asyncio
async def test_disk_backed_snapshot_mounts_when_group_is_expanded(
    tmp_path: Path, tool_cls: type[EditFileToolCall] | type[WriteFileToolCall], tool_name: str, mode: str
) -> None:
    store = SnapshotStore(tmp_path)
    before_hash = store.save_data_as_blob(b"old\n").content_hash
    after_hash = store.save_data_as_blob(b"new\n").content_hash
    ref = FileSnapshotRef(store.mutations_dir, before_hash, after_hash)

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({tool_name: KIND_FILESYSTEM_WRITE})
        await panel.add_user_message("change file")
        await panel.add_tool_start("file1", tool_name, KIND_FILESYSTEM_WRITE, args={"path": "src/app.py"})
        # Keep the group live so a collapse defers the diff without pruning
        # and replaying the card whose completion is under test.
        await panel.add_tool_start("sibling", "plain_tool", "", args={})
        group = panel.query_one(ToolGroup)
        open_process_for(group)
        group.collapsed = False
        assert not group.collapsed
        tool = group.query_one(tool_cls)
        if mode == "collapsed":
            group.collapsed = True

        group.complete_tool("file1", "Changed src/app.py", 25, file_snapshot=ref, lazy=mode == "lazy")

        # Completing a disk-backed tool must not read its snapshots inline.
        assert tool._after_content is None
        assert not list(pilot.app.query(InlineUnifiedDiffLines))

        if mode == "lazy":
            assert tool._diff_pending is True
            assert tool._diff_timer is None
            await tool.mount_diff_if_pending()
        elif mode == "collapsed":
            await wait_for(
                lambda: tool._diff_timer is None and tool._diff_pending,
                pilot=pilot,
                description="collapsed live diff defers its scheduled mount",
            )
            assert tool._after_content is None
            assert not list(pilot.app.query(InlineUnifiedDiffLines))
            group.collapsed = False

        await _wait_for_inline_diff(pilot, tool)

        assert tool._get_after_content() == "new\n"
        assert tool.has_class("-done")
        assert not tool._diff_pending
        assert not tool.query_one("#ft-spinner").display
        assert not list(pilot.app.query(DiffView))
        assert len(list(pilot.app.query(InlineUnifiedDiffLines))) == 1


@pytest.mark.asyncio
async def test_inline_diff_wait_does_not_finish_while_visible_child_is_still_mounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({"edit_file": KIND_FILESYSTEM_WRITE})
        await panel.add_user_message("change file")
        await panel.add_tool_start("file1", "edit_file", KIND_FILESYSTEM_WRITE, args={"path": "src/app.py"})
        # A live sibling keeps the collapsed card mounted instead of pruning it.
        await panel.add_tool_start("sibling", "plain_tool", "", args={})
        group = panel.query_one(ToolGroup)
        open_process_for(group)
        group.collapsed = False
        tool = group.query_one(EditFileToolCall)
        group.collapsed = True
        group.complete_tool("file1", "Edited src/app.py", 25, file_snapshot=("old\n", "new\n"))
        # Deferring a live mount while collapsed marks the card done, which
        # exposes its content when reopened. A fresh lazy card stays hidden
        # until its first mount finishes and cannot reproduce this window.
        await wait_for(
            lambda: tool._diff_timer is None and tool._diff_pending and tool.has_class("-done"),
            pilot=pilot,
            description="collapsed live diff is deferred before reopening",
        )
        content = tool.query_one("#ft-content")
        original_mount = content.mount
        original_pause = pilot.pause
        child_mounted = asyncio.Event()
        release_mount = asyncio.Event()
        waiter_polled = asyncio.Event()

        async def hold_mounted_diff(widget: InlineUnifiedDiffLines) -> None:
            await original_mount(widget)
            child_mounted.set()
            await release_mount.wait()

        async def observe_waiter_pause(delay: float | None = None) -> None:
            waiter_polled.set()
            await original_pause(delay)

        monkeypatch.setattr(content, "mount", hold_mounted_diff)
        monkeypatch.setattr(pilot, "pause", observe_waiter_pause)
        group.collapsed = False
        worker = group._pending_content_worker
        assert worker is not None
        waiter: asyncio.Task[InlineUnifiedDiffLines] | None = None
        try:
            await wait_for(
                lambda: child_mounted.is_set() or worker.is_finished,
                description="inline diff child mounts before the parent resumes",
            )
            if worker.is_finished:
                await worker.wait()
            assert child_mounted.is_set()
            diff = tool.query_one(InlineUnifiedDiffLines)
            await wait_for(
                lambda: diff.is_mounted and diff.region.width > 0,
                description="held inline diff is visible",
            )
            assert tool._diff_mounting and tool._diff_pending

            waiter = asyncio.create_task(_wait_for_inline_diff(pilot, tool))
            await wait_for(
                lambda: waiter_polled.is_set() or waiter.done(),
                description="inline diff waiter observes the held mount",
            )
            assert not waiter.done()
        finally:
            release_mount.set()
            await worker.wait()
            if waiter is not None:
                await waiter

        assert not tool._diff_mounting
        assert not tool._diff_pending


@pytest.mark.asyncio
async def test_deferred_live_diff_posts_absorb_after_visible_rows_are_prewarmed() -> None:
    app = _GcEditFileToolApp()
    async with app.run_test(size=(80, 12)) as pilot:
        tool = app.query_one(EditFileToolCall)
        tool.set_complete(
            "Edited src/app.py",
            25,
            file_snapshot=("old\n", "new\n"),
        )

        await _wait_for_inline_diff(pilot, tool)
        await wait_for(
            lambda: app.gc_messages,
            pilot=pilot,
            description="file edit publishes its GC message",
        )

        assert len(app.gc_messages) == 1
        assert app.gc_messages[0].reason is GcAbsorbReason.STABLE_CONTENT_MOUNTED
        assert app.gc_messages[0].terminal_boundary is False
        assert app.prewarmed_at_request is True


@pytest.mark.asyncio
async def test_write_file_preview_truncation_keeps_raw_snapshot_for_copy() -> None:
    raw_line = "<!doctype html><html><body>" + ("x" * (WRITE_FILE_PREVIEW_MAX_LINE_CHARS + 20))
    raw_html = raw_line + "</body></html>"

    async with _WriteFileToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)

        tool.set_complete(
            f"Written {len(raw_html)} chars (1 lines) to /repo/dist/index.html.",
            25,
            file_snapshot=("", raw_html),
        )

        diff = await _wait_for_inline_diff(pilot, tool)
        assert raw_html not in diff.code_after
        assert diff.code_after.endswith("...")
        assert tool._get_after_content() == raw_html
        copy_sections = tool._tool_copy_sections()
        assert any(raw_html in text for _, _, text in copy_sections)


@pytest.mark.asyncio
async def test_edit_file_inline_diff_is_height_capped() -> None:
    before = "\n".join(f"old {index}" for index in range(80)) + "\n"
    after = "\n".join(f"new {index}" for index in range(80)) + "\n"

    async with _EditFileToolApp().run_test() as pilot:
        tool = pilot.app.query_one(EditFileToolCall)

        tool.set_complete("Edited src/app.py", 25, file_snapshot=(before, after))

        diff = await _wait_for_inline_diff(pilot, tool)
        assert diff.max_display_lines == _EDIT_FILE_INLINE_MAX_DISPLAY_LINES

        flat = pilot.app.query_one(UnifiedDiffLines)
        assert flat.styles.height.value == _EDIT_FILE_INLINE_MAX_DISPLAY_LINES


@pytest.mark.asyncio
async def test_file_tool_mounts_text_fallback_when_inline_diff_prepare_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_prepare(self: InlineUnifiedDiffLines) -> None:
        raise RuntimeError("prepare failed")

    monkeypatch.setattr(InlineUnifiedDiffLines, "prepare", fail_prepare)

    async with _WriteFileToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)
        result = "Written 4 chars (1 lines) to /repo/dist/index.html."

        tool.set_complete(result, 25, file_snapshot=("", "new\n"))

        await wait_for(
            lambda: tool.query_one("#ft-content").children,
            pilot=pilot,
            description="file tool content is restored",
        )

        assert not list(pilot.app.query(InlineUnifiedDiffLines))
        fallback = tool.query_one("#ft-content").query_one(Static)
        assert fallback.render().plain == result


@pytest.mark.asyncio
async def test_write_file_large_snapshot_copy_diff_is_bounded() -> None:
    html = _LargeOneLineHtml("<!doctype html><html><body>" + ("x" * 1_000_000) + "</body></html>")

    async with _WriteFileToolApp().run_test() as pilot:
        tool = pilot.app.query_one(WriteFileToolCall)

        tool.set_complete(
            "Written 1000039 chars (1 lines) to /repo/dist/index.html.",
            25,
            file_snapshot=("", html),
        )

        copy_sections = tool._tool_copy_sections()
        preview = next(text for title, _, text in copy_sections if title == "Diff Preview")
        assert "Full diff omitted" in preview
        assert len(preview) < _FILE_COPY_FULL_DIFF_MAX_CHARS
        assert "x" * 1000 not in preview


def test_large_edit_file_copy_preview_uses_real_unified_diff() -> None:
    before_lines = [f"line {i}" for i in range(80)]
    after_lines = before_lines.copy()
    after_lines[50] = "line fifty changed"
    before = "\n".join(before_lines) + "\n" + ("tail" * 20_000)
    after = "\n".join(after_lines) + "\n" + ("tail" * 20_000)
    tool = EditFileToolCall("c1", "edit_file", args={"path": "src/app.py"})
    tool.result_text = "Edited src/app.py"
    tool._before_content = before
    tool._after_content = after

    copy_sections = tool._tool_copy_sections()
    preview = next(text for title, _, text in copy_sections if title == "Diff Preview")

    assert "showing bounded diff around first change" in preview
    assert "-line 50" in preview
    assert "+line fifty changed" in preview
    assert "-line 0" not in preview
    assert "+line 0" not in preview
    assert "Bounded preview contains no changed lines" not in preview


def test_large_edit_file_copy_preview_keeps_dash_plus_prefixed_changes() -> None:
    before_lines = [f"line {i}" for i in range(80)]
    after_lines = before_lines.copy()
    before_lines[50] = "-- removed flag"
    after_lines[50] = "++ added flag"
    before = "\n".join(before_lines) + "\n" + ("tail" * 20_000)
    after = "\n".join(after_lines) + "\n" + ("tail" * 20_000)
    tool = EditFileToolCall("c1", "edit_file", args={"path": "src/app.py"})
    tool.result_text = "Edited src/app.py"
    tool._before_content = before
    tool._after_content = after

    copy_sections = tool._tool_copy_sections()
    preview = next(text for title, _, text in copy_sections if title == "Diff Preview")

    assert "--- removed flag" in preview
    assert "+++ added flag" in preview


def test_file_tool_copy_full_diff_includes_final_newline_change() -> None:
    tool = EditFileToolCall("c1", "edit_file", args={"path": "src/app.py"})
    tool.result_text = "Edited src/app.py"
    tool._before_content = "line"
    tool._after_content = "line\n"

    copy_sections = tool._tool_copy_sections()
    diff = next(text for title, _, text in copy_sections if title == "Diff")

    assert "-line [no newline]" in diff
    assert "+line [EOL: LF]" in diff


def test_file_tool_copy_full_diff_includes_line_ending_change() -> None:
    tool = EditFileToolCall("c1", "edit_file", args={"path": "src/app.py"})
    tool.result_text = "Edited src/app.py"
    tool._before_content = "line\r\n"
    tool._after_content = "line\n"

    copy_sections = tool._tool_copy_sections()
    diff = next(text for title, _, text in copy_sections if title == "Diff")

    assert "-line [EOL: CRLF]" in diff
    assert "+line [EOL: LF]" in diff


def test_large_file_copy_preview_includes_line_ending_change() -> None:
    before = ("line\r\n" * 14_000) + "tail\r\n"
    after = ("line\n" * 14_000) + "tail\n"
    tool = EditFileToolCall("c1", "edit_file", args={"path": "src/app.py"})
    tool.result_text = "Edited src/app.py"
    tool._before_content = before
    tool._after_content = after

    copy_sections = tool._tool_copy_sections()
    diff = next(text for title, _, text in copy_sections if title == "Diff Preview")

    assert "-line [EOL: CRLF]" in diff
    assert "+line [EOL: LF]" in diff
