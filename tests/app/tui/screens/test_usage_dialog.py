# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The /usage dialog: its tabs and keys, what each view shows, and what opening it costs the main screen."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import date, timedelta
from itertools import pairwise
from typing import TYPE_CHECKING, Any

import pytest
from rich.cells import cell_len
from rich.style import Style
from textual.app import App, ComposeResult
from textual.widgets import Button, Static, Tab, TabbedContent, TextArea
from textual.widgets._tabbed_content import ContentTabs

from chrys.app.tui.app import ChrysApp
from chrys.app.tui.i18n import LocaleController, LocaleSwitchStatus
from chrys.app.tui.screens.dialogs.usage import (
    UsageDialog,
    _bar_spans,
    _ChoiceButton,
    _fit_row_columns,
    _fitting,
    _grid_shape,
    _heat_level,
    _model_time,
    _MonthGrid,
    _project_labels,
    _RangeTabs,
    _share_bar,
    _tokens,
    _TrendChart,
    _TrendColumn,
    _UsageRows,
    _week_columns,
)
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.service.analytics import usage_report
from chrys.service.analytics.reader import TrajectoryScanCancelled
from chrys.service.analytics.usage_report import UsageCall, UsageReport
from chrys.service.state.store import JsonFileStateStore
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import ShutdownOnlyEngine
from tests.support.tui_helpers import click_when_settled, resize_when_settled, rich_plain, rich_segment_lines
from tests.support.waiting import wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from textual.pilot import Pilot
    from textual.widget import Widget

_TODAY = date(2026, 10, 28)  # A Wednesday.
_OVERVIEW_KEYS = "←→ range  t scale  g group  ↑↓ row"
_HATCH = "⢁⠔"
_CALENDAR_KEYS = "←→↑↓ day  [ ] month"
_READING = "Reading usage from every session…"
_FAILED = "Could not read usage."
_EMPTY = "No model calls recorded yet."


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield Static("host")


def _call(day: date = _TODAY, **overrides: Any) -> UsageCall:
    values: dict[str, Any] = {
        "day": day,
        "hour": 12,
        "model": "model-a",
        "source": "main",
        "project": "/w/alpha",
        "new_input": 1000,
        "cache_read": 3000,
        "output": 200,
        "duration_ms": 3000,
        "first_chunk_ms": 1000,
        "reported": True,
        "failed": False,
    }
    return UsageCall(**{**values, **overrides})


def _report(*calls: UsageCall, today: date = _TODAY, unreadable: int = 0) -> UsageReport:
    by_day: dict[date, int] = {}
    by_hour: dict[int, int] = {}
    for call in calls:
        by_day[call.day] = by_day.get(call.day, 0) + call.new_input + call.output
        if call.day == today:
            by_hour[call.hour] = by_hour.get(call.hour, 0) + call.new_input + call.output
    return UsageReport(
        calls=calls,
        today=today,
        unreadable_logs=unreadable,
        new_tokens_by_day=by_day,
        new_tokens_by_hour=by_hour,
    )


def _sample_report() -> UsageReport:
    return _report(
        _call(),
        _call(_TODAY - timedelta(days=1), model="model-b", source="sub_agent", project="/w/beta", new_input=9000),
        _call(_TODAY - timedelta(days=40), new_input=50_000, source="title_gen"),
    )


def _text(dialog: UsageDialog, static_id: str) -> str:
    return rich_plain(dialog.query_one(f"#{static_id}", Static).render())


def _subtitle(dialog: UsageDialog) -> str:
    return str(dialog.query_one("#usage-container").border_subtitle)


def _title(dialog: UsageDialog) -> str:
    return str(dialog.query_one("#usage-container").border_title)


def _section_title(dialog: UsageDialog, section_id: str) -> str:
    return str(dialog.query_one(f"#{section_id}").border_title)


def _painted(pilot: Pilot) -> str:
    """What the terminal actually shows: a widget too short for its text shows only part of it."""
    return "\n".join(strip.text for strip in pilot.app.screen._compositor.render_strips())


def _painted_within(pilot: Pilot, widget: Widget) -> str:
    """What a one-row widget shows: unlike the whole screen, no other widget's copy of the text can answer for it."""
    region = widget.region
    return pilot.app.screen._compositor.render_strips()[region.y].crop(region.x, region.right).text.strip()


def _active_tab(dialog: UsageDialog) -> str:
    return dialog.query_one("#usage-tabs", TabbedContent).active


def _active_range(dialog: UsageDialog) -> str:
    active = dialog.query_one(_RangeTabs).active_tab
    return "" if active is None else active.label.plain


def _chosen(dialog: UsageDialog, section_id: str) -> str:
    return dialog.query(f"#{section_id} .usage-choice.-selected").only_one(Button).label.plain


def _kpis(dialog: UsageDialog) -> dict[str, str]:
    tiles = (rich_plain(tile.render()).split("\n") for tile in dialog.query(".usage-kpi").results(Static))
    return dict(tiles)


def _headers(dialog: UsageDialog) -> list[str]:
    return [column.label.plain for column in dialog.query_one(_UsageRows).ordered_columns]


def _rows(dialog: UsageDialog) -> list[list[str]]:
    table = dialog.query_one(_UsageRows)
    return [[cell.plain for cell in table.get_row_at(index)] for index in range(table.row_count)]


def _row_names(dialog: UsageDialog) -> list[str]:
    return [row[0] for row in _rows(dialog)]


def _cursor_name(dialog: UsageDialog) -> str:
    return _row_names(dialog)[dialog.query_one(_UsageRows).cursor_row]


def _chart_lines(dialog: UsageDialog) -> list[str]:
    return rich_plain(dialog.query_one(_TrendChart).render()).split("\n")


def _grid_text(dialog: UsageDialog) -> str:
    return rich_plain(dialog.query_one(_MonthGrid).render())


def _day_title(dialog: UsageDialog) -> str:
    return _section_title(dialog, "usage-day")


async def _open(pilot: Pilot, report: UsageReport | None = None) -> UsageDialog:
    loaded = report if report is not None else _sample_report()
    dialog = UsageDialog(lambda _cancel: loaded)
    await pilot.app.push_screen(dialog)
    await wait_for(
        lambda: pilot.app.screen is dialog and dialog.is_mounted and not dialog._reading,
        pilot=pilot,
        description="usage read",
    )
    await pilot.pause()
    return dialog


async def _open_calendar(pilot: Pilot, report: UsageReport | None = None) -> UsageDialog:
    dialog = await _open(pilot, report)
    await pilot.press("tab")
    await wait_for(lambda: bool(_day_title(dialog)), pilot=pilot, description="calendar rendered")
    await wait_for(lambda: dialog.query_one(_MonthGrid).size.height > 0, pilot=pilot, description="calendar laid out")
    return dialog


@pytest.mark.parametrize(("locale", "label"), [("en", "Side questions"), ("zh-Hans", "顺便问")])
async def test_side_question_usage_has_a_localized_source(
    monkeypatch: pytest.MonkeyPatch, locale: str, label: str
) -> None:
    from chrys.app.tui import i18n as tui_i18n

    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    if locale != "en":
        assert controller.switch_locale(locale).status is LocaleSwitchStatus.EFFECTIVE_CHANGED

    class _LocalizedApp(_HostApp):
        locale_controller = controller

    async with _LocalizedApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, _report(_call(source="side_question")))
        await click_when_settled(pilot, dialog.query_one("#usage-group-source", _ChoiceButton))
        await wait_for(lambda: _row_names(dialog) == [label], pilot=pilot, description="localized side-question usage")
        assert "side_question" not in _painted(pilot)


async def test_the_tabs_are_a_real_tab_view_switched_by_click_and_by_the_tab_key() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        tabs = dialog.query_one(ContentTabs).query(Tab)

        assert [tab.label.plain for tab in tabs] == ["Overview", "Calendar"]
        assert _active_tab(dialog) == "usage-overview"

        await pilot.press("tab")
        await wait_for(lambda: _active_tab(dialog) == "usage-calendar", pilot=pilot, description="to the calendar")
        await pilot.press("tab")
        await wait_for(lambda: _active_tab(dialog) == "usage-overview", pilot=pilot, description="back to the overview")

        await click_when_settled(pilot, tabs.last())
        await wait_for(lambda: _active_tab(dialog) == "usage-calendar", pilot=pilot, description="click")


async def test_the_dialog_reads_once_and_the_number_keys_and_r_do_nothing() -> None:
    loads: list[int] = []

    def _load(_cancel: threading.Event) -> UsageReport:
        loads.append(1)
        return _sample_report()

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog._report is not None, pilot=pilot, description="first read")

        await pilot.press("2", "r", "1")

        assert not await wait_until(lambda: len(loads) > 1, timeout=0.2, pilot=pilot)
        assert _active_tab(dialog) == "usage-overview"


