# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow selection, request races, transcript isolation and real coordinator runs in ChrysApp."""

from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec

import pytest
from textual.widgets import Button, Static

from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.screens.dialogs.workflow_result import WorkflowResultDialog
from chrys.app.tui.widgets.chat.agent_transcript_surface import TranscriptAssistantOp, TranscriptToolResultOp
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.app.tui.widgets.workflow.panel import WorkflowPanel
from chrys.app.tui.widgets.workflow.selection import WorkflowList, WorkflowRow
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.workflows.ledger import ledger_path
from tests.app.tui.screens.main._workflow_support import (
    WorkflowEngine,
    confirm_workflow_cancel,
    dismiss_workflow_notice,
    open_workflow,
    select_workflow_view,
    start_workflow,
    switch_mode,
    workflow_notice_text,
    workflow_selecting,
    workflow_selection,
)
from tests.orchestration.workflows._hosting import make_host, make_profile, make_project, patch_runtime, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import CONDITIONAL_LOOP_WORKFLOW, python_workflow


async def test_mode_drafts_selection_confirmation_and_keyboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "first", python_workflow("def fn(value):\n    return value\n", "fn", title="A [workflow]"))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        composer = main.query_one(InputBar)
        composer.replace_draft("chat draft")
        await switch_mode(main, pilot)
        await click_when_settled(pilot, "#workflow-new")
        panel = main.query_one(WorkflowPanel)
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 2, pilot=pilot)
        assert panel.display and not main.query_one(ChatPanel).display
        assert not composer.display and composer.snapshot_draft().text == "chat draft"
        picker = main._workflow.browser._picker.selection.query_one(WorkflowList)
        picker.focus()
        picker.highlighted = next(
            i for i, row in enumerate(main._workflow.browser._picker.selection.rows) if row.workflow_id == "first"
        )
        await pilot.press("j", "k", "enter")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        dialog = app.screen
        assert isinstance(dialog, WorkflowConfirmDialog)
        assert dialog.preview.source.workflow_id == "first"  # The module has not run yet.
        assert "def fn" in dialog.preview.source.source.decode()
        await wait_for(lambda: bool(dialog.query("#workflow-confirm-yes")), pilot=pilot)
        await click_when_settled(pilot, "#workflow-confirm-yes")
        await wait_for(lambda: not workflow_selecting(main), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        assert panel.preview is not None
        assert panel.preview.title == "A [workflow]"
        assert main._workflow.browser.catalog.ledger().is_confirmed(panel.preview.ledger_entry())
        await wait_for(
            lambda: not panel.query_one("#workflow-start", Button).disabled,
            pilot=pilot,
            description="confirmed workflow controls reflect the completed preview",
        )
        assert not panel.query_one("#workflow-start", Button).disabled
        assert not panel.query("#workflow-code")
        graph = panel.query_one(WorkflowGraph)
        assert not graph.selected_node
        await switch_mode(main, pilot)
        assert composer.snapshot_draft().text == "chat draft"
        await switch_mode(main, pilot)
        assert not composer.display and composer.snapshot_draft().text == "chat draft"
        scan_started, release_scan = Event(), Event()
        selection_rows = main._workflow.browser._selection_rows

        def delayed_rows(catalog: WorkflowCatalog) -> tuple[list[WorkflowRow], str]:
            scan_started.set()
            assert release_scan.wait(5), "selection scan was not released"
            return selection_rows(catalog)

        with monkeypatch.context() as scanning:
            scanning.setattr(main._workflow.browser, "_selection_rows", delayed_rows)
            try:
                main._suggestions.dispatch_slash_command("/workflow")
                await wait_for(scan_started.is_set, pilot=pilot)
                # Opening the shell does not mean its background scan has populated rows.
                assert workflow_selecting(main) and not main._workflow.browser._picker.selection.rows
            finally:
                release_scan.set()
            await wait_for(
                lambda: any(row.workflow_id == "first" for row in main._workflow.browser._picker.selection.rows),
                pilot=pilot,
            )
        picker = main._workflow.browser._picker.selection.query_one(WorkflowList)
        picker.focus()
        index = next(
            i for i, row in enumerate(main._workflow.browser._picker.selection.rows) if row.workflow_id == "first"
        )
        option = picker.get_option_at_index(index)
        assert option.disabled
        assert str(option.prompt) == "◦ A [workflow]\n  - first · project"
        picker.highlighted = index
        await pilot.press("enter")
        assert isinstance(app.screen, WorkflowPickerDialog)
        await click_when_settled(pilot, picker, offset=(2, picker._index_to_line[index] - picker.scroll_offset.y))
        assert isinstance(app.screen, WorkflowPickerDialog)
        await pilot.press("home", "enter")
        await wait_for(lambda: not workflow_selecting(main), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        assert panel.preview is not None and panel.preview.source.workflow_id == "demo-workflow"
        assert not main._workflow.session_id and not panel.run_ids
        main._suggestions.dispatch_slash_command("/workflow")
        await wait_for(lambda: workflow_selecting(main), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        assert "workflow" in {command.name for command in main._suggestions._slash_commands}


async def test_start_latch_reply_correlation_covered_projection_and_transcripts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    bus = EventBus()
    engine = WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    requests: list[events.WorkflowRunRequest] = []
    cancels: list[events.WorkflowCancelRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    async def cancelled(event: events.WorkflowCancelRequest) -> None:
        cancels.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    await bus.subscribe(events.WorkflowCancelRequest, cancelled)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        panel = main.query_one(WorkflowPanel)
        await wait_for(lambda: bool(main._workflow.browser._picker.selection.rows), pilot=pilot)
        main._workflow.browser._picker.selection.query_one(WorkflowList).focus()
        await pilot.press("enter")
        await wait_for(lambda: panel.preview is not None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        assert app.screen is main
        assert not ledger_path(main._workflow.browser.catalog.config_dir).exists()
        start = panel.query_one("#workflow-start", Button)
        await wait_for(
            lambda: start.region.width > 0 and app.get_widget_at(start.region.x, start.region.y)[0] is start,
            pilot=pilot,
        )
        await start_workflow(pilot, "  review me\n")
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        main._on_workflow_start()
        assert len(requests) == 1 and main._workflow.run_control._pending_run.request_id == requests[0].request_id
        assert panel.query_one("#workflow-start", Button).disabled
        await bus.publish(events.WorkflowRunRejected(request_id="unrelated", error="wrong"))
        assert main._workflow.awaiting_engine
        await bus.publish(
            events.WorkflowRunRejected(request_id=requests[0].request_id, error="invalid", message="Try again")
        )
        await wait_for(lambda: workflow_notice_text(main) == "invalid: Try again", pilot=pilot)
        await dismiss_workflow_notice(main, pilot, "invalid: Try again")
        await wait_for(lambda: not panel.query_one("#workflow-start", Button).disabled, pilot=pilot)
        await start_workflow(pilot, "  review me\n")
        await wait_for(lambda: len(requests) == 2, pilot=pilot)
        assert requests[0].input_text == requests[1].input_text == "  review me\n"
        await engine.set_execution(ExecutionSnapshot("workflow", "run1", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=requests[1].request_id, run_id="run1", selection=workflow_selection(main, "workflow-session")
            )
        )
        assert panel.preview is not None
        source = panel.preview.source
        await bus.publish(
            events.WorkflowRunStarted(
                run_id="run1",
                workflow_id=source.workflow_id,
                canonical_path=source.canonical_path,
                title=panel.preview.title,
                manifest=panel.preview.manifest,
            )
        )
        await wait_for(lambda: not panel.query_one("#workflow-stop", Button).disabled, pilot=pilot)
        graph = panel.query_one(WorkflowGraph)
        node_id = next(node["id"] for node in panel.preview.manifest["nodes"] if node["kind"] == "agent")
        main._workflow.session_view.open_node(node_id)
        await wait_for(lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted, pilot=pilot)
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        restyle = create_autospec(graph._node_spans, side_effect=graph._node_spans)
        monkeypatch.setattr(graph, "_node_spans", restyle)
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run1",
                node_id=node_id,
                activation_id=f"{node_id}@1",
                attempt=1,
                state="running",
                invocation_id="child",
            )
        )
        origin = InvocationOrigin("workflow_node", "", "child", None, attempt=1)
        await bus.publish(events.InvocationMessage(origin=origin, text="workflow transcript only"))
        await bus.publish(
            events.InvocationToolCallStart(origin=origin, call_id="tool", tool_name="check", tool_kind="shell")
        )
        await bus.publish(
            events.InvocationToolCallResult(
                origin=origin, call_id="tool", tool_name="check", result="Error: failed", metadata={"failed": True}
            )
        )
        await bus.publish(
            events.WorkflowNodeOutput(run_id="run1", node_id=node_id, kind="emit", ordinal=1, summary_text="fragment")
        )
        await wait_for(lambda: dialog.selected is not None and dialog.selected.state == "running", pilot=pilot)
        assert restyle.call_count == 0
        run = main._workflow.session_view.projector.current
        assert run is not None
        first, last = run.journals["child", 1].operations[0], run.journals["child", 1].operations[-1]
        assert isinstance(first, TranscriptAssistantOp) and first.text == "workflow transcript only"
        assert isinstance(last, TranscriptToolResultOp) and last.canonical_status == "failed"
        assert not main.query_one(ChatPanel).query("AgentMessage")
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main and restyle.call_count > 0, pilot=pilot)
        composer = main.query_one(InputBar)
        composer.replace_draft("  keep this draft\n")
        composer.focus_input()
        await pilot.press("enter")
        assert composer.snapshot_draft().text == "  keep this draft\n"
        before = main._services.execution()
        main._suggestions.dispatch_slash_command("/new")
        assert main._services.execution() == before
        assert panel.query_one("#workflow-start", Button).disabled
        await click_when_settled(pilot, "#workflow-stop")
        await confirm_workflow_cancel(pilot)
        await wait_for(lambda: len(cancels) == 1, pilot=pilot)
        assert cancels[0].run_id == "run1"
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await bus.publish(events.WorkflowRunFinished(run_id="run1", outcome="cancelled"))
        await wait_for(lambda: not panel.query_one("#workflow-start", Button).disabled, pilot=pilot)
        assert panel.query_one("#workflow-stop", Button).disabled


async def test_builtin_demo_runs_from_selection_to_persisted_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    tour_text = "Start at the entry point. " * 60
    agents = [MockChatClient(responses=[MockResponse(text=text)]) for text in ("Reader notes", tour_text)]
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), *agents])
    host = make_host(
        tmp_path, project=project, profiles=[make_profile(), make_profile("QA")], allow_user_interaction=True
    )
    app = make_chrys_app(tmp_path / "sessions", engine=host.engine, event_bus=host.event_bus)
    try:
        async with app.run_test(size=(120, 45)) as pilot:
            await host.start()
            main = app._main_screen
            assert main is not None
            await wait_for(lambda: not main._state.run.agent_loading and app.screen is main, pilot=pilot)
            main.action_workflow()
            panel = main.query_one(WorkflowPanel)
            await wait_for(lambda: bool(main._workflow.browser._picker.selection.rows), pilot=pilot)
            picker = main._workflow.browser._picker.selection.query_one(WorkflowList)
            picker.highlighted = next(
                i
                for i, row in enumerate(main._workflow.browser._picker.selection.rows)
                if row.workflow_id == "demo-workflow"
            )
            picker.focus()
            await pilot.press("enter")
            await wait_for(lambda: panel.preview is not None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
            # The demo's own input convention settles both of its questions up front.
            await start_workflow(pilot, "interactive: false\nWhere does it start?")
            await wait_for(
                lambda: (
                    main._workflow.session_view.projector.current is not None
                    and main._workflow.session_view.projector.current.finished is not None
                ),
                pilot=pilot,
                timeout=20,
            )
            await host.engine.wait_for_run_task()
            run = main._workflow.session_view.projector.current
            assert run is not None and run.finished is not None
            assert run.finished.outcome == "completed"
            assert len(run.journals) == 2
            assert not panel.output_visible
            await select_workflow_view(main, pilot, "output")
            await wait_for(lambda: tour_text in str(panel.query_one("#workflow-outputs", Static).content), pilot=pilot)
            output = panel.query_one("#workflow-outputs", Static).content
            assert tour_text in str(output)
            assert all(client.call_count == 1 for client in agents)
            assert not main.query_one(ChatPanel).query("AgentMessage")
            # A quick tour has one output; Result, beside Cancel on the Graph tab, reads its stored value in full.
            await select_workflow_view(main, pilot, "graph")
            result = panel.query_one("#workflow-result", Button)
            assert result.visible
            await click_when_settled(pilot, result)
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowResultDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query(VirtualizedMarkdown))
                ),
                pilot=pilot,
            )
            assert str(app.screen.query_one("#workflow-result-frame").border_title) == "Result · render_tour"
            assert tour_text in app.screen.query_one(VirtualizedMarkdown).source
    finally:
        await host.shutdown()


