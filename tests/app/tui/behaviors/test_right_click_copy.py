# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Right-click copy of drag selections through a real Textual screen."""

from __future__ import annotations

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.clipboard import wait_for_clipboard
from tests.support.waiting import wait_for


class _CopyScreen(RightClickScreenCopyMixin, Screen[None]):
    def compose(self) -> ComposeResult:
        # The trailing newline renders a blank third row that ``str.splitlines()`` does not count.
        yield Static(Text("first\nsecond\n"), id="trailing")
        yield Static(Text("next"), id="next")


class _CopyApp(App[None]):
    def get_default_screen(self) -> Screen[None]:
        return _CopyScreen()


async def test_right_click_copies_a_drag_that_starts_on_a_trailing_blank_row(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

    app = _CopyApp()
    async with app.run_test(size=(40, 10)) as pilot:
        screen = app.screen
        trailing = screen.query_one("#trailing", Static)
        following = screen.query_one("#next", Static)
        assert trailing.region.height == 3
        blank_row = trailing.region.bottom - 1

        await pilot.mouse_down(offset=(0, blank_row))
        await pilot.hover(offset=(3, following.region.y))
        await pilot.mouse_up(offset=(3, following.region.y))
        await wait_for(lambda: set(screen.selections) == {trailing, following}, pilot=pilot)
        await pilot.click(offset=(0, following.region.y), button=3)

        await wait_for_clipboard(app)
        assert copied == ["\nnex"]
        assert app.clipboard == "\nnex"
        assert screen.selections == {}
