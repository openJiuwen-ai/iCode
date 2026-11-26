# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the Textual compositor CJK file patch."""

from __future__ import annotations

import ast
import importlib.util
import re
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest
import textual._compositor as compositor_mod
from rich.cells import get_character_cell_size
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
    explicitly: all CJK patches, then the one tab-selection patch. The
    joined-text fallback builds on the earlier partial-update fragment.
    """
    tab_matches = [
        patch
        for patch in patcher._patches
        if patch.package == "textual"
        and patch.module_file == "_compositor.py"
        and patch.old_fragment == textual_tab_selection._COMPOSITOR_GET_WIDGET_AND_OFFSET_AT_OLD
    ]
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
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in ("_has_stable_crop_widths", "_stable_character_width")
    ]

    namespace: dict[str, Any] = {
        "Console": Console,
        "Control": Control,
        "_MERGED_STRIP": object(),
        "cell_len": cell_len,
        "lru_cache": lru_cache,
    }
    exec(
        compile(ast.Module(body=[*helpers, render_segments], type_ignores=[]), "<staged textual._compositor>", "exec"),
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
    for name in (
        "bisect_left",
        "bisect_right",
        "_MERGED_STRIP",
        "_ALWAYS_TRUE",
        *textual_compositor_cjk._RUNTIME_HELPERS,
    ):
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
    source = _staged_patched_compositor_source().replace(textual_compositor_cjk._FAST_RENDER_CHOPS, "unmatched body")
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
        pytest.param("é日한　a", 0, 5, 20, 5, "　a", id="stable-mixed-widths"),
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
        # VS Code's Unicode 11 terminal advances one cell for these; Rich advances two.
        ("🫠 ", "repaint", " regression", 24, (6, 14)),
        ("🥹 ", "repaint", " regression", 24, (6, 14)),
        # A soft hyphen changed from punctuation to a format character in Unicode 4.
        ("soft\u00adhyphen ", "repaint", " regression", 32, (12, 22)),
        # Without VS16, Rich measures these as one cell and tmux 3.8 uses two.
        ("✍ ", "repaint", " regression", 24, (4, 14)),
        ("🕵 ", "repaint", " regression", 24, (4, 14)),
        ("A👩‍", "💻", "notes", 8, (4, 7)),
        ("A❤", "️", "notes", 8, (1, 2)),
        ("", "🏃🏽‍♀️", "abcdefghijklmnopqr", 20, (19, 20)),
        ("🫱🏽‍", "🫲🏾", "abcdefghijklmnop", 20, (16, 17)),
    ],
)
async def test_terminal_dependent_width_replays_complete_rows(
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
    # Preserve every segment and style from column zero, including a successor
    # if these glyphs can wrap. Normal title updates spare unrelated rows.
    origins = [y for y, left, right in partial.spans if (left, right) == (0, terminal_width)]
    assert origins == list(range(len(partial.spans)))
    if prefix in ("🫠 ", "🥹 "):
        assert origins == [0]
    elif prefix in ("✍ ", "🕵 "):
        assert origins == [0, 1]
    assert partial.render_segments(console) == "\n".join(full.split("\n")[: len(origins)])


def test_left_crop_past_the_last_rendered_cell(_render_segments_implementation: str) -> None:
    # A divided chop may reserve a trailing cell beyond its surviving text.
    # Replaying the final segment there can wrap onto the next terminal row.
    update = object.__new__(ChopsUpdate)
    update.chops = [{1: Strip([Segment("abcdef  ")], 9)}]
    update.spans = [(0, 9, 10)]
    assert update.render_segments(Console(force_terminal=True)) == ""


_CURSOR_MOVE = re.compile(r"\x1b\[(\d+);(\d+)H")
_STYLE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def _replay(screen: list[str], output: str, unicode_version: str = "auto") -> list[str]:
    """Write a partial update's text, at its cursor moves, over ``screen``."""
    rows = [
        [
            cell
            for character in line
            for cell in (character, *[""] * (get_character_cell_size(character, unicode_version) - 1))
        ]
        for line in screen
    ]
    pieces = _CURSOR_MOVE.split(output)
    for row, column, text in zip(pieces[1::3], pieces[2::3], pieces[3::3], strict=True):
        y, x = int(row) - 1, int(column) - 1
        for character in _STYLE.sub("", text).rstrip("\n"):
            width = get_character_cell_size(character, unicode_version)
            rows[y][x : x + width] = [character, *[""] * (width - 1)]
            x += width
    return ["".join(row) for row in rows]


