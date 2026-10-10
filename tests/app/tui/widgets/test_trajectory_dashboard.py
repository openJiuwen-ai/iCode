# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Frozen v5.51 trajectory-dashboard structure and display tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import NoReturn
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.color import Color
from textual.geometry import Size
from textual.theme import Theme
from textual.widgets import Tab, Tabs

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.support.gc_freeze import DetachedLruCache, GcFreezeBlockReason
from chrys.app.tui.widgets.chat.session_json import SessionJsonPanel
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.app.tui.widgets.trajectory import DashboardTab, TrajectoryDashboard
from chrys.app.tui.widgets.trajectory.insights import insights_lines
from chrys.app.tui.widgets.trajectory.presentation import RenderContext, ResponsiveTier
from chrys.app.tui.widgets.trajectory.text_view import TrajectoryTextView
from chrys.foundation.config.settings import Settings
from chrys.foundation.trajectory.envelope import SegmentedField
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.service.analytics import TrajectoryAnalyzer, TrajectoryScanCancelled
from tests.app.tui.widgets._trajectory_fixtures import (
    _NS,
    _DashboardApp,
    _in_any_box,
    _StyledDashboardApp,
    _tool,
    _wait_loaded,
    _write_operations,
    _write_p2_operations,
    open_dashboard,
    page_text,
    plain_look,
)
from tests.service.analytics._events import EventLog
from tests.support.waiting import wait_for

# The Insights page as a wide dashboard lays it out.
_WIDE_PAGE = RenderContext(width=220, tier=ResponsiveTier.WIDE)
# A primary colour no page style falls back to.
_THEME_PRIMARY = "#13579b"


class _LocalizedThemedDashboardApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.locale_controller = LocaleController(Settings(locale="zh-Hans"))
        self.register_theme(Theme(name="trajectory-test", primary=_THEME_PRIMARY))
        self.theme = "trajectory-test"

    def compose(self) -> ComposeResult:
        yield TrajectoryDashboard(locale_controller=self.locale_controller)


async def test_dashboard_has_four_clickable_tabs_without_compare_and_placeholders(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(150, 32)) as (dashboard, pilot):
        assert [tab.id for tab in dashboard.query_one("#trajectory-tabs", Tabs).query(Tab)] == [
            DashboardTab.OVERVIEW,
            DashboardTab.TIMELINE,
            DashboardTab.INSIGHTS,
            DashboardTab.SESSION_DATA,
        ]
        assert all(not tab.disabled for tab in dashboard.query(Tab))
        assert all(not isinstance(tab.label, str) for tab in dashboard.query(Tab))
        assert list(dashboard.query("#insights-sub-tabs")) == []
        assert list(dashboard.query("#compare")) == []
        assert list(dashboard.query("#flow-mode-tabs")) == []
        assert dashboard.query_one("#trajectory-tabs", Tabs).size.height == 2
        assert dashboard.styles.background == Color.parse(pilot.app.theme_variables["background"])
        assert dashboard.border_subtitle is not None
        assert dashboard.border_subtitle.endswith(" · session")
        assert "exact" in dashboard.border_subtitle and "unresolved" in dashboard.border_subtitle
        # Legend labels share the subtitle's colour with the session id; only glyphs are coloured.
        legend = Text.from_markup(dashboard.border_subtitle)
        glyph_start = legend.plain.index("✓")
        label_start = legend.plain.index("exact")
        assert any(span.start <= glyph_start < span.end for span in legend.spans)
        assert not any(span.start <= label_start < span.end for span in legend.spans)

        await pilot.click("#insights")
        insights = page_text(dashboard.query_one(TrajectoryTextView))
        assert "Findings" in insights
        assert "Diagnostics" in insights


