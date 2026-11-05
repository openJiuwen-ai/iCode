# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure contract tests for Standard, Emacs, and Vim keymaps."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import product

import pytest

from chrys.app.tui.widgets.editor import (
    MAX_VIM_COUNT,
    MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS,
    EditorCommand,
    EditorIntent,
    EditorKeyStroke,
    EditorRegisterKind,
    EditorScrollDirection,
    EditorStatus,
    EditorTextRange,
    EmacsKeymap,
    StandardKeymap,
    VimKeymap,
    VimState,
)
from chrys.foundation.i18n import MessageRef
from chrys.foundation.i18n.formatting import format_message

type Location = tuple[int, int]


def _render_status(status: EditorStatus) -> str:
    return format_message(status) if isinstance(status, MessageRef) else status


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _offset(text: str, location: Location) -> int:
    lines = _lines(text)
    row = min(max(location[0], 0), len(lines) - 1)
    column = min(max(location[1], 0), len(lines[row]))
    return sum(len(line) + 1 for line in lines[:row]) + column


def _location(text: str, offset: int) -> Location:
    remaining = min(max(offset, 0), len(text))
    lines = _lines(text)
    for row, line in enumerate(lines[:-1]):
        if remaining <= len(line):
            return row, remaining
        remaining -= len(line) + 1
    return len(lines) - 1, min(remaining, len(lines[-1]))


