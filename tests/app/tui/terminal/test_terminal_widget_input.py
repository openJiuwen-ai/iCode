# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The terminal widget as a keyboard: keys, Escape, pastes, the clipboard, focus reports and replies."""

from __future__ import annotations

from typing import ClassVar

import pytest
from textual import events
from textual._xterm_parser import XTermParser
from textual.binding import Binding, BindingType
from textual.geometry import Offset

from chrys.app.tui.behaviors import right_click_copy
from chrys.app.tui.clipboard import ClipboardApp, ClipboardReadApp
from chrys.app.tui.terminal import widget as widget_module
from chrys.app.tui.terminal.leaked_reports import MAX_HELD_LENGTH
from chrys.app.tui.terminal.widget import Terminal
from chrys.foundation.patches import textual_extended_keys, textual_windows_keys
from chrys.foundation.patches.textual_kitty_keyboard import apply_runtime_patch as apply_kitty_patch
from tests.app.tui.terminal._widget_harness import TerminalApp, connect_stdin, select_text
from tests.support.keyboard_patches import isolated_keyboard_patches as isolated_keyboard_patches
from tests.support.waiting import wait_for

# Long enough that a held Escape stays held for the whole of a test.
_NEVER = 60.0
# Short enough that a held Escape is let go while the test waits for it.
_SOON = 0.01


class _Clipboards:
    """Stands in for the clipboard helpers the widget calls."""

    def __init__(self, contents: str = "") -> None:
        self.contents = contents
        self.copied: list[str] = []

    def copy(self, app: ClipboardApp, text: str, *, max_terminal_bytes: int | None = None) -> bool:
        self.copied.append(text)
        return True

    def paste(self, app: ClipboardReadApp) -> str:
        return self.contents


def _replace_clipboards(monkeypatch: pytest.MonkeyPatch, contents: str = "") -> _Clipboards:
    clipboards = _Clipboards(contents)
    monkeypatch.setattr(right_click_copy, "copy_text_to_clipboards", clipboards.copy)
    monkeypatch.setattr(widget_module, "paste_text_from_clipboards", clipboards.paste)
    return clipboards


