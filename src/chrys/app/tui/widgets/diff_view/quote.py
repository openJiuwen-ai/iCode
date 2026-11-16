# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Snapshot a raw diff line with the lines around it, complete provenance and bounded fences, and read it back."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from chrys.app.tui.widgets.diff_view.rows import DiffRow, RowKind, line_identity
from chrys.app.tui.widgets.text_area import MESSAGE_EDITOR_PASTE_MAX_TOKENS
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.text.tokenizer import TOKEN_UNITS_PER_TOKEN, MixedLanguageTokenizer

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from chrys.app.tui.util.diff_comments import LineIdentity
    from chrys.app.tui.widgets.diff_view.rows import Hunk


@dataclass(frozen=True)
class DiffQuote:
    """Immutable model text captured when the comment is opened."""

    content: str
    path: str
    line: LineIdentity  # The file version and line number the quote names.
    truncated: bool
    original_tokens: int
    context_omitted: bool = False
    """The lines around the quoted one were left out to keep the quote within the limit."""


@dataclass(frozen=True)
class QuotedLine:
    """One line of a quote as the model reads it: its number in the quoted version, kind and text."""

    number: int
    kind: RowKind  # CONTEXT, or REMOVED in the old version and ADDED in the new one.
    text: str
    quoted: bool


@dataclass(frozen=True)
class ParsedDiffQuote:
    """What a Diff quote's text says, read back from it for display."""

    path: str
    version: Literal["before", "after"]
    turn: int | None  # None when the quote covers the whole session.
    lines: tuple[QuotedLine, ...]


_MARKS = {RowKind.REMOVED: "-", RowKind.ADDED: "+", RowKind.CONTEXT: " "}
_KINDS = {mark: kind for kind, mark in _MARKS.items()}
_SESSION_SCOPE = "this session"
_PROMPT_PREVIEW_CHARS = 40


def quote_scope(turn: int | None, prompt: str | None = None) -> str:
    """Which changes a quote's version is relative to, in words the model can resolve.

    A turn number alone is hard for the model to count once history is compacted, so the start of the user
    message that began the turn goes with it.
    """
    if turn is None:
        return _SESSION_SCOPE
    preview = " ".join((prompt or "").split())
    if not preview:
        return f"turn {turn}"
    if len(preview) > _PROMPT_PREVIEW_CHARS:
        preview = preview[: _PROMPT_PREVIEW_CHARS - 1] + "…"
    return f'turn {turn}, the reply to the user message "{surrogate_safe_text(preview)}"'


def _render(path: str, version: str, scope: str, quoted: int, lines: Sequence[QuotedLine]) -> str:
    """The one text form of a Diff quote; :func:`parse_diff_quote` reads exactly this back."""
    header = f"File: {path} (as it was {version} the changes made in {scope}; line numbers count in that version)"
    target = f'Quoted line: {quoted}, marked with ">"'
    if len(lines) > 1:
        target += "; the lines around it are context"
    width = len(str(max(line.number for line in lines)))
    body = [f"{'>' if line.quoted else ' '} {line.number:>{width}} {_MARKS[line.kind]} {line.text}" for line in lines]
    fence = _fence("\n".join(line.text for line in lines))
    return "\n".join([header, target, fence, *body, fence])


def raw_line(row: DiffRow, lines: tuple[Sequence[str], Sequence[str]]) -> str:
    """Use comparison lines rather than sanitized display content (including tabs)."""
    if row.kind is RowKind.REMOVED and row.before is not None:
        return lines[0][row.before - 1]
    if row.after is not None:
        return lines[1][row.after - 1]
    return ""


def _fence(body: str) -> str:
    """The shortest run of at least three backticks that *body* does not contain.

    Substring searches run at C speed and a few of them suffice, where listing every run of a long line is slow.
    """
    high = 3
    while "`" * high in body:
        high *= 2
    low = high // 2  # A run this long occurs once ``high`` has grown.
    while high > 3 and high - low > 1:
        middle = (low + high) // 2
        if "`" * middle in body:
            low = middle
        else:
            high = middle
    return "`" * high


def _kind_in(version: str, number: int, hunks: Iterable[Hunk]) -> RowKind:
    """Whether line *number* of the *version* text was removed, added, or is unchanged context.

    Lines outside every hunk, the folded context included, are unchanged.
    """
    index = number - 1
    for hunk in hunks:
        for tag, i1, i2, j1, j2 in hunk:
            if version == "before" and tag in ("delete", "replace") and i1 <= index < i2:
                return RowKind.REMOVED
            if version == "after" and tag in ("insert", "replace") and j1 <= index < j2:
                return RowKind.ADDED
    return RowKind.CONTEXT


