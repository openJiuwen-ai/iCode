# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for DiffScreen clipboard interactions."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.events import MouseDown, MouseUp
from textual.geometry import Offset
from textual.selection import Selection

from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.screens.diff.screen import DiffScreen
from chrys.app.tui.util.diff_entries import DiffFileEntry
from chrys.app.tui.widgets.diff_view.code import CodeColumn
from chrys.service.mutations.types import MutationOp


def _entry(path: str, rel: str, *, before_text: str, after_text: str) -> DiffFileEntry:
    return DiffFileEntry(
        path=path,
        rel_path=rel,
        operation=MutationOp.MODIFY,
        old_path=None,
        before_text=before_text,
        after_text=after_text,
        is_binary=False,
        encoding="utf-8",
    )


class _DiffCopyApp(App):
    def __init__(self) -> None:
        super().__init__()
        self.diff_screen = DiffScreen(
            {
                1: [
                    _entry(
                        "/repo/changed.py",
                        "changed.py",
                        before_text="old line\nsecond line\n",
                        after_text="first line\nsecond line\n",
                    )
                ]
            },
            cwd="/repo",
            session_id="session-1234567890",
        )

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.diff_screen)


@pytest.mark.asyncio
async def test_right_click_copies_diff_selection_then_clears_it(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

    app = _DiffCopyApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()

        screen = app.diff_screen
        code_column = screen.query_one(".code-right", CodeColumn)
        screen.selections = {code_column: Selection(Offset(0, 0), Offset(len("first line"), 0))}

        click_x = code_column.region.x + 1
        click_y = code_column.region.y
        app.post_message(
            MouseDown(
                code_column,
                x=click_x,
                y=click_y,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=click_x,
                screen_y=click_y,
            )
        )
        await pilot.pause()
        app.post_message(
            MouseUp(
                code_column,
                x=click_x,
                y=click_y,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=click_x,
                screen_y=click_y,
            )
        )
        await pilot.pause()

        assert app.clipboard == "first line"
        await wait_for_clipboard(app)
        assert copied == ["first line"]
        assert screen.get_selected_text() is None
