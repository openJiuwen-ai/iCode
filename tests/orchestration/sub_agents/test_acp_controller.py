# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AcpSubAgentPolicy lifecycle: connect retries, cascade aborts, tool-call flushes, and usage accounting."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Literal

import pytest
from acp.schema import (
    AgentMessageChunk,
    TextContentBlock,
)

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    InvocationAborted,
    InvocationCascadeAborted,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationPaused,
    InvocationResumed,
    InvocationRetryAttempt,
    InvocationToolCallResult,
    InvocationToolCallStart,
)
from chrys.foundation.trajectory.event_types import EventType, RetryMode, RetryReason
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.orchestration.invoker import acp as acp_module
from chrys.orchestration.invoker.acp_protocol import AcpPermissionBroker, drain_acp_task
from chrys.orchestration.invoker.contracts import SubAgentStatus
from chrys.orchestration.sub_agents.acp_policy import AcpSubAgentPolicy
from chrys.service.acp_client import AcpPromptOutcome, AcpPromptUsage
from chrys.service.acp_client.errors import AcpConnectError, AcpRefusalError, AcpTransportError
from chrys.service.agent_middleware.control.approval import ApprovalMiddleware
from chrys.service.approval.policy import ApprovalMode, ApprovalPolicy
from chrys.service.approval.turn_context import TurnContextHolder, TurnContextReader
from chrys.service.profiles.agents.schema import ApprovalConfig
from tests.orchestration.sub_agents._acp_fakes import (
    AcpClientDouble,
    FakeAcpClient,
    LateCloseClient,
    PrePromptClient,
    StreamingAcpClient,
    cancel_before_prompt,
    hanging_client,
    install_client,
    make_controller,
    running_tool_call,
    session_notification,
)
from tests.service.trajectory._fakes import CancelAckSink, FakeSink, make_context
from tests.support.event_capture import capture_events
from tests.support.waiting import wait_for


