# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Prepared close reaches actual registered kernel/ACP tool operations."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationCascadeAborted, InvocationPaused, UserMessage
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import get_platform
from chrys.foundation.util.sub_agent_context import SubAgentParentResultMetadata, sub_agent_parent_result_metadata
from chrys.orchestration.invoker.contracts import AbortCause, PreparedClosed
from chrys.orchestration.invoker.resources import OperationLifetime
from chrys.orchestration.sub_agents import tools as tools_module
from chrys.orchestration.sub_agents.acp_policy import AcpSubAgentPolicy
from chrys.orchestration.sub_agents.shell import SubAgentToolShell
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.hooks.events import HookEvent
from chrys.service.llm.clients import create_client
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    CompactionConfig,
    SubAgentRef,
    ToolsConfig,
)
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.session.sub_agent_logs import SubAgentSessionLogWriter
from tests.orchestration.invoker._build_fixtures import build_recipe_engine
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.close_races import assert_entered_before_completion
from tests.support.loaded_agents import install_loaded_agent
from tests.support.paths import SRC_ROOT
from tests.support.scripted_clients import ErrorMockChatClient, FrameworkBoom
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, with_wait_deadline


def test_parent_commit_cannot_rebind_after_consumption():
    shell = SubAgentToolShell(
        origin=InvocationOrigin("sub_agent", "session", "child", None),
        tool_name="Explore",
        agent_name="Explore",
        event_bus=None,
    )
    committed = []
    shell.bind_parent_interrupt_commit(lambda: committed.append("first"))
    with pytest.raises(RuntimeError, match="parent interrupt commit is already bound"):
        shell.bind_parent_interrupt_commit(lambda: committed.append("second"))
    # A synchronous shell has no running OperationLifetime to request_close.
    shell._commit_parent_interrupted_result()
    assert committed == ["first"]
    with pytest.raises(RuntimeError, match="parent interrupt commit is already bound"):
        shell.bind_parent_interrupt_commit(lambda: committed.append("third"))
    shell._commit_parent_interrupted_result()
    assert committed == ["first"]


