# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pins the TurnBindings function-middleware chain.

The chain order is a contract (first = outermost): everything sits inside
``tool_events``, so a call a middleware answers itself (``ask_user``) still
publishes ``ToolCallStart``/``ToolCallResult``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult, InvocationToolCallStart
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.tool_kinds import KIND_SUB_AGENT, set_tool_kind
from chrys.foundation.tool_result_metadata import (
    TOOL_INTERRUPTED_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from chrys.foundation.util.sub_agent_context import sub_agent_parent_result_metadata
from chrys.kernel import AgentSession, LoopRecorder, tool
from chrys.kernel.middleware import ChatMiddlewareLayer, FunctionMiddleware
from chrys.kernel.types import ChatResponse, Content, Message
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.resources import Conversation
from chrys.service.agent_middleware import ApprovalMiddleware
from chrys.service.agent_middleware.events.tool_events import ToolEventMiddleware
from tests.orchestration.invoker._main_pass import continuation_pass
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from chrys.kernel.middleware import FunctionInvocationContext


class _PassThrough(FunctionMiddleware):
    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        await call_next()


class _ShortCircuit(FunctionMiddleware):
    """Sets the result without calling next — the ask_user intercept shape."""

    def __init__(self) -> None:
        self.calls = 0

    async def process(self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]) -> None:
        self.calls += 1
        context.result = "short-circuited"


class _ScriptedClient:
    """Innermost fake wire client (non-streaming turns only)."""

    def __init__(self, turns: list[ChatResponse]) -> None:
        self.turns = list(turns)

    def get_response(self, messages: Any, *, stream: bool = False, options: Any = None, **kwargs: Any) -> Any:
        turn = self.turns.pop(0)

        async def _resolve() -> ChatResponse:
            return turn

        return _resolve()


def _make_executor(
    *,
    agent: Any = None,
    event_bus: EventBus | None = None,
    ask_user: FunctionMiddleware | None = None,
    compaction_strategy: Any = None,
    run_cycle_start_hooks: tuple[Any, ...] = (),
) -> tuple[TurnBindings, FunctionMiddleware, FunctionMiddleware]:
    ask_user = ask_user if ask_user is not None else _PassThrough()
    approval = create_autospec(ApprovalMiddleware, instance=True)
    approval.process.side_effect = _PassThrough().process
    executor = TurnBindings(
        conversation=Conversation(),
        agent=agent if agent is not None else MagicMock(),
        session=AgentSession(),
        event_bus=event_bus or EventBus(),
        approval_middleware=approval,  # type: ignore[arg-type]
        ask_user_middleware=ask_user,  # type: ignore[arg-type]
        injection_middleware=MagicMock(),
        compaction_strategy=compaction_strategy,
        run_cycle_start_hooks=run_cycle_start_hooks,
    )
    return executor, ask_user, approval


def test_chain_order_is_pinned() -> None:
    """[tool_events, ask_user, approval, sleep, interrupt, hook_commit] — identity."""
    executor, ask_user, approval = _make_executor()

    middleware = executor._build_run_kwargs()["middleware"]

    assert middleware == [
        executor.tool_events,
        ask_user,
        approval,
        executor._sleep,
        executor._interrupt,
        executor._hook_commit,
    ]


def test_run_cycle_start_hooks_fire_once_per_cycle() -> None:
    """Hooks fire at the START of every user-initiated cycle — before the
    first attempt, even when the run fails — so state carried across a
    cycle's whole-run retries (validation budget) cannot leak into the next
    independent run through an aborted cycle."""
    agent = MagicMock()
    agent.client = SimpleNamespace(STORES_BY_DEFAULT=False)
    agent.run = AsyncMock(side_effect=RuntimeError("boom"))
    calls: list[int] = []
    executor, _ask_user, _approval = _make_executor(
        agent=agent,
        run_cycle_start_hooks=(lambda: calls.append(1),),
    )

    asyncio.run(continuation_pass(executor, []))
    assert executor.state.run_failed
    assert len(calls) == 1

    asyncio.run(continuation_pass(executor, []))
    assert len(calls) == 2


def test_raising_run_cycle_start_hook_fails_the_run_cleanly() -> None:
    """A raising hook must land on the normal executor error path — run_failed
    set, no exception escaping, and ``is_running`` cleared — not leave the
    executor permanently 'running'."""
    agent = MagicMock()
    agent.client = SimpleNamespace(STORES_BY_DEFAULT=False)
    agent.run = AsyncMock(return_value=MagicMock())

    def _bad_hook() -> None:
        raise RuntimeError("hook exploded")

    executor, _ask_user, _approval = _make_executor(agent=agent, run_cycle_start_hooks=(_bad_hook,))

    asyncio.run(continuation_pass(executor, []))

    assert executor.state.run_failed
    assert "hook exploded" in executor.state.last_error
    assert not executor.state.running