async def test_insights_renders_all_p2_sections_on_one_page(tmp_path: Path) -> None:
    path = tmp_path / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p2_operations(path)

    async with open_dashboard(path, size=(220, 100), session_id="abcd1234") as (dashboard, pilot):
        analysis = dashboard._analysis
        view = dashboard.query_one(TrajectoryTextView)

        await pilot.click("#insights")
        await wait_for(
            lambda: "Tokens per turn" in page_text(view),
            timeout=10,
            pilot=pilot,
            description="combined insights page render",
        )
        # Captured after the insights render settles: the live-refresh timer only
        # runs on the overview/timeline tabs, so any later bump is an accidental
        # analysis load scheduled by the insights render path.
        load_generation = dashboard._load_generation
        page = page_text(view)
        assert "Findings" in page and "Diagnostics" in page
        assert "Tool activity" in page
        assert _in_any_box(view._lines, "mcp · figma_render")
        assert _in_any_box(view._lines, "Unclassified: 1")
        assert "MCP servers" in page and "figma" in page and "render" in page
        assert _in_any_box(view._lines, "return volume") and _in_any_box(view._lines, "connection wait")
        assert "Skills" in page and "slides" in page and "scripts/render.py" in page
        assert _in_any_box(view._lines, "Skill changed during the session")
        assert "Tokens per turn" in page
        assert "cache creation" in page and "cache hit" in page
        assert "Context re-send cost · top 5" in page
        assert "FILE CHANGES" not in page
        assert "─ TOKENS ─" not in page
        assert (
            page.index("Skills")
            < page.index("MCP servers")
            < page.index("Tool activity")
            < page.index("Context re-send cost · top 5")
            < page.index("Tokens per turn")
            < page.index("Findings")
            < page.index("Diagnostics")
        )
        # The tall diagnostics wall exceeds the pair height gap, so this pair
        # falls back to stacked full-width boxes instead of a padded column.
        assert not any("Findings" in line.plain and "Diagnostics" in line.plain for line in view._lines)
        assert any("Skills" in line.plain and "MCP servers" in line.plain for line in view._lines)
        assert any(
            "Tool activity" in line.plain and "Context re-send cost · top 5" in line.plain for line in view._lines
        )
        assert "input share [" in page
        assert any("input share [" in line.plain and "cache hit [" in line.plain for line in view._lines)
        assert any("█" in line.plain and "%]" in line.plain for line in view._lines)

        assert dashboard._analysis is analysis
        assert dashboard._load_generation == load_generation
        await wait_for(
            lambda: view.max_scroll_x == 0,
            timeout=10,
            pilot=pilot,
            description="combined insights horizontal settle",
        )
        assert max(cell_len(line.plain) for line in view._lines) <= view.scrollable_content_region.width


def test_insights_keeps_unbalanced_pairs_side_by_side_despite_height_gap(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    turn = "4" * 32
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, turn_id=turn, payload={"turn_number": 1})
    for index in range(6):
        _tool(log, str(index) * 32, turn, index, f"tool{index}", "filesystem.read", f"{index}" * 16)
    log.add("turn.finished", 7 * _NS, turn_id=turn, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)

    page = insights_lines(plain_look(), _WIDE_PAGE, TrajectoryAnalyzer().load(path))

    lines = [line.plain for line in page]
    # Six tools make the tools box far taller than the empty context-cost
    # box, yet the pair stays on one row so the tools box does not stretch
    # full-width; the two integration summaries share a row the same way.
    assert any("Tool activity" in line and "Context re-send cost · top 5" in line for line in lines)
    assert any("Skills" in line and "MCP servers" in line for line in lines)
    assert _in_any_box(page, "filesystem.read · tool5")
    assert _in_any_box(page, "This session has no MCP calls.")


