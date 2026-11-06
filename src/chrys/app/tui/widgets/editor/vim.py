# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure Vim keymap state machine for the modal message editor."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from chrys.app.tui.widgets.editor.coordinates import location_to_offset, offset_to_location
from chrys.app.tui.widgets.editor.types import (
    COMMON_EDITOR_INTENTS,
    MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS,
    MESSAGE_EDITOR_MAX_CHARACTERS,
    EditorCommand,
    EditorCommandHost,
    EditorIntent,
    EditorKeyResult,
    EditorKeyStroke,
    EditorLocation,
    EditorMode,
    EditorRegister,
    EditorRegisterKind,
    EditorStatus,
    EditorTextRange,
)
from chrys.foundation.i18n import msg

MAX_VIM_COUNT: Final = 9_999

_VIM_MODE_NORMAL = msg("tui.editor.vim.mode.normal", fallback="Normal")
_VIM_MODE_INSERT = msg("tui.editor.vim.mode.insert", fallback="Insert")
_VIM_MODE_VISUAL = msg("tui.editor.vim.mode.visual", fallback="Visual")
_VIM_MODE_VISUAL_LINE = msg("tui.editor.vim.mode.visual_line", fallback="Visual Line")
_VIM_NO_WRITE = msg(
    "tui.editor.vim.no_write_since_change",
    fallback="No write since last change; use :q!",
)
_VIM_NOT_EDITOR_COMMAND = msg(
    "tui.editor.vim.not_editor_command",
    fallback="Not an editor command: {command}",
)


class VimState(StrEnum):
    """Finite states supported by the v1 Vim keymap."""

    NORMAL = "normal"
    INSERT = "insert"
    VISUAL_CHAR = "visual_char"
    VISUAL_LINE = "visual_line"
    OPERATOR_PENDING = "operator_pending"
    TEXT_OBJECT_PENDING = "text_object_pending"
    REPLACE_PENDING = "replace_pending"
    COMMAND = "command"


class VimOperator(StrEnum):
    """Supported Vim operators."""

    DELETE = "d"
    CHANGE = "c"
    YANK = "y"


class VimMotion(StrEnum):
    """Motions accepted in Normal, Visual, and operator-pending states."""

    LEFT = "h"
    RIGHT = "l"
    DOWN = "j"
    UP = "k"
    LINE_START = "0"
    FIRST_NON_BLANK = "^"
    LINE_END = "$"
    WORD_NEXT = "w"
    WORD_PREVIOUS = "b"
    WORD_END = "e"
    DOCUMENT_START = "gg"
    DOCUMENT_END = "G"


class VimTextObject(StrEnum):
    """Text objects supported by v1."""

    WORD = "w"
    WORD_BIG = "W"
    DOUBLE_QUOTE = '"'
    SINGLE_QUOTE = "'"
    BACKTICK = "`"
    PAREN = "("
    BRACKET = "["
    BRACE = "{"


@dataclass(frozen=True, slots=True)
class _MotionTarget:
    location: EditorLocation
    inclusive: bool = False
    linewise: bool = False


_MOTIONS = MappingProxyType(
    {
        "h": VimMotion.LEFT,
        "left": VimMotion.LEFT,
        "l": VimMotion.RIGHT,
        "right": VimMotion.RIGHT,
        "j": VimMotion.DOWN,
        "down": VimMotion.DOWN,
        "k": VimMotion.UP,
        "up": VimMotion.UP,
        "0": VimMotion.LINE_START,
        "^": VimMotion.FIRST_NON_BLANK,
        "circumflex_accent": VimMotion.FIRST_NON_BLANK,
        "$": VimMotion.LINE_END,
        "dollar_sign": VimMotion.LINE_END,
        "w": VimMotion.WORD_NEXT,
        "b": VimMotion.WORD_PREVIOUS,
        "e": VimMotion.WORD_END,
        "G": VimMotion.DOCUMENT_END,
    }
)

_OPERATOR_MOTIONS = frozenset(
    {
        VimMotion.LEFT,
        VimMotion.RIGHT,
        VimMotion.LINE_START,
        VimMotion.FIRST_NON_BLANK,
        VimMotion.LINE_END,
        VimMotion.WORD_NEXT,
        VimMotion.WORD_PREVIOUS,
        VimMotion.WORD_END,
        VimMotion.DOCUMENT_END,
    }
)

_OPERATORS = MappingProxyType(
    {
        "d": VimOperator.DELETE,
        "c": VimOperator.CHANGE,
        "y": VimOperator.YANK,
    }
)

_INSERT_COMMANDS = frozenset({"i", "I", "a", "A", "o", "O"})
_ALIASES = MappingProxyType(
    {
        "D": (VimOperator.DELETE, VimMotion.LINE_END, False),
        "C": (VimOperator.CHANGE, VimMotion.LINE_END, False),
        "Y": (VimOperator.YANK, None, True),
        "S": (VimOperator.CHANGE, None, True),
        "s": (VimOperator.CHANGE, VimMotion.RIGHT, False),
    }
)

_TEXT_OBJECTS = MappingProxyType(
    {
        "w": VimTextObject.WORD,
        "W": VimTextObject.WORD_BIG,
        '"': VimTextObject.DOUBLE_QUOTE,
        "'": VimTextObject.SINGLE_QUOTE,
        "`": VimTextObject.BACKTICK,
        "(": VimTextObject.PAREN,
        "b": VimTextObject.PAREN,
        "[": VimTextObject.BRACKET,
        "{": VimTextObject.BRACE,
        "B": VimTextObject.BRACE,
    }
)


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _after_character(host: EditorCommandHost, row: int, column: int, line_length: int) -> int:
    """The column after the character under the cursor; an atomic span (a live label) is one character."""
    after = next((high for low, high in host.editor_atomic_spans(row) if low == column), column + 1)
    return min(after, line_length)