async def test_the_keys_of_the_showing_tab_are_in_the_border_subtitle() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        assert _subtitle(dialog) == _OVERVIEW_KEYS

        await pilot.press("tab")
        await wait_for(lambda: _subtitle(dialog) == _CALENDAR_KEYS, pilot=pilot, description="calendar keys")
        await pilot.press("tab")
        await wait_for(lambda: _subtitle(dialog) == _OVERVIEW_KEYS, pilot=pilot, description="overview keys")


async def test_nothing_takes_focus_so_the_arrows_reach_the_dialog() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        assert dialog.query_one(ContentTabs).can_focus is False
        assert dialog.query_one(_RangeTabs).can_focus is False
        assert dialog.query_one(_UsageRows).can_focus is False
        assert all(button.can_focus is False for button in dialog.query(_ChoiceButton))
        assert dialog.focused is None

        await pilot.press("left")
        await wait_for(lambda: _active_range(dialog) == "This week", pilot=pilot, description="range moved")
        assert _active_tab(dialog) == "usage-overview"

        await click_when_settled(pilot, dialog.query_one("#usage-group-source", _ChoiceButton))
        await click_when_settled(pilot, dialog.query_one(_UsageRows), offset=(2, 1))
        await pilot.press("left")
        await wait_for(lambda: _active_range(dialog) == "Today", pilot=pilot, description="range moved after clicks")
        assert dialog.focused is None


async def test_the_title_stays_the_windows_name_once_the_numbers_are_in() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        assert _title(dialog) == "Token Usage"


async def test_the_overview_opens_on_this_month_and_moves_by_left_and_right() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        assert _active_range(dialog) == "This month"
        assert _section_title(dialog, "usage-trend") == "Trend"
        assert _kpis(dialog) == {
            "New": "10k",
            "Input": "10k",
            "Output": "400",
            "Cached": "6.0k",
            "Hit": "38%",
            "TPS": "100",
            "Time": "6s",
            "Calls": "2",
        }

        await pilot.press("right")
        await wait_for(lambda: _active_range(dialog) == "30 days", pilot=pilot, description="30 days")
        await pilot.press("right")
        await wait_for(lambda: _active_range(dialog) == "All", pilot=pilot, description="all")
        await wait_for(lambda: _kpis(dialog)["New"] == "61k", pilot=pilot, description="all's numbers")
        await pilot.press("right")
        await wait_for(lambda: _active_range(dialog) == "Today", pilot=pilot, description="around to today")
        await wait_for(lambda: _kpis(dialog)["New"] == "1.2k", pilot=pilot, description="today's numbers")
        await pilot.press("left")
        await wait_for(lambda: _active_range(dialog) == "All", pilot=pilot, description="back to all")


async def test_the_ranges_are_tabs_a_click_chooses() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        ranges = dialog.query_one(_RangeTabs)

        labels = ["Today", "This week", "This month", "30 days", "All"]
        assert [tab.label.plain for tab in ranges.query(Tab)] == labels
        await wait_for(
            lambda: any(all(label in line for label in labels) for line in _painted(pilot).split("\n")),
            pilot=pilot,
            description="every range painted on one line",
        )

        await click_when_settled(pilot, dialog.query_one("#usage-range-0", Tab))

        await wait_for(lambda: _kpis(dialog)["New"] == "1.2k", pilot=pilot, description="today's numbers")
        assert _active_range(dialog) == "Today"


async def test_the_trend_follows_the_range_until_t_toggles_it() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        assert _chosen(dialog, "usage-trend") == "Daily"

        await pilot.press("t")
        assert _chosen(dialog, "usage-trend") == "Weekly"
        await pilot.press("t")
        assert _chosen(dialog, "usage-trend") == "Daily"

        await pilot.press("t", "right")
        await wait_for(lambda: _active_range(dialog) == "30 days", pilot=pilot, description="30 days")
        assert _chosen(dialog, "usage-trend") == "Daily"
        await pilot.press("t")
        assert _chosen(dialog, "usage-trend") == "Weekly"

        await pilot.press("right")
        await wait_for(lambda: _active_range(dialog) == "All", pilot=pilot, description="all")
        assert _chosen(dialog, "usage-trend") == "Weekly"
        await pilot.press("t")
        assert _chosen(dialog, "usage-trend") == "Daily"
        await pilot.press("left", "right")
        await wait_for(lambda: _active_range(dialog) == "All", pilot=pilot, description="all again")
        assert _chosen(dialog, "usage-trend") == "Weekly"


def _scale_buttons_showing(dialog: UsageDialog) -> list[bool]:
    return [button.display for button in dialog.query("#usage-trend .usage-choice")]


async def test_today_is_a_chart_of_its_24_hours_with_no_scale_to_choose() -> None:
    report = _report(
        _call(hour=9, new_input=4_000_000, output=0),
        _call(hour=21, new_input=2_000_000, output=0),
        _call(_TODAY - timedelta(days=1), hour=9, new_input=90_000_000, output=0),
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)
        assert _scale_buttons_showing(dialog) == [True, True]

        await pilot.press("left", "left")
        await wait_for(lambda: _active_range(dialog) == "Today", pilot=pilot, description="today")

        chart = dialog.query_one(_TrendChart)
        await wait_for(lambda: _chart_lines(dialog)[-1].startswith("00:00"), pilot=pilot, description="hours")
        lines = _chart_lines(dialog)
        assert lines[-1].split() == ["00:00", "06:00", "12:00", "18:00"]
        spans = _bar_spans(24, chart.size.width)
        top, bottom = lines[0], lines[-2]
        start, cells = spans[9]
        assert top[start : start + cells] == "█" * cells
        assert top.count("█") == cells
        assert bottom[spans[21][0]] == "█"
        assert bottom[spans[8][0]] in _HATCH
        # Yesterday's larger spend is not today's.
        assert _text(dialog, "usage-peak") == "peak 4.00M"
        assert _scale_buttons_showing(dialog) == [False, False]


