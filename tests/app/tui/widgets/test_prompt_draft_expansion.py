# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The prompt editor sees exactly the expanded send text and maps draft positions in both directions."""

from __future__ import annotations

from itertools import product

import pytest

from chrys.app.tui.widgets.chrome import prompt_draft
from chrys.app.tui.widgets.chrome.prompt_draft import ChatDraft, ShortcutIntent
from chrys.foundation.text.draft_references import EMPTY_REFERENCES, DraftReferences, expand_references


def _references() -> DraftReferences:
    references = EMPTY_REFERENCES
    for kind, content in (
        ("paste", "  pasted\ntext\n"),
        ("quote", "@a.py and !`pwd`"),
        ("quote", "first\n@b.py\n!`ls`"),
        ("image", "/shots/a b.png"),
    ):
        references, _entry = references.with_new(kind, content)
    return references


@pytest.mark.parametrize(
    "text",
    [
        "plain [Image #99] text",
        "[Pasted text #1 +2 lines]",
        "[Quote #1]",
        "[Quote #2]",
        "[Image #1]",
        "[Pasted text #1 +2 lines][Quote #1][Image #1][Quote #2]",
        "[Pasted text #1 +2 lines] tail",
        "head [Pasted text #1 +2 lines]",
        "a [Pasted text #1 +2 lines] middle [Pasted text #1 +2 lines] z",
    ],
)
def test_editor_expansion_is_the_send_expansion_for_each_label_arrangement(text: str) -> None:
    references = _references()
    owner = ShortcutIntent("fix", "Code", "model")
    expanded = prompt_draft.expand_draft(ChatDraft(text, owner=owner, references=references), 0)

    assert expanded.text == expand_references(text, references)
    assert expanded.draft == ChatDraft(expanded.text, owner=owner)
    assert not expanded.draft.references


@pytest.mark.parametrize(("before", "after"), list(product(("", "a", "a\n"), ("", "b", "\nb", "\n\nb"))))
def test_multiline_quotes_have_the_same_blank_lines_and_inert_mentions_as_a_send(before: str, after: str) -> None:
    text = f"{before}[Quote #2]{after}"
    expanded = prompt_draft.expand_draft(ChatDraft(text, references=_references()), 0)

    assert expanded.text == expand_references(text, _references())
    assert "\\@b.py" in expanded.text
    assert "\\!`ls`" in expanded.text


def test_fields_and_every_position_outside_labels_shift_by_the_expansion_length() -> None:
    references, entry = EMPTY_REFERENCES.with_new("paste", "first\nsecond\nthird")
    text = f"X {entry.label} Y {entry.label} Z"
    fields = ((0, 1), (len(entry.label) + 3, len(entry.label) + 4), (len(text) - 1, len(text)))
    expanded = prompt_draft.expand_draft(ChatDraft(text, fields=fields, references=references), len(text))

    assert expanded.text == "X first\nsecond\nthird Y first\nsecond\nthird Z"
    expected_fields = tuple((expanded.text.index(letter), expanded.text.index(letter) + 1) for letter in "XYZ")
    assert expanded.draft.fields == expected_fields
    assert expanded.cursor == len(expanded.text)
    for original_field, expanded_field in zip(fields, expected_fields, strict=True):
        assert (
            prompt_draft.expand_draft(ChatDraft(text, references=references), original_field[0]).cursor
            == expanded_field[0]
        )
        assert expanded.original_position(expanded_field[0]) == original_field[0]
        assert expanded.original_position(expanded_field[1]) == original_field[1]


def test_label_boundaries_and_inside_expanded_content_map_to_the_specified_edges() -> None:
    references, entry = EMPTY_REFERENCES.with_new("paste", "first\nsecond")
    text = f"a {entry.label} z"
    draft = ChatDraft(text, references=references)
    start, end = 2, 2 + len(entry.label)
    expanded = prompt_draft.expand_draft(draft, start)
    span = expanded.references[0]

    assert (span.original_start, span.original_end, span.start, span.end) == (start, end, 2, 14)
    assert expanded.cursor == span.start
    assert prompt_draft.expand_draft(draft, end).cursor == span.end
    assert prompt_draft.expand_draft(draft, start + 1).cursor == span.end
    assert expanded.original_position(span.start) == start
    for position in range(span.start + 1, span.end + 1):
        assert expanded.original_position(position) == end
    assert expanded.original_position(span.start - 1) == start - 1
    assert expanded.original_position(span.end + 1) == end + 1


def test_two_occurrences_of_the_same_label_have_distinct_expanded_ranges() -> None:
    references, entry = EMPTY_REFERENCES.with_new("paste", "first\nsecond")
    text = f"{entry.label} / {entry.label}"
    expanded = prompt_draft.expand_draft(ChatDraft(text, references=references), len(entry.label) + 3)

    assert expanded.cursor == len(entry.content) + 3
    assert [(span.start, span.end) for span in expanded.references] == [(0, 12), (15, 27)]
    assert [span.original_start for span in expanded.references] == [0, len(entry.label) + 3]


def test_expansion_does_not_strip_paste_whitespace_or_reinterpret_label_shaped_content() -> None:
    references, entry = EMPTY_REFERENCES.with_new("paste", "  [Image #1]\n\n")
    references, _image = references.with_new("image", "/a.png")
    draft = ChatDraft(entry.label, references=references)
    expanded = prompt_draft.expand_draft(draft, 0)

    assert expanded.text == "  [Image #1]\n\n"
    assert not expanded.draft.references
    assert draft.sent_text() == expanded.text
    assert expanded.draft.sent_text() == "[Image #1]"
