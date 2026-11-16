# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""F12 respects execution admission/finalization and the selected application mode."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal

import pytest
from textual.widgets import Static, TabbedContent
from textual.widgets._footer import FooterKey

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.trajectory import TrajectoryDashboard
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import (
    WorkflowEngine,
    open_workflow,
    select_workflow_view,
    start_workflow,
    switch_mode,
    workflow_selection,
)


@pytest.mark.parametrize("workflow", [False, True], ids=["chat", "workflow"])
async def test_f12_waits_for_completion_and_lease_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: bool
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    requests: list[events.WorkflowRunRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    async def admitted(event: events.UserMessage) -> None:
        event.started_turn = True

    await bus.subscribe(events.WorkflowRunRequest, requested)
    await bus.subscribe(events.UserMessage, admitted)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        panel = main._workflow_panel
        chat = main.query_one(ChatPanel)
        dashboard = main.query_one(TrajectoryDashboard)
        composer = main.query_one(InputBar)
        if workflow:
            preview = await open_workflow(main, pilot, "demo-workflow")
        else:
            details = main._state.runtime.details
            await bus.publish(
                events.AgentRuntimeUpdated(
                    runtime_details=replace(details, model=replace(details.model, model_id="test-model"))
                )
            )
            # The frontend submit can precede the engine's admission lease.
            main._state.submit.begin("Review this")
            await pilot.press("f12")
            assert not dashboard.foreground and chat.display
            assert main.check_action("toggle_trajectory_dashboard", ()) is False
            main._state.submit.clear()
        composer.replace_draft("Review this")
        if workflow:
            await start_workflow(pilot, "Review this")
        else:
            composer.focus_input()
            await pilot.press("enter")
        if workflow:
            await wait_for(lambda: bool(requests), pilot=pilot)
            # Workflow admission is independently pending until Accepted arrives.
            assert main._workflow.awaiting_engine and engine.snapshot.kind == "idle"
            await pilot.press("f12")
            assert not dashboard.foreground and panel.display
            assert main.check_action("toggle_trajectory_dashboard", ()) is False
            await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
            await bus.publish(
                events.WorkflowRunAccepted(
                    request_id=requests[0].request_id,
                    run_id="run",
                    selection=workflow_selection(main, "workflow-session"),
                )
            )
            await bus.publish(events.WorkflowRunStarted(run_id="run", title=preview.title, manifest=preview.manifest))
        else:
            await wait_for(lambda: main._state.run.agent_running, pilot=pilot)
            await engine.set_execution(ExecutionSnapshot("turn", cancellable=True), main._services.bus)
        await wait_for(lambda: main._execution_binding_busy == (engine.snapshot.kind != "idle"), pilot=pilot)
        await pilot.press("f12")
        assert not dashboard.foreground
        assert main._workflow.workflow_mode is workflow
        assert panel.display is workflow and chat.display is not workflow
        # The handler must also reject direct dispatch from a stale footer action.
        main.action_toggle_trajectory_dashboard()
        assert not dashboard.foreground

        if workflow:
            await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="completed"))
        else:
            await bus.publish(events.InvocationMessage(origin=InvocationOrigin("turn", "", "turn", None), text="Done."))
            await wait_for(lambda: not main._state.run.agent_running, pilot=pilot)
        # Terminal output arrives before saving/draining releases the execution lease.
        await pilot.press("f12")
        assert not dashboard.foreground
        assert main.check_action("toggle_trajectory_dashboard", ()) is False
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await wait_for(lambda: main._execution_binding_busy == (engine.snapshot.kind != "idle"), pilot=pilot)
        # The execution observer schedules chrome refresh on the next UI callback.
        await wait_for(lambda: main._trajectory_binding_available is not workflow, pilot=pilot)
        await pilot.press("f12")
        if workflow:
            assert not dashboard.foreground
            main.action_toggle_trajectory_dashboard()
            assert not dashboard.foreground
        else:
            await wait_for(lambda: dashboard.foreground, pilot=pilot)
            assert not chat.display and not panel.display
            await pilot.press("escape")
            await wait_for(lambda: not dashboard.foreground, pilot=pilot)
        assert panel.display is workflow and chat.display is not workflow
        assert main._workflow.workflow_mode is workflow


