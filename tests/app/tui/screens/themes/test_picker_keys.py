# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keys the theme picker reads back to back, as a terminal hands over what was typed while the loop was busy."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App
from textual.events import Key
from textual.widgets import OptionList

from chrys.app.tui.screens.main import MainScreen
from tests.support.waiting import wait_for

from .helpers import make_app, wait_for_themes


def _type_at_once(app: App[object], *keys: tuple[str, str | None]) -> None:
    # The driver posts each key to the app, which forwards it to the focused list; nothing runs in between.
    for key, character in keys:
        app.post_message(Key(key, character))


@pytest.mark.parametrize("moves", [1, 2])
async def test_enter_typed_right_after_moves_applies_the_theme_moved_to(tmp_path: Path, moves: int) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    with patch("chrys.app.tui.app.persist_theme", autospec=True, return_value=None) as persist:
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            main = app.screen
            assert isinstance(main, MainScreen)
            await pilot.press("f9")
            picker = await wait_for_themes(pilot)
            options = picker.query_one(OptionList)
            assert options.highlighted is not None
            moved_to = options.get_option_at_index(options.highlighted + moves).id
            assert moved_to and moved_to != "chrys-legacy"

            _type_at_once(app, *((("down", None),) * moves), ("enter", "\r"))
            await wait_for(lambda: app.screen is main, pilot=pilot, description="the picker closed")
            await pilot.pause()

            assert app.theme == app._settings.theme == moved_to
            persist.assert_called_once_with(moved_to)


async def test_enter_typed_right_after_end_opens_theme_management(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    with patch("chrys.app.tui.app.persist_theme", autospec=True, return_value=None) as persist:
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            main = app.screen
            assert isinstance(main, MainScreen)
            await pilot.press("f9")
            await wait_for_themes(pilot)

            _type_at_once(app, ("end", None), ("enter", "\r"))
            await wait_for(
                lambda: app.screen is main and main.theme_editor is not None,
                pilot=pilot,
                description="the theme editor opened",
            )
            assert main.theme_editor is not None and main.theme_editor.editor.document.draft.name == "chrys-legacy"
            persist.assert_not_called()


@pytest.mark.parametrize("key", ["down", "end"])
async def test_escape_typed_right_after_a_move_keeps_the_original_theme(tmp_path: Path, key: str) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    with patch("chrys.app.tui.app.persist_theme", autospec=True, return_value=None) as persist:
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            main = app.screen
            assert isinstance(main, MainScreen)
            await pilot.press("f9")
            await wait_for_themes(pilot)

            _type_at_once(app, (key, None), ("escape", "\x1b"))
            await wait_for(lambda: app.screen is main, pilot=pilot, description="the picker closed")
            await pilot.pause()

            assert app.theme == app._settings.theme == "chrys-legacy"
            persist.assert_not_called()
