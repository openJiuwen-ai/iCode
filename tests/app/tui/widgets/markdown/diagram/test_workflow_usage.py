# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A run adds one statistics compartment to agent nodes, stable through updates and completion."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

import pytest
from textual.app import App, ComposeResult

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.widgets.markdown.diagram.model import Direction
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView
from chrys.app.tui.widgets.workflow.projector import WorkflowProjector
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.service.workflows.transcript import NodeUsage
from tests.support.waiting import wait_for


@pytest.mark.parametrize("direction", [Direction.LEFT_RIGHT, Direction.TOP_DOWN])
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_statistics_compartment_and_frozen_elapsed_survive_layout_changes(
    direction: Direction, locale: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = {"nodes": [{"id": "review", "kind": "agent", "agent": {"profile": "QA", "model": "m"}}]}

    class Harness(App):
        def compose(self) -> ComposeResult:
            yield WorkflowGraph()

    async with Harness().run_test(size=(120, 30)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.direction = direction
        graph.show_manifest(manifest, [], locale=LocaleController(Settings(locale=locale)), reserve_usage=True)
        original_height = graph.geometry["review"].height
        started = datetime.now(UTC) - timedelta(seconds=85)
        graph.show_nodes({"review": NodeView("running", running_since=started, usage=NodeUsage())})
        assert graph.geometry["review"].height == original_height
        assert "0" in graph._usage_labels["review"]
        await wait_for(lambda: graph.outer_size.width == 120, pilot=pilot)
        diagram, geometry = graph.diagram, graph.geometry
        compile_graph = create_autospec(graph._compile_manifest, side_effect=graph._compile_manifest)
        monkeypatch.setattr(graph, "_compile_manifest", compile_graph)
        for count in (1, 68, 9999):
            graph.show_nodes({"review": NodeView("running", running_since=started, usage=NodeUsage(count, 2_700_000))})
            assert graph.diagram is diagram and graph.geometry is geometry
        assert compile_graph.call_count == 0
        assert "2.7m" in graph._usage_labels["review"]
        assert "Ctx" not in graph._usage_labels["review"]
        box = graph.geometry["review"]
        y = box.y + box.height - 2 + graph.diagram_origin.y
        assert graph._usage_labels["review"] in graph.render_line(y).text
        separator = graph.render_line(y - 1).text
        assert "├" in separator and "┤" in separator

        graph.show_nodes({"review": NodeView("completed", elapsed_seconds=85, usage=NodeUsage(9999, 2_700_000))})
        elapsed = "1 minute 25 seconds" if locale == "en" else "1 分钟 25 秒"
        assert graph._elapsed_labels == {"review": elapsed}
        graph.advance_animation()
        assert graph._elapsed_labels == {"review": elapsed}
        assert elapsed in graph.render_line(y + 1).text
        graph.toggle_layout()
        assert graph._usage_labels["review"] and graph._elapsed_labels["review"] == elapsed
        assert graph.geometry["review"].height == original_height
        # A node that ran under a second shows milliseconds, in either layout.
        graph.show_nodes({"review": NodeView("completed", elapsed_seconds=0.412, usage=NodeUsage(9999, 2_700_000))})
        subsecond = "412 ms" if locale == "en" else "412 毫秒"
        assert graph._elapsed_labels == {"review": subsecond}
        graph.advance_animation()
        assert graph._elapsed_labels == {"review": subsecond}
        assert subsecond in graph.render_line(y + 1).text
        # Starting a different run clears statistics without changing its geometry.
        graph.show_manifest(manifest, [], locale=LocaleController(Settings(locale=locale)), reserve_usage=True)
        assert graph.geometry["review"].height == original_height
        assert not graph._usage_labels and not graph._elapsed_labels


async def test_a_definition_without_a_run_reserves_nothing_for_statistics() -> None:
    manifest = {
        "nodes": [
            {"id": "review", "kind": "agent", "agent": {"profile": "QA", "model": "m"}},
            {"id": "report", "kind": "python", "callable": {"name": "report"}},
        ]
    }

    class Harness(App):
        def compose(self) -> ComposeResult:
            yield WorkflowGraph()

    async with Harness().run_test(size=(120, 30)) as pilot:
        graph = pilot.app.query_one(WorkflowGraph)
        graph.show_manifest(manifest, [])
        idle = graph.geometry
        # An agent is drawn like any other node: a title and a detail row, no wider than they need.
        assert idle["review"].section_breaks == idle["report"].section_breaks == (1,)
        assert (idle["review"].width, idle["review"].height) == (idle["report"].width, idle["report"].height)
        # Statistics that arrive without a reserved row are not painted over the detail row.
        graph.show_nodes({"review": NodeView("running", usage=NodeUsage(3, 1200))})
        await wait_for(lambda: graph.outer_size.width == 120, pilot=pilot)
        box = idle["review"]
        detail = graph.render_line(box.y + box.height - 2 + graph.diagram_origin.y).text
        assert "QA · m" in detail and graph._usage_labels["review"] not in detail

        # A run reserves the compartment for every agent at once, and only for agents.
        graph.show_manifest(manifest, [], reserve_usage=True)
        running = graph.geometry
        assert running["review"].section_breaks == (1, 3)
        assert running["review"].height == box.height + 2 and running["review"].width > box.width
        assert (running["report"].width, running["report"].height) == (idle["report"].width, idle["report"].height)


def test_node_elapsed_accumulates_execution_and_excludes_manual_retry_wait() -> None:
    projector = WorkflowProjector()
    started = datetime(2026, 9, 16, tzinfo=UTC)
    projector.record(events.WorkflowRunStarted(run_id="r", timestamp=started))
    for seconds, attempt, state in (
        (0, 1, "running"),
        (20, 1, "awaiting_retry"),
        (120, 2, "running"),
        (140, 2, "completed"),
    ):
        projector.record(
            events.WorkflowNodeStateChanged(
                run_id="r",
                node_id="review",
                activation_id="review@iter#1",
                invocation_id="i",
                attempt=attempt,
                state=state,
                timestamp=started + timedelta(seconds=seconds),
            )
        )
    projector.record(
        events.WorkflowRunFinished(run_id="r", outcome="completed", timestamp=started + timedelta(seconds=200))
    )
    run = projector.current
    assert run is not None
    assert run.timings["review@iter#1"].seconds == 40
    assert run.timings["review@iter#1"].running_since is None