async def test_this_week_is_a_chart_of_its_seven_days_with_no_scale_to_choose() -> None:
    report = _report(
        _call(date(2026, 10, 26), new_input=3_000_000, output=0),
        _call(new_input=1_000_000, output=0),
        _call(date(2026, 10, 25), new_input=90_000_000, output=0),
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        await pilot.press("left")
        await wait_for(lambda: _active_range(dialog) == "This week", pilot=pilot, description="this week")

        chart = dialog.query_one(_TrendChart)
        await wait_for(lambda: len(_chart_lines(dialog)[-1].split()) == 7, pilot=pilot, description="seven days")
        lines = _chart_lines(dialog)
        assert lines[-1].split() == ["10-26", "10-27", "10-28", "10-29", "10-30", "10-31", "11-01"]
        spans = _bar_spans(7, chart.size.width)
        assert lines[0].count("█") == spans[0][1]
        assert lines[0].startswith("█" * spans[0][1])
        # Today is the last day with a bar: the rest of the week has yet to come.
        assert lines[-2][spans[2][0]] == "█"
        assert all(lines[-2][spans[day][0]] in _HATCH for day in range(3, 7))
        # Last Sunday's larger spend is not this week's.
        assert _text(dialog, "usage-peak") == "peak 3.00M"
        assert _scale_buttons_showing(dialog) == [False, False]


def test_a_day_after_the_report_ended_has_no_bar_as_no_total_counts_it() -> None:
    report = _report(_call(new_input=1000, output=0), _call(_TODAY + timedelta(days=1)))

    columns = _week_columns(report, date(2026, 10, 26))

    assert [column.value for column in columns] == [0, 0, 1000, 0, 0, 0, 0]


@pytest.mark.parametrize(("presses", "name"), [(("left",), "This week"), (("left", "left"), "Today")])
async def test_t_changes_nothing_and_is_not_offered_where_the_trend_has_one_scale(
    presses: tuple[str, ...], name: str
) -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        await pilot.press(*presses)
        await wait_for(lambda: _active_range(dialog) == name, pilot=pilot, description=name)
        await wait_for(
            lambda: _subtitle(dialog) == "←→ range  g group  ↑↓ row", pilot=pilot, description="keys without t"
        )
        before = _chart_lines(dialog)

        await pilot.press("t")

        assert _chart_lines(dialog) == before
        assert _scale_buttons_showing(dialog) == [False, False]
        assert _subtitle(dialog) == "←→ range  g group  ↑↓ row"


@pytest.mark.parametrize(
    ("presses", "name"), [((), "This month"), (("right",), "30 days"), (("right", "right"), "All")]
)
async def test_the_longer_ranges_keep_both_scales(presses: tuple[str, ...], name: str) -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        await pilot.press("left", "right", *presses)
        await wait_for(lambda: _active_range(dialog) == name, pilot=pilot, description=name)
        await wait_for(lambda: _subtitle(dialog) == _OVERVIEW_KEYS, pilot=pilot, description="keys with t")

        assert _scale_buttons_showing(dialog) == [True, True]
        chosen = _chosen(dialog, "usage-trend")
        await pilot.press("t")
        assert _chosen(dialog, "usage-trend") != chosen


async def test_the_trend_line_refits_when_the_scale_buttons_come_and_go() -> None:
    report = _report(_call(new_input=83_790_000, output=0))
    async with _HostApp().run_test(size=(42, 24)) as pilot:
        dialog = await _open(pilot, report)
        peak = dialog.query_one("#usage-peak")
        await wait_for(lambda: _painted_within(pilot, peak) == "83.79M", pilot=pilot, description="beside the buttons")

        await pilot.press("left", "left")
        await wait_for(lambda: _painted_within(pilot, peak) == "peak 83.79M", pilot=pilot, description="room to spare")

        await pilot.press("right", "right")
        await wait_for(lambda: _painted_within(pilot, peak) == "83.79M", pilot=pilot, description="buttons back")


async def test_the_trend_and_group_buttons_choose_by_click() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        await click_when_settled(pilot, dialog.query_one("#usage-trend-weekly", _ChoiceButton))
        await wait_for(lambda: _chosen(dialog, "usage-trend") == "Weekly", pilot=pilot, description="weekly")

        await click_when_settled(pilot, dialog.query_one("#usage-group-project", _ChoiceButton))
        await wait_for(lambda: _chosen(dialog, "usage-groups") == "Project", pilot=pilot, description="project")
        assert _headers(dialog)[0] == "Project"
        assert _row_names(dialog) == ["beta", "alpha"]


async def test_the_chart_fills_its_width_and_shows_the_peak_and_mondays() -> None:
    report = _report(
        _call(date(2026, 10, 1), new_input=7_000_000, output=0),
        _call(new_input=7_000_000, output=0),
        _call(_TODAY - timedelta(days=2), new_input=1000),
    )

    def _assert_filled(dialog: UsageDialog) -> None:
        chart = dialog.query_one(_TrendChart)
        lines = _chart_lines(dialog)
        bars = lines[:-1]
        assert len(bars) == chart.size.height - 1
        # The first day is the peak, at the chart's edge; the month's last days have yet to come, up to the other.
        assert bars[0].startswith("█")
        assert bars[0][-1] in _HATCH
        assert len(bars[0]) == chart.size.width
        assert "▁" in bars[-1]
        assert lines[-1].split() == ["10-05", "10-12", "10-19", "10-26"]

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        assert _text(dialog, "usage-peak").endswith("peak 7.00M")
        _assert_filled(dialog)
        narrow = dialog.query_one(_TrendChart).size.width

        await resize_when_settled(pilot, 140, 50)

        await wait_for(lambda: dialog.query_one(_TrendChart).size.width > narrow, pilot=pilot, description="wider")
        _assert_filled(dialog)


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_a_blank_row_keeps_the_tallest_bar_off_the_trend_buttons(size: tuple[int, int]) -> None:
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open(pilot)
        chart = dialog.query_one(_TrendChart)
        await wait_for(lambda: chart.region.height > 0, pilot=pilot, description="chart laid out")

        assert chart.region.y == dialog.query_one("#usage-trend-daily", _ChoiceButton).region.bottom + 1


async def test_whatever_a_bar_leaves_of_its_column_is_hatched_in_a_faint_color_of_its_own() -> None:
    report = _report(
        _call(hour=9, new_input=4_000_000, output=0),
        _call(hour=21, new_input=2_000_000, output=0),
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)
        await pilot.press("left", "left")
        await wait_for(lambda: _chart_lines(dialog)[-1].startswith("00:00"), pilot=pilot, description="hours")

        chart = dialog.query_one(_TrendChart)
        bars = _chart_lines(dialog)[:-1]
        spans = _bar_spans(24, chart.size.width)
        start, cells = spans[8]
        # Every row alike, and by the chart's column, so the lines run on across rows and the gaps.
        expected = "".join(_HATCH[column % 2] for column in range(start, start + cells))
        assert all(line[start : start + cells] == expected for line in bars)
        assert all(line[start + cells] == " " for line in bars), "the gap between columns stays clear"
        half = spans[21][0]
        assert bars[0][half] == _HATCH[half % 2]
        assert bars[-1][half] == "█"

        rendered = chart.render()
        track = chart.get_component_rich_style("usage-chart--track")
        hatched = [span.style for span in rendered.spans if set(rendered.plain[span.start : span.end]) <= set(_HATCH)]
        assert hatched
        assert set(hatched) == {track}
        assert track.color != chart.rich_style.color
        await wait_for(lambda: _HATCH[0] in _painted(pilot), pilot=pilot, description="hatching painted")


@pytest.mark.parametrize("theme", ["ansi-dark", "ansi-light"])
async def test_under_an_ansi_theme_the_hatching_takes_the_terminals_own_gray(theme: str) -> None:
    app = _HostApp()
    async with app.run_test(size=(100, 30)) as pilot:
        app.theme = theme
        dialog = await _open(pilot)

        track = dialog.query_one(_TrendChart).get_component_rich_style("usage-chart--track")
        assert track.color is not None
        assert track.color.number == 8


async def test_a_range_with_no_tokens_at_all_is_a_chart_of_hatching_not_a_blank() -> None:
    report = _report(_call(_TODAY - timedelta(days=1)))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)
        await pilot.press("left", "left")
        await wait_for(lambda: _chart_lines(dialog)[-1].startswith("00:00"), pilot=pilot, description="hours")

        bars = _chart_lines(dialog)[:-1]
        assert all(set(line) == {*_HATCH, " "} for line in bars)


@pytest.mark.parametrize(
    ("presses", "name", "labels"),
    [
        ((), "This month", ["10-01", "10-05", "10-12", "10-19", "10-26"]),
        (("right",), "30 days", ["09-29", "10-05", "10-12", "10-19", "10-26"]),
    ],
)
async def test_by_the_week_a_month_or_30_days_shows_only_the_weeks_of_the_range(
    presses: tuple[str, ...], name: str, labels: list[str]
) -> None:
    report = _report(
        _call(date(2026, 9, 7), new_input=90_000_000, output=0),
        # In the first column's week, but before the range: the column counts only the range's days.
        _call(date(2026, 9, 28), new_input=80_000_000, output=0),
        _call(date(2026, 10, 2), new_input=3_000_000, output=0),
        _call(new_input=1_000_000, output=0),
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)
        await pilot.press(*presses, "t")
        await wait_for(lambda: _active_range(dialog) == name, pilot=pilot, description=name)
        await wait_for(lambda: _chosen(dialog, "usage-trend") == "Weekly", pilot=pilot, description="weekly")

        lines = _chart_lines(dialog)
        assert lines[-1].split() == labels
        spans = _bar_spans(len(labels), dialog.query_one(_TrendChart).size.width)
        assert lines[0].startswith("█" * spans[0][1])
        assert lines[0].count("█") == spans[0][1]
        assert _text(dialog, "usage-peak") == "peak 3.00M"


async def test_a_month_just_begun_still_shows_all_of_it_by_the_day_and_by_the_week() -> None:
    today = date(2026, 11, 1)
    report = _report(
        _call(today, new_input=2_000_000, output=0),
        _call(date(2026, 10, 30), new_input=90_000_000, output=0),
        today=today,
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        width = dialog.query_one(_TrendChart).size.width
        lines = _chart_lines(dialog)
        assert lines[-1].split() == ["11-02", "11-09", "11-16", "11-23"]
        spans = _bar_spans(30, width)
        assert lines[0].startswith("█" * spans[0][1])
        assert lines[0].count("█") == spans[0][1]
        assert _text(dialog, "usage-peak") == "peak 2.00M"

        await pilot.press("t")

        lines = _chart_lines(dialog)
        assert lines[-1].split() == ["11-01", "11-02", "11-09", "11-16", "11-23", "11-30"]
        spans = _bar_spans(6, width)
        assert lines[0].startswith("█" * spans[0][1])
        assert lines[0].count("█") == spans[0][1]


def test_a_narrow_chart_gives_up_the_empty_columns_after_the_last_bar_first() -> None:
    def columns_with_a_bar_at(index: int | None) -> list[_TrendColumn]:
        return [_TrendColumn("", 5 if hour == index else 0) for hour in range(24)]

    early = columns_with_a_bar_at(3)
    late = columns_with_a_bar_at(20)
    empty = columns_with_a_bar_at(None)

    assert _fitting(early, 10) == early[:10]
    assert _fitting(late, 10) == late[11:21]
    assert _fitting(empty, 10) == empty[-10:]
    assert _fitting(early, 30) == early


async def test_a_chart_too_narrow_for_every_day_shows_the_newest_and_their_peak() -> None:
    report = _report(_call(_TODAY - timedelta(days=400), new_input=9_000_000, output=0), _call())
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot, report)

        await pilot.press("right", "right")
        await wait_for(lambda: _active_range(dialog) == "All", pilot=pilot, description="all")
        await pilot.press("t")
        await wait_for(lambda: _chosen(dialog, "usage-trend") == "Daily", pilot=pilot, description="daily")

        assert _text(dialog, "usage-peak").endswith("peak 1.2k")
        assert _kpis(dialog)["New"] == "9.00M"


