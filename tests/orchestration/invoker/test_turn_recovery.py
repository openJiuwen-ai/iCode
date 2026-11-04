# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Turn invocation boundaries, owner-close wiring and observer flags."""

from __future__ import annotations

import asyncio
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import UserInterrupt, UserMessage, UserRetry
from chrys.kernel import AgentResponse, tool
from chrys.orchestration.engine.run.runner import TurnRunner
from chrys.orchestration.invoker.contracts import AbortCause, Aborted, Failed, Ok, RunIntent
from chrys.service.llm.mock import MockResponse
from tests.orchestration.invoker._build_fixtures import build_recipe_engine
from tests.orchestration.invoker._main_pass import fresh_pass


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("route", ["user", "shutdown"])
async def test_real_engine_interrupt_routes_through_backend_abort(route, stream, tmp_path, monkeypatch, agent_engine):
    entered, release = asyncio.Event(), asyncio.Event()

    @tool
    async def blocked() -> str:
        entered.set()
        await release.wait()
        return "done"

    engine, _, _ = await build_recipe_engine(
        agent_engine,
        monkeypatch,
        tmp_path,
        main=[MockResponse(tool_calls=[("blocked", "c1", {})])],
        child=[],
        stream=stream,
    )
    executor = engine.current.loaded.bindings
    executor._agent.default_options["tools"] = [blocked]
    backend = executor.backend
    abort = create_autospec(backend.abort, side_effect=backend.abort)
    monkeypatch.setattr(backend, "abort", abort)
    try:
        await engine.event_bus.publish(UserMessage(text="work"))
        await asyncio.wait_for(entered.wait(), 5)
        handle = backend.active_handle
        assert handle is not None
        if route == "user":
            await engine.event_bus.publish(UserInterrupt())
            await engine.wait_for_run_task()
        else:
            await engine.shutdown()
        cause = AbortCause.USER_CANCEL if route == "user" else AbortCause.OWNER_CLOSE
        abort.assert_awaited_once_with(handle, cause)
        outcome = executor.inputs.outcome
        assert isinstance(outcome, Aborted)
        assert outcome.handle is handle
        assert outcome.cause is cause
    finally:
        release.set()
        await engine.shutdown()


async def test_real_prepare_stop_retry_does_not_use_previous_invocation(
    tmp_path, monkeypatch, agent_engine, *, engine_services
):
    engine, main, _ = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[ValueError("first failed"), MockResponse(text="retried")], child=[]
    )
    begun = []
    original_begin = engine.current.loaded.bindings.inputs.begin_invocation

    def begin(*, typed_text=None):
        original_begin(typed_text=typed_text)
        begun.append(engine.current.loaded.bindings.inputs.origin)

    monkeypatch.setattr(
        engine.current.loaded.bindings.inputs, "begin_invocation", create_autospec(original_begin, side_effect=begin)
    )
    entered, release = asyncio.Event(), asyncio.Event()
    original = TurnRunner._fire_before_turn
    try:
        await engine.event_bus.publish(UserMessage(text="first"))
        await engine.wait_for_run_task()
        assert isinstance(engine.current.loaded.bindings.inputs.outcome, Failed)
        previous = engine.current.loaded.bindings.inputs.origin
        assert previous == begun[0]

        async def prepare(*args, **kwargs):
            if not kwargs.get("is_retry", False):
                entered.set()
                await release.wait()
            return await original(*args, **kwargs)

        monkeypatch.setattr(TurnRunner, "_fire_before_turn", create_autospec(original, side_effect=prepare))
        await engine.event_bus.publish(UserMessage(text="second"))
        await asyncio.wait_for(entered.wait(), 5)
        current = engine.current.loaded.bindings.inputs.origin
        assert current != previous
        assert engine.current.loaded.bindings.inputs.outcome is None
        assert engine.current.loaded.bindings.inputs.evidence.passes == ()
        await engine.event_bus.publish(UserInterrupt())
        release.set()
        await engine.wait_for_run_task()
        assert engine.current.loaded.bindings.state.was_interrupted
        await engine.event_bus.publish(UserRetry())
        await engine.wait_for_run_task()  # A real task would surface StaleContinuation here.
        assert isinstance(engine.current.loaded.bindings.inputs.outcome, Ok)
        assert not engine_services(engine).fsm.is_running()
        assert main.call_count == 2
        assert engine.current.loaded.bindings.inputs.origin == current
        assert engine.current.loaded.bindings.inputs.evidence.passes == (
            engine.current.loaded.bindings.inputs.outcome.handle.pass_id,
        )
        assert engine.current.loaded.bindings.inputs.origin.invocation_id != previous.invocation_id, (
            "new stopped turn reused old origin",
            engine.current.loaded.bindings.inputs.evidence,
        )
    finally:
        release.set()
        await engine.shutdown()