@pytest.mark.parametrize("emoji", ["🫠", "🥹"])
def test_left_crop_preserves_text_on_a_unicode_11_terminal(_render_segments_implementation: str, emoji: str) -> None:
    # Rich lays out the prefix in two cells. VS Code's default xterm.js uses one.
    assert get_character_cell_size(emoji, "latest") == 2
    assert get_character_cell_size(emoji, "11.0.0") == 1
    title = f"{emoji} repaint regression"
    update = object.__new__(ChopsUpdate)
    update.chops = [{0: Strip([Segment(title)])}]
    update.spans = [(0, 5, cell_len(title))]
    output = update.render_segments(Console(force_terminal=True, color_system=None))
    assert _replay([title], output, "11.0.0") == [title]


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


@pytest.mark.parametrize("cuts", [[0, 8], [0, 1, 3, 8]])
def test_render_chops_reuses_fully_owned_strip(_preserve_runtime_targets, monkeypatch, cuts):
    textual_compositor_cjk.apply_runtime_patch()
    comp = compositor_mod.Compositor()
    comp._cuts = [cuts]
    region = Region(0, 0, 8, 1)
    front = Strip([Segment("你好世界")])
    back = Strip([Segment("covered!")])
    monkeypatch.setattr(comp, "_get_renders", lambda crop: [(region, region, [front]), (region, region, [back])])

    def unexpected_division(self, cuts):
        pytest.fail("A completely covered or exactly fitting strip was divided")

    monkeypatch.setattr(Strip, "divide", unexpected_division)
    (row,) = comp._render_chops(region, lambda y: True)
    assert row[0] is front
    assert all(row[x] is compositor_mod._MERGED_STRIP for x in cuts[1:-1])


def test_render_chops_short_strip_leaves_other_buckets_available(_preserve_runtime_targets, monkeypatch):
    textual_compositor_cjk.apply_runtime_patch()
    comp = compositor_mod.Compositor()
    comp._cuts = [[0, 2, 4, 6]]
    region = Region(0, 0, 6, 1)
    middle = Region(2, 0, 2, 1)
    layers = [
        (middle, middle, [Strip([Segment("MM")])]),
        (region, region, [Strip([Segment("短")])]),
        (region, region, [Strip([Segment("abcdef")])]),
    ]
    monkeypatch.setattr(comp, "_get_renders", lambda crop: layers)
    (row,) = comp._render_chops(region, lambda y: True)
    assert [part.text for part in row.values()] == ["短", "MM", "ef"]


def test_crop_width_cache_reuses_codepoints_across_different_lines(_preserve_runtime_targets):
    textual_compositor_cjk.apply_runtime_patch()
    cached_width = compositor_mod._stable_character_width
    cached_width.cache_clear()
    assert compositor_mod._has_stable_crop_widths("中文")
    assert not compositor_mod._has_stable_crop_widths("中🫠")
    before = cached_width.cache_info()
    assert compositor_mod._has_stable_crop_widths("文中文")
    assert not compositor_mod._has_stable_crop_widths("文🫠")
    after = cached_width.cache_info()
    assert after.misses == before.misses
    assert after.hits > before.hits


@pytest.mark.parametrize(
    ("row", "text", "expected_rows"),
    [
        (1, "🫠 title", [1]),
        (1, "✍ title", [1, 2]),
        (4, "✍ title", list(range(6))),
        (1, "🏃🏽‍♀️" * 6, list(range(6))),
    ],
    ids=["local", "wrap", "bottom-margin", "unbounded-spill"],
)
async def test_width_dependent_repaint_preserves_row_origins(_preserve_runtime_targets, row, text, expected_rows):
    class Rows(App[None]):
        CSS = "Static { height: 1; width: 100%; }"

        def compose(self) -> ComposeResult:
            for y in range(6):
                yield Static(text if y == row else f"row {y}", markup=False)

    textual_compositor_cjk.apply_runtime_patch()
    async with Rows().run_test(size=(12, 6)) as pilot:
        compositor = pilot.app.screen._compositor
        console = Console(force_terminal=True)
        full = compositor.render_full_update().render_segments(console).split("\n")
        compositor._dirty_regions.add(Region(3, row, 2, 1))
        update = compositor.render_partial_update()
        assert update is not None
        assert update.spans == [(y, 0, 12) for y in expected_rows]
        assert update.render_segments(console) == "\n".join(full[y] for y in expected_rows)


