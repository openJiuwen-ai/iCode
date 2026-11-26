# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The current child operation outlives its controller's returned result."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import get_platform
from chrys.kernel import Agent
from chrys.orchestration.invoker.acp_protocol import AcpPermissionBroker
from chrys.orchestration.invoker.contracts import SubAgentStatus
from chrys.orchestration.sub_agents import tools as tools_module
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.acp_client import AcpAgentClient
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.hooks.events import HookEvent
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    CompactionConfig,
    SubAgentRef,
    ToolsConfig,
)
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.sub_agent_logs import SubAgentSessionLogWriter
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.paths import SRC_ROOT
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, with_wait_deadline


@pytest.mark.parametrize("cancel_at_end", [False, True])
@pytest.mark.parametrize("backend", ["kernel", "acp"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_returned_child_retains_registry_until_writer_usage_and_end_hook_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_at_end: bool, backend: str, wait_for_child_window
) -> None:
    client = MockChatClient(responses=[MockResponse(text="done")])
    monkeypatch.setattr(tools_module, "create_client", create_autospec(tools_module.create_client, return_value=client))
    order: list[str] = []
    transport_states: list[tuple[int | None, bool]] = []
    completed_writes: list[bool] = []
    end_entered = asyncio.Event()
    end_release = asyncio.Event()

    async def drain_usage() -> None:
        order.append("usage-drained")

    tools = SubAgentTools(
        event_bus=EventBus(), session_id="parent", session_dir=tmp_path, drain_parent_usage_publishes=drain_usage
    )
    original_hook = tools._fire_sub_agent_hook
    original_write = SubAgentSessionLogWriter.write
    original_close = ApprovalMiddleware.close
    original_broker_close = AcpPermissionBroker.close
    original_force_close = AcpAgentClient.force_close
    agent_enter = create_autospec(Agent.__aenter__, side_effect=Agent.__aenter__)
    agent_exit = create_autospec(Agent.__aexit__, side_effect=Agent.__aexit__)
    monkeypatch.setattr(Agent, "__aenter__", agent_enter)
    monkeypatch.setattr(Agent, "__aexit__", agent_exit)

    async def hook(*args: object, **kwargs: object) -> None:
        # autospec below validates the real signature; all arguments are forwarded.
        if args[0] == HookEvent.SUB_AGENT_END:
            order.append("end-entered")
            assert len(tools._controllers) == 1
            assert next(iter(tools._controllers.values())).status == SubAgentStatus.COMPLETED
            end_entered.set()
            await end_release.wait()
            order.append("end-finished")
        await original_hook(*args, **kwargs)

    async def write(*args: object, **kwargs: object) -> bool:
        result = await original_write(*args, **kwargs)
        if kwargs["status"] == "completed":
            order.append("writer-completed")
            completed_writes.append(result)
        return result

    async def close(approval: ApprovalMiddleware) -> None:
        assert tools._controllers == {}
        order.append("approval-close")
        await original_close(approval)

    async def broker_close(broker: AcpPermissionBroker) -> None:
        assert len(tools._controllers) == 1
        await original_broker_close(broker)
        order.append("broker-close")

    async def force_close(client: AcpAgentClient) -> None:
        await original_force_close(client)
        spawn = client._spawn
        consumer = client._consumer_task
        transport_states.append(
            (spawn.process.returncode if spawn is not None else None, consumer is not None and consumer.done())
        )
        order.append("transport-closed")

    monkeypatch.setattr(tools, "_fire_sub_agent_hook", create_autospec(original_hook, side_effect=hook))
    monkeypatch.setattr(SubAgentSessionLogWriter, "write", create_autospec(original_write, side_effect=write))
    monkeypatch.setattr(ApprovalMiddleware, "close", create_autospec(original_close, side_effect=close))
    monkeypatch.setattr(AcpPermissionBroker, "close", create_autospec(original_broker_close, side_effect=broker_close))
    monkeypatch.setattr(AcpAgentClient, "force_close", create_autospec(original_force_close, side_effect=force_close))
    runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
    if backend == "kernel":
        await tools.register(
            SubAgentRef(profile="Explore", tool_name="Explore"),
            AgentProfile(name="Explore", tools=ToolsConfig(builtins=[]), compaction=CompactionConfig(enabled=False)),
            runtime,
            settings=Settings(),
            fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock", stream=False),
        )
    else:
        await tools.register_acp(
            SubAgentRef(profile="Explore", tool_name="Explore"),
            AgentProfile(
                name="Explore",
                acp=AcpAgentConfig(
                    command=sys.executable,
                    args=[str(STUB_SCRIPT)],
                    env={"PYTHONPATH": str(SRC_ROOT), "CHRYS_ACP_STUB_SCENARIO": "happy"},
                    cwd=str(tmp_path),
                    handshake_timeout_seconds=20,
                ),
            ),
            runtime,
        )
    task = asyncio.create_task(tools.get_tools()[0].func(prompt="work"))
    try:
        await wait_for_child_window(task, end_entered, "sub-agent end hook entered", acp=backend == "acp")
        assert not task.done()
        assert tools._total_active == 1
        assert bool(tools._live_approvals) is (backend == "kernel")
        prefix = [
            *(["transport-closed"] if backend == "acp" else []),
            "writer-completed",
            *(["broker-close"] if backend == "acp" else []),
            "usage-drained",
            "end-entered",
        ]
        assert order == prefix
        assert completed_writes == [True]
        if backend == "acp":
            assert len(transport_states) == 1
            returncode, consumer_done = transport_states[0]
            assert returncode is not None
            assert consumer_done
        if cancel_at_end:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            end_release.set()
            assert await task == ("done" if backend == "kernel" else "stub response")
        assert order == [
            *prefix,
            *([] if cancel_at_end else ["end-finished"]),
            *(["approval-close"] if backend == "kernel" else []),
        ]
        assert tools._controllers == {}
        assert tools._live_approvals == []
        assert tools._total_active == 0
        assert agent_enter.call_count == (1 if backend == "kernel" else 0)
        assert agent_exit.call_count == 0
        if backend == "kernel":
            assert agent_enter.call_args.args[0] is tools._agents["Explore"]
    finally:
        end_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await tools.cleanup()
    assert agent_exit.call_count == (1 if backend == "kernel" else 0)
    if backend == "kernel":
        assert agent_exit.call_args.args[0] is agent_enter.call_args.args[0]
