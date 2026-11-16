# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Production-app harness for theme editing without touching user files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from textual.message import Message
from textual.notifications import Notification, Notify
from textual.pilot import Pilot
from textual.screen import Screen
from textual.theme import Theme
from textual.widget import Widget
from textual.widgets import Button, OptionList, TabbedContent

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main import MainScreen
from chrys.app.tui.screens.settings.dialog import SettingsDialog
from chrys.app.tui.screens.themes.dialogs import _PickerModal
from chrys.app.tui.screens.themes.editor import ResettableThemeEditor
from chrys.app.tui.screens.themes.panel import ThemeEditorPanel
from chrys.app.tui.screens.themes.picker import ThemesScreen
from chrys.app.tui.screens.themes.save import SaveThemeDialog
from chrys.app.tui.themes.document import copy_theme
from chrys.app.tui.themes.store import ThemeFileRevision, UserThemeStore
from chrys.app.tui.widgets.select import Select
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.service.state.store import JsonFileStateStore
from tests.support.paths import SRC_ROOT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import EmptyAgentRegistry, ShutdownOnlyEngine
from tests.support.waiting import wait_for


@dataclass
class NotificationCapture:
    """Retain delivered notifications after their transient toast widgets expire."""

    expire_on_delivery: bool = False
    notifications: list[Notification] = field(default_factory=list)

    def __call__(self, message: Message) -> None:
        if isinstance(message, Notify):
            notification = message.notification
            self.notifications.append(notification)
            if self.expire_on_delivery:
                # Model a loaded worker delivering the message after its toast
                # deadline, without sleeping or bypassing notification dispatch.
                notification.raised_at -= notification.timeout


class _EditorScreen(Screen):
    """Component host: no chat, settings, shell, sidebar or main-screen bindings.

    Dialog tests still exercise the real panel, editor, preview, CSS, storage
    and localization. MainScreen lifecycle/layout tests must use make_app.
    GC freeze stays disabled: its transcript hooks belong to MainScreen.
    """

    def sync_footer_bindings(self) -> None:
        """AppFocus can arrive in headless tests; this host has no footer."""

    async def open_theme_editor(
        self, theme: Theme, store: UserThemeStore, revision: ThemeFileRevision | None = None
    ) -> None:
        await self.mount(ThemeEditorPanel(theme, store, revision))

    def on_screen_resume(self) -> None:
        for panel in self.query(ThemeEditorPanel):
            panel.resume()


class _EditorApp(ChrysApp):
    # Textual resolves inherited relative paths against the subclass module.
    CSS_PATH: ClassVar[list[Path]] = [SRC_ROOT / "chrys" / "app" / "tui" / path for path in ChrysApp.CSS_PATH]

    def _build_main_screen(self) -> _EditorScreen:
        return _EditorScreen()


def make_app(tmp_path: Path, name: str | Theme = "chrys", locale: str = "en", *, component: bool = False) -> ChrysApp:
    """Seed a detached theme before startup when a test needs specific input data."""
    app_type = _EditorApp if component else ChrysApp
    app = app_type(
        EventBus(),
        ShutdownOnlyEngine(),
        settings=Settings(theme=name.name if isinstance(name, Theme) else name, locale=locale),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=EmptyAgentRegistry(),
        gc_freeze_enabled=False,
    )  # type: ignore[arg-type]
    if isinstance(name, Theme):
        app.register_theme(copy_theme(name))
    return app


async def open_editor(app: ChrysApp, pilot: Pilot, tmp_path: Path, *, new: bool = False) -> ResettableThemeEditor:
    """Open the displayed theme; opt into an unsaved copy only when a test needs one."""
    await pilot.pause()
    source = app.get_theme(app.theme)
    assert source is not None
    assert isinstance(app.screen, (MainScreen, _EditorScreen))
    await app.screen.open_theme_editor(
        copy_theme(source, name=f"{source.name}-copy") if new else source, UserThemeStore(tmp_path / "themes")
    )
    await wait_for(lambda: bool(app.screen.query(ResettableThemeEditor)), pilot=pilot)
    editor = app.screen.query_one(ResettableThemeEditor)
    await wait_for(
        lambda: (
            editor.region.width > 0
            and app.theme_preview is not None
            and app.focused is editor._color_buttons["primary"]
            and screen_is_settled(app, app.screen)
        ),
        pilot=pilot,
    )
    return editor


async def wait_for_themes(pilot: Pilot) -> ThemesScreen:
    """Wait for populated choices, layout and focus before browsing or pressing Enter."""

    def ready() -> bool:
        screen = pilot.app.screen
        if not isinstance(screen, ThemesScreen) or not screen.is_mounted or not screen_is_settled(pilot.app, screen):
            return False
        options = screen.query_one(OptionList)
        return (
            options.is_mounted
            and options.option_count > 0
            and options.highlighted is not None
            and options.has_focus
            and bool(options.content_region)
        )

    await wait_for(ready, pilot=pilot, description="theme choices are populated, laid out and focused")
    screen = pilot.app.screen
    assert isinstance(screen, ThemesScreen)
    return screen


