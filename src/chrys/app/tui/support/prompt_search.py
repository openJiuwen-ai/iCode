# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tiered search over prompt-history entries.

Both sides are NFKC-normalized and case-folded, so full-width letters and
digits match their ASCII forms. The query splits on whitespace into terms, and
an entry matches only when every term matches it. Each term matches at the
best of these tiers, strongest first:

- ``WORD``: the term occurs as a whole word.
- ``PREFIX``: the term occurs starting at a word start.
- ``SUBSTRING``: the term occurs anywhere.
- ``ACRONYM``: the term spells the first characters of consecutive words.
- ``FUZZY``: the term's characters occur in order within a window at most
  ``_FUZZY_SPAN_FACTOR`` times the term's length.

A word is a run of letters and digits; each Han or kana character is a word by
itself, since those scripts are written without spaces. Entries rank by an
exact match of the whole entry first, then by their weakest term's tier, then
by the sum of their tiers, then by how tight their fuzzy matches are (fewest
skipped characters in total); anything still equal keeps its input order
(newest first for prompt history). Tiers always outrank recency.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from enum import IntEnum


class MatchTier(IntEnum):
    """How strongly one query term matches an entry; higher is stronger."""

    FUZZY = 1
    ACRONYM = 2
    SUBSTRING = 3
    PREFIX = 4
    WORD = 5


_FUZZY_SPAN_FACTOR = 3

_UNSPACED_SCRIPT_RANGES = (
    (0x3040, 0x30FF),  # Hiragana, Katakana
    (0x3400, 0x4DBF),  # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),  # CJK Unified Ideographs
    (0xF900, 0xFAFF),  # CJK Compatibility Ideographs
    (0x20000, 0x3FFFF),  # CJK Unified Ideographs Extensions B and later
)


def normalize_for_search(text: str) -> str:
    """Return *text* NFKC-normalized and case-folded for comparison."""
    return unicodedata.normalize("NFKC", text).casefold()


def _is_unspaced_script(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _UNSPACED_SCRIPT_RANGES)


def _joins_word(char: str) -> bool:
    """Return whether *char* continues a word with a neighbor of the same kind."""
    return char.isalnum() and not _is_unspaced_script(char)


def _is_boundary(text: str, index: int) -> bool:
    """Return whether a word boundary falls between ``text[index - 1]`` and ``text[index]``."""
    if index <= 0 or index >= len(text):
        return True
    return not (_joins_word(text[index - 1]) and _joins_word(text[index]))


def _word_initials(text: str) -> str:
    """Return the first character of every word in *text*, in order."""
    return "".join(
        char
        for index, char in enumerate(text)
        if (char.isalnum() or _is_unspaced_script(char)) and _is_boundary(text, index)
    )


def _contiguous_tier(term: str, text: str) -> MatchTier | None:
    best: MatchTier | None = None
    index = text.find(term)
    while index >= 0:
        if _is_boundary(text, index):
            if _is_boundary(text, index + len(term)):
                return MatchTier.WORD
            best = MatchTier.PREFIX
        elif best is None:
            best = MatchTier.SUBSTRING
        index = text.find(term, index + 1)
    return best


def _tightest_window(term: str, text: str) -> int | None:
    """Return the length of the shortest window holding *term* as a subsequence, or None.

    Each round takes the leftmost match after the previous window's start and
    tightens it from its end backwards, so every candidate start is tried once.
    """
    best: int | None = None
    search_from = 0
    while True:
        position = search_from
        for char in term:
            position = text.find(char, position)
            if position < 0:
                return best
            position += 1
        end = position
        position = end - 1
        for char in reversed(term):
            position = text.rfind(char, 0, position + 1)
            position -= 1
        start = position + 1
        if best is None or end - start < best:
            best = end - start
        search_from = start + 1


def _term_match(term: str, text: str, initials: str) -> tuple[MatchTier, int] | None:
    """Return the strongest tier at which normalized *term* matches normalized *text*, or None.

    The second value counts the characters a fuzzy match skips; it is 0 for
    every other tier.
    """
    tier = _contiguous_tier(term, text)
    if tier is not None:
        return tier, 0
    if term in initials:
        return MatchTier.ACRONYM, 0
    window = _tightest_window(term, text)
    if window is not None and window <= _FUZZY_SPAN_FACTOR * len(term):
        return MatchTier.FUZZY, window - len(term)
    return None


def rank_prompts(prompts: Sequence[str], query: str) -> list[str]:
    """Return the *prompts* matching every query term, strongest first, ties in input order."""
    terms = normalize_for_search(query).split()
    if not terms:
        return list(prompts)
    joined_query = " ".join(terms)
    ranked: list[tuple[bool, int, int, int, int, str]] = []
    for order, prompt in enumerate(prompts):
        text = normalize_for_search(prompt)
        initials = _word_initials(text)
        tiers: list[MatchTier] = []
        skipped = 0
        for term in terms:
            match = _term_match(term, text, initials)
            if match is None:
                break
            tiers.append(match[0])
            skipped += match[1]
        else:
            exact = " ".join(text.split()) == joined_query
            ranked.append((not exact, -min(tiers), -sum(tiers), skipped, order, prompt))
    ranked.sort()
    return [entry[-1] for entry in ranked]
