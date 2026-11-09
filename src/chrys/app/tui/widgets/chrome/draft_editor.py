# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Edit a chat draft's sending text with the input box's whole-field rules.

:class:`DraftEditor` is a :class:`MessageEditor` (keymaps, Markdown, the
character ceiling) that edits a whole :class:`ChatDraft` by the input box's
own rules (:class:`DraftTextArea`): prompt fields stay whole and a click
selects a field. Pastes stay plain text and images become file mentions.
The editor's key handlers reach the document only through
its range methods and ``edit()``, so the rules hold for Vim and Emacs too:
a range read for a register is the range an edit takes, so a yank, a kill
or a delete never holds half a field.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

from textual.widgets.text_area import Selection

from chrys.app.tui.widgets.chrome.draft_text_area import DRAFT_COMPONENT_CLASSES, DraftTextArea, draft_text_area_css
from chrys.app.tui.widgets.editor import EditorTextRange, MessageEditor
from chrys.foundation.text.draft_references import unit_safe_range
from chrys.foundation.text.mentions import format_file_mention

if TYPE_CHECKING:
    from pathlib import Path

    from textual import events

    from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft


class DraftEditor(DraftTextArea, MessageEditor):
    """Edit a chat draft in the message editor, by the input box's rules (module docstring)."""

    COMPONENT_CLASSES: ClassVar[set[str]] = set(DRAFT_COMPONENT_CLASSES)
    FOLD_PASTES: ClassVar[bool] = False

    DEFAULT_CSS = draft_text_area_css("DraftEditor")

    def __init__(
        self,
        draft: ChatDraft,
        *,
        paste_cwd: str,
        clipboard_image_dir: Path | None,
        **kwargs: Any,
    ) -> None:
        super().__init__(text=draft.text, **kwargs)
        self.set_paste_cwd(paste_cwd)
        self.set_clipboard_image_dir(clipboard_image_dir)
        self.set_prompt_fields(draft.fields, select=False)
        self._draft_owner = draft.owner
        self._entry_draft = self.chat_draft()

    @property
    def editor_is_dirty(self) -> bool:
        """Whether the whole draft differs from the expanded draft the editor opened on."""
        return self.chat_draft() != self._entry_draft

    def read_editor_range(self, source_range: EditorTextRange) -> str:
        # A register or kill ring gets what a delete of the same range removes.
        return super().read_editor_range(self._unit_safe_range(source_range))

    def editor_unit_spans(self) -> list[tuple[int, int]]:
        if not self._prompt_fields:
            return []
        return self._unit_spans()

    def _accepts_text(self) -> bool:
        return self._active_keymap().accepts_text()

    def _unit_safe_range(self, source_range: EditorTextRange) -> EditorTextRange:
        document = self.document
        first, last = sorted((source_range.start, source_range.end))
        start, end = unit_safe_range(
            self._unit_spans(),
            document.get_index_from_location(first),
            document.get_index_from_location(last),
        )
        return EditorTextRange(document.get_location_from_index(start), document.get_location_from_index(end))

    def _validate_selection(self, selection: Selection) -> Selection:
        # Where text cannot be typed (Vim Normal), the cursor rests on a character,
        # never past a line's last one: a unit that ends the line is one character
        # there, so a cursor it pushed to the line's end rests on its start. A
        # selection (Vim Visual) keeps its exclusive end after the unit, so the
        # unit shows selected when a yank or delete would take it.
        validated = super()._validate_selection(selection)
        if not self._prompt_fields or not selection.is_empty or validated.end == selection.end or self._accepts_text():
            return validated
        row, column = validated.end
        line = self.document[row]
        if column == len(line):
            for start, end in self._unit_spans(row):
                if end == column:
                    return Selection.cursor(self._unit_edge((row, start), forward=False))
        return validated

    def _images_for_insert(self, paths: Sequence[str]) -> str:
        return self._spaced_for_insert(" ".join(format_file_mention(path) for path in paths), trailing_space=False)

    async def _on_key(self, event: events.Key) -> None:
        # As in the input box, Tab goes to the next prompt field while text can
        # be typed; otherwise the editor's own Tab (indent, or the keymap's).
        if event.key == "tab" and self.prompt_fields and self._accepts_text() and self.select_next_prompt_field():
            event.prevent_default()
            event.stop()


__all__ = ["DraftEditor"]
