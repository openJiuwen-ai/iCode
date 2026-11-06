# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reusable multi-line message editor with pluggable keymaps."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from textual.message import Message
from textual.widgets import TextArea
from textual.widgets.text_area import EditResult

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets.editor.highlighter import MarkdownHighlightedTextArea
from chrys.app.tui.widgets.editor.keymaps import DEFAULT_EDITOR_KEYMAP_REGISTRY, EditorKeymapRegistry
from chrys.app.tui.widgets.editor.types import (
    MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS,
    MESSAGE_EDITOR_MAX_CHARACTERS,
    EditorBufferSnapshot,
    EditorCommand,
    EditorIntent,
    EditorKeymap,
    EditorKeyResult,
    EditorKeyStroke,
    EditorLocation,
    EditorMode,
    EditorScrollDirection,
    EditorStatus,
    EditorTextRange,
)
from chrys.app.tui.widgets.text_area import MESSAGE_EDITOR_PASTE_MAX_TOKENS
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual import events


_EDITOR_CHARACTER_LIMIT_TITLE = msg(
    "tui.editor.title.character_limit",
    fallback="Editor character limit",
)
_EDITOR_CHARACTER_LIMIT = msg(
    "tui.editor.character_limit",
    fallback="Editor content is limited to {limit} characters.",
)
_EDITOR_PASTE_REQUIRES_INSERT = msg(
    "tui.editor.status.paste_requires_insert",
    fallback="Paste requires Insert mode",
)