@dataclass
class _Host:
    text: str
    cursor: Location = (0, 0)
    entry_text: str | None = None
    selection_range: EditorTextRange = field(default_factory=lambda: EditorTextRange((0, 0), (0, 0)))
    edits: int = 0
    activity: int = 0
    _undo: list[tuple[str, Location]] = field(default_factory=list)
    _redo: list[tuple[str, Location]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.entry_text is None:
            self.entry_text = self.text
        self.selection_range = EditorTextRange(self.cursor, self.cursor)

    @property
    def editor_text(self) -> str:
        return self.text

    @property
    def editor_cursor_location(self) -> Location:
        return self.cursor

    @property
    def editor_selection(self) -> EditorTextRange:
        return self.selection_range

    @property
    def editor_page_height(self) -> int:
        return 6

    @property
    def editor_is_dirty(self) -> bool:
        return self.text != self.entry_text

    def move_editor_cursor(self, location: Location, *, select: bool = False) -> None:
        target = self._clamp(location)
        anchor = self.selection_range.start if select else target
        self.cursor = target
        self.selection_range = EditorTextRange(anchor, target)

    def set_editor_selection(self, anchor: Location, active: Location) -> None:
        self.cursor = self._clamp(active)
        self.selection_range = EditorTextRange(self._clamp(anchor), self.cursor)

    def execute_editor_command(self, command: EditorCommand, *, select: bool = False) -> None:
        row, column = self.cursor
        lines = _lines(self.text)
        if command is EditorCommand.CURSOR_LEFT:
            target = _location(self.text, max(_offset(self.text, self.cursor) - 1, 0))
        elif command is EditorCommand.CURSOR_RIGHT:
            target = _location(self.text, _offset(self.text, self.cursor) + 1)
        elif command is EditorCommand.CURSOR_UP:
            target = max(row - 1, 0), min(column, len(lines[max(row - 1, 0)]))
        elif command is EditorCommand.CURSOR_DOWN:
            target_row = min(row + 1, len(lines) - 1)
            target = target_row, min(column, len(lines[target_row]))
        elif command is EditorCommand.LINE_START:
            target = row, 0
        elif command is EditorCommand.LINE_END:
            target = row, len(lines[row])
        elif command is EditorCommand.WORD_LEFT:
            index = _offset(self.text, self.cursor)
            while index and self.text[index - 1].isspace():
                index -= 1
            while index and not self.text[index - 1].isspace():
                index -= 1
            target = _location(self.text, index)
        elif command is EditorCommand.WORD_RIGHT:
            index = _offset(self.text, self.cursor)
            while index < len(self.text) and not self.text[index].isspace():
                index += 1
            while index < len(self.text) and self.text[index].isspace():
                index += 1
            target = _location(self.text, index)
        elif command is EditorCommand.DOCUMENT_START:
            target = (0, 0)
        elif command is EditorCommand.DOCUMENT_END:
            target = (len(lines) - 1, len(lines[-1]))
        elif command is EditorCommand.PAGE_UP:
            target = max(row - self.editor_page_height, 0), column
        elif command is EditorCommand.PAGE_DOWN:
            target = min(row + self.editor_page_height, len(lines) - 1), column
        elif command is EditorCommand.UNDO:
            self.editor_undo()
            return
        elif command is EditorCommand.REDO:
            self.editor_redo()
            return
        elif command is EditorCommand.DELETE_LEFT:
            start = _location(self.text, max(_offset(self.text, self.cursor) - 1, 0))
            self.delete_editor_range(EditorTextRange(start, self.cursor))
            return
        else:
            end = _location(self.text, _offset(self.text, self.cursor) + 1)
            self.delete_editor_range(EditorTextRange(self.cursor, end))
            return
        self.move_editor_cursor(target, select=select)

    def read_editor_range(self, source_range: EditorTextRange) -> str:
        start, end = sorted((_offset(self.text, source_range.start), _offset(self.text, source_range.end)))
        return self.text[start:end]

    def replace_editor_range(self, source_range: EditorTextRange, text: str) -> None:
        self.replace_editor_ranges([(source_range, text)])

    def replace_editor_ranges(self, replacements: Sequence[tuple[EditorTextRange, str]]) -> None:
        if not replacements:
            return
        self._undo.append((self.text, self.cursor))
        self._redo.clear()
        for source_range, text in reversed(replacements):
            start, end = sorted((_offset(self.text, source_range.start), _offset(self.text, source_range.end)))
            self.text = self.text[:start] + text + self.text[end:]
            self.cursor = _location(self.text, start + len(text))
        self.selection_range = EditorTextRange(self.cursor, self.cursor)
        self.edits += 1
        self.activity += 1

    def delete_editor_range(self, source_range: EditorTextRange) -> None:
        self.replace_editor_range(source_range, "")

    def editor_unit_spans(self) -> list[tuple[int, int]]:
        return []

    def editor_undo(self) -> None:
        if self._undo:
            self._redo.append((self.text, self.cursor))
            self.text, self.cursor = self._undo.pop()
        self.activity += 1

    def editor_redo(self) -> None:
        if self._redo:
            self._undo.append((self.text, self.cursor))
            self.text, self.cursor = self._redo.pop()
        self.activity += 1

    def scroll_editor_page(self, direction: EditorScrollDirection) -> None:
        command = EditorCommand.PAGE_UP if direction is EditorScrollDirection.UP else EditorCommand.PAGE_DOWN
        self.execute_editor_command(command)

    def notify_editor_activity(self) -> None:
        self.activity += 1

    def emit_editor_intent(self, _intent: EditorIntent, status: EditorStatus = "") -> None:
        del status

    def _clamp(self, location: Location) -> Location:
        lines = _lines(self.text)
        row = min(max(location[0], 0), len(lines) - 1)
        return row, min(max(location[1], 0), len(lines[row]))


def _stroke(key: str) -> EditorKeyStroke:
    character = key if len(key) == 1 else None
    return EditorKeyStroke(key, character)


def _run_vim(keys: list[str], text: str = "one two\nthree four", cursor: Location = (0, 0)) -> tuple[VimKeymap, _Host]:
    keymap = VimKeymap()
    host = _Host(text, cursor)
    for key in keys:
        keymap.handle_key(_stroke(key), host)
    return keymap, host


@pytest.mark.parametrize(
    ("key", "intent", "consumed"),
    [
        ("ctrl+o", EditorIntent.ACCEPT, True),
        ("ctrl+enter", EditorIntent.ACCEPT, True),
        ("escape", EditorIntent.REQUEST_ESCAPE_CANCEL, True),
        ("f3", EditorIntent.CYCLE_MODE, True),
        ("enter", EditorIntent.NONE, False),
        ("left", EditorIntent.NONE, False),
        ("ctrl+z", EditorIntent.NONE, False),
        ("x", EditorIntent.NONE, False),
    ],
)
def test_standard_key_table(key: str, intent: EditorIntent, consumed: bool) -> None:
    result = StandardKeymap().handle_key(_stroke(key), _Host("text"))
    assert (result.intent, result.consumed) == (intent, consumed)


@pytest.mark.parametrize(
    "key",
    [
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "ctrl+left",
        "ctrl+right",
        "shift+left",
        "ctrl+a",
        "ctrl+c",
        "ctrl+insert",
        "ctrl+x",
        "ctrl+v",
        "ctrl+shift+v",
        "shift+insert",
        "super+c",
        "super+x",
        "super+v",
        "ctrl+z",
        "ctrl+y",
        "f8",
    ],
)
def test_standard_inherited_table_delegates_to_enhanced_text_area(key: str) -> None:
    assert StandardKeymap().handle_key(_stroke(key), _Host("text")).consumed is False


@pytest.mark.parametrize(
    ("key", "intent"),
    [
        ("ctrl+o", EditorIntent.ACCEPT),
        ("ctrl+enter", EditorIntent.ACCEPT),
        ("escape", EditorIntent.REQUEST_ESCAPE_CANCEL),
        ("f3", EditorIntent.CYCLE_MODE),
    ],
)
def test_emacs_common_key_table(key: str, intent: EditorIntent) -> None:
    result = EmacsKeymap().handle_key(_stroke(key), _Host("text"))
    assert result.consumed is True
    assert result.intent is intent


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("ctrl+b", (1, 3)),
        ("ctrl+f", (1, 5)),
        ("ctrl+p", (0, 4)),
        ("ctrl+n", (2, 4)),
        ("ctrl+a", (1, 0)),
        ("ctrl+e", (1, 5)),
        ("alt+b", (1, 0)),
        ("alt+f", (2, 0)),
        ("alt+less_than", (0, 0)),
        ("alt+greater_than", (2, 5)),
        ("ctrl+v", (2, 4)),
        ("alt+v", (0, 4)),
    ],
)
def test_emacs_movement_table(key: str, expected: Location) -> None:
    host = _Host("zero\nalpha\nomega", (1, 4))
    result = EmacsKeymap().handle_key(_stroke(key), host)
    assert result.consumed is True
    assert host.cursor == expected


def test_emacs_mark_region_copy_kill_yank_and_clear_are_local() -> None:
    keymap = EmacsKeymap()
    host = _Host("alpha beta", (0, 0))

    for key in ["ctrl+space", "alt+f", "alt+w"]:
        keymap.handle_key(_stroke(key), host)
    assert keymap.kill_ring == "alpha "
    assert keymap.mark is None
    assert host.text == "alpha beta"

    host.move_editor_cursor((0, 10))
    keymap.handle_key(_stroke("ctrl+w"), host)
    assert host.text == "alpha "
    assert keymap.kill_ring == "beta"
    keymap.handle_key(_stroke("ctrl+y"), host)
    assert host.text == "alpha beta"

    keymap.handle_key(_stroke("ctrl+space"), host)
    keymap.handle_key(_stroke("ctrl+g"), host)
    assert keymap.mark is None


