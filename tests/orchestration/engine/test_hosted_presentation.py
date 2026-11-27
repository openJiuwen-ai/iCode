# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TurnBindings wiring tests for provider-hosted presentation events."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    Error,
    Event,
    InvocationContentDelta,
    InvocationMessage,
    InvocationToolCallArgsUpdated,
    InvocationToolCallResult,
    InvocationToolCallStart,
    InvocationToolCallStatusUpdated,
    UserMessage,
)
from chrys.foundation.tool_result_metadata import TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY
from chrys.kernel import (
    AgentResponse,
    AgentSession,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
)
from chrys.kernel.middleware import ChatContext
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.orchestration.invoker.resources import Conversation
from chrys.service.agent_middleware.events.hosted_tools import (
    FinalTextOp,
    HostedToolResultOp,
    adapt_hosted_tool,
)
from chrys.service.agent_middleware.response_validation import ResponseValidationMiddleware
from chrys.service.llm.mock import MockResponse
from chrys.service.llm.openai_responses.client import OPENAI_RESPONSES
from chrys.service.llm.openai_responses.stream import StreamState
from tests.orchestration.invoker._main_pass import continuation_pass
from tests.support.event_capture import capture_event_sequence
from tests.support.pipeline_helpers import create_test_engine
from tests.support.scripted_clients import HostedMockResponse
from tests.support.waiting import await_run_task_chain, wait_for


def _responses_events(*, before: str = "", after: str = "", status: str = "completed") -> list[SimpleNamespace]:
    """Build a small Responses event fixture for the production normalizer."""
    item = SimpleNamespace(
        type="web_search_call",
        id="ws_1",
        status=status,
        action=SimpleNamespace(type="search", query="Chrys"),
    )
    events: list[SimpleNamespace] = []
    if before:
        events.append(
            SimpleNamespace(
                type="response.output_text.delta",
                delta=before,
                item_id="msg_before",
                output_index=0,
                content_index=0,
            )
        )
    events.extend(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=1,
                item=SimpleNamespace(type="web_search_call", id="ws_1", status="in_progress", action=None),
            ),
            SimpleNamespace(type="response.output_item.done", output_index=1, item=item),
        ]
    )
    if after:
        events.append(
            SimpleNamespace(
                type="response.output_text.delta",
                delta=after,
                item_id="msg_after",
                output_index=2,
                content_index=0,
            )
        )
    return events


def _response_completed_with_usage() -> SimpleNamespace:
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            id="resp_1",
            conversation=None,
            model="gpt-test",
            created_at=0,
            status="completed",
            incomplete_details=None,
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=4,
                total_tokens=14,
                input_tokens_details=None,
                output_tokens_details=None,
            ),
        ),
    )


def _plain_text_then_reasoning_events() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            type="response.output_text.delta",
            delta="Final answer.",
            item_id="msg_final",
            output_index=0,
            content_index=0,
        ),
        SimpleNamespace(
            type="response.output_item.added",
            output_index=1,
            item=SimpleNamespace(
                type="reasoning",
                id="reasoning_1",
                content=[SimpleNamespace(text="internal reasoning")],
                summary=[],
                encrypted_content=None,
            ),
        ),
    ]


class _ValidatedAgent:
    """Minimal agent surface that runs parsed updates through validation."""

    def __init__(
        self,
        validation: ResponseValidationMiddleware,
        events: Sequence[SimpleNamespace],
        *,
        wait_after_updates: bool = False,
        explode_after_updates: bool = False,
    ) -> None:
        self.client = SimpleNamespace(STORES_BY_DEFAULT=False, FORCES_STATELESS=False)
        self._validation = validation
        self._events = list(events)
        self._wait_after_updates = wait_after_updates
        self._explode_after_updates = explode_after_updates
        self.wire_calls = 0

    async def run(self, messages: Sequence[Message], *, stream: bool = False, **_kwargs: Any) -> AgentResponse[Any]:
        assert not stream
        context = ChatContext(
            client=self.client,
            messages=list(messages),
            options={},
            stream=True,
            kwargs={"client_kwargs": {}},
        )

        async def _raw_updates() -> AsyncIterator[ChatResponseUpdate]:
            state = StreamState({}, model="gpt-test", variant=OPENAI_RESPONSES)
            for event in self._events:
                yield state.update_for(event)
            if self._explode_after_updates:
                raise RuntimeError("wire exploded")
            if self._wait_after_updates:
                await asyncio.Event().wait()

        async def _call_next() -> None:
            self.wire_calls += 1
            context.result = ResponseStream(_raw_updates(), finalizer=ChatResponse.from_updates)

        await self._validation.process(context, _call_next)
        assert isinstance(context.result, ResponseStream)
        _ = [update async for update in context.result]
        final = await context.result.get_final_response()
        return AgentResponse(messages=final.messages, finish_reason=final.finish_reason)


