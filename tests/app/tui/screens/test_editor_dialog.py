# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pilot tests for the Standard-mode message editor dialog."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.color import Color
from textual.content import Content
from textual.css.query import NoMatches
from textual.events import Paste
from textual.widgets import Button, Select, Static, TextArea

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.editor import EditorDialog, EditorDialogResult
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.support.gc_freeze import (
    GcAbsorbReason,
    GcAbsorbRequested,
    GcFreezeBlockReason,
    GcReclaimReason,
    GcReclaimRequested,
)
from chrys.app.tui.theme import CHRYS_ANSI_THEME
from chrys.app.tui.util.git_branch import GitBranchMonitor
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import ChatDraft, InputBar, InputDraftSnapshot
from chrys.app.tui.widgets.editor import (
    MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS,
    MESSAGE_EDITOR_MAX_CHARACTERS,
    EditorBufferSnapshot,
    EditorMode,
    EmacsKeymap,
    MessageEditor,
    VimKeymap,
    VimState,
)
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.service.state.store import JsonFileStateStore
from tests.support.paths import SRC_ROOT
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import ShutdownOnlyEngine
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.asyncio

_UI_WAIT_TIMEOUT_SECONDS = 10.0


@pytest.fixture(autouse=True)
def _prevent_editor_preference_disk_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.app.tui.screens.main.screen.persist_editor_keymap", lambda _mode: None)
    monkeypatch.setattr(GitBranchMonitor, "_start_observer", lambda _monitor, _target: None)


class _DialogApp(App[None]):
    pass


class _LocalizedDialogApp(App[None]):
    def __init__(self, locale: str) -> None:
        self.locale_controller = LocaleController(Settings(locale=locale))
        super().__init__()


class _MainScreenEditorApp(App[None]):
    def __init__(self, on_editor_keymap_changed: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self.main_screen = MainScreen(
            EventBus(),
            engine_provider=None,
            on_editor_keymap_changed=on_editor_keymap_changed,
        )
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield from ()

    async def on_mount(self) -> None:
        await self.push_screen(self.main_screen)

    def on_input_bar_user_submitted(self, event: InputBar.UserSubmitted) -> None:
        self.submitted.append(event.text)


def _hint_text(dialog: EditorDialog) -> str:
    return str(dialog.query_one("#editor-hint", Static).render())


def _status_text(dialog: EditorDialog) -> str:
    return str(dialog.query_one("#editor-status", Static).render())


async def _wait_for_condition(
    predicate,
    pilot,
    *,
    description: str,
    timeout: float = _UI_WAIT_TIMEOUT_SECONDS,
    stable_observations: int = 1,
) -> None:
    """Observe every completed UI pump before checking the shared wait deadline."""
    consecutive_matches = 0

    def condition() -> bool:
        nonlocal consecutive_matches
        try:
            if predicate():
                consecutive_matches += 1
            else:
                consecutive_matches = 0
        except NoMatches:
            consecutive_matches = 0
        return consecutive_matches >= stable_observations

    await wait_for(condition, pilot=pilot, timeout=timeout, description=description)


async def _wait_for_editor_dialog_ready(app: App, pilot, *, description: str) -> None:
    """Wait until the pushed EditorDialog is laid out, not merely current.

    ``app.screen`` flips to the dialog before its first compositor reflow; a
    ``pilot.click`` issued in that window resolves against a null screen
    region and raises ``OutOfBounds`` on slow CI workers.
    """
    await _wait_for_condition(
        lambda: (
            isinstance(app.screen, EditorDialog)
            and app.screen.is_mounted
            and screen_is_settled(app, app.screen)
            and app.screen.query_one("#editor-cancel", Button).region.width > 0
        ),
        pilot,
        description=description,
    )


@pytest.mark.parametrize("ready_after_pump", [True, False])
async def test_editor_wait_checks_final_observation_after_slow_ui_pump(
    monkeypatch: pytest.MonkeyPatch, ready_after_pump: bool
) -> None:
    now = 0.0
    ready = False

    class SlowPilot:
        async def pause(self, _delay: float | None = None) -> None:
            nonlocal now, ready
            # Model a Windows UI pump which completes after the wait deadline.
            # No real suspension: restore the clock before yielding to the loop.
            now = _UI_WAIT_TIMEOUT_SECONDS + 1
            ready = ready_after_pump

    with monkeypatch.context() as clock:
        clock.setattr(time, "monotonic", lambda: now)
        if ready_after_pump:
            await _wait_for_condition(lambda: ready, SlowPilot(), description="editor focus after slow pump")
        else:
            with pytest.raises(AssertionError, match="editor focus after slow pump"):
                await _wait_for_condition(lambda: ready, SlowPilot(), description="editor focus after slow pump")


async def test_dialog_mounts_focused_editor_with_entry_cursor_and_hint() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("first\nsecond", (1, 3)))

    async with app.run_test(size=(120, 30)) as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()

        editor = dialog.query_one(MessageEditor)
        assert editor.has_focus
        assert editor.text == "first\nsecond"
        assert editor.cursor_location == (1, 3)
        assert editor.mode is EditorMode.STANDARD
        assert _hint_text(dialog) == " Ctrl+O  Ctrl+Enter  Commit  Esc  Discard  F3  Mode"
        assert _status_text(dialog) == "Ready"
        assert results == []


async def test_dialog_renders_semantic_status_and_preserves_plain_legacy_status() -> None:
    app = _LocalizedDialogApp("zh-Hans")
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)))

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await pilot.pause()

        reference = MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS.bind()
        dialog._refresh_status(reference, warning=True)
        assert dialog._editor_status is reference
        assert _status_text(dialog) == "已达到字符限制 · 最大 500,000"

        dialog._refresh_status("Legacy [status]", warning=False)
        assert dialog._editor_status == "Legacy [status]"
        assert _status_text(dialog) == "Legacy [status]"


async def test_dialog_mount_normalizes_entry_cursor_for_vim_normal_mode() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="Vim editor focus",
        )

        assert dialog.query_one(MessageEditor).cursor_location == (0, 4)


