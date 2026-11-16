# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow run snapshots and questions retain their owning run and attempt."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from rich.syntax import Syntax
from textual.widgets import Button, Static

import chrys.app.tui.screens.main.workflow_browser as controller_module
from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState
from chrys.app.tui.widgets import AskUserOptions, AskUserPrompt
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.selection import WorkflowList
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserQuestion
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.workflows.journal import WorkflowJournal
from chrys.service.workflows.outcomes import RunOutcome
from chrys.service.workflows.scheduler import AttemptRef
from tests.app.tui.screens.main._workflow_support import (
    select_archived_run,
    start_workflow,
    workflow_notice_text,
    workflow_selecting,
)
from tests.orchestration.workflows._hosting import make_host, make_profile, make_project, patch_runtime, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.platform_fakes import platform_with_config_dir
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import (
    WorkflowEngine,
    dismiss_workflow_notice,
    open_workflow,
    run_store,
    select_workflow_view,
    switch_mode,
    workflow_selection,
)

LOOP_SOURCE = b"""from chrys.workflows import WorkflowBuilder
def echo(value):
    return value
def done(value):
    return True
def body(scope):
    node = scope.python('echo', echo)
    return node, node
wf = WorkflowBuilder('Loop snapshot')
loop = wf.loop('round', body, until=done, max_iterations=2)
wf.start(loop)
wf.output(loop)
workflow = wf.build()
"""


async def test_chat_selectors_unlock_after_turn_finishes_following_screen_resume(tmp_path: Path) -> None:
    engine = WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        status = main.query_one(StatusBar)
        status.set_profile("Code")
        status.set_model(ModelIndicatorState("Test model", "", "select", "test", True))
        assert status._tags_interactive()
        assert not main._workflow.workflow_mode and main._workflow.session_view.projector.current is None
        assert not main._workflow.awaiting_engine

        await app.push_screen(ConfirmDialog())
        await engine.set_execution(ExecutionSnapshot("turn", cancellable=True), main._services.bus)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main and status._execution_busy, pilot=pilot)
        assert not status._tags_interactive()
        for selector in ("#profile-tag", "#model-tag"):
            assert status.query_one(selector).styles.pointer == "default"
        # Let the timer observe the active lease before releasing it.
        main._workflow.tick()
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await wait_for(status._tags_interactive, pilot=pilot)
        for selector in ("#profile-tag", "#model-tag"):
            assert status.query_one(selector).styles.pointer == "pointer"


@pytest.mark.parametrize("changed_directory", ["workspace", "config"])
async def test_preview_becomes_stale_when_catalog_directory_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_directory: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "local", python_workflow("def echo(value):\n    return value\n", "echo"))
    other = tmp_path / "other"
    other.mkdir()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "local")
        panel = main._workflow_panel
        start = panel.query_one("#workflow-start", Button)
        assert not start.disabled and not panel.stale
        if changed_directory == "workspace":
            await main._services.bus.publish(events.WorkspaceUpdated(primary_cwd=str(other)))
            assert main._workspace_cwd() == str(other)
        else:
            monkeypatch.setattr(
                controller_module,
                "get_platform",
                create_autospec(controller_module.get_platform, return_value=platform_with_config_dir(other)),
            )
        await wait_for(lambda: panel.stale and start.disabled, pilot=pilot)
        assert not workflow_selecting(main)
        await dismiss_workflow_notice(main, pilot, "Reopen")
        async with capture_event_sequence(main._services.bus, events.WorkflowRunRequest) as requests:
            assert not main._workflow.run_control.start("draft")
        assert not requests and not main._workflow.awaiting_engine and not workflow_notice_text(main)
        main._workflow.browser.check_preview(force=True)
        assert panel.stale


