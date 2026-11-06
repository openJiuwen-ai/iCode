# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""How a main turn shows its model output while it arrives.

The pass's content projector publishes answer text as ``InvocationContentDelta``
events while the provider streams it, one segment per run of same-channel text
in one response, each closed by an ``InvocationContentFinished``; the final
``InvocationMessage`` then completes the turn without repeating the text. A
blocking response goes out whole when it lands. Shown text is never taken back:
a failure or an interrupt after it ends the turn without sending the request
again, and the saved history keeps the partial answer ahead of the turn's
status marker. ``icode run`` shows none of it while it streams, so there the
request is sent again and the text the retry gave up is neither shown nor kept.
A bare Continue later drops the kept answer to ask for it again, and names its
segments in an ``InvocationAnswerWithdrawn`` before it sends that request.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.errors import ProviderResponseError, is_retryable
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationRetryAttempt,
    InvocationToolCallStart,
    UserInterrupt,
    UserMessage,
    UserRetry,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.retry import StreamStall
from chrys.kernel import ChatResponse, ChatResponseUpdate, Content, ContextProvider
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.content_projection import ContentProjector
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import echo_tool, make_mock_settings_and_registry
from tests.support.tool_packs import install_tools
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from chrys.orchestration.engine.engine import AgentEngine

_EVENT_TYPES: tuple[type, ...] = (
    InvocationAnswerWithdrawn,
    InvocationContentAbandoned,
    InvocationContentDelta,
    InvocationContentFinished,
    InvocationMessage,
    InvocationToolCallStart,
    InvocationRetryAttempt,
    Error,
)


class _HeldStreamClient(MockChatClient):
    """Holds its first stream after the first text update until released, then continues or fails.

    *then* streams right after that text, before the hold. *hold_after* names
    the content type whose first update the stream holds after.
    """

    def __init__(
        self,
        responses: list[MockResponse],
        *,
        failure: BaseException | None = None,
        then: tuple[Content, ...] = (),
        hold_after: str = "text",
    ) -> None:
        super().__init__(responses=responses)
        self.shown = asyncio.Event()
        self.release = asyncio.Event()
        self._failure = failure
        self._then = then
        self._hold_after = hold_after
        self._held = False

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: Any
    ) -> AsyncIterator[ChatResponseUpdate]:
        hold, self._held = not self._held, True
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update
            if hold and any(content.type == self._hold_after for content in update.contents):
                if self._then:
                    yield ChatResponseUpdate(role="assistant", contents=list(self._then), model=model_id)
                # Resumed only once the caller asks for the next update: the first one was handled.
                hold = False
                self.shown.set()
                await self.release.wait()
                if self._failure is not None:
                    raise self._failure


@dataclass(slots=True)
class _Started:
    engine: AgentEngine
    bus: EventBus
    events: list[object]
    store: JsonFileStateStore


async def _start(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    client: MockChatClient,
    *,
    stream: bool = True,
    surface: SessionSurface | None = None,
    service_side: bool = False,
) -> _Started:
    events: list[object] = []
    bus = EventBus()
    for cls in _EVENT_TYPES:
        await bus.subscribe(cls, lambda event, _events=events: collect_events(_events, event))
    settings, model_registry = make_mock_settings_and_registry(stream=stream)
    if service_side:
        # Responses with `store: true`: the provider keeps the conversation, so a retry runs the whole pass again.
        model_registry = ModelProfileRegistry()
        model_registry.register(
            ModelProfile(
                id="mock-profile",
                name="mock",
                provider="openai",
                api_style="responses",
                model_id="gpt-mock",
                stream=stream,
                max_context_tokens=100_000,
                chat_options='{"store": true}',
            )
        )

    async def _create_client(_settings: object = None, **kwargs: Any) -> MockChatClient:
        # A wire client numbers tool batches through the build's callbacks; the mock needs them handed over.
        client._on_intermediate_text = kwargs.get("on_intermediate_text")
        return client

    monkeypatch.setattr(builder_module, "create_client", _create_client)
    install_tools(monkeypatch, [echo_tool])
    store = JsonFileStateStore(tmp_path)
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
        surface=surface,
    )
    await engine.start(_PROFILE)
    return _Started(engine=engine, bus=bus, events=events, store=store)


