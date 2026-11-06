# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow agent nodes retry like chat: a request retries in place, a failed pass resumes on the next attempt."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterable, Iterable
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.agent_node as agent_node_module
import chrys.orchestration.workflows.agent_node_build as agent_node_build_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.errors import ErrorKind, classify_error
from chrys.foundation.events.types import (
    InvocationAnswerWithdrawn,
    InvocationContentDelta,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    WorkflowNodeStateChanged,
)
from chrys.foundation.hosted_tools import HostedToolStatus
from chrys.kernel import ChatResponseUpdate, Content, FinishReason, FinishReasonLiteral, FunctionTool, Message
from chrys.orchestration.invoker.resources import PreparedAgent
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.acp_client import AcpAgentClient, AcpConnectError
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.transcript import read_node_transcript
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_project,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.event_capture import capture_event_sequence
from tests.support.provider_errors import openai_status
from tests.support.scripted_clients import HostedMockChatClient, HostedMockResponse
from tests.support.tool_packs import install_tools
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

pytestmark = pytest.mark.asyncio

# Never reached on its own: the test says when the pass deadline passes.
_NODE_TIMEOUT = 3600.0
# The model profile asks the provider to store responses, which also lets a response run in the background.
_SERVICE_SIDE_STORAGE = '{"store": true}'


def _workflow(*, max_attempts: int, timeout: float | None = None, profile: str = PROFILE) -> bytes:
    timeout_arg = f", timeout={timeout!r}" if timeout is not None else ""
    return (
        "from chrys.workflows import Retry, WorkflowBuilder\n"
        "wf = WorkflowBuilder('retry')\n"
        f"review = wf.agent('review', profile={profile!r}{timeout_arg}, retry=Retry(max_attempts={max_attempts}))\n"
        "wf.start(review)\nwf.output(review)\nworkflow = wf.build()\n"
    ).encode()


def _acp_profile(scenario: str) -> AgentProfile:
    return AgentProfile(
        name="External",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=[str(STUB_SCRIPT)],
            env={"CHRYS_ACP_STUB_SCENARIO": scenario},
            idle_timeout_seconds=0,
        ),
    )


def _hold_node_deadline(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """The node's pass deadline passes once each time the test sets the returned event; other waits stay real."""
    expire = asyncio.Event()
    real_wait = asyncio.wait

    async def wait(
        tasks: Iterable[asyncio.Future[Any]], *, timeout: float | None = None, return_when: str = asyncio.ALL_COMPLETED
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        tasks = set(tasks)
        if timeout != _NODE_TIMEOUT:
            return await real_wait(tasks, timeout=timeout, return_when=return_when)
        expiry = asyncio.ensure_future(expire.wait())
        try:
            await real_wait({*tasks, expiry}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
        expire.clear()
        done = {task for task in tasks if task.done()}
        return done, tasks - done

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio))
    shadow.wait = wait  # type: ignore[attr-defined]  # the shadow's own name, not the stdlib module's
    monkeypatch.setattr(agent_node_module, "asyncio", shadow)
    return expire


def _results_for(messages: list[Message]) -> list[str]:
    return [
        content.call_id for message in messages for content in message.contents if content.type == "function_result"
    ]


def _user_texts(messages: list[Message]) -> list[str]:
    # The request's copy of the last user message carries the turn's system reminders.
    return [message.text.partition(" <system-reminder>")[0] for message in messages if message.role == "user"]


def _node_states(events: list[Any]) -> list[tuple[str, int, str]]:
    return [(e.state, e.attempt, e.error_class) for e in of_type(events, WorkflowNodeStateChanged)]


async def test_a_pass_that_times_out_after_a_tool_resumes_without_running_the_tool_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expire = _hold_node_deadline(monkeypatch)
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "call_1", {"path": str(reference)})]),
            MockResponse(text="never", delay=3600),
            MockResponse(text="reviewed"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus, WorkflowNodeStateChanged, InvocationToolCallStart, InvocationResumed
        ) as events:
            task = asyncio.create_task(run(host, "review", input_text="Review the reference."))
            await wait_for(
                lambda: len(client.call_history) == 2 or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the model call after the tool is in flight",
            )
            assert not task.done()
            expire.set()
            result, _stream = await task
        assert result.outcome.value == "completed"
        assert result.outputs[0].value.text == "reviewed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        # The tool ran once, under the first attempt; the second attempt announced that it carries the pass on.
        starts = of_type(events, InvocationToolCallStart)
        assert [(event.tool_name, event.origin.attempt) for event in starts] == [("read_file", 1)]
        assert [event.origin.attempt for event in of_type(events, InvocationResumed)] == [2]
        assert len(client.call_history) == 3
        resumed, _options = client.call_history[2]
        assert _user_texts(list(resumed)) == ["Review the reference."]
        assert _results_for(list(resumed)) == ["call_1"]
    finally:
        await host.shutdown()


