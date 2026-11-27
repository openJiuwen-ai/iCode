# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Copy-on-select of drag selections through a real Textual screen.

A macOS terminal may keep Cmd+C for itself and cannot see a selection the TUI
draws, so finishing a drag is what puts the text on the clipboard.
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import Static

from chrys.app.tui.behaviors import right_click_copy
from chrys.app.tui.behaviors.right_click_copy import RightClickScreenCopyMixin
from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from tests.support.waiting import wait_for


class _CopyScreen(RightClickScreenCopyMixin, Screen[None]):
    def compose(self) -> ComposeResult:
        yield Static("first", id="first")
        yield Static("next", id="next")


class _CopyApp(App[None]):
    def get_default_screen(self) -> Screen[None]:
        return _CopyScreen()


@pytest.fixture
def copied(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    writes: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", writes.append)
    return writes


async def test_finishing_a_drag_copies_the_selection_once_and_keeps_it(
    monkeypatch: pytest.MonkeyPatch, copied: list[str]
) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: True)
    app = _CopyApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down("#first", offset=(0, 0))
        await pilot.hover("#next", offset=(3, 0))
        assert copied == []

        await pilot.mouse_up("#next", offset=(3, 0))
        await wait_for(lambda: bool(copied), pilot=pilot)

        await wait_for_clipboard(app)
        assert copied == ["first\nnex"]
        assert app.screen.get_selected_text() == "first\nnex"


async def test_a_plain_click_leaves_the_clipboard_alone(monkeypatch: pytest.MonkeyPatch, copied: list[str]) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: True)
    app = _CopyApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.click("#first", offset=(1, 0))
        await pilot.pause()

        await wait_for_clipboard(app)
        assert copied == []


async def test_a_drag_copies_nothing_where_copy_on_select_is_off(
    monkeypatch: pytest.MonkeyPatch, copied: list[str]
) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: False)
    app = _CopyApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down("#first", offset=(0, 0))
        await pilot.hover("#next", offset=(3, 0))
        await pilot.mouse_up("#next", offset=(3, 0))
        await wait_for(lambda: bool(app.screen.selections), pilot=pilot)

        await wait_for_clipboard(app)
        assert copied == []


class _EditorApp(App[None]):
    def compose(self) -> ComposeResult:
        yield EnhancedTextArea("hello world", id="editor")


async def test_finishing_a_drag_in_a_text_box_copies_it_and_keeps_it_selected(
    monkeypatch: pytest.MonkeyPatch, copied: list[str]
) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: True)
    app = _EditorApp()
    async with app.run_test(size=(40, 10)) as pilot:
        editor = app.query_one("#editor", EnhancedTextArea)
        # The default TextArea border and padding put the first character at (2, 1).
        await pilot.mouse_down("#editor", offset=(2, 1))
        await pilot.hover("#editor", offset=(7, 1))
        assert editor.selected_text == "hello"
        assert copied == []

        await pilot.mouse_up("#editor", offset=(7, 1))
        await wait_for(lambda: bool(copied), pilot=pilot)

        await wait_for_clipboard(app)
        assert copied == ["hello"]
        assert editor.selected_text == "hello"


async def test_a_click_or_keyboard_selection_in_a_text_box_leaves_the_clipboard_alone(
    monkeypatch: pytest.MonkeyPatch, copied: list[str]
) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: True)
    app = _EditorApp()
    async with app.run_test(size=(40, 10)) as pilot:
        editor = app.query_one("#editor", EnhancedTextArea)
        await pilot.click("#editor", offset=(2, 1))
        await pilot.press("shift+right", "shift+right")
        assert editor.selected_text == "he"

        await wait_for_clipboard(app)
        assert copied == []


async def test_a_text_box_drag_copies_nothing_where_copy_on_select_is_off(
    monkeypatch: pytest.MonkeyPatch, copied: list[str]
) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: False)
    app = _EditorApp()
    async with app.run_test(size=(40, 10)) as pilot:
        editor = app.query_one("#editor", EnhancedTextArea)
        await pilot.mouse_down("#editor", offset=(2, 1))
        await pilot.hover("#editor", offset=(7, 1))
        await pilot.mouse_up("#editor", offset=(7, 1))
        assert editor.selected_text == "hello"

        await wait_for_clipboard(app)
        assert copied == []


class _BlankScreen(RightClickScreenCopyMixin, Screen[None]):
    def compose(self) -> ComposeResult:
        yield Static("      ", id="blank")


class _BlankApp(App[None]):
    def get_default_screen(self) -> Screen[None]:
        return _BlankScreen()


async def test_a_drag_over_blank_text_keeps_the_clipboard(monkeypatch: pytest.MonkeyPatch, copied: list[str]) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: True)
    app = _BlankApp()
    async with app.run_test(size=(40, 10)) as pilot:
        await pilot.mouse_down("#blank", offset=(0, 0))
        await pilot.hover("#blank", offset=(4, 0))
        await pilot.mouse_up("#blank", offset=(4, 0))
        selected = app.screen.get_selected_text()
        assert selected is not None and not selected.strip()

        await wait_for_clipboard(app)
        assert copied == []


async def test_a_text_box_drag_over_blank_text_keeps_the_clipboard(
    monkeypatch: pytest.MonkeyPatch, copied: list[str]
) -> None:
    monkeypatch.setattr(right_click_copy, "copies_on_select", lambda: True)
    app = _EditorApp()
    async with app.run_test(size=(40, 10)) as pilot:
        editor = app.query_one("#editor", EnhancedTextArea)
        # Only the space between the two words.
        await pilot.mouse_down("#editor", offset=(7, 1))
        await pilot.hover("#editor", offset=(8, 1))
        await pilot.mouse_up("#editor", offset=(8, 1))
        assert editor.selected_text == " "

        await wait_for_clipboard(app)
        assert copied == []
