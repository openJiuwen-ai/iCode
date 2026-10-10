# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Timeline tab rendering, turn navigation, the canvas floor and the dependency-graph toggle of the dashboard."""

from __future__ import annotations

import re
from pathlib import Path

from rich.cells import cell_len
from rich.text import Text
from textual.widgets import Tab, Tabs

from chrys.app.tui.widgets.trajectory import DashboardTab
from chrys.app.tui.widgets.trajectory.insights import _diagnostic_lines
from chrys.app.tui.widgets.trajectory.presentation import RenderContext, ResponsiveTier
from chrys.app.tui.widgets.trajectory.text_view import TrajectoryTextView
from chrys.app.tui.widgets.trajectory.timeline import _operation_identity, timeline_lines
from chrys.foundation.trajectory.event_types import EventType
from chrys.service.analytics import TimelineDiagnosticCode, TrajectoryAnalyzer
from tests.app.tui.widgets._trajectory_fixtures import _NS, _write_operations, open_dashboard, page_text, plain_look
from tests.service.analytics._events import EventLog
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for, wait_until_quiet


async def test_timeline_renders_operations_hierarchy_identity_ruler_and_unresolved_bar(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path, second_turn=True)

    async with open_dashboard(path, size=(150, 32)) as (dashboard, pilot):
        analysis = dashboard._analysis
        assert analysis is not None
        # Both turns live in the events file, so a live refresh landing at any
        # point reproduces the same turn ids and cannot invalidate navigation.
        first_turn, second_turn = analysis.turns
        dashboard.select_turn(second_turn.turn_id)
        dashboard.query_one(TrajectoryTextView).focus()
        await wait_for(
            lambda: dashboard.query_one(TrajectoryTextView).has_focus,
            pilot=pilot,
            description="control focus before interaction",
        )
        await wait_for(
            lambda: any(
                tabs.display and tabs.active == "turn-1"
                for tabs in dashboard.query(Tabs)
                if tabs.id == "timeline-turn-tabs"
            ),
            timeout=5,
            pilot=pilot,
            description="selected timeline turn tab",
        )
        assert dashboard._selected_turn_id == second_turn.turn_id

        turn_tabs = dashboard.query_one("#timeline-turn-tabs", Tabs)
        assert turn_tabs.display is True
        assert [tab.id for tab in turn_tabs.query(Tab)] == ["turn-0", "turn-1"]
        assert [tab.label.plain for tab in turn_tabs.query(Tab)] == ["Turn 1", "Turn 2"]
        assert turn_tabs.active == "turn-1"

        # Arrow keys scroll the timeline; they no longer switch turns.
        await pilot.press("up")
        await pilot.pause()
        assert dashboard._selected_turn_id == second_turn.turn_id

        turn_tabs.active = "turn-0"
        await wait_for(
            lambda: dashboard._selected_turn_id == first_turn.turn_id,
            timeout=5,
            pilot=pilot,
            description="turn tab navigation",
        )
        text = page_text(dashboard.query_one(TrajectoryTextView))

        assert "time" in text
        assert "↑/↓" not in text
        assert re.search(r"Prepare\s+preparation", text)
        assert "Bash (#01234567)" in text
        hook_operation = next(operation for operation in first_turn.operations if operation.family == "hook.operation")
        assert _operation_identity(plain_look(), hook_operation) == "after_tool (register-sess...)"
        assert "after_tool (register-sess...)" in text
        assert "approval" in text
        assert "Explore" in text
        assert "│ Bash" in text
        assert "│ │ approval" in text
        assert "?···" in text
        assert "wait lifecycle has no terminal endpoint" not in text
        wait_diagnostic = next(
            item for item in analysis.diagnostics.timeline_operations if item.identity == "user_input"
        )
        assert wait_diagnostic.code is TimelineDiagnosticCode.MISSING_TERMINAL
        diagnostic_text = "\n".join(line.plain for line in _diagnostic_lines(plain_look(), analysis))
        assert "Turn 1 · user_input @44444444: lifecycle has no terminal endpoint" in diagnostic_text
        assert "idle" not in text.lower()


