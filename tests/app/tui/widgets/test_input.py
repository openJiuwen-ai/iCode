# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the reusable EnhancedInput widget."""

from __future__ import annotations

import pytest
from textual.actions import SkipAction
from textual.app import App, ComposeResult

from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.widgets.input import EnhancedInput
from tests.support.waiting import wait_for


class _Harness(App[None]):
    def compose(self) -> ComposeResult:
        yield EnhancedInput("hello world", id="input")


@pytest.mark.asyncio
async def test_ctrl_a_selects_all_and_delete_clears_value() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("ctrl+a")
        await pilot.pause()

        assert input_widget.selected_text == "hello world"

        await pilot.press("backspace")
        await pilot.pause()

        assert input_widget.value == ""


@pytest.mark.asyncio
async def test_regular_keys_still_use_textual_input_handling() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.value = ""
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.pause()

        await pilot.press("x")
        await pilot.pause()

        assert input_widget.value == "x"


@pytest.mark.asyncio
@pytest.mark.parametrize("copy_key", ["ctrl+c", "ctrl+insert"])
async def test_copy_keys_synchronize_app_and_os_clipboards(
    monkeypatch: pytest.MonkeyPatch,
    copy_key: str,
) -> None:
    os_clipboard: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", os_clipboard.append)

    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()
        await pilot.pause()

        await pilot.press(copy_key)
        await pilot.pause()

        assert input_widget.value == "hello world"
        assert app.clipboard == "hello world"
        await wait_for_clipboard(app)
        assert os_clipboard == ["hello world"]


@pytest.mark.asyncio
async def test_ctrl_x_then_ctrl_v_round_trips_through_os_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    os_clipboard = {"text": "older-os-text"}
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.clipboard_copy",
        lambda text: os_clipboard.__setitem__("text", text),
    )
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.platform_helpers.clipboard_paste",
        lambda: os_clipboard["text"],
    )

    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()
        await pilot.pause()

        await pilot.press("ctrl+x")
        await pilot.pause()

        assert input_widget.value == ""
        await wait_for_clipboard(app)
        assert os_clipboard["text"] == "hello world"

        app.copy_to_clipboard("stale-app-text")
        await pilot.press("ctrl+v")
        await pilot.pause()

        assert input_widget.value == "hello world"


@pytest.mark.asyncio
async def test_copy_without_selection_preserves_skip_action() -> None:
    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await pilot.pause()
        input_widget.cursor_position = len(input_widget.value)

        with pytest.raises(SkipAction):
            input_widget.action_copy()


@pytest.mark.asyncio
@pytest.mark.parametrize("paste_key", ["ctrl+v", "shift+insert"])
async def test_paste_keys_prefer_current_os_clipboard(
    monkeypatch: pytest.MonkeyPatch,
    paste_key: str,
) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: "fresh-system-key")

    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        app.copy_to_clipboard("stale-app-clipboard")
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()
        await pilot.pause()

        await pilot.press(paste_key)
        await pilot.pause()

        assert input_widget.value == "fresh-system-key"


@pytest.mark.asyncio
async def test_ctrl_v_falls_back_to_app_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: "")

    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        app.copy_to_clipboard("fallback-key")
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()
        await pilot.pause()

        await pilot.press("ctrl+v")
        await pilot.pause()

        assert input_widget.value == "fallback-key"


@pytest.mark.asyncio
async def test_ctrl_v_keeps_single_line_input_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr(
        "chrys.app.tui.clipboard.platform_helpers.clipboard_paste",
        lambda: "first-line\nignored-line",
    )

    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()
        await pilot.pause()

        await pilot.press("ctrl+v")
        await pilot.pause()

        assert input_widget.value == "first-line"


@pytest.mark.asyncio
@pytest.mark.parametrize("paste_key", ["ctrl+v", "shift+insert"])
@pytest.mark.parametrize("clipboard", ["", "\n", "\r\n", "\nrest"])
async def test_paste_of_empty_first_line_preserves_value_and_selection(
    monkeypatch: pytest.MonkeyPatch, paste_key: str, clipboard: str
) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: clipboard)
    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()
        selection = input_widget.selection

        await pilot.press(paste_key)

        assert input_widget.value == "hello world"
        assert input_widget.selection == selection


@pytest.mark.asyncio
@pytest.mark.parametrize("clipboard", ["   ", "   \nrest", "  first line  \nrest"])
async def test_paste_preserves_first_line_whitespace(monkeypatch: pytest.MonkeyPatch, clipboard: str) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: clipboard)
    app = _Harness()
    async with app.run_test() as pilot:
        input_widget = app.query_one("#input", EnhancedInput)
        input_widget.focus()
        await wait_for(lambda: input_widget.has_focus, pilot=pilot, description="control focus before interaction")
        input_widget.select_all()

        await pilot.press("ctrl+v")

        assert input_widget.value == clipboard.splitlines()[0]
        assert input_widget.selected_text == ""
