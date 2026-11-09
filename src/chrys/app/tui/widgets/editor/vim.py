# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure Vim keymap state machine for the modal message editor."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, field
from enum import StrEnum
from functools import cached_property
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
from chrys.foundation.text.lines import normalize_line_endings

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


_UNIT: Final = "\ufffc"
"""One whole unit (a prompt field) in :class:`_UnitView` text."""


@dataclass(frozen=True)
class _UnitView:
    """The editor text with each whole unit (a prompt field) as one character.

    Every command that counts or scans characters runs on :attr:`text`, so a
    unit is one character to h/l/x/X/r/~, a word of its own to w/b/e and
    iw/aw (part of a WORD to iW/aW), and never a quote or bracket. :meth:`at` and
    :meth:`location` map between the two texts; a position inside a unit is
    the unit's character, and the position after that character is the
    unit's end.
    """

    source: str
    units: tuple[tuple[int, int], ...] = ()
    """Each unit's ``[start, end)`` in :attr:`source`, ordered; overlapping units form one."""
    _starts: tuple[int, ...] = field(default=(), repr=False)
    _indices: tuple[int, ...] = field(default=(), repr=False)

    @classmethod
    def of(cls, host: EditorCommandHost) -> _UnitView:
        source = host.editor_text
        units: list[tuple[int, int]] = []
        for low, high in sorted(host.editor_unit_spans()):
            if units and low < units[-1][1]:
                units[-1] = units[-1][0], max(units[-1][1], high)
            else:
                units.append((low, high))
        indices: list[int] = []
        removed = 0
        for low, high in units:
            indices.append(low - removed)
            removed += high - low - 1
        return cls(source, tuple(units), tuple(low for low, _ in units), tuple(indices))

    @cached_property
    def text(self) -> str:
        """Build the scanning text only when a command needs it."""
        if not self.units:
            return self.source
        pieces: list[str] = []
        last = 0
        for low, high in self.units:
            pieces.extend((self.source[last:low], _UNIT))
            last = high
        pieces.append(self.source[last:])
        return "".join(pieces)

    @cached_property
    def lines(self) -> list[str]:
        return _lines(self.source)

    def normal(self, location: EditorLocation) -> EditorLocation:
        """Clamp to a Normal character, landing at a unit's start."""
        row = min(max(location[0], 0), len(self.lines) - 1)
        # Textual document columns exclude the carriage return in CRLF.
        column = min(max(location[1], 0), max(len(self.lines[row].removesuffix("\r")) - 1, 0))
        if self.units:
            for low, high in self.columns(row):
                if low < column < high:
                    return row, low
        return row, column

    def index(self, offset: int) -> int:
        """The view index of source *offset*."""
        before = bisect_left(self._starts, offset)
        if not before:
            return offset
        _, high = self.units[before - 1]
        if offset < high:
            return self._indices[before - 1]
        return self._indices[before - 1] + 1 + offset - high

    def offset(self, index: int) -> int:
        """The source offset of view *index*: a unit's character is its start."""
        before = bisect_left(self._indices, index)
        if before < len(self._indices) and self._indices[before] == index:
            return self.units[before][0]
        if not before:
            return index
        return self.units[before - 1][1] + index - self._indices[before - 1] - 1

    def at(self, location: EditorLocation) -> int:
        row = min(max(location[0], 0), len(self.lines) - 1)
        column = min(max(location[1], 0), len(self.lines[row]))
        return self.index(sum(map(len, self.lines[:row])) + row + column)

    def location(self, index: int) -> EditorLocation:
        offset = min(max(self.offset(max(index, 0)), 0), len(self.source))
        return self.source.count("\n", 0, offset), offset - self.source.rfind("\n", 0, offset) - 1

    def row_bounds(self, row: int) -> tuple[int, int]:
        """The view indices where *row* starts and ends (the end before its newline)."""
        line = self.lines[row].removesuffix("\r")
        return self.at((row, 0)), self.at((row, len(line)))

    def columns(self, row: int) -> list[tuple[int, int]]:
        """The ``[start, end)`` columns of the units on *row*."""
        start = sum(map(len, self.lines[:row])) + row
        end = start + len(self.lines[row])
        return [(low - start, high - start) for low, high in self.units if start <= low and high <= end]

    def rows(self) -> set[int]:
        """The rows holding a unit."""
        rows: set[int] = set()
        row = previous = 0
        for low, _ in self.units:
            row += self.source.count("\n", previous, low)
            rows.add(row)
            previous = low
        return rows

    def after(self, location: EditorLocation) -> EditorLocation:
        """The location after the character at *location* (a unit is one character)."""
        return self.location(self.at(location) + 1)


