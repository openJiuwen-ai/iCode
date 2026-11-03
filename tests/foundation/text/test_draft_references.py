# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Draft labels for images, quotes and pasted text: which are live, how they edit, and what they send."""

from __future__ import annotations

from chrys.foundation.text.draft_references import (
    EMPTY_REFERENCES,
    DraftReference,
    DraftReferences,
    ReferenceKind,
    expand_references,
    expand_with_quote_spans,
    image_labels_to_paths,
    inert_quote,
    iter_reference_labels,
    label_boundary,
    label_safe_range,
    labels_as_words,
    labels_from_words,
    paste_folds,
)
from chrys.foundation.text.mentions import format_file_mention, iter_mention_tokens
from chrys.service.prompt_shortcuts.shell import shell_commands
from chrys.service.prompt_shortcuts.template import parse_arguments


def _with(*items: tuple[ReferenceKind, str]) -> tuple[DraftReferences, list[str]]:
    """References built from ``(kind, content)`` items, and their labels in order."""
    references = EMPTY_REFERENCES
    labels: list[str] = []
    for kind, content in items:
        references, entry = references.with_new(kind, content)
        labels.append(entry.label)
    return references, labels


def _live(text: str, references: DraftReferences) -> list[str]:
    return [text[label.start : label.end] for label in iter_reference_labels(text, references)]


def test_each_kind_is_numbered_from_one_on_its_own() -> None:
    _references, labels = _with(
        ("image", "/a.png"), ("quote", "x"), ("paste", "one\ntwo\nthree\nfour"), ("image", "/b.png"), ("paste", "p")
    )

    assert labels == ["[Image #1]", "[Quote #1]", "[Pasted text #1 +3 lines]", "[Image #2]", "[Pasted text #2]"]


def test_a_label_is_live_only_as_its_entry_reads_it() -> None:
    references, _labels = _with(("image", "/a.png"), ("paste", "a\nb"))
    text = "[Image #1] [Image #2] [Quote #1] [image #1] [Image #01] [Pasted text #1 +1 lines] [Pasted text #1]"

    assert _live(text, references) == ["[Image #1]", "[Pasted text #1 +1 lines]"]


def test_every_kind_has_the_same_fields() -> None:
    references, _labels = _with(("image", "/a.png"), ("quote", "q"), ("paste", "p"))

    assert references.entries == (
        DraftReference("image", 1, "/a.png"),
        DraftReference("quote", 1, "q"),
        DraftReference("paste", 1, "p"),
    )


def test_a_paste_folds_past_800_characters_or_too_many_newlines_for_the_screen() -> None:
    assert not paste_folds("x" * 800, screen_rows=40)
    assert paste_folds("x" * 801, screen_rows=40)
    assert not paste_folds("1\n2\n3", screen_rows=40)
    assert paste_folds("1\n2\n3\n4", screen_rows=40)
    assert not paste_folds("1\n2", screen_rows=11)
    assert paste_folds("1\n2\n3", screen_rows=11)
    assert paste_folds("1\n2", screen_rows=10)
    assert not paste_folds("one line", screen_rows=5)


def test_each_label_sends_what_it_stands_for_in_its_place() -> None:
    pasted = "line one\nline two\n  indented"
    references, _labels = _with(("image", "/shots/a b.png"), ("paste", pasted), ("quote", "said\nthis"))

    sent = expand_references("look [Image #1] at[Pasted text #1 +2 lines]and [Quote #1]是什么意思", references)

    mention = format_file_mention("/shots/a b.png")
    assert sent == f"look {mention} at{pasted}and \n> said\n> this\n\n是什么意思"
    assert [token.value for token in iter_mention_tokens(sent)] == ["/shots/a b.png"]


def test_a_quote_of_one_line_stays_in_place_and_one_of_several_is_a_block_quote_of_its_own() -> None:
    references, _labels = _with(("quote", "one line"), ("quote", "a\n# not a heading\n\n- not a bullet\n```\n"))

    assert expand_references("explain [Quote #1] please", references) == "explain “one line” please"
    sent = expand_references("explain [Quote #2] please", references)
    # On lines of its own, ended by a blank line: the quoted Markdown stays inside the quote.
    assert sent == "explain \n> a\n> # not a heading\n>\n> - not a bullet\n> ```\n\n please"
    assert expand_references("[Quote #2]", references) == "> a\n> # not a heading\n>\n> - not a bullet\n> ```"
    assert (
        expand_references("x\n[Quote #2]\n\ny", references)
        == "x\n> a\n> # not a heading\n>\n> - not a bullet\n> ```\n\ny"
    )


def test_a_label_named_twice_sends_its_content_twice() -> None:
    references, _labels = _with(("quote", "first"), ("quote", "second"), ("quote", "unused"))

    assert expand_references("[Quote #2] then [Quote #1] and [Quote #2]", references) == (
        "“second” then “first” and “second”"
    )


def test_an_image_mention_stands_apart_from_the_text_around_it() -> None:
    references, _labels = _with(("image", "/a.png"))

    sent = expand_references("看[Image #1]这张图", references)

    assert [token.value for token in iter_mention_tokens(sent)] == ["/a.png"]
    assert sent == f"看 {format_file_mention('/a.png')} 这张图"


def test_what_a_label_stands_for_is_sent_as_text_and_never_read_as_labels() -> None:
    references, _labels = _with(("quote", "see [Image #1] there"), ("image", "/a.png"))

    assert expand_references("[Quote #1]", references) == "“see [Image #1] there”"


