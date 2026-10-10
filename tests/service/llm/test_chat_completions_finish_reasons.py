# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat Completions finish reasons: spellings, failure reasons, refused calls and streams that end without one."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable, Sequence
from types import ModuleType
from typing import Any

import pytest
from openai import APIError
from openai.types import CompletionUsage
from openai.types.chat.chat_completion import ChatCompletion, Choice
from openai.types.chat.chat_completion_chunk import (
    ChatCompletionChunk,
    ChoiceDelta,
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_chunk import Choice as ChunkChoice
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_function_tool_call import ChatCompletionMessageFunctionToolCall, Function

from chrys.foundation.errors import (
    ErrorKind,
    ProviderResponseError,
    classify_error,
    invalidates_continuation_token,
    is_context_overflow,
)
from chrys.kernel import (
    CONTEXT_WINDOW_FILLED_KEY,
    ChatClientException,
    ChatResponse,
    Content,
    Message,
    ResponseStream,
    tool,
    wire_progress_scope,
)
from chrys.kernel.loop import StallExhaustedAction
from chrys.kernel.middleware import ChatMiddlewareLayer
from chrys.orchestration.invoker.attempts import WireRetryPolicyAdapter
from chrys.service.agent_middleware.response_validation import ResponseValidationMiddleware
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions import client as chat_completions_client
from chrys.service.llm.chat_completions.stream import StreamState
from chrys.service.profiles.models.options import STREAM_REQUIRES_FINISH_REASON_OPTION
from tests.support.openai_chat_wire import ChatReply, scripted_openai
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer

_STREAM_LOGGER = "chrys.service.llm.chat_completions.stream"
_NO_FINISH_WARNING = "ended without a finish reason"


def _chunk(delta: ChoiceDelta | None, *, finish_reason: str | None = None) -> ChatCompletionChunk:
    choice = ChunkChoice.model_construct(index=0, delta=delta, finish_reason=finish_reason)
    return ChatCompletionChunk.model_construct(
        id="chunk-1", object="chat.completion.chunk", created=1_717_171_717, model="test", choices=[choice], usage=None
    )


def _text(text: str, *, finish_reason: str | None = None) -> ChatCompletionChunk:
    return _chunk(ChoiceDelta.model_construct(role="assistant", content=text), finish_reason=finish_reason)


def _refusal(text: str) -> ChatCompletionChunk:
    return _chunk(ChoiceDelta.model_construct(role="assistant", refusal=text))


def _fragment(arguments: str, *, index: int = 0, name: str | None = "read_file") -> ChoiceDeltaToolCall:
    return ChoiceDeltaToolCall.model_construct(
        index=index,
        id=f"call_{index}",
        type="function",
        function=ChoiceDeltaToolCallFunction.model_construct(name=name, arguments=arguments),
    )


def _call(arguments: str, *, index: int = 0, finish_reason: str | None = None) -> ChatCompletionChunk:
    fragment = _fragment(arguments, index=index)
    return _chunk(ChoiceDelta.model_construct(role="assistant", tool_calls=[fragment]), finish_reason=finish_reason)


def _completion(
    *,
    content: str | None = None,
    refusal: str | None = None,
    reasoning: str | None = None,
    calls: int = 0,
    finish_reason: str | None = "stop",
) -> ChatCompletion:
    tool_calls = [
        ChatCompletionMessageFunctionToolCall(
            id=f"call_{index}", type="function", function=Function(name="read_file", arguments='{"path": "a"}')
        )
        for index in range(calls)
    ]
    # Set only when given: an absent field stays absent on the wire.
    extra = {} if reasoning is None else {"reasoning_content": reasoning}
    message = ChatCompletionMessage.model_construct(
        role="assistant", content=content, refusal=refusal, tool_calls=tool_calls or None, **extra
    )
    choice = Choice.model_construct(index=0, message=message, finish_reason=finish_reason)
    return ChatCompletion.model_construct(
        id="completion-1", object="chat.completion", created=1_717_171_717, model="test", choices=[choice], usage=None
    )


async def _respond(
    reply: ChatReply,
    *,
    options: dict[str, Any] | None = None,
    done: bool = True,
    breaks_off: bool = False,
    held_open: bool = False,
) -> tuple[ChatResponse, list[dict[str, Any]]]:
    """The response *reply* decodes to, and the request bodies sent for it."""
    stream = not isinstance(reply, ChatCompletion)
    async with scripted_openai([reply], done=done, breaks_off=breaks_off, held_open=held_open) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        result = client._inner_get_response(
            messages=[Message("user", ["hi"])], options=dict(options or {}), stream=stream
        )
        if isinstance(result, ResponseStream):
            _ = [update async for update in result]
            return await result.get_final_response(), wire.requests
        return await result, wire.requests


def _calls(response: ChatResponse) -> list[Content]:
    return [content for message in response.messages for content in message.contents if content.type == "function_call"]


# ---------------------------------------------------------------------------
# Spellings and failure reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize(
    ("sent", "read"),
    [
        ("stop", "stop"),
        ("end", "stop"),
        ("length", "length"),
        ("sensitive", "content_filter"),
        ("vendor_reason", "vendor_reason"),
    ],
)
async def test_finish_reasons_are_read_in_the_kernels_spelling(stream: bool, sent: str, read: str) -> None:
    reply: ChatReply = [_text("Hi", finish_reason=sent)] if stream else _completion(content="Hi", finish_reason=sent)

    response, _ = await _respond(reply)

    assert response.finish_reason == read
    assert CONTEXT_WINDOW_FILLED_KEY not in response.additional_properties
    assert response.text == "Hi"


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize(
    ("reason", "kind"),
    [("network_error", ErrorKind.STREAM_TRUNCATED), ("insufficient_system_resource", ErrorKind.OVERLOADED)],
)
async def test_a_failed_completion_raises_a_retryable_provider_error(
    stream: bool, reason: str, kind: ErrorKind
) -> None:
    reply: ChatReply = (
        [_text("Partial", finish_reason=reason)] if stream else _completion(content="Partial", finish_reason=reason)
    )

    with pytest.raises(ProviderResponseError) as raised:
        await _respond(reply)

    assert raised.value.code == reason
    assert (classify_error(raised.value).kind, classify_error(raised.value).retryable) == (kind, True)
    assert invalidates_continuation_token(raised.value) is False


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_context_overflow_finish_reason_fails_without_retry(stream: bool) -> None:
    reason = "model_context_window_exceeded"
    reply: ChatReply = [_text("", finish_reason=reason)] if stream else _completion(finish_reason=reason)

    with pytest.raises(ProviderResponseError) as raised:
        await _respond(reply)

    assert is_context_overflow(raised.value)
    assert classify_error(raised.value).retryable is False
    assert invalidates_continuation_token(raised.value) is True
    assert raised.value.provider_message == "The model filled its context window before it produced an answer."


