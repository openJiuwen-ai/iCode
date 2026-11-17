# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused real-Textual pilots for editor keymap integration and history."""

from __future__ import annotations

from typing import Literal
from unittest.mock import Mock

import pytest
from textual._xterm_parser import XTermParser
from textual.app import App, ComposeResult
from textual.events import Paste

from chrys.app.tui.clipboard import wait_for_clipboard
from chrys.app.tui.widgets.editor import EditorMode, EditorStatus, EmacsKeymap, MessageEditor, VimKeymap, VimState
from chrys.app.tui.widgets.editor.types import EditorIntent
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.patches import textual_extended_keys, textual_windows_keys
from chrys.foundation.patches.textual_kitty_keyboard import apply_runtime_patch as apply_kitty_patch
from tests.support.keyboard_patches import isolated_keyboard_patches as isolated_keyboard_patches
from tests.support.waiting import wait_for

pytestmark = pytest.mark.asyncio


def _render_status(status: EditorStatus) -> str:
    return format_message(status) if isinstance(status, MessageRef) else status


class _EditorApp(App[None]):
    def __init__(
        self,
        text: str,
        cursor: tuple[int, int],
        mode: EditorMode,
        *,
        tab_behavior: Literal["focus", "indent"] = "focus",
    ) -> None:
        super().__init__()
        self.editor = MessageEditor(text=text, cursor_location=cursor, mode=mode, tab_behavior=tab_behavior)
        self.intents: list[EditorIntent] = []

    def compose(self) -> ComposeResult:
        yield self.editor

    def on_message_editor_intent_requested(self, event: MessageEditor.IntentRequested) -> None:
        self.intents.append(event.intent)


@pytest.mark.parametrize("mode", [EditorMode.STANDARD, EditorMode.EMACS, EditorMode.VIM])
@pytest.mark.parametrize(
    ("windows", "newline", "accept"),
    [
        (False, "\x1b[13;2u", "\x1b[13;5u"),
        (False, "\x1b[27;2;13~", "\x1b[27;5;13~"),
        (True, "\x1b[13;28;13;1;16;1_", "\x1b[13;28;10;1;8;1_"),
    ],
    ids=["kitty", "xterm", "windows"],
)
async def test_shift_enter_inserts_newline_and_ctrl_enter_accepts_editor(
    isolated_keyboard_patches: None, mode: EditorMode, windows: bool, newline: str, accept: str
) -> None:
    apply_kitty_patch()
    textual_extended_keys.apply_runtime_patch()
    parser = textual_windows_keys.get_parser_class()() if windows else XTermParser()
    app = _EditorApp("hix", (0, 2), mode)
    async with app.run_test() as pilot:
        if mode is EditorMode.VIM:
            for event in parser.feed(newline):
                app.post_message(event)
            await pilot.pause()
            assert app.editor.text == "hix"
            await pilot.press("i")
        for event in parser.feed(newline):
            app.post_message(event)
        await pilot.pause()
        assert app.editor.text == "hi\nx"
        assert app.editor.cursor_location == (1, 0)
        assert app.intents == []

        for event in parser.feed(accept):
            app.post_message(event)
        await pilot.pause()
        assert app.intents == [EditorIntent.ACCEPT]
        assert app.editor.text == "hi\nx"


@pytest.fixture(params=["xterm", "windows"])
def keyboard_protocol(request: pytest.FixtureRequest, isolated_keyboard_patches: None) -> tuple[str, XTermParser]:
    apply_kitty_patch()
    textual_extended_keys.apply_runtime_patch()
    parser = textual_windows_keys.get_parser_class()() if request.param == "windows" else XTermParser()
    return request.param, parser


async def test_shift_backspace_deletes_after_a_capital(keyboard_protocol: tuple[str, XTermParser]) -> None:
    protocol, parser = keyboard_protocol
    app = _EditorApp("A", (0, 1), EditorMode.STANDARD)
    async with app.run_test() as pilot:
        sequence = "\x1b[8;14;8;1;16;1_" if protocol == "windows" else "\x1b[27;2;127~"
        for event in parser.feed(sequence):
            app.post_message(event)
        await wait_for(lambda: app.editor.text == "", pilot=pilot, description="Shift+Backspace deletes the capital")