class _BystanderApp(TerminalApp):
    """Hears of whatever input the focused widget lets through to it."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("x", "note_binding", "Note")]

    def __init__(self) -> None:
        super().__init__()
        self.pastes_heard: list[str] = []
        self.bindings_run = 0

    def on_paste(self, event: events.Paste) -> None:
        self.pastes_heard.append(event.text)

    def action_note_binding(self) -> None:
        self.bindings_run += 1


# -- keys --------------------------------------------------------------------------------------------


async def test_typed_keys_reach_the_program() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        await pilot.press("l", "s", "space", "minus", "A", "enter", "backspace", "tab", "ctrl+d")

        assert stdin.writes == ["l", "s", " ", "-", "A", "\r", "\x7f", "\t", "\x04"]


@pytest.mark.parametrize("windows", [False, True])
async def test_extended_legacy_chords_keep_the_same_bytes_for_the_program(
    isolated_keyboard_patches: None, windows: bool
) -> None:
    apply_kitty_patch()
    textual_extended_keys.apply_runtime_patch()
    parser = textual_windows_keys.get_parser_class()() if windows else XTermParser()
    sequences = (
        [
            "8;14;8;1;2;1",
            "72;35;8;1;8;1",
            "73;23;9;1;8;1",
            "77;50;13;1;8;1",
            "219;26;27;1;8;1",
            "66;48;98;1;2;1",
            "70;33;102;1;2;1",
        ]
        if windows
        else ["27;3;127", "27;5;104", "27;5;105", "27;5;109", "27;5;91", "27;3;98", "27;3;102"]
    )
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)
        # A full-screen child receives Escape immediately instead of holding it
        # for the shell's double-Escape shortcut.
        await app.terminal.write("\x1b[?1049h")
        for sequence in sequences:
            for event in parser.feed(f"\x1b[{sequence}{'_' if windows else '~'}"):
                app.post_message(event)
        await pilot.pause()
        assert stdin.writes == ["\x17", "\x7f", "\t", "\r", "\x1b", "\x1b[1;5D", "\x1b[1;5C"]


async def test_key_with_nobody_to_send_it_to_is_dropped() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        terminal.set_write_to_stdin(None)

        await pilot.press("a")
        await terminal.write("\x1b[6n")

        assert stdin.writes == []


async def test_input_method_text_arrives_whole() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        # One key event carrying a committed word, which is how a terminal hands over IME text.
        app.post_message(events.Key("你好", "你好"))
        await wait_for(lambda: stdin.writes == ["你好"], pilot=pilot, description="IME text sent")


async def test_keys_stop_at_the_terminal() -> None:
    app = _BystanderApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("x")
        assert stdin.writes == ["x"]
        assert app.bindings_run == 0

        # With the focus elsewhere the same key is the app's binding again.
        app.query_one("#elsewhere").focus()
        await wait_for(lambda: not terminal.has_focus, pilot=pilot, description="focus elsewhere")
        await pilot.press("x")
        assert app.bindings_run == 1
        assert stdin.writes == ["x"]


async def test_cursor_keys_follow_the_programs_cursor_key_mode() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("up", "home")
        await terminal.write("\x1b[?1h")
        await pilot.press("up", "home", "ctrl+right")
        await terminal.write("\x1b[?1l")
        await pilot.press("down")

        assert stdin.writes == ["\x1b[A", "\x1b[H", "\x1bOA", "\x1bOH", "\x1b[1;5C", "\x1b[B"]


async def test_modified_keys_are_told_apart_once_the_program_asks_for_the_kitty_protocol() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("ctrl+enter", "shift+enter")
        assert stdin.writes == ["\r", "\r"]

        await terminal.write("\x1b[>1u")
        await pilot.press("ctrl+enter", "shift+enter", "a", "enter")
        assert stdin.writes[2:] == ["\x1b[13;5u", "\x1b[13;2u", "a", "\r"]

        await terminal.write("\x1b[<u")
        await pilot.press("ctrl+enter")
        assert stdin.writes[6:] == ["\r"]


async def test_modified_keys_are_told_apart_once_the_program_asks_for_modify_other_keys() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await terminal.write("\x1b[>4;2m")
        await pilot.press("ctrl+enter", "a")

        assert stdin.writes == ["\x1b[27;5;13~", "a"]


async def test_modify_other_keys_is_asked_for_by_level_and_answered_by_level() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        # Level 1 leaves a key its well-known spelling; level 2 is for the program that wants them all.
        await terminal.write("\x1b[>4;1m")
        await pilot.press("shift+tab", "ctrl+a", "ctrl+enter", "ctrl+shift+2", "ctrl+shift+3")
        assert stdin.writes == ["\x1b[Z", "\x01", "\x1b[27;5;13~", "\x00", "\x1b[27;6;35~"]

        await terminal.write("\x1b[>4;2m")
        await pilot.press("shift+tab", "ctrl+a", "ctrl+enter", "ctrl+shift+2", "ctrl+shift+3")
        assert stdin.writes[5:] == [
            "\x1b[27;2;9~",
            "\x1b[27;5;97~",
            "\x1b[27;5;13~",
            "\x1b[27;6;64~",
            "\x1b[27;6;35~",
        ]


async def test_protocol_replies_go_to_the_program() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)):
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await terminal.write("ab\r\ncd\x1b[6n")

        assert stdin.writes == ["\x1b[2;3R"]


# -- Escape ------------------------------------------------------------------------------------------


async def test_single_escape_reaches_the_program_once_a_second_tap_is_out_of_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _SOON)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("escape")
        await wait_for(lambda: stdin.writes == ["\x1b"], pilot=pilot, description="held Escape let go")

        assert terminal.has_focus
        assert app.messages_of(Terminal.EscapeExited) == []


async def test_escape_is_held_while_a_second_tap_may_follow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("escape")
        assert stdin.writes == []

        # The next key settles it: that was an Escape, and here it comes, ahead of the key.
        await pilot.press("x")
        assert stdin.writes == ["\x1b", "x"]


async def test_second_escape_leaves_the_terminal_and_sends_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("escape", "escape")
        await wait_for(lambda: app.messages_of(Terminal.EscapeExited), pilot=pilot, description="exit announced")

        assert [message.control for message in app.messages_of(Terminal.EscapeExited)] == [terminal]
        assert not terminal.has_focus
        assert stdin.writes == []

        # Nothing is left held to come out later.
        terminal.focus()
        await wait_for(lambda: terminal.has_focus, pilot=pilot, description="terminal focused again")
        await pilot.press("a")
        assert stdin.writes == ["a"]


async def test_escape_after_other_keys_is_a_first_tap_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("escape", "[", "escape")

        assert stdin.writes == ["\x1b["]
        assert terminal.has_focus
        assert app.messages_of(Terminal.EscapeExited) == []

        await pilot.press("escape")
        await wait_for(lambda: app.messages_of(Terminal.EscapeExited), pilot=pilot, description="exit announced")
        assert stdin.writes == ["\x1b["]


async def test_escape_goes_straight_to_a_full_screen_program() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await terminal.write("\x1b[?1049h")

        await pilot.press("escape")
        assert stdin.writes == ["\x1b"]

        await pilot.press("escape")
        assert stdin.writes == ["\x1b", "\x1b"]
        assert terminal.has_focus
        assert app.messages_of(Terminal.EscapeExited) == []


async def test_held_escape_is_spelled_the_way_the_program_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _SOON)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await terminal.write("\x1b[>1u")

        await pilot.press("escape")
        await wait_for(lambda: stdin.writes == ["\x1b[27u"], pilot=pilot, description="held Escape let go")


async def test_held_escape_comes_out_ahead_of_a_key_that_is_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        await pilot.press("escape", "up")

        assert stdin.writes == ["\x1b", "\x1b[A"]


_LEAKED_REPORTS = {
    "focus-in": ["[", "I"],
    "focus-out": ["[", "O"],
    "sgr-press": ["[", "<", "0", ";", "3", ";", "4", "M"],
    "sgr-release": ["[", "<", "0", ";", "3", ";", "4", "m"],
    "urxvt": ["[", "3", "2", ";", "3", ";", "4", "M"],
    "two-field-tail": ["[", "3", ";", "4", "M"],
    "x10": ["[", "M", "space", "!", '"'],
}


@pytest.mark.parametrize("keys", _LEAKED_REPORTS.values(), ids=_LEAKED_REPORTS.keys())
async def test_report_from_the_outer_terminal_that_leaked_in_as_keys_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, keys: list[str]
) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("escape", *keys)

        assert stdin.writes == []
        assert terminal.has_focus
        assert app.messages_of(Terminal.EscapeExited) == []

        # The report is gone for good and typing goes on as before.
        await pilot.press("a")
        assert stdin.writes == ["a"]


async def test_report_that_never_completes_reaches_the_program_after_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _SOON)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        await pilot.press("escape", "[", "<", "0")
        await wait_for(lambda: stdin.text == "\x1b[<0", pilot=pilot, description="held keys let go")


async def test_escape_sequence_that_is_no_report_is_forwarded_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        await pilot.press("escape", "[", "A", "b")

        assert stdin.writes == ["\x1b[", "A", "b"]


async def test_held_keys_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        await pilot.press("escape", "[", *["1"] * 40)

        # Digits could go on forever without ever ruling a report out; the cap does it for them.
        assert stdin.writes[0] == "\x1b[" + "1" * (MAX_HELD_LENGTH - 2)
        assert stdin.writes[1:] == ["1"] * (40 - (MAX_HELD_LENGTH - 2))


async def test_reset_forgets_a_held_escape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(widget_module, "ESCAPE_TAP_DURATION", _NEVER)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await pilot.press("escape", "[")

        terminal.reset()
        await pilot.press("a")

        # It was meant for the program that is gone.
        assert stdin.writes == ["a"]


# -- paste and the clipboard -------------------------------------------------------------------------


async def test_paste_is_typed_out_unless_the_program_takes_bracketed_pastes() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        app.post_message(events.Paste("one\ntwo\r\nthree"))
        await wait_for(lambda: len(stdin.writes) == 1, pilot=pilot, description="paste delivered")
        assert stdin.writes == ["one\rtwo\rthree"]

        await terminal.write("\x1b[?2004h")
        app.post_message(events.Paste("one\ntwo"))
        await wait_for(lambda: len(stdin.writes) == 2, pilot=pilot, description="bracketed paste delivered")
        assert stdin.writes[1] == "\x1b[200~one\ntwo\x1b[201~"


async def test_bracketed_paste_cannot_end_itself_early() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)):
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await terminal.write("\x1b[?2004h")

        await terminal.paste("safe\x1b[201~rm -rf\n")

        assert stdin.writes == ["\x1b[200~saferm -rf\n\x1b[201~"]


async def test_paste_stops_at_the_terminal() -> None:
    app = _BystanderApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        app.post_message(events.Paste("for the program"))
        await wait_for(lambda: stdin.writes == ["for the program"], pilot=pilot, description="paste delivered")
        assert app.pastes_heard == []

        # With the focus elsewhere a paste does come up to the app.
        app.query_one("#elsewhere").focus()
        await wait_for(lambda: not terminal.has_focus, pilot=pilot, description="focus elsewhere")
        app.post_message(events.Paste("for the app"))
        await wait_for(lambda: app.pastes_heard == ["for the app"], pilot=pilot, description="paste heard by the app")
        assert stdin.writes == ["for the program"]


@pytest.mark.parametrize("key", ["ctrl+c", "ctrl+insert"])
async def test_copy_key_copies_a_selection_instead_of_reaching_the_program(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    clipboards = _replace_clipboards(monkeypatch)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await terminal.write("some text")
        select_text(terminal, Offset(5, 0), Offset(9, 0))

        await pilot.press(key)

        assert clipboards.copied == ["text"]
        assert stdin.writes == []


@pytest.mark.parametrize("select_empty_cells", [False, True])
async def test_ctrl_c_without_selected_text_interrupts_the_program(
    monkeypatch: pytest.MonkeyPatch, select_empty_cells: bool
) -> None:
    clipboards = _replace_clipboards(monkeypatch)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        if select_empty_cells:
            await terminal.write("some text\r\n")
            select_text(terminal, Offset(0, 1), Offset(4, 1))
            assert app.screen.get_selected_text() == ""

        await pilot.press("ctrl+c")

        assert stdin.writes == ["\x03"]
        assert clipboards.copied == []


async def test_ctrl_insert_without_a_selection_does_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    clipboards = _replace_clipboards(monkeypatch)
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        stdin = connect_stdin(app.terminal)

        await pilot.press("ctrl+insert", "a")

        # Copy with nothing to copy; the program is not sent an Insert it never asked about.
        assert stdin.writes == ["a"]
        assert clipboards.copied == []


async def test_shift_insert_pastes_the_clipboard(monkeypatch: pytest.MonkeyPatch) -> None:
    _replace_clipboards(monkeypatch, "from\nthe clipboard")
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)

        await pilot.press("shift+insert")
        assert stdin.writes == ["from\rthe clipboard"]

        await terminal.write("\x1b[?2004h")
        await pilot.press("shift+insert")
        assert stdin.writes[1:] == ["\x1b[200~from\nthe clipboard\x1b[201~"]


async def test_shift_insert_with_an_empty_clipboard_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _replace_clipboards(monkeypatch, "")
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await terminal.write("\x1b[?2004h")

        await pilot.press("shift+insert", "a")

        assert stdin.writes == ["a"]


# -- focus -------------------------------------------------------------------------------------------


async def test_focus_changes_are_reported_to_a_program_that_asked() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await wait_for(lambda: terminal.has_focus, pilot=pilot, description="terminal focused")
        await terminal.write("\x1b[?1004h")

        app.query_one("#elsewhere").focus()
        await wait_for(lambda: stdin.writes == ["\x1b[O"], pilot=pilot, description="focus-out reported")

        terminal.focus()
        await wait_for(lambda: stdin.writes == ["\x1b[O", "\x1b[I"], pilot=pilot, description="focus-in reported")


async def test_focus_changes_are_not_reported_unasked() -> None:
    app = TerminalApp()
    async with app.run_test(size=(40, 11)) as pilot:
        terminal = app.terminal
        stdin = connect_stdin(terminal)
        await wait_for(lambda: terminal.has_focus, pilot=pilot, description="terminal focused")

        app.query_one("#elsewhere").focus()
        await wait_for(lambda: not terminal.has_focus, pilot=pilot, description="focus elsewhere")
        terminal.focus()
        await wait_for(lambda: terminal.has_focus, pilot=pilot, description="terminal focused again")

        # The key arrives after both focus changes were handled, and is all that was sent.
        await pilot.press("a")
        assert stdin.writes == ["a"]