def _reasoning(text: str) -> ChatCompletionChunk:
    return _chunk(ChoiceDelta.model_construct(role="assistant", reasoning_content=text))


_FILLED = "model_context_window_exceeded"


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_completion(reasoning="Thinking", finish_reason=_FILLED), id="blocking_reasoning"),
        pytest.param([_reasoning("Thinking"), _text("", finish_reason=_FILLED)], id="streamed_reasoning"),
        # Reasoning models often send a blank line as they turn to answering.
        pytest.param(
            _completion(content="\n\n", reasoning="Thinking", finish_reason=_FILLED), id="blocking_whitespace"
        ),
        pytest.param(
            [_reasoning("Thinking"), _text("\n\n"), _text("", finish_reason=_FILLED)], id="streamed_whitespace"
        ),
        # A call fragment without a name is dropped: no call was made.
        pytest.param(
            [
                _chunk(ChoiceDelta.model_construct(role="assistant", tool_calls=[_fragment('{"pa', name=None)])),
                _text("", finish_reason=_FILLED),
            ],
            id="nameless_call_fragment",
        ),
    ],
)
async def test_a_context_window_filled_before_an_answer_fails_without_retry(reply: ChatReply) -> None:
    with pytest.raises(ProviderResponseError) as raised:
        await _respond(reply)

    assert (raised.value.code, classify_error(raised.value).retryable) == (_FILLED, False)


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_completion(content="Partial", finish_reason="model_context_window_exceeded"), id="blocking_text"),
        pytest.param(
            _completion(refusal="Partial", finish_reason="model_context_window_exceeded"), id="blocking_refusal"
        ),
        pytest.param([_text("Partial", finish_reason="model_context_window_exceeded")], id="text_in_the_same_chunk"),
        pytest.param([_text("Partial"), _text("", finish_reason="model_context_window_exceeded")], id="text_before"),
        pytest.param(
            [_refusal("Partial"), _text("", finish_reason="model_context_window_exceeded")], id="refusal_before"
        ),
    ],
)
async def test_a_context_window_filled_mid_answer_keeps_it_as_cut_off(reply: ChatReply) -> None:
    # The request fit: the model was already answering when it filled the window.
    response, _ = await _respond(reply)

    assert response.text == "Partial"
    assert response.finish_reason == "length"
    # A caller that would rather send a smaller prompt can tell.
    assert response.additional_properties[CONTEXT_WINDOW_FILLED_KEY] is True


async def test_a_context_window_filled_mid_call_reads_as_cut_off() -> None:
    response, _ = await _respond([_call('{"path": "a"}'), _text("", finish_reason="model_context_window_exceeded")])

    assert response.finish_reason == "length"
    assert response.additional_properties[CONTEXT_WINDOW_FILLED_KEY] is True


@pytest.mark.parametrize("late", ["network_error", "model_context_window_exceeded", "length"])
async def test_a_finish_reason_after_a_choice_finished_leaves_its_answer(late: str) -> None:
    response, requests = await _respond([_text("Hi", finish_reason="stop"), _text("", finish_reason=late)])

    assert (response.text, response.finish_reason, len(requests)) == ("Hi", "stop", 1)
    assert CONTEXT_WINDOW_FILLED_KEY not in response.additional_properties


async def test_a_failure_after_released_calls_still_runs_them() -> None:
    runs, requests, raised = await _tool_runs(
        [_call('{"path": "a"}', finish_reason="tool_calls"), _text("", finish_reason="network_error")]
    )

    assert (runs, requests, raised) == (["a"], 2, None)