@pytest.mark.parametrize(
    ("theme", "selection_background_is_lighter"),
    [("textual-dark", True), ("textual-light", False)],
)
async def test_editor_selection_contrast_tracks_dark_and_light_themes(
    theme: str,
    selection_background_is_lighter: bool,
) -> None:
    app = _DialogApp()
    app.theme = theme
    dialog = EditorDialog(EditorBufferSnapshot("selected text", (0, 0)), mode=EditorMode.EMACS)

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description=f"{theme} editor focus",
        )

        editor = dialog.query_one(MessageEditor)
        selection_style = editor.get_component_rich_style("text-area--selection")
        cursor_line_style = editor.get_component_rich_style("text-area--cursor-line")
        assert selection_style.color is not None
        assert selection_style.bgcolor is not None
        assert selection_style.color.triplet is not None
        assert selection_style.bgcolor.triplet is not None
        foreground_brightness = sum(selection_style.color.triplet)
        background_brightness = sum(selection_style.bgcolor.triplet)
        assert (background_brightness > foreground_brightness) is selection_background_is_lighter
        assert selection_style.bgcolor != cursor_line_style.bgcolor
        assert not selection_style.reverse


@pytest.mark.parametrize(
    ("theme", "editor_background_is_lighter"),
    [("textual-dark", True), ("textual-light", False)],
)
async def test_editor_surface_contrast_tracks_dark_and_light_themes(
    theme: str,
    editor_background_is_lighter: bool,
) -> None:
    app = _DialogApp()
    app.theme = theme
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)))

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description=f"{theme} editor focus",
        )

        editor_background = dialog.query_one(MessageEditor).rich_style.bgcolor
        dialog_background = dialog.query_one("#editor-dialog").rich_style.bgcolor
        assert editor_background is not None
        assert dialog_background is not None
        assert editor_background.triplet is not None
        assert dialog_background.triplet is not None
        editor_brightness = sum(editor_background.triplet)
        dialog_brightness = sum(dialog_background.triplet)
        assert (editor_brightness > dialog_brightness) is editor_background_is_lighter


@pytest.mark.parametrize(
    ("theme", "text_color_number", "background_color_number"),
    [
        ("ansi-dark", 0, 7),
        ("ansi-light", 7, 0),
        ("chrys-ansi", 0, 7),
    ],
)
async def test_editor_selection_uses_explicit_contrast_in_ansi_themes(
    theme: str,
    text_color_number: int,
    background_color_number: int,
) -> None:
    app = _DialogApp()
    if theme == "chrys-ansi":
        app.register_theme(CHRYS_ANSI_THEME)
    app.theme = theme
    dialog = EditorDialog(EditorBufferSnapshot("selected text", (0, 0)), mode=EditorMode.EMACS)

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description=f"{theme} editor focus",
        )

        selection_style = dialog.query_one(MessageEditor).get_component_rich_style("text-area--selection")
        assert selection_style.color is not None
        assert selection_style.bgcolor is not None
        assert selection_style.color.number == text_color_number
        assert selection_style.bgcolor.number == background_color_number
        assert not selection_style.reverse


@pytest.mark.parametrize(
    ("mode", "keys", "expected_hint", "expected_status"),
    [
        (EditorMode.STANDARD, (), " Ctrl+O  Ctrl+Enter  Commit  Esc  Discard  F3  Mode", "Ready"),
        (
            EditorMode.EMACS,
            (),
            " Ctrl+O  Commit  Esc  Discard  Ctrl+Space  Select  F3  Mode",
            "Ready",
        ),
        (
            EditorMode.EMACS,
            ("ctrl+@",),
            " Ctrl+O  Commit  Esc  Discard  Ctrl+Space  Select  F3  Mode",
            "Selection active",
        ),
        (
            EditorMode.VIM,
            (),
            " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard",
            "Normal",
        ),
        (
            EditorMode.VIM,
            ("i",),
            " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard",
            "Insert",
        ),
        (
            EditorMode.VIM,
            ("v",),
            " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard",
            "Visual",
        ),
        (
            EditorMode.VIM,
            ("V",),
            " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard",
            "Visual Line",
        ),
        (
            EditorMode.VIM,
            ("3", "d"),
            " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard",
            "Normal · Pending: 3d",
        ),
        (
            EditorMode.VIM,
            (":", "w"),
            " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard",
            "Command :w",
        ),
    ],
)
async def test_mode_hint_stays_fixed_while_live_status_changes(
    mode: EditorMode,
    keys: tuple[str, ...],
    expected_hint: str,
    expected_status: str,
) -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("one two", (0, 0)), mode=mode)

    async with app.run_test(size=(100, 26)) as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="hint test editor focus",
        )
        if keys:
            await pilot.press(*keys)
        await _wait_for_condition(
            lambda: _hint_text(dialog) == expected_hint and _status_text(dialog) == expected_status,
            pilot,
            description=f"fixed hint {expected_hint!r} and status {expected_status!r}",
        )

        rendered = dialog.query_one("#editor-hint", Static).render()
        assert isinstance(rendered, Content)
        assert str(rendered) == expected_hint
        assert any(span.style.reverse for span in rendered.spans)
        if mode is EditorMode.VIM:
            reverse_keycaps = [rendered.plain[span.start : span.end] for span in rendered.spans if span.style.reverse]
            assert " i " in reverse_keycaps
            assert " v " in reverse_keycaps
            assert " : " in reverse_keycaps


@pytest.mark.parametrize("save_key", ["ctrl+o", "ctrl+enter"])
async def test_keyboard_save_returns_edited_text_without_submitting(save_key: str) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("enter", *"more")
        await pilot.press(save_key)
        await pilot.pause()

        assert results == [
            EditorDialogResult(
                accepted=True,
                text="draft\nmore",
                mode=EditorMode.STANDARD,
            )
        ]


async def test_save_is_immediate_and_discard_requires_confirmation() -> None:
    app = _DialogApp()

    async with app.run_test() as pilot:
        saved: list[EditorDialogResult] = []
        save_dialog = EditorDialog(EditorBufferSnapshot("", (0, 0)))
        app.push_screen(save_dialog, saved.append)
        await pilot.pause()
        await pilot.press(*"saved")
        await pilot.click("#editor-commit")
        await pilot.pause()
        assert saved[0].accepted is True
        assert saved[0].text == "saved"

        cancelled: list[EditorDialogResult] = []
        cancel_dialog = EditorDialog(EditorBufferSnapshot("original", (0, 8)))
        app.push_screen(cancel_dialog, cancelled.append)
        await pilot.pause()
        await pilot.press(*" dirty")
        await click_when_settled(pilot, "#editor-cancel")
        assert isinstance(app.screen, ConfirmDialog)
        assert cancelled == []
        await click_when_settled(pilot, "#confirm-yes")
        await pilot.pause()
        assert cancelled[0].accepted is False
        assert cancelled[0].text == "original dirty"