def _normal_location(text: str, location: EditorLocation) -> EditorLocation:
    lines = _lines(text)
    row = min(max(location[0], 0), len(lines) - 1)
    line = lines[row]
    return row, min(max(location[1], 0), max(len(line) - 1, 0))


def _first_non_blank(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_word(character: str) -> bool:
    return character.isalnum() or character == "_"


def _token_class(character: str, *, big: bool = False) -> int:
    if character.isspace():
        return 0
    if big or _is_word(character):
        return 1
    return 2


def _next_word_start(text: str, offset: int, count: int) -> int:
    index = min(max(offset, 0), len(text))
    for _ in range(count):
        if index >= len(text):
            break
        character_class = _token_class(text[index])
        while index < len(text) and _token_class(text[index]) == character_class and character_class != 0:
            index += 1
        while index < len(text) and text[index].isspace():
            index += 1
        if character_class == 0 and index < len(text):
            continue
    return index


def _previous_word_start(text: str, offset: int, count: int) -> int:
    index = min(max(offset, 0), len(text))
    for _ in range(count):
        index = max(index - 1, 0)
        while index > 0 and text[index].isspace():
            index -= 1
        character_class = _token_class(text[index]) if text else 0
        while index > 0 and _token_class(text[index - 1]) == character_class and character_class != 0:
            index -= 1
    return index


def _word_end(text: str, offset: int, count: int) -> int:
    if not text:
        return 0
    index = min(max(offset, 0), len(text) - 1)
    for _ in range(count):
        character_class = _token_class(text[index])
        has_later_character_in_token = (
            character_class != 0 and index < len(text) - 1 and _token_class(text[index + 1]) == character_class
        )
        if not has_later_character_in_token:
            if index < len(text) - 1:
                index += 1
            while index < len(text) - 1 and text[index].isspace():
                index += 1
        character_class = _token_class(text[index])
        while index < len(text) - 1 and _token_class(text[index + 1]) == character_class and character_class != 0:
            index += 1
    return index


def _advance_location(text: str, location: EditorLocation, amount: int = 1) -> EditorLocation:
    return offset_to_location(text, location_to_offset(text, location) + amount)


def _range_from_offsets(text: str, start: int, end: int) -> EditorTextRange:
    low, high = sorted((start, end))
    return EditorTextRange(offset_to_location(text, low), offset_to_location(text, high))


class VimKeymap:
    """Vim v1 state machine with one unnamed register."""

    def __init__(self) -> None:
        self._state = VimState.NORMAL
        self._count = 0
        self._operator: VimOperator | None = None
        self._operator_count = 1
        self._text_object_scope = ""
        self._prefix = ""
        self._command_buffer = ""
        self._register = EditorRegister("")
        self._visual_anchor: EditorLocation | None = None
        self._visual_active: EditorLocation | None = None

    @property
    def mode(self) -> EditorMode:
        return EditorMode.VIM

    @property
    def state(self) -> VimState:
        return self._state

    @property
    def count(self) -> int:
        return self._count

    @property
    def register(self) -> EditorRegister:
        return self._register

    @property
    def command_buffer(self) -> str:
        return self._command_buffer

    @property
    def state_label(self) -> EditorStatus:
        if self._state is VimState.COMMAND:
            return f"Command :{self._command_buffer}"
        pending = self._pending_label()
        if pending:
            return f"Normal · Pending: {pending}"
        labels = {
            VimState.NORMAL: _VIM_MODE_NORMAL,
            VimState.INSERT: _VIM_MODE_INSERT,
            VimState.VISUAL_CHAR: _VIM_MODE_VISUAL,
            VimState.VISUAL_LINE: _VIM_MODE_VISUAL_LINE,
            VimState.OPERATOR_PENDING: _VIM_MODE_NORMAL,
            VimState.TEXT_OBJECT_PENDING: _VIM_MODE_NORMAL,
            VimState.REPLACE_PENDING: _VIM_MODE_NORMAL,
        }
        return labels[self._state].bind()

    def handle_key(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        if stroke.character is not None and len(stroke.character) == 1 and stroke.character.isprintable():
            stroke = EditorKeyStroke(stroke.character, stroke.character)
        if stroke.key == "ctrl+q":
            return EditorKeyResult()
        if stroke.key == "ctrl+insert":
            return EditorKeyResult()
        if stroke.key in {"tab", "shift+tab"}:
            return EditorKeyResult()
        if intent := COMMON_EDITOR_INTENTS.get(stroke.key):
            return self._result(intent)
        if self._state is VimState.INSERT:
            return self._handle_insert(stroke, host)
        if self._state is VimState.COMMAND:
            return self._handle_command(stroke, host)
        if stroke.key == "escape":
            self._return_normal(host)
            return self._result()
        if self._state in {VimState.VISUAL_CHAR, VimState.VISUAL_LINE}:
            return self._handle_visual(stroke, host)
        if self._state is VimState.REPLACE_PENDING:
            return self._handle_replace(stroke, host)
        if self._state is VimState.TEXT_OBJECT_PENDING:
            return self._handle_text_object(stroke, host)
        if self._state is VimState.OPERATOR_PENDING:
            return self._handle_operator_pending(stroke, host)
        return self._handle_normal(stroke, host)

    def reset(self) -> None:
        self._state = VimState.NORMAL
        self._count = 0
        self._operator = None
        self._operator_count = 1
        self._text_object_scope = ""
        self._prefix = ""
        self._command_buffer = ""
        self._register = EditorRegister("")
        self._visual_anchor = None
        self._visual_active = None

    def normalize_cursor(self, host: EditorCommandHost) -> None:
        """Keep the visible cursor on a Vim Normal character position."""
        self._normalize_host_cursor(host)

    def prepare_delegated_edit(self) -> None:
        """Insert-mode edits do not invalidate Vim's modal state."""

    def prepare_delegated_paste(self) -> bool:
        """Permit direct document paste only while Vim is in Insert state."""
        return self._state is VimState.INSERT

    def accepts_text(self) -> bool:
        """Only Insert state types into the document."""
        return self._state is VimState.INSERT

    def _handle_normal(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        key = stroke.key
        if self._prefix:
            return self._handle_prefix(key, host)
        if key.isdigit() and len(key) == 1 and (key != "0" or self._count):
            self._accumulate_count(int(key))
            return self._result()
        if key in {"g", "Z"}:
            self._prefix = key
            return self._result()
        if operator := _OPERATORS.get(key):
            self._operator = operator
            self._operator_count = self._take_count()
            self._state = VimState.OPERATOR_PENDING
            return self._result()
        if key == "r":
            self._state = VimState.REPLACE_PENDING
            return self._result()
        if key == ":":
            self._command_buffer = ""
            self._state = VimState.COMMAND
            self._count = 0
            return self._result()
        if key in _INSERT_COMMANDS:
            # Counted Insert replay is deliberately excluded in v1. Discard
            # the prefix uniformly so no command has partial count behavior.
            self._take_count()
            self._enter_insert(key, host)
            return self._result()
        if key in {"v", "V"}:
            self._start_visual(key == "V", host)
            return self._result()
        if alias := _ALIASES.get(key):
            count = self._take_count()
            operator, motion, linewise = alias
            self._apply_precomposed_operator(operator, motion, linewise, count, host)
            return self._result()
        if motion := _MOTIONS.get(key):
            if motion is VimMotion.DOCUMENT_END and self._count:
                count = self._take_count()
                self._move_to_line(count - 1, host)
                return self._result()
            count = self._take_count()
            self._move(motion, count, host)
            return self._result()
        if key in {"ctrl+d", "ctrl+u"}:
            count = self._take_count()
            self._move_half_page(key == "ctrl+d", count, host)
            return self._result()
        if key in {"x", "X"}:
            count = self._take_count()
            self._delete_characters(before=key == "X", count=count, host=host)
            return self._result()
        if key == "J":
            self._join_lines(self._take_count(), host)
            return self._result()
        if key == "~":
            self._toggle_case(self._take_count(), host)
            return self._result()
        if key in {"p", "P"}:
            status = self._paste(after=key == "p", count=self._take_count(), host=host)
            return self._result(status=status, warning=bool(status), character_limit=bool(status))
        if key == "u":
            self._count = 0
            host.execute_editor_command(EditorCommand.UNDO)
            self._normalize_host_cursor(host)
            return self._result()
        if key in {"ctrl+r", "ctrl+shift+r"}:
            self._count = 0
            host.execute_editor_command(EditorCommand.REDO)
            self._normalize_host_cursor(host)
            return self._result()
        self._count = 0
        return self._result()

    def _handle_prefix(self, key: str, host: EditorCommandHost) -> EditorKeyResult:
        prefix = self._prefix
        self._prefix = ""
        if prefix == "g" and key == "g":
            count = self._take_count()
            lines = _lines(host.editor_text)
            target_row = min(count - 1, len(lines) - 1)
            host.move_editor_cursor((target_row, 0))
        elif prefix == "Z" and key == "Z":
            self._count = 0
            return self._result(EditorIntent.ACCEPT)
        elif prefix == "Z" and key == "Q":
            self._count = 0
            return self._result(EditorIntent.CANCEL_IMMEDIATE)
        else:
            self._count = 0
        return self._result()

    def _handle_insert(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        if stroke.key != "escape":
            return EditorKeyResult()
        cursor = host.editor_cursor_location
        line = _lines(host.editor_text)[cursor[0]]
        if cursor[1] > 0 and line:
            cursor = cursor[0], cursor[1] - 1
        host.move_editor_cursor(_normal_location(host.editor_text, cursor))
        self._state = VimState.NORMAL
        return self._result()

    def _handle_replace(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        character = stroke.character
        if character is None or len(character) != 1 or character in {"\n", "\r"}:
            self._return_normal(host)
            return self._result()
        text = host.editor_text
        row, column = _normal_location(text, host.editor_cursor_location)
        line = _lines(text)[row]
        count = min(self._take_count(), max(len(line) - column, 0))
        if count:
            source_range = EditorTextRange((row, column), (row, column + count))
            host.replace_editor_range(source_range, character * count)
            host.move_editor_cursor((row, column + count - 1))
        else:
            host.notify_editor_activity()
        self._state = VimState.NORMAL
        return self._result()

    def _handle_operator_pending(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        key = stroke.key
        if self._prefix:
            prefix = self._prefix
            self._prefix = ""
            operator = self._operator
            if prefix == "g" and key == "g" and operator is not None:
                explicit_count = self._count > 0 or self._operator_count > 1
                if explicit_count:
                    self._apply_operator_to_line(operator, self._effective_operator_count() - 1, host)
                else:
                    self._apply_operator_to_document_edge(operator, start=True, host=host)
            else:
                self._return_normal(host)
            return self._result()
        if key.isdigit() and len(key) == 1 and (key != "0" or self._count):
            self._accumulate_count(int(key))
            return self._result()
        operator = self._operator
        if operator is None:
            self._return_normal(host)
            return self._result()
        if key in {"i", "a"}:
            self._text_object_scope = key
            self._state = VimState.TEXT_OBJECT_PENDING
            return self._result()
        if key == operator.value:
            effective = self._effective_operator_count()
            self._apply_line_operator(operator, effective, host)
            return self._result()
        if key == "g":
            self._prefix = "g"
            return self._result()
        if motion := _MOTIONS.get(key):
            if motion not in _OPERATOR_MOTIONS:
                self._return_normal(host)
                return self._result()
            explicit_document_line = motion is VimMotion.DOCUMENT_END and (self._count > 0 or self._operator_count > 1)
            count = self._effective_operator_count()
            if explicit_document_line:
                self._apply_operator_to_line(operator, count - 1, host)
            else:
                self._apply_motion_operator(operator, motion, count, host)
            return self._result()
        self._return_normal(host)
        return self._result()

    def _handle_text_object(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        operator = self._operator
        text_object = _TEXT_OBJECTS.get(stroke.key)
        if operator is None or text_object is None:
            self._return_normal(host)
            return self._result()
        source_range = self._resolve_text_object(
            text_object,
            around=self._text_object_scope == "a",
            count=self._effective_operator_count(),
            host=host,
        )
        if source_range is None:
            self._return_normal(host)
            return self._result()
        self._apply_operator(operator, source_range, linewise=False, host=host)
        return self._result()

    def _handle_visual(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        key = stroke.key
        if self._prefix:
            prefix = self._prefix
            self._prefix = ""
            if prefix == "g" and key == "g":
                self._move_to_visual_line(self._take_count() - 1, host)
            return self._result()
        if key.isdigit() and len(key) == 1 and (key != "0" or self._count):
            self._accumulate_count(int(key))
            return self._result()
        if motion := _MOTIONS.get(key):
            if motion is VimMotion.DOCUMENT_END and self._count:
                self._move_to_visual_line(self._take_count() - 1, host)
                return self._result()
            self._move_visual(motion, self._take_count(), host)
            return self._result()
        if key == "g":
            self._prefix = "g"
            return self._result()
        if key in {"ctrl+d", "ctrl+u"}:
            count = self._take_count()
            active = self._visual_active or _normal_location(host.editor_text, host.editor_cursor_location)
            distance = max(host.editor_page_height // 2, 1) * count
            row = active[0] + (distance if key == "ctrl+d" else -distance)
            self._move_to_visual_line(row, host)
            return self._result()
        if key == "o":
            self._visual_anchor, self._visual_active = self._visual_active, self._visual_anchor
            self._render_visual_selection(host)
            return self._result()
        if key in {"y", "d", "x", "c"}:
            operator = VimOperator.YANK if key == "y" else VimOperator.CHANGE if key == "c" else VimOperator.DELETE
            source_range = self._visual_range(host)
            linewise = self._state is VimState.VISUAL_LINE
            if linewise:
                anchor = self._visual_anchor or host.editor_cursor_location
                active = self._visual_active or host.editor_cursor_location
                start_row, end_row = sorted((anchor[0], active[0]))
                if operator is VimOperator.CHANGE:
                    source_range = self._line_change_range(host.editor_text, start_row, end_row - start_row + 1)
            self._apply_operator(
                operator,
                source_range,
                linewise=linewise,
                linewise_start_row=start_row if linewise else None,
                host=host,
            )
            if linewise and operator is not VimOperator.YANK:
                self._position_after_line_operator(start_row, host)
            return self._result()
        self._count = 0
        return self._result()

    def _handle_command(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        if stroke.key == "escape":
            self._command_buffer = ""
            self._state = VimState.NORMAL
            return self._result()
        if stroke.key == "backspace":
            self._command_buffer = self._command_buffer[:-1]
            return self._result()
        if stroke.key == "enter":
            command = self._command_buffer
            if command in {"wq", "x"}:
                return self._result(EditorIntent.ACCEPT)
            if command == "q!":
                return self._result(EditorIntent.CANCEL_IMMEDIATE)
            if command == "q":
                if not host.editor_is_dirty:
                    return self._result(EditorIntent.CANCEL_IMMEDIATE)
                return self._result(status=_VIM_NO_WRITE.bind(), warning=True)
            return self._result(status=_VIM_NOT_EDITOR_COMMAND.bind(command=command), warning=True)
        if stroke.character is not None and stroke.character.isprintable():
            self._command_buffer += stroke.character
        return self._result()

    def _start_visual(self, linewise: bool, host: EditorCommandHost) -> None:
        location = _normal_location(host.editor_text, host.editor_cursor_location)
        self._visual_anchor = location
        self._visual_active = location
        self._state = VimState.VISUAL_LINE if linewise else VimState.VISUAL_CHAR
        self._count = 0
        self._render_visual_selection(host)

    def _move_visual(self, motion: VimMotion, count: int, host: EditorCommandHost) -> None:
        active = self._visual_active or _normal_location(host.editor_text, host.editor_cursor_location)
        target = self._resolve_motion_target(motion, count, active, host.editor_text).location
        self._visual_active = _normal_location(host.editor_text, target)
        self._render_visual_selection(host)

    def _move_to_visual_line(self, row: int, host: EditorCommandHost) -> None:
        active = self._visual_active or _normal_location(host.editor_text, host.editor_cursor_location)
        lines = _lines(host.editor_text)
        target_row = min(max(row, 0), len(lines) - 1)
        self._visual_active = _normal_location(host.editor_text, (target_row, active[1]))
        self._render_visual_selection(host)

    def _render_visual_selection(self, host: EditorCommandHost) -> None:
        anchor = self._visual_anchor
        active = self._visual_active
        if anchor is None or active is None:
            return
        if self._state is VimState.VISUAL_LINE:
            start_row, end_row = sorted((anchor[0], active[0]))
            source_range = self._line_edit_range(host.editor_text, start_row, end_row - start_row + 1)
            if active[0] >= anchor[0]:
                host.set_editor_selection(source_range.start, source_range.end)
            else:
                host.set_editor_selection(source_range.end, source_range.start)
            return
        anchor_offset = location_to_offset(host.editor_text, anchor)
        active_offset = location_to_offset(host.editor_text, active)
        if anchor_offset <= active_offset:
            host.set_editor_selection(anchor, _advance_location(host.editor_text, active))
        else:
            host.set_editor_selection(_advance_location(host.editor_text, anchor), active)

    def _visual_range(self, host: EditorCommandHost) -> EditorTextRange:
        text = host.editor_text
        anchor = self._visual_anchor or host.editor_cursor_location
        active = self._visual_active or host.editor_cursor_location
        if self._state is VimState.VISUAL_LINE:
            start_row, end_row = sorted((anchor[0], active[0]))
            return self._line_edit_range(text, start_row, end_row - start_row + 1)
        start = min(location_to_offset(text, anchor), location_to_offset(text, active))
        end = min(max(location_to_offset(text, anchor), location_to_offset(text, active)) + 1, len(text))
        return _range_from_offsets(text, start, end)

    def _enter_insert(self, command: str, host: EditorCommandHost) -> None:
        text = host.editor_text
        lines = _lines(text)
        row, column = _normal_location(text, host.editor_cursor_location)
        line = lines[row]
        if command == "i":
            location = row, column
        elif command == "I":
            location = row, _first_non_blank(line)
        elif command == "a":
            location = row, _after_character(host, row, column, len(line))
        elif command == "A":
            location = row, len(line)
        elif command == "o":
            insert_at = (row, len(line))
            host.replace_editor_range(EditorTextRange(insert_at, insert_at), "\n")
            location = row + 1, 0
        else:
            insert_at = (row, 0)
            host.replace_editor_range(EditorTextRange(insert_at, insert_at), "\n")
            location = row, 0
        host.move_editor_cursor(location)
        self._state = VimState.INSERT

    def _move(self, motion: VimMotion, count: int, host: EditorCommandHost) -> None:
        current = _normal_location(host.editor_text, host.editor_cursor_location)
        target = self._resolve_motion_target(motion, count, current, host.editor_text)
        host.move_editor_cursor(_normal_location(host.editor_text, target.location))

    def _move_to_line(self, row: int, host: EditorCommandHost) -> None:
        lines = _lines(host.editor_text)
        target_row = min(max(row, 0), len(lines) - 1)
        host.move_editor_cursor(_normal_location(host.editor_text, (target_row, 0)))

    def _move_half_page(self, down: bool, count: int, host: EditorCommandHost) -> None:
        text = host.editor_text
        lines = _lines(text)
        row, column = _normal_location(text, host.editor_cursor_location)
        distance = max(host.editor_page_height // 2, 1) * count
        row = min(row + distance, len(lines) - 1) if down else max(row - distance, 0)
        host.move_editor_cursor(_normal_location(text, (row, column)))

    def _resolve_motion_target(
        self,
        motion: VimMotion,
        count: int,
        current: EditorLocation,
        text: str,
    ) -> _MotionTarget:
        lines = _lines(text)
        row, column = _normal_location(text, current)
        if motion is VimMotion.LEFT:
            return _MotionTarget((row, max(column - count, 0)))
        if motion is VimMotion.RIGHT:
            return _MotionTarget((row, min(column + count, max(len(lines[row]) - 1, 0))))
        if motion in {VimMotion.DOWN, VimMotion.UP}:
            row_delta = count if motion is VimMotion.DOWN else -count
            target_row = min(max(row + row_delta, 0), len(lines) - 1)
            return _MotionTarget(_normal_location(text, (target_row, column)))
        if motion is VimMotion.LINE_START:
            return _MotionTarget((row, 0))
        if motion is VimMotion.FIRST_NON_BLANK:
            return _MotionTarget((row, _first_non_blank(lines[row])))
        if motion is VimMotion.LINE_END:
            target_row = min(row + count - 1, len(lines) - 1)
            target_line = lines[target_row]
            return _MotionTarget((target_row, max(len(target_line) - 1, 0)), inclusive=bool(target_line))
        if motion is VimMotion.WORD_NEXT:
            offset = _next_word_start(text, location_to_offset(text, (row, column)), count)
            return _MotionTarget(offset_to_location(text, offset))
        if motion is VimMotion.WORD_PREVIOUS:
            offset = _previous_word_start(text, location_to_offset(text, (row, column)), count)
            return _MotionTarget(offset_to_location(text, offset))
        if motion is VimMotion.WORD_END:
            offset = _word_end(text, location_to_offset(text, (row, column)), count)
            return _MotionTarget(offset_to_location(text, offset), inclusive=True)
        if motion is VimMotion.DOCUMENT_START:
            return _MotionTarget((0, 0), linewise=True)
        return _MotionTarget((len(lines) - 1, max(len(lines[-1]) - 1, 0)), inclusive=True, linewise=True)

    def _apply_precomposed_operator(
        self,
        operator: VimOperator,
        motion: VimMotion | None,
        linewise: bool,
        count: int,
        host: EditorCommandHost,
    ) -> None:
        if linewise:
            self._apply_line_operator(operator, count, host)
        elif motion is not None:
            self._apply_motion_operator(operator, motion, count, host)

    def _apply_motion_operator(
        self,
        operator: VimOperator,
        motion: VimMotion,
        count: int,
        host: EditorCommandHost,
    ) -> None:
        text = host.editor_text
        current = _normal_location(text, host.editor_cursor_location)
        if motion is VimMotion.RIGHT:
            row, column = current
            end = min(column + count, len(_lines(text)[row]))
            self._apply_operator(operator, EditorTextRange(current, (row, end)), linewise=False, host=host)
            return
        if motion is VimMotion.LEFT:
            row, column = current
            start = max(column - count, 0)
            self._apply_operator(operator, EditorTextRange((row, start), current), linewise=False, host=host)
            return
        if operator is VimOperator.CHANGE and motion is VimMotion.WORD_NEXT and count == 1:
            current_offset = location_to_offset(text, current)
            if current_offset < len(text) and not text[current_offset].isspace():
                target = self._resolve_motion_target(VimMotion.WORD_END, 1, current, text)
                end_offset = min(location_to_offset(text, target.location) + 1, len(text))
                self._apply_operator(
                    operator,
                    _range_from_offsets(text, current_offset, end_offset),
                    linewise=False,
                    host=host,
                )
                return
        target = self._resolve_motion_target(motion, count, current, text)
        if target.linewise:
            start_row, end_row = sorted((current[0], target.location[0]))
            range_factory = self._line_change_range if operator is VimOperator.CHANGE else self._line_edit_range
            source_range = range_factory(text, start_row, end_row - start_row + 1)
            self._apply_operator(
                operator,
                source_range,
                linewise=True,
                linewise_start_row=start_row,
                host=host,
            )
            if operator is not VimOperator.YANK:
                self._position_after_line_operator(start_row, host)
            return
        start_offset = location_to_offset(text, current)
        end_offset = location_to_offset(text, target.location)
        if target.inclusive:
            if end_offset >= start_offset:
                end_offset = min(end_offset + 1, len(text))
            else:
                start_offset = min(start_offset + 1, len(text))
        source_range = _range_from_offsets(text, start_offset, end_offset)
        self._apply_operator(operator, source_range, linewise=False, host=host)

    def _apply_operator_to_document_edge(
        self,
        operator: VimOperator,
        *,
        start: bool,
        host: EditorCommandHost,
    ) -> None:
        row = _normal_location(host.editor_text, host.editor_cursor_location)[0]
        if start:
            start_row = 0
            count = row + 1
        else:
            start_row = row
            count = len(_lines(host.editor_text)) - row
        range_factory = self._line_change_range if operator is VimOperator.CHANGE else self._line_edit_range
        source_range = range_factory(host.editor_text, start_row, count)
        self._apply_operator(
            operator,
            source_range,
            linewise=True,
            linewise_start_row=start_row,
            host=host,
        )
        if operator is not VimOperator.YANK:
            self._position_after_line_operator(start_row, host)

    def _apply_operator_to_line(self, operator: VimOperator, row: int, host: EditorCommandHost) -> None:
        current_row = _normal_location(host.editor_text, host.editor_cursor_location)[0]
        lines = _lines(host.editor_text)
        target_row = min(max(row, 0), len(lines) - 1)
        start_row, end_row = sorted((current_row, target_row))
        count = end_row - start_row + 1
        range_factory = self._line_change_range if operator is VimOperator.CHANGE else self._line_edit_range
        self._apply_operator(
            operator,
            range_factory(host.editor_text, start_row, count),
            linewise=True,
            linewise_start_row=start_row,
            host=host,
        )
        if operator is not VimOperator.YANK:
            self._position_after_line_operator(start_row, host)

    def _apply_line_operator(self, operator: VimOperator, count: int, host: EditorCommandHost) -> None:
        row = _normal_location(host.editor_text, host.editor_cursor_location)[0]
        range_factory = self._line_change_range if operator is VimOperator.CHANGE else self._line_edit_range
        source_range = range_factory(host.editor_text, row, count)
        self._apply_operator(
            operator,
            source_range,
            linewise=True,
            linewise_start_row=row,
            host=host,
        )
        if operator is not VimOperator.YANK:
            self._position_after_line_operator(row, host)

    def _apply_operator(
        self,
        operator: VimOperator,
        source_range: EditorTextRange,
        *,
        linewise: bool,
        linewise_start_row: int | None = None,
        host: EditorCommandHost,
    ) -> None:
        text = host.read_editor_range(source_range)
        if linewise:
            register_text = self._linewise_register_text(text, source_range, host.editor_text)
            if register_text:
                self._register = EditorRegister(register_text, EditorRegisterKind.LINEWISE)
        else:
            if text:
                self._register = EditorRegister(text)
        target_state = VimState.INSERT if operator is VimOperator.CHANGE else VimState.NORMAL
        if operator is VimOperator.YANK:
            cursor = (linewise_start_row, 0) if linewise_start_row is not None else source_range.start
            host.move_editor_cursor(_normal_location(host.editor_text, cursor))
        elif text:
            host.delete_editor_range(source_range)
            if target_state is VimState.NORMAL:
                self._normalize_host_cursor(host)
        else:
            host.notify_editor_activity()
        self._state = target_state
        self._clear_pending()

    def _delete_characters(self, *, before: bool, count: int, host: EditorCommandHost) -> None:
        text = host.editor_text
        row, column = _normal_location(text, host.editor_cursor_location)
        line = _lines(text)[row]
        start = max(column - count, 0) if before else column
        end = column if before else min(column + count, len(line))
        source_range = EditorTextRange((row, start), (row, end))
        deleted = host.read_editor_range(source_range)
        if deleted:
            self._register = EditorRegister(deleted)
            host.delete_editor_range(source_range)
            self._normalize_host_cursor(host)
        else:
            host.notify_editor_activity()

    def _join_lines(self, count: int, host: EditorCommandHost) -> None:
        text = host.editor_text
        lines = _lines(text)
        row = _normal_location(text, host.editor_cursor_location)[0]
        end_row = min(row + max(count, 2) - 1, len(lines) - 1)
        if end_row == row:
            host.notify_editor_activity()
            return
        replacement = lines[row].rstrip()
        for line in lines[row + 1 : end_row + 1]:
            replacement += " " + line.strip()
        source_range = EditorTextRange((row, 0), (end_row, len(lines[end_row])))
        cursor_column = len(lines[row].rstrip())
        host.replace_editor_range(source_range, replacement)
        host.move_editor_cursor((row, min(cursor_column, max(len(replacement) - 1, 0))))

    def _toggle_case(self, count: int, host: EditorCommandHost) -> None:
        text = host.editor_text
        row, column = _normal_location(text, host.editor_cursor_location)
        line = _lines(text)[row]
        # An atomic span (a live label) counts as one character and keeps its text.
        atomic = host.editor_atomic_spans(row)
        start = next((low for low, high in atomic if low < column < high), column)
        end = start
        pieces: list[str] = []
        for _ in range(count):
            if end >= len(line):
                break
            span = next(((low, high) for low, high in atomic if low == end), None)
            if span is not None:
                pieces.append(line[span[0] : span[1]])
                end = span[1]
                continue
            character = line[end]
            pieces.append(character.lower() if character.isupper() else character.upper())
            end += 1
        if end == start:
            host.notify_editor_activity()
            return
        host.replace_editor_range(EditorTextRange((row, start), (row, end)), "".join(pieces))
        host.move_editor_cursor(_normal_location(host.editor_text, (row, end)))

    def _paste(self, *, after: bool, count: int, host: EditorCommandHost) -> EditorStatus:
        register = self._register
        if not register.text:
            host.notify_editor_activity()
            return ""
        text = host.editor_text
        inserted_character_count = len(register.text) * count
        if inserted_character_count > MESSAGE_EDITOR_MAX_CHARACTERS - len(text):
            host.notify_editor_activity()
            return MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS.bind()
        row, column = _normal_location(text, host.editor_cursor_location)
        payload = register.text * count
        if register.kind is EditorRegisterKind.LINEWISE:
            lines = _lines(text)
            if after:
                if row < len(lines) - 1:
                    location = row + 1, 0
                    inserted = payload
                else:
                    location = row, len(lines[row])
                    inserted = "\n" + payload.removesuffix("\n")
            else:
                location = row, 0
                inserted = payload
            host.replace_editor_range(EditorTextRange(location, location), inserted)
            target_row = row + 1 if after else row
            host.move_editor_cursor((min(target_row, len(_lines(host.editor_text)) - 1), 0))
            return ""
        location = (row, _after_character(host, row, column, len(_lines(text)[row]))) if after else (row, column)
        insertion_offset = location_to_offset(text, location)
        host.replace_editor_range(EditorTextRange(location, location), payload)
        target = offset_to_location(host.editor_text, insertion_offset + len(payload) - 1)
        host.move_editor_cursor(_normal_location(host.editor_text, target))
        return ""

    def _resolve_text_object(
        self,
        text_object: VimTextObject,
        *,
        around: bool,
        count: int,
        host: EditorCommandHost,
    ) -> EditorTextRange | None:
        text = host.editor_text
        cursor_offset = location_to_offset(text, _normal_location(text, host.editor_cursor_location))
        if text_object in {VimTextObject.WORD, VimTextObject.WORD_BIG}:
            return self._word_object(
                text,
                cursor_offset,
                around=around,
                big=text_object is VimTextObject.WORD_BIG,
                count=count,
            )
        if text_object in {VimTextObject.DOUBLE_QUOTE, VimTextObject.SINGLE_QUOTE, VimTextObject.BACKTICK}:
            return self._quote_object(text, cursor_offset, text_object.value, around=around)
        delimiters = {
            VimTextObject.PAREN: ("(", ")"),
            VimTextObject.BRACKET: ("[", "]"),
            VimTextObject.BRACE: ("{", "}"),
        }
        opening, closing = delimiters[text_object]
        return self._pair_object(text, cursor_offset, opening, closing, around=around, count=count)

    def _word_object(
        self,
        text: str,
        cursor: int,
        *,
        around: bool,
        big: bool,
        count: int,
    ) -> EditorTextRange | None:
        if not text:
            return None
        index = min(cursor, len(text) - 1)
        if text[index].isspace():
            while index < len(text) and text[index].isspace():
                index += 1
            if index == len(text):
                return None
        character_class = _token_class(text[index], big=big)
        start = index
        end = index + 1
        while start > 0 and _token_class(text[start - 1], big=big) == character_class:
            start -= 1
        while end < len(text) and _token_class(text[end], big=big) == character_class:
            end += 1
        for _ in range(count - 1):
            while end < len(text) and text[end].isspace():
                end += 1
            if end >= len(text):
                break
            character_class = _token_class(text[end], big=big)
            while end < len(text) and _token_class(text[end], big=big) == character_class:
                end += 1
        if around:
            if end < len(text) and text[end].isspace():
                while end < len(text) and text[end].isspace():
                    end += 1
            else:
                while start > 0 and text[start - 1].isspace():
                    start -= 1
        return _range_from_offsets(text, start, end)

    def _quote_object(self, text: str, cursor: int, quote: str, *, around: bool) -> EditorTextRange | None:
        line_start = text.rfind("\n", 0, cursor) + 1
        line_end = text.find("\n", cursor)
        if line_end == -1:
            line_end = len(text)
        delimiters = [index for index in range(line_start, line_end) if text[index] == quote]
        pair = next(
            (
                (delimiters[index], delimiters[index + 1])
                for index in range(0, len(delimiters) - 1, 2)
                if delimiters[index] <= cursor <= delimiters[index + 1]
            ),
            None,
        )
        if pair is None:
            return None
        opening, closing = pair
        start = opening if around else opening + 1
        end = closing + 1 if around else closing
        return _range_from_offsets(text, start, end)

    def _pair_object(
        self,
        text: str,
        cursor: int,
        opening: str,
        closing: str,
        *,
        around: bool,
        count: int,
    ) -> EditorTextRange | None:
        stack: list[int] = []
        candidates: list[tuple[int, int]] = []
        for index, character in enumerate(text):
            if character == opening:
                stack.append(index)
            elif character == closing and stack:
                start = stack.pop()
                if start <= cursor <= index:
                    candidates.append((start, index))
        if not candidates:
            return None
        candidates.sort(key=lambda pair: pair[1] - pair[0])
        selected = candidates[min(count - 1, len(candidates) - 1)]
        start = selected[0] if around else selected[0] + 1
        end = selected[1] + 1 if around else selected[1]
        return _range_from_offsets(text, start, end)

    def _line_edit_range(self, text: str, start_row: int, count: int) -> EditorTextRange:
        lines = _lines(text)
        start_row = min(max(start_row, 0), len(lines) - 1)
        end_row = min(start_row + max(count, 1) - 1, len(lines) - 1)
        if end_row < len(lines) - 1:
            return EditorTextRange((start_row, 0), (end_row + 1, 0))
        if start_row > 0:
            return EditorTextRange((start_row - 1, len(lines[start_row - 1])), (end_row, len(lines[end_row])))
        return EditorTextRange((0, 0), (end_row, len(lines[end_row])))

    def _line_change_range(self, text: str, start_row: int, count: int) -> EditorTextRange:
        lines = _lines(text)
        start_row = min(max(start_row, 0), len(lines) - 1)
        end_row = min(start_row + max(count, 1) - 1, len(lines) - 1)
        return EditorTextRange((start_row, 0), (end_row, len(lines[end_row])))

    def _linewise_register_text(
        self,
        selected_text: str,
        source_range: EditorTextRange,
        document_text: str,
    ) -> str:
        lines = _lines(document_text)
        if source_range.end[0] > source_range.start[0] and source_range.end[1] == 0:
            return selected_text
        starts_with_preceding_newline = source_range.end[0] > source_range.start[0] and source_range.start[1] == len(
            lines[source_range.start[0]]
        )
        if starts_with_preceding_newline and selected_text.startswith("\n"):
            selected_text = selected_text[1:]
        return selected_text + "\n"

    def _normalize_host_cursor(self, host: EditorCommandHost) -> None:
        host.move_editor_cursor(_normal_location(host.editor_text, host.editor_cursor_location))

    def _position_after_line_operator(self, start_row: int, host: EditorCommandHost) -> None:
        lines = _lines(host.editor_text)
        host.move_editor_cursor((min(start_row, len(lines) - 1), 0))

    def _return_normal(self, host: EditorCommandHost) -> None:
        visual_active = self._visual_active if self._state in {VimState.VISUAL_CHAR, VimState.VISUAL_LINE} else None
        self._state = VimState.NORMAL
        self._clear_pending()
        self._visual_anchor = None
        self._visual_active = None
        target = visual_active or host.editor_cursor_location
        host.move_editor_cursor(_normal_location(host.editor_text, target))

    def _clear_pending(self) -> None:
        self._count = 0
        self._operator = None
        self._operator_count = 1
        self._text_object_scope = ""
        self._prefix = ""

    def _accumulate_count(self, digit: int) -> None:
        self._count = min(self._count * 10 + digit, MAX_VIM_COUNT)

    def _take_count(self) -> int:
        count = self._count or 1
        self._count = 0
        return count

    def _effective_operator_count(self) -> int:
        return min(self._operator_count * self._take_count(), MAX_VIM_COUNT)

    def _pending_label(self) -> str:
        if self._state is VimState.REPLACE_PENDING:
            return f"{self._count or ''}r"
        count = str(self._count) if self._count else ""
        operator = self._operator.value if self._operator is not None else ""
        return f"{self._operator_count if self._operator_count > 1 else ''}{operator}{count}{self._text_object_scope}{self._prefix}"

    def _result(
        self,
        intent: EditorIntent = EditorIntent.NONE,
        status: EditorStatus = "",
        *,
        warning: bool = False,
        character_limit: bool = False,
    ) -> EditorKeyResult:
        return EditorKeyResult(
            consumed=True, intent=intent, status=status, warning=warning, character_limit=character_limit
        )
