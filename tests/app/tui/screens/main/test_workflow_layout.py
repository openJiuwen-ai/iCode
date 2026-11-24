# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The workflow layout control reflows the graph while retaining live node state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from rich.cells import cell_len
from textual.widgets import Button

from chrys.app.tui.widgets.markdown.diagram.model import Direction
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import RetryTarget
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.orchestration.workflows._hosting import make_workdir, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled, resize_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, select_workflow_view

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_layout_button_preserves_live_state_and_retry_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(
        project,
        "layout",
        python_workflow(
            "def first(value):\n    return value\ndef second(value):\n    return value\n", "first", "second"
        ),
    )
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus, settings=Settings(locale=locale))
    async with app.run_test(size=(120, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "layout")
        panel = main._workflow_panel
        graph = panel.query_one(WorkflowGraph)
        toggle = panel.query_one("#workflow-layout", Button)
        vertical_label = "↕ Vertical" if locale == "en" else "↕ 纵向排列"
        horizontal_label = "↔ Horizontal" if locale == "en" else "↔ 横向排列"
        assert str(toggle.label) == vertical_label
        assert graph.direction == Direction.LEFT_RIGHT
        assert graph.geometry["second"].x > graph.geometry["first"].x
        assert panel.query_one("#workflow-start", Button).variant == "success"
        panel.run_id = "run"
        main._workflow.session_view._run_ids = ["run"]
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        started = datetime.now(UTC) - timedelta(seconds=85)
        failure = events.WorkflowNodeStateChanged(
            run_id="run", node_id="second", activation_id="second@iter#1", attempt=2, state="awaiting_retry"
        )
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        await bus.publish(
            events.WorkflowNodeStateChanged(run_id="run", node_id="first", state="running", timestamp=started)
        )
        await bus.publish(failure)
        await wait_for(lambda: "second" in graph._retry_regions and "first" in graph._elapsed_labels, pilot=pilot)
        graph.select_node("second")
        states = dict(graph._views)
        await resize_when_settled(pilot, 80, 42)
        await wait_for(lambda: graph.outer_size.width == panel.content_size.width, pilot=pilot)

        for direction in (Direction.TOP_DOWN, Direction.LEFT_RIGHT):
            await wait_for(lambda: not toggle.has_class("-active"), pilot=pilot)
            toggle.scroll_visible(animate=False, immediate=True)
            await wait_for(lambda: toggle.region.right <= panel.content_region.right, pilot=pilot)
            await click_when_settled(pilot, toggle)
            await wait_for(lambda direction=direction: graph.direction == direction, pilot=pilot)
            assert str(toggle.label) == (vertical_label if direction == Direction.LEFT_RIGHT else horizontal_label)
            assert toggle.content_size.width >= cell_len(str(toggle.label))
            assert toggle.render_line(0).text.strip() == str(toggle.label)
            assert toggle.region.right <= panel.content_region.right
            buttons = list(panel.query("#workflow-controls Button"))
            # Result stays hidden until a run finishes with outputs; a hidden button has no region.
            assert [button.id for button in buttons if not button.visible] == ["workflow-result"]
            assert all(button.region.height == 3 for button in buttons if button.visible)
            first, second = graph.geometry["first"], graph.geometry["second"]
            assert (second.x > first.x) if direction == Direction.LEFT_RIGHT else (second.y > first.y)
            assert graph.selected_node == "second" and graph._views == states
            assert graph._views["first"].running_since == started
            assert "first" in graph._elapsed_labels
            assert graph._views["second"].retry == RetryTarget(failure.run_id, failure.activation_id, failure.attempt)
            assert not panel.query_one("#workflow-stop", Button).disabled
            assert panel.query_one("#workflow-start", Button).disabled
            app.save_screenshot(f"workflow-layout-{direction.value}.svg", path=str(tmp_path))

        await select_workflow_view(main, pilot, "output")
        await select_workflow_view(main, pilot, "graph")
        assert graph.direction == Direction.LEFT_RIGHT
        region = graph._retry_regions["second"]
        graph.scroll_to_region(region, animate=False, force=True)
        await wait_for(
            lambda: (
                0
                <= region.x + graph.diagram_origin.x - round(graph.scroll_offset.x)
                < graph.scrollable_content_region.width
            ),
            pilot=pilot,
        )
        async with capture_event_sequence(bus, events.WorkflowNodeRetryRequest) as requests:
            assert await pilot.click(
                graph,
                offset=(
                    region.x + graph.diagram_origin.x - round(graph.scroll_offset.x) + graph.gutter.left,
                    region.y + graph.diagram_origin.y - round(graph.scroll_offset.y) + graph.gutter.top,
                ),
            )
            await wait_for(lambda: bool(requests), pilot=pilot)
        assert isinstance(requests[0], events.WorkflowNodeRetryRequest)
        assert requests[0].activation_id == failure.activation_id
        assert graph._views["second"].retry_pending
        await wait_for(lambda: not toggle.has_class("-active"), pilot=pilot)
        await click_when_settled(pilot, toggle)
        assert graph._views["second"].retry_pending
        await click_when_settled(pilot, graph, offset=(0, 0))
        assert not graph.selected_node
        await wait_for(lambda: not toggle.has_class("-active"), pilot=pilot)
        await click_when_settled(pilot, toggle)
        assert not graph.selected_node
        app.locale_controller.switch_locale("zh-Hans" if locale == "en" else "en")
        await wait_for(lambda: str(toggle.label) == ("↕ 纵向排列" if locale == "en" else "↕ Vertical"), pilot=pilot)
        assert toggle.render_line(0).text.strip() == str(toggle.label)