@pytest.mark.parametrize("route", ["fresh", "retry"])
async def test_real_engine_owner_close_cause_reaches_outcome(route, tmp_path, monkeypatch, agent_engine):
    engine, _, _ = await build_recipe_engine(agent_engine, monkeypatch, tmp_path, main=[ValueError("failed")], child=[])
    entered, release = asyncio.Event(), asyncio.Event()
    outcomes = []
    backend = engine.current.loaded.bindings.backend
    closing = None
    try:
        if route == "retry":
            await engine.event_bus.publish(UserMessage(text="first"))
            await engine.wait_for_run_task()
            assert isinstance(engine.current.loaded.bindings.inputs.outcome, Failed)

        async def attempt(input_messages, run_kwargs, *, stream, service_side):
            entered.set()
            await release.wait()
            return AgentResponse(messages=[])

        original_run = backend.run

        async def observe(request):
            outcome = await original_run(request)
            outcomes.append(outcome)
            return outcome

        monkeypatch.setattr(backend._attempts, "run", create_autospec(backend._attempts.run, side_effect=attempt))
        monkeypatch.setattr(backend, "run", create_autospec(original_run, side_effect=observe))
        await engine.event_bus.publish(UserMessage(text="work") if route == "fresh" else UserRetry())
        await asyncio.wait_for(entered.wait(), 5)
        closing = asyncio.create_task(engine.current.loaded.prepared.aclose())
        await asyncio.wait_for(closing, 5)
        assert len(outcomes) == 1
        assert isinstance(outcomes[0], Aborted)
        assert outcomes[0].cause is AbortCause.OWNER_CLOSE
    finally:
        release.set()
        if closing is not None:
            await asyncio.gather(closing, return_exceptions=True)
        await engine.shutdown()


async def test_retry_after_pre_run_interrupt_following_failed_turn(executor):
    from chrys.kernel import Message
    from tests.orchestration.invoker._main_pass import retry_pass
    from tests.support.scripted_clients import ErrorMockChatClient

    executor._agent.client = ErrorMockChatClient([ValueError("failed"), MockResponse(text="retried")])
    await fresh_pass(executor, ["first"])
    assert isinstance(executor.inputs.outcome, Failed)
    previous = executor.inputs.origin
    executor.inputs.begin_invocation()
    assert executor.inputs.outcome is None
    executor.inputs.fresh_request(["second"])
    current = executor.inputs.origin
    executor.record_pre_run_interrupt()
    assert executor.inputs.origin == current != previous
    executor.backend.history_state.setdefault("messages", []).append(Message("user", ["second"]))
    await retry_pass(executor)
    assert isinstance(executor.inputs.outcome, Ok)
    assert executor._agent.client.call_count == 2
    assert executor.inputs.origin == current
    assert executor.inputs.evidence.passes == (executor.inputs.outcome.handle.pass_id,)


@pytest.mark.parametrize("restore", ["setter", "snapshot"])
async def test_retry_after_history_restore_continues_instead_of_raising(executor, restore):
    from chrys.kernel import Message
    from chrys.orchestration.invoker.contracts import RunIntent
    from tests.orchestration.invoker._main_pass import retry_pass
    from tests.support.scripted_clients import ErrorMockChatClient

    backend = executor.backend
    executor._agent.client = ErrorMockChatClient([ValueError("failed"), MockResponse(text="retried")])
    backend.history_state = {"messages": [Message("user", ["restored"])]}
    snapshot = backend.checkpoint()
    await fresh_pass(executor, ["first"])
    assert isinstance(executor.inputs.outcome, Failed)
    ticket = executor.inputs.outcome.continuation
    if restore == "setter":
        backend.history_state = {"messages": [Message("user", ["restored"])]}
    else:
        backend.restore(snapshot)
    assert backend.continuation_is_live(ticket, executor.inputs.origin) is False
    request = executor.inputs.continuation_request([])
    assert request.intent is RunIntent.CONTINUE
    assert request.continuation is None
    assert executor.inputs.outcome.continuation is None
    await retry_pass(executor)
    assert isinstance(executor.inputs.outcome, Ok)
    assert executor._agent.client.call_count == 2


async def test_foreign_ticket_admission_failure_finalizes_real_engine(
    tmp_path, monkeypatch, agent_engine, caplog, *, engine_services
):
    from dataclasses import replace

    from chrys.orchestration.invoker.contracts import RunIntent

    engine, main, _ = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[ValueError("first failed")], child=[]
    )
    try:
        await engine.event_bus.publish(UserMessage(text="work"))
        await engine.wait_for_run_task()
        policy = engine.current.loaded.bindings.inputs
        failed = policy.outcome
        assert isinstance(failed, Failed)
        original = policy.continuation_request

        def foreign(messages):
            return replace(original(messages), intent=RunIntent.RETRY, continuation=replace(failed.continuation))

        monkeypatch.setattr(policy, "continuation_request", create_autospec(original, side_effect=foreign))
        save = engine.writer.save_current_session
        saved = create_autospec(save, side_effect=save)
        monkeypatch.setattr(engine.writer, "save_current_session", saved)
        await engine.event_bus.publish(UserRetry())
        await engine.wait_for_run_task()
        assert not engine_services(engine).fsm.is_running()
        assert engine.current.loaded.bindings.state.run_failed is True
        assert engine.current.loaded.bindings.state.last_error == "Continuation no longer names this live state"
        assert "Turn admission failed" in caplog.text
        assert engine.execution_busy() is False
        saved.assert_awaited_once()
        assert main.call_count == 1
    finally:
        await engine.shutdown()


