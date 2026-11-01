# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow elapsed time advances without new events or rebuilding the graph."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.widgets import Static

from chrys.app.tui.widgets.workflow import graph as graph_module
from chrys.app.tui.widgets.workflow import panel as panel_module
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, open_workflow


async def test_elapsed_clock_updates_units_and_resets_for_a_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    started_at = datetime(2026, 9, 16, tzinfo=UTC)
    clock = create_autospec(datetime)
    clock.now.return_value = started_at
    monkeypatch.setattr(panel_module, "datetime", clock)
    monkeypatch.setattr(graph_module, "datetime", clock)
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 45)) as pilot:
        app.animation_level = "none"
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        panel = main._workflow_panel
        header = panel.query_one("#workflow-header", Static)
        await wait_for(lambda: str(header.content).endswith("idle"), pilot=pilot)
        panel.run_id = "first"
        await engine.set_execution(ExecutionSnapshot("workflow", "first", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="first", timestamp=started_at, manifest=preview.manifest))
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="read_request",
                activation_id="read_request@iter#1",
                state="running",
                timestamp=started_at,
            )
        )
        await wait_for(lambda: str(header.content).endswith("running (0ms)"), pilot=pilot)
        graph = panel.query_one(WorkflowGraph)
        diagram, geometry = graph.diagram, graph.geometry
        # Only the clock moves: the real controller timer must refresh the label.
        for seconds, display, node_display in (
            (0.412, "412ms", "412 ms"),
            (59, "59s", "59 seconds"),
            (60, "1m", "1 minute"),
            (65, "1m 05s", "1 minute 5 seconds"),
            (3599, "59m 59s", "59 minutes 59 seconds"),
            (3600, "1h", "1 hour"),
            (7380, "2h 03m", "2 hours 3 minutes"),
            (86400, "1d", "1 day"),
            (93600, "1d 02h", "1 day 2 hours"),
        ):
            clock.now.return_value = started_at + timedelta(seconds=seconds)
            await wait_for(lambda display=display: str(header.content).endswith(f"running ({display})"), pilot=pilot)
            await wait_for(
                lambda node_display=node_display: graph._elapsed_labels.get("read_request") == node_display,
                pilot=pilot,
            )
            box = graph.geometry["read_request"]
            y = box.y + box.height - 1 + graph.diagram_origin.y - round(graph.scroll_offset.y)
            assert node_display in graph.render_line(y).text
            assert graph.diagram is diagram and graph.geometry is geometry

        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="write_tour",
                activation_id="write_tour@iter#1",
                state="awaiting_retry",
                timestamp=clock.now.return_value,
            )
        )
        await wait_for(lambda: str(header.content).endswith("awaiting retry"), pilot=pilot)
        clock.now.return_value = started_at + timedelta(days=2, hours=3)
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="write_tour",
                activation_id="write_tour@iter#1",
                state="running",
                timestamp=clock.now.return_value,
            )
        )
        await wait_for(lambda: str(header.content).endswith("running (2d 03h)"), pilot=pilot)

        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="read_request",
                activation_id="read_request@iter#1",
                state="awaiting_retry",
                timestamp=clock.now.return_value,
            )
        )
        await wait_for(lambda: "read_request" in graph._retry_regions, pilot=pilot)
        assert graph._elapsed_labels["read_request"] == "2 days 3 hours"
        frozen = graph._views["read_request"].elapsed_seconds
        clock.now.return_value += timedelta(minutes=1)
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="read_request",
                activation_id="read_request@iter#1",
                state="running",
                attempt=2,
                timestamp=clock.now.return_value,
            )
        )
        await wait_for(lambda: graph._views["read_request"].running_since is not None, pilot=pilot)
        assert graph._views["read_request"].elapsed_seconds == frozen
        assert "read_request" not in graph._retry_regions

        clock.now.return_value += timedelta(seconds=85)
        await bus.publish(
            events.WorkflowRunFinished(run_id="first", outcome="completed", timestamp=clock.now.return_value)
        )
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await wait_for(lambda: str(header.content).endswith("completed"), pilot=pilot)
        assert graph._views["read_request"].elapsed_seconds == frozen + 85
        assert graph._elapsed_labels["read_request"] == "2 days 3 hours"
        assert all(view.running_since is None for view in graph._views.values())
        panel.run_id = "second"
        await engine.set_execution(ExecutionSnapshot("workflow", "second", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunStarted(run_id="second", timestamp=clock.now.return_value, manifest=preview.manifest)
        )
        await wait_for(lambda: str(header.content).endswith("running (0ms)"), pilot=pilot)
        assert not graph._elapsed_labels and not graph._usage_labels
