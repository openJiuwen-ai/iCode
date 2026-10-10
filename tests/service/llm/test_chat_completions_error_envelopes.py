# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Error envelopes a gateway answers a Chat Completions request with under HTTP 200.

They are raised as the service's own error, with the status, code, type and
message as sent, so the classifier judges each by what it says: a failure a
retry meets again (authentication, quota, a rejected request) is final, any
other may pass on a retry. A body that is no envelope stays an invalid
response.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from chrys.foundation.errors import ErrorKind, classify_error
from chrys.kernel import ChatClientException, Message, ResponseStream
from chrys.kernel.loop import StallExhaustedAction
from chrys.kernel.middleware import ChatMiddlewareLayer
from chrys.orchestration.invoker.attempts import WireRetryPolicyAdapter
from chrys.service.agent_middleware.response_validation import ResponseValidationMiddleware
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions.validation import _INVALID_STREAM_BODY_CAPTURE_LIMIT, ErrorEnvelopeError
from tests.support.scripted_wire import ScriptedWire
from tests.support.transcript_invariants import InvariantCheckedToolLoopLayer
from tests.support.wire_cases import Reply
from tests.support.wire_cases._kit import json_reply, sse_reply

# What the gateway in the field sent: its own code and type, and a message
# that is itself JSON text.
_BUSY = {
    "code": "3004080000",
    "type": "ServiceUnavailableError",
    "message": '{"code":"ServiceUnavailable","message":"service busy"}',
}


@asynccontextmanager
async def _client(*replies: Reply) -> AsyncIterator[tuple[ChatCompletionsClient, ScriptedWire]]:
    wire = ScriptedWire(replies)
    # No proxy from the environment may route around the scripted transport.
    http_client = httpx.AsyncClient(transport=wire.transport, trust_env=False)
    sdk_client = AsyncOpenAI(api_key="sk-test", base_url="https://api.test/v1", max_retries=0, http_client=http_client)
    client = ChatCompletionsClient(model="test", sdk_client=sdk_client)
    try:
        yield client, wire
    finally:
        await client.aclose()


async def _failure(reply: Reply, *, stream: bool) -> ChatClientException:
    """The error the client raises for *reply*."""
    async with _client(reply) as (client, _):
        result = client._inner_get_response(messages=[Message("user", ["hi"])], options={}, stream=stream)
        with pytest.raises(ChatClientException) as raised:
            if isinstance(result, ResponseStream):
                _ = [update async for update in result]
            else:
                await result
    return raised.value


def _answer(text: str) -> Reply:
    chunk = {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 1_717_171_717,
        "model": "test",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
    return sse_reply([(None, chunk), (None, "[DONE]")])


@pytest.mark.parametrize(
    ("error", "kind", "retryable"),
    [
        pytest.param(_BUSY, ErrorKind.UNKNOWN, True, id="gateway-busy"),
        pytest.param(
            {"code": "server_error", "type": "server_error", "message": "Overloaded."},
            ErrorKind.SERVER_ERROR,
            True,
            id="server-error",
        ),
        pytest.param("upstream timeout", ErrorKind.UNKNOWN, True, id="message-alone"),
        pytest.param(
            {"code": "invalid_api_key", "type": "invalid_request_error", "message": "Incorrect API key provided."},
            ErrorKind.AUTH_FAILED,
            False,
            id="bad-key",
        ),
        pytest.param(
            {"code": "invalid_api_key", "message": "Incorrect API key provided."},
            ErrorKind.AUTH_FAILED,
            False,
            id="bad-key-code-alone",
        ),
        pytest.param(
            {"code": None, "type": "invalid_request_error", "param": "temperature", "message": "Bad temperature."},
            ErrorKind.REQUEST_REJECTED,
            False,
            id="bad-parameter",
        ),
        pytest.param(
            {"code": "insufficient_quota", "message": "You exceeded your current quota."},
            ErrorKind.QUOTA_EXHAUSTED,
            False,
            id="quota",
        ),
        pytest.param(
            {"type": "authentication_error", "message": "Invalid x-api-key."},
            ErrorKind.AUTH_FAILED,
            False,
            id="auth-type",
        ),
        pytest.param(
            {"code": "authentication_error", "message": "Invalid key."},
            ErrorKind.AUTH_FAILED,
            False,
            id="auth-code",
        ),
        pytest.param(
            {"code": "context_length_exceeded", "message": "This model's maximum context length is 8192 tokens."},
            ErrorKind.CONTEXT_OVERFLOW,
            False,
            id="context-overflow",
        ),
        pytest.param(
            {"code": None, "type": "content_filter", "message": "The response was filtered."},
            ErrorKind.CONTENT_FILTERED,
            False,
            id="content-filter-type",
        ),
    ],
)
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_an_error_envelope_under_http_200_is_judged_by_its_code_and_type(
    error: dict[str, Any] | str, kind: ErrorKind, retryable: bool, stream: bool
) -> None:
    raised = await _failure(json_reply({"error": error}), stream=stream)

    classified = classify_error(raised)
    assert (classified.kind, classified.retryable) == (kind, retryable)
    envelope = raised.__cause__
    assert isinstance(envelope, ErrorEnvelopeError)
    assert envelope.status_code == 200
    signal = classified.signal
    assert signal is not None
    assert signal.source is envelope
    assert signal.status_code == 200
    details = error if isinstance(error, dict) else {"message": error}
    assert (signal.code, signal.error_type, signal.message) == (
        details.get("code"),
        details.get("type"),
        details["message"],
    )


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"status": "busy"}, id="no-error-member"),
        pytest.param({"error": None}, id="null-error"),
        pytest.param({"error": {}}, id="empty-error"),
        pytest.param({"error": " "}, id="blank-error"),
        pytest.param([{"error": _BUSY}], id="array"),
    ],
)
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_a_json_body_that_is_no_error_envelope_stays_an_invalid_response(body: Any, stream: bool) -> None:
    raised = await _failure(json_reply(body), stream=stream)

    classified = classify_error(raised)
    assert (classified.kind, classified.retryable) == (ErrorKind.INVALID_RESPONSE, False)
    assert not isinstance(raised.__cause__, ErrorEnvelopeError)


