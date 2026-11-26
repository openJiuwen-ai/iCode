# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Exercise the CJK compositor fix with Textual imported before patching."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from chrys.foundation.patches import textual_compositor_cjk
from tests.support.installed_packages import copy_installed_package


@pytest.mark.parametrize("patch_mode", ["startup", "runtime-only", "already-patched", "earlier-release"])
def test_first_launch_repairs_preimported_compositor(tmp_path: Path, patch_mode: str) -> None:
    package = copy_installed_package("textual", tmp_path)
    target = package / "_compositor.py"
    source = target.read_text(encoding="utf-8")
    # The development venv may already be patched. Reconstruct the old CJK
    # implementation only in this process's private Textual package copy.
    for patch in reversed(textual_compositor_cjk._PATCHES):
        if patch.new_fragment in source:
            source = source.replace(patch.new_fragment, patch.old_fragment, 1)
    assert textual_compositor_cjk._OLD_RENDER_CHOPS in source
    assert textual_compositor_cjk._NEW_RENDER_CHOPS not in source
    if patch_mode in ("already-patched", "earlier-release"):
        for patch in textual_compositor_cjk._PATCHES:
            # Earlier releases already installed the original eleven fragments.
            if patch_mode == "already-patched" or patch not in (
                textual_compositor_cjk._DIRTY_SPAN_LEFT_EDGE_PATCH,
                *textual_compositor_cjk._PARTIAL_ROW_PATCHES,
                textual_compositor_cjk._FULL_FRAME_FOR_JOINED_TEXT,
            ):
                source = source.replace(patch.old_fragment, patch.new_fragment, 1)
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
from textual.widgets import Static
from rich.console import Console
from rich.text import Text

Compositor = compositor_module.Compositor
ChopsUpdate = compositor_module.ChopsUpdate
assert screen_module.Compositor is Compositor
sentinel_before = getattr(compositor_module, "_MERGED_STRIP", None)
assert (sentinel_before is not None) == (sys.argv[2] in ("already-patched", "earlier-release"))
original_render_chops = Compositor._render_chops
original_selection = Compositor.get_widget_and_offset_at
existing_compositor = Compositor()
existing_update = ChopsUpdate([], []) if sentinel_before is not None else ChopsUpdate([], [], [])

if sys.argv[2] != "runtime-only":
    results = apply_all()
    cjk_results = [r for r in results if r.patch in textual_compositor_cjk._PATCHES]
    assert len(cjk_results) == 15
    status = "skipped" if sys.argv[2] != "startup" else "applied"
    partial = (textual_compositor_cjk._DIRTY_SPAN_LEFT_EDGE_PATCH, *textual_compositor_cjk._PARTIAL_ROW_PATCHES, textual_compositor_cjk._FULL_FRAME_FOR_JOINED_TEXT)
    partial_status = "applied" if sys.argv[2] == "earlier-release" else status
    assert all(r.status == (partial_status if r.patch in partial else status) for r in cjk_results), cjk_results
else:
    source_before = Path(compositor_module.__file__).read_bytes()
    textual_compositor_cjk.apply_runtime_patch()
    assert Path(compositor_module.__file__).read_bytes() == source_before
    # The CJK patch must leave the independently installed selection method alone.
    assert Compositor.get_widget_and_offset_at is original_selection

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
    async with app.run_test(size=(8, 2)):
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
    result = subprocess.run(
        [sys.executable, "-B", "-X", "utf8", "-c", script, str(tmp_path), patch_mode],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
