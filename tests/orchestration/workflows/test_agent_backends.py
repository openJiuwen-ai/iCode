# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow agent activations borrow MCP connections and own fresh ACP transports."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import create_autospec

import psutil
import pytest

import chrys.service.acp_client.client as acp_client_module
import chrys.service.mcp._connection as mcp_connection_module
from chrys.foundation.events.types import (
    InvocationResumed,
    InvocationStarted,
    InvocationToolCallResult,
    WorkflowNodeRetryRequest,
    WorkflowNodeStateChanged,
)
from chrys.service.acp_client import AcpAgentClient
from chrys.service.acp_client.spawn import AcpSpawnResult
from chrys.service.acp_client.spec import AcpAgentSpec
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    MCPServerConfig,
    ModelConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.runtime_metadata import TOTAL_SESSION_TOKENS_KEY
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.transcript import read_node_transcript, read_node_usage
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    hold_workflow_deadline,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
)
from tests.service.mcp.test_cache import _FakeMCPTool
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.event_capture import capture_event_sequence
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


def _workflow(profile: str) -> bytes:
    return (
        "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
        f"node = wf.agent('node', profile={profile!r})\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
    ).encode()


async def test_workflow_agent_uses_session_mcp_cache_and_releases_its_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_client = MockChatClient(
        responses=[MockResponse(tool_calls=[("remote", "call1", {"value": "mcp result"})]), MockResponse(text="done")]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node_client])
    config = MCPServerConfig(name="srv", transport="stdio", command="unused")
    profile = make_profile()
    profile.tools.mcp = [config]
    fake = _FakeMCPTool()
    factory = create_autospec(mcp_connection_module._create_mcp_tool, return_value=fake)
    monkeypatch.setattr(mcp_connection_module, "_create_mcp_tool", factory)
    disconnect = MCPAdapter.disconnect_all
    released: list[MCPAdapter] = []

    async def release(self: MCPAdapter) -> None:
        await disconnect(self)
        released.append(self)

    monkeypatch.setattr(MCPAdapter, "disconnect_all", create_autospec(disconnect, side_effect=release))
    project = make_project(tmp_path)
    write_workflow(project, "mcp", _workflow(PROFILE))
    host = make_host(tmp_path, project=project, profiles=[profile])
    try:
        await confirm(host, "mcp")
        result, _events = await run(host, "mcp")
        assert result.outcome.value == "completed"
        assert factory.call_count == fake.enter_count == 1
        assert len(released) == 1 and released[0].server_names == []
        assert any(
            content.result == "mcp result"
            for message in node_client.call_history[1][0]
            for content in message.contents
            if content.type == "function_result"
        ), node_client.call_history[1][0]
        assert fake.exit_count == 0  # the session cache still owns the shared connection
    finally:
        await host.shutdown()
    assert fake.exit_count == 1