def _turn_events[E](events: list[object], cls: type[E]) -> list[E]:
    return [event for event in events if isinstance(event, cls) and event.origin.kind == "turn"]


async def _wait_shown(started: _Started, client: _HeldStreamClient) -> None:
    """Wait until the held stream showed its first text, surfacing a run that ended first."""

    def _shown_or_ended() -> bool:
        task = started.engine.turns.turn_state.lease.run_task
        return client.shown.is_set() or (task is not None and task.done())

    await wait_for(_shown_or_ended, timeout=ENGINE_TURN_TIMEOUT, description="the stream's first text shown")
    assert client.shown.is_set(), "the run ended before its stream showed any text"


async def _saved_assistant_messages(started: _Started) -> list[dict[str, Any]]:
    saved = await started.store.load_session_raw(started.engine.session.session_id)
    assert saved is not None
    return [message for message in saved if message["role"] == "assistant"]


def _texts(message: dict[str, Any]) -> list[str]:
    return [content["text"] for content in message["contents"] if content["type"] == "text"]


def _marker(message: dict[str, Any]) -> object:
    return message["additional_properties"].get(HistoryMarkerKind.KEY)


async def test_answer_text_arrives_while_streaming_and_the_final_message_follows(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _HeldStreamClient([MockResponse(text="Hello world", chunk_size=6)])
    started = await _start(agent_engine, tmp_path, monkeypatch, client)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)

        # Mid-stream: the first chunk is out, nothing closed or completed yet.
        assert [delta.text for delta in _turn_events(started.events, InvocationContentDelta)] == ["Hello "]
        assert _turn_events(started.events, InvocationContentFinished) == []
        assert _turn_events(started.events, InvocationMessage) == []
    finally:
        client.release.set()
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    deltas = _turn_events(started.events, InvocationContentDelta)
    assert [(delta.channel, delta.text) for delta in deltas] == [("answer", "Hello "), ("answer", "world")]
    assert len({delta.segment_id for delta in deltas}) == 1
    (finished,) = _turn_events(started.events, InvocationContentFinished)
    assert (finished.segment_id, finished.status) == (deltas[0].segment_id, "completed")
    (final,) = _turn_events(started.events, InvocationMessage)
    assert (final.is_final, final.text) == (True, "Hello world")
    assert started.events.index(deltas[-1]) < started.events.index(finished) < started.events.index(final)
    assert client.call_count == 1


