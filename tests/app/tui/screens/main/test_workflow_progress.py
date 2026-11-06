# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow agent lifecycle messages reuse chat rendering and survive detail reopen."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from textual.widgets import Button, Static, TabbedContent

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.compaction_card import CompactionCard
from chrys.app.tui.widgets.chat.messages import (
    AgentMessage,
    ErrorMessage,
    InterruptedMessage,
    RetryMessage,
    SystemMessage,
    UserMessage,
)
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, open_workflow


@pytest.mark.parametrize("detail_open", [True, False])
@pytest.mark.parametrize("state", ["failed", "retrying", "awaiting_retry"])
async def test_agent_failure_uses_chat_error_and_retry_recovers_the_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, detail_open: bool, state: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.show_preview(preview, run_id="run")
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        running = events.WorkflowNodeStateChanged(
            run_id="run",
            node_id="architecture",
            activation_id="review@iter#1",
            attempt=1,
            state="running",
            invocation_id="review",
        )
        origin = InvocationOrigin("workflow_node", "", "review", None, attempt=1)
        await bus.publish(running)
        if detail_open:
            # Details can mount while the agent is still being prepared.
            main._workflow.session_view.open_node(running.node_id)
            await wait_for(lambda: bool(app.screen.query(AgentTranscriptSurface)), pilot=pilot)
        await bus.publish(events.InvocationStarted(origin=origin, opening_prompt="Review [this] change."))
        await bus.publish(events.InvocationToolCallStart(origin=origin, call_id="pending", tool_name="read_file"))

        failed = replace(running, state=state, error="Provider rejected [context]: limit exceeded")
        await bus.publish(failed)
        await bus.publish(failed)  # Duplicated state facts do not duplicate the card.
        await bus.publish(events.InvocationToolCallStart(origin=origin, call_id="late", tool_name="read_file"))
        if not detail_open:
            main._workflow.session_view.open_node(running.node_id)
        await wait_for(lambda: bool(app.screen.query(ErrorMessage)), pilot=pilot)
        dialog = app.screen
        assert isinstance(dialog, WorkflowNodeDialog)
        dialog.query_one(TabbedContent).active = "workflow-transcript-tab"
        surface = dialog.query_one(AgentTranscriptSurface)
        assert [message._text for message in surface.query(UserMessage)] == ["Review [this] change."]
        assert isinstance(surface.direct_children()[0], UserMessage)
        error = surface.query_one(ErrorMessage)
        assert error._render_text().plain == "✗ Error\nProvider rejected [context]: limit exceeded"
        assert len(surface.query(ErrorMessage)) == 1
        assert not error.query(Button)  # Workflow owns the node-scoped Retry action.
        assert not surface.query(InterruptedMessage)
        assert surface.query_one(ToolGroup).all_complete
        assert "late" not in surface.query_one(ToolGroup)._tool_records

        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        main._workflow.session_view.open_node(running.node_id)
        await wait_for(lambda: bool(app.screen.query(ErrorMessage)), pilot=pilot)
        assert [message._text for message in app.screen.query(UserMessage)] == ["Review [this] change."]
        if state != "failed":
            # Real workflow retries preserve the invocation id across attempts; one that starts over shows
            # its own transcript from the prompt.
            await bus.publish(replace(running, attempt=2))
            await wait_for(lambda: not app.screen.query(ErrorMessage), pilot=pilot)
            retried = InvocationOrigin("workflow_node", "", "review", None, attempt=2)
            await bus.publish(events.InvocationMessage(origin=retried, text="Recovered", is_final=True))
            await bus.publish(replace(running, attempt=2, state="completed"))
            # The terminal node update can leave unrelated Textual timers active.
            # Observe the message without requiring Pilot's whole-screen idle barrier.
            await wait_for(lambda: bool(app.screen.query(AgentMessage)))
            assert not app.screen.query(ErrorMessage)
            assert [message._text for message in app.screen.query(UserMessage)] == ["Review [this] change."]