@pytest.mark.parametrize("mode", list(EditorMode))
@pytest.mark.parametrize("text", ["draft", " \n"])
async def test_discard_confirms_unchanged_nonempty_text(text: str, mode: EditorMode) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot(text, (0, 0)), mode=mode)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await click_when_settled(pilot, "#editor-cancel")

        confirmation = app.screen
        assert isinstance(confirmation, ConfirmDialog)
        assert results == []
        assert dialog.query_one(MessageEditor).text == text
        assert confirmation.query_one("#confirm-yes", Button).variant == "error"
        dialog.request_discard()
        assert sum(isinstance(screen, ConfirmDialog) for screen in app.screen_stack) == 1

        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: bool(results), pilot=pilot, description="confirmed editor discard")
        assert results == [EditorDialogResult(accepted=False, text=text, mode=mode)]


@pytest.mark.parametrize("cancel_action", ["button", "escape", "backdrop"])
async def test_cancel_discard_preserves_editor_text_cursor_and_undo(cancel_action: str) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="editor mounted before editing")
        editor = dialog.query_one(MessageEditor)
        await wait_for(lambda: editor.has_focus, pilot=pilot, description="editor focused before editing")
        await pilot.press("x", "escape")
        assert dialog.discard_armed is True
        cursor = editor.cursor_location
        await click_when_settled(pilot, "#editor-cancel")
        assert isinstance(app.screen, ConfirmDialog)
        if cancel_action == "button":
            await click_when_settled(pilot, "#confirm-no")
        elif cancel_action == "escape":
            await pilot.press("escape")
        else:
            await pilot.click(offset=(0, 0))
        await wait_for(
            lambda: app.screen is dialog and editor.has_focus,
            pilot=pilot,
            description="cancelled discard returns to editor",
        )

        assert results == []
        assert editor.text == "draftx"
        assert editor.cursor_location == cursor
        assert dialog.discard_armed is False
        await pilot.press("ctrl+z")
        assert editor.text == "draft"
        await pilot.press("ctrl+o")
        await wait_for(lambda: bool(results), pilot=pilot, description="commit after cancelling discard")
        assert results == [EditorDialogResult(accepted=True, text="draft", mode=EditorMode.STANDARD)]


@pytest.mark.parametrize("initial_text", ["", "draft"])
async def test_discard_empty_editor_closes_without_confirmation(initial_text: str) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot(initial_text, (0, 0)))

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="empty editor mounted")
        dialog.query_one(MessageEditor).clear()
        await click_when_settled(pilot, "#editor-cancel")
        await wait_for(lambda: bool(results), pilot=pilot, description="empty editor discarded")
        assert not any(isinstance(screen, ConfirmDialog) for screen in app.screen_stack)
        assert results == [EditorDialogResult(accepted=False, text="", mode=EditorMode.STANDARD)]


async def test_discard_confirmation_uses_and_refreshes_app_locale() -> None:
    app = _LocalizedDialogApp("zh-Hans")
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)))

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await click_when_settled(pilot, "#editor-cancel")
        confirmation = app.screen
        assert isinstance(confirmation, ConfirmDialog)
        assert confirmation.query_one("#confirm-container").border_title == "放弃提示词？"  # noqa: RUF001
        assert str(confirmation.query_one("#confirm-message", Static).render()) == (
            "关闭编辑器，不提交本次内容吗？"  # noqa: RUF001
        )
        assert str(confirmation.query_one("#confirm-yes", Button).label) == "放弃"
        assert str(confirmation.query_one("#confirm-no", Button).label) == "取消"

        app.locale_controller.switch_locale("en")
        assert confirmation.query_one("#confirm-container").border_title == "Discard prompt?"
        assert str(confirmation.query_one("#confirm-message", Static).render()) == (
            "Close the editor without committing this content?"
        )
        assert str(confirmation.query_one("#confirm-yes", Button).label) == "Discard"


async def test_clean_escape_cancels_immediately_including_after_undo() -> None:
    app = _DialogApp()

    async with app.run_test() as pilot:
        results: list[EditorDialogResult] = []
        dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("x", "ctrl+z")
        assert dialog.query_one(MessageEditor).text == "draft"

        await pilot.press("escape")
        await pilot.pause()

        assert results[0].accepted is False


async def test_dirty_escape_arms_then_second_escape_discards() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("x", "escape")
        await pilot.pause()

        assert results == []
        assert dialog.discard_armed is True
        assert _status_text(dialog) == "Unsaved changes · Esc again discards · Ctrl+O/Ctrl+Enter commits"

        await pilot.press("escape")
        await pilot.pause()

        assert results[0].accepted is False


@pytest.mark.parametrize("traversal_key", ["tab", "shift+tab"])
async def test_focus_traversal_keeps_dirty_discard_armed_until_escape(traversal_key: str) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="discard traversal editor focus",
        )
        await pilot.press("x", "escape", traversal_key)

        assert dialog.discard_armed is True
        assert _status_text(dialog) == "Unsaved changes · Esc again discards · Ctrl+O/Ctrl+Enter commits"

        await pilot.press("escape")
        await _wait_for_condition(
            lambda: bool(results),
            pilot,
            description="armed discard after focus traversal",
        )
        assert results[0].accepted is False


async def test_edit_disarms_dirty_escape_but_cursor_movement_does_not() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("x", "escape")
        assert dialog.discard_armed is True

        await pilot.press("left")
        assert dialog.discard_armed is True

        await pilot.press("y")
        await pilot.pause()
        assert dialog.discard_armed is False

        await pilot.press("escape")
        assert dialog.discard_armed is True
        dialog.query_one(MessageEditor).move_cursor((0, 0))
        await pilot.press("backspace")
        await pilot.pause()
        assert dialog.discard_armed is False

        await pilot.press("escape")
        assert dialog.discard_armed is True
        assert results == []


