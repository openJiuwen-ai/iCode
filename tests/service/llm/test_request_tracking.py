# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request IDs cross the real SDK/HTTP boundary without merging concurrent calls."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import httpx
import pytest
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from chrys.foundation.trajectory.context import TRAJECTORY_EXCHANGE_KWARG, ExchangeTrace
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.foundation.util.chrys_headers import REQUEST_ATTEMPT_ID_HEADER
from chrys.kernel import Content
from chrys.kernel.types import ChatResponse, Message
from chrys.service.llm.anthropic_messages.client import AnthropicMessagesClient
from chrys.service.llm.chat_completions.client import ChatCompletionsClient
from chrys.service.llm.clients import _build_profile_http_client
from chrys.service.llm.observer import WireCallObserver
from chrys.service.llm.openai_responses.client import ResponsesApiClient
from chrys.service.llm.request_tracking import (
    REQUEST_ATTEMPT_ID_METADATA,
    RequestTracking,
    build_request_tracking_hooks,
)
from chrys.service.llm.wire_client import WireClient
from chrys.service.profiles.models.schema import ModelProfile
from tests.service.llm.test_route_extension_wire import _ANTHROPIC_REPLY, _OPENAI_REPLY
from tests.service.trajectory._fakes import FakeSink, make_context


def _profile(provider: str = "openai") -> ModelProfile:
    return ModelProfile(id="p", name="p", provider=provider, model_id="test-model", api_key="test")


