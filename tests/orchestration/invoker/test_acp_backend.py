# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP backend ownership with the real managed client and stub subprocess."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.kernel import Message
from chrys.orchestration.invoker.acp import AcpConversation
from chrys.orchestration.invoker.acp_protocol import AcpUpdateTranslator
from chrys.orchestration.invoker.contracts import AbortCause, Aborted, Failed, Ok, PreparedClosed, RunIntent, RunRequest
from chrys.service.acp_client import AcpAgentClient
from chrys.service.approval.policy import ApprovalMode
from tests.orchestration.sub_agents._acp_fakes import make_broker
from tests.service.acp_client.helpers import CallbackRecorder, make_spec
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, with_wait_deadline


async def test_direct_backend_has_no_caller_binding_and_owns_monotonic_transport(tmp_path: Path) -> None:
    bus = EventBus()
    origin = InvocationOrigin("sub_agent", "parent", "child", None)
    verdicts = []
    backend = AcpConversation(
        origin=origin,
        tool_name="external",
        agent_name="External",
        prompt="work",
        event_bus=bus,
        broker=make_broker(bus, [ApprovalMode.BYPASS]),
        spec_factory=lambda ordinal: make_spec(
            tmp_path, scenario="retry_reused_tool_usage", extra_env={"CHRYS_ACP_STUB_FLAG_FILE": str(tmp_path / "wire")}
        ),
        terminal_projection=verdicts.append,
        pass_started=lambda: None,
    )
    try:
        request = RunRequest([Message("user", ["work"])], RunIntent.FRESH, origin)
        first = await backend.run(request)
        assert isinstance(first, Failed)
        assert first.continuation is not None
        second = await backend.run(RunRequest(request.messages, RunIntent.RETRY, origin, first.continuation))
        assert isinstance(second, Ok)
        assert backend.transport_ordinal == 2
        assert first.handle.invocation_id == second.handle.invocation_id == origin.invocation_id
        assert first.handle.pass_id != second.handle.pass_id
        assert first.usage.total_tokens == 21
        assert second.usage.total_tokens == 8
        assert len(verdicts) == 1
        assert verdicts[0].succeeded is True
        assert backend.export_audit()["transport_ordinal"] == 2
        assert "chrys_history" not in backend.export_audit()
    finally:
        await backend.aclose()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_direct_backend_close_drains_active_managed_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_for_child_window
) -> None:
    bus = EventBus()
    origin = InvocationOrigin("sub_agent", "parent", "child", None)
    entered = asyncio.Event()
    original_prompt = AcpAgentClient.prompt

    async def prompt(client, text):
        entered.set()
        return await original_prompt(client, text)

    monkeypatch.setattr(AcpAgentClient, "prompt", prompt)
    backend = AcpConversation(
        origin=origin,
        tool_name="external",
        agent_name="External",
        prompt="work",
        event_bus=bus,
        broker=make_broker(bus, [ApprovalMode.BYPASS]),
        spec_factory=lambda ordinal: make_spec(tmp_path, scenario="idle_stall"),
        terminal_projection=lambda verdict: None,
        pass_started=lambda: None,
    )
    request = RunRequest([Message("user", ["work"])], RunIntent.FRESH, origin)
    running = asyncio.create_task(backend.run(request))
    try:
        await wait_for_child_window(running, entered, "prompt sent to the ACP stub")
        await asyncio.wait_for(asyncio.gather(backend.aclose(), backend.aclose()), 10)
        outcome = await running
        assert isinstance(outcome, Aborted)
        assert outcome.cause is AbortCause.OWNER_CLOSE
        assert outcome.effects.external_stateful is True
        assert backend.active_handle is None
        assert backend._active_client is None
        assert backend.transport_ordinal == 1
        with pytest.raises(PreparedClosed):
            await backend.run(request)
        assert backend.transport_ordinal == 1
    finally:
        await backend.aclose()
        await asyncio.gather(running, return_exceptions=True)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_direct_backend_close_does_not_join_caller_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wait_for_child_window
) -> None:
    bus = EventBus()
    origin = InvocationOrigin("sub_agent", "parent", "child", None)
    entered = asyncio.Event()
    original_prompt = AcpAgentClient.prompt

    async def prompt(client: AcpAgentClient, text: str):
        entered.set()
        return await original_prompt(client, text)

    monkeypatch.setattr(AcpAgentClient, "prompt", create_autospec(original_prompt, side_effect=prompt))
    broker = make_broker(bus, [ApprovalMode.BYPASS])
    close_broker = create_autospec(broker.close, side_effect=broker.close)
    monkeypatch.setattr(broker, "close", close_broker)
    backend = AcpConversation(
        origin=origin,
        tool_name="external",
        agent_name="External",
        prompt="work",
        event_bus=bus,
        broker=broker,
        spec_factory=lambda ordinal: make_spec(tmp_path, scenario="idle_stall"),
        terminal_projection=lambda verdict: None,
        pass_started=lambda: None,
    )
    request = RunRequest([Message("user", ["work"])], RunIntent.FRESH, origin)

    async def caller():
        try:
            return await backend.run(request)
        finally:
            await backend.aclose()

    running = asyncio.create_task(caller())
    closing = None
    try:
        await wait_for_child_window(running, entered, "prompt sent to the ACP stub")
        closing = asyncio.create_task(backend.aclose())
        # Joining the caller here would deadlock its finally against this close.
        # pytest-timeout also bounds regressions that cannot drain cancellation.
        outcome, _ = await asyncio.wait_for(asyncio.gather(running, closing), 5)
        assert isinstance(outcome, Aborted)
        assert outcome.cause is AbortCause.OWNER_CLOSE
        assert outcome.effects.external_stateful is True
        assert backend.active_handle is None
        assert backend._active_client is None
        close_broker.assert_awaited_once()
    finally:
        await backend.aclose()
        await asyncio.gather(running, *([closing] if closing is not None else []), return_exceptions=True)