async def test_sidebar_refresh_under_emoji_title_spares_unrelated_rows(
    tmp_path, monkeypatch, _preserve_runtime_targets
):
    from textual.widgets import Input

    from chrys.app.tui.screens.dialogs.session_title import SessionTitleDialog
    from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
    from chrys.app.tui.widgets.chat.panel import ChatPanel
    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
    from tests.support.pilot_barrier import screen_is_settled
    from tests.support.tui_app_harness import make_chrys_app
    from tests.support.waiting import wait_for

    textual_compositor_cjk.apply_runtime_patch()
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(82, 45)) as pilot:
        main = app._main_screen
        sidebar = main.query_one(SidebarPanel)
        if not sidebar.display:
            main.action_toggle_sidebar()
        panel = main.query_one(ChatPanel)
        for index in range(8):
            await panel.mount(UserMessage(f"问题 {index}"), AgentMessage(f"## Answer {index}\n\nA **paragraph**."))
        dialog = SessionTitleDialog(custom_title="🫠 Investigate sidebar repaint regression")
        await app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and screen_is_settled(app, dialog), pilot=pilot)
        field = dialog.query_one(Input)
        field.cursor_blink = False
        await pilot.press("home", "right", "right", "right")
        title_y = field.content_region.y
        assert field.content_region.x < sidebar.region.x < field.content_region.right

        compositor = dialog._compositor
        render_chops = compositor._render_chops
        render_uncertain = compositor_mod._render_width_dependent_update
        repaint_bands = []

        def record_passes(comp, crop, chops, spans, backgrounds):
            if comp is not compositor or crop != sidebar.region:
                return render_uncertain(comp, crop, chops, spans, backgrounds)
            bands = []
            repaint_bands.append(bands)

            def record_band(region, is_rendered_line):
                bands.append(region)
                return render_chops(region, is_rendered_line)

            with monkeypatch.context() as scope:
                scope.setattr(comp, "_render_chops", record_band)
                return render_uncertain(comp, crop, chops, spans, backgrounds)

        monkeypatch.setattr(compositor_mod, "_render_width_dependent_update", record_passes)
        displayed = []
        display = app._display

        def record_display(screen, update):
            if screen is dialog and isinstance(update, ChopsUpdate):
                displayed.append(update)
            return display(screen, update)

        monkeypatch.setattr(app, "_display", record_display)
        sidebar.refresh()
        await wait_for(
            lambda: any((title_y, 0, 82) in update.spans for update in displayed),
            pilot=pilot,
            description="the sidebar repaint redraws the emoji title from column zero",
        )
        repaint = next(update for update in displayed if (title_y, 0, 82) in update.spans)
        assert repaint_bands and all(band.height <= 2 for bands in repaint_bands for band in bands)
        assert any(left == sidebar.region.x for _, left, _ in repaint.spans)
        assert len({y for y, left, right in repaint.spans if (left, right) == (0, 82)}) < 5


def test_width_dependent_repaint_clips_damage_from_before_a_resize(_preserve_runtime_targets, monkeypatch):
    from textual.geometry import Size

    textual_compositor_cjk.apply_runtime_patch()
    comp = compositor_mod.Compositor()
    comp.size = Size(8, 4)
    comp._cuts = [[0, 8] for _ in range(4)]
    region = comp.size.region
    strips = [Strip([Segment("🫠 title")])] + [Strip.blank(8)] * 3
    monkeypatch.setattr(comp, "_get_renders", lambda crop: [(region, region, strips)])
    offscreen = Region(2, 9, 10, 3)
    comp._dirty_regions.update([Region(3, 0, 1, 1), offscreen])
    update = comp.render_partial_update()
    assert update is not None and update.spans == [(0, 0, 8)]
    assert Text.from_ansi(update.render_segments(Console(force_terminal=True))).plain == "🫠 title"
    comp._dirty_regions.add(offscreen)
    assert comp.render_partial_update() is None
    # The bounding box touches the screen even though neither rectangle does.
    comp._dirty_regions.update([Region(0, 8, 3, 2), Region(10, 0, 3, 2)])
    assert comp.render_partial_update() is None


@pytest.mark.parametrize(
    ("prefix", "control"),
    [("🫠", Control.bell().segment), ("ab", Control.bell().segment), ("ab", Segment("\t")), ("ab", Segment("\r"))],
    ids=["emoji-bell", "ascii-bell", "tab", "carriage-return"],
)
def test_control_segment_retains_full_frame_fallback(_preserve_runtime_targets, monkeypatch, prefix, control):
    from textual.geometry import Size

    textual_compositor_cjk.apply_runtime_patch()
    comp = compositor_mod.Compositor()
    comp.size = Size(10, 5)
    comp._cuts = [[0, 10] for _ in range(5)]
    region = comp.size.region
    strips = [Strip([Segment(prefix), control, Segment(" title  ")])] + [Strip.blank(10)] * 4
    monkeypatch.setattr(comp, "_get_renders", lambda crop: [(region, region, strips)])
    comp._dirty_regions.add(Region(3, 0, 2, 1))
    update = comp.render_partial_update()
    assert update is not None and update.spans == [(y, 0, 10) for y in range(5)]
    console = Console(force_terminal=True)
    assert update.render_segments(console) == comp.render_full_update().render_segments(console)