@pytest.mark.parametrize("key", ["ctrl+space", "ctrl+@"])
def test_emacs_mark_accepts_textual_and_terminal_nul_key_names(key: str) -> None:
    keymap = EmacsKeymap()
    host = _Host("alpha", (0, 2))

    result = keymap.handle_key(_stroke(key), host)

    assert result.consumed is True
    assert keymap.mark == (0, 2)
    assert _render_status(keymap.state_label) == "Selection active"


def test_emacs_undo_clears_mark_after_document_changes() -> None:
    keymap = EmacsKeymap()
    host = _Host("alpha", (0, 5))
    host.replace_editor_range(EditorTextRange((0, 5), (0, 5)), " beta")
    keymap.handle_key(_stroke("ctrl+space"), host)

    keymap.handle_key(_stroke("ctrl+/"), host)

    assert host.text == "alpha"
    assert keymap.mark is None
    assert host.selection_range == EditorTextRange(host.cursor, host.cursor)


@pytest.mark.parametrize(
    ("key", "text", "cursor", "expected", "killed"),
    [
        ("backspace", "a🙂b", (0, 2), "ab", ""),
        ("ctrl+h", "a🙂b", (0, 2), "ab", ""),
        ("ctrl+d", "a🙂b", (0, 1), "ab", ""),
        ("alt+d", "one  two", (0, 0), "  two", "one"),
        ("ctrl+w", "one  two", (0, 8), "one  ", "two"),
        ("ctrl+k", "one\ntwo", (0, 1), "o\ntwo", "ne"),
        ("ctrl+k", "one\ntwo", (0, 3), "onetwo", "\n"),
    ],
)
def test_emacs_edit_and_kill_table(
    key: str,
    text: str,
    cursor: Location,
    expected: str,
    killed: str,
) -> None:
    keymap = EmacsKeymap()
    host = _Host(text, cursor)
    keymap.handle_key(_stroke(key), host)
    assert host.text == expected
    if killed:
        assert keymap.kill_ring == killed
    assert host.edits == 1


def test_emacs_boundary_and_alt_w_without_region_are_silent_noops() -> None:
    keymap = EmacsKeymap()
    host = _Host("text")
    keymap.handle_key(_stroke("backspace"), host)
    keymap.handle_key(_stroke("alt+w"), host)
    assert host.text == "text"
    assert host.edits == 0
    assert keymap.kill_ring == ""


def test_emacs_consecutive_kills_chain_and_movement_breaks_the_chain() -> None:
    keymap = EmacsKeymap()
    host = _Host("one\ntwo three", (0, 1))

    keymap.handle_key(_stroke("ctrl+k"), host)
    keymap.handle_key(_stroke("ctrl+k"), host)
    assert keymap.kill_ring == "ne\n"

    host.move_editor_cursor((0, 3))
    keymap.handle_key(_stroke("ctrl+w"), host)
    assert keymap.kill_ring == "otwne\n"

    keymap.handle_key(_stroke("ctrl+f"), host)
    keymap.handle_key(_stroke("alt+d"), host)
    assert keymap.kill_ring == " three"


@pytest.mark.parametrize(
    ("keys", "cursor", "state"),
    [
        (["l", "l"], (0, 2), VimState.NORMAL),
        (["j"], (1, 0), VimState.NORMAL),
        (["G"], (1, 9), VimState.NORMAL),
        (["g", "g"], (0, 0), VimState.NORMAL),
        (["i"], (0, 0), VimState.INSERT),
        (["v", "l"], (0, 2), VimState.VISUAL_CHAR),
        (["V", "j"], (1, 10), VimState.VISUAL_LINE),
    ],
)
def test_vim_normal_insert_visual_tables(keys: list[str], cursor: Location, state: VimState) -> None:
    keymap, host = _run_vim(keys)
    assert keymap.state is state
    assert host.cursor == cursor


def test_vim_counts_leading_zero_multiplication_and_clamp() -> None:
    keymap, host = _run_vim(["2", "l"])
    assert host.cursor == (0, 2)

    keymap, host = _run_vim(["0"], cursor=(0, 2))
    assert host.cursor == (0, 0)

    keymap, host = _run_vim(["2", "d", "2", "w"], text="one two three four five")
    assert host.text == "five"

    keymap = VimKeymap()
    host = _Host("text")
    for key in "999999":
        keymap.handle_key(_stroke(key), host)
    assert keymap.count == MAX_VIM_COUNT


def test_vim_counted_line_end_document_lines_and_word_objects() -> None:
    _keymap, host = _run_vim(["2", "$"], "one\ntwo\nthree")
    assert host.cursor == (1, 2)

    _keymap, host = _run_vim(["2", "G"], "one\ntwo\nthree")
    assert host.cursor == (1, 0)

    _keymap, host = _run_vim(["d", "2", "G"], "one\ntwo\nthree", (2, 0))
    assert host.text == "one"

    _keymap, host = _run_vim(["d", "2", "i", "w"], "one two three")
    assert host.text == " three"