async def test_shadowed_sources_warnings_and_preview_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.foundation.platform import get_platform
    from chrys.service.workflows.discovery import global_workflows_dir

    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "demo-workflow", b'print("load output")\nraise RuntimeError("broken preview")\n')
    global_dir = global_workflows_dir(get_platform().config_dir)
    global_dir.mkdir(parents=True)
    atomic_write_owner_only_bytes(
        global_dir / "demo-workflow.py", python_workflow("def fn(value):\n    return value\n", "fn")
    )
    from chrys.service.workflows import discovery
    from chrys.service.workflows.discovery import SourceLayout, WorkflowSource

    atomic_write_owner_only_bytes(global_dir / "unreadable.py", b"unreadable")
    real_read = discovery.read_source

    def read_source(path: Path, source_kind: str, *, layout: SourceLayout) -> WorkflowSource:
        if path.name == "unreadable.py":
            raise PermissionError("unreadable test file")
        return real_read(path, source_kind, layout=layout)

    monkeypatch.setattr(discovery, "read_source", create_autospec(real_read, side_effect=read_source))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        panel = main.query_one(WorkflowPanel)
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 4, pilot=pilot)
        assert [
            row.shadowed for row in main._workflow.browser._picker.selection.rows if row.workflow_id == "demo-workflow"
        ] == [False, True, True]
        assert "unreadable.py" in str(
            main._workflow.browser._picker.selection.query_one("#workflow-warnings", Static).content
        )
        picker = main._workflow.browser._picker.selection.query_one(WorkflowList)
        picker.highlighted = next(
            i for i, row in enumerate(main._workflow.browser._picker.selection.rows) if row.shadowed
        )
        picker.focus()
        await pilot.press("enter")
        assert panel.preview is None and workflow_selecting(main)
        picker.highlighted = next(
            i
            for i, row in enumerate(main._workflow.browser._picker.selection.rows)
            if row.workflow_id == "demo-workflow" and not row.shadowed
        )
        await pilot.press("enter")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        await click_when_settled(pilot, "#workflow-confirm-yes")
        await wait_for(lambda: "broken preview" in workflow_notice_text(main), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        assert "Traceback" in workflow_notice_text(main) and "load output" in workflow_notice_text(main)
        await dismiss_workflow_notice(main, pilot, "broken preview")
        assert workflow_selecting(main)
        write_workflow(project, "another", python_workflow("def fn(value):\n    return value\n", "fn"))
        main.action_workflow()
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 5, pilot=pilot)


