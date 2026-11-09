# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Building blocks the trajectory dashboard's pages share: layout tiers, the look
pages draw with, precision badges, section boxes and metric formatting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import NamedTuple

from rich.cells import cell_len
from rich.console import Console
from rich.style import Style
from rich.text import Text

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.formatting import format_token_count
from chrys.app.tui.util.rich_style import rich_style_from_textual_color
from chrys.app.tui.widgets.trajectory.chartkit import bordered_section
from chrys.foundation.i18n import Localizer, MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.service.analytics import Grade, Metric, Precision, TokenUsage, UsageBucket

UNAVAILABLE = msg("tui.trajectory.unavailable", fallback="No trajectory data is available for this session.")
NO_TURNS = msg("tui.trajectory.no_turns", fallback="No completed turns are available.")
ELAPSED_SCOPE = msg(
    "tui.trajectory.elapsed_scope",
    fallback="Total time excludes preparation between submission and the start of the turn.",
)
_PRECISION_EXACT = msg("tui.trajectory.precision.exact", fallback="exact")
_PRECISION_ESTIMATED = msg("tui.trajectory.precision.estimated", fallback="estimated")
_PRECISION_MISSING = msg("tui.trajectory.precision.missing", fallback="missing")
_PRECISION_UNRESOLVED = msg("tui.trajectory.precision.unresolved", fallback="unresolved")
_PRECISION_LABELS = {
    Precision.EXACT: _PRECISION_EXACT,
    Precision.ESTIMATED: _PRECISION_ESTIMATED,
    Precision.MISSING: _PRECISION_MISSING,
    Precision.UNRESOLVED: _PRECISION_UNRESOLVED,
}
PRECISION_SYMBOLS = {
    Precision.EXACT: "✓",
    Precision.ESTIMATED: "~",
    Precision.MISSING: "−",  # noqa: RUF001
    Precision.UNRESOLVED: "✗",
}
METRIC_ELAPSED = msg("tui.trajectory.metric.elapsed", fallback="total time")
METRIC_CP_RESPONSE = msg("tui.trajectory.metric.cp_response", fallback="bottleneck (response)")
METRIC_CP_COMPUTE = msg("tui.trajectory.metric.cp_compute", fallback="bottleneck (compute)")
METRIC_WORK = msg("tui.trajectory.metric.work", fallback="actual work time")
METRIC_PARALLELISM = msg("tui.trajectory.metric.parallelism", fallback="parallelism")
METRIC_OVERLAP = msg("tui.trajectory.metric.overlap", fallback="parallel time saved")
METRIC_USAGE = msg("tui.trajectory.metric.usage", fallback="total token usage")
BUCKET_MODEL = msg("tui.trajectory.bucket.model", fallback="model")
BUCKET_TOOLS = msg("tui.trajectory.bucket.tools", fallback="tools")
BUCKET_WAIT = msg("tui.trajectory.bucket.wait", fallback="wait")
BUCKET_IDLE = msg("tui.trajectory.bucket.idle", fallback="idle")
TURN_LABEL = msg("tui.trajectory.turn", fallback="Turn {turn}")
TIME_RULER = msg("tui.trajectory.time_ruler", fallback="time")
TOKEN_INPUT = msg("tui.trajectory.token_usage.input", fallback="input")
TOKEN_OUTPUT = msg("tui.trajectory.token_usage.output", fallback="output")
TOKEN_REASONING = msg("tui.trajectory.token_usage.reasoning", fallback="reasoning")
TOKEN_CACHE_READ = msg("tui.trajectory.token_usage.cache_read", fallback="cache read")
TOKEN_CACHE_HIT = msg("tui.trajectory.token_usage.cache_hit", fallback="cache hit")
TOOL_USAGE_MORE = msg("tui.trajectory.tool_usage.more", fallback="+{value} more")

WIDE_MIN_COLUMNS = 120
_MID_MIN_COLUMNS = 80
_NARROW_MIN_COLUMNS = 60

_HOOK_ID_DISPLAY_MAX_LENGTH = 16
_HOOK_ID_TRUNCATION_SUFFIX = "..."
_HOOK_EVENT_DISPLAY_NAMES = {
    "before_tool_call": "before_tool",
    "after_tool_call": "after_tool",
}