# ---------------------------------------------------------------------------
# Refused or filtered responses never run their calls
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param([_call('{"path": "a"}', finish_reason="content_filter")], id="filtered_in_the_calls_chunk"),
        pytest.param([_call('{"path": "a"}', finish_reason="sensitive")], id="sensitive_in_the_calls_chunk"),
        pytest.param([_call('{"path": "a"}'), _text("", finish_reason="content_filter")], id="filtered_after_calls"),
        pytest.param([_refusal("I can't."), _call('{"path": "a"}', finish_reason="tool_calls")], id="refusal_first"),
        pytest.param(
            [_call('{"path": "a"}'), _refusal("I can't."), _text("", finish_reason="tool_calls")], id="refusal_after"
        ),
        pytest.param([_refusal("I can't."), _call('{"path": "a"}')], id="refusal_then_eof"),
    ],
)
async def test_a_refused_stream_runs_none_of_its_calls(chunks: Sequence[ChatCompletionChunk]) -> None:
    emitted: list[Content] = []
    async with scripted_openai([chunks]) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        assert isinstance(stream, ResponseStream)
        with pytest.raises(ProviderResponseError) as raised:
            async for update in stream:
                emitted.extend(update.contents)

    assert [content for content in emitted if content.type == "function_call"] == []
    assert raised.value.code == "content_filter"
    assert classify_error(raised.value).kind is ErrorKind.CONTENT_FILTERED
    assert classify_error(raised.value).retryable is False
    assert invalidates_continuation_token(raised.value) is True


@pytest.mark.parametrize(
    "completion",
    [
        pytest.param(_completion(refusal="I can't.", calls=1, finish_reason="tool_calls"), id="refusal"),
        pytest.param(_completion(calls=1, finish_reason="content_filter"), id="filtered"),
        pytest.param(_completion(calls=1, finish_reason="sensitive"), id="sensitive"),
    ],
)
async def test_a_refused_completion_runs_none_of_its_calls(completion: ChatCompletion) -> None:
    with pytest.raises(ProviderResponseError) as raised:
        await _respond(completion)

    assert raised.value.code == "content_filter"
    assert invalidates_continuation_token(raised.value) is True


@pytest.mark.parametrize("reason", ["network_error", "model_context_window_exceeded"])
@pytest.mark.parametrize(
    "shape",
    ["blocking", "streaming", "refusal_in_the_failing_chunk", "refusal_in_the_failing_chunk_after_released_calls"],
)
async def test_a_refusal_with_calls_outranks_a_failed_finish_reason(shape: str, reason: str) -> None:
    replies: dict[str, ChatReply] = {
        "blocking": _completion(refusal="I can't.", calls=1, finish_reason=reason),
        "streaming": [_refusal("I can't."), _call('{"path": "a"}'), _text("", finish_reason=reason)],
        "refusal_in_the_failing_chunk": [
            _call('{"path": "a"}'),
            _chunk(ChoiceDelta.model_construct(role="assistant", refusal="I can't."), finish_reason=reason),
        ],
        "refusal_in_the_failing_chunk_after_released_calls": [
            _call('{"path": "a"}', finish_reason="tool_calls"),
            _on_choice(
                _chunk(ChoiceDelta.model_construct(role="assistant", refusal="I can't."), finish_reason=reason), 1
            ),
        ],
    }

    with pytest.raises(ProviderResponseError) as raised:
        await _respond(replies[shape])

    assert raised.value.code == "content_filter"
    assert classify_error(raised.value).retryable is False
    assert invalidates_continuation_token(raised.value) is True


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize(("refusal", "calls"), [("I can't.", 0), (None, 1)], ids=["refusal_only", "calls_only"])
async def test_without_a_refusal_with_calls_a_failed_finish_reason_stands(
    stream: bool, refusal: str | None, calls: int
) -> None:
    chunks = [*([_refusal(refusal)] if refusal else []), *([_call('{"path": "a"}')] if calls else [])]
    reply: ChatReply = (
        [*chunks, _text("", finish_reason="network_error")]
        if stream
        else _completion(refusal=refusal, calls=calls, finish_reason="network_error")
    )

    with pytest.raises(ProviderResponseError) as raised:
        await _respond(reply)

    assert (raised.value.code, classify_error(raised.value).retryable) == ("network_error", True)


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_a_refusal_without_calls_is_an_ordinary_answer(stream: bool) -> None:
    reply: ChatReply = (
        [_refusal("I can't help with that."), _text("", finish_reason="stop")]
        if stream
        else _completion(refusal="I can't help with that.")
    )

    response, _ = await _respond(reply)

    assert response.text == "I can't help with that."


async def test_calls_of_an_unrefused_stream_still_run() -> None:
    response, _ = await _respond([_text("Reading."), _call('{"path": "a"}', finish_reason="tool_calls")])

    assert [call.arguments for call in _calls(response)] == ['{"path": "a"}']


def _on_choice(chunk: ChatCompletionChunk, index: int) -> ChatCompletionChunk:
    [choice] = chunk.choices
    choice.index = index
    return chunk


