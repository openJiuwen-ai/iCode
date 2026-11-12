# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Snapshot one raw diff line with complete provenance and bounded fences."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.app.tui.widgets.diff_view.rows import DiffRow, RowKind, line_identity
from chrys.app.tui.widgets.text_area import MESSAGE_EDITOR_PASTE_MAX_TOKENS
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.text.tokenizer import TOKEN_UNITS_PER_TOKEN, MixedLanguageTokenizer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from chrys.app.tui.util.diff_comments import LineIdentity


@dataclass(frozen=True)
class DiffQuote:
    """Immutable model text captured when the comment is opened."""

    content: str
    path: str
    line: LineIdentity  # The file version and line number the quote names.
    truncated: bool
    original_tokens: int


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


def build_diff_quote(
    row: DiffRow,
    lines: tuple[Sequence[str], Sequence[str]],
    path: str,
    source: str,
    *,
    max_tokens: int = MESSAGE_EDITOR_PASTE_MAX_TOKENS,
) -> DiffQuote | None:
    """Keep the header whole and, when needed, shorten within the single code line."""
    identity = line_identity(row)
    if identity is None:
        return None
    # The line number counts in this version of the file: a removed line only exists before the change.
    version, number = identity
    # A filename that is not valid UTF-8 keeps surrogates, which no model request can encode.
    header = f"File: {surrogate_safe_text(path)} ({version}) | Source: {source}"
    raw = raw_line(row, lines)
    prefix = {RowKind.REMOVED: "-", RowKind.ADDED: "+"}.get(row.kind, " ")

    def fenced(body: str) -> str:
        fence = _fence(body)
        return header + "\n" + fence + "\n" + f"{number} {prefix} {body}" + "\n" + fence

    tokenizer = MixedLanguageTokenizer()
    content = fenced(raw)
    units = tokenizer.count_token_units(content)
    original_tokens = max(1, units // TOKEN_UNITS_PER_TOKEN)  # As count_tokens(content), without a second scan.
    truncated = original_tokens > max_tokens
    if truncated:

        def fits(length: int) -> bool:
            return tokenizer.count_tokens(fenced(raw[:length] + "…")) <= max_tokens

        # Token units add up per character, so the line's share of the budget predicts the longest prefix that
        # fits to within a few characters; gallop from there instead of searching the whole line.
        overhead = tokenizer.count_token_units(fenced("…"))  # A longer fence only shifts the guess by a few steps.
        line_units = units - tokenizer.count_token_units(fenced(""))
        budget = (max_tokens + 1) * TOKEN_UNITS_PER_TOKEN - 1 - overhead
        low = high = max(0, min(len(raw), len(raw) * budget // max(1, line_units)))
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
        content = fenced(raw[:low] + "…")
        if tokenizer.count_tokens(content) > max_tokens:
            return None
    return DiffQuote(content, path, identity, truncated, original_tokens)
