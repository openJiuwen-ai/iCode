# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Docked editor integration with layout, Settings, navigation and preview teardown."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from textual.color import Color
from textual.widgets import Button, OptionList, TextArea

from chrys.app.tui.screens.main import MainScreen
from chrys.app.tui.terminal import Terminal
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import UserThemeStore
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.app.tui.widgets.select import Select
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import UserInjectCancel
from tests.support.tui_app_harness import EmptyAgentRegistry, SessionGenerationEngine, make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for

from .helpers import make_app, wait_for_confirmation, wait_for_editor, wait_for_settings_dialog, wait_for_themes


@pytest.mark.parametrize("dirty", [False, True])
async def test_entering_shell_closes_editor_after_resolving_unsaved_changes(tmp_path: Path, dirty: bool) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    with patch("chrys.app.tui.app.persist_theme", autospec=True) as persist:
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            main = app.screen
            assert isinstance(main, MainScreen)
            original = copy_theme(app.current_theme)
            await main.manage_themes()
            await wait_for(lambda: main.theme_editor is not None, pilot=pilot)
            panel = main.theme_editor
            editor = panel.editor
            await wait_for(lambda: app.focused is editor._color_buttons["primary"], pilot=pilot)
            if dirty:
                token = editor.document.begin("color:primary")
                editor.preview_edit(token, "#123456")
                assert editor.commit_edit(token)
            shell = main.query_one(ShellPanel)
            with patch.object(shell, "_start_shell", autospec=True) as start_shell:
                main.query_one(InputBar).focus_input()
                await wait_for(lambda: app.focused is main.query_one(InputBar).query_one(TextArea), pilot=pilot)
                await pilot.press("!")
                if dirty:
                    await wait_for_confirmation(pilot)
                    start_shell.assert_not_called()
                    assert not main.shell_mode_state
                    await pilot.press("escape")
                    await wait_for(lambda: app.screen is main, pilot=pilot)
                    assert main.theme_editor is panel and editor.document.unsaved
                    assert app.current_theme.primary == "#123456"
                    assert not main.shell_mode_state
                    main.query_one(InputBar).focus_input()
                    await wait_for(lambda: app.focused is main.query_one(InputBar).query_one(TextArea), pilot=pilot)
                    await pilot.press("!")
                    await wait_for_confirmation(pilot)
                    await pilot.click("#confirm-yes")
                # Entering shell mode asks for the terminal's focus, which the app gives once it gets to
                # that request: after the state has changed.
                await wait_for(
                    lambda: (
                        main.theme_editor is None and main.shell_mode_state and app.focused is shell.query_one(Terminal)
                    ),
                    pilot=pilot,
                    description="shell mode and terminal focus after closing the theme editor",
                )
                assert app.theme_preview is None and app.current_theme == original
                assert not main.has_class("--theme-editor-compact")
                assert main.query_one(SidebarPanel).display
                assert shell.display and app.focused is shell.query_one(Terminal)
                assert not main.query_one(ChatPanel).display
                start_shell.assert_called_once()
                persist.assert_not_called()


