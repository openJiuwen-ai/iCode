# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small terminal-native chart primitives used by the trajectory dashboard."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

from rich.cells import cell_len, set_cell_size
from rich.console import Console
from rich.style import Style
from rich.text import Text

from chrys.service.analytics.math import Interval

# One blank cell between each vertical border and the box content.
SECTION_BOX_PADDING = 1
# Cells a bordered section spends on chrome: two borders plus padding on both sides.
SECTION_BOX_CHROME = 2 + 2 * SECTION_BOX_PADDING


def section_interior_width(width: int) -> int:
    """Content cells available inside a bordered section of *width* cells."""
    return max(1, width - SECTION_BOX_CHROME)


def timeline_bar(
    start: int,
    end: int,
    *,
    origin: int,
    span: int,
    width: int,
    glyph: str = "▮",
    style: Style | str = "",
) -> Text:
    """Render one clipped interval into a fixed-width terminal bar."""
    usable = max(1, width)
    if span <= 0 or end <= start:
        return Text(" " * usable)
    left = min(usable - 1, max(0, (start - origin) * usable // span))
    right = min(usable, max(left + 1, (end - origin) * usable // span))
    return Text.assemble(Text(" " * left), Text(glyph * (right - left), style=style), Text(" " * (usable - right)))


def unresolved_bar(width: int, *, style: Style | str = "") -> Text:
    """Render an explicitly unknown interval without implying a duration."""
    usable = max(1, width)
    if usable == 1:
        return Text("?", style=style)
    return Text(f"?{'·' * (usable - 2)}?", style=style)


def time_ruler(span_ns: int, *, width: int) -> Text:
    """Render non-overlapping tick labels over a turn-relative nanosecond axis."""
    usable = max(1, width)
    cells = [" "] * usable
    tick_count = 1 if usable < 12 else min(7, usable // 12 + 1)
    for index in range(tick_count):
        denominator = max(1, tick_count - 1)
        position = index * (usable - 1) // denominator
        value = span_ns * index // denominator
        label = _axis_duration(value, span_ns)
        if len(label) > usable:
            label = label[:usable]
        start = min(max(0, position - len(label) // 2), usable - len(label))
        for offset, character in enumerate(label):
            cells[start + offset] = character
    return Text("".join(cells))


class WaterfallTurn[K](NamedTuple):
    """One turn of a waterfall: its length and each lane's intervals, as offsets from the turn's start."""

    span_ns: int
    intervals: Mapping[K, Iterable[Interval]]


class WaterfallLane[K](NamedTuple):
    """One lane of a waterfall: the key its intervals are stored under and how its cells are drawn."""

    key: K
    glyph: str
    style: Style | str


def waterfall_lanes[K](
    turns: Iterable[WaterfallTurn[K]],
    *,
    width: int,
    lanes: Sequence[WaterfallLane[K]],
    separator_style: Style | str = "dim",
) -> dict[K, Text]:
    """Render concatenated per-turn intervals as mutually exclusive lanes.

    Every terminal cell is painted in at most one lane: the one whose
    intervals cover the largest share of that cell's time window, ties going
    to the earlier entry in *lanes*.  A lane therefore never claims an
    instant another lane owns, and sub-cell slivers cannot bridge a gap the
    neighbouring lane is drawn in.
    """
    keys = [lane.key for lane in lanes]
    materialized = [(max(0, turn.span_ns), {key: tuple(turn.intervals.get(key, ())) for key in keys}) for turn in turns]
    total = sum(span for span, _ in materialized)
    usable = max(1, width)
    cells: dict[K, list[str]] = {key: [" "] * usable for key in keys}
    if total <= 0:
        return {key: Text("".join(cells[key])) for key in keys}
    coverage = {key: [0.0] * usable for key in keys}
    separators: list[int] = []
    cursor = 0
    for index, (span, intervals) in enumerate(materialized):
        turn_left = cursor * usable // total
        turn_right = (cursor + span) * usable // total
        turn_width = max(1, turn_right - turn_left)
        for key in keys:
            lane_coverage = coverage[key]
            for start, end in intervals[key]:
                if end <= start or span <= 0:
                    continue
                first = max(0.0, start * turn_width / span)
                last = min(float(turn_width), end * turn_width / span)
                cell = int(first)
                while cell < last and turn_left + cell < usable:
                    lane_coverage[turn_left + cell] += min(cell + 1, last) - max(cell, first)
                    cell += 1
        cursor += span
        if index < len(materialized) - 1:
            separators.append(min(usable - 1, cursor * usable // total))
    for cell in range(usable):
        winner = max(keys, key=lambda key: coverage[key][cell])
        if coverage[winner][cell] > 0:
            cells[winner][cell] = next(lane.glyph for lane in lanes if lane.key == winner)
    for separator in separators:
        for key in keys:
            cells[key][separator] = "┊"
    rendered: dict[K, Text] = {}
    for lane in lanes:
        text = Text("".join(cells[lane.key]))
        for index, character in enumerate(cells[lane.key]):
            if character == lane.glyph:
                text.stylize(lane.style, index, index + 1)
            elif character == "┊":
                text.stylize(separator_style, index, index + 1)
        rendered[lane.key] = text
    return rendered


def coverage_bar(
    exact: float,
    estimated: float,
    missing: float,
    unresolved: float,
    *,
    width: int = 8,
    exact_style: Style | str = "",
    estimated_style: Style | str = "",
    missing_style: Style | str = "",
    unresolved_style: Style | str = "",
) -> Text:
    """Render a closed four-state coverage composition."""
    usable = max(4, width)
    shares = (max(0.0, exact), max(0.0, estimated), max(0.0, missing), max(0.0, unresolved))
    total = sum(shares)
    if total <= 0:
        return Text("?" * usable)
    raw = [share / total * usable for share in shares]
    counts = [int(value) for value in raw]
    for index in sorted(range(4), key=lambda item: raw[item] - counts[item], reverse=True)[: usable - sum(counts)]:
        counts[index] += 1
    return Text.assemble(
        Text("█" * counts[0], style=exact_style),
        Text("▒" * counts[1], style=estimated_style),
        Text("░" * counts[2], style=missing_style),
        Text("·" * counts[3], style=unresolved_style),
    )


def percentage_meter(
    value: float | None,
    *,
    width: int = 10,
    style: Style | str = "",
    empty_style: Style | str = "dim",
) -> Text:
    """Render an htop-style bracket meter with a right-aligned percentage."""
    usable = max(3, width)
    if value is None:
        fill = 0
        rendered_value = "—"
    else:
        bounded = min(100.0, max(0.0, value))
        fill = round(bounded * usable / 100)
        rendered_value = f"{value:.1f}%"
    return Text.assemble(
        Text("["),
        Text("█" * fill, style=style),
        Text("░" * (usable - fill), style=empty_style),
        Text(f" {rendered_value:>6}"),
        Text("]"),
    )


def fit_cells(value: str, width: int) -> str:
    """Crop or pad text to exactly *width* terminal cells."""
    return set_cell_size(value, max(0, width))


def fit_text_cells(value: Text, width: int) -> Text:
    """Crop styled text with an ellipsis, or pad it, to exactly *width* terminal cells."""
    if width <= 0:
        return Text()
    fitted = value.copy()
    fitted.truncate(width, overflow="ellipsis", pad=True)
    return fitted


def bordered_section(
    title: str | Text,
    lines: Iterable[Text],
    *,
    width: int,
    console: Console,
    border_style: Style | str = "dim",
    title_style: Style | str = "bold",
    content_height: int | None = None,
) -> list[Text]:
    """Wrap content in a fixed-cell-width box with an inline title."""
    usable = max(6, width)
    interior_width = usable - SECTION_BOX_CHROME
    padding = " " * SECTION_BOX_PADDING
    rendered_title = title.copy() if isinstance(title, Text) else Text(title)
    rendered_title.stylize_before(title_style)
    rendered_title.truncate(usable - 6, overflow="ellipsis")
    top_fill = usable - cell_len(rendered_title.plain) - 5
    top = Text.assemble(
        Text("┌─ ", style=border_style),
        rendered_title,
        Text(" "),
        Text(f"{'─' * top_fill}┐", style=border_style),
    )
    wrapped: list[Text] = []
    for line in lines:
        wrapped.extend(list(line.wrap(console, interior_width, overflow="fold")) or [Text()])
    target_height = max(len(wrapped), content_height or 0)
    wrapped.extend(Text() for _ in range(target_height - len(wrapped)))
    body = [
        Text.assemble(
            Text("│", style=border_style),
            Text(padding),
            fit_text_cells(line, interior_width),
            Text(padding),
            Text("│", style=border_style),
        )
        for line in wrapped
    ]
    bottom = Text(f"└{'─' * (usable - 2)}┘", style=border_style)
    return [top, *body, bottom]


def _axis_duration(value_ns: int, span_ns: int) -> str:
    """Compact tick label whose unit follows the magnitude: ms → s → m → h → d.

    The origin borrows the axis span's unit so ``0s`` sits next to ``1m46s``
    while a sub-second axis still reads ``0ms … 800ms``.
    """
    seconds = value_ns / 1_000_000_000
    if value_ns <= 0:
        return "0ms" if span_ns < 1_000_000_000 else "0s"
    # The unit is chosen after rounding, so 999.6 ms reads 1.0s rather than
    # 1000ms — the same rule the turn and operation durations use.
    milliseconds = round(seconds * 1000)
    if milliseconds < 1000:
        return f"{milliseconds}ms"
    if seconds < 10:
        return f"{seconds:.1f}s"
    whole_seconds = round(seconds)
    if whole_seconds < 60:
        return f"{whole_seconds}s"
    minutes, remainder = divmod(whole_seconds, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d{hours:02d}h"
    return f"{hours}h{minutes:02d}m"
