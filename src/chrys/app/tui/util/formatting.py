# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared formatting helpers for TUI display text."""

from __future__ import annotations

import unicodedata

from rich.cells import get_character_cell_size, load_cell_table, split_graphemes
from rich.text import Text


def format_token_count(n: int) -> str:
    """Format a token count with compact human-friendly units."""
    if n >= 999_950_000:
        return f"{n / 1_000_000_000:.1f}b"
    if n >= 999_950:
        return f"{n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def format_typed_count(n: int) -> str:
    """Shorten a count a person typed, so they can check its digits at a glance.

    As is below 1000, else K/M/B with up to two decimals and trailing zeros
    dropped (``128_000`` → ``128K``, ``1_280_000`` → ``1.28M``). A value the two
    decimals do not hold exactly is marked ``~`` (``3_500_008`` → ``~3.5M``).
    Rounding is half-up on integers, and a value that rounds to 1000 of a unit
    moves to the next one (``999_995`` → ``~1M``, never ``~1000K``).
    """
    if n < 1_000:
        return str(n)
    scale, unit = 1_000, "K"
    for next_scale, next_unit in ((1_000_000, "M"), (1_000_000_000, "B")):
        if (n * 100 + scale // 2) // scale < 100_000:
            break
        scale, unit = next_scale, next_unit
    whole, hundredths = divmod((n * 100 + scale // 2) // scale, 100)
    fraction = f"{hundredths:02d}".rstrip("0")
    approximate = "~" if n * 100 % scale else ""
    return f"{approximate}{whole}.{fraction}{unit}" if fraction else f"{approximate}{whole}{unit}"


def with_count_hint(label: Text, raw: str) -> Text:
    """Return *label* followed by a dim ``· 128K`` hint when *raw* is an integer ≥ 1000.

    The hint is ``format_typed_count``'s form, so a rounded value shows as ``· ~3.5M``.
    """
    try:
        value = int(raw.strip())
    except ValueError:
        return label
    if value < 1_000:
        return label
    hinted = label.copy()
    hinted.append(f" · {format_typed_count(value)}", style="dim")
    return hinted


def format_byte_size(size_bytes: int) -> str:
    """Format bytes as a human-readable string (e.g. '1.2 MB')."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    kib = size_bytes / 1024
    if kib < 1024:
        return f"{kib:.1f} KB"
    mib = kib / 1024
    if mib < 1024:
        return f"{mib:.1f} MB"
    gib = mib / 1024
    return f"{gib:.1f} GB"


def elapsed_parts(seconds: float) -> tuple[tuple[int, str], ...]:
    """Decompose elapsed seconds into at most two adjacent units, milliseconds through days.

    A duration under one second is milliseconds alone, truncated as the larger units are.
    """
    total = max(0, int(seconds))
    if total == 0:
        return ((max(0, int(seconds * 1000)), "ms"),)
    for scale, unit, remainder_scale, remainder_unit in (
        (86400, "d", 3600, "h"),
        (3600, "h", 60, "m"),
        (60, "m", 1, "s"),
    ):
        if total >= scale:
            whole, remainder = divmod(total, scale)
            part = remainder // remainder_scale
            return ((whole, unit), (part, remainder_unit)) if part else ((whole, unit),)
    return ((total, "s"),)


def format_elapsed(seconds: float) -> str:
    """Format elapsed seconds using at most two compact units, milliseconds under a second."""
    return " ".join(
        f"{value:02d}{unit}" if index else f"{value}{unit}"
        for index, (value, unit) in enumerate(elapsed_parts(seconds))
    )


_REGIONAL_INDICATOR_LOW = 0x1F1E6
_REGIONAL_INDICATOR_HIGH = 0x1F1FF
"""The two code points of a regional indicator, the halves of a flag emoji."""

_ZERO_WIDTH_FORMAT_CHARS = "\u200c\u200d\u200b\ufeff\u2060"
"""Format controls that render nothing and only dangle at an elision cut.

``split_graphemes`` glues these onto the preceding character (ZWJ, ZWNJ and
ZWSP are Extend in UAX #29; the BOM and word joiner are zero-width formats),
so a cluster can end with one even though it spends no cell.  Left next to the
ellipsis they join nothing, so they are stripped from the cut edge.
"""


def _is_regional_indicator(char: str) -> bool:
    """Return whether *char* is a regional indicator symbol (half a flag)."""
    return _REGIONAL_INDICATOR_LOW <= ord(char) <= _REGIONAL_INDICATOR_HIGH


def _regional_indicator_count(text: str, start: int, end: int) -> int:
    """How many regional indicator code points sit in ``text[start:end]``."""
    return sum(1 for index in range(start, end) if _is_regional_indicator(text[index]))


def _is_lone_regional_indicator(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` is a single regional indicator, a half flag.

    A flag is a pair of regional indicators (the second may carry a variation
    selector).  An unpaired one renders as a lettered box.
    """
    return _is_regional_indicator(text[start]) and _regional_indicator_count(text, start, end) == 1


def _trailing_virama(text: str, start: int, end: int) -> str | None:
    """The virama at the end of ``text[start:end]``, past any trailing Extends.

    ``split_graphemes`` glues a nukta (combining class 7) onto a cluster that
    already carries a virama, so the virama is not always the final code point.
    UAX #29 GB9c continues a conjunct through ``Linker Extend*`` to the next
    consonant, and those Extends must not hide the linker.
    """
    for index in range(end - 1, start - 1, -1):
        character = text[index]
        combining = unicodedata.combining(character)
        if combining == 9:
            return character
        if combining == 0:
            break
    return None


def _ends_with_virama(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` ends with a virama, possibly followed by Extends."""
    return _trailing_virama(text, start, end) is not None


def _same_indic_script(virama_char: str, letter_char: str) -> bool:
    """Whether *letter_char* is a letter of the script that owns *virama_char*.

    Unicode names start with the script for both a virama (``DEVANAGARI SIGN
    VIRAMA``) and its consonants (``DEVANAGARI LETTER SSA``). Comparing that
    first word keeps a Latin ``A`` beside a Devanagari virama out of the
    conjunct without a hard-coded table of Indic code-point blocks.
    """
    virama_name = unicodedata.name(virama_char, "")
    if not virama_name:
        return False
    script = virama_name.split(" ", 1)[0]
    return unicodedata.name(letter_char, "").startswith(f"{script} ")


def _starts_with_letter(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` begins with a letter."""
    return end > start and unicodedata.category(text[start]).startswith("L")


_NARROW_TO_WIDE = load_cell_table().narrow_to_wide
"""Characters ``rich`` renders one cell wider once a VS16 gives them emoji presentation."""


def _is_emoji_part(character: str) -> bool:
    """Whether *character* is a wide emoji glyph a ZWJ can join to the previous one."""
    return get_character_cell_size(character) == 2 and unicodedata.category(character) == "So"


def _joined_glyph_length(text: str, index: int) -> int:
    """Code points of the emoji glyph at *index* a ZWJ can join, or zero if it is none.

    A joined glyph is either a wide emoji already, or a narrow character its own
    VS16 turns into one: ``🏴‍☠️`` and ``👩‍❤️‍💋‍👨`` put such a character after a
    joiner, and the whole sequence still spends two cells.
    """
    if index >= len(text):
        return 0
    if _is_emoji_part(text[index]):
        return 1
    if text[index] in _NARROW_TO_WIDE and index + 1 < len(text) and text[index + 1] == "\ufe0f":
        return 2
    return 0


def _cell_width(text: str) -> int:
    """Measure *text* in terminal cells, ZWJ-aware.

    ``rich.cells.cell_len`` skips the code point after every ZWJ unconditionally,
    which is right for an emoji ZWJ sequence but under-counts a ZWJ glued to a
    non-emoji: it measures ``\\u200d中`` as zero cells though ``中`` really takes
    two.  Here a ZWJ only swallows the following glyph when the glyph before it is
    an emoji part and the one after joins it, so a width budget spends the cells a
    terminal really uses.  VS16 keeps ``rich``'s narrow-to-wide rule, so an emoji
    presentational sequence stays two cells, and a selector only ever widens the
    character it directly follows.
    """
    total = 0
    last_measured_character: str | None = None
    prev_is_emoji = False
    index = 0
    length = len(text)
    while index < length:
        character = text[index]
        if character == "\u200d":
            # A joiner swallows the glyph after it only when the glyph before is
            # an emoji part *and* the one after joins it; otherwise it joins
            # nothing and the following glyph spends its own cells.
            joined = _joined_glyph_length(text, index + 1) if prev_is_emoji else 0
            if joined:
                index += joined
            else:
                prev_is_emoji = False
            # Either way the character before whatever comes next is no longer
            # the one VS16 could widen.
            last_measured_character = None
        elif character == "\ufe0f":
            # VS16 gives a narrow-to-wide character its emoji presentation, but
            # only the character it directly follows: a selector that reaches a
            # base across a joiner or any other zero-width character changes
            # nothing.
            if last_measured_character is not None and last_measured_character in _NARROW_TO_WIDE:
                total += 1
                prev_is_emoji = True
            last_measured_character = None
        else:
            character_width = get_character_cell_size(character)
            if character_width:
                total += character_width
                last_measured_character = character
                prev_is_emoji = _is_emoji_part(character)
            else:
                # Every other zero-width character -- ZWNJ, ZWSP, the BOM, the
                # word joiner, a combining mark, a skin tone -- ends the base a
                # selector could widen; only ZWNJ and ZWSP additionally break a
                # joining sequence.
                last_measured_character = None
                if character in "\u200c\u200b":
                    prev_is_emoji = False
        index += 1
    return total


def _regional_indicator_units(text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every regional indicator plus its variation selectors.

    Units are found at the code-point level rather than from ``split_graphemes``
    spans, so a ZWJ that glues two indicators into one grapheme span cannot hide
    where one flag ends and the next begins.
    """
    units: list[tuple[int, int]] = []
    index = 0
    length = len(text)
    while index < length:
        if not _is_regional_indicator(text[index]):
            index += 1
            continue
        start = index
        index += 1
        while index < length and (text[index] in "\ufe0e\ufe0f" or unicodedata.combining(text[index]) != 0):
            index += 1
        units.append((start, index))
    return units


def _cluster_gap(text: str, offset: int) -> list[tuple[int, int, int]]:
    """Cluster a stretch with no regional indicators through ``split_graphemes``.

    A virama glued to its base but not to the following letter is merged here
    (UAX #29 GB9c), so an Indic conjunct stays whole.  Positions are absolute:
    *offset* is the stretch's start in the source text.
    """
    spans, _total_width = split_graphemes(text)
    clusters: list[tuple[int, int, int]] = []
    total = len(spans)
    index = 0
    while index < total:
        start, end, _reported_width = spans[index]
        index += 1
        while index < total:
            virama = _trailing_virama(text, start, end)
            if virama is None:
                break
            next_start, next_end, _next_width = spans[index]
            if not (
                next_start == end
                and _starts_with_letter(text, next_start, next_end)
                and _same_indic_script(virama, text[next_start])
            ):
                break
            index += 1
            end = next_end
        clusters.append((offset + start, offset + end, _cell_width(text[start:end])))
    return clusters


def _display_clusters(text: str) -> list[tuple[int, int, int]]:
    """Split *text* into user-perceived characters as ``(start, end, cell_width)``.

    ``rich.cells.split_graphemes`` keeps a ZWJ emoji sequence and a combining
    mark with its base in one cluster, but it reports the two regional
    indicators of a flag as two clusters, which would let a width budget cut a
    flag into two lettered boxes.  Regional indicators are therefore paired
    left-to-right at the code-point level (a half flag may carry a variation
    selector), and a ZWJ glued between two flags stays a separate zero-width
    cluster instead of misaligning the pairing.  An odd leftover indicator
    stays on its own, so it is kept or dropped whole with the rest of an end.

    An Indic virama is a combining mark, so ``split_graphemes`` glues it to the
    preceding letter but — not implementing UAX #29 GB9c — leaves the following
    letter separate; a following consonant of the same script is merged here,
    even with a nukta between them, so a conjunct is never cut whole.

    Widths are measured by :func:`_cell_width` rather than taken from the span:
    a span built by gluing a code point onto a zero-width joiner is reported as
    zero cells wide, while a ``ZWJ + wide letter`` really spends the letter's
    cells.
    """
    units = _regional_indicator_units(text)
    clusters: list[tuple[int, int, int]] = []
    cursor = 0
    index = 0
    while index < len(units):
        start, end = units[index]
        # Pair a regional indicator with the next one when they are adjacent.
        if index + 1 < len(units):
            next_start, next_end = units[index + 1]
            if next_start == end:
                index += 1
                end = next_end
        if cursor < start:
            clusters.extend(_cluster_gap(text[cursor:start], cursor))
        clusters.append((start, end, _cell_width(text[start:end])))
        cursor = end
        index += 1
    if cursor < len(text):
        clusters.extend(_cluster_gap(text[cursor:], cursor))
    return clusters


def _side_text(text: str, clusters: list[tuple[int, int, int]], indices: list[int], *, keep_left: bool) -> str:
    """Join *indices* into one end, stripping only the edge that meets the ellipsis.

    The ellipsis sits on the head's right and the tail's left, where a
    zero-width format character joins nothing.  The outer edge is part of the
    source text and may carry glyph meaning -- a Malayalam Chillu N ends in a
    ZWJ that shapes the letter -- so it is kept.  Measuring and rendering share
    this one string, so a width taken here is the width that is finally printed.
    """
    side = "".join(text[clusters[index][0] : clusters[index][1]] for index in indices)
    return side.rstrip(_ZERO_WIDTH_FORMAT_CHARS) if keep_left else side.lstrip(_ZERO_WIDTH_FORMAT_CHARS)


def _fit_side(
    text: str,
    clusters: list[tuple[int, int, int]],
    indices: list[int],
    cells: int,
    *,
    keep_left: bool,
) -> None:
    """Drop whole clusters from the ellipsis end until *indices* fits *cells*.

    A presentational sequence can measure one cell wider in Rich than in
    :func:`_cell_width`, so a side built to its ``_cell_width`` share may still
    overflow Rich's count.  Shrinking each side to its own Rich-managed share
    keeps the overrun on the side that caused it instead of draining the other.
    Popping from the ellipsis end (the head's tail, the tail's head) leaves the
    head a prefix and the tail a suffix, and ``indices`` shrinks each step so
    the loop terminates even across zero-width clusters.
    """
    while indices and Text(_side_text(text, clusters, indices, keep_left=keep_left)).cell_len > cells:
        if keep_left:
            indices.pop()
        else:
            indices.pop(0)


def _render_elision(
    text: str,
    clusters: list[tuple[int, int, int]],
    head_indices: list[int],
    tail_indices: list[int],
) -> str:
    """Join the chosen clusters into ``head…tail``, stripping only the cut edges."""
    head = _side_text(text, clusters, head_indices, keep_left=True)
    tail = _side_text(text, clusters, tail_indices, keep_left=False)
    return f"{head}…{tail}"


def elide_middle(text: str, max_width: int) -> str:
    """Cap *text* at *max_width* terminal cells, eliding the middle.

    Head and tail survive (both ends of a name tend to carry the
    distinguishing parts); widths are measured in cells so CJK names don't
    overflow the cap.  Cutting happens on display clusters rather than code
    points, so an emoji, a flag or a letter carrying a combining mark is kept
    whole or dropped whole, never sliced in half.  Head and tail stay a
    contiguous prefix and suffix of the input: even a regional indicator left
    without its partner is kept when it falls inside one, while zero-width
    format characters at the cut are stripped so the ellipsis never sits
    beside a dangling joiner.

    The result is never wider than *max_width* as the caller renders it.  The
    budget comes from :func:`_cell_width` (which spends a ZWJ-following glyph's
    cells unless the glyph joins an emoji sequence), then :class:`rich.text.Text`
    rechecks each side because Rich counts some presentation sequences a cell
    wider; a side that overflows its own share is shrunk on that side alone, so
    an overrun in the head never eats the tail.
    """
    if max_width <= 0:
        # Not even the ellipsis fits.
        return ""
    if _cell_width(text) <= max_width and Text(text).cell_len <= max_width:
        return text
    budget = max_width - 1  # the ellipsis cell
    tail_budget = budget // 2
    head_budget = budget - tail_budget
    clusters = _display_clusters(text)

    head_indices: list[int] = []
    head_used = 0
    for index, (_start, _end, width) in enumerate(clusters):
        if head_used + width > head_budget:
            break
        head_used += width
        head_indices.append(index)
    # A head of only zero-width characters is invisible and only glues the
    # ellipsis to nothing; drop it rather than spend the head's slot on it.
    if head_used == 0:
        head_indices = []

    tail_indices: list[int] = []
    tail_used = 0
    head_last = head_indices[-1] if head_indices else -1
    # Stop at the head's last cluster so the two ends never overlap.
    for index in range(len(clusters) - 1, head_last, -1):
        _start, _end, width = clusters[index]
        if tail_used + width > tail_budget:
            break
        tail_used += width
        tail_indices.append(index)
    tail_indices.reverse()
    if tail_used == 0:
        tail_indices = []

    # Rich can measure a presentational sequence one cell above _cell_width, so
    # shrink each side to its own Rich share rather than draining the tail.
    # After this Text(head) <= head_budget, Text(tail) <= tail_budget, and the
    # ellipsis is one cell, so the result renders at most
    # head_budget + 1 + tail_budget == max_width -- no second pass needed.
    _fit_side(text, clusters, head_indices, head_budget, keep_left=True)
    _fit_side(text, clusters, tail_indices, tail_budget, keep_left=False)
    return _render_elision(text, clusters, head_indices, tail_indices)
