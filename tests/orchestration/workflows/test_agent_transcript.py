# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real kernel workflow passes persist their tool history across terminal outcomes and retries."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.agent_node as node_module
from chrys.foundation.errors import ProviderResponseError
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationToolCallStart,
    WorkflowNodeRetryRequest,
    WorkflowNodeStateChanged,
)
from chrys.kernel import ChatResponseUpdate, Content
from chrys.orchestration.workflows.agent_archive import AgentNodeArchive, CoalescedCheckpoint
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.store import read_node_value
from chrys.service.workflows.transcript import read_node_transcript, read_node_usage
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.pipeline_helpers import fail_nested_before_response_on_nth
from tests.support.scripted_clients import HostedMockChatClient, HostedMockResponse
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

from ._transcript_support import ArchiveClient, source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from chrys.kernel import FinishReason, FinishReasonLiteral

type _Shown = InvocationContentDelta | InvocationToolCallStart | InvocationMessage


class _NodeTranscript:
    """What the node shows, in order: each streamed segment whole, each tool start, the final message."""

    _TYPES = (InvocationContentDelta, InvocationToolCallStart, InvocationMessage)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self.events: list[_Shown] = []

    async def start(self) -> None:
        for event_type in self._TYPES:
            await self._bus.subscribe(event_type, self._record)

    async def close(self) -> None:
        for event_type in self._TYPES:
            await self._bus.unsubscribe(event_type, self._record)

    async def __aenter__(self) -> _NodeTranscript:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()

    async def _record(self, event: _Shown) -> None:
        if event.origin.kind == "workflow_node":
            self.events.append(event)

    def shown(self) -> list[str]:
        shown: list[str] = []
        segment = None
        for event in self.events:
            if isinstance(event, InvocationContentDelta):
                if event.segment_id == segment:
                    shown[-1] += event.text
                else:
                    shown.append(event.text)
                segment = event.segment_id
                continue
            segment = None
            if isinstance(event, InvocationToolCallStart):
                shown.append(f"hosted {event.tool_name}" if event.provider_hosted else event.tool_name)
            else:
                shown.append(f"final: {event.text}" if event.is_final else f"message: {event.text}")
        return shown


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("stream", [False, True])
async def test_attempt_archive_survives_shutdown_with_reused_call_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, stream: bool
) -> None:
    project = make_workdir(tmp_path)
    target = project / "input.txt"
    target.write_text("Equal content must survive twice.")
    node_client = ArchiveClient(target, outcome)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client], builtin_tools=True)
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])], stream=stream)
    transcript = _NodeTranscript(host.event_bus)
    await transcript.start()
    task = None
    try:
        await confirm(host, "archive")
        task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
        if outcome == "cancelled":
            await wait_for(lambda: node_client.waiting.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
            if task.done():
                await task
            assert node_client.waiting.is_set()
            session_dir = host.workflow_session_dir
            assert session_dir is not None
            run_id = host.engine.workflows.active_run_id
            assert run_id is not None
            directory = run_dir(session_dir, run_id)

            def checkpoint_has_results() -> bool:
                checkpoint = read_node_transcript(directory, "node@iter#1", 1)
                return (
                    checkpoint is not None
                    and checkpoint.status == "orphaned"
                    and len(
                        [c for m in checkpoint.replay.messages for c in m["contents"] if c["type"] == "function_result"]
                    )
                    == 2
                )

            await wait_for(checkpoint_has_results, timeout=ENGINE_TURN_TIMEOUT)
            await host.cancel_workflow()
        result, _events = await task
        assert result.outcome.value == ("node_failed" if outcome == "failed" else outcome)
        assert transcript.shown() == [
            "First inspection.",
            "read_file",
            "Second inspection.",
            "read_file",
            *(["Done", "final: Done"] if outcome == "completed" else []),
        ]
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, result.run_id)
    finally:
        await host.shutdown()
        await transcript.close()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
    stored = read_node_transcript(directory, "node@iter#1", 1)
    assert stored is not None and stored.status == outcome
    messages = stored.replay.messages
    assert messages[0]["role"] == "user"
    calls = [c for m in messages for c in m["contents"] if c["type"] == "function_call"]
    results = [c for m in messages for c in m["contents"] if c["type"] == "function_result"]
    assert len(calls) == len(results) == 2
    assert [c["call_id"] for c in calls] == ["reused-call", "reused-call"]
    assert all("Equal content" in c["result"] for c in results)
    assert stored.usage.tool_calls == 2
    usage = read_node_usage(directory, "node@iter#1", 1)
    assert (
        usage is not None
        and usage.tool_calls == stored.usage.tool_calls
        and usage.usage_tokens == stored.usage.usage_tokens
    )
    if outcome == "failed":
        assert "Archive failure [literal]" in stored.error
    if outcome == "completed":
        assert messages[-1]["contents"][0]["text"] == "Done"
        # The node's value is the final answer alone, not the prose between its tool calls.
        output = read_node_value(directory, "node@iter#1", 1, "output", node_kind="agent")
        assert output is not None and output["value"] == {"text": "Done", "data": None}
        assert [item.value.text for item in result.outputs] == ["Done"]