@pytest.mark.parametrize("narration", ["", "Let me read the reference."], ids=["silent", "narrated"])
async def test_a_resumed_attempt_keeps_the_narration_of_a_completed_tool_exchange(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, narration: str
) -> None:
    """The resumed attempt continues after the narration in its history and never writes it again."""
    expire = _hold_node_deadline(monkeypatch)
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = MockChatClient(
        responses=[
            MockResponse(text=narration, tool_calls=[("read_file", "call_1", {"path": str(reference)})]),
            MockResponse(text="never", delay=3600),
            MockResponse(text="reviewed"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            task = asyncio.create_task(run(host, "review", input_text="Review the reference."))
            await wait_for(
                lambda: len(client.call_history) == 2 or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the model call after the tool is in flight",
            )
            assert not task.done()
            expire.set()
            result, _stream = await task
        assert result.outcome.value == "completed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        resumed, _options = client.call_history[2]
        assert [message.text for message in resumed if message.role == "assistant"] == [narration]
    finally:
        await host.shutdown()


@pytest.mark.parametrize("held", [False, True], ids=["delivered", "timed-out-while-delivered"])
async def test_a_resumed_attempt_withdraws_the_answer_it_asks_for_again_with_its_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    """Node details carry the transcript on from the resume, then drop what the attempt asks for again.

    Both reach every view even when the attempt's deadline cancels its pass while they are delivered: a
    later attempt carries on from what the views hold by then.
    """
    expire = _hold_node_deadline(monkeypatch)
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("read_file", "call_1", {"path": str(reference)})]),
            MockResponse(text="Looking. never", chunk_size=9, chunk_delay=3600),
            MockResponse(text="reviewed"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "review", _workflow(max_attempts=3, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, stream=True, profiles=[make_profile(builtins=["filesystem.read"])])
    holding, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    deadline_wait = agent_node_module.asyncio.wait

    async def wait(
        tasks: Iterable[asyncio.Future[Any]], *, timeout: float | None = None, return_when: str = asyncio.ALL_COMPLETED
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        if holding.is_set() and timeout == agent_node_module.ABORT_GRACE:
            # The aborted pass is still delivering its resume: the grace runs out, and the node cancels the pass.
            cancelled.set()
            return set(), set(tasks)
        return await deadline_wait(tasks, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(agent_node_module.asyncio, "wait", wait)

    async def slow_view(event: InvocationResumed) -> None:
        if held and event.origin.attempt == 2:
            holding.set()
            await release.wait()

    await host.event_bus.subscribe(InvocationResumed, slow_view)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus,
            WorkflowNodeStateChanged,
            InvocationContentDelta,
            InvocationResumed,
            InvocationAnswerWithdrawn,
        ) as events:
            task = asyncio.create_task(run(host, "review", input_text="Review the reference."))
            await wait_for(
                lambda: bool(of_type(events, InvocationContentDelta)) or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the answer after the tool shown",
            )
            assert not task.done()
            expire.set()
            if held:
                await wait_for(lambda: holding.is_set() or task.done(), description="the resume delivered")
                assert holding.is_set()
                expire.set()
                await wait_for(lambda: cancelled.is_set() or task.done(), description="the pass cancelled")
                assert cancelled.is_set()
                release.set()
            result, _stream = await task
        assert result.outcome.value == "completed"
        timed_out_resume = [("retrying", 2, "agent_timeout"), ("running", 3, ""), ("completed", 3, "")]
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            *(timed_out_resume if held else [("completed", 2, "")]),
        ]
        assert [event.origin.attempt for event in of_type(events, InvocationResumed)] == ([2, 3] if held else [2])
        (shown, *_after) = of_type(events, InvocationContentDelta)
        assert (shown.origin.attempt, shown.text) == (1, "Looking. ")
        withdrawn = of_type(events, InvocationAnswerWithdrawn)
        assert [(event.origin.attempt, event.segment_ids) for event in withdrawn] == [(2, (shown.segment_id,))]
        assert events.index(of_type(events, InvocationResumed)[0]) < events.index(withdrawn[0])
        # The request goes on after the tool call, not from the answer it withdrew.
        resumed, _options = client.call_history[-1]
        assert [message.text for message in resumed if message.role == "assistant"] == [""]
    finally:
        release.set()
        await host.event_bus.unsubscribe(InvocationResumed, slow_view)
        await host.shutdown()


async def test_a_node_that_keeps_timing_out_fails_with_agent_timeout_after_its_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="never", delay=3600)] * 2)],
    )
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=0.3))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationResumed) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert (result.outcome.value, result.node_id) == ("node_failed", "review")
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("failed", 2, "agent_timeout"),
        ]
        assert "0.3s" in of_type(events, WorkflowNodeStateChanged)[-1].error
        # The first pass retained no work past its prompt, so the second one starts over rather than resuming.
        assert of_type(events, InvocationResumed) == []
        assert result.duration < 60
    finally:
        await host.shutdown()


