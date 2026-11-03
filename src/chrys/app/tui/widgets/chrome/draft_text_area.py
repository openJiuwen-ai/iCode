# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Editing a chat draft: the rules the input box and the message editor share.

A draft is text plus what lives in it (:class:`ChatDraft`): the labels'
references, the unfilled prompt fields and the prompt shortcut it belongs to.
:class:`DraftTextArea` is the one implementation of how that draft edits, so
the message editor is the input box made bigger:

- labels are one unit (:func:`label_boundary` for every cursor move,
  :func:`label_safe_range` for every edit), styled, and open on a click;
- prompt fields shift with edits, undo and redo, and any edit that touches
  one makes it the user's text;
- an edit that empties the draft ends its fields and owner;
- a draft that leaves (sent, or taken in by a running turn) is one undo
  step that brings it back whole, labels live (:meth:`leave_draft`);
  otherwise a reference table that is replaced rather than grown drops the
  undo steps that would bring back labels with nothing behind them;
- a long paste folds into a label, and image paths or a clipboard image
  become image labels.

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
from typing import TYPE_CHECKING

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
    label_boundary,
    label_safe_range,
    paste_folds,
)

if TYPE_CHECKING:
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


def _left_draft(edits: Sequence[Edit]) -> _DraftLeft | None:
    # ``leave_draft`` checkpoints before and after its edit, so it is a batch of its own.
    if len(edits) == 1 and isinstance(edits[0], _DraftLeft):
        return edits[0]
    return None


