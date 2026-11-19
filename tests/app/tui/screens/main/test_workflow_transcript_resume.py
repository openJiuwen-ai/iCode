# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fresh TUI history views replay archives produced by real workflow agent execution."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import TabbedContent

from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.sessions import WorkflowSessionPick
from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.messages import AgentMessage, ErrorMessage, InterruptedMessage, UserMessage
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.service.llm.mock import MockChatClient
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
)
from tests.orchestration.workflows._transcript_support import ArchiveClient, source
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

from ._workflow_support import WorkflowEngine, switch_mode

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled", "retried"])
async def test_resume_replays_node_progress_without_live_state_or_workflow_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    target = project / "input.txt"
    target.write_text("Archived tool result [literal]")
    client = ArchiveClient(target, "failed" if outcome == "retried" else outcome)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    workflow_path = write_workflow(project, "archive", source())
    host = make_host(
        tmp_path,
        project=project,
        profiles=[make_profile(builtins=["filesystem.read"])],
        allow_user_interaction=outcome == "retried",
    )
    paused: list[events.WorkflowNodeStateChanged] = []

    async def node_changed(event: events.WorkflowNodeStateChanged) -> None:
        if event.state == "awaiting_retry":
            paused.append(event)

    await host.event_bus.subscribe(events.WorkflowNodeStateChanged, node_changed)
    task = None
    try:
        await confirm(host, "archive")
        task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
        if outcome == "retried":
            await wait_for(lambda: bool(paused) or task.done(), timeout=ENGINE_TURN_TIMEOUT)
            if task.done():
                await task
            assert paused
            failed = paused[0]
            client.outcome = "completed"
            await host.event_bus.publish(
                events.WorkflowNodeRetryRequest(
                    run_id=failed.run_id,
                    node_id=failed.node_id,
                    activation_id=failed.activation_id,
                    expected_failed_attempt=failed.attempt,
                    request_id="retry",
                )
            )
        if outcome == "cancelled":
            await wait_for(lambda: client.waiting.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
            if task.done():
                await task
            assert client.waiting.is_set()
            await host.cancel_workflow()
        result, _events = await task
        session_id = host.workflow_session_id
        assert session_id is not None
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
    # History must not execute, rebuild, or depend on the current workflow file.
    workflow_path.unlink()
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)

    async def restore(event: events.SessionRestore) -> None:
        pytest.fail("Workflow history must never restore a Chat agent")
        await bus.publish(events.SessionReady(session_id=session_id, primary_cwd=str(project)))
        await bus.publish(events.SessionRestored(session_id=session_id, primary_cwd=str(project)))

    await bus.subscribe(events.SessionRestore, restore)
    try:
        async with app.run_test(size=(140, 50)) as pilot:
            main = app._main_screen
            assert main is not None
            await switch_mode(main, pilot)
            await main._sessions.do_session_restore(WorkflowSessionPick(session_id))
            await wait_for(lambda: main._workflow_panel.run_id == result.run_id and app.screen is main, pilot=pilot)
            assert main._workflow.session_view.projector.current is None
            graph = main._workflow_panel.query_one(WorkflowGraph)
            await wait_for(lambda: "node" in graph._usage_labels, pilot=pilot)
            assert "Tool calls: 2" in graph._usage_labels["node"]
            assert "Ctx:" not in graph._usage_labels["node"]
            assert "node" in graph._elapsed_labels
            assert all(view.running_since is None for view in graph._views.values())
            main._workflow.session_view.open_node("node")
            await wait_for(lambda: bool(app.screen.query(AgentTranscriptSurface)), pilot=pilot)
            dialog = app.screen
            assert isinstance(dialog, WorkflowNodeDialog)
            dialog.query_one(TabbedContent).active = "workflow-transcript-tab"
            surface = dialog.query_one(AgentTranscriptSurface)
            await wait_for(lambda: bool(surface.query(ToolGroup)), pilot=pilot)
            await wait_for(
                lambda: sum(len(group._tool_records) for group in surface.query(ToolGroup)) == 2,
                pilot=pilot,
            )
            assert all(group.all_complete for group in surface.query(ToolGroup))
            assert [message._text for message in surface.query(UserMessage)] == ["Read twice"]
            assert not surface.query(".invocation-progress")
            assert [
                type(widget) for widget in surface.direct_children() if isinstance(widget, AgentMessage | ToolGroup)
            ][:4] == [AgentMessage, ToolGroup, AgentMessage, ToolGroup]
            assert [message.text for message in surface.query(AgentMessage)][:2] == [
                "First inspection.",
                "Second inspection.",
            ]
            if outcome == "failed":
                await wait_for(lambda: bool(surface.query(ErrorMessage)), pilot=pilot)
                assert "Archive failure [literal]" in surface.query_one(ErrorMessage)._render_text().plain
            elif outcome == "cancelled":
                await wait_for(lambda: bool(surface.query(InterruptedMessage)), pilot=pilot)
            else:
                await wait_for(lambda: len(surface.query(AgentMessage)) == 3, pilot=pilot)
                assert surface.query(AgentMessage).last().text == "Done"
                assert not surface.query(ErrorMessage) and not surface.query(InterruptedMessage)
            if outcome == "retried":
                # The same invocation owns both attempts. Switching the visible
                # attempt must replace its archive and its terminal notice.
                await click_when_settled(pilot, "#workflow-attempt-0")
                await wait_for(lambda: bool(dialog.query(ErrorMessage)), pilot=pilot)
                failed_surface = dialog.query_one(AgentTranscriptSurface)
                assert failed_surface is not surface
                assert "Archive failure [literal]" in failed_surface.query_one(ErrorMessage)._render_text().plain
                assert sum(len(group._tool_records) for group in failed_surface.query(ToolGroup)) == 2
                await click_when_settled(pilot, "#workflow-attempt-1")
                await wait_for(
                    lambda: (
                        dialog.query_one(AgentTranscriptSurface) is not failed_surface
                        and bool(dialog.query(AgentMessage))
                    ),
                    pilot=pilot,
                )
                assert not dialog.query(ErrorMessage)
            assert client.call_count == (2 if outcome == "failed" else 3)
    finally:
        await bus.unsubscribe(events.SessionRestore, restore)