def _first_non_blank(line: str) -> int:
    return len(line) - len(line.lstrip())


def _is_word(character: str) -> bool:
    return character.isalnum() or character == "_"


def _token_class(character: str, *, big: bool = False) -> int:
    if character.isspace():
        return 0
    if big or _is_word(character):
        return 1
    if character == _UNIT:
        return 3
    return 2


def _extends(character: str, token_class: int, *, big: bool = False) -> bool:
    """Whether *character* continues a token of *token_class*; a unit is a word of its own (class 3)."""
    return token_class in {1, 2} and _token_class(character, big=big) == token_class


def _next_word_start(text: str, offset: int, count: int) -> int:
    index = min(max(offset, 0), len(text))
    for _ in range(count):
        if index >= len(text):
            break
        character_class = _token_class(text[index])
        if character_class:
            index += 1
        while index < len(text) and _extends(text[index], character_class):
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
        while index > 0 and _extends(text[index - 1], character_class):
            index -= 1
    return index


def _word_end(text: str, offset: int, count: int) -> int:
    if not text:
        return 0
    index = min(max(offset, 0), len(text) - 1)
    for _ in range(count):
        character_class = _token_class(text[index])
        has_later_character_in_token = index < len(text) - 1 and _extends(text[index + 1], character_class)
        if not has_later_character_in_token:
            if index < len(text) - 1:
                index += 1
            while index < len(text) - 1 and text[index].isspace():
                index += 1
        character_class = _token_class(text[index])
        while index < len(text) - 1 and _extends(text[index + 1], character_class):
            index += 1
    return index


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
        # Where the last j/k, Ctrl+D or Ctrl+U left the cursor and the column it aims for.
        self._vertical_goal: tuple[EditorLocation, int] | None = None

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
        if not self._keeps_vertical_goal(stroke.key):
            self._vertical_goal = None
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

    def _keeps_vertical_goal(self, key: str) -> bool:
        """Whether *key* leaves a sticky column in place: a vertical move or a count digit before one."""
        if self._state not in {VimState.NORMAL, VimState.VISUAL_CHAR, VimState.VISUAL_LINE}:
            return False
        if key in {"j", "k", "down", "up", "ctrl+d", "ctrl+u"}:
            return True
        return key.isdigit() and len(key) == 1 and (key != "0" or self._count > 0)

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
        self._vertical_goal = None

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
            self._move_to_line(self._take_count() - 1, host)
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
        view = _UnitView.of(host)
        cursor = host.editor_cursor_location
        index = max(view.at(cursor) - 1, view.row_bounds(cursor[0])[0])
        host.move_editor_cursor(view.normal(view.location(index)))
        self._state = VimState.NORMAL
        return self._result()

    def _handle_replace(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        character = stroke.character
        if character is None or len(character) != 1 or character in {"\n", "\r"}:
            self._return_normal(host)
            return self._result()
        view = _UnitView.of(host)
        row, column = view.normal(host.editor_cursor_location)
        index = view.at((row, column))
        end = min(index + self._take_count(), view.row_bounds(row)[1])
        if end > index:
            start = view.location(index)
            host.replace_editor_range(EditorTextRange(start, view.location(end)), character * (end - index))
            host.move_editor_cursor((row, start[1] + end - index - 1))
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
            distance = max(host.editor_page_height // 2, 1) * count
            self._move_visual(VimMotion.DOWN if key == "ctrl+d" else VimMotion.UP, distance, host)
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
        view = _UnitView.of(host)
        location = view.normal(host.editor_cursor_location)
        self._visual_anchor = location
        self._visual_active = location
        self._state = VimState.VISUAL_LINE if linewise else VimState.VISUAL_CHAR
        self._count = 0
        self._render_visual_selection(host, view)

    def _move_visual(self, motion: VimMotion, count: int, host: EditorCommandHost) -> None:
        view = _UnitView.of(host)
        active = self._visual_active or view.normal(host.editor_cursor_location)
        goal = self._vertical_goal
        column = goal[1] if goal is not None and goal[0] == host.editor_cursor_location else active[1]
        self._visual_active = view.normal(
            self._resolve_motion_target(motion, count, active, view, vertical_column=column).location
        )
        self._render_visual_selection(host, view)
        if motion in {VimMotion.DOWN, VimMotion.UP}:
            self._vertical_goal = host.editor_cursor_location, column

    def _move_to_visual_line(self, row: int, host: EditorCommandHost) -> None:
        view = _UnitView.of(host)
        active = self._visual_active or view.normal(host.editor_cursor_location)
        self._visual_active = view.normal((row, active[1]))
        self._render_visual_selection(host, view)

    def _render_visual_selection(self, host: EditorCommandHost, view: _UnitView | None = None) -> None:
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
        view = view or _UnitView.of(host)
        anchor_offset = view.at(anchor)
        active_offset = view.at(active)
        if anchor_offset <= active_offset:
            host.set_editor_selection(anchor, view.after(active))
        else:
            host.set_editor_selection(view.after(anchor), active)

    def _visual_range(self, host: EditorCommandHost) -> EditorTextRange:
        text = host.editor_text
        anchor = self._visual_anchor or host.editor_cursor_location
        active = self._visual_active or host.editor_cursor_location
        if self._state is VimState.VISUAL_LINE:
            start_row, end_row = sorted((anchor[0], active[0]))
            return self._line_edit_range(text, start_row, end_row - start_row + 1)
        first, last = sorted((anchor, active))
        return EditorTextRange(first, _UnitView.of(host).after(last))

    def _enter_insert(self, command: str, host: EditorCommandHost) -> None:
        view = _UnitView.of(host)
        lines = view.lines
        row, column = view.normal(host.editor_cursor_location)
        line = lines[row]
        if command == "i":
            location = row, column
        elif command == "I":
            location = row, _first_non_blank(line)
        elif command == "a":
            location = view.location(min(view.at((row, column)) + 1, view.row_bounds(row)[1]))
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
        view = _UnitView.of(host)
        current = view.normal(host.editor_cursor_location)
        column = self._vertical_column(current)
        target = self._resolve_motion_target(motion, count, current, view, vertical_column=column)
        host.move_editor_cursor(view.normal(target.location))
        if motion in {VimMotion.DOWN, VimMotion.UP}:
            self._vertical_goal = host.editor_cursor_location, column

    def _vertical_column(self, current: EditorLocation) -> int:
        """The column j/k aim for: kept from the j/k that left the cursor here, else the cursor's (Vim's sticky column)."""
        goal = self._vertical_goal
        return goal[1] if goal is not None and goal[0] == current else current[1]

    def _move_to_line(self, row: int, host: EditorCommandHost) -> None:
        host.move_editor_cursor(_UnitView.of(host).normal((row, 0)))

    def _move_half_page(self, down: bool, count: int, host: EditorCommandHost) -> None:
        distance = max(host.editor_page_height // 2, 1) * count
        self._move(VimMotion.DOWN if down else VimMotion.UP, distance, host)

    def _resolve_motion_target(
        self,
        motion: VimMotion,
        count: int,
        current: EditorLocation,
        view: _UnitView,
        *,
        vertical_column: int | None = None,
    ) -> _MotionTarget:
        lines = view.lines
        row, column = view.normal(current)
        index = view.at((row, column))
        if motion is VimMotion.LEFT:
            return _MotionTarget(view.location(max(index - count, view.row_bounds(row)[0])))
        if motion is VimMotion.RIGHT:
            first, end = view.row_bounds(row)
            return _MotionTarget(view.location(max(min(index + count, end - 1), first)))
        if motion in {VimMotion.DOWN, VimMotion.UP}:
            row_delta = count if motion is VimMotion.DOWN else -count
            target_row = min(max(row + row_delta, 0), len(lines) - 1)
            return _MotionTarget(view.normal((target_row, column if vertical_column is None else vertical_column)))
        if motion is VimMotion.LINE_START:
            return _MotionTarget((row, 0))
        if motion is VimMotion.FIRST_NON_BLANK:
            return _MotionTarget((row, _first_non_blank(lines[row])))
        if motion is VimMotion.LINE_END:
            target_row = min(row + count - 1, len(lines) - 1)
            first, end = view.row_bounds(target_row)
            return _MotionTarget(view.location(max(end - 1, first)), inclusive=end > first)
        if motion is VimMotion.WORD_NEXT:
            return _MotionTarget(view.location(_next_word_start(view.text, index, count)))
        if motion is VimMotion.WORD_PREVIOUS:
            return _MotionTarget(view.location(_previous_word_start(view.text, index, count)))
        if motion is VimMotion.WORD_END:
            return _MotionTarget(view.location(_word_end(view.text, index, count)), inclusive=True)
        if motion is VimMotion.DOCUMENT_START:
            return _MotionTarget((0, 0), linewise=True)
        return _MotionTarget(view.normal((len(lines) - 1, len(lines[-1]))), inclusive=True, linewise=True)

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
        view = _UnitView.of(host)
        text = view.source
        current = view.normal(host.editor_cursor_location)
        if motion in {VimMotion.LEFT, VimMotion.RIGHT}:
            self._apply_operator(
                operator,
                self._character_range(host, count, before=motion is VimMotion.LEFT, view=view),
                linewise=False,
                host=host,
            )
            return
        if operator is VimOperator.CHANGE and motion is VimMotion.WORD_NEXT and count == 1:
            current_index = view.at(current)
            if current_index < len(view.text) and not view.text[current_index].isspace():
                end_index = current_index + 1
                token_class = _token_class(view.text[current_index])
                while end_index < len(view.text) and _extends(view.text[end_index], token_class):
                    end_index += 1
                self._apply_operator(
                    operator,
                    EditorTextRange(view.location(current_index), view.location(end_index)),
                    linewise=False,
                    host=host,
                )
                return
        target = self._resolve_motion_target(motion, count, current, view)
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
        start_index = view.at(current)
        end_index = view.at(target.location)
        if target.inclusive:
            if end_index >= start_index:
                end_index = min(end_index + 1, len(view.text))
            else:
                start_index = min(start_index + 1, len(view.text))
        low, high = sorted((start_index, end_index))
        self._apply_operator(
            operator, EditorTextRange(view.location(low), view.location(high)), linewise=False, host=host
        )

    def _apply_operator_to_document_edge(
        self,
        operator: VimOperator,
        *,
        start: bool,
        host: EditorCommandHost,
    ) -> None:
        row = _UnitView.of(host).normal(host.editor_cursor_location)[0]
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
        current_row = _UnitView.of(host).normal(host.editor_cursor_location)[0]
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
        row = _UnitView.of(host).normal(host.editor_cursor_location)[0]
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
            host.move_editor_cursor(_UnitView.of(host).normal(cursor))
        elif text:
            host.delete_editor_range(source_range)
            if target_state is VimState.NORMAL:
                self._normalize_host_cursor(host)
        else:
            host.notify_editor_activity()
        self._state = target_state
        self._clear_pending()

    def _character_range(
        self, host: EditorCommandHost, count: int, *, before: bool, view: _UnitView | None = None
    ) -> EditorTextRange:
        """The *count* characters before or under the cursor, a unit counting as one (x/X, dl/dh)."""
        view = view or _UnitView.of(host)
        row, column = view.normal(host.editor_cursor_location)
        index = view.at((row, column))
        first, end = view.row_bounds(row)
        if before:
            return EditorTextRange(view.location(max(index - count, first)), view.location(index))
        return EditorTextRange(view.location(index), view.location(min(index + count, end)))

    def _delete_characters(self, *, before: bool, count: int, host: EditorCommandHost) -> None:
        source_range = self._character_range(host, count, before=before)
        deleted = host.read_editor_range(source_range)
        if deleted:
            self._register = EditorRegister(deleted)
            host.delete_editor_range(source_range)
            self._normalize_host_cursor(host)
        else:
            host.notify_editor_activity()

    def _join_lines(self, count: int, host: EditorCommandHost) -> None:
        text = host.editor_text
        lines = _lines(normalize_line_endings(text) if "\r" in text else text)
        row = min(max(host.editor_cursor_location[0], 0), len(lines) - 1)
        end_row = min(row + max(count, 2) - 1, len(lines) - 1)
        if end_row == row:
            host.notify_editor_activity()
            return
        first_content = lines[row].rstrip()
        cursor_column = len(first_content)
        rows_with_units = _UnitView.of(host).rows()
        if row in rows_with_units:
            start = (row, cursor_column)
            prefix = ""
        else:
            start = (row, 0)
            prefix = first_content
        unit_rows = [next_row for next_row in range(row + 1, end_row + 1) if next_row in rows_with_units]
        replacements: list[tuple[EditorTextRange, str]] = []
        segment_row = row
        for unit_row in unit_rows:
            line = lines[unit_row]
            content_start = _first_non_blank(line)
            # Ordinary lines can join their surrounding seams in one edit.
            replacement = " ".join([prefix, *map(str.strip, lines[segment_row + 1 : unit_row]), ""])
            replacements.append((EditorTextRange(start, (unit_row, content_start)), replacement))
            start = (unit_row, max(len(line.rstrip()), content_start))
            segment_row = unit_row
            prefix = ""
        end = (end_row, len(lines[end_row]))
        if start < end:
            replacement = " ".join([prefix, *map(str.strip, lines[segment_row + 1 : end_row + 1])])
            replacements.append((EditorTextRange(start, end), replacement))
        host.replace_editor_ranges(replacements)
        # Joining leaves a seam space after the first line's content.
        host.move_editor_cursor((row, cursor_column))

    def _toggle_case(self, count: int, host: EditorCommandHost) -> None:
        view = _UnitView.of(host)
        text = view.source
        row, column = view.normal(host.editor_cursor_location)
        line = view.lines[row].removesuffix("\r")
        units = view.columns(row)
        start = next((low for low, high in units if low < column < high), column)
        end = start
        span_index = 0
        while span_index < len(units) and units[span_index][1] <= start:
            span_index += 1
        run_start = start
        pieces: list[str] = []
        replacements: list[tuple[EditorTextRange, str]] = []
        for _ in range(count):
            if end >= len(line):
                break
            if span_index < len(units) and units[span_index][0] == end:
                if pieces:
                    replacements.append((EditorTextRange((row, run_start), (row, end)), "".join(pieces)))
                    pieces = []
                span_end = units[span_index][1]
                end = span_end
                run_start = end
                span_index += 1
                continue
            character = line[end]
            toggled = character.lower() if character.isupper() else character.upper()
            pieces.append(toggled)
            end += 1
        if pieces:
            replacements.append((EditorTextRange((row, run_start), (row, end)), "".join(pieces)))
        if end == start:
            host.notify_editor_activity()
            return
        if not replacements:
            # Keep the no-change undo step when the counted range contains only units.
            replacements.append((EditorTextRange((row, start), (row, end)), line[start:end]))
        host.replace_editor_ranges(replacements)
        new_end = end + len(host.editor_text) - len(text)
        host.move_editor_cursor(_UnitView.of(host).normal((row, new_end)))

    def _paste(self, *, after: bool, count: int, host: EditorCommandHost) -> EditorStatus:
        register = self._register
        if not register.text:
            host.notify_editor_activity()
            return ""
        view = _UnitView.of(host)
        text = view.source
        inserted_character_count = len(register.text) * count
        if inserted_character_count > MESSAGE_EDITOR_MAX_CHARACTERS - len(text):
            host.notify_editor_activity()
            return MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS.bind()
        row, column = view.normal(host.editor_cursor_location)
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
        location = view.location(min(view.at((row, column)) + 1, view.row_bounds(row)[1])) if after else (row, column)
        insertion_offset = location_to_offset(text, location)
        host.replace_editor_range(EditorTextRange(location, location), payload)
        target = offset_to_location(host.editor_text, insertion_offset + len(payload) - 1)
        host.move_editor_cursor(_UnitView.of(host).normal(target))
        return ""

    def _resolve_text_object(
        self,
        text_object: VimTextObject,
        *,
        around: bool,
        count: int,
        host: EditorCommandHost,
    ) -> EditorTextRange | None:
        view = _UnitView.of(host)
        text = view.text
        cursor_offset = view.at(view.normal(host.editor_cursor_location))
        if text_object in {VimTextObject.WORD, VimTextObject.WORD_BIG}:
            offsets = self._word_object(
                text,
                cursor_offset,
                around=around,
                big=text_object is VimTextObject.WORD_BIG,
                count=count,
            )
        elif text_object in {VimTextObject.DOUBLE_QUOTE, VimTextObject.SINGLE_QUOTE, VimTextObject.BACKTICK}:
            offsets = self._quote_object(text, cursor_offset, text_object.value, around=around)
        else:
            delimiters = {
                VimTextObject.PAREN: ("(", ")"),
                VimTextObject.BRACKET: ("[", "]"),
                VimTextObject.BRACE: ("{", "}"),
            }
            opening, closing = delimiters[text_object]
            offsets = self._pair_object(text, cursor_offset, opening, closing, around=around, count=count)
        if offsets is None:
            return None
        return EditorTextRange(view.location(offsets[0]), view.location(offsets[1]))

    def _word_object(
        self,
        text: str,
        cursor: int,
        *,
        around: bool,
        big: bool,
        count: int,
    ) -> tuple[int, int] | None:
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
        while start > 0 and _extends(text[start - 1], character_class, big=big):
            start -= 1
        while end < len(text) and _extends(text[end], character_class, big=big):
            end += 1
        for _ in range(count - 1):
            while end < len(text) and text[end].isspace():
                end += 1
            if end >= len(text):
                break
            character_class = _token_class(text[end], big=big)
            end += 1
            while end < len(text) and _extends(text[end], character_class, big=big):
                end += 1
        if around:
            if end < len(text) and text[end].isspace():
                while end < len(text) and text[end].isspace():
                    end += 1
            else:
                while start > 0 and text[start - 1].isspace():
                    start -= 1
        return start, end

    def _quote_object(self, text: str, cursor: int, quote: str, *, around: bool) -> tuple[int, int] | None:
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
        return start, end

    def _pair_object(
        self,
        text: str,
        cursor: int,
        opening: str,
        closing: str,
        *,
        around: bool,
        count: int,
    ) -> tuple[int, int] | None:
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
        return start, end

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
        host.move_editor_cursor(_UnitView.of(host).normal(host.editor_cursor_location))

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
        host.move_editor_cursor(_UnitView.of(host).normal(target))

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
