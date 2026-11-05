# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the wire-client base: request reporting, response timing and intermediate text."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import copy
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from chrys.foundation.trajectory.context import (
    TRAJECTORY_EXCHANGE_KWARG,
    ExchangeTrace,
    side_call_scope,
    trajectory_scope,
)
from chrys.foundation.trajectory.envelope import ActorRole
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY, build_trajectory_timing
from chrys.foundation.util.chrys_headers import PARENT_SESSION_ID_HEADER, SESSION_ID_HEADER
from chrys.kernel import (
    AgentResponse,
    AgentSession,
    ChatMiddlewareLayer,
    ChatResponse,
    ChatResponseUpdate,
    Content,
    Message,
    ResponseStream,
    SessionContext,
    ToolLoopLayer,
    internal_side_call_scope,
)
from chrys.kernel.exceptions import (
    ChatClientContentFilterException,
    ChatClientInvalidRequestException,
)
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.llm.observer import WireCallObserver
from chrys.service.llm.wire_client import RequestHeaders, WireClient
from chrys.service.session.message_metadata import MESSAGE_CREATED_AT_KEY, stamp_message_response_timing
from tests.service.trajectory._fakes import FakeSink, make_context
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer


def test_assembled_stack_keeps_the_wire_client_class_callbacks_and_headers() -> None:
    from openai import AsyncOpenAI

    from chrys.service.llm.clients import _assemble_stack
    from chrys.service.llm.openai_responses import DeepSeekResponsesApiClient

    calls: list[str] = []

    def _callback(text: str) -> None:
        calls.append(text)

    sdk_client = AsyncOpenAI(api_key="sk-fake")
    client = _assemble_stack(
        DeepSeekResponsesApiClient,
        sdk_client,
        model_id="deepseek-test",
        session_id="session-1",
        parent_session_id="parent-1",
        use_route_session_context=False,
        on_intermediate_text=_callback,
        max_iterations=7777,
        max_consecutive_errors=10,
        tool_result_ceiling_tokens=None,
    )
    assert isinstance(client, ToolLoopLayer)
    assert isinstance(client.inner, ChatMiddlewareLayer)
    raw = client.inner.inner

    prepared = raw._build_request([Message("user", ["hi"])], {})

    assert type(raw) is DeepSeekResponsesApiClient
    assert raw.sdk_client is sdk_client
    assert raw.model == "deepseek-test"
    assert raw._observer._on_intermediate_text is _callback
    assert prepared["extra_headers"][SESSION_ID_HEADER] == "session-1"
    assert prepared["extra_headers"][PARENT_SESSION_ID_HEADER] == "parent-1"
    assert calls == []


def test_raw_clients_require_preconfigured_sdk_clients() -> None:
    from chrys.service.llm.anthropic_messages import AnthropicMessagesClient
    from chrys.service.llm.chat_completions import ChatCompletionsClient
    from chrys.service.llm.openai_responses import ResponsesApiClient

    with pytest.raises(ValueError, match="pre-configured sdk_client"):
        ChatCompletionsClient(model="gpt-test")
    with pytest.raises(ValueError, match="pre-configured sdk_client"):
        ResponsesApiClient(model="gpt-test")
    with pytest.raises(ValueError, match="pre-configured sdk_client"):
        AnthropicMessagesClient(model="claude-test")


def test_each_protocol_client_is_built_over_the_sdk_client_it_is_handed() -> None:
    from anthropic import AsyncAnthropic
    from openai import AsyncOpenAI

    from chrys.service.llm.anthropic_messages import AnthropicMessagesClient
    from chrys.service.llm.chat_completions import ChatCompletionsClient
    from chrys.service.llm.openai_responses import ResponsesApiClient

    observer = WireCallObserver()
    headers = RequestHeaders(session_id="s")
    openai_sdk = AsyncOpenAI(api_key="sk-fake")
    anthropic_sdk = AsyncAnthropic(api_key="sk-fake")

    cases: list[tuple[Any, Any]] = [
        (ChatCompletionsClient, openai_sdk),
        (ResponsesApiClient, openai_sdk),
        (AnthropicMessagesClient, anthropic_sdk),
    ]

    for client_cls, sdk in cases:
        client = client_cls.from_sdk_client(sdk, model="m", observer=observer, request_headers=headers)
        assert client.sdk_client is sdk
        assert client.model == "m"
        assert client._observer is observer
        assert client._request_headers is headers