def test_insights_describes_context_re_send_rows_by_message_kind(tmp_path: Path) -> None:
    item_id = "7" * 32
    revision_id = "8" * 32
    segment_id = "9" * 32
    exchange_id = "a" * 32
    path = tmp_path / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    revision = log.add(
        "context.revision.recorded",
        _NS,
        operation_id=revision_id,
        parent_operation_id=exchange_id,
        payload={
            "revision_id": revision_id,
            "is_checkpoint": True,
            "item_count": 1,
            "untokenized_item_count": 0,
            "unidentified_item_count": 0,
        },
        segmented_fields=(SegmentedField(field_pointer="/payload/refs", segment_group_id=segment_id, segment_count=1),),
    )
    log.add(
        "event.segment",
        _NS,
        operation_id=None,
        payload={
            "parent_event_id": revision.event_id,
            "field_pointer": "/payload/refs",
            "segment_group_id": segment_id,
            "segment_index": 0,
            "segment_count": 1,
            "encoding": "array_slice",
            "entries": [{"item_id": item_id, "occurrence": 0, "position": 0, "action": "add"}],
        },
    )
    log.span(
        "model.exchange",
        exchange_id,
        2 * _NS,
        3 * _NS,
        start_payload={"context_revision_id": revision_id},
    )
    log.add("turn.finished", 3 * _NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "messages": [
                        {
                            "role": "assistant",
                            "additional_properties": {ANALYTICS_ITEM_ID_KEY: item_id, "_group": {"token_count": 1234}},
                            "contents": [
                                {"type": "function_call", "call_id": "c1", "name": "zsh", "arguments": "{}"},
                                {"type": "function_call", "call_id": "c2", "name": "zsh", "arguments": "{}"},
                                {"type": "function_call", "call_id": "c3", "name": "read_file", "arguments": "{}"},
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    page = insights_lines(plain_look(), _WIDE_PAGE, TrajectoryAnalyzer().load(path))

    assert _in_any_box(page, "tokens × model requests that re-sent the item")  # noqa: RUF001
    head_index = next(
        index
        for index, line in enumerate(page)
        if "assistant message (zsh ×2, read_file) · since turn 1" in line.plain  # noqa: RUF001
    )
    # Two lines per item: the total cost rides the head line in compact
    # units, the cost formula and relative bar follow on the next line.
    assert page[head_index].plain.rstrip(" │").endswith("1.2k")
    detail = page[head_index + 1].plain
    assert "1.2k tok × 1 re-sends" in detail  # noqa: RUF001
    assert "▬" in detail
    assert not any(item_id[:12] in line.plain for line in page)


async def test_session_data_is_only_json_content_and_obeys_lifecycle_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / ".chrys" / "sessions" / "session" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_operations(path)
    calls: list[tuple[str, bool]] = []

    async with _DashboardApp().run_test(size=(120, 30)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        session_json = dashboard.query_one(SessionJsonPanel)
        original_hide = session_json.hide_session_json

        def load_session(_session_id: str) -> None:
            calls.append(("load", session_json.display))

        def hide_session_json() -> None:
            calls.append(("hide", session_json.display))
            original_hide()

        monkeypatch.setattr(session_json, "load_session", load_session)
        monkeypatch.setattr(session_json, "hide_session_json", hide_session_json)
        dashboard.show_session("session", path)
        await _wait_loaded(dashboard, pilot)

        await pilot.click("#session-data")
        await pilot.pause()
        assert dashboard.active_tab is DashboardTab.SESSION_DATA
        assert dashboard.display is True
        assert session_json.display is True
        assert dashboard.query_one(TrajectoryTextView).display is False
        assert calls[-1] == ("load", True)
        assert dashboard.border_subtitle == str(path.parents[1] / "session.json")
        assert session_json.styles.border.top[0] == ""

        await pilot.click("#overview")
        await pilot.pause()
        assert session_json.display is False
        assert calls[-1][0] == "hide"
        assert dashboard.border_subtitle is not None
        assert dashboard.border_subtitle.endswith(" · session")

        await pilot.click("#session-data")
        dashboard.hide_dashboard()
        assert session_json.display is False
        assert calls[-1][0] == "hide"


async def test_tab_activation_moves_focus_into_the_visible_content_view(tmp_path: Path) -> None:
    """Page keys must scroll the activated tab's document without a click inside it."""
    path = tmp_path / ".chrys" / "sessions" / "session" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_operations(path)

    async with open_dashboard(path, size=(120, 30)) as (dashboard, pilot):
        await pilot.click("#session-data")
        await pilot.pause()
        assert pilot.app.focused is dashboard.query_one(SessionJsonPanel)

        await pilot.click("#overview")
        await pilot.pause()
        assert pilot.app.focused is dashboard.query_one(TrajectoryTextView)


async def test_session_data_status_renders_like_the_dashboard_empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog()
    log.coverage()
    log.write(path)

    def label_segment(strip, needle: str):
        segment = next((segment for segment in strip if needle in segment.text), None)
        assert segment is not None, f"{needle!r} missing from rendered row {strip.text!r}"
        return segment

    async with _DashboardApp().run_test(size=(120, 30)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        session_json = dashboard.query_one(SessionJsonPanel)
        view = dashboard.query_one(TrajectoryTextView)
        monkeypatch.setattr(session_json, "_resolve_session_path", lambda _session_id: None)
        dashboard.show_session("session", path)
        await _wait_loaded(dashboard, pilot)
        # Analysis can populate _lines before the display flip gets a layout
        # pass. Wait for the actual viewport pixels, not just the backing text.
        await wait_for(
            lambda: "No completed turns" in view.render_line(view.scrollable_content_region.height // 2).text,
            timeout=5,
            pilot=pilot,
            description="dashboard empty-state label in rendered viewport",
        )
        empty_strip = view.render_line(view.scrollable_content_region.height // 2)
        empty_label = label_segment(empty_strip, "No completed turns")
        empty_hatch = next(segment for segment in empty_strip._segments if segment.text.startswith("╲"))
        empty_filler = next(segment for segment in view.render_line(0)._segments if segment.text.startswith("╲"))

        await pilot.click("#session-data")
        await wait_for(
            lambda: (
                session_json.display
                and "No session file found."
                in session_json.render_line(session_json.scrollable_content_region.height // 2).text
            ),
            timeout=5,
            pilot=pilot,
            description="session-data status label in rendered viewport",
        )
        width = session_json.scrollable_content_region.width
        status_strip = session_json.render_line(session_json.scrollable_content_region.height // 2)
        plain = "".join(segment.text for segment in status_strip._segments)
        status_label = label_segment(status_strip, "No session file found.")
        status_hatch = next(segment for segment in status_strip._segments if segment.text.startswith("╲"))
        # A label-free row is a span-less Text unless the helper adds one;
        # Text.render() would then drop the hatch colour and paint the row in
        # the widget's bright foreground (the defect seen in the real app).
        status_filler = next(
            segment for segment in session_json.render_line(0)._segments if segment.text.startswith("╲")
        )
        json_foreground = session_json.visual_style.rich_style.color

    # Same shape and styling as the trajectory empty state: a padded, centered
    # label between hatch runs, rendered through the same Rich helpers.
    assert status_label.text == " No session file found. "
    assert plain.startswith("╲") and plain.endswith("╲")
    assert cell_len(plain) == width
    left = plain.index(status_label.text)
    right = width - left - cell_len(status_label.text)
    assert abs(left - right) <= 1
    assert status_label.style is not None and empty_label.style is not None
    assert status_label.style.color == empty_label.style.color
    assert status_label.style.bold == empty_label.style.bold
    assert status_hatch.style is not None and empty_hatch.style is not None
    assert status_hatch.style.color == empty_hatch.style.color
    assert empty_filler.style is not None and status_filler.style is not None
    assert status_filler.style.color == empty_hatch.style.color
    assert empty_filler.style.color == empty_hatch.style.color
    assert status_filler.style.color != json_foreground
    # The shared label style must remain muted after theme color resolution.
    assert status_label.style.color is not None
    assert status_label.style.color != json_foreground


async def test_session_data_load_overlays_the_shared_loading_indicator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)
    session_file = tmp_path / "session.json"
    session_file.write_text(json.dumps({"value": 1}), encoding="utf-8")
    started = asyncio.Event()
    release = asyncio.Event()
    original_load = SessionJsonPanel._load_worker

    async def slow_load(
        panel: SessionJsonPanel, path: Path, dark: bool, gutter_color: str | None, generation: int
    ) -> None:
        started.set()
        await release.wait()
        await original_load(panel, path, dark, gutter_color, generation)

    monkeypatch.setattr(SessionJsonPanel, "_load_worker", create_autospec(original_load, side_effect=slow_load))

    async with _DashboardApp().run_test(size=(120, 30)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        session_json = dashboard.query_one(SessionJsonPanel)
        text_view = dashboard.query_one(TrajectoryTextView)
        loading_state = dashboard.query_one("#trajectory-loading-state")
        monkeypatch.setattr(session_json, "_resolve_session_path", lambda _session_id: session_file)
        dashboard.show_session("session", path)
        await _wait_loaded(dashboard, pilot)
        assert loading_state.display is False

        await pilot.click("#session-data")
        await wait_for(
            lambda: started.is_set() and loading_state.display is True,
            timeout=5,
            pilot=pilot,
            description="session data load indicator",
        )
        # The viewer stays displayed so its worker can commit; the shared
        # indicator floats over it on its own layer, leaving the tab strip.
        assert session_json.is_loading is True
        assert session_json.display is True
        assert text_view.display is False
        assert dashboard.query_one(ChrysLoadingIndicator).display is True
        tabs = dashboard.query_one("#trajectory-tabs", Tabs)
        await wait_for(
            lambda: (
                loading_state.region == session_json.region
                and loading_state.region.y == tabs.region.y + tabs.region.height
            ),
            timeout=5,
            pilot=pilot,
            description="loading overlay covers the session viewer",
        )

        release.set()
        await wait_for(
            lambda: not session_json.is_loading and loading_state.display is False,
            timeout=5,
            pilot=pilot,
            description="session data load settles",
        )
        assert session_json.display is True
        assert session_json._plain_lines
        assert text_view.display is False

        # Leaving the tab releases the viewer and never leaves the overlay up.
        await pilot.click("#overview")
        await pilot.pause()
        assert session_json.display is False
        assert loading_state.display is False
        assert text_view.display is True


async def test_vertical_scrollbar_drag_moves_virtualized_content() -> None:
    async with _DashboardApp().run_test(size=(90, 24)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        dashboard.display = True
        view = dashboard.query_one(TrajectoryTextView)
        view.display = True
        view.set_lines([Text(f"operation {index}") for index in range(200)])
        await pilot.pause()
        assert view.max_scroll_y > 0

        x = view.size.width - 1
        await pilot._post_mouse_events([events.MouseDown], view, offset=(x, 1), button=1)
        await pilot._post_mouse_events([events.MouseMove], view, offset=(x, view.size.height - 2), button=1)
        await pilot._post_mouse_events([events.MouseUp], view, offset=(x, view.size.height - 2), button=1)

        assert view.scroll_offset.y > 0


async def test_vertical_scroll_repaints_from_the_absolute_content_line() -> None:
    async with _DashboardApp().run_test(size=(90, 24)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        dashboard.display = True
        view = dashboard.query_one(TrajectoryTextView)
        view.display = True
        view.set_lines([Text(f"operation {index}") for index in range(200)])
        await pilot.pause()

        before = view.render_line(0).text.rstrip()
        view.scroll_to(y=12, animate=False, force=True, immediate=True)
        await pilot.pause()
        after = view.render_line(0).text.rstrip()

        assert before == "operation 0"
        assert after == "operation 12"
        assert before != after
        assert (12, 0, view.scrollable_content_region.width) in view._strips


async def test_render_line_composes_text_base_style_over_widget_background() -> None:
    async with _StyledDashboardApp().run_test(size=(90, 24)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        dashboard.display = True
        view = dashboard.query_one(TrajectoryTextView)
        view.display = True
        view.set_lines([Text("base style", style="#ff0000")])
        await pilot.pause()

        content = next(segment for segment in view.render_line(0) if segment.text.startswith("base style"))

        assert content.style is not None
        assert content.style.color is not None
        assert content.style.color.triplet is not None
        assert content.style.color.triplet.hex == "#ff0000"
        assert content.style.bgcolor is not None
        assert content.style.bgcolor.triplet is not None
        assert content.style.bgcolor.triplet.hex == "#123456"


async def test_render_line_composes_widget_line_spans_and_zebra_styles() -> None:
    async with _StyledDashboardApp().run_test(size=(90, 24)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        dashboard.display = True
        view = dashboard.query_one(TrajectoryTextView)
        view.display = True
        line = Text("base span", style="#ff0000")
        line.stylize("#00ff00 bold", 5, 9)
        line.stylize("on #654321", 0, len(line))
        view.set_lines([line])
        await pilot.pause()

        segments = tuple(segment for segment in view.render_line(0) if segment.text.strip())
        base = next(segment for segment in segments if "base" in segment.text)
        span = next(segment for segment in segments if "span" in segment.text)

        assert base.style is not None
        assert base.style.color is not None
        assert base.style.color.triplet is not None
        assert base.style.color.triplet.hex == "#ff0000"
        assert base.style.bgcolor is not None
        assert base.style.bgcolor.triplet is not None
        assert base.style.bgcolor.triplet.hex == "#654321"
        assert span.style is not None
        assert span.style.color is not None
        assert span.style.color.triplet is not None
        assert span.style.color.triplet.hex == "#00ff00"
        assert span.style.bgcolor is not None
        assert span.style.bgcolor.triplet is not None
        assert span.style.bgcolor.triplet.hex == "#654321"
        assert span.style.bold is True


async def test_theme_and_locale_refresh_invalidate_both_dashboard_cache_levels(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(100, 30)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        # A planted sentinel key: it only has to be hashable and distinct from
        # any real entry, so it deliberately does not mirror the arity of the
        # real presentation key (the cache never validates key shape).
        presentation_marker = (
            DashboardTab.OVERVIEW,
            None,
            None,
            -999,
            1,
            1,
            "theme-marker",
            -999,
        )
        strip_marker = (-999, -999, -999)
        dashboard._presentation_cache[presentation_marker] = ((Text("stale"),), 5)
        view._strips[strip_marker] = view.render_line(0)

        pilot.app.theme = "textual-light"
        await pilot.pause()

        assert presentation_marker not in dashboard._presentation_cache
        assert strip_marker not in view._strips

        dashboard._presentation_cache[presentation_marker] = ((Text("stale"),), 5)
        view._strips[strip_marker] = view.render_line(0)
        dashboard.refresh_localization()

        assert presentation_marker not in dashboard._presentation_cache
        assert strip_marker not in view._strips


@pytest.mark.parametrize(
    ("tab", "dependencies", "title"),
    [
        pytest.param(DashboardTab.OVERVIEW, False, "会话信息", id="overview"),
        pytest.param(DashboardTab.TIMELINE, False, "第 1 轮", id="timeline"),
        pytest.param(DashboardTab.TIMELINE, True, "依赖图 · 第 1 轮", id="dependency-graph"),
        pytest.param(DashboardTab.INSIGHTS, False, "MCP 服务器", id="insights"),
    ],
)
async def test_pages_draw_in_the_dashboard_locale_and_theme(
    tmp_path: Path, tab: DashboardTab, dependencies: bool, title: str
) -> None:
    path = tmp_path / "events.jsonl"
    _write_p2_operations(path)

    async with open_dashboard(path, size=(150, 40), app=_LocalizedThemedDashboardApp()) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        dashboard.query_one("#trajectory-tabs", Tabs).active = tab
        await wait_for(lambda: dashboard.active_tab is tab, timeout=5, pilot=pilot, description=f"{tab} tab active")
        if dependencies:
            dashboard.action_toggle_timeline_dependencies()
        await wait_for(
            lambda: dashboard.active_tab is tab and bool(view._lines) and title in view._lines[0].plain,
            timeout=5,
            pilot=pilot,
            description=f"{tab} page drawn in zh-Hans",
        )

        heading = view._lines[0]
        style = heading.get_style_at_offset(pilot.app.console, heading.plain.index(title))
        assert style.color is not None
        assert style.color.triplet is not None
        assert style.color.triplet.hex == _THEME_PRIMARY


async def test_resize_reflows_presentation_without_reaggregation(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(100, 30)) as (dashboard, pilot):
        assert dashboard._live_timer is not None
        dashboard._live_timer.pause()
        analysis = dashboard._analysis
        generation = dashboard._load_generation
        old_key = dashboard._presentation_key
        old_width = max(cell_len(line.plain) for line in dashboard.query_one(TrajectoryTextView)._lines)

        await pilot.resize_terminal(150, 30)
        # The resize reaches the dashboard as its own widget event after the
        # app-level relayout; poll for the reflow rather than assume one pump.
        await wait_for(
            lambda: dashboard._presentation_key != old_key,
            timeout=5,
            pilot=pilot,
            description="presentation reflow after resize",
        )
        new_width = max(cell_len(line.plain) for line in dashboard.query_one(TrajectoryTextView)._lines)

        assert dashboard._analysis is analysis
        assert dashboard._load_generation == generation
        assert new_width != old_width


def _missing_log(tmp_path: Path) -> Path:
    return tmp_path / "missing.jsonl"


def _coverage_only_log(tmp_path: Path) -> Path:
    path = tmp_path / "events.jsonl"
    log = EventLog()
    log.coverage()
    log.write(path)
    return path


@pytest.mark.parametrize(
    ("prepare_log", "expected_message", "forbidden_fragments"),
    [
        pytest.param(
            _missing_log,
            "No trajectory data is available for this session.",
            ("P2", "P3", "legacy"),
            id="missing_trajectory",
        ),
        pytest.param(
            _coverage_only_log,
            "No completed turns are available.",
            ("total time", "Findings"),
            id="no_completed_turns",
        ),
    ],
)
async def test_hatched_empty_state_renders_on_every_data_tab(
    tmp_path: Path,
    prepare_log: Callable[[Path], Path],
    expected_message: str,
    forbidden_fragments: tuple[str, ...],
) -> None:
    async with open_dashboard(prepare_log(tmp_path), size=(100, 30)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        for tab in (DashboardTab.OVERVIEW, DashboardTab.TIMELINE, DashboardTab.INSIGHTS):
            dashboard.query_one("#trajectory-tabs", Tabs).active = tab
            await pilot.pause()
            text = page_text(view)
            assert expected_message in text
            assert "╲" in text
            assert len(view._lines) > 1
            assert all(fragment.lower() not in text.lower() for fragment in forbidden_fragments)
        assert dashboard.query_one("#timeline-turn-tabs", Tabs).display is False


async def test_session_with_only_corrupt_lines_exposes_diagnostics_without_turns(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("not-json\n", encoding="utf-8")

    async with open_dashboard(path, size=(120, 40)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        assert "No completed turns are available." in page_text(view)
        await pilot.click("#timeline")
        await pilot.pause()
        assert "No completed turns are available." in page_text(view)
        await pilot.click("#insights")
        await wait_for(
            lambda: "Diagnostics" in page_text(view),
            timeout=5,
            pilot=pilot,
            description="zero-turn diagnostics render",
        )
        text = page_text(view)
        assert "Corrupt line" in text
        assert "No completed turns are available." not in text


async def test_short_content_hatch_fills_viewport_and_overflowing_content_does_not(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(220, 100)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        await wait_for(
            lambda: not view.show_vertical_scrollbar and len(view._lines) == view.scrollable_content_region.height,
            timeout=5,
            pilot=pilot,
            description="hatch fill settle",
        )
        assert set(view._lines[-1].plain) == {"╲"}

        await pilot.resize_terminal(220, 24)
        await wait_for(
            lambda: view.show_vertical_scrollbar,
            timeout=5,
            pilot=pilot,
            description="overflow scrollbar settle",
        )
        assert "╲" not in view._lines[-1].plain


async def test_hatch_fill_built_during_resize_transition_does_not_poison_the_cache(tmp_path: Path) -> None:
    """The dashboard resizes before its text view reflows; a fill built for the
    old region must not be served for the settled one."""
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(220, 100)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        await wait_for(
            lambda: not view.show_vertical_scrollbar and len(view._lines) == view.scrollable_content_region.height,
            timeout=5,
            pilot=pilot,
            description="tall fill settle",
        )
        await pilot.resize_terminal(220, 24)
        await wait_for(
            lambda: view.show_vertical_scrollbar,
            timeout=5,
            pilot=pilot,
            description="shrink settle",
        )

        # Replay a grow to a height this session has never rendered: the
        # dashboard's resize handler renders while the text view still has the
        # shrunken region, then the terminal actually grows and only the
        # view's own settle re-render can repair the fill. The dashboard spans
        # the whole test terminal, so its resize event carries the raw size.
        grown_size = Size(220, 90)
        dashboard.on_resize(events.Resize(grown_size, grown_size))
        await pilot.resize_terminal(220, 90)
        await wait_for(
            lambda: not view.show_vertical_scrollbar and len(view._lines) == view.scrollable_content_region.height,
            timeout=5,
            pilot=pilot,
            description="regrown fill settle",
        )
        assert dashboard._available_size == dashboard.size
        assert dashboard._available_size != grown_size
        assert set(view._lines[-1].plain) == {"╲"}


async def test_dashboard_participates_in_gc_freeze_and_releases_hidden_cache(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(100, 30)) as (dashboard, _pilot):
        text_view = dashboard.query_one(TrajectoryTextView)

        assert dashboard.gc_freeze_block_reason() is GcFreezeBlockReason.TRAJECTORY_DASHBOARD_VISIBLE
        dashboard.active_tab = DashboardTab.SESSION_DATA
        assert dashboard.gc_freeze_block_reason() is None
        dashboard.prepare_for_gc_freeze()
        assert isinstance(text_view._strips, DetachedLruCache)
        assert isinstance(dashboard._presentation_cache, DetachedLruCache)
        dashboard.after_gc_freeze()
        dashboard.active_tab = DashboardTab.OVERVIEW
        dashboard.hide_dashboard()
        assert dashboard.gc_freeze_block_reason() is None
        dashboard.prepare_for_gc_freeze()
        assert isinstance(text_view._strips, DetachedLruCache)
        assert isinstance(dashboard._presentation_cache, DetachedLruCache)
        dashboard.after_gc_freeze()
        assert not isinstance(text_view._strips, DetachedLruCache)
        assert dashboard._analyzer is None
        assert dashboard._analysis is None


async def test_tab_switch_pauses_incremental_scan_and_hide_cancels_and_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    stopped = Event()

    def slow_load(
        _analyzer: TrajectoryAnalyzer,
        _path: Path,
        *,
        cancel_event: Event | None = None,
    ) -> NoReturn:
        assert cancel_event is not None
        started.set()
        # Leak guard only: this must stay well clear of the 5s waits below so
        # a loaded runner cannot make the stub time out before the test asks
        # for cancellation. Those waits are the real deadline.
        if not cancel_event.wait(timeout=30):
            raise AssertionError("dashboard scan was not cancelled")
        stopped.set()
        raise TrajectoryScanCancelled

    monkeypatch.setattr(TrajectoryAnalyzer, "load", slow_load)

    async with _DashboardApp().run_test(size=(100, 30)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        dashboard.show_session("session", tmp_path / "events.jsonl")
        await wait_for(started.is_set, timeout=5, pilot=pilot, description="trajectory scan start")
        cancel_event = dashboard._scan_cancel_event
        assert cancel_event is not None
        assert dashboard._live_timer is not None
        assert dashboard._live_timer._active.is_set()

        dashboard.query_one("#trajectory-tabs").active = DashboardTab.INSIGHTS
        await pilot.pause()
        assert not cancel_event.is_set()
        assert not dashboard._live_timer._active.is_set()

        dashboard.hide_dashboard()
        await wait_for(stopped.is_set, timeout=5, pilot=pilot, description="trajectory scan cancellation")
        assert cancel_event.is_set()
        assert dashboard._analyzer is None
        assert dashboard.query_one(TrajectoryTextView)._lines == []


async def test_long_load_shows_loading_indicator_instead_of_empty_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)
    started = asyncio.Event()
    release = asyncio.Event()
    original_load = TrajectoryDashboard._load

    async def slow_load(
        dashboard: TrajectoryDashboard,
        generation: int,
        analyzer: TrajectoryAnalyzer,
        load_path: Path | None,
        cancel_event: Event,
    ) -> None:
        started.set()
        # Hold at the async worker boundary. Textual owns cancellation, so
        # UI assertions cannot strand a thread or race a separate 5s timer.
        await release.wait()
        await original_load(dashboard, generation, analyzer, load_path, cancel_event)

    monkeypatch.setattr(TrajectoryDashboard, "_load", create_autospec(original_load, side_effect=slow_load))

    async with _DashboardApp().run_test(size=(100, 30)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        dashboard.show_session("session", path)
        await wait_for(started.is_set, timeout=5, pilot=pilot, description="trajectory scan start")
        await pilot.pause()
        loading_state = dashboard.query_one("#trajectory-loading-state")
        view = dashboard.query_one(TrajectoryTextView)
        assert loading_state.display is True
        assert dashboard.query_one(ChrysLoadingIndicator).display is True
        assert view.display is False

        # Re-renders provoked while the scan is still running (tab switches,
        # resizes) must not surface the "no data" empty state.
        dashboard.query_one("#trajectory-tabs", Tabs).active = DashboardTab.INSIGHTS
        await pilot.pause()
        await pilot.resize_terminal(110, 32)
        # The App dispatches a resize from a debounce timer, which Pilot's
        # settle barrier does not wait for: without this the checks below
        # could pass before the resize re-render ever ran.
        await wait_for(
            lambda: dashboard.region.width == 110, pilot=pilot, description="dashboard laid out at 110 columns"
        )
        await pilot.pause()
        assert view.display is False
        assert "No trajectory data" not in page_text(view)

        release.set()
        await _wait_loaded(dashboard, pilot)
        await pilot.pause()
        assert loading_state.display is False
        assert view.display is True
        assert "Diagnostics" in page_text(view)


def test_dashboard_builds_its_localized_title_before_an_app_runs() -> None:
    dashboard = TrajectoryDashboard(locale_controller=LocaleController(Settings(locale="zh-Hans")))

    assert dashboard.border_title == "轨迹"


async def test_localized_tab_labels_are_rich_text_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TrajectoryDashboard, "_render_message", lambda self, reference: "label [literal")

    async with _DashboardApp().run_test(size=(100, 30)) as pilot:
        dashboard = pilot.app.query_one(TrajectoryDashboard)
        await pilot.pause()

        assert all(tab.label.plain == "label [literal" for tab in dashboard.query(Tab))
