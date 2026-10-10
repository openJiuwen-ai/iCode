# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Client contracts over the real ``create_client`` stack and provider SDKs, behind a scripted transport.

How the clients behave toward the layers around them: the traits the kernel
reads off the class and through the stack, that a stream sends nothing until
it is consumed and releases its connection when cancelled or closed early,
that request observers see the exact views sent, that a tool batch is
published before its tools run, that internal side calls are neither timed
nor published (nor reported to a forwarded exchange trace) while the
LAST_WORDS side call reports an exchange of its own, that concurrent
requests on one client keep their own exchange traces, the exact chat and
tool span attributes, the exact User-Agent, that a profile header spelled in
another case goes out once, the text a failed request is wrapped in, the SDK
retry hook Chrys overrides, and that importing the package loads no provider
SDK.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import dataclasses
import inspect
import json
import os
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any
from unittest import mock

import httpx
import pytest
from opentelemetry.trace import SpanKind, StatusCode

from chrys import __version__
from chrys.foundation.trajectory.context import TRAJECTORY_EXCHANGE_KWARG, ExchangeTrace, trajectory_scope
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.kernel import (
    TELEMETRY_GATE,
    ChatClientException,
    Message,
    ResponseStream,
    instrumentation,
    internal_side_call_scope,
    tool,
)
from chrys.kernel.client import _ClientLastWordsCompleter
from chrys.service.llm.clients import create_client
from chrys.service.llm.openai_exceptions import OpenAIContentFilterException
from chrys.service.profiles.models.options import effective_chat_options
from chrys.service.profiles.models.schema import API_STYLE_RESPONSES, ModelProfile
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.scripted_wire import ScriptedWire, pin_wire_inputs, route_clients_to
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, wait_until
from tests.support.wire_cases import CASES, Case, Reply
from tests.support.wire_cases._kit import (
    ANTH_USAGE_1,
    CC_USAGE_1,
    RESP_USAGE_1,
    anth_message,
    anth_replies,
    anth_text,
    anth_weather_turns,
    cc_completion,
    cc_lookup_call,
    cc_replies,
    cc_text,
    cc_weather_turns,
    resp_function_call,
    resp_message,
    resp_replies,
    resp_response,
    resp_weather_turns,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

# Every (provider, api_style) pair, named by its grid case prefix.
COMBOS = ("openai_cc", "deepseek_cc", "glm_cc", "openai_responses", "deepseek_responses", "anthropic")
# The wire protocol each combo speaks.
PROTOCOLS = {
    "openai_cc": "cc",
    "deepseek_cc": "cc",
    "glm_cc": "cc",
    "openai_responses": "responses",
    "deepseek_responses": "responses",
    "anthropic": "anthropic",
}
MODES = pytest.mark.parametrize("stream", [False, True], ids=["send", "stream"])
# The first grid turn answers with this text beside its tool call.
FIRST_TURN_TEXT = "Let me check the weather."


def _case(combo: str, *, stream: bool) -> Case:
    return CASES[f"{combo}_{'stream' if stream else 'send'}"]


def _options(case: Case) -> dict[str, Any]:
    return {**(effective_chat_options(case.profile()) or {}), **case.options()}


@contextlib.asynccontextmanager
async def _stack(
    case: Case, transport: httpx.AsyncBaseTransport, monkeypatch: pytest.MonkeyPatch, **kwargs: Any
) -> AsyncIterator[Any]:
    route_clients_to(transport, monkeypatch)
    stack = await create_client(case.profile(), session_id="contracts", **kwargs)
    try:
        yield stack
    finally:
        await stack.aclose()


async def _land(result: Any, *, stream: bool) -> Any:
    return await (result.get_final_response() if stream else result)


# ---------------------------------------------------------------------------
# Traits
# ---------------------------------------------------------------------------

# OTEL provider name, stores by default, forces stateless, minimum output cap, service URL.
TRAITS: dict[str, tuple[str, bool, bool, int, str]] = {
    "openai_cc": ("openai", False, True, 1, "https://api.openai.com/v1/"),
    "deepseek_cc": ("openai", False, True, 1, "https://api.deepseek.com"),
    "glm_cc": ("openai", False, True, 1, "https://open.bigmodel.cn/api/paas/v4/"),
    "openai_responses": ("openai", True, False, 16, "https://api.openai.com/v1/"),
    "deepseek_responses": ("openai", False, True, 16, "https://api.deepseek.com"),
    "anthropic": ("anthropic", False, True, 1, "https://api.anthropic.com"),
}


def _traits(holder: Any, client: Any) -> tuple[str, bool, bool, int, str]:
    return (
        holder.OTEL_PROVIDER_NAME,
        holder.STORES_BY_DEFAULT,
        holder.FORCES_STATELESS,
        holder.MIN_OUTPUT_CAP_TOKENS,
        client.service_url(),
    )


@pytest.mark.parametrize("combo", COMBOS)
async def test_wire_client_traits(combo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Callers read the traits off the wire class and through the stack, which forwards them."""
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=False)
    async with _stack(case, ScriptedWire(()).transport, monkeypatch) as stack:
        wire = stack.inner.inner
        assert _traits(type(wire), wire) == TRAITS[combo]
        assert _traits(stack, stack) == TRAITS[combo]


async def test_mock_client_traits() -> None:
    profile = ModelProfile(id="mock", name="mock", provider="mock", model_id="mock-model")
    client = await create_client(profile, session_id="contracts")
    try:
        assert _traits(client, client) == ("unknown", False, False, 1, "Unknown")
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# Streams are lazy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", COMBOS)
@pytest.mark.parametrize("layer", ["inner", "wire", "stack"])
async def test_a_stream_sends_nothing_until_it_is_consumed(
    combo: str, layer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every layer hands back the stream itself, synchronously, and the request leaves on consumption."""
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=True)
    wire = ScriptedWire(case.replies)
    async with _stack(case, wire.transport, monkeypatch) as stack:
        client = stack.inner.inner
        messages = case.messages()
        options = _options(case)
        if layer == "inner":
            stream = client._inner_get_response(messages=messages, stream=True, options=options)
        elif layer == "wire":
            stream = client.get_response(messages, stream=True, options=options)
        else:
            stream = stack.get_response(messages, stream=True, options=options)
        assert isinstance(stream, ResponseStream)
        assert not await wait_until(lambda: wire.requests, timeout=0.2)

        await stream.get_final_response()

    # The tool loop runs the tool and asks again; a single layer asks once.
    assert len(wire.requests) == (2 if layer == "stack" else 1)


class _StalledBody(httpx.AsyncByteStream):
    """Serve every SSE event but the last one that sends data, then stall until closed.

    A closing ``[DONE]`` sends none: a Chat Completions stream that sent its
    usage after its finish reason has ended, so its usage is held back instead.
    """

    def __init__(self, body: bytes) -> None:
        events = body.split(b"\n\n")[:-1]
        if events[-1] == b"data: [DONE]":
            events.pop()
        self._head = b"\n\n".join(events[:-1]) + b"\n\n"
        self._closing = asyncio.Event()
        self.served = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._head
        self.served.set()
        await self._closing.wait()

    async def aclose(self) -> None:
        self.closed = True
        self._closing.set()


# The grid streams, plus the two Responses paths that open their SDK stream differently:
# the structured-output helper and the background resume.
_STALLED_STREAMS = (
    *(f"{combo}_stream" for combo in COMBOS),
    "openai_responses_structured_stream",
    "openai_responses_resume_stream",
)


@pytest.mark.parametrize("name", _STALLED_STREAMS)
@pytest.mark.parametrize("ending", ["cancelled", "closed"])
async def test_a_stream_left_early_releases_its_connection(
    name: str, ending: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer cancelled mid-stream, or closing the stream after its first update, closes the response body."""
    pin_wire_inputs(monkeypatch)
    case = CASES[name]
    reply = case.replies[0]
    body = _StalledBody(reply.body)

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(reply.status, headers=list(reply.headers), stream=body, request=request)

    async with _stack(case, httpx.MockTransport(handle), monkeypatch) as stack:
        stream = stack.inner.inner.get_response(case.messages(), stream=True, options=_options(case))
        if ending == "closed":
            await anext(stream)
            assert not body.closed
            await stream.aclose()
        else:

            async def consume() -> None:
                async for _ in stream:
                    pass

            task = asyncio.create_task(consume())
            try:
                await wait_for(
                    lambda: body.served.is_set() or task.done(),
                    timeout=ENGINE_TEST_WAIT_TIMEOUT,
                    description="the stream stalls mid-body",
                )
                if task.done():
                    await task
                assert not body.closed
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert body.closed


# ---------------------------------------------------------------------------
# Request observation
# ---------------------------------------------------------------------------


def _parameters(function: Callable[..., Any]) -> list[tuple[str, Any, Any]]:
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(function).parameters.values()
    ]


@pytest.mark.parametrize("combo", COMBOS)
@MODES
async def test_the_request_observer_sees_the_exact_views_sent(
    combo: str, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The middleware layer observes the caller's messages, the client the fresh wire views it then sends.

    A view is a new wrapper with a new contents list over the caller's very
    Content objects and metadata dict: identity is how request contents are
    matched back to conversation state.
    """
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    wire = ScriptedWire(case.replies[:1])
    observed: list[list[Message]] = []
    sent: list[list[Message]] = []
    async with _stack(case, wire.transport, monkeypatch) as stack:
        client = stack.inner.inner
        send = client._inner_get_response

        def spy(*, messages: Sequence[Message], stream: bool = False, options: Mapping[str, Any], **kwargs: Any) -> Any:
            sent.append(list(messages))
            return send(messages=messages, stream=stream, options=options, **kwargs)

        assert _parameters(spy) == _parameters(send)
        monkeypatch.setattr(client, "_inner_get_response", spy)
        messages = case.messages()
        await _land(
            stack.inner.get_response(
                messages,
                stream=stream,
                options=_options(case),
                request_message_observer=lambda views: observed.append(list(views)),
            ),
            stream=stream,
        )

    assert len(observed) == 2
    assert len(sent) == 1
    assert [view is message for view, message in zip(observed[0], messages, strict=True)] == [True] * len(messages)
    views = sent[0]
    assert [view is seen for view, seen in zip(views, observed[1], strict=True)] == [True] * len(views)
    for view, message in zip(views, messages, strict=True):
        assert view is not message
        assert view.contents is not message.contents
        assert [content is original for content, original in zip(view.contents, message.contents, strict=True)] == [
            True
        ] * len(message.contents)
        assert view.additional_properties is message.additional_properties


# ---------------------------------------------------------------------------
# A tool batch is published before its tools run
# ---------------------------------------------------------------------------


def _call_only_replies(combo: str, *, stream: bool) -> tuple[Reply, ...]:
    """The grid weather turns, with no text beside the first turn's tool call."""
    protocol = PROTOCOLS[combo]
    if protocol == "cc":
        call = {"role": "assistant", "content": None, "tool_calls": [cc_lookup_call()]}
        first = cc_completion(
            response_id="chatcmpl-golden-1", message=call, finish_reason="tool_calls", usage=CC_USAGE_1
        )
        return cc_replies((first, cc_weather_turns()[1]), stream=stream)
    if protocol == "responses":
        output = [resp_function_call("fc_golden_1", "call_lookup_1")]
        first = resp_response(response_id="resp_golden_1", output=output, usage=RESP_USAGE_1)
        return resp_replies((first, resp_weather_turns()[1]), stream=stream)
    call_block = {"type": "tool_use", "id": "toolu_golden_1", "name": "lookup", "input": {"city": "Paris"}}
    first = anth_message(message_id="msg_golden_1", content=[call_block], stop_reason="tool_use", usage=ANTH_USAGE_1)
    return anth_replies((first, anth_weather_turns()[1]), stream=stream)


@pytest.mark.parametrize("combo", COMBOS)
@MODES
@pytest.mark.parametrize("said", [FIRST_TURN_TEXT, ""], ids=["text", "call_only"])
async def test_a_tool_batch_is_published_before_its_tools_run(
    combo: str, stream: bool, said: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The publisher hears the text beside a batch's calls, or ``""`` as its boundary, before the tools run.

    The send path awaits the async publisher, which the test holds; the
    stream path calls the sync one as the stream finalizes.
    """
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    wire = ScriptedWire(case.replies if said else _call_only_replies(combo, stream=stream))
    published: list[tuple[str, str]] = []
    heard_by_tool: list[list[tuple[str, str]]] = []
    holding = asyncio.Event()
    release = asyncio.Event()

    async def on_text_async(text: str) -> None:
        holding.set()
        await release.wait()
        published.append(("async", text))

    def on_text_sync(text: str) -> None:
        published.append(("sync", text))

    @tool(name="lookup", description="Look up the weather in a city.")
    def lookup(city: str) -> str:
        heard_by_tool.append(list(published))
        return f"Sunny in {city}."

    async with _stack(
        case,
        wire.transport,
        monkeypatch,
        on_intermediate_text_async=on_text_async,
        on_intermediate_text_sync=on_text_sync,
    ) as stack:
        options = {**_options(case), "tools": [lookup]}
        run = asyncio.create_task(
            _land(stack.get_response(case.messages(), stream=stream, options=options), stream=stream)
        )
        try:
            if not stream:
                await wait_for(
                    lambda: holding.is_set() or run.done(),
                    timeout=ENGINE_TEST_WAIT_TIMEOUT,
                    description="the publisher holds the batch",
                )
                if run.done():
                    await run
                assert not await wait_until(lambda: heard_by_tool or run.done(), timeout=0.2)
            release.set()
            await run
        finally:
            release.set()
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)

    heard = [("sync" if stream else "async", said)]
    assert heard_by_tool == [heard]
    assert published == heard


# ---------------------------------------------------------------------------
# Internal side calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", COMBOS)
@MODES
@pytest.mark.parametrize("side_call", [False, True], ids=["conversation", "side_call"])
async def test_internal_side_calls_are_neither_timed_nor_published(
    combo: str, stream: bool, side_call: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A conversation response is stamped, published and reported to its exchange; a side call's is not.

    Both run under a main-actor context with its exchange trace forwarded, as
    a side call inherits the conversation's client kwargs verbatim.
    """
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    wire = ScriptedWire(case.replies[:1])
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange(new_analytics_id())
    published: list[tuple[str, str]] = []

    async def on_text_async(text: str) -> None:
        published.append(("async", text))

    def on_text_sync(text: str) -> None:
        published.append(("sync", text))

    async with _stack(
        case,
        wire.transport,
        monkeypatch,
        on_intermediate_text_async=on_text_async,
        on_intermediate_text_sync=on_text_sync,
    ) as stack:
        client = stack.inner.inner
        with trajectory_scope(context), internal_side_call_scope() if side_call else contextlib.nullcontext():
            result = client.get_response(
                case.messages(),
                stream=stream,
                options=_options(case),
                client_kwargs={TRAJECTORY_EXCHANGE_KWARG: ExchangeTrace(context)},
            )
            response = await _land(result, stream=stream)

    stamps = [
        [key for key in (MESSAGE_CREATED_AT_KEY, TRAJECTORY_TIMING_KEY) if key in message.additional_properties]
        for message in response.messages
    ]
    exchange_events = [
        len(sink.of_type(event_type))
        for event_type in (EventType.MODEL_EXCHANGE_STARTED, EventType.MODEL_EXCHANGE_FINISHED)
    ]
    assert response.messages
    if side_call:
        assert stamps == [[]] * len(response.messages)
        assert published == []
        assert sink.drafts == []
    else:
        assert stamps == [[MESSAGE_CREATED_AT_KEY, TRAJECTORY_TIMING_KEY]] * len(response.messages)
        assert published == [("sync" if stream else "async", FIRST_TURN_TEXT)]
        assert exchange_events == [1, 1]


@pytest.mark.parametrize("combo", COMBOS)
@MODES
async def test_the_last_words_side_call_reports_an_exchange_of_its_own(
    combo: str, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handed the live call's client kwargs, the completer reports its call as the completer side call.

    Its exchange hangs off the run, not off the conversation's exchange
    forwarded in those kwargs.
    """
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    wire = ScriptedWire(_marked_replies(PROTOCOLS[combo], "note", stream=stream))
    sink = FakeSink()
    context = make_context(sink)
    conversation = ExchangeTrace(context.with_cycle(new_analytics_id()).with_exchange(new_analytics_id()))
    async with _stack(case, wire.transport, monkeypatch) as stack:
        completer = _ClientLastWordsCompleter(
            stack.inner.inner,
            stream=stream,
            options=_options(case),
            client_kwargs={TRAJECTORY_EXCHANGE_KWARG: conversation},
        )
        with trajectory_scope(context):
            note = await completer.complete_last_words(case.messages(), "Write the note.", max_output_tokens=512)

    assert note == "Sunny for note."
    started = sink.only(EventType.MODEL_EXCHANGE_STARTED)
    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert sink.event_types == [
        EventType.MODEL_EXCHANGE_STARTED,
        EventType.MODEL_REQUEST_PREPARED,
        EventType.MODEL_REQUEST_HEADERS_RECEIVED,
        EventType.MODEL_EXCHANGE_FINISHED,
    ]
    prepared = sink.only(EventType.MODEL_REQUEST_PREPARED)
    received = sink.only(EventType.MODEL_REQUEST_HEADERS_RECEIVED)
    assert prepared.operation_id == received.operation_id == started.operation_id
    assert prepared.actor == received.actor == started.actor
    assert prepared.payload["request_attempt_id"] == received.payload["request_attempt_id"]
    assert finished.payload["request_attempt_id"] == prepared.payload["request_attempt_id"]
    for event in (started, finished):
        assert (event.actor.kind, event.actor.role) == ("side_call", "completer")
        assert event.operation_id == started.operation_id
        assert event.parent_operation_id == context.run_operation_id
        assert event.turn_id == context.turn_id
    assert started.operation_id != conversation.context.exchange_operation_id
    assert started.payload["stream"] is stream
    assert finished.payload["response_id"] == _RESPONSE_IDS[PROTOCOLS[combo]].format(marker="note")


# ---------------------------------------------------------------------------
# Concurrent acquisitions keep their own exchange traces
# ---------------------------------------------------------------------------


class _HeldWire:
    """Answer each request by the marker its body carries, only once the test releases them."""

    def __init__(self, replies: Mapping[str, Reply]) -> None:
        self._replies = dict(replies)
        self.arrived: list[str] = []
        self.release = asyncio.Event()
        self.transport = httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        marker = next(marker for marker in self._replies if marker in body)
        self.arrived.append(marker)
        await self.release.wait()
        reply = self._replies.pop(marker)
        return httpx.Response(reply.status, headers=list(reply.headers), content=reply.body, request=request)


def _marked_replies(protocol: str, marker: str, *, stream: bool) -> tuple[Reply, ...]:
    text = f"Sunny for {marker}."
    if protocol == "cc":
        return cc_replies([cc_text(text, response_id=f"chatcmpl-{marker}")], stream=stream)
    if protocol == "responses":
        response = resp_response(response_id=f"resp_{marker}", output=[resp_message(f"msg_{marker}", text)])
        return resp_replies([response], stream=stream)
    return anth_replies([anth_text(text, message_id=f"msg_{marker}")], stream=stream)


_RESPONSE_IDS = {
    "cc": "chatcmpl-{marker}",
    "responses": "resp_{marker}",
    "anthropic": "msg_{marker}",
}


@pytest.mark.parametrize(
    ("protocol", "combo"), [("cc", "openai_cc"), ("responses", "openai_responses"), ("anthropic", "anthropic")]
)
@MODES
async def test_concurrent_requests_on_one_client_keep_their_own_exchange_traces(
    protocol: str, combo: str, stream: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both requests are in flight together; each trace's events land in its own sink with its own response."""
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    markers = ("alpha", "beta")
    wire = _HeldWire({marker: _marked_replies(protocol, marker, stream=stream)[0] for marker in markers})
    sinks = {marker: FakeSink() for marker in markers}

    async with _stack(case, wire.transport, monkeypatch) as stack:
        client = stack.inner.inner
        options = _options(case)

        async def acquire(marker: str) -> None:
            context = make_context(sinks[marker]).with_cycle(new_analytics_id()).with_exchange(new_analytics_id())
            with trajectory_scope(context):
                result = client._inner_get_response(
                    messages=[Message("user", [f"Weather for {marker}?"])],
                    stream=stream,
                    options=options,
                    **{TRAJECTORY_EXCHANGE_KWARG: ExchangeTrace(context)},
                )
                await _land(result, stream=stream)

        tasks = [asyncio.create_task(acquire(marker)) for marker in markers]
        try:
            await wait_for(
                lambda: len(wire.arrived) == len(markers) or any(task.done() for task in tasks),
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="both requests in flight",
            )
            for task in tasks:
                if task.done():
                    await task
            assert sorted(wire.arrived) == list(markers)
            wire.release.set()
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    for marker, sink in sinks.items():
        started = sink.only(EventType.MODEL_EXCHANGE_STARTED)
        finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
        assert finished.operation_id == started.operation_id
        assert finished.payload["response_id"] == _RESPONSE_IDS[protocol].format(marker=marker)


# ---------------------------------------------------------------------------
# Telemetry spans
# ---------------------------------------------------------------------------

# What each protocol's chat span adds for the grid's first and second turn.
_CHAT_SPAN_TURNS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "cc": (
        {
            "gen_ai.response.id": "chatcmpl-golden-1",
            "gen_ai.response.finish_reasons": '["tool_calls"]',
            "gen_ai.usage.cache_read.input_tokens": 8,
            "gen_ai.usage.input_tokens": 40,
            "gen_ai.usage.output_tokens": 12,
            "gen_ai.usage.reasoning.output_tokens": 5,
        },
        {
            "gen_ai.response.id": "chatcmpl-golden-2",
            "gen_ai.response.finish_reasons": '["stop"]',
            "gen_ai.usage.input_tokens": 70,
            "gen_ai.usage.output_tokens": 9,
        },
    ),
    "responses": (
        {
            "gen_ai.response.id": "resp_golden_1",
            "gen_ai.usage.cache_read.input_tokens": 8,
            "gen_ai.usage.input_tokens": 40,
            "gen_ai.usage.output_tokens": 12,
            "gen_ai.usage.reasoning.output_tokens": 5,
        },
        {
            "gen_ai.response.id": "resp_golden_2",
            "gen_ai.usage.cache_read.input_tokens": 0,
            "gen_ai.usage.input_tokens": 70,
            "gen_ai.usage.output_tokens": 9,
            "gen_ai.usage.reasoning.output_tokens": 0,
        },
    ),
    "anthropic": (
        {
            "gen_ai.response.id": "msg_golden_1",
            "gen_ai.response.finish_reasons": '["tool_calls"]',
            "gen_ai.usage.cache_creation.input_tokens": 6,
            "gen_ai.usage.cache_read.input_tokens": 8,
            "gen_ai.usage.input_tokens": 54,
            "gen_ai.usage.output_tokens": 12,
        },
        {
            "gen_ai.response.id": "msg_golden_2",
            "gen_ai.response.finish_reasons": '["stop"]',
            "gen_ai.usage.cache_creation.input_tokens": 0,
            "gen_ai.usage.cache_read.input_tokens": 0,
            "gen_ai.usage.input_tokens": 70,
            "gen_ai.usage.output_tokens": 9,
        },
    ),
}
_TOOL_CALL_IDS = {"cc": "call_lookup_1", "responses": "call_lookup_1", "anthropic": "toolu_golden_1"}
_TOOL_DURATION = "chrys.function.invocation.duration"


def _chat_span(provider_name: str, server_address: str, turn: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": provider_name,
        "gen_ai.request.choice.count": 1,
        "gen_ai.request.model": "golden-model",
        "gen_ai.response.model": "golden-model-2026",
        "server.address": server_address,
        **turn,
    }


@pytest.fixture
def finished_spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("contracts")
    monkeypatch.setattr(
        instrumentation, "get_tracer", mock.create_autospec(instrumentation.get_tracer, return_value=tracer)
    )
    monkeypatch.setattr(TELEMETRY_GATE, "enabled", True)
    monkeypatch.setattr(TELEMETRY_GATE, "sensitive_data", False)
    yield exporter
    provider.shutdown()


@pytest.mark.parametrize("combo", COMBOS)
@MODES
@pytest.mark.parametrize("layer", ["wire", "stack"])
async def test_chat_and_tool_spans_carry_the_exact_attributes(
    combo: str, stream: bool, layer: str, finished_spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    wire = ScriptedWire(case.replies if layer == "stack" else case.replies[:1])
    async with _stack(case, wire.transport, monkeypatch) as stack:
        client = stack if layer == "stack" else stack.inner.inner
        await _land(client.get_response(case.messages(), stream=stream, options=_options(case)), stream=stream)

    protocol = PROTOCOLS[combo]
    provider_name, *_, server_address = TRAITS[combo]
    first, second = _CHAT_SPAN_TURNS[protocol]
    expected = [("chat golden-model", _chat_span(provider_name, server_address, first))]
    if layer == "stack":
        lookup = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.call.id": _TOOL_CALL_IDS[protocol],
            "gen_ai.tool.description": "Look up the weather in a city.",
            "gen_ai.tool.name": "lookup",
            "gen_ai.tool.type": "function",
        }
        expected += [
            ("execute_tool lookup", lookup),
            ("chat golden-model", _chat_span(provider_name, server_address, second)),
        ]

    spans = finished_spans.get_finished_spans()
    assert [
        (span.name, {key: value for key, value in (span.attributes or {}).items() if key != _TOOL_DURATION})
        for span in spans
    ] == expected
    assert [(span.kind, span.status.status_code, list(span.events)) for span in spans] == [
        (SpanKind.INTERNAL, StatusCode.UNSET, [])
    ] * len(expected)
    # The tool span alone measures its invocation.
    assert [type((span.attributes or {}).get(_TOOL_DURATION)) for span in spans] == [
        float if name == "execute_tool lookup" else type(None) for name, _ in expected
    ]


async def test_tuned_request_options_add_no_span_attributes(
    finished_spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sampling, seed, stop and output-cap options stay off the chat span; its address is the profile's base URL."""
    pin_wire_inputs(monkeypatch)
    case = CASES["openai_cc_tuned_send"]
    async with _stack(case, ScriptedWire(case.replies[:1]).transport, monkeypatch) as stack:
        await stack.inner.inner.get_response(case.messages(), options=_options(case))

    (span,) = finished_spans.get_finished_spans()
    first, _ = _CHAT_SPAN_TURNS["cc"]
    assert dict(span.attributes or {}) == _chat_span("openai", "https://llm.example.test/v1/", first)


# ---------------------------------------------------------------------------
# Static headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo", COMBOS)
async def test_the_user_agent_names_chrys_python_and_the_sdk(combo: str, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import openai

    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=False)
    reply = case.replies[0]
    user_agents: list[list[str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        user_agents.append(request.headers.get_list("user-agent"))
        return httpx.Response(reply.status, headers=list(reply.headers), content=reply.body, request=request)

    async with _stack(case, httpx.MockTransport(handle), monkeypatch) as stack:
        await stack.inner.inner.get_response(case.messages(), options=_options(case))

    runtime = sys.version_info
    sdk = (
        f"AsyncAnthropic/Python {anthropic.__version__}"
        if case.provider == "anthropic"
        else f"AsyncOpenAI/Python {openai.__version__}"
    )
    assert user_agents == [[f"Chrys/{__version__} Python/{runtime.major}.{runtime.minor}.{runtime.micro} {sdk}"]]


# Headers each protocol's SDK sets itself, spelled in another case than the SDK's.
_SDK_HEADERS = {
    "cc": ("authorization", "openai-organization", "openai-project"),
    "responses": ("authorization", "openai-organization", "openai-project"),
    "anthropic": ("x-api-key", "Anthropic-Version"),
}


@pytest.mark.parametrize("combo", COMBOS)
async def test_a_profile_header_in_another_case_replaces_the_one_chrys_or_the_sdk_sets(
    combo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Header names are case-insensitive: each goes out once, with the profile's value."""
    pin_wire_inputs(monkeypatch)
    names = ("user-agent", "x-client-name", *_SDK_HEADERS[PROTOCOLS[combo]])
    base = _case(combo, stream=False)
    case = dataclasses.replace(
        base,
        profile_fields={**base.profile_fields, "http_headers": json.dumps({name: f"profile {name}" for name in names})},
    )
    reply = case.replies[0]
    sent: list[list[tuple[str, list[str]]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append([(name, request.headers.get_list(name)) for name in names])
        return httpx.Response(reply.status, headers=list(reply.headers), content=reply.body, request=request)

    async with _stack(case, httpx.MockTransport(handle), monkeypatch) as stack:
        await stack.inner.inner.get_response(case.messages(), options=_options(case))

    assert sent == [[(name, [f"profile {name}"]) for name in names]]


# ---------------------------------------------------------------------------
# Failed requests
# ---------------------------------------------------------------------------

# The protocol name an OpenAI SDK client's failure wrapper starts with.
_FAILURE_PREFIXES = {"cc": "Chat Completions", "responses": "Responses API"}


@pytest.mark.parametrize("combo", [combo for combo in COMBOS if PROTOCOLS[combo] != "anthropic"])
@MODES
@pytest.mark.parametrize(
    ("code", "wrapper", "says"),
    [
        (None, ChatClientException, "request failed"),
        ("content_filter", OpenAIContentFilterException, "request was blocked by a content filter"),
    ],
    ids=["rejected", "content-filter"],
)
async def test_a_failed_request_is_wrapped_under_the_protocol_name(
    combo: str,
    stream: bool,
    code: str | None,
    wrapper: type[ChatClientException],
    says: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper's text names the protocol and the provider error, never the client class."""
    pin_wire_inputs(monkeypatch)
    case = _case(combo, stream=stream)
    body = json.dumps({"error": {"message": "rejected", "type": "invalid_request_error", "code": code}})
    rejection = Reply(400, body.encode("utf-8"), (("content-type", "application/json"),))
    async with _stack(case, ScriptedWire([rejection]).transport, monkeypatch) as stack:
        with pytest.raises(ChatClientException) as raised:
            await _land(
                stack.inner.inner.get_response(case.messages(), stream=stream, options=_options(case)), stream=stream
            )

    assert type(raised.value) is wrapper
    assert raised.value.args[0] == f"{_FAILURE_PREFIXES[PROTOCOLS[combo]]} {says}: {raised.value.__cause__}"


# ---------------------------------------------------------------------------
# The SDK retry hooks Chrys overrides
# ---------------------------------------------------------------------------


def test_the_openai_sdk_sleeps_for_a_retry_inside_the_handler_that_caught_the_error() -> None:
    """Chrys's OpenAI guard reads the handled error with ``sys.exception()`` and forwards keywords only.

    So the hook must stay a coroutine taking keywords only, and every call
    site in ``request()`` must sit inside the ``except`` block that caught the
    error, the generic ``except Exception`` one included.
    """
    import openai._base_client

    sdk_client = openai._base_client.AsyncAPIClient
    hook = sdk_client._sleep_for_retry
    assert inspect.iscoroutinefunction(hook)
    parameters = list(inspect.signature(hook).parameters.values())[1:]
    assert parameters
    assert {parameter.kind for parameter in parameters} == {inspect.Parameter.KEYWORD_ONLY}

    tree = ast.parse(textwrap.dedent(inspect.getsource(sdk_client.request)))
    handlers: list[str | None] = []

    def visit(node: ast.AST, handler: ast.ExceptHandler | None) -> None:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_sleep_for_retry":
            assert handler is not None, f"line {node.lineno}: retry sleep outside an except block"
            assert not node.args, f"line {node.lineno}: positional retry sleep arguments"
            handlers.append(ast.unparse(handler.type) if handler.type is not None else None)
        for child in ast.iter_child_nodes(node):
            visit(child, node if isinstance(node, ast.ExceptHandler) else handler)

    visit(tree, None)
    assert "Exception" in handlers


def test_the_anthropic_sdk_asks_whether_to_retry_with_the_caught_error() -> None:
    """Chrys's Anthropic guard decides from the error the SDK hands ``_should_retry_exception``.

    So the hook must stay a plain method taking that one error, and
    ``request()`` must call it from the ``except`` block that caught the
    attempt's error, passing exactly that error, before any retry sleep.
    """
    import anthropic._base_client

    sdk_client = anthropic._base_client.AsyncAPIClient
    hook = sdk_client._should_retry_exception
    assert not inspect.iscoroutinefunction(hook)
    parameters = list(inspect.signature(hook).parameters.values())
    assert [parameter.kind for parameter in parameters] == [inspect.Parameter.POSITIONAL_OR_KEYWORD] * 2

    tree = ast.parse(textwrap.dedent(inspect.getsource(sdk_client.request)))
    decisions: list[str] = []
    for handler in ast.walk(tree):
        if not isinstance(handler, ast.ExceptHandler):
            continue
        calls = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_should_retry_exception"
        ]
        for call in calls:
            assert [ast.unparse(arg) for arg in call.args] == [handler.name], (
                f"line {call.lineno}: not the caught error"
            )
            sleeps = [
                node.lineno
                for node in ast.walk(handler)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_sleep_for_retry"
            ]
            assert all(line > call.lineno for line in sleeps), f"line {call.lineno}: sleeps before deciding"
            decisions.append(ast.unparse(handler.type) if handler.type is not None else "")
    assert decisions == ["Exception"]


# ---------------------------------------------------------------------------
# Import cost
# ---------------------------------------------------------------------------


def test_importing_the_llm_package_loads_no_provider_sdk(tmp_path: Any) -> None:
    home = str(tmp_path)
    env = {**os.environ, "HOME": home, "USERPROFILE": home, "APPDATA": home}
    code = (
        "import sys\n"
        "import chrys.service.llm\n"
        "import chrys.service.llm.clients\n"
        "print(sorted(name for name in ('openai', 'anthropic') if name in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=ENGINE_TURN_TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"


def test_every_combo_is_a_distinct_pair() -> None:
    pairs = {(CASES[f"{combo}_send"].provider, CASES[f"{combo}_send"].api_style) for combo in COMBOS}
    assert len(pairs) == len(COMBOS)
    assert ("openai", API_STYLE_RESPONSES) in pairs
