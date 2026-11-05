# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ClickOutsideDismissMixin and its application to modal screens."""

from __future__ import annotations

from time import monotonic

import pytest
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.events import MouseDown
from textual.geometry import Offset, Region
from textual.screen import ModalScreen
from textual.selection import SELECT_ALL
from textual.widgets import OptionList, Static

from chrys.app.tui.behaviors.click_outside_dismiss import ClickOutsideDismissMixin
from chrys.app.tui.screens.dialogs.base import BaseDialog
from tests.app.tui.screens.themes.helpers import wait_for_themes


class _ResultDialog(ClickOutsideDismissMixin, ModalScreen[str | None]):
    """Centered dialog so screen corner (0, 0) is always backdrop."""

    DEFAULT_CSS = """
    _ResultDialog { align: center middle; }
    _ResultDialog > #box { width: 20; height: 5; background: $surface; }
    """

    def __init__(self, *, result: str | None = "picked", allow: bool = True) -> None:
        super().__init__()
        self._result = result
        self._allow = allow

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("hi", id="label")

    def _default_dismiss_result(self):  # type: ignore[override]
        return self._result

    def _allow_click_outside_dismiss(self) -> bool:
        return self._allow


class _BaseResultDialog(BaseDialog[str | None]):
    """BaseDialog-backed modal for hook/flag behavior."""

    DEFAULT_CSS = """
    _BaseResultDialog { align: center middle; }
    _BaseResultDialog > #box { width: 20; height: 5; background: $surface; }
    """

    def __init__(self, *, result: str | None = "picked", dismiss_on_backdrop: bool = True) -> None:
        self._result = result
        self.before_dismiss_results: list[object | None] = []
        super().__init__(dismiss_on_backdrop=dismiss_on_backdrop)

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            yield Static("hi", id="label")

    def _default_dismiss_result(self):  # type: ignore[override]
        return self._result

    def _before_dismiss(self, result: object | None = None) -> None:
        self.before_dismiss_results.append(result)


class _Harness(App):
    def __init__(self, screen: ModalScreen) -> None:
        super().__init__()
        self._screen = screen
        self.dismissed = False
        self.result: object = "UNSET"

    def on_mount(self) -> None:
        self.push_screen(self._screen, self._done)

    def _done(self, value: object) -> None:
        self.dismissed = True
        self.result = value


@pytest.mark.asyncio
async def test_repeated_dismiss_on_mixin_modal_completes_once() -> None:
    dialog = _ResultDialog(result="picked")
    async with _Harness(dialog).run_test() as pilot:
        await pilot.pause()

        dialog.dismiss("picked")
        dialog.dismiss("late")
        await pilot.pause()

        assert pilot.app.dismissed
        assert pilot.app.result == "picked"
        assert not isinstance(pilot.app.screen, _ResultDialog)


@pytest.mark.asyncio
async def test_backdrop_click_dismisses_with_default_result() -> None:
    async with _Harness(_ResultDialog(result="picked")).run_test() as pilot:
        await pilot.pause()
        assert isinstance(pilot.app.screen, _ResultDialog)

        await pilot.click(offset=(0, 0))
        await pilot.pause()

        assert pilot.app.dismissed
        assert pilot.app.result == "picked"
        assert not isinstance(pilot.app.screen, _ResultDialog)


@pytest.mark.asyncio
async def test_click_on_child_widget_does_not_dismiss() -> None:
    async with _Harness(_ResultDialog()).run_test() as pilot:
        await pilot.pause()

        await pilot.click("#label")
        await pilot.pause()

        assert not pilot.app.dismissed
        assert isinstance(pilot.app.screen, _ResultDialog)


@pytest.mark.asyncio
async def test_right_click_on_backdrop_does_not_dismiss() -> None:
    async with _Harness(_ResultDialog()).run_test() as pilot:
        await pilot.pause()

        await pilot.click(offset=(0, 0), button=3)
        await pilot.pause()

        assert not pilot.app.dismissed
        assert isinstance(pilot.app.screen, _ResultDialog)