async def _tool_runs(chunks: Sequence[ChatCompletionChunk | str]) -> tuple[list[str], int, BaseException | None]:
    """Drive *chunks* through the tool loop: the tool runs, the requests sent and what the run raised."""
    runs: list[str] = []

    @tool
    def read_file(path: str) -> str:
        runs.append(path)
        return "contents"

    async with scripted_openai([chunks, [_text("done", finish_reason="stop")]]) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        layer = InvariantCheckedToolLoopLayer(
            ChatMiddlewareLayer(client, middleware=[ResponseValidationMiddleware(backoff_schedule=(0,))])
        )
        result = layer.get_response([Message("user", ["go"])], stream=True, options={"tools": [read_file]})
        assert isinstance(result, ResponseStream)
        try:
            await result.get_final_response()
        except (ProviderResponseError, ChatClientException) as error:
            return runs, len(wire.requests), error
        return runs, len(wire.requests), None


@pytest.mark.parametrize(
    "late",
    [
        pytest.param(_refusal("I can't."), id="refusal"),
        pytest.param(_text("", finish_reason="content_filter"), id="filtered"),
        pytest.param(_on_choice(_refusal("I can't."), 1), id="refusal_on_another_choice"),
    ],
)
async def test_a_refusal_after_released_calls_still_runs_none_of_them(late: ChatCompletionChunk) -> None:
    runs, requests, raised = await _tool_runs([_call('{"path": "a"}', finish_reason="tool_calls"), late])

    assert (runs, requests) == ([], 1)
    assert isinstance(raised, ProviderResponseError)
    assert raised.code == "content_filter"


async def test_released_calls_run_when_no_refusal_follows() -> None:
    runs, requests, raised = await _tool_runs(
        [_call('{"path": "a"}', finish_reason="tool_calls"), _on_choice(_text("", finish_reason="stop"), 1)]
    )

    assert (runs, requests, raised) == (["a"], 2, None)


@pytest.mark.parametrize(
    "finished",
    [
        pytest.param(_call('{"path": "a"}', finish_reason="tool_calls"), id="calls"),
        pytest.param(_text("Hi", finish_reason="stop"), id="text"),
    ],
)
async def test_an_error_the_service_sends_after_a_finished_stream_fails_it(finished: ChatCompletionChunk) -> None:
    # Unlike a connection that breaks off after the end, the service itself
    # says the reply failed.
    runs, requests, raised = await _tool_runs(
        [finished, '{"error": {"code": "server_error", "message": "The reply failed."}}']
    )

    assert (runs, requests) == ([], 1)
    assert isinstance(raised, ChatClientException)
    assert isinstance(raised.__cause__, APIError)


def _cut_off(*fragments: ChoiceDeltaToolCall, refusal: str | None = None) -> ChatCompletionChunk:
    """A chunk ending its choice at the length limit with *fragments*, and *refusal* when given."""
    delta = ChoiceDelta.model_construct(role="assistant", refusal=refusal, tool_calls=list(fragments))
    return _chunk(delta, finish_reason="length")


_NAMELESS = _fragment("{", name=None)


@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param([_cut_off(_NAMELESS, refusal="I can't.")], id="same_chunk"),
        pytest.param([_refusal("I can't."), _cut_off(_NAMELESS)], id="refusal_first"),
        pytest.param([_cut_off(_NAMELESS), _refusal("I can't.")], id="refusal_after"),
    ],
)
async def test_a_refusal_beside_a_dropped_nameless_call_stays_an_answer(chunks: Sequence[ChatCompletionChunk]) -> None:
    response, _ = await _respond(chunks)

    assert response.text == "I can't."
    assert _calls(response) == []


async def test_a_named_call_beside_a_dropped_nameless_one_is_still_refused() -> None:
    named = _fragment('{"path": "a"}', index=1)
    runs, requests, raised = await _tool_runs([_cut_off(_NAMELESS, named, refusal="I can't.")])

    assert (runs, requests) == ([], 1)
    assert isinstance(raised, ProviderResponseError)
    assert raised.code == "content_filter"


# ---------------------------------------------------------------------------
# Usage a failed completion reported
# ---------------------------------------------------------------------------

_USAGE = CompletionUsage(prompt_tokens=70, completion_tokens=9, total_tokens=79)


def _reporting_usage(chunk: ChatCompletionChunk) -> ChatCompletionChunk:
    chunk.usage = _USAGE
    return chunk


def _completion_reporting_usage(**fields: Any) -> ChatCompletion:
    completion = _completion(**fields)
    completion.usage = _USAGE
    return completion


