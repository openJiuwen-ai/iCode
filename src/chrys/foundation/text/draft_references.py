# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``[Image #n]`` / ``[Quote #n]`` / ``[Pasted text #n +L lines]`` labels in a chat draft.

Every label kind follows the same rules; only what a label sends differs.

A label is plain draft text. Whether it means anything is decided only by
the draft's :class:`DraftReferences`: a label is live when the draft
registered an entry of its kind and number and the text reads exactly that
entry's label; anything else is just the characters. Nothing tracks where a
label sits, so undo, cut and paste and an editor round trip need no rules of
their own.

In the composer a live label is one unit. Two rules keep it whole, applied
where every cursor move and every edit already pass: a cursor or selection
end never rests inside a label (:func:`label_boundary`), and an edit that
reaches into a label takes all of it (:func:`label_safe_range`). Typing,
pasting, deleting and inserting a new label follow from those two.

A label's text is written once, when its entry is registered, in the
composer's language (:data:`LabelMaker`), and kept with the entry: a label
stays live when the language changes later.

Text leaves the composer through one function, :func:`expand_references`,
and each label becomes what it stands for in place: an image label its
``@file`` mention (the engine's attachment path), a pasted-text label the text
it folded, a quote label its text in quotation marks. One level only, as
Claude Code does: label text inside a paste or a quote is just text. What is
sent is what the model, the transcript and the prompt history see; labels
exist only in the composer, and sent text never turns back into them.

A quote is what someone else said, so nothing in it acts: an ``@file`` in it
attaches nothing and a ``!`command``` in it runs nothing. Each is written with
a backslash before it (:func:`inert_quote`), which every scanner of sent text
reads as plain text, wherever that text goes later (history, a fork, a retry).
Pasted text is the user's own words and acts like typed text.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from chrys.foundation.text.mentions import format_file_mention

type ReferenceKind = Literal["image", "quote", "paste"]

type LabelMaker = Callable[[ReferenceKind, int, str], str]
"""Writes the label for an entry of a kind, number and content (the composer passes one in its language)."""

_WORD_BY_KIND: dict[ReferenceKind, str] = {"image": "Image", "quote": "Quote", "paste": "Pasted text"}


def english_label(kind: ReferenceKind, number: int, content: str) -> str:
    """The English label: ``[Image #1]``, ``[Quote #1]``, ``[Pasted text #1 +3 lines]`` (newlines, as Claude Code counts)."""
    word = _WORD_BY_KIND[kind]
    lines = content.count("\n") if kind == "paste" else 0
    return f"[{word} #{number} +{lines} lines]" if lines else f"[{word} #{number}]"


PASTE_FOLD_MIN_CHARS = 800
"""A paste longer than this many characters folds into a label (as Claude Code does)."""


def paste_folds(text: str, *, screen_rows: int) -> bool:
    """Whether pasted *text* folds into a ``[Pasted text #n]`` label rather than showing inline.

    It folds when it is longer than :data:`PASTE_FOLD_MIN_CHARS` or holds more
    newlines than ``max(0, min(screen_rows - 10, 2))``: three or more on any
    usual terminal, fewer on a very short one.
    """
    return len(text) > PASTE_FOLD_MIN_CHARS or text.count("\n") > max(0, min(screen_rows - 10, 2))


@dataclass(frozen=True, slots=True)
class DraftReference:
    """One thing a draft label stands for: the same fields for every kind.

    *content* is the image's path, the quoted text, or the pasted text;
    *label* is the draft text that names it (English when none is given).
    """

    kind: ReferenceKind
    number: int
    content: str
    label: str = ""

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", english_label(self.kind, self.number, self.content))


@dataclass(frozen=True, slots=True)
class DraftReferences:
    """What a draft's labels stand for, numbered from 1 per kind.

    Only ever grows while the draft lives, so undo and a paste back find what
    a label stood for; the next message starts over, numbering from 1.
    """

    entries: tuple[DraftReference, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.entries)

    def get(self, kind: ReferenceKind, number: int) -> DraftReference | None:
        """The entry label *number* of *kind* stands for, if registered."""
        return next((entry for entry in self.entries if entry.kind == kind and entry.number == number), None)

    def with_new(
        self, kind: ReferenceKind, content: str, *, label: LabelMaker = english_label
    ) -> tuple[DraftReferences, DraftReference]:
        """These references plus a new entry of *kind*, numbered after the last one of its kind, labelled by *label*."""
        number = max((entry.number for entry in self.entries if entry.kind == kind), default=0) + 1
        entry = DraftReference(kind, number, content, label(kind, number, content))
        return DraftReferences((*self.entries, entry)), entry


EMPTY_REFERENCES = DraftReferences()


@dataclass(frozen=True, slots=True)
class ReferenceLabel:
    """A live label in a draft's text, ``[start, end)`` code points, and what it stands for."""

    reference: DraftReference
    start: int
    end: int


def iter_reference_labels(text: str, references: DraftReferences) -> list[ReferenceLabel]:
    """The live labels of *text*, in order: each place it reads exactly an entry's label."""
    if not references:
        return []
    found: list[ReferenceLabel] = []
    for entry in references.entries:
        start = text.find(entry.label)
        while start != -1:
            found.append(ReferenceLabel(entry, start, start + len(entry.label)))
            start = text.find(entry.label, start + len(entry.label))
    found.sort(key=lambda label: (label.start, -label.end))
    labels: list[ReferenceLabel] = []
    for label in found:
        if not labels or label.start >= labels[-1].end:
            labels.append(label)
    return labels


def label_boundary(text: str, references: DraftReferences, index: int, *, forward: bool) -> int:
    """*index*, or the edge of the live label it falls strictly inside: its end moving *forward*, else its start."""
    for label in iter_reference_labels(text, references):
        if label.start < index < label.end:
            return label.end if forward else label.start
    return index


def label_safe_range(text: str, references: DraftReferences, start: int, end: int) -> tuple[int, int]:
    """The ``[start, end)`` an edit of *text* applies to so that it cuts no live label.

    A range reaching into a label grows to take the whole label; an insertion
    point inside one moves to its end.
    """
    for label in iter_reference_labels(text, references):
        if start == end:
            if label.start < start < label.end:
                return label.end, label.end
        elif label.start < end and start < label.end:
            start, end = min(start, label.start), max(end, label.end)
    return start, end


# Stands in for a space inside a label while text is split into words; a
# private-use character, so typed text never holds one.
_LABEL_SPACE = "\ue000"


def labels_as_words(text: str, references: DraftReferences) -> str:
    """*text* with each live label made one word: its spaces become a stand-in of the same length.

    For splitting draft text on whitespace (a prompt shortcut's ``$1`` …): a
    live label is one unit there as it is under the cursor, and label-shaped
    text nobody registered splits like any text. Offsets are kept, and
    :func:`labels_from_words` turns the stand-ins back.
    """
    pieces: list[str] = []
    cursor = 0
    for label in iter_reference_labels(text, references):
        pieces.append(text[cursor : label.start])
        pieces.append(text[label.start : label.end].replace(" ", _LABEL_SPACE))
        cursor = label.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def labels_from_words(text: str) -> str:
    """Undo :func:`labels_as_words` for every label-word *text* holds."""
    return text.replace(_LABEL_SPACE, " ")


def expand_references(text: str, references: DraftReferences) -> str:
    """*text* as it leaves the composer: every live label replaced, in place, by what it sends.

    An image label becomes its ``@file`` mention, a pasted-text label the text
    it folded, a quote label its text: one line between quotation marks,
    several as a Markdown block quote on lines of their own
    (:func:`quote_form`). Text without live labels comes back unchanged.
    """
    return expand_with_quote_spans(text, references)[0]


def expand_with_quote_spans(text: str, references: DraftReferences) -> tuple[str, list[tuple[int, int]]]:
    """:func:`expand_references`, and the ``[start, end)`` of each quote in the result, quotation marks included."""
    pieces: list[str] = []
    quotes: list[tuple[int, int]] = []
    length = 0
    last = 0
    for label in iter_reference_labels(text, references):
        entry = label.reference
        pieces.append(text[last : label.start])
        length += label.start - last
        if entry.kind == "quote":
            replacement = quote_form(text, label)
            quotes.append((length, length + len(replacement)))
        elif entry.kind == "image":
            replacement = _set_apart(text, label, format_file_mention(entry.content))
        else:
            replacement = entry.content
        pieces.append(replacement)
        length += len(replacement)
        last = label.end
    pieces.append(text[last:])
    return "".join(pieces), quotes


def image_labels_to_paths(text: str, references: DraftReferences) -> str:
    """*text* with its image labels replaced by their bare paths, every other label kept, for a model without vision."""
    labels = [label for label in iter_reference_labels(text, references) if label.reference.kind == "image"]
    return _replace_labels(text, labels, lambda entry: entry.content)


# An ``@`` opens a file mention at the start or after whitespace; ``!`…``` is a
# prompt-shortcut command anywhere. A backslash before either keeps it text.
_ACTING_MENTION_RE = re.compile(r"(?<!\S)@")
_ACTING_COMMAND = "!`"


def inert_quote(text: str) -> str:
    """*text* as a quote sends it: every ``@file`` mention and ``!`command``` in it escaped, so none acts."""
    return _ACTING_MENTION_RE.sub(r"\\@", text).replace(_ACTING_COMMAND, "\\" + _ACTING_COMMAND)


def quote_form(text: str, label: ReferenceLabel) -> str:
    """What the quote *label* in *text* sends: its text, made inert (:func:`inert_quote`).

    One line goes in place between quotation marks. Several go as a Markdown
    block quote, set on lines of their own and ended by a blank line, so the
    Markdown someone else wrote (a heading, a list, a code fence) stays
    inside the quote instead of reshaping the message around it.
    """
    body = inert_quote(label.reference.content).strip("\n")
    if "\n" not in body:
        return f"“{body}”"
    block = "\n".join(f"> {line}" if line else ">" for line in body.split("\n"))
    before = text[: label.start]
    after = text[label.end :]
    if before and not before.endswith("\n"):
        block = f"\n{block}"
    if after and not after.startswith("\n\n"):
        # A line right after a block quote would continue it.
        block = f"{block}\n" if after.startswith("\n") else f"{block}\n\n"
    return block


def _set_apart(text: str, label: ReferenceLabel, replacement: str) -> str:
    """*replacement* for an image *label*, spaced so a mention opens after whitespace and runs to whitespace."""
    if label.start > 0 and not text[label.start - 1].isspace():
        replacement = f" {replacement}"
    if label.end < len(text) and not text[label.end].isspace():
        replacement = f"{replacement} "
    return replacement


def _replace_labels(text: str, labels: list[ReferenceLabel], form: Callable[[DraftReference], str]) -> str:
    """*text* with each of *labels* replaced by ``form(entry)``."""
    pieces: list[str] = []
    last = 0
    for label in labels:
        entry = label.reference
        replacement = form(entry)
        if entry.kind == "image":
            replacement = _set_apart(text, label, replacement)
        pieces.append(text[last : label.start])
        pieces.append(replacement)
        last = label.end
    pieces.append(text[last:])
    return "".join(pieces)


__all__ = [
    "EMPTY_REFERENCES",
    "PASTE_FOLD_MIN_CHARS",
    "DraftReference",
    "DraftReferences",
    "LabelMaker",
    "ReferenceKind",
    "ReferenceLabel",
    "english_label",
    "expand_references",
    "expand_with_quote_spans",
    "image_labels_to_paths",
    "inert_quote",
    "iter_reference_labels",
    "label_boundary",
    "label_safe_range",
    "labels_as_words",
    "labels_from_words",
    "paste_folds",
    "quote_form",
]