async def test_timeline_canvas_floor_keeps_columns_and_scrolls_below_71_cells(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path, second_turn=True)

    async with open_dashboard(path, size=(150, 32)) as (dashboard, pilot):
        analysis = dashboard._analysis
        assert analysis is not None
        first_turn = analysis.turns[0]
        dashboard.select_turn(first_turn.turn_id)
        view = dashboard.query_one(TrajectoryTextView)
        view.focus()
        await pilot.pause()

        def bash_line() -> str:
            return next(line.plain for line in view._lines if "Bash (#01234567)" in line.plain)

        def operation_lines() -> list[str]:
            rows = view._lines[2 : 2 + len(first_turn.operations)]
            assert len(rows) == len(first_turn.operations) > 0
            return [line.plain for line in rows]

        def canvas_width() -> int:
            # 71 cells is the narrowest layout whose columns all fit; below it
            # the timeline draws on a fixed 92-cell canvas and scrolls
            # horizontally instead of squeezing the bars away. The page lays
            # out one cell short of the view: its right gap.
            content_width = view.scrollable_content_region.width - 1
            return content_width if content_width >= 71 else max(content_width, 92)

        for width in (150, 90, 71, 70, 50):
            await pilot.resize_terminal(width, 32)
            await wait_for(
                lambda width=width: dashboard.outer_size.width == width and cell_len(bash_line()) == canvas_width(),
                timeout=5,
                pilot=pilot,
                description="timeline canvas resize",
            )
            bash = bash_line()
            assert bash.endswith("4.00 s")
            assert all(cell_len(line) == canvas_width() for line in operation_lines())
            if canvas_width() > view.scrollable_content_region.width:
                await wait_for(
                    lambda: view.max_scroll_x > 0,
                    timeout=5,
                    pilot=pilot,
                    description="timeline horizontal scroll",
                )
            else:
                await wait_for(
                    lambda: view.max_scroll_x == 0,
                    timeout=5,
                    pilot=pilot,
                    description="timeline no horizontal overflow",
                )