async def test_retry_compaction_pressure_and_progress_are_scoped_to_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        main._workflow_panel.show_preview(preview, run_id="run")
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(events.WorkflowRunStarted(run_id="run", manifest=preview.manifest))
        await bus.publish(
            events.WorkflowNodeStateChanged(
                run_id="run",
                node_id="architecture",
                activation_id="review@iter#1",
                attempt=1,
                state="running",
                invocation_id="review",
            )
        )
        origin = InvocationOrigin("workflow_node", "", "review", None, attempt=1)
        main._workflow.session_view.open_node("architecture")
        await wait_for(lambda: bool(app.screen.query(AgentTranscriptSurface)), pilot=pilot)
        surface = app.screen.query_one(AgentTranscriptSurface)
        await bus.publish(
            events.InvocationRetryAttempt(
                origin=origin,
                message="Temporary provider error",
                attempt=2,
                max_attempts=7,
                delay_seconds=3,
            )
        )
        await wait_for(lambda: bool(surface.query(RetryMessage)), pilot=pilot)
        retry_body = surface.query_one(RetryMessage).query_one(".status-body", Static)
        assert "Temporary provider error" in retry_body.render().plain
        await bus.publish(events.InvocationCompactionStarted(origin=origin, compaction_id="fold"))
        await wait_for(lambda: bool(surface.query(CompactionCard)), pilot=pilot)
        assert not surface.query(RetryMessage)
        card = surface.query_one(CompactionCard)
        for attempt in range(1, CompactionCard.QUIET_RETRY_NOTICES + 2):
            await bus.publish(
                events.InvocationRetryAttempt(
                    origin=origin,
                    scope="compaction",
                    message="side call",
                    detail="Rate limit [literal]",
                    attempt=attempt,
                    max_attempts=7,
                    delay_seconds=1,
                )
            )
        await wait_for(
            lambda: "Rate limit [literal]" in str(card.query_one("#compaction-retry-notice", Static).content),
            pilot=pilot,
        )
        assert card.status == "running" and not surface.query(RetryMessage)
        await bus.publish(events.InvocationCompactionFinished(origin=origin, compaction_id="fold", outcome="ok"))
        await bus.publish(events.InvocationCompactionCommitted(origin=origin, compaction_id="fold"))
        await bus.publish(events.InvocationCompactionCommitted(origin=origin, compaction_id="fold"))
        for count in (1, 2, 3):
            await bus.publish(
                events.InvocationProgress(
                    origin=origin,
                    tool_call_count=count,
                    total_tokens=100,
                    total_usage_tokens=300,
                    usage_unreported_attempts=1,
                )
            )
        assert not surface.query(".invocation-progress")
        run = main._workflow.session_view.projector.current
        assert run is not None
        assert run.usage["review"].tool_calls == 3
        assert run.usage["review"].usage_tokens == 300
        assert run.usage["review"].unreported_attempts == 1

        await bus.publish(events.InvocationToolCallStart(origin=origin, call_id="live", tool_name="read_file"))
        await bus.publish(events.InvocationContextPressure(origin=origin, reason="no_progress"))
        await wait_for(lambda: bool(surface.query("SystemMessage.-warning")), pilot=pilot)
        assert "insufficient progress" in str(surface.query_one(SystemMessage).content)
        assert surface.query_one(ToolGroup).is_tool_running("live")
        assert not surface.query(ErrorMessage) and not surface.query(InterruptedMessage)
        await bus.publish(events.InvocationPaused(origin=origin, last_error="Paused failure"))
        await wait_for(lambda: bool(surface.query(ErrorMessage)), pilot=pilot)
        # Usage publications drain independently from the pass's error/cancel boundary.
        await bus.publish(events.InvocationProgress(origin=origin, total_usage_tokens=400))
        assert run.usage["review"].usage_tokens == 400
        assert len(surface.query(ErrorMessage)) == 1
        await bus.publish(events.InvocationResumed(origin=origin))
        await wait_for(lambda: not surface.query(ErrorMessage), pilot=pilot)
        await bus.publish(events.InvocationAborted(origin=origin))
        await wait_for(lambda: bool(surface.query(InterruptedMessage)), pilot=pilot)
        await bus.publish(events.InvocationCascadeAborted(origin=origin))
        await bus.publish(events.InvocationProgress(origin=origin, total_usage_tokens=500))
        await bus.publish(events.InvocationCompactionCommitted(origin=origin, compaction_id="fold"))
        assert run.usage["review"].usage_tokens == 500
        assert len(surface.query(InterruptedMessage)) == 1
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        main._workflow.session_view.open_node("architecture")
        await wait_for(lambda: bool(app.screen.query(InterruptedMessage)), pilot=pilot)
        assert not app.screen.query(".invocation-progress")
