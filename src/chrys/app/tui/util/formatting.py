# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared formatting helpers for TUI display text."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.cells import cell_len

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


def elide_middle(text: str, max_width: int) -> str:
    """Cap *text* at *max_width* terminal cells, eliding the middle.

    Head and tail survive (both ends of a name tend to carry the
    distinguishing parts); widths are measured in cells so CJK names don't
    overflow the cap.
    """
    if cell_len(text) <= max_width:
        return text
    if max_width <= 0:
        # Not even the ellipsis fits.
        return ""
    budget = max_width - 1  # the ellipsis cell
    tail_budget = budget // 2
    head_budget = budget - tail_budget
    head: list[str] = []
    used = 0
    for char in text:
        used += cell_len(char)
        if used > head_budget:
            break
        head.append(char)
    tail: list[str] = []
    used = 0
    for char in reversed(text):
        used += cell_len(char)
        if used > tail_budget:
            break
        tail.append(char)
    return f"{''.join(head)}…{''.join(reversed(tail))}"