@pytest.mark.parametrize("change", ["reorder", "remove", "localize"])
async def test_selection_refresh_tracks_source_or_clamps_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    project = make_project(tmp_path)
    for name in ("first", "last"):
        write_workflow(project, name, python_workflow("def fn(value):\n    return value\n", "fn"))
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        panel = main._workflow_panel
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) > 2, pilot=pilot)
        picker = main._workflow.browser._picker.selection.query_one(WorkflowList)
        picker.highlighted = len(main._workflow.browser._picker.selection.rows) - 1
        selected = main._workflow.browser._picker.selection.selected_row()
        assert selected is not None
        if change == "reorder":
            main._workflow.browser._picker.selection.show_rows(
                list(reversed(main._workflow.browser._picker.selection.rows)), "", preserve_selection=True
            )
            assert picker.highlighted == 0 and main._workflow.browser._picker.selection.selected_row() == selected
        elif change == "remove":
            main._workflow.browser._picker.selection.show_rows(
                main._workflow.browser._picker.selection.rows[:1], "", preserve_selection=True
            )
            assert picker.highlighted == 0
            assert (
                main._workflow.browser._picker.selection.selected_row()
                == main._workflow.browser._picker.selection.rows[0]
            )
        else:
            panel.refresh_localization()
            assert main._workflow.browser._picker.selection.selected_row() == selected


@pytest.mark.parametrize("history_source", ["memory", "disk"])
async def test_reopened_run_keeps_recorded_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, history_source: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = python_workflow("def echo(value):\n    return value\n", "echo", title="Original title")
    write_workflow(project, "echo", source)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        original = await open_workflow(main, pilot, "echo")
        panel = main._workflow_panel
        assert "Original title" in str(panel.border_title)
        assert "Original title" in str(main._workflow_panel.border_title)
        session_id = str(uuid4())
        main._workflow.session_view.selection = workflow_selection(main, session_id)
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        store = run_store(
            directory / "workflows" / new_analytics_id(), original, session_id=session_id, started_at="2026-09-15"
        )
        journal = WorkflowJournal(
            store, main._services.bus if history_source == "memory" else None, session_id=session_id
        )
        try:
            await journal.run_started()
            await journal.finish(RunOutcome.COMPLETED)
        finally:
            await store.close()
        write_workflow(project, "echo", source.replace(b"Original title", b"Revised title"))
        main._workflow.browser.enter_selection()
        revised = await open_workflow(main, pilot, "echo")
        assert revised.title == "Revised title"
        await select_archived_run(main, pilot, store.header.run_id)
        await wait_for(lambda: panel.run_id == store.header.run_id, pilot=pilot)
        assert panel.run_id == store.header.run_id
        header = str(panel.border_title)
        assert "Original title" in header and "Revised title" not in header
        assert "Original title" in str(main._workflow_panel.border_title)