class MessageEditor(MarkdownHighlightedTextArea):
    """Modal message canvas that delegates unhandled Standard keys to Textual."""

    class IntentRequested(Message):
        """Posted when a keymap requests a dialog-level action."""

        def __init__(self, intent: EditorIntent, status: EditorStatus = "", *, warning: bool = False) -> None:
            self.intent = intent
            self.status = status
            self.warning = warning
            super().__init__()

    class ModeChanged(Message):
        """Posted after the active keymap changes."""

        def __init__(self, mode: EditorMode) -> None:
            self.mode = mode
            super().__init__()

    class EditActivity(Message):
        """Posted for editing activity that should disarm discard confirmation."""

    class StatusChanged(Message):
        """Posted when mode-local status text changes."""

        def __init__(self, status: EditorStatus, *, warning: bool = False) -> None:
            self.status = status
            self.warning = warning
            super().__init__()

    def __init__(
        self,
        *,
        text: str = "",
        cursor_location: EditorLocation = (0, 0),
        mode: EditorMode | str = EditorMode.STANDARD,
        max_paste_tokens: int | None = MESSAGE_EDITOR_PASTE_MAX_TOKENS,
        registry: EditorKeymapRegistry | None = None,
        **kwargs: Any,
    ) -> None:
        if len(text) > MESSAGE_EDITOR_MAX_CHARACTERS:
            raise ValueError(f"Message editor text exceeds {MESSAGE_EDITOR_MAX_CHARACTERS:,} characters")
        self._registry = registry or DEFAULT_EDITOR_KEYMAP_REGISTRY
        self._keymap: EditorKeymap | None = self._registry.create(mode)
        self._initial_cursor_location = cursor_location
        self._entry_text = text
        self._status: EditorStatus = ""
        self._status_warning = False
        self._status_is_character_limit = False
        self._character_limit_notified = False
        super().__init__(text=text, max_paste_tokens=max_paste_tokens, **kwargs)

    @property
    def mode(self) -> EditorMode:
        """Return the active keymap mode."""
        return self._active_keymap().mode

    @property
    def status_text(self) -> EditorStatus:
        """Return transient feedback or the active keymap state label."""
        return self._status or self.markdown_highlight_status or self._active_keymap().state_label

    @property
    def status_warning(self) -> bool:
        """Whether the current transient status should use warning styling."""
        return bool(self._status) and self._status_warning

    @property
    def active_keymap(self) -> EditorKeymap:
        """Expose the active pure keymap for state-aware dialog behavior and tests."""
        return self._active_keymap()

    def set_mode(self, mode: EditorMode | str) -> None:
        """Replace the keymap with a fresh registry instance."""
        keymap = self._registry.create(mode)
        current = self._active_keymap()
        if keymap.mode == current.mode:
            current.reset()
            self.move_cursor(self.cursor_location)
            current.normalize_cursor(self)
            self._set_status("")
            return
        current.reset()
        self.move_cursor(self.cursor_location)
        self._keymap = keymap
        keymap.normalize_cursor(self)
        self._set_status("")
        self.post_message(self.ModeChanged(keymap.mode))

    def snapshot(self) -> EditorBufferSnapshot:
        """Capture editor text and cursor without changing either."""
        return EditorBufferSnapshot(self.text, self.cursor_location)

    def load_text(self, text: str) -> None:
        """Replace the document while enforcing the editor's hard ceiling."""
        if len(text) > MESSAGE_EDITOR_MAX_CHARACTERS:
            raise ValueError(f"Message editor text exceeds {MESSAGE_EDITOR_MAX_CHARACTERS:,} characters")
        super().load_text(text)

    def insert(
        self,
        text: str,
        location: EditorLocation | None = None,
        *,
        maintain_selection_offset: bool = True,
    ) -> EditResult:
        """Insert without allowing the editor document to exceed its hard ceiling."""
        target = self.cursor_location if location is None else location
        limited = self._limit_inserted_text(text, target, target)
        return super().insert(limited, location, maintain_selection_offset=maintain_selection_offset)

    def replace(
        self,
        insert: str,
        start: EditorLocation,
        end: EditorLocation,
        *,
        maintain_selection_offset: bool = True,
    ) -> EditResult:
        """Replace a range without allowing the document to exceed its hard ceiling."""
        limited = self._limit_inserted_text(insert, start, end)
        return super().replace(limited, start, end, maintain_selection_offset=maintain_selection_offset)

    def dispatch_keymap_stroke(self, stroke: EditorKeyStroke) -> EditorKeyResult:
        """Dispatch a pure stroke and synchronize dynamic keymap status."""
        result = self._active_keymap().handle_key(stroke, self)
        if result.consumed:
            self._set_status(
                result.status,
                warning=result.warning,
                force=True,
                character_limit=result.character_limit,
            )
        return result

    def on_mount(self) -> None:
        self.move_cursor(self._clamp_location(self._initial_cursor_location))
        self._active_keymap().normalize_cursor(self)

    def on_unmount(self) -> None:
        self._active_keymap().reset()
        self._keymap = None

    async def _on_key(self, event: events.Key) -> None:
        result = self.dispatch_keymap_stroke(EditorKeyStroke(event.key, event.character))
        if result.consumed:
            event.prevent_default()
            event.stop()
            if result.intent is not EditorIntent.NONE:
                self.emit_editor_intent(result.intent, result.status, warning=result.warning)
            return

        tab_edit = event.key == "tab" and self.tab_behavior == "indent"
        editing_attempt = tab_edit or (
            event.key not in {"tab", "shift+tab"}
            and (
                event.character is not None
                or event.key
                in {
                    "backspace",
                    "ctrl+shift+v",
                    "ctrl+v",
                    "ctrl+x",
                    "ctrl+y",
                    "ctrl+z",
                    "delete",
                    "enter",
                    "shift+insert",
                    "shift+enter",
                    "super+v",
                    "super+x",
                }
            )
        )
        if editing_attempt:
            self._active_keymap().prepare_delegated_edit()
        await super()._on_key(event)
        if editing_attempt:
            self.notify_editor_activity()

    async def _on_paste(self, event: events.Paste) -> None:
        if not self._active_keymap().prepare_delegated_paste():
            event.prevent_default()
            event.stop()
            self._set_status(_EDITOR_PASTE_REQUIRES_INSERT.bind(), warning=True, force=True)
            return
        await super()._on_paste(event)
        self.notify_editor_activity()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if event.text_area is not self:
            return
        if len(self.text) < MESSAGE_EDITOR_MAX_CHARACTERS and self._character_limit_notified:
            self._character_limit_notified = False
            if self._status_is_character_limit:
                self._set_status("")
        self.notify_editor_activity()

    @property
    def editor_text(self) -> str:
        return self.text

    @property
    def editor_cursor_location(self) -> EditorLocation:
        return self.cursor_location

    @property
    def editor_selection(self) -> EditorTextRange:
        start, end = self.selection
        return EditorTextRange(start, end)

    @property
    def editor_page_height(self) -> int:
        return max(self.content_size.height, 1)

    @property
    def editor_is_dirty(self) -> bool:
        return self.text != self._entry_text

    def move_editor_cursor(self, location: EditorLocation, *, select: bool = False) -> None:
        self.move_cursor(self._clamp_location(location), select=select)

    def set_editor_selection(self, anchor: EditorLocation, active: EditorLocation) -> None:
        self.move_cursor(self._clamp_location(anchor))
        self.move_cursor(self._clamp_location(active), select=True)

    def execute_editor_command(self, command: EditorCommand, *, select: bool = False) -> None:
        """Dispatch a typed keymap command without reflective first-party lookup."""
        match command:
            case EditorCommand.CURSOR_LEFT:
                self.action_cursor_left(select)
            case EditorCommand.CURSOR_RIGHT:
                self.action_cursor_right(select)
            case EditorCommand.CURSOR_UP:
                self.action_cursor_up(select)
            case EditorCommand.CURSOR_DOWN:
                self.action_cursor_down(select)
            case EditorCommand.LINE_START:
                self.action_cursor_line_start(select)
            case EditorCommand.LINE_END:
                self.action_cursor_line_end(select)
            case EditorCommand.WORD_LEFT:
                self.action_cursor_word_left(select)
            case EditorCommand.WORD_RIGHT:
                self.action_cursor_word_right(select)
            case EditorCommand.DOCUMENT_START:
                self.move_cursor((0, 0), select=select)
            case EditorCommand.DOCUMENT_END:
                self.move_cursor(self.document.end, select=select)
            case EditorCommand.DELETE_LEFT:
                self.action_delete_left()
            case EditorCommand.DELETE_RIGHT:
                self.action_delete_right()
            case EditorCommand.UNDO:
                self.editor_undo()
            case EditorCommand.REDO:
                self.editor_redo()
            case EditorCommand.PAGE_UP:
                self.action_cursor_page_up()
            case EditorCommand.PAGE_DOWN:
                self.action_cursor_page_down()

    def read_editor_range(self, source_range: EditorTextRange) -> str:
        return self.get_text_range(source_range.start, source_range.end)

    def replace_editor_range(self, source_range: EditorTextRange, text: str) -> None:
        self.history.checkpoint()
        self.replace(text, source_range.start, source_range.end, maintain_selection_offset=False)
        self.history.checkpoint()
        self.notify_editor_activity()

    def delete_editor_range(self, source_range: EditorTextRange) -> None:
        self.history.checkpoint()
        self.delete(source_range.start, source_range.end, maintain_selection_offset=False)
        self.history.checkpoint()
        self.notify_editor_activity()

    def editor_atomic_spans(self, row: int) -> list[tuple[int, int]]:
        del row
        return []

    def editor_undo(self) -> None:
        self.action_undo()
        self.notify_editor_activity()

    def editor_redo(self) -> None:
        self.action_redo()
        self.notify_editor_activity()

    def scroll_editor_page(self, direction: EditorScrollDirection) -> None:
        if direction is EditorScrollDirection.UP:
            self.action_cursor_page_up()
        else:
            self.action_cursor_page_down()

    def notify_editor_activity(self) -> None:
        self.post_message(self.EditActivity())

    def emit_editor_intent(self, intent: EditorIntent, status: EditorStatus = "", *, warning: bool = False) -> None:
        self.post_message(self.IntentRequested(intent, status, warning=warning))

    def _set_status(
        self,
        status: EditorStatus,
        *,
        warning: bool = False,
        force: bool = False,
        character_limit: bool = False,
    ) -> None:
        if self._status == status and self._status_warning == warning and not force:
            return
        self._status = status
        self._status_warning = warning
        self._status_is_character_limit = character_limit and bool(status)
        self.post_message(self.StatusChanged(self.status_text, warning=self.status_warning))

    def _on_markdown_highlight_status_changed(self) -> None:
        self.post_message(self.StatusChanged(self.status_text, warning=self.status_warning))

    def _active_keymap(self) -> EditorKeymap:
        keymap = self._keymap
        if keymap is None:
            raise RuntimeError("MessageEditor keymap is unavailable after unmount")
        return keymap

    def _limit_inserted_text(
        self,
        insert: str,
        start: EditorLocation,
        end: EditorLocation,
    ) -> str:
        retained_characters = len(self.text) - len(self.get_text_range(start, end))
        available = max(MESSAGE_EDITOR_MAX_CHARACTERS - retained_characters, 0)
        if len(insert) <= available:
            return insert
        self._set_status(MESSAGE_EDITOR_CHARACTER_LIMIT_STATUS.bind(), warning=True, force=True, character_limit=True)
        if not self._character_limit_notified:
            self._character_limit_notified = True
            localizer = widget_localizer(self)
            self.notify(
                render_str(
                    localizer,
                    _EDITOR_CHARACTER_LIMIT.bind(limit=f"{MESSAGE_EDITOR_MAX_CHARACTERS:,}"),
                ),
                title=render_str(localizer, _EDITOR_CHARACTER_LIMIT_TITLE.bind()),
                severity="warning",
                markup=False,
            )
        return insert[:available]

    def _clamp_location(self, location: EditorLocation) -> EditorLocation:
        row = min(max(location[0], 0), self.document.line_count - 1)
        column = min(max(location[1], 0), len(self.document.get_line(row)))
        return row, column