@pytest.mark.parametrize(
    ("count", "width"),
    [(28, 92), (28, 55), (8, 70), (75, 113), (14, 14), (14, 5), (1, 30)],
)
def test_bars_run_from_edge_to_edge_without_overlapping(count: int, width: int) -> None:
    spans = _bar_spans(count, width)

    assert len(spans) == count
    assert spans[0][0] == 0
    assert spans[-1][0] + spans[-1][1] == width
    assert all(cells >= 0 for _start, cells in spans)
    assert all(start + cells <= after for (start, cells), (after, _cells) in pairwise(spans))


def test_bars_with_room_for_gaps_are_equal_and_bars_without_it_touch() -> None:
    assert {cells for _start, cells in _bar_spans(28, 92)} == {2}
    dense = _bar_spans(75, 113)
    assert all(start + cells == after for (start, cells), (after, _cells) in pairwise(dense))
    assert _bar_spans(0, 30) == []


async def test_the_weekly_chart_counts_only_the_days_in_range_of_a_cut_week() -> None:
    # This month starts on a Thursday: its first bar is that week from the 1st, not all of it.
    report = _report(
        _call(date(2026, 10, 2), new_input=1_000_000, output=0),
        _call(date(2026, 9, 30), new_input=9_000_000, output=0),
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        await pilot.press("t")

        # The week of Sep 28 falls in the range, so the 9M day outside it cannot set the peak.
        assert _text(dialog, "usage-peak").endswith("peak 1.00M")


async def test_the_groups_list_their_numbers_and_cycle_with_g() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        assert _chosen(dialog, "usage-groups") == "Model"
        assert _headers(dialog) == ["Model", "", "New", "Input", "Output", "Hit", "TPS", "Calls", "Failed"]
        rows = _rows(dialog)
        assert rows[0][0] == "model-b"
        assert rows[0][2:] == ["9.2k", "9.0k", "200", "25%", "100", "1", "0"]
        assert rows[1][0] == "model-a"
        assert rows[1][2:] == ["1.2k", "1.0k", "200", "75%", "100", "1", "0"]

        await pilot.press("g")
        assert _chosen(dialog, "usage-groups") == "Source"
        assert _headers(dialog) == ["Source", "", "New", "Input", "Output", "Hit", "Calls", "Failed"]
        assert _row_names(dialog)[0] == "Sub-agent"

        await pilot.press("g")
        assert _chosen(dialog, "usage-groups") == "Project"
        assert _row_names(dialog)[0] == "beta"

        await pilot.press("g")
        assert _chosen(dialog, "usage-groups") == "Model"


async def test_the_row_cursor_moves_by_key_and_click_and_resets_with_the_list() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        assert _cursor_name(dialog) == "model-b"

        await pilot.press("down")
        assert _cursor_name(dialog) == "model-a"
        await pilot.press("down")
        assert _cursor_name(dialog) == "model-a"
        await pilot.press("up", "up")
        assert _cursor_name(dialog) == "model-b"

        # The header is the table's first row.
        await click_when_settled(pilot, dialog.query_one(_UsageRows), offset=(2, 2))
        await wait_for(lambda: _cursor_name(dialog) == "model-a", pilot=pilot, description="clicked row")

        table = dialog.query_one(_UsageRows)
        width = table.size.width
        await resize_when_settled(pilot, 120, 30)
        await wait_for(
            lambda: table.size.width > width and dialog._rows_shown == (2, 0, table.size.width - 1),
            pilot=pilot,
            description="rebuilt for the new width",
        )
        assert table.row_count == 2
        assert _cursor_name(dialog) == "model-a"

        await pilot.press("g")
        assert dialog.query_one(_UsageRows).cursor_row == 0

        await pilot.press("g", "g", "down", "right")
        await wait_for(lambda: _active_range(dialog) == "30 days", pilot=pilot, description="next range")
        assert dialog.query_one(_UsageRows).cursor_row == 0


async def test_the_table_is_built_again_only_when_its_contents_change(monkeypatch: pytest.MonkeyPatch) -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)
        table = dialog.query_one(_UsageRows)
        clears: list[bool] = []
        real_clear = table.clear

        def _clear(columns: bool = False) -> _UsageRows:
            clears.append(columns)
            return real_clear(columns=columns)

        monkeypatch.setattr(table, "clear", _clear)

        await pilot.press("down", "t", "t", "tab", "tab")
        await pilot.pause()
        assert clears == []

        await pilot.press("g")
        assert clears == [True]


def test_a_narrow_table_drops_columns_by_how_telling_they_are() -> None:
    def _kept(width: int, *, models: bool = True) -> tuple[list[str], int, int]:
        columns, name_cells, share_cells = _fit_row_columns(width, models=models)
        return [column.label.fallback for column in columns], name_cells, share_cells

    assert _kept(28) == (["New"], 20, 0)
    assert _kept(50) == (["New", "Hit", "TPS"], 24, 0)
    assert _kept(71, models=False)[0] == ["New", "Input", "Output", "Hit", "Calls", "Failed"]

    wide_names, wide_name_cells, wide_share = _kept(120)
    assert wide_names == ["New", "Input", "Output", "Hit", "TPS", "Calls", "Failed"]
    assert wide_share == 23
    assert wide_name_cells == 41


def test_a_share_bar_shows_any_share_above_nothing() -> None:
    assert _share_bar(1.0, 10) == "█" * 10
    assert _share_bar(0.5, 10) == "█" * 5
    assert _share_bar(0.0, 10) == ""
    assert _share_bar(0.0001, 10) == "▏"


async def test_a_model_never_timed_has_no_speed_to_show() -> None:
    report = _report(_call(first_chunk_ms=None))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        assert _rows(dialog)[0][_headers(dialog).index("TPS")] == "-"
        assert _kpis(dialog)["TPS"] == "-"


async def test_a_group_with_more_rows_than_fit_scrolls_with_the_cursor() -> None:
    report = _report(*[_call(model=f"model-{index:02}", new_input=1000 + index, output=0) for index in range(30)])
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot, report)
        table = dialog.query_one(_UsageRows)
        assert table.row_count == 30
        assert "model-29" in _painted(pilot)
        assert "model-00" not in _painted(pilot)

        await pilot.press(*["down"] * 29)

        assert _cursor_name(dialog) == "model-00"
        await wait_for(lambda: "model-00" in _painted(pilot), pilot=pilot, description="last row in view")

        await pilot.press("pageup")
        assert _cursor_name(dialog) != "model-00"


async def test_a_call_with_no_tokens_lists_no_row_but_counts_as_unreported() -> None:
    report = _report(_call(), _call(model="silent", new_input=0, cache_read=0, output=0, reported=False, failed=True))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        assert "silent" not in _row_names(dialog)
        assert "1 call unreported" in _text(dialog, "usage-note")


async def test_the_note_says_where_the_numbers_start_and_what_they_leave_out() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, _report(_call(_TODAY - timedelta(days=3)), unreadable=2))

        assert _text(dialog, "usage-note") == "2 logs unreadable · from 10-25 · no deleted sessions/external ACP"


async def test_a_day_of_another_year_shows_its_year() -> None:
    report = _report(_call(date(2025, 12, 20)), today=date(2026, 1, 5))
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        dialog = await _open(pilot, report)

        await pilot.press("left", "left", "left")
        await wait_for(lambda: _active_range(dialog) == "All", pilot=pilot, description="all")
        assert "from 2025-12-20" in _text(dialog, "usage-note")


async def test_labels_from_the_logs_stay_literal_and_clean() -> None:
    report = _report(_call(model="[red]x\x1b[31m[/oops]", project="/w/[bold]p\udcff"))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        name = _row_names(dialog)[0]
        assert "[red]x" in name
        assert "[/oops]" in name
        assert "\x1b" not in name
        await wait_for(lambda: "[red]x" in _painted(pilot), pilot=pilot, description="painted as written")

        await pilot.press("g", "g")

        name = _row_names(dialog)[0]
        assert "[bold]p" in name
        assert "\udcff" not in name


async def test_the_unknown_cases_read_as_words() -> None:
    report = _report(_call(model="", project="", source="new_role"))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot, report)

        assert _row_names(dialog) == ["Unknown model"]
        await pilot.press("g")
        assert _row_names(dialog) == ["new_role"]
        await pilot.press("g")
        assert _row_names(dialog) == ["Other workspaces"]


def test_project_labels_widen_only_as_far_as_needed() -> None:
    assert _project_labels(["/a/one", "/a/two", "/b/two", "/x/y/same", "/z/y/same", ""]) == {
        "/a/one": "one",
        "/a/two": "a/two",
        "/b/two": "b/two",
        "/x/y/same": "/x/y/same",
        "/z/y/same": "/z/y/same",
    }