async def test_agent_loading_hides_f12_and_its_end_restores_the_footer_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None

        def footer_has_f12() -> bool:
            return any(key.key == "f12" for key in main.query(FooterKey))

        await wait_for(lambda: not main.is_startup_agent_loading() and footer_has_f12(), pilot=pilot)
        # The loading state alone changes: no other binding refresh moves the key.
        main.set_startup_agent_loading(True)
        await wait_for(lambda: not footer_has_f12(), pilot=pilot, description="F12 hidden while loading")
        assert "f12" not in main.active_bindings
        main.set_startup_agent_loading(False)
        await wait_for(footer_has_f12, pilot=pilot, description="F12 back once the agent is ready")
        assert "f12" in main.active_bindings


async def test_workflow_hides_f12_and_switching_back_to_chat_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        dashboard = main.query_one(TrajectoryDashboard)
        await switch_mode(main, pilot)
        await wait_for(lambda: not any(key.key == "f12" for key in main.query(FooterKey)), pilot=pilot)
        assert "f12" not in main.active_bindings
        await pilot.press("f12")
        assert not dashboard.foreground
        preview = await open_workflow(main, pilot, "demo-workflow")
        panel = main._workflow_panel
        tabs = panel.query_one(TabbedContent)
        graph = panel.query_one(WorkflowGraph)
        diagram = graph.diagram
        composer = main.query_one(InputBar)
        composer.replace_draft("Keep this workflow draft")
        # F12 leaves Workflow mode exactly as it was (asserted each time), so every view is tried on one
        # screen; the code view, whose Escape is its own, goes last and stays selected through the switches below.
        views: tuple[Literal["graph", "output", "code"], ...] = ("graph", "output", "code")
        for view in views:
            await select_workflow_view(main, pilot, view)
            badge = str(main.query_one("#mode-badge", Static).content)
            await pilot.press("f12")
            main.action_toggle_trajectory_dashboard()
            assert not dashboard.foreground and not dashboard.display
            assert main._workflow.workflow_mode
            assert str(main.query_one("#mode-badge", Static).content) == badge
            assert panel.display and not main.query_one(ChatPanel).display
            assert not main.query_one(SidebarPanel).query_one(TabbedContent).get_tab("tab-context").display
            assert tabs.active == f"workflow-{view}-tab"
            assert panel.preview is not None and panel.preview.spec_digest == preview.spec_digest
            assert graph.diagram is diagram
            assert composer.value == "Keep this workflow draft"
            assert "f12" not in main.active_bindings

        await switch_mode(main, pilot)
        assert not main._workflow.workflow_mode
        await wait_for(lambda: any(key.key == "f12" for key in main.query(FooterKey)), pilot=pilot)
        assert "f12" in main.active_bindings
        for exit_key in ("escape", "f12"):
            await pilot.press("f12")
            await wait_for(lambda: dashboard.foreground and dashboard.display, pilot=pilot)
            await pilot.press(exit_key)
            await wait_for(lambda: not dashboard.foreground, pilot=pilot)
        assert main.query_one(ChatPanel).display
        # Entering Workflow from an open Chat dashboard closes it and removes F12.
        await pilot.press("f12")
        await wait_for(lambda: dashboard.foreground, pilot=pilot)
        await switch_mode(main, pilot)
        await wait_for(lambda: not any(key.key == "f12" for key in main.query(FooterKey)), pilot=pilot)
        assert main._workflow.workflow_mode and panel.display and not dashboard.foreground
        assert composer.value == "Keep this workflow draft"
        await pilot.press("f12")
        assert not dashboard.foreground
