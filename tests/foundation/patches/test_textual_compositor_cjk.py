# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Textual compositor CJK file patch."""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import textual._compositor as compositor_mod
from rich.console import Console
from rich.control import Control
from rich.segment import Segment
from rich.text import Text
from textual._cells import cell_len
from textual._compositor import ChopsUpdate
from textual._context import visible_screen_stack
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.geometry import Region
from textual.screen import ModalScreen
from textual.strip import Strip
from textual.widgets import Static

from chrys.foundation.patches import patcher, textual_compositor_cjk, textual_tab_selection
from chrys.foundation.patches.patcher import FilePatch, apply_patch_group


def _compositor_patch_group_in_production_order() -> tuple[FilePatch, ...]:
    """Return the shared file group in ``apply_all()`` import order.

    ``tests/conftest.py`` imports tab selection before this module, so the global
    registry order differs from a fresh production bootstrap.  Build the order
    explicitly: all 19 CJK patches, then the one tab-selection patch. The
    joined-text fallback builds on the earlier partial-update fragment.
    """
    tab_matches = [
        patch
        for patch in patcher._patches
        if patch.package == "textual"
        and patch.module_file == "_compositor.py"
        and patch.old_fragment == textual_tab_selection._COMPOSITOR_GET_WIDGET_AND_OFFSET_AT_OLD
    ]
    assert len(textual_compositor_cjk._PATCHES) == 19
    assert len(tab_matches) == 1
    return (*textual_compositor_cjk._PATCHES, tab_matches[0])


def _staged_patched_compositor_source() -> str:
    """Return installed compositor source after production-order staging."""
    spec = importlib.util.find_spec("textual")
    assert spec is not None
    assert spec.origin is not None
    source = (Path(spec.origin).parent / "_compositor.py").read_text(encoding="utf-8")

    for patch in _compositor_patch_group_in_production_order():
        if patch.new_fragment in source or any(equivalent in source for equivalent in patch.equivalent_fragments):
            continue
        assert patch.old_fragment in source, f"Textual compositor fragment drifted: {patch.description}"
        source = source.replace(patch.old_fragment, patch.new_fragment, 1)
    return source


def _compile_staged_render_segments() -> Callable[[Any, Console], str]:
    """Compile the exact method produced by the staged file patches."""
    source = _staged_patched_compositor_source()
    tree = ast.parse(source)
    chops_update = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChopsUpdate")
    render_segments = next(
        node for node in chops_update.body if isinstance(node, ast.FunctionDef) and node.name == "render_segments"
    )
    width_helper = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_has_stable_crop_widths"
    )

    namespace: dict[str, Any] = {
        "Console": Console,
        "Control": Control,
        "_MERGED_STRIP": object(),
        "cell_len": cell_len,
    }
    exec(
        compile(
            ast.Module(body=[width_helper, render_segments], type_ignores=[]), "<staged textual._compositor>", "exec"
        ),
        namespace,
    )
    implementation = namespace["render_segments"]
    assert callable(implementation)
    return implementation


def test_file_patch_fragments_match_installed_textual() -> None:
    """Fail on upstream drift before ``apply_all()`` can warn and continue."""
    _staged_patched_compositor_source()


def test_dirty_span_crop_patch_applies_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "textual"
    package.mkdir()
    target = package / "_compositor.py"
    target.write_text(textual_compositor_cjk._OLD_RENDER_SEGMENTS_CROP, encoding="utf-8")
    monkeypatch.setattr(patcher, "_locate_package_dir", lambda _package: package)

    crop_patch = textual_compositor_cjk._DIRTY_SPAN_CROP_PATCH
    first = apply_patch_group([crop_patch])
    second = apply_patch_group([crop_patch])

    assert [result.status for result in first] == ["applied"]
    assert [result.status for result in second] == ["skipped"]
    assert target.read_text(encoding="utf-8") == textual_compositor_cjk._NEW_RENDER_SEGMENTS_CROP