@pytest.mark.parametrize(
    ("chat_options", "warned"),
    [("", True), ('{"thinking": {"type": "disabled"}}', False)],
    ids=["no-thinking-option", "thinking-disabled"],
)
async def test_workflow_agent_warns_once_when_its_on_demand_mcp_tools_can_unbind_its_thinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, chat_options: str, warned: bool
) -> None:
    """The node's own model and chat options decide the warning; the session's mock model would not warn."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])
    profile = make_profile("MCP")
    profile.model = ModelConfig(profile_id="claude")
    profile.tools.mcp = [
        MCPServerConfig(name=name, transport="stdio", command="unused", use_progressive_disclosure=True)
        for name in ("alpha", "down", "beta")
    ]

    async def connect(_adapter: MCPAdapter, config: MCPServerConfig) -> list[object]:
        if config.name == "down":
            raise RuntimeError("server down")
        return []

    monkeypatch.setattr(MCPAdapter, "connect", create_autospec(MCPAdapter.connect, side_effect=connect))
    project = make_project(tmp_path)
    write_workflow(project, "mcp", _workflow("MCP"))
    claude = ModelProfile(
        id="claude", name="Claude 5.5", provider="anthropic", model_id="claude-opus-5-5", chat_options=chat_options
    )
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile], models=[claude])
    try:
        await confirm(host, "mcp")
        with caplog.at_level(logging.WARNING, logger="chrys.service.mcp.thinking_warning"):
            result, _events = await run(host, "mcp")
        assert result.outcome.value == "completed"
    finally:
        await host.shutdown()

    messages = [record.getMessage() for record in caplog.records if record.name == "chrys.service.mcp.thinking_warning"]
    if warned:
        [message] = messages
        assert message.startswith(
            "Agent 'MCP' on model profile 'Claude 5.5': MCP server(s) 'alpha', 'beta' load tools on demand"
        )
    else:
        assert messages == []


async def test_cancellation_while_mcp_shell_opens_disconnects_partial_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    profile = AgentProfile(
        name="MCP",
        tools=ToolsConfig(builtins=[], mcp=[MCPServerConfig(name="srv", transport="stdio", command="unused")]),
    )
    original = MCPAdapter.connect_all
    connected = asyncio.Event()
    adapters: list[MCPAdapter] = []
    fake = _FakeMCPTool()
    monkeypatch.setattr(
        mcp_connection_module,
        "_create_mcp_tool",
        create_autospec(mcp_connection_module._create_mcp_tool, return_value=fake),
    )

    async def connect(self: MCPAdapter, configs, *, progress=None):
        tools = await original(self, configs, progress=progress)
        adapters.append(self)
        connected.set()
        await asyncio.Event().wait()
        return tools

    monkeypatch.setattr(MCPAdapter, "connect_all", create_autospec(original, side_effect=connect))
    project = make_project(tmp_path)
    path = write_workflow(project, "mcp", _workflow("MCP"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    task = None
    try:
        await confirm(host, "mcp")
        task = asyncio.create_task(run(host, "mcp"))
        await wait_for(
            lambda: connected.is_set() or task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="workflow MCP acquisition",
        )
        if task.done():
            await task
        assert connected.is_set()
        active_source = host.engine.workflows.active_source
        assert active_source is not None and active_source.canonical_path == str(path.resolve())
        await host.cancel_workflow()
        result, _events = await task
        assert result.outcome.value == "cancelled"
        assert len(adapters) == 1 and adapters[0].server_names == []
        assert host.engine.workflows.active_source is None
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
    assert fake.exit_count == 1


@pytest.mark.parametrize(
    "scenario", ["happy", "idle_stall", "deadline", "override", "retry_reused_tool_usage", "timeout_reused_tool_usage"]
)
async def test_acp_workflow_completion_cancel_and_automatic_retry_reap_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    expire = hold_workflow_deadline(monkeypatch, 3600) if scenario == "deadline" else None
    trace = tmp_path / "acp-attempts.jsonl"
    profile = AgentProfile(
        name="External",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=[str(STUB_SCRIPT)],
            env={
                "CHRYS_ACP_STUB_SCENARIO": "idle_stall" if scenario == "deadline" else scenario,
                "CHRYS_ACP_STUB_FLAG_FILE": str(trace),
            },
            idle_timeout_seconds=0,
        ),
    )
    processes: list[psutil.Process] = []
    specs: list[AcpAgentSpec] = []
    prompts: list[str] = []
    original_spawn = acp_client_module.spawn_acp_process

    async def spawn(spec: AcpAgentSpec) -> AcpSpawnResult:
        specs.append(spec)
        result = await original_spawn(spec)
        processes.append(psutil.Process(result.process.pid))
        return result

    monkeypatch.setattr(acp_client_module, "spawn_acp_process", create_autospec(original_spawn, side_effect=spawn))
    prompting = asyncio.Event()
    original_prompt = AcpAgentClient.prompt

    async def prompt(self: AcpAgentClient, text: str):
        prompts.append(text)
        prompting.set()
        return await original_prompt(self, text)

    monkeypatch.setattr(AcpAgentClient, "prompt", create_autospec(original_prompt, side_effect=prompt))
    project = make_project(tmp_path)
    source = _workflow("External")
    if scenario == "override":
        source = source.replace(
            b"profile='External'", b"profile='External', model='mock', instructions_suffix='Be terse.'"
        )
    write_workflow(project, "external", source)
    if scenario == "timeout_reused_tool_usage":
        source = source.replace(b"profile='External'", b"profile='External', timeout=3601")
        write_workflow(project, "external", source)
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile], allow_user_interaction=True)
    tool_finished = asyncio.Event()

    async def on_tool(event: InvocationToolCallResult) -> None:
        tool_finished.set()

    await host.event_bus.subscribe(InvocationToolCallResult, on_tool)
    original_wait = asyncio.wait
    expired = False

    async def expire_first_pass(tasks, *, timeout=None, return_when=asyncio.ALL_COMPLETED):
        nonlocal expired
        if scenario == "timeout_reused_tool_usage" and timeout == 3601 and not expired:
            expired = True
            await tool_finished.wait()
            return set(), set(tasks)
        return await original_wait(tasks, timeout=timeout, return_when=return_when)

    monkeypatch.setattr(asyncio, "wait", expire_first_pass)
    opening_prompts: list[str] = []

    async def started(event: InvocationStarted) -> None:
        if event.origin.kind == "workflow_node":
            opening_prompts.append(event.opening_prompt)

    awaiting: list[WorkflowNodeStateChanged] = []

    async def retry(event: WorkflowNodeStateChanged) -> None:
        # A timeout or a remote error retries on its own; a stop for a decision is recorded, then answered.
        if event.state == "awaiting_retry":
            awaiting.append(event)
            await host.event_bus.publish(
                WorkflowNodeRetryRequest(
                    run_id=event.run_id,
                    node_id=event.node_id,
                    activation_id=event.activation_id,
                    request_id="retry",
                    expected_failed_attempt=event.attempt,
                )
            )

    await host.event_bus.subscribe(WorkflowNodeStateChanged, retry)
    await host.event_bus.subscribe(InvocationStarted, started)
    task = None
    try:
        await confirm(host, "external")
        task = asyncio.create_task(
            run(host, "external", input_text="Review the input", timeout=3600 if expire is not None else 0)
        )
        await wait_for(
            lambda: prompting.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="ACP workflow prompt"
        )
        if task.done():
            await task
        assert prompting.is_set()
        if scenario == "idle_stall":
            await host.cancel_workflow()
        elif expire is not None:
            expire.set()
        result, events = await task
        assert opening_prompts == [prompts[0]]
        assert result.outcome.value == ("cancelled" if scenario in {"idle_stall", "deadline"} else "completed")
        assert all(not process.is_running() for process in processes)
        if scenario not in {"idle_stall", "deadline"}:
            assert result.outputs[0].value.text == "stub response"
        if scenario == "override":
            assert specs[0].model_id == "mock"
            assert prompts == ["Review the input\n\nBe terse."]
        if scenario in {"retry_reused_tool_usage", "timeout_reused_tool_usage"}:
            assert awaiting == []
            records = [json.loads(line) for line in trace.read_text().splitlines()]
            assert len(records) == len(processes) == 2
            assert records[0]["pid"] != records[1]["pid"]
            assert records[0]["prompt"] == records[1]["prompt"] == ["Review the input"]
            assert [
                event.attempt
                for event in events
                if isinstance(event, WorkflowNodeStateChanged) and event.state == "running"
            ] == [1, 2]
        else:
            assert len(processes) == 1
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, result.run_id)
        attempt = 2 if scenario in {"retry_reused_tool_usage", "timeout_reused_tool_usage"} else 1
        transcript = read_node_transcript(directory, "node@iter#1", attempt)
        assert transcript is not None
        user_messages = [message for message in transcript.replay.messages if message["role"] == "user"]
        assert len(user_messages) == 1 and transcript.replay.messages[0] == user_messages[0]
        assert user_messages[0]["contents"] == [{"type": "text", "text": prompts[-1]}]
        # The archive records the pass outcome. The scheduler separately marks
        # the whole run cancelled when its deadline aborts this timed-out pass.
        expected_status = (
            "failed" if scenario == "deadline" else "cancelled" if scenario == "idle_stall" else "completed"
        )
        assert transcript.status == expected_status
        if transcript.status == "completed":
            assert "stub response" in str(transcript.replay.messages)
        if attempt == 2:
            previous = read_node_transcript(directory, "node@iter#1", 1)
            assert previous is not None and previous.status == "failed"
            assert previous.replay.messages != transcript.replay.messages
            assert previous.replay.messages[0] == user_messages[0]
            assert (previous.usage.tool_calls, previous.usage.usage_tokens) == (1, 21)
            assert (transcript.usage.tool_calls, transcript.usage.usage_tokens) == (2, 29)
            usage = read_node_usage(directory, "node@iter#1", 2)
            assert usage is not None and usage.usage_tokens == 29
            session_state = (
                await JsonFileStateStore(tmp_path / "sessions").load_workflow_session(host.workflow_session_id)
            ).encode()
            assert session_state is not None and session_state[TOTAL_SESSION_TOKENS_KEY] == 29
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, retry)
        await host.event_bus.unsubscribe(InvocationStarted, started)
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


async def test_acp_progress_is_archived_before_terminal_and_cancel_preserves_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.orchestration.workflows.agent_archive import AgentNodeArchive

    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    profile = AgentProfile(
        name="External",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=[str(STUB_SCRIPT)],
            env={"CHRYS_ACP_STUB_SCENARIO": "text_then_stall"},
            idle_timeout_seconds=0,
        ),
    )
    project = make_project(tmp_path)
    write_workflow(project, "external", _workflow("External"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    checkpointed = asyncio.Event()
    writes = []
    real_write = AgentNodeArchive.write

    async def record(archive, **kwargs):
        await real_write(archive, **kwargs)
        state = kwargs["acp_state"]
        if state and state["translated_updates"]:
            writes.append((kwargs["status"], kwargs["stats"].tool_call_count))
            checkpointed.set()

    monkeypatch.setattr(AgentNodeArchive, "write", create_autospec(real_write, side_effect=record))
    task = None
    try:
        await confirm(host, "external")
        task = asyncio.create_task(run(host, "external"))
        await wait_for(lambda: checkpointed.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
        if task.done():
            await task
        assert checkpointed.is_set()
        assert writes == [("running", 1)] and not task.done()
        await host.cancel_workflow()
        result, events = await task
        assert result.outcome.value == "cancelled"
        assert writes[-1] == ("cancelled", 1)
        activation = next(event.activation_id for event in events if isinstance(event, WorkflowNodeStateChanged))
        assert host.workflow_session_dir is not None
        transcript = read_node_transcript(run_dir(host.workflow_session_dir, result.run_id), activation, 1)
        assert transcript is not None and transcript.status == "cancelled"
        assert "checkpoint progress" in str(transcript.replay.messages)
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    ("result_mode", "expected"), [("transcript", "first continuation\n\nsecond"), ("last_segment", "second")]
)
async def test_acp_node_value_has_the_extent_the_profile_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result_mode: str, expected: str
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    profile = AgentProfile(
        name="External",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=[str(STUB_SCRIPT)],
            env={"CHRYS_ACP_STUB_SCENARIO": "message_id_boundaries"},
            result_mode=result_mode,
        ),
    )
    project = make_project(tmp_path)
    write_workflow(project, "external", _workflow("External"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    try:
        await confirm(host, "external")
        result, _ = await run(host, "external")
        assert result.outcome.value == "completed"
        assert [item.value.text for item in result.outputs] == [expected]
    finally:
        await host.shutdown()


@pytest.mark.parametrize(
    ("scenario", "states"),
    [
        (
            "idle_stall",
            [
                ("running", 1, ""),
                ("retrying", 1, "agent_transient"),
                ("running", 2, ""),
                ("failed", 2, "agent_transient"),
            ],
        ),
        ("auth_required", [("running", 1, ""), ("failed", 1, "agent_non_transient")]),
    ],
)
async def test_an_acp_node_retries_a_silent_agent_in_a_fresh_session_but_not_a_login_it_cannot_perform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str, states: list[tuple[str, int, str]]
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    profile = AgentProfile(
        name="External",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=[str(STUB_SCRIPT)],
            env={"CHRYS_ACP_STUB_SCENARIO": scenario},
            idle_timeout_seconds=0.5,
        ),
    )
    processes: list[psutil.Process] = []
    earlier_alive_at_spawn: list[bool] = []
    original_spawn = acp_client_module.spawn_acp_process

    async def spawn(spec: AcpAgentSpec) -> AcpSpawnResult:
        earlier_alive_at_spawn.append(any(process.is_running() for process in processes))
        result = await original_spawn(spec)
        processes.append(psutil.Process(result.process.pid))
        return result

    monkeypatch.setattr(acp_client_module, "spawn_acp_process", create_autospec(original_spawn, side_effect=spawn))
    project = make_project(tmp_path)
    write_workflow(
        project,
        "external",
        (
            b"from chrys.workflows import Retry, WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
            b"node = wf.agent('node', profile='External', retry=Retry(max_attempts=2))\n"
            b"wf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
        ),
    )
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    try:
        await confirm(host, "external")
        async with capture_event_sequence(host.event_bus, WorkflowNodeStateChanged, InvocationResumed) as events:
            result, _stream = await run(host, "external", input_text="Review the input")
        assert result.outcome.value == "node_failed"
        node_states = [event for event in events if isinstance(event, WorkflowNodeStateChanged)]
        assert [(event.state, event.attempt, event.error_class) for event in node_states] == states
        attempts = states[-1][1]
        # Every attempt opens its own session in its own process; none carries an earlier one on.
        assert len(processes) == len({process.pid for process in processes}) == attempts
        # A retry spawns its agent only after the failed attempt's process was reaped.
        assert earlier_alive_at_spawn == [False] * attempts
        assert all(not process.is_running() for process in processes)
        assert not [event for event in events if isinstance(event, InvocationResumed)]
    finally:
        await host.shutdown()


async def test_workflow_agent_hands_its_servers_cwd_and_the_session_folder_to_the_mcp_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The node build passes the session folder; ``cwd`` is resolved against it in the connection cache."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])
    profile = make_profile("MCP")
    profile.tools.mcp = [MCPServerConfig(name="own", transport="stdio", command="unused", cwd="tools/srv")]
    seen: list[tuple[str, str | None]] = []

    async def connect(adapter: MCPAdapter, config: MCPServerConfig) -> list[object]:
        seen.append((config.cwd, adapter._stdio_cwd))
        return []

    monkeypatch.setattr(MCPAdapter, "connect", create_autospec(MCPAdapter.connect, side_effect=connect))
    project = make_project(tmp_path)
    write_workflow(project, "mcp", _workflow("MCP"))
    host = make_host(tmp_path, project=project, profiles=[make_profile(), profile])
    try:
        await confirm(host, "mcp")
        result, _events = await run(host, "mcp")
        assert result.outcome.value == "completed"
    finally:
        await host.shutdown()

    [(configured, base)] = seen
    assert configured == "tools/srv"
    assert base is not None and os.path.samefile(base, project)
