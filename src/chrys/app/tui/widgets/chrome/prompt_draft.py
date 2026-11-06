# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The chat draft as one value, and what it belongs to when a prompt shortcut expanded into it.

Everything that edits a draft (the input box, the message editor) holds and
hands back a whole :class:`ChatDraft`; the prompt-shortcut card reads the
owner from it: one draft, one owner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from chrys.app.tui.widgets.chrome.commands import parse_slash_draft
from chrys.foundation.text.draft_references import EMPTY_REFERENCES, DraftReferences, expand_references


@dataclass(frozen=True, slots=True)
class ShortcutIntent:
    """What an expanded prompt shortcut declares for its draft, kept whatever the session uses now.

    Keeping the declaration, showing it and switching for it are separate: the
    card shows only what differs from the session now, and a send compares
    again when it switches. A shortcut naming the agent already in use keeps
    that name, so a send that changes the agent meanwhile does not lose it.
    How many ``!`command``` placeholders the draft holds is read from its text.
    """

    name: str
    agent: str
    """The frontmatter ``agent``, as written; empty when it names none."""
    model: str
    """The frontmatter ``model``, as written; empty when it names none."""
    switch_dropped: bool = False
    """✕, or the user's own agent or model pick: the session keeps its agent and model."""
    path: str = ""
    """The shortcut file the draft was expanded from (``PromptShortcut.path``): the engine checks it still loads it."""


type PromptField = tuple[int, int]
"""``[start, end)`` code-point span of an unfilled prompt-shortcut placeholder in the draft."""


def shift_prompt_fields(fields: Sequence[PromptField], start: int, end: int, inserted: int) -> tuple[PromptField, ...]:
    """Return *fields* after ``[start, end)`` is replaced by *inserted* characters.

    A field the edit touches stops being a field: once the user types into,
    over or across a placeholder it is their text. Text inserted exactly at a
    field's edge leaves the field as it was, moving it when the text lands
    before it.
    """
    delta = inserted - (end - start)
    kept: list[PromptField] = []
    for field_start, field_end in fields:
        if end <= field_start:
            kept.append((field_start + delta, field_end + delta))
        elif start >= field_end:
            kept.append((field_start, field_end))
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class ChatDraft:
    """The chat draft as one value: its text, unfilled prompt fields, shortcut owner and label references.

    Whatever moves a draft as a whole (history, the editor, a send that
    gives it back) moves all of it together, so a draft comes back the way
    it left. An edit that empties the text ends the fields and owner; the
    references last until the draft itself is over, so undo and a paste
    back still find what a label stands for. A draft that leaves the
    composer is one undo step that brings all of it back
    (``DraftTextArea.leave_draft``).
    """

    text: str
    fields: tuple[PromptField, ...] = ()
    owner: ShortcutIntent | None = None
    references: DraftReferences = EMPTY_REFERENCES

    def sent_text(self) -> str:
        """The text this draft sends: stripped, its labels expanded (``expand_references``)."""
        return expand_references(self.text.strip(), self.references)

    def command_text(self) -> str:
        """The text a submit reads a ``/command`` from: the one rule for routing and the send button.

        A draft that starts with ``/`` is read as written, so a label among a
        shortcut's arguments stays one argument. One that starts with a label
        is read as it sends: a long paste folds by its size, so a command it
        holds is the command that goes out.
        """
        text = self.text.strip()
        if parse_slash_draft(text) is not None or not self.references or not text.startswith("["):
            return text
        return self.sent_text()


__all__ = ["ChatDraft", "PromptField", "ShortcutIntent", "shift_prompt_fields"]