def _usage_only() -> ChatCompletionChunk:
    """The chunk ``include_usage`` adds after the finish reason: no choice, only the usage."""
    return ChatCompletionChunk.model_construct(
        id="chunk-1", object="chat.completion.chunk", created=1_717_171_717, model="test", choices=[], usage=_USAGE
    )


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(_completion_reporting_usage(content="Partial", finish_reason="network_error"), id="failed"),
        pytest.param(
            _completion_reporting_usage(refusal="I can't.", calls=1, finish_reason="tool_calls"), id="refused"
        ),
        pytest.param([_reporting_usage(_text("Partial", finish_reason="network_error"))], id="failed_stream"),
        pytest.param(
            [_refusal("I can't."), _reporting_usage(_call('{"path": "a"}', finish_reason="tool_calls"))],
            id="refused_stream",
        ),
        pytest.param(
            [_call('{"path": "a"}', finish_reason="tool_calls"), _reporting_usage(_on_choice(_refusal("I can't."), 1))],
            id="refused_after_released_calls_stream",
        ),
        pytest.param(
            [_refusal("I can't."), _call('{"path": "a"}'), _reporting_usage(_text("", finish_reason="network_error"))],
            id="refused_then_failed_stream",
        ),
        # The usage comes in a chunk of its own after the finish reason.
        pytest.param([_text("Partial", finish_reason="network_error"), _usage_only()], id="failed_stream_then_usage"),
        pytest.param(
            [_text("", finish_reason="model_context_window_exceeded"), _usage_only()],
            id="context_overflow_stream_then_usage",
        ),
        pytest.param(
            [_refusal("I can't."), _call('{"path": "a"}'), _text("", finish_reason="network_error"), _usage_only()],
            id="refused_then_failed_stream_then_usage",
        ),
        pytest.param(
            [_refusal("I can't."), _call('{"path": "a"}', finish_reason="tool_calls"), _usage_only()],
            id="refused_stream_then_usage",
        ),
        pytest.param(
            [_call('{"path": "a"}'), _text("", finish_reason="content_filter"), _usage_only()],
            id="filtered_stream_then_usage",
        ),
        pytest.param(
            [_call('{"path": "a"}', finish_reason="tool_calls"), _on_choice(_refusal("I can't."), 1), _usage_only()],
            id="refused_after_released_calls_stream_then_usage",
        ),
    ],
)
async def test_a_failed_completion_carries_the_usage_it_reported(reply: ChatReply) -> None:
    with pytest.raises(ProviderResponseError) as raised:
        await _respond(reply)

    usage = raised.value.usage_details
    assert usage is not None
    assert (usage["input_token_count"], usage["output_token_count"], usage["total_token_count"]) == (70, 9, 79)


def _refusal_with_a_call() -> ChatCompletionChunk:
    """A refusal and a call in one chunk, which finishes the choice."""
    delta = ChoiceDelta.model_construct(role="assistant", refusal="I can't.", tool_calls=[_fragment('{"path": "a"}')])
    return _chunk(delta, finish_reason="tool_calls")


@pytest.mark.parametrize(
    ("settling", "code"),
    [
        pytest.param(_text("Partial", finish_reason="network_error"), "network_error", id="failed"),
        pytest.param(_refusal_with_a_call(), "content_filter", id="refused_with_a_call"),
    ],
)
async def test_a_settled_stream_is_read_on_only_for_its_usage(settling: ChatCompletionChunk, code: str) -> None:
    # A refusal with a call that follows changes nothing: the stream settled.
    chunks = [settling, _on_choice(_refusal_with_a_call(), 1), _text("More"), _usage_only()]
    emitted: list[Content] = []
    async with scripted_openai([chunks]) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        assert isinstance(stream, ResponseStream)
        with pytest.raises(ProviderResponseError) as raised:
            async for update in stream:
                emitted.extend(update.contents)

    assert emitted == []
    assert raised.value.code == code
    assert raised.value.usage_details is not None


def test_a_failure_reported_with_its_usage_is_raised_at_that_chunk() -> None:
    # Nothing is left to wait for: a connection held open after it must not
    # hold the call until it stalls.
    state = StreamState(ChatCompletionsClient.VARIANT)

    with pytest.raises(ProviderResponseError) as raised:
        state.updates_for(_reporting_usage(_text("Partial", finish_reason="network_error")))

    assert raised.value.code == "network_error"
    assert raised.value.usage_details is not None


# ---------------------------------------------------------------------------
# Streams that break off or go quiet
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("chunks", "code", "retryable"),
    [
        pytest.param([_refusal("I can't."), _call('{"path": "a"}')], "content_filter", False, id="refusal_with_a_call"),
        pytest.param([_text("Partial", finish_reason="network_error")], "network_error", True, id="failed"),
        pytest.param(
            [_text("", finish_reason="model_context_window_exceeded")],
            "model_context_window_exceeded",
            False,
            id="context_overflow",
        ),
    ],
)
async def test_what_a_stream_reported_outranks_its_connection_breaking_off(
    chunks: Sequence[ChatCompletionChunk], code: str, retryable: bool
) -> None:
    with pytest.raises(ProviderResponseError) as raised:
        await _respond(chunks, breaks_off=True)

    assert (raised.value.code, classify_error(raised.value).retryable) == (code, retryable)


def _stall_policy(validation: ResponseValidationMiddleware) -> WireRetryPolicyAdapter:
    """No retry of any kind: a stall raises ``StreamStall`` after 10 s without progress."""

    async def no_sleep(_seconds: int) -> bool:
        return False

    async def publish(_message: str, _attempt: int, _total: int, _delay: int, _error: BaseException) -> None:
        return None

    return WireRetryPolicyAdapter(
        max_retries=0,
        stall_timeout_seconds=10.0,
        stall_max_retries=0,
        stall_exhausted_action=StallExhaustedAction.RAISE,
        backoff_schedule=(0,),
        interrupted=lambda: False,
        interruptible_sleep=no_sleep,
        publish_retry=publish,
        hosted_commits_in_flight=validation.hosted_commits_in_flight,
    )


