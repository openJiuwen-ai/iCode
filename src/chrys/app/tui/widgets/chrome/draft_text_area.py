# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Editing a chat draft: the rules the input box and the message editor share.

A draft is text plus what lives in it (:class:`ChatDraft`): the labels'
references, the unfilled prompt fields and the prompt shortcut it belongs to.
:class:`DraftTextArea` is the one implementation of how the input box and
the expanded message editor edit their whole units:

- labels and prompt fields are whole units (:func:`unit_boundary` for every
  cursor move, :func:`unit_safe_range` for every edit); clicking a label opens
  it, and clicking inside a prompt field selects it while text can be typed,
  or rests on its start otherwise;
- prompt fields shift with edits, undo and redo; an edit that covers a field
  ends it, unless the entire replacement writes back exactly the same text;
  writers leave unchanged units outside their ranges; undo restores fields removed
  whole by an edit, unless the draft was empty when the batch was undone;
  an edit after an undo starts a batch of its own;
- an edit that empties the draft ends its fields and owner;
- a draft that leaves (sent, or taken in by a running turn) is one undo
  step that brings it back whole, labels live (:meth:`leave_draft`);
  otherwise a reference table that is replaced rather than grown drops the
  undo steps that would bring back labels with nothing behind them;
- the input box folds long pastes and registers image labels; the expanded
  editor keeps pasted text and inserts images in their sending form.

It is a mixin for a ``TextArea`` host listed before the host class, which
declares :data:`DRAFT_COMPONENT_CLASSES` and :func:`draft_text_area_css`
itself (Textual reads both only from widget classes).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, fields
from itertools import accumulate
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.message import Message
from textual.widgets.text_area import Document, Edit, EditResult, Location, Selection, TextAreaTheme

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets.chrome.image_paste import pasted_image_paths, save_clipboard_image_to_file
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, PromptField, ShortcutIntent, shift_prompt_fields
from chrys.foundation.i18n import msg
from chrys.foundation.platform import safe_getcwd
from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
    DraftReference,
    DraftReferences,
    ReferenceKind,
    iter_reference_labels,
    paste_folds,
    unit_boundary,
    unit_safe_range,
)
from chrys.foundation.text.lines import normalize_line_endings

if TYPE_CHECKING:
    from textual import events

    from chrys.app.tui.widgets import EnhancedTextArea

    _DraftTextAreaBase = EnhancedTextArea
else:
    _DraftTextAreaBase = object

# A label's words follow the composer's language, inside the brackets this
# module adds; it keeps the text it was written with (``DraftReference.label``).
# The English text is ``english_label``'s.
_LABEL_IMAGE = msg("tui.draft.label.image", fallback="Image #{number}")
_LABEL_QUOTE = msg("tui.draft.label.quote", fallback="Quote #{number}")
_LABEL_PASTE = msg("tui.draft.label.paste", fallback="Pasted text #{number}")
_LABEL_PASTE_LINES = msg("tui.draft.label.paste_lines", fallback="Pasted text #{number} +{lines} lines")
_QUOTE_TRUNCATED_TITLE = msg("tui.draft.title.quote_truncated", fallback="Quote truncated")
_QUOTE_TRUNCATED = msg(
    "tui.draft.quote_truncated",
    fallback="Quote truncated: {original_tokens} tokens exceeds the {limit}-token limit.",
)

DRAFT_COMPONENT_CLASSES = frozenset({"draft--reference", "draft--prompt-field"})
"""The component classes a :class:`DraftTextArea` host declares."""


def draft_text_area_css(host: str) -> str:
    """The ``DEFAULT_CSS`` rules a :class:`DraftTextArea` host declares, scoped to *host*."""
    return f"""
    {host} > .draft--prompt-field {{
        background: $accent 30%;
    }}
    {host} > .draft--reference {{
        color: $accent;
        text-style: bold underline;
    }}
    """


def _nowhere() -> object:
    return None


@dataclass
class _DraftTheme(TextAreaTheme):
    """A theme whose cursor line paints only its background.

    Textual paints the cursor line over each line ``get_line`` returns, with
    the component's whole style (``TextAreaTheme.apply_css`` reads it
    non-partial, so it carries the text color): a label on the cursor's line
    would lose its color. The background still loses to the cursor line, so
    prompt fields turn the line highlight off instead
    (``_repaint_prompt_fields``).
    """

    @classmethod
    def of(cls, theme: TextAreaTheme) -> _DraftTheme:
        return cls(**{item.name: getattr(theme, item.name) for item in fields(theme) if item.init})

    def apply_css(self, text_area) -> None:
        super().apply_css(text_area)
        if self.cursor_line_style is not None:
            self.cursor_line_style = self.cursor_line_style.background_style


