# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Archived artifact failures and session changes at the actual workflow view boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from rich.syntax import Syntax
from textual.widgets import Button, Static, TabbedContent

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.main import workflow_content
from chrys.app.tui.widgets.workflow import graph as graph_module
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.values import WorkflowValueView
from chrys.foundation.events.bus import EventBus
from chrys.service.workflows.layout import SOURCE_FILE
from chrys.service.workflows.store import node_value_path, read_run_header
from tests.app.tui.screens.main._workflow_support import select_archived_run, workflow_notice_text, workflow_selecting
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_history import record_workflow_run

from ._workflow_support import (
    WorkflowEngine,
    open_workflow,
    save_workflow_session,
    select_workflow_view,
    value_text,
    workflow_selection,
)

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_restored_node_shows_previous_output_without_live_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        state_store = main._services.state_store
        assert state_store is not None
        session_id = str(uuid4())
        directory = state_store.session_dir(session_id)
        run_ids = [uuid4().hex, uuid4().hex]
        for run_id in run_ids:
            await record_workflow_run(
                directory / "workflows" / run_id, session_id=session_id, title="Archived", outcome="completed"
            )
        await save_workflow_session(state_store, session_id, project)
        compile_graph = create_autospec(
            graph_module.compile_ir_with_geometry, side_effect=graph_module.compile_ir_with_geometry
        )
        monkeypatch.setattr(graph_module, "compile_ir_with_geometry", compile_graph)
        await main._workflow.session_view.restore_session(session_id, run_ids[-1])
        await wait_for(lambda: app.screen is main, pilot=pilot)
        main._workflow.refresh()
        assert compile_graph.call_count == 1
        assert main._workflow_panel.preview is None
        main._workflow.session_view.open_node("check")
        await wait_for(lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted, pilot=pilot)
        previous = app.screen.query_one("#workflow-node-previous", WorkflowValueView)
        await wait_for(lambda: "Archived workflow output" in value_text(previous), pilot=pilot)
        assert previous.display


@pytest.mark.parametrize("source_change", ["replaced", "deleted"])
async def test_history_uses_snapshots_after_source_is_changed_or_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_change: str
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        run_id = uuid4().hex
        run_directory = directory / "workflows" / run_id
        await record_workflow_run(run_directory, session_id=directory.name, title="Archived", outcome="completed")
        source_path = Path(read_run_header(run_directory)["canonical_path"])
        source_path.write_bytes((run_directory / SOURCE_FILE).read_bytes())
        if source_change == "deleted":
            source_path.unlink()
        else:
            source_path.write_text("raise AssertionError('Historical views must never execute source')\n")

        await select_archived_run(main, pilot, run_id)
        panel = main._workflow_panel
        await wait_for(lambda: panel.run_id == run_id and bool(panel.query_one(WorkflowGraph).geometry), pilot=pilot)
        assert set(panel.query_one(WorkflowGraph).geometry) == {"check"}
        assert not panel.query_one("#workflow-start", Button).disabled
        await select_workflow_view(main, pilot, "code")
        source = panel.query_one("#workflow-code-source", Static)
        await wait_for(lambda: isinstance(source.content, Syntax), pilot=pilot)
        assert isinstance(source.content, Syntax) and source.content.code == "# Archived workflow source\n"
        await select_workflow_view(main, pilot, "output")
        await wait_for(
            lambda: "Archived workflow output" in str(panel.query_one("#workflow-outputs", Static).content),
            pilot=pilot,
        )
        assert workflow_notice_text(main) == ""


@pytest.mark.parametrize("damaged_output", [False, True])
async def test_archived_output_ignores_input_and_handles_invalid_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damaged_output: bool
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        run_id = uuid4().hex
        run_directory = directory / "workflows" / run_id
        await record_workflow_run(run_directory, session_id=directory.name, title="Archived", outcome="completed")
        node_value_path(run_directory, "check@iter#1", 1, "input").write_text("{", encoding="utf-8")
        if damaged_output:
            node_value_path(run_directory, "check@iter#1", 1, "output").write_text(
                '{"value": "invalid"}', encoding="utf-8"
            )
        await select_archived_run(main, pilot, run_id)
        await wait_for(
            lambda: app.screen is main and main._workflow_panel.run_id == run_id,
            pilot=pilot,
        )
        await select_workflow_view(main, pilot, "output")
        expected = "Invalid workflow output record" if damaged_output else "Archived workflow output"
        await wait_for(
            lambda: expected in str(main._workflow_panel.query_one("#workflow-outputs", Static).content),
            pilot=pilot,
        )


async def test_source_read_cannot_cross_session_restore_with_same_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(120, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, str(uuid4()))
        first_directory = main._workflow.session_view._session_dir()
        store = main._services.state_store
        assert first_directory is not None and store is not None
        second_id, run_id = str(uuid4()), uuid4().hex
        second_directory = store.session_dir(second_id)
        for directory in (first_directory, second_directory):
            run_directory = directory / "workflows" / run_id
            await record_workflow_run(run_directory, session_id=directory.name, title="Archived", outcome="completed")
            (run_directory / SOURCE_FILE).write_text(f"# {directory.name}\n", encoding="utf-8")
        await select_archived_run(main, pilot, run_id)
        await wait_for(lambda: app.screen is main and main._workflow_panel.run_id == run_id, pilot=pilot)
        started, release = Event(), Event()
        original_source = workflow_content.read_run_source

        def delayed_source(directory: Path) -> bytes:
            if directory.parent.parent == first_directory:
                started.set()
                assert release.wait(10), "old session's source read was not released"
            return original_source(directory)

        monkeypatch.setattr(workflow_content, "read_run_source", delayed_source)
        try:
            tabs = main._workflow_panel.query_one("#workflow-run", TabbedContent)
            await click_when_settled(pilot, tabs.get_tab("workflow-code-tab"))
            await wait_for(started.is_set, pilot=pilot)
            main._workflow.session_view.selection = workflow_selection(main, second_id)
            main._workflow.session_view.content.reset()
            main._workflow_panel.run_id = ""
            await select_archived_run(main, pilot, run_id)
            await wait_for(
                lambda: app.screen is main and main._workflow_panel.run_id == run_id and not workflow_selecting(main),
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
            await select_workflow_view(main, pilot, "code")
            source = main._workflow_panel.query_one("#workflow-code-source", Static)
            await wait_for(
                lambda: isinstance(source.content, Syntax) and second_directory.name in source.content.code,
                pilot=pilot,
            )
        finally:
            release.set()
            await asyncio.gather(*tuple(main._workflow._tasks))
        assert isinstance(source.content, Syntax) and source.content.code == f"# {second_directory.name}\n"
