# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Built-in and user-theme groups keep the same selectable values across lists."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.theme import BUILTIN_THEMES
from textual.widgets import Button, OptionList
from textual.widgets._select import SelectOverlay

from chrys.app.tui.screens.themes.picker import ThemesScreen
from chrys.app.tui.theme import CHRYS_LEGACY_THEME, CHRYS_THEMES
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import UserThemeStore
from chrys.app.tui.widgets.select import Select
from tests.support.waiting import wait_for

from .helpers import _EditorScreen, make_app, open_editor, wait_for_themes


@pytest.mark.parametrize("users", [(), ("a-custom", "chrys-custom", "z-custom")])
async def test_theme_lists_group_by_source_and_keep_keyboard_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, users: tuple[str, ...]
) -> None:
    directory = tmp_path / "themes"
    monkeypatch.setattr("chrys.app.tui.theme_loader.default_theme_directory", lambda: directory)
    monkeypatch.setattr("chrys.app.tui.themes.store.default_theme_directory", lambda: directory)
    store = UserThemeStore(directory)
    for name in users:
        store.save(copy_theme(CHRYS_LEGACY_THEME, name=name), None)
    builtins = sorted({*BUILTIN_THEMES, *CHRYS_THEMES})
    expected = [*users, *builtins]
    app = make_app(tmp_path, "chrys", component=True)
    async with app.run_test(size=(100, 44)) as pilot:
        await app.push_screen(ThemesScreen(app.theme))
        await wait_for_themes(pilot)
        options = app.screen.query_one(OptionList)
        assert [option.id for option in options.options] == [*expected, "__manage_themes__"]
        assert [i for i, option in enumerate(options.options) if option._divider] == (
            [len(users) - 1, len(expected) - 1] if users else [len(builtins) - 1]
        )
        assert options.highlighted is not None
        assert options.get_option_at_index(options.highlighted).id == "chrys"
        if users:
            options.highlighted = len(users) - 1
            await pilot.press("down")
            assert app.theme == builtins[0]
            await pilot.press("up")
            assert app.theme == users[-1]
        await pilot.press("escape")
        await wait_for(lambda: isinstance(app.screen, _EditorScreen) and app.screen.is_mounted, pilot=pilot)
        editor = await open_editor(app, pilot, tmp_path)
        selector = editor.query_one(Select)
        await pilot.click(selector)
        overlay = selector.query_one(SelectOverlay)
        assert [str(option.prompt) for option in overlay.options] == expected
        assert [i for i, option in enumerate(overlay.options) if option._divider] == ([len(users) - 1] if users else [])
        if users:
            overlay.select(len(users))
            await pilot.press("up", "enter")
            await wait_for(lambda: editor.document.draft.name == users[-1] and not editor._switching, pilot=pilot)
            assert editor.query_one(Select).value == users[-1]
            assert editor.query_one("#theme-delete", Button).display
            assert app.theme == app._settings.theme == "chrys"
        else:
            await pilot.press("escape")
            # Exercise the empty -> first user -> empty choice transitions.
            # File save/delete transactions are covered by management/deletion;
            # this test owns list grouping and keyboard traversal only.
            app.register_user_theme(copy_theme(CHRYS_LEGACY_THEME, name="a-custom"))
            editor._refresh_selector()
            await pilot.click(selector)
            assert [str(option.prompt) for option in overlay.options] == ["a-custom", *builtins]
            assert overlay.get_option_at_index(0)._divider
            await pilot.press("escape")
            app.unregister_user_theme("a-custom")
            editor._refresh_selector()
            await pilot.click(selector)
            assert [str(option.prompt) for option in overlay.options] == builtins
            assert not any(option._divider for option in overlay.options)
