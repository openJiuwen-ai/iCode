# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Overview page rendering, session-info actions, live refresh and responsive breakpoints of the dashboard."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from rich.cells import cell_len
from rich.style import Style
from textual.geometry import Region, Size

from chrys.app.tui.widgets.trajectory import DashboardTab, ResponsiveTier
from chrys.app.tui.widgets.trajectory import panel as trajectory_panel
from chrys.app.tui.widgets.trajectory.text_view import TrajectoryTextView
from chrys.service.analytics import TrajectoryAnalyzer
from tests.app.tui.widgets._trajectory_fixtures import (
    _NS,
    _in_any_box,
    _write_operations,
    _write_p1_operations,
    open_dashboard,
    page_text,
)
from tests.service.analytics._events import EventLog
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for


def _store_layout_session(tmp_path: Path) -> tuple[Path, Path]:
    """Lay a session folder out the way the store does; returns ``(session_dir, events_path)``."""
    session_dir = tmp_path / "sessions" / "0123456789ab"
    trajectory_dir = session_dir / "trajectory"
    trajectory_dir.mkdir(parents=True)
    path = trajectory_dir / "events.jsonl"
    _write_operations(path)
    (session_dir / "session.json").write_bytes(b"x" * 2048)
    (session_dir / "mutations").mkdir()
    (session_dir / "mutations" / "0001.diff").write_bytes(b"y" * 3072)
    return session_dir, path


async def test_overview_renders_kpi_waterfall_coverage_and_structured_diagnostics(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path, diagnostics=True)

    async with open_dashboard(path, size=(220, 36)) as (dashboard, pilot):
        text = page_text(dashboard.query_one(TrajectoryTextView))

        assert "Time & usage" in text
        assert "Session info ✗" in text
        assert "Where time went" in text
        assert "Parallelism & busy" in text
        assert "KEY METRICS" not in text
        assert "total time" in text
        assert "model" in text and "tools" in text and "wait" in text and "idle" in text
        assert "Busy share (model and tools independent; >100% = parallel work)" in text
        assert "data confidence" in text
        assert "Per-turn time breakdown" in text
        overview_lines = [line.plain for line in dashboard.query_one(TrajectoryTextView)._lines]
        idle_row = next(index for index, line in enumerate(overview_lines) if line.startswith("│ idle"))
        # The waterfall closes with a time ruler on the lanes' cumulative scale.
        assert overview_lines[idle_row + 1].startswith("│ time")
        assert "0s" in overview_lines[idle_row + 1]
        assert "Token usage" in text
        assert "Skill usage" in text
        assert "MCP usage" in text
        assert "Action breakdown" in text
        assert "Failure recovery" in text
        assert "Change verification" in text
        assert "Submission wait (submit → work starts)" in text
        assert "Diagnostics" not in text
        view = dashboard.query_one(TrajectoryTextView)
        await wait_for(
            lambda: view.max_scroll_x == 0,
            timeout=5,
            pilot=pilot,
            description="overview horizontal settle",
        )
        assert max(cell_len(line.plain) for line in view._lines) <= view.scrollable_content_region.width
        assert all(
            any(label in line.plain and any(symbol in line.plain for symbol in "✓~−✗") for line in view._lines)  # noqa: RUF001
            for label in ("model", "tools", "wait", "idle")
        )
        assert "✓ exact" not in text
        assert dashboard.border_subtitle is not None
        assert "exact" in dashboard.border_subtitle and "unresolved" in dashboard.border_subtitle
        assert dashboard.border_subtitle.endswith(" · session")
        assert "cache hit" in text
        assert "cache creation" not in text
        assert "█" in text and "░" in text

        await pilot.click("#insights")
        insight_lines = dashboard.query_one(TrajectoryTextView)._lines
        insights = page_text(view)
        assert "Diagnostics" in insights
        assert _in_any_box(insight_lines, "Corrupt line")
        assert _in_any_box(insight_lines, "Unsupported line")
        assert _in_any_box(insight_lines, "metrics in the affected range degrade to unresolved")
        assert _in_any_box(insight_lines, "after seq")
        assert _in_any_box(insight_lines, "Accounted-prefix seq")
        assert _in_any_box(insight_lines, "response linkage lacks final_exchange_operation_id")
        # Recorded-duration drift collapses into one explanatory summary line
        # instead of a per-span wall of notes.
        assert _in_any_box(insight_lines, "1 span's recorded duration drifts from its lifecycle interval")
        assert _in_any_box(insight_lines, "(wait; up to 100 ms)")
        assert not _in_any_box(insight_lines, "@11111111")
        assert _in_any_box(insight_lines, "Containment wait @66666666")