async def test_connect_retry_cancelled_during_ui_publish_does_not_open_backoff(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    sink = FakeSink()
    boundary = new_analytics_id()
    install_client(monkeypatch, scripts=[{"connect": AcpConnectError("temporary")}])

    async def cancel_publish(_event: InvocationRetryAttempt) -> None:
        raise asyncio.CancelledError

    await bus.subscribe(InvocationRetryAttempt, cancel_publish)
    controller = make_controller(
        bus,
        tmp_path,
        trajectory_context=make_context(sink),
        trajectory_boundary_operation_id=boundary,
    )

    with pytest.raises(asyncio.CancelledError):
        await controller.run()

    assert sink.event_types == []
    assert AcpClientDouble.force_closes == 1
    sink.assert_operations_settled()


async def test_connect_retry_cancelled_during_schedule_ack_does_not_start_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    sink = CancelAckSink(at=1)
    install_client(monkeypatch, scripts=[{"connect": AcpConnectError("temporary")}])
    controller = make_controller(
        bus,
        tmp_path,
        trajectory_context=make_context(sink),
        trajectory_boundary_operation_id=new_analytics_id(),
    )

    with pytest.raises(asyncio.CancelledError):
        await controller.run()

    assert sink.event_types == [EventType.RETRY_SCHEDULED]
    scheduled = sink.only(EventType.RETRY_SCHEDULED)
    assert scheduled.payload["reason_code"] == RetryReason.CONNECTION
    assert scheduled.payload["retry_mode"] == RetryMode.CONNECTION
    assert AcpClientDouble.force_closes == 1


async def test_controller_retries_only_connect_and_accounts_authoritative_usage_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    retries = await capture_events(bus, InvocationRetryAttempt)
    install_client(
        monkeypatch,
        scripts=[
            {"connect": AcpConnectError("temporary")},
            {
                "prompt": AcpPromptOutcome(
                    stop_reason="end_turn",
                    usage=AcpPromptUsage(
                        input_tokens=2,
                        output_tokens=3,
                        total_tokens=11,
                        cached_read_tokens=4,
                    ),
                )
            },
        ],
    )

    async def no_wait(_delay: float) -> None:
        # The failed process must be torn down before entering backoff.
        assert AcpClientDouble.force_closes == 1
        return

    monkeypatch.setattr(acp_module.asyncio, "sleep", no_wait)
    usage_calls: list[tuple[Any, ...]] = []
    controller = make_controller(
        bus,
        tmp_path,
        usage_callback=lambda *args: usage_calls.append(args),
    )
    # The fake sends no chunks, so success correctly becomes empty-output.
    result = await controller.run()
    assert result.startswith("Error:")
    assert len(retries) == 1
    assert AcpClientDouble.force_closes == 2
    assert controller.policy.backend.total_usage_tokens == 11
    assert len(usage_calls) == 1
    assert usage_calls[0][6] == 4
    assert usage_calls[0][-1] == 11


async def test_stateless_open_session_failure_retries_instead_of_pausing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failure classified stateless by open_session stays in the retry window.

    The client deliberately classifies failures that settled before
    session/new was sent as retryable AcpConnectError; the controller must
    honor that instead of pausing on its own connected-flag bookkeeping.
    """
    bus = EventBus()
    retries = await capture_events(bus, InvocationRetryAttempt)
    paused = await capture_events(bus, InvocationPaused)
    install_client(
        monkeypatch,
        scripts=[
            {"open": AcpConnectError("agent exited after initialize")},
            {"prompt": AcpPromptOutcome(stop_reason="end_turn", usage=None)},
        ],
    )

    async def no_wait(_delay: float) -> None:
        return

    monkeypatch.setattr(acp_module.asyncio, "sleep", no_wait)
    controller = make_controller(bus, tmp_path)
    result = await controller.run()
    # The fake sends no chunks, so the successful retry is empty-output.
    assert result.startswith("Error:")
    assert len(retries) == 1
    assert paused == []


async def test_translator_callback_adopts_translator_before_transport_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The audit-trail adoption seam must fire before connect().

    A permission decision recorded during open_session (ACP allows requests
    the moment session/new is announced) must already be part of the durable
    trail when a pre-handshake failure writes the pause audit.
    """
    bus = EventBus()
    order: list[str] = []

    class OrderClient(FakeAcpClient):
        async def connect(self) -> None:
            order.append("connect")
            await super().connect()

    install_client(monkeypatch, OrderClient, scripts=[{"prompt": AcpPromptOutcome(stop_reason="end_turn", usage=None)}])

    async def adopt(translator: Any) -> None:
        order.append("adopt")
        assert translator is not None

    controller = make_controller(
        bus,
        tmp_path,
        translator_callback=adopt,
    )
    await controller.run()
    assert order[:2] == ["adopt", "connect"]


async def test_controller_post_session_transport_pauses_then_abort_never_leaks_diagnostic_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    pauses = await capture_events(bus, InvocationPaused)
    install_client(monkeypatch, scripts=[{"prompt": AcpTransportError("wire closed")}])
    controller = make_controller(bus, tmp_path)
    task = asyncio.create_task(controller.run())
    await wait_for(lambda: bool(pauses), description="ACP pause")
    assert pauses[0].diagnostic_path == str(tmp_path / "stderr.log")
    assert controller.request_abort()
    result = await task
    assert result.startswith("Error:")
    assert str(tmp_path / "stderr.log") not in result


async def test_controller_cascade_abort_during_prompt_is_non_blocking_and_cancels_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    cascade_events = await capture_events(bus, InvocationCascadeAborted)

    HangingClient = hanging_client()

    install_client(monkeypatch, HangingClient)
    controller = make_controller(bus, tmp_path)
    task = asyncio.create_task(controller.run())
    await wait_for(HangingClient.entered.is_set, description="ACP prompt")
    await controller.cascade_abort()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cascade_events


async def test_acp_cascade_commits_parent_before_controller_finalization(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    order: list[str] = []

    HangingClient = hanging_client()

    async def finalize() -> None:
        order.append("finalized")

    install_client(monkeypatch, HangingClient)
    controller = make_controller(
        bus,
        tmp_path,
        parent_interrupted_result_commit=lambda: order.append("parent-committed"),
        cancellation_finalizer=finalize,
    )
    task = asyncio.create_task(controller.run())
    await wait_for(HangingClient.entered.is_set, description="ACP prompt")

    await controller.cascade_abort()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The parent slot commits synchronously at cascade time; the durable log
    # finalization is deferred to the invocation wrapper, which runs it only
    # after ``run()`` has fully unwound (teardown + terminal flush included).
    assert order == ["parent-committed"]

    await controller.finalize_cancellation()
    assert order == ["parent-committed", "finalized"]


async def test_cascade_during_pause_callback_await_does_not_deadlock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cascade that lands while the pause callback is awaiting must not hang run().

    _pause_and_wait installs _pending_decision only AFTER awaiting _pause_callback.
    A cascade during that await finds no future to resolve (its one-shot
    _resolve_decision is a no-op) and no active client to cancel; without the
    post-callback re-check, a fresh unresolved future is then created and awaited
    forever. Reproduce with a gated pause callback.
    """
    bus = EventBus()
    cascade_events = await capture_events(bus, InvocationCascadeAborted)
    pauses = await capture_events(bus, InvocationPaused)
    install_client(monkeypatch, scripts=[{"prompt": AcpTransportError("wire closed")}])

    in_pause_cb = asyncio.Event()
    release_pause_cb = asyncio.Event()

    async def gated_pause_callback() -> None:
        in_pause_cb.set()
        await release_pause_cb.wait()

    controller = make_controller(
        bus,
        tmp_path,
        pause_callback=gated_pause_callback,
    )
    task = asyncio.create_task(controller.run())
    # Inside the pause-callback await, with _pending_decision still None — the
    # exact window where a cascade would otherwise be lost.
    await wait_for(in_pause_cb.is_set, description="pause callback entered")
    await controller.cascade_abort()
    release_pause_cb.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cascade_events
    assert controller.status is SubAgentStatus.CASCADE_ABORTED
    # No pause was announced for an invocation that was already being torn down.
    assert not pauses


async def test_post_session_refusal_flushes_interrupted_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A refusal after the remote streamed a tool call must finalize that call.

    Config/auth/refusal handlers previously skipped translator.flush_interrupted(),
    so a InvocationToolCallStart published before the rejection had no matching
    result — stale running ownership after the parent invocation terminated.
    """
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)

    class RefusingClient(StreamingAcpClient):
        async def prompt(self, _prompt: str) -> AcpPromptOutcome:
            await self._sink.put(1, running_tool_call("running", "Running"))
            raise AcpRefusalError("policy refusal")

    install_client(monkeypatch, RefusingClient)
    controller = make_controller(bus, tmp_path)
    result = await controller.run()

    assert result.startswith("Error:")
    assert controller.status is SubAgentStatus.COMPLETED
    # The started tool call was flushed as interrupted — no dangling running call.
    assert any(r.result == "(interrupted)" for r in results)


async def test_tool_call_started_during_force_close_is_flushed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A tool-call start the still-alive update consumer processes DURING the
    force-close ladder must be finalized by the post-teardown sweep.

    The client cancels the update consumer only at the very end of force_close,
    so a buffered update dispatched to the translator mid-teardown lands AFTER
    the per-handler flush. Without the post-close sweep it leaves a
    InvocationToolCallStart with no result and stale running ownership.
    """
    bus = EventBus()
    starts = await capture_events(bus, InvocationToolCallStart)
    results = await capture_events(bus, InvocationToolCallResult)

    install_client(monkeypatch, LateCloseClient)
    controller = make_controller(bus, tmp_path)
    await controller.run()

    assert any(s.call_id.endswith("late") for s in starts)
    assert any(r.result == "(interrupted)" for r in results)


async def test_completed_count_synced_after_cancel_flush(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cancelling before the prompt flushes the tool call and counts it in completed_tool_calls.

    Every sibling error handler syncs ``self._completed_calls`` after its flush;
    the cancel handler and the post-teardown sweep must too, or a call the cancel
    flush terminalized publishes its terminal event yet stays absent from
    ``completed_tool_calls`` — the value tools.py persists into the audit log.
    """
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)

    install_client(monkeypatch, PrePromptClient)
    controller = make_controller(
        bus,
        tmp_path,
        attempt_callback=cancel_before_prompt,
    )
    with pytest.raises(asyncio.CancelledError):
        await controller.run()

    assert any(r.result == "(interrupted)" for r in results)
    # The flushed call is counted for the audit log, not silently dropped.
    assert controller.policy.backend.completed_tool_calls == 1