class ResponsiveTier(StrEnum):
    """Panel-local width tiers frozen by the trajectory UI contract."""

    WIDE = "wide"
    MID = "mid"
    NARROW = "narrow"
    FLOOR = "floor"

    @classmethod
    def for_width(cls, width: int) -> ResponsiveTier:
        """The tier of a dashboard *width* columns wide."""
        if width >= WIDE_MIN_COLUMNS:
            return cls.WIDE
        if width >= _MID_MIN_COLUMNS:
            return cls.MID
        if width >= _NARROW_MIN_COLUMNS:
            return cls.NARROW
        return cls.FLOOR


@dataclass(frozen=True, slots=True)
class RenderContext:
    """The layout a page renders for.

    *width* is the content width the page lays out in. *tier* follows the
    dashboard's own width instead, so the re-render for settled scrollbars
    narrows *width* without changing the tier.
    """

    width: int
    tier: ResponsiveTier


@dataclass(frozen=True, slots=True)
class DashboardLook:
    """What drawing a page depends on beyond its data and layout: the console
    that measures text, the theme's colour variables and the active locale."""

    console: Console
    theme_variables: Mapping[str, str]
    localizer: Localizer | None

    def message(self, reference: MessageRef) -> str:
        return render_message(self.localizer, reference)

    def semantic_style(self, name: str, fallback: str, *, bold: bool | None = None) -> Style:
        return rich_style_from_textual_color(self.theme_variables.get(name, fallback), bold=bold)


def render_message(localizer: Localizer | None, reference: MessageRef) -> str:
    """*reference* in the active locale, or its English fallback when there is no localizer."""
    if localizer is None:
        return format_message(reference)
    return render_str(localizer, reference)


def identity_with_hook_id(identity: str, hook_id: str | None) -> str:
    displayed_identity = _HOOK_EVENT_DISPLAY_NAMES.get(identity, identity)
    if hook_id is None:
        return displayed_identity
    displayed_hook_id = hook_id
    if len(displayed_hook_id) > _HOOK_ID_DISPLAY_MAX_LENGTH:
        prefix_length = _HOOK_ID_DISPLAY_MAX_LENGTH - len(_HOOK_ID_TRUNCATION_SUFFIX)
        displayed_hook_id = f"{displayed_hook_id[:prefix_length]}{_HOOK_ID_TRUNCATION_SUFFIX}"
    if hook_id == identity:
        return displayed_hook_id
    return f"{displayed_identity} ({displayed_hook_id})"


class SectionSpec(NamedTuple):
    """One boxed section of a row: its title, its content lines and the box's outer width."""

    title: MessageDef | MessageRef | Text
    lines: list[Text]
    width: int


def bordered_section_row(look: DashboardLook, specs: tuple[SectionSpec, ...]) -> list[Text]:
    boxes = [section_box(look, spec.title, spec.lines, width=spec.width) for spec in specs]
    content_height = max(len(box) - 2 for box in boxes)
    boxes = [
        section_box(look, spec.title, spec.lines, width=spec.width, content_height=content_height) for spec in specs
    ]
    rows: list[Text] = []
    for index in range(len(boxes[0])):
        parts: list[Text] = []
        for box in boxes:
            if parts:
                parts.append(Text(" "))
            parts.append(box[index])
        rows.append(Text.assemble(*parts))
    return rows


def metric_value(metric: Metric) -> str:
    if metric.value is None:
        return "—"
    if isinstance(metric.value, float):
        return f"{metric.value:.2f}×"  # noqa: RUF001
    return format_duration(metric.value)


def precision_badge(look: DashboardLook, precision: Precision) -> Text:
    return Text(PRECISION_SYMBOLS[precision], style=precision_style(look, precision))


def precision_style(look: DashboardLook, precision: Precision) -> Style:
    if precision is Precision.EXACT:
        return look.semantic_style("success", "green", bold=True)
    if precision is Precision.ESTIMATED:
        return look.semantic_style("warning", "yellow", bold=True)
    if precision is Precision.UNRESOLVED:
        return look.semantic_style("error", "red", bold=True)
    return Style(dim=True)


def section_style(look: DashboardLook) -> Style:
    return look.semantic_style("primary", "blue", bold=True)


def _border_style(look: DashboardLook) -> Style:
    return Style.combine([look.semantic_style("secondary", "bright_black"), Style(dim=True)])