async def test_paste_delete_undo_redo_and_vim_mutation_disarm_escape() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)))

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="edit-activity dialog focus",
        )
        editor = dialog.query_one(MessageEditor)

        await pilot.press("x", "escape")
        assert dialog.discard_armed is True
        await editor._on_paste(Paste("pasted"))
        await _wait_for_condition(
            lambda: not dialog.discard_armed,
            pilot,
            description="paste disarming escape",
        )

        await pilot.press("escape", "ctrl+z")
        await _wait_for_condition(
            lambda: not dialog.discard_armed,
            pilot,
            description="undo disarming escape",
        )
        await pilot.press("escape", "ctrl+y")
        await _wait_for_condition(
            lambda: not dialog.discard_armed,
            pilot,
            description="redo disarming escape",
        )
        await pilot.press("escape", "backspace")
        await _wait_for_condition(
            lambda: not dialog.discard_armed,
            pilot,
            description="deletion disarming escape",
        )

        await pilot.press("escape", "f3", "f3", "x")
        await _wait_for_condition(
            lambda: not dialog.discard_armed,
            pilot,
            description="Vim mutation disarming escape",
        )


async def test_backdrop_click_does_not_dismiss_editor() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)))

    async with app.run_test(size=(100, 30)) as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()

        await pilot.click(offset=(0, 0))
        await pilot.pause()

        assert results == []
        assert app.screen is dialog


async def test_save_refuses_to_dismiss_while_input_is_unavailable() -> None:
    app = _DialogApp()
    available = False
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)), can_commit=lambda: available)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("x", "ctrl+o")
        await pilot.pause()

        assert results == []
        assert app.screen is dialog
        assert dialog.query_one(MessageEditor).text == "draftx"
        assert _status_text(dialog) == "Input unavailable"
        assert dialog.query_one("#editor-status", Static).has_class("editor-status-warning")

        available = True
        await pilot.press("y")
        await pilot.pause()
        assert _status_text(dialog) == "Ready"
        assert not dialog.query_one("#editor-status", Static).has_class("editor-status-warning")

        await pilot.press("ctrl+o")
        await pilot.pause()
        assert results[0].accepted is True
        assert results[0].text == "draftxy"


async def test_input_prompt_button_opens_editor_with_current_draft() -> None:
    app = _MainScreenEditorApp()

    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "current draft"
        input_bar.query_one("#chat-input", TextArea).move_cursor((0, 7))
        await pilot.pause()

        await pilot.click("#editor-btn")
        await _wait_for_editor_dialog_ready(app, pilot, description="editor opened from prompt button")

        editor = app.screen.query_one(MessageEditor)
        assert editor.text == "current draft"
        assert editor.cursor_location == (0, 7)

        await click_when_settled(pilot, "#editor-cancel")
        await click_when_settled(pilot, "#confirm-yes")
        await _wait_for_condition(
            lambda: app.screen is app.main_screen,
            pilot,
            description="editor closed after prompt-button open",
        )

        chat_input = input_bar.query_one("#chat-input", TextArea)
        assert chat_input.has_focus
        assert chat_input.cursor_location == (0, 7)


async def test_changed_underlying_draft_requires_second_save_before_overwrite() -> None:
    app = _MainScreenEditorApp()

    async with app.run_test() as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "original"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.pause()
        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: isinstance(app.screen, EditorDialog),
            pilot,
            description="editor opened for draft conflict",
        )
        dialog = app.screen
        assert isinstance(dialog, EditorDialog)
        await pilot.press(*" edited")

        input_bar.replace_draft("restored externally")
        await pilot.pause()
        await pilot.press("ctrl+o")

        assert app.screen is dialog
        assert input_bar.value == "restored externally"
        assert _status_text(dialog) == "Chat draft changed — Commit again to overwrite"

        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: app.screen is app.main_screen,
            pilot,
            description="confirmed editor overwrite",
        )
        assert input_bar.value == "original edited"


async def test_tab_to_commit_keeps_draft_overwrite_confirmation_armed() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(
        EditorBufferSnapshot("draft", (0, 5), draft_revision=1),
        current_draft_revision=lambda: 2,
    )

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="overwrite traversal editor focus",
        )
        await pilot.press("ctrl+o")
        assert _status_text(dialog) == "Chat draft changed — Commit again to overwrite"

        await pilot.press("tab")
        assert dialog.query_one("#editor-commit").has_focus
        assert dialog._overwrite_armed_revision == 2
        assert _status_text(dialog) == "Chat draft changed — Commit again to overwrite"

        await pilot.press("enter")
        await _wait_for_condition(
            lambda: bool(results),
            pilot,
            description="confirmed overwrite from focused Commit button",
        )
        assert results[0].accepted is True


@pytest.mark.parametrize("traversal_key", ["tab", "shift+tab"])
async def test_emacs_mark_survives_focus_traversal(traversal_key: str) -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("alpha beta", (0, 0)), mode=EditorMode.EMACS)

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="Emacs traversal editor focus",
        )
        editor = dialog.query_one(MessageEditor)
        await pilot.press("ctrl+space", "alt+f")
        keymap = editor.active_keymap
        assert isinstance(keymap, EmacsKeymap)
        mark = keymap.mark
        selected = editor.selected_text
        assert mark == (0, 0)
        assert selected

        await pilot.press(traversal_key)

        assert keymap.mark == mark
        assert editor.selected_text == selected


@pytest.mark.parametrize(
    ("mode", "save_keys", "click_selector"),
    [
        (EditorMode.STANDARD, ("ctrl+o",), None),
        (EditorMode.EMACS, ("ctrl+enter",), None),
        (EditorMode.STANDARD, (), "#editor-commit"),
        (EditorMode.VIM, ("Z", "Z"), None),
        (EditorMode.VIM, (":", "w", "q", "enter"), None),
    ],
)
async def test_every_save_path_obeys_current_commit_gate(
    mode: EditorMode,
    save_keys: tuple[str, ...],
    click_selector: str | None,
) -> None:
    app = _DialogApp()
    available = False
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(
        EditorBufferSnapshot("draft", (0, 5)),
        mode=mode,
        can_commit=lambda: available,
    )

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="commit-gate editor focus",
        )
        if click_selector is None:
            await pilot.press(*save_keys)
        else:
            await pilot.click(click_selector)

        assert results == []
        assert app.screen is dialog
        assert dialog.query_one(MessageEditor).text == "draft"
        assert _status_text(dialog) == "Input unavailable"

        available = True
        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: bool(results),
            pilot,
            description="save after commit gate reopens",
        )
        assert results[0].accepted is True