@pytest.mark.parametrize(
    ("chunks", "code", "retryable"),
    [
        pytest.param([_refusal_with_a_call()], "content_filter", False, id="refused_with_a_call"),
        pytest.param(
            [_call('{"path": "a"}', finish_reason="tool_calls"), _on_choice(_refusal("I can't."), 1)],
            "content_filter",
            False,
            id="refused_after_released_calls",
        ),
        pytest.param([_text("Partial", finish_reason="network_error")], "network_error", True, id="failed"),
        pytest.param(
            [_text("", finish_reason="model_context_window_exceeded")],
            "model_context_window_exceeded",
            False,
            id="context_overflow",
        ),
    ],
)
async def test_a_settled_stream_that_goes_quiet_fails_as_it_settled_before_it_stalls(
    monkeypatch: pytest.MonkeyPatch, chunks: Sequence[ChatCompletionChunk], code: str, retryable: bool
) -> None:
    # What may follow is only its usage, waited on far shorter than a stall.
    monkeypatch.setattr(chat_completions_client, "_ENDED_STREAM_WAIT_SECONDS", 0)
    validation = ResponseValidationMiddleware(backoff_schedule=(0,))

    async with scripted_openai([chunks], held_open=True) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(client, middleware=[validation]))
        result = layer.get_response(
            [Message("user", ["hi"])],
            stream=True,
            options={},
            client_kwargs={"wire_retry_policy": _stall_policy(validation)},
        )
        assert isinstance(result, ResponseStream)
        with pytest.raises(ProviderResponseError) as raised:
            await result.get_final_response()

    assert (raised.value.code, classify_error(raised.value).retryable) == (code, retryable)
    assert len(wire.requests) == 1


async def _settle(
    chunks: Sequence[ChatCompletionChunk | str],
    *,
    held_open: bool = False,
    pace: Callable[[], Awaitable[object]] | None = None,
) -> ProviderResponseError:
    """The error the client raises for a stream of *chunks*, read to its end."""
    async with scripted_openai([chunks], held_open=held_open, pace=pace) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        assert isinstance(stream, ResponseStream)
        with pytest.raises(ProviderResponseError) as raised:
            _ = [update async for update in stream]
    return raised.value


async def test_the_chunk_that_settles_a_stream_restarts_the_stall_timer() -> None:
    # The stall watchdog times idle gaps from its last progress report. The
    # chunk is not yielded, so without the report the watchdog could fire
    # before the wait for the usage runs out, however short that wait is.
    reports: list[None] = []

    with wire_progress_scope(lambda: reports.append(None)):
        error = await _settle([_text("Partial"), _text("", finish_reason="network_error"), _usage_only()])

    assert error.code == "network_error"
    assert error.usage_details is not None
    assert reports == [None]


async def test_the_chunk_that_finishes_a_stream_restarts_the_stall_timer() -> None:
    reports: list[None] = []

    with wire_progress_scope(lambda: reports.append(None)):
        response, _ = await _respond([_text("Hi", finish_reason="stop"), _usage_only()])

    assert response.text == "Hi"
    assert response.usage_details is not None
    assert reports == [None]


async def test_a_settled_stream_raises_at_its_usage_without_reading_on() -> None:
    # Nothing is left to wait for: a connection held open after the usage
    # must not hold the call for the rest of the bound.
    events_sent = 0

    async def count() -> None:
        nonlocal events_sent
        events_sent += 1

    error = await _settle([_text("Partial", finish_reason="network_error"), _usage_only(), _text("More")], pace=count)

    assert (error.code, error.usage_details is not None) == ("network_error", True)
    assert events_sent == 2


class _TickingLoop:
    """A loop whose clock reads 1, 2, 3, … s, one tick per reading."""

    def __init__(self) -> None:
        self._ticks = itertools.count(1)

    def time(self) -> float:
        return float(next(self._ticks))


@pytest.mark.parametrize(
    ("chunks", "text", "reads_from"),
    [
        # A settled stream yields nothing more: what follows adds nothing.
        pytest.param(
            [_text("Partial", finish_reason="network_error"), _text("More"), _text("More"), _text("More")],
            None,
            [1, 1, 1, 1],
            id="settled",
        ),
        pytest.param(
            [_text("Hi", finish_reason="stop"), _text(""), _text(""), _usage_only()], "Hi", [1, 1, 1], id="finished"
        ),
        # A gateway may send the finish reason before the rest of the text.
        pytest.param(
            [_text("Hi", finish_reason="stop"), _text(" there"), _text(""), _text("!"), _usage_only()],
            "Hi there!",
            [1, 2, 2, 3],
            id="text_after_the_finish_reason",
        ),
    ],
)
async def test_what_follows_the_end_of_a_stream_is_waited_for_while_it_adds_to_the_answer(
    monkeypatch: pytest.MonkeyPatch, chunks: list[ChatCompletionChunk], text: str | None, reads_from: list[int]
) -> None:
    # Each read waits until the bound after the last chunk that added to the
    # answer: chunks that add nothing do not extend it, however many come.
    deadlines: list[float] = []

    def timeout_at(when: float) -> asyncio.Timeout:
        deadlines.append(when)
        return asyncio.timeout(None)

    loop = _TickingLoop()
    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), timeout_at=timeout_at, get_running_loop=lambda: loop)
    monkeypatch.setattr(chat_completions_client, "asyncio", shadow)

    if text is None:
        assert (await _settle(chunks)).code == "network_error"
    else:
        response, _ = await _respond(chunks)
        assert (response.text, response.usage_details is not None) == (text, True)
    bound = chat_completions_client._ENDED_STREAM_WAIT_SECONDS
    assert deadlines == [tick + bound for tick in reads_from]
    # Far shorter than a stall, which the read timeout sets (300 s by default).
    assert 0 < bound <= 10