async def test_a_rate_limit_after_a_tool_retries_the_request_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    rate_limited = await openai_status(
        429, {"error": {"type": "requests", "code": "rate_limit_exceeded", "message": "Rate limit reached"}}
    )
    assert classify_error(rate_limited).retryable
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response,
            side_effect=[
                MockResponse(tool_calls=[("read_file", "call_1", {"path": str(reference)})]),
                rate_limited,
                MockResponse(text="reviewed"),
            ],
        ),
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "review", _workflow(max_attempts=2))
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus, WorkflowNodeStateChanged, InvocationToolCallStart, InvocationRetryAttempt
        ) as events:
            result, _stream = await run(host, "review", input_text="Review the reference.")
        assert result.outcome.value == "completed"
        assert _node_states(events) == [("running", 1, ""), ("completed", 1, "")]
        assert [event.tool_name for event in of_type(events, InvocationToolCallStart)] == ["read_file"]
        (retry,) = of_type(events, InvocationRetryAttempt)
        assert (retry.scope, retry.origin.attempt, retry.attempt) == ("wire", 1, 1)
        assert "Rate limit reached" in retry.message
        assert len(client.call_history) == 3
        retried, _options = client.call_history[2]
        assert _results_for(list(retried)) == ["call_1"]
    finally:
        await host.shutdown()


async def test_an_exhausted_quota_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quota = await openai_status(
        429, {"error": {"type": "insufficient_quota", "code": "insufficient_quota", "message": "Quota exceeded"}}
    )
    assert classify_error(quota).kind is ErrorKind.QUOTA_EXHAUSTED
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response,
            side_effect=[MockResponse(tool_calls=[("read_file", "call_1", {"path": str(reference)})]), quota],
        ),
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "review", _workflow(max_attempts=3))
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationRetryAttempt) as events:
            result, _stream = await run(host, "review", input_text="Review the reference.")
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, "agent_non_transient")]
        assert of_type(events, InvocationRetryAttempt) == []
        assert len(client.call_history) == 2
    finally:
        await host.shutdown()