def test_runtime_patch_version_gate_precedes_private_api_access(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A future Textual may remove private APIs before its patch can be skipped."""
    import textual
    import textual._compositor as compositor_mod

    monkeypatch.setattr(textual, "__version__", "9.0.0")
    monkeypatch.delattr(compositor_mod, "ChopsUpdate")

    textual_compositor_cjk.apply_runtime_patch()

    assert "loaded Textual is not the pinned 8.2.7" in caplog.text


@pytest.fixture
def _preserve_runtime_targets(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Restore every shared Textual method and global changed by the runtime patch."""
    for class_name, names in textual_compositor_cjk._RUNTIME_TARGETS.items():
        cls = getattr(compositor_mod, class_name)
        for name in names:
            monkeypatch.setattr(cls, name, getattr(cls, name))
    absent_globals: list[str] = []
    for name in ("bisect_left", "bisect_right", "_MERGED_STRIP", "_ALWAYS_TRUE", "_has_stable_crop_widths"):
        if hasattr(compositor_mod, name):
            monkeypatch.setattr(compositor_mod, name, getattr(compositor_mod, name))
        else:
            absent_globals.append(name)
    yield
    for name in absent_globals:
        if hasattr(compositor_mod, name):
            delattr(compositor_mod, name)


def test_runtime_fragment_drift_leaves_all_live_methods_unchanged(
    tmp_path, monkeypatch, caplog, _preserve_runtime_targets
):
    monkeypatch.setattr(ChopsUpdate, "render_segments", lambda self, console: "old render")
    source = _staged_patched_compositor_source().replace(textual_compositor_cjk._NEW_RENDER_CHOPS, "unmatched body")
    target = tmp_path / "_compositor.py"
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(compositor_mod, "__file__", str(target))
    before = [
        (getattr(compositor_mod, class_name), name, getattr(getattr(compositor_mod, class_name), name))
        for class_name, names in textual_compositor_cjk._RUNTIME_TARGETS.items()
        for name in names
    ]

    textual_compositor_cjk.apply_runtime_patch()

    assert "fragment drifted: CJK-safe effective cuts" in caplog.text
    assert all(getattr(cls, name) is implementation for cls, name, implementation in before)


@pytest.fixture(params=("staged-file", "runtime"))
def _render_segments_implementation(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    _preserve_runtime_targets: None,
) -> str:
    """Exercise both the next-process file patch and first-process fallback."""
    if request.param == "staged-file":
        implementation = _compile_staged_render_segments()
    else:

        def unpatched_render_segments(_self: Any, _console: Any) -> str:
            raise AssertionError("runtime patch did not replace the loaded Textual method")

        monkeypatch.setattr(ChopsUpdate, "render_segments", unpatched_render_segments)
        textual_compositor_cjk.apply_runtime_patch()
        implementation = ChopsUpdate.render_segments
        assert getattr(implementation, textual_compositor_cjk._RUNTIME_PATCH_MARKER, False)

        textual_compositor_cjk.apply_runtime_patch()
        assert ChopsUpdate.render_segments is implementation

    monkeypatch.setattr(ChopsUpdate, "render_segments", implementation)
    return request.param


@pytest.mark.parametrize(
    ("segments", "chop_x", "x1", "x2", "expected"),
    [
        pytest.param(("点击查看详情",), 126, 100, 129, "点击", id="cjk-mid-character"),
        pytest.param(("点击查看详情",), 126, 100, 130, "点击", id="cjk-aligned"),
        pytest.param(("点击", "查看详情"), 126, 100, 130, "点击", id="segment-boundary"),
        pytest.param(("A", "击", "B" * 20), 0, 0, 2, "A击", id="multiple-segments"),
        pytest.param(("hello!",), 10, 0, 15, "hello", id="ascii"),
        pytest.param(("a👍b",), 10, 0, 12, "a👍", id="wide-emoji"),
        pytest.param(("点击",), 10, 0, 10, "", id="zero"),
        pytest.param(("点击",), 10, 11, 14, "点击", id="full-length-and-left-overlap"),
        pytest.param(("  ", "点击查看详情", "  "), 124, 5, 129, "  点击", id="tooltip-geometry"),
    ],
)
def test_render_segments_crops_at_complete_character_boundaries(
    _render_segments_implementation: str,
    segments: tuple[str, ...],
    chop_x: int,
    x1: int,
    x2: int,
    expected: str,
) -> None:
    strip = Strip(Segment(text) for text in segments)
    update = object.__new__(ChopsUpdate)
    update.chops = [{chop_x: strip}]
    update.spans = [(0, x1, x2)]

    terminal_output = update.render_segments(Console(force_terminal=True, color_system=None))
    rendered = Text.from_ansi(terminal_output).plain
    requested_end = min(strip.cell_length, x2 - chop_x)

    assert rendered == expected
    assert strip.text.startswith(rendered)
    assert 0 <= cell_len(rendered) - requested_end <= 1


@pytest.mark.parametrize(
    ("text", "chop_x", "x1", "x2", "expected_x", "expected"),
    [
        pytest.param("hello world", 0, 6, 40, 6, "world", id="ascii"),
        pytest.param("点击查看", 10, 13, 40, 12, "击查看", id="cjk-mid-character"),
        pytest.param("点击查看", 10, 14, 40, 14, "查看", id="cjk-aligned"),
        pytest.param("点击查看详情", 10, 13, 18, 12, "击查看", id="cjk-both-edges"),
        pytest.param("#1中*2", 0, 4, 20, 4, "*2", id="cjk-with-ascii-keycap-bases"),
        pytest.param("✍ repaint", 0, 4, 20, 0, "✍ repaint", id="bare-writing-hand"),
        pytest.param("🕵 repaint", 0, 4, 20, 0, "🕵 repaint", id="bare-detective"),
        pytest.param("ok ❤️ done", 0, 4, 40, 0, "ok ❤️ done", id="emoji-variation"),
        pytest.param("ok 👩‍💻 done", 0, 6, 40, 0, "ok 👩‍💻 done", id="after-emoji-zwj"),
        pytest.param("ok 🇨🇳 done", 0, 4, 40, 0, "ok 🇨🇳 done", id="flag"),
        pytest.param("🇨🇳🇺🇸!", 0, 3, 4, 0, "🇨🇳🇺🇸", id="second-flag"),
    ],
)
def test_render_segments_starts_at_the_dirty_span(
    _render_segments_implementation: str,
    text: str,
    chop_x: int,
    x1: int,
    x2: int,
    expected_x: int,
    expected: str,
) -> None:
    """Cells left of the span are not repainted, apart from a divided wide character.

    A character spanning multiple code points is written whole.
    """
    update = object.__new__(ChopsUpdate)
    update.chops = [{chop_x: Strip([Segment(text)])}]
    update.spans = [(0, x1, x2)]

    terminal_output = update.render_segments(Console(force_terminal=True, color_system=None))

    assert terminal_output.startswith(Control.move_to(expected_x, 0).segment.text)
    assert Text.from_ansi(terminal_output).plain == expected


@pytest.mark.parametrize(
    ("prefix", "styled", "suffix", "terminal_width", "dirty"),
    [
        # Without VS16, Rich measures these as one cell and tmux 3.8 uses two.
        ("✍ ", "repaint", " regression", 24, (4, 14)),
        ("🕵 ", "repaint", " regression", 24, (4, 14)),
        ("A👩‍", "💻", "notes", 8, (4, 7)),
        ("A❤", "️", "notes", 8, (1, 2)),
        ("", "🏃🏽‍♀️", "abcdefghijklmnopqr", 20, (19, 20)),
        ("🫱🏽‍", "🫲🏾", "abcdefghijklmnop", 20, (16, 17)),
    ],
)
async def test_partial_update_with_terminal_dependent_width_replays_the_full_frame(
    _preserve_runtime_targets: None, prefix: str, styled: str, suffix: str, terminal_width: int, dirty: tuple[int, int]
) -> None:
    # The cursor can split a glyph across styles. Terminals may also advance
    # a different number of columns than Rich for the very same emoji.
    value = Text.assemble(prefix, (styled, "bold"), suffix)

    class EmojiApp(App[None]):
        CSS = "Static { height: 1; width: 100%; }"

        def compose(self) -> ComposeResult:
            yield Static(value)
            yield Static("NEXT LINE")

    textual_compositor_cjk.apply_runtime_patch()
    app = EmojiApp()
    async with app.run_test(size=(terminal_width, 4)) as pilot:
        await pilot.pause()
        compositor = app.screen._compositor
        console = Console(force_terminal=True)
        full = compositor.render_full_update().render_segments(console)
        start, end = dirty
        compositor._dirty_regions.add(Region(start, 0, end - start, 1))
        partial = compositor.render_partial_update()

    assert isinstance(partial, ChopsUpdate)
    assert partial.render_segments(console) == full


def test_left_crop_past_the_last_rendered_cell(_render_segments_implementation: str) -> None:
    # A divided chop may reserve a trailing cell beyond its surviving text.
    # Replaying the final segment there can wrap onto the next terminal row.
    update = object.__new__(ChopsUpdate)
    update.chops = [{1: Strip([Segment("abcdef  ")], 9)}]
    update.spans = [(0, 9, 10)]
    assert update.render_segments(Console(force_terminal=True)) == ""


_CURSOR_MOVE = re.compile(r"\x1b\[(\d+);(\d+)H")
_STYLE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _replay(screen: list[str], output: str) -> list[str]:
    """Write a partial update's text, at its cursor moves, over ``screen``."""
    rows = [[cell for character in line for cell in (character, *[""] * (cell_len(character) - 1))] for line in screen]
    pieces = _CURSOR_MOVE.split(output)
    for row, column, text in zip(pieces[1::3], pieces[2::3], pieces[3::3], strict=True):
        y, x = int(row) - 1, int(column) - 1
        for character in _STYLE.sub("", text).rstrip("\n"):
            width = cell_len(character)
            rows[y][x : x + width] = [character, *[""] * (width - 1)]
            x += width
    return ["".join(row) for row in rows]


_BODY = "已创建工作区“p1”。\n可以随时切换到这个工作区。\n切换后新会话将在该工作区中开始。"


class _WorkspaceCreated(ModalScreen[None]):
    DEFAULT_CSS = """
    _WorkspaceCreated { align: center middle; background: $background 50%; }
    #dialog { width: 80; height: auto; border: round $primary; padding: 0 1; }
    #body { width: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Static(_BODY, id="body")
            yield Static("切换", id="switch")


class _AppWithSidebar(App[None]):
    CSS = "#sidebar { dock: right; width: 52; height: 1fr; }"

    def compose(self) -> ComposeResult:
        yield Static("对话", id="chat")
        yield Static("工作区", id="sidebar")


async def test_sidebar_repaint_under_a_dialog_keeps_its_body(
    _preserve_runtime_targets: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repaint of the sidebar beside a dialog leaves the dialog's body on screen.

    The body ends at the sidebar's left edge; the dialog's border and padding
    reach over the sidebar, so the sidebar's repaint also redraws those rows.
    Nothing changed, so the partial update must leave the screen as it was.
    """
    textual_compositor_cjk.apply_runtime_patch()
    app = _AppWithSidebar()
    async with app.run_test(size=(180, 45)) as pilot:
        await app.push_screen(_WorkspaceCreated())
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, _WorkspaceCreated)
        sidebar = app.screen_stack[0].query_one("#sidebar")
        body = dialog.query_one("#body")
        assert body.region.right == sidebar.region.x

        token = visible_screen_stack.set(app._background_screens)
        try:
            screen = [strip.text for strip in dialog._compositor.render_strips()]

            def unexpected_body_render(_crop):
                pytest.fail("the sidebar repaint rendered the omitted dialog body")

            monkeypatch.setattr(body, "render_lines", unexpected_body_render)
            # MainScreen forwards each region it repaints under a dialog this way.
            dialog._compositor._dirty_regions.add(sidebar.region)
            update = dialog._compositor.render_partial_update()
            assert update is not None
            # A lower widget must not own a strip crossing into the omitted body,
            # including when a terminal replays joined glyphs from the strip origin.
            body_chop = update.chops[body.region.y][body.region.x]
            assert body_chop is not None and body_chop.cell_length == body.region.width
        finally:
            visible_screen_stack.reset(token)

    assert update is not None
    assert _replay(screen, update.render_segments(Console(force_terminal=True))) == screen