@pytest.mark.parametrize(
    ("keys", "expected_cursor", "expected_text"),
    [
        (["e"], (0, 6), "one two three"),
        (["2", "e"], (0, 12), "one two three"),
        (["d", "e"], (0, 2), "on three"),
        (["d", "2", "e"], (0, 1), "on"),
    ],
)
def test_vim_word_end_advances_from_an_existing_word_end(
    keys: list[str],
    expected_cursor: Location,
    expected_text: str,
) -> None:
    _keymap, host = _run_vim(keys, "one two three", (0, 2))

    assert host.cursor == expected_cursor
    assert host.text == expected_text


def test_vim_operator_rejects_unadvertised_vertical_motion_and_s_handles_last_character() -> None:
    keymap, host = _run_vim(["d", "j"], "one\ntwo")
    assert host.text == "one\ntwo"
    assert keymap.state is VimState.NORMAL

    keymap, host = _run_vim(["s"], "one", (0, 2))
    assert host.text == "on"
    assert keymap.state is VimState.INSERT


@pytest.mark.parametrize("keys", [["D"], ["d", "$"], ["y", "$"], ["C"], ["c", "$"]])
def test_vim_line_end_operator_does_not_consume_newline_from_empty_line(keys: list[str]) -> None:
    keymap, host = _run_vim(keys, "abc\n\ndef", (1, 0))

    assert host.text == "abc\n\ndef"
    assert keymap.state is (VimState.INSERT if keys[0] in {"C", "c"} else VimState.NORMAL)


def test_vim_noop_character_operator_preserves_unnamed_register() -> None:
    keymap, host = _run_vim(["y", "y", "j", "d", "l"], "abc\n\ndef")

    assert host.text == "abc\n\ndef"
    assert keymap.register.text == "abc\n"
    assert keymap.register.kind is EditorRegisterKind.LINEWISE


@pytest.mark.parametrize(
    ("keys", "text", "cursor", "state"),
    [
        (["I"], "  one", (0, 2), VimState.INSERT),
        (["a"], "one", (0, 1), VimState.INSERT),
        (["A"], "one", (0, 3), VimState.INSERT),
        (["o"], "one\n", (1, 0), VimState.INSERT),
        (["O"], "\none", (0, 0), VimState.INSERT),
        (["2", "x"], "e", (0, 0), VimState.NORMAL),
        (["X"], "ne", (0, 0), VimState.NORMAL),
        (["~"], "One", (0, 1), VimState.NORMAL),
        (["ctrl+d"], "one\ntwo\nthree\nfour", (3, 0), VimState.NORMAL),
        (["ctrl+u"], "one\ntwo\nthree\nfour", (0, 0), VimState.NORMAL),
    ],
)
def test_vim_remaining_normal_table(
    keys: list[str],
    text: str,
    cursor: Location,
    state: VimState,
) -> None:
    start_cursor = (3, 0) if keys == ["ctrl+u"] else (0, 1) if keys == ["X"] else (0, 0)
    keymap, host = _run_vim(keys, "one\ntwo\nthree\nfour" if keys[0].startswith("ctrl+") else "one", start_cursor)
    if keys in (["I"], ["a"], ["A"], ["o"], ["O"]):
        keymap, host = _run_vim(keys, "  one" if keys == ["I"] else "one")
    assert host.text == text
    assert host.cursor == cursor
    assert keymap.state is state


def test_vim_unnamed_linewise_register_p_and_p_before() -> None:
    keymap, host = _run_vim(["y", "y", "j", "p"], "one\ntwo")
    assert host.text == "one\ntwo\none"
    assert keymap.register.kind is EditorRegisterKind.LINEWISE

    keymap, host = _run_vim(["y", "y", "j", "P"], "one\ntwo")
    assert host.text == "one\none\ntwo"


def test_vim_linewise_yank_at_eof_keeps_logical_row_for_paste_before() -> None:
    keymap, host = _run_vim(["y", "y", "P"], "one\ntwo", (1, 0))

    assert host.text == "one\ntwo\ntwo"
    assert host.cursor == (1, 0)
    assert keymap.register.text == "two\n"


def test_vim_counted_blank_linewise_paste_at_eof_preserves_every_line() -> None:
    keymap, host = _run_vim(["y", "y", "2", "p"], "")

    assert host.text == "\n\n"
    assert host.text.split("\n") == ["", "", ""]
    assert keymap.register.text == "\n"


def test_vim_counted_paste_rejects_oversize_result_before_building_payload() -> None:
    original = "x" * 100
    keymap = VimKeymap()
    host = _Host(original)
    for key in ("y", "y", "9", "9", "9", "9"):
        keymap.handle_key(_stroke(key), host)

    result = keymap.handle_key(_stroke("p"), host)

    assert _render_status(result.status) == format_message(MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS.bind())
    assert host.text == original
    assert host.edits == 0


@pytest.mark.parametrize(
    ("motions", "expected_selection", "expected_selected", "expected_after_delete"),
    [
        ([], EditorTextRange((0, 0), (1, 0)), "one\n", "two"),
        (["j"], EditorTextRange((0, 0), (1, 3)), "one\ntwo", ""),
    ],
)
def test_vim_visual_line_selection_matches_the_range_applied_by_delete(
    motions: list[str],
    expected_selection: EditorTextRange,
    expected_selected: str,
    expected_after_delete: str,
) -> None:
    keymap = VimKeymap()
    host = _Host("one\ntwo")

    keymap.handle_key(_stroke("V"), host)
    for motion in motions:
        keymap.handle_key(_stroke(motion), host)

    assert host.editor_selection == expected_selection
    assert host.read_editor_range(host.editor_selection) == expected_selected

    keymap.handle_key(_stroke("d"), host)

    expected_register = expected_selected if expected_selected.endswith("\n") else expected_selected + "\n"
    assert keymap.register.text == expected_register
    assert host.text == expected_after_delete