# The complete Vim state matrix lives in widgets/editor/test_keymaps.py.
# These exercise the TextArea, selection and command-line event routes.
@pytest.mark.parametrize("prefix", [("i",), ("v",), (":",)])
async def test_vim_escape_changes_state_without_dismissing_or_arming(prefix: tuple[str, ...]) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("one two", (0, 0)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press(*prefix, "escape")
        keymap = dialog.query_one(MessageEditor).active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is VimState.NORMAL
        assert dialog.discard_armed is False
        assert results == []


@pytest.mark.parametrize("prefix", [("i",), (":",)])
async def test_ctrl_o_accepts_from_vim_text_and_command_inputs(prefix: tuple[str, ...]) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("one two", (0, 0)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press(*prefix, "ctrl+o")
        await pilot.pause()
        assert results[0].accepted is True


@pytest.mark.parametrize(("keys", "accepted"), [(("Z", "Z"), True), (("Z", "Q"), False)])
async def test_vim_zz_and_zq_are_immediate_save_and_cancel(keys: tuple[str, ...], accepted: bool) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press(*keys)
        await pilot.pause()
        assert results[0].accepted is accepted


@pytest.mark.parametrize(("command", "accepted"), [("wq", True), ("x", True), ("q!", False), ("q", False)])
async def test_vim_limited_command_line_dismissal_paths(command: str, accepted: bool) -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press(":", *command, "enter")
        await pilot.pause()
        assert results[0].accepted is accepted


async def test_vim_dirty_q_and_bare_w_refuse_without_mutating_or_dismissing() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("i", "X", "escape", ":", "q", "enter")
        assert results == []
        assert dialog.query_one(MessageEditor).text == "Xdraft"
        assert _status_text(dialog) == "No write since last change; use :q!"
        assert dialog.query_one("#editor-status", Static).has_class("editor-status-warning")

        await pilot.press("backspace", "w", "enter")
        assert results == []
        assert dialog.query_one(MessageEditor).text == "Xdraft"
        assert _status_text(dialog) == "Not an editor command: w"
        assert dialog.query_one("#editor-status", Static).has_class("editor-status-warning")


async def test_editor_status_styling_does_not_infer_severity_from_text() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 0)))

    async with app.run_test() as pilot:
        await app.push_screen(dialog)
        await pilot.pause()

        dialog._refresh_status("No write since this is ordinary status", warning=False)

        assert not dialog.query_one("#editor-status", Static).has_class("editor-status-warning")


async def test_emacs_escape_guard_kill_activity_and_mode_switch_reset() -> None:
    app = _DialogApp()
    results: list[EditorDialogResult] = []
    dialog = EditorDialog(EditorBufferSnapshot("one two", (0, 0)), mode=EditorMode.EMACS)

    async with app.run_test() as pilot:
        app.push_screen(dialog, results.append)
        await pilot.pause()
        await pilot.press("alt+d", "escape")
        assert dialog.discard_armed is True
        await pilot.press("ctrl+y")
        assert dialog.discard_armed is False

        await pilot.press("x", "escape")
        assert dialog.discard_armed is True
        await pilot.press("f3")
        assert dialog.query_one(MessageEditor).mode is EditorMode.VIM
        assert dialog.discard_armed is False
        assert results == []


async def test_f3_resets_incomplete_vim_operator_before_returning_to_vim() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("one two", (0, 0)), mode=EditorMode.VIM)

    async with app.run_test() as pilot:
        app.push_screen(dialog)
        await pilot.pause()
        await pilot.press("d")
        original = dialog.query_one(MessageEditor).active_keymap
        assert isinstance(original, VimKeymap)
        assert original.state is VimState.OPERATOR_PENDING
        assert _status_text(dialog) == "Normal · Pending: d"

        await pilot.press("f3", "f3", "f3")
        replacement = dialog.query_one(MessageEditor).active_keymap
        assert isinstance(replacement, VimKeymap)
        assert replacement is not original
        assert replacement.state is VimState.NORMAL


async def test_ctrl_q_remains_the_priority_main_screen_quit_and_is_not_an_editor_hint() -> None:
    binding = next(binding for binding in MainScreen.BINDINGS if binding.key == "ctrl+q")
    assert binding.action == "quit"
    assert binding.priority is True
    status = MessageEditor(mode=EditorMode.VIM).status_text
    assert isinstance(status, MessageRef)
    assert "C-Q" not in format_message(status)

    class _QuitProbeApp(_MainScreenEditorApp):
        def __init__(self) -> None:
            super().__init__()
            self.quit_requests = 0

        def action_quit(self) -> None:
            self.quit_requests += 1

    app = _QuitProbeApp()

    async with app.run_test() as pilot:
        await pilot.pause()
        app.main_screen.query_one(InputBar).focus_input()
        await wait_for(
            lambda: app.main_screen.query_one(InputBar).query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+q")
        assert app.quit_requests == 1


async def test_main_screen_editor_cancel_and_writeback_preserve_drafts_without_submitting() -> None:
    app = _MainScreenEditorApp()
    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.value = "original"
        text_area.move_cursor((0, 2))
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot)

        await pilot.press("ctrl+o")
        await _wait_for_editor_dialog_ready(app, pilot, description="editor before cancellation")
        assert app.screen.query_one(MessageEditor).cursor_location == (0, 2)
        await pilot.press("X")
        await click_when_settled(pilot, "#editor-cancel")
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: app.screen is app.main_screen and text_area.has_focus, pilot=pilot)
        assert input_bar.value == "original"
        assert text_area.cursor_location == (0, 2)
        assert app.submitted == []

        await pilot.press("ctrl+o")
        await _wait_for_editor_dialog_ready(app, pilot, description="editor before writeback")
        assert app.screen.query_one(MessageEditor).cursor_location == (0, 2)
        await pilot.press("X", "ctrl+o")
        await wait_for(lambda: app.screen is app.main_screen and text_area.has_focus, pilot=pilot)
        assert input_bar.value == "orXiginal"
        assert text_area.cursor_location == (0, 9)
        assert app.submitted == []

        for replacement in ("", "new draft"):
            await pilot.press("ctrl+o")
            await _wait_for_editor_dialog_ready(app, pilot, description="editor before replacing the draft")
            editor = app.screen.query_one(MessageEditor)
            assert editor.text == input_bar.value
            editor.clear()
            if replacement:
                editor.insert(replacement)
            await pilot.press("ctrl+o")
            await wait_for(lambda: app.screen is app.main_screen and text_area.has_focus, pilot=pilot)
            assert input_bar.value == replacement
            assert app.submitted == []