@pytest.mark.parametrize("leave", ["mode", "selection"])
async def test_preview_cancellation_waits_for_cleanup_and_cannot_open_a_late_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, leave: str
) -> None:
    from chrys.orchestration.workflows.preview import WorkflowPreview

    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    ready, cancelling, release, cleaned = (asyncio.Event() for _ in range(4))
    real_preview = WorkflowCatalog.preview

    async def preview(
        catalog: WorkflowCatalog,
        workflow_id: str,
        *,
        timeout: float | None = None,
        request_id: str = "",
        expected_identity=None,
        authorize=None,
    ) -> WorkflowPreview:
        assert timeout == 30.0
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelling.set()
            await release.wait()
            cleaned.set()
        raise AssertionError("cancelled preview cannot return")

    monkeypatch.setattr(WorkflowCatalog, "preview", create_autospec(real_preview, side_effect=preview))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        panel = main.query_one(WorkflowPanel)
        await wait_for(lambda: bool(main._workflow.browser._picker.selection.rows), pilot=pilot)
        main._workflow.browser._picker.selection.query_one(WorkflowList).focus()
        await pilot.press("enter")
        await wait_for(ready.is_set, pilot=pilot)
        task = main._workflow.browser.preview_flow.task
        assert task is not None
        if leave == "mode":
            # A modal owns input while loading; exercise an external mode transition.
            main._set_workflow_mode(False)
        else:
            main.action_workflow()
        await wait_for(cancelling.is_set, pilot=pilot)
        assert not task.done() and not cleaned.is_set()
        release.set()
        await wait_for(task.done, pilot=pilot)
        assert cleaned.is_set() and task.cancelled()
        assert panel.preview is None and workflow_selecting(main)
        assert app.screen is main if leave == "mode" else isinstance(app.screen, WorkflowPickerDialog)


