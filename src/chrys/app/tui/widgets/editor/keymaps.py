# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Table-driven Standard and Emacs editor keymaps."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from chrys.app.tui.widgets.editor.coordinates import location_to_offset, offset_to_location
from chrys.app.tui.widgets.editor.types import (
    COMMON_EDITOR_INTENTS,
    EditorCommand,
    EditorCommandHost,
    EditorIntent,
    EditorKeymap,
    EditorKeyResult,
    EditorKeyStroke,
    EditorLocation,
    EditorMode,
    EditorStatus,
    EditorTextRange,
)
from chrys.app.tui.widgets.editor.vim import VimKeymap
from chrys.foundation.i18n import msg

type EditorKeymapFactory = Callable[[], EditorKeymap]

_EDITOR_READY = msg("tui.editor.status.ready", fallback="Ready")
_EDITOR_SELECTION_ACTIVE = msg("tui.editor.status.selection_active", fallback="Selection active")

_EMACS_COMMANDS: Mapping[str, EditorCommand] = MappingProxyType(
    {
        "ctrl+b": EditorCommand.CURSOR_LEFT,
        "ctrl+f": EditorCommand.CURSOR_RIGHT,
        "ctrl+p": EditorCommand.CURSOR_UP,
        "ctrl+n": EditorCommand.CURSOR_DOWN,
        "ctrl+a": EditorCommand.LINE_START,
        "ctrl+e": EditorCommand.LINE_END,
        "alt+b": EditorCommand.WORD_LEFT,
        "alt+f": EditorCommand.WORD_RIGHT,
        "alt+less_than": EditorCommand.DOCUMENT_START,
        "alt+<": EditorCommand.DOCUMENT_START,
        "alt+greater_than": EditorCommand.DOCUMENT_END,
        "alt+>": EditorCommand.DOCUMENT_END,
        "ctrl+v": EditorCommand.PAGE_DOWN,
        "alt+v": EditorCommand.PAGE_UP,
        "ctrl+/": EditorCommand.UNDO,
        # Legacy terminals encode Ctrl+/ as the same 0x1F control character
        # as Ctrl+_, while extended keyboard protocols preserve the slash.
        "ctrl+underscore": EditorCommand.UNDO,
        "ctrl+slash": EditorCommand.UNDO,
    }
)


def _consumed(intent: EditorIntent = EditorIntent.NONE, status: EditorStatus = "") -> EditorKeyResult:
    return EditorKeyResult(consumed=True, intent=intent, status=status)


def _lines(text: str) -> list[str]:
    return text.split("\n")


def _ordered_range(text: str, first: EditorLocation, second: EditorLocation) -> EditorTextRange:
    if location_to_offset(text, first) <= location_to_offset(text, second):
        return EditorTextRange(first, second)
    return EditorTextRange(second, first)


def _previous_word_offset(text: str, offset: int) -> int:
    index = min(max(offset, 0), len(text))
    while index > 0 and text[index - 1].isspace():
        index -= 1
    if index > 0 and (text[index - 1].isalnum() or text[index - 1] == "_"):
        while index > 0 and (text[index - 1].isalnum() or text[index - 1] == "_"):
            index -= 1
    else:
        while index > 0 and not text[index - 1].isspace() and not (text[index - 1].isalnum() or text[index - 1] == "_"):
            index -= 1
    return index


def _next_word_offset(text: str, offset: int) -> int:
    index = min(max(offset, 0), len(text))
    while index < len(text) and text[index].isspace():
        index += 1
    if index < len(text) and (text[index].isalnum() or text[index] == "_"):
        while index < len(text) and (text[index].isalnum() or text[index] == "_"):
            index += 1
    elif index < len(text) and not text[index].isspace():
        while index < len(text) and not text[index].isspace() and not (text[index].isalnum() or text[index] == "_"):
            index += 1
    return index


