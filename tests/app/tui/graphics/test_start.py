# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Which apps get terminal graphics at start."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.color import ColorSystem

from chrys.app.tui.graphics import manager, payloads
from chrys.app.tui.graphics.detect import GraphicsProtocol
from chrys.app.tui.graphics.probe import TerminalReport
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from tests.support.terminal_images import TerminalImagesApp, image_previews
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import MAIN_SCREEN_BUILD_TIMEOUT, wait_for

if TYPE_CHECKING:
    from pathlib import Path

_GHOSTTY = TerminalReport("ghostty 1.4.0", frozenset({62, 22, 52}), (16, 34))
_WINDOWS_TERMINAL = TerminalReport("", frozenset({61, 4, 6, 22, 52}), (10, 20))
_WEZTERM = TerminalReport("WezTerm 20240203-110809-5046fc22", frozenset({65, 4, 6, 18, 22}), (19, 38))


def _start(
    app: TerminalImagesApp, monkeypatch: pytest.MonkeyPatch, *, enabled: bool, report: TerminalReport | None
) -> manager.TerminalGraphics | None:
    """Start graphics as a terminal-backed app would: test apps are headless."""
    with monkeypatch.context() as patch:
        patch.setattr(TerminalImagesApp, "is_headless", property(lambda _app: False))
        return manager.start_terminal_graphics(app, enabled=enabled, report=report)


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    """Make ``env`` what the environment says of the terminal, whatever terminal runs the tests."""
    for name in ("WT_SESSION", "TERM_PROGRAM"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize(
    ("report", "env", "enabled", "expected"),
    [
        pytest.param(_GHOSTTY, {}, True, GraphicsProtocol.KITTY, id="ghostty"),
        pytest.param(_WINDOWS_TERMINAL, {"WT_SESSION": "abc"}, True, GraphicsProtocol.SIXEL, id="windows-terminal"),
        pytest.param(_WEZTERM, {}, True, GraphicsProtocol.ITERM, id="wezterm"),
        pytest.param(_GHOSTTY, {}, False, None, id="setting-off"),
        pytest.param(TerminalReport("XTerm(390)", frozenset({4})), {}, True, None, id="unknown-terminal"),
        pytest.param(None, {"WT_SESSION": "abc"}, True, None, id="not-asked"),
    ],
)
async def test_graphics_start_for_known_terminals_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
    report: TerminalReport | None,
    env: dict[str, str],
    enabled: bool,
    expected: GraphicsProtocol | None,
) -> None:
    _set_env(monkeypatch, env)
    app = TerminalImagesApp(None)
    async with app.run_test(size=(40, 10)):
        app.console._color_system = ColorSystem.TRUECOLOR
        graphics = _start(app, monkeypatch, enabled=enabled, report=report)

        assert (None if graphics is None else graphics.protocol) is expected
        assert manager.graphics_for(app) is graphics
        if graphics is not None:
            await graphics.close()
            assert manager.graphics_for(app) is None


@pytest.mark.parametrize("report", [_WINDOWS_TERMINAL, _WEZTERM], ids=["sixel", "iterm"])
async def test_row_images_need_the_textual_their_overlay_was_written_against(
    monkeypatch: pytest.MonkeyPatch, report: TerminalReport
) -> None:
    _set_env(monkeypatch, {"WT_SESSION": "abc"})
    app = TerminalImagesApp(None)
    async with app.run_test(size=(40, 10)):
        app.console._color_system = ColorSystem.TRUECOLOR
        graphics = _start(app, monkeypatch, enabled=True, report=report)
        assert graphics is not None
        await graphics.close()
        monkeypatch.setattr(manager, "TEXTUAL_VERSION", "0.0.0")

        assert _start(app, monkeypatch, enabled=True, report=report) is None


async def test_headless_apps_draw_half_blocks() -> None:
    app = TerminalImagesApp(None)
    async with app.run_test(size=(40, 10)):
        app.console._color_system = ColorSystem.TRUECOLOR
        assert manager.start_terminal_graphics(app, enabled=True, report=_GHOSTTY) is None
        assert manager.graphics_for(app) is None


async def test_placeholder_images_take_the_answered_cell_size_where_the_tty_gives_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[tuple[int, int]] = []

    def kitty_png(data_uri: str, *, columns: int, rows: int, cell_pixels: tuple[int, int]) -> bytes:
        sizes.append(cell_pixels)
        return payloads.kitty_png(data_uri, columns=columns, rows=rows, cell_pixels=cell_pixels)

    monkeypatch.setattr(manager, "kitty_png", kitty_png)
    monkeypatch.setattr(manager, "_tty_cell_pixels", lambda: None)
    app = TerminalImagesApp(None)
    async with app.run_test(size=(80, 24)) as pilot:
        app.console._color_system = ColorSystem.TRUECOLOR
        graphics = _start(app, monkeypatch, enabled=True, report=_GHOSTTY)
        assert graphics is not None
        await app.mount(TerminalImages(image_previews((40, 20))))
        await wait_for(lambda: bool(sizes), pilot=pilot, description="the image's payload is built")

        assert sizes == [(16, 34)]
        await graphics.close()


async def test_the_app_starts_graphics_with_what_the_terminal_answered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    starts: list[tuple[bool, TerminalReport | None]] = []

    def start_terminal_graphics(
        _app: object, *, enabled: bool, report: TerminalReport | None
    ) -> manager.TerminalGraphics | None:
        starts.append((enabled, report))
        return None

    monkeypatch.setattr(manager, "start_terminal_graphics", start_terminal_graphics)
    app = make_chrys_app(tmp_path, terminal_report=_GHOSTTY)
    async with app.run_test():
        await wait_for(
            lambda: bool(starts), description="graphics start with the main screen", timeout=MAIN_SCREEN_BUILD_TIMEOUT
        )

    assert starts == [(True, _GHOSTTY)]