async def _executor_fixture(
    raw_events: Sequence[SimpleNamespace],
    *,
    wait_after_updates: bool = False,
    explode_after_updates: bool = False,
) -> tuple[TurnBindings, _ValidatedAgent, list[Event]]:
    bus = EventBus()
    events: list[Event] = []

    async def _collect(event: Event) -> None:
        events.append(event)

    for event_type in (
        InvocationMessage,
        Error,
        InvocationToolCallStart,
        InvocationToolCallArgsUpdated,
        InvocationToolCallStatusUpdated,
        InvocationToolCallResult,
    ):
        await bus.subscribe(event_type, _collect)

    validation = ResponseValidationMiddleware()
    agent = _ValidatedAgent(
        validation,
        raw_events,
        wait_after_updates=wait_after_updates,
        explode_after_updates=explode_after_updates,
    )
    executor = TurnBindings(
        conversation=Conversation(),
        agent=agent,  # type: ignore[arg-type]
        session=AgentSession(),
        event_bus=bus,
        approval_middleware=MagicMock(),
        ask_user_middleware=MagicMock(),
        injection_middleware=MagicMock(),
        response_validation_middleware=validation,
    )
    return executor, agent, events


def _hosted_image_item() -> Content:
    """A completed hosted image result stamped with its provider item id, as every hosted adapter stamps it."""
    return Content.from_image_generation_tool_result(
        image_id="image-1",
        outputs=[Content.from_uri("data:image/png;base64,QUJD", media_type="image/png")],
        hosted_provider="openai",
        provider_item_id="ig_1",
        provider_phase="terminal",
        provider_status="completed",
    )


def _turn_messages(events: Sequence[Event]) -> list[tuple[str, bool]]:
    return [
        (event.text, event.is_final)
        for event in events
        if isinstance(event, InvocationMessage) and event.origin.kind == "turn"
    ]


