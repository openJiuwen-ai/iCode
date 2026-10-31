# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""UsageDialog — token usage across all sessions: totals, trend and groupings, and a calendar of days."""

from __future__ import annotations

import asyncio
import logging
import threading
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from itertools import pairwise
from math import ceil
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Final

from rich.cells import cell_len, set_cell_size
from rich.style import Style
from rich.text import Text
from textual import on
from textual.containers import Horizontal, Vertical, VerticalGroup, VerticalScroll
from textual.widget import Widget
from textual.widgets import Button, DataTable, Static, Tab, TabbedContent, TabPane, Tabs
from textual.widgets._tabbed_content import ContentTabs

from chrys.app.tui.binding_display import CLOSE_BINDING, localized_binding
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.foundation.i18n import MessageDef, MessageRef, msg
from chrys.foundation.i18n.formatting import sanitize_legacy_scalar
from chrys.foundation.platform.files import surrogate_safe_text
from chrys.foundation.trajectory.envelope import ActorRole
from chrys.service.analytics.reader import TrajectoryScanCancelled
from chrys.service.analytics.usage_report import UsageReport, UsageSummary, UsageTotals, percentile, week_start

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from textual.app import ComposeResult
    from textual.events import Click, Key, Resize

logger = logging.getLogger(__name__)

_TITLE = msg("tui.usage.title", fallback="Token Usage")
_TITLE_READING = msg("tui.usage.title_reading", fallback="Token Usage · reading…")
_TAB_OVERVIEW = msg("tui.usage.tab.overview", fallback="Overview")
_TAB_CALENDAR = msg("tui.usage.tab.calendar", fallback="Calendar")
_KEYS_OVERVIEW = msg("tui.usage.keys.overview", fallback="←→ range  t scale  g group  ↑↓ row")
_KEYS_OVERVIEW_FIXED_SCALE = msg("tui.usage.keys.overview_fixed_scale", fallback="←→ range  g group  ↑↓ row")
_KEYS_CALENDAR = msg("tui.usage.keys.calendar", fallback="←→↑↓ day  {month_keys} month")
_LOADING = msg("tui.usage.loading", fallback="Reading usage from every session…")
_FAILED = msg("tui.usage.failed", fallback="Could not read usage.")
_EMPTY = msg("tui.usage.empty", fallback="No model calls recorded yet.")
_RANGE_TODAY = msg("tui.usage.range.today", fallback="Today")
_RANGE_WEEK = msg("tui.usage.range.week", fallback="This week")
_RANGE_MONTH = msg("tui.usage.range.month", fallback="This month")
_RANGE_30_DAYS = msg("tui.usage.range.last_30_days", fallback="30 days")
_RANGE_ALL = msg("tui.usage.range.all", fallback="All")
_TREND = msg("tui.usage.trend", fallback="Trend")
_TREND_DAILY = msg("tui.usage.trend.daily", fallback="Daily")
_TREND_WEEKLY = msg("tui.usage.trend.weekly", fallback="Weekly")
_TREND_PEAK = msg("tui.usage.trend.peak", fallback="peak {value}")
_GROUP = msg("tui.usage.group", fallback="Group")
_GROUP_MODEL = msg("tui.usage.group.model", fallback="Model")
_GROUP_SOURCE = msg("tui.usage.group.source", fallback="Source")
_GROUP_PROJECT = msg("tui.usage.group.project", fallback="Project")
_COLUMN_NEW = msg("tui.usage.column.new", fallback="New")
_COLUMN_HIT = msg("tui.usage.column.hit", fallback="Hit")
_COLUMN_SPEED = msg("tui.usage.column.speed", fallback="TPS")
_NOTE_UNREPORTED = msg(
    "tui.usage.note.unreported",
    fallback="{count} call unreported",
    plural_fallback="{count} calls unreported",
)
_NOTE_SINCE = msg("tui.usage.note.since", fallback="from {day}")
_NOTE_SCOPE = msg("tui.usage.note.scope", fallback="no deleted sessions/external ACP")
_NOTE_UNREADABLE = msg(
    "tui.usage.note.unreadable",
    fallback="{count} log unreadable",
    plural_fallback="{count} logs unreadable",
)
_UNKNOWN_MODEL = msg("tui.usage.model.unknown", fallback="Unknown model")
_OTHER_WORKSPACES = msg("tui.usage.project.other", fallback="Other workspaces")
_SOURCE_MAIN = msg("tui.usage.source.main", fallback="Main agent")
_SOURCE_SUB_AGENT = msg("tui.usage.source.sub_agent", fallback="Sub-agent")
_SOURCE_WORKFLOW_NODE = msg("tui.usage.source.workflow_node", fallback="Workflow node")
_SOURCE_APPROVAL_JUDGE = msg("tui.usage.source.approval_judge", fallback="Approval judge")
_SOURCE_COMPACTION = msg("tui.usage.source.compaction", fallback="Compaction")
_SOURCE_TITLE_GEN = msg("tui.usage.source.title_gen", fallback="Session titles")
_SOURCE_COMPLETER = msg("tui.usage.source.completer", fallback="Last Words")
_SOURCE_LABELS: Final[dict[str, MessageDef]] = {
    ActorRole.MAIN: _SOURCE_MAIN,
    ActorRole.SUB_AGENT: _SOURCE_SUB_AGENT,
    ActorRole.WORKFLOW_NODE: _SOURCE_WORKFLOW_NODE,
    ActorRole.APPROVAL_JUDGE: _SOURCE_APPROVAL_JUDGE,
    ActorRole.COMPACTION: _SOURCE_COMPACTION,
    ActorRole.TITLE_GEN: _SOURCE_TITLE_GEN,
    ActorRole.COMPLETER: _SOURCE_COMPLETER,
}
_MONTH = msg("tui.usage.calendar.month", fallback="{year}-{month}")
_WEEKDAYS_SHORT = msg("tui.usage.calendar.weekdays_short", fallback="Mo Tu We Th Fr Sa Su")
_WEEKDAYS = msg("tui.usage.calendar.weekdays", fallback="Mon Tue Wed Thu Fri Sat Sun")
_WEEK_TOTAL = msg("tui.usage.calendar.week_total", fallback="Week")
_TOP_MODELS = msg("tui.usage.calendar.top_models", fallback="Top models")
_LABEL_NEW_INPUT = msg("tui.usage.label.new_input", fallback="New input")
_LABEL_OUTPUT = msg("tui.usage.label.output", fallback="Output")
_LABEL_CACHE_READ = msg("tui.usage.label.cache_read", fallback="Cache read")
_LABEL_CALLS = msg("tui.usage.label.calls", fallback="Calls")
_LABEL_FAILED = msg("tui.usage.label.failed", fallback="Failed")
_KPI_INPUT = msg("tui.usage.kpi.input", fallback="Input")
_KPI_CACHED = msg("tui.usage.kpi.cached", fallback="Cached")
_KPI_SPEED = msg("tui.usage.kpi.speed", fallback="TPS")
_KPI_TIME = msg("tui.usage.kpi.time", fallback="Time")