@pytest.mark.parametrize("sidebar_visible", [True, False])
@pytest.mark.parametrize("close_editor_first", [True, False])
async def test_alternate_screen_and_compact_dock_share_sidebar_restoration(
    tmp_path: Path, sidebar_visible: bool, close_editor_first: bool
) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        sidebar = main.query_one(SidebarPanel)
        if not sidebar_visible:
            await pilot.press("ctrl+g")
        await pilot.press("f9")
        await wait_for_themes(pilot)
        # Fullscreen mode disables F9, but a background terminal can enter
        # alternate screen after the theme picker has already opened.
        terminal = main.query_one(Terminal)
        terminal.post_message(Terminal.AlternateScreenChanged(terminal, True))
        await wait_for(lambda: main._state.shell.fullscreen_terminal and not sidebar.display, pilot=pilot)
        await pilot.press("end", "enter")
        await wait_for(lambda: main.theme_editor is not None and app.screen is main, pilot=pilot)
        assert not sidebar.display

        async def leave_alternate() -> None:
            terminal.post_message(Terminal.AlternateScreenChanged(terminal, False))
            await wait_for(lambda: not main._state.shell.fullscreen_terminal, pilot=pilot)

        async def close_editor() -> None:
            await click_when_settled(pilot, "#theme-close")
            await wait_for(lambda: main.theme_editor is None, pilot=pilot)

        if close_editor_first:
            await close_editor()
            assert not sidebar.display
            await leave_alternate()
        else:
            await leave_alternate()
            assert not sidebar.display and main.query_one(ChatPanel).region.width >= 40
            await close_editor()
        assert sidebar.display == sidebar_visible
        # Repeated terminal exit messages must not invert the restored choice.
        await leave_alternate()
        assert sidebar.display == sidebar_visible


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_theme_command_reports_preview_rejection_without_logging_success(tmp_path: Path, locale: str) -> None:
    app = make_app(tmp_path, "chrys-legacy", locale)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        await main.manage_themes()
        await wait_for(lambda: main.theme_editor is not None, pilot=pilot)
        input_bar = main.query_one(InputBar)
        with (
            patch.object(app, "notify", wraps=app.notify) as notify,
            patch.object(main.query_one(SidebarPanel).debug_panel, "log_event") as debug,
            patch("chrys.app.tui.app.persist_theme", autospec=True) as persist,
        ):
            input_bar.value = "/theme dracula"
            await input_bar.action_submit()
            await wait_for(lambda: notify.call_count == 1, pilot=pilot)
            expected = "Close the theme editor" if locale == "en" else "关闭主题编辑器"
            assert expected in notify.call_args.args[0]
            assert notify.call_args.kwargs["severity"] == "warning"
            assert app.theme == app._settings.theme == "chrys-legacy"
            assert not any(call.args[0] == "ThemeChanged" for call in debug.call_args_list)
            persist.assert_not_called()
            await click_when_settled(pilot, "#theme-close")
            await wait_for(lambda: main.theme_editor is None, pilot=pilot)
            input_bar.value = "/theme dracula"
            await input_bar.action_submit()
            await wait_for(lambda: app.theme == app._settings.theme == "dracula", pilot=pilot)
            persist.assert_called_once_with("dracula")
            debug.assert_any_call("ThemeChanged", "dracula")


@pytest.mark.parametrize("sidebar_visible", [True, False])
async def test_compact_dock_respects_inline_sidebar_state_and_restores_it(
    tmp_path: Path, sidebar_visible: bool
) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(80, 40)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        sidebar = main.query_one(SidebarPanel)
        chat = main.query_one(ChatPanel)
        await chat.mount(*(ToolCall(f"layout-{i}", "read_file", args={"path": str(i)}) for i in range(6)))
        await pilot.press("ctrl+g", "ctrl+g")
        if not sidebar_visible:
            await pilot.press("ctrl+g")
        assert sidebar.display == sidebar_visible
        with patch.object(main, "update_node_styles", wraps=main.update_node_styles) as restyle:
            await main.manage_themes()
            await wait_for(lambda: main.theme_editor is not None and chat.region.width >= 40, pilot=pilot)
            assert not sidebar.display
            await pilot.press("ctrl+g")
            assert not sidebar.display and chat.region.width >= 40
            # A Screen's size is the App's, which changes before the screen is laid out at it.
            await resize_when_settled(pilot, 160, 40)
            await wait_for(lambda: sidebar.display == sidebar_visible and not main._layout_required, pilot=pilot)
            assert sidebar.display == sidebar_visible
            await resize_when_settled(pilot, 80, 40)
            await wait_for(lambda: not sidebar.display and not main._layout_required, pilot=pilot)
            assert not sidebar.display
            await pilot.click("#theme-close")
            await wait_for(lambda: main.theme_editor is None, pilot=pilot)
            assert sidebar.display == sidebar_visible
            restyle.assert_not_called()


class SettingsRegistry(EmptyAgentRegistry):
    def list_profiles(self, *, include_sub_agent_only: bool = True) -> list[object]:
        return []


async def test_settings_theme_is_disabled_during_preview_and_enabled_after_close(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, settings=Settings(theme="chrys-legacy"), agent_registry=SettingsRegistry())
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        await main.open_theme_editor(copy_theme(app.current_theme), UserThemeStore(tmp_path / "themes"))
        await pilot.press("f10")
        dialog = await wait_for_settings_dialog(pilot)
        row = next(row for row in dialog.rows() if row.spec.key == "ui.theme")
        selector = row.query_one(Select)
        assert selector.disabled and "Close the theme editor" in row.hint_text()
        app.locale_controller.switch_locale("zh-Hans")
        assert selector.disabled and "关闭主题编辑器" in row.hint_text()
        selector.value = "dracula"
        await wait_for(lambda: selector.value == "chrys-legacy", pilot=pilot)
        await pilot.press("escape")
        await wait_for_editor(pilot)
        await pilot.click("#theme-close")
        await wait_for(lambda: main.theme_editor is None, pilot=pilot)
        await pilot.press("f10")
        dialog = await wait_for_settings_dialog(pilot)
        selector = next(row for row in dialog.rows() if row.spec.key == "ui.theme").query_one(Select)
        assert not selector.disabled
        with patch("chrys.app.tui.app.persist_theme", autospec=True) as persist:
            selector.value = "dracula"
            await wait_for(lambda: app.theme == app._settings.theme == "dracula", pilot=pilot)
            persist.assert_called_once_with("dracula")