def test_vim_visual_line_escape_restores_the_logical_active_cursor() -> None:
    keymap = VimKeymap()
    host = _Host("one\ntwo")

    for key in ["V", "j"]:
        keymap.handle_key(_stroke(key), host)
    assert host.cursor == (1, 3)

    keymap.handle_key(_stroke("escape"), host)

    assert keymap.state is VimState.NORMAL
    assert host.cursor == (1, 0)
    assert host.editor_selection == EditorTextRange((1, 0), (1, 0))


@pytest.mark.parametrize(
    ("keys", "expected_selected"),
    [
        (["v"], "o"),
        (["v", "l"], "on"),
        (["l", "l", "v", "h"], "ne"),
    ],
)
def test_vim_visual_char_rendered_selection_matches_operated_range(
    keys: list[str],
    expected_selected: str,
) -> None:
    keymap = VimKeymap()
    host = _Host("one")

    for key in keys:
        keymap.handle_key(_stroke(key), host)

    assert host.read_editor_range(host.editor_selection) == expected_selected
    keymap.handle_key(_stroke("y"), host)
    assert keymap.register.text == expected_selected


@pytest.mark.parametrize("visual", ["v", "V"])
@pytest.mark.parametrize("operation", ["y", "d", "x", "c"])
def test_vim_visual_operations_and_anchor_swap(visual: str, operation: str) -> None:
    keymap, _host = _run_vim([visual, "l", "o", "o", operation], "one\ntwo")
    assert keymap.state is (VimState.INSERT if operation == "c" else VimState.NORMAL)
    assert keymap.register.text


@pytest.mark.parametrize(
    ("alias", "expanded"),
    [("D", ["d", "$"]), ("C", ["c", "$"]), ("Y", ["y", "y"]), ("S", ["c", "c"]), ("s", ["c", "l"])],
)
@pytest.mark.parametrize("prefix", [[], ["2"]])
def test_vim_aliases_use_operator_range_machinery(alias: str, expanded: list[str], prefix: list[str]) -> None:
    text = "alpha beta\ngamma delta\nomega"
    alias_keymap, alias_host = _run_vim([*prefix, alias], text, (0, 2))
    expanded_keymap, expanded_host = _run_vim([*prefix, *expanded], text, (0, 2))
    assert alias_host.text == expanded_host.text
    assert alias_keymap.register == expanded_keymap.register
    assert alias_keymap.state is expanded_keymap.state


@pytest.mark.parametrize("operator", ["d", "c", "y"])
@pytest.mark.parametrize("motion", ["h", "l", "w", "b", "e", "0", "^", "$", "G"])
def test_vim_every_advertised_operator_motion_completes(operator: str, motion: str) -> None:
    keymap, host = _run_vim([operator, motion], "  one two\nthree", (0, 3))
    assert keymap.state is (VimState.INSERT if operator == "c" else VimState.NORMAL)
    assert host.edits == (0 if operator == "y" else 1)


@pytest.mark.parametrize("operator", ["d", "c", "y"])
def test_vim_operator_gg_uses_the_document_edge(operator: str) -> None:
    keymap, host = _run_vim([operator, "g", "g"], "one\ntwo\nthree", (2, 0))
    assert keymap.state is (VimState.INSERT if operator == "c" else VimState.NORMAL)
    assert host.edits == (0 if operator == "y" else 1)


@pytest.mark.parametrize(
    ("sequence", "expected", "kind"),
    [
        (["d", "d"], "two\nthree", EditorRegisterKind.LINEWISE),
        (["c", "c"], "\ntwo\nthree", EditorRegisterKind.LINEWISE),
        (["y", "y"], "one\ntwo\nthree", EditorRegisterKind.LINEWISE),
        (["3", "d", "d"], "", EditorRegisterKind.LINEWISE),
    ],
)
def test_vim_repeated_line_operators(sequence: list[str], expected: str, kind: EditorRegisterKind) -> None:
    keymap, host = _run_vim(sequence, "one\ntwo\nthree")
    assert host.text == expected
    assert keymap.register.kind is kind


def test_vim_change_last_and_empty_lines_preserves_a_blank_insert_target() -> None:
    keymap, host = _run_vim(["c", "c"], "one\ntwo", (1, 0))
    assert host.text == "one\n"
    assert host.cursor == (1, 0)
    assert keymap.state is VimState.INSERT

    keymap, host = _run_vim(["c", "c"], "", (0, 0))
    assert host.text == ""
    assert host.cursor == (0, 0)
    assert keymap.state is VimState.INSERT


@pytest.mark.parametrize(
    ("scope", "object_key", "text", "cursor", "expected"),
    [
        ("i", "w", "say alpha now", (0, 5), "say  now"),
        ("a", "w", "say alpha now", (0, 5), "say now"),
        ("i", "W", "say a-b now", (0, 5), "say  now"),
        ("a", "W", "say a-b now", (0, 5), "say now"),
        ("i", '"', 'say "alpha" now', (0, 6), 'say "" now'),
        ("a", '"', 'say "alpha" now', (0, 6), "say  now"),
        ("a", "'", "say 'alpha' now", (0, 6), "say  now"),
        ("i", "'", "say 'alpha' now", (0, 6), "say '' now"),
        ("i", "`", "say `alpha` now", (0, 6), "say `` now"),
        ("i", "(", "a (one (two)) z", (0, 9), "a (one ()) z"),
        ("a", "b", "a (one) z", (0, 4), "a  z"),
        ("i", "[", "a [one] z", (0, 4), "a [] z"),
        ("a", "{", "a {one} z", (0, 4), "a  z"),
        ("i", "B", "a {one} z", (0, 4), "a {} z"),
    ],
)
def test_vim_text_objects(scope: str, object_key: str, text: str, cursor: Location, expected: str) -> None:
    keymap, host = _run_vim(["d", scope, object_key], text, cursor)
    assert keymap.state is VimState.NORMAL
    assert host.text == expected