async def test_a_timeout_during_the_retry_backoff_sends_no_further_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expire = _hold_node_deadline(monkeypatch)
    rate_limited = await openai_status(
        429, {"error": {"type": "requests", "code": "rate_limit_exceeded", "message": "Rate limit reached"}}
    )
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(client._next_response, side_effect=[rate_limited, MockResponse(text="never sent")]),
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=1, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project)

    async def expire_in_backoff(event: InvocationRetryAttempt) -> None:
        expire.set()

    await host.event_bus.subscribe(InvocationRetryAttempt, expire_in_backoff)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationRetryAttempt) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, "agent_timeout")]
        assert [event.delay_seconds for event in of_type(events, InvocationRetryAttempt)] == [3]
        assert len(client.call_history) == 1
    finally:
        await host.event_bus.unsubscribe(InvocationRetryAttempt, expire_in_backoff)
        await host.shutdown()


def _install_hold_tool(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """Give the node one tool that runs until its pass ends; the returned event is set once it runs."""
    entered = asyncio.Event()

    async def hold(reason: Annotated[str, "Why to wait"]) -> str:
        entered.set()
        await asyncio.Event().wait()
        return "never"

    tool = FunctionTool(func=hold, name="hold", description="Waits until the pass ends")

    install_tools(monkeypatch, [tool])
    return entered


@pytest.mark.parametrize("stream", [False, True], ids=["whole", "streamed"])
async def test_a_timeout_during_a_local_tool_resumes_after_the_hosted_call_of_the_same_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """The repaired history keeps the response that ran the hosted call, so the next attempt carries its result on."""
    expire = _hold_node_deadline(monkeypatch)
    hosted = [
        Content.from_mcp_server_tool_call("mc1", "create_issue"),
        Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("issue 7")]),
    ]
    client = HostedMockChatClient(
        responses=[
            HostedMockResponse(hosted=hosted, tool_calls=[("hold", "call_1", {"reason": "review"})]),
            MockResponse(text="filed"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    entered = _install_hold_tool(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, stream=stream)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationResumed) as events:
            task = asyncio.create_task(run(host, "review", input_text="File the issue."))
            await wait_for(
                lambda: entered.is_set() or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the local tool is running",
            )
            assert not task.done()
            expire.set()
            result, _stream = await task
        assert result.outcome.value == "completed"
        assert result.outputs[0].value.text == "filed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        assert [event.origin.attempt for event in of_type(events, InvocationResumed)] == [2]
        assert len(client.call_history) == 2
        resumed, _options = client.call_history[1]
        assert [(c.type, c.call_id) for m in resumed for c in m.contents if c.provider_hosted] == [
            ("mcp_server_tool_call", "mc1"),
            ("mcp_server_tool_result", "mc1"),
        ]
        assert _results_for(list(resumed)) == ["call_1"]
    finally:
        await host.shutdown()


async def test_a_request_dropped_after_running_hosted_work_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The response never landed, so a retry would send the request again and run the hosted call a second time."""
    client = HostedMockChatClient(
        responses=[
            HostedMockResponse(
                hosted=[Content.from_mcp_server_tool_call("mc1", "create_issue")],
                error_after_hosted=ConnectionResetError("connection reset by peer"),
            ),
            MockResponse(text="created again"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=3))
    host = make_host(tmp_path, project=project, stream=True)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationRetryAttempt) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, "agent_transient")]
        assert of_type(events, InvocationRetryAttempt) == []
        assert len(client.call_history) == 1
    finally:
        await host.shutdown()


async def test_an_earlier_request_answering_the_same_hosted_call_id_does_not_make_a_dropped_one_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Providers mint per-response ids: the first request's kept result says nothing about the second's call."""
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = HostedMockChatClient(
        responses=[
            HostedMockResponse(
                hosted=[
                    Content.from_mcp_server_tool_call("mc1", "create_issue"),
                    Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("issue 7")]),
                ],
                tool_calls=[("read_file", "call_1", {"path": str(reference)})],
            ),
            HostedMockResponse(
                hosted=[Content.from_mcp_server_tool_call("mc1", "create_issue")],
                error_after_hosted=ConnectionResetError("connection reset by peer"),
            ),
            MockResponse(text="created again"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    write_workflow(project, "review", _workflow(max_attempts=3))
    host = make_host(tmp_path, project=project, stream=True)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationRetryAttempt) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, "agent_transient")]
        assert of_type(events, InvocationRetryAttempt) == []
        assert len(client.call_history) == 2
    finally:
        await host.shutdown()