@pytest.mark.parametrize("protocol", ["chat", "responses", "anthropic"])
async def test_wire_response_correlates_attempt_provider_and_exchange(protocol: str, tmp_path: Path) -> None:
    sink = FakeSink()
    trace = ExchangeTrace(make_context(sink).with_exchange(new_analytics_id()))
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers[REQUEST_ATTEMPT_ID_HEADER])
        if protocol == "anthropic":
            payload = _ANTHROPIC_REPLY
        elif protocol == "responses":
            payload = {
                "id": "resp_1",
                "object": "response",
                "created_at": 0,
                "model": "test-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_1",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "ok", "annotations": []}],
                    }
                ],
            }
        else:
            payload = _OPENAI_REPLY
        return httpx.Response(
            200, json=payload, headers={"request-id" if protocol == "anthropic" else "x-request-id": "req_provider"}
        )

    http = _build_profile_http_client(
        _profile("anthropic" if protocol == "anthropic" else "openai"),
        httpx.Timeout(5),
        transport=httpx.MockTransport(handle),
        raw_http_log_path=tmp_path / "http.jsonl",
    )
    observer = WireCallObserver()
    if protocol == "anthropic":
        client = AnthropicMessagesClient.from_sdk_client(
            AsyncAnthropic(api_key="test", http_client=http, max_retries=0), model="test-model", observer=observer
        )
    else:
        kind = ResponsesApiClient if protocol == "responses" else ChatCompletionsClient
        client = kind.from_sdk_client(
            AsyncOpenAI(api_key="test", http_client=http, max_retries=0), model="test-model", observer=observer
        )
    try:
        response = await client.get_response(
            [Message("user", ["hi"])], client_kwargs={TRAJECTORY_EXCHANGE_KWARG: trace}
        )
    finally:
        await client.aclose()
    assert len(sent) == 1
    assert response.additional_properties["request_attempt_id"] == sent[0]
    assert response.additional_properties["provider_request_id"] == "req_provider"
    assert response.messages[0].additional_properties[REQUEST_ATTEMPT_ID_METADATA] == sent[0]
    events = [
        d
        for d in sink.drafts
        if d.event_type in {EventType.MODEL_REQUEST_PREPARED, EventType.MODEL_REQUEST_HEADERS_RECEIVED}
    ]
    assert [d.event_type for d in events] == [
        EventType.MODEL_REQUEST_PREPARED,
        EventType.MODEL_REQUEST_HEADERS_RECEIVED,
    ]
    assert all(d.operation_id == trace.operation_id and d.payload["request_attempt_id"] == sent[0] for d in events)
    assert events[-1].payload["provider_request_id"] == "req_provider"
    records = [json.loads(line) for line in (tmp_path / "http.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {row["exchange_id"] for row in records} == set(sent)


async def test_redirects_and_concurrent_scopes_never_share_attempt_ids() -> None:
    sink = FakeSink()
    contexts = [make_context(sink).with_exchange(new_analytics_id()) for _ in range(2)]
    trackers = [RequestTracking(context) for context in contexts]
    arrived = asyncio.Event()
    count = 0
    sent: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal count
        sent.append(request.headers[REQUEST_ATTEMPT_ID_HEADER])
        if request.url.path == "/start":
            count += 1
            if count == 2:
                arrived.set()
            await arrived.wait()
            return httpx.Response(307, headers={"location": "/end", "x-request-id": "req_redirect"})
        return httpx.Response(200, json={})

    async with _build_profile_http_client(
        _profile(), httpx.Timeout(5), transport=httpx.MockTransport(handle)
    ) as client:

        async def send(tracker: RequestTracking) -> None:
            with tracker.scope():
                await client.get("https://example.test/start")

        await asyncio.gather(*(send(tracker) for tracker in trackers))
    assert len(set(sent)) == 4
    assert trackers[0].request_attempt_id != trackers[1].request_attempt_id
    assert all(t.provider_request_id is None for t in trackers)
    for context in contexts:
        rows = [d for d in sink.drafts if d.operation_id == context.exchange_operation_id]
        assert len(rows) == 4
        assert len({d.payload["request_attempt_id"] for d in rows}) == 2


@pytest.mark.parametrize("stream", [False, True])
async def test_sdk_retry_and_stream_keep_request_scope(stream: bool) -> None:
    sink = FakeSink()
    trace = ExchangeTrace(make_context(sink).with_exchange(new_analytics_id()))
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(request.headers[REQUEST_ATTEMPT_ID_HEADER])
        if len(sent) == 1:
            return httpx.Response(
                429, json={"error": {"message": "retry"}}, headers={"retry-after-ms": "1", "x-request-id": "req_failed"}
            )
        if stream:
            chunk = {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": "test-model",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            }
            return httpx.Response(
                200,
                text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
                headers={"content-type": "text/event-stream", "x-request-id": "req_ok"},
            )
        return httpx.Response(200, json=_OPENAI_REPLY, headers={"x-request-id": "req_ok"})

    http = _build_profile_http_client(_profile(), httpx.Timeout(5), transport=httpx.MockTransport(handle))
    client = ChatCompletionsClient.from_sdk_client(
        AsyncOpenAI(api_key="test", http_client=http, max_retries=1), model="test-model", observer=WireCallObserver()
    )
    try:
        if stream:
            response = await client.get_response(
                [Message("user", ["hi"])], stream=True, client_kwargs={TRAJECTORY_EXCHANGE_KWARG: trace}
            ).get_final_response()
        else:
            response = await client.get_response(
                [Message("user", ["hi"])], client_kwargs={TRAJECTORY_EXCHANGE_KWARG: trace}
            )
    finally:
        await client.aclose()
    assert len(sent) == len(set(sent)) == 2
    assert response.additional_properties["request_attempt_id"] == sent[-1]
    assert response.messages[0].additional_properties[REQUEST_ATTEMPT_ID_METADATA] == sent[-1]
    assert response.additional_properties["provider_request_id"] == "req_ok"
    assert [
        d.payload["provider_request_id"]
        for d in sink.drafts
        if d.event_type == EventType.MODEL_REQUEST_HEADERS_RECEIVED
    ] == ["req_failed", "req_ok"]


async def test_connection_failure_still_records_prepared_attempt() -> None:
    sink = FakeSink()
    tracker = RequestTracking(make_context(sink).with_exchange(new_analytics_id()))

    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async with _build_profile_http_client(
        _profile(), httpx.Timeout(5), transport=httpx.MockTransport(handle)
    ) as client:
        with tracker.scope(), pytest.raises(httpx.ConnectError):
            await client.get("https://example.test/")
    assert len(sink.drafts) == 1
    assert sink.drafts[0].event_type == EventType.MODEL_REQUEST_PREPARED
    assert sink.drafts[0].payload["request_attempt_id"] == tracker.request_attempt_id


async def _provider_request_id_from(headers: dict[str, str]) -> str | None:
    sink = FakeSink()
    tracker = RequestTracking(make_context(sink).with_exchange(new_analytics_id()))

    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers=headers)

    async with _build_profile_http_client(
        _profile(), httpx.Timeout(5), transport=httpx.MockTransport(handle)
    ) as client:
        with tracker.scope():
            await client.get("https://example.test/")
    [received] = [d for d in sink.drafts if d.event_type == EventType.MODEL_REQUEST_HEADERS_RECEIVED]
    assert received.payload.get("provider_request_id") == tracker.provider_request_id
    return tracker.provider_request_id


async def test_provider_request_id_is_read_from_the_provider_header_table() -> None:
    # Which header each provider uses is pinned in test_provider_request_ids.py.
    assert await _provider_request_id_from({"x-ds-trace-id": "deepseek", "eo-log-uuid": "edge"}) == "deepseek"


async def test_provider_request_id_is_absent_without_a_known_header() -> None:
    assert await _provider_request_id_from({"cf-ray": "edge", "eo-log-uuid": "cdn"}) is None


class _OneAttemptWireClient(WireClient):
    """Answers with *response* after one HTTP attempt passes the real request hook."""

    def __init__(self, response: ChatResponse[Any]) -> None:
        super().__init__(observer=WireCallObserver())
        self.response = response
        self.sent: list[str] = []

    def _send(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Awaitable[ChatResponse[Any]]:
        async def _response() -> ChatResponse[Any]:
            request = httpx.Request("POST", "https://example.test/")
            for hook in build_request_tracking_hooks()["request"]:
                await hook(request)
            self.sent.append(request.headers[REQUEST_ATTEMPT_ID_HEADER])
            return self.response

        return _response()

    def _open_stream(
        self,
        *,
        messages: Sequence[Message],
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> NoReturn:
        raise AssertionError("this double answers non-streaming requests only")


async def test_attempt_id_marks_only_the_messages_this_request_returned() -> None:
    """Echoed request messages keep their own attempt; contents never carry one.

    A tool result inherits its call content's properties, so an attempt id on
    the call would make local tool output look like provider output.
    """
    earlier_attempt = new_analytics_id()
    echoed = Message("assistant", ["earlier"])
    echoed.additional_properties[REQUEST_ATTEMPT_ID_METADATA] = earlier_attempt
    fresh = Message(
        "assistant",
        [Content.from_text("now"), Content.from_function_call(call_id="call_1", name="lookup", arguments={})],
    )
    client = _OneAttemptWireClient(ChatResponse(messages=[echoed, fresh]))

    response = await client._inner_get_response(messages=[Message("user", ["hi"]), echoed], options={})

    assert response.messages == [echoed, fresh]
    assert len(client.sent) == 1
    assert echoed.additional_properties[REQUEST_ATTEMPT_ID_METADATA] == earlier_attempt
    assert fresh.additional_properties[REQUEST_ATTEMPT_ID_METADATA] == client.sent[0]
    assert [REQUEST_ATTEMPT_ID_METADATA in content.additional_properties for content in fresh.contents] == [
        False,
        False,
    ]