def test_compaction_tokenizer_is_forwarded_to_the_agent_run() -> None:
    tokenizer = object()
    strategy = MagicMock(tokenizer=tokenizer)
    executor, _ask_user, _approval = _make_executor(compaction_strategy=strategy)

    run_kwargs = executor._build_run_kwargs()

    assert run_kwargs["compaction_strategy"] is strategy
    assert run_kwargs["tokenizer"] is tokenizer


@pytest.mark.asyncio
async def test_a_call_ask_user_answers_itself_still_publishes_tool_events() -> None:
    """ask_user sits inside tool_events: a result set without call_next still
    yields ToolCallStart AND ToolCallResult, and the tool body never runs."""
    bus = EventBus()
    starts: list[InvocationToolCallStart] = []
    results: list[InvocationToolCallResult] = []

    async def _on_start(event: InvocationToolCallStart) -> None:
        starts.append(event)

    async def _on_result(event: InvocationToolCallResult) -> None:
        results.append(event)

    await bus.subscribe(InvocationToolCallStart, _on_start)
    await bus.subscribe(InvocationToolCallResult, _on_result)

    short_circuit = _ShortCircuit()
    executor, _ask_user, _approval = _make_executor(event_bus=bus, ask_user=short_circuit)
    middleware = executor._build_run_kwargs()["middleware"]

    @tool(name="boom")
    async def boom(text: str) -> str:
        raise AssertionError("tool body must not run — ask_user short-circuits")

    call = Content.from_function_call(call_id="c1", name="boom", arguments={"text": "x"})
    turns = [
        ChatResponse(messages=[Message(role="assistant", contents=[call])]),
        ChatResponse(messages=[Message(role="assistant", contents=["done"])]),
    ]
    layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(_ScriptedClient(turns)))

    response = await layer.get_response(
        [Message(role="user", contents=["hi"])],
        options={"tools": [boom]},
        middleware=middleware,
    )

    assert short_circuit.calls == 1
    fn_results = [item for msg in response.messages for item in msg.contents if item.type == "function_result"]
    assert len(fn_results) == 1
    assert "short-circuited" in str(fn_results[0].result)
    assert [event.tool_name for event in starts] == ["boom"]
    assert len(results) == 1
    assert "short-circuited" in results[0].result
    assert results[0].call_id == starts[0].call_id


@pytest.mark.asyncio
async def test_sub_agent_cancel_callback_fills_parent_slot_with_replay_references() -> None:
    from chrys.orchestration.sub_agents.tools import (
        _commit_interrupted_parent_result,
        _upgrade_interrupted_parent_result,
    )

    bus = EventBus()
    entered = asyncio.Event()

    @tool(name="delegated")
    async def delegated() -> str:
        metadata = sub_agent_parent_result_metadata.get()
        assert metadata is not None
        metadata.sub_agent_invocation_id = "child-1"
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Preserve the early cancellation commit even when the initial
            # running audit write failed, then upgrade that filled slot after
            # the terminal write creates the first durable audit file.
            _commit_interrupted_parent_result(metadata)
            metadata.sub_agent_log_file = "child-1.json"
            metadata.sub_agent_audit_complete = True
            _upgrade_interrupted_parent_result(metadata)
            raise

    set_tool_kind(delegated, KIND_SUB_AGENT)
    recorder = LoopRecorder()
    call = Content.from_function_call("parent-call", "delegated", arguments={})
    layer = InvariantCheckedToolLoopLayer(
        ChatMiddlewareLayer(_ScriptedClient([ChatResponse(messages=[Message("assistant", [call])])]))
    )
    task = asyncio.create_task(
        layer.get_response(
            [Message("user", ["delegate"])],
            options={"tools": [delegated]},
            middleware=[ToolEventMiddleware(bus, origin=InvocationOrigin("turn", "", "turn-test", None))],
            client_kwargs={"loop_recorder": recorder},
        )
    )
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    projected = recorder.loop_messages
    assert projected is not None
    result = projected[-1].contents[0]
    metadata = result.additional_properties[TOOL_RESULT_METADATA_KEY]
    assert metadata[TOOL_INTERRUPTED_METADATA_KEY] is True
    assert metadata["sub_agent_invocation_id"] == "child-1"
    assert metadata["sub_agent_log_file"] == "child-1.json"
    assert metadata["sub_agent_audit_complete"] is True
