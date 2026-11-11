# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancellation is confirmed once and cannot target a replacement workflow run."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, confirm_workflow_cancel, open_workflow, workflow_selection


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_cancel_requires_confirmation_and_dismissal_keeps_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    cancellations: list[events.WorkflowCancelRequest] = []

    async def cancel(event: events.WorkflowCancelRequest) -> None:
        cancellations.append(event)

    await bus.subscribe(events.WorkflowCancelRequest, cancel)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus, settings=Settings(locale=locale))
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.show_preview(preview, run_id="run")
        main._workflow.session_view.selection = workflow_selection(main)
        main._workflow.session_view._run_ids = ["run"]
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        await wait_for(lambda: not main._workflow_panel.query_one("#workflow-stop", Button).disabled, pilot=pilot)
        await click_when_settled(pilot, "#workflow-stop")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-no"))
            ),
            pilot=pilot,
        )
        dialog = app.screen
        main._workflow.run_control.stop()
        assert app.screen is dialog and cancellations == []
        message = str(dialog.query_one("#confirm-message", Static).content)
        assert ("cannot be resumed" if locale == "en" else "无法续跑") in message
        assert ("new run" if locale == "en" else "重新开始") in message
        await click_when_settled(pilot, "#confirm-no")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert not main.query_one("#workflow-stop", Button).has_focus
        assert cancellations == [] and engine.snapshot.kind == "workflow"
        main._workflow.run_control.stop()
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert cancellations == []
        # Esc from the main screen uses this same warning, with one confirmation.
        await pilot.press("escape")
        await confirm_workflow_cancel(pilot)
        await wait_for(lambda: len(cancellations) == 1 and app.screen is main, pilot=pilot)
        assert cancellations[0].run_id == "run"


@pytest.mark.parametrize("replacement", ["idle", "new-run"])
async def test_stale_cancel_confirmation_never_cancels_a_new_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    cancellations: list[events.WorkflowCancelRequest] = []

    async def cancel(event: events.WorkflowCancelRequest) -> None:
        cancellations.append(event)

    await bus.subscribe(events.WorkflowCancelRequest, cancel)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.run_id = "old-run"
        main._workflow.session_view.selection = workflow_selection(main)
        main._workflow.session_view._run_ids = ["old-run", "finishing"]
        await engine.set_execution(ExecutionSnapshot("workflow", "old-run", True), main._services.bus)
        main._workflow.run_control.stop()
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        dialog = app.screen
        await engine.set_execution(
            ExecutionSnapshot("idle") if replacement == "idle" else ExecutionSnapshot("workflow", replacement, True),
            main._services.bus,
        )
        # Race a queued affirmative result against the scheduler's new lease.
        dialog.dismiss(True)
        await wait_for(lambda: main._workflow.run_control._cancel_confirmation is None, pilot=pilot)
        assert cancellations == []
        main._workflow_panel.run_id = "finishing"
        await engine.set_execution(ExecutionSnapshot("workflow", "finishing", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="finishing"))
        main._workflow.run_control.stop()
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        await bus.publish(events.WorkflowRunFinished(run_id="finishing", outcome="completed"))
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert cancellations == []


async def test_pending_request_cannot_cancel_an_unrelated_starting_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        control = main._workflow.run_control
        assert control.start("")
        pending = control._pending_run
        assert pending is not None
        await engine.set_execution(ExecutionSnapshot("workflow", "another-run", True, "another-request"), bus)
        assert not control.can_stop()
        control.stop()
        assert control._cancel_confirmation is None
        await engine.set_execution(ExecutionSnapshot("workflow", "my-run", True, pending.request_id), bus)
        assert control.can_stop()
        control.stop()
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        dialog = app.screen
        # A queued click must not cross the boundary to a replacement request/run.
        await engine.set_execution(ExecutionSnapshot("workflow", "replacement", True, "replacement-request"), bus)
        dialog.dismiss(True)
        await wait_for(lambda: control._cancel_confirmation is None, pilot=pilot)
        assert not control.can_stop()
