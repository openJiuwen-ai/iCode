# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the pending-retry mechanism.

When a user interrupts a running agent and clicks Resume before the LLM call
returns, the retry must be queued and executed once the current run finishes.
Without this, the TUI shows the agent as running but the backend silently
drops the retry, leaving the UI stuck in "Thinking" forever.

Verifies:
1. Retry while running → queued, then auto-executed after run completes
2. Multiple retry clicks while running → only one retry fires
3. A new user message clears the pending retry
4. Retry when idle → works normally (regression)
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserMessage, UserRetry
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.execution import PendingRetry, RetryInput
from chrys.orchestration.engine.state.machine import EngineState, Trigger
from chrys.service.llm.mock import MockResponse
from tests.support.pipeline_helpers import (
    create_test_engine,
    extract_final_messages,
    wait_for_idle,
)
from tests.support.turn_services import make_turn_retry
from tests.support.waiting import await_run_task_chain

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def check_pending_retry(engine: AgentEngine, *, engine_services) -> None:
    make_turn_retry(
        current=engine.current,
        session=engine.session,
        turn_state=engine.turns.turn_state,
        permits=engine.permits,
        loader=engine.loader,
        bus=engine.event_bus,
        fsm=engine_services(engine).fsm,
        history=engine_services(engine).history,
        trajectory_recorder=engine_services(engine).trajectory_recorder,
    ).start_pending_retry_if_due()


async def _wait_pending_done(ctx, timeout: float = 15.0) -> None:
    """Wait until the pending-retry chain has fully drained.

    The budget covers two full runs (the interrupted original and its retry),
    each with a session save behind it; loaded Windows workers have needed
    well over five seconds for that.

    Waiting on (``executor.state.running == False`` AND FSM != PENDING_RETRY) alone
    races the engine: ``_post_run`` flips PENDING_RETRY → RUNNING via
    ``RUN_INTERRUPTED``, then ``await _save_current_session()`` yields the loop
    before pending-retry dispatch spawns the new retry task.  Both
    conditions are momentarily true while the retry hasn't been queued yet —
    a fixed settle is unreliable on slow CI workers (notably Windows).

    Drain the run task (``_turn_state.lease.run_task``) instead: pending-retry
    dispatch overwrites it with the retry task, so awaiting the latest task and
    re-sampling after a small settle catches every chained run.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        task = ctx.engine.turns.turn_state.lease.run_task
        if task is not None and not task.done():
            remaining = deadline - asyncio.get_running_loop().time()
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), timeout=max(0.01, remaining))
            # That task may have spawned a retry task in its _post_run — re-check.
            continue
        # No task running; let any synchronously-queued task get a chance to
        # appear in ``_turn_state.lease.run_task`` before declaring the chain drained.
        await asyncio.sleep(0.05)
        if ctx.engine.turns.turn_state.lease.run_task is task:
            executor = ctx.engine.current.loaded.bindings
            if executor is not None and not executor.state.running and ctx.engine.state != EngineState.PENDING_RETRY:
                return
    raise TimeoutError("Engine did not finish pending retry within timeout")


async def _wait_llm_call_started(ctx, expected_count: int = 1, timeout: float = 2.0) -> None:
    """Wait until the mock client has begun ``expected_count`` LLM call(s).

    Fixed sleeps race on slow CI workers: the executor can take more than
    30 ms to dispatch the LLM call.  If we ``set_interrupted()`` before
    the call enters ``_inner_get_response``, the interrupt check short-
    circuits it, ``call_count`` never increments, and the test mis-counts
    the original call.  Gate on ``call_count`` instead.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if ctx.mock_client.call_count >= expected_count:
            return
        await asyncio.sleep(0.01)
    raise TimeoutError(f"LLM call {expected_count} did not start within {timeout}s")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wait_for_run_task_observes_retry_task_replacement() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    original_entered = asyncio.Event()
    allow_retry_replacement = asyncio.Event()
    retry_started = asyncio.Event()
    retry_release = asyncio.Event()

    async def retry_task() -> None:
        retry_started.set()
        await retry_release.wait()

    async def original_task() -> None:
        original_entered.set()
        await allow_retry_replacement.wait()
        engine.turns.turn_state.lease.run_task = asyncio.create_task(retry_task())

    engine.turns.turn_state.lease.run_task = asyncio.create_task(original_task())
    await asyncio.wait_for(original_entered.wait(), timeout=5.0)
    waiter = asyncio.create_task(engine.wait_for_run_task())
    try:
        await asyncio.sleep(0)
        allow_retry_replacement.set()
        await asyncio.wait_for(retry_started.wait(), timeout=5.0)
        await asyncio.sleep(0)
        assert not waiter.done()
    finally:
        retry_release.set()
        await asyncio.gather(waiter, return_exceptions=True)


