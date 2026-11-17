# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared formatting helpers for TUI display text."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len, split_graphemes

if TYPE_CHECKING:
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


def _is_regional_indicator(char: str) -> bool:
    """Return whether *char* is a regional indicator symbol (half a flag)."""
    return _REGIONAL_INDICATOR_LOW <= ord(char) <= _REGIONAL_INDICATOR_HIGH


def _regional_indicator_run_at_end(text: str) -> int:
    """How many regional indicators *text* ends with, a variation selector not breaking the run."""
    count = 0
    for char in reversed(text):
        if _is_regional_indicator(char):
            count += 1
        elif char not in "\ufe0e\ufe0f":
            break
    return count


def _regional_indicator_run_at_start(text: str) -> int:
    """How many regional indicators *text* starts with, a variation selector not breaking the run."""
    count = 0
    for char in text:
        if _is_regional_indicator(char):
            count += 1
        elif char not in "\ufe0e\ufe0f":
            break
    return count


def _display_clusters(text: str) -> list[tuple[int, int, int]]:
    """Split *text* into user-perceived characters as ``(start, end, cell_width)``.

    ``rich.cells.split_graphemes`` already keeps a ZWJ emoji sequence and a
    combining mark with its base in one cluster, but it reports the two regional
    indicators of a flag as two clusters, which would let a width budget cut a
    flag into two lettered boxes. A contiguous pair of regional indicators is
    therefore joined here; an odd leftover stays on its own.

    The width is measured on the substring rather than taken from the span:
    a span built by gluing a code point onto a zero-width one is reported as
    zero cells wide while the terminal really spends the code point's width.
    """
    spans, _total_width = split_graphemes(text)
    clusters: list[tuple[int, int, int]] = []
    total = len(spans)
    index = 0
    while index < total:
        start, end, _reported_width = spans[index]
        index += 1
        if end - start == 1 and _is_regional_indicator(text[start]) and index < total:
            next_start, next_end, _next_width = spans[index]
            # The second half may carry a variation selector, so it is only
            # required to *begin* with a regional indicator.
            if next_start == end and _is_regional_indicator(text[next_start]):
                index += 1
                end = next_end
        clusters.append((start, end, cell_len(text[start:end])))
    return clusters


def elide_middle(text: str, max_width: int) -> str:
    """Cap *text* at *max_width* terminal cells, eliding the middle.

    Head and tail survive (both ends of a name tend to carry the
    distinguishing parts); widths are measured in cells so CJK names don't
    overflow the cap. Cutting happens on display clusters rather than code
    points, so an emoji, a flag or a letter carrying a combining mark is kept
    whole or dropped whole, never sliced in half.

    The result is never wider than *max_width*: cluster widths are measured on
    the text itself, because ``split_graphemes`` reports a span it built around
    a zero-width character as zero cells wide.
    """
    if cell_len(text) <= max_width:
        return text
    if max_width <= 0:
        # Not even the ellipsis fits.
        return ""
    budget = max_width - 1  # the ellipsis cell
    tail_budget = budget // 2
    head_budget = budget - tail_budget
    clusters = _display_clusters(text)

    head_end = 0
    head_used = 0
    for cluster in clusters:
        if head_used + cluster[2] > head_budget:
            break
        head_used += cluster[2]
        head_end += 1
    # A regional indicator with no partner renders as a lettered box, so drop any
    # left stranded at the cut instead of spending cells on it.  Parity is read
    # from the head text, not from a cluster: a half flag can be glued to its
    # neighbour by a ZWJ, which no single cluster reports on its own.
    while head_end and _regional_indicator_run_at_end(text[: clusters[head_end - 1][1]]) % 2:
        head_end -= 1

    tail_start = len(clusters)
    tail_used = 0
    # Stop at head_end so the two ends can never cover the same cluster.
    for index in range(len(clusters) - 1, head_end - 1, -1):
        cluster = clusters[index]
        if tail_used + cluster[2] > tail_budget:
            break
        tail_used += cluster[2]
        tail_start = index
    # Same rule at the other end: a tail that starts inside a flag shows half of it.
    while tail_start < len(clusters) and _regional_indicator_run_at_start(text[clusters[tail_start][0] :]) % 2:
        tail_start += 1

    head_text = text[: clusters[head_end - 1][1]] if head_end else ""
    tail_text = text[clusters[tail_start][0] :] if tail_start < len(clusters) else ""
    return f"{head_text}…{tail_text}"