async def test_preview_deadline_and_confirmation_cancel_stay_in_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.orchestration.workflows.preview import WorkflowPreview

    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "slow", b"import time\ntime.sleep(60)\n")
    real_preview = WorkflowCatalog.preview

    async def preview(
        catalog: WorkflowCatalog,
        workflow_id: str,
        *,
        timeout: float | None = None,
        request_id: str = "",
        expected_identity=None,
        trust: bool = False,
        authorize=None,
    ) -> WorkflowPreview:
        # Time out a second after the source is trusted. Discovery and inspection before the Trust
        # dialog count against the preview deadline, and a loaded runner can spend a short one
        # there, so the dialog would never open.
        deadline = asyncio.timeout(None)

        async def authorize_then_start_deadline(inspection) -> bool:
            accepted = await authorize(inspection)
            if accepted:
                deadline.reschedule(asyncio.get_running_loop().time() + 1.0)
            return accepted

        async with deadline:
            return await real_preview(
                catalog,
                workflow_id,
                timeout=timeout,
                request_id=request_id,
                expected_identity=expected_identity,
                trust=trust,
                authorize=None if authorize is None else authorize_then_start_deadline,
            )

    monkeypatch.setattr(WorkflowCatalog, "preview", create_autospec(real_preview, side_effect=preview))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        panel = main.query_one(WorkflowPanel)
        await wait_for(lambda: len(main._workflow.browser._picker.selection.rows) == 2, pilot=pilot)
        main._workflow.browser.open("slow")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        await click_when_settled(pilot, "#workflow-confirm-yes")
        await wait_for(lambda: "timed out" in workflow_notice_text(main), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        await dismiss_workflow_notice(main, pilot, "timed out")
        assert workflow_selecting(main) and panel.preview is None
        write_workflow(project, "slow", python_workflow("def fn(value):\n    return value\n", "fn"))
        main._workflow.browser.open("slow", input_text="start after loading")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert workflow_selecting(main) and not ledger_path(main._workflow.browser.catalog.config_dir).exists()
        main._workflow.browser.open("demo-workflow")
        await wait_for(
            lambda: panel.preview is not None and app.screen is main, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
        )
        assert not main._workflow.awaiting_engine and not panel.run_ids


async def test_empty_workflow_input_lease_guards_and_escape_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
    from chrys.app.tui.widgets.chrome.status_bar import StatusBar

    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    bus = EventBus()
    requests: list[events.WorkflowRunRequest] = []
    cancellations: list[events.WorkflowCancelRequest] = []
    changes: list[events.Event] = []

    async def start(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    async def cancel(event: events.WorkflowCancelRequest) -> None:
        cancellations.append(event)

    async def changed(event: events.Event) -> None:
        changes.append(event)

    await bus.subscribe(events.WorkflowRunRequest, start)
    await bus.subscribe(events.WorkflowCancelRequest, cancel)
    for event_type in (events.AgentProfileSwitch, events.SessionNew, events.WorkspaceChange, events.SettingsReload):
        await bus.subscribe(event_type, changed)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        main.action_workflow()
        panel = main.query_one(WorkflowPanel)
        await pilot.press("escape")
        main._on_workflow_start()
        await wait_for(lambda: "Pick a workflow" in workflow_notice_text(main), pilot=pilot)
        await dismiss_workflow_notice(main, pilot, "Pick a workflow")
        main.action_workflow()
        await wait_for(lambda: bool(main._workflow.browser._picker.selection.rows), pilot=pilot)
        await click_when_settled(pilot, "#workflow-list", offset=(2, 1))
        await wait_for(lambda: panel.preview is not None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        await start_workflow(pilot)
        main._on_workflow_start()
        main._on_workflow_start()
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        assert requests[0].input_text == ""
        await engine.set_execution(ExecutionSnapshot("workflow", "empty-run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=requests[0].request_id,
                run_id="empty-run",
                selection=workflow_selection(main, "workflow-session"),
            )
        )
        await wait_for(
            lambda: (
                main._execution_binding_busy == (engine.snapshot.kind != "idle")
                and not main.query_one(StatusBar)._tags_interactive()
            ),
            pilot=pilot,
        )
        assert str(main.query_one("#mode-badge", Static).content) == " APP MODE: Workflow "
        assert not main.query_one(StatusBar)._tags_interactive()
        await main._config_actions.switch_agent_profile("QA")
        await main._config_actions.on_model_picked("another-model")
        await main._sessions.create_new_session()
        main._suggestions.dispatch_slash_command("/clear")
        main._suggestions.dispatch_slash_command(f"/cd {tmp_path}")
        assert changes == []
        # /cd opens the bound-session notice; close it before cancelling the run.
        await wait_for(lambda: main._workflow.browser._workspace_notice is app.screen, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await pilot.press("escape")
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: len(cancellations) == 1, pilot=pilot)
        assert cancellations[0].run_id == "empty-run"


async def test_late_acceptance_stays_with_its_source_and_frames_keep_all_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "other", python_workflow("def fn(value):\n    return value\n", "fn"))
    bus = EventBus()
    engine = WorkflowEngine()
    requests: list[events.WorkflowRunRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        panel = main.query_one(WorkflowPanel)
        original = panel.preview
        assert original is not None
        await start_workflow(pilot)
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        main.action_workflow()
        main._workflow.browser.open("other")
        assert panel.preview is original  # Admission fixes the selected workflow.
        await engine.set_execution(ExecutionSnapshot("workflow", "first", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                run_id="first",
                request_id=requests[0].request_id,
                selection=workflow_selection(main, "workflow-session"),
            )
        )
        await bus.publish(
            events.WorkflowRunStarted(
                run_id="first",
                canonical_path=original.source.canonical_path,
                workflow_id="demo-workflow",
                manifest=original.manifest,
            )
        )
        assert panel.run_id == "first"
        main.action_workflow()
        main._workflow.browser.open("demo-workflow")
        await dismiss_workflow_notice(main, pilot, "Create a new session")
        await wait_for(lambda: panel.run_id == "first", pilot=pilot)
        await wait_for(
            lambda: (
                main._execution_binding_busy == (engine.snapshot.kind != "idle") and not main._workflow._refresh_pending
            ),
            pilot=pilot,
        )
        graph = panel.query_one(WorkflowGraph)
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="write_tour",
                activation_id="a",
                attempt=1,
                state="running",
                invocation_id="original-child",
            )
        )
        # Usage fills the manifest's reserved compartment without changing geometry.
        await wait_for(lambda: "write_tour" in graph._usage_labels and not main._workflow._refresh_pending, pilot=pilot)
        styles = create_autospec(graph._node_spans, side_effect=graph._node_spans)
        monkeypatch.setattr(graph, "_node_spans", styles)
        diagram, geometry, scroll = graph.diagram, graph.geometry, graph.scroll_offset
        for ordinal in range(100):
            await bus.publish(
                events.WorkflowNodeOutput(
                    run_id="first",
                    node_id="write_tour",
                    activation_id="a",
                    kind="emit",
                    attempt=1,
                    ordinal=ordinal,
                    summary_text=str(ordinal),
                )
            )
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="first",
                node_id="write_tour",
                activation_id="a",
                attempt=1,
                state="awaiting_retry",
                invocation_id="original-child",
            )
        )
        await wait_for(lambda: styles.call_count > 0, pilot=pilot)
        assert styles.call_count == 1
        assert graph.diagram is diagram and graph.geometry is geometry and graph.scroll_offset == scroll
        first = main._workflow.session_view.projector.current
        assert first is not None and len(first.facts) == 103
        assert panel.query_one("#workflow-start", Button).disabled
        assert not panel.query_one("#workflow-stop", Button).disabled
        for run_id in ("second", "third"):
            await bus.publish(events.WorkflowRunStarted(run_id=run_id, manifest=original.manifest))
            await bus.publish(
                events.WorkflowNodeStateChanged(
                    run_id=run_id, node_id="write_tour", state="running", invocation_id=run_id
                )
            )
        assert main._workflow.session_view.projector.run("first") is None
        assert main._workflow.session_view.projector.run("second") is not None
        assert main._workflow.session_view.projector.run("third") is not None
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await bus.publish(events.SessionReady(session_id="new-session"), raise_handler_errors=True)
        assert main._workflow.session_view.projector.run("third") is not None
        assert await main._workflow.session_view.new_session()
        assert (
            main._workflow.session_view.projector.current is None
            and main._workflow.session_view.projector.previous is None
        )
        assert not workflow_selecting(main) and not panel.run_id and panel.preview is original


async def test_manifest_warning_is_visible_after_trusted_source_is_loaded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    write_workflow(project, "conditional", CONDITIONAL_LOOP_WORKFLOW)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await switch_mode(main, pilot)
        main._workflow.browser.load_preview("conditional")
        await wait_for(
            lambda: isinstance(app.screen, WorkflowConfirmDialog) and app.screen.is_mounted,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        dialog = app.screen
        assert isinstance(dialog, WorkflowConfirmDialog)
        assert not dialog.query(".workflow-manifest-warning")  # Computed only after source trust.
        await click_when_settled(pilot, "#workflow-confirm-yes")
        panel = main.query_one(WorkflowPanel)
        await wait_for(
            lambda: panel.preview is not None and app.screen is main, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
        )
        warning = panel.query_one("#workflow-manifest-warnings", Static)
        assert warning.display and "loop_no_value" in str(warning.content)
        assert main._workflow.browser._picker is None  # Discovery notices have separate ownership.