async def test_timeline_keeps_a_one_cell_gap_each_side_and_its_scrollbar_against_the_border(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(110, 30)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        def page_width() -> int:
            return max(cell_len(line.plain) for line in view._lines)

        # The Overview overflows this height, so the Timeline opens beside a
        # scrollbar its shorter page has to retire.
        await wait_for(lambda: view.show_vertical_scrollbar, pilot=pilot, description="overview scrollbar")
        await click_when_settled(pilot, "#timeline")
        await wait_for(
            lambda: view.has_class("-timeline") and not view.show_vertical_scrollbar,
            pilot=pilot,
            description="timeline without a scrollbar",
        )
        region = view.scrollable_content_region
        assert region.x == view.region.x + 1
        assert region.right == view.region.right
        assert page_width() == region.width - 1

        await resize_when_settled(pilot, 110, 14)
        await wait_for(
            lambda: view.show_vertical_scrollbar and page_width() == view.scrollable_content_region.width - 1,
            pilot=pilot,
            description="timeline beside its scrollbar",
        )
        # The scrollbar takes the view's last column, right against the border.
        assert view.scrollable_content_region.right == view.region.right - 1
        assert view.region.right == dashboard.region.right - 1


async def test_a_timeline_line_that_just_fits_takes_the_gap_instead_of_scrolling_for_it(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(110, 30)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        def is_note(line: Text) -> bool:
            return line.plain.startswith("Total time excludes")

        def scrollbars() -> tuple[bool, bool]:
            return view.show_horizontal_scrollbar, view.show_vertical_scrollbar

        async def resize_to(width: int) -> None:
            # Every other line is laid out one cell short of the view: its gap.
            await resize_when_settled(pilot, width, 30)
            await wait_for(
                lambda: max(cell_len(line.plain) for line in view._lines if not is_note(line)) == width - chrome - 1,
                pilot=pilot,
                description="timeline laid out for the new width",
            )
            await wait_until_quiet(
                lambda: (scrollbars(), view.scrollable_content_region, view.virtual_size),
                pilot=pilot,
                description="timeline scrollbars settled",
            )

        await click_when_settled(pilot, "#timeline")
        await wait_for(
            lambda: view.has_class("-timeline") and scrollbars() == (False, False),
            pilot=pilot,
            description="timeline without scrollbars",
        )
        chrome = 110 - view.scrollable_content_region.width
        # The closing note keeps its own width whatever the page's.
        note = next(cell_len(line.plain) for line in view._lines if is_note(line))

        # Exactly as wide as the view, the note takes the gap's cell.
        await resize_to(note + chrome)
        assert scrollbars() == (False, False)
        assert view.virtual_size.width == view.scrollable_content_region.width == note

        # One cell wider than the view, it scrolls, and the gap follows it.
        await resize_to(note + chrome - 1)
        assert scrollbars() == (True, False)
        assert view.virtual_size.width == note + 1
        assert len(view._lines) == view.scrollable_content_region.height


def test_timeline_model_run_bar_uses_accent_without_recoloring_model_category(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)
    turn = TrajectoryAnalyzer().load(path).turns[0]
    look = plain_look({"primary": "#0000aa", "accent": "#aa00aa"})

    lines = timeline_lines(look, RenderContext(width=148, tier=ResponsiveTier.WIDE), turn)
    run_line = next(line for line in lines if re.match(r"Model\s+run\s", line.plain))
    cycle_line = next(line for line in lines if re.match(r"Model\s+\u2502 cycle\s", line.plain))

    run_label_style = run_line.get_style_at_offset(look.console, 0)
    run_bar_style = run_line.get_style_at_offset(look.console, run_line.plain.index("▮"))
    cycle_label_style = cycle_line.get_style_at_offset(look.console, 0)
    cycle_bar_style = cycle_line.get_style_at_offset(look.console, cycle_line.plain.index("▮"))

    assert run_label_style.color == cycle_label_style.color
    assert cycle_bar_style.color == cycle_label_style.color
    assert run_bar_style.color == look.semantic_style("accent", "magenta", bold=True).color
    assert run_bar_style.color != run_label_style.color


async def test_interrupted_retry_renders_one_logical_turn_tab(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    first_turn_id = "4" * 32
    retry_turn_id = "5" * 32
    log = EventLog()
    log.coverage()
    log.add(
        EventType.TURN_STARTED,
        0,
        turn_id=first_turn_id,
        payload={"turn_number": 1, "is_retry": False},
    )
    log.add(
        EventType.TURN_FINISHED,
        2 * _NS,
        turn_id=first_turn_id,
        payload={"end_reason": "interrupted", "duration_ms": 2_000},
    )
    log.settled(3 * _NS, drained_scopes=[])
    log.add(
        EventType.TURN_STARTED,
        100 * _NS,
        turn_id=retry_turn_id,
        payload={"turn_number": 1, "is_retry": True},
    )
    log.add(
        EventType.TURN_FINISHED,
        103 * _NS,
        turn_id=retry_turn_id,
        payload={"end_reason": "cancelled", "duration_ms": 3_000},
    )
    log.write(path)

    async with open_dashboard(path, size=(120, 32)) as (dashboard, pilot):
        analysis = dashboard._analysis
        assert analysis is not None
        assert len(analysis.turns) == 1
        assert analysis.turn(retry_turn_id) is analysis.turns[0]
        dashboard.select_turn(retry_turn_id)
        assert dashboard._selected_turn_id == analysis.turns[0].turn_id
        # A repeated request reuses the presentation the first one cached and
        # still lands on the logical turn.
        dashboard.select_turn(retry_turn_id)
        assert dashboard._selected_turn_id == analysis.turns[0].turn_id
        await wait_for(
            lambda: any(
                [tab.label.plain for tab in tabs.query(Tab)] == ["Turn 1"]
                for tabs in dashboard.query(Tabs)
                if tabs.id == "timeline-turn-tabs"
            ),
            timeout=5,
            pilot=pilot,
            description="logical turn tab replacement",
        )
        turn_tabs = dashboard.query_one("#timeline-turn-tabs", Tabs)
        assert [tab.label.plain for tab in turn_tabs.query(Tab)] == ["Turn 1"]


async def test_timeline_opens_on_the_newest_turn(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path, second_turn=True)

    async with open_dashboard(path, size=(150, 32)) as (dashboard, pilot):
        analysis = dashboard._analysis
        assert analysis is not None
        _first_turn, second_turn = analysis.turns
        await click_when_settled(pilot, "#timeline")
        await wait_for(
            lambda: any(tabs.active == "turn-1" for tabs in dashboard.query(Tabs) if tabs.id == "timeline-turn-tabs"),
            timeout=5,
            pilot=pilot,
            description="turn tabs showing the newest turn",
        )

        # The turn tab strip activates its first tab when it mounts without
        # one; the Timeline must have recorded the newest turn by then.
        assert dashboard._selected_turn_id == second_turn.turn_id
        assert dashboard.query_one(TrajectoryTextView)._lines[0].plain.startswith("Turn 2 ")


async def test_selecting_a_turn_the_analysis_lacks_shows_the_newest_turn(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path, second_turn=True)
    missing_turn_id = "f" * 32

    async with open_dashboard(path, size=(150, 32)) as (dashboard, _pilot):
        analysis = dashboard._analysis
        assert analysis is not None
        first_turn, second_turn = analysis.turns
        assert analysis.turn(missing_turn_id) is None
        view = dashboard.query_one(TrajectoryTextView)
        dashboard.select_turn(first_turn.turn_id)
        assert view._lines[0].plain.startswith("Turn 1 ")

        dashboard.select_turn(missing_turn_id)
        assert dashboard._selected_turn_id == second_turn.turn_id
        assert view._lines[0].plain.startswith("Turn 2 ")
        # The repeated request hits the presentation the first one cached.
        dashboard.select_turn(missing_turn_id)
        assert dashboard._selected_turn_id == second_turn.turn_id
        assert view._lines[0].plain.startswith("Turn 2 ")


def _write_first_of_two_turns(path: Path) -> bytes:
    """Write only the first turn of the two-turn log; returns the second turn's lines to append later."""
    two_turns = path.with_name("two-turns.jsonl")
    _write_operations(two_turns, second_turn=True)
    one_turn = path.with_name("one-turn.jsonl")
    _write_operations(one_turn)
    lines = two_turns.read_bytes().splitlines(keepends=True)
    first_turn_lines = len(one_turn.read_bytes().splitlines())
    path.write_bytes(b"".join(lines[:first_turn_lines]))
    return b"".join(lines[first_turn_lines:])


def _append(path: Path, data: bytes) -> None:
    with path.open("ab") as handle:
        handle.write(data)


async def test_timeline_stays_on_its_turn_when_a_live_session_adds_one(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    second_turn = _write_first_of_two_turns(path)

    async with open_dashboard(path, size=(150, 32)) as (dashboard, pilot):
        analysis = dashboard._analysis
        assert analysis is not None
        (first_turn,) = analysis.turns
        await click_when_settled(pilot, "#timeline")
        await wait_for(
            lambda: dashboard.active_tab is DashboardTab.TIMELINE and dashboard._selected_turn_id is not None,
            pilot=pilot,
            description="timeline tab showing a turn",
        )
        # The Timeline opens on the session's only turn.
        assert dashboard._selected_turn_id == first_turn.turn_id

        _append(path, second_turn)
        await wait_for(
            lambda: dashboard._analysis is not None and len(dashboard._analysis.turns) == 2,
            timeout=5,
            pilot=pilot,
            description="live refresh picking up the appended turn",
        )
        await wait_for(
            lambda: any(
                # The replacement strip sets its active tab when it mounts,
                # after its tabs can already be queried.
                [tab.id for tab in tabs.query(Tab)] == ["turn-0", "turn-1"] and bool(tabs.active)
                for tabs in dashboard.query(Tabs)
                if tabs.id == "timeline-turn-tabs"
            ),
            timeout=5,
            pilot=pilot,
            description="turn tabs for both turns",
        )

        # The turn the reader opened stays selected: a new turn does not move them.
        assert dashboard._selected_turn_id == first_turn.turn_id
        assert dashboard.query_one("#timeline-turn-tabs", Tabs).active == "turn-0"
        assert dashboard.query_one(TrajectoryTextView)._lines[0].plain.startswith("Turn 1 ")


async def test_space_toggles_timeline_between_time_axis_and_dependency_graph(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    _write_operations(path)

    async with open_dashboard(path, size=(150, 40)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        def turn_tabs_visible() -> bool:
            # Lines commit before the queued remove/mount of the turn tabs.
            # During replacement there may be no matching widget at all.
            return any(tabs.display for tabs in dashboard.query(Tabs) if tabs.id == "timeline-turn-tabs")

        await pilot.click("#timeline")
        await wait_for(
            lambda: "Space: dependency graph" in page_text(view) and turn_tabs_visible(),
            timeout=10,
            pilot=pilot,
            description="timeline render",
        )
        timeline = page_text(view)
        assert "Turn 1" in timeline
        assert "Dependency graph" not in timeline
        assert dashboard.query_one("#timeline-turn-tabs", Tabs).display

        await pilot.press("space")
        await wait_for(
            lambda: "Dependency graph · turn 1" in page_text(view) and turn_tabs_visible(),
            timeout=10,
            pilot=pilot,
            description="dependency graph render",
        )
        graph = page_text(view)
        assert "⇠" in graph
        assert "Space: timeline" in graph
        assert "after_tool (register-sess...)" in graph
        assert dashboard.query_one("#timeline-turn-tabs", Tabs).display

        await pilot.press("space")
        await wait_for(
            lambda: "Space: dependency graph" in page_text(view) and turn_tabs_visible(),
            timeout=10,
            pilot=pilot,
            description="timeline restored after second space",
        )
        assert "Dependency graph" not in page_text(view)


async def test_timeline_preserves_separator_after_full_width_category(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    approval_id = "a" * 32
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add(
        EventType.APPROVAL_REQUESTED,
        0,
        payload={"approval_request_id": approval_id},
    )
    log.add(
        EventType.APPROVAL_RESOLVED,
        _NS,
        payload={"approval_request_id": approval_id},
    )
    log.add("turn.finished", _NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)

    async with open_dashboard(path, size=(150, 32)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        await pilot.click("#timeline")
        await wait_for(
            lambda: "Space: dependency graph" in page_text(view),
            timeout=10,
            pilot=pilot,
            description="full-width timeline category render",
        )
        timeline_approval = next(line.plain for line in view._lines if line.plain.startswith("Approval"))
        assert timeline_approval.startswith("Approval approval")

        await pilot.press("space")
        await wait_for(
            lambda: "Dependency graph" in page_text(view),
            timeout=10,
            pilot=pilot,
            description="full-width dependency category render",
        )
        graph_approval = next(line.plain for line in view._lines if line.plain.startswith("Approval"))
        assert graph_approval.startswith("Approval ┄ approval")


async def test_dependency_graph_marks_adjacent_only_operations_instead_of_fabricating_edges(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "a" * 32,
        0,
        _NS,
        start_payload={"tool_name": "first", "tool_kind": "filesystem.read", "argument_fingerprint": "1" * 16},
    )
    log.span(
        "tool.operation",
        "b" * 32,
        _NS,
        2 * _NS,
        start_payload={"tool_name": "second", "tool_kind": "filesystem.read", "argument_fingerprint": "2" * 16},
    )
    log.add("turn.finished", 2 * _NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)

    async with open_dashboard(path, size=(150, 40)) as (dashboard, pilot):
        view = dashboard.query_one(TrajectoryTextView)

        await pilot.click("#timeline")
        await pilot.press("space")
        await wait_for(
            lambda: "Dependency graph" in page_text(view),
            timeout=10,
            pilot=pilot,
            description="dependency graph render for adjacency fixture",
        )
        lines = [line.plain for line in view._lines]
        body = "\n".join(line for line in lines if "adjacent only" not in line)
        assert "⇠" not in body
        assert "└→" not in body
        first_line = next(line for line in lines if "first (#" in line)
        second_line = next(line for line in lines if "second (#" in line)
        assert "┄" in first_line
        assert "┄" in second_line