@pytest.mark.parametrize("cancel_keys", [("Z", "Q"), (":", "q", "!", "enter")])
async def test_main_screen_explicit_vim_cancel_preserves_original_draft_and_cursor(
    cancel_keys: tuple[str, ...],
) -> None:
    app = _MainScreenEditorApp()

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        text_area = input_bar.query_one("#chat-input", TextArea)
        input_bar.value = "original"
        text_area.move_cursor((0, 2))
        text_area.focus()
        await wait_for(lambda: text_area.has_focus, pilot=pilot, description="control focus before interaction")

        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: isinstance(app.screen, EditorDialog),
            pilot,
            description="editor before explicit Vim cancel",
        )
        await pilot.press("f3", "f3", "i", "X", "escape", *cancel_keys)
        await _wait_for_condition(
            lambda: app.screen is app.main_screen,
            pilot,
            description="explicit Vim cancellation",
        )

        assert input_bar.value == "original"
        assert text_area.cursor_location == (0, 2)
        assert app.submitted == []


async def test_main_screen_save_uses_current_lock_and_running_state() -> None:
    app = _MainScreenEditorApp()

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "draft"
        input_bar.agent_running = True
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+o")
        await pilot.pause()
        dialog = app.screen
        assert isinstance(dialog, EditorDialog)

        input_bar.locked = True
        await pilot.pause()
        await pilot.press("x", "ctrl+o")
        await pilot.pause()
        assert app.screen is dialog
        assert input_bar.value == "draft"

        input_bar.locked = False
        input_bar.agent_running = False
        await pilot.pause()
        await pilot.press("ctrl+o")
        await pilot.pause()
        assert input_bar.value == "draftx"
        assert app.submitted == []


async def test_main_screen_idle_to_running_transition_accepts_into_current_draft_state() -> None:
    app = _MainScreenEditorApp()

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "idle draft"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: isinstance(app.screen, EditorDialog),
            pilot,
            description="editor opened while idle",
        )

        input_bar.agent_running = True
        await pilot.press(*" injected", "ctrl+o")
        await _wait_for_condition(
            lambda: app.screen is app.main_screen,
            pilot,
            description="editor accepted after running transition",
        )

        assert input_bar.agent_running is True
        assert input_bar.value == "idle draft injected"
        assert app.submitted == []


async def test_main_screen_rejects_oversize_draft_before_opening_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    app = _MainScreenEditorApp()
    notifications: list[tuple[str, str]] = []
    monkeypatch.setattr(
        app.main_screen,
        "notify",
        lambda message, *, title="", **_kwargs: notifications.append((str(message), title)),
    )

    async with app.run_test(size=(120, 36)) as pilot:
        input_bar = app.main_screen.query_one(InputBar)
        monkeypatch.setattr(
            input_bar,
            "snapshot_draft",
            lambda: InputDraftSnapshot(ChatDraft("x" * (MESSAGE_EDITOR_MAX_CHARACTERS + 1)), (0, 0), 0),
        )
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )

        await pilot.press("ctrl+o")
        await pilot.pause()

        assert app.screen is app.main_screen
        assert notifications == [("Editor supports drafts up to 500,000 characters.", "Draft too large for editor")]


async def test_editor_layout_has_mode_hint_header_and_status_action_row() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("# draft", (0, 7)))

    async with app.run_test(size=(120, 30)) as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="editor layout mount",
        )

        header = dialog.query_one("#editor-header")
        editor_container = dialog.query_one("#editor-dialog")
        actions = dialog.query_one("#editor-actions")
        mode_label = dialog.query_one("#editor-mode-label", Static)
        mode_select = dialog.query_one("#editor-mode", Select)
        hint = dialog.query_one("#editor-hint", Static)
        status_label = dialog.query_one("#editor-status-label", Static)
        status = dialog.query_one("#editor-status", Static)
        commit = dialog.query_one("#editor-commit", Button)
        cancel = dialog.query_one("#editor-cancel", Button)
        editor = dialog.query_one(MessageEditor)

        assert list(dialog.query("#editor-prompt-label")) == []
        assert editor_container.border_title == "Prompt Editor"
        assert editor_container.border_subtitle == "Standard"
        assert list(dialog.query("#editor-toolbar")) == []
        assert list(dialog.query("#editor-preview-toggle")) == []
        assert list(dialog.query("#editor-open-hint")) == []
        assert list(dialog.query("#editor-preview")) == []
        assert commit.label.plain == "Commit"
        assert cancel.label.plain == "Discard"
        assert cancel.variant == "error"
        assert str(mode_label.render()) == "Mode:"
        assert mode_label.region.y == mode_select.region.y == hint.region.y
        assert header.region.height == mode_label.region.height == mode_select.region.height == hint.region.height == 1
        assert mode_label.region.x == header.region.x
        assert mode_select.region.x >= mode_label.region.right
        assert mode_select.region.width == 14
        assert hint.region.x >= mode_select.region.right
        assert hint.region.right == header.region.right
        assert editor.region.y == header.region.bottom + 1
        assert not editor.styles.border
        assert editor.styles.background.a == pytest.approx(0.08)
        assert str(status_label.render()) == "Status:"
        assert status_label.region.y == status.region.y == commit.region.y == cancel.region.y
        assert status_label.region.height == status.region.height == commit.region.height == cancel.region.height == 3
        assert status_label.region.x == actions.region.x
        assert status.region.x >= status_label.region.right
        assert cancel.region.right == actions.region.right


