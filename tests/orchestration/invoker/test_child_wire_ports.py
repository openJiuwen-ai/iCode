# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real SubAgentTool registration keeps the controller's wire configuration live."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationRetryAttempt
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.platform import get_platform
from chrys.kernel import StallExhaustedAction
from chrys.orchestration.invoker.attempts import WireRetryPolicyAdapter
from chrys.orchestration.sub_agents import tools as tools_module
from chrys.orchestration.sub_agents.kernel_policy import KernelSubAgentPolicy
from chrys.orchestration.sub_agents.shell import SubAgentToolShell
from chrys.orchestration.sub_agents.tools import SubAgentTools
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.schema import AgentProfile, CompactionConfig, SubAgentRef, ToolsConfig
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.trajectory.retries import RetryBackoffTrace
from tests.kernel.test_wire_retry import _ScriptedWire, _text_response, _text_update
from tests.support.event_capture import capture_event_sequence
from tests.support.frozen_clock import freeze_monotonic


@pytest.mark.parametrize("stream", [False, True])
async def test_real_child_tool_reads_changed_budget_and_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stream: bool
) -> None:
    wire = _ScriptedWire(
        [
            ConnectionError("first"),
            ConnectionError("second"),
            [_text_update("done")] if stream else _text_response("done"),
        ]
    )
    client = MockChatClient()
    monkeypatch.setattr(
        client, "_inner_get_response", create_autospec(client._inner_get_response, side_effect=wire.get_response)
    )
    monkeypatch.setattr(tools_module, "create_client", create_autospec(tools_module.create_client, return_value=client))
    original_run = SubAgentToolShell.run
    controllers: list[SubAgentToolShell] = []
    policies: list[WireRetryPolicyAdapter] = []
    sleeps: list[int] = []

    async def run(controller: SubAgentToolShell) -> str:
        policy = controller.policy._run_kwargs["client_kwargs"]["wire_retry_policy"]
        assert isinstance(policy, WireRetryPolicyAdapter)
        # The policy is already constructed by the real tool entry here.
        assert policy.max_retries == 0
        controller.policy._max_retries = 2
        controller.policy._backoff = (13, 17)
        assert policy.max_retries == policy.stall_max_retries == 2
        assert policy.backoff_seconds(0) == 13
        assert policy.stall_exhausted_action is StallExhaustedAction.RAISE
        assert policy.prepare_retry is None
        validation = controller.policy._hosted_commits_probe.__self__
        assert policy.hosted_commits_in_flight == validation.hosted_commits_in_flight
        assert controller.policy._hosted_commits_probe == validation.hosted_commits_observed
        controllers.append(controller)
        policies.append(policy)
        return await original_run(controller)

    async def sleep(controller: KernelSubAgentPolicy, seconds: int) -> bool:
        sleeps.append(seconds)
        # Also change backoff between wire attempts, after kernel acquired policy.
        controller._backoff = (19, 23)
        return False

    monkeypatch.setattr(SubAgentToolShell, "run", create_autospec(original_run, side_effect=run))
    monkeypatch.setattr(
        KernelSubAgentPolicy,
        "sleep_for_wire_retry",
        create_autospec(KernelSubAgentPolicy.sleep_for_wire_retry, side_effect=sleep),
    )
    service_trace = create_autospec(RetryBackoffTrace.open, side_effect=RetryBackoffTrace.open)
    monkeypatch.setattr(RetryBackoffTrace, "open", service_trace)
    bus = EventBus()
    tools = SubAgentTools(event_bus=bus, session_id="parent", session_dir=tmp_path, max_transient_retries=0)
    try:
        await tools.register(
            SubAgentRef(profile="Explore", tool_name="Explore"),
            AgentProfile(
                name="Explore",
                instructions="fixed instruction",
                tools=ToolsConfig(builtins=[]),
                compaction=CompactionConfig(enabled=False),
            ),
            SessionEnvironment(cwd=str(tmp_path), platform=get_platform()),
            settings=Settings(),
            fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock", stream=stream),
        )
        async with capture_event_sequence(bus, InvocationRetryAttempt) as events:
            assert await tools.get_tools()[0].func(prompt="fixed input") == "done"
        assert [(event.attempt, event.max_attempts, event.delay_seconds) for event in events] == [
            (1, 2, 13),
            (2, 2, 23),
        ]
        assert sleeps == [13, 23]
        assert [call["stream"] for call in wire.calls] == [stream, stream, stream]
        first = wire.calls[0]["messages"]
        assert all(
            [(m.role, m.text) for m in call["messages"]] == [(m.role, m.text) for m in first] for call in wire.calls
        )
        assert policies[0].max_retries == policies[0].stall_max_retries == 2
        assert controllers[0].policy._attempts.handle is controllers[0].policy._attempt_handle
        assert controllers[0].policy._attempt_handle.task is None
        service_trace.assert_not_called()
    finally:
        await tools.cleanup()