@pytest.mark.asyncio
async def test_wait_for_run_task_observes_post_run_pending_retry_task(tmp_path: Path) -> None:
    responses = [
        MockResponse(text="First response that will be interrupted"),
        MockResponse(text="Retry completed successfully"),
    ]
    ctx = await create_test_engine(responses, tmp_path)

    release_first_call = asyncio.Event()
    first_call_entered = asyncio.Event()
    release_retry_call = asyncio.Event()
    retry_call_entered = asyncio.Event()
    original_inner = ctx.mock_client._inner_get_response
    call_number = 0

    def _gate_first_and_retry_call(*, messages, stream, options, **kwargs):
        nonlocal call_number
        call_number += 1
        if call_number == 1:

            async def _await_first_release():
                first_call_entered.set()
                await release_first_call.wait()
                return await original_inner(messages=messages, stream=stream, options=options, **kwargs)

            return _await_first_release()
        if call_number == 2:

            async def _await_retry_release():
                retry_call_entered.set()
                await release_retry_call.wait()
                return await original_inner(messages=messages, stream=stream, options=options, **kwargs)

            return _await_retry_release()
        return original_inner(messages=messages, stream=stream, options=options, **kwargs)

    ctx.mock_client._inner_get_response = _gate_first_and_retry_call

    waiter: asyncio.Task[None] | None = None
    try:
        await ctx.bus.publish(UserMessage(text="Start something"))
        await asyncio.wait_for(first_call_entered.wait(), timeout=5.0)
        ctx.engine.current.loaded.bindings._interrupt.set_interrupted()
        await ctx.bus.publish(UserRetry())

        waiter = asyncio.create_task(ctx.engine.wait_for_run_task())
        release_first_call.set()
        await asyncio.wait_for(retry_call_entered.wait(), timeout=5.0)
        await asyncio.sleep(0)

        assert not waiter.done()

        release_retry_call.set()
        await asyncio.wait_for(waiter, timeout=5.0)

        assert ctx.engine.state != EngineState.PENDING_RETRY
        assert ctx.mock_client.call_count == 2
    finally:
        release_first_call.set()
        release_retry_call.set()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        ctx.mock_client._inner_get_response = original_inner
        await ctx.cleanup()


@pytest.mark.asyncio
async def test_wait_for_run_task_observes_already_done_successful_task() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())

    async def successful_task() -> None:
        return None

    task = asyncio.create_task(successful_task())
    engine.turns.turn_state.lease.run_task = task
    await task

    await asyncio.wait_for(engine.wait_for_run_task(), timeout=5.0)

    assert engine.turns.turn_state.lease.run_task is task


@pytest.mark.asyncio
async def test_wait_for_run_task_propagates_already_done_failed_task() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    original_error = RuntimeError("run failed")

    async def failed_task() -> None:
        raise original_error

    task = asyncio.create_task(failed_task())
    engine.turns.turn_state.lease.run_task = task
    await asyncio.sleep(0)
    assert task.done()

    with pytest.raises(RuntimeError) as exc_info:
        await engine.wait_for_run_task()

    assert exc_info.value is original_error


@pytest.mark.asyncio
async def test_wait_for_run_task_propagates_cancelled_run_task() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())

    async def waiting_task() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(waiting_task())
    engine.turns.turn_state.lease.run_task = task
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await engine.wait_for_run_task()


@pytest.mark.asyncio
async def test_boundary_drain_reports_inner_run_task_cancellation() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())

    async def waiting_task() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(waiting_task())
    engine.turns.turn_state.lease.run_task = task
    await asyncio.sleep(0)
    task.cancel()

    outcome = await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert outcome.cancelled is True
    assert task.cancelled()