@pytest.mark.asyncio
async def test_failed_hosted_sequence_publishes_call_result_then_final() -> None:
    executor, _agent, events = await _executor_fixture(
        _responses_events(before="Checking sources.", after="No result was available.", status="failed")
    )

    await continuation_pass(executor, [Message("user", ["search"])])

    start = next(
        index
        for index, event in enumerate(events)
        if (isinstance(event, InvocationToolCallStart) and event.origin.kind == "turn")
    )
    result = next(
        index
        for index, event in enumerate(events)
        if (isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn")
    )
    final = next(
        index
        for index, event in enumerate(events)
        if (isinstance(event, InvocationMessage) and event.origin.kind == "turn")
        and event.is_final
        and event.text == "No result was available."
    )
    failed = events[result]
    assert isinstance(failed, InvocationToolCallResult) and failed.origin.kind == "turn"
    assert start < result < final
    assert failed.provider_status == "failed"
    assert failed.result.startswith("Error: ")
    # The narration before the search is the content projector's: the bridge sends only the completion.
    assert _turn_messages(events) == [("No result was available.", True)]


@pytest.mark.parametrize(
    ("provider_text", "expected_result", "expected_synthesized"),
    [
        ("", "Error: Provider-hosted tool failed.", True),
        ("provider failed", "Error: provider failed", False),
    ],
    ids=["synthesized", "provider-originated"],
)
@pytest.mark.asyncio
async def test_published_hosted_result_carries_synthesized_failure_signal_only_for_fallback(
    provider_text: str,
    expected_result: str,
    expected_synthesized: bool,
) -> None:
    executor, _agent, events = await _executor_fixture([])
    executor._bound_emitter = executor._invocation_publishers.bind(executor.inputs.origin)
    result = Content.from_search_tool_result(
        "search_semantic",
        tool_name="web_search",
        result=provider_text,
        status="",
        hosted_family="search",
        hosted_provider="openai",
        provider_status="",
        additional_properties={"is_error": True},
    )
    view = adapt_hosted_tool(None, result)

    await executor._publish_hosted_operation(
        HostedToolResultOp(0, "hosted:semantic", view, (0, 0, 0)), emitter=executor._emitter
    )

    published = next(
        event for event in events if (isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn")
    )
    assert published.result == expected_result
    assert (published.metadata.get(TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY) is True) is expected_synthesized


@pytest.mark.parametrize(
    ("operation", "expected"),
    [
        (FinalTextOp("Answer."), ("Answer.", False)),
        (FinalTextOp("", structured_output_completed=True), ("", True)),
    ],
    ids=["text", "structured-only"],
)
@pytest.mark.asyncio
async def test_final_text_op_publishes_the_turn_completion_message(
    operation: FinalTextOp, expected: tuple[str, bool]
) -> None:
    executor, _agent, events = await _executor_fixture([])
    executor._bound_emitter = executor._invocation_publishers.bind(executor.inputs.origin)

    await executor._publish_hosted_operation(operation, emitter=executor._emitter)

    (message,) = events
    assert isinstance(message, InvocationMessage) and message.origin.kind == "turn"
    assert (message.text, message.structured_output_completed) == expected
    assert message.is_final is True


@pytest.mark.asyncio
async def test_output_item_added_and_done_publish_one_start_through_hook_and_bridge() -> None:
    executor, _agent, events = await _executor_fixture(_responses_events(after="Done."))

    await continuation_pass(executor, [Message("user", ["search"])])

    starts = [event for event in events if (isinstance(event, InvocationToolCallStart) and event.origin.kind == "turn")]
    results = [
        event for event in events if (isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn")
    ]
    assert len(starts) == len(results) == 1
    assert starts[0].call_id == results[0].call_id
    assert starts[0].provider_call_id == "ws_1"


@pytest.mark.asyncio
async def test_hosted_card_ids_stay_unique_across_rebuilt_bindings() -> None:
    # An engine rebuild (model or agent switch) builds new TurnBindings; frontends key cards by call_id.
    call_ids: list[str] = []
    for _ in range(2):
        executor, _agent, events = await _executor_fixture(_responses_events(after="Done."))
        await continuation_pass(executor, [Message("user", ["search"])])
        (start,) = [event for event in events if isinstance(event, InvocationToolCallStart)]
        call_ids.append(start.call_id)

    assert call_ids[0] != call_ids[1]


@pytest.mark.asyncio
async def test_response_completed_usage_publishes_only_the_text_after_the_hosted_output() -> None:
    raw_events = [
        *_responses_events(before="Checking sources.", after="Final answer."),
        _response_completed_with_usage(),
    ]
    executor, _agent, events = await _executor_fixture(raw_events)

    await continuation_pass(executor, [Message("user", ["search"])])

    assert _turn_messages(events) == [("Final answer.", True)]


@pytest.mark.asyncio
async def test_plain_reasoning_publishes_only_the_final_text() -> None:
    executor, _agent, events = await _executor_fixture(_plain_text_then_reasoning_events())

    await continuation_pass(executor, [Message("user", ["answer"])])

    assert _turn_messages(events) == [("Final answer.", True)]


@pytest.mark.asyncio
async def test_search_without_final_text_fails_without_blank_agent_message() -> None:
    # A search-only response is rejected but not blank, so nothing resends it: the turn fails.
    executor, agent, events = await _executor_fixture(_responses_events())

    await continuation_pass(executor, [Message("user", ["search"])])

    assert agent.wire_calls == 1
    assert executor.state.run_failed
    assert [
        event
        for event in events
        if (isinstance(event, InvocationMessage) and event.origin.kind == "turn") and event.is_final
    ] == []
    assert len([event for event in events if isinstance(event, Error)]) == 1


@pytest.mark.asyncio
async def test_interrupt_terminalizes_running_hosted_card() -> None:
    raw_events = _responses_events(before="Checking sources.")[:2]
    executor, _agent, events = await _executor_fixture(raw_events, wait_after_updates=True)
    task = asyncio.create_task(continuation_pass(executor, [Message("user", ["search"])]))
    await wait_for(
        lambda: (
            task.done()
            or any((isinstance(event, InvocationToolCallStart) and event.origin.kind == "turn") for event in events)
        ),
        description="the hosted start",
    )
    assert not task.done()

    await executor.interrupt()
    await task

    interrupted = [
        event
        for event in events
        if (isinstance(event, InvocationToolCallStatusUpdated) and event.origin.kind == "turn")
        and event.status == "interrupted"
    ]
    assert len(interrupted) == 1
    assert executor.state.was_interrupted
    # An interrupted pass publishes no completion; the narration it streamed is the projector's.
    assert _turn_messages(events) == []


@pytest.mark.asyncio
async def test_terminal_executor_error_terminalizes_running_hosted_card() -> None:
    # A wire failure mid-search must fail the visible card before the run
    # error goes out: ACP clients have no error banner tied to the card and
    # would otherwise show it in-progress forever.
    raw_events = _responses_events()[:1]
    executor, _agent, events = await _executor_fixture(raw_events, explode_after_updates=True)

    await continuation_pass(executor, [Message("user", ["search"])])

    assert executor.state.run_failed
    failed = next(
        index
        for index, event in enumerate(events)
        if (isinstance(event, InvocationToolCallStatusUpdated) and event.origin.kind == "turn")
        and event.status == "failed"
    )
    error = next(index for index, event in enumerate(events) if isinstance(event, Error))
    start = next(
        index
        for index, event in enumerate(events)
        if (isinstance(event, InvocationToolCallStart) and event.origin.kind == "turn")
    )
    assert start < failed < error


@pytest.mark.asyncio
async def test_local_function_tools_keep_tool_event_middleware_as_single_publisher(tmp_path: Any) -> None:
    context = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "local_1", {"message": "hello"})]), MockResponse(text="done")],
        tmp_path,
    )
    try:
        await context.send_message("go")

        starts = [
            event
            for event in context.events
            if (isinstance(event, InvocationToolCallStart) and event.origin.kind == "turn")
        ]
        results = [
            event
            for event in context.events
            if (isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn")
        ]
        assert len(starts) == len(results) == 1
        assert starts[0].provider_hosted is False
        assert results[0].provider_hosted is False
    finally:
        await context.cleanup()


@pytest.mark.asyncio
async def test_non_hosted_response_keeps_existing_final_text_path(tmp_path: Any) -> None:
    context = await create_test_engine([MockResponse(text="unchanged final text")], tmp_path)
    try:
        await context.send_message("go")

        finals = [
            event.text
            for event in context.events
            if (isinstance(event, InvocationMessage) and event.origin.kind == "turn") and event.is_final
        ]
        assert finals == ["unchanged final text"]
        assert not any(
            (isinstance(event, InvocationToolCallStart) and event.origin.kind == "turn") for event in context.events
        )
    finally:
        await context.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
@pytest.mark.parametrize(
    "call",
    [("echo", "local_1", {"message": "hello"}), ("no_such_tool", "c-1", {})],
    ids=["tool-start", "unknown-tool"],
)
async def test_narration_before_hosted_output_is_published_once(
    tmp_path: Any, call: tuple[str, str, dict[str, Any]], stream: bool
) -> None:
    """The narration streams as answer content before the next response's hosted output, and only once."""
    context = await create_test_engine(
        [
            MockResponse(text="Let me look. ", tool_calls=[call]),
            HostedMockResponse(text="Here it is.", hosted=[_hosted_image_item()]),
        ],
        tmp_path,
        stream=stream,
    )
    try:
        async with capture_event_sequence(
            context.bus, InvocationContentDelta, InvocationToolCallStart, InvocationToolCallResult, InvocationMessage
        ) as events:
            await context.send_message("go")

        hosted_at = next(
            i
            for i, event in enumerate(events)
            if isinstance(event, (InvocationToolCallStart, InvocationToolCallResult)) and event.provider_hosted
        )
        answer = [
            (i, event)
            for i, event in enumerate(events)
            if isinstance(event, InvocationContentDelta) and event.origin.kind == "turn" and event.channel == "answer"
        ]
        assert "".join(delta.text for i, delta in answer if i < hosted_at) == "Let me look. "
        assert "".join(delta.text for i, delta in answer if i > hosted_at) == "Here it is."
        assert [
            event.provider_hosted
            for event in events
            if isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn"
        ] == ([False, True] if call[0] == "echo" else [True])
        assert _turn_messages(events) == [("Here it is.", True)]
        (final,) = [event for event in events if isinstance(event, InvocationMessage)]
        assert final.segment_id == answer[-1][1].segment_id
    finally:
        await context.cleanup()


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "blocking"])
async def test_text_before_hosted_output_shows_above_its_card(tmp_path: Any, stream: bool) -> None:
    """The response's text and its hosted output show in the order the response holds them."""
    context = await create_test_engine(
        [HostedMockResponse(lead_text="Drawing it.", hosted=[_hosted_image_item()], text="Here it is.")],
        tmp_path,
        stream=stream,
    )
    try:
        async with capture_event_sequence(
            context.bus, InvocationContentDelta, InvocationToolCallStart, InvocationToolCallResult
        ) as events:
            await context.send_message("go")

        hosted = [
            i
            for i, event in enumerate(events)
            if isinstance(event, (InvocationToolCallStart, InvocationToolCallResult)) and event.provider_hosted
        ]
        answer = [
            (i, event.text)
            for i, event in enumerate(events)
            if isinstance(event, InvocationContentDelta) and event.origin.kind == "turn" and event.channel == "answer"
        ]
        assert hosted
        assert "".join(text for i, text in answer if i < hosted[0]) == "Drawing it."
        assert "".join(text for i, text in answer if i > hosted[-1]) == "Here it is."
    finally:
        await context.cleanup()


