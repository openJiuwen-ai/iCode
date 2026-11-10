# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The retry fixture through the real worker, scheduler, and TUI."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from textual.containers import VerticalScroll
from textual.widgets import Button, Static, TabbedContent, Tabs

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.service.llm.mock import MockChatClient
from tests.app.tui.screens.main._workflow_support import start_workflow
from tests.orchestration.workflows._hosting import make_host, make_project, patch_runtime, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import confirm_workflow_cancel, open_workflow, select_workflow_view

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from textual.pilot import Pilot

    from chrys.app.tui.app import ChrysApp
    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.orchestration.session_host import ChrysSessionHost

PLAYGROUND_SOURCE = Path(__file__).resolve().parents[4] / "service/workflows/fixtures/retry_playground.py"
# The fixture paces itself for a person watching. These tests follow its events, so a slow
# runner spends its per-test budget on the two real workers and the TUI, not on waiting.
# No line ending in the needle: a Windows checkout gives the fixture CRLF.
_DEMO_PACE, _TEST_PACE = b"PACE = 0.8", b"PACE = 0.1"


@dataclass
class _AwaitingRetry:
    """The playground, stopped where flaky_check has used up its automatic attempts."""

    app: ChrysApp
    pilot: Pilot
    host: ChrysSessionHost
    main: MainScreen
    client: MockChatClient
    states: list[events.WorkflowNodeStateChanged]


@contextlib.asynccontextmanager
async def _playground_awaiting_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_AwaitingRetry]:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = PLAYGROUND_SOURCE.read_bytes()
    assert source.count(_DEMO_PACE) == 1
    write_workflow(project, "test_workflow_playground", source.replace(_DEMO_PACE, _TEST_PACE))
    client = MockChatClient(responses=[])
    patch_runtime(monkeypatch, [client])
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    app = make_chrys_app(tmp_path / "sessions", engine=host.engine, event_bus=host.event_bus)
    states: list[events.WorkflowNodeStateChanged] = []

    async def state_changed(event: events.WorkflowNodeStateChanged) -> None:
        states.append(event)

    await host.event_bus.subscribe(events.WorkflowNodeStateChanged, state_changed)
    try:
        async with app.run_test(size=(140, 48)) as pilot:
            await host.start()
            main = app._main_screen
            assert main is not None
            await wait_for(lambda: not main._state.run.agent_loading and app.screen is main, pilot=pilot)
            await open_workflow(main, pilot, "test_workflow_playground")
            await start_workflow(pilot)
            await wait_for(lambda: any(state.state == "awaiting_retry" for state in states), pilot=pilot, timeout=15)
            run = main._workflow.session_view.projector.current
            assert run is not None and run.finished is None
            assert run.nodes["flaky_check"].attempt == 2
            assert run.nodes["flaky_check"].state == "awaiting_retry"
            assert run.nodes["healthy_check"].state == "completed"
            assert "summary" not in run.nodes or run.nodes["summary"].state != "completed"
            assert not main._workflow_panel.query("#workflow-node-retry")
            yield _AwaitingRetry(app, pilot, host, main, client, states)
    finally:
        await host.shutdown()