async def test_text_before_a_tool_call_is_its_own_segment_closed_before_the_tool_starts(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MockChatClient(
        responses=[
            MockResponse(text="Let me check", tool_calls=[("echo", "c1", {"message": "x"})]),
            MockResponse(text="All done"),
        ]
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client)
    await started.bus.publish(UserMessage(text="go"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    narration, answer = _turn_events(started.events, InvocationContentDelta)
    assert (narration.text, answer.text) == ("Let me check", "All done")
    assert narration.segment_id != answer.segment_id
    first_closed, last_closed = _turn_events(started.events, InvocationContentFinished)
    assert [first_closed.segment_id, last_closed.segment_id] == [narration.segment_id, answer.segment_id]
    (tool_start,) = _turn_events(started.events, InvocationToolCallStart)
    assert started.events.index(first_closed) < started.events.index(tool_start) < started.events.index(answer)
    (final,) = _turn_events(started.events, InvocationMessage)
    assert (final.is_final, final.text) == (True, "All done")

    # The narration stays on its batch's message; no sidecar repeats text the message shows.
    calling, *_ = await _saved_assistant_messages(started)
    assert _texts(calling) == ["Let me check"]
    assert calling["additional_properties"]["_batch_id"] == 1
    assert "_intermediate_text" not in calling["additional_properties"]


async def test_a_blocking_response_goes_out_whole_when_it_lands(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MockChatClient(responses=[MockResponse(text="line1\nline2")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, stream=False)
    await started.bus.publish(UserMessage(text="go"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    (delta,) = _turn_events(started.events, InvocationContentDelta)
    assert delta.text == "line1\nline2"
    (finished,) = _turn_events(started.events, InvocationContentFinished)
    (final,) = _turn_events(started.events, InvocationMessage)
    assert (final.is_final, final.text) == (True, "line1\nline2")
    assert started.events.index(delta) < started.events.index(finished) < started.events.index(final)


@pytest.mark.parametrize("surface", [None, SessionSurface.TUI, SessionSurface.ACP], ids=["no_surface", "tui", "acp"])
async def test_a_failure_after_shown_text_is_not_retried_and_the_partial_answer_is_kept(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, surface: SessionSurface | None
) -> None:
    """The TUI and ACP stream a reply as it arrives: sending the request again would show it twice."""
    failure = ProviderResponseError("stream_truncated", "the connection dropped", retryable=True)
    # Every wire lane would resend this failure had the stream shown nothing.
    assert is_retryable(failure)
    client = _HeldStreamClient(
        [MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="must not run")], failure=failure
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client, surface=surface)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
    finally:
        client.release.set()
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert client.call_count == 1
    assert _turn_events(started.events, InvocationRetryAttempt) == []
    assert len([event for event in started.events if isinstance(event, Error)]) == 1
    assert _turn_events(started.events, InvocationMessage) == []
    (delta,) = _turn_events(started.events, InvocationContentDelta)
    assert delta.text == "Partial "
    (finished,) = _turn_events(started.events, InvocationContentFinished)
    assert (finished.segment_id, finished.status) == (delta.segment_id, "failed")

    answer, error_marker, turn_marker = await _saved_assistant_messages(started)
    assert _texts(answer) == ["Partial "]
    assert _marker(answer) is None
    assert _marker(error_marker) == HistoryMarkerKind.INTERRUPTED
    assert error_marker["additional_properties"]["_interrupted_by"] == "error"
    assert _marker(turn_marker) == HistoryMarkerKind.TURN


_DROPPED = ProviderResponseError("stream_truncated", "the connection dropped", retryable=True)


def _answers(messages: list[dict[str, Any]]) -> list[list[str]]:
    return [_texts(message) for message in messages if _marker(message) is None]


@pytest.mark.parametrize("service_side", [False, True], ids=["local", "service_side"])
async def test_icode_run_sends_a_request_again_after_text_it_never_printed(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, service_side: bool
) -> None:
    """`icode run` holds a reply until it knows what the text is, so a reply cut off midway showed nothing."""
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    client = _HeldStreamClient(
        [MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="Whole answer")], failure=_DROPPED
    )
    client.release.set()
    started = await _start(
        agent_engine, tmp_path, monkeypatch, client, surface=SessionSurface.CLI, service_side=service_side
    )
    await started.bus.publish(UserMessage(text="go"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert client.call_count == 2
    assert [event for event in started.events if isinstance(event, Error)] == []
    (retry,) = _turn_events(started.events, InvocationRetryAttempt)
    assert retry.scope == ("run" if service_side else "wire")
    cut = _turn_events(started.events, InvocationContentDelta)[0]
    assert cut.text == "Partial "
    (abandoned,) = _turn_events(started.events, InvocationContentAbandoned)
    assert abandoned.segment_ids == (cut.segment_id,)
    cut_finished, answer_finished = _turn_events(started.events, InvocationContentFinished)
    assert (cut_finished.segment_id, cut_finished.status, answer_finished.status) == (
        cut.segment_id,
        "failed",
        "completed",
    )
    assert started.events.index(retry) < started.events.index(abandoned) < started.events.index(cut_finished)
    (final,) = _turn_events(started.events, InvocationMessage)
    assert final.text == "Whole answer"
    assert _answers(await _saved_assistant_messages(started)) == [["Whole answer"]]


async def test_icode_run_gives_up_every_segment_of_the_reply_it_sends_again(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reasoning after the cut text completed that text's segment: the retry gives it up all the same."""
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (0,))
    client = _HeldStreamClient(
        [MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="Whole answer")],
        failure=_DROPPED,
        then=(Content.from_text_reasoning(text="hmm"),),
    )
    client.release.set()
    started = await _start(agent_engine, tmp_path, monkeypatch, client, surface=SessionSurface.CLI)
    await started.bus.publish(UserMessage(text="go"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert client.call_count == 2
    cut, thought = _turn_events(started.events, InvocationContentDelta)[:2]
    assert [(cut.channel, cut.text), (thought.channel, thought.text)] == [("answer", "Partial "), ("reasoning", "hmm")]
    (abandoned,) = _turn_events(started.events, InvocationContentAbandoned)
    assert abandoned.segment_ids == (cut.segment_id, thought.segment_id)
    assert [event.status for event in _turn_events(started.events, InvocationContentFinished)] == [
        "completed",
        "failed",
        "completed",
    ]
    assert _answers(await _saved_assistant_messages(started)) == [["Whole answer"]]


@pytest.mark.parametrize("service_side", [False, True], ids=["local", "service_side"])
async def test_icode_run_falls_back_to_a_blocking_request_after_a_stall_past_text_it_never_printed(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, service_side: bool
) -> None:
    # The stream stalls right after its first text, as the watchdog reports a stall.
    client = _HeldStreamClient(
        [MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="Whole answer")],
        failure=StreamStall("Streaming response produced no progress"),
    )
    client.release.set()
    started = await _start(
        agent_engine, tmp_path, monkeypatch, client, surface=SessionSurface.CLI, service_side=service_side
    )
    # No stall retry: the stalled stream goes straight to its blocking fallback.
    started.engine.current.require_loaded().bindings._max_retries_override = 0
    await started.bus.publish(UserMessage(text="go"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert client.call_count == 2
    cut = _turn_events(started.events, InvocationContentDelta)[0]
    (abandoned,) = _turn_events(started.events, InvocationContentAbandoned)
    assert abandoned.segment_ids == (cut.segment_id,)
    (final,) = _turn_events(started.events, InvocationMessage)
    assert final.text == "Whole answer"
    assert _answers(await _saved_assistant_messages(started)) == [["Whole answer"]]


@pytest.mark.parametrize("service_side", [False, True], ids=["local", "service_side"])
async def test_an_interrupt_during_the_retry_backoff_keeps_the_text_icode_run_held(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, service_side: bool
) -> None:
    """The retry has not given the reply up yet: the interrupted turn keeps it, as a failed one would."""
    monkeypatch.setattr(TurnBindings, "_BACKOFF_SCHEDULE", (30,))
    client = _HeldStreamClient(
        [MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="must not run")], failure=_DROPPED
    )
    client.release.set()
    started = await _start(
        agent_engine, tmp_path, monkeypatch, client, surface=SessionSurface.CLI, service_side=service_side
    )
    await started.bus.publish(UserMessage(text="go"))

    def _announced_or_ended() -> bool:
        task = started.engine.turns.turn_state.lease.run_task
        return bool(_turn_events(started.events, InvocationRetryAttempt)) or (task is not None and task.done())

    await wait_for(_announced_or_ended, timeout=ENGINE_TURN_TIMEOUT, description="the retry announced")
    assert _turn_events(started.events, InvocationRetryAttempt), "the run ended before it announced a retry"
    await started.bus.publish(UserInterrupt())
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert client.call_count == 1
    assert _turn_events(started.events, InvocationContentAbandoned) == []
    answer, interrupted_marker, _turn_marker = await _saved_assistant_messages(started)
    assert _texts(answer) == ["Partial "]
    assert _marker(interrupted_marker) == HistoryMarkerKind.INTERRUPTED


async def test_an_interrupt_mid_stream_keeps_the_partial_answer(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _HeldStreamClient([MockResponse(text="Partial answer", chunk_size=8)])
    started = await _start(agent_engine, tmp_path, monkeypatch, client)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()

    loaded = started.engine.current.require_loaded()
    assert loaded.bindings.state.was_interrupted
    assert [event for event in started.events if isinstance(event, Error)] == []
    assert _turn_events(started.events, InvocationMessage) == []
    (delta,) = _turn_events(started.events, InvocationContentDelta)
    (finished,) = _turn_events(started.events, InvocationContentFinished)
    assert (finished.segment_id, finished.status) == (delta.segment_id, "interrupted")

    answer, interrupted_marker, turn_marker = await _saved_assistant_messages(started)
    assert _texts(answer) == ["Partial "]
    assert _marker(interrupted_marker) == HistoryMarkerKind.INTERRUPTED
    assert _marker(turn_marker) == HistoryMarkerKind.TURN


async def test_an_interrupt_mid_thinking_keeps_nothing_and_the_next_prompt_drops_the_one_it_followed(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Thinking cut off is not kept, so the prompt it followed got no response: the next prompt drops it.

    The TUI removes that prompt, with the thinking it showed, when the next one is sent.
    """
    client = _HeldStreamClient(
        [MockResponse(reasoning_text="Weighing the options", text="never"), MockResponse(text="Second answer")],
        hold_after="text_reasoning",
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client)
    try:
        await started.bus.publish(UserMessage(text="first question"))
        await _wait_shown(started, client)
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()
    (delta,) = _turn_events(started.events, InvocationContentDelta)
    assert (delta.channel, delta.text) == ("reasoning", "Weighing the options")
    # Only the turn's status markers follow the prompt.
    assert [message for message in await _saved_assistant_messages(started) if _marker(message) is None] == []

    await started.bus.publish(UserMessage(text="second question"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    saved = await started.store.load_session_raw(started.engine.session.session_id)
    assert saved is not None
    prompts = [_texts(message) for message in saved if message["role"] == "user" and _marker(message) is None]
    assert prompts == [["second question"]]
    answers = [_texts(message) for message in saved if message["role"] == "assistant" and _marker(message) is None]
    assert answers == [["Second answer"]]


async def test_a_later_turn_that_runs_no_pass_keeps_no_earlier_partial_answer(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kept answer belongs to the turn that showed it, never to the next failed one."""
    client = _HeldStreamClient([MockResponse(text="Partial answer", chunk_size=8), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()
    interrupts = iter([True])

    def interrupted_before_the_run() -> bool:
        return next(interrupts, False)

    # The second turn is interrupted before its run starts, so it finalizes without a pass.
    monkeypatch.setattr(
        started.engine.turns.turn_state.lease, "consume_pre_executor_interrupt", interrupted_before_the_run
    )
    await started.bus.publish(UserMessage(text="second question"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert client.call_count == 1
    answers = [_texts(message) for message in await _saved_assistant_messages(started) if _marker(message) is None]
    assert answers == [["Partial "]]


@pytest.mark.parametrize(
    ("stream", "calls_tool"),
    # A streamed tool call closed its answer segment before landing: nothing shows while it lands.
    [(True, False), (False, False), (False, True)],
    ids=["streamed-final", "blocking-final", "blocking-tool-call"],
)
async def test_an_interrupt_while_a_landed_answer_shows_keeps_it_once(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, stream: bool, calls_tool: bool
) -> None:
    """The loop records a response only after it shows, so the turn keeps a shown answer cut off there."""
    text = "Let me check" if calls_tool else "Hello world"
    tool_calls = [("echo", "c1", {"message": "x"})] if calls_tool else []
    client = MockChatClient(responses=[MockResponse(text=text, tool_calls=tool_calls), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, stream=stream)
    landing, showing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    response_ready = ContentProjector.response_ready

    async def mark_landing(
        self: ContentProjector,
        response: ChatResponse,
        *,
        shown: Callable[[Content | None], Awaitable[None]] | None = None,
    ) -> None:
        landing.set()
        await response_ready(self, response, shown=shown)

    monkeypatch.setattr(ContentProjector, "response_ready", create_autospec(response_ready, side_effect=mark_landing))

    async def hold_while_landing(event: InvocationContentDelta | InvocationContentFinished) -> None:
        if landing.is_set() and not showing.is_set():
            showing.set()
            await release.wait()

    await started.bus.subscribe(InvocationContentDelta, hold_while_landing)
    await started.bus.subscribe(InvocationContentFinished, hold_while_landing)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await wait_for(showing.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the landed answer showing")
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        release.set()

    assert client.call_count == 1
    answer, interrupted_marker, turn_marker = await _saved_assistant_messages(started)
    assert _texts(answer) == [text]
    assert _marker(interrupted_marker) == HistoryMarkerKind.INTERRUPTED
    assert _marker(turn_marker) == HistoryMarkerKind.TURN


class _HeldAfterRun(ContextProvider):
    """Holds the run's after-providers pass, once the final answer showed, until released."""

    def __init__(self) -> None:
        super().__init__("held-after-run")
        self.holding = asyncio.Event()
        self.release = asyncio.Event()

    async def after_run(self, *, agent: Any, session: Any, context: Any, state: dict[str, Any]) -> None:
        self.holding.set()
        await self.release.wait()


@pytest.mark.parametrize("stored", [False, True], ids=["before-history-stores-it", "after-history-stores-it"])
@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
async def test_an_interrupt_after_the_final_answer_showed_keeps_it_once(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, stream: bool, stored: bool
) -> None:
    """History stores a final answer only once the run's providers run: an interrupt on either side keeps it once."""
    client = MockChatClient(responses=[MockResponse(text="Hello world"), MockResponse(text="never")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, stream=stream)
    providers = started.engine.current.require_loaded().agent.context_providers
    history = next(
        index for index, provider in enumerate(providers) if isinstance(provider, CompressibleHistoryProvider)
    )
    held = _HeldAfterRun()
    # Providers run in reverse order: one listed after history runs before history stores the answer.
    providers.insert(history if stored else history + 1, held)

    def _held_or_ended() -> bool:
        task = started.engine.turns.turn_state.lease.run_task
        return held.holding.is_set() or (task is not None and task.done())

    try:
        await started.bus.publish(UserMessage(text="go"))
        await wait_for(_held_or_ended, timeout=ENGINE_TURN_TIMEOUT, description="the after-providers pass held")
        assert held.holding.is_set(), "the run ended before its after-providers pass"
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        held.release.set()

    assert started.engine.current.require_loaded().bindings.state.was_interrupted
    assert client.call_count == 1
    answer, interrupted_marker, turn_marker = await _saved_assistant_messages(started)
    assert _texts(answer) == ["Hello world"]
    assert _marker(interrupted_marker) == HistoryMarkerKind.INTERRUPTED
    assert _marker(turn_marker) == HistoryMarkerKind.TURN


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
async def test_a_turn_that_ends_well_leaves_no_answer_for_a_later_failed_turn(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    """A final answer waits for history until its pass ends; the turn's end takes it, failed or not."""
    client = MockChatClient(responses=[MockResponse(text="Hello world")])
    started = await _start(agent_engine, tmp_path, monkeypatch, client, stream=stream)

    await started.bus.publish(UserMessage(text="go"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    state = started.engine.current.require_loaded().bindings.state
    assert not state.run_failed and not state.was_interrupted
    assert state.partial_answer == ()


def _search_call() -> Content:
    return Content.from_hosted_tool_call(
        "search-1",
        tool_name="web_search",
        arguments={"query": "Chrys"},
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="start",
    )


async def _interrupt_after_narration_a_hosted_call_and_an_answer(
    agent_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *retry: MockResponse
) -> tuple[_Started, _HeldStreamClient, list[str]]:
    """Interrupt a stream that showed narration, a provider-hosted call and answer text; return its segments."""
    client = _HeldStreamClient(
        [MockResponse(text="Searching."), *retry], then=(_search_call(), Content.from_text("Found "))
    )
    started = await _start(agent_engine, tmp_path, monkeypatch, client)
    try:
        await started.bus.publish(UserMessage(text="go"))
        await _wait_shown(started, client)
        await started.bus.publish(UserInterrupt())
        await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)
    finally:
        client.release.set()
    segments = list(dict.fromkeys(delta.segment_id for delta in _turn_events(started.events, InvocationContentDelta)))
    assert [delta.text for delta in _turn_events(started.events, InvocationContentDelta)] == ["Searching.", "Found "]
    return started, client, segments


async def test_a_bare_continue_withdraws_every_segment_it_drops_before_asking_again(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cut-off response never reached history, so its narration before the hosted call goes with the answer."""
    started, client, segments = await _interrupt_after_narration_a_hosted_call_and_an_answer(
        agent_engine, tmp_path, monkeypatch, MockResponse(text="Found Chrys.")
    )
    assert len(segments) == 2
    requests_at_withdrawal: list[int] = []

    async def count_requests(_event: InvocationAnswerWithdrawn) -> None:
        requests_at_withdrawal.append(client.call_count)

    await started.bus.subscribe(InvocationAnswerWithdrawn, count_requests)

    await started.bus.publish(UserRetry())
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    (withdrawn,) = _turn_events(started.events, InvocationAnswerWithdrawn)
    assert withdrawn.segment_ids == tuple(segments)
    assert requests_at_withdrawal == [1]
    sent, _options = client.call_history[-1]
    assert [message.text for message in sent if message.role == "assistant"] == []
    answers = [_texts(message) for message in await _saved_assistant_messages(started) if _marker(message) is None]
    assert answers == [["Found Chrys."]]


async def test_a_continue_with_a_note_goes_on_from_the_kept_answer_and_withdraws_nothing(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, _client, _segments = await _interrupt_after_narration_a_hosted_call_and_an_answer(
        agent_engine, tmp_path, monkeypatch, MockResponse(text="Going on.")
    )

    await started.bus.publish(UserRetry(text="go on"))
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert _turn_events(started.events, InvocationAnswerWithdrawn) == []


@pytest.mark.parametrize("stop", ["interrupt", "cancel"])
async def test_a_continue_stopped_while_its_withdrawal_is_delivered_still_delivers_it_and_asks_nothing(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    """History dropped the answer already: a frontend that missed the withdrawal would keep showing it."""
    started, client, segments = await _interrupt_after_narration_a_hosted_call_and_an_answer(
        agent_engine, tmp_path, monkeypatch, MockResponse(text="never")
    )
    delivering, release = asyncio.Event(), asyncio.Event()
    delivered: list[tuple[str, ...]] = []

    async def slow_frontend(_event: InvocationAnswerWithdrawn) -> None:
        delivering.set()
        await release.wait()

    async def later_frontend(event: InvocationAnswerWithdrawn) -> None:
        delivered.append(event.segment_ids)

    await started.bus.subscribe(InvocationAnswerWithdrawn, slow_frontend)
    await started.bus.subscribe(InvocationAnswerWithdrawn, later_frontend)

    await started.bus.publish(UserRetry())
    run = started.engine.turns.turn_state.lease.run_task
    assert run is not None
    await wait_for(lambda: delivering.is_set() or run.done(), description="the withdrawal sent")
    assert delivering.is_set()
    if stop == "interrupt":
        await started.bus.publish(UserInterrupt())
    else:
        run.cancel()
    assert not run.done()
    release.set()
    await await_run_task_chain(started.engine, turn_state=started.engine.turns.turn_state)

    assert delivered == [tuple(segments)]
    assert client.call_count == 1