def test_vim_absent_or_outside_text_object_is_non_destructive() -> None:
    keymap, host = _run_vim(["d", "i", "("], "outside (inside)", (0, 0))
    assert host.text == "outside (inside)"
    assert host.edits == 0
    assert keymap.state is VimState.NORMAL


@pytest.mark.parametrize(
    ("keys", "expected_state"),
    [
        ([], VimState.NORMAL),
        (["i"], VimState.INSERT),
        (["v"], VimState.VISUAL_CHAR),
        (["V"], VimState.VISUAL_LINE),
        (["d"], VimState.OPERATOR_PENDING),
        (["d", "i"], VimState.TEXT_OBJECT_PENDING),
        (["r"], VimState.REPLACE_PENDING),
        ([":"], VimState.COMMAND),
    ],
)
def test_vim_escape_resets_every_state(keys: list[str], expected_state: VimState) -> None:
    keymap, host = _run_vim(keys)
    assert keymap.state is expected_state
    keymap.handle_key(_stroke("escape"), host)
    assert keymap.state is VimState.NORMAL


@pytest.mark.parametrize("keys", [[], ["i"], ["v"], ["V"], ["d"], ["d", "i"], ["r"], [":"]])
def test_vim_ctrl_o_accepts_from_every_state(keys: list[str]) -> None:
    keymap, host = _run_vim(keys)
    result = keymap.handle_key(_stroke("ctrl+o"), host)
    assert result.intent is EditorIntent.ACCEPT


@pytest.mark.parametrize(
    ("command", "dirty", "intent", "status"),
    [
        ("wq", False, EditorIntent.ACCEPT, ""),
        ("x", True, EditorIntent.ACCEPT, ""),
        ("q!", True, EditorIntent.CANCEL_IMMEDIATE, ""),
        ("q", False, EditorIntent.CANCEL_IMMEDIATE, ""),
        ("q", True, EditorIntent.NONE, "No write since last change; use :q!"),
        ("w", True, EditorIntent.NONE, "Not an editor command: w"),
        ("write", True, EditorIntent.NONE, "Not an editor command: write"),
    ],
)
def test_vim_limited_command_line(command: str, dirty: bool, intent: EditorIntent, status: str) -> None:
    entry = "entry"
    host = _Host("dirty" if dirty else entry, entry_text=entry)
    keymap = VimKeymap()
    keymap.handle_key(_stroke(":"), host)
    for character in command:
        keymap.handle_key(_stroke(character), host)
    result = keymap.handle_key(_stroke("enter"), host)
    assert result.intent is intent
    assert _render_status(result.status) == status
    assert host.text == ("dirty" if dirty else entry)


def test_vim_command_edit_abandon_zz_zq_and_ctrl_q_contract() -> None:
    keymap, host = _run_vim([":", "q", "x", "backspace"])
    assert keymap.command_buffer == "q"
    keymap.handle_key(_stroke("escape"), host)
    assert keymap.state is VimState.NORMAL

    keymap, host = _run_vim(["Z"])
    assert keymap.handle_key(_stroke("Z"), host).intent is EditorIntent.ACCEPT
    keymap, host = _run_vim(["Z"])
    assert keymap.handle_key(_stroke("Q"), host).intent is EditorIntent.CANCEL_IMMEDIATE

    keymap = VimKeymap()
    result = keymap.handle_key(_stroke("ctrl+q"), _Host("text"))
    assert result.consumed is False


@pytest.mark.parametrize(
    "sequence",
    [
        ["c", "w"],
        ["3", "d", "d"],
        ["c", "i", '"'],
        ["J"],
        ["r", "X"],
        ["D"],
        ["C"],
        ["Y"],
        ["S"],
        ["s"],
    ],
)
def test_vim_compound_mutations_are_one_host_edit_and_one_undo(sequence: list[str]) -> None:
    text = 'one "two" three\nfour\nfive'
    _keymap, host = _run_vim(sequence, text, (0, 5))
    if sequence == ["Y"]:
        assert host.edits == 0
        return
    assert host.edits == 1
    changed = host.text
    host.editor_undo()
    assert host.text == text
    host.editor_redo()
    assert host.text == changed


@pytest.mark.parametrize(
    "sequence",
    [["c", "w"], ["3", "d", "d"], ["c", "i", '"'], ["J"], ["r", "X"], ["D"], ["C"], ["S"], ["s"]],
)
def test_vim_undo_does_not_cross_an_earlier_edit(sequence: list[str]) -> None:
    host = _Host('one "two" three\nfour\nfive', (0, 5))
    end = _location(host.text, len(host.text))
    host.replace_editor_range(EditorTextRange(end, end), "!")
    seeded = host.text
    keymap = VimKeymap()
    host.move_editor_cursor((0, 5))
    for key in sequence:
        keymap.handle_key(_stroke(key), host)
    host.editor_undo()
    assert host.text == seeded


