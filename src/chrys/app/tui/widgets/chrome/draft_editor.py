# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The message editor for a chat draft: the input box made bigger.

:class:`DraftEditor` is a :class:`MessageEditor` (keymaps, Markdown, the
character ceiling) that edits a whole :class:`ChatDraft` by the input box's
own rules (:class:`DraftTextArea`): labels stay one unit and open on a
click, prompt fields follow edits and Tab, long pastes fold and images
become labels. The editor's key handlers reach the document only through
its range methods and ``edit()``, so the rules hold for Vim and Emacs too:
a range read for a register is the range an edit takes, so a yank, a kill
or a delete never holds half a label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

from textual.widgets.text_area import Selection

from chrys.app.tui.widgets.chrome.draft_text_area import DRAFT_COMPONENT_CLASSES, DraftTextArea, draft_text_area_css
from chrys.app.tui.widgets.editor import EditorTextRange, MessageEditor
from chrys.foundation.text.draft_references import iter_reference_labels, label_safe_range

if TYPE_CHECKING:
    from pathlib import Path

    from textual import events

    from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft
    from chrys.app.tui.widgets.editor.types import EditorLocation


class DraftEditor(DraftTextArea, MessageEditor):
    """Edit a chat draft in the message editor, by the input box's rules (module docstring)."""

    COMPONENT_CLASSES: ClassVar[set[str]] = set(DRAFT_COMPONENT_CLASSES)

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
        self._set_references(draft.references)
        self.set_prompt_fields(draft.fields, select=False)
        self._draft_owner = draft.owner
        self._entry_draft = self.chat_draft()

    @property
    def editor_is_dirty(self) -> bool:
        """Whether the draft differs from the one the editor opened on, fields and labels included."""
        return self.chat_draft() != self._entry_draft

    def read_editor_range(self, source_range: EditorTextRange) -> str:
        # A register or kill ring gets what a delete of the same range removes.
        return super().read_editor_range(self._label_safe_range(source_range))

    def editor_atomic_spans(self, row: int) -> list[tuple[int, int]]:
        return [(label.start, label.end) for label in iter_reference_labels(self.document[row], self._references)]

    def _label_safe_range(self, source_range: EditorTextRange) -> EditorTextRange:
        document = self.document
        first, last = sorted((source_range.start, source_range.end))
        start, end = label_safe_range(
            self.text,
            self._references,
            document.get_index_from_location(first),
            document.get_index_from_location(last),
        )
        return EditorTextRange(document.get_location_from_index(start), document.get_location_from_index(end))

    def _validate_selection(self, selection: Selection) -> Selection:
        # Where text cannot be typed (Vim Normal), the cursor rests on a character,
        # never past a line's last one: a label that ends the line is one character
        # there, so a cursor it pushed to the line's end rests on its start. A
        # selection (Vim Visual) keeps its exclusive end after the label, so the
        # label shows selected when a yank or delete would take it.
        validated = super()._validate_selection(selection)
        if (
            not self._references
            or not selection.is_empty
            or validated.end == selection.end
            or self._active_keymap().accepts_text()
        ):
            return validated
        row, column = validated.end
        line = self.document[row]
        if column == len(line):
            for label in iter_reference_labels(line, self._references):
                if label.end == column:
                    return Selection.cursor((row, label.start))
        return validated

    def _limit_inserted_text(self, insert: str, start: EditorLocation, end: EditorLocation) -> str:
        """Cut *insert* to the character ceiling, never inside one of its live labels."""
        limited = super()._limit_inserted_text(insert, start, end)
        if len(limited) == len(insert):
            return limited
        cut = len(limited)
        for label in iter_reference_labels(insert, self._references):
            if label.start < cut < label.end:
                return insert[: label.start]
        return limited

    async def _on_key(self, event: events.Key) -> None:
        # As in the input box, Tab goes to the next prompt field while text can
        # be typed; otherwise the editor's own Tab (indent, or the keymap's).
        if (
            event.key == "tab"
            and self.prompt_fields
            and self._active_keymap().accepts_text()
            and self.select_next_prompt_field()
        ):
            event.prevent_default()
            event.stop()


__all__ = ["DraftEditor"]
