# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A new workflow selects in the current workspace without mutating history on cancel."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from textual.widgets import Button

from chrys.app.tui.screens.dialogs.confirm import NoticeDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, workflow_selection


async def test_new_workflow_picker_uses_current_workspace_and_cancel_keeps_bound_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_project = make_project(tmp_path / "original")
    current_project = make_project(tmp_path / "current")
    source = python_workflow("def echo(value):\n    return value\n", "echo")
    write_workflow(original_project, "original_only", source)
    write_workflow(current_project, "current_only", source)
    monkeypatch.chdir(original_project)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "original_only")
        controller, panel = main._workflow, main._workflow_panel
        session_id = str(uuid4())
        assert controller.run_control.start("old run")
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=controller.run_control._pending_run.request_id,
                run_id="old-run",
                selection=workflow_selection(main, session_id),
            )
        )
        await bus.publish(
            events.WorkflowRunStarted(
                run_id="old-run",
                session_id=session_id,
                workflow_id=preview.source.workflow_id,
                title=preview.title,
                manifest=preview.manifest,
                canonical_path=preview.source.canonical_path,
                source_kind=preview.source.source_kind,
                spec_digest=preview.spec_digest,
            )
        )
        await bus.publish(events.WorkflowRunFinished(run_id="old-run", session_id=session_id, outcome="completed"))
        await bus.publish(events.WorkspaceUpdated(primary_cwd=str(current_project)))
        assert controller.browser.catalog.project_cwd == original_project
        assert controller.project_cwd == str(original_project)

        catalog = WorkflowCatalog(
            config_dir=controller.browser.catalog.config_dir, project_cwd=current_project, bus=bus
        )
        current_preview = await catalog.preview("current_only", trust=True)
        catalog.confirm(current_preview)
        for cancel in (True, False):
            controller.browser.enter_selection()
            await wait_for(
                lambda: (
                    isinstance(app.screen, NoticeDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query("#confirm-no"))
                ),
                pilot=pilot,
            )
            assert str(app.screen.query_one("#confirm-yes", Button).label) == "New"
            assert str(app.screen.query_one("#confirm-no", Button).label) == "OK"
            if cancel:
                await click_when_settled(pilot, "#confirm-no")
                await wait_for(lambda: app.screen is main, pilot=pilot)
                assert controller.session_id == session_id and panel.preview == preview
                controller.browser.enter_selection()
                await wait_for(
                    lambda: (
                        isinstance(app.screen, NoticeDialog)
                        and app.screen.is_mounted
                        and bool(app.screen.query("#confirm-no"))
                    ),
                    pilot=pilot,
                )
            await click_when_settled(pilot, "#confirm-yes")
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowPickerDialog)
                    and app.screen.is_mounted
                    and main._workflow.browser._picker.selection.is_mounted
                    and any(row.workflow_id == "current_only" for row in main._workflow.browser._picker.selection.rows)
                ),
                pilot=pilot,
            )
            assert not any(row.workflow_id == "original_only" for row in main._workflow.browser._picker.selection.rows)
            assert controller.session_id == session_id
            assert controller.project_cwd == str(original_project)
            if cancel:
                await pilot.press("escape")
                await wait_for(lambda: app.screen is main, pilot=pilot)
                assert panel.preview == preview and panel.run_ids == ["old-run"]
                assert controller.session_id == session_id
                assert controller.browser.catalog.project_cwd == original_project
                continue
            picker = main._workflow.browser._picker.selection.list
            picker.highlighted = next(
                i
                for i, row in enumerate(main._workflow.browser._picker.selection.rows)
                if row.workflow_id == "current_only"
            )
            picker.focus()
            await pilot.press("enter")
            await wait_for(
                lambda: (
                    app.screen is main
                    and panel.preview is not None
                    and panel.preview.source.workflow_id == "current_only"
                ),
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
        assert not controller.session_id and not panel.run_ids
        assert controller.browser.catalog.project_cwd == current_project
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert controller.run_control.start("new run")
            await wait_for(lambda: bool(requests), pilot=pilot)
        assert len(requests) == 1
        assert isinstance(requests[0], events.WorkflowRunRequest)
        assert requests[0].target.workspace.primary_cwd == str(current_project)
        assert requests[0].session_id is None