class DraftTextArea(_DraftTextAreaBase):
    """The draft-editing rules of a ``TextArea`` host (module docstring)."""

    def __init__(self, *args, **kwargs) -> None:
        # Before the host's init: it sets the selection and may load text, which read these.
        self._references: DraftReferences = EMPTY_REFERENCES
        self._prompt_fields: tuple[PromptField, ...] = ()
        self._draft_owner: ShortcutIntent | None = None
        # Set while a whole draft is written: its own clearing must not end the owner it brings.
        self._writing_draft = False
        self._prompt_field_columns_of: tuple[PromptField, ...] | None = None
        self._prompt_field_columns_cache: dict[int, list[tuple[int, int]]] = {}
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

        def __init__(self, reference: DraftReference) -> None:
            self.reference = reference
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
        """Highlight *fields* of the current text and, with *select*, select the first one."""
        length = len(self.text)
        self._prompt_fields = tuple(sorted((start, end) for start, end in fields if 0 <= start < end <= length))
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
        self._keep_labels_whole(edit)
        if not self._prompt_fields:
            result = super().edit(edit)
            self._end_owner_if_emptied()
            return result
        document = self.document
        start = document.get_index_from_location(edit.top)
        end = document.get_index_from_location(edit.bottom)
        # Shift before the base edit posts Changed and repaints. ``len(edit.text)`` is the length
        # the document stores: text reaches the draft with LF line ends only (typing inserts
        # ``\n``, a paste or drop goes through ``_sanitize_paste_text``, an expansion through
        # ``sanitize_terminal_block``), so no CRLF collapses on the way in.
        self._prompt_fields = shift_prompt_fields(self._prompt_fields, start, end, len(edit.text))
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
        if left is not None:
            left.table_after = self._references
        elif self._prompt_fields and edits:
            steps: list[tuple[Location, Location, str] | None] = []
            for recorded in reversed(edits):
                # The span the edit wrote, and the text it replaced there.
                done = recorded._edit_result
                steps.append(None if done is None else (recorded.top, done.end_location, done.replaced_text))
            self._replay_prompt_fields(steps)
        super()._undo_batch(edits)
        if left is not None:
            draft = left.draft
            self._adopt_references(draft.references)
            self.set_prompt_fields(draft.fields, select=False)
            owner = draft.owner
            self.set_draft_owner(owner if owner is not None and self.draft_place() == left.place else None)
        self._end_owner_if_emptied()

    def _redo_batch(self, edits: Sequence[Edit]) -> None:
        left = _left_draft(edits)
        if left is None and self._prompt_fields and edits:
            self._replay_prompt_fields([(recorded.top, recorded.bottom, recorded.text) for recorded in edits])
        super()._redo_batch(edits)
        if left is not None:
            self._adopt_references(left.table_after)
        self._end_owner_if_emptied()

    def _replay_prompt_fields(self, steps: Sequence[tuple[Location, Location, str] | None]) -> None:
        """Move the fields across *steps*, replacements applied in order to the current text.

        Each step is ``(top, bottom, text)`` against the text the step before
        it left; a scratch copy of the document turns each into offsets, as
        ``edit()`` does on the live one. A step that cannot be placed drops
        every field: a field is only highlighted where it is known to be.
        """
        scratch = Document(self.text)
        fields = self._prompt_fields
        for step in steps:
            if step is None:
                fields = ()
                break
            top, bottom, text = step
            start = scratch.get_index_from_location(top)
            end = scratch.get_index_from_location(bottom)
            written = scratch.replace_range(top, bottom, text)
            fields = shift_prompt_fields(
                fields, start, end, scratch.get_index_from_location(written.end_location) - start
            )
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
                first = bisect_right(line_starts, start) - 1
                last = min(bisect_right(line_starts, end) - 1, len(lines) - 1)
                for row in range(first, last + 1):
                    line_start = line_starts[row]
                    left = max(start, line_start) - line_start
                    right = min(end, line_start + len(lines[row])) - line_start
                    if left < right:
                        columns.setdefault(row, []).append((left, right))
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

    def _spaced_for_insert(self, text: str) -> str:
        """*text* with a space on each side where it would otherwise touch other text at the selection."""
        document = self.document
        first, last = sorted(self.selection)
        start = document.get_index_from_location(first)
        end = document.get_index_from_location(last)
        draft = self.text
        before = " " if start > 0 and not draft[start - 1].isspace() else ""
        after = " " if end >= len(draft) or not draft[end].isspace() else ""
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
        labels = self._image_paste_labels(text)
        if labels is not None:
            return labels
        text = super()._sanitize_paste_text(text)
        if self.read_only or not paste_folds(text, screen_rows=self.app.size.height):
            return text
        return self._register("paste", [text])

    def _validate_selection(self, selection: Selection) -> Selection:
        """Keep each selection end off the inside of a live label: a label is one unit.

        Every cursor move passes here (keys, clicks, drags, programmatic moves);
        an end landing inside a label goes to the label's edge in the direction
        it moved. A label holds no newline, so only that end's line is read.
        """
        selection = super()._validate_selection(selection)
        if not self._references:
            return selection
        previous = self.selection
        return Selection(
            self._label_edge(selection.start, forward=selection.start >= previous.start),
            self._label_edge(selection.end, forward=selection.end >= previous.end),
        )

    def _label_edge(self, location: Location, *, forward: bool) -> Location:
        row, column = location
        return row, label_boundary(self.document[row], self._references, column, forward=forward)

    def _keep_labels_whole(self, edit: Edit) -> None:
        """Make *edit* take any live label it reaches into whole, or insert beside one it falls in."""
        if not self._references:
            return
        document = self.document
        start = document.get_index_from_location(edit.top)
        end = document.get_index_from_location(edit.bottom)
        safe_start, safe_end = label_safe_range(self.text, self._references, start, end)
        if (safe_start, safe_end) != (start, end):
            edit.from_location = document.get_location_from_index(safe_start)
            edit.to_location = document.get_location_from_index(safe_end)

    async def _on_click(self, event) -> None:
        """Open what a clicked live label stands for (a click, not the end of a drag)."""
        if not self._references or not self.selection.is_empty:
            return
        row, column = self.get_target_document_location(event)
        if row >= self.document.line_count or column >= len(self.document[row]):
            return
        index = self.document.get_index_from_location((row, column))
        for label in iter_reference_labels(self.text, self._references):
            if label.start <= index < label.end:
                event.stop()
                self.post_message(self.ReferenceClicked(label.reference))
                return

    def _image_paste_labels(self, text: str) -> str | None:
        """Image labels for pasted/dropped image paths, or a terminal's empty paste of a clipboard image; else ``None``.

        Every paste path reaches here once its host has accepted the paste (the
        editor's Vim mode accepts only in Insert), so labels follow the same
        gate as text.
        """
        try:
            if not text:
                return self._prepare_clipboard_image_paste_text()
            return self._prepare_image_path_paste_text(text)
        except OSError, RuntimeError, ValueError:
            return None

    def _prepare_image_path_paste_text(self, text: str) -> str | None:
        """Register a paste of image paths as the draft's images and return their labels; ``None`` otherwise."""
        if self.read_only:
            return None
        paths = pasted_image_paths(text, cwd=Path(self._paste_cwd))
        if not paths:
            return None
        return self._spaced_for_insert(self._register("image", [str(path) for path in paths]))

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
        return self._spaced_for_insert(self._register("image", [str(path)]))

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
        """Paste a platform clipboard image as an image label, else the clipboard's text."""
        if self.read_only:
            return
        if not self._insert_clipboard_image_paste():
            super().action_paste()


__all__ = ["DRAFT_COMPONENT_CLASSES", "DraftTextArea", "draft_text_area_css"]