# The badge reads both its iteration and its limit from the run's recorded manifest, whichever way the
# outcome derives the iteration (a verdict or the entry's state); so each revision meets one outcome.
@pytest.mark.parametrize(
    ("outcome", "revision"),
    [(RunOutcome.COMPLETED, "remove_loop"), (RunOutcome.CANCELLED, "change_limit")],
    ids=["completed-remove_loop", "cancelled-change_limit"],
)
async def test_reopened_run_keeps_recorded_loop_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, revision: str, outcome: RunOutcome
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "wf", LOOP_SOURCE)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        original = await open_workflow(main, pilot, "wf")
        session_id = str(uuid4())
        main._workflow.session_view.selection = workflow_selection(main, session_id)
        directory = main._workflow.session_view._session_dir()
        assert directory is not None
        store = run_store(
            directory / "workflows" / new_analytics_id(), original, session_id=session_id, started_at="2026-01-01"
        )
        journal = WorkflowJournal(store, None, session_id=session_id)
        try:
            await journal.loop_iteration(
                AttemptRef(store.header.run_id, "round", "round@iter#1", 1),
                1,
                "exit" if outcome == RunOutcome.COMPLETED else "continue",
            )
            if outcome == RunOutcome.CANCELLED:
                # The second iteration began, but no verdict was produced before cancellation.
                await journal.node_state(
                    AttemptRef(store.header.run_id, "echo", "echo@iter#2", 1), "cancelled", iteration=2
                )
            await journal.finish(outcome)
        finally:
            await store.close()
        revised_source = (
            python_workflow("def echo(value):\n    return value\n", "echo")
            if revision == "remove_loop"
            else LOOP_SOURCE.replace(b"max_iterations=2", b"max_iterations=9")
        )
        write_workflow(project, "wf", revised_source)
        revised = await open_workflow(main, pilot, "wf")
        assert revised.manifest != original.manifest
        panel = main._workflow_panel
        await select_archived_run(main, pilot, store.header.run_id)
        await wait_for(lambda: panel.run_id == store.header.run_id, pilot=pilot)
        assert panel.run_id == store.header.run_id
        assert workflow_notice_text(main) == ""
        graph = panel.query_one(WorkflowGraph)
        assert set(graph.geometry) == {node["id"] for node in original.manifest["nodes"]}
        _, badge_y, _ = graph._badge_positions["round"]
        label = "Iteration 1/2" if outcome == RunOutcome.COMPLETED else "Iteration 2/2"
        assert label in graph.render_line(badge_y - round(graph.scroll_y) + graph.diagram_origin.y).text
        assert str(panel.query_one("#workflow-iterations", Static).content) == f"round {label}"
        await select_workflow_view(main, pilot, "code")
        await wait_for(lambda: panel.code_visible, pilot=pilot)
        code = panel.query_one("#workflow-code-source", Static).content
        assert isinstance(code, Syntax) and code.code == LOOP_SOURCE.decode()
        await pilot.press("escape")
        assert not panel.code_visible and workflow_notice_text(main) == ""