async def test_post_close_sweep_is_cancellation_safe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second interrupt landing DURING the post-teardown sweep must not tear
    it. The sweep is shielded like force_close, so a call streamed at close time
    is still finalized even though the run is being cancelled a second time."""
    bus = EventBus()
    run_task: asyncio.Task[Any] | None = None

    async def cancel_during_sweep(event: InvocationToolCallResult) -> None:
        # The late call's result is the sweep's to publish: cancel the run there,
        # mid-sweep, to model a re-interrupt. Subscribed before the capture, so a
        # torn publish never reaches it.
        if event.call_id.endswith("late") and run_task is not None:
            run_task.cancel()
            await asyncio.sleep(0)

    await bus.subscribe(InvocationToolCallResult, cancel_during_sweep)
    results = await capture_events(bus, InvocationToolCallResult)

    install_client(monkeypatch, LateCloseClient)
    controller = make_controller(bus, tmp_path)
    run_task = asyncio.create_task(controller.run())
    with pytest.raises(asyncio.CancelledError):
        await run_task

    # The re-interrupt did not strand the late call: the sweep still finalized it.
    assert any(r.result == "(interrupted)" for r in results)


async def test_cascade_during_abort_publish_wins_over_normal_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cascade_abort landing DURING the InvocationAborted publish must win.

    The user Abort decision passes the pre-branch cascade recheck, then awaits
    InvocationAborted publication; a concurrent cascade sets _cascade_requested in
    that window. Without a post-publish recheck run() returns the abort error
    normally while InvocationCascadeAborted is already published — a contradiction.
    """
    bus = EventBus()
    cascades = await capture_events(bus, InvocationCascadeAborted)
    install_client(monkeypatch, scripts=[{"prompt": AcpTransportError("drain failed", usage=None)}])
    controller = make_controller(bus, tmp_path)

    async def cascade_mid_abort_publish(_event: InvocationAborted) -> None:
        # Runs inline during the InvocationAborted publish (the exact window).
        await controller.cascade_abort()

    await bus.subscribe(InvocationAborted, cascade_mid_abort_publish)

    task = asyncio.create_task(controller.run())
    await wait_for(lambda: controller.status.value == "paused", description="abort-vs-cascade pause")
    assert controller.request_abort()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cascade won: status is CASCADE_ABORTED and its terminal event fired.
    assert controller.status is SubAgentStatus.CASCADE_ABORTED
    assert len(cascades) == 1