@pytest.mark.parametrize(("emoji", "height"), [("🫠", 6), ("✍", 2)], ids=["bounded", "bottom-margin"])
async def test_width_dependent_repaint_restores_stacked_modal_backgrounds(_preserve_runtime_targets, emoji, height):
    class Bottom(App[None]):
        def compose(self) -> ComposeResult:
            yield Static("ROOT")

    class Middle(ModalScreen[None]):
        DEFAULT_CSS = "Middle { background: $background 30%; } Middle Static { offset: 5 0; width: 4; height: 1; }"

        def compose(self) -> ComposeResult:
            yield Static("MID")

    class Top(ModalScreen[None]):
        DEFAULT_CSS = "Top { background: $background 30%; } Top Static { offset: 10 0; width: 14; height: 1; }"

        def compose(self) -> ComposeResult:
            yield Static(f"{emoji} title")

    textual_compositor_cjk.apply_runtime_patch()
    async with Bottom().run_test(size=(24, height)) as pilot:
        app = pilot.app
        await app.push_screen(Middle())
        await app.push_screen(Top())
        await pilot.pause()
        comp = app.screen._compositor
        console = Console(force_terminal=True)
        full = comp.render_update(full=True, screen_stack=app._background_screens).render_segments(console)
        assert all(text in Text.from_ansi(full).plain for text in ("ROOT", "MID", emoji))
        comp._dirty_regions.add(Region(15, 0, 2, 1))
        update = comp.render_update(screen_stack=app._background_screens)
        assert isinstance(update, ChopsUpdate)
        expected_rows = 1 if height == 6 else height
        assert update.spans == [(y, 0, 24) for y in range(expected_rows)]
        assert update.render_segments(console) == "\n".join(full.split("\n")[:expected_rows])


def test_sidebar_repaint_uses_real_width_of_an_omitted_left_widget(_preserve_runtime_targets, monkeypatch):
    from textual.geometry import Size

    textual_compositor_cjk.apply_runtime_patch()
    comp = compositor_mod.Compositor()
    comp.size = Size(12, 6)
    comp._cuts = [[0, 12], [0, 4, 12], *[[0, 12] for _ in range(4)]]
    # The dirty edge crosses an emoji on row zero and an ASCII strip on row one.
    # On a Unicode 11 terminal the left widget is a cell narrower than its
    # layout on row one, shifting the physical column where SIDEBAR begins.
    title = Region(0, 0, 12, 1)
    left = Region(0, 1, 4, 1)
    right = Region(4, 1, 8, 1)
    lower = Region(0, 2, 12, 4)
    comp._visible_widgets = {Static(): (r, r) for r in (title, left, right, lower)}

    def render(crop):
        return [
            (title, title, [Strip([Segment("🫠 title    ")])]),
            (left, left, [Strip([Segment("🫠ab")]) if crop.overlaps(left) else Strip.blank(4)]),
            (right, right, [Strip([Segment("SIDEBAR ")])]),
            (lower, lower, [Strip.blank(12)] * 4),
        ]

    monkeypatch.setattr(comp, "_get_renders", render)
    comp._dirty_regions.add(Region(6, 0, 3, 2))
    update = comp.render_partial_update()
    assert update is not None and update.spans == [(0, 0, 12), (1, 0, 12)]
    console = Console(force_terminal=True)
    full = comp.render_full_update().render_segments(console)
    assert update.render_segments(console) == "\n".join(full.split("\n")[:2])


async def test_consecutive_wraps_repair_the_gap_before_a_later_dirty_row(_preserve_runtime_targets):
    class Rows(App[None]):
        CSS = "Static { height: 1; width: 100%; }"

        def compose(self) -> ComposeResult:
            for text in ["✍12345678901", "✍abcdefghijk", "ROW2--target", "third", "fourth", "fifth", "last"]:
                yield Static(text, markup=False)

    textual_compositor_cjk.apply_runtime_patch()
    async with Rows().run_test(size=(12, 7)) as pilot:
        compositor = pilot.app.screen._compositor
        console = Console(force_terminal=True)
        full = compositor.render_full_update().render_segments(console)
        compositor._dirty_regions.update([Region(3, 0, 2, 1), Region(2, 5, 2, 1)])
        update = compositor.render_partial_update()
        assert update is not None
        assert (2, 0, 12) in update.spans
        assert "ROW2--target" in update.chops[2][0].text
        assert update.render_segments(console) == full
