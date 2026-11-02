# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real app entry, read-only built-ins, saving, selection and cancellation."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import create_autospec, patch

import pytest
from textual.widgets import Button, Input, Label, OptionList
from textual.widgets._select import SelectOverlay

from chrys.app.tui.screens.main import MainScreen
from chrys.app.tui.screens.themes.editor import ResettableThemeEditor
from chrys.app.tui.screens.themes.panel import ThemeEditorPanel
from chrys.app.tui.theme import CHRYS_LEGACY_THEME
from chrys.app.tui.theme_loader import load_user_themes
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import ThemeStoreError, UserThemeStore
from chrys.app.tui.widgets.btw import ChatColumn
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.app_header import AppHeader
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.select import Select
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.foundation.config.settings import persist_theme
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for

from .helpers import (
    _EditorScreen,
    make_app,
    open_editor,
    press_button,
    wait_for_confirmation,
    wait_for_editor,
    wait_for_save_dialog,
    wait_for_themes,
)


@pytest.mark.parametrize(("source", "locale"), [("chrys-legacy", "zh-Hans"), ("ansi-dark", "en")])
async def test_manage_entry_builtin_protection_and_save_applies_theme(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    locale: str,
) -> None:
    directory = tmp_path / "themes"
    monkeypatch.setattr("chrys.app.tui.themes.store.default_theme_directory", lambda: directory)
    monkeypatch.setattr("chrys.app.tui.theme_loader.default_theme_directory", lambda: directory)
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = make_app(tmp_path, source, locale)
    original = copy_theme(app.get_theme(source))
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.press("f9")
        await wait_for_themes(pilot)
        options = app.screen.query_one(OptionList)
        assert options.get_option_at_index(options.option_count - 1).id == "__manage_themes__"
        options.highlighted = options.option_count - 1
        await pilot.press("enter")
        await wait_for(
            lambda: isinstance(app.screen, MainScreen) and bool(app.screen.query(ResettableThemeEditor)), pilot=pilot
        )
        editor = app.screen.query_one(ResettableThemeEditor)
        assert editor.query_one("#theme-select", Select).value == source
        assert not editor.query("#theme-new")
        assert not editor.query_one("#theme-delete", Button).display
        assert not editor._color_buttons["primary"].disabled
        assert not editor.document.unsaved
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        assert editor.document.unsaved
        assert app.theme == source
        assert app.get_theme(source) == original
        persisted.assert_not_called()
        await click_when_settled(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        field = app.screen.query_one(Input)
        dialog = app.screen
        name = field.value
        field.value = source.upper()
        assert await pilot.click("#save-theme-confirm")
        await wait_for(lambda: app.screen.query_one("#save-theme-confirm", Button).disabled, pilot=pilot)
        field.value = name
        button = app.screen.query_one("#save-theme-confirm", Button)
        await wait_for(
            lambda: not button.disabled and not button.has_class("-active") and not dialog._layout_required,
            pilot=pilot,
        )
        assert await pilot.click("#save-theme-confirm")
        await wait_for(lambda: isinstance(app.screen, MainScreen), pilot=pilot)
        assert not editor.document.unsaved
        assert app.theme == app._settings.theme == name
        assert app.current_theme == editor.document.draft
        persisted.assert_called_once_with(name)
        await wait_for(lambda: editor.query_one("#theme-delete", Button).display, pilot=pilot)
        assert not editor.query_one("#theme-delete", Button).disabled
        assert app.get_theme(source) == original
        assert app.get_theme(name) == editor.document.draft
        loaded, warnings = load_user_themes(directory)
        assert warnings == [] and loaded == [editor.document.draft]
        assert loaded[0].ansi == original.ansi
        # Delete has just appeared ahead of Close in the same row. Until that row is laid out again,
        # Close's old place is where Delete will be.
        await click_when_settled(pilot, "#theme-close")
        await wait_for(
            lambda: (
                app.theme_preview is None and isinstance(app.screen, MainScreen) and app.screen.theme_editor is None
            ),
            pilot=pilot,
        )
        assert isinstance(app.screen, MainScreen)
        assert app.screen.theme_editor is None
        assert app.current_theme == loaded[0]
        assert app.theme == app._settings.theme == name
        persisted.assert_called_once_with(name)
        await pilot.press("f9")
        await wait_for_themes(pilot)
        options = app.screen.query_one(OptionList)
        assert options.highlighted is not None
        assert options.get_option_at_index(options.highlighted).id == name
        await pilot.press("enter")
        await wait_for(lambda: app._settings.theme == name, pilot=pilot)
        persisted.assert_called_once_with(name)


def test_builtin_drafts_are_editable_but_cannot_overwrite_the_source(tmp_path: Path) -> None:
    store = UserThemeStore(tmp_path)
    editor = ThemeEditorPanel(CHRYS_LEGACY_THEME, store).editor
    token = editor.document.begin("color:primary")
    editor.document.stage(token, "#123456")
    assert editor.document.commit(token)
    assert CHRYS_LEGACY_THEME.primary != "#123456"
    with pytest.raises(ThemeStoreError):
        store.save(editor.document.draft, None)


async def test_discard_confirmation_retains_or_closes_the_draft(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy", "zh-Hans")
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        original = app.theme
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        await pilot.press("escape")
        await wait_for_confirmation(pilot)
        assert str(app.screen.query_one("#confirm-yes", Button).label) == "放弃修改"
        await pilot.click("#confirm-no")
        await wait_for(lambda: isinstance(app.screen, MainScreen), pilot=pilot)
        assert editor.document.draft.primary == "#123456"
        assert len(editor.document.undo_stack) == 1
        await pilot.press("escape")
        await wait_for_confirmation(pilot)
        await pilot.click("#confirm-yes")
        await wait_for(lambda: app.theme_preview is None, pilot=pilot)
        assert app.current_theme.name == app.theme == original
        assert not list((tmp_path / "themes").glob("*.yaml"))


async def test_save_conflict_keeps_unsaved_edits_and_undo_history(tmp_path: Path) -> None:
    store = UserThemeStore(tmp_path / "themes")
    theme = copy_theme(CHRYS_LEGACY_THEME, name="custom")
    revision = store.save(theme, None)
    app = make_app(tmp_path, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        assert isinstance(app.screen, _EditorScreen)
        await app.screen.open_theme_editor(theme, store, revision)
        editor = app.screen.query_one(ResettableThemeEditor)
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "blue")
        assert editor.commit_edit(token)
        revision.path.write_text("primary: red\n")
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        assert not app.screen.query_one(Input).disabled
        await press_button(pilot, "#save-theme-confirm")
        await wait_for(
            lambda: "changed on disk" in str(app.screen.query_one("#save-theme-error", Label).content), pilot=pilot
        )
        assert editor.document.unsaved
        assert len(editor.document.undo_stack) == 1
        assert editor.document.draft.primary == "blue"
        assert revision.path.read_text() == "primary: red\n"
        assert app.theme == app._settings.theme == "chrys"
        await press_button(pilot, "#save-theme-cancel")
        await wait_for_editor(pilot)
        await press_button(pilot, "#theme-undo")
        assert editor.document.draft == theme


async def test_narrow_save_cancel_restores_preview_without_persisting(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(60, 24)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await pilot.click("#theme-save")
        await wait_for_save_dialog(pilot)
        field = app.screen.query_one(Input)
        field.value = "MY-COPY"
        await pilot.pause()
        assert app.current_theme.name == editor.document.draft.name
        assert not app.has_class("-chrys")
        field.value = "bad/name"
        await pilot.click("#save-theme-confirm")
        await wait_for(lambda: app.screen.query_one("#save-theme-confirm", Button).disabled, pilot=pilot)
        assert app.screen.can_view_entire(app.screen.query_one("#save-theme-cancel"))
        assert app.screen.query_one("#save-theme-error", Label).size.height > 1
        await pilot.click("#save-theme-cancel")
        await wait_for(lambda: app.current_theme.name == editor.document.draft.name, pilot=pilot)
        assert not app.has_class("-chrys")
        assert editor.document.undo_stack == []
        assert not list((tmp_path / "themes").glob("*.yaml"))


@pytest.mark.parametrize("size", [(140, 50), (80, 32), (60, 24)])
async def test_editor_reflows_the_same_chat_beside_its_dock(tmp_path: Path, size: tuple[int, int]) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        chat = main.query_one(ChatPanel)
        original_region = chat.region
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await pilot.pause()
        panel = main.query_one(ThemeEditorPanel)
        assert panel.region.width <= 42
        if size[0] == 140:
            assert panel.region.width == 42
            for label in main.query(".--rows-grid Label"):
                assert isinstance(label, Label)
                if label.content.plain in {"scrollbar-background", "block-cursor-background"}:
                    # The right-hand reset gutter leaves 22 label columns;
                    # longer names wrap while the swatch keeps its full width.
                    assert label.region.height == (1 if label.content.plain == "scrollbar-background" else 2)
                    assert label.region.width > editor._color_buttons["primary"].region.width
        assert app.screen is main
        assert main.query_one(ChatPanel) is chat
        for widget in (chat, main.query_one(AppHeader), main.query_one(InputBar)):
            assert widget.region.x >= panel.region.right
            assert widget.region.width > 0
        assert main.query_one(AppHeader).region.width == size[0] - panel.region.width
        assert chat.region.right <= size[0]
        body = main.query_one("#theme-editor-body")
        assert main.query_one("#theme-close").region.right == body.region.right - 1
        assert main.query_one("#theme-save").region.right == body.region.right - 1
        assert chat.region.x - body.region.right == 0
        assert main.query_one(".--rows-grid Label").region.height == 1
        await pilot.press("f9")
        assert app.screen is main
        assert len(main.query(ThemeEditorPanel)) == 1
        assert await pilot.click("#theme-close")
        await wait_for_confirmation(pilot)
        # The confirmation helper waits for the affirmative button's focus.
        # Activate it without capturing mouse coordinates across modal reflow.
        await pilot.press("enter")
        await wait_for(lambda: app.screen is main, pilot=pilot, description="discard confirmation dismissed")
        await wait_for(
            lambda: main.theme_editor is None and app.theme_preview is None and chat.region == original_region,
            pilot=pilot,
            description="theme editor removed and original chat layout restored",
        )
        assert main.query_one(ChatPanel) is chat
        assert chat.region == original_region


# Each case also resizes across the compact breakpoint while the editor is open.
@pytest.mark.parametrize("width", [60, 140])
@pytest.mark.parametrize("source", ["chrys-ansi", "chrys-legacy"])
async def test_edit_entry_adopts_current_theme_without_restyling_transcript(
    tmp_path: Path, width: int, source: str
) -> None:
    app = make_app(tmp_path, source)
    async with app.run_test(size=(width, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        chat = main.query_one(ChatPanel)
        cards = [ToolCall(f"tool-{i}", "read_file", args={"path": f"file-{i}.py"}) for i in range(12)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("File contents")
        await wait_for(lambda: chat.virtual_size.height > chat.size.height, pilot=pilot)
        await pilot.press("f9")
        await wait_for_themes(pilot)
        options = app.screen.query_one(OptionList)
        options.highlighted = options.option_count - 1
        await pilot.pause()
        original = copy_theme(app.current_theme)
        variables = app.theme_variables.copy()
        column = main.query_one(ChatColumn)
        normal_minimum = column.styles.min_width
        assert normal_minimum is not None and normal_minimum.value == 35
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)
        transcript_nodes = set(chat.walk_children())
        with (
            patch.object(
                Path, "rglob", autospec=True, side_effect=AssertionError("Opening an editor must not scan source")
            ),
            patch("ast.parse", autospec=True, side_effect=ast.parse) as parse_python,
            patch.object(app, "refresh_css", autospec=True, side_effect=app.refresh_css) as refresh,
            patch.object(main, "update_node_styles", wraps=main.update_node_styles) as restyle_main,
            patch.object(app.stylesheet, "apply", wraps=app.stylesheet.apply) as style_node,
            patch("chrys.app.tui.themes.preview.validate_theme") as validate,
        ):
            await pilot.click(options, offset=(4, options.region.height - 1))
            await wait_for(lambda: main.theme_editor is not None and main.theme_editor.region.width > 0, pilot=pilot)
            await pilot.pause()
            assert main.theme_editor is not None
            editor = main.theme_editor.editor
            assert app.focused is editor._color_buttons["primary"]
            assert app.current_theme == original
            assert app.theme_variables == variables
            assert app.current_theme is not app.get_theme(source)
            assert main.query_one(SidebarPanel).display == (width >= 128)
            assert chat.region.x >= main.theme_editor.region.right
            assert chat.region.width > 0
            assert column.region.right <= main.region.right
            assert column.styles.min_width is not None
            assert column.styles.min_width.value == (0 if width < 128 else 35)
            for resized_width in (60, 140):
                await resize_when_settled(pilot, resized_width, 50)
                assert main.query_one(SidebarPanel).display == (resized_width >= 128)
                assert column.region.right <= main.region.right
                assert column.region.x >= main.theme_editor.region.right
                assert column.styles.min_width.value == (0 if resized_width < 128 else 35)
            assert column.styles.min_width == normal_minimum
            assert chat.region.x >= main.theme_editor.region.right
            assert editor.query_one(".--rows-grid Label").region.height == 1
            # Closing an unchanged draft also needs no theme work. The chat
            # must still reflow when the split panel is added and removed.
            await pilot.click("#theme-close")
            await wait_for(lambda: main.theme_editor is None and app.theme_preview is None, pilot=pilot)
            await pilot.pause()
            assert main.query_one(SidebarPanel).display
            refresh.assert_not_called()
            restyle_main.assert_not_called()
            validate.assert_not_called()
            assert not any(call.args[0] in transcript_nodes for call in style_node.call_args_list)
        assert app.screen is main
        assert main.query_one(ChatPanel) is chat
        assert len(chat.query(ToolCall)) == len(cards)
        # Textual parses literal action arguments in eval mode. Opening this
        # panel must never parse modules to discover their CSS definitions.
        assert all(call.kwargs.get("mode", "exec") == "eval" for call in parse_python.call_args_list)
        assert app.theme == app._settings.theme == source


async def test_dropdown_switches_in_place_and_protects_unsaved_drafts(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy", "zh-Hans", component=True)
    store = UserThemeStore(tmp_path / "themes")
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, _EditorScreen)
        await main.open_theme_editor(copy_theme(app.current_theme), store)
        editor = main.query_one(ResettableThemeEditor)
        await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
        await pilot.click("#theme-select")
        selector = editor.query_one(Select)
        assert selector.expanded
        await pilot.press("escape")
        assert not selector.expanded
        assert bool(main.query(ThemeEditorPanel))
        await pilot.click("#theme-select")
        assert selector.expanded
        # Use real type-ahead selection; the grouping tests cover arrow-key
        # traversal across every separator.
        await pilot.press("d", "enter")
        await wait_for(lambda: editor.document.draft.name == "dracula" and not editor._switching, pilot=pilot)
        assert main.query_one(ResettableThemeEditor) is editor
        assert app.screen is main
        assert app.theme == app._settings.theme == "chrys-legacy"
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        draft_id = editor.document.id
        editor.query_one(Select).value = "chrys-legacy"
        await wait_for_confirmation(pilot)
        await wait_for(lambda: app.screen.can_view_entire(app.screen.query_one("#confirm-no")), pilot=pilot)
        await pilot.click("#confirm-no")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert editor.document.id == draft_id
        assert editor.query_one(Select).value == "dracula"
        assert editor.document.draft.primary == "#123456"
        assert len(editor.document.undo_stack) == 1
        editor.query_one(Select).value = "chrys-legacy"
        await wait_for_confirmation(pilot)
        await wait_for(lambda: app.screen.can_view_entire(app.screen.query_one("#confirm-yes")), pilot=pilot)
        await pilot.click("#confirm-yes")
        await wait_for(lambda: editor.document.draft.name == "chrys-legacy" and not editor._switching, pilot=pilot)
        assert editor.document.id != draft_id
        editor.preview_edit(token, "#654321")
        assert not editor.commit_edit(token)
        assert editor.document.draft == CHRYS_LEGACY_THEME
        assert not editor.document.unsaved
        assert editor.document.undo_stack == []
        await click_when_settled(pilot, "#theme-close")
        await wait_for(lambda: not main.query(ThemeEditorPanel), pilot=pilot)
        assert app.screen is main


async def test_select_user_theme_and_save_as_preserves_original_file(tmp_path: Path) -> None:
    store = UserThemeStore(tmp_path / "themes")
    original = copy_theme(CHRYS_LEGACY_THEME, name="my-theme")
    store.save(original, None)
    app = make_app(tmp_path, "chrys-legacy", component=True)
    app.register_user_theme(original)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, _EditorScreen)
        await main.open_theme_editor(copy_theme(app.current_theme), store)
        editor = main.query_one(ResettableThemeEditor)
        await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
        editor.query_one(Select).value = "my-theme"
        await wait_for(lambda: editor.revision is not None and not editor._switching, pilot=pilot)
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        field = app.screen.query_one(Input)
        assert not field.disabled
        field.value = "CHRYS-DARK"
        await press_button(pilot, "#save-theme-confirm")
        await wait_for(lambda: app.screen.query_one("#save-theme-confirm", Button).disabled, pilot=pilot)
        field.value = "my-new-theme"
        button = app.screen.query_one("#save-theme-confirm", Button)
        await wait_for(lambda: not button.disabled and not button.has_class("-active"), pilot=pilot)
        await press_button(pilot, "#save-theme-confirm")
        await wait_for(lambda: app.screen is main and not editor.document.unsaved, pilot=pilot)
        assert store.load("my-theme")[0] == original
        assert store.load("my-new-theme")[0].primary == "#123456"
        assert editor.query_one(Select).value == "my-new-theme"
        assert app.theme == app._settings.theme == "my-new-theme"
        await wait_for_editor(pilot)
        selector = editor.query_one(Select)
        await pilot.click(selector)
        overlay = selector.query_one(SelectOverlay)
        await wait_for(lambda: selector.expanded and overlay.region.height > 0, pilot=pilot)
        assert overlay.highlighted == 0
        assert str(overlay.get_option_at_index(0).prompt) == "my-new-theme"
        assert overlay.get_option_at_index(1)._divider
        await pilot.press("escape")
        await press_button(pilot, "#theme-close")
        await wait_for(lambda: app.theme_preview is None, pilot=pilot)
        assert app.current_theme == store.load("my-new-theme")[0]


@pytest.mark.parametrize("active", ["custom", "chrys-legacy"])
async def test_saving_user_theme_survives_later_preview_and_discard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active: str
) -> None:
    store = UserThemeStore(tmp_path / "themes")
    theme = copy_theme(CHRYS_LEGACY_THEME, name="custom")
    revision = store.save(theme, None)
    monkeypatch.setattr("chrys.app.tui.theme_loader.default_theme_directory", lambda: store.directory)
    persisted = create_autospec(persist_theme)
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", persisted)
    app = make_app(tmp_path, active, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, _EditorScreen)
        await main.open_theme_editor(theme, store, revision)
        editor = main.query_one(ResettableThemeEditor)
        await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        await press_button(pilot, "#save-theme-confirm")
        await wait_for(lambda: app.screen is main and not editor.document.unsaved, pilot=pilot)
        saved = store.load("custom")[0]
        assert saved.primary == "#123456"
        assert app.current_theme == saved
        assert app.theme == app._settings.theme == "custom"
        if active == "custom":
            persisted.assert_not_called()
        else:
            persisted.assert_called_once_with("custom")

        # A later draft, including another theme, must not replace the saved
        # selection or the colors restored when the user discards that draft.
        await wait_for_editor(pilot)
        editor.query_one(Select).value = "dracula"
        await wait_for(lambda: editor.document.draft.name == "dracula" and not editor._switching, pilot=pilot)
        app.theme = "chrys"
        assert app.theme == "custom"
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#654321")
        assert editor.commit_edit(token)
        await press_button(pilot, "#theme-close")
        await wait_for_confirmation(pilot)
        await wait_for(lambda: app.screen.can_view_entire(app.screen.query_one("#confirm-yes")), pilot=pilot)
        await press_button(pilot, "#confirm-yes")
        await wait_for(lambda: not main.query(ThemeEditorPanel) and app.theme_preview is None, pilot=pilot)
        assert app.current_theme == saved
        assert app.theme == app._settings.theme == "custom"

    restarted = make_app(tmp_path, app._settings.theme)
    assert restarted.get_theme("custom") == saved
    assert restarted.theme == restarted._settings.theme == "custom"
