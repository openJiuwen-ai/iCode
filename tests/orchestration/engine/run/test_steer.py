# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Steer: a message that joins a running turn and takes over from what the turn is doing.

A ``UserMessage`` with ``steer=True`` sent while the turn's pass produces
aborts that pass: the response it was streaming is given up
(``InvocationContentAbandoned``), tool calls still running are cancelled and
keep their interrupted results, and the pass ends with ``InvocationSteered``.
The turn goes on in the same run with a new pass that continues from what the
first committed, without its cut-off answer (``InvocationAnswerWithdrawn``),
and delivers the steer message at its first model call. No interrupted marker,
save or new turn comes between. A steer that arrives once the pass's answer is
complete aborts nothing: the message misses the run as any queued message
does. A plain message still waits for the next model call.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated, Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import (
    Error,
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationSteered,
    UserInject,
    UserInjectCancel,
    UserInjectResult,
    UserInterrupt,
    UserMessage,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.recovery import RecoveryPersistOutcome
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import FunctionTool
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.engine.run.finalizer import TurnFinalizer
from chrys.orchestration.engine.state.machine import EngineState
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.invoker.contracts import AbortCause
from chrys.orchestration.invoker.kernel import KernelConversation
from chrys.service.llm.mock import MockChatClient, MockResponse
from tests.orchestration.engine.run._streaming_helpers import (
    _HeldStreamClient,
    _marker,
    _start,
    _Started,
    _texts,
    _turn_events,
    _wait_shown,
)
from tests.support.loaded_agents import install_loaded_agent
from tests.support.pipeline_helpers import echo_tool
from tests.support.scripted_clients import FrameworkBoom
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Awaitable, Callable
    from pathlib import Path

    from chrys.foundation.models.invocations import PassHandle
    from chrys.kernel import AgentResponse, ChatResponse, ChatResponseUpdate, FinishReason, FinishReasonLiteral, Message
    from chrys.orchestration.invoker.contracts import AbortResult

_STEER_EVENTS: tuple[type, ...] = (InvocationSteered, UserInjectResult)


class _HeldTool:
    """A tool call the test holds: it runs until released, and records whether it was cancelled."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled: list[str] = []
        self.end_cancelled = False
        """Released, end the call cancelled from inside, as a sub-agent's tool does when the child is aborted."""

        async def hold(message: Annotated[str, "What to hold"]) -> str:
            self.entered.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.append(message)
                raise
            if self.end_cancelled:
                raise asyncio.CancelledError
            return f"held: {message}"

        self.tool = FunctionTool(func=hold, name="hold", description="Hold until released")


def _request(client: MockChatClient, index: int) -> list[Message]:
    sent, _options = client.call_history[index]
    return [message for message in sent if message.role != "system"]


def _said(messages: list[Message]) -> list[tuple[str, str]]:
    """Each message's role and own text, without the reminders a request adds after it."""
    return [(message.role, message.contents[0].text or "") for message in messages]


def _results(messages: list[Message]) -> list[str]:
    return [
        str(content.result) for message in messages for content in message.contents if content.type == "function_result"
    ]


async def _saved(started: _Started) -> list[dict[str, Any]]:
    saved = await started.store.load_session_raw(started.engine.session.session_id)
    assert saved is not None
    return saved


async def _wait_entered(started: _Started, tool: _HeldTool) -> None:
    def _entered_or_ended() -> bool:
        task = started.engine.turns.turn_state.lease.run_task
        return tool.entered.is_set() or (task is not None and task.done())

    await wait_for(_entered_or_ended, timeout=ENGINE_TURN_TIMEOUT, description="the held tool running")
    assert tool.entered.is_set(), "the run ended before the tool ran"


def _inject_results(started: _Started, text: str) -> list[bool]:
    return [event.consumed for event in started.events if isinstance(event, UserInjectResult) and event.text == text]


async def test_a_steer_mid_answer_drops_the_cut_off_answer_and_goes_on_with_the_message(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6), MockResponse(text="Steered.")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()

    assert not [event for event in started.events if isinstance(event, Error)]
    assert client.call_count == 2
    second = _request(client, 1)
    assert _said(second) == [("user", "go"), ("user", "do this instead")]
    (cut_off, *rest) = _turn_events(started.events, InvocationContentDelta)
    assert cut_off.text == "Hello "
    (abandoned,) = _turn_events(started.events, InvocationContentAbandoned)
    assert abandoned.segment_ids == (cut_off.segment_id,)
    (steered,) = _turn_events(started.events, InvocationSteered)
    (withdrawn,) = _turn_events(started.events, InvocationAnswerWithdrawn)
    assert withdrawn.segment_ids == (cut_off.segment_id,)
    assert started.events.index(abandoned) < started.events.index(steered) < started.events.index(withdrawn)
    assert [delta.text for delta in rest] == ["Steered."]
    assert _inject_results(started, "do this instead") == [True]

    # One turn, no interrupt in it: the steer message follows the opener, and the second pass answered.
    assert started.engine.state is EngineState.IDLE
    assert started.engine.session.turn_number == 1
    saved = await _saved(started)
    assert [_marker(message) for message in saved if _marker(message) is not None] == [HistoryMarkerKind.TURN]
    unmarked = [message for message in saved if _marker(message) is None]
    assert [(message["role"], _texts(message)) for message in unmarked] == [
        ("user", ["go"]),
        ("user", ["do this instead"]),
        ("assistant", ["Steered."]),
    ]
    assert unmarked[1]["additional_properties"][HistoryMarkerKind.INJECTED_KEY] is True


async def test_a_steer_while_a_tool_runs_cancels_it_and_goes_on_after_its_interrupted_result(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _HeldTool()
    client = MockChatClient(
        responses=[
            MockResponse(text="Let me check", tool_calls=[("hold", "c1", {"message": "x"})]),
            MockResponse(text="Steered."),
        ]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[held.tool])
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert held.cancelled == ["x"]
    assert client.call_count == 2
    second = _request(client, 1)
    # The narration stays with its call, which keeps the result saying it was interrupted.
    assert [message.role for message in second] == ["user", "assistant", "tool", "user"]
    assert second[1].text == "Let me check"
    (result,) = _results(second)
    assert result.startswith("Error: Tool execution was interrupted.")
    assert _said(second)[-1] == ("user", "do this instead")
    assert len(_turn_events(started.events, InvocationSteered)) == 1
    assert _turn_events(started.events, InvocationAnswerWithdrawn) == []
    assert _inject_results(started, "do this instead") == [True]
    assert started.engine.state is EngineState.IDLE


async def test_a_plain_message_waits_for_the_running_tool_and_the_next_model_call(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = _HeldTool()
    client = MockChatClient(
        responses=[MockResponse(tool_calls=[("hold", "c1", {"message": "x"})]), MockResponse(text="Done.")]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[held.tool])
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserMessage(text="also this"))
        assert started.engine.current.require_loaded().bindings.state.running
        held.release.set()
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert held.cancelled == []
    assert client.call_count == 2
    second = _request(client, 1)
    assert _results(second) == ["held: x"]
    assert _said(second)[-1] == ("user", "also this")
    assert _turn_events(started.events, InvocationSteered) == []


async def test_a_steer_once_the_answer_landed_aborts_nothing_and_the_message_misses_the_run(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A complete answer is never dropped: the steer comes back unsent, as any message that misses its run."""
    client = MockChatClient(responses=[MockResponse(text="All done."), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    landed, release = asyncio.Event(), asyncio.Event()
    succeeded = TurnBindings.succeeded

    async def hold_after_landing(self: TurnBindings, result: AgentResponse) -> None:
        landed.set()
        await release.wait()
        await succeeded(self, result)

    monkeypatch.setattr(TurnBindings, "succeeded", create_autospec(succeeded, side_effect=hold_after_landing))
    try:
        await started.bus.publish(UserMessage(text="go"))
        await wait_for(landed.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the answer landed")
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        release.set()
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        release.set()

    assert client.call_count == 1
    assert _turn_events(started.events, InvocationSteered) == []
    assert _turn_events(started.events, InvocationAnswerWithdrawn) == []
    assert _inject_results(started, "do this instead") == [False]
    assert started.engine.state is EngineState.IDLE
    assert [message.text for message in started.engine.history_messages if message.role == "assistant"][-2:] == [
        "All done.",
        "",
    ]


async def test_a_steer_before_the_pass_produces_reaches_its_first_model_call(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No request went out yet, so nothing is dropped: the first one carries the message."""
    client = MockChatClient(responses=[MockResponse(text="Both."), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    beginning, release = asyncio.Event(), asyncio.Event()
    begin = TurnBindings.begin

    async def hold_begin(self: TurnBindings, request: Any, resources: Any) -> None:
        beginning.set()
        await release.wait()
        await begin(self, request, resources)

    monkeypatch.setattr(TurnBindings, "begin", create_autospec(begin, side_effect=hold_begin))
    try:
        await started.bus.publish(UserMessage(text="go"))
        await wait_for(beginning.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the pass beginning")
        await started.bus.publish(UserMessage(text="and this", steer=True))
        release.set()
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        release.set()

    assert client.call_count == 1
    assert _said(_request(client, 0)) == [("user", "go"), ("user", "and this")]
    assert _turn_events(started.events, InvocationSteered) == []
    assert _inject_results(started, "and this") == [True]


async def test_a_steer_in_the_first_call_keeps_one_opener_whose_reminders_render_the_same(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The aborted pass never stored its input: the next pass sends that very message, byte for byte."""
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6), MockResponse(text="Steered.")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()

    first_opener = _request(client, 0)[0]
    second_opener = _request(client, 1)[0]
    assert [content.text for content in second_opener.contents] == [content.text for content in first_opener.contents]
    openers = [
        message
        for message in started.engine.history_messages
        if message.role == "user" and not message.additional_properties.get(HistoryMarkerKind.INJECTED_KEY)
    ]
    assert len(openers) == 1
    assert read_analytics_item_id(openers[0].additional_properties) == read_analytics_item_id(
        first_opener.additional_properties
    )
    assert HistoryMarkerKind.SYSTEM_REMINDERS_KEY in openers[0].additional_properties


async def test_a_stop_between_the_steered_pass_and_the_next_ends_the_turn_interrupted(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop dominates a steer: the turn ends with what the steer settled, and the message comes back unsent."""
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    settle = TurnFinalizer.settle_steered_pass

    async def stop_after_settling(self: TurnFinalizer, inputs: Any, *, withdraw: Any) -> None:
        await settle(self, inputs, withdraw=withdraw)
        await started.bus.publish(UserInterrupt())

    monkeypatch.setattr(TurnFinalizer, "settle_steered_pass", create_autospec(settle, side_effect=stop_after_settling))
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()

    assert client.call_count == 1
    assert started.engine.state is EngineState.INTERRUPTED
    assert _inject_results(started, "do this instead") == [False]
    saved = await _saved(started)
    unmarked = [message for message in saved if _marker(message) is None]
    # The cut-off answer the steer dropped stays dropped.
    assert [(message["role"], _texts(message)) for message in unmarked] == [("user", ["go"])]
    assert HistoryMarkerKind.INTERRUPTED in [_marker(message) for message in saved]


async def test_a_stop_in_the_pass_after_a_steer_keeps_the_work_both_passes_committed(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each pass's work stays in the order it ran: the next pass's loop messages merge after the settled ones."""
    held = _HeldTool()
    client = _HeldStreamClient(
        [
            MockResponse(tool_calls=[("hold", "c1", {"message": "x"})]),
            MockResponse(tool_calls=[("echo", "c2", {"message": "second"})]),
            MockResponse(text="Partial second", chunk_size=8),
            MockResponse(text="never"),
        ],
        hold_stream=3,
    )
    started = await _start(
        agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[echo_tool, held.tool]
    )
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await _wait_shown(started, client)
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()
        client.release.set()

    assert client.call_count == 3
    assert started.engine.state is EngineState.INTERRUPTED
    saved = await _saved(started)
    unmarked = [message for message in saved if _marker(message) is None]
    roles = [message["role"] for message in unmarked]
    # The first pass's call and its interrupted result, the steer message, the
    # second pass's call and result, and its kept answer.
    assert roles == ["user", "assistant", "tool", "user", "assistant", "tool", "assistant"]
    call_ids = [
        content["call_id"]
        for message in unmarked
        for content in message["contents"]
        if content["type"] == "function_call"
    ]
    assert call_ids == ["c1", "c2"]
    assert _texts(unmarked[3]) == ["do this instead"]
    assert _texts(unmarked[6]) == ["Partial "]
    item_ids = [message["additional_properties"].get("_chrys_analytics_item_id") for message in saved]
    assert len(item_ids) == len(set(item_ids))


async def test_a_recovery_checkpoint_between_passes_holds_the_settled_work_once(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steered pass's loop messages are in history now: a checkpoint must not merge them a second time."""
    held = _HeldTool()
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("echo", "c0", {"message": "first"})]),
            MockResponse(tool_calls=[("hold", "c1", {"message": "x"})]),
            MockResponse(text="Steered."),
        ]
    )
    started = await _start(
        agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[echo_tool, held.tool]
    )
    snapshots: list[dict[str, Any] | None] = []
    settle = TurnFinalizer.settle_steered_pass

    async def snapshot_after_settling(self: TurnFinalizer, inputs: Any, *, withdraw: Any) -> None:
        await settle(self, inputs, withdraw=withdraw)
        snapshots.append(started.engine.writer.build_recovery_snapshot())

    monkeypatch.setattr(
        TurnFinalizer, "settle_steered_pass", create_autospec(settle, side_effect=snapshot_after_settling)
    )
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    (snapshot,) = snapshots
    assert snapshot is not None
    messages = snapshot["messages"]
    call_ids = [
        content.call_id for message in messages for content in message.contents if content.type == "function_call"
    ]
    assert call_ids == ["c0", "c1"]
    item_ids = [read_analytics_item_id(message.additional_properties) for message in messages]
    assert len(item_ids) == len(set(item_ids))
    assert client.call_count == 3


async def test_work_the_steered_pass_committed_after_its_last_request_is_durable_before_the_next_one(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result that landed beside the cancelled call is persisted before the next pass's first request.

    Settling hands that work to history, so the steered pass's recorder no
    longer counts it and the next request's own barrier would not run.
    """
    held = _HeldTool()
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("echo", "c0", {"message": "first"}), ("hold", "c1", {"message": "x"})]),
            MockResponse(text="Steered."),
        ]
    )
    started = await _start(
        agent_engine,
        tmp_path,
        monkeypatch,
        client,
        event_types=_STEER_EVENTS,
        tools=[echo_tool, held.tool],
    )
    barriers: list[tuple[int, RecoveryPersistOutcome, list[str]]] = []
    barrier = SessionWriter.persist_barrier

    async def recording_barrier(self: SessionWriter) -> RecoveryPersistOutcome:
        outcome = await barrier(self)
        recovered = await started.store.load_recovery_session(started.engine.session.session_id)
        results = [
            content.call_id
            for message in (recovered or {}).get("messages", [])
            for content in message.contents
            if content.type == "function_result"
        ]
        barriers.append((client.call_count, outcome, results))
        return outcome

    monkeypatch.setattr(SessionWriter, "persist_barrier", create_autospec(barrier, side_effect=recording_barrier))
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await wait_for(
            lambda: started.engine.current.require_loaded().loop_recorder.committed_count > 0,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the call beside the held one committed",
        )
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert client.call_count == 2
    # Between the passes: the first pass's request went out, the next one's not yet.
    assert (1, RecoveryPersistOutcome.PERSISTED, ["c0", "c1"]) in barriers


async def test_a_steer_once_the_final_answer_landed_aborts_nothing_while_the_attempt_finishes(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer is complete when it lands; the attempt's after-run work (compaction, say) does not reopen it."""
    client = MockChatClient(responses=[MockResponse(text="All done."), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    landed, release = asyncio.Event(), asyncio.Event()
    response_ready = TurnBindings._response_ready

    async def hold_after_landing(self: TurnBindings, response: ChatResponse, final: bool) -> None:
        await response_ready(self, response, final)
        landed.set()
        await release.wait()

    monkeypatch.setattr(
        TurnBindings, "_response_ready", create_autospec(response_ready, side_effect=hold_after_landing)
    )
    try:
        await started.bus.publish(UserMessage(text="go"))
        await wait_for(landed.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the final answer landed")
        conversation = started.engine.current.require_loaded().bindings.backend
        assert conversation.active_handle is not None
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        assert conversation.active_abort_cause is None
        release.set()
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        release.set()

    assert client.call_count == 1
    assert _turn_events(started.events, InvocationSteered) == []
    assert _turn_events(started.events, InvocationAnswerWithdrawn) == []
    assert _inject_results(started, "do this instead") == [False]
    assert started.engine.state is EngineState.IDLE
    assert "All done." in [message.text for message in started.engine.history_messages if message.role == "assistant"]


async def test_a_steer_stands_once_accepted_though_its_message_is_cancelled_while_sub_agents_stop(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Aborting a running sub-agent ends the parent's tool call; a cancel then must not leave the turn half-stopped."""
    held = _HeldTool()
    client = MockChatClient(
        responses=[MockResponse(tool_calls=[("hold", "c1", {"message": "x"})]), MockResponse(text="Went on.")]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[held.tool])
    steer = KernelConversation.steer
    cascaded: list[bool] = []

    async def steer_through_a_cascade(
        self: KernelConversation, handle: PassHandle, *, before_abort: Callable[[], Awaitable[None]]
    ) -> AbortResult:
        async def cascade() -> None:
            await before_abort()
            # Meanwhile the user takes the message back; then, as a cascade-aborted child does, the parent's tool
            # call ends cancelled, and the parent's attempt with it, before the steer itself stops anything.
            await started.bus.publish(UserInjectCancel(injection_id="s1"))
            held.end_cancelled = True
            held.release.set()
            await wait_for(
                lambda: self._attempt_handle.task is None, timeout=ENGINE_TURN_TIMEOUT, description="the attempt ended"
            )
            cascaded.append(True)

        return await steer(self, handle, before_abort=cascade)

    monkeypatch.setattr(KernelConversation, "steer", create_autospec(steer, side_effect=steer_through_a_cascade))
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserInject(text="do this instead", injection_id="s1", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert cascaded == [True]
    assert not [event for event in started.events if isinstance(event, Error)]
    # The steered pass settled and the next went on, without the message the user took back.
    assert len(_turn_events(started.events, InvocationSteered)) == 1
    assert client.call_count == 2
    second = _request(client, 1)
    assert [message.role for message in second] == ["user", "assistant", "tool"]
    assert _inject_results(started, "do this instead") == [False]
    assert started.engine.state is EngineState.IDLE


class _FailingAfterToolClient(MockChatClient):
    """The model call after the held tool's result fails: a terminal error, not retried."""

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterable[ChatResponseUpdate]:
        if self.call_count == 2:
            raise FrameworkBoom("the call after the tool fails")
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update


async def test_a_steered_pass_that_fails_while_sub_agents_stop_ends_in_the_steer_not_a_failure(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass is given up once the steer is accepted: an error it meets before the interrupt ends no turn."""
    held = _HeldTool()
    client = _FailingAfterToolClient(
        responses=[
            MockResponse(tool_calls=[("hold", "c1", {"message": "x"})]),
            MockResponse(text="unused"),
            MockResponse(text="Went on."),
        ]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[held.tool])
    steer = KernelConversation.steer

    async def steer_through_a_cascade(
        self: KernelConversation, handle: PassHandle, *, before_abort: Callable[[], Awaitable[None]]
    ) -> AbortResult:
        async def cascade() -> None:
            await before_abort()
            # While sub-agents stop, the tool ends and the pass's next model call fails.
            held.release.set()
            await wait_for(
                lambda: self._attempt_handle.task is None, timeout=ENGINE_TURN_TIMEOUT, description="the attempt ended"
            )

        return await steer(self, handle, before_abort=cascade)

    monkeypatch.setattr(KernelConversation, "steer", create_autospec(steer, side_effect=steer_through_a_cascade))
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        await started.bus.publish(UserInject(text="do this instead", injection_id="s1", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert client.call_count == 3
    assert not [event for event in started.events if isinstance(event, Error)]
    assert len(_turn_events(started.events, InvocationSteered)) == 1
    third = _request(client, 2)
    assert _said(third)[-1] == ("user", "do this instead")
    assert _results(third) == ["held: x"]
    assert _inject_results(started, "do this instead") == [True]
    assert started.engine.state is EngineState.IDLE


async def test_a_stop_while_the_steered_pass_gives_up_its_answer_ends_the_turn_without_it(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The steer already gave the answer up on screen: a Stop then ends the turn, and history drops it too."""
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    stopped: list[bool] = []

    async def stop_once(_event: InvocationContentAbandoned) -> None:
        if not stopped:
            stopped.append(True)
            await started.bus.publish(UserInterrupt())

    await started.bus.subscribe(InvocationContentAbandoned, stop_once)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()

    assert stopped == [True]
    assert client.call_count == 1
    assert started.engine.state is EngineState.INTERRUPTED
    assert _inject_results(started, "do this instead") == [False]
    (cut_off, *_rest) = _turn_events(started.events, InvocationContentDelta)
    (withdrawn,) = _turn_events(started.events, InvocationAnswerWithdrawn)
    assert withdrawn.segment_ids == (cut_off.segment_id,)
    saved = await _saved(started)
    unmarked = [message for message in saved if _marker(message) is None]
    assert [(message["role"], _texts(message)) for message in unmarked] == [("user", ["go"])]


class _HeldCascade:
    """Sub-agent tools whose first abort cascade the test holds, as children slow to stop do."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cascades = 0

    async def cascade_abort_all(self) -> None:
        self.cascades += 1
        if self.cascades == 1:
            self.entered.set()
            await self.release.wait()

    def tool_names(self) -> list[str]:
        return []

    def finalize_pending_cleanups(self) -> None:
        return None


async def test_a_steer_accepted_while_stop_waits_for_sub_agents_ends_the_turn_after_its_pass(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop holds the turn before it waits for sub-agents to stop: a steer it meets there starts no next pass."""
    held = _HeldTool()
    client = MockChatClient(
        responses=[MockResponse(tool_calls=[("hold", "c1", {"message": "x"})]), MockResponse(text="never")]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS, tools=[held.tool])
    sub_agents = _HeldCascade()
    install_loaded_agent(started.engine, sub_agent_tools=sub_agents)
    stops: list[asyncio.Task[None]] = []
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_entered(started, held)
        stop = asyncio.create_task(started.bus.publish(UserInterrupt()))
        stops.append(stop)
        await wait_for(
            lambda: sub_agents.entered.is_set() or stop.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="Stop waiting for sub-agents",
        )
        assert not stop.done()
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        # The steered pass settles and the turn ends while Stop still waits.
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
        assert not stop.done()
    finally:
        sub_agents.release.set()
        held.release.set()
        for stop in stops:
            await stop

    assert sub_agents.cascades == 2
    assert len(_turn_events(started.events, InvocationSteered)) == 1
    assert held.cancelled == ["x"]
    assert client.call_count == 1
    assert started.engine.state is EngineState.INTERRUPTED
    assert _inject_results(started, "do this instead") == [False]


async def test_a_shutdown_while_the_steered_pass_settles_starts_no_next_pass(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    settle = TurnFinalizer.settle_steered_pass
    shutdowns: list[asyncio.Task[None]] = []

    async def shut_down_while_settling(self: TurnFinalizer, inputs: Any, *, withdraw: Any) -> None:
        await settle(self, inputs, withdraw=withdraw)
        shutdowns.append(asyncio.create_task(started.engine.shutdown()))
        # Between the passes the shutdown finds no pass running to abort: it waits for the turn to end.
        await wait_for(
            lambda: started.engine.state is EngineState.SHUTTING_DOWN,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the shutdown under way",
        )

    monkeypatch.setattr(
        TurnFinalizer, "settle_steered_pass", create_autospec(settle, side_effect=shut_down_while_settling)
    )
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await wait_for(lambda: bool(shutdowns), timeout=ENGINE_TURN_TIMEOUT, description="the shutdown started")
        await asyncio.wait_for(shutdowns[0], timeout=ENGINE_TURN_TIMEOUT)
    finally:
        client.release.set()

    assert client.call_count == 1


async def test_a_shutdown_while_the_steered_pass_gives_up_its_answer_still_settles_it_as_steered(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the steer gave up on screen stays given up in history, though closing took over the pass's cause."""
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, event_types=_STEER_EVENTS)
    shutdowns: list[asyncio.Task[None]] = []

    async def shut_down_once(_event: InvocationContentAbandoned) -> None:
        if shutdowns:
            return
        conversation = started.engine.current.require_loaded().bindings.backend
        shutdowns.append(asyncio.create_task(started.engine.shutdown()))
        await wait_for(
            lambda: conversation.active_abort_cause is AbortCause.OWNER_CLOSE,
            timeout=ENGINE_TURN_TIMEOUT,
            description="closing took over the steered pass",
        )

    await started.bus.subscribe(InvocationContentAbandoned, shut_down_once)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserMessage(text="do this instead", steer=True))
        await wait_for(lambda: bool(shutdowns), timeout=ENGINE_TURN_TIMEOUT, description="the shutdown started")
        await asyncio.wait_for(shutdowns[0], timeout=ENGINE_TURN_TIMEOUT)
    finally:
        client.release.set()

    assert client.call_count == 1
    (cut_off, *_rest) = _turn_events(started.events, InvocationContentDelta)
    (withdrawn,) = _turn_events(started.events, InvocationAnswerWithdrawn)
    assert withdrawn.segment_ids == (cut_off.segment_id,)
    saved = await _saved(started)
    unmarked = [message for message in saved if _marker(message) is None]
    assert [(message["role"], _texts(message)) for message in unmarked] == [("user", ["go"])]