async def test_streamed_prose_of_an_unknown_tool_call_reaches_the_node_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown tool starts no tool; the prose streamed before it shows once, and the value leaves it out."""
    project = make_workdir(tmp_path)
    node_client = MockChatClient(
        responses=[
            MockResponse(text="Let me check. ", tool_calls=[("no_such_tool", "c-1", {})]),
            MockResponse(text="Done"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, stream=True)
    try:
        async with _NodeTranscript(host.event_bus) as transcript:
            await confirm(host, "archive")
            result, _events = await run(host, "archive", input_text="Check")
        assert result.outcome.value == "completed"
        assert transcript.shown() == ["Let me check. ", "Done", "final: Done"]
        assert [item.value.text for item in result.outputs] == ["Done"]
    finally:
        await host.shutdown()


def _hosted_image_item() -> Content:
    """A completed hosted image result stamped with its provider item id, as every hosted adapter stamps it."""
    return Content.from_image_generation_tool_result(
        image_id="image-1",
        outputs=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
        hosted_provider="openai",
        provider_item_id="ig_1",
        provider_phase="terminal",
        provider_status="completed",
    )


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
async def test_prose_before_hosted_output_reaches_the_node_transcript_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """The prose precedes the next response's hosted output and shows once."""
    project = make_workdir(tmp_path)
    node_client = HostedMockChatClient(
        responses=[
            MockResponse(text="Let me check. ", tool_calls=[("no_such_tool", "c-1", {})]),
            HostedMockResponse(text="Done", hosted=[_hosted_image_item()]),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, stream=stream)
    try:
        async with _NodeTranscript(host.event_bus) as transcript:
            await confirm(host, "archive")
            result, _events = await run(host, "archive", input_text="Check")
        assert result.outcome.value == "completed"
        assert transcript.shown() == ["Let me check. ", "hosted image", "Done", "final: Done"]
        assert [item.value.text for item in result.outputs] == ["Done"]
    finally:
        await host.shutdown()


async def test_prose_of_a_failed_pass_reaches_the_node_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that fails after an unknown call keeps the prose it streamed, and no final message follows."""
    project = make_workdir(tmp_path)
    node_client = MockChatClient(
        responses=[MockResponse(text="Let me check. ", tool_calls=[("no_such_tool", "c-1", {})])]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    # The second response fails before it starts.
    fail_nested_before_response_on_nth(monkeypatch, 2)
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, stream=True)
    try:
        async with _NodeTranscript(host.event_bus) as transcript:
            await confirm(host, "archive")
            result, _events = await run(host, "archive", input_text="Check")
        assert result.outcome.value == "node_failed"
        assert transcript.shown() == ["Let me check. "]
    finally:
        await host.shutdown()


@pytest.mark.parametrize("failure", ["stream_error", "error_carrying_hosted_work", "rejected_response"])
async def test_a_hosted_card_shown_as_the_response_fails_never_completes_the_answer_before_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Hosted work the failed response ran shows as it fails, and the answer it streamed never completed."""
    project = make_workdir(tmp_path)
    node_client = MockChatClient(responses=[MockResponse(text="unused")])
    if failure == "error_carrying_hosted_work":
        # The error carries hosted work the response ran, which shows as it fails.
        streamed = [Content.from_text("Partial ans")]
    else:
        # Held behind a call that never starts, the hosted card shows when the response is given up.
        call = Content.from_function_call("c-1", "" if failure == "rejected_response" else "no_such_tool", arguments={})
        streamed = [call, _hosted_image_item(), Content.from_text("Partial ans")]

    async def cut(
        _resp: MockResponse, model_id: str, _finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterator[ChatResponseUpdate]:
        for content in streamed:
            yield ChatResponseUpdate(role="assistant", contents=[content], model=model_id)
        if failure == "stream_error":
            raise RuntimeError("dropped")
        if failure == "error_carrying_hosted_work":
            raise ProviderResponseError(
                "server_error", "dropped", retryable=False, observed_contents=(_hosted_image_item(),)
            )

    monkeypatch.setattr(node_client, "_stream_updates", cut)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, stream=True)
    ends: list[str] = []

    async def record(event: InvocationContentFinished) -> None:
        if event.origin.kind == "workflow_node" and event.channel == "answer":
            ends.append(event.status)

    await host.event_bus.subscribe(InvocationContentFinished, record)
    try:
        async with _NodeTranscript(host.event_bus) as transcript:
            await confirm(host, "archive")
            result, _events = await run(host, "archive", input_text="Check")
        assert result.outcome.value == "node_failed"
        assert transcript.shown() == ["Partial ans", "hosted image"]
        # The node transcript keeps a completed segment before a card; the cut-off answer ends failed.
        assert ends == ["failed"]
    finally:
        await host.shutdown()
        await host.event_bus.unsubscribe(InvocationContentFinished, record)


@pytest.mark.parametrize("close", ["cancel", "shutdown"])
async def test_closing_the_run_settles_prose_a_subscriber_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, close: str
) -> None:
    """A subscriber that never returns cannot hold a closing node, and nothing it held arrives after the pass."""
    project = make_workdir(tmp_path)
    node_client = MockChatClient(
        responses=[
            MockResponse(text="Let me check. ", tool_calls=[("no_such_tool", "c-1", {})]),
            MockResponse(text="Done"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, stream=True)
    publishing = asyncio.Event()
    abandoned = asyncio.Event()
    delivered: list[str] = []

    async def stuck(event: InvocationContentDelta) -> None:
        if event.origin.kind == "workflow_node":
            publishing.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                abandoned.set()
                raise

    async def late(event: InvocationContentDelta | InvocationMessage) -> None:
        if event.origin.kind == "workflow_node":
            delivered.append(event.text)

    await host.event_bus.subscribe(InvocationContentDelta, stuck)
    await host.event_bus.subscribe(InvocationContentDelta, late)
    await host.event_bus.subscribe(InvocationMessage, late)
    task = None
    try:
        await confirm(host, "archive")
        task = asyncio.create_task(run(host, "archive", input_text="Check"))
        await wait_for(lambda: publishing.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
        if task.done():
            await task
        assert publishing.is_set()

        if close == "cancel":
            await host.cancel_workflow()
        else:
            await host.shutdown()
        await wait_for(task.done, timeout=ENGINE_TURN_TIMEOUT, description="the closed run ending")
        result, _events = await task
        assert result.outcome.value == "cancelled"
        assert abandoned.is_set()
        assert delivered == []
    finally:
        await host.shutdown()
        await host.event_bus.unsubscribe(InvocationContentDelta, stuck)
        await host.event_bus.unsubscribe(InvocationContentDelta, late)
        await host.event_bus.unsubscribe(InvocationMessage, late)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_manual_retry_keeps_the_failed_attempt_archive_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    target = project / "input.txt"
    target.write_text("Retained work")
    client = ArchiveClient(target, "failed")
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "archive", source())
    host = make_host(
        tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])], allow_user_interaction=True
    )
    paused: list[WorkflowNodeStateChanged] = []

    async def state(event: WorkflowNodeStateChanged) -> None:
        if event.state == "awaiting_retry":
            paused.append(event)

    await host.event_bus.subscribe(WorkflowNodeStateChanged, state)
    task = None
    try:
        await confirm(host, "archive")
        task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
        await wait_for(lambda: bool(paused) or task.done(), timeout=ENGINE_TURN_TIMEOUT)
        if task.done():
            await task
        assert paused
        event = paused[0]
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, event.run_id)
        first = read_node_transcript(directory, event.activation_id, 1)
        assert first is not None and first.status == "failed"
        client.outcome = "completed"
        await host.event_bus.publish(
            WorkflowNodeRetryRequest(
                run_id=event.run_id,
                node_id=event.node_id,
                activation_id=event.activation_id,
                expected_failed_attempt=1,
                request_id="retry",
            )
        )
        result, _events = await task
        assert result.outcome.value == "completed"
        assert read_node_transcript(directory, event.activation_id, 1) == first
        second = read_node_transcript(directory, event.activation_id, 2)
        assert second is not None and second.status == "completed"
        assert second.replay.messages[-1]["contents"][0]["text"] == "Done"
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_kernel_tool_rounds_coalesce_and_final_snapshot_keeps_all_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = make_workdir(tmp_path)
    target = project / "input.txt"
    target.write_text("Retained result")
    client = ArchiveClient(target, "completed", pause_before_final=True)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "archive", source())
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    checkpoint_factory = create_autospec(
        CoalescedCheckpoint, side_effect=lambda write: CoalescedCheckpoint(write, interval=3600)
    )
    monkeypatch.setattr(node_module, "CoalescedCheckpoint", checkpoint_factory)
    writes = create_autospec(AgentNodeArchive.write, side_effect=AgentNodeArchive.write)
    monkeypatch.setattr(AgentNodeArchive, "write", writes)
    task = None
    try:
        await confirm(host, "archive")
        task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
        await wait_for(lambda: client.waiting.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
        if task.done():
            await task
        assert client.waiting.is_set()
        assert writes.await_count == 1  # The pass-start snapshot precedes both tool rounds.
        client.release_final.set()
        result, _ = await task
        assert result.outcome.value == "completed"
        assert writes.await_count == 2
        assert writes.await_args.kwargs["status"] == "completed"
        assert host.workflow_session_dir is not None
        stored = read_node_transcript(run_dir(host.workflow_session_dir, result.run_id), "node@iter#1", 1)
        assert stored is not None and stored.status == "completed"
        results = [c for m in stored.replay.messages for c in m["contents"] if c["type"] == "function_result"]
        assert len(results) == 2
    finally:
        client.release_final.set()
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