@pytest.mark.parametrize("action", ["close", "abort", "cascade"])
async def test_close_during_translator_adoption_never_spawns(
    tmp_path: Path, action: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    bus = EventBus()
    origin = InvocationOrigin("sub_agent", "parent", "child", None)
    entered, release = asyncio.Event(), asyncio.Event()
    wire = tmp_path / "wire.jsonl"
    specs = []

    async def adopt(translator: AcpUpdateTranslator) -> None:
        entered.set()
        await release.wait()

    def spec(ordinal: int):
        specs.append(ordinal)
        return make_spec(
            tmp_path, scenario="retry_reused_tool_usage", extra_env={"CHRYS_ACP_STUB_FLAG_FILE": str(wire)}
        )

    backend = AcpConversation(
        origin=origin,
        tool_name="external",
        agent_name="External",
        prompt="work",
        event_bus=bus,
        broker=make_broker(bus, [ApprovalMode.BYPASS]),
        spec_factory=spec,
        translator_callback=adopt,
        terminal_projection=lambda verdict: None,
        pass_started=lambda: None,
    )
    request = RunRequest([Message("user", ["work"])], RunIntent.FRESH, origin)
    running = asyncio.create_task(backend.run(request))
    closing = None
    latched = asyncio.Event()
    original_latch = backend.latch_abort

    def latch(cause):
        original_latch(cause)
        latched.set()

    monkeypatch.setattr(backend, "latch_abort", create_autospec(original_latch, side_effect=latch))
    cause = AbortCause.CASCADE if action == "cascade" else AbortCause.OWNER_CLOSE
    try:
        await asyncio.wait_for(entered.wait(), 5)
        ordinal = backend.transport_ordinal
        handle = backend.active_handle
        assert handle is not None
        if action == "close":
            # Close must latch while adoption remains suspended.
            closing = asyncio.create_task(backend.aclose())
            await asyncio.wait_for(latched.wait(), 5)
        else:
            await backend.abort(handle, cause)
        release.set()
        outcome = await asyncio.wait_for(running, 5)
        if closing is not None:
            await closing
        assert isinstance(outcome, Aborted)
        assert outcome.cause is cause
        assert backend.transport_ordinal == ordinal == 1
        assert specs == []
        assert not wire.exists()
        assert backend._active_client is None
    finally:
        release.set()
        await backend.aclose()
        await asyncio.gather(running, *([closing] if closing is not None else []), return_exceptions=True)


async def test_abort_rejects_later_pass_without_rewriting_cause(tmp_path: Path) -> None:
    bus = EventBus()
    origin = InvocationOrigin("sub_agent", "parent", "child", None)
    backend = AcpConversation(
        origin=origin,
        tool_name="external",
        agent_name="External",
        prompt="work",
        event_bus=bus,
        broker=make_broker(bus, [ApprovalMode.BYPASS]),
        spec_factory=lambda ordinal: make_spec(tmp_path),
        terminal_projection=lambda verdict: None,
        pass_started=lambda: None,
    )

    async def adopt(translator: AcpUpdateTranslator) -> None:
        handle = backend.active_handle
        assert handle is not None
        await backend.abort(handle, AbortCause.USER_CANCEL)

    backend._translator_callback = adopt
    try:
        request = RunRequest([Message("user", ["work"])], RunIntent.FRESH, origin)
        outcome = await backend.run(request)
        assert isinstance(outcome, Aborted)
        assert outcome.cause is AbortCause.USER_CANCEL
        with pytest.raises(PreparedClosed):
            await backend.run(request)
        assert backend._pass_cause is AbortCause.USER_CANCEL
        assert backend.transport_ordinal == 1
    finally:
        await backend.aclose()


async def test_common_shell_publishes_cascade_after_direct_backend_abort(tmp_path: Path) -> None:
    from chrys.foundation.events.types import InvocationCascadeAborted
    from tests.orchestration.sub_agents._acp_fakes import make_controller
    from tests.support.event_capture import capture_event_sequence

    bus = EventBus()
    shell = make_controller(bus, tmp_path, spec_factory=lambda ordinal: make_spec(tmp_path, scenario="happy"))
    backend = shell.policy.backend

    async def adopted(translator: AcpUpdateTranslator) -> None:
        handle = backend.active_handle
        assert handle is not None
        await backend.abort(handle, AbortCause.CASCADE)

    backend._translator_callback = adopted
    try:
        async with capture_event_sequence(bus, InvocationCascadeAborted) as events:
            with pytest.raises(asyncio.CancelledError):
                await shell.run()
            await shell.finalize_cancellation()
        assert len(events) == 1
        assert events[0].origin == shell.origin
        assert shell.evidence.passes == (shell.outcome.handle.pass_id,)
    finally:
        await backend.aclose()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_child_window_budget_starts_after_the_acp_handshake(tmp_path: Path, wait_for_child_window) -> None:
    client = AcpAgentClient(make_spec(tmp_path), CallbackRecorder())
    entered = asyncio.Event()

    async def start_session() -> None:
        await client.connect()
        await client.open_session()
        entered.set()

    operation = asyncio.create_task(start_session())
    try:
        # No scheduling time is needed after session/new. Starting this clock before spawn fails immediately.
        await wait_for_child_window(operation, entered, "ACP session opened", window_timeout=0)
        await operation
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await client.force_close()