def precision_label(look: DashboardLook, precision: Precision) -> str:
    return look.message(_PRECISION_LABELS[precision].bind())


def section_box(
    look: DashboardLook,
    title: MessageDef | MessageRef | Text,
    lines: list[Text],
    *,
    width: int,
    content_height: int | None = None,
) -> list[Text]:
    if isinstance(title, Text):
        rendered_title = title
    else:
        reference = title.bind() if isinstance(title, MessageDef) else title
        rendered_title = look.message(reference)
    return bordered_section(
        rendered_title,
        lines,
        width=width,
        console=look.console,
        border_style=_border_style(look),
        title_style=section_style(look),
        content_height=content_height,
    )


def badged_section_title(look: DashboardLook, title: MessageDef | MessageRef, precision: Precision) -> Text:
    reference = title.bind() if isinstance(title, MessageDef) else title
    return Text.assemble(
        Text(look.message(reference)),
        Text(" "),
        precision_badge(look, precision),
    )


def fit_text_right(value: Text, width: int) -> Text:
    fitted = value.copy()
    fitted.truncate(max(0, width), overflow="ellipsis")
    return Text.assemble(Text(" " * max(0, width - cell_len(fitted.plain))), fitted)


def align_edges(left: Text, right: Text, width: int) -> Text:
    usable = max(0, width)
    fitted_right = right.copy()
    fitted_right.truncate(usable, overflow="ellipsis")
    right_width = cell_len(fitted_right.plain)
    separator_width = int(bool(left.plain and fitted_right.plain and right_width < usable))
    fitted_left = left.copy()
    fitted_left.truncate(max(0, usable - right_width - separator_width), overflow="ellipsis")
    gap = usable - cell_len(fitted_left.plain) - right_width
    return Text.assemble(fitted_left, Text(" " * gap), fitted_right)


def section_row_widths(width: int, count: int) -> tuple[int, ...]:
    gaps = count - 1
    base = max(6, (width - gaps) // count)
    return (*(base,) * (count - 1), max(6, width - base * (count - 1) - gaps))


def callsite(operation_id: str) -> str:
    return operation_id[:8]


def format_duration(value_ns: int | float) -> str:
    seconds = float(value_ns) / 1_000_000_000
    if round(seconds * 1000) < 1000:
        return f"{seconds * 1000:.0f} ms"
    if round(seconds, 2) < 60:
        return f"{seconds:.2f} s"
    # Minutes and up still truncate, so an elapsed time only reaches the next
    # unit once it is whole seconds long; a duration the seconds branch just
    # declined (≥59.995 s) carries into the first whole minute instead.
    minutes, remainder = divmod(max(int(seconds), 60), 60)
    if minutes < 60:
        return f"{minutes}m {remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours:02d}h"
    return f"{hours}h {minutes:02d}m"


def cache_hit_metric(usage: TokenUsage) -> Metric:
    """Derive the cache-hit share of input tokens from the display buckets."""
    cache_read = usage.buckets[UsageBucket.CACHE_READ]
    total_input = usage.buckets[UsageBucket.INPUT]
    if cache_read.value is None:
        return Metric(None, cache_read.precision, cache_read.reason)
    if total_input.value is None:
        return Metric(None, total_input.precision, total_input.reason)
    if float(total_input.value) == 0:
        return Metric(None, Precision.MISSING, total_input.reason)
    if (
        float(total_input.value) < 0
        or float(cache_read.value) < 0
        or float(cache_read.value) > float(total_input.value)
    ):
        return Metric(None, Precision.UNRESOLVED)
    percent = 100 * float(cache_read.value) / float(total_input.value)
    value = min(100, max(1, round(percent))) if cache_read.value else 0
    grade = derived_metric_grade(cache_read, total_input)
    return Metric(value, grade.precision, grade.reason)


def derived_metric_grade(*metrics: Metric) -> Grade:
    """The weakest grade among *metrics*, which a value derived from them inherits."""
    for precision in (Precision.UNRESOLVED, Precision.MISSING, Precision.ESTIMATED):
        for metric in metrics:
            if metric.precision is precision:
                return metric.grade
    return Grade(Precision.EXACT)


def format_tokens(value: int | float | None) -> str:
    """Compact token count sharing the chat status bar's k/m/b units."""
    if value is None:
        return "—"
    return format_token_count(int(value))