async def test_workflow_agent_question_without_chat_card_stays_answerable_in_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(
        project,
        "agent_question",
        b"""from chrys.workflows import WorkflowBuilder
wf = WorkflowBuilder('Agent question')
node = wf.agent('interview', profile='Headless')
wf.start(node)
wf.output(node)
workflow = wf.build()
""",
    )
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("ask_user", "question_call", {"questions": [{"question": "Which target?"}]})]),
            MockResponse(text="Target confirmed"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    host = make_host(
        tmp_path, project=project, profiles=[make_profile(builtins=["ask_user"])], allow_user_interaction=True
    )
    app = make_chrys_app(tmp_path / "sessions", engine=host.engine, event_bus=host.event_bus)
    try:
        async with app.run_test(size=(120, 45)) as pilot:
            await host.start()
            main = app._main_screen
            assert main is not None
            await wait_for(lambda: not main._state.run.agent_loading and app.screen is main, pilot=pilot)
            await open_workflow(main, pilot, "agent_question")
            await start_workflow(pilot)
            await wait_for(
                lambda: isinstance(app.screen, AskUserDialog) and app.screen.is_mounted,
                pilot=pilot,
                description="workflow agent question dialog and its children are mounted",
                timeout=ENGINE_TURN_TIMEOUT,
            )
            dialog = app.screen
            assert isinstance(dialog, AskUserDialog)
            assert not main.query_one(ChatPanel).is_tool_running("question_call")
            assert not dialog._allow_inline and not dialog.query("#askuser-inline")
            dialog.query_one("#askuser-input", EnhancedTextArea).load_text("staging")
            submit = dialog.query_one("#askuser-submit", Button)
            await wait_for(
                lambda: bool(submit.region) and dialog.region.contains_region(submit.region) and not submit.disabled,
                pilot=pilot,
            )
            await click_when_settled(pilot, "#askuser-submit")
            await wait_for(
                lambda: (
                    main._workflow.session_view.projector.current is not None
                    and main._workflow.session_view.projector.current.finished is not None
                ),
                pilot=pilot,
            )
            await host.engine.wait_for_run_task()
            run = main._workflow.session_view.projector.current
            assert run is not None and run.finished is not None and run.finished.outcome == "completed"
            assert client.call_count == 2 and app.screen is main
    finally:
        await host.shutdown()


STRUCTURED_ASK_SOURCE = python_workflow(
    "from chrys.workflows import Option, Question\n"
    "async def pick(value, ctx):\n"
    "    branch, areas = await ctx.ask([\n"
    "        Question('Which branch?', header='Branch', options=[Option('main', 'the trunk'), 'release']),\n"
    "        Question('Which areas?', header='Areas', options=['API', 'UI'], multi_select=True),\n"
    "    ])\n"
    "    return f'{branch.choice}|{\",\".join(areas.selected)}|{areas.text}'\n",
    "pick",
)


async def test_workflow_structured_questions_resolve_the_node_through_the_dialog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "structured", STRUCTURED_ASK_SOURCE)
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    published: list[events.WorkflowNodeAnswer] = []

    async def answered(event: events.WorkflowNodeAnswer) -> None:
        published.append(event)

    await host.event_bus.subscribe(events.WorkflowNodeAnswer, answered)
    app = make_chrys_app(tmp_path / "sessions", engine=host.engine, event_bus=host.event_bus)
    try:
        async with app.run_test(size=(120, 45)) as pilot:
            await host.start()
            main = app._main_screen
            assert main is not None
            await wait_for(lambda: not main._state.run.agent_loading and app.screen is main, pilot=pilot)
            await open_workflow(main, pilot, "structured")
            await start_workflow(pilot)
            await wait_for(
                lambda: isinstance(app.screen, AskUserDialog) and app.screen.is_mounted,
                pilot=pilot,
                description="the node's questions open in the chat ask dialog",
                timeout=ENGINE_TURN_TIMEOUT,
            )
            rejected = app.screen
            assert isinstance(rejected, AskUserDialog)
            prompt = rejected.query_one(AskUserPrompt)
            assert [(q.header, [o.label for o in q.options], q.multi_select) for q in prompt.questions] == [
                ("Branch", ["main", "release"], False),
                ("Areas", ["API", "UI"], True),
            ]
            run = main._workflow.session_view.projector.current
            assert run is not None
            (request_id,) = run.questions
            # A result that does not fit the questions (two choices on a single-select) never reaches the
            # runner, which would refuse it and leave nothing to answer: the questions open again instead.
            await rejected.dismiss((request_id, (AskUserAnswer(values=("main", "release")), AskUserAnswer())))
            await wait_for(
                lambda: isinstance(app.screen, AskUserDialog) and app.screen is not rejected and app.screen.is_mounted,
                pilot=pilot,
                description="the rejected questions reopen",
            )
            assert not published and not main._workflow.run_control._pending_answers
            dialog = app.screen
            assert isinstance(dialog, AskUserDialog)
            prompt = dialog.query_one(AskUserPrompt)
            branch = dialog.query_one("#askuser-q0-options", AskUserOptions)
            await wait_for(lambda: branch.has_focus, pilot=pilot, description="first question option focus")
            branch.toggle(branch.get_option_at_index(0))  # a single-select choice moves on by itself
            await wait_for(lambda: prompt.active_index == 1, pilot=pilot)
            areas = dialog.query_one("#askuser-q1-options", AskUserOptions)
            areas.toggle(areas.get_option_at_index(1))
            await pilot.pause()
            areas.toggle(areas.get_option_at_index(0))
            dialog.query_one("#askuser-input", EnhancedTextArea).insert("both, please")
            await pilot.pause()
            dialog.query_one("#askuser-submit", Button).press()
            await wait_for(lambda: prompt.active_index == 2, pilot=pilot, description="the review pane")
            dialog.query_one("#askuser-submit", Button).press()
            await wait_for(lambda: run.finished is not None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
            # The finished event precedes closing the run store; the result is kept once the run task ends.
            await host.engine.workflows.wait_idle()
            assert run.finished is not None and run.finished.outcome == "completed"
            result = host.engine.workflows.result(run.started.run_id)
            assert result is not None
            # The dialog hands back click order; the node receives its selection in option order.
            assert [answer.answers for answer in published] == [
                (AskUserAnswer(values=("main",)), AskUserAnswer(values=("UI", "API"), note="both, please"))
            ]
            assert [output.value.text for output in result.outputs] == ["main|API,UI|both, please"]
            assert app.screen is main
    finally:
        await host.event_bus.unsubscribe(events.WorkflowNodeAnswer, answered)
        await host.shutdown()


# Any state but running retires the attempt's questions in the projector, and the dialog closes from
# that projection alone, whatever the state; so each state runs once and each presentation meets one.
@pytest.mark.parametrize(
    ("state", "presentation"),
    [("awaiting_retry", "open"), ("failed", "covered"), ("cancelled", "queued"), ("completed", "open")],
)
async def test_workflow_question_expires_with_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, presentation: str, state: str
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
        await bus.publish(events.WorkflowRunStarted(run_id="run"))
        attempt = events.WorkflowNodeStateChanged(
            run_id="run", node_id="interview", activation_id="interview@iter#2", attempt=2, state="running"
        )
        await bus.publish(attempt)
        if presentation == "queued":
            await app.push_screen(ConfirmDialog())
        question = events.WorkflowNodeAskUser(
            run_id="run",
            node_id=attempt.node_id,
            activation_id=attempt.activation_id,
            attempt=attempt.attempt,
            request_id="question",
            questions=(AskUserQuestion("Which target?"),),
        )
        await bus.publish(question)
        run = main._workflow.session_view.projector.current
        assert run is not None
        dialog = None
        if presentation != "queued":
            await wait_for(lambda: isinstance(app.screen, AskUserDialog) and app.screen.is_mounted, pilot=pilot)
            dialog = app.screen
            assert isinstance(dialog, AskUserDialog)
        if presentation == "covered":
            await app.push_screen(ConfirmDialog())
        # A different activation or attempt cannot expire this question.
        await bus.publish(replace(attempt, activation_id="interview@iter#1", state=state))
        await bus.publish(replace(attempt, attempt=1, state=state))
        assert run.questions == {question.request_id: question}
        # Drive the exact state boundary emitted by timeout/cancellation, without a wall-clock race.
        await bus.publish(replace(attempt, state=state))
        assert not run.questions
        assert run.finished is None
        if dialog is not None:
            await wait_for(lambda: dialog._dismiss_requested, pilot=pilot)
        if presentation in {"queued", "covered"}:
            assert isinstance(app.screen, ConfirmDialog)
            await pilot.press("escape")
        await wait_for(lambda: app.screen is main and main._workflow.run_control._question is None, pilot=pilot)
        main._workflow.refresh()
        assert app.screen is main and not answers


@pytest.mark.parametrize("older_run", [False, True])
async def test_restore_missing_run_shows_a_meaningful_notice(tmp_path: Path, older_run: bool) -> None:
    from chrys.app.tui.widgets.workflow import text
    from chrys.service.state.workflow import WorkflowSessionState
    from tests.support.workflow_history import record_workflow_run, workflow_state

    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        store = main._services.state_store
        assert store is not None
        session_id, missing = str(uuid4()), new_analytics_id()
        await store.save_workflow_session(
            session_id,
            WorkflowSessionState.decode(workflow_state(tmp_path, run_count=1, latest_run_id=missing)),
        )
        if older_run:
            await record_workflow_run(
                store.session_dir(session_id) / "workflows" / new_analytics_id(),
                session_id=session_id,
                title="Older",
                outcome="completed",
            )
        await switch_mode(main, pilot)
        await main._workflow.session_view.restore_session(session_id, run_id=missing if older_run else "")
        await dismiss_workflow_notice(main, pilot, text.render(text.NO_RECORD.bind(), app.locale_controller))
        assert app.screen is main
