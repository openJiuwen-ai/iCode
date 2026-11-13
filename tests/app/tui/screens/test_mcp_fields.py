# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""MCP field rendering cannot send external controls to the terminal."""

from __future__ import annotations

import pytest

from chrys.app.tui.screens.agents.panels.mcp_fields import MCPInput, MCPTextArea
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import WidgetApp, click_when_settled
from tests.support.waiting import wait_for


@pytest.mark.parametrize(
    "control", ["\x1b]52;c;VEVTVA==\x1b\\", "\x00\x07\x08\x7f", "\x90\x9b\x9d\x9c", "\u202e\u2066\u200b\ufeff"]
)
async def test_control_rendering_preserves_raw_values_and_selection(monkeypatch, control: str) -> None:
    raw = "before" + control + "after"
    expected = sanitize_legacy_scalar(raw)
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_copy", copied.append)
    app = WidgetApp(lambda: [MCPInput(raw), MCPTextArea(raw + "\n\tsecond line")])
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        field = app.query_one(MCPInput)
        area = app.query_one(MCPTextArea)
        assert expected in field.render_line(0).text
        assert expected in area.render_line(0).text
        assert area.get_line(1).plain == "\tsecond line"
        assert field.value == raw
        assert area.text == raw + "\n\tsecond line"
        field.select_all()
        field.action_copy()
        assert copied[-1] == raw
        area.action_select_all()
        area.action_copy()
        assert copied[-1] == raw + "\n\tsecond line"


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("width", [12, 16, 20, 24])
@pytest.mark.parametrize("control", ["\x1b", "\x9b"])
async def test_wrapped_controls_do_not_hide_following_text(read_only: bool, width: int, control: str) -> None:
    raw = "a" * 8 + control * 10 + "VISIBLE_END"
    app = WidgetApp(lambda: MCPTextArea(raw, read_only=read_only))
    async with app.run_test(size=(60, 20)) as pilot:
        area = app.query_one(MCPTextArea)
        area.styles.width = width
        area.styles.height = 8
        await wait_for(
            lambda: area.region.width == width and screen_is_settled(app, app.screen),
            pilot=pilot,
            description="narrow field laid out",
        )

        visible = "".join(
            area.render_line(y).crop(0, area.content_size.width).text.rstrip() for y in range(area.content_size.height)
        )
        assert visible == sanitize_legacy_scalar(raw)
        assert area.text == raw


@pytest.mark.parametrize(("prefix", "column"), [("ab", 3), ("界a", 4), ("áb", 3)])
@pytest.mark.parametrize("control", ["\x1b", "\x9b"])
async def test_input_mouse_position_and_delete_follow_displayed_cells(prefix: str, column: int, control: str) -> None:
    raw = prefix + control + "cd"
    app = WidgetApp(lambda: MCPInput(raw))
    async with app.run_test(size=(40, 12)) as pilot:
        field = app.query_one(MCPInput)
        await click_when_settled(pilot, field, offset=(field.gutter.left + column, field.gutter.top))
        await pilot.press("delete")

        assert field.value == prefix + control + "d"
        assert field.cursor_position == len(prefix) + 1
        assert field.cursor_screen_offset.x == field.content_region.x + column


async def test_text_area_mouse_edit_and_undo_keep_raw_controls() -> None:
    raw = "ab\x1bcd\nsecond"
    app = WidgetApp(lambda: MCPTextArea(raw))
    async with app.run_test(size=(40, 12)) as pilot:
        area = app.query_one(MCPTextArea)
        await click_when_settled(pilot, area, offset=(area.gutter.left + 3, area.gutter.top))
        await pilot.press("delete")

        assert area.text == "ab\x1bd\nsecond"
        assert area.cursor_location == (0, 3)
        assert area.cursor_screen_offset.x == area.content_region.x + 3
        await pilot.press("ctrl+z")
        assert area.text == raw


async def test_input_scrolls_to_the_displayed_end() -> None:
    raw = "a" * 8 + "\x1b" * 10 + "VISIBLE_END"
    app = WidgetApp(lambda: MCPInput(raw))
    async with app.run_test(size=(20, 10)) as pilot:
        field = app.query_one(MCPInput)
        field.focus()
        await pilot.press("home", "end")
        await wait_for(lambda: field.scroll_x > 0, pilot=pilot, description="input scrolled to end")

        assert "VISIBLE_END" in field.render_line(0).text
        assert field.value == raw
        column = field.render_line(0).text.index("_END") + 1
        await click_when_settled(pilot, field, offset=(field.gutter.left + column, field.gutter.top))
        await pilot.press("delete")
        assert field.value == raw.removesuffix("END") + "ND"


async def test_text_area_scrolls_using_display_width_without_wrapping() -> None:
    raw = "a" * 8 + "\x1b" * 10 + "VISIBLE_END"
    app = WidgetApp(lambda: MCPTextArea(raw, soft_wrap=False))
    async with app.run_test(size=(20, 10)) as pilot:
        area = app.query_one(MCPTextArea)
        area.focus()
        area.move_cursor(area.document.end)
        await wait_for(lambda: area.scroll_x > 0, pilot=pilot, description="text area scrolled to end")

        assert "VISIBLE_END" in area.render_line(0).text
        assert area.content_region.contains(*area.cursor_screen_offset)
        assert area.text == raw
        column = area.render_line(0).text.index("_END") + 1
        await click_when_settled(pilot, area, offset=(area.gutter.left + column, area.gutter.top))
        await pilot.press("delete")
        assert area.text == raw.removesuffix("END") + "ND"


async def test_text_area_reload_and_incremental_edits_rewrap_safe_lines() -> None:
    raw = "a" * 8 + "\x1b" * 10 + "VISIBLE_END"
    app = WidgetApp(lambda: MCPTextArea("initial"))
    async with app.run_test(size=(20, 10)) as pilot:
        area = app.query_one(MCPTextArea)
        area.load_text("a" * 8 + "VISIBLE_END")
        area.insert("\x1b" * 10, (0, 8))
        await pilot.pause()

        visible = "".join(
            area.render_line(y).crop(0, area.content_size.width).text.rstrip() for y in range(area.content_size.height)
        )
        assert visible == sanitize_legacy_scalar(raw)
        assert area.text == raw
        area.focus()
        await pilot.press("ctrl+z")
        assert area.text == "a" * 8 + "VISIBLE_END"
        await pilot.press("ctrl+y")
        assert area.text == raw