@pytest.mark.asyncio
async def test_allow_hook_gates_backdrop_dismiss() -> None:
    dialog = _ResultDialog(allow=False)
    async with _Harness(dialog).run_test() as pilot:
        await pilot.pause()

        # Suppressed while the gate is closed.
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert not pilot.app.dismissed
        assert isinstance(pilot.app.screen, _ResultDialog)

        # Opening the gate restores backdrop-dismiss.
        dialog._allow = True
        await pilot.click(offset=(0, 0))
        await pilot.pause()
        assert pilot.app.dismissed
        assert not isinstance(pilot.app.screen, _ResultDialog)


@pytest.mark.asyncio
async def test_base_dialog_backdrop_flag_blocks_click_dismiss() -> None:
    dialog = _BaseResultDialog(dismiss_on_backdrop=False)
    async with _Harness(dialog).run_test() as pilot:
        await pilot.pause()

        await pilot.click(offset=(0, 0))
        await pilot.pause()

        assert not pilot.app.dismissed
        assert isinstance(pilot.app.screen, _BaseResultDialog)
        assert dialog.before_dismiss_results == []


@pytest.mark.asyncio
async def test_base_dialog_before_dismiss_runs_once() -> None:
    dialog = _BaseResultDialog()
    async with _Harness(dialog).run_test() as pilot:
        await pilot.pause()

        dialog.dismiss("picked")
        dialog.dismiss("late")
        await pilot.pause()

        assert pilot.app.dismissed
        assert pilot.app.result == "picked"
        assert dialog.before_dismiss_results == ["picked"]


@pytest.mark.asyncio
async def test_base_dialog_right_click_copies_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

    dialog = _BaseResultDialog()
    async with _Harness(dialog).run_test() as pilot:
        await pilot.pause()
        label = dialog.query_one("#label", Static)
        dialog.selections = {label: SELECT_ALL}

        click_x = label.region.x
        click_y = label.region.y
        pilot.app.post_message(
            MouseDown(
                label,
                x=click_x,
                y=click_y,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=click_x,
                screen_y=click_y,
            )
        )
        await pilot.pause()

        assert pilot.app.clipboard == "hi"
        assert copied == ["hi"]
        assert dialog.selections == {}
        assert not pilot.app.dismissed


@pytest.mark.asyncio
async def test_base_dialog_discards_detached_selection_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    dialog = _BaseResultDialog()
    async with _Harness(dialog).run_test() as pilot:
        await pilot.pause()
        label = dialog.query_one("#label", Static)
        await label.remove()
        await pilot.pause()

        dialog.selections = {label: SELECT_ALL}
        dialog._mouse_down_offset = Offset(1, 1)
        dialog._selecting = True
        pilot.app._mouse_down_widget = label

        def stale_content_hit(_x: int, _y: int) -> tuple[Static | None, Offset | None]:
            return label, Offset(0, 0)

        def stale_hit(_x: int, _y: int) -> tuple[Static, Region]:
            return label, Region(1, 1, 10, 1)

        # The layout before the removal answers both, as the screen's one map does.
        monkeypatch.setattr(dialog, "get_widget_and_offset_at", stale_content_hit)
        monkeypatch.setattr(dialog, "get_widget_at", stale_hit)

        dialog._forward_event(
            MouseDown(
                label,
                x=1,
                y=1,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=1,
                screen_y=1,
            )
        )

        assert dialog.selections == {}
        assert dialog._mouse_down_offset is None
        assert dialog._selecting is False
        assert pilot.app._mouse_down_widget is None


@pytest.mark.asyncio
async def test_themes_backdrop_click_restores_previewed_theme() -> None:
    from chrys.app.tui.screens.themes import ThemesScreen

    class _ThemeHarness(App):
        def __init__(self) -> None:
            super().__init__()
            self.original = ""
            self.result: object = "UNSET"

        def on_mount(self) -> None:
            self.original = self.theme
            self.push_screen(ThemesScreen(self.original), self._done)

        def _done(self, value: object) -> None:
            self.result = value

    async with _ThemeHarness().run_test() as pilot:
        await wait_for_themes(pilot)
        app = pilot.app
        original = app.original

        # Simulate a live preview drift to a different theme.
        other = next(t for t in app.available_themes if t != original)
        app.theme = other
        assert app.theme != original

        # Backdrop click is a cancel: it must restore the original theme.
        await pilot.click(offset=(0, 0))
        await pilot.pause()

        assert app.theme == original
        assert app.result is None
        assert not isinstance(app.screen, ThemesScreen)


