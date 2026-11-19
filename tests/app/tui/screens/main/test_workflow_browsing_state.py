# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transactional browsing and active-session controls remain independent of the viewed run."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.workspace import Workspace
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_history import record_workflow_run
from tests.support.workflow_workers import python_workflow

from ._workflow_support import (
    WorkflowEngine,
    confirm_workflow_cancel,
    dismiss_workflow_notice,
    open_workflow,
    save_workflow_session,
    switch_mode,
    workflow_notice_text,
    workflow_selection,
)

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("failure", ["load", "declined"])
async def test_failed_selection_keeps_the_committed_preview_ready_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(
        project,
        "other",
        b"raise ValueError('broken selection')\n"
        if failure == "load"
        else python_workflow("def echo(value):\n    return value\n", "echo"),
    )
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        browser = main._workflow.browser
        loaded, draft = browser.loaded, browser.draft
        read = create_autospec(browser.catalog.preview, side_effect=browser.catalog.preview)
        monkeypatch.setattr(browser.catalog, "preview", read)
        browser.open("other")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        if failure == "load":
            await click_when_settled(pilot, "#workflow-confirm-yes")
            await wait_for(
                lambda: "broken selection" in workflow_notice_text(main), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
            )
            await dismiss_workflow_notice(main, pilot, "broken selection")
        else:
            await pilot.press("escape")
        await wait_for(
            lambda: app.screen is main and not main._workflow_panel.previewing, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
        )
        assert browser.loaded is loaded and browser.draft is draft
        assert loaded is not None and main._workflow_panel.preview is loaded.preview
        async with capture_event_sequence(main._services.bus, events.WorkflowRunRequest) as requests:
            assert main._workflow.run_control.start("run the original")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert requests[0].target.workflow_id == "demo-workflow"
            assert requests[0].pins is not None
            assert requests[0].pins.identity == preview.source.identity
            assert requests[0].pins.spec_digest == preview.spec_digest
        assert read.call_count == 1


async def test_following_draft_reconciles_workspace_changes_received_in_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    engine.workspace = Workspace.from_cwd(project)
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        await switch_mode(main, pilot)
        engine.workspace = Workspace.from_cwd(other)
        await main._services.bus.publish(events.WorkspaceUpdated(primary_cwd=str(other)))
        await switch_mode(main, pilot)
        await wait_for(
            lambda: main._workflow.project_cwd == str(other) and not main._workflow.browser.workspace_busy,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert main._workflow.browser.draft.follows_workspace
        assert main._workflow.browser.loaded is not None
        assert main._workflow.browser.loaded.catalog.project_cwd == other
        assert not main._workflow_panel.stale


async def test_history_tab_can_stop_the_active_run_of_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        control = main._workflow.run_control
        for run_id in ("older-run", "active-run"):
            assert control.start("")
            pending = control._pending_run
            assert pending is not None
            await bus.publish(
                events.WorkflowRunAccepted(
                    request_id=pending.request_id,
                    run_id=run_id,
                    selection=workflow_selection(main, "session"),
                )
            )
            await bus.publish(events.WorkflowRunStarted(run_id=run_id, manifest=preview.manifest, session_id="session"))
            if run_id == "older-run":
                await bus.publish(events.WorkflowRunFinished(run_id=run_id, outcome="completed", session_id="session"))
        await engine.set_execution(ExecutionSnapshot("workflow", "active-run", True), main._services.bus)
        await main._workflow.session_view.select_run("older-run")
        await wait_for(lambda: not main._workflow_panel.query_one("#workflow-stop", Button).disabled, pilot=pilot)
        async with capture_event_sequence(bus, events.WorkflowCancelRequest) as cancelled:
            await pilot.press("ctrl+b")
            await wait_for(lambda: control._cancel_confirmation is not None, pilot=pilot)
            dialog = control._cancel_confirmation[1]
            await wait_for(lambda: dialog.is_mounted, pilot=pilot)
            assert "active-run" in str(dialog.query_one("#confirm-message", Static).content)
            await confirm_workflow_cancel(pilot)
            await wait_for(lambda: bool(cancelled), pilot=pilot)
            assert cancelled[0].run_id == "active-run"
            assert main._workflow_panel.run_id == "older-run"


@pytest.mark.parametrize("missing", ["header", "spec"])
async def test_session_pick_announces_fallback_to_the_newest_readable_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        store = main._services.state_store
        assert store is not None
        session_id = str(uuid4())
        directory = store.session_dir(session_id)
        older, newest = uuid4().hex, uuid4().hex
        for run_id in (older, newest):
            await record_workflow_run(
                directory / "workflows" / run_id, session_id=session_id, title=run_id, outcome="completed"
            )
        await save_workflow_session(store, session_id, project)
        (directory / "workflows" / newest / ("run.json" if missing == "header" else "spec.json")).unlink()
        await switch_mode(main, pilot)
        await main._workflow.session_view.restore_session(session_id)
        await dismiss_workflow_notice(main, pilot, f"newest readable run: {older}")
        assert main._workflow_panel.run_id == older
        assert main._workflow.session_id == session_id


async def test_localization_reprojects_the_current_run_without_a_preview_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
    from chrys.app.tui.widgets.workflow.node_view import NodeView

    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        projector = main._workflow.session_view.projector
        projector.record(
            events.WorkflowRunStarted(
                run_id="run", workflow_id="demo-workflow", manifest=preview.manifest, title="Recorded title"
            )
        )
        projector.record(
            events.WorkflowNodeStateChanged(
                run_id="run",
                node_id="architecture",
                activation_id="review",
                attempt=1,
                state="completed",
            )
        )
        run = projector.run("run")
        assert run is not None
        main._workflow.session_view._show_run(run)
        graph = main._workflow_panel.query_one(WorkflowGraph)
        await wait_for(lambda: graph._views.get("architecture", NodeView()).state == "completed", pilot=pilot)
        compile_graph = create_autospec(graph.show_manifest, side_effect=graph.show_manifest)
        monkeypatch.setattr(graph, "show_manifest", compile_graph)
        main._workflow_panel.refresh_localization()
        assert compile_graph.call_count == 0
        assert graph._views["architecture"].state == "completed"
        await wait_for(lambda: compile_graph.call_count == 1, pilot=pilot)
        assert graph._views["architecture"].state == "completed"
        assert main._workflow_panel.definition.manifest["title"] == preview.title