async def test_a_finished_stream_ends_at_its_usage_while_the_connection_stays_open(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # The usage is the last thing a stream sends: the call does not wait on.
    with caplog.at_level(logging.WARNING, logger=chat_completions_client.__name__):
        response, _ = await _respond([_text("Hi", finish_reason="stop"), _usage_only()], held_open=True)

    assert (response.text, response.usage_details is not None) == ("Hi", True)
    assert [record for record in caplog.records if record.name == chat_completions_client.__name__] == []


def _late_choice() -> ChatCompletionChunk:
    """A chunk for a choice no chunk before it named: the requests ask for one."""
    choice = ChunkChoice.model_construct(
        index=1, delta=ChoiceDelta.model_construct(role="assistant", content="Other"), finish_reason=None
    )
    return ChatCompletionChunk.model_construct(
        id="chunk-1", object="chat.completion.chunk", created=1_717_171_717, model="test", choices=[choice], usage=None
    )


@pytest.mark.parametrize("end", ["breaks_off", "held_open"])
async def test_a_choice_that_first_shows_up_after_the_end_is_cut_off_by_a_break(
    monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    monkeypatch.setattr(chat_completions_client, "_ENDED_STREAM_WAIT_SECONDS", 0.5)

    with pytest.raises(ChatClientException) as raised:
        await _respond(
            [_text("Hi", finish_reason="stop"), _late_choice()],
            breaks_off=end == "breaks_off",
            held_open=end == "held_open",
        )

    assert classify_error(raised.value).retryable is True


# A call fragment whose arguments are no string, which the SDK keeps as sent:
# a chunk that carries one cannot be read past it.
_UNREADABLE_FRAGMENT = (
    '{"index": 1, "id": "call_bad", "type": "function", "function": {"name": "read_file", "arguments": {}}}'
)


# Each is data the SDK makes a chunk of that cannot be read: it takes ``null``
# for ``None``. The last two are read in part before they fail: a call begun,
# a filter reported.
@pytest.mark.parametrize(
    "trailing",
    [
        pytest.param('{"choices": null}', id="no_chunk"),
        pytest.param('{"choices": [null]}', id="null_choice"),
        pytest.param("null", id="null"),
        pytest.param(
            '{"id": "chunk-1", "created": 1717171717, "model": "test", "choices": [{"index": 1, "delta": '
            '{"role": "assistant", "tool_calls": [{"index": 0, "id": "call_late", "type": "function", '
            f'"function": {{"name": "read_file", "arguments": "{{}}"}}}}, {_UNREADABLE_FRAGMENT}]}}, '
            '"finish_reason": null}]}',
            id="late_call",
        ),
        pytest.param(
            '{"id": "chunk-1", "created": 1717171717, "model": "test", "choices": [{"index": 0, "delta": {}, '
            '"finish_reason": "content_filter"}, {"index": 1, "delta": {"role": "assistant", "tool_calls": '
            f'[{_UNREADABLE_FRAGMENT}]}}, "finish_reason": null}}]}}',
            id="late_filter",
        ),
    ],
)
async def test_a_chunk_that_cannot_be_read_after_the_end_fails_the_reply(trailing: str) -> None:
    """As before the end, on purpose: skipped, it could keep a call it began or lose a filter it reports.

    The calls the stream finished with then never run.
    """
    with pytest.raises(ChatClientException):
        await _respond([_call('{"path": "a"}', finish_reason="tool_calls"), trailing])


@pytest.mark.parametrize(
    ("chunks", "code"),
    [
        pytest.param(
            [_text("", finish_reason="model_context_window_exceeded"), "{not json", _usage_only()],
            "model_context_window_exceeded",
            id="failed",
        ),
        pytest.param([_refusal_with_a_call(), "{not json"], "content_filter", id="refused_with_a_call"),
    ],
)
async def test_invalid_data_after_a_settled_stream_cannot_unsettle_it(
    chunks: Sequence[ChatCompletionChunk | str], code: str
) -> None:
    error = await _settle(chunks)

    assert (error.code, classify_error(error).retryable) == (code, False)


@pytest.mark.parametrize(
    ("chunks", "text", "reason"),
    [
        pytest.param([_text("Hi", finish_reason="stop")], "Hi", "stop", id="stopped"),
        pytest.param([_text("Partial", finish_reason="content_filter")], "Partial", "content_filter", id="filtered"),
        pytest.param([_text("", finish_reason="content_filter")], "", "content_filter", id="filtered_before_text"),
    ],
)
@pytest.mark.parametrize(
    ("end", "trailing", "logged"),
    [
        pytest.param("breaks_off", [], "failed after its end", id="breaks_off"),
        pytest.param("", ["{not json"], "failed after its end", id="invalid_data"),
        pytest.param("", ["\udcff"], "failed after its end", id="not_utf8"),
        pytest.param("held_open", [], "stayed open after its end", id="held_open"),
    ],
)
async def test_a_finished_stream_stands_however_its_connection_ends(
    monkeypatch: pytest.MonkeyPatch,
    chunks: list[ChatCompletionChunk],
    text: str,
    reason: str,
    end: str,
    trailing: list[str],
    logged: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Sent again, a filtered answer meets the same filter, and hosted work
    # the answer ran would block the resend.
    if end == "held_open":
        monkeypatch.setattr(chat_completions_client, "_ENDED_STREAM_WAIT_SECONDS", 0)

    with caplog.at_level(logging.WARNING, logger=chat_completions_client.__name__):
        response, requests = await _respond(
            [*chunks, *trailing], breaks_off=end == "breaks_off", held_open=end == "held_open"
        )

    assert (response.text, response.finish_reason, len(requests)) == (text, reason, 1)
    [record] = [record for record in caplog.records if record.name == chat_completions_client.__name__]
    assert logged in record.getMessage()
    # A connection held open is no failure to trace.
    assert bool(record.exc_info) is (end != "held_open")


async def test_what_follows_a_settled_stream_cannot_unsettle_it_by_being_malformed() -> None:
    malformed = ChatCompletionChunk.model_construct(
        id="chunk-1", object="chat.completion.chunk", created=1_717_171_717, model="test", choices=None, usage=None
    )

    error = await _settle([_text("", finish_reason="model_context_window_exceeded"), malformed, _usage_only()])

    assert (error.code, classify_error(error).retryable) == ("model_context_window_exceeded", False)


async def test_a_stream_that_breaks_off_before_deciding_anything_may_be_sent_again() -> None:
    # A refusal without calls decides nothing: it is an ordinary answer.
    with pytest.raises(ChatClientException) as raised:
        await _respond([_refusal("I can't.")], breaks_off=True)

    assert classify_error(raised.value).retryable is True


# ---------------------------------------------------------------------------
# Streams that end without a finish reason
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("done", [True, False], ids=["done", "eof"])
@pytest.mark.parametrize("arguments", ["", "  ", "{}", '{"path": "a"}'])
async def test_calls_with_whole_arguments_are_released_at_the_end(done: bool, arguments: str) -> None:
    response, _ = await _respond([_call(arguments)], done=done)

    assert [call.call_id for call in _calls(response)] == ["call_0"]


@pytest.mark.parametrize("done", [True, False], ids=["done", "eof"])
@pytest.mark.parametrize("arguments", ['{"path": ', "[]", "null", '"text"'])
async def test_a_call_cut_off_at_the_end_fails_the_whole_response(done: bool, arguments: str) -> None:
    emitted: list[Content] = []
    chunks = [_call('{"path": "a"}', index=0), _call(arguments, index=1)]
    async with scripted_openai([chunks], done=done) as wire:
        client = ChatCompletionsClient(model="test", sdk_client=wire.client)
        stream = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=True)
        assert isinstance(stream, ResponseStream)
        with pytest.raises(ProviderResponseError) as raised:
            async for update in stream:
                emitted.extend(update.contents)

    # The whole call batch is withheld, the complete call included.
    assert [content for content in emitted if content.type == "function_call"] == []
    assert raised.value.code == "stream_truncated"
    assert classify_error(raised.value).retryable is True
    assert invalidates_continuation_token(raised.value) is False


_UNFINISHED_ENDS = [
    pytest.param([_text("Hi")], id="no_finish_reason"),
    pytest.param([_text("Hi", finish_reason="")], id="empty_finish_reason"),
    pytest.param([_text("Hi"), _chunk(None)], id="null_delta"),
]


@pytest.mark.parametrize("done", [True, False], ids=["done", "eof"])
@pytest.mark.parametrize("chunks", _UNFINISHED_ENDS)
async def test_an_unfinished_text_stream_is_kept_with_a_warning(
    chunks: Sequence[ChatCompletionChunk], done: bool, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=_STREAM_LOGGER):
        response, _ = await _respond(chunks, done=done)

    assert response.text == "Hi"
    assert response.finish_reason is None
    assert [record.getMessage() for record in caplog.records if _NO_FINISH_WARNING in record.getMessage()] == [
        "Chat Completions stream ended without a finish reason; the answer may be incomplete"
    ]


@pytest.mark.parametrize("done", [True, False], ids=["done", "eof"])
@pytest.mark.parametrize("chunks", _UNFINISHED_ENDS)
async def test_an_unfinished_text_stream_fails_when_the_profile_requires_a_finish_reason(
    chunks: Sequence[ChatCompletionChunk], done: bool
) -> None:
    with pytest.raises(ProviderResponseError) as raised:
        await _respond(chunks, options={STREAM_REQUIRES_FINISH_REASON_OPTION: True}, done=done)

    assert raised.value.code == "stream_truncated"
    assert classify_error(raised.value).retryable is True


async def test_a_finished_stream_logs_nothing(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=_STREAM_LOGGER):
        response, _ = await _respond(
            [_text("Hi"), _text("", finish_reason="stop")], options={STREAM_REQUIRES_FINISH_REASON_OPTION: True}
        )

    assert response.text == "Hi"
    assert [record for record in caplog.records if _NO_FINISH_WARNING in record.getMessage()] == []


@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
async def test_the_finish_reason_requirement_is_never_sent(stream: bool) -> None:
    reply: ChatReply = [_text("Hi", finish_reason="stop")] if stream else _completion(content="Hi")

    _, requests = await _respond(reply, options={STREAM_REQUIRES_FINISH_REASON_OPTION: True})

    assert [STREAM_REQUIRES_FINISH_REASON_OPTION in request for request in requests] == [False]
