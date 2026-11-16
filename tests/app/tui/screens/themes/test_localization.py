# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live locale changes preserve theme drafts, modal edits and save outcomes."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest
from textual.containers import VerticalScroll
from textual.theme import Theme
from textual.widgets import Button, Input, Label, OptionList, Tabs
from textual.widgets._toast import Toast

from chrys.app.tui.screens.main import MainScreen
from chrys.app.tui.screens.settings.dialog import SettingsDialog
from chrys.app.tui.screens.themes.dialogs import _PickerModal
from chrys.app.tui.screens.themes.editor import ResettableThemeEditor
from chrys.app.tui.screens.themes.palette import _Xterm256PalettePicker
from chrys.app.tui.screens.themes.save import SaveThemeDialog
from chrys.app.tui.theme import CHRYS_LEGACY_THEME
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import _CONFLICT, ThemeFileRevision, ThemeStoreError, UserThemeStore
from chrys.app.tui.widgets.color_picker import ColorPicker
from chrys.app.tui.widgets.select import Select
from chrys.foundation.config.settings import Settings
from tests.support.tui_app_harness import EmptyAgentRegistry, make_chrys_app
from tests.support.waiting import wait_for

from .helpers import (
    NotificationCapture,
    make_app,
    open_editor,
    press_button,
    wait_for_confirmation,
    wait_for_picker,
    wait_for_save_dialog,
    wait_for_settings_dialog,
    wait_for_themes,
)


class _SettingsRegistry(EmptyAgentRegistry):
    def list_profiles(self, *, include_sub_agent_only: bool = True) -> list[object]:
        return []


async def test_settings_language_switch_relabels_editor_without_replacing_draft(tmp_path: Path) -> None:
    app = make_chrys_app(
        tmp_path, settings=Settings(theme="chrys-legacy", locale="en"), agent_registry=_SettingsRegistry()
    )
    async with app.run_test(size=(140, 50)) as pilot:
        await wait_for(lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted, pilot=pilot)
        main = app.screen
        assert isinstance(main, MainScreen)
        await main.open_theme_editor(copy_theme(app.current_theme), UserThemeStore(tmp_path / "themes"))
        editor = main.query_one(ResettableThemeEditor)
        await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
        token = editor.document.begin("color:primary")
        editor.preview_edit(token, "#123456")
        assert editor.commit_edit(token)
        document = editor.document
        history = list(document.undo_stack)
        selector = editor.query_one(Select)
        fields = editor.query_one("#theme-fields", VerticalScroll)
        fields.scroll_to(y=15, animate=False, immediate=True)
        await wait_for(lambda: fields.scroll_y == 15, pilot=pilot)
        await pilot.press("f10")
        await wait_for_settings_dialog(pilot)
        settings = app.screen
        assert isinstance(settings, SettingsDialog)
        locale = next(row for row in settings.rows() if row.spec.key == "ui.locale").query_one(Select)
        with patch.object(app, "refresh_css", autospec=True, side_effect=app.refresh_css) as refresh:
            locale.value = "zh-Hans"
            await wait_for(
                lambda: str(editor.query_one("#theme-editor-body").border_title) == "主题编辑器", pilot=pilot
            )
            assert str(editor.query_one("#theme-save", Button).label) == "保存"
            assert editor.query_one("#theme-colors-heading", Label).content.plain == "Colors"
            assert editor.query_one("#theme-group-border", Label).content.plain == "Borders"
            assert not editor.query("#editor-status")
            assert str(editor._variable_swatches["input-cursor-foreground"].label) == "(not set)"
            assert editor.query_one(Select) is selector
            assert editor.query_one("#theme-fields") is fields
            assert fields.scroll_y == 15
            assert editor.document is document and document.undo_stack == history
            assert document.draft.primary == "#123456"
            locale.value = "en"
            await wait_for(lambda: str(editor.query_one("#theme-save", Button).label) == "Save", pilot=pilot)
            assert editor.document.unsaved
            refresh.assert_not_called()