async def test_overview_session_info_shows_folder_sizes_and_wall_clock_for_store_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capability_checks: list[None] = []

    def can_open_folder() -> bool:
        capability_checks.append(None)
        return True

    monkeypatch.setattr(trajectory_panel, "can_open_in_file_manager", can_open_folder)
    session_dir, path = _store_layout_session(tmp_path)

    async with open_dashboard(path, size=(150, 40)) as (dashboard, _pilot):
        assert capability_checks == [None]
        view = dashboard.query_one(TrajectoryTextView)
        lines = view._lines
        text = page_text(view)

        assert "Session info ✓" in text
        assert "0123456789ab" in text  # the path keeps its tail when cropped
        assert "session.json" in text and "2.0 KB" in text
        assert "3.0 KB" in text  # mutations subtree
        assert "3 files" in text
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text)  # local wall clock
        open_line = next(line for line in lines if "Open folder" in line.plain)
        assert open_line.plain.index("Copy path") < open_line.plain.index("Open folder")
        metas = [span.style.meta for span in open_line.spans if isinstance(span.style, Style)]
        assert {"@click": "copy_session_path"} in metas
        assert {"@click": "open_session_folder"} in metas

        copied: list[str] = []
        notices: list[str] = []
        monkeypatch.setattr(trajectory_panel, "copy_text_to_clipboards", lambda _app, value: copied.append(value))
        monkeypatch.setattr(dashboard, "notify", lambda message, **kwargs: notices.append(message))
        dashboard.query_one(TrajectoryTextView).action_copy_session_path()
        assert copied == [str(session_dir)]
        assert notices == ["Path copied"]

        opened: list[Path] = []
        monkeypatch.setattr(trajectory_panel, "open_in_file_manager", opened.append)
        view.action_open_session_folder()
        assert opened == [session_dir]

        monkeypatch.setattr(
            trajectory_panel,
            "open_in_file_manager",
            lambda folder: (_ for _ in ()).throw(FileNotFoundError("xdg-open")),
        )
        dashboard.open_session_folder()
        assert len(notices) == 2 and "xdg-open" in notices[1]

        monkeypatch.setattr(trajectory_panel, "can_open_in_file_manager", lambda: False)
        dashboard.open_session_folder()
        assert len(notices) == 3 and "current environment" in notices[2]


async def test_overview_session_info_hides_open_folder_over_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trajectory_panel, "can_open_in_file_manager", lambda: False)
    _session_dir, path = _store_layout_session(tmp_path)

    async with open_dashboard(path, size=(150, 40)) as (dashboard, _pilot):
        lines = dashboard.query_one(TrajectoryTextView)._lines

        assert any("0123456789ab" in line.plain for line in lines)
        assert any("Copy path" in line.plain for line in lines)
        assert all("Open folder" not in line.plain for line in lines)
        assert any(
            span.style.meta.get("@click") == "copy_session_path"
            for line in lines
            for span in line.spans
            if isinstance(span.style, Style)
        )
        assert all(
            span.style.meta.get("@click") != "open_session_folder"
            for line in lines
            for span in line.spans
            if isinstance(span.style, Style)
        )


async def test_overview_session_info_degrades_to_placeholders_for_a_loose_events_file(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(150, 40)) as (dashboard, _pilot):
        view = dashboard.query_one(TrajectoryTextView)
        lines = view._lines
        text = page_text(view)

        assert "Session info" in text
        assert dashboard._session_storage is None
        on_disk_line = next(line.plain for line in lines if "on disk" in line.plain)
        assert "—" in on_disk_line
        # The folder line still names the log's parent so "Open folder" stays honest.
        assert tmp_path.name in text