async def test_with_no_request_retries_a_failed_pass_resumes_on_the_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference", encoding="utf-8")
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response,
            side_effect=[
                MockResponse(tool_calls=[("read_file", "call_1", {"path": str(reference)})]),
                ConnectionResetError("connection reset by peer"),
                MockResponse(text="reviewed"),
            ],
        ),
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    write_workflow(project, "review", _workflow(max_attempts=2))
    host = make_host(
        tmp_path,
        project=project,
        profiles=[make_profile(builtins=["filesystem.read"])],
        settings=Settings(model_profile="mock-profile", max_transient_retries=0),
    )
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus, WorkflowNodeStateChanged, InvocationToolCallStart, InvocationResumed
        ) as events:
            result, _stream = await run(host, "review", input_text="Review the reference.")
        assert result.outcome.value == "completed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_transient"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        assert [event.tool_name for event in of_type(events, InvocationToolCallStart)] == ["read_file"]
        assert [event.origin.attempt for event in of_type(events, InvocationResumed)] == [2]
        resumed, _options = client.call_history[2]
        assert _user_texts(list(resumed)) == ["Review the reference."]
        assert _results_for(list(resumed)) == ["call_1"]
    finally:
        await host.shutdown()


async def test_under_service_side_storage_a_failed_request_retries_the_whole_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    client = MockChatClient(responses=[])
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response,
            side_effect=[ConnectionResetError("connection reset by peer"), MockResponse(text="ok")],
        ),
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2))
    host = make_host(tmp_path, project=project, chat_options=_SERVICE_SIDE_STORAGE)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationRetryAttempt) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "completed"
        assert result.outputs[0].value.text == "ok"
        assert _node_states(events) == [("running", 1, ""), ("completed", 1, "")]
        (retry,) = of_type(events, InvocationRetryAttempt)
        assert (retry.scope, retry.origin.attempt, retry.attempt) == ("run", 1, 1)
        assert len(client.call_history) == 2
    finally:
        await host.shutdown()


class _HostedCallThenStops(MockChatClient):
    """Streams a provider-hosted call that is still running, then fails, or never answers again."""

    def __init__(self, failure: BaseException | None) -> None:
        super().__init__(responses=[MockResponse(text="never streamed")])
        self._failure = failure

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterable[ChatResponseUpdate]:
        running = Content.from_hosted_tool_call(
            "hosted-1", tool_name="hosted_shell", status="in_progress", hosted_provider="openai"
        )
        yield ChatResponseUpdate(contents=[running], role="assistant", model=model_id)
        if self._failure is not None:
            raise self._failure
        await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("failure", "status", "error_class"),
    [
        (ConnectionResetError("connection reset by peer"), HostedToolStatus.FAILED, "agent_transient"),
        (None, HostedToolStatus.INTERRUPTED, "agent_timeout"),
    ],
    ids=["failed", "timed-out"],
)
async def test_a_pass_that_ends_while_a_hosted_call_runs_settles_its_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException | None,
    status: HostedToolStatus,
    error_class: str,
) -> None:
    """The attempt's transcript never leaves a provider-hosted call running once the attempt ended."""
    expire = _hold_node_deadline(monkeypatch)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), _HostedCallThenStops(failure)])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=1, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, stream=True)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus, WorkflowNodeStateChanged, InvocationToolCallStart, InvocationToolCallStatusUpdated
        ) as events:
            task = asyncio.create_task(run(host, "review", input_text="x"))
            await wait_for(
                lambda: bool(of_type(events, InvocationToolCallStart)) or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the hosted call started",
            )
            if failure is None:
                assert not task.done()
                expire.set()
            result, _stream = await task
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, error_class)]
        (start,) = of_type(events, InvocationToolCallStart)
        assert start.provider_hosted
        updates = [
            event for event in of_type(events, InvocationToolCallStatusUpdated) if event.call_id == start.call_id
        ]
        assert updates[-1].status == status
        assert updates[-1].origin.attempt == 1
    finally:
        await host.shutdown()