async def wait_for_settings_dialog(pilot: Pilot) -> SettingsDialog:
    """Wait for rows and tab activation, including deferred initial control focus."""

    def ready() -> bool:
        dialog = pilot.app.screen
        if not isinstance(dialog, SettingsDialog) or not dialog.is_mounted or not screen_is_settled(pilot.app, dialog):
            return False
        tabs = dialog.query_one(TabbedContent)
        rows = dialog.rows()
        if not tabs.is_mounted or not tabs.active or not rows or not all(row.is_mounted for row in rows):
            return False
        pane = tabs.active_pane
        return pane is not None and bool(pane.content_region) and pane.has_focus_within

    await wait_for(ready, pilot=pilot, description="settings rows and active pane have mounted, laid out and focused")
    dialog = pilot.app.screen
    assert isinstance(dialog, SettingsDialog)
    return dialog


async def wait_for_picker(pilot: Pilot, kind: type[Widget]) -> _PickerModal:
    """Wait for replacement controls, deferred focus and the resulting modal layout.

    Query visibility precedes Mount and layout; clicking at that point can
    use the old mode's button coordinates or race Tabs' initial activation.
    The screen's ``focused`` moves before the widget's own ``has_focus``,
    which its message pump sets on the Focus event; wait for the flag the
    tests assert.
    """

    def ready() -> bool:
        modal = pilot.app.screen
        if not isinstance(modal, _PickerModal) or not modal.is_mounted or not screen_is_settled(pilot.app, modal):
            return False
        pickers = modal.query(kind)
        if not pickers:
            return False
        picker = pickers.first()
        focused = modal.focused
        return (
            picker.is_mounted
            and focused is not None
            and focused.has_focus
            and picker in focused.ancestors_with_self
            and modal.can_view_entire(modal.query_one("#picker-confirm", Button))
        )

    await wait_for(ready, pilot=pilot, description=f"{kind.__name__} is focused and its dialog is laid out")
    modal = pilot.app.screen
    assert isinstance(modal, _PickerModal)
    return modal


async def wait_for_save_dialog(pilot: Pilot) -> SaveThemeDialog:
    """Do not type or click until the new screen has mounted and painted its buttons."""

    def ready() -> bool:
        dialog = pilot.app.screen
        if not isinstance(dialog, SaveThemeDialog) or not dialog.is_mounted or not screen_is_settled(pilot.app, dialog):
            return False
        button = dialog.query_one("#save-theme-confirm", Button)
        return button.is_mounted and dialog.can_view_entire(button)

    await wait_for(ready, pilot=pilot, description="save dialog buttons have their final visible geometry")
    dialog = pilot.app.screen
    assert isinstance(dialog, SaveThemeDialog)
    return dialog


async def wait_for_confirmation(pilot: Pilot) -> ConfirmDialog:
    """Wait past nested button mounting and deferred autofocus before clicking.

    A screen can be on the stack while its buttons still have empty regions.
    Checking its type alone races the first layout on slower CI workers.
    """

    def ready() -> bool:
        dialog = pilot.app.screen
        if not isinstance(dialog, ConfirmDialog) or not dialog.is_mounted or not screen_is_settled(pilot.app, dialog):
            return False
        buttons = list(dialog.query(Button))
        return (
            bool(buttons)
            and all(button.is_mounted and bool(button.region) and dialog.can_view_entire(button) for button in buttons)
            and dialog.query_one("#confirm-yes", Button).has_focus
        )

    await wait_for(ready, pilot=pilot, description="confirmation buttons are laid out and autofocus has completed")
    dialog = pilot.app.screen
    assert isinstance(dialog, ConfirmDialog)
    return dialog


async def press_button(pilot: Pilot, target: Button | str) -> None:
    """Dispatch a component action through Textual's real Button.Pressed route.

    Storage/transaction tests don't need mouse hit-testing or three idle waits
    per action. Keep Pilot.click in mouse, focus, layout and dock integration
    tests. Callers still wait for the resulting state or dialog readiness;
    this queue barrier alone doesn't guarantee completion of deferred work.
    """
    button = pilot.app.screen.query_one(target, Button) if isinstance(target, str) else target
    assert button.screen is pilot.app.screen
    assert button.is_mounted and button.display and not button.disabled
    button.press()
    await pilot.pause(0)


async def wait_for_editor(pilot: Pilot) -> ResettableThemeEditor:
    """Wait for ScreenResume and the selector/button reflow after a dialog closes.

    The app's screen stack changes before ScreenResume reaches MainScreen.
    That handler refreshes the selector and may reveal the Delete button,
    moving Close; testing only app.screen or the saved document races both.
    """
    await wait_for(
        lambda: isinstance(pilot.app.screen, (MainScreen, _EditorScreen)) and pilot.app.screen.is_mounted, pilot=pilot
    )
    # Pop posts ScreenResume synchronously; drain the restored screen's queue
    # before inspecting geometry that could still be left over from suspension.
    await pilot.pause()

    def ready() -> bool:
        main = pilot.app.screen
        if not isinstance(main, (MainScreen, _EditorScreen)) or not main.is_mounted or main._layout_required:
            return False
        panels = main.query(ThemeEditorPanel)
        if not panels:
            return False
        panel = panels.first()
        if not panel.is_mounted:
            return False
        editor = panel.editor
        if not editor.is_mounted or editor._switching or editor._recompose_pending:
            return False
        return editor.query_one(Select).value == editor.document.draft.name and main.can_view_entire(
            editor.query_one("#theme-close")
        )

    await wait_for(ready, pilot=pilot, description="editor has resumed with current selector and button geometry")
    return pilot.app.screen.query_one(ResettableThemeEditor)