@pytest.mark.parametrize("backend", ["kernel", "acp"])
@pytest.mark.parametrize("window", ["pause", "pause-callback", "returned"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_prepared_close_drains_registered_operation_in_all_three_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
    backend: str,
    window: str,
    engine_services,
    wait_for_child_window,
) -> None:
    entered, release_end, latched = asyncio.Event(), asyncio.Event(), asyncio.Event()
    bus = EventBus()
    tools = SubAgentTools(event_bus=bus, session_id="parent", session_dir=tmp_path, max_transient_retries=0)
    client = ErrorMockChatClient([MockResponse(text="done") if window == "returned" else FrameworkBoom("pause")])
    parent_root = tmp_path / "parent"
    parent_root.mkdir()
    parent, _, _ = await build_recipe_engine(
        agent_engine, monkeypatch, parent_root, main=[MockResponse(text="parent ready")], child=[]
    )
    await parent.event_bus.publish(UserMessage(text="parent opener"))
    await parent.wait_for_run_task()
    install_loaded_agent(parent, sub_agent_tools=tools)
    # The independently registered child still uses this test's scripted model.
    monkeypatch.setattr(tools_module, "create_client", create_autospec(create_client, return_value=client))
    original_write = SubAgentSessionLogWriter.write
    original_hook = tools._fire_sub_agent_hook
    order: list[str] = []
    terminals, cascades, finishes, unlinks = [], [], [], []
    original_finish = OperationLifetime.finish
    original_unlink = tools_module.secure_unlink_owner_verified
    original_save = engine_services(parent).persistence.save_session

    def finish(lifetime):
        finishes.append(lifetime)
        original_finish(lifetime)

    def unlink(path):
        unlinks.append(path)
        order.append("pending-unlink")
        return original_unlink(path)

    async def save(*args, **kwargs):
        result = await original_save(*args, **kwargs)
        order.append("parent-save-success" if result else "parent-save-failure")
        return result

    monkeypatch.setattr(OperationLifetime, "finish", create_autospec(original_finish, side_effect=finish))
    monkeypatch.setattr(
        tools_module, "secure_unlink_owner_verified", create_autospec(original_unlink, side_effect=unlink)
    )
    monkeypatch.setattr(
        engine_services(parent).persistence, "save_session", create_autospec(original_save, side_effect=save)
    )

    async def cascaded(event):
        cascades.append(event)

    await bus.subscribe(InvocationCascadeAborted, cascaded)
    # Callback never has to be released for close to drain it. This pins the
    # old kernel late-future gap without changing the standalone cascade oracle.
    pause_callback_release = asyncio.Event()

    async def write(*args: object, **kwargs: object) -> bool:
        if kwargs.get("ended"):
            terminals.append(kwargs["status"])
        if window == "pause-callback" and kwargs["status"] == "paused":
            entered.set()
            await pause_callback_release.wait()
        result = await original_write(*args, **kwargs)
        if kwargs.get("ended"):
            order.append("terminal-writer")
        return result

    async def hook(*args: object, **kwargs: object) -> None:
        if args[0] == HookEvent.SUB_AGENT_END:
            order.append("end-hook")
            if window == "returned":
                entered.set()
                await release_end.wait()
            order.append("end-hook-drained")
        await original_hook(*args, **kwargs)

    async def paused(event: InvocationPaused) -> None:
        if window == "pause":
            entered.set()

    monkeypatch.setattr(SubAgentSessionLogWriter, "write", create_autospec(original_write, side_effect=write))
    monkeypatch.setattr(tools, "_fire_sub_agent_hook", create_autospec(original_hook, side_effect=hook))
    await bus.subscribe(InvocationPaused, paused)
    runtime = SessionEnvironment(cwd=str(tmp_path), platform=get_platform())
    ref = SubAgentRef(profile="Explore", tool_name="Explore")
    if backend == "kernel":
        await tools.register(
            ref,
            AgentProfile(name="Explore", tools=ToolsConfig(builtins=[]), compaction=CompactionConfig(enabled=False)),
            runtime,
            settings=Settings(),
            fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock", stream=False),
        )
    else:
        await tools.register_acp(
            ref,
            AgentProfile(
                name="Explore",
                acp=AcpAgentConfig(
                    command=sys.executable,
                    args=[str(STUB_SCRIPT)],
                    env={
                        "PYTHONPATH": str(SRC_ROOT),
                        "CHRYS_ACP_STUB_SCENARIO": "happy" if window == "returned" else "crash_mid_prompt",
                    },
                    cwd=str(tmp_path),
                    handshake_timeout_seconds=20,
                ),
            ),
            runtime,
        )
    prepared = tools._prepared_by_tool["Explore"]
    parent_results: list[dict[str, object]] = []
    parent_metadata = SubAgentParentResultMetadata(
        parent_provider_call_id="parent-call",
        parent_event_call_id="parent:1",
        commit_interrupted_result=lambda metadata: parent_results.append(dict(metadata)),
    )
    token = sub_agent_parent_result_metadata.set(parent_metadata)
    try:
        operation = asyncio.create_task(tools.get_tools()[0].func(prompt="work"))
    finally:
        sub_agent_parent_result_metadata.reset(token)
    closing: asyncio.Task[None] | None = None
    try:
        await wait_for_child_window(
            operation, entered, "child operation reached the close window", acp=backend == "acp"
        )
        controller = next(iter(tools._controllers.values()))
        original_request = controller.request_close
        original_commit = controller._parent_interrupted_result_commit
        assert original_commit is not None
        assert parent_metadata.interrupted_result_committed is False
        assert parent_results == []
        commits: list[bool] = []
        snapshots: list[dict[str, bool]] = []
        decision = controller._pending_decision
        if window == "pause":
            assert decision is not None and not decision.done()
        else:
            assert decision is None

        def commit() -> None:
            original_commit()
            commits.append(True)

        monkeypatch.setattr(controller, "_parent_interrupted_result_commit", commit)

        def request(cause: AbortCause) -> None:
            assert cause is AbortCause.OWNER_CLOSE
            original_request(cause)
            # No await or scheduled observer: preserve the state on return
            # from this very call, before close_with can execute cascade_abort.
            snapshot = {
                "cascade": controller._cascade_requested,
                "parent_committed": (
                    commits == [True]
                    and controller._parent_interrupted_result_commit is None
                    and parent_metadata.interrupted_result_committed
                    and len(parent_results) == 1
                    and parent_results[0]["sub_agent_invocation_id"] == parent_metadata.sub_agent_invocation_id
                ),
            }
            if decision is not None:
                snapshot["decision"] = decision.done() and decision.result() == "cascade_abort"
            if isinstance(controller.policy, AcpSubAgentPolicy):
                snapshot["broker_aborted"] = controller.policy.backend._broker._aborted
                snapshot["cascade_event"] = controller.policy.backend._cascade_event.is_set()
            snapshots.append(snapshot)
            latched.set()

        monkeypatch.setattr(controller, "request_close", create_autospec(original_request, side_effect=request))
        drain_entered = asyncio.Event()
        original_drained = type(controller).drained.fget

        async def observed_drained(self) -> None:
            drain_entered.set()
            await original_drained(self)

        monkeypatch.setattr(type(controller), "drained", property(observed_drained))
        closing = asyncio.create_task(prepared.aclose())
        await asyncio.wait_for(latched.wait(), 5)
        await assert_entered_before_completion(drain_entered, closing)
        if window == "returned":
            assert not closing.done()
            assert tools._controllers
            release_end.set()
        await asyncio.wait_for(closing, 10)
        assert operation.done()
        assert "terminal-writer" in order
        assert "end-hook-drained" in order
        assert len(snapshots) == 1
        for value in snapshots[0].values():
            assert value is True, snapshots
        await asyncio.wait_for(controller.drained, 5)
        results = await asyncio.gather(operation, return_exceptions=True)
        if window != "returned":
            assert isinstance(results[0], asyncio.CancelledError)
        assert tools._controllers == {}
        assert tools._live_approvals == []
        assert tools._total_active == 0
        assert "terminal-writer" in order
        assert "end-hook-drained" in order
        assert not pause_callback_release.is_set()
        audits = [json.loads(path.read_text()) for path in (tmp_path / "sub_agents" / "sessions").glob("*.json")]
        assert len(audits) == 1
        assert audits[0]["meta"]["status"] in {"completed", "cancelled", "cascade_aborted"}
        assert len(finishes) == 1
        assert finishes[0] is controller._operation
        assert len(cascades) == 1
        assert cascades[0].origin is controller.origin
        expected_terminals = (
            ["completed", "cascade_aborted"]
            if window == "returned" and backend == "kernel"
            else ["completed" if window == "returned" else "cancelled" if backend == "acp" else "cascade_aborted"]
        )
        assert terminals == expected_terminals
        assert order.count("end-hook-drained") == 1
        assert unlinks == []
        [pending] = tools._pending_cleanup_paths
        assert pending.exists() is (window != "returned")
        assert await parent.writer.save_current_session() is True
        assert unlinks == [pending]
        assert order.index("parent-save-success") < order.index("pending-unlink")
        assert not pending.exists()
        assert tools._pending_cleanup_paths == set()
        # A closed owner cannot create another runtime even via the retained tool.
        with pytest.raises(PreparedClosed):
            await tools.get_tools()[0].func(prompt="late")
    finally:
        pause_callback_release.set()
        release_end.set()
        if not operation.done():
            operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await bus.unsubscribe(InvocationPaused, paused)
        await tools.cleanup()
        await parent.shutdown()