@pytest.mark.parametrize("stream", [False, True])
async def test_real_child_cascade_drains_sleep_result_before_owned_task_cancel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stream: bool
) -> None:
    import asyncio

    from chrys.foundation.events.types import InvocationToolCallResult, UserInterrupt
    from chrys.service.agent_middleware.control import sleep as sleep_module
    from tests.kernel.test_wire_retry import _call_response, _call_update

    # The sleep's own clock, frozen: the interrupted result reports exactly 0 seconds slept.
    freeze_monotonic(monkeypatch, sleep_module)

    provider_entered = asyncio.Event()
    sleep_entered = asyncio.Event()
    result_entered = asyncio.Event()
    release_result = asyncio.Event()
    cancelled = asyncio.Event()

    async def waiting_response():
        provider_entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    # The second provider call can never win before cascade finishes.
    def waiting_stream():
        from chrys.kernel import ChatResponse, ResponseStream

        async def updates():
            await waiting_response()
            yield _text_update("unreachable")

        return ResponseStream(updates(), finalizer=ChatResponse.from_updates)

    first = (
        [_call_update("sleep-call", "sleep", seconds=30, reason="hold")]
        if stream
        else _call_response("sleep-call", "sleep", seconds=30, reason="hold")
    )
    wire = _ScriptedWire([first, waiting_stream if stream else waiting_response])
    client = MockChatClient()
    monkeypatch.setattr(
        client, "_inner_get_response", create_autospec(client._inner_get_response, side_effect=wire.get_response)
    )
    monkeypatch.setattr(tools_module, "create_client", create_autospec(tools_module.create_client, return_value=client))
    bus = EventBus()
    original_subscribe = bus.subscribe

    async def subscribe(event_type, handler):
        await original_subscribe(event_type, handler)
        if event_type is UserInterrupt:
            sleep_entered.set()

    monkeypatch.setattr(bus, "subscribe", create_autospec(original_subscribe, side_effect=subscribe))
    results: list[InvocationToolCallResult] = []

    async def hold_result(event: InvocationToolCallResult) -> None:
        results.append(event)
        result_entered.set()
        await release_result.wait()

    await bus.subscribe(InvocationToolCallResult, hold_result)
    tools = SubAgentTools(event_bus=bus, session_id="parent", session_dir=tmp_path, max_transient_retries=0)
    task = None
    cascade = None
    try:
        await tools.register(
            SubAgentRef(profile="Explore", tool_name="Explore"),
            AgentProfile(
                name="Explore",
                tools=ToolsConfig(builtins=["sleep"]),
                compaction=CompactionConfig(enabled=False),
            ),
            SessionEnvironment(cwd=str(tmp_path), platform=get_platform()),
            settings=Settings(),
            fallback_profile=ModelProfile(id="mock", name="mock", provider="mock", model_id="mock", stream=stream),
        )
        task = asyncio.create_task(tools.get_tools()[0].func(prompt="sleep"))
        await sleep_entered.wait()
        controller = next(iter(tools._controllers.values()))
        handle = controller.policy._attempt_handle
        owned = handle.task
        assert owned is not None
        assert controller.policy._attempts.handle is handle
        cascade = asyncio.create_task(controller.cascade_abort())
        await result_entered.wait()
        assert not cascade.done()
        assert owned.cancelling() == 0
        assert not provider_entered.is_set()
        assert results[0].result == "Sleep interrupted after 0 seconds (requested 30 seconds)."
        assert results[0].metadata["sleep_interrupted"] is True
        release_result.set()
        await cascade
        with pytest.raises(asyncio.CancelledError):
            await task
        assert owned.done()
        assert handle.task is None
        assert not provider_entered.is_set() or cancelled.is_set()
    finally:
        release_result.set()
        for pending in (cascade, task):
            if pending is not None:
                if not pending.done():
                    pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        await bus.unsubscribe(InvocationToolCallResult, hold_result)
        await tools.cleanup()