@pytest.mark.parametrize("retry_entry", ["graph", "details"])
async def test_playground_waits_for_manual_retry_then_continues_without_replaying_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retry_entry: str
) -> None:
    async with _playground_awaiting_retry(tmp_path, monkeypatch) as playground:
        app, pilot, host, main, states = (
            playground.app,
            playground.pilot,
            playground.host,
            playground.main,
            playground.states,
        )

        async def screenshot(name: str) -> None:
            await pilot.wait_for_scheduled_animations()
            refreshed = asyncio.Event()
            app.call_after_refresh(refreshed.set)
            await wait_for(refreshed.is_set, pilot=pilot)
            app.save_screenshot(name, path=str(tmp_path))

        panel = main._workflow_panel
        run = main._workflow.session_view.projector.current
        assert run is not None

        # Export the actual screen after projection/layout, not an illustration.
        main._workflow.refresh()
        await screenshot("01-awaiting-retry.svg")
        graph = panel.query_one(WorkflowGraph)
        graph.select_node("flaky_check")
        graph.focus()
        await pilot.press("enter")
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted and app.screen.selected is not None
            ),
            pilot=pilot,
        )
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        assert dialog.selected is not None and dialog.selected.attempt == 2
        tabs = dialog.query_one(TabbedContent)
        assert not dialog.query("#workflow-transcript-tab")
        tabs.active = "workflow-output-tab"
        retry = dialog.query_one("#workflow-node-retry", Button)
        await wait_for(
            lambda: (
                "Intentional playground failure (2/2)" in str(dialog.query_one("#workflow-node-error", Static).content)
                and tabs.get_pane("workflow-output-tab").display
                and retry.region.width > 0
                and app.get_widget_at(retry.region.x, retry.region.y)[0] is retry
            ),
            pilot=pilot,
        )
        assert not retry.disabled and dialog.query_one("#workflow-node-actions").display
        assert str(dialog.query_one("#workflow-node").border_subtitle) == "awaiting retry"
        assert dialog.query_one("#workflow-node-errors").display
        assert not dialog.query("#workflow-node-cancel")
        await screenshot("02-node-retry.svg")
        await wait_for(
            lambda: "RuntimeError" in str(dialog.query_one("#workflow-node-diagnostics", Static).content),
            pilot=pilot,
        )
        if retry_entry == "details":
            await click_when_settled(pilot, retry)
        else:
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main and "flaky_check" in graph._retry_regions, pilot=pilot)
            region = graph._retry_regions["flaky_check"]
            x = region.x - round(graph.scroll_offset.x) + graph.diagram_origin.x
            y = region.y - round(graph.scroll_offset.y) + graph.diagram_origin.y
            await screenshot("02-graph-retry.svg")
            await click_when_settled(pilot, graph, offset=(x + graph.gutter.left, y + graph.gutter.top))
            await wait_for(lambda: run.nodes["flaky_check"].attempt == 3, pilot=pilot)
            assert app.screen is main
        await wait_for(lambda: run.finished is not None, pilot=pilot, timeout=15)
        await host.engine.wait_for_run_task()
        assert run.finished is not None and run.finished.outcome == "completed"
        assert run.nodes["flaky_check"].attempt == 3
        assert run.nodes["summary"].state == "completed"
        assert sum(state.node_id == "prepare" and state.state == "running" for state in states) == 1
        assert sum(state.node_id == "healthy_check" and state.state == "running" for state in states) == 1
        if retry_entry == "details":
            await wait_for(lambda: len(dialog.query_one("#workflow-attempt-tabs", Tabs).query("Tab")) == 3, pilot=pilot)
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)
        await select_workflow_view(main, pilot, "output")
        await wait_for(
            lambda: "flaky_check: 3 attempts" in str(panel.query_one("#workflow-outputs", Static).content),
            pilot=pilot,
        )
        panel.query_one("#workflow-outputs-scroll", VerticalScroll).scroll_end(animate=False)
        await screenshot("03-completed.svg")


async def test_playground_rerun_gets_a_fresh_worker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async with _playground_awaiting_retry(tmp_path, monkeypatch) as playground:
        app, pilot, host, main = playground.app, playground.pilot, playground.host, playground.main
        panel, projector = main._workflow_panel, main._workflow.session_view.projector
        run = projector.current
        assert run is not None
        graph = panel.query_one(WorkflowGraph)
        # The key is ignored until the graph has projected the state that offers Retry.
        await wait_for(
            lambda: "flaky_check" in graph._retry_regions, pilot=pilot, description="graph offers Retry on flaky_check"
        )
        graph.select_node("flaky_check")
        graph.focus()
        await pilot.press("r")
        await wait_for(lambda: run.finished is not None, pilot=pilot, timeout=15)
        await host.engine.wait_for_run_task()
        assert run.finished is not None and run.finished.outcome == "completed"
        assert run.nodes["flaky_check"].attempt == 3 and app.screen is main

        # The failure counter is worker module state, so a reused worker would pass at once.
        await wait_for(lambda: not panel.query_one("#workflow-start", Button).disabled, pilot=pilot)
        await start_workflow(pilot)
        await wait_for(
            lambda: (
                projector.current is not None
                and projector.current is not run
                and projector.current.nodes.get("flaky_check") is not None
                and projector.current.nodes["flaky_check"].state == "awaiting_retry"
            ),
            pilot=pilot,
            timeout=15,
        )
        rerun = projector.current
        assert rerun is not None and rerun.nodes["flaky_check"].attempt == 2
        assert playground.client.call_count == 0


async def test_playground_cancel_at_the_retry_boundary_ends_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _playground_awaiting_retry(tmp_path, monkeypatch) as playground:
        pilot, host, main = playground.pilot, playground.host, playground.main
        panel = main._workflow_panel
        run = main._workflow.session_view.projector.current
        assert run is not None
        # Stop at the boundary where Retry would continue the run cancels it instead.
        assert str(panel.query_one("#workflow-stop", Button).label) == "■ Cancel"
        await click_when_settled(pilot, "#workflow-stop")
        await confirm_workflow_cancel(pilot)
        await wait_for(lambda: run.finished is not None, pilot=pilot)
        await host.engine.wait_for_run_task()
        assert run.finished is not None and run.finished.outcome == "cancelled"
        assert run.nodes["flaky_check"].attempt == 2
        assert playground.client.call_count == 0