async def test_drain_task_cancellation_safe_survives_repeated_awaiter_cancel() -> None:
    """The shield-LOOP the cascade-publish / teardown / flush paths funnel through
    must not abandon its task when the AWAITER is cancelled more than once.

    ``asyncio.shield`` only protects the shielded task from an external cancel —
    the awaiter still receives ``CancelledError``. A single ``await
    asyncio.shield(task)`` therefore returns (cancelled) the instant a second
    interrupt / shutdown lands, abandoning e.g. the in-flight
    InvocationCascadeAborted publish. The loop re-awaits until the task genuinely
    completes, then re-raises ``CancelledError``.
    """
    released = asyncio.Event()

    async def slow() -> str:
        await released.wait()
        return "done"

    inner = asyncio.create_task(slow())
    drain = asyncio.create_task(drain_acp_task(inner))
    await asyncio.sleep(0)  # let drain reach `await asyncio.shield(inner)`

    for _ in range(2):  # repeated interrupt / shutdown cancels of the awaiter
        drain.cancel()
        await asyncio.sleep(0)
        # A naive single-shield drain would already be done (cancelled) here,
        # having abandoned `inner`; the loop keeps it waiting.
        assert not drain.done()
        assert not inner.done()

    released.set()
    with pytest.raises(asyncio.CancelledError):
        await drain
    # The shielded task ran to completion despite the repeated cancels.
    assert inner.done() and inner.result() == "done"