@dataclass
class _DraftLeft(Edit):
    """The edit that empties the composer as its draft leaves it (:meth:`DraftTextArea.leave_draft`).

    Undoing it brings *draft* back whole: its labels live, its fields, and
    its owner only while the composer is still in the *place* it left from.
    *table_after* is the table the composer holds once it is empty again: the empty
    one the draft left, or the next draft's, which an undo across this step
    keeps for the redo that comes back to it.
    """

    draft: ChatDraft = field(kw_only=True)
    place: object = field(kw_only=True)
    table_after: DraftReferences = field(default=EMPTY_REFERENCES, kw_only=True)


@dataclass
class _DraftReplaced(Edit):
    """An editor save, with the labels and fields on each side of its single replacement.

    The owner follows ordinary editing and is never replayed. The table kept
    on undo includes labels registered by later edits, for their subsequent redo.
    """

    fields_before: tuple[PromptField, ...] = field(kw_only=True)
    references_before: DraftReferences = field(kw_only=True)
    fields_after: tuple[PromptField, ...] = field(kw_only=True)
    table_after: DraftReferences = field(kw_only=True)


def _left_draft(edits: Sequence[Edit]) -> _DraftLeft | None:
    # ``leave_draft`` checkpoints before and after its edit, so it is a batch of its own.
    if len(edits) == 1 and isinstance(edits[0], _DraftLeft):
        return edits[0]
    return None


def _fields_after_edit(
    fields: Sequence[PromptField], start: int, replaced: str, text: str
) -> tuple[tuple[PromptField, ...], tuple[PromptField, ...]]:
    """The fields after *replaced* at *start* becomes *text*, and those it removed, relative to *start*.

    Both strings are in document storage form. Only an entirely identical
    replacement leaves the fields alone; writers exclude unchanged units.
    """
    if replaced == text:
        return tuple(fields), ()
    end = start + len(replaced)
    removed = tuple((low - start, high - start) for low, high in fields if start <= low < high <= end)
    return shift_prompt_fields(fields, start, end, len(text)), removed