class _BackgroundResponseThenSilence(MockChatClient):
    """The first request announces a background response still running at the provider, then goes silent."""

    def __init__(self) -> None:
        super().__init__(responses=[MockResponse(text="never streamed"), MockResponse(text="created again")])
        # Set once the stream is asked for more: the announcement was consumed.
        self.announced = asyncio.Event()

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterable[ChatResponseUpdate]:
        if self.announced.is_set():
            async for update in super()._stream_updates(resp, model_id, finish_reason):
                yield update
            return
        yield ChatResponseUpdate(
            contents=[], role="assistant", model=model_id, continuation_token={"response_id": "resp_1"}
        )
        self.announced.set()
        await asyncio.Event().wait()


async def test_a_timeout_that_abandons_a_running_background_response_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The abandoned response may still run provider-hosted tools that a new request would repeat."""
    expire = _hold_node_deadline(monkeypatch)
    client = _BackgroundResponseThenSilence()
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client, MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, stream=True)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            task = asyncio.create_task(run(host, "review", input_text="x"))
            await wait_for(
                lambda: client.announced.is_set() or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the background response is running",
            )
            assert not task.done()
            expire.set()
            result, _stream = await task
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, "agent_timeout")]
        assert len(client.call_history) == 1
    finally:
        await host.shutdown()


async def test_a_failed_poll_of_a_background_response_never_creates_its_hosted_work_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next attempt polls the response the failed pass left running; the poll's empty response is rejected
    for good, so the node fails rather than send a fresh request that would run the hosted call again."""
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    # A background response runs a hosted MCP call and drops; polling it returns nothing; a new request answers.
    client = HostedMockChatClient(
        responses=[
            HostedMockResponse(
                hosted=[Content.from_mcp_server_tool_call("mc1", "create_issue")],
                continuation_token={"response_id": "resp_1"},
                error_after_hosted=ConnectionResetError("connection reset by peer"),
            ),
            MockResponse(text=""),
            MockResponse(text="created again"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=3))
    host = make_host(
        tmp_path,
        project=project,
        stream=True,
        chat_options=_SERVICE_SIDE_STORAGE,
        settings=Settings(model_profile="mock-profile", max_transient_retries=0),
    )
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus, WorkflowNodeStateChanged, InvocationToolCallStart, InvocationResumed
        ) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "node_failed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_transient"),
            ("running", 2, ""),
            ("failed", 2, "agent_non_transient"),
        ]
        assert len(client.call_history) == 2
        _messages, poll_options = client.call_history[1]
        assert poll_options.get("continuation_token") == {"response_id": "resp_1"}
        # The poll carries on the response the first attempt showed, so its transcript carries on too.
        (hosted_start,) = of_type(events, InvocationToolCallStart)
        assert (hosted_start.provider_hosted, hosted_start.origin.attempt) == (True, 1)
        assert [event.origin.attempt for event in of_type(events, InvocationResumed)] == [2]
    finally:
        await host.shutdown()


async def test_an_acp_agent_that_cannot_reopen_for_a_retry_fails_the_node_rather_than_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expire = _hold_node_deadline(monkeypatch)
    real_build = agent_node_module.build_acp_node
    builds = 0

    def build(*args: Any, **kwargs: Any) -> Any:
        nonlocal builds
        builds += 1
        if builds == 2:
            raise RuntimeError("agent command vanished")
        return real_build(*args, **kwargs)

    monkeypatch.setattr(agent_node_module, "build_acp_node", create_autospec(real_build, side_effect=build))
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=3, timeout=_NODE_TIMEOUT, profile="External"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), _acp_profile("idle_stall")])

    async def expire_while_running(event: WorkflowNodeStateChanged) -> None:
        if event.state == "running":
            expire.set()

    await host.event_bus.subscribe(WorkflowNodeStateChanged, expire_while_running)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            result, _stream = await run(host, "review", input_text="x")
        # The timed-out attempt retired its agent; the retry's new one could not be built, which is a
        # launch failure: the node fails without a further automatic attempt, and the run is not cancelled.
        assert (result.outcome.value, result.node_id) == ("node_failed", "review")
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("failed", 2, "agent_non_transient"),
        ]
        assert "RuntimeError: agent command vanished" in of_type(events, WorkflowNodeStateChanged)[-1].error
        assert builds == 2
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, expire_while_running)
        await host.shutdown()