async def test_p1_overview_renders_findings_bottom_panes_and_non_additive_submission_latency(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(240, 400), session_id="abcd1234") as (dashboard, pilot):
        text_view = dashboard.query_one(TrajectoryTextView)
        text = page_text(text_view)

        assert "Findings" not in text
        assert "Unverified change" not in text
        assert "Action breakdown" in text
        for label, value in (("search", "1"), ("read", "3"), ("edit", "2"), ("verify", "1")):
            row = next(line.plain for line in text_view._lines if line.plain.startswith(f"│ {label}"))
            assert value in row
        assert "Failure recovery" in text
        assert any("tool failures" in line.plain and "2/7" in line.plain for line in text_view._lines)
        assert any("repeated identical failures" in line.plain and "1" in line.plain for line in text_view._lines)
        assert "Change verification" in text
        assert any("verified[bold].py" in line.plain and "verified" in line.plain for line in text_view._lines)
        assert any("after_verify.py" in line.plain and "after verify" in line.plain for line in text_view._lines)
        assert any("net_zero.py" in line.plain and "cancelled out" in line.plain for line in text_view._lines)
        assert "Token usage" in text
        assert "No skills were used." in text
        assert "No MCP tools were called." in text
        assert "Submission wait (submit → work starts)" in text
        assert "How long each message waited" in text
        assert any("started a new turn" in line.plain and "2 samples" in line.plain for line in text_view._lines)
        assert any(
            "injected into an ongoing turn" in line.plain and "1 sample" in line.plain for line in text_view._lines
        )
        assert any("never became a turn" in line.plain and "1 sample" in line.plain for line in text_view._lines)
        assert "median" in text and "p90" in text and "slowest" in text
        assert any(line.plain.startswith("│ Turn 1") for line in text_view._lines)
        assert "Σ" not in text
        # The load's display flip commits lines before the view's region reflects the new
        # layout; the fitting re-render lands one refresh later, so wait for the settled frame.
        await wait_for(
            lambda: bool(text_view._lines) and text_view.max_scroll_x == 0,
            timeout=5,
            pilot=pilot,
            description="overview settled first frame",
        )
        assert max(cell_len(line.plain) for line in text_view._lines) <= text_view.scrollable_content_region.width

        await pilot.click("#insights")
        insights_view = dashboard.query_one(TrajectoryTextView)
        insights = page_text(insights_view)
        assert "Findings" in insights
        assert "Unverified change" in insights
        assert "Repeated tool fingerprint" in insights
        assert "Changes cancelled out" in insights
        assert " · deterministic" not in insights
        assert insights.index("Findings") < insights.index("Diagnostics")
        # The ignore interaction no longer exists, so no ignored-count footer.
        assert "ignored finding" not in insights
        assert "Data-integrity notes" in insights


async def test_overview_section_rows_align_both_edges_without_horizontal_overflow(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    # Narrow tier with the box padding still leaving the latency labels intact.
    async with open_dashboard(path, size=(74, 400), session_id="abcd1234") as (dashboard, _pilot):
        view = dashboard.query_one(TrajectoryTextView)
        # Same load-flip transient as the settled-frame wait above: the edge
        # alignment assertions must read the fitted rebuild, not the first commit.
        await wait_for(
            lambda: bool(view._lines) and view.max_scroll_x == 0,
            timeout=5,
            pilot=_pilot,
            description="narrow overview settled first frame",
        )

        elapsed = next(line.plain for line in view._lines if line.plain.startswith("│ total time"))
        search = next(line.plain for line in view._lines if line.plain.startswith("│ search"))
        failures = next(line.plain for line in view._lines if line.plain.startswith("│ tool failures"))
        change = next(line.plain for line in view._lines if line.plain.startswith("│ verified[bold].py"))
        stats = next(line.plain for line in view._lines if line.plain.startswith("│ started"))
        sample = next(line.plain for line in view._lines if line.plain.startswith("│ Turn 1"))

        assert elapsed.endswith("✗ │")
        assert search.endswith("✓ │")
        assert failures.endswith("✓ │")
        assert change.endswith("verified ~ │")
        assert stats.endswith("slowest 2.00 s ✓ │")
        assert sample.endswith("✓ │")
        assert view.max_scroll_x == 0
        assert max(cell_len(line.plain) for line in view._lines) <= view.scrollable_content_region.width


async def test_overview_findings_are_display_only_and_arrow_keys_scroll(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(200, 24), session_id="abcd1234") as (dashboard, pilot):
        await pilot.click("#insights")
        view = dashboard.query_one(TrajectoryTextView)
        text = page_text(view)
        view.focus()
        await wait_for(lambda: view.has_focus, pilot=pilot, description="control focus before interaction")
        await pilot.press("down")
        await pilot.pause()

        assert "Unverified change" in text
        assert "↑/↓ select" not in text
        assert dashboard.active_tab is DashboardTab.INSIGHTS
        assert view.scroll_offset.y > 0


async def test_live_refresh_preserves_scroll_and_tab_switch_resets_it(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(200, 24), session_id="abcd1234") as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        view.scroll_to(y=3, animate=False, force=True, immediate=True)
        await pilot.pause()
        assert view.scroll_offset.y == 3

        analysis = dashboard._analysis
        assert analysis is not None
        # A live session appends events and republishes the same view under a
        # new analysis generation; the reader's scroll position must survive.
        dashboard._analysis = replace(analysis, generation=analysis.generation + 1)
        dashboard._render_active_view()
        await pilot.pause()
        assert view.scroll_offset.y == 3

        await pilot.click("#insights")
        await pilot.pause()
        assert dashboard.active_tab is DashboardTab.INSIGHTS
        assert view.scroll_offset.y == 0


async def test_stale_text_view_region_cannot_widen_the_render_past_the_dashboard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(74, 24), session_id="abcd1234") as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        # The load schedules one more render for after the text view's own
        # layout settles; the first frame the user keeps must fit the view.
        await wait_for(
            lambda: (
                bool(view._lines)
                and view.max_scroll_x == 0
                and max(cell_len(line.plain) for line in view._lines) <= view.scrollable_content_region.width
            ),
            timeout=5,
            pilot=pilot,
            description="trajectory dashboard settled first render",
        )
        available = dashboard._available_width()
        assert 0 < available <= 74
        # The display flip that ends a load publishes lines before the text
        # view's region reflects the new layout; a stale, wider region must
        # not leak oversized lines into the commit.
        stale = Region(0, 0, available + 126, view.scrollable_content_region.height)
        monkeypatch.setattr(TrajectoryTextView, "scrollable_content_region", property(lambda self: stale))
        assert dashboard._content_width() <= available
        dashboard._render_active_view()
        assert max(cell_len(line.plain) for line in view._lines) <= available


