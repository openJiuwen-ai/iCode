# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the CJK compositor fix with Textual imported before patching."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from chrys.foundation.patches import textual_compositor_cjk
from tests.support.installed_packages import copy_installed_package


@pytest.mark.parametrize(
    "patch_mode",
    [
        "startup",
        "runtime-only",
        "already-patched",
        "earlier-release",
        "previous-pr",
        "before-composition",
        "initial-pr",
        "bare-emoji-pr",
        "cached-pr",
        "cached-pr-runtime",
    ],
)
def test_first_launch_repairs_preimported_compositor(tmp_path: Path, patch_mode: str) -> None:
    package = copy_installed_package("textual", tmp_path)
    target = package / "_compositor.py"
    source = target.read_text(encoding="utf-8")
    # The development venv may already be patched. Reconstruct the old CJK
    # implementation only in this process's private Textual package copy.
    source = source.replace(textual_compositor_cjk._LEGACY_SEGMENT_CROP, "", 1)
    cleanup = textual_compositor_cjk._REMOVE_LEGACY_CROP
    install_patches = tuple(p for p in textual_compositor_cjk._PATCHES if p != cleanup)
    for patch in reversed(install_patches):
        if patch.new_fragment in source:
            source = source.replace(patch.new_fragment, patch.old_fragment, 1)
    assert textual_compositor_cjk._OLD_RENDER_CHOPS in source
    assert textual_compositor_cjk._NEW_RENDER_CHOPS not in source
    # These are the fragments present in each upgrade source.
    width_upgrades = (
        textual_compositor_cjk._CROP_WIDTH_HELPER_PATCH,
        textual_compositor_cjk._TERMINAL_WIDTH_UPGRADE,
        textual_compositor_cjk._LEFT_WIDTH_UPGRADE,
        textual_compositor_cjk._FRAME_WIDTH_UPGRADE,
    )
    before_composition = [
        patch for patch in install_patches if patch not in textual_compositor_cjk._COMPOSITION_UPGRADES
    ]
    previous_pr = [patch for patch in before_composition if patch not in width_upgrades]
    sidebar_fixes = (
        textual_compositor_cjk._DIRTY_SPAN_LEFT_EDGE_PATCH,
        *textual_compositor_cjk._PARTIAL_ROW_PATCHES,
        textual_compositor_cjk._FULL_FRAME_FOR_JOINED_TEXT,
    )
    earlier_release = [patch for patch in previous_pr if patch not in sidebar_fixes]
    assert len(previous_pr) == 15 and len(earlier_release) == 11
    bare_emoji_pr = [patch for patch in before_composition if patch != textual_compositor_cjk._TERMINAL_WIDTH_UPGRADE]
    installed = {
        "already-patched": install_patches,
        "before-composition": before_composition,
        "initial-pr": earlier_release,
        "bare-emoji-pr": bare_emoji_pr,
        "cached-pr": bare_emoji_pr,
        "cached-pr-runtime": bare_emoji_pr,
        "previous-pr": previous_pr,
        "earlier-release": earlier_release,
    }.get(patch_mode, ())
    for patch in installed:
        assert patch.old_fragment in source
        source = source.replace(patch.old_fragment, patch.new_fragment, 1)
    legacy_crop = patch_mode in ("initial-pr", "cached-pr", "cached-pr-runtime")
    if legacy_crop:
        right_crop = "\n" + textual_compositor_cjk._NEW_RENDER_SEGMENTS_CROP
        assert source.count(right_crop) == 1
        source = source.replace(right_crop, textual_compositor_cjk._LEGACY_SEGMENT_CROP + right_crop, 1)
    target.write_text(source, encoding="utf-8")

    script = '''
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, sys.argv[1])
# Textual is imported before apply_all() runs, as in production, where ChrysApp
# imports it before main reaches bootstrap_runtime(). The Textual modules that
# hold the patched classes are imported below and Chrys holds none, so the
# app's own import, the bulk of a cold start, would add no holder to repair.
from chrys.foundation.patches import apply_all, textual_compositor_cjk
from chrys.foundation.patches import textual_win_sleep
import textual._compositor as compositor_module
import textual.screen as screen_module
from textual.app import App, ComposeResult
from textual.geometry import Region
from textual.strip import Strip
from textual.widgets import Static
from rich.console import Console
from rich.segment import Segment
from rich.text import Text

Compositor = compositor_module.Compositor
ChopsUpdate = compositor_module.ChopsUpdate
assert screen_module.Compositor is Compositor
sentinel_before = getattr(compositor_module, "_MERGED_STRIP", None)
assert (sentinel_before is not None) == (sys.argv[2] not in ("startup", "runtime-only"))
original_render_chops = Compositor._render_chops
original_selection = Compositor.get_widget_and_offset_at
existing_compositor = Compositor()
existing_update = ChopsUpdate([], []) if sentinel_before is not None else ChopsUpdate([], [], [])

if sys.argv[2] not in ("runtime-only", "cached-pr-runtime"):
    results = apply_all()
    cjk_results = [r for r in results if r.patch in textual_compositor_cjk._PATCHES]
    assert [r.status for r in cjk_results] == sys.argv[3].split(","), cjk_results
    assert textual_compositor_cjk._LEGACY_SEGMENT_CROP not in Path(compositor_module.__file__).read_text(encoding="utf-8")
else:
    source_before = Path(compositor_module.__file__).read_bytes()
    textual_compositor_cjk.apply_runtime_patch()
    assert Path(compositor_module.__file__).read_bytes() == source_before
    # The CJK patch must leave the independently installed selection method alone.
    assert Compositor.get_widget_and_offset_at is original_selection

# The compositor's full-frame fallback can hide a second crop in the raw
# renderer. Exercise its own dirty span as well.
row = ChopsUpdate([{0: Strip([Segment("✍ repaint")])}], [(0, 4, 9)])
assert Text.from_ansi(row.render_segments(Console(force_terminal=True))).plain == "✍ repaint"
textual_win_sleep.apply_runtime_patch()
assert compositor_module.Compositor is Compositor
assert screen_module.Compositor is Compositor
assert compositor_module.ChopsUpdate is ChopsUpdate
assert type(existing_compositor) is Compositor
assert type(existing_update) is ChopsUpdate
if sentinel_before is not None:
    assert compositor_module._MERGED_STRIP is sentinel_before

class ProbeApp(App):
    CSS = """
    Screen { layers: back front; }
    #back { layer: back; position: absolute; offset: 1 0; width: 1; height: 1; }
    #front { layer: front; position: absolute; width: 8; height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Static("x", id="back", markup=False)
        yield Static("你好世界", id="front", markup=False)

async def main():
    app = ProbeApp()
    async with app.run_test(size=(8, 2)) as pilot:
        compositor = app.screen._compositor
        # The obscured widget contributes a cut inside the first wide character.
        assert 1 in compositor.cuts[0], compositor.cuts
        rendered = compositor.render_strips()[0].text
        assert rendered == "你好世界", repr(rendered)
        console = Console(force_terminal=False, color_system=None, _environ={})
        for simplify in (False, True):
            update = compositor.render_full_update(simplify=simplify)
            assert Text.from_ansi(update.render_segments(console)).plain.splitlines()[0] == "你好世界"
        # A merged leader starts before this dirty span. Its actual width must
        # win over the old chop_ends bucket in both output paths.
        compositor._dirty_regions.add(Region(1, 0, 7, 1))
        update = compositor.render_partial_update()
        assert isinstance(update, ChopsUpdate)
        assert Text.from_ansi(update.render_segments(console)).plain == "你好世界"
        rich_text = "".join(segment.text for segment in console.render(update) if not segment.control)
        assert rich_text == "你好世界", rich_text
        # A span starting on a character boundary leaves the cells left of it alone.
        compositor._dirty_regions.add(Region(2, 0, 6, 1))
        update = compositor.render_partial_update()
        assert Text.from_ansi(update.render_segments(console)).plain == "好世界"
        # Exercise the updated predicate on this pre-imported compositor.
        front = app.query_one("#front", Static)
        for title in ("✍ repaint", "🫠 repaint"):
            front.update(title)
            await pilot.pause()
            full = compositor.render_full_update().render_segments(console)
            compositor._dirty_regions.add(Region(3, 0, 2, 1))
            assert compositor.render_partial_update().render_segments(console) == full

asyncio.run(main())
assert Compositor._render_chops is not original_render_chops
installed = [
    (getattr(compositor_module, class_name), name, getattr(getattr(compositor_module, class_name), name))
    for class_name, names in textual_compositor_cjk._RUNTIME_TARGETS.items()
    for name in names
]
textual_compositor_cjk.apply_runtime_patch()
assert all(getattr(cls, name) is implementation for cls, name, implementation in installed)
'''
    skipped = set(installed)
    if not legacy_crop:
        skipped.add(cleanup)
    expected = ",".join("skipped" if patch in skipped else "applied" for patch in textual_compositor_cjk._PATCHES)
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", script, str(tmp_path), patch_mode, expected],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
