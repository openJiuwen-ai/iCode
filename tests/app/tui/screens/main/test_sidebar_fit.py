# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The sidebar stays on screen when the terminal is too narrow for its usual width."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.app.tui.theme import CHRYS_LEGACY_THEME
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import UserThemeStore
from chrys.app.tui.widgets.btw import ChatColumn
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel, TabOverflowMark
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import resize_when_settled
from tests.support.waiting import wait_for


def _right_mark_shown(sidebar: SidebarPanel, screen_width: int) -> bool:
    mark = sidebar.query_one("#sidebar-tabs-overflow-right", TabOverflowMark)
    return mark.active and mark.region.width == 2 and mark.region.right <= screen_width - 1


@pytest.mark.asyncio
async def test_a_narrow_terminal_shrinks_the_sidebar_to_the_room_beside_the_chat(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(60, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatColumn)
        # The chat keeps its 35 cells; the sidebar takes the 25 left, border and right mark included.
        await wait_for(lambda: sidebar.region.width == 25 and sidebar.region.right == 60, pilot=pilot)
        assert chat.region.width == 35
        await wait_for(lambda: _right_mark_shown(sidebar, 60), pilot=pilot)

        await resize_when_settled(pilot, 120, 30)
        await wait_for(lambda: sidebar.region.width == 42, pilot=pilot, description="the usual minimum is back")

        await resize_when_settled(pilot, 70, 24)
        await wait_for(lambda: sidebar.region.width == 35 and sidebar.region.right == 70, pilot=pilot)
        await wait_for(lambda: _right_mark_shown(sidebar, 70), pilot=pilot)

        # Narrower still, the sidebar's 30% alone would outgrow the 10 cells left.
        await resize_when_settled(pilot, 45, 24)
        await wait_for(lambda: sidebar.region.width == 10 and sidebar.region.right == 45, pilot=pilot)
        await wait_for(lambda: _right_mark_shown(sidebar, 45), pilot=pilot)
        assert chat.region.width == 35


@pytest.mark.asyncio
async def test_the_sidebar_hides_where_one_character_and_the_marks_no_longer_fit(tmp_path: Path) -> None:
    """One English letter, 4 cells for the marks and 2 for the border, beside the chat's 35: 42 columns."""
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(42, 24)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatColumn)
        await wait_for(lambda: sidebar.display and sidebar.region.width == 7, pilot=pilot)

        await resize_when_settled(pilot, 41, 24)
        await wait_for(lambda: not sidebar.display and chat.region.width == 41, pilot=pilot)
        await pilot.press("ctrl+g")
        await pilot.pause()
        assert not sidebar.display, "the sidebar cannot be opened where it does not fit"

        await resize_when_settled(pilot, 42, 24)
        await wait_for(lambda: sidebar.display and sidebar.region.width == 7, pilot=pilot)

        # A sidebar the user closed stays closed when the terminal widens again.
        await pilot.press("ctrl+g")
        await wait_for(lambda: not sidebar.display, pilot=pilot)
        await resize_when_settled(pilot, 41, 24)
        await resize_when_settled(pilot, 120, 30)
        await pilot.pause()
        assert not sidebar.display


@pytest.mark.asyncio
async def test_the_sidebar_follows_the_chat_column_s_minimum_width(tmp_path: Path) -> None:
    """The room is what the chat column's own ``min-width`` leaves, not a copy of it."""
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatColumn)
        chat.styles.min_width = 40

        await resize_when_settled(pilot, 60, 24)
        await wait_for(lambda: sidebar.region.width == 20 and sidebar.region.right == 60, pilot=pilot)
        assert chat.region.width == 40


@pytest.mark.asyncio
async def test_closing_the_compact_theme_editor_fits_the_sidebar_beside_the_chat_again(tmp_path: Path) -> None:
    """The compact editor drops the chat column's minimum; closing it restores the room beside the chat."""
    app = make_chrys_app(tmp_path / "sessions")
    async with app.run_test(size=(120, 30)) as pilot:
        main = app._main_screen
        assert main is not None
        sidebar = main.query_one(SidebarPanel)
        store = UserThemeStore(tmp_path / "themes")
        store.save(copy_theme(CHRYS_LEGACY_THEME, name="custom"), None)
        theme, revision = store.load("custom")
        await main.open_theme_editor(theme, store, revision)
        await resize_when_settled(pilot, 60, 24)
        await wait_for(lambda: main.has_class("--theme-editor-compact"), pilot=pilot)

        editor = main.theme_editor
        assert editor is not None
        editor.action_close()
        await wait_for(lambda: main.theme_editor is None and sidebar.display, pilot=pilot)
        await wait_for(lambda: sidebar.region.width == 25 and sidebar.region.right == 60, pilot=pilot)