_OVERVIEW_TAB: Final = "usage-overview"
_CALENDAR_TAB: Final = "usage-calendar"
_RANGES: Final = (_RANGE_TODAY, _RANGE_WEEK, _RANGE_MONTH, _RANGE_30_DAYS, _RANGE_ALL)
_RANGE_TAB_IDS: Final = tuple(f"usage-range-{index}" for index in range(len(_RANGES)))
_RANGE_TODAY_INDEX: Final = 0
_RANGE_WEEK_INDEX: Final = 1
_RANGE_MONTH_INDEX: Final = 2
_RANGE_ALL_INDEX: Final = 4
_GROUPS: Final = (_GROUP_MODEL, _GROUP_SOURCE, _GROUP_PROJECT)
_GROUP_BUTTON_IDS: Final = ("usage-group-model", "usage-group-source", "usage-group-project")
_GROUP_MODEL_INDEX: Final = 0
_GROUP_SOURCE_INDEX: Final = 1
_TREND_BUTTON_IDS: Final = ("usage-trend-daily", "usage-trend-weekly")
_KPI_IDS: Final = ("new", "input", "output", "cached", "hit", "speed", "time", "calls")
_BAR_GLYPHS: Final = " ▁▂▃▄▅▆▇█"
_HATCH_GLYPHS: Final = "⢁⠔"
_SHARE_GLYPHS: Final = " ▏▎▍▌▋▊▉█"
_MIN_TREND_DAYS: Final = 14
_MIN_TREND_WEEKS: Final = 8
# Below this many rows the dialog's content gives up the sections' frames and gaps.
_COMPACT_BELOW_ROWS: Final = 30
# Below this many columns the month needs the width of the day panel, which goes.
_DAY_PANEL_MIN_CELLS: Final = 65
# A tile's left border and padding, and the widest number: tiles narrower than this would clip it.
_KPI_MIN_CELLS: Final = 8
_NAME_MIN_CELLS: Final = 18
_SHARE_MIN_CELLS: Final = 6
_SHARE_MAX_CELLS: Final = 24
_WEEK_TOTAL_CELLS: Final = 6
_WEEK_GAP_CELLS: Final = 2
_DAY_COLUMNS_MIN_CELLS: Final = 7 * 2 + 6
_BIG_CELL_MIN_CELLS: Final = 7
_BIG_CELL_MIN_ROWS: Final = 2
_LONG_WEEKDAY_MIN_CELLS: Final = 4
_HEAT_LEVELS: Final = 5
_MODEL_NEW_CELLS: Final = 7
_MODEL_SPEED_CELLS: Final = 6
_DETAIL_DEFAULT_CELLS: Final = 32
_DETAIL_DEFAULT_ROWS: Final = 12


def _tokens(count: int) -> str:
    """A token count short enough for a table column, at most six cells; a count that rounds up to 100M drops its decimals too."""
    if count >= 99_995_000:
        return f"{count / 1_000_000:.0f}M"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.2f}M"
    if count >= 10_000:
        return f"{count / 1_000:.0f}k"
    if count >= 1_000:
        return f"{count / 1_000:.1f}k"
    return str(count)


def _percent(share: float | None) -> str:
    return "-" if share is None else f"{share:.0%}"


def _speed(speed: float | None) -> str:
    return "-" if speed is None else str(round(speed))


def _model_time(milliseconds: int) -> str:
    seconds = milliseconds / 1000
    if seconds < 59.5:
        return f"{seconds:.0f}s"
    if seconds < 3570:
        return f"{seconds / 60:.0f}m"
    if seconds < 86_220:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86_400:.1f}d"


def _clip(text: str, cells: int) -> str:
    if cell_len(text) <= cells:
        return text
    return set_cell_size(text, cells - 1) + "…" if cells > 0 else ""


def _right(text: str, cells: int) -> str:
    return " " * (cells - cell_len(text)) + text


def _center(text: str, cells: int) -> str:
    before = (cells - cell_len(text)) // 2
    return " " * before + text + " " * (cells - cell_len(text) - before)


def _day_label(day: date, today: date) -> str:
    """Month and day; the year too when it is not this one, since a long history spans several."""
    return f"{day:%m-%d}" if day.year == today.year else f"{day:%Y-%m-%d}"


def _range_dates(index: int, report: UsageReport) -> tuple[date, date]:
    today = report.today
    starts = (
        today,
        week_start(today),
        today.replace(day=1),
        today - timedelta(days=29),
        min(report.first_day or today, today),
    )
    return starts[index], today


def _spread(left: Text, right: str, width: int) -> Text:
    """*left* with *right* at the end of a *width*-cell line; a line too short for both keeps *left*."""
    gap = width - left.cell_len - cell_len(right)
    if gap < 1:
        return left
    return Text.assemble(left, " " * gap, right)


@dataclass(frozen=True, slots=True)
class _TrendColumn:
    label: str
    """What the axis says under the bar; empty for none."""
    value: int


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _tokens_on(report: UsageReport, day: date) -> int:
    """A day's new tokens; a day yet to come has none, whatever a skewed clock wrote, as no range's total counts it."""
    return report.new_tokens_by_day.get(day, 0) if day <= report.today else 0


def _hourly_columns(report: UsageReport) -> list[_TrendColumn]:
    """Today's 24 hours, every sixth one named."""
    return [
        _TrendColumn(f"{hour:02}:00" if hour % 6 == 0 else "", report.new_tokens_by_hour.get(hour, 0))
        for hour in range(24)
    ]


def _week_columns(report: UsageReport, monday: date) -> list[_TrendColumn]:
    """The whole week from *monday*, every day named."""
    return [_TrendColumn(f"{day:%m-%d}", _tokens_on(report, day)) for day in _days(monday, monday + timedelta(days=6))]


def _daily_columns(report: UsageReport, first: date, last: date) -> list[_TrendColumn]:
    """Each day from *first* through *last*, Mondays named."""
    return [
        _TrendColumn(f"{day:%m-%d}" if day.weekday() == 0 else "", _tokens_on(report, day))
        for day in _days(first, last)
    ]


