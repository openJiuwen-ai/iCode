# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Save-name validation on submission and completion while another dialog owns the screen."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from textual.color import Color
from textual.theme import Theme
from textual.widgets import Button, Input, Label

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.themes.editor import ResettableThemeEditor
from chrys.app.tui.theme import CHRYS_LEGACY_THEME
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.preview import validate_theme
from chrys.app.tui.themes.store import _CONFLICT, _EXISTS, _IO_ERROR, ThemeFileRevision, ThemeStoreError, UserThemeStore
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from tests.support.waiting import wait_for

from .helpers import _EditorScreen, make_app, open_editor, press_button, wait_for_save_dialog


# Each spelling and each way to submit, once: the conflict is reached by the button either way,
# and Enter and the button end in the same save handler, so the two do not interact.
@pytest.mark.parametrize(("submit", "name"), [("button", "custom"), ("enter", "CUSTOM")])
async def test_restoring_original_name_after_case_only_conflict_allows_overwrite(
    tmp_path: Path, name: str, submit: str
) -> None:
    store = UserThemeStore(tmp_path / "themes")
    original = copy_theme(CHRYS_LEGACY_THEME, name=name)
    revision = store.save(original, None)
    app = make_app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, _EditorScreen)
        await main.open_theme_editor(original, store, revision)
        editor = main.query_one(ResettableThemeEditor)
        await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        field = dialog.query_one(Input)
        button = dialog.query_one("#save-theme-confirm", Button)
        assert field.value == name and not button.disabled

        with patch.object(store, "validate_name", autospec=True, side_effect=store.validate_name) as check:
            field.value = name.swapcase()
            await press_button(pilot, button)
            await wait_for(lambda: button.disabled, pilot=pilot)
            assert dialog._error_message == _EXISTS.bind()
            assert app.theme == app._settings.theme == "chrys-legacy"
            check.assert_called_once_with(name.swapcase(), None)

            field.value = name
            await wait_for(lambda: dialog._last_input == name, pilot=pilot)
            assert not button.disabled
            assert not dialog.query_one("#save-theme-error").has_class("--error")
            check.assert_called_once()
            # Re-entering the rejected spelling reuses its error; restoring
            # the original spelling still enables a revision-backed overwrite.
            field.value = name.swapcase()
            await wait_for(lambda: button.disabled, pilot=pilot)
            field.value = name
            await wait_for(lambda: not button.disabled, pilot=pilot)
            check.assert_called_once()
            if submit == "enter":
                field.focus()
                await pilot.press("enter")
            else:
                await wait_for(lambda: not button.has_class("-active"), pilot=pilot)
                await press_button(pilot, button)
            await wait_for(lambda: app.screen is main and not editor.document.unsaved, pilot=pilot)
            assert check.call_args.args == (name, revision)

        saved, latest = store.load(name)
        assert saved.primary == "#123456"
        assert latest.path == revision.path and latest.digest != revision.digest
        assert list(store.directory.glob("*.yaml")) == [revision.path]
        assert editor.revision == latest
        assert app.current_theme == saved
        assert app.theme == app._settings.theme == name


@pytest.mark.parametrize("failure", ["exists", "conflict", "io"])
async def test_save_failure_only_enables_retry_for_io_errors(tmp_path: Path, failure: str) -> None:
    app = make_app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        button = dialog.query_one("#save-theme-confirm", Button)
        definition = {"exists": _EXISTS, "conflict": _CONFLICT, "io": _IO_ERROR}[failure]
        error = ThemeStoreError(definition.bind(reason="Permission denied") if failure == "io" else definition.bind())
        with patch.object(editor.store, "save", side_effect=error) as save:
            await press_button(pilot, button)
            await wait_for(lambda: dialog._error_message == error.display and not dialog._saving, pilot=pilot)
            assert not dialog.query_one(Input).disabled
            assert button.disabled == (failure != "io")
            assert dialog.query_one("#save-theme-error", Label).content.plain
            save.assert_called_once()
            assert editor.document.unsaved
            assert not list(editor.store.directory.glob("*.yaml"))
            assert app.theme == app._settings.theme == "chrys-legacy"
        if failure == "io":
            await wait_for(lambda: not button.has_class("-active"), pilot=pilot)
            await press_button(pilot, button)
            await wait_for(lambda: app.screen is editor.screen and not editor.document.unsaved, pilot=pilot)
            assert list(editor.store.directory.glob("*.yaml"))
        else:
            dialog.query_one(Input).value = "different-name"
            await wait_for(lambda: not button.disabled, pilot=pilot)
            assert not dialog.query_one("#save-theme-error").has_class("--error")