class DraftTextArea(_DraftTextAreaBase):
    """The draft-editing rules of a ``TextArea`` host (module docstring)."""

    FOLD_PASTES: ClassVar[bool] = True

    def __init__(self, *args, **kwargs) -> None:
        # Before the host's init: it sets the selection and may load text, which read these.
        self._references: DraftReferences = EMPTY_REFERENCES
        self._prompt_fields: tuple[PromptField, ...] = ()
        self._draft_owner: ShortcutIntent | None = None
        # Set while a whole draft is written: its own clearing must not end the owner it brings.
        self._writing_draft = False
        self._prompt_field_columns_of: tuple[PromptField, ...] | None = None
        self._prompt_field_columns_cache: dict[int, list[tuple[int, int]]] = {}
        self._field_mouse_down: Location | None = None
        self._field_mouse_moved = False
        # Session restore/workspace updates replace this before normal use; this covers early paste during startup.
        self._paste_cwd: str = safe_getcwd()
        self._clipboard_image_dir: Path | None = None
        self.draft_place: Callable[[], object] = _nowhere
        """Where a prompt shortcut's owner holds (the host's session and folder): one undone in another place loses it."""
        super().__init__(*args, **kwargs)
        # A snapshot of the initial setting: prompt fields turn the line highlight
        # off while they show and restore this value, not one set later.
        self._highlights_cursor_line = self.highlight_cursor_line

    class ReferenceClicked(Message):
        """Posted when a live draft label is clicked: *reference* is what it stands for."""

        def __init__(self, reference: DraftReference, start: int) -> None:
            self.reference = reference
            self.start = start
            super().__init__()

    class DraftOwnerChanged(Message):
        """Posted when the prompt shortcut the draft belongs to changes (``ChatDraft.owner``)."""

        def __init__(self, owner: ShortcutIntent | None) -> None:
            self.owner = owner
            super().__init__()

    def _set_theme(self, theme: str) -> None:
        # Textual 8.2.7 ``TextArea._set_theme`` copies the named theme into ``_theme``.
        super()._set_theme(theme)
        if self._theme is not None:
            self._theme = _DraftTheme.of(self._theme)

    @property
    def prompt_fields(self) -> tuple[PromptField, ...]:
        """Unfilled placeholders left by the last prompt-shortcut expansion."""
        return self._prompt_fields

    def set_prompt_fields(self, fields: Sequence[PromptField], *, select: bool = True) -> None:
        """Highlight valid single-line *fields* and, with *select*, select the first one."""
        text = self.text
        self._prompt_fields = tuple(
            sorted(
                (start, end)
                for start, end in fields
                if 0 <= start < end <= len(text) and "\n" not in text[start:end] and "\r" not in text[start:end]
            )
        )
        self.selection = self.selection
        self._repaint_prompt_fields()
        if select and self._prompt_fields:
            self._select_prompt_field(self._prompt_fields[0])

    @property
    def draft_owner(self) -> ShortcutIntent | None:
        """The prompt shortcut this draft belongs to, if a shortcut expanded into it."""
        return self._draft_owner

    def set_draft_owner(self, owner: ShortcutIntent | None) -> None:
        """Make *owner* the draft's, keeping its text and fields; posts the change."""
        if owner == self._draft_owner:
            return
        self._draft_owner = owner
        self.post_message(self.DraftOwnerChanged(owner))

    def chat_draft(self) -> ChatDraft:
        """The draft now: text, unfilled fields, owner and label references."""
        return ChatDraft(self.text, self._prompt_fields, self._draft_owner, self._references)

    def write_draft(self, draft: ChatDraft, *, select_field: bool = False) -> None:
        """Replace the whole draft with *draft*, its fields and owner included; the cursor ends at its end.

        With *select_field* the first field is selected instead.
        """
        self._writing_draft = True
        try:
            self._drop_prompt_fields()
            self.clear()
            if draft.text:
                self.insert(draft.text)
        finally:
            self._writing_draft = False
        self._set_references(draft.references if draft.text else EMPTY_REFERENCES)
        self.set_prompt_fields(draft.fields, select=select_field)
        self.set_draft_owner(draft.owner if draft.text else None)

    def _end_owner_if_emptied(self) -> None:
        # An edit that empties the draft ends what it belonged to; a whole write states its own.
        if not self._writing_draft and not self.text:
            self._drop_prompt_fields()
            self.set_draft_owner(None)

    def replace_draft(self, draft: ChatDraft) -> None:
        """Apply an editor result as one undo step, leaving ownership to ordinary editing.

        Other whole-draft writers keep their existing semantics. This step
        owns both reference tables, so switching them cannot discard history.
        """
        before = self.chat_draft()
        self._drop_prompt_fields()
        self._references = draft.references if draft.text else EMPTY_REFERENCES
        self.history.checkpoint()
        replacement = _DraftReplaced(
            draft.text,
            (0, 0),
            self.document.end,
            maintain_selection_offset=False,
            fields_before=before.fields,
            references_before=before.references,
            fields_after=draft.fields,
            table_after=self._references,
        )
        self.edit(replacement)
        self._adopt_references(self._references)
        self.set_prompt_fields(draft.fields, select=False)
        self.set_draft_owner(draft.owner if self.text else None)
        self.history.checkpoint()

    def edit_paste(self, entry: DraftReference, content: str, *, start: int) -> None:
        """Save edited *content* for the paste *entry* as one undo step.

        The result is what pasting *content* now would give: still long, the
        same numbered label (its line count updated); short, the text itself;
        empty, nothing. Every place the label appears changes alike, and the
        cursor ends after the one at *start* (else the first). Unchanged
        content, or a draft that no longer shows the label, changes nothing.
        """
        draft = self.chat_draft()
        same = [label for label in iter_reference_labels(draft.text, draft.references) if label.reference == entry]
        if entry.kind != "paste" or not same:
            return
        if all(label.start != start for label in same):
            start = same[0].start
        content = normalize_line_endings(content)
        if content == entry.content:
            return
        references = draft.references
        replacement = content
        if content and paste_folds(content, screen_rows=self.app.size.height):
            replacement = self._label("paste", entry.number, content)
            references = references.with_replaced(DraftReference("paste", entry.number, content, replacement))
        text, fields = draft.text, draft.fields
        for label in reversed(same):
            text = text[: label.start] + replacement + text[label.end :]
            fields = shift_prompt_fields(fields, label.start, label.end, len(replacement))
        earlier = sum(1 for label in same if label.start < start)
        cursor = start + earlier * (len(replacement) - len(entry.label)) + len(replacement)
        self.replace_draft(ChatDraft(text, fields, draft.owner, references))
        self.move_cursor(Document(text).get_location_from_index(cursor))

    def clear(self) -> EditResult:
        """Empty the composer and end what its labels stand for.

        The next draft numbers from 1 (``_set_references`` drops the undo
        steps that would bring the labels back). A draft that leaves goes
        through :meth:`leave_draft` instead, which keeps it for undo. A whole
        write sets its own table once its text is in.
        """
        result = super().clear()
        if not self._writing_draft:
            self._set_references(EMPTY_REFERENCES)
        return result

    def leave_draft(self) -> None:
        """Empty the composer because its draft left it (sent, or taken in by a running turn), as one undo step.

        Undoing that step brings the draft back whole (:class:`_DraftLeft`),
        whether or not it holds labels, as plain text always came back. The
        next draft still numbers from 1: the step, not the composer, keeps the
        table. Nothing leaves an empty composer.
        """
        if not self.text:
            return
        draft = self.chat_draft()
        # Set directly: the step holds the table, so no undo step loses what its labels stand for.
        self._references = EMPTY_REFERENCES
        self.history.checkpoint()
        self.edit(
            _DraftLeft(
                "",
                (0, 0),
                self.document.end,
                maintain_selection_offset=False,
                draft=draft,
                # Read only for a shortcut's draft, the one it matters to (as ``take_draft_for`` does).
                place=self.draft_place() if draft.owner is not None else None,
            )
        )
        self.history.checkpoint()

    def take_back(self, draft: ChatDraft, *, replaces: bool = False) -> bool:
        """Bring *draft* back by undoing the last draft's leaving, if that is the last step; else False.

        It must be the draft that left, unchanged. With *replaces*, *draft*
        is what that draft comes back as (a refused send hands it back, its
        owner perhaps dropped): the step takes it in place of what left, as
        long as the text is the same. Where it cannot be undone (its text
        was rewritten, or other steps came after), the undo steps end, so
        none brings back what the hand-back took away. Either way the
        leaving did not happen after all, so no redo repeats it (Textual
        8.2.7 ``EditHistory._redo_stack``), as a written draft leaves none.
        """
        if self.text:
            return False
        stack = self.history.undo_stack
        left = _left_draft(stack[-1]) if stack else None
        if left is not None and (left.draft == draft or (replaces and left.draft.text == draft.text)):
            left.draft = draft
            left.place = self.draft_place() if draft.owner is not None else None
            self.undo()
            self.history._redo_stack.clear()
            return True
        if replaces and any(_left_draft(batch) is not None for batch in stack):
            self.history.clear()
        return False

    def _drop_prompt_fields(self) -> None:
        if self._prompt_fields:
            self._prompt_fields = ()
            self._repaint_prompt_fields()

    def _repaint_prompt_fields(self) -> None:
        # The cursor line is styled over each line ``get_line`` returns, which
        # would hide a field on the cursor's line: leave it unstyled while
        # fields are shown.
        self.highlight_cursor_line = self._highlights_cursor_line and not self._prompt_fields
        # The rendered-line cache is keyed on text, cursor and theme, not on
        # these spans: drop it so the next paint restyles the lines.
        self._line_cache.clear()
        self.refresh()

    def _select_prompt_field(self, span: PromptField) -> None:
        self.history.checkpoint()
        document = self.document
        start, end = span
        self.selection = Selection(document.get_location_from_index(start), document.get_location_from_index(end))

    def select_next_prompt_field(self) -> bool:
        """Select the field after the current selection, wrapping around; False with none left."""
        fields = self._prompt_fields
        if not fields:
            return False
        document = self.document
        first, last = sorted(self.selection)
        start = document.get_index_from_location(first)
        end = document.get_index_from_location(last)
        if (start, end) in fields:
            following = fields[(fields.index((start, end)) + 1) % len(fields)]
        else:
            following = next((span for span in fields if span[0] >= start), fields[0])
        self._select_prompt_field(following)
        return True

    def edit(self, edit: Edit) -> EditResult:
        self._keep_units_whole(edit)
        if not self._prompt_fields:
            result = super().edit(edit)
            self._end_owner_if_emptied()
            return result
        document = self.document
        # Move the fields before the base edit validates the selection, posts Changed and
        # repaints. Offsets count the document's own line breaks, so the new text is compared
        # as the document will store it: its lines joined by the document's newline.
        self._prompt_fields, removed = _fields_after_edit(
            self._prompt_fields,
            document.get_index_from_location(edit.top),
            document.get_text_range(edit.top, edit.bottom),
            document.newline.join(Document(edit.text).lines),
        )
        # Textual's recorded Edit owns the fields its replacement removes, relative to its start.
        edit.__dict__["_removed_prompt_fields"] = removed
        if not self._prompt_fields:
            self.highlight_cursor_line = self._highlights_cursor_line
        result = super().edit(edit)
        self._end_owner_if_emptied()
        return result

    # Undo and redo replay a batch of recorded edits straight on the document,
    # not through ``edit()`` (Textual 8.2.7 ``TextArea._undo_batch`` /
    # ``_redo_batch``). Each recorded edit knows the exact span it changed, so
    # the fields move across those spans; telling them apart by comparing the
    # text before and after would misplace a change next to a field whose
    # characters match it.

    # A draft's leaving (``_DraftLeft``) swaps the whole draft instead: an empty
    # composer holds no fields or owner, only the table of a next draft whose
    # text was undone, kept for the redo.

    def _undo_batch(self, edits: Sequence[Edit]) -> None:
        left = _left_draft(edits)
        replacement = edits[0] if len(edits) == 1 and isinstance(edits[0], _DraftReplaced) else None
        if replacement is not None:
            replacement.table_after = self._references
            self._drop_prompt_fields()
            self._references = EMPTY_REFERENCES
            # Textual refreshes scrollbars before restoring the recorded cursor.
            # Their watcher must see a location valid on either side of a whole replacement.
            self.move_cursor((0, 0))
        elif left is not None:
            left.table_after = self._references
        elif edits and (self._prompt_fields or any(getattr(edit, "_removed_prompt_fields", ()) for edit in edits)):
            steps: list[tuple[Location, Location, str, tuple[PromptField, ...]] | None] = []
            for recorded in reversed(edits):
                # The span the edit wrote, and the text it replaced there.
                done = recorded._edit_result
                removed = getattr(recorded, "_removed_prompt_fields", ())
                steps.append(None if done is None else (recorded.top, done.end_location, done.replaced_text, removed))
            self._replay_prompt_fields(steps)
        super()._undo_batch(edits)
        # Textual checkpoints after a redo only: without this, the next short edit would join
        # the batch this undo uncovers, and a sent draft would no longer be recognised
        # as its own step.
        self.history.checkpoint()
        if replacement is not None:
            self._adopt_references(replacement.references_before)
            self.set_prompt_fields(replacement.fields_before, select=False)
        elif left is not None:
            draft = left.draft
            self._adopt_references(draft.references)
            self.set_prompt_fields(draft.fields, select=False)
            owner = draft.owner
            self.set_draft_owner(owner if owner is not None and self.draft_place() == left.place else None)
        self._end_owner_if_emptied()

    def _redo_batch(self, edits: Sequence[Edit]) -> None:
        left = _left_draft(edits)
        replacement = edits[0] if len(edits) == 1 and isinstance(edits[0], _DraftReplaced) else None
        if replacement is not None:
            self._drop_prompt_fields()
            self._references = EMPTY_REFERENCES
            self.move_cursor((0, 0))
        elif left is None and self._prompt_fields and edits:
            self._replay_prompt_fields([(recorded.top, recorded.bottom, recorded.text, ()) for recorded in edits])
        super()._redo_batch(edits)
        if replacement is not None:
            self._adopt_references(replacement.table_after)
            self.set_prompt_fields(replacement.fields_after, select=False)
        elif left is not None:
            self._adopt_references(left.table_after)
        self._end_owner_if_emptied()

    def _replay_prompt_fields(
        self, steps: Sequence[tuple[Location, Location, str, tuple[PromptField, ...]] | None]
    ) -> None:
        """Move and restore fields across *steps*, replacements applied in order to the current text.

        Each step is ``(top, bottom, text, removed_fields)`` against the text the
        step before it left; removed fields are relative to its start. A scratch
        document turns locations into offsets, as ``edit()`` does on the live
        one. A step that cannot be placed drops every field: a field is only
        highlighted where it is known to be.
        """
        # Joining every line does not change the live document's newline.
        # Seed that storage form before loading text that may now be one line.
        scratch = Document(self.document.newline)
        scratch.replace_range((0, 0), scratch.end, self.text)
        fields = self._prompt_fields
        for step in steps:
            if step is None:
                fields = ()
                break
            top, bottom, text, removed = step
            start = scratch.get_index_from_location(top)
            written = scratch.replace_range(top, bottom, text)
            fields, _removed = _fields_after_edit(
                fields, start, written.replaced_text, scratch.get_text_range(top, written.end_location)
            )
            if removed:
                fields = tuple(sorted((*fields, *((start + low, start + high) for low, high in removed))))
        self._prompt_fields = fields
        self._repaint_prompt_fields()

    def load_text(self, text: str) -> None:
        # A plain whole write: no fields, no owner, no references.
        self._drop_prompt_fields()
        self._set_references(EMPTY_REFERENCES)
        super().load_text(text)
        self.set_draft_owner(None)

    def get_line(self, line_index: int) -> Text:
        line = super().get_line(line_index)
        if self._references and "[" in line.plain:
            # A label holds no newline, so each line is matched on its own.
            labels = iter_reference_labels(line.plain, self._references)
            if labels:
                style = self.get_component_rich_style("draft--reference", partial=False)
                for label in labels:
                    line.stylize(style, label.start, label.end)
        if not self._prompt_fields:
            return line
        spans = self._prompt_field_columns().get(line_index)
        if spans:
            style = self.get_component_rich_style("draft--prompt-field", partial=False)
            for start, end in spans:
                line.stylize(style, start, end)
        return line

    def _prompt_field_columns(self) -> dict[int, list[tuple[int, int]]]:
        """The columns each line's fields cover, worked out once per change of the fields.

        Every change of the text makes new fields (``edit()``, undo, redo,
        ``load_text``), so the fields themselves key this. Paint asks line by
        line, and a line's offset in the text costs a walk over every line
        above it: done per painted line, a long draft paints slowly.
        """
        fields = self._prompt_fields
        if self._prompt_field_columns_of is not fields:
            document = self.document
            lines = document.lines
            newline = len(document.newline)
            line_starts = list(accumulate((len(text) + newline for text in lines), initial=0))
            columns: dict[int, list[tuple[int, int]]] = {}
            for start, end in fields:
                row = bisect_right(line_starts, start) - 1
                line_start = line_starts[row]
                columns.setdefault(row, []).append((start - line_start, end - line_start))
            self._prompt_field_columns_cache = columns
            self._prompt_field_columns_of = fields
        return self._prompt_field_columns_cache

    @property
    def references(self) -> DraftReferences:
        """What the draft's labels stand for."""
        return self._references

    def _set_references(self, references: DraftReferences) -> None:
        if references == self._references:
            return
        previous = self._references
        if references.entries[: len(previous.entries)] != previous.entries:
            # A table that does not just grow ends the old one: its undo steps
            # would bring back labels with nothing, or something else, behind them.
            self.history.clear()
        self._adopt_references(references)

    def _adopt_references(self, references: DraftReferences) -> None:
        """Make *references* the live table, leaving the undo steps alone (an undo or redo swaps tables this way)."""
        self._references = references
        # Newly live labels may hold the cursor: put it back on an edge.
        self.selection = self.selection
        # The rendered-line cache is keyed on text, cursor and theme, not on
        # which labels are live: drop it so the next paint restyles them.
        self._line_cache.clear()
        self.refresh()

    def _restore_references(self, before: DraftReferences) -> None:
        """Take back entries registered for a label that never reached the text; no undo step names them, so all stay."""
        self._references = before
        self._line_cache.clear()

    def _register(self, kind: ReferenceKind, contents: Sequence[str]) -> str:
        """Register *contents* as the draft's next references of *kind*; the labels naming them, space-separated."""
        references = self._references
        labels: list[str] = []
        for content in contents:
            # The composer and its labels share LF storage, so expansion only shifts offsets by length.
            content = normalize_line_endings(content)
            references, entry = references.with_new(kind, content, label=self._label)
            labels.append(entry.label)
        self._set_references(references)
        return " ".join(labels)

    def _label(self, kind: ReferenceKind, number: int, content: str) -> str:
        """The label for a new entry, in the composer's language."""
        if kind == "image":
            message = _LABEL_IMAGE.bind(number=str(number))
        elif kind == "quote":
            message = _LABEL_QUOTE.bind(number=str(number))
        elif lines := content.count("\n"):
            message = _LABEL_PASTE_LINES.bind(number=str(number), lines=str(lines))
        else:
            message = _LABEL_PASTE.bind(number=str(number))
        return f"[{render_str(widget_localizer(self), message)}]"

    def _spaced_for_insert(self, text: str, *, trailing_space: bool = True) -> str:
        """Set *text* apart at the selection, optionally leaving a space at the draft's end for more input."""
        document = self.document
        first, last = sorted(self.selection)
        start = document.get_index_from_location(first)
        end = document.get_index_from_location(last)
        draft = self.text
        before = " " if start > 0 and not draft[start - 1].isspace() else ""
        after = " " if (end >= len(draft) and trailing_space) or (end < len(draft) and not draft[end].isspace()) else ""
        return f"{before}{text}{after}"

    def insert_reference(self, kind: ReferenceKind, content: str) -> bool:
        """Insert a new label for *content* at the selection, set apart by spaces; False when the draft cannot take it."""
        if self.read_only:
            return False
        if kind == "quote":
            # A quote holds what a paste of the same text would: the same cap.
            content = self._cap_to_paste_tokens(content, notice=_QUOTE_TRUNCATED, title=_QUOTE_TRUNCATED_TITLE)
        before = self._references
        label = self._register(kind, [content])
        result = self._replace_via_keyboard(self._spaced_for_insert(label), *self.selection)
        if not result:
            self._restore_references(before)
            return False
        self.move_cursor(result.end_location)
        return True

    def _sanitize_paste_text(self, text: str) -> str:
        """Turn a paste of images into image labels, else fold a long paste into a ``[Pasted text #n]`` label.

        Images never fold, however many: a folded paste holds plain text only
        (one level, as Claude Code does), so label text in it is just text.
        A folded label stands exactly where the text would have gone, with
        no spaces added: sending it puts the text back as if it had been
        pasted. Text folds after the base normalizes and caps it.
        """
        images = self._image_paste_text(text)
        if images is not None:
            return images
        text = super()._sanitize_paste_text(text)
        if self.read_only or not self.FOLD_PASTES or not paste_folds(text, screen_rows=self.app.size.height):
            return text
        return self._register("paste", [text])

    def _validate_selection(self, selection: Selection) -> Selection:
        """Keep each selection end off the inside of a live label or prompt field.

        Every cursor move passes here (keys, clicks, drags, programmatic moves);
        an end landing inside a unit goes to its edge in the direction it moved.
        Units hold no newline, so only that end's line is read.
        """
        selection = super()._validate_selection(selection)
        if not self._references and not self._prompt_fields:
            return selection
        previous = self.selection
        return Selection(
            self._unit_edge(selection.start, forward=selection.start >= previous.start),
            self._unit_edge(selection.end, forward=selection.end >= previous.end),
        )

    def _unit_spans(self, row: int | None = None) -> list[tuple[int, int]]:
        """The live labels and fields, ordered as absolute offsets or columns on *row*."""
        if not self._references and not self._prompt_fields:
            return []
        text = self.text if row is None else self.document[row]
        fields = self._prompt_fields if row is None else self._prompt_field_columns().get(row, ())
        return sorted(
            [(label.start, label.end) for label in iter_reference_labels(text, self._references)] + list(fields)
        )

    def _unit_edge(self, location: Location, *, forward: bool) -> Location:
        row, column = location
        return row, unit_boundary(self._unit_spans(row), column, forward=forward)

    def _keep_units_whole(self, edit: Edit) -> None:
        """Make *edit* take any unit it reaches into whole, or insert beside one it falls in."""
        if not self._references and not self._prompt_fields:
            return
        document = self.document
        start = document.get_index_from_location(edit.top)
        end = document.get_index_from_location(edit.bottom)
        safe_start, safe_end = unit_safe_range(self._unit_spans(), start, end)
        if (safe_start, safe_end) != (start, end):
            edit.from_location = document.get_location_from_index(safe_start)
            edit.to_location = document.get_location_from_index(safe_end)

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        # Textual dispatches each MRO class's handler once; the base owns the selection.
        self._field_mouse_down = self.get_target_document_location(event)
        self._field_mouse_moved = False

    async def _on_mouse_move(self, event: events.MouseMove) -> None:
        # Only between a press and its click, and only until the mouse first leaves the pressed cell.
        if (
            self._field_mouse_down is not None
            and not self._field_mouse_moved
            and self.get_target_document_location(event) != self._field_mouse_down
        ):
            self._field_mouse_moved = True

    def _accepts_text(self) -> bool:
        """Whether typing writes into the draft now (the editor's Vim Normal and Visual modes do not)."""
        return True

    async def _on_click(self, event) -> None:
        """Select a clicked field while typing, rest on its start otherwise, or open a live label."""
        pressed = self._field_mouse_down if not self._field_mouse_moved else None
        self._field_mouse_down = None
        row, column = self.get_target_document_location(event)
        if row >= self.document.line_count or column >= len(self.document[row]):
            return
        index = self.document.get_index_from_location((row, column))
        if pressed == (row, column):
            for start, end in self._prompt_fields:
                if start <= index < end:
                    event.stop()
                    if self._accepts_text():
                        self._select_prompt_field((start, end))
                    else:
                        self.move_cursor(self.document.get_location_from_index(start))
                    return
        if not self._references or not self.selection.is_empty:
            return
        for label in iter_reference_labels(self.text, self._references):
            if label.start <= index < label.end:
                event.stop()
                self.post_message(self.ReferenceClicked(label.reference, label.start))
                return

    def _image_paste_text(self, text: str) -> str | None:
        """Text for pasted/dropped image paths, or a terminal's empty paste of a clipboard image; else ``None``.

        Every paste path reaches here once its host has accepted the paste (the
        editor's Vim mode accepts only in Insert), so images follow the same
        gate as text.
        """
        try:
            if not text:
                return self._prepare_clipboard_image_paste_text()
            return self._prepare_image_path_paste_text(text)
        except OSError, RuntimeError, ValueError:
            return None

    def _prepare_image_path_paste_text(self, text: str) -> str | None:
        """Prepare pasted image paths in the host's insertion form; ``None`` otherwise."""
        if self.read_only:
            return None
        paths = pasted_image_paths(text, cwd=Path(self._paste_cwd))
        if not paths:
            return None
        return self._images_for_insert([str(path) for path in paths])

    def _images_for_insert(self, paths: Sequence[str]) -> str:
        """How every accepted image paste appears in this host's draft."""
        return self._spaced_for_insert(self._register("image", paths))

    def set_paste_cwd(self, cwd: str) -> None:
        """Set the cwd used to resolve relative pasted/dropped image paths."""
        self._paste_cwd = cwd

    def set_clipboard_image_dir(self, directory: str | Path | None) -> None:
        """Set the session-scoped directory used for clipboard image attachments."""
        self._clipboard_image_dir = Path(directory) if directory is not None else None

    def _prepare_clipboard_image_paste_text(self) -> str | None:
        """Return paste text for a bitmap image currently on the platform clipboard."""
        # TODO(perf): Clipboard capture may run osascript/PIL/file I/O. Move
        # this boundary to a worker when paste completion can remain ordered
        # with subsequent input events.
        if self.read_only:
            return None
        path = save_clipboard_image_to_file(self._clipboard_image_dir)
        if path is None:
            return None
        return self._images_for_insert([str(path)])

    def _insert_paste_text(self, text: str) -> bool:
        text = self._sanitize_paste_text(text)
        if result := self._replace_via_keyboard(text, *self.selection):
            self.move_cursor(result.end_location)
            return True
        return False

    def _insert_clipboard_image_paste(self) -> bool:
        text = self._prepare_clipboard_image_paste_text()
        if not text:
            return False
        return self._insert_paste_text(text)

    def action_paste(self) -> None:
        """Paste a platform clipboard image in the host's insertion form, else the clipboard's text."""
        if self.read_only:
            return
        if not self._insert_clipboard_image_paste():
            super().action_paste()


__all__ = ["DRAFT_COMPONENT_CLASSES", "DraftTextArea", "draft_text_area_css"]