class StandardKeymap:
    """Delegate ordinary editing to Textual and own only modal commands."""

    @property
    def mode(self) -> EditorMode:
        return EditorMode.STANDARD

    @property
    def state_label(self) -> EditorStatus:
        return _EDITOR_READY.bind()

    def handle_key(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        _ = host
        if intent := COMMON_EDITOR_INTENTS.get(stroke.key):
            return _consumed(intent)
        if stroke.key == "escape":
            return _consumed(EditorIntent.REQUEST_ESCAPE_CANCEL)
        return EditorKeyResult()

    def reset(self) -> None:
        return

    def normalize_cursor(self, host: EditorCommandHost) -> None:
        """Retain Textual's insertion-point cursor semantics."""
        _ = host

    def prepare_delegated_edit(self) -> None:
        """Standard mode has no modal state to clear before an edit."""

    def prepare_delegated_paste(self) -> bool:
        """Allow ordinary Textual paste handling."""
        return True

    def accepts_text(self) -> bool:
        """Standard mode always types into the document."""
        return True


class EmacsKeymap:
    """Emacs navigation, mark/region, and a modal-local kill ring."""

    def __init__(self) -> None:
        self._mark: EditorLocation | None = None
        self._kill_ring = ""
        self._last_command_was_kill = False

    @property
    def mode(self) -> EditorMode:
        return EditorMode.EMACS

    @property
    def state_label(self) -> EditorStatus:
        definition = _EDITOR_SELECTION_ACTIVE if self._mark is not None else _EDITOR_READY
        return definition.bind()

    @property
    def mark(self) -> EditorLocation | None:
        """Return the mark for pure state tests and extension diagnostics."""
        return self._mark

    @property
    def kill_ring(self) -> str:
        """Return the modal-local kill text."""
        return self._kill_ring

    def handle_key(self, stroke: EditorKeyStroke, host: EditorCommandHost) -> EditorKeyResult:
        if stroke.key not in {"alt+d", "ctrl+k", "ctrl+w"}:
            self._last_command_was_kill = False
        if intent := COMMON_EDITOR_INTENTS.get(stroke.key):
            return _consumed(intent)
        if stroke.key == "escape":
            return _consumed(EditorIntent.REQUEST_ESCAPE_CANCEL)
        if command := _EMACS_COMMANDS.get(stroke.key):
            host.execute_editor_command(command, select=self._mark is not None)
            if command in {EditorCommand.UNDO, EditorCommand.REDO}:
                self._clear_mark(host)
            return _consumed()
        if stroke.key in {"ctrl+space", "ctrl+@"}:
            self._mark = host.editor_cursor_location
            host.move_editor_cursor(self._mark)
            return _consumed()
        if stroke.key == "ctrl+g":
            self._clear_mark(host)
            return _consumed()
        if stroke.key in {"backspace", "ctrl+h"}:
            self._delete_left(host)
            return _consumed()
        if stroke.key == "ctrl+d":
            self._delete_right(host)
            return _consumed()
        if stroke.key == "alt+d":
            self._kill_next_word(host)
            return _consumed()
        if stroke.key == "alt+w":
            self._copy_region(host)
            return _consumed()
        if stroke.key == "ctrl+w":
            self._kill_region_or_previous_word(host)
            return _consumed()
        if stroke.key == "ctrl+k":
            self._kill_line_end(host)
            return _consumed()
        if stroke.key == "ctrl+y":
            self._yank(host)
            return _consumed()
        return EditorKeyResult()

    def reset(self) -> None:
        self._mark = None
        self._kill_ring = ""
        self._last_command_was_kill = False

    def normalize_cursor(self, host: EditorCommandHost) -> None:
        """Retain Textual's insertion-point cursor semantics."""
        _ = host

    def prepare_delegated_edit(self) -> None:
        """Deactivate a region before Textual performs an unowned edit."""
        self._mark = None
        self._last_command_was_kill = False

    def prepare_delegated_paste(self) -> bool:
        """Allow paste after deactivating any Emacs region."""
        self.prepare_delegated_edit()
        return True

    def accepts_text(self) -> bool:
        """Emacs mode always types into the document."""
        return True

    def _active_region(self, host: EditorCommandHost) -> EditorTextRange | None:
        mark = self._mark
        if mark is None or mark == host.editor_cursor_location:
            return None
        return _ordered_range(host.editor_text, mark, host.editor_cursor_location)

    def _clear_mark(self, host: EditorCommandHost) -> None:
        self._mark = None
        host.move_editor_cursor(host.editor_cursor_location)

    def _delete_left(self, host: EditorCommandHost) -> None:
        text = host.editor_text
        cursor_offset = location_to_offset(text, host.editor_cursor_location)
        if cursor_offset == 0:
            host.notify_editor_activity()
            return
        host.delete_editor_range(
            EditorTextRange(offset_to_location(text, cursor_offset - 1), host.editor_cursor_location)
        )
        self._mark = None

    def _delete_right(self, host: EditorCommandHost) -> None:
        text = host.editor_text
        cursor_offset = location_to_offset(text, host.editor_cursor_location)
        if cursor_offset >= len(text):
            host.notify_editor_activity()
            return
        host.delete_editor_range(
            EditorTextRange(host.editor_cursor_location, offset_to_location(text, cursor_offset + 1))
        )
        self._mark = None

    def _copy_region(self, host: EditorCommandHost) -> None:
        region = self._active_region(host)
        if region is None:
            return
        self._kill_ring = host.read_editor_range(region)
        self._last_command_was_kill = False
        self._clear_mark(host)

    def _kill_region_or_previous_word(self, host: EditorCommandHost) -> None:
        region = self._active_region(host)
        backward = True
        if region is None:
            text = host.editor_text
            cursor_offset = location_to_offset(text, host.editor_cursor_location)
            start_offset = _previous_word_offset(text, cursor_offset)
            region = EditorTextRange(offset_to_location(text, start_offset), host.editor_cursor_location)
        else:
            backward = location_to_offset(host.editor_text, host.editor_cursor_location) > location_to_offset(
                host.editor_text, self._mark or host.editor_cursor_location
            )
        self._kill(host, region, backward=backward)

    def _kill_next_word(self, host: EditorCommandHost) -> None:
        text = host.editor_text
        cursor_offset = location_to_offset(text, host.editor_cursor_location)
        end_offset = _next_word_offset(text, cursor_offset)
        self._kill(
            host,
            EditorTextRange(host.editor_cursor_location, offset_to_location(text, end_offset)),
            backward=False,
        )

    def _kill_line_end(self, host: EditorCommandHost) -> None:
        text = host.editor_text
        lines = _lines(text)
        row, column = host.editor_cursor_location
        line_end = (row, len(lines[row]))
        if column < len(lines[row]):
            region = EditorTextRange(host.editor_cursor_location, line_end)
        elif row < len(lines) - 1:
            start_offset = location_to_offset(text, line_end)
            region = EditorTextRange(line_end, offset_to_location(text, start_offset + 1))
        else:
            region = EditorTextRange(line_end, line_end)
        self._kill(host, region, backward=False)

    def _kill(self, host: EditorCommandHost, region: EditorTextRange, *, backward: bool) -> None:
        self._mark = None
        text = host.read_editor_range(region)
        if not text:
            self._last_command_was_kill = True
            host.notify_editor_activity()
            return
        if self._last_command_was_kill:
            self._kill_ring = text + self._kill_ring if backward else self._kill_ring + text
        else:
            self._kill_ring = text
        self._last_command_was_kill = True
        host.delete_editor_range(region)

    def _yank(self, host: EditorCommandHost) -> None:
        if not self._kill_ring:
            host.notify_editor_activity()
            return
        cursor = host.editor_cursor_location
        host.replace_editor_range(EditorTextRange(cursor, cursor), self._kill_ring)
        self._mark = None


def _standard_keymap() -> EditorKeymap:
    return StandardKeymap()


def _emacs_keymap() -> EditorKeymap:
    return EmacsKeymap()


def _vim_keymap() -> EditorKeymap:
    return VimKeymap()


_DEFAULT_FACTORIES: Mapping[EditorMode, EditorKeymapFactory] = MappingProxyType(
    {
        EditorMode.STANDARD: _standard_keymap,
        EditorMode.EMACS: _emacs_keymap,
        EditorMode.VIM: _vim_keymap,
    }
)


class EditorKeymapRegistry:
    """Immutable keymap-factory registry that returns fresh instances."""

    def __init__(self, factories: Mapping[EditorMode, EditorKeymapFactory] | None = None) -> None:
        configured = _DEFAULT_FACTORIES if factories is None else factories
        if EditorMode.STANDARD not in configured:
            raise ValueError("Standard editor keymap factory is required")
        self._factories: Mapping[EditorMode, EditorKeymapFactory] = MappingProxyType(dict(configured))

    def create(self, mode: EditorMode | str) -> EditorKeymap:
        """Create a fresh keymap, falling back to Standard when unavailable."""
        normalized = EditorMode.parse(mode)
        factory = self._factories.get(normalized, self._factories[EditorMode.STANDARD])
        return factory()


DEFAULT_EDITOR_KEYMAP_REGISTRY = EditorKeymapRegistry()