async def test_ctrl_bracket_leaves_vim_insert_mode(keyboard_protocol: tuple[str, XTermParser]) -> None:
    protocol, parser = keyboard_protocol
    app = _EditorApp("text", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("i")
        keymap = app.editor.active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is VimState.INSERT
        sequence = "\x1b[219;26;27;1;8;1_" if protocol == "windows" else "\x1b[27;5;91~"
        for event in parser.feed(sequence):
            app.post_message(event)
        await wait_for(lambda: keymap.state is VimState.NORMAL, pilot=pilot, description="Ctrl+[ leaves Insert mode")
        assert app.editor.text == "text"


async def test_ctrl_underscore_undo_reaches_emacs_editor(keyboard_protocol: tuple[str, XTermParser]) -> None:
    protocol, parser = keyboard_protocol
    app = _EditorApp("one two", (0, 0), EditorMode.EMACS)
    async with app.run_test() as pilot:
        await pilot.press("alt+d")
        assert app.editor.text == " two"
        sequence = "\x1b[189;12;31;1;24;1_" if protocol == "windows" else "\x1b[27;6;95~"
        for event in parser.feed(sequence):
            app.post_message(event)
        await wait_for(lambda: app.editor.text == "one two", pilot=pilot, description="Ctrl+_ undoes deletion")


@pytest.mark.parametrize("undo_key", ["ctrl+/", "ctrl+underscore", "ctrl+slash"])
async def test_emacs_kill_yank_undo_redo_are_isolated_real_history_units(undo_key: str) -> None:
    app = _EditorApp("one two", (0, 0), EditorMode.EMACS)
    async with app.run_test() as pilot:
        await pilot.press("alt+d")
        assert app.editor.text == " two"
        assert isinstance(app.editor.active_keymap, EmacsKeymap)
        assert app.editor.active_keymap.kill_ring == "one"

        await pilot.press(undo_key)
        assert app.editor.text == "one two"
        await pilot.press("ctrl+y")
        assert app.editor.text == "oneone two"


async def test_native_windows_ctrl_slash_undo_reaches_emacs_editor(
    isolated_keyboard_patches: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_kitty_patch()
    textual_extended_keys.apply_runtime_patch()
    mapper = Mock(return_value=ord("/"))
    monkeypatch.setattr(textual_windows_keys, "_load_virtual_key_mapper", lambda: mapper)
    app = _EditorApp("one two", (0, 0), EditorMode.EMACS)
    async with app.run_test() as pilot:
        await pilot.press("alt+d")
        assert app.editor.text == " two"

        parser = textual_windows_keys.get_parser_class()()
        for event in parser.feed("\x1b[191;53;0;1;8;1_\x1b[191;53;0;0;8;1_"):
            app.post_message(event)
        await wait_for(lambda: app.editor.text == "one two", pilot=pilot, description="native Ctrl+/ undoes deletion")
        mapper.assert_called_once_with(191, 2)


async def test_emacs_delegated_edit_deactivates_stale_mark_before_later_kill() -> None:
    app = _EditorApp("abc def", (0, 0), EditorMode.EMACS)
    async with app.run_test() as pilot:
        await pilot.press("ctrl+space", "alt+f", "X", "alt+f")

        assert app.editor.text == "X def"
        assert app.editor.selected_text == ""
        assert isinstance(app.editor.active_keymap, EmacsKeymap)
        assert app.editor.active_keymap.mark is None

        await pilot.press("ctrl+w")
        assert app.editor.text == "X "
        assert app.editor.active_keymap.kill_ring == "def"


async def test_emacs_indent_tab_remains_a_delegated_edit() -> None:
    app = _EditorApp("alpha", (0, 0), EditorMode.EMACS, tab_behavior="indent")
    async with app.run_test() as pilot:
        await pilot.press("ctrl+space", "alt+f")
        keymap = app.editor.active_keymap
        assert isinstance(keymap, EmacsKeymap)
        assert keymap.mark == (0, 0)
        assert app.editor.selected_text == "alpha"

        await pilot.press("tab")

        assert keymap.mark is None
        assert app.editor.selected_text == ""
        assert app.editor.text != "alpha"


@pytest.mark.parametrize(
    ("keys", "changed"),
    [
        (("3", "d", "d"), "four"),
        (("J",), "one two three\nfive\nfour"),
        (("r", "X"), "Xne\ntwo three\nfive\nfour"),
        (("D",), "\ntwo three\nfive\nfour"),
        (("s",), "ne\ntwo three\nfive\nfour"),
    ],
)
async def test_vim_compound_command_is_one_real_undo_and_redo(keys: tuple[str, ...], changed: str) -> None:
    original = "one\ntwo three\nfive\nfour"
    app = _EditorApp(original, (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press(*keys)
        assert app.editor.text == changed

        await pilot.press("escape", "u")
        assert app.editor.text == original
        await pilot.press("ctrl+r")
        assert app.editor.text == changed


@pytest.mark.parametrize("redo_key", ["ctrl+r", "ctrl+shift+r"])
async def test_vim_linewise_visual_paste_supports_redo_key_variants(redo_key: str) -> None:
    app = _EditorApp("one two\nthree four", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("V", "y", "p")
        assert app.editor.text == "one two\none two\nthree four"

        await pilot.press("u")
        assert app.editor.text == "one two\nthree four"
        await pilot.press(redo_key)
        assert app.editor.text == "one two\none two\nthree four"


async def test_vim_change_then_insert_uses_documented_two_public_undo_units() -> None:
    app = _EditorApp("one two", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("c", "w", "N", "E", "W", "escape")
        assert app.editor.text == "NEW two"

        await pilot.press("u")
        assert app.editor.text == " two"
        await pilot.press("u")
        assert app.editor.text == "one two"


async def test_vim_change_inner_quote_then_insert_uses_documented_two_public_undo_units() -> None:
    original = '"one two" tail'
    app = _EditorApp(original, (0, 2), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("c", "i", '"', "N", "E", "W", "escape")
        assert app.editor.text == '"NEW" tail'

        await pilot.press("u")
        assert app.editor.text == '"" tail'
        await pilot.press("u")
        assert app.editor.text == original


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("i", "Xone"),
        ("I", "Xone"),
        ("a", "oXne"),
        ("A", "oneX"),
        ("o", "one\nX"),
        ("O", "X\none"),
    ],
)
async def test_vim_insert_commands_uniformly_ignore_counts(command: str, expected: str) -> None:
    app = _EditorApp("one", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("3", command, "X", "escape")

        assert app.editor.text == expected


async def test_mode_switch_replaces_and_resets_incomplete_keymap_state() -> None:
    app = _EditorApp("one two", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("d")
        keymap = app.editor.active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is VimState.OPERATOR_PENDING

        app.editor.set_mode(EditorMode.STANDARD)
        app.editor.set_mode(EditorMode.VIM)
        await pilot.pause()
        assert app.editor.mode is EditorMode.VIM
        replacement = app.editor.active_keymap
        assert isinstance(replacement, VimKeymap)
        assert replacement is not keymap
        assert replacement.state is VimState.NORMAL


async def test_vim_empty_lines_unicode_visual_and_linewise_register_paste() -> None:
    app = _EditorApp("🙂 alpha\n\nomega", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("x")
        assert app.editor.text == " alpha\n\nomega"
        await pilot.press("j", "d", "d", "p")
        assert app.editor.text == " alpha\nomega\n"


@pytest.mark.parametrize(
    ("prefix", "expected_state"),
    [
        ((), VimState.NORMAL),
        (("d",), VimState.OPERATOR_PENDING),
        (("v",), VimState.VISUAL_CHAR),
        ((":",), VimState.COMMAND),
    ],
)
async def test_vim_paste_is_rejected_outside_insert_without_changing_modal_state(
    prefix: tuple[str, ...],
    expected_state: VimState,
) -> None:
    app = _EditorApp("one two", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        if prefix:
            await pilot.press(*prefix)
        keymap = app.editor.active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is expected_state

        await app.editor._on_paste(Paste("PASTED"))

        assert app.editor.text == "one two"
        assert keymap.state is expected_state
        assert _render_status(app.editor.status_text) == "Paste requires Insert mode"


async def test_vim_paste_edits_document_in_insert_mode() -> None:
    app = _EditorApp("one two", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("i")
        await app.editor._on_paste(Paste("PASTED"))

        keymap = app.editor.active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is VimState.INSERT
        assert app.editor.text == "PASTEDone two"


async def test_vim_shift_insert_pastes_only_in_insert_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    monkeypatch.setattr("chrys.app.tui.clipboard.platform_helpers.clipboard_paste", lambda: "PASTED")
    app = _EditorApp("one two", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("shift+insert")
        assert app.editor.text == "one two"

        await pilot.press("i", "shift+insert")
        assert app.editor.text == "PASTEDone two"


async def test_vim_ctrl_insert_copies_visual_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    copied: list[str] = []
    monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
    app = _EditorApp("one two", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press("v", "l", "ctrl+insert")

        assert app.clipboard == "on"
        await wait_for_clipboard(app)
        assert copied == ["on"]
        keymap = app.editor.active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is VimState.VISUAL_CHAR


async def test_unbound_printable_delegates_in_emacs_but_not_vim_normal() -> None:
    emacs_app = _EditorApp("", (0, 0), EditorMode.EMACS)
    async with emacs_app.run_test() as pilot:
        await pilot.press("x", "enter", "y")
        assert emacs_app.editor.text == "x\ny"

    vim_app = _EditorApp("", (0, 0), EditorMode.VIM)
    async with vim_app.run_test() as pilot:
        await pilot.press("f", "F", "t", "T", ".", "/", "?", "q", "@", '"')
        assert vim_app.editor.text == ""
        assert vim_app.editor.cursor_location == (0, 0)
        assert all(
            token not in _render_status(vim_app.editor.status_text)
            for token in ("Search", "Macro", "Register", "Repeat")
        )


async def test_general_ex_command_is_non_destructive_and_remains_correctable() -> None:
    app = _EditorApp("draft", (0, 0), EditorMode.VIM)
    async with app.run_test() as pilot:
        await pilot.press(":", *"set number", "enter")

        assert app.editor.text == "draft"
        keymap = app.editor.active_keymap
        assert isinstance(keymap, VimKeymap)
        assert keymap.state is VimState.COMMAND
        assert _render_status(app.editor.status_text) == "Not an editor command: set number"