def _weekly_columns(report: UsageReport, first: date, last: date) -> list[_TrendColumn]:
    """Each week *first* through *last* touch.

    A week they cut counts only their days and goes by the first of them, so the bars add up to the range's total and
    no date falls outside it.
    """
    columns = []
    monday = week_start(first)
    while monday <= last:
        days = _days(max(monday, first), min(monday + timedelta(days=6), last))
        columns.append(_TrendColumn(f"{days[0]:%m-%d}", sum(_tokens_on(report, day) for day in days)))
        monday += timedelta(weeks=1)
    return columns


def _fitting(columns: Sequence[_TrendColumn], width: int) -> Sequence[_TrendColumn]:
    """The newest columns that *width* cells can show; the empty ones after the last bar go first, losing no bar."""
    if not 0 < width < len(columns):
        return columns
    after_last_bar = next((index for index in range(len(columns), 0, -1) if columns[index - 1].value), len(columns))
    stop = max(after_last_bar, width)
    return columns[stop - width : stop]


def _even_spans(count: int, total: int, gap: int = 0) -> list[tuple[int, int]]:
    """Each of *count* runs' start and size across *total*, *gap* apart; their sizes differ by one at most."""
    free = total - gap * (count - 1)
    edges = [index * free // count for index in range(count + 1)]
    return [(edge + index * gap, after - edge) for index, (edge, after) in enumerate(pairwise(edges))]


def _span_at(spans: Sequence[tuple[int, int]], position: int) -> int | None:
    """The index of the span holding *position*, if one does."""
    return next((index for index, (start, size) in enumerate(spans) if start <= position < start + size), None)


def _bar_spans(count: int, width: int) -> list[tuple[int, int]]:
    """Each of *count* bars' first cell and width, from the left edge to the right.

    Bars with room for gaps are equal, and the gaps take up the difference; bars without it touch.
    """
    if count == 0:
        return []
    if width + 1 >= count * 2:
        bar_cells = (width + 1) // count - 1
        return [(index * (width - bar_cells) // max(1, count - 1), bar_cells) for index in range(count)]
    return _even_spans(count, width)


def _hatch(start: int, cells: int) -> str:
    """*cells* of fine diagonal lines from column *start*: their dots run on across cells, rows and the gaps of a chart."""
    return "".join(_HATCH_GLYPHS[column % len(_HATCH_GLYPHS)] for column in range(start, start + cells))


def _chart(columns: Sequence[_TrendColumn], width: int, rows: int, track: Style) -> Text:
    """Rows of block glyphs across *width* on a linear scale; *track* hatches what a bar leaves of its column."""
    spans = _bar_spans(len(columns), width)
    peak = max((column.value for column in columns), default=0)
    levels = [ceil(column.value / peak * rows * 8) if peak else 0 for column in columns]
    chart = Text()
    for row in reversed(range(rows)):
        position = 0
        for level, (start, cells) in zip(levels, spans, strict=True):
            chart.append(" " * (start - position))
            eighths = max(0, min(8, level - row * 8))
            if eighths:
                chart.append(_BAR_GLYPHS[eighths] * cells)
            else:
                chart.append(_hatch(start, cells), style=track)
            position = start + cells
        chart.append("\n")
    return chart


def _axis(columns: Sequence[_TrendColumn], width: int) -> str:
    """The columns' labels under their bars, each placed only if it clears the one before."""
    line = ""
    for column, (position, _cells) in zip(columns, _bar_spans(len(columns), width), strict=True):
        label = column.label
        if label and (not line or position > len(line)) and position + len(label) <= width:
            line += " " * (position - len(line)) + label
    return line


def _share_bar(share: float, cells: int) -> str:
    """A bar of *cells* at most, to an eighth of a cell; any share above nothing shows."""
    eighths = ceil(max(0.0, min(1.0, share)) * cells * 8)
    full, rest = divmod(eighths, 8)
    return "█" * full + (_SHARE_GLYPHS[rest] if rest else "")


def _ranked(groups: dict[str, UsageTotals]) -> list[tuple[str, UsageTotals]]:
    """Groups that spent tokens, the largest first."""
    rows = [(key, totals) for key, totals in groups.items() if totals.new_tokens or totals.cache_read]
    rows.sort(key=lambda row: (-row[1].new_tokens, -row[1].cache_read, row[0]))
    return rows


def _project_labels(projects: Iterable[str]) -> dict[str, str]:
    """A short name for each folder: its base name, with its parent when two share one, else the whole path."""
    paths = [path for path in set(projects) if path]
    names = [Path(path).name or path for path in paths]
    parents = [Path(path).parent.name for path in paths]
    labels = {}
    for path, name, parent in zip(paths, names, parents, strict=True):
        label = name if names.count(name) == 1 else f"{parent}/{name}"
        same = sum(
            1 for other, other_parent in zip(names, parents, strict=True) if (other, other_parent) == (name, parent)
        )
        labels[path] = path if same > 1 else label
    return labels


def _weekday_names(reference: MessageDef, render: Callable[[MessageRef], str]) -> list[str]:
    """The seven names of a space-separated weekday message, Monday first."""
    names = render(reference.bind()).split()
    return names if len(names) == 7 else reference.fallback.split()


def _heat_thresholds(report: UsageReport) -> tuple[int, int, int]:
    """Quartiles of the days with usage, fixed per snapshot so that a shade means the same in every month."""
    active = [count for count in report.new_tokens_by_day.values() if count > 0]
    return (percentile(active, 0.25) or 0, percentile(active, 0.5) or 0, percentile(active, 0.75) or 0)


def _heat_level(count: int, thresholds: tuple[int, int, int]) -> int:
    """0 for a day without usage, else 1 to 4 by the snapshot's quartiles."""
    if count <= 0:
        return 0
    for level, threshold in enumerate(thresholds, start=1):
        if count <= threshold:
            return level
    return _HEAT_LEVELS - 1


def _shift_month(day: date, months: int) -> date:
    """The same day of the month *months* away, or that month's last when it has no such day."""
    index = day.year * 12 + day.month - 1 + months
    year, month = index // 12, index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _clamp_day(day: date, report: UsageReport) -> date:
    first = (report.first_day or report.today).replace(day=1)
    return max(first, min(day, report.today))


def _month_mondays(day: date) -> list[date]:
    first = day.replace(day=1)
    last = first.replace(day=monthrange(first.year, first.month)[1])
    mondays = []
    monday = week_start(first)
    while monday <= last:
        mondays.append(monday)
        monday += timedelta(weeks=1)
    return mondays


@dataclass(frozen=True, slots=True)
class _RowColumn:
    label: MessageDef
    cells: int
    value: Callable[[UsageTotals], str]
    models_only: bool = False


_ROW_COLUMN_NEW: Final = _RowColumn(_COLUMN_NEW, 8, lambda totals: _tokens(totals.new_tokens))
_ROW_COLUMN_INPUT: Final = _RowColumn(_KPI_INPUT, 8, lambda totals: _tokens(totals.new_input))
_ROW_COLUMN_OUTPUT: Final = _RowColumn(_LABEL_OUTPUT, 8, lambda totals: _tokens(totals.output))
_ROW_COLUMN_HIT: Final = _RowColumn(_COLUMN_HIT, 6, lambda totals: _percent(totals.cache_hit))
_ROW_COLUMN_SPEED: Final = _RowColumn(_COLUMN_SPEED, 12, lambda totals: _speed(totals.speed), models_only=True)
_ROW_COLUMN_CALLS: Final = _RowColumn(_LABEL_CALLS, 7, lambda totals: str(totals.calls))
_ROW_COLUMN_FAILED: Final = _RowColumn(_LABEL_FAILED, 7, lambda totals: str(totals.failed))
_ROW_COLUMNS: Final = (
    _ROW_COLUMN_NEW,
    _ROW_COLUMN_INPUT,
    _ROW_COLUMN_OUTPUT,
    _ROW_COLUMN_HIT,
    _ROW_COLUMN_SPEED,
    _ROW_COLUMN_CALLS,
    _ROW_COLUMN_FAILED,
)
# What a narrow table keeps, the most telling first.
_ROW_COLUMN_PRIORITY: Final = (
    _ROW_COLUMN_NEW,
    _ROW_COLUMN_HIT,
    _ROW_COLUMN_SPEED,
    _ROW_COLUMN_OUTPUT,
    _ROW_COLUMN_INPUT,
    _ROW_COLUMN_CALLS,
    _ROW_COLUMN_FAILED,
)


def _fit_row_columns(width: int, *, models: bool) -> tuple[list[_RowColumn], int, int]:
    """The number columns that fit beside a readable name, then the name's cells and the share bar's (0 for none)."""
    spare = width - _NAME_MIN_CELLS
    kept = []
    for column in _ROW_COLUMN_PRIORITY:
        if (models or not column.models_only) and column.cells <= spare:
            kept.append(column)
            spare -= column.cells
    share_cells = min(spare // 2, _SHARE_MAX_CELLS)
    if share_cells < _SHARE_MIN_CELLS:
        share_cells = 0
    return (
        [column for column in _ROW_COLUMNS if column in kept],
        _NAME_MIN_CELLS + spare - share_cells,
        share_cells,
    )


@dataclass(frozen=True, slots=True)
class _GridShape:
    """How a month fills the space it has: where each weekday's column and each week's rows start, and their sizes."""

    days: list[tuple[int, int]]
    weeks: list[tuple[int, int]]
    week_cells: int

    @property
    def big(self) -> bool:
        """Cells that hold a day's tokens under its date."""
        return (
            min(cells for _start, cells in self.days) >= _BIG_CELL_MIN_CELLS
            and min(rows for _start, rows in self.weeks) >= _BIG_CELL_MIN_ROWS
        )


def _grid_shape(width: int, height: int, weeks: int) -> _GridShape:
    """Days and weeks up to every edge, with a gap row between weeks once there is room for one.

    Weeks of one row stay that way rather than some growing to two, which would read as a difference.
    """
    week_cells = _WEEK_TOTAL_CELLS if width >= 7 * 3 + 6 + _WEEK_GAP_CELLS + _WEEK_TOTAL_CELLS else 0
    day_cells = width - (_WEEK_GAP_CELLS + week_cells if week_cells else 0)
    rows = height - 1
    gap = 1 if rows >= 2 * weeks - 1 else 0
    tall = (rows - gap * (weeks - 1)) // weeks >= _BIG_CELL_MIN_ROWS
    return _GridShape(
        _even_spans(7, max(_DAY_COLUMNS_MIN_CELLS, day_cells), 1),
        _even_spans(weeks, rows, gap) if tall else [(index * (1 + gap), 1) for index in range(weeks)],
        week_cells,
    )


class _UsageScroll(VerticalScroll, can_focus=False):
    """Scrolls by wheel on a terminal too short for the pane; the arrow keys stay with the dialog."""


class _UsageFrame(VerticalGroup):
    """The dialog's frame, which reports its size: the compact layout goes by it."""

    def __init__(self, on_resized: Callable[[], None], *, id: str) -> None:
        self._on_resized = on_resized
        super().__init__(id=id)

    def on_resize(self, _event: Resize) -> None:
        self._on_resized()


class _ChoiceButton(Button, can_focus=False):
    """A flat button for a choice in the dialog; every key stays with the dialog, so it takes no focus."""

    def __init__(self, label: str, choose: Callable[[], None], *, id: str) -> None:
        super().__init__(Text(label), id=id, classes="usage-choice", flat=True)
        self.choose = choose


class _RangeTabs(Tabs, can_focus=False):
    """The date ranges; the arrow keys stay with the dialog, which moves them from there."""


class _PeakLine(Static):
    """The line beside the trend's buttons: what fits changes as they come and go, with no other size changing."""

    def __init__(self, on_resized: Callable[[], None], *, id: str, classes: str) -> None:
        super().__init__(id=id, classes=classes)
        self._on_resized = on_resized

    def on_resize(self, _event: Resize) -> None:
        self._on_resized()


class _TrendChart(Widget):
    """Bars across the widget's whole width and height, with their labels on the last row."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"usage-chart--axis", "usage-chart--track"}

    def __init__(self, on_resized: Callable[[], None], *, id: str) -> None:
        super().__init__(id=id)
        self._on_resized = on_resized
        self._columns: Sequence[_TrendColumn] = ()

    def show(self, columns: Sequence[_TrendColumn]) -> None:
        self._columns = columns
        self.refresh()

    def on_resize(self, _event: Resize) -> None:
        self._on_resized()

    def render(self) -> Text:
        width, height = self.size
        if width <= 0 or height <= 0:
            return Text()
        shown = _fitting(self._columns, width)
        chart = _chart(shown, width, max(1, height - 1), self.get_component_rich_style("usage-chart--track"))
        chart.append(_axis(shown, width), style=self.get_component_rich_style("usage-chart--axis"))
        return chart


class _UsageRows(DataTable[Text], can_focus=False):
    """One row per group. The dialog moves the cursor from its own keys; a click or the wheel works here."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {"usage-rows--share"}

    def __init__(self, on_resized: Callable[[], None], *, id: str) -> None:
        super().__init__(id=id, cursor_type="row", cell_padding=0, cursor_foreground_priority="renderable")
        self._on_resized = on_resized

    def on_resize(self, _event: Resize) -> None:
        self._on_resized()

    @property
    def share_style(self) -> Style:
        full = self.get_component_rich_style("usage-rows--share")
        return full.without_color + Style.from_color(full.color)


class _MonthGrid(Widget):
    """A month of days shaded by tokens: a big view with each day's tokens when there is room, else dates alone."""

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        *(f"usage-grid--heat-{level}" for level in range(_HEAT_LEVELS)),
        "usage-grid--selected",
        "usage-grid--outside",
        "usage-grid--head",
    }

    def __init__(self, on_resized: Callable[[], None], pick: Callable[[date], None], *, id: str) -> None:
        super().__init__(id=id)
        self._on_resized = on_resized
        self._pick = pick
        self._selected: date | None = None
        self._today = date.min
        self._tokens: Mapping[date, int] = {}
        self._thresholds = (0, 0, 0)
        self._weekday_names: tuple[list[str], list[str]] = ([], [])
        self._week_label = ""

    def show(
        self,
        selected: date,
        report: UsageReport,
        thresholds: tuple[int, int, int],
        *,
        weekdays: tuple[list[str], list[str]],
        week_label: str,
    ) -> None:
        self._selected = selected
        self._today = report.today
        self._tokens = report.new_tokens_by_day
        self._thresholds = thresholds
        self._weekday_names = weekdays
        self._week_label = week_label
        self.refresh()

    def on_resize(self, _event: Resize) -> None:
        self._on_resized()

    def heat_style(self, level: int) -> Style:
        return self.get_component_rich_style(f"usage-grid--heat-{level}")

    def _cell_style(self, day: date, month: int) -> Style:
        if day == self._selected:
            return self.get_component_rich_style("usage-grid--selected")
        if day.month != month or day > self._today:
            return self.get_component_rich_style("usage-grid--outside")
        return self.heat_style(_heat_level(self._tokens.get(day, 0), self._thresholds))

    def _append_cell(self, line: Text, day: date, month: int, row: int, size: tuple[int, int], *, big: bool) -> None:
        style = self._cell_style(day, month)
        cells, cell_rows = size
        if row == 0:
            label = str(day.day)
            before = 1 if big else (cells - len(label)) // 2
            line.append(" " * before, style)
            line.append(label, style + Style(underline=True) if day == self._today else style)
            line.append(" " * (cells - before - len(label)), style)
            return
        count = self._tokens.get(day, 0) if day.month == month and day <= self._today else 0
        if big and row == cell_rows - 1 and count > 0:
            line.append(_right(_tokens(count) + " ", cells), style)
            return
        line.append(" " * cells, style)

    def render(self) -> Text:
        width, height = self.size
        selected = self._selected
        if selected is None or width <= 0 or height <= 0:
            return Text()
        mondays = _month_mondays(selected)
        shape = _grid_shape(width, height, len(mondays))
        big = shape.big
        head_style = self.get_component_rich_style("usage-grid--head")
        short_names, long_names = self._weekday_names
        long_fits = all(cells >= _LONG_WEEKDAY_MIN_CELLS for _start, cells in shape.days)
        names = long_names if long_fits else short_names
        grid = Text(no_wrap=True)
        grid.append(
            " ".join(_center(name, cells) for name, (_start, cells) in zip(names, shape.days, strict=True)), head_style
        )
        if shape.week_cells:
            grid.append(" " * _WEEK_GAP_CELLS + _right(self._week_label, shape.week_cells), head_style)
        next_row = 0
        for monday, (first_row, cell_rows) in zip(mondays, shape.weeks, strict=True):
            days = [monday + timedelta(days=offset) for offset in range(7)]
            grid.append("\n" * (first_row - next_row))
            next_row = first_row + cell_rows
            for row in range(cell_rows):
                grid.append("\n")
                for index, (day, (_start, cells)) in enumerate(zip(days, shape.days, strict=True)):
                    if index:
                        grid.append(" ")
                    self._append_cell(grid, day, selected.month, row, (cells, cell_rows), big=big)
                if shape.week_cells and row == 0:
                    total = sum(self._tokens.get(day, 0) for day in days)
                    grid.append(" " * _WEEK_GAP_CELLS + _right(_tokens(total), shape.week_cells), head_style)
        return grid

    def _day_at(self, x: int, y: int) -> date | None:
        selected = self._selected
        if selected is None:
            return None
        width, height = self.size
        mondays = _month_mondays(selected)
        shape = _grid_shape(width, height, len(mondays))
        week = _span_at(shape.weeks, y - 1)
        column = _span_at(shape.days, x)
        if week is None or column is None:
            return None
        return mondays[week] + timedelta(days=column)

    def on_click(self, event: Click) -> None:
        offset = event.get_content_offset(self)
        day = None if offset is None else self._day_at(offset.x, offset.y)
        if day is not None:
            event.stop()
            self._pick(day)


class UsageDialog(BaseDialog[None]):
    """Modal dialog for the token usage recorded by every session."""

    CSS_PATH = "usage.tcss"
    # Nothing here takes focus, so every key reaches on_key whichever tab is showing.
    AUTO_FOCUS = ""

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
    ]

    def __init__(self, load_report: Callable[[threading.Event], UsageReport]) -> None:
        """*load_report* blocks while it reads, and stops with ``TrajectoryScanCancelled`` once its event is set."""
        super().__init__()
        self._load_report = load_report
        self._cancel_load: threading.Event | None = None
        self._reading = True
        self._read_failed = False
        self._report: UsageReport | None = None
        self._summaries: dict[tuple[date, date], UsageSummary] = {}
        self._project_names: dict[str, str] = {}
        self._heat: tuple[int, int, int] = (0, 0, 0)
        self._range = _RANGE_MONTH_INDEX
        self._weekly_trend: bool | None = None
        """``None`` follows the range: weekly for the whole history, daily otherwise."""
        self._group = _GROUP_MODEL_INDEX
        self._rows_shown: tuple[int, int, int] | None = None
        """The table's range, group and width: it is rebuilt only for a new one."""
        self._day: date | None = None

    def compose(self) -> ComposeResult:
        render = self._render_message
        with _UsageFrame(self._render_active, id="usage-container") as frame:
            frame.border_title = Text(render(_TITLE.bind()))
            frame.border_subtitle = Text(render(_KEYS_OVERVIEW.bind()))
            yield Static(id="usage-status")
            with TabbedContent(id="usage-tabs", initial=_OVERVIEW_TAB):
                with (
                    TabPane(render(_TAB_OVERVIEW.bind()), id=_OVERVIEW_TAB),
                    _UsageScroll(classes="usage-pane", id="usage-overview-pane"),
                ):
                    yield from self._compose_overview()
                with (
                    TabPane(render(_TAB_CALENDAR.bind()), id=_CALENDAR_TAB),
                    _UsageScroll(classes="usage-pane", id="usage-calendar-pane"),
                ):
                    yield from self._compose_calendar()

    def _compose_overview(self) -> ComposeResult:
        render = self._render_message
        yield _RangeTabs(
            *(Tab(render(label.bind()), id=tab_id) for label, tab_id in zip(_RANGES, _RANGE_TAB_IDS, strict=True)),
            active=_RANGE_TAB_IDS[self._range],
            id="usage-ranges",
        )
        with Horizontal(id="usage-kpis"):
            for kpi in _KPI_IDS:
                yield Static(id=f"usage-kpi-{kpi}", classes="usage-kpi")
        with Vertical(id="usage-trend", classes="usage-section") as trend:
            trend.border_title = Text(render(_TREND.bind()))
            with Horizontal(classes="usage-controls"):
                yield Static(Text(render(_TREND.bind())), classes="usage-section-label")
                yield _ChoiceButton(
                    render(_TREND_DAILY.bind()), lambda: self._set_weekly_trend(False), id=_TREND_BUTTON_IDS[0]
                )
                yield _ChoiceButton(
                    render(_TREND_WEEKLY.bind()), lambda: self._set_weekly_trend(True), id=_TREND_BUTTON_IDS[1]
                )
                yield _PeakLine(self._render_active, id="usage-peak", classes="usage-controls-end")
            yield _TrendChart(self._render_active, id="usage-chart")
        with Vertical(id="usage-groups", classes="usage-section") as groups:
            groups.border_title = Text(render(_GROUP.bind()))
            with Horizontal(classes="usage-controls"):
                yield Static(Text(render(_GROUP.bind())), classes="usage-section-label")
                for index, (label, button_id) in enumerate(zip(_GROUPS, _GROUP_BUTTON_IDS, strict=True)):
                    yield _ChoiceButton(render(label.bind()), partial(self._set_group, index), id=button_id)
            yield _UsageRows(self._render_active, id="usage-rows")
        yield Static(id="usage-note", classes="usage-note")

    def _compose_calendar(self) -> ComposeResult:
        with Horizontal(id="usage-calendar"):
            with Vertical(id="usage-calendar-left"):
                with Horizontal(id="usage-month-bar", classes="usage-controls"):
                    yield _ChoiceButton("←", partial(self._shift_selected_month, -1), id="usage-month-previous")
                    yield Static(id="usage-month")
                    yield _ChoiceButton("→", partial(self._shift_selected_month, 1), id="usage-month-next")
                yield _MonthGrid(self._render_active, self._pick_day, id="usage-calendar-grid")
                yield Static(id="usage-heat-legend")
            with Vertical(id="usage-day", classes="usage-section"):
                yield Static(id="usage-calendar-detail")

    def on_mount(self) -> None:
        if not self._composed_live:
            return
        # The tab bar would take the arrow keys when clicked; a click still switches tabs.
        self.query_one(ContentTabs).can_focus = False
        self.call_after_refresh(self._start_read)

    def on_unmount(self) -> None:
        self._stop_read()

    def _before_dismiss(self, _result: object | None = None) -> None:
        self._stop_read()

    def action_close(self) -> None:
        """Close the dialog."""
        self.dismiss(None)

    def _render_message(self, reference: MessageRef) -> str:
        return render_str(widget_localizer(self), reference)

    def _stop_read(self) -> None:
        if self._cancel_load is not None:
            self._cancel_load.set()

    def _start_read(self) -> None:
        """Whole-history I/O waits for the loading shell to paint, unless the dialog has closed by then."""
        if not (self._composed_live and self.is_mounted):
            return
        cancel = threading.Event()
        self._cancel_load = cancel
        self._render_active()
        self.run_worker(self._read(cancel), exit_on_error=False)

    async def _read(self, cancel: threading.Event) -> None:
        report: UsageReport | None = None
        try:
            report = await asyncio.to_thread(self._load_report, cancel)
        except TrajectoryScanCancelled:
            return
        except Exception:
            logger.exception("Failed to read the usage report")
        if not self._composed_live:
            return
        self._reading = False
        if report is None:
            self._read_failed = True
        else:
            self._adopt(report)
        self._render_active()

    def _adopt(self, report: UsageReport) -> None:
        self._report = report
        self._project_names = _project_labels(call.project for call in report.calls)
        self._heat = _heat_thresholds(report)
        self._day = _clamp_day(report.today, report)

    def _summary(self, start: date, end: date) -> UsageSummary:
        summary = self._summaries.get((start, end))
        if summary is None or self._report is None:
            summary = self._summaries[(start, end)] = (self._report or UsageReport((), end)).summarize(start, end)
        return summary

    @property
    def _on_calendar(self) -> bool:
        return self.query_one("#usage-tabs", TabbedContent).active == _CALENDAR_TAB

    @property
    def _compact(self) -> bool:
        return self.query_one("#usage-container").has_class("-compact")

    def on_key(self, event: Key) -> None:
        key = event.key
        if key == "tab":
            self.query_one("#usage-tabs", TabbedContent).active = _OVERVIEW_TAB if self._on_calendar else _CALENDAR_TAB
        elif self._report is None or not (
            self._on_calendar_key(key) if self._on_calendar else self._on_overview_key(key)
        ):
            return
        event.prevent_default()
        event.stop()

    def _on_overview_key(self, key: str) -> bool:
        ranges = self.query_one(_RangeTabs)
        rows = self.query_one(_UsageRows)
        moves: dict[str, Callable[[], None]] = {
            "left": ranges.action_previous_tab,
            "right": ranges.action_next_tab,
            "up": rows.action_cursor_up,
            "down": rows.action_cursor_down,
            "pageup": rows.action_page_up,
            "pagedown": rows.action_page_down,
            "t": lambda: self._set_weekly_trend(not self._is_weekly_trend),
            "g": lambda: self._set_group((self._group + 1) % len(_GROUPS)),
        }
        move = moves.get(key)
        if move is None:
            return False
        move()
        return True

    def _on_calendar_key(self, key: str) -> bool:
        day = self._day
        if day is None:
            return False
        moves = {"left": -1, "right": 1, "up": -7, "down": 7}
        if key in moves:
            self._pick_day(day + timedelta(days=moves[key]))
        elif key in ("left_square_bracket", "right_square_bracket"):
            self._shift_selected_month(1 if key == "right_square_bracket" else -1)
        else:
            return False
        return True

    @on(Tabs.TabActivated, "#usage-ranges")
    def _range_activated(self, event: Tabs.TabActivated) -> None:
        event.stop()
        index = _RANGE_TAB_IDS.index(event.tabs.active)
        if index != self._range:
            self._range = index
            self._weekly_trend = None
            self._render_active()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if isinstance(event.button, _ChoiceButton):
            event.button.choose()

    def _set_weekly_trend(self, weekly: bool) -> None:
        self._weekly_trend = weekly
        self._render_active()

    def _set_group(self, index: int) -> None:
        self._group = index
        self._render_active()

    def _pick_day(self, day: date) -> None:
        if self._report is not None:
            self._day = _clamp_day(day, self._report)
            self._render_active()

    def _shift_selected_month(self, months: int) -> None:
        if self._day is not None:
            self._pick_day(_shift_month(self._day, months))

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        event.stop()
        self._render_active()

    def _render_active(self) -> None:
        if not self._composed_live or not self.is_mounted:
            return
        frame = self.query_one("#usage-container")
        frame.border_title = Text(self._render_message(self._title()))
        frame.border_subtitle = Text(self._render_message(self._keys()))
        if frame.content_size.height > 0:
            frame.set_class(frame.content_size.height < _COMPACT_BELOW_ROWS, "-compact")
            frame.set_class(frame.content_size.width < _DAY_PANEL_MIN_CELLS, "-narrow")
        report = self._report
        has_numbers = report is not None and bool(report.calls)
        status = self.query_one("#usage-status")
        status.display = not has_numbers
        if report is None or not has_numbers:
            self._set("usage-status", self._status_text())
        elif self._on_calendar:
            self._render_calendar(report)
        else:
            self._render_overview(report)

    def _title(self) -> MessageRef:
        return _TITLE_READING.bind() if self._reading else _TITLE.bind()

    def _keys(self) -> MessageRef:
        if self._on_calendar:
            return _KEYS_CALENDAR.bind(month_keys="[ ]")
        return _KEYS_OVERVIEW.bind() if self._has_scale_choice else _KEYS_OVERVIEW_FIXED_SCALE.bind()

    def _set(self, static_id: str, content: Text | str) -> None:
        self.query_one(f"#{static_id}", Static).update(
            content if isinstance(content, Text) else Text(content),
            layout=False,
        )

    def _status_text(self) -> str:
        if self._read_failed:
            return self._render_message(_FAILED.bind())
        if self._reading:
            return self._render_message(_LOADING.bind())
        status = self._render_message(_EMPTY.bind())
        unreadable = self._report.unreadable_logs if self._report is not None else 0
        return f"{status} · {self._render_message(_NOTE_UNREADABLE.bind(count=unreadable))}" if unreadable else status

    def _select_choice(self, button_ids: Sequence[str], selected: int) -> None:
        for index, button_id in enumerate(button_ids):
            self.query_one(f"#{button_id}", _ChoiceButton).set_class(index == selected, "-selected")

    def _render_overview(self, report: UsageReport) -> None:
        start, end = _range_dates(self._range, report)
        summary = self._summary(start, end)
        self._render_kpis(summary.total)
        self._render_trend(report, start, end)
        self._render_groups(summary)
        self._set("usage-note", " · ".join(self._note_parts(summary.total, report)))

    def _render_kpis(self, total: UsageTotals) -> None:
        tiles = (
            (_COLUMN_NEW, _tokens(total.new_tokens)),
            (_KPI_INPUT, _tokens(total.new_input)),
            (_LABEL_OUTPUT, _tokens(total.output)),
            (_KPI_CACHED, _tokens(total.cache_read)),
            (_COLUMN_HIT, _percent(total.cache_hit)),
            (_KPI_SPEED, _speed(total.speed)),
            (_KPI_TIME, _model_time(total.duration_ms)),
            (_LABEL_CALLS, str(total.calls)),
        )
        fitting = self.query_one("#usage-kpis").size.width // _KPI_MIN_CELLS or len(tiles)
        for index, (kpi, (label, value)) in enumerate(zip(_KPI_IDS, tiles, strict=True)):
            self.query_one(f"#usage-kpi-{kpi}").display = index < fitting
            self._set(
                f"usage-kpi-{kpi}",
                Text.assemble((self._render_message(label.bind()), "dim"), "\n", (value, "bold")),
            )

    @property
    def _has_scale_choice(self) -> bool:
        """Today goes by the hour and a week by the day; only a longer range can be read by either scale."""
        return self._range not in (_RANGE_TODAY_INDEX, _RANGE_WEEK_INDEX)

    @property
    def _is_weekly_trend(self) -> bool:
        return self._range == _RANGE_ALL_INDEX if self._weekly_trend is None else self._weekly_trend

    def _trend_columns(self, report: UsageReport, start: date, end: date) -> list[_TrendColumn]:
        if self._range == _RANGE_TODAY_INDEX:
            return _hourly_columns(report)
        if self._range == _RANGE_WEEK_INDEX:
            return _week_columns(report, start)
        weekly = self._is_weekly_trend
        first, last = start, end
        if self._range == _RANGE_MONTH_INDEX:
            # All of the month, so one just begun is not a single bar across the chart.
            last = start.replace(day=monthrange(start.year, start.month)[1])
        elif self._range == _RANGE_ALL_INDEX:
            # A short history still gets a chart's worth of columns, empty before it began.
            shortest = (
                week_start(end) - timedelta(weeks=_MIN_TREND_WEEKS - 1)
                if weekly
                else end - timedelta(days=_MIN_TREND_DAYS - 1)
            )
            first = min(start, shortest)
        return _weekly_columns(report, first, last) if weekly else _daily_columns(report, first, last)

    def _render_trend(self, report: UsageReport, start: date, end: date) -> None:
        render = self._render_message
        columns = self._trend_columns(report, start, end)
        chart = self.query_one(_TrendChart)
        chart.show(columns)
        for button_id in _TREND_BUTTON_IDS:
            self.query_one(f"#{button_id}", _ChoiceButton).display = self._has_scale_choice
        self._select_choice(_TREND_BUTTON_IDS, 1 if self._is_weekly_trend else 0)
        peak = max((column.value for column in _fitting(columns, chart.size.width)), default=0)
        value = _tokens(peak)
        peak_text = render(_TREND_PEAK.bind(value=value))
        width = self.query_one("#usage-peak").size.width
        # A clipped number reads as a smaller one, so only a whole one is shown.
        self._set("usage-peak", next((choice for choice in (peak_text, value) if cell_len(choice) <= width), ""))

    def _group_label(self, key: str) -> str:
        if self._group == _GROUP_MODEL_INDEX:
            return sanitize_legacy_scalar(key) or self._render_message(_UNKNOWN_MODEL.bind())
        if self._group == _GROUP_SOURCE_INDEX:
            label = _SOURCE_LABELS.get(key)
            return self._render_message(label.bind()) if label is not None else sanitize_legacy_scalar(key)
        name = self._project_names.get(key, "")
        return sanitize_legacy_scalar(surrogate_safe_text(name)) or self._render_message(_OTHER_WORKSPACES.bind())

    def _render_groups(self, summary: UsageSummary) -> None:
        self._select_choice(_GROUP_BUTTON_IDS, self._group)
        table = self.query_one(_UsageRows)
        # One cell stays free for the scrollbar a long list brings.
        width = table.size.width - 1
        shown = (self._range, self._group, width)
        previous = self._rows_shown
        if width <= 0 or shown == previous:
            return
        self._rows_shown = shown
        row = table.cursor_row if previous is not None and previous[:2] == shown[:2] else 0
        columns, name_cells, share_cells = _fit_row_columns(width, models=self._group == _GROUP_MODEL_INDEX)
        render = self._render_message
        table.clear(columns=True)
        table.add_column(Text(render(_GROUPS[self._group].bind())), width=name_cells)
        if share_cells:
            table.add_column(Text(""), width=share_cells)
        for column in columns:
            table.add_column(Text(render(column.label.bind()), justify="right"), width=column.cells)
        groups = (summary.models, summary.sources, summary.projects)[self._group]
        share_style = table.share_style
        for key, totals in _ranked(groups):
            cells = [Text(_clip(self._group_label(key), name_cells - 1))]
            if share_cells:
                share = totals.new_tokens / summary.total.new_tokens if summary.total.new_tokens else 0.0
                cells.append(Text(_share_bar(share, share_cells - 1), style=share_style))
            cells.extend(Text(column.value(totals), justify="right") for column in columns)
            table.add_row(*cells)
        table.move_cursor(row=row, animate=False)

    def _note_parts(self, total: UsageTotals, report: UsageReport) -> list[str]:
        render = self._render_message
        parts = []
        if report.unreadable_logs:
            parts.append(render(_NOTE_UNREADABLE.bind(count=report.unreadable_logs)))
        if total.unreported:
            parts.append(render(_NOTE_UNREPORTED.bind(count=total.unreported)))
        if report.first_day is not None:
            parts.append(render(_NOTE_SINCE.bind(day=_day_label(report.first_day, report.today))))
        parts.append(render(_NOTE_SCOPE.bind()))
        return parts

    def _render_calendar(self, report: UsageReport) -> None:
        render = self._render_message
        day = self._day or report.today
        self._set("usage-month", render(_MONTH.bind(year=str(day.year), month=f"{day.month:02d}")))
        first_month = (report.first_day or report.today).replace(day=1)
        self.query_one("#usage-month-previous", _ChoiceButton).disabled = day.replace(day=1) <= first_month
        self.query_one("#usage-month-next", _ChoiceButton).disabled = day.replace(day=1) >= report.today.replace(day=1)
        grid = self.query_one(_MonthGrid)
        grid.show(
            day,
            report,
            self._heat,
            weekdays=(_weekday_names(_WEEKDAYS_SHORT, render), _weekday_names(_WEEKDAYS, render)),
            week_label=render(_WEEK_TOTAL.bind()),
        )
        self._set("usage-heat-legend", self._heat_legend(grid))
        title = f"{day:%Y-%m-%d} {_weekday_names(_WEEKDAYS, render)[day.weekday()]}"
        self.query_one("#usage-day").border_title = Text(title)
        self._set("usage-calendar-detail", self._calendar_detail(day, title, report))

    def _heat_legend(self, grid: _MonthGrid) -> Text:
        """What each shade means, on one line: too narrow for every label, it names the scale's two ends."""
        low, middle, high = self._heat
        labels = ("0", f"≤{_tokens(low)}", f"≤{_tokens(middle)}", f"≤{_tokens(high)}", f">{_tokens(high)}")
        chips = [("  ", grid.heat_style(level)) for level in range(_HEAT_LEVELS)]
        legend = Text("  ").join(Text.assemble(chip, f" {label}") for chip, label in zip(chips, labels, strict=True))
        if legend.cell_len <= (grid.size.width or legend.cell_len):
            return legend
        return Text.assemble(f"{labels[0]} ", *chips, f" {labels[-1]}")

    def _calendar_detail(self, day: date, title: str, report: UsageReport) -> Text:
        render = self._render_message
        summary = self._summary(day, day)
        total = summary.total
        detail = self.query_one("#usage-calendar-detail")
        width = detail.size.width or _DETAIL_DEFAULT_CELLS
        rows = detail.size.height or _DETAIL_DEFAULT_ROWS
        lines = [Text(title, style="bold")] if self._compact else []
        new_line = _spread(Text(render(_COLUMN_NEW.bind())), _tokens(total.new_tokens), width)
        new_line.stylize("bold")
        lines.append(new_line)
        pairs = (
            (_LABEL_NEW_INPUT, _tokens(total.new_input)),
            (_LABEL_OUTPUT, _tokens(total.output)),
            (_LABEL_CACHE_READ, _tokens(total.cache_read)),
            (_COLUMN_HIT, _percent(total.cache_hit)),
            (_KPI_SPEED, _speed(total.speed)),
            (_LABEL_CALLS, str(total.calls)),
            (_LABEL_FAILED, str(total.failed)),
        )
        lines.extend(_spread(Text(render(label.bind())), value, width) for label, value in pairs)
        notes = self._note_parts(total, report)
        models = _ranked(summary.models)
        # A blank line and a heading above the models, and a blank line above the note.
        room = rows - len(lines) - len(notes) - 3
        if models and room > 0:
            numbers = _right(render(_COLUMN_NEW.bind()), _MODEL_NEW_CELLS) + _right(
                render(_COLUMN_SPEED.bind()), _MODEL_SPEED_CELLS
            )
            lines += [Text(), _spread(Text(render(_TOP_MODELS.bind()), style="bold"), numbers, width)]
            name_cells = max(1, width - _MODEL_NEW_CELLS - _MODEL_SPEED_CELLS)
            for key, totals in models[:room]:
                name = _clip(sanitize_legacy_scalar(key) or render(_UNKNOWN_MODEL.bind()), name_cells - 1)
                lines.append(
                    _spread(
                        Text(name),
                        _right(_tokens(totals.new_tokens), _MODEL_NEW_CELLS)
                        + _right(_speed(totals.speed), _MODEL_SPEED_CELLS),
                        width,
                    )
                )
        fitting_notes = notes[: max(0, rows - len(lines) - 1)]
        if fitting_notes:
            lines += [Text()] * (rows - len(lines) - len(fitting_notes))
            lines += [Text(_clip(note, width), style="dim") for note in fitting_notes]
        return Text("\n").join(lines)
