# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Source freshness, question lifetimes and workflow file management in ChrysApp."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from rich.syntax import Syntax
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from chrys.app.tui.widgets.workflow.selection import WorkflowList
from chrys.app.tui.widgets.workflow.values import WorkflowValueView
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserQuestion
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.workflows.discovery import WorkflowSource, global_workflows_dir
from chrys.service.workflows.journal import WorkflowJournal
from chrys.service.workflows.ledger import ConfirmationLedger, ledger_path
from chrys.service.workflows.outcomes import RunOutcome
from chrys.service.workflows.scheduler import AttemptRef
from tests.app.tui.screens.main._workflow_support import (
    dismiss_workflow_notice,
    select_archived_run,
    select_workflow_view,
    switch_mode,
)
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, run_store, value_text, workflow_selection


async def test_global_shadowed_symlink_delete_forgets_ledger_but_keeps_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = python_workflow("def fn(value):\n    return value\n", "fn")
    target = tmp_path / "target.py"
    atomic_write_owner_only_bytes(target, source)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        directory = global_workflows_dir(main._workflow.browser.catalog.config_dir)
        directory.mkdir(parents=True)
        path = directory / "wf.py"
        atomic_write_owner_only_bytes(path, source)
        preview = await main._workflow.browser.catalog.preview("wf", trust=True)
        main._workflow.browser.catalog.confirm(preview)
        path.unlink()
        try:
            path.symlink_to(target)
        except OSError:
            pytest.skip("symlink creation unavailable")
        write_workflow(project, "wf", source)
        session_id = str(uuid4())
        assert main._services.state_store is not None
        session_dir = main._services.state_store.session_dir(session_id)
        assert session_dir is not None
        history = session_dir / "workflows" / new_analytics_id()
        store = run_store(history, preview, session_id=session_id, started_at="2026-01-01")
        await store.close()
        main.action_workflow()
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 3, pilot=pilot)
        picker = main._workflow.browser.picker
        assert picker is not None
        picker.selection.list.highlighted = next(
            index for index, row in enumerate(picker.selection.rows) if row.canonical_path == str(path)
        )
        row = main._workflow.browser._picker.selection.selected_row()
        assert row is not None and row.shadowed and row.source.source_kind == "global"
        main._workflow.browser._picker.selection.query_one(WorkflowList).focus()
        await pilot.press("d")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        body = str(app.screen.query_one("#confirm-message", Static).content)
        assert str(path) in body and "Global files cannot be recovered" in body
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 2, pilot=pilot)
        assert not path.is_symlink() and target.read_bytes() == source and history.exists()
        assert (
            ConfirmationLedger(ledger_path(main._workflow.browser.catalog.config_dir)).recorded(
                str(target.resolve()), "global"
            )
            is None
        )


async def test_notice_is_recorded_hidden_and_shown_on_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.run_id = "run"
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="completed"))
        await switch_mode(main, pilot)
        await bus.publish(events.WorkflowRunNotice(run_id="run", code="notice", message="A [literal] notice"))
        await switch_mode(main, pilot)
        assert not main._workflow_panel.query("#workflow-banner")
        await select_workflow_view(main, pilot, "output")
        await wait_for(
            lambda: (
                "A [literal] notice" in str(main._workflow_panel.query_one("#workflow-status-output", Static).content)
            ),
            pilot=pilot,
        )


@pytest.mark.parametrize("trigger", ["show", "timer", "start"])
async def test_changed_source_disables_start_and_code_escape_is_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    trigger: str,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = python_workflow("def fn(value):\n    return value\n", "fn")
    path = write_workflow(project, "wf", source)
    bus = EventBus()
    requests: list[events.WorkflowRunRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "wf")
        panel = main._workflow_panel
        if trigger == "show":
            await switch_mode(main, pilot)
        path.write_bytes(source + b"\n# edited [source]\n")
        if trigger == "show":
            await switch_mode(main, pilot)
        elif trigger == "timer":
            main._workflow.tick()
        else:
            # No await between the rewrite and Enter's routing: the pre-start check owns this race.
            assert not main._workflow.run_control.start("draft")
        await wait_for(lambda: panel.query_one("#workflow-start", Button).disabled, pilot=pilot)
        assert panel.stale and not requests
        await dismiss_workflow_notice(main, pilot, "Reopen")
        await select_workflow_view(main, pilot, "code")
        await wait_for(lambda: panel.code_visible, pilot=pilot)
        code = panel.query_one("#workflow-code-source", Static).content
        assert isinstance(code, Syntax) and "edited [source]" in code.code
        assert panel.code_differs and not panel.query("#workflow-code-banner")
        await pilot.press("escape")
        assert not panel.code_visible and app.screen is main
        assert not requests
        # Acknowledging the changed source and opening a fresh preview clears old notices.
        await open_workflow(main, pilot, "wf")
        await select_workflow_view(main, pilot, "code")
        await wait_for(lambda: not main._workflow._tasks and not main._workflow._refresh_pending, pilot=pilot)
        assert app.screen is main and not main._workflow.feedback._pending_notices


