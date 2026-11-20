# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow progress preserves real kernel message/tool boundaries during execution and replay."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from chrys.app.tui.widgets.chat.agent_transcript_surface import (
    AgentTranscriptJournal,
    AgentTranscriptSurface,
    AnswerSegments,
    TranscriptAssistantOp,
    TranscriptToolStartOp,
)
from chrys.app.tui.widgets.chat.messages import AgentMessage, UserMessage
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.workflow.projector import INVOCATION_EVENTS, journal_operation
from chrys.foundation.events.types import InvocationEvent
from chrys.kernel import Content
from chrys.service.llm.mock import MockChatClient
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.transcript import read_node_transcript
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.orchestration.workflows._transcript_support import ArchiveClient, source
from tests.support.scripted_clients import HostedMockChatClient, HostedMockResponse
from tests.support.tui_helpers import LocalizedWidgetApp
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

NODE_PROMPT = "Node-specific input [literal]: " + "\n".join(["review the change"] * 40)


@pytest.mark.parametrize("stream", [False, True])
async def test_workflow_live_and_archived_progress_interleave_text_and_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    workdir = make_workdir(tmp_path)
    target = workdir / "input.txt"
    target.write_text("Read this twice.")
    client = ArchiveClient(target, "completed", pause_before_final=True)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    # The node receives an upstream value, not the workflow's top-level input
    # or a truncated status summary. Structured data is not agent user text.
    workflow_source = (
        source()
        .decode()
        .replace(
            "wf.start(node)",
            "from chrys.workflows import WorkflowValue\n"
            f"def prepare(value, ctx):\n    return WorkflowValue(text={NODE_PROMPT!r}, data={{'hidden': True}})\n"
            "prepare_node = wf.python('prepare', prepare)\nwf.start(prepare_node)\nwf.edge(prepare_node, node)",
        )
    )
    write_workflow(workdir, "archive", workflow_source.encode())
    host = make_host(tmp_path, project=workdir, profiles=[make_profile(builtins=["filesystem.read"])], stream=stream)
    journal = AgentTranscriptJournal()
    answers = AnswerSegments()

    async def record(event: InvocationEvent) -> None:
        if event.origin.kind == "workflow_node" and (operation := journal_operation(event, answers)) is not None:
            journal.record(operation)

    for event_type in INVOCATION_EVENTS:
        await host.event_bus.subscribe(event_type, record)
    surface = AgentTranscriptSurface(journal)
    task = None
    try:
        await confirm(host, "archive")
        async with LocalizedWidgetApp(lambda: surface).run_test() as pilot:
            task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
            await wait_for(lambda: client.waiting.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT, pilot=pilot)
            if task.done():
                await task
            assert client.waiting.is_set()
            await wait_for(lambda: len(surface.query(ToolGroup)) == 2, pilot=pilot)
            assert [message._text for message in surface.query(UserMessage)] == [NODE_PROMPT]
            assert [
                type(widget)
                for widget in surface.direct_children()
                if isinstance(widget, UserMessage | AgentMessage | ToolGroup)
            ] == [UserMessage, AgentMessage, ToolGroup, AgentMessage, ToolGroup]
            assert [message.text for message in surface.query(AgentMessage)] == [
                "First inspection.",
                "Second inspection.",
            ]
            client.release_final.set()
            result, _events = await task
            assert result.outcome.value == "completed"
            await wait_for(lambda: len(surface.query(AgentMessage)) == 3, pilot=pilot)
            _assert_order(surface)
            session_dir = host.workflow_session_dir
            assert session_dir is not None
            directory = run_dir(session_dir, result.run_id)
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        for event_type in INVOCATION_EVENTS:
            await host.event_bus.unsubscribe(event_type, record)

    archive = read_node_transcript(directory, "node@iter#1", 1)
    assert archive is not None
    # A terminal archive replaces the live journal, including its user input.
    restored = AgentTranscriptSurface(journal, persisted_replay=archive.replay)
    async with LocalizedWidgetApp(lambda: restored).run_test() as pilot:
        await wait_for(lambda: len(restored.query(AgentMessage)) == 3, pilot=pilot)
        _assert_order(restored)


def _assert_order(surface: AgentTranscriptSurface) -> None:
    assert [message._text for message in surface.query(UserMessage)] == [NODE_PROMPT]
    assert [
        type(widget)
        for widget in surface.direct_children()
        if isinstance(widget, UserMessage | AgentMessage | ToolGroup)
    ] == [
        UserMessage,
        AgentMessage,
        ToolGroup,
        AgentMessage,
        ToolGroup,
        AgentMessage,
    ]
    assert [message.text for message in surface.query(AgentMessage)] == [
        "First inspection.",
        "Second inspection.",
        "Done",
    ]
    groups = list(surface.query(ToolGroup))
    assert all(group.all_complete and len(group._tool_records) == 1 for group in groups)


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
async def test_text_before_a_hosted_call_shows_before_its_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    workdir = make_workdir(tmp_path)
    hosted = Content.from_image_generation_tool_result(
        image_id="image-1",
        outputs=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
        hosted_provider="openai",
        provider_item_id="ig_1",
        provider_phase="terminal",
        provider_status="completed",
    )
    node_client = HostedMockChatClient(
        responses=[HostedMockResponse(lead_text="Drawing it.", hosted=[hosted], text="Done")]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    write_workflow(workdir, "archive", source())
    host = make_host(tmp_path, project=workdir, stream=stream)
    journal = AgentTranscriptJournal()
    answers = AnswerSegments()

    async def record(event: InvocationEvent) -> None:
        if event.origin.kind == "workflow_node" and (operation := journal_operation(event, answers)) is not None:
            journal.record(operation)

    for event_type in INVOCATION_EVENTS:
        await host.event_bus.subscribe(event_type, record)
    try:
        await confirm(host, "archive")
        result, _events = await run(host, "archive", input_text="Draw")
    finally:
        await host.shutdown()
        for event_type in INVOCATION_EVENTS:
            await host.event_bus.unsubscribe(event_type, record)

    assert result.outcome.value == "completed"
    # The text before the hosted call ends its segment before the call's card starts, so it shows first.
    assert [
        operation.tool_name if isinstance(operation, TranscriptToolStartOp) else operation.text
        for operation in journal.operations
        if isinstance(operation, TranscriptToolStartOp)
        or (isinstance(operation, TranscriptAssistantOp) and not operation.final)
    ] == ["Drawing it.", "image", "Done"]