async def test_a_short_terminal_gives_up_the_frames_and_a_taller_one_gets_them_back() -> None:
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot)
        frame = dialog.query_one("#usage-container")

        assert frame.has_class("-compact")
        label = dialog.query_one("#usage-trend .usage-section-label", Static)
        assert label.display is True
        assert _text(dialog, "usage-peak").startswith("peak")

        await resize_when_settled(pilot, 120, 44)

        await wait_for(lambda: not frame.has_class("-compact"), pilot=pilot, description="frames back")
        await wait_for(lambda: not label.display, pilot=pilot, description="inline label gone")
        assert _text(dialog, "usage-peak").startswith("peak")
        assert _section_title(dialog, "usage-trend") == "Trend"


async def test_the_calendar_opens_on_today_and_the_arrows_move_the_day() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open_calendar(pilot)

        assert _text(dialog, "usage-month") == "2026-10"
        assert _grid_text(dialog).split("\n")[0].split() == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun", "Week"]
        assert _day_title(dialog) == "2026-10-28 Wed"
        detail = _text(dialog, "usage-calendar-detail")
        assert "New" in detail
        assert "model-a" in detail

        await pilot.press("left")
        assert _day_title(dialog) == "2026-10-27 Tue"
        assert "model-b" in _text(dialog, "usage-calendar-detail")
        await pilot.press("up")
        assert _day_title(dialog) == "2026-10-20 Tue"
        await pilot.press("down")
        assert _day_title(dialog) == "2026-10-27 Tue"
        await pilot.press("right", "right")
        assert _day_title(dialog) == "2026-10-28 Wed"


async def test_the_calendar_stays_between_todays_day_and_the_first_month_with_records() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open_calendar(pilot)

        await pilot.press("right")
        assert _day_title(dialog).startswith("2026-10-28")

        await pilot.press("left_square_bracket")
        assert _text(dialog, "usage-month") == "2026-09"
        assert _day_title(dialog).startswith("2026-09-28")
        await pilot.press("left_square_bracket")
        assert _day_title(dialog).startswith("2026-09-01")

        await pilot.press("right_square_bracket", "right_square_bracket", "right_square_bracket")
        assert _day_title(dialog).startswith("2026-10-28")


async def test_the_calendar_keeps_a_month_end_on_the_shorter_months_last_day() -> None:
    report = _report(_call(date(2026, 1, 31)), today=date(2026, 3, 31))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open_calendar(pilot, report)

        await pilot.press("left_square_bracket")

        assert _day_title(dialog).startswith("2026-02-28")


async def test_the_month_buttons_change_the_month_and_stop_at_its_limits() -> None:
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        dialog = await _open_calendar(pilot)
        previous = dialog.query_one("#usage-month-previous", _ChoiceButton)
        following = dialog.query_one("#usage-month-next", _ChoiceButton)

        assert following.disabled is True
        assert previous.disabled is False

        await click_when_settled(pilot, previous)

        await wait_for(lambda: _text(dialog, "usage-month") == "2026-09", pilot=pilot, description="previous month")
        assert _day_title(dialog).startswith("2026-09-28")
        assert previous.disabled is True
        assert following.disabled is False

        await click_when_settled(pilot, following)
        await wait_for(lambda: _text(dialog, "usage-month") == "2026-10", pilot=pilot, description="next month")


async def test_a_click_on_a_day_selects_it_and_a_click_between_days_does_nothing() -> None:
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        dialog = await _open_calendar(pilot)
        grid = dialog.query_one(_MonthGrid)
        width, height = grid.size
        shape = _grid_shape(width, height, 5)  # October 2026 touches five weeks.
        x, cells = shape.days[2]
        first_row, rows = shape.weeks[2]
        y = 1 + first_row

        await click_when_settled(pilot, grid, offset=(x + 1, y))
        await wait_for(lambda: _day_title(dialog).startswith("2026-10-14"), pilot=pilot, description="clicked day")

        assert shape.weeks[3][0] > first_row + rows
        await click_when_settled(pilot, grid, offset=(x + 1, y + rows))
        await pilot.pause()
        assert _day_title(dialog).startswith("2026-10-14")

        await click_when_settled(pilot, grid, offset=(x + cells, y))
        await pilot.pause()
        assert _day_title(dialog).startswith("2026-10-14")

        for index, (start, day_cells) in enumerate(shape.days):
            assert grid._day_at(start, y) == date(2026, 10, 12 + index)
            assert grid._day_at(start + day_cells - 1, y) == date(2026, 10, 12 + index)


@pytest.mark.parametrize(("size", "big"), [((80, 24), False), ((120, 40), True)])
async def test_the_grid_shows_each_days_tokens_when_it_has_room(size: tuple[int, int], big: bool) -> None:
    report = _report(
        _call(new_input=7_650_000, output=0), _call(_TODAY - timedelta(days=1), new_input=1_230_000, output=0)
    )
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot, report)
        width, height = dialog.query_one(_MonthGrid).size

        assert _grid_shape(width, height, 5).big is big
        grid = _grid_text(dialog)
        assert ("7.65M" in grid) is big
        assert ("1.23M" in grid) is big
        # This week's total shows either way, on the row of its dates.
        total_line = next(line for line in grid.split("\n") if "8.88M" in line)
        assert total_line.split()[:3] == ["26", "27", "28"]


@pytest.mark.parametrize(
    ("width", "height", "weeks"),
    [(75, 29, 5), (83, 45, 5), (41, 15, 5), (38, 16, 5), (57, 20, 6), (35, 24, 4)],
)
def test_the_grid_reaches_every_edge_of_its_space(width: int, height: int, weeks: int) -> None:
    shape = _grid_shape(width, height, weeks)

    assert shape.week_cells == 6
    assert shape.days[0][0] == 0
    assert all(start + cells + 1 == after for (start, cells), (after, _cells) in pairwise(shape.days))
    sunday_start, sunday_cells = shape.days[-1]
    assert sunday_start + sunday_cells + 2 + shape.week_cells == width, "the week column follows Sunday by two cells"
    assert max(cells for _start, cells in shape.days) - min(cells for _start, cells in shape.days) <= 1

    assert shape.weeks[0][0] == 0
    last_start, last_rows = shape.weeks[-1]
    assert last_start + last_rows == height - 1
    assert max(rows for _start, rows in shape.weeks) - min(rows for _start, rows in shape.weeks) <= 1


def test_the_grid_shape_picks_big_cells_and_gaps_by_the_room_it_has() -> None:
    assert _grid_shape(75, 29, 5).big is True

    small = _grid_shape(41, 15, 5)
    assert small.big is False
    assert {rows for _start, rows in small.weeks} == {2}
    assert small.weeks[1][0] == 3, "a gap row between weeks"

    wide_but_flat = _grid_shape(75, 8, 6)
    assert wide_but_flat.big is False
    assert wide_but_flat.weeks == [(index, 1) for index in range(6)]

    # Too short for every week to take two rows: all stay at one, the rest stays free.
    one_row_each = _grid_shape(40, 16, 6)
    assert one_row_each.weeks == [(index * 2, 1) for index in range(6)]

    assert _grid_shape(40, 30, 5).big is False

    assert _grid_shape(35, 15, 5).week_cells == 6
    no_week_column = _grid_shape(34, 15, 5)
    assert no_week_column.week_cells == 0
    assert no_week_column.days[-1][0] + no_week_column.days[-1][1] == 34


def test_a_days_shade_goes_by_the_snapshots_quartiles() -> None:
    thresholds = (10, 20, 30)
    assert [_heat_level(count, thresholds) for count in (0, 1, 10, 11, 20, 21, 30, 31)] == [0, 1, 1, 2, 2, 3, 3, 4]


async def test_each_day_is_painted_in_its_shade_and_the_selected_day_stands_out() -> None:
    report = _report(
        *[_call(_TODAY - timedelta(days=offset), new_input=1000 * (offset + 1), output=0) for offset in range(8)]
    )
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        dialog = await _open_calendar(pilot, report)
        await pilot.press("up")
        grid = dialog.query_one(_MonthGrid)
        painted = {span.style for span in grid.render().spans}

        backgrounds = {grid.heat_style(level).bgcolor for level in range(5)}
        selected = grid.get_component_rich_style("usage-grid--selected")
        assert len(backgrounds) == 5
        assert selected.bgcolor not in backgrounds
        assert selected in painted
        assert grid.heat_style(1) + Style(underline=True) in painted, "today's date is underlined"
        assert grid.heat_style(0) in painted
        assert grid.heat_style(1) in painted
        assert grid.heat_style(4) in painted


async def test_moving_to_a_busier_day_paints_all_of_its_models() -> None:
    yesterday = _TODAY - timedelta(days=1)
    report = _report(
        _call(_TODAY - timedelta(days=2), new_input=0, output=0, cache_read=0),
        _call(yesterday, model="model-a"),
        _call(yesterday, model="model-b"),
        _call(yesterday, model="model-c"),
    )
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        await _open_calendar(pilot, report)
        await wait_for(lambda: "2026-10-28 Wed" in _painted(pilot), pilot=pilot, description="today painted")
        assert "Top models" not in _painted(pilot)

        await pilot.press("left")

        await wait_for(lambda: "model-c" in _painted(pilot), pilot=pilot, description="all three models painted")