async def test_a_retired_acp_agent_that_fails_to_close_leaves_the_timeout_to_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expire = _hold_node_deadline(monkeypatch)
    teardown_failures = [RuntimeError("teardown failed")]

    class _TeardownFailsOnce(PreparedAgent):
        async def aclose(self) -> None:
            await super().aclose()
            if teardown_failures:
                raise teardown_failures.pop()

    monkeypatch.setattr(agent_node_module, "PreparedAgent", _TeardownFailsOnce)
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=_NODE_TIMEOUT, profile="External"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), _acp_profile("idle_stall")])

    async def expire_while_running(event: WorkflowNodeStateChanged) -> None:
        if event.state == "running":
            expire.set()

    await host.event_bus.subscribe(WorkflowNodeStateChanged, expire_while_running)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert (result.outcome.value, result.node_id) == ("node_failed", "review")
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("failed", 2, "agent_timeout"),
        ]
        # The first attempt's timeout retired its agent, whose close failed; the failure stayed out of the result.
        assert teardown_failures == []
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, expire_while_running)
        await host.shutdown()


async def test_an_acp_agent_its_connection_retries_cannot_reach_is_not_retried_by_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    real_connect = AcpAgentClient.connect
    connects = 0

    async def connect(client: AcpAgentClient) -> None:
        nonlocal connects
        connects += 1
        raise AcpConnectError("injected handshake failure")

    monkeypatch.setattr(AcpAgentClient, "connect", create_autospec(real_connect, side_effect=connect))
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2, profile="External"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), _acp_profile("happy")])
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert (result.outcome.value, result.node_id) == ("node_failed", "review")
        # The attempt's own connection retries already ran; the launch is broken, as a chat sub-agent treats it.
        assert _node_states(events) == [("running", 1, ""), ("failed", 1, "agent_non_transient")]
        assert connects == 6
    finally:
        await host.shutdown()


class _ShowsThenStops(MockChatClient):
    """Each scripted request streams one update, then fails or never answers again; the next answers "reviewed"."""

    def __init__(self, *passes: tuple[bool, BaseException | None]) -> None:
        super().__init__(
            responses=[*(MockResponse(text="never streamed") for _ in passes), MockResponse(text="reviewed")]
        )
        self._passes = list(passes)
        self.streaming = asyncio.Event()

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterable[ChatResponseUpdate]:
        if not self._passes:
            async for update in super()._stream_updates(resp, model_id, finish_reason):
                yield update
            return
        shows_text, failure = self._passes.pop(0)
        # Progress either way; only the shown kind carries text the node streams on.
        yield ChatResponseUpdate(
            contents=[Content.from_text("Looking. ")] if shows_text else [], role="assistant", model=model_id
        )
        self.streaming.set()
        if failure is not None:
            raise failure
        await asyncio.Event().wait()


def _archived_answers(host: ChrysSessionHost, run_id: str) -> list[str]:
    stored = read_node_transcript(run_dir(host.workflow_session_dir, run_id), "review@iter#1", 1)
    assert stored is not None
    return [
        content["text"]
        for message in stored.replay.messages
        if message["role"] == "assistant"
        for content in message["contents"]
        if content["type"] == "text"
    ]


@pytest.mark.parametrize("shows_text", [True, False], ids=["shown", "nothing-shown"])
async def test_a_transient_failure_resumes_and_asks_again_for_the_answer_it_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shows_text: bool
) -> None:
    """The next attempt starts by itself, and its request no longer ends with the half answer."""
    client = _ShowsThenStops((shows_text, ConnectionResetError("connection reset by peer")))
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2))
    host = make_host(
        tmp_path, project=project, stream=True, settings=Settings(model_profile="mock-profile", max_transient_retries=0)
    )
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "completed"
        assert result.outputs[0].value.text == "reviewed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_transient"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        assert len(client.call_history) == 2
        resumed, _options = client.call_history[1]
        assert _user_texts(list(resumed)) == ["x"]
        assert [message.text for message in resumed if message.role == "assistant"] == []
        if shows_text:
            # The shown text stays in the first attempt's transcript, as node details show it after a reload.
            assert _archived_answers(host, result.run_id) == ["Looking. "]
    finally:
        await host.shutdown()