def test_log_appends_do_not_bypass_the_storage_scan_throttle(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    log = EventLog()
    log.coverage()
    log.turn(1, 2)
    log.write(path)
    analyzer = TrajectoryAnalyzer()
    previous = analyzer.load(path)
    append = EventLog()
    append.add("turn.started", 10 * _NS, turn_id="5" * 32, payload={"turn_number": 2})
    append.add("turn.finished", 12 * _NS, turn_id="5" * 32, payload={"end_reason": "cancelled", "duration_ms": 0})
    append.write(tmp_path / "append.jsonl", start_sequence=4)
    with path.open("ab") as handle:
        handle.write((tmp_path / "append.jsonl").read_bytes())

    analysis, storage = trajectory_panel._refresh_with_storage(analyzer, collect_storage=False, cancel_event=Event())

    # The append produced a fresh analysis, yet the directory walk stays on
    # the caller's coarse clock; the panel keeps its previous figures.
    assert analysis is not previous
    assert storage is None

    analysis, storage = trajectory_panel._refresh_with_storage(analyzer, collect_storage=True, cancel_event=Event())

    assert storage is not None


async def test_verify_command_change_has_distinct_presentation_identity_and_reaggregates(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(200, 70), session_id="abcd1234") as (dashboard, pilot):
        before = dashboard._analysis
        assert before is not None
        assert before.validation is not None
        assert before.validation.funnel.verify.value == 1

        view = dashboard.query_one(TrajectoryTextView)

        def presentation_key() -> tuple[object, ...]:
            analysis = dashboard._analysis
            region = view.scrollable_content_region
            return (
                DashboardTab.OVERVIEW,
                False,
                None,
                analysis.generation if analysis is not None else -1,
                dashboard._available_width(),
                dashboard._available_height(),
                region.width,
                region.height,
                view.show_vertical_scrollbar,
                view.show_horizontal_scrollbar,
                "cargo test",
                dashboard._presentation_revision,
            )

        dashboard.set_verify_commands("cargo test")
        # The reload hides the text view behind the loading indicator, so the
        # first build after the analysis swap may run before the view's region
        # settles (uncached by design); wait for the settled presentation.
        await wait_for(
            lambda: (
                dashboard._analysis is not None
                and dashboard._analysis is not before
                and dashboard._presentation_cache.get(presentation_key()) is not None
            ),
            timeout=5,
            pilot=pilot,
            description="trajectory verify-command reprojection",
        )

        after = dashboard._analysis
        assert after is not None
        assert after.validation is not None
        assert after.validation.funnel.verify.value == 0
        assert view.scrollable_content_region.height > 0
        assert dashboard._presentation_cache.get(presentation_key()) is not None


async def test_breakpoints_produce_wide_mid_narrow_and_floor_shapes(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(180, 400)) as (dashboard, pilot):
        assert dashboard._analysis is not None

        rendered: dict[ResponsiveTier, str] = {}
        for tier, width in (
            (ResponsiveTier.WIDE, 140),
            (ResponsiveTier.MID, 90),
            (ResponsiveTier.NARROW, 70),
            (ResponsiveTier.FLOOR, 50),
        ):
            await resize_when_settled(pilot, width, 400)
            dashboard._available_size = Size(width, 400)
            dashboard._clear_presentation_cache()
            dashboard._render_active_view()
            assert dashboard.responsive_tier is tier
            view = dashboard.query_one(TrajectoryTextView)
            rendered[tier] = page_text(view)
            assert view.max_scroll_x == 0
            assert max(cell_len(line.plain) for line in view._lines) <= view.scrollable_content_region.width
            if tier in {ResponsiveTier.WIDE, ResponsiveTier.NARROW}:
                section_titles = (
                    "Time & usage",
                    "Where time went",
                    "Parallelism & busy",
                    "Per-turn time breakdown",
                    "Token usage",
                    "Skill usage",
                    "MCP usage",
                    "Action breakdown",
                    "Failure recovery",
                    "Change verification",
                    "Submission wait (submit → work starts)",
                )
                for title in section_titles:
                    assert any(line.plain.startswith("┌") and title in line.plain for line in view._lines)

        assert "Per-turn time breakdown" in rendered[ResponsiveTier.WIDE]
        assert "Token usage" in rendered[ResponsiveTier.WIDE]
        assert "Action breakdown" in rendered[ResponsiveTier.WIDE]
        assert "Failure recovery" in rendered[ResponsiveTier.WIDE]
        assert "Change verification" in rendered[ResponsiveTier.WIDE]
        assert "Per-turn time breakdown" in rendered[ResponsiveTier.MID]
        assert "Token usage" in rendered[ResponsiveTier.MID]
        assert "Action breakdown" in rendered[ResponsiveTier.MID]
        assert "Change verification" in rendered[ResponsiveTier.MID]
        assert "Per-turn time breakdown" in rendered[ResponsiveTier.NARROW]
        assert "Action breakdown" in rendered[ResponsiveTier.NARROW]
        assert all(label in rendered[ResponsiveTier.NARROW] for label in ("search", "read", "edit", "verify"))
        assert "Per-turn time breakdown" not in rendered[ResponsiveTier.FLOOR]
        assert "Terminal too narrow" in rendered[ResponsiveTier.FLOOR]
        assert "Findings" not in rendered[ResponsiveTier.FLOOR]
        assert "cache read" not in rendered[ResponsiveTier.FLOOR]
        assert "Action breakdown" not in rendered[ResponsiveTier.FLOOR]
        assert "Failure recovery" not in rendered[ResponsiveTier.FLOOR]
        assert "Change verification" not in rendered[ResponsiveTier.FLOOR]
        assert "Submission wait" not in rendered[ResponsiveTier.FLOOR]


async def test_settled_scrollbar_narrows_the_content_but_not_the_breakpoint_tier(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    # The dashboard's border takes 2 columns, leaving exactly the
    # MID breakpoint; the Overview overflows 30 rows, so its vertical
    # scrollbar takes one more column from the content.
    async with open_dashboard(path, size=(82, 30)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        await wait_for(
            lambda: (
                view.show_vertical_scrollbar
                and bool(view._lines)
                and max(cell_len(line.plain) for line in view._lines) <= view.scrollable_content_region.width
            ),
            timeout=5,
            pilot=pilot,
            description="overview settled under its vertical scrollbar",
        )
        assert dashboard._available_width() == 80
        assert view.scrollable_content_region.width == 79

        # The tier follows the dashboard's width, so the 79-cell content
        # still lays the KPI sections out side by side.
        assert dashboard.responsive_tier is ResponsiveTier.MID
        assert any("Time & usage" in line.plain and "Where time went" in line.plain for line in view._lines)
        assert view.max_scroll_x == 0


async def test_settled_rerender_keeps_the_dashboard_tier(tmp_path: Path) -> None:
    path = tmp_path / ".chrys" / "sessions" / "abcd1234" / "trajectory" / "events.jsonl"
    path.parent.mkdir(parents=True)
    _write_p1_operations(path)

    async with open_dashboard(path, size=(82, 30)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)
        await click_when_settled(pilot, "#timeline")
        await wait_for(
            lambda: (
                dashboard.active_tab is DashboardTab.TIMELINE
                and not view.show_vertical_scrollbar
                and view.scrollable_content_region.width == 79
            ),
            timeout=5,
            pilot=pilot,
            description="timeline settled without a vertical scrollbar",
        )

        # The Timeline's left padding leaves the view 79 of the dashboard's 80
        # cells. Render the Overview before the view can grow its scrollbar:
        # the first build fills the scrollbar-free 79 cells and overflows, so
        # only the settled re-render can bring every line within 78 cells.
        dashboard.active_tab = DashboardTab.OVERVIEW
        dashboard._render_active_view()

        assert max(cell_len(line.plain) for line in view._lines) <= 78
        assert any("Time & usage" in line.plain and "Where time went" in line.plain for line in view._lines)