async def test_historical_run_source_and_unavailable_agent_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = b"from chrys.workflows import WorkflowBuilder\nwf=WorkflowBuilder('old')\na=wf.agent('agent', profile='Code')\nwf.start(a)\nwf.output(a)\nworkflow=wf.build()\n"
    path = write_workflow(project, "wf", source)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "wf")
        session_id = str(uuid4())
        main._workflow.session_view.selection = workflow_selection(main, session_id)
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        store = run_store(
            directory / "workflows" / new_analytics_id(),
            preview,
            session_id=session_id,
            started_at="2026-01-01",
        )
        journal = WorkflowJournal(store, None, session_id=session_id)
        try:
            ref = AttemptRef(store.header.run_id, "agent", "agent@iter#1", 1)
            store.write_node_value(ref.activation_id, 1, "input", {"value": {"text": "old input"}})
            store.write_node_value(ref.activation_id, 1, "output", {"value": {"text": "old output"}})
            await journal.node_state(ref, "completed", invocation_id="gone")
            await journal.finish(RunOutcome.COMPLETED)
        finally:
            await store.close()
        await open_workflow(main, pilot, "wf")
        panel = main._workflow_panel
        await select_archived_run(main, pilot, store.header.run_id)
        await wait_for(lambda: panel.run_id == store.header.run_id, pilot=pilot)
        assert main._workflow.session_view.projector.current is None
        path.write_bytes(source + b"# now changed\n")
        # The source poll raises the modal on its own schedule; settle it before clicking under it.
        await dismiss_workflow_notice(main, pilot, "Reopen")
        await select_workflow_view(main, pilot, "code")
        await wait_for(lambda: panel.code_visible, pilot=pilot)
        code = panel.query_one("#workflow-code-source", Static).content
        assert isinstance(code, Syntax) and code.code == source.decode()
        await pilot.press("escape", "j", "enter")
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted and app.screen.selected is not None
            ),
            pilot=pilot,
        )
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        await wait_for(
            lambda: "old output" in value_text(dialog.query_one("#workflow-node-output", WorkflowValueView)),
            pilot=pilot,
        )
        # Transcript I/O and mounting follow the output update in a separate
        # awaited phase of the records worker.
        await wait_for(
            lambda: "Transcript not available" in " ".join(str(widget.content) for widget in dialog.query(Static)),
            pilot=pilot,
            description="missing archived transcript is displayed",
        )
        assert "Transcript not available" in " ".join(str(widget.content) for widget in dialog.query(Static))


