# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow cancellation settles live and reopened node process transcripts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import Button, TabbedContent

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.messages import InterruptedMessage
from chrys.app.tui.widgets.chat.tool_call import ToolGroup, ToolGroupTitle
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import title_runs
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, confirm_workflow_cancel, open_workflow


@pytest.mark.parametrize("detail_open", [True, False])
async def test_stop_settles_node_process_and_rejects_late_activity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detail_open: bool
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.show_preview(preview, run_id="run")
        main._workflow.session_view._run_ids = ["run"]
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        running = events.WorkflowNodeStateChanged(
            run_id="run",
            node_id="architecture",
            activation_id="architecture@iter#1",
            attempt=1,
            state="running",
            invocation_id="review",
        )
        await bus.publish(running)
        completed = replace(
            running, node_id="entry_points", activation_id="entry_points@iter#1", invocation_id="security"
        )
        await bus.publish(completed)
        origin = InvocationOrigin("workflow_node", "", "review", None, attempt=1)
        for call_id in ("finished", "pending"):
            await bus.publish(
                events.InvocationToolCallStart(
                    origin=origin,
                    call_id=call_id,
                    tool_name="read_file",
                    tool_kind="filesystem.read",
                    args={"path": f"{call_id}.py"},
                )
            )
            if call_id == "finished":
                await bus.publish(
                    events.InvocationToolCallResult(
                        origin=origin, call_id=call_id, tool_name="read_file", result="completed result"
                    )
                )
        await bus.publish(events.InvocationCompactionStarted(origin=origin, compaction_id="compaction"))
        await bus.publish(
            events.InvocationMessage(
                origin=InvocationOrigin("workflow_node", "", "security", None, attempt=1),
                text="Finished review",
                is_final=True,
            )
        )
        await bus.publish(replace(completed, state="completed"))

        if detail_open:
            main._workflow.session_view.open_node(running.node_id)
            await wait_for(
                lambda: (
                    isinstance(app.screen, WorkflowNodeDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query(TabbedContent))
                ),
                pilot=pilot,
            )
            dialog = app.screen
            assert isinstance(dialog, WorkflowNodeDialog)
            dialog.query_one(TabbedContent).active = "workflow-transcript-tab"
            await wait_for(lambda: bool(dialog.query(ToolGroup)), pilot=pilot)
            group = dialog.query_one(ToolGroup)
            await wait_for(
                lambda: (
                    group.is_tool_running("pending") and "pending.py" in group.query_one(ToolGroupTitle).render().plain
                ),
                pilot=pilot,
                description="running tool activity header rendered",
            )

        cancels: list[events.WorkflowCancelRequest] = []

        async def cancelled(event: events.WorkflowCancelRequest) -> None:
            cancels.append(event)
            # The scheduler publishes this terminal state before draining tools;
            # the invocation identity may be omitted on a subsequent state fact.
            await bus.publish(replace(running, state="cancelled", invocation_id=""))
            await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="cancelled"))
            await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)

        await bus.subscribe(events.WorkflowCancelRequest, cancelled)
        main._workflow.run_control.stop()
        await confirm_workflow_cancel(pilot)
        await wait_for(lambda: engine.snapshot.kind == "idle", pilot=pilot)
        assert len(cancels) == 1 and cancels[0].run_id == "run"
        run = main._workflow.session_view.projector.current
        assert run is not None
        journal = run.journals["review", 1]
        stopped_operations = journal.operations
        # Duplicate terminal facts and late provider callbacks cannot revive the
        # transcript or add a second interruption notice.
        await bus.publish(replace(running, state="cancelled"))
        late = events.InvocationToolCallStart(
            origin=origin, call_id="late", tool_name="read_file", tool_kind="filesystem.read"
        )
        await bus.publish(late)
        await bus.publish(events.InvocationToolCallStatusUpdated(origin=origin, call_id="pending", status="running"))
        await bus.publish(
            events.InvocationToolCallResult(origin=origin, call_id="pending", tool_name="read_file", result="too late")
        )
        assert journal.operations == stopped_operations
        assert len(run.journals["security", 1].operations) == 1

        if not detail_open:
            main._workflow.session_view.open_node(running.node_id)
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowNodeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query(TabbedContent))
            ),
            pilot=pilot,
        )
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        dialog.query_one(TabbedContent).active = "workflow-transcript-tab"
        await wait_for(lambda: bool(dialog.query(InterruptedMessage)), pilot=pilot)
        surface = dialog.query_one(AgentTranscriptSurface)
        group = surface.query_one(ToolGroup)
        await wait_for(
            lambda: (
                "pending.py" not in group.query_one(ToolGroupTitle).render().plain
                and not title_runs(group.query_one(ToolGroupTitle))
            ),
            pilot=pilot,
            description="cancelled tool activity header settled",
        )
        assert group.all_complete
        assert not group.is_tool_running("pending")
        assert group._tool_records["pending"].canonical_status == "interrupted"
        assert group._tool_records["finished"].canonical_status == "completed"
        assert group._tool_records["finished"].result == "completed result"
        assert "pending.py" not in group.query_one(ToolGroupTitle).render().plain
        title = group.query_one(ToolGroupTitle)
        assert not title_runs(title)
        assert title._auto_refresh_timer is None or not title._auto_refresh_timer._active.is_set()
        assert surface.query_one(CompactionCard).status != "running"
        assert len(surface.query(InterruptedMessage)) == 1
        notice = surface.query_one(InterruptedMessage)
        assert notice._reason == "cancelled" and "Interrupted" in notice._header
        assert not notice.query(Button)

        # Reopening the same cancelled invocation replays the terminal operation.
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        main._workflow.session_view.open_node(running.node_id)
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowNodeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query(TabbedContent))
            ),
            pilot=pilot,
        )
        await wait_for(lambda: bool(app.screen.query(InterruptedMessage)), pilot=pilot)
        assert app.screen.query_one(ToolGroup).all_complete