async def test_name_typing_never_checks_files_or_restyles_a_populated_transcript(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        chat = app.screen.query_one(ChatPanel)
        cards = [ToolCall(f"save-{i}", "read_file", args={"path": str(i)}) for i in range(12)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("File contents")
        await wait_for(lambda: chat.virtual_size.height > chat.size.height, pilot=pilot)
        editor = await open_editor(app, pilot, tmp_path, new=True)
        original_name = app.current_theme.name
        with (
            patch.object(
                editor.store, "validate_name", autospec=True, side_effect=editor.store.validate_name
            ) as check_name,
            patch("chrys.app.tui.themes.preview.validate_theme", autospec=True, side_effect=validate_theme) as validate,
        ):
            await pilot.click("#theme-save")
            await wait_for_save_dialog(pilot)
            dialog = app.screen
            field = dialog.query_one(Input)
            check_name.assert_not_called()
            validate.assert_not_called()
            with (
                patch.object(dialog, "set_timer", autospec=True, side_effect=dialog.set_timer) as timer,
                patch.object(app, "refresh_css", autospec=True, side_effect=app.refresh_css) as refresh,
            ):
                field.value = ""
                await wait_for(lambda: dialog._last_input == "", pilot=pilot)
                field.focus()
                await pilot.press(*"mytheme9")
                await wait_for(lambda: dialog._last_input == "mytheme9", pilot=pilot)
                assert not dialog.query_one("#save-theme-confirm", Button).disabled
                assert dialog.query_one("#save-theme-error", Label).content.plain == (
                    "Built-in themes cannot be overwritten. New themes must have a unique name."
                )
                assert app.current_theme.name == original_name
                check_name.assert_not_called()
                validate.assert_not_called()
                refresh.assert_not_called()
                timer.assert_not_called()

                await pilot.click("#save-theme-confirm")
                await wait_for(lambda: app.screen is editor.screen and not editor.document.unsaved, pilot=pilot)
                validate.assert_called_once()
                # The target-name preview and the committed selection each
                # restyle once; typing still performs neither pass.
                assert refresh.call_count == 2
                assert app.theme == app._settings.theme == "mytheme9"
                assert check_name.called
                assert editor.store.load("mytheme9")[0] == editor.document.draft
                assert chat.query(ToolCall).last() is cards[-1]


@pytest.mark.parametrize("action", ["enter", "button", "cancel"])
async def test_name_is_checked_only_on_submit_and_cancel_keeps_the_preview(tmp_path: Path, action: str) -> None:
    app = make_app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        field = dialog.query_one(Input)
        original_name = app.current_theme.name
        with patch.object(dialog, "_preview", autospec=True, side_effect=dialog._preview) as preview:
            field.value = "latest-name"
            if action != "cancel":
                if action == "enter":
                    await field.action_submit()
                else:
                    await press_button(pilot, "#save-theme-confirm")
                await wait_for(lambda: app.screen is editor.screen and not editor.document.unsaved, pilot=pilot)
                assert (tmp_path / "themes" / "latest-name.yaml").is_file()
                assert editor.document.draft.name == "latest-name"
                preview.assert_called_once()
                assert preview.call_args.args[0].name == "latest-name"
            else:
                await pilot.press("escape")
                await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
                preview.assert_not_called()
                assert app.current_theme.name == original_name
                assert app.theme == app._settings.theme == "chrys-legacy"
                assert not (tmp_path / "themes" / "latest-name.yaml").exists()


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("conflict", ["builtin", "user"])
async def test_duplicate_name_stays_disabled_until_changed_without_rechecking(
    tmp_path: Path, locale: str, conflict: str
) -> None:
    app = make_app(tmp_path, "chrys-legacy", locale, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        rejected_name = "chrys" if conflict == "builtin" else "existing-theme"
        original_file = None
        original_data = None
        if conflict == "user":
            revision = editor.store.save(copy_theme(editor.document.draft, name=rejected_name), None)
            original_file = revision.path
            original_data = original_file.read_bytes()
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        field = dialog.query_one(Input)
        button = dialog.query_one("#save-theme-confirm", Button)
        status = dialog.query_one("#save-theme-error", Label)
        warning = (
            "内置主题只读，新建主题不能与已有的主题重名。"  # noqa: RUF001
            if locale == "zh-Hans"
            else "Built-in themes cannot be overwritten. New themes must have a unique name."
        )
        assert status.content.plain == warning
        assert status.styles.color == Color.parse(app.get_css_variables()["warning"])
        assert button.region.y == status.region.bottom + 1
        assert button.region.bottom == dialog.query_one("#save-theme-container").content_region.bottom

        with patch.object(
            editor.store, "validate_name", autospec=True, side_effect=editor.store.validate_name
        ) as check:
            field.value = rejected_name
            await wait_for(lambda: dialog._last_input == rejected_name, pilot=pilot)
            assert not button.disabled and status.content.plain == warning
            check.assert_not_called()
            await press_button(pilot, button)
            await wait_for(lambda: button.disabled, pilot=pilot)
            assert status.has_class("--error")
            assert status.styles.color == Color.parse(app.get_css_variables()["error"])
            error_text = status.content.plain
            assert error_text != warning
            check.assert_called_once()

            field.focus()
            await pilot.press("enter")
            field.value = rejected_name.upper()
            await wait_for(lambda: dialog._last_input == rejected_name.upper(), pilot=pilot)
            assert button.disabled and status.content.plain == error_text
            check.assert_called_once()
            field.value = "new-theme"
            await wait_for(lambda: not button.disabled, pilot=pilot)
            assert status.content.plain == warning and not status.has_class("--error")
            assert status.styles.color == Color.parse(app.get_css_variables()["warning"])
            check.assert_called_once()
            field.value = rejected_name
            await wait_for(lambda: button.disabled, pilot=pilot)
            assert status.content.plain == error_text
            check.assert_called_once()
            field.value = "new-theme"
            await wait_for(lambda: not button.disabled and not button.has_class("-active"), pilot=pilot)
            await press_button(pilot, button)
            await wait_for(lambda: app.screen is editor.screen and not editor.document.unsaved, pilot=pilot)
        assert editor.store.load("new-theme")[0] == editor.document.draft
        if original_file is not None:
            assert original_file.read_bytes() == original_data


async def test_save_completion_does_not_pop_a_covering_dialog(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        name = dialog.query_one(Input).value
        started, release = Event(), Event()
        save = editor.store.save

        def held_save(theme: Theme, revision: ThemeFileRevision | None) -> ThemeFileRevision:
            started.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release the save")
            return save(theme, revision)

        with patch.object(editor.store, "save", autospec=True, side_effect=held_save):
            cover = ConfirmDialog()
            try:
                dialog.query_one("#save-theme-confirm", Button).press()
                await wait_for(started.is_set)
                await app.push_screen(cover)
                await wait_for(lambda: cover.is_mounted)
            finally:
                release.set()
            await wait_for(lambda: dialog._dismiss_requested, pilot=pilot)
            assert app.screen is cover and dialog in app.screen_stack
            assert not editor.document.unsaved
            assert app.theme == app._settings.theme == name
            assert (tmp_path / "themes" / f"{name}.yaml").is_file()
            await pilot.press("escape")
            await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
            assert dialog not in app.screen_stack