async def test_transport_usage_survives_drain_failure_and_is_charged_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    bus = EventBus()
    usage = AcpPromptUsage(input_tokens=8, output_tokens=13, total_tokens=25, cached_read_tokens=3)
    install_client(monkeypatch, scripts=[{"prompt": AcpTransportError("drain failed", usage=usage)}])
    calls: list[tuple[Any, ...]] = []
    controller = make_controller(
        bus,
        tmp_path,
        usage_callback=lambda *args: calls.append(args),
    )
    task = asyncio.create_task(controller.run())
    await wait_for(lambda: controller.status.value == "paused", description="usage-bearing transport pause")
    assert controller.request_abort()
    await task
    assert controller.policy.backend.total_usage_tokens == 25
    assert controller.policy.backend.usage_unreported_attempts == 0
    assert len(calls) == 1
    assert calls[0][6] == 3
    assert calls[0][-1] == 25


def test_turn_context_consumers_share_one_protocol_compliant_holder() -> None:
    holder = TurnContextHolder()
    bus = EventBus()
    middleware = ApprovalMiddleware(
        approval_policy=ApprovalPolicy(ApprovalConfig(), tools=[]),
        event_bus=bus,
        turn_context=holder,
    )
    broker = AcpPermissionBroker(
        event_bus=bus,
        session_id=None,
        caller_name="External",
        mode_getter=lambda: ApprovalMode.MANUAL,
        turn_context=holder,
        workspace_roots=[],
        workspace_cwd="",
        approval_judge=None,
        ask_user_timeout_seconds=None,
    )
    assert isinstance(holder, TurnContextReader)
    assert middleware._turn_context is holder
    assert broker._turn_context is holder