@pytest.mark.asyncio
async def test_themes_late_cancel_does_not_restore_after_apply() -> None:
    from chrys.app.tui.screens.themes import ThemesScreen

    class _ThemeHarness(App):
        def __init__(self) -> None:
            super().__init__()
            self.screen_obj: ThemesScreen | None = None
            self.original = ""
            self.result: object = "UNSET"

        def on_mount(self) -> None:
            self.original = self.theme
            self.screen_obj = ThemesScreen(self.original)
            self.push_screen(self.screen_obj, self._done)

        def _done(self, value: object) -> None:
            self.result = value

    async with _ThemeHarness().run_test() as pilot:
        await wait_for_themes(pilot)
        app = pilot.app
        screen = app.screen_obj
        assert screen is not None
        selected = next(t for t in app.available_themes if t != app.original)
        app.theme = selected
        option_list = screen.query_one(OptionList)
        option = option_list.get_option(selected)
        option_index = option_list.get_option_index(selected)

        screen._last_selected = selected
        screen._last_selected_time = monotonic()
        screen._on_selected(OptionList.OptionSelected(option_list, option, option_index))
        screen.action_cancel()
        await pilot.pause()

        assert app.theme == selected
        assert app.result == selected


def _escape_action(cls: type) -> str | None:
    """Return the action bound to Escape on a screen, if any."""
    for binding in getattr(cls, "BINDINGS", []):
        key = getattr(binding, "key", None)
        action = getattr(binding, "action", None)
        if key is None and isinstance(binding, tuple):
            key = binding[0]
            action = binding[1] if len(binding) > 1 else None
        if key == "escape":
            return action
    return None


def test_unescapable_modals_excluded_from_click_dismiss() -> None:
    """Modals that disable Escape must not be backdrop-dismissable."""
    from chrys.app.tui.screens.dialogs.approval import ApprovalDialog
    from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog
    from chrys.app.tui.screens.dialogs.image_compression import ImageCompressionDialog

    # Each binds Escape to a no-op action — confirming they are un-escapable.
    no_op_actions = {"noop", "ignore_escape"}
    for cls in (ApprovalDialog, AskUserDialog, ImageCompressionDialog):
        assert _escape_action(cls) in no_op_actions, cls.__name__
        assert not issubclass(cls, ClickOutsideDismissMixin), cls.__name__


def test_escapable_modals_have_click_dismiss() -> None:
    """Representative escapable modals should be backdrop-dismissable."""
    from chrys.app.tui.screens.agents.config import AgentsConfigScreen
    from chrys.app.tui.screens.agents.picker import AgentsScreen
    from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen
    from chrys.app.tui.screens.dialogs.base import BaseDialog
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
    from chrys.app.tui.screens.dialogs.file_picker import FilePicker
    from chrys.app.tui.screens.dialogs.man_page import ManPageDialog
    from chrys.app.tui.screens.dialogs.runtime_details import RuntimeDetailsDialog
    from chrys.app.tui.screens.dialogs.tool_view import ToolDetailModal
    from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
    from chrys.app.tui.screens.logs.screen import LogViewerScreen
    from chrys.app.tui.screens.models.screen import ModelConfigScreen
    from chrys.app.tui.screens.sessions.screen import SessionsScreen
    from chrys.app.tui.screens.settings.dialog import SettingsDialog
    from chrys.app.tui.screens.settings.migrate_dialog import MigrateSessionsDialog
    from chrys.app.tui.screens.themes import ThemesScreen

    for cls in (
        AgentsConfigScreen,
        AgentsScreen,
        ApprovalModeScreen,
        ConfirmDialog,
        FilePicker,
        LogViewerScreen,
        ManPageDialog,
        MigrateSessionsDialog,
        ModelConfigScreen,
        RuntimeDetailsDialog,
        RollbackModal,
        SessionsScreen,
        SettingsDialog,
        ThemesScreen,
        ToolDetailModal,
    ):
        assert issubclass(cls, BaseDialog), cls.__name__
        assert issubclass(cls, ClickOutsideDismissMixin), cls.__name__