async def test_a_compressed_error_envelope_on_a_stream_request_is_read_decoded() -> None:
    body = gzip.compress(json.dumps({"error": _BUSY}).encode("utf-8"))
    reply = Reply(200, body, (("content-type", "application/json"), ("content-encoding", "gzip")))

    raised = await _failure(reply, stream=True)

    assert isinstance(raised.__cause__, ErrorEnvelopeError)
    assert classify_error(raised).retryable is True


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_an_error_envelope_led_by_a_byte_order_mark_is_read_as_one(stream: bool) -> None:
    body = b"\xef\xbb\xbf" + json.dumps({"error": _BUSY}).encode("utf-8")

    raised = await _failure(Reply(200, body, (("content-type", "application/json"),)), stream=stream)

    assert isinstance(raised.__cause__, ErrorEnvelopeError)
    assert classify_error(raised).retryable is True


async def test_an_error_envelope_cut_off_at_the_stream_capture_limit_stays_an_invalid_response() -> None:
    # Only the start of a body a stream request cannot read is kept: what it
    # holds may parse as a whole envelope, yet more of the body went unread.
    envelope = json.dumps({"error": _BUSY})
    body = envelope + " " * _INVALID_STREAM_BODY_CAPTURE_LIMIT

    raised = await _failure(Reply(200, body.encode("utf-8"), (("content-type", "application/json"),)), stream=True)

    assert classify_error(raised).kind is ErrorKind.INVALID_RESPONSE
    assert not isinstance(raised.__cause__, ErrorEnvelopeError)


def _wire_policy(validation: ResponseValidationMiddleware, retried: list[BaseException]) -> WireRetryPolicyAdapter:
    """Up to two wire retries without waiting, each one recorded."""

    async def no_sleep(_seconds: int) -> bool:
        return False

    async def publish(_message: str, _attempt: int, _total: int, _delay: int, error: BaseException) -> None:
        retried.append(error)

    return WireRetryPolicyAdapter(
        max_retries=2,
        stall_timeout_seconds=10.0,
        stall_max_retries=0,
        stall_exhausted_action=StallExhaustedAction.RAISE,
        backoff_schedule=(0,),
        interrupted=lambda: False,
        interruptible_sleep=no_sleep,
        publish_retry=publish,
        hosted_commits_in_flight=validation.hosted_commits_in_flight,
    )


@pytest.mark.parametrize("stream", [True, False], ids=["stream", "blocking"])
async def test_the_wire_retry_sends_again_after_a_transient_error_envelope(stream: bool) -> None:
    validation = ResponseValidationMiddleware(backoff_schedule=(0,))
    retried: list[BaseException] = []
    answer = _answer("Hi") if stream else json_reply(_completion("Hi"))

    async with _client(json_reply({"error": _BUSY}), answer) as (client, wire):
        layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(client, middleware=[validation]))
        result = layer.get_response(
            [Message("user", ["hi"])],
            stream=stream,
            options={},
            client_kwargs={"wire_retry_policy": _wire_policy(validation, retried)},
        )
        response = await result.get_final_response() if isinstance(result, ResponseStream) else await result

    assert response.text == "Hi"
    assert len(wire.requests) == 2
    assert [type(error.__cause__) for error in retried] == [ErrorEnvelopeError]


async def test_the_wire_retry_does_not_send_again_after_a_final_error_envelope() -> None:
    validation = ResponseValidationMiddleware(backoff_schedule=(0,))
    retried: list[BaseException] = []
    bad_key = {"code": "invalid_api_key", "type": "invalid_request_error", "message": "Incorrect API key provided."}

    async with _client(json_reply({"error": bad_key})) as (client, wire):
        layer = InvariantCheckedToolLoopLayer(ChatMiddlewareLayer(client, middleware=[validation]))
        result = layer.get_response(
            [Message("user", ["hi"])],
            stream=True,
            options={},
            client_kwargs={"wire_retry_policy": _wire_policy(validation, retried)},
        )
        assert isinstance(result, ResponseStream)
        with pytest.raises(ChatClientException) as raised:
            await result.get_final_response()

    assert isinstance(raised.value.__cause__, ErrorEnvelopeError)
    assert len(wire.requests) == 1
    assert retried == []


def _completion(text: str) -> dict[str, Any]:
    return {
        "id": "completion-1",
        "object": "chat.completion",
        "created": 1_717_171_717,
        "model": "test",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
    }
