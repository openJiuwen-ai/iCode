# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expand a prompt shortcut's template into draft text.

The placeholder syntax is the subset opencode and Claude Code share:

* ``$ARGUMENTS`` is everything typed after the command name;
* ``$1``, ``$2``, … are the arguments split on whitespace, a quoted run
  that opens an argument counting as one; the highest-numbered placeholder
  takes every remaining argument;
* a template with no placeholder gets the typed arguments appended after a
  blank line.

Unlike opencode, which sends at once and turns a missing argument into an
empty string, the expansion lands in the draft: a placeholder with no
argument becomes its hint text, and its span is returned as a *field* the
frontend highlights for the user to fill in.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

_ARGUMENT_RE = re.compile(r"\$(?:ARGUMENTS|([1-9]\d*))")
_POSITIONAL_RE = re.compile(r"\$([1-9]\d*)")
_ARGS_RE = re.compile(
    r"""
    (?<!\S)"((?:\\.|[^"\\])*)"             # a quoted run that opens an argument; \" escapes its quote
    | (?<!\S)"((?:\\.|[^"\\])*\\)"(?!\S)  # one closed only by a quote after its own backslash: "C:\dir\"
    | (?<!\S)'((?:\\.|[^'\\])*)'
    | (?<!\S)'((?:\\.|[^'\\])*\\)'(?!\S)
    | ([^\s"']\S*)                          # a bare word: a quote after its first character is part of it
    """,
    re.VERBOSE,
)


@dataclass(frozen=True, slots=True)
class PromptExpansion:
    """Draft text and the ``[start, end)`` code-point spans still to be filled in."""

    text: str
    fields: tuple[tuple[int, int], ...]


def parse_arguments(arguments: str) -> list[str]:
    """Split typed arguments on whitespace, keeping quoted runs together without their quotes.

    A quote opens a quoted run only at the start of an argument: inside a word
    it is part of the word, so ``it's broken`` is ``it's`` and ``broken``.

    As opencode's documentation shows, a quote inside a quoted argument is
    escaped with a backslash: ``"{ \\"key\\": \\"value\\" }"`` is ``{ "key": "value" }``.
    Any other backslash stays as typed, so a Windows path keeps its separators.
    """
    args: list[str] = []
    for match in _ARGS_RE.finditer(arguments):
        double, double_backslash_end, single, single_backslash_end, bare = match.groups()
        double = double if double is not None else double_backslash_end
        single = single if single is not None else single_backslash_end
        if double is not None:
            args.append(double.replace('\\"', '"'))
        elif single is not None:
            args.append(single.replace("\\'", "'"))
        else:
            args.append(bare)
    return args


def expand_template(
    template: str,
    arguments: str,
    *,
    default_hint: Callable[[int | None], str],
) -> PromptExpansion:
    """Expand *template* with the typed *arguments*.

    *default_hint* gives an unfilled placeholder its hint text: it gets the
    1-based position of ``$n``, or None for ``$ARGUMENTS``.
    """
    arguments = arguments.strip()
    args = parse_arguments(arguments)
    last = max((int(match) for match in _POSITIONAL_RE.findall(template)), default=0)

    pieces: list[str] = []
    fields: list[tuple[int, int]] = []
    length = 0
    cursor = 0
    has_placeholder = False
    for match in _ARGUMENT_RE.finditer(template):
        has_placeholder = True
        literal = template[cursor : match.start()]
        pieces.append(literal)
        length += len(literal)
        cursor = match.end()
        value, is_field = _placeholder_value(match.group(1), arguments, args, last, default_hint)
        pieces.append(value)
        if is_field and value:
            fields.append((length, length + len(value)))
        length += len(value)
    pieces.append(template[cursor:])
    text = "".join(pieces)

    if not has_placeholder and arguments:
        text = f"{text}\n\n{arguments}"
    stripped = text.lstrip()
    shift = len(text) - len(stripped)
    text = stripped.rstrip()
    clipped = tuple(
        (start - shift, end - shift) for start, end in fields if start - shift >= 0 and end - shift <= len(text)
    )
    return PromptExpansion(text=text, fields=clipped)


def _placeholder_value(
    position: str | None,
    arguments: str,
    args: list[str],
    last: int,
    default_hint: Callable[[int | None], str],
) -> tuple[str, bool]:
    """Return a placeholder's replacement and whether it is a field left to fill in."""
    if position is None:
        if arguments:
            return arguments, False
        return default_hint(None), True
    index = int(position) - 1
    if index < len(args):
        if index + 1 == last:
            return " ".join(args[index:]), False
        return args[index], False
    return default_hint(index + 1), True