@pytest.mark.parametrize("site", ["fresh_request", "retry_request", "backend_run"])
@pytest.mark.parametrize("error_name", ["StaleContinuation", "OverlappingRun", "PreparedClosed", "UnsupportedRequest"])
async def test_admission_errors_finalize_and_save_once(
    tmp_path, monkeypatch, agent_engine, caplog, site, error_name, *, engine_services
):
    from chrys.orchestration.invoker.contracts import (
        OverlappingRun,
        PreparedClosed,
        StaleContinuation,
        UnsupportedRequest,
    )

    errors = {c.__name__: c for c in (StaleContinuation, OverlappingRun, PreparedClosed, UnsupportedRequest)}
    engine, main, _ = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[ValueError("first failed")], child=[]
    )
    try:
        await engine.event_bus.publish(UserMessage(text="first"))
        await engine.wait_for_run_task()
        backend = engine.current.loaded.bindings.backend
        target = "run" if site == "backend_run" else "validate"
        original = backend.run if site == "backend_run" else backend.validate
        monkeypatch.setattr(
            backend, target, create_autospec(original, side_effect=errors[error_name]("admission denied"))
        )
        save = engine.writer.save_current_session
        saved = create_autospec(save, side_effect=save)
        monkeypatch.setattr(engine.writer, "save_current_session", saved)
        await engine.event_bus.publish(UserMessage(text="second") if site == "fresh_request" else UserRetry())
        await engine.wait_for_run_task()
        assert not engine_services(engine).fsm.is_running()
        assert engine.current.loaded.bindings.state.run_failed is True
        assert engine.current.loaded.bindings.state.last_error == "admission denied"
        assert "Turn admission failed: admission denied" in caplog.text
        assert engine.execution_busy() is False
        saved.assert_awaited_once()
        assert main.call_count == 1
    finally:
        await engine.shutdown()


async def test_fresh_request_keeps_begun_origin(executor):
    executor.inputs.begin_invocation()
    begun_origin = executor.inputs.origin
    request = executor.inputs.fresh_request(["work"])
    assert request.origin is begun_origin
    assert executor.inputs.origin is begun_origin


async def test_live_policy_ticket_is_retained_until_run(executor):
    executor.backend._attempts.run = create_autospec(executor.backend._attempts.run, side_effect=ValueError("failed"))
    await fresh_pass(executor, ["work"])
    failed = executor.inputs.outcome
    assert isinstance(failed, Failed)
    for _ in range(2):
        request = executor.inputs.continuation_request([])
        assert request.intent is RunIntent.RETRY
        assert request.continuation is failed.continuation
        assert executor.inputs.outcome is failed


@pytest.mark.parametrize("site", ["fresh_request", "retry_request", "fresh_backend_validate", "retry_backend_validate"])
@pytest.mark.parametrize("error_name", ["StaleContinuation", "OverlappingRun", "PreparedClosed", "UnsupportedRequest"])
async def test_request_admission_failure_converges_fsm(
    tmp_path, monkeypatch, agent_engine, site, error_name, *, engine_services
):
    from chrys.orchestration.invoker import contracts

    classes = {
        cls.__name__: cls
        for cls in (
            contracts.StaleContinuation,
            contracts.OverlappingRun,
            contracts.PreparedClosed,
            contracts.UnsupportedRequest,
        )
    }
    engine, main, _ = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[ValueError("first failed")], child=[]
    )
    try:
        await engine.event_bus.publish(UserMessage(text="first"))
        await engine.wait_for_run_task()
        original = engine.current.loaded.bindings.backend.validate
        calls = 0

        def validate(request):
            nonlocal calls
            calls += 1
            if calls == (2 if "backend" in site else 1):
                raise classes[error_name]("admission denied")
            return original(request)

        monkeypatch.setattr(
            engine.current.loaded.bindings.backend, "validate", create_autospec(original, side_effect=validate)
        )
        save = engine.writer.save_current_session
        saved = create_autospec(save, side_effect=save)
        monkeypatch.setattr(engine.writer, "save_current_session", saved)
        await engine.event_bus.publish(UserMessage(text="second") if site.startswith("fresh") else UserRetry())
        task = engine.turns.turn_state.lease.run_task
        result = await asyncio.gather(task, return_exceptions=True)
        assert not engine_services(engine).fsm.is_running(), (
            result,
            engine_services(engine).fsm.state,
            saved.await_count,
        )
        assert result == [None]
        assert engine.current.loaded.bindings.state.run_failed is True
        assert engine.current.loaded.bindings.state.last_error == "admission denied"
        assert engine.execution_busy() is False
        saved.assert_awaited_once()
        assert main.call_count == 1
    finally:
        if engine.turns.turn_state.lease.run_task is not None and engine.turns.turn_state.lease.run_task.done():
            engine.turns.turn_state.lease.release_run_task()
        await engine.shutdown()