@pytest.mark.asyncio
async def test_boundary_drain_caller_cancellation_does_not_cancel_run_task() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    task_started = asyncio.Event()
    release_task = asyncio.Event()

    async def waiting_task() -> None:
        task_started.set()
        await release_task.wait()

    task = asyncio.create_task(waiting_task())
    engine.turns.turn_state.lease.run_task = task
    await asyncio.wait_for(task_started.wait(), timeout=5.0)

    waiter = asyncio.create_task(engine.turns.turn_state.drain_for_boundary())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert not task.cancelled()
    assert not task.done()

    release_task.set()
    await asyncio.wait_for(task, timeout=5.0)


@pytest.mark.asyncio
async def test_pending_retry_dispatch_disabled_prevents_auto_retry(
    monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine_services(engine).fsm.transition(Trigger.START)
    engine_services(engine).fsm.transition(Trigger.USER_MESSAGE)
    engine.turns.turn_state.lease.pending_retry = PendingRetry(
        retry=RetryInput("old retry", "created"), session_generation=engine.session_generation
    )
    engine.turns.turn_state.lease.disable_pending_retry_dispatch_for_session_transition(engine.session_generation)
    calls: list[str] = []

    async def fake_retry(retry: RetryInput, **_kwargs: object) -> None:
        calls.append(retry.text)

    monkeypatch.setattr(engine.turns, "retry_and_save", fake_retry)

    check_pending_retry(engine, engine_services=engine_services)
    await asyncio.sleep(0)

    assert engine.turns.turn_state.lease.run_task is None
    assert calls == []
    assert engine.turns.turn_state.lease.pending_retry.retry.text == ""


@pytest.mark.asyncio
async def test_pending_retry_shutdown_prevents_auto_retry(monkeypatch: pytest.MonkeyPatch, *, engine_services) -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    engine_services(engine).fsm.transition(Trigger.START)
    engine_services(engine).fsm.transition(Trigger.USER_MESSAGE)
    engine.turns.turn_state.lease.pending_retry = PendingRetry(
        retry=RetryInput("old retry", "created"), session_generation=engine.session_generation
    )
    engine.session.shutting_down = True
    calls: list[str] = []

    async def fake_retry(retry: RetryInput, **_kwargs: object) -> None:
        calls.append(retry.text)

    monkeypatch.setattr(engine.turns, "retry_and_save", fake_retry)

    check_pending_retry(engine, engine_services=engine_services)
    await asyncio.sleep(0)

    assert engine.turns.turn_state.lease.run_task is None
    assert calls == []
    assert engine.turns.turn_state.lease.pending_retry.retry.text == ""


class TestPendingRetry:
    """Test the _pending_retry queue mechanism."""

    @pytest.mark.asyncio
    async def test_retry_while_running_queues_and_completes(self, tmp_path: Path):
        """Retry while executor is running: queued, then auto-fires after run.

        Flow:
        1. Send message; first LLM call is gated on ``release_first_call``
        2. Set interrupt flag while the call is held
        3. Publish UserRetry while is_running=True → state == PENDING_RETRY
        4. Release the gated call; pending-retry dispatch fires → retry executes
        5. Final AgentMessage published with retry's response

        Why gated instead of ``delay=N``: a fixed delay races
        ``on_user_retry``'s ``await _wait_for_agent_load_idle()`` yield on
        slow CI workers (notably Windows) — the original run can complete
        during that yield, dropping us into the "retry when idle" branch
        and skipping ``PENDING_RETRY`` entirely.
        """
        responses = [
            MockResponse(text="First response that will be interrupted"),
            MockResponse(text="Retry completed successfully"),
        ]
        ctx = await create_test_engine(responses, tmp_path)

        release_first_call = asyncio.Event()
        first_call_entered = asyncio.Event()
        original_inner = ctx.mock_client._inner_get_response
        first_call_gated = False

        def _gate_first_call(*, messages, stream, options, **kwargs):
            nonlocal first_call_gated
            if not first_call_gated:
                first_call_gated = True

                async def _await_release():
                    first_call_entered.set()
                    await release_first_call.wait()
                    return await original_inner(messages=messages, stream=stream, options=options, **kwargs)

                return _await_release()
            return original_inner(messages=messages, stream=stream, options=options, **kwargs)

        ctx.mock_client._inner_get_response = _gate_first_call

        try:
            # Send message — first LLM call blocks until release_first_call is set
            await ctx.bus.publish(UserMessage(text="Start something"))
            await asyncio.wait_for(first_call_entered.wait(), timeout=5.0)
            assert ctx.engine.current.loaded.bindings.state.running, "TurnBindings should be running (gated LLM call)"

            # Set interrupt flag (simulates user pressing Stop)
            ctx.engine.current.loaded.bindings._interrupt.set_interrupted()

            # Publish retry while first call is held — must be queued.
            # The gated call guarantees the original run cannot complete
            # during ``on_user_retry``'s internal yield.
            await ctx.bus.publish(UserRetry())
            assert ctx.engine.state == EngineState.PENDING_RETRY, "Retry should be queued when executor is running"

            # Release the held call and wait for the retry chain to drain
            release_first_call.set()
            await _wait_pending_done(ctx)

            assert ctx.engine.state != EngineState.PENDING_RETRY, "Pending retry should have been consumed"
            assert ctx.mock_client.call_count == 2, (
                f"Expected 2 LLM calls (original + retry), got {ctx.mock_client.call_count}"
            )

            finals = extract_final_messages(ctx.events)
            assert any("Retry completed" in t for t in finals), f"Expected retry response in final messages: {finals}"

        finally:
            release_first_call.set()
            ctx.mock_client._inner_get_response = original_inner
            await ctx.cleanup()

    @pytest.mark.asyncio
    async def test_retry_while_running_no_duplicate(self, tmp_path: Path):
        """Multiple retry clicks while running → only one retry fires."""
        responses = [
            MockResponse(text="First"),
            MockResponse(text="Single retry response"),
        ]
        ctx = await create_test_engine(responses, tmp_path)

        release_first_call = asyncio.Event()
        first_call_entered = asyncio.Event()
        original_inner = ctx.mock_client._inner_get_response
        first_call_gated = False

        def _gate_first_call(*, messages, stream, options, **kwargs):
            nonlocal first_call_gated
            if not first_call_gated:
                first_call_gated = True

                async def _await_release():
                    first_call_entered.set()
                    await release_first_call.wait()
                    return await original_inner(messages=messages, stream=stream, options=options, **kwargs)

                return _await_release()
            return original_inner(messages=messages, stream=stream, options=options, **kwargs)

        ctx.mock_client._inner_get_response = _gate_first_call

        try:
            await ctx.bus.publish(UserMessage(text="Go"))
            await asyncio.wait_for(first_call_entered.wait(), timeout=5.0)

            ctx.engine.current.loaded.bindings._interrupt.set_interrupted()

            # Send multiple retries while the first call is held
            await ctx.bus.publish(UserRetry())
            await ctx.bus.publish(UserRetry())
            await ctx.bus.publish(UserRetry())

            assert ctx.engine.state == EngineState.PENDING_RETRY, "Should be queued"

            release_first_call.set()
            await _wait_pending_done(ctx)

            # Only 2 LLM calls total (original + 1 retry, not 3 retries)
            assert ctx.mock_client.call_count == 2, f"Expected exactly 2 LLM calls, got {ctx.mock_client.call_count}"

        finally:
            release_first_call.set()
            ctx.mock_client._inner_get_response = original_inner
            await ctx.cleanup()

    @pytest.mark.asyncio
    async def test_pending_retry_is_cleared_when_original_run_fails(self, tmp_path: Path):
        """A queued retry should not auto-run after the in-flight run fails."""
        responses = [MockResponse(text="Slow first"), MockResponse(text="Should not run")]
        ctx = await create_test_engine(responses, tmp_path)

        release_first_call = asyncio.Event()
        first_call_entered = asyncio.Event()
        original_inner = ctx.mock_client._inner_get_response

        def _fail_first(*, messages, stream, options, **kwargs):
            if ctx.mock_client.call_count == 0:
                ctx.mock_client._call_index += 1

                async def _throw():
                    first_call_entered.set()
                    await release_first_call.wait()
                    raise RuntimeError("simulated failure")

                return _throw()
            return original_inner(messages=messages, stream=stream, options=options, **kwargs)

        ctx.mock_client._inner_get_response = _fail_first

        try:
            await ctx.bus.publish(UserMessage(text="Start something"))
            await asyncio.wait_for(first_call_entered.wait(), timeout=5.0)
            assert ctx.engine.current.loaded.bindings.state.running

            await ctx.bus.publish(UserRetry(text="queued retry"))
            assert ctx.engine.state == EngineState.PENDING_RETRY

            release_first_call.set()
            await _wait_pending_done(ctx)

            assert ctx.engine.state is EngineState.FAILED
            assert ctx.engine.turns.turn_state.lease.pending_retry.retry.text == ""
            assert ctx.engine.turns.turn_state.lease.pending_retry == PendingRetry()
            assert ctx.mock_client.call_count == 1
        finally:
            release_first_call.set()
            ctx.mock_client._inner_get_response = original_inner
            await ctx.cleanup()

    @pytest.mark.asyncio
    async def test_pending_retry_cleared_by_new_message(self, tmp_path: Path):
        """A new user message supersedes the pending retry."""
        responses = [
            MockResponse(text="Slow first"),
            # The new user message's run gets this
            MockResponse(text="Response to new message"),
        ]
        ctx = await create_test_engine(responses, tmp_path)
        first_call_entered = asyncio.Event()
        release_first_call = asyncio.Event()
        original_inner = ctx.mock_client._inner_get_response
        first_call_blocked = False

        def _gate_first_call(*, messages, stream, options, **kwargs):
            nonlocal first_call_blocked
            if not first_call_blocked:
                first_call_blocked = True

                async def _await_release():
                    first_call_entered.set()
                    await release_first_call.wait()
                    return await original_inner(messages=messages, stream=stream, options=options, **kwargs)

                return _await_release()
            return original_inner(messages=messages, stream=stream, options=options, **kwargs)

        ctx.mock_client._inner_get_response = _gate_first_call

        try:
            await ctx.bus.publish(UserMessage(text="First"))
            await asyncio.wait_for(first_call_entered.wait(), timeout=5.0)

            ctx.engine.current.loaded.bindings._interrupt.set_interrupted()

            # Queue a retry
            await ctx.bus.publish(UserRetry())
            assert ctx.engine.state == EngineState.PENDING_RETRY

            # Wait for the interrupted run to finish
            release_first_call.set()
            await _wait_pending_done(ctx)

            # Now send a new user message — should clear pending retry
            ctx.events.clear()
            await ctx.bus.publish(UserMessage(text="New message instead"))
            await wait_for_idle(ctx)

            assert ctx.engine.state != EngineState.PENDING_RETRY, "New message should clear pending retry"

            # The new message should get a response
            finals = extract_final_messages(ctx.events)
            assert len(finals) >= 1, f"Expected final message for new message, got {finals}"

        finally:
            release_first_call.set()
            ctx.mock_client._inner_get_response = original_inner
            await ctx.cleanup()

    @pytest.mark.asyncio
    async def test_resume_when_idle_works_normally(self, tmp_path: Path):
        """Retry when executor is idle: normal path (regression test)."""
        responses = [
            # First call: tool + interrupted
            MockResponse(tool_calls=[("echo", "c1", {"message": "hello"})]),
            MockResponse(text="Will be interrupted"),
            # Resume
            MockResponse(text="Resumed successfully"),
        ]
        ctx = await create_test_engine(responses, tmp_path)

        try:
            # Interrupt during final response (same pattern as existing test)
            original_inner = ctx.mock_client._inner_get_response
            call_count = [0]
            interrupt_mw = ctx.engine.current.loaded.bindings._interrupt

            def _interrupt_on_second(*, messages, stream, options, **kwargs):
                call_count[0] += 1
                if call_count[0] == 2:
                    interrupt_mw.set_interrupted()
                return original_inner(messages=messages, stream=stream, options=options, **kwargs)

            ctx.mock_client._inner_get_response = _interrupt_on_second

            await ctx.send_message("Do something")

            assert ctx.engine.current.loaded.bindings.state.was_interrupted
            assert ctx.engine.state != EngineState.PENDING_RETRY, "No pending retry — executor is idle"

            # Now retry normally (executor is idle)
            ctx.mock_client._inner_get_response = original_inner
            ctx.events.clear()
            await ctx.send_retry()

            finals = extract_final_messages(ctx.events)
            assert any("Resumed" in t for t in finals), f"Expected resumed response: {finals}"

        finally:
            ctx.mock_client._inner_get_response = original_inner
            await ctx.cleanup()