async def test_a_calendar_opened_during_the_read_paints_the_whole_month_when_it_arrives() -> None:
    release = threading.Event()

    def _load(_cancel: threading.Event) -> UsageReport:
        release.wait(30)
        return _sample_report()

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted, pilot=pilot, description="dialog shown")
        await pilot.press("tab")
        await wait_for(lambda: _READING in _painted(pilot), pilot=pilot, description="shell painted")

        release.set()

        await wait_for(lambda: "Top models" in _painted(pilot), pilot=pilot, description="detail painted")
        painted = _painted(pilot)
        assert "Mon" in painted
        assert "≤" in painted


@pytest.mark.parametrize(("size", "every_label"), [((80, 24), False), ((120, 40), True)])
async def test_a_six_week_month_paints_its_last_week_and_a_one_line_legend(
    size: tuple[int, int], every_label: bool
) -> None:
    # August 2026 starts on a Saturday and ends on a Monday: it touches six weeks.
    report = _report(
        _call(date(2026, 8, 15), new_input=1_111_000),
        _call(date(2026, 8, 16), new_input=2_222_000),
        _call(date(2026, 8, 17), new_input=3_333_000),
        _call(date(2026, 8, 18), new_input=4_444_000),
        today=date(2026, 8, 31),
    )
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot, report)

        legend = _text(dialog, "usage-heat-legend")
        assert "\n" not in legend
        assert dialog.query_one("#usage-heat-legend").region.height == 1
        assert ">3.33M" in legend
        assert ("≤" in legend) is every_label
        assert cell_len(legend) <= dialog.query_one(_MonthGrid).size.width
        await wait_for(lambda: ">3.33M" in _painted(pilot), pilot=pilot, description="legend painted whole")
        last_week = [line for line in _grid_text(dialog).split("\n") if line.strip()][-1]
        assert last_week.split()[0] == "31"
        assert dialog.query_one("#usage-calendar-pane").max_scroll_y == 0


async def test_a_failure_on_the_calendar_is_painted_whole_at_the_smallest_size() -> None:
    def _load(_cancel: threading.Event) -> UsageReport:
        raise OSError("disk went away")

    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: dialog._read_failed, pilot=pilot, description="read failed")
        await pilot.press("tab")

        await wait_for(lambda: _FAILED in _painted(pilot), pilot=pilot, description="whole message painted")


async def test_a_day_with_no_calls_shows_zeros_and_no_models() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open_calendar(pilot)
        await pilot.press("up")

        detail = _text(dialog, "usage-calendar-detail")
        assert "Top models" not in detail
        assert [line.split() for line in detail.split("\n") if line.startswith("Calls")] == [["Calls", "0"]]


async def test_every_line_fits_the_smallest_regular_terminal_without_scrolling() -> None:
    report = _report(*[_call(model=f"model-{index}", new_input=1000 + index) for index in range(10)])
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot, report)
        pane = dialog.query_one("#usage-overview-pane")

        assert pane.max_scroll_y == 0
        container = dialog.query_one("#usage-container")
        assert dialog.query_one("#usage-note").region.bottom <= container.content_region.bottom
        assert dialog.query_one(_UsageRows).region.height >= 4
        assert dialog.query_one(_TrendChart).region.height >= 3

        await pilot.press("tab")
        calendar_pane = dialog.query_one("#usage-calendar-pane")
        await wait_for(lambda: calendar_pane.region.height > 0, pilot=pilot, description="calendar laid out")

        assert calendar_pane.max_scroll_y == 0


@pytest.mark.parametrize(
    ("locale", "overview", "calendar"),
    [
        ("en", _OVERVIEW_KEYS, _CALENDAR_KEYS),
        (
            "zh-Hans",
            "←→ 范围  t 日/周  g 分组  ↑↓ 选行",
            "←→↑↓ 选日期  [ ] 换月",
        ),
    ],
)
async def test_the_key_hints_fit_the_border_of_the_smallest_regular_terminal(
    monkeypatch: pytest.MonkeyPatch, locale: str, overview: str, calendar: str
) -> None:
    from chrys.app.tui import i18n as tui_i18n

    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    if locale != "en":
        assert controller.switch_locale(locale).status is LocaleSwitchStatus.EFFECTIVE_CHANGED

    class _LocalizedApp(_HostApp):
        locale_controller = controller

    async with _LocalizedApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot)
        frame_width = dialog.query_one("#usage-container").region.width

        assert _subtitle(dialog) == overview
        assert cell_len(overview) <= frame_width - 6

        await pilot.press("tab")
        await wait_for(lambda: _subtitle(dialog) == calendar, pilot=pilot, description="calendar keys")
        assert cell_len(calendar) <= frame_width - 6


@pytest.mark.parametrize(
    ("locale", "size", "peak"),
    [
        ("en", (120, 40), "peak 83.79M"),
        ("en", (80, 24), "peak 83.79M"),
        ("en", (46, 24), "peak 83.79M"),
        ("en", (42, 40), "83.79M"),
        ("en", (38, 24), ""),
        ("zh-Hans", (56, 24), "峰值 83.79M"),
        ("zh-Hans", (50, 24), "峰值 83.79M"),
    ],
)
async def test_the_trend_line_gives_up_its_label_before_any_of_the_number(
    monkeypatch: pytest.MonkeyPatch, locale: str, size: tuple[int, int], peak: str
) -> None:
    from chrys.app.tui import i18n as tui_i18n

    controller = LocaleController(Settings(locale="en"))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    if locale != "en":
        assert controller.switch_locale(locale).status is LocaleSwitchStatus.EFFECTIVE_CHANGED

    class _LocalizedApp(_HostApp):
        locale_controller = controller

    report = _report(_call(new_input=83_790_000, output=0))
    async with _LocalizedApp().run_test(size=size) as pilot:
        dialog = await _open(pilot, report)
        line = dialog.query_one("#usage-peak")

        await wait_for(lambda: _painted_within(pilot, line) == peak, pilot=pilot, description="the whole peak painted")
        assert all(cell_len(row) <= size[0] for row in _painted(pilot).split("\n")), "nothing pushes past the border"


async def test_the_trend_line_refits_after_a_resize_and_a_trip_to_the_other_tab() -> None:
    report = _report(_call(new_input=83_790_000, output=0))
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot, report)
        line = dialog.query_one("#usage-peak")

        def shows(text: str) -> Callable[[], bool]:
            return lambda: _painted_within(pilot, line) == text

        await wait_for(shows("peak 83.79M"), pilot=pilot, description="room for the label")

        await resize_when_settled(pilot, 42, 24)
        await wait_for(shows("83.79M"), pilot=pilot, description="the label gives way")

        await pilot.press("tab")
        await wait_for(lambda: _active_tab(dialog) == "usage-calendar", pilot=pilot, description="on the calendar")
        await resize_when_settled(pilot, 80, 24)
        await pilot.press("tab")
        await wait_for(shows("peak 83.79M"), pilot=pilot, description="the label returns")


@pytest.mark.parametrize(
    ("milliseconds", "shown"),
    [
        (6_000, "6s"),
        (59_499, "59s"),
        (59_500, "1m"),
        (3_569_000, "59m"),
        (3_570_000, "1.0h"),
        (86_219_000, "23.9h"),
        (86_220_000, "1.0d"),
        (107_640_000, "1.2d"),
        (50_000_000_000, "578.7d"),
    ],
)
def test_the_model_time_takes_the_next_unit_before_rounding_would_show_a_full_one(
    milliseconds: int, shown: str
) -> None:
    assert _model_time(milliseconds) == shown


async def test_a_taller_terminal_lists_more_rows() -> None:
    report = _report(*[_call(model=f"model-{index:02}", new_input=1000 + index, output=0) for index in range(30)])
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        dialog = await _open(pilot, report)
        table = dialog.query_one(_UsageRows)
        before = table.region.height

        await resize_when_settled(pilot, 100, 40)

        await wait_for(lambda: table.region.height > before, pilot=pilot, description="rows grow with the terminal")