def test_the_base_class_takes_no_sdk_client() -> None:
    with pytest.raises(NotImplementedError, match="_ScriptedWireClient does not take a provider SDK client"):
        _ScriptedWireClient.from_sdk_client(object(), model="m")


class _FailingWireClient(WireClient):
    """A wire client whose response or stream fails with *exc* once consumed."""

    def __init__(self, exc: Exception) -> None:
        super().__init__(observer=WireCallObserver())
        self.exc = exc

    def _send(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        del messages, options, kwargs

        async def _response() -> Any:
            raise self.exc

        return _response()

    def _open_stream(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        del messages, options, kwargs

        async def _updates() -> Any:
            raise self.exc
            yield ChatResponseUpdate(contents=[Content.from_text("unused")])

        return ResponseStream(
            _updates(),
            finalizer=ChatResponse.from_updates,
        )


class _ScriptedWireClient(WireClient):
    """A wire client that answers with *response*, or one *stream_text* message, and records its calls."""

    def __init__(
        self,
        *,
        response: Any = None,
        stream_text: str = "streamed",
        delay: float = 0,
        observed: bool = True,
        request_headers: RequestHeaders | None = None,
    ) -> None:
        super().__init__(observer=WireCallObserver() if observed else None, request_headers=request_headers)
        self.response = response
        self.stream_text = stream_text
        self.delay = delay
        self.calls: list[dict[str, Any]] = []

    def _record(self, messages: Any, options: Any, *, stream: bool, kwargs: dict[str, Any]) -> None:
        self.calls.append({"messages": list(messages), "options": dict(options), "stream": stream, "kwargs": kwargs})

    def _send(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        self._record(messages, options, stream=False, kwargs=kwargs)

        async def _response() -> Any:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            if self.response is not None:
                return self.response
            return ChatResponse(
                messages=[
                    Message(
                        "assistant",
                        [Content.from_text(self.stream_text)],
                    )
                ],
                response_format=options.get("response_format"),
            )

        return _response()

    def _open_stream(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        self._record(messages, options, stream=True, kwargs=kwargs)

        async def _updates() -> Any:
            if self.delay > 0:
                await asyncio.sleep(self.delay)
            yield ChatResponseUpdate(
                contents=[Content.from_text(self.stream_text)],
                role="assistant",
            )

        return ResponseStream(
            _updates(),
            finalizer=lambda updates: ChatResponse.from_updates(
                updates,
                output_format_type=options.get("response_format"),
            ),
        )


class _NoopCompaction:
    async def __call__(self, messages: list[Any], context: Any = None) -> bool:
        self.messages = messages
        self.context = context
        return False


async def test_chrys_chat_client_exception_propagates_non_streaming() -> None:
    inner = ValueError("root cause")
    chrys_exc = ChatClientInvalidRequestException(
        "provider rejected request",
        inner_exception=inner,
        log_level=None,
    )
    client = _FailingWireClient(chrys_exc)

    with pytest.raises(ChatClientInvalidRequestException) as exc_info:
        await client._inner_get_response(messages=[Message("user", ["hi"])], options={})

    assert exc_info.value is chrys_exc
    assert exc_info.value.args == ("provider rejected request", inner)


async def test_chrys_chat_client_exception_propagates_streaming() -> None:
    inner = RuntimeError("filter details")
    chrys_exc = ChatClientContentFilterException(
        "provider content filter",
        inner_exception=inner,
        log_level=None,
    )
    client = _FailingWireClient(chrys_exc)

    stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)

    assert isinstance(stream, ResponseStream)
    with pytest.raises(ChatClientContentFilterException) as exc_info:
        async for _update in stream:
            pass
    assert exc_info.value is chrys_exc
    assert exc_info.value.args == ("provider content filter", inner)


async def test_streaming_get_response_with_compaction_returns_chrys_stream() -> None:
    client = _ScriptedWireClient(stream_text="compacted stream")
    compaction = _NoopCompaction()

    stream = client.get_response(
        [Message("user", ["hi"])],
        stream=True,
        options={},
        compaction_strategy=compaction,
    )

    assert isinstance(stream, ResponseStream)
    updates = [update async for update in stream]
    assert [update.text for update in updates] == ["compacted stream"]
    final = await stream.get_final_response()
    assert final.text == "compacted stream"
    assert client.calls[0]["stream"] is True
    assert compaction.messages


async def test_native_response_preserves_lazy_value_parse() -> None:
    class StructuredPayload(BaseModel):
        answer: int

    native_response = ChatResponse(
        messages=[Message("assistant", [Content.from_text("not-json")])],
        response_format=StructuredPayload,
    )
    client = _ScriptedWireClient(response=native_response)

    response = await client._inner_get_response(messages=[Message("user", ["hi"])], options={})

    assert response._value_parsed is False
    with pytest.raises(ValidationError):
        _ = response.value


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_wire_response_persists_measured_trajectory_timing(stream: bool) -> None:
    client = _ScriptedWireClient(stream_text="timed")

    response_or_stream = client._inner_get_response(
        messages=[Message("user", ["hi"])],
        options={},
        stream=stream,
    )
    if stream:
        async for _update in response_or_stream:
            pass
        response = await response_or_stream.get_final_response()
    else:
        response = await response_or_stream

    message = response.messages[-1]
    timing = message.additional_properties[TRAJECTORY_TIMING_KEY]
    assert timing["started_at"] <= timing["finished_at"]
    assert timing["finished_at"] == message.additional_properties[MESSAGE_CREATED_AT_KEY]
    assert isinstance(timing["duration_ms"], int)
    assert timing["duration_ms"] >= 0


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_measured_timing_survives_tool_loop_and_history_persistence(stream: bool) -> None:
    """The production loop and history provider preserve the wire-stamped message."""
    wire_client = _ScriptedWireClient(stream_text="persisted", delay=0.01)
    client = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(wire_client))
    user = Message("user", [Content.from_text("hi")])
    provider = CompressibleHistoryProvider()
    session = AgentSession(session_id="timing-survival")
    context = SessionContext(session_id="timing-survival", input_messages=[user])
    state: dict[str, Any] = {"messages": [], "compressed_msgs": []}

    await provider.before_run(agent=object(), session=session, context=context, state=state)
    wire_messages = context.get_messages(include_input=True)
    if stream:
        response_stream = client.get_response(wire_messages, stream=True, options={})
        assert isinstance(response_stream, ResponseStream)
        _ = [update async for update in response_stream]
        response = await response_stream.get_final_response()
    else:
        pending_response = client.get_response(wire_messages, stream=False, options={})
        assert not isinstance(pending_response, ResponseStream)
        response = await pending_response
    context._response = AgentResponse(messages=response.messages)
    await provider.after_run(agent=object(), session=session, context=context, state=state)

    persisted = state["messages"][-1]
    assert persisted is response.messages[-1]
    timing = persisted.additional_properties[TRAJECTORY_TIMING_KEY]
    assert timing["finished_at"] == persisted.additional_properties[MESSAGE_CREATED_AT_KEY]
    assert timing["duration_ms"] >= 1


async def test_wire_response_stamps_provider_hosted_tool_contents() -> None:
    hosted_call = Content.from_search_tool_call(
        "search-1",
        tool_name="web_search",
        arguments={"query": "timing"},
    )
    native_response = ChatResponse(messages=[Message("assistant", [hosted_call])])
    client = _ScriptedWireClient(response=native_response)

    response = await client._inner_get_response(messages=[Message("user", ["hi"])], options={})

    message_timing = response.messages[-1].additional_properties[TRAJECTORY_TIMING_KEY]
    assert hosted_call.additional_properties[TRAJECTORY_TIMING_KEY] == message_timing


async def test_wire_response_timing_does_not_mutate_echoed_request_objects() -> None:
    old_started_at = "2000-01-01T01:02:03+00:00"
    old_finished_at = "2000-01-01T01:02:04+00:00"
    hosted_call = Content.from_search_tool_call(
        "search-old",
        tool_name="web_search",
        arguments={"query": "old"},
    )
    old_timing = build_trajectory_timing(
        started_at=old_started_at,
        finished_at=old_finished_at,
        duration_ms=1_000,
    )
    hosted_call.additional_properties[TRAJECTORY_TIMING_KEY] = dict(old_timing)
    echoed = Message("assistant", [hosted_call])
    stamp_message_response_timing(
        echoed,
        started_at=old_started_at,
        finished_at=old_finished_at,
        duration_ms=1_000,
    )
    shallow_hosted_echo = copy(hosted_call)
    assert shallow_hosted_echo.additional_properties is hosted_call.additional_properties
    shallow_echo = Message("assistant", [shallow_hosted_echo])
    fresh = Message("assistant", [Content.from_text("fresh")])
    client = _ScriptedWireClient(response=ChatResponse(messages=[echoed, shallow_echo, fresh]))

    response = await client._inner_get_response(messages=[echoed], options={})

    assert echoed.additional_properties[TRAJECTORY_TIMING_KEY] == old_timing
    assert echoed.additional_properties[MESSAGE_CREATED_AT_KEY] == old_timing["finished_at"]
    assert hosted_call.additional_properties[TRAJECTORY_TIMING_KEY] == old_timing
    assert shallow_hosted_echo.additional_properties[TRAJECTORY_TIMING_KEY] == old_timing
    assert response.messages[-1].additional_properties[TRAJECTORY_TIMING_KEY] != old_timing


async def test_native_stream_final_response_preserves_response_format() -> None:
    class StructuredPayload(BaseModel):
        answer: str

    client = _ScriptedWireClient(stream_text='{"answer":"ok"}')

    stream = client._inner_get_response(
        messages=[Message("user", ["hi"])],
        options={"response_format": StructuredPayload},
        stream=True,
    )
    updates = [update async for update in stream]
    final = await stream.get_final_response()

    assert [update.text for update in updates] == ['{"answer":"ok"}']
    assert final.value == StructuredPayload(answer="ok")


# ──────────────── internal side-call suppression ─────────────────────────
#
# LAST_WORDS side calls go through ``_inner_get_response`` inside
# ``internal_side_call_scope()``.  If the model ignores the no-tools
# instruction and returns text alongside a function_call, the observer must NOT
# publish that text (or a batch-boundary signal) — the throwaway side-call
# response never joins the conversation.


class _ToolCallWireClient(WireClient):
    """A wire client whose responses carry text + function_call."""

    def __init__(self, *, on_intermediate_text: Callable[[str], None] | None = None) -> None:
        super().__init__(observer=WireCallObserver(on_intermediate_text=on_intermediate_text))

    @staticmethod
    def _contents() -> list[Content]:
        return [Content.from_text("Let me check"), Content.from_function_call("call-1", "tool")]

    def _send(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        del messages, options, kwargs

        async def _response() -> Any:
            return ChatResponse(messages=[Message("assistant", self._contents())])

        return _response()

    def _open_stream(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        del messages, options, kwargs

        async def _updates() -> Any:
            yield ChatResponseUpdate(contents=self._contents(), role="assistant")

        return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)


async def test_intermediate_text_suppressed_in_internal_side_call_non_streaming() -> None:
    fired: list[str] = []
    client = _ToolCallWireClient(on_intermediate_text=fired.append)

    with internal_side_call_scope():
        response = await client._inner_get_response(messages=[Message("user", ["hi"])], options={})

    assert response.text == "Let me check"
    assert fired == []

    # Control: the same response outside the scope does fire the callback.
    await client._inner_get_response(messages=[Message("user", ["hi"])], options={})
    assert fired == ["Let me check"]


async def test_intermediate_text_suppressed_in_internal_side_call_streaming() -> None:
    fired: list[str] = []
    client = _ToolCallWireClient(on_intermediate_text=fired.append)

    with internal_side_call_scope():
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        final = await stream.get_final_response()

    assert final.text == "Let me check"
    assert fired == []

    # Control: outside the scope the result hook publishes on finalization.
    stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
    await stream.get_final_response()
    assert fired == ["Let me check"]


# ──────────────── side-call exchange closure ─────────────────────────────
#
# A side call below the kernel opens its own exchange trace; nothing above it
# holds the handle, so a stream that never reaches a final response has to
# report its own end or the acquisition reads as one still in flight.


class _StreamWireClient(WireClient):
    """A wire client whose stream ends in *fail_with* (or normally)."""

    def __init__(self, fail_with: type[BaseException] | None) -> None:
        super().__init__(observer=WireCallObserver())
        self.fail_with = fail_with

    def _send(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("this double only streams")

    def _open_stream(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        del messages, options, kwargs
        fail_with = self.fail_with

        async def _updates() -> Any:
            yield ChatResponseUpdate(contents=[Content.from_text("partial")], role="assistant")
            if fail_with is not None:
                raise fail_with()

        return ResponseStream(_updates(), finalizer=ChatResponse.from_updates)


async def _drain_side_call_stream(client: Any, sink: FakeSink) -> None:
    with trajectory_scope(make_context(sink)), internal_side_call_scope(), side_call_scope(ActorRole.COMPLETER):
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        await stream.get_final_response()


async def test_a_side_call_stream_that_errors_closes_its_own_exchange() -> None:
    sink = FakeSink()
    with pytest.raises(RuntimeError):
        await _drain_side_call_stream(_StreamWireClient(RuntimeError), sink)

    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == ExchangeOutcome.ERROR
    assert finished.payload["error_code"] == "RuntimeError"
    assert finished.operation_id == sink.only(EventType.MODEL_EXCHANGE_STARTED).operation_id


async def test_a_side_call_stream_dropped_mid_flight_closes_its_own_exchange() -> None:
    sink = FakeSink()
    with pytest.raises(asyncio.CancelledError):
        await _drain_side_call_stream(_StreamWireClient(asyncio.CancelledError), sink)

    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["outcome"] == ExchangeOutcome.ABANDONED


async def test_a_side_call_stream_that_finishes_reports_success_once() -> None:
    sink = FakeSink()
    await _drain_side_call_stream(_StreamWireClient(None), sink)

    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["outcome"] == ExchangeOutcome.SUCCESS


async def test_a_forwarded_exchange_is_left_to_the_loop_that_owns_it() -> None:
    """The loop closes its own exchanges with the outcome it knows (stalled,
    interrupted), so a failing stream must not close them first."""
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange(new_analytics_id())
    client = _StreamWireClient(RuntimeError)
    with trajectory_scope(context), pytest.raises(RuntimeError):
        stream = client._inner_get_response(
            messages=[Message("user", ["hi"])],
            options={},
            stream=True,
            **{TRAJECTORY_EXCHANGE_KWARG: ExchangeTrace(context)},
        )
        await stream.get_final_response()

    assert sink.of_type(EventType.MODEL_EXCHANGE_STARTED)
    assert not sink.of_type(EventType.MODEL_EXCHANGE_FINISHED)


async def test_a_per_request_model_override_cannot_grow_past_the_line_budget() -> None:
    """A profile's chat options are unrestricted, and the per-request model
    override is the one request fact only the start marker carries: one long
    enough to make that line unwritable would leave the terminal closing a
    start that became a gap."""
    sink = FakeSink()
    client = _StreamWireClient(None)
    client.model = "client-model"
    with trajectory_scope(make_context(sink)), internal_side_call_scope(), side_call_scope(ActorRole.COMPLETER):
        stream = client._inner_get_response(
            messages=[Message("user", ["hi"])], options={"model": "m" * 200_000}, stream=True
        )
        await stream.get_final_response()

    # The sink applies the writer's own checks, so an unbounded override fails
    # here as the over-budget line it would have been.
    assert sink.only(EventType.MODEL_EXCHANGE_STARTED).payload["request_model"] == "m" * 256
    assert sink.only(EventType.MODEL_EXCHANGE_FINISHED).payload["outcome"] == ExchangeOutcome.SUCCESS


async def test_a_request_without_a_model_override_reports_the_clients_model() -> None:
    """A side call names no model in its options, and a failed one has no response to name it."""
    sink = FakeSink()
    client = _StreamWireClient(None)
    client.model = "client-model"
    with trajectory_scope(make_context(sink)), internal_side_call_scope(), side_call_scope(ActorRole.COMPLETER):
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        await stream.get_final_response()

    assert sink.only(EventType.MODEL_EXCHANGE_STARTED).payload["request_model"] == "client-model"


# ──────────────── the base call path ─────────────────────────────────────


@pytest.mark.parametrize("observed", [True, False], ids=["observed", "bare"])
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_the_exchange_handle_never_reaches_the_protocol(observed: bool, stream: bool) -> None:
    """The loop's trajectory handle is dropped before dispatch, with or without an observer."""
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange(new_analytics_id())
    client = _ScriptedWireClient(observed=observed)

    with trajectory_scope(context):
        result = client._inner_get_response(
            messages=[Message("user", ["hi"])],
            options={},
            stream=stream,
            other="kept",
            **{TRAJECTORY_EXCHANGE_KWARG: ExchangeTrace(context)},
        )
        if stream:
            await result.get_final_response()
        else:
            await result

    assert client.calls[0]["stream"] is stream
    assert client.calls[0]["kwargs"] == {"other": "kept"}
    # Only an observed client reports the request it was handed.
    assert bool(sink.of_type(EventType.MODEL_EXCHANGE_STARTED)) is observed


class _RejectingWireClient(WireClient):
    """A wire client whose protocol half rejects the request before returning anything."""

    def __init__(self, exc: Exception) -> None:
        super().__init__(observer=WireCallObserver())
        self.exc = exc

    def _send(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        raise self.exc

    def _open_stream(self, *, messages: Any, options: Any, **kwargs: Any) -> Any:
        raise self.exc


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_request_the_protocol_rejects_fails_its_started_exchange(stream: bool) -> None:
    sink = FakeSink()
    error = ValueError("rejected before sending")
    client = _RejectingWireClient(error)

    with (
        trajectory_scope(make_context(sink)),
        internal_side_call_scope(),
        side_call_scope(ActorRole.COMPLETER),
        pytest.raises(ValueError) as exc_info,
    ):
        client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=stream)

    assert exc_info.value is error
    finished = sink.only(EventType.MODEL_EXCHANGE_FINISHED)
    assert finished.payload["outcome"] == ExchangeOutcome.ERROR
    assert finished.payload["error_code"] == "ValueError"
    assert finished.operation_id == sink.only(EventType.MODEL_EXCHANGE_STARTED).operation_id


def test_a_client_without_request_headers_leaves_the_request_unstamped() -> None:
    request: dict[str, Any] = {"model": "m", "extra_headers": {"X-Caller": "kept"}}

    _ScriptedWireClient()._stamp_request_headers(request)

    assert request == {"model": "m", "extra_headers": {"X-Caller": "kept"}}


def test_a_client_with_request_headers_stamps_them() -> None:
    request: dict[str, Any] = {"model": "m"}

    _ScriptedWireClient(request_headers=RequestHeaders(session_id="s"))._stamp_request_headers(request)

    assert request["extra_headers"][SESSION_ID_HEADER] == "s"