def test_nothing_in_a_quote_acts_when_it_is_sent() -> None:
    references, _labels = _with(("quote", "@/tmp/first.png then see @src/a.py, run !`ls` and \\!`pwd`"))

    sent = expand_references("mine @/tmp/typed.png [Quote #1]", references)

    assert sent == "mine @/tmp/typed.png “\\@/tmp/first.png then see \\@src/a.py, run \\!`ls` and \\\\!`pwd`”"
    assert [token.value for token in iter_mention_tokens(sent)] == ["/tmp/typed.png"]
    assert shell_commands(sent) == []


def test_pasted_text_is_the_users_own_words_and_acts() -> None:
    references, _labels = _with(("paste", "see @src/a.py\nrun !`ls`\n\n."))

    sent = expand_references("[Pasted text #1 +3 lines]", references)

    assert [token.value for token in iter_mention_tokens(sent)] == ["src/a.py"]
    assert shell_commands(sent) == ["ls"]


def test_a_quote_without_anything_that_acts_is_sent_as_written() -> None:
    assert inert_quote("mail a@b.c about `code` and the ! mark") == "mail a@b.c about `code` and the ! mark"


def test_label_text_inside_a_paste_is_just_text() -> None:
    # One level, as Claude Code does: a paste holding label text sends that text.
    references, _labels = _with(("quote", "said"))
    references, _entry = references.with_new("paste", "see [Quote #1]\nline\nline\nline")

    sent, quotes = expand_with_quote_spans("[Quote #1] and [Pasted text #1 +3 lines]", references)

    assert sent == "“said” and see [Quote #1]\nline\nline\nline"
    assert [sent[start:end] for start, end in quotes] == ["“said”"]


def test_a_label_keeps_the_text_it_was_written_with() -> None:
    def chinese(kind: ReferenceKind, number: int, content: str) -> str:
        return {"image": f"[图片 #{number}]", "quote": f"[引用 #{number}]", "paste": f"[粘贴文本 #{number}]"}[kind]

    references, quote = EMPTY_REFERENCES.with_new("quote", "said", label=chinese)
    references, _image = references.with_new("image", "/a.png")

    assert quote.label == "[引用 #1]"
    assert _live("[引用 #1] [Quote #1] [Image #1]", references) == ["[引用 #1]", "[Image #1]"]
    assert expand_references("[引用 #1]", references) == "“said”"


def test_label_shaped_text_with_a_huge_number_is_plain_text() -> None:
    references, _labels = _with(("quote", "q"))
    huge = f"[Quote #{'9' * 5000}] [Quote #1234567890] [Quote #1]"

    assert expand_references(huge, references) == f"[Quote #{'9' * 5000}] [Quote #1234567890] “q”"


def test_unregistered_labels_and_plain_text_send_unchanged() -> None:
    assert expand_references("see [Image #3]", EMPTY_REFERENCES) == "see [Image #3]"
    references, _labels = _with(("quote", "q"))
    assert expand_references("no labels here", references) == "no labels here"


def test_image_labels_become_paths_for_a_model_without_vision_and_other_labels_stay() -> None:
    references, _labels = _with(("image", "/a.png"), ("quote", "q"), ("paste", "p\nq"))

    text = image_labels_to_paths("x[Image #1] [Quote #1] [Pasted text #1 +1 lines]", references)

    assert text == "x /a.png [Quote #1] [Pasted text #1 +1 lines]"


def test_a_position_inside_a_label_goes_to_the_edge_it_moves_toward() -> None:
    references, _labels = _with(("quote", "q"))
    text = "a [Quote #1] b"

    assert label_boundary(text, references, 5, forward=True) == 12
    assert label_boundary(text, references, 5, forward=False) == 2
    assert label_boundary(text, references, 2, forward=True) == 2
    assert label_boundary(text, references, 12, forward=False) == 12
    assert label_boundary(text, EMPTY_REFERENCES, 5, forward=True) == 5


def test_an_edit_takes_a_label_it_reaches_into_whole_and_inserts_beside_one() -> None:
    references, _labels = _with(("quote", "q"), ("paste", "p\nq"))
    text = "a [Quote #1][Pasted text #1 +1 lines] b"

    assert label_safe_range(text, references, 11, 12) == (2, 12)
    assert label_safe_range(text, references, 12, 13) == (12, 37)
    assert label_safe_range(text, references, 11, 13) == (2, 37)
    assert label_safe_range(text, references, 5, 5) == (12, 12)
    assert label_safe_range(text, references, 12, 12) == (12, 12)
    assert label_safe_range(text, references, 0, 1) == (0, 1)


def test_a_live_label_is_one_word_when_text_is_split_on_whitespace() -> None:
    references, _labels = _with(("image", "/a.png"), ("paste", "a\nb\nc\nd"), ("quote", "q"))
    typed = "[Image #1] x[Pasted text #1 +3 lines]y [Quote #1] [Image #2] [Pasted text #1 +9 lines]"

    words = labels_as_words(typed, references)

    assert len(words) == len(typed)
    assert [labels_from_words(word) for word in parse_arguments(words)] == [
        "[Image #1]",
        "x[Pasted text #1 +3 lines]y",
        "[Quote #1]",
        "[Image",
        "#2]",
        "[Pasted",
        "text",
        "#1",
        "+9",
        "lines]",
    ]
    assert labels_from_words(words) == typed
    assert labels_as_words("[Image #1] tail", EMPTY_REFERENCES) == "[Image #1] tail"