async def test_the_reading_message_sits_in_the_middle_of_the_dialog() -> None:
    release = threading.Event()

    def _load(_cancel: threading.Event) -> UsageReport:
        release.wait(30)
        return _sample_report()

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: _READING in _painted(pilot), pilot=pilot, description="message painted")

        lines = _painted(pilot).split("\n")
        row = next(index for index, line in enumerate(lines) if _READING in line)
        column = lines[row].index(_READING)
        frame = dialog.query_one("#usage-container").region
        assert abs(row - (frame.y + frame.height // 2)) <= 1
        assert abs(column + len(_READING) // 2 - (frame.x + frame.width // 2)) <= 1
        assert "Overview" not in _painted(pilot)

        release.set()

        await wait_for(lambda: "Overview" in _painted(pilot), pilot=pilot, description="tabs painted")
        assert _READING not in _painted(pilot)


async def test_the_first_read_starts_only_after_the_shell_has_a_place_on_screen() -> None:
    """Whole-history I/O waits for the loading shell, which the user sees first."""
    seen: list[tuple[int, str]] = []

    async with _HostApp().run_test(size=(100, 30)) as pilot:

        def _load(_cancel: threading.Event) -> UsageReport:
            seen.append((dialog.query_one("#usage-status").region.area, _text(dialog, "usage-status")))
            return _sample_report()

        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: bool(seen), pilot=pilot, description="read started")

        area, status = seen[0]
        assert area > 0
        assert status == _READING


async def test_a_dialog_closed_before_its_first_read_starts_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    loads: list[int] = []
    held: list[Callable[[], None]] = []

    def _load(_cancel: threading.Event) -> UsageReport:
        loads.append(1)
        return _sample_report()

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        real_call_after_refresh = dialog.call_after_refresh

        def _hold_the_read(callback: Callable[..., object], *args: object, **kwargs: object) -> bool:
            if callback == dialog._start_read:
                held.append(dialog._start_read)
                return True
            return real_call_after_refresh(callback, *args, **kwargs)

        monkeypatch.setattr(dialog, "call_after_refresh", _hold_the_read)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: bool(held), pilot=pilot, description="first read waiting for the shell")
        await pilot.press("escape")
        await wait_for(lambda: pilot.app.screen is not dialog, pilot=pilot, description="dialog closed")

        held[0]()

        assert not await wait_until(lambda: bool(loads), timeout=0.2, pilot=pilot)


async def test_the_shell_paints_before_the_read_finishes() -> None:
    release = threading.Event()
    cancels: list[threading.Event] = []

    def _load(cancel: threading.Event) -> UsageReport:
        cancels.append(cancel)
        release.wait(30)
        return _sample_report()

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: bool(cancels), pilot=pilot, description="read started")
        await wait_for(lambda: _READING in _painted(pilot), pilot=pilot, description="overview shell")
        assert _title(dialog) == "Token Usage · reading…"

        await pilot.press("tab")
        await wait_for(lambda: _subtitle(dialog) == _CALENDAR_KEYS, pilot=pilot, description="calendar keys")
        assert _READING in _painted(pilot)

        release.set()
        await wait_for(lambda: _text(dialog, "usage-month") == "2026-10", pilot=pilot, description="read done")
        await wait_for(lambda: _READING not in _painted(pilot), pilot=pilot, description="message gone")


async def test_closing_during_a_read_stops_the_read_and_a_late_result_goes_nowhere() -> None:
    started = threading.Event()
    stopped = threading.Event()

    def _load(cancel: threading.Event) -> UsageReport:
        started.set()
        cancel.wait(30)
        stopped.set()
        return _sample_report()

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(started.is_set, pilot=pilot, description="read started")

        await pilot.press("escape")

        await wait_for(lambda: pilot.app.screen is not dialog, pilot=pilot, description="dialog closed")
        await wait_for(stopped.is_set, pilot=pilot, description="read stopped")
        await pilot.pause()
        assert dialog._report is None


async def test_a_cancelled_read_shows_nothing_and_breaks_nothing() -> None:
    started = threading.Event()

    def _load(cancel: threading.Event) -> UsageReport:
        started.set()
        cancel.wait(30)
        raise TrajectoryScanCancelled

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(started.is_set, pilot=pilot, description="read started")

        await pilot.press("escape")

        await wait_for(lambda: pilot.app.screen is not dialog, pilot=pilot, description="dialog closed")
        assert pilot.app.is_running


async def test_a_failed_read_says_so(caplog: pytest.LogCaptureFixture) -> None:
    def _load(_cancel: threading.Event) -> UsageReport:
        raise OSError("disk went away")

    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = UsageDialog(_load)
        await pilot.app.push_screen(dialog)
        await wait_for(lambda: _FAILED in _painted(pilot), pilot=pilot, description="failure shown")

        assert "Failed to read the usage report" in caplog.text
        assert _title(dialog) == "Token Usage"


def _bold_cells(dialog: UsageDialog, static_id: str) -> str:
    lines = rich_segment_lines(dialog.query_one(f"#{static_id}", Static).render())
    return "\n".join("".join(cell.text for cell in line if cell.style and cell.style.bold) for line in lines)


async def test_only_the_days_total_headings_and_selected_cell_are_bold() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open_calendar(pilot, _sample_report())
        await wait_for(lambda: "Top models" in _painted(pilot), pilot=pilot, description="calendar painted")
        top_models = next(line for line in _painted(pilot).split("\n") if "Top models" in line)
        assert top_models.split("Top models")[1].split()[:2] == ["New", "TPS"]

        detail = _bold_cells(dialog, "usage-calendar-detail")
        assert "2026-10-28 Wed" in detail
        assert "New" in detail
        assert "Top models" in detail
        assert "New input" not in detail
        assert "model-a" not in detail

        lines = rich_segment_lines(dialog.query_one(_MonthGrid).render())
        bold = "".join(cell.text for line in lines for cell in line if cell.style and cell.style.bold)
        assert bold.split() == ["28"]


async def test_the_calendar_note_counts_the_selected_days_unreported_calls() -> None:
    report = _report(_call(), _call(_TODAY - timedelta(days=1), reported=False))
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open_calendar(pilot, report)
        await wait_for(lambda: "no deleted" in _text(dialog, "usage-calendar-detail"), pilot=pilot, description="note")
        assert "unreported" not in _text(dialog, "usage-calendar-detail")

        await pilot.press("left")

        await wait_for(
            lambda: "1 call unreported" in _text(dialog, "usage-calendar-detail"),
            pilot=pilot,
            description="yesterday's note",
        )


@pytest.mark.parametrize("size", [(80, 24), (120, 40)])
async def test_the_note_sits_at_the_foot_of_the_day_panel_one_part_to_a_line(size: tuple[int, int]) -> None:
    report = _report(_call(), _call(reported=False), unreadable=2)
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot, report)
        detail = dialog.query_one("#usage-calendar-detail")
        expected = ["2 logs unreadable", "1 call unreported", "from 10-28", "no deleted sessions/external ACP"]
        await wait_for(
            lambda: _text(dialog, "usage-calendar-detail").split("\n")[-4:] == expected,
            pilot=pilot,
            description="note at the foot",
        )

        assert len(_text(dialog, "usage-calendar-detail").split("\n")) == detail.size.height
        await wait_for(lambda: expected[-1] in _painted(pilot), pilot=pilot, description="last line painted")


@pytest.mark.parametrize(("size", "gap_above_legend"), [((80, 24), 0), ((120, 40), 1), ((160, 55), 1)])
async def test_the_day_panel_and_the_legend_reach_the_bottom_of_the_dialog(
    size: tuple[int, int], gap_above_legend: int
) -> None:
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot)
        bottom = dialog.query_one("#usage-container").content_region.bottom
        grid = dialog.query_one(_MonthGrid)
        legend = dialog.query_one("#usage-heat-legend")

        assert dialog.query_one("#usage-day").region.bottom == bottom
        assert legend.region.bottom == bottom
        assert grid.region.bottom + gap_above_legend == legend.region.y
        assert dialog.query_one("#usage-calendar-pane").max_scroll_y == 0


@pytest.mark.parametrize("size", [(120, 40), (160, 55)])
async def test_the_month_fills_the_grid_up_to_the_week_column_and_the_last_row(size: tuple[int, int]) -> None:
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot)
        grid = dialog.query_one(_MonthGrid)
        await wait_for(lambda: bool(_grid_text(dialog)), pilot=pilot, description="grid rendered")

        lines = _grid_text(dialog).split("\n")
        assert len(lines) == grid.size.height, "the last week ends on the grid's last row"
        assert cell_len(lines[0]) == grid.size.width, "the week total ends at the grid's right edge"
        assert lines[0].endswith("Week")
        sunday = rich_segment_lines(grid.render())[1]
        painted_to = sum(cell_len(cell.text) for cell in sunday[:-1])
        assert grid.size.width - painted_to == 6 + 2, "only the gap and the week column follow Sunday"


async def test_the_calendar_lists_every_model_the_panel_has_room_for_when_first_shown() -> None:
    report = _report(*[_call(model=f"model-{index}", new_input=1000 + index) for index in range(5)])
    async with _HostApp().run_test(size=(120, 40)) as pilot:
        await _open_calendar(pilot, report)

        await wait_for(lambda: "model-0" in _painted(pilot), pilot=pilot, description="every model painted")
        assert "model-4" in _painted(pilot)


