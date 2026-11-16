# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Live workflow input admission, output navigation and node repainting."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.geometry import Region
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.app.tui.screens.main._workflow_support import (
    WorkflowEngine,
    open_workflow,
    select_workflow_view,
    workflow_selection,
)
from tests.orchestration.workflows._hosting import make_project
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for


@pytest.mark.parametrize("submit", ["button", "keyboard"])
async def test_running_workflow_uses_input_dialog_and_preserves_chat_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submit: str
) -> None:
    from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
    from chrys.app.tui.widgets.editor import MessageEditor

    monkeypatch.chdir(make_project(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    requests = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        composer = main.query_one(InputBar)
        composer.replace_draft("chat draft")
        assert not composer.display
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        app.screen.query_one(MessageEditor).load_text("  review this\n")
        if submit == "keyboard":
            await pilot.press("ctrl+enter")
        else:
            await click_when_settled(pilot, "#workflow-input-start")
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        assert requests[0].input_text == "  review this\n"
        graph = main.query_one(WorkflowGraph)
        await wait_for(lambda: app.focused is graph, pilot=pilot)
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=requests[0].request_id, run_id="run", selection=workflow_selection(main)
            )
        )
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        first_node = preview.manifest["nodes"][0]["id"]
        await bus.publish(
            events.WorkflowNodeStateChanged(run_id="run", node_id=first_node, activation_id=first_node, state="running")
        )
        await wait_for(lambda: graph._views.get(first_node, NodeView()).state == "running", pilot=pilot)
        assert not graph.selected_node
        box = graph.geometry[first_node]
        running_color = graph.get_component_rich_style("workflow-node--running").color
        assert any(span.start == box.x and span.style.color == running_color for span in graph.cell_styles[box.y])
        await wait_for(lambda: main.query_one("#workflow-start", Button).disabled, pilot=pilot)
        main._on_workflow_start()
        assert app.screen is main and composer.value == "chat draft"
        assert not main.query_one(ChatPanel).query("AgentMessage")
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="completed"))
        await wait_for(lambda: not main.query_one("#workflow-start", Button).disabled, pilot=pilot)


async def test_node_completion_and_next_start_repaint_after_details_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 48)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        cards = [ToolCall(f"chat-{i}", "read_file", args={"path": "file.py"}) for i in range(20)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("content\n" * 10)
        preview = await open_workflow(main, pilot, "demo-workflow")
        panel = main._workflow_panel
        graph = panel.query_one(WorkflowGraph)
        panel.run_id = "run"
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", title=preview.title, manifest=preview.manifest))
        reviews = ("architecture", "entry_points", "conventions")
        for node in reviews:
            await bus.publish(
                events.WorkflowNodeStateChanged(run_id="run", node_id=node, activation_id=node, state="running")
            )
        await wait_for(lambda: graph._views.get("conventions", NodeView()).state == "running", pilot=pilot)
        graph.select_node("conventions")
        graph.focus()
        await pilot.press("enter")
        await wait_for(lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted, pilot=pilot)
        for node in (*reviews, "merge_findings"):
            await bus.publish(
                events.WorkflowNodeStateChanged(run_id="run", node_id=node, activation_id=node, state="completed")
            )
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run", node_id="write_tour", activation_id="write_tour", state="running"
            )
        )
        await pilot.press("escape")
        await wait_for(
            lambda: app.screen is main and graph._views.get("write_tour", NodeView()).state == "running", pilot=pilot
        )
        assert graph.selected_node == "conventions" and not graph._hovered_node
        for node in reviews:
            assert graph._views[node].state == "completed"
        graph.select_node("write_tour")
        await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)
        diagram, geometry = graph.diagram, graph.geometry
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        for node, component in (
            ("conventions", "workflow-node--success"),
            ("write_tour", "workflow-node--selected"),
        ):
            box = graph.geometry[node]
            # Parallel reviewers need not share a viewport with the later verdict.
            graph.scroll_to_region(
                Region(box.x + graph.diagram_origin.x, box.y + graph.diagram_origin.y, box.width, box.height),
                animate=False,
                immediate=True,
                force=True,
            )
            y = box.y + 1 + graph.diagram_origin.y - round(graph.scroll_offset.y)
            assert 0 <= y < graph.size.height
            strips = graph.render_lines(Region(0, y, graph.size.width, 1))
            expected = graph.get_component_rich_style(component).color
            assert expected is not None
            assert any(
                segment.text == node
                and segment.style
                and segment.style.color
                and segment.style.color.get_truecolor(app.ansi_theme) == expected.get_truecolor(app.ansi_theme)
                for strip in strips
                for segment in strip
            )
        scroll, frame = graph.scroll_offset, graph._animation_frame
        await wait_for(lambda: graph._animation_frame != frame, pilot=pilot)
        assert graph.diagram is diagram and graph.geometry is geometry and graph.scroll_offset == scroll
        assert layout.call_count == 0
        await bus.publish(
            events.WorkflowNodeOutput(
                run_id="run",
                node_id="write_tour",
                activation_id="write_tour",
                kind="emit",
                summary_text="Status [literal]",
            )
        )
        await select_workflow_view(main, pilot, "output")
        await wait_for(
            lambda: "Status [literal]" in str(panel.query_one("#workflow-status-output", Static).content), pilot=pilot
        )
        assert not panel.query_one("#workflow-graph-tab").display
        app.save_screenshot("workflow-output.svg", path=str(tmp_path))
        await pilot.press("escape")
        await wait_for(lambda: panel.query_one("#workflow-graph-tab").display, pilot=pilot)
        app.save_screenshot("workflow-running.svg", path=str(tmp_path))
