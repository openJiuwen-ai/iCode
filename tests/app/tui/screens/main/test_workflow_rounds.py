# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loop badges show the round being executed, including retry and cancellation."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.node_view import NodeView
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, open_workflow

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")

SOURCE = b"""from chrys.workflows import WorkflowBuilder
def echo(value):
    return value
def done(value):
    return False
def body(scope):
    node = scope.python('echo', echo)
    return node, node
wf = WorkflowBuilder('Loop rounds')
loop = wf.loop('round', body, until=done, max_iterations=3, on_exhausted='continue')
wf.start(loop)
wf.output(loop)
workflow = wf.build()
"""


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
@pytest.mark.parametrize("outcome", ["completed", "cancelled"])
async def test_round_badge_advances_at_entry_and_retains_the_terminal_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str, outcome: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "rounds", SOURCE)
    engine, bus = WorkflowEngine(), EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus, settings=Settings(locale=locale))
    async with app.run_test(size=(120, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "rounds")
        panel = main._workflow_panel
        graph = panel.query_one(WorkflowGraph)
        panel.run_id = "run"
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run", node_id="round", activation_id="round@iter#1", attempt=1, state="running"
            )
        )
        first = "Iteration 1/3" if locale == "en" else "迭代 1/3"
        await wait_for(lambda: graph._badges.get("round") == first, pilot=pilot)
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run",
                node_id="echo",
                activation_id="first-body-epoch",
                iteration=1,
                attempt=1,
                state="completed",
            )
        )
        await bus.publish(events.WorkflowLoopIteration(run_id="run", loop_id="round", iteration=1, verdict="continue"))
        second = "Iteration 2/3" if locale == "en" else "迭代 2/3"
        for state, attempt in (("running", 1), ("awaiting_retry", 1), ("running", 2)):
            await bus.publish(
                events.WorkflowNodeStateChanged(
                    run_id="run",
                    node_id="echo",
                    activation_id="retried-body-epoch",
                    iteration=2,
                    attempt=attempt,
                    state=state,
                )
            )
            await wait_for(lambda state=state: graph._views.get("echo", NodeView()).state == state, pilot=pilot)
            assert graph._badges["round"] == second
        if outcome == "completed":
            await bus.publish(events.WorkflowLoopIteration(run_id="run", loop_id="round", iteration=2, verdict="exit"))
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run", node_id="round", activation_id="round@iter#1", attempt=1, state=outcome
            )
        )
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome=outcome))
        await wait_for(lambda: graph._views.get("round", NodeView()).state == outcome, pilot=pilot)
        assert graph._badges["round"] == second
        assert str(panel.query_one("#workflow-iterations", Static).content) == f"round {second}"
