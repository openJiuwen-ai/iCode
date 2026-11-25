# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Previous-run reads stay local to one node dialog, separate from current records."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.main import workflow_session_view
from chrys.app.tui.widgets.workflow.values import WorkflowValueView
from chrys.service.workflows.store import node_value_path
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for
from tests.support.workflow_history import record_workflow_run

from ._workflow_support import WorkflowEngine, save_workflow_session, value_text

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("history_state", ["corrupt", "empty", "missing_current"])
async def test_previous_history_is_cached_without_hiding_current_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history_state: str
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
        previous_id, current_id = uuid4().hex, uuid4().hex
        for run_id in (previous_id, current_id):
            await record_workflow_run(
                directory / "workflows" / run_id, session_id=session_id, title="Archived", outcome="completed"
            )
        previous_output = node_value_path(directory / "workflows" / previous_id, "check@iter#1", 1, "output")
        if history_state == "corrupt":
            previous_output.write_text("{", encoding="utf-8")
        await save_workflow_session(state_store, session_id, project)
        await main._workflow.session_view.restore_session(session_id, current_id)
        await wait_for(lambda: app.screen is main, pilot=pilot)
        original_history = workflow_session_view.session_runs

        def history(session_dir: Path):
            records = original_history(session_dir)
            if history_state == "empty":
                return []
            if history_state == "missing_current":
                return [record for record in records if record.directory.name != current_id]
            return records

        read = create_autospec(original_history, side_effect=history)
        monkeypatch.setattr(workflow_session_view, "session_runs", read)
        main._workflow.session_view.open_node("check")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted,
            pilot=pilot,
        )
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        await wait_for(
            lambda: (
                "Archived workflow output" in value_text(dialog.query_one("#workflow-node-output", WorkflowValueView))
            ),
            pilot=pilot,
        )
        assert not dialog.query_one("#workflow-node-previous").display
        await dialog.load_records(dialog._generation, dialog.selected).wait()
        await dialog.load_records(dialog._generation, dialog.selected).wait()
        read.assert_called_once()
        # The error area is written after every pane has shown; the cached history keeps its read error.
        assert bool(str(dialog.query_one("#workflow-node-error", Static).content)) == (history_state == "corrupt")
        if history_state == "corrupt":
            # Reopening starts a fresh cache; a repaired archive becomes readable.
            previous_output.write_text('{"value": "Repaired previous output"}', encoding="utf-8")
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)
            main._workflow.session_view.open_node("check")
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowNodeDialog)
                    and app.screen.is_mounted
                    and "Repaired previous output"
                    in value_text(app.screen.query_one("#workflow-node-previous", WorkflowValueView))
                ),
                pilot=pilot,
            )
            assert read.call_count == 2