async def test_mode_select_and_f3_stay_synchronized() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)), mode=EditorMode.EMACS)

    async with app.run_test(size=(120, 30)) as pilot:
        app.push_screen(dialog)
        await pilot.pause()
        editor = dialog.query_one(MessageEditor)
        mode_select = dialog.query_one("#editor-mode", Select)
        editor_container = dialog.query_one("#editor-dialog")
        accent = Color.parse(app.get_css_variables()["accent"])
        success = Color.parse(app.get_css_variables()["success"])
        primary = Color.parse(app.get_css_variables()["primary"])

        assert editor.mode is EditorMode.EMACS
        assert mode_select.value == EditorMode.EMACS.value
        assert editor_container.border_subtitle == "Emacs"
        assert editor_container.has_class("editor-mode-emacs")
        assert editor_container.styles.border_top[1] == accent.with_alpha(0.8)
        assert editor_container.styles.border_title_color == accent
        assert editor_container.styles.border_subtitle_color == accent
        assert _hint_text(dialog) == " Ctrl+O  Commit  Esc  Discard  Ctrl+Space  Select  F3  Mode"

        mode_select.focus()
        await wait_for(lambda: mode_select.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.press("f3")
        assert editor.mode is EditorMode.VIM
        assert mode_select.value == EditorMode.VIM.value
        assert editor_container.border_subtitle == "Vim"
        assert editor_container.has_class("editor-mode-vim")
        assert not editor_container.has_class("editor-mode-emacs")
        assert editor_container.styles.border_top[1] == success.with_alpha(0.8)
        assert editor_container.styles.border_title_color == success
        assert editor_container.styles.border_subtitle_color == success
        assert _hint_text(dialog) == " i  Edit  v  Visual  :  Command  Ctrl+O  ZZ  Commit  ZQ  :q!  Discard"

        mode_select.value = EditorMode.STANDARD.value
        await pilot.pause()
        assert editor.mode is EditorMode.STANDARD
        assert editor_container.border_subtitle == "Standard"
        assert editor_container.has_class("editor-mode-standard")
        assert not editor_container.has_class("editor-mode-vim")
        assert editor_container.styles.border_top[1] == primary.with_alpha(0.8)
        assert editor_container.styles.border_title_color == primary
        assert editor_container.styles.border_subtitle_color == primary


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.VIM])
async def test_keyboard_and_mouse_focus_traversal_reaches_all_editor_controls(mode: EditorMode) -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("draft", (0, 5)), mode=mode)

    async with app.run_test(size=(120, 30)) as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="initial canvas focus",
        )

        expected_ids = ["editor-commit", "editor-cancel", "editor-mode", "message-editor"]
        visited: list[str | None] = []
        for _ in expected_ids:
            await pilot.press("tab")
            visited.append(dialog.focused.id if dialog.focused is not None else None)
        assert visited == expected_ids

        await pilot.press("shift+tab")
        assert dialog.query_one("#editor-mode", Select).has_focus
        await pilot.click("#message-editor", offset=(2, 1))
        assert dialog.query_one(MessageEditor).has_focus
        await pilot.click("#editor-mode")
        assert dialog.query_one("#editor-mode", Select).expanded is True


async def test_narrow_terminal_keeps_editor_and_bottom_controls_usable() -> None:
    app = _DialogApp()
    dialog = EditorDialog(EditorBufferSnapshot("narrow draft", (0, 6)))

    async with app.run_test(size=(55, 18)) as pilot:
        app.push_screen(dialog)
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).has_focus,
            pilot,
            description="narrow editor focus",
        )

        assert dialog.query_one(MessageEditor).text == "narrow draft"
        assert dialog.query_one(MessageEditor).outer_size.width > 0
        assert dialog.query_one("#editor-mode", Select).outer_size.width == 12
        assert dialog.query_one("#editor-hint", Static).outer_size.width > 0
        assert dialog.query_one("#editor-status", Static).outer_size.width > 0
        assert dialog.query_one("#editor-commit", Button).outer_size.width > 0
        assert dialog.query_one("#editor-cancel", Button).outer_size.width > 0


@pytest.mark.parametrize("dismissal", ["commit", "cancel"])
async def test_keymap_persists_and_seeds_subsequent_dialog_on_commit_or_cancel(
    monkeypatch: pytest.MonkeyPatch,
    dismissal: str,
) -> None:
    persisted: list[str] = []
    monkeypatch.setattr("chrys.app.tui.screens.main.screen.persist_editor_keymap", persisted.append)
    overrides: list[str] = []
    app = _MainScreenEditorApp(on_editor_keymap_changed=overrides.append)

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.value = "original"
        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+o")
        await pilot.pause()

        await pilot.press("f3")
        if dismissal == "commit":
            await pilot.press("ctrl+o")
        else:
            await click_when_settled(pilot, "#editor-cancel")
            await click_when_settled(pilot, "#confirm-yes")
        await pilot.pause()

        assert persisted == overrides == [EditorMode.EMACS.value]
        assert input_bar.value == "original"

        input_bar.focus_input()
        await wait_for(
            lambda: input_bar.query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+o")
        await pilot.pause()
        next_dialog = app.screen
        assert isinstance(next_dialog, EditorDialog)
        assert next_dialog.query_one(MessageEditor).mode is EditorMode.EMACS
        assert next_dialog.query_one("#editor-mode", Select).value == EditorMode.EMACS.value

        await _wait_for_editor_dialog_ready(app, pilot, description="reopened editor with unchanged keymap")
        await click_when_settled(pilot, "#editor-cancel")
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: app.screen is app.main_screen, pilot=pilot)
        assert persisted == overrides == [EditorMode.EMACS.value]


async def test_queued_editor_requests_open_only_one_dialog() -> None:
    app = _MainScreenEditorApp()

    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause()
        input_bar = app.main_screen.query_one(InputBar)
        input_bar.post_message(InputBar.EditorRequested())
        input_bar.post_message(InputBar.EditorRequested())
        await _wait_for_condition(
            lambda: isinstance(app.screen, EditorDialog),
            pilot,
            description="single editor from queued requests",
        )
        await pilot.pause()

        assert sum(isinstance(screen, EditorDialog) for screen in app.screen_stack) == 1