@dataclass
class _AtomicHost(_Host):
    spans: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    replacement_batches: list[tuple[str, tuple[tuple[EditorTextRange, str], ...]]] = field(default_factory=list)

    def editor_unit_spans(self) -> list[tuple[int, int]]:
        return [
            (_offset(self.text, (row, low)), _offset(self.text, (row, high)))
            for row, spans in self.spans.items()
            for low, high in spans
        ]

    def replace_editor_ranges(self, replacements: Sequence[tuple[EditorTextRange, str]]) -> None:
        self.replacement_batches.append((self.text, tuple(replacements)))
        super().replace_editor_ranges(replacements)


def _assert_preserving_writer_contract(host: _AtomicHost) -> None:
    for original, replacements in host.replacement_batches:
        previous_end = (0, 0)
        for source_range, replacement in replacements:
            assert previous_end <= source_range.start <= source_range.end
            previous_end = source_range.end
            start = _offset(original, source_range.start)
            end = _offset(original, source_range.end)
            if original[start:end] == replacement:
                continue
            for row, spans in host.spans.items():
                for low, high in spans:
                    unit_start = _offset(original, (row, low))
                    unit_end = _offset(original, (row, high))
                    assert end <= unit_start or start >= unit_end


@pytest.mark.parametrize(
    ("text", "spans", "count", "expected", "cursor"),
    [
        ("argument 1 z", [(0, 10)], 1, "argument 1 z", 10),
        ("ß argument 1 z", [(2, 12)], 3, "SS argument 1 z", 13),
        ("ﬃargument 1İ z", [(1, 11)], 3, "FFIargument 1i̇ z", 15),
        ("İargument 1ﬃ z", [(1, 11)], 3, "i̇argument 1FFI z", 15),
        ("aFIELD[L]z", [(1, 6), (6, 9)], 3, "AFIELD[L]z", 9),
        ("aFIELD[L]z", [(1, 6), (6, 9)], 5, "AFIELD[L]Z", 9),
        ("aFIELD", [(1, 6)], 5, "AFIELD", 1),
        ("xabcdefghz", [(4, 9), (1, 6)], 3, "XabcdefghZ", 9),
    ],
)
def test_vim_toggle_skips_atomic_units_and_uses_written_character_lengths(
    text: str, spans: list[tuple[int, int]], count: int, expected: str, cursor: int
) -> None:
    host = _AtomicHost(text, spans={0: spans})
    keymap = VimKeymap()
    for key in [*str(count), "~"]:
        keymap.handle_key(_stroke(key), host)

    assert host.text == expected
    assert host.cursor == (0, cursor)
    assert host.edits == 1
    assert len(host.replacement_batches) == 1
    _assert_preserving_writer_contract(host)
    keymap.handle_key(_stroke("u"), host)
    assert host.text == text
    keymap.handle_key(_stroke("ctrl+r"), host)
    assert host.text == expected


def test_vim_toggle_only_units_keeps_its_own_unchanged_undo_step() -> None:
    host = _AtomicHost("FIELD", spans={0: [(0, 5)]})
    host.replace_editor_range(EditorTextRange((0, 5), (0, 5)), "!")
    host.move_editor_cursor((0, 0))
    keymap = VimKeymap()
    keymap.handle_key(_stroke("~"), host)
    assert host.text == "FIELD!"
    assert host.edits == 2
    keymap.handle_key(_stroke("u"), host)
    assert host.text == "FIELD!"
    keymap.handle_key(_stroke("u"), host)
    assert host.text == "FIELD"


@pytest.mark.parametrize(
    ("text", "spans", "count", "expected", "cursor"),
    [
        ("ab \nargument 1", {1: [(0, 10)]}, 2, "ab argument 1", 2),
        ("ab\n  argument 1", {1: [(2, 12)]}, 2, "ab argument 1", 2),
        ("ab\n\targument 1", {1: [(1, 11)]}, 2, "ab argument 1", 2),
        ("ab\n  \ncd", {}, 3, "ab  cd", 2),
        ("FIELD  \n [L]\t\n  LAST  ", {0: [(0, 5)], 1: [(1, 4)], 2: [(2, 6)]}, 3, "FIELD [L] LAST", 5),
        ("ab\n   ", {}, 2, "ab ", 2),
        (" \n\t\n", {}, 3, "  ", 0),
        ("ab\n\ncd\t", {}, 3, "ab  cd", 2),
    ],
)
def test_vim_join_edits_only_seams_and_final_trailing_whitespace(
    text: str, spans: dict[int, list[tuple[int, int]]], count: int, expected: str, cursor: int
) -> None:
    host = _AtomicHost(text, spans=spans)
    keymap = VimKeymap()
    for key in [*str(count), "J"]:
        keymap.handle_key(_stroke(key), host)
    assert host.text == expected
    assert host.cursor == (0, cursor)
    assert host.edits == 1
    assert len(host.replacement_batches) == 1
    _assert_preserving_writer_contract(host)
    keymap.handle_key(_stroke("u"), host)
    assert host.text == text
    keymap.handle_key(_stroke("ctrl+r"), host)
    assert host.text == expected


def test_vim_join_small_exhaustive_inputs_match_existing_text_and_cursor_rules() -> None:
    fragments = ["", "a", " a ", "\ta\t", " ", "\t"]
    for parts in product(fragments, repeat=3):
        for row in (0, 1):
            for count in (2, 3):
                text = "\n".join(parts)
                _keymap, host = _run_vim([*str(count), "J"], text, (row, 0))
                end_row = min(row + count - 1, len(parts) - 1)
                joined = " ".join([parts[row].rstrip(), *(line.strip() for line in parts[row + 1 : end_row + 1])])
                expected = "\n".join([*parts[:row], joined, *parts[end_row + 1 :]])
                assert host.text == expected, (parts, row, count)
                assert host.cursor == (row, min(len(parts[row].rstrip()), max(len(joined) - 1, 0)))