@pytest.mark.parametrize("kind", ["rgb", "raw", "xterm"])
async def test_picker_locale_switch_keeps_invalid_input_and_transaction(tmp_path: Path, kind: str) -> None:
    source = copy_theme(CHRYS_LEGACY_THEME)
    if kind == "raw":
        # Bare auto is valid CSS, but not a concrete picker color. This reaches
        # the raw editor without bypassing startup's invalid-theme recovery.
        source.variables["text-muted"] = "auto"
    app = make_app(tmp_path, source, component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        target = "var:text-muted" if kind == "raw" else "color:background"
        initial = editor.document.draft.variables["text-muted"] if kind == "raw" else editor.document.draft.background
        modal = _PickerModal(editor, target=target, initial=initial, allow_transparent=False)
        await app.push_screen(modal)
        await wait_for_picker(pilot, Input if kind == "raw" else ColorPicker)
        if kind == "xterm":
            modal.query_one(Tabs).active = "picker-mode-xterm"
            await wait_for_picker(pilot, _Xterm256PalettePicker)
            picker = modal.query_one(_Xterm256PalettePicker)
            selection = picker.selection_region
        else:
            field = modal.query_one("#css-value" if kind == "raw" else "#color-expression", Input)
            field.focus()
            field.value = "not-a-color"
            await wait_for(lambda: modal.query_one("#picker-confirm", Button).disabled, pilot=pilot)
            selection = field.selection
        transaction = editor.document.transaction
        token = modal.token
        focused = app.focused
        with patch.object(app, "refresh_css", autospec=True, side_effect=app.refresh_css) as refresh:
            app.locale_controller.switch_locale("zh-Hans")
            await wait_for(lambda: str(modal.query_one("#picker-confirm", Button).label) == "确认", pilot=pilot)
            assert app.screen is modal and modal.token == token
            assert editor.document.transaction is transaction
            assert app.focused is focused
            if kind == "xterm":
                assert modal.query_one(_Xterm256PalettePicker) is picker
                assert picker.selection_region == selection
                lines = "".join(segment.text for y in range(len(picker._rows)) for segment in picker.render_line(y))
                assert "灰阶" in lines and "此背景必须保持不透明" in lines
            else:
                assert field.value == "not-a-color" and field.selection == selection
                assert modal.query_one("#picker-confirm", Button).disabled
                if kind == "raw":
                    assert "CSS 值无效" in modal.query_one("#picker-error", Label).content.plain
                    assert "无效输入会保留" in modal.query_one("#picker-css-hint", Label).content.plain
                else:
                    assert "有效颜色" in modal.query_one("#color-error", Label).content.plain
                    assert modal.query_one("#color-expression-label", Label).content.plain == "颜色表达式 / HEX"
            app.locale_controller.switch_locale("en")
            await wait_for(lambda: str(modal.query_one("#picker-confirm", Button).label) == "Confirm", pilot=pilot)
            refresh.assert_not_called()
        if kind == "raw":
            assert "Invalid CSS value" in modal.query_one("#picker-error", Label).content.plain
            field.value = "#123456"
            await wait_for(lambda: app.current_theme.variables["text-muted"] == "#123456", pilot=pilot)
            assert not modal.query_one("#picker-confirm", Button).disabled
            modal._css_changed(Input.Changed(field, "not-a-color"))
            assert modal._current_value == "#123456"
            await press_button(pilot, "#picker-restore")
            await wait_for(
                lambda: field.value == "auto" and not modal.query_one("#picker-confirm", Button).disabled,
                pilot=pilot,
            )
            assert modal.query_one("#picker-error", Label).content.plain == ""
            assert app.current_theme.variables["text-muted"] == "auto"
            await press_button(pilot, "#picker-confirm")
            assert editor.document.draft.variables["text-muted"] == "auto"
        else:
            await press_button(pilot, "#picker-cancel")
        await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
        assert not editor.document.undo_stack


async def test_save_toast_uses_completion_locale_and_keeps_inflight_save_locked(tmp_path: Path) -> None:
    locale = "en"  # Submit in Chinese, then change language while disk I/O is blocked.
    app = make_app(tmp_path, "chrys-legacy", component=True)
    capture = NotificationCapture(expire_on_delivery=True)
    async with app.run_test(size=(140, 50), notifications=True, message_hook=capture) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        assert isinstance(dialog, SaveThemeDialog)
        field = dialog.query_one(Input)
        field.value = "chrys"
        await press_button(pilot, "#save-theme-confirm")
        await wait_for(lambda: dialog.query_one("#save-theme-confirm", Button).disabled, pilot=pilot)
        app.locale_controller.switch_locale("zh-Hans")
        assert dialog.query_one("#save-theme-error", Label).content.plain == "内置主题只读，请使用新名称。"  # noqa: RUF001
        assert field.value == "chrys"
        field.value = "saved-theme"
        await wait_for(lambda: not dialog.query_one("#save-theme-confirm", Button).disabled, pilot=pilot)
        started, release = Event(), Event()
        save = editor.store.save

        def delayed_save(theme: Theme, revision: ThemeFileRevision | None) -> ThemeFileRevision:
            started.set()
            if not release.wait(10):
                raise TimeoutError("Test did not release the save")
            return save(theme, revision)

        with (
            patch.object(editor.store, "save", autospec=True, side_effect=delayed_save),
            patch.object(app, "notify", autospec=True, side_effect=app.notify) as notify,
        ):
            try:
                dialog.query_one("#save-theme-confirm", Button).press()
                await wait_for(started.is_set)
                app.locale_controller.switch_locale(locale)
                button = dialog.query_one("#save-theme-confirm", Button)
                assert str(button.label) == "Saving…"
                assert button.disabled and field.disabled and field.value == "saved-theme"
                notify.assert_not_called()
            finally:
                release.set()
            await wait_for(lambda: app.screen is editor.screen and not editor.document.unsaved, pilot=pilot)
            # Pilot's queue barriers may outlive the three-second toast. The
            # delivered notification remains evidence of its completion locale.
            await wait_for(lambda: bool(capture.notifications), pilot=pilot, description="save notification delivered")
            (toast,) = capture.notifications
            expected = "Theme saved: saved-theme"
            assert toast.message == expected and toast.severity == "information" and not toast.markup
            assert toast.title == "Theme editor" and toast.timeout == 3
            assert toast.has_expired and not app.screen.query(Toast)
            assert (tmp_path / "themes" / "saved-theme.yaml").is_file()
            notify.assert_called_once()


async def test_failed_save_error_and_discard_prompt_retranslate_without_success_toast(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy", component=True)
    async with app.run_test(size=(140, 50)) as pilot:
        editor = await open_editor(app, pilot, tmp_path, new=True)
        await press_button(pilot, "#theme-save")
        await wait_for_save_dialog(pilot)
        dialog = app.screen
        with (
            patch.object(editor.store, "save", autospec=True, side_effect=ThemeStoreError(_CONFLICT.bind())),
            patch.object(app, "notify", autospec=True, side_effect=app.notify) as notify,
        ):
            await press_button(pilot, "#save-theme-confirm")
            await wait_for(
                lambda: "changed on disk" in dialog.query_one("#save-theme-error", Label).content.plain, pilot=pilot
            )
            app.locale_controller.switch_locale("zh-Hans")
            assert "主题文件已被修改" in dialog.query_one("#save-theme-error", Label).content.plain
            assert editor.document.unsaved
            notify.assert_not_called()
        await press_button(pilot, "#save-theme-cancel")
        await wait_for(lambda: app.screen is editor.screen, pilot=pilot)
        await press_button(pilot, "#theme-close")
        await wait_for_confirmation(pilot)
        confirm = app.screen
        app.locale_controller.switch_locale("en")
        assert str(confirm.query_one("#confirm-yes", Button).label) == "Discard"
        assert "unsaved changes" in confirm.query_one("#confirm-message").content.plain
        await press_button(pilot, "#confirm-no")
        await wait_for(
            lambda: app.screen is editor.screen and confirm not in app.locale_controller._surfaces,
            pilot=pilot,
        )
        assert editor in app.locale_controller._surfaces
        assert dialog not in app.locale_controller._surfaces and confirm not in app.locale_controller._surfaces


async def test_theme_list_relabels_manage_entry_without_changing_highlight(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(100, 40)) as pilot:
        await pilot.press("f9")
        await wait_for_themes(pilot)
        screen = app.screen
        options = screen.query_one(OptionList)
        assert str(options.get_option("__manage_themes__").prompt) == "Edit themes…"
        selected = options.highlighted
        app.locale_controller.switch_locale("zh-Hans")
        assert str(screen.query_one("#container").border_title) == "主题"
        assert str(options.get_option("__manage_themes__").prompt) == "修改主题…"
        assert options.highlighted == selected
        await pilot.press("escape")
        await wait_for(lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted, pilot=pilot)
        assert screen not in app.locale_controller._surfaces