async def test_chrys_app_passes_loaded_editor_keymap_to_main_screen(tmp_path: Path) -> None:
    class _Engine(ShutdownOnlyEngine):
        async def shutdown(self) -> None:
            return

    app = ChrysApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(editor_keymap="vim"),
        state_store=JsonFileStateStore(tmp_path),
    )

    screen = app._build_main_screen()

    assert screen._editor_mode is EditorMode.VIM


async def test_populated_main_screen_editor_overlay_does_not_restyle_recompose_or_relayout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Engine(ShutdownOnlyEngine):
        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = ChrysApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    async with app.run_test(size=(120, 36)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        panel = main_screen.query_one(ChatPanel)
        transcript = []
        for index in range(24):
            transcript.extend(
                (
                    UserMessage(f"Question {index}\nwith a second line"),
                    AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
                )
            )
        await panel.mount(*transcript)
        await _wait_for_condition(
            lambda: (
                len(panel.walk_children()) >= 96
                and all(markdown.source for markdown in panel.query(VirtualizedMarkdown))
            ),
            pilot,
            description="populated transcript composition",
        )
        main_screen.query_one(InputBar).focus_input()
        await _wait_for_condition(
            lambda: main_screen.query_one(InputBar).query_one("#chat-input", TextArea).has_focus,
            pilot,
            description="composer focus before overlay",
        )
        await _wait_for_condition(
            lambda: screen_is_settled(app, main_screen),
            pilot,
            description="settled underlay layout",
            stable_observations=4,
        )

        style_updates: list[bool] = []
        footer_recomposes: list[None] = []
        layout_refreshes: list[None] = []
        monkeypatch.setattr(
            main_screen,
            "update_node_styles",
            lambda animate=True: style_updates.append(animate),
        )
        monkeypatch.setattr(
            main_screen,
            "_refresh_layout",
            lambda *_args, **_kwargs: layout_refreshes.append(None),
        )

        async def record_footer_recompose() -> None:
            footer_recomposes.append(None)

        monkeypatch.setattr(main_screen.query_one(ChrysFooter), "recompose", record_footer_recompose)

        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: isinstance(app.screen, EditorDialog),
            pilot,
            description="editor overlay open",
        )
        editor_dialog = app.screen
        assert isinstance(editor_dialog, EditorDialog)
        await pilot.press("f3", *" edited")
        await _wait_for_condition(
            lambda: editor_dialog.query_one(MessageEditor).text == " edited",
            pilot,
            description="overlay editor interaction",
        )
        input_bar = main_screen.query_one(InputBar)
        input_bar.agent_running = True
        await pilot.pause()
        assert str(input_bar.query_one("#send-btn", Button).label) == "Interrupt"
        assert input_bar._button_geometry_dirty is True
        assert main_screen._layout_required is False
        assert layout_refreshes == []
        assert await pilot.hover("#editor-cancel") is True
        assert app.mouse_over is not None
        await click_when_settled(pilot, "#editor-cancel")
        await click_when_settled(pilot, "#confirm-yes")
        await _wait_for_condition(
            lambda: app.screen is main_screen,
            pilot,
            description="editor overlay close",
        )
        revealed_mouse_over = main_screen.get_hover_widgets_at(*app.mouse_position).widgets[0]
        await _wait_for_condition(
            lambda: app.mouse_over is revealed_mouse_over,
            pilot,
            description="stationary pointer transferred to revealed underlay",
        )

        assert style_updates == []
        assert footer_recomposes == []
        assert layout_refreshes == []
        assert input_bar._button_geometry_dirty is False
        assert input_bar.query_one("#send-btn", Button).region.width == len("Interrupt") + 4
        assert revealed_mouse_over.is_attached


async def test_editor_uses_top_screen_gc_gate_without_participants_or_gc_posts(
    tmp_path: Path,
) -> None:
    gc_messages: list[GcAbsorbRequested | GcReclaimRequested] = []

    class _GcProbeApp(ChrysApp):
        CSS_PATH = SRC_ROOT / "chrys" / "app" / "tui" / "chrys.tcss"

        # Textual dispatches class handlers; instance monkeypatches are ignored.
        # Leave default handling enabled so ChrysApp still forwards the requests.
        def on_gc_absorb_requested(self, event: GcAbsorbRequested) -> None:
            gc_messages.append(event)

        def on_gc_reclaim_requested(self, event: GcReclaimRequested) -> None:
            gc_messages.append(event)

    class _Engine(ShutdownOnlyEngine):
        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    app = _GcProbeApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )
    async with app.run_test(size=(120, 30)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        participants = main_screen._gc_freeze_participants
        # Confirm both probes participate in real dispatch before the no-posts assertion.
        controls = [
            GcAbsorbRequested(GcAbsorbReason.STABLE_CONTENT_MOUNTED),
            GcReclaimRequested(GcReclaimReason.STABLE_CONTENT_REMOVED, prompt=False),
        ]
        for control in controls:
            await app._on_message(control)
        assert gc_messages[-2:] == controls
        gc_messages.clear()
        main_screen.query_one(InputBar).focus_input()
        await wait_for(
            lambda: main_screen.query_one(InputBar).query_one("#chat-input").has_focus,
            pilot=pilot,
            description="composer focus before interaction",
        )
        await pilot.press("ctrl+o")
        await _wait_for_condition(
            lambda: isinstance(app.screen, EditorDialog),
            pilot,
            description="GC-gated editor open",
        )
        dialog = app.screen
        assert isinstance(dialog, EditorDialog)

        assert app.freeze_block_reason() is GcFreezeBlockReason.TOP_SCREEN
        assert main_screen._gc_freeze_participants == participants
        assert all(participant not in set(dialog.walk_children()) for participant in participants)

        await pilot.press(*"edited")
        await _wait_for_condition(
            lambda: dialog.query_one(MessageEditor).text == "edited",
            pilot,
            description="editor interaction before GC assertion",
        )
        await click_when_settled(pilot, "#editor-cancel")
        await click_when_settled(pilot, "#confirm-yes")
        await _wait_for_condition(
            lambda: app.screen is main_screen,
            pilot,
            description="GC-gated editor close",
        )

        assert gc_messages == []
        assert main_screen._gc_freeze_participants == participants