async def test_cascade_abort_wins_over_remote_end_turn_race(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A remote finishing before our cancel lands must not turn an interrupt into success."""
    bus = EventBus()
    aborted = await capture_events(bus, InvocationCascadeAborted)
    controller_box: dict[str, AcpSubAgentPolicy] = {}

    class _RacingClient(FakeAcpClient):
        async def prompt(self, _prompt: str) -> AcpPromptOutcome:
            await controller_box["controller"].cascade_abort()
            return AcpPromptOutcome(stop_reason="end_turn", usage=None)

    install_client(monkeypatch, _RacingClient, scripts=[{}])
    controller = make_controller(bus, tmp_path)
    controller_box["controller"] = controller

    with pytest.raises(asyncio.CancelledError):
        await controller.run()

    assert len(aborted) == 1


async def test_cascade_during_terminal_force_close_overrides_completed_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cascade_abort landing during a terminal handler's finally force-close
    (a window r8's flush/close awaits widened) must win over the COMPLETED
    result the handler already computed: the in-prompt guard sees a clean flag,
    so only run()'s re-check before `return result` can honor the cascade."""
    bus = EventBus()
    aborted = await capture_events(bus, InvocationCascadeAborted)
    controller_box: dict[str, AcpSubAgentPolicy] = {}

    class _CascadeOnCloseClient(FakeAcpClient):
        async def prompt(self, _prompt: str) -> AcpPromptOutcome:
            # No cascade yet — the in-prompt guard passes and the success path
            # computes a terminal (empty-output) result.
            return AcpPromptOutcome(stop_reason="end_turn", usage=None)

        async def force_close(self) -> None:
            # Cascade arrives only during teardown, AFTER the result exists.
            await controller_box["controller"].cascade_abort()
            await super().force_close()

    install_client(monkeypatch, _CascadeOnCloseClient, scripts=[{}])
    controller = make_controller(bus, tmp_path)
    controller_box["controller"] = controller

    with pytest.raises(asyncio.CancelledError):
        await controller.run()

    assert controller.status is SubAgentStatus.CASCADE_ABORTED
    assert len(aborted) == 1


async def test_cascade_after_resolved_pause_decision_overrides_abort(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cascade_abort that lands AFTER the user's Abort already resolved the
    pause decision must still win. The settled `_pending_decision` future makes
    cascade's one-shot `_resolve_decision` a no-op, so `decision` stays "abort";
    run() must re-check `_cascade_requested` before acting on it, else it
    publishes a contradictory InvocationAborted and returns normally instead of
    cascade-cancelling."""
    bus = EventBus()
    aborted = await capture_events(bus, InvocationAborted)
    resumed = await capture_events(bus, InvocationResumed)
    cascaded = await capture_events(bus, InvocationCascadeAborted)
    controller_box: dict[str, AcpSubAgentPolicy] = {}

    async def on_paused(_event: InvocationPaused) -> None:
        # This runs inline during _pause_and_wait's InvocationPaused publish,
        # BEFORE it awaits the decision future: resolve to "abort" first, then
        # cascade (its resolve no-ops the settled future) — exactly the race.
        controller = controller_box["controller"]
        assert controller.request_abort()
        await controller.cascade_abort()

    await bus.subscribe(InvocationPaused, on_paused)

    install_client(monkeypatch, scripts=[{"prompt": AcpTransportError("wire closed")}])
    controller = make_controller(bus, tmp_path)
    controller_box["controller"] = controller

    with pytest.raises(asyncio.CancelledError):
        await controller.run()

    assert controller.status is SubAgentStatus.CASCADE_ABORTED
    # The stale abort/retry decision must NOT surface a lifecycle event.
    assert aborted == []
    assert resumed == []
    assert len(cascaded) == 1


@pytest.mark.parametrize("result_mode", ["last_segment", "transcript"])
async def test_controller_reports_no_unpublished_final_after_terminal_tool_for_every_result_mode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    result_mode: Literal["last_segment", "transcript"],
) -> None:
    from acp.schema import ToolCallStart

    bus = EventBus()
    deltas = await capture_events(bus, InvocationContentDelta)
    finished = await capture_events(bus, InvocationContentFinished)
    messages = await capture_events(bus, InvocationMessage)

    class TextThenToolClient(StreamingAcpClient):
        async def prompt(self, _prompt: str) -> AcpPromptOutcome:
            await self._sink.put(
                1,
                session_notification(
                    AgentMessageChunk(
                        sessionUpdate="agent_message_chunk",
                        messageId="answer",
                        content=TextContentBlock(type="text", text="answer before tool"),
                    )
                ),
            )
            await self._sink.put(
                2,
                session_notification(
                    ToolCallStart(
                        sessionUpdate="tool_call",
                        toolCallId="terminal",
                        title="Read",
                        kind="read",
                        status="completed",
                    )
                ),
            )
            return AcpPromptOutcome(stop_reason="end_turn", usage=None)

    install_client(monkeypatch, TextThenToolClient)
    controller = make_controller(
        bus,
        tmp_path,
        result_mode=result_mode,
    )

    assert await controller.run() == "answer before tool"
    # The answer streamed once and its segment ended at the tool call; nothing sends it again.
    assert [(delta.origin.kind, delta.channel, delta.text) for delta in deltas] == [
        ("sub_agent", "answer", "answer before tool")
    ]
    assert [(event.segment_id, event.status) for event in finished] == [(deltas[0].segment_id, "completed")]
    assert messages == []
    assert controller.policy.backend.transcript_final_text == ""