def test_vim_join_last_line_does_not_write_or_add_an_undo_step() -> None:
    keymap, host = _run_vim(["J"], "first\nlast", (1, 2))
    assert host.text == "first\nlast"
    assert host.cursor == (1, 2)
    assert host.edits == 0
    assert keymap.state is VimState.NORMAL


@pytest.mark.parametrize(
    "text",
    ["ab\ncd\nef", "ab \n  \ncd", " \n\t\n", " ab \n  cd  \n\tef\t", "ab\ncd\n   "],
)
def test_vim_join_coalesces_plain_text_seams_into_one_replacement(text: str) -> None:
    host = _AtomicHost(text)
    keymap = VimKeymap()
    for key in "3J":
        keymap.handle_key(_stroke(key), host)
    parts = text.split("\n")
    assert host.text == " ".join([parts[0].rstrip(), *(part.strip() for part in parts[1:])])
    assert len(host.replacement_batches) == 1
    assert len(host.replacement_batches[0][1]) == 1
    assert host.replacement_batches[0][1][0][0] == EditorTextRange((0, 0), (2, len(parts[-1])))
    assert host.edits == 1
    keymap.handle_key(_stroke("u"), host)
    assert host.text == text


@pytest.mark.parametrize(
    ("protected_rows", "expected_replacements"),
    [((), 1), ((0,), 1), ((2,), 2), ((4,), 2), ((0, 2, 4), 3), ((0, 1, 2, 3, 4), 5)],
)
def test_vim_join_breaks_coalesced_replacements_only_at_lines_with_units(
    protected_rows: tuple[int, ...], expected_replacements: int
) -> None:
    lines = ["ab ", "  one  ", "\ttwo\t", "  three  ", "  four  "]
    spans = {row: [(len(lines[row]) - len(lines[row].lstrip()), len(lines[row].rstrip()))] for row in protected_rows}
    text = "\n".join(lines)
    host = _AtomicHost(text, spans=spans)
    keymap = VimKeymap()
    for key in "5J":
        keymap.handle_key(_stroke(key), host)
    assert host.text == "ab one two three four"
    assert host.cursor == (0, 2)
    assert len(host.replacement_batches) == 1
    assert len(host.replacement_batches[0][1]) == expected_replacements
    assert host.replacement_batches[0][1][0][0].start == (0, 2 if 0 in protected_rows else 0)
    _assert_preserving_writer_contract(host)
    keymap.handle_key(_stroke("u"), host)
    assert host.text == text
    keymap.handle_key(_stroke("ctrl+r"), host)
    assert host.text == "ab one two three four"


@pytest.mark.parametrize("keys", [["o"], ["O"], ["y", "y", "p"], ["y", "y", "P"]])
def test_vim_other_inserting_writers_leave_units_outside_their_ranges(keys: list[str]) -> None:
    host = _AtomicHost("FIELD rest\nlast", spans={0: [(0, 5)]})
    keymap = VimKeymap()
    for key in keys:
        keymap.handle_key(_stroke(key), host)
    assert host.edits == 1
    _assert_preserving_writer_contract(host)


@pytest.mark.parametrize(
    "keys",
    [["x"], ["d", "w"], ["c", "w"], ["D"], ["C"], ["s"], ["S"], ["d", "i", "w"], ["r", "Q"], ["v", "d"], ["v", "c"]],
)
def test_vim_other_replacing_writers_intend_to_remove_touched_units(keys: list[str]) -> None:
    host = _AtomicHost("FIELD rest", spans={0: [(0, 5)]})
    keymap = VimKeymap()
    for key in keys:
        keymap.handle_key(_stroke(key), host)
    assert host.edits == 1
    assert len(host.replacement_batches) == 1
    original, replacements = host.replacement_batches[0]
    for source_range, replacement in replacements:
        assert _offset(original, source_range.start) < 5
        assert _offset(original, source_range.end) > 0
        assert replacement in {"", "Q"}


@pytest.mark.parametrize("keys", [["ctrl+d"], ["alt+d"], ["ctrl+k"], ["ctrl+space", "alt+f", "ctrl+w"]])
def test_emacs_deleting_writers_intend_to_remove_touched_units(keys: list[str]) -> None:
    host = _AtomicHost("FIELD rest", spans={0: [(0, 5)]})
    keymap = EmacsKeymap()
    for key in keys:
        keymap.handle_key(_stroke(key), host)
    assert host.edits == 1
    for original, replacements in host.replacement_batches:
        for source_range, replacement in replacements:
            assert _offset(original, source_range.start) < 5
            assert _offset(original, source_range.end) > 0
            assert replacement == ""


def test_emacs_yank_inserts_next_to_units_without_rewriting_them() -> None:
    host = _AtomicHost("FIELD rest", cursor=(0, 6), spans={0: [(0, 5)]})
    keymap = EmacsKeymap()
    keymap.handle_key(_stroke("ctrl+k"), host)
    host.replacement_batches.clear()
    host.move_editor_cursor((0, 0))
    keymap.handle_key(_stroke("ctrl+y"), host)
    assert host.text == "restFIELD "
    _assert_preserving_writer_contract(host)