async def test_an_interrupt_while_landed_text_shows_publishes_its_held_hosted_card_at_once(tmp_path: Any) -> None:
    """An interrupt shows the hosted card the response held behind its text while frontends still take its events."""
    context = await create_test_engine(
        [HostedMockResponse(lead_text="Drawing it.", hosted=[_hosted_image_item()], text="Here it is.")],
        tmp_path,
        stream=False,
    )
    showing, release_text = asyncio.Event(), asyncio.Event()
    hosted: list[type[Event]] = []

    async def hold_text(event: InvocationContentDelta) -> None:
        if event.text == "Drawing it.":
            showing.set()
            await release_text.wait()

    async def record_card(event: InvocationToolCallStart | InvocationToolCallResult) -> None:
        if event.provider_hosted:
            hosted.append(type(event))

    await context.bus.subscribe(InvocationContentDelta, hold_text)
    await context.bus.subscribe(InvocationToolCallStart, record_card)
    await context.bus.subscribe(InvocationToolCallResult, record_card)
    try:
        await context.bus.publish(UserMessage(text="go"))
        await wait_for(showing.is_set, description="the landed text showing")
        # The TUI stops taking the run's events once the interrupt it published returns.
        await context.send_interrupt()
        assert hosted == [InvocationToolCallStart, InvocationToolCallResult]

        release_text.set()
        await await_run_task_chain(context.engine, turn_state=context.engine.turns.turn_state)
        assert hosted == [InvocationToolCallStart, InvocationToolCallResult]
    finally:
        release_text.set()
        await context.cleanup()