async def test_escape_keeps_chat_suggestions_injections_and_interrupts_working_with_dock(tmp_path: Path) -> None:
    app = make_chrys_app(tmp_path, engine=SessionGenerationEngine())
    cancelled: list[UserInjectCancel] = []

    async def on_cancel(event: UserInjectCancel) -> None:
        cancelled.append(event)

    await app._bus.subscribe(UserInjectCancel, on_cancel)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        main = app.screen
        assert isinstance(main, MainScreen)
        await main.manage_themes()
        panel = main.theme_editor
        assert panel is not None
        await wait_for(lambda: app.focused is panel.editor._color_buttons["primary"], pilot=pilot)
        input_bar = main.query_one(InputBar)
        suggestions = main.query_one(SuggestionList)
        input_bar.focus_input()
        await wait_for(lambda: input_bar.query_one("#chat-input").has_focus, pilot=pilot)
        await pilot.press("/")
        await wait_for(lambda: suggestions.is_visible, pilot=pilot)
        await pilot.press("escape")
        assert not suggestions.is_visible and main.theme_editor is panel
        assert input_bar.query_one("#chat-input", TextArea).text == "/"
        main._set_agent_running(True)
        main._state.pending_injection.begin("queued", "queued text")
        await pilot.press("escape")
        await wait_for(lambda: bool(cancelled), pilot=pilot)
        assert not main._state.pending_injection.active
        assert cancelled[0].injection_id == "queued"
        assert app.screen is main and main.theme_editor is panel
        await pilot.press("escape")
        await wait_for_confirmation(pilot)
        assert str(app.screen.query_one("#confirm-yes", Button).label) == "Interrupt"
        await pilot.press("escape")
        main._set_agent_running(False)
        await pilot.press("escape")
        await wait_for(lambda: main.theme_editor is None, pilot=pilot)
        assert app.screen is main


@pytest.mark.parametrize("missing_source", [False, True])
async def test_preview_end_restores_css_and_classes_when_admission_cannot_restore(
    tmp_path: Path, missing_source: bool
) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        original = copy_theme(app.current_theme)
        if missing_source:
            app.register_user_theme(copy_theme(original, name="removed-user"))
            app.apply_theme_setting("removed-user")
            await pilot.pause()
        chosen = app._settings.theme
        candidate = copy_theme(original, name="draft-light")
        candidate.background = "#FAEEDD"
        candidate.dark = False
        assert app.begin_theme_preview(candidate)
        preview = app.theme_preview
        assert preview is not None
        if missing_source:
            app.unregister_theme(app.theme)
        expected = "chrys" if missing_source else "chrys-legacy"
        with (
            patch.object(preview, "show", autospec=True, return_value=False),
            patch.object(app, "refresh_css", wraps=app.refresh_css) as refresh,
            patch("chrys.app.tui.app.persist_theme", autospec=True) as persist,
        ):
            app.end_theme_preview()
            await wait_for(lambda: refresh.called, pilot=pilot)
            assert app.theme_preview is None and app.current_theme.name == expected
            assert app.has_class(f"-theme-{expected}") and app.has_class("-dark-mode")
            assert not app.has_class("-theme-draft-light") and not app.has_class("-light-mode")
            assert app.screen.styles.background == Color.parse(app.get_css_variables()["background"])
            assert app._settings.theme == chosen
            persist.assert_not_called()
            refresh.assert_called_once()


async def test_picker_mount_is_idempotent_and_confirm_is_the_only_preference_write(tmp_path: Path) -> None:
    app = make_app(tmp_path, "chrys-legacy")
    async with app.run_test(size=(100, 44)) as pilot:
        await pilot.press("f9")
        await wait_for_themes(pilot)
        picker = app.screen
        count = picker.query_one(OptionList).option_count
        picker.on_mount()
        assert app._theme_selection_depth == 1
        assert picker.query_one(OptionList).option_count == count
        with patch("chrys.app.tui.app.persist_theme", autospec=True) as persist:
            picker.query_one(OptionList).highlighted = next(
                i for i, option in enumerate(picker.query_one(OptionList).options) if option.id == "dracula"
            )
            await wait_for(lambda: app.theme == "dracula", pilot=pilot)
            persist.assert_not_called()
            await pilot.press("enter")
            await wait_for(lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted, pilot=pilot)
            assert app._theme_selection_depth == 0
            assert app._settings.theme == "dracula"
            persist.assert_called_once_with("dracula")