def build_diff_quote(
    row: DiffRow,
    lines: tuple[Sequence[str], Sequence[str]],
    path: str,
    scope: str,
    *,
    hunks: Iterable[Hunk] = (),
    max_tokens: int = MESSAGE_EDITOR_PASTE_MAX_TOKENS,
) -> DiffQuote | None:
    """The quoted line with the line before and after it in the same version, within *max_tokens*.

    When the three lines do not fit, the quoted line goes alone; when it alone does not fit either, it is
    shortened within the line. The header is never shortened.
    """
    identity = line_identity(row)
    if identity is None:
        return None
    # The line number counts in this version of the file: a removed line only exists before the change.
    version, number = identity
    # A filename that is not valid UTF-8 keeps surrogates, which no model request can encode.
    safe_path = surrogate_safe_text(path)
    raw = raw_line(row, lines)
    text = lines[0] if version == "before" else lines[1]
    kind = row.kind if row.kind in _MARKS else RowKind.CONTEXT

    def single(body: str) -> str:
        return _render(safe_path, version, scope, number, [QuotedLine(number, kind, body, quoted=True)])

    tokenizer = MixedLanguageTokenizer()
    budget = (max_tokens + 1) * TOKEN_UNITS_PER_TOKEN  # count_tokens(text) <= max_tokens exactly below this.
    content = single(raw)
    units = tokenizer.count_token_units(content)
    original_tokens = max(1, units // TOKEN_UNITS_PER_TOKEN)  # As count_tokens(content), without a second scan.
    around = [n for n in (number - 1, number + 1) if 1 <= n <= len(text)]
    # Every character costs at least this many units: a neighbour that cannot fit is never scanned.
    floor = min(tokenizer.count_token_units("a"), tokenizer.count_token_units("中"))
    if around and units + floor * sum(len(text[n - 1]) + 7 for n in around) < budget:
        quoted = [
            QuotedLine(
                n,
                kind if n == number else _kind_in(version, n, hunks),
                raw if n == number else text[n - 1],
                n == number,
            )
            for n in sorted((*around, number))
        ]
        context = _render(safe_path, version, scope, number, quoted)
        if tokenizer.count_token_units(context) < budget:
            return DiffQuote(context, path, identity, False, original_tokens)
    context_omitted = bool(around)
    truncated = units >= budget
    if truncated:

        def fits(length: int) -> bool:
            return tokenizer.count_token_units(single(raw[:length] + "…")) < budget

        # Token units add up per character, so the line's share of the budget predicts the longest prefix that
        # fits to within a few characters; gallop from there instead of searching the whole line.
        overhead = tokenizer.count_token_units(single("…"))  # A longer fence only shifts the guess by a few steps.
        line_units = units - tokenizer.count_token_units(single(""))
        room = budget - 1 - overhead
        low = high = max(0, min(len(raw), len(raw) * room // max(1, line_units)))
        step = 1
        if fits(low):
            while low + step < len(raw) and fits(low + step):
                low += step
                step *= 2
            high = min(len(raw), low + step)
        else:
            while high - step > 0 and not fits(high - step):
                high -= step
                step *= 2
            low = max(0, high - step)
        # Now prefixes up to ``low`` fit (or ``low`` is 0) and ``high`` does not.
        while high - low > 1:
            middle = (low + high) // 2
            if fits(middle):
                low = middle
            else:
                high = middle
        content = single(raw[:low] + "…")
        if tokenizer.count_token_units(content) >= budget:
            return None
    return DiffQuote(content, path, identity, truncated, original_tokens, context_omitted)


_HEADER = re.compile(
    r"File: (?P<path>.*) \(as it was (?P<version>before|after) the changes made in (?P<scope>.*); "
    r"line numbers count in that version\)"
)
# Numbers stop at nine digits, more than any line or turn count: a longer one is plain text, which int() may refuse.
_NUMBER = "[1-9][0-9]{0,8}"
_TARGET = re.compile(rf'Quoted line: (?P<number>{_NUMBER}), marked with ">"(?:; the lines around it are context)?')
_TURN_SCOPE = re.compile(rf'turn (?P<turn>{_NUMBER})(?:, the reply to the user message ".*")?')
_LINE = re.compile(rf"(?P<marker>[> ]) +(?P<number>{_NUMBER}) (?P<mark>[-+ ]) (?P<text>.*)")


def parse_diff_quote(content: str) -> ParsedDiffQuote | None:
    """The quote :func:`build_diff_quote` wrote as *content*, or None for any other text.

    Only the whole form is read back: anything that differs from it by one character is not a Diff quote.
    """
    parts = content.split("\n")  # Not splitlines(): that also splits at characters a code line may hold.
    if not 5 <= len(parts) <= 7:
        return None
    header, target = _HEADER.fullmatch(parts[0]), _TARGET.fullmatch(parts[1])
    if header is None or target is None:
        return None
    scope = header["scope"]
    turn_scope = _TURN_SCOPE.fullmatch(scope)
    if scope != _SESSION_SCOPE and turn_scope is None:
        return None
    version = "before" if header["version"] == "before" else "after"
    changed = RowKind.REMOVED if version == "before" else RowKind.ADDED
    lines: list[QuotedLine] = []
    for part in parts[3:-1]:
        line = _LINE.fullmatch(part)
        if line is None:
            return None
        kind = _KINDS[line["mark"]]
        if kind not in (changed, RowKind.CONTEXT):
            return None
        lines.append(QuotedLine(int(line["number"]), kind, line["text"], line["marker"] == ">"))
    numbers = [line.number for line in lines]
    quoted = [line.number for line in lines if line.quoted]
    if numbers != list(range(numbers[0], numbers[0] + len(numbers))) or quoted != [int(target["number"])]:
        return None
    # The quoted line has a neighbour on each side it can: only a file's first or last line goes without one.
    if (len(lines) == 3 and not lines[1].quoted) or (len(lines) == 2 and lines[0].quoted and numbers[0] != 1):
        return None
    if _render(header["path"], version, scope, quoted[0], lines) != content:
        return None
    turn = int(turn_scope["turn"]) if turn_scope is not None else None
    return ParsedDiffQuote(header["path"], version, turn, tuple(lines))