def _answer_segments(events: list[Any]) -> list[tuple[int, str]]:
    """Each answer segment the node showed, whole, with the attempt that showed it."""
    segments: dict[str, tuple[int, str]] = {}
    for event in of_type(events, InvocationContentDelta):
        if event.channel == "answer":
            attempt, text = segments.get(event.segment_id, (event.origin.attempt, ""))
            segments[event.segment_id] = (attempt, text + event.text)
    return list(segments.values())


async def test_under_service_side_storage_a_failure_after_shown_text_is_left_to_the_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whole-run retry would send the request again and show a second answer, so the attempt fails instead."""
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    failure = ConnectionResetError("connection reset by peer")
    # The whole-run lane would retry this failure had the pass shown nothing.
    assert classify_error(failure).retryable
    client = _ShowsThenStops((True, failure))
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2))
    host = make_host(
        tmp_path,
        project=project,
        stream=True,
        chat_options=_SERVICE_SIDE_STORAGE,
        settings=Settings(model_profile="mock-profile", max_transient_retries=3),
    )
    try:
        await confirm(host, "review")
        async with capture_event_sequence(
            host.event_bus, WorkflowNodeStateChanged, InvocationRetryAttempt, InvocationContentDelta
        ) as events:
            result, _stream = await run(host, "review", input_text="x")
        assert result.outcome.value == "completed"
        assert result.outputs[0].value.text == "reviewed"
        assert of_type(events, InvocationRetryAttempt) == []
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_transient"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        # The first attempt showed its text once and nothing after it; only the next attempt answers.
        assert _answer_segments(events) == [(1, "Looking. "), (2, "reviewed")]
        assert len(client.call_history) == 2
        resumed, _options = client.call_history[1]
        assert [message.text for message in resumed if message.role == "assistant"] == []
        assert _archived_answers(host, result.run_id) == ["Looking. "]
    finally:
        await host.shutdown()


@pytest.mark.parametrize("shows_text", [True, False], ids=["shown", "nothing-shown"])
async def test_a_timeout_resumes_and_asks_again_for_the_answer_it_cut_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shows_text: bool
) -> None:
    expire = _hold_node_deadline(monkeypatch)
    client = _ShowsThenStops((shows_text, None))
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2, timeout=_NODE_TIMEOUT))
    host = make_host(tmp_path, project=project, stream=True)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged) as events:
            task = asyncio.create_task(run(host, "review", input_text="x"))
            # The update was handled before the stream is pulled again, so any text it carried is shown.
            await wait_for(
                lambda: client.streaming.is_set() or task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="the first pass streamed its update",
            )
            assert not task.done()
            expire.set()
            result, _stream = await task
        assert result.outcome.value == "completed"
        assert _node_states(events) == [
            ("running", 1, ""),
            ("retrying", 1, "agent_timeout"),
            ("running", 2, ""),
            ("completed", 2, ""),
        ]
        assert len(client.call_history) == 2
        resumed, _options = client.call_history[1]
        assert [message.text for message in resumed if message.role == "assistant"] == []
        if shows_text:
            assert _archived_answers(host, result.run_id) == ["Looking. "]
    finally:
        await host.shutdown()


async def test_a_cancelled_node_keeps_the_answer_it_showed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _ShowsThenStops((True, None))
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    project = make_project(tmp_path)
    write_workflow(project, "review", _workflow(max_attempts=2))
    host = make_host(tmp_path, project=project, stream=True)
    task: asyncio.Task[Any] | None = None
    try:
        await confirm(host, "review")
        task = asyncio.create_task(run(host, "review", input_text="x"))
        await wait_for(
            lambda: client.streaming.is_set() or task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the pass streamed its update",
        )
        assert not task.done()
        await host.cancel_workflow()
        result, _stream = await task
        assert result.outcome.value == "cancelled"
        assert _archived_answers(host, result.run_id) == ["Looking. "]
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