@pytest.mark.parametrize("end", ["answer", "before", "open", "covered"])
async def test_workflow_question_answers_and_run_terminal_owns_dialog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    end: str,
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    answers: list[events.WorkflowNodeAnswer] = []

    async def answered(event: events.WorkflowNodeAnswer) -> None:
        answers.append(event)

    await bus.subscribe(events.WorkflowNodeAnswer, answered)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        main.query_one(InputBar).replace_draft("keep this draft")
        await bus.publish(events.WorkflowRunStarted(run_id="run"))
        question = events.WorkflowNodeAskUser(
            run_id="run",
            node_id="py",
            activation_id="py@iter#1",
            attempt=1,
            request_id="question",
            questions=(AskUserQuestion("Your [answer]?"),),
        )
        await bus.publish(question)
        if end == "before":
            await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
            await wait_for(lambda: not main._workflow._refresh_pending, pilot=pilot)
            assert app.screen is main and not answers
            return
        await wait_for(
            lambda: isinstance(app.screen, AskUserDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="workflow question dialog and its children are mounted",
        )
        dialog = app.screen
        assert isinstance(dialog, AskUserDialog)
        assert not dialog._allow_inline
        if end == "answer":
            area = dialog.query_one("#askuser-input", EnhancedTextArea)
            area.load_text("a precise answer")
            await click_when_settled(pilot, dialog.query_one("#askuser-submit", Button))
            await wait_for(lambda: len(answers) == 1 and app.screen is main, pilot=pilot)
            assert answers[0].request_id == "question"
            assert answers[0].answers == (AskUserAnswer(values=("a precise answer",)),)
            assert answers[0].activation_id == "py@iter#1"
            await bus.publish(question)
            await wait_for(lambda: not main._workflow._refresh_pending, pilot=pilot)
            assert app.screen is main and len(answers) == 1
            assert main._workflow.run_control._pending_answers == {"question"}
            await bus.publish(
                events.WorkflowNodeAnswered(
                    run_id="run",
                    node_id="py",
                    activation_id="py@iter#1",
                    attempt=1,
                    request_id="question",
                    answer="a precise answer",
                )
            )
            await wait_for(lambda: not main._workflow.run_control._pending_answers, pilot=pilot)
            run = main._workflow.session_view.projector.current
            assert run is not None and run.question_states == {"question": "answered"}
            assert not run.questions
        else:
            if end == "covered":
                await app.push_screen(ConfirmDialog())
                await wait_for(
                    lambda: (
                        isinstance(app.screen, ConfirmDialog)
                        and app.screen.is_mounted
                        and bool(app.screen.query("#confirm-no"))
                    ),
                    pilot=pilot,
                )
            await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
            await wait_for(lambda: dialog._dismiss_requested, pilot=pilot)
            if end == "covered":
                assert isinstance(app.screen, ConfirmDialog)
                await click_when_settled(pilot, "#confirm-no")
            await wait_for(lambda: app.screen is main, pilot=pilot)
            assert not answers
        assert main.query_one(InputBar).snapshot_draft().text == "keep this draft"


async def test_project_file_delete_confirmation_and_active_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = python_workflow("def fn(value):\n    return value\n", "fn")
    write_workflow(project, "demo-workflow", source)
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        await wait_for(lambda: bool(main._workflow.browser._picker.selection.rows), pilot=pilot)
        picker = main._workflow.browser._picker.selection.query_one(WorkflowList)
        picker.highlighted = next(
            i
            for i, row in enumerate(main._workflow.browser._picker.selection.rows)
            if row.source.source_kind == "builtin"
        )
        picker.focus()
        await pilot.press("d")
        assert isinstance(app.screen, WorkflowPickerDialog)
        picker.highlighted = next(
            i
            for i, row in enumerate(main._workflow.browser._picker.selection.rows)
            if row.workflow_id == "demo-workflow" and not row.shadowed
        )
        row = main._workflow.browser._picker.selection.selected_row()
        assert row is not None and isinstance(row.source, WorkflowSource) and row.source.source_kind == "project"
        path = Path(row.source.canonical_path)
        assert path.read_bytes() == source
        picker.focus()
        await pilot.press("d")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-no"))
            ),
            pilot=pilot,
        )
        body = str(app.screen.query_one("#confirm-message", Static).content)
        assert str(path) in body and "Git" in body and "project" in body
        confirm = app.screen
        await click_when_settled(pilot, "#confirm-no")
        await wait_for(
            lambda: (
                app.screen is not confirm and isinstance(app.screen, WorkflowPickerDialog) and app.screen.is_mounted
            ),
            pilot=pilot,
        )
        assert path.exists()
        picker.focus()
        await pilot.press("d")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        # Admission while the confirmation is open must be checked again at deletion.
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunStarted(run_id="run", workflow_id="demo-workflow", canonical_path=str(path))
        )
        await click_when_settled(pilot, "#confirm-yes")
        await dismiss_workflow_notice(main, pilot, "Stop this workflow")
        await wait_for(lambda: isinstance(app.screen, WorkflowPickerDialog) and app.screen.is_mounted, pilot=pilot)
        assert path.exists()
        picker.focus()
        await pilot.press("d")
        await dismiss_workflow_notice(main, pilot, "Stop this workflow")
        assert isinstance(app.screen, WorkflowPickerDialog) and path.exists()
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
        picker.focus()
        await pilot.press("d")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 1, pilot=pilot)
        assert not path.exists() and main._workflow.browser._picker.selection.rows[0].source.source_kind == "builtin"
