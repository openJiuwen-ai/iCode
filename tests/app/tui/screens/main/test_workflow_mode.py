# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""App mode stays fixed, and says why, from pending submission through execution cleanup."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import OptionList, Static

from chrys.app.tui.screens.dialogs.app_mode import AppModeDialog
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.app.tui.screens.main._workflow_support import start_workflow
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, open_workflow, switch_mode, workflow_selection


@pytest.mark.parametrize("workflow", [False, True], ids=["chat", "workflow"])
async def test_app_mode_is_locked_until_pending_and_execution_work_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: bool
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    engine, bus = WorkflowEngine(), EventBus()
    requests: list[events.WorkflowRunRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        composer = main.query_one(InputBar)
        if workflow:
            preview = await open_workflow(main, pilot, "demo-workflow")
        composer.replace_draft("Keep this draft")
        badge = main.query_one("#mode-badge", Static)
        expected_badge = " APP MODE: Workflow " if workflow else " APP MODE: Chat "
        expected_notice = (
            "Cannot switch app mode while a workflow is running"
            if workflow
            else "Cannot switch app mode while the agent is busy"
        )
        notices: list[tuple[str, str, str]] = []

        def notify(
            message: str,
            *,
            title: str = "",
            severity: str = "information",
            timeout: float | None = None,
            markup: bool = True,
        ) -> None:
            notices.append((message, title, severity))

        monkeypatch.setattr(main, "notify", notify)

        async def assert_mode_locked() -> None:
            draft = composer.snapshot_draft().text
            notices.clear()
            await click_when_settled(pilot, badge)
            assert app.screen is main
            assert notices == [(expected_notice, "Busy", "warning")]
            # Stale picker callbacks and direct /workflow dispatch use the same gate.
            main._set_workflow_mode(not workflow)
            if not workflow:
                main.action_workflow()
            assert app.screen is main
            assert main._workflow.workflow_mode is workflow
            assert composer.snapshot_draft().text == draft
            assert str(badge.content) == expected_badge

        if workflow:
            await start_workflow(pilot)
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert main._workflow.awaiting_engine and engine.snapshot.kind == "idle"
        else:
            main._state.submit.begin(composer.value)
        await assert_mode_locked()

        if workflow:
            await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
            await bus.publish(
                events.WorkflowRunAccepted(
                    request_id=requests[0].request_id,
                    run_id="run",
                    selection=workflow_selection(main, "workflow-session"),
                )
            )
            await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        else:
            main._state.submit.clear()
            main._set_agent_running(True)
            # Presentation may become busy before the backend acquires its lease.
            await assert_mode_locked()
            await engine.set_execution(ExecutionSnapshot("turn", cancellable=True), main._services.bus)
        await assert_mode_locked()

        if workflow:
            await bus.publish(
                events.WorkflowNodeStateChanged(
                    run_id="run", node_id="write_tour", activation_id="write_tour@iter#1", state="awaiting_retry"
                )
            )
            await assert_mode_locked()
            await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
        else:
            main._set_agent_running(False)
        # Terminal UI state precedes final saving and lease release.
        await assert_mode_locked()
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        notices.clear()
        await switch_mode(main, pilot)
        assert main._workflow.workflow_mode is not workflow
        assert notices == []


@pytest.mark.parametrize("workflow", [False, True], ids=["chat", "workflow"])
async def test_mode_picker_rechecks_execution_when_its_selection_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workflow: bool
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    engine = WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        if workflow:
            await open_workflow(main, pilot, "demo-workflow")
        composer = main.query_one(InputBar)
        composer.replace_draft("Preserve the current mode's draft")
        await click_when_settled(pilot, "#mode-badge")
        await wait_for(lambda: isinstance(app.screen, AppModeDialog) and app.screen.is_mounted, pilot=pilot)
        await engine.set_execution(
            ExecutionSnapshot("workflow" if workflow else "turn", "run", True), main._services.bus
        )
        picker = app.screen.query_one(OptionList)
        picker.highlighted = 0 if workflow else 1
        await pilot.press("enter")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert main._workflow.workflow_mode is workflow
        assert composer.value == "Preserve the current mode's draft"
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await switch_mode(main, pilot)
        assert main._workflow.workflow_mode is not workflow
