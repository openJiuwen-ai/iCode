# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for prompt shortcut argument substitution (opencode-compatible) and hint fields."""

from __future__ import annotations

import pytest

from chrys.service.prompt_shortcuts import PromptExpansion, expand_template
from chrys.service.prompt_shortcuts.template import parse_arguments


def _default_hint(position: int | None) -> str:
    return "args" if position is None else f"arg{position}"


def _expand(template: str, arguments: str) -> PromptExpansion:
    return expand_template(template, arguments, default_hint=_default_hint)


def _field_texts(expansion: PromptExpansion) -> list[str]:
    return [expansion.text[start:end] for start, end in expansion.fields]


@pytest.mark.parametrize(
    ("template", "arguments", "expected"),
    [
        ("Tone: $1, text: $2", "formal hello", "Tone: formal, text: hello"),
        ("Fix all of: $ARGUMENTS", "a  b c", "Fix all of: a  b c"),
        ("$1 / $2", "a b c d", "a / b c d"),
        ("only $2", "a b c", "only b c"),
        ("$1|$2", '"a b" c', "a b|c"),
        ("$1|$2", "'a b' c", "a b|c"),
        ("$1|$2", 'x "y z"', "x|y z"),
        ("$1 :: $ARGUMENTS", "x y", "x y :: x y"),
        ("$1 and $1 again", "one two", "one two and one two again"),
        ("a  $1  b", "x", "a  x  b"),
        ("语气要$1：", "正式", "语气要正式："),  # noqa: RUF001
        ("语气要 $1：", "正式", "语气要 正式："),  # noqa: RUF001
        ("Do it", "now please", "Do it\n\nnow please"),
        ("Do it", "", "Do it"),
        ("Do it", "   ", "Do it"),
    ],
    ids=[
        "positional",
        "arguments-raw",
        "highest-takes-rest",
        "highest-only-takes-rest",
        "double-quoted",
        "single-quoted",
        "quoted-last",
        "mixed-arguments-and-positional",
        "repeated-placeholder",
        "spaces-kept",
        "cjk-no-space",
        "cjk-with-space",
        "no-placeholder-appends",
        "no-placeholder-no-args",
        "no-placeholder-blank-args",
    ],
)
def test_arguments_substitute_like_opencode(template: str, arguments: str, expected: str) -> None:
    expansion = _expand(template, arguments)

    assert expansion.text == expected
    assert expansion.fields == ()


def test_quoted_arguments_stay_quoted_in_dollar_arguments() -> None:
    assert _expand("Q: $ARGUMENTS", '"a b" c').text == 'Q: "a b" c'


def test_missing_positionals_become_default_hint_fields() -> None:
    expansion = _expand("Translate in a $1 tone:\n\n$2", "")

    assert expansion.text == "Translate in a arg1 tone:\n\narg2"
    assert _field_texts(expansion) == ["arg1", "arg2"]
    assert expansion.fields == ((15, 19), (27, 31))


def test_missing_arguments_placeholder_becomes_whole_default_hint() -> None:
    expansion = _expand("Review $ARGUMENTS please", "")

    assert expansion.text == "Review args please"
    assert expansion.fields == ((7, 11),)


def test_partial_arguments_leave_the_rest_as_fields() -> None:
    expansion = _expand("$1-$2-$3", "a")

    assert expansion.text == "a-arg2-arg3"
    assert _field_texts(expansion) == ["arg2", "arg3"]


def test_every_occurrence_of_a_missing_placeholder_is_a_field() -> None:
    expansion = _expand("$1 then $1", "")

    assert expansion.text == "arg1 then arg1"
    assert expansion.fields == ((0, 4), (10, 14))


def test_default_hint_receives_position_or_none() -> None:
    seen: list[int | None] = []

    def record(position: int | None) -> str:
        seen.append(position)
        return "h"

    expand_template("$2 $ARGUMENTS", "", default_hint=record)

    assert sorted(seen, key=lambda p: -1 if p is None else p) == [None, 2]


def test_fields_are_code_point_spans() -> None:
    expansion = expand_template(
        "😀 语气要$1：$2",  # noqa: RUF001
        "",
        default_hint=lambda position: "语气" if position == 1 else "正文",
    )

    assert expansion.text == "😀 语气要语气：正文"  # noqa: RUF001
    assert expansion.fields == ((5, 7), (8, 10))
    assert _field_texts(expansion) == ["语气", "正文"]


def test_bang_command_stays_literal() -> None:
    expansion = _expand("Stats:\n!`git diff --stat`\nFocus $1", "tests")

    assert expansion.text == "Stats:\n!`git diff --stat`\nFocus tests"


# -- quoting inside an argument -------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ('config.json src "{ \\"key\\": \\"value\\" }"', ["config.json", "src", '{ "key": "value" }']),
        ('"C:\\Program Files\\x" y', ["C:\\Program Files\\x", "y"]),
        ('"C:\\dir\\" x', ["C:\\dir\\", "x"]),
        ("'C:\\dir\\' x", ["C:\\dir\\", "x"]),
        ("'it\\'s' ok", ["it's", "ok"]),
        ('a "b c" \'d e\' ""', ["a", "b c", "d e", ""]),
        ('"open x', ["open", "x"]),
    ],
    ids=[
        "opencode-escaped-quotes",
        "windows-path-keeps-backslashes",
        "trailing-backslash-before-quote",
        "trailing-backslash-before-single-quote",
        "single-quote-escape",
        "quoted-runs-and-empty",
        "unterminated-quote",
    ],
)
def test_a_backslash_escapes_only_its_own_quote(arguments: str, expected: list[str]) -> None:
    assert parse_arguments(arguments) == expected


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("it's broken", ["it's", "broken"]),
        ('don\'t "a b"', ["don't", "a b"]),
        ("O'Brien's 'x y' z", ["O'Brien's", "x y", "z"]),
        ('say"hi" there', ['say"hi"', "there"]),
        ("rock'n'roll", ["rock'n'roll"]),
    ],
    ids=[
        "apostrophe",
        "apostrophe-then-quoted",
        "apostrophes-then-single-quoted",
        "double-quote-in-word",
        "two-in-word",
    ],
)
def test_a_quote_inside_a_word_is_part_of_the_word(arguments: str, expected: list[str]) -> None:
    """Only a quote that starts an argument opens a quoted run."""
    assert parse_arguments(arguments) == expected


def test_an_apostrophe_reaches_its_placeholder_whole() -> None:
    assert _expand("Review: $1 | rest: $2", "it's broken now").text == "Review: it's | rest: broken now"
    assert _expand("All: $ARGUMENTS", "it's broken now").text == "All: it's broken now"


def test_each_quote_escapes_only_itself() -> None:
    # Inside single quotes \" is not an escape, and inside double quotes \' is not: the backslash stays.
    assert parse_arguments(r"""'say \"hi\"'""") == [r"say \"hi\""]
    assert parse_arguments(r'"it\'s"') == [r"it\'s"]


def test_opencodes_documented_example_lands_in_its_placeholder() -> None:
    expansion = _expand(
        "Create $1 in $2 with: $3",
        'config.json src "{ \\"key\\": \\"value\\" }"',
    )

    assert expansion.text == 'Create config.json in src with: { "key": "value" }'
    assert expansion.fields == ()
