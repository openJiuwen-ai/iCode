# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""iCode frees the terminal's images before it leaves the alternate screen."""

from __future__ import annotations

import re
from contextlib import nullcontext
from typing import TYPE_CHECKING

import pytest
from textual.screen import Screen

from chrys.app.tui.graphics.detect import GraphicsProtocol
from chrys.app.tui.graphics.manager import TerminalGraphics
from chrys.app.tui.widgets.chat.image_view import TerminalImages
from tests.support.terminal_images import image_previews
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from textual.widget import Widget

    from chrys.app.tui.app import ChrysApp

_LEFT = "<left the alternate screen>"


class _ImageScreen(Screen[None]):
    def __init__(self, view: TerminalImages) -> None:
        super().__init__()
        self._view = view

    def compose(self) -> Iterator[Widget]:
        yield self._view


def _crash() -> None:
    msg = "a handler failed"
    raise RuntimeError(msg)


@pytest.mark.parametrize("crash", [False, True], ids=["exit", "crash"])
async def test_images_are_deleted_while_the_alternate_screen_is_still_the_terminals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, crash: bool
) -> None:
    """kitty and Ghostty keep each screen's images apart: a delete sent after leaving it frees nothing.

    Textual leaves the alternate screen when its message loop ends and dispatches Unmount after, so
    the images must be gone by then; ``run_test`` unmounts first, so the loop is waited out here. A
    crash ends the loop without the exit message.
    """
    writes: list[str] = []
    app = make_chrys_app(tmp_path)
    raised = pytest.raises(RuntimeError, match="a handler failed") if crash else nullcontext()
    with raised:
        await _run_until_the_loop_ends(app, writes, monkeypatch, crash=crash)

    assert _LEFT in writes
    before = "".join(writes[: writes.index(_LEFT)])
    assert re.findall(r"a=d,d=I,i=(\d+)", before) == [str(_sent_id(writes))]


def _sent_id(writes: list[str]) -> int:
    (image_id,) = {int(found) for found in re.findall(r"a=T,[^\x1b]*?i=(\d+)", "".join(writes))}
    return image_id


async def _run_until_the_loop_ends(
    app: ChrysApp, writes: list[str], monkeypatch: pytest.MonkeyPatch, *, crash: bool
) -> None:
    async with app.run_test(size=(80, 24)) as pilot:
        app._terminal_graphics = TerminalGraphics(
            app, GraphicsProtocol.KITTY, writes.append, cell_pixels=lambda: (10, 20)
        )
        view = TerminalImages(image_previews((40, 20)))
        await app.push_screen(_ImageScreen(view))
        await wait_for(lambda: view.slots[0].kitty_id, pilot=pilot, description="the image was sent")
        image_id = view.slots[0].kitty_id
        driver = app._driver
        assert driver is not None
        leave = driver.stop_application_mode

        def stop_application_mode() -> None:
            writes.append(_LEFT)
            leave()

        monkeypatch.setattr(driver, "stop_application_mode", stop_application_mode)
        if crash:
            app.call_later(_crash)
        else:
            app.exit()
        await wait_for(lambda: _LEFT in writes, description="the message loop ended")
        assert image_id == _sent_id(writes)
