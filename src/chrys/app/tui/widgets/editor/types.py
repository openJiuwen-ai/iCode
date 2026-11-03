# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure editor-mode values and keymap contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol

from chrys.foundation.i18n import MessageDef, MessageRef, msg

type EditorLocation = tuple[int, int]
type EditorStatus = MessageRef | str

MESSAGE_EDITOR_MAX_CHARACTERS: Final = 500_000
"""Maximum number of Unicode code points retained by the message editor."""

MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS: Final[MessageDef] = msg(
    "tui.editor.status.character_limit",
    fallback="Character limit reached · 500,000 maximum",
)
"""Status shown when an edit would exceed the message-editor ceiling."""


class EditorMode(StrEnum):
    """User-selectable editor keymap modes."""

    STANDARD = "standard"
    EMACS = "emacs"
    VIM = "vim"

    @classmethod
    def parse(cls, value: object) -> EditorMode:
        """Normalize *value*, falling back to Standard for invalid input."""
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value.strip().lower())
            except ValueError:
                pass
        return cls.STANDARD


class EditorIntent(StrEnum):
    """Mode-independent requests emitted by an editor keymap."""

    ACCEPT = "accept"
    REQUEST_ESCAPE_CANCEL = "request_escape_cancel"
    CANCEL_IMMEDIATE = "cancel_immediate"
    CYCLE_MODE = "cycle_mode"
    NONE = "none"


COMMON_EDITOR_INTENTS: Final[Mapping[str, EditorIntent]] = MappingProxyType(
    {
        "ctrl+o": EditorIntent.ACCEPT,
        "ctrl+enter": EditorIntent.ACCEPT,
        "f3": EditorIntent.CYCLE_MODE,
    }
)
"""Mode-independent keyboard intents shared by keymaps and dialog chrome."""


class EditorScrollDirection(StrEnum):
    """Page-scroll directions exposed by :class:`EditorCommandHost`."""

    UP = "up"
    DOWN = "down"


class EditorCommand(StrEnum):
    """Host commands used by table-driven keymaps."""

    CURSOR_LEFT = "cursor_left"
    CURSOR_RIGHT = "cursor_right"
    CURSOR_UP = "cursor_up"
    CURSOR_DOWN = "cursor_down"
    LINE_START = "line_start"
    LINE_END = "line_end"
    WORD_LEFT = "word_left"
    WORD_RIGHT = "word_right"
    DOCUMENT_START = "document_start"
    DOCUMENT_END = "document_end"
    DELETE_LEFT = "delete_left"
    DELETE_RIGHT = "delete_right"
    UNDO = "undo"
    REDO = "redo"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"


class EditorRegisterKind(StrEnum):
    """Register shapes used by future Emacs and Vim keymaps."""

    CHARACTERWISE = "characterwise"
    LINEWISE = "linewise"


@dataclass(frozen=True, slots=True)
class EditorTextRange:
    """A half-open source range in the editor document."""

    start: EditorLocation
    end: EditorLocation


@dataclass(frozen=True, slots=True)
class EditorBufferSnapshot:
    """Immutable editor text and cursor state."""

    text: str
    cursor_location: EditorLocation
    draft_revision: int = 0


@dataclass(frozen=True, slots=True)
class EditorRegister:
    """Text stored by a modal keymap without retaining its host widget."""

    text: str
    kind: EditorRegisterKind = EditorRegisterKind.CHARACTERWISE

    @property
    def linewise(self) -> bool:
        """Whether this register must paste as whole lines."""
        return self.kind is EditorRegisterKind.LINEWISE


@dataclass(frozen=True, slots=True)
class EditorKeyStroke:
    """Textual-independent key input passed to pure keymaps."""

    key: str
    character: str | None = None


@dataclass(frozen=True, slots=True)
class EditorKeyResult:
    """Result of dispatching one key through a keymap."""

    consumed: bool = False
    intent: EditorIntent = EditorIntent.NONE
    status: EditorStatus = ""
    warning: bool = False
    character_limit: bool = False


class EditorCommandHost(Protocol):
    """Narrow document surface available to editor keymaps.

    Compound edits go through ``replace_editor_range`` or
    ``delete_editor_range``; implementations isolate each call as one public
    undo batch. Keymaps receive this host only for the duration of a key
    dispatch and must never retain it.
    """

    @property
    def editor_text(self) -> str: ...

    @property
    def editor_cursor_location(self) -> EditorLocation: ...

    @property
    def editor_selection(self) -> EditorTextRange: ...

    @property
    def editor_page_height(self) -> int: ...

    @property
    def editor_is_dirty(self) -> bool: ...

    def move_editor_cursor(self, location: EditorLocation, *, select: bool = False) -> None: ...

    def set_editor_selection(self, anchor: EditorLocation, active: EditorLocation) -> None: ...

    def execute_editor_command(self, command: EditorCommand, *, select: bool = False) -> None: ...

    def read_editor_range(self, source_range: EditorTextRange) -> str: ...

    def replace_editor_range(self, source_range: EditorTextRange, text: str) -> None: ...

    def delete_editor_range(self, source_range: EditorTextRange) -> None: ...

    def editor_atomic_spans(self, row: int) -> list[tuple[int, int]]:
        """The ``[start, end)`` columns of *row* that edit only as a whole and keep their text (a draft's live labels)."""
        ...

    def editor_undo(self) -> None: ...

    def editor_redo(self) -> None: ...

    def scroll_editor_page(self, direction: EditorScrollDirection) -> None: ...

    def notify_editor_activity(self) -> None: ...

    def emit_editor_intent(self, intent: EditorIntent, status: EditorStatus = "", *, warning: bool = False) -> None: ...


class EditorKeymap(Protocol):
    """Textual-independent contract implemented by every editor keymap."""

    @property
    def mode(self) -> EditorMode: ...

    @property
    def state_label(self) -> EditorStatus: ...

    def handle_key(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult: ...

    def normalize_cursor(self, host: EditorCommandHost) -> None: ...

    def prepare_delegated_edit(self) -> None: ...

    def prepare_delegated_paste(self) -> bool: ...

    def accepts_text(self) -> bool:
        """Whether typed text goes into the document now (no pending modal command); changes nothing."""
        ...

    def reset(self) -> None: ...