@pytest.mark.parametrize(
    ("count", "shown"),
    [
        (999, "999"),
        (9_999, "10.0k"),
        (999_999, "1000k"),
        (1_000_000, "1.00M"),
        (99_994_999, "99.99M"),
        (99_995_000, "100M"),
        (99_999_999, "100M"),
        (100_000_000, "100M"),
        (999_999_999, "1000M"),
        (9_999_999_999, "10000M"),
    ],
)
def test_a_token_count_never_takes_more_than_six_cells(count: int, shown: str) -> None:
    assert _tokens(count) == shown
    assert len(shown) <= 6


def _assert_whole_month(dialog: UsageDialog, pilot: Pilot) -> None:
    grid = dialog.query_one(_MonthGrid)
    lines = _grid_text(dialog).split("\n")
    assert len(lines) == grid.size.height, "no line of the month wrapped"
    assert all(cell_len(line) <= grid.size.width for line in lines)
    assert lines[0].split()[:7] == ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"] or lines[0].split()[0] == "Mon"
    painted = _painted(pilot)
    assert any(all(day in line for day in ("26", "27", "28", "29", "30", "31")) for line in painted.split("\n"))
    assert dialog.query_one("#usage-month-next").region.right <= grid.region.right, "the next-month arrow is in reach"


@pytest.mark.parametrize("size", [(60, 24), (64, 40), (71, 40)])
async def test_a_narrow_terminal_leaves_out_the_day_panel_and_shows_the_whole_month(size: tuple[int, int]) -> None:
    report = _report(_call(new_input=74_220_000))
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot, report)
        panel = dialog.query_one("#usage-day")
        await wait_for(lambda: not panel.display, pilot=pilot, description="panel left out")
        await wait_for(lambda: ">" in _painted(pilot), pilot=pilot, description="legend painted")
        await pilot.pause()

        _assert_whole_month(dialog, pilot)
        assert "74.22M" in _text(dialog, "usage-heat-legend")
        assert "74.22M" in _painted(pilot)

        await resize_when_settled(pilot, 120, 40)

        await wait_for(lambda: panel.display, pilot=pilot, description="panel back")


@pytest.mark.parametrize("size", [(72, 24), (72, 40)])
async def test_the_narrowest_window_with_the_day_panel_still_shows_the_whole_month(size: tuple[int, int]) -> None:
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open_calendar(pilot)
        await wait_for(lambda: "Calls" in _painted(pilot), pilot=pilot, description="panel painted")
        await pilot.pause()

        assert dialog.query_one("#usage-day").display is True
        _assert_whole_month(dialog, pilot)


@pytest.mark.parametrize(("size", "tiles"), [((80, 24), 8), ((72, 24), 7), ((60, 24), 6), ((50, 20), 5)])
async def test_a_narrow_terminal_drops_the_last_tiles_and_keeps_every_number_whole(
    size: tuple[int, int], tiles: int
) -> None:
    report = _report(_call(new_input=71_450_000, output=12_340_000, cache_read=99_990_000))
    async with _HostApp().run_test(size=size) as pilot:
        dialog = await _open(pilot, report)
        all_tiles = list(dialog.query(".usage-kpi").results(Static))

        await wait_for(lambda: sum(tile.display for tile in all_tiles) == tiles, pilot=pilot, description="tiles fit")
        await pilot.pause()

        shown = [tile for tile in all_tiles if tile.display]
        assert shown == all_tiles[:tiles]
        for tile in shown:
            label, value = rich_plain(tile.render()).split("\n")
            assert cell_len(label) <= tile.content_size.width
            assert cell_len(value) <= tile.content_size.width


async def test_a_short_day_panel_keeps_the_notes_that_fit_the_most_telling_first() -> None:
    report = _report(_call(), _call(reported=False))
    async with _HostApp().run_test(size=(80, 16)) as pilot:
        dialog = await _open_calendar(pilot, report)
        detail = dialog.query_one("#usage-calendar-detail")
        await wait_for(lambda: "1 call unreported" in _painted(pilot), pilot=pilot, description="note painted")

        lines = _text(dialog, "usage-calendar-detail").split("\n")
        assert len(lines) == detail.size.height
        assert lines[-1] == "1 call unreported"
        assert "no deleted" not in _painted(pilot)


async def test_an_app_exit_while_the_dialog_mounts_breaks_nothing() -> None:
    """Once the app is exiting, mounting composes nothing, so the dialog has no tabs to set up."""
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        pilot.app.push_screen(UsageDialog(lambda _cancel: _sample_report()))
        pilot.app.exit()


async def test_the_calendar_says_when_some_logs_could_not_be_read() -> None:
    report = replace(_sample_report(), unreadable_logs=2)
    async with _HostApp().run_test(size=(80, 24)) as pilot:
        await _open(pilot, report)
        await pilot.press("tab")

        await wait_for(lambda: "2 logs unreadable" in _painted(pilot), pilot=pilot, description="note painted")


async def test_with_no_calls_read_because_the_logs_are_unreadable_each_tab_says_so() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        await _open(pilot, _report(unreadable=2))

        expected = f"{_EMPTY} · 2 logs unreadable"
        await wait_for(lambda: expected in _painted(pilot), pilot=pilot, description="overview says so")

        await pilot.press("tab")
        await pilot.pause()
        assert expected in _painted(pilot)


async def test_with_no_calls_each_tab_says_so() -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        await _open(pilot, _report())

        await wait_for(lambda: _EMPTY in _painted(pilot), pilot=pilot, description="overview empty")

        await pilot.press("left", "t", "g", "down", "tab", "left", "left_square_bracket")
        await pilot.pause()
        assert _EMPTY in _painted(pilot)


@pytest.mark.parametrize("key", ["q", "escape"])
async def test_q_and_escape_close(key: str) -> None:
    async with _HostApp().run_test(size=(100, 30)) as pilot:
        dialog = await _open(pilot)

        await pilot.press(key)

        await wait_for(lambda: pilot.app.screen is not dialog, pilot=pilot, description=f"closed by {key}")


async def test_opening_from_a_populated_main_screen_does_not_restyle_recompose_or_relayout_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class _Engine(ShutdownOnlyEngine):
        async def shutdown(self) -> None:
            return

    class _EmptyRegistry:
        def list_profiles(self) -> list[object]:
            return []

        def load_all(self) -> None:
            return

        def get(self, _name: str) -> None:
            return None

    monkeypatch.delenv("TEXTUAL_DRIVER", raising=False)
    app = ChrysApp(
        EventBus(),
        _Engine(),  # type: ignore[arg-type]
        settings=Settings(),
        state_store=JsonFileStateStore(tmp_path),
        agent_registry=_EmptyRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=False,
    )

    async with app.run_test(size=(120, 36)) as pilot:
        main_screen = app.screen
        assert isinstance(main_screen, MainScreen)
        panel = main_screen.query_one(ChatPanel)
        transcript = []
        for index in range(24):
            transcript.extend(
                (
                    UserMessage(f"Question {index}\nwith a second line"),
                    AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
                )
            )
        await panel.mount(*transcript)
        await wait_for(
            lambda: (
                len(panel.walk_children()) >= 96
                and all(markdown.source for markdown in panel.query(VirtualizedMarkdown))
            ),
            pilot=pilot,
            description="populated transcript composition",
        )
        main_screen.query_one(InputBar).focus_input()
        await wait_for(
            lambda: main_screen.query_one(InputBar).query_one("#chat-input", TextArea).has_focus,
            pilot=pilot,
            description="composer focus before overlay",
        )
        await wait_for(
            lambda: screen_is_settled(app, main_screen),
            pilot=pilot,
            description="settled underlay layout",
        )

        style_updates: list[bool] = []
        footer_recomposes: list[None] = []
        layout_refreshes: list[None] = []
        monkeypatch.setattr(
            main_screen,
            "update_node_styles",
            lambda animate=True: style_updates.append(animate),
        )
        monkeypatch.setattr(
            main_screen,
            "_refresh_layout",
            lambda *_args, **_kwargs: layout_refreshes.append(None),
        )

        async def record_footer_recompose() -> None:
            footer_recomposes.append(None)

        monkeypatch.setattr(main_screen.query_one(ChrysFooter), "recompose", record_footer_recompose)

        monkeypatch.setattr(usage_report, "load_usage_report", lambda _store, _cancel: _sample_report())
        main_screen._config_actions.open_usage()
        await wait_for(lambda: isinstance(app.screen, UsageDialog), pilot=pilot, description="usage open")
        dialog = app.screen
        assert isinstance(dialog, UsageDialog)
        await wait_for(lambda: "model-b" in _painted(pilot), pilot=pilot, description="usage read")
        await pilot.press("tab", "left", "left_square_bracket", "tab", "right", "t", "g", "down", "tab")
        await wait_for(lambda: "no deleted" in _painted(pilot), pilot=pilot, description="calendar note painted")
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main_screen, pilot=pilot, description="usage closed")

        assert style_updates == []
        assert footer_recomposes == []
        assert layout_refreshes == []
