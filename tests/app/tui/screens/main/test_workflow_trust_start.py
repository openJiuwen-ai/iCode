# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real trust UI and cancellation while the worker is still importing its workflow."""

from __future__ import annotations

from pathlib import Path

import psutil
import pytest
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.app.tui.widgets.workflow.info import WorkflowInfo
from chrys.foundation.events import types as events
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.llm.mock import MockChatClient
from tests.orchestration.workflows._hosting import make_host, make_project, patch_runtime, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, confirm_workflow_cancel, open_workflow, select_workflow_view


async def test_trust_dialog_precedes_top_level_execution_and_cancel_has_no_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    source += python_workflow("def fn(value):\n    return value\n", "fn")
    write_workflow(project, "untrusted", source)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        main._workflow.browser.open("untrusted")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert not marker.exists()
        inspection = app.screen.query_one(WorkflowInfo).data
        assert inspection is not None and inspection.requires_trust
        assert not app.screen.query("#workflow-info-nodes")
        app.save_screenshot("trust-before-execution.svg", path=str(tmp_path))
        await click_when_settled(pilot, "#workflow-confirm-no")
        await wait_for(
            lambda: app.screen is main and not main._workflow_panel.previewing, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
        )
        assert not marker.exists()
        assert main._workflow_panel.preview is None
        assert (
            main._workflow.browser.catalog.ledger().recorded(str(project / ".chrys/workflows/untrusted.py"), "project")
            is None
        )
        main._workflow.browser.open("untrusted")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert not marker.exists()
        await click_when_settled(pilot, "#workflow-confirm-yes")
        await wait_for(
            lambda: main._workflow_panel.preview is not None and app.screen is main,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert marker.exists()
        preview = main._workflow_panel.preview
        assert preview is not None and main._workflow.browser.catalog.ledger().is_confirmed(preview.ledger_entry())
        await select_workflow_view(main, pilot, "info")
        info = main._workflow_panel.query_one(WorkflowInfo)
        await wait_for(lambda: bool(info.query("#workflow-info-nodes")), pilot=pilot)
        assert info.data is not None and not info.data.requires_trust
        assert preview.environment.python_version in [str(widget.content) for widget in info.query(Static)]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_starting_worker_can_be_cancelled_before_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    block, pid = tmp_path / "block-load", tmp_path / "worker-pid"
    source = (
        "import os, threading\nfrom pathlib import Path\n"
        f"if Path({str(block)!r}).exists():\n"
        f"    Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "    threading.Event().wait()\n"
    ).encode() + python_workflow("def fn(value):\n    return value\n", "fn")
    write_workflow(project, "slow", source)
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    app = make_chrys_app(tmp_path / "sessions", engine=host.engine, event_bus=host.event_bus)
    replies: list[events.WorkflowRunAccepted | events.WorkflowRunRejected] = []

    async def replied(event: events.WorkflowRunAccepted | events.WorkflowRunRejected) -> None:
        replies.append(event)

    await host.event_bus.subscribe(events.WorkflowRunAccepted, replied)
    await host.event_bus.subscribe(events.WorkflowRunRejected, replied)
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await host.start()
            main = app._main_screen
            assert main is not None
            await wait_for(lambda: not main._state.run.agent_loading and app.screen is main, pilot=pilot)
            await open_workflow(main, pilot, "slow")
            block.touch()
            # A start cancelled before acceptance leaves the draft idle with no run (asserted each time),
            # so every trigger cancels its own start, on its own worker, from the same screen.
            for trigger in ("button", "ctrl+b", "escape"):
                pid.unlink(missing_ok=True)
                replies.clear()
                assert main._workflow.run_control.start("")
                pending = main._workflow.run_control._pending_run
                assert pending is not None
                await wait_for(lambda: pid.exists() and bool(pid.read_text()), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
                worker = psutil.Process(int(pid.read_text()))
                assert replies == [] and not main._workflow.session_view.run_ids
                assert host.engine.execution().request_id == pending.request_id
                stop = main._workflow_panel.query_one("#workflow-stop", Button)
                await wait_for(lambda stop=stop: not stop.disabled, pilot=pilot)
                app.save_screenshot(f"starting-cancellable-{trigger.replace('+', '-')}.svg", path=str(tmp_path))
                if trigger == "button":
                    await click_when_settled(pilot, stop)
                else:
                    await pilot.press(trigger)
                await confirm_workflow_cancel(pilot)
                await wait_for(lambda: bool(replies) and not main._workflow.awaiting_engine, pilot=pilot)
                await host.engine.workflows.wait_idle()
                assert len(replies) == 1, trigger
                assert isinstance(replies[0], events.WorkflowRunRejected) and replies[0].error == "cancelled"
                assert replies[0].request_id == pending.request_id
                assert host.engine.execution() == ExecutionSnapshot("idle")
                assert not worker.is_running()
                assert not main._workflow.session_view.run_ids
                assert main._workflow.feedback.notice is None
    finally:
        await host.shutdown()
