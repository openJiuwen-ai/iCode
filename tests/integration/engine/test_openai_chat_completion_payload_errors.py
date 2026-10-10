# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat Completions HTTP payload diagnostics through the full engine pipeline."""

from __future__ import annotations

import asyncio
import gzip
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import Error, InvocationMessage, UserMessage
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@dataclass(frozen=True)
class _PayloadCase:
    name: str
    content_type: str
    body: bytes
    expected_error: str | None
    expected_text: str | None = None
    stream: bool = False
    content_encoding: str | None = None


_KEEPALIVE_ONLY_SSE = b": keepalive\n\n"
# A gateway's error body under HTTP 200 that a retry would meet again.
_BAD_KEY_ENVELOPE = (
    b'{"error":{"code":"invalid_api_key","type":"invalid_request_error","message":"Incorrect API key."}}'
)


@dataclass(frozen=True)
class _ProviderCase:
    provider: str


def _valid_chat_completion_body() -> bytes:
    payload: dict[str, Any] = {
        "id": "chatcmpl_test",
        "object": "chat.completion",
        "created": 1,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "ok",
                    "refusal": None,
                    "annotations": [],
                },
                "finish_reason": "stop",
                "logprobs": None,
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def _valid_chat_completion_sse_body() -> bytes:
    chunks = (
        {
            "id": "chatcmpl_test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "ok"},
                    "finish_reason": None,
                    "logprobs": None,
                }
            ],
        },
        {
            "id": "chatcmpl_test",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                    "logprobs": None,
                }
            ],
        },
    )
    events = b"".join(b"data: " + json.dumps(chunk, separators=(",", ":")).encode() + b"\n\n" for chunk in chunks)
    return events + b"data: [DONE]\n\n"


_CASES = (
    _PayloadCase(
        name="empty",
        content_type="application/json",
        body=b"",
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): response body is not valid JSON "
            "(Expecting value: line 1 column 1 (char 0)). Response body: <empty>"
        ),
    ),
    _PayloadCase(
        name="html",
        content_type="text/html",
        body=b"<html><body>bad gateway</body></html>",
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'text/html'): decoded payload type is str; expected a ChatCompletion object. "
            "Response body: '<html><body>bad gateway</body></html>'"
        ),
    ),
    _PayloadCase(
        name="valid_json",
        content_type="application/json",
        body=_valid_chat_completion_body(),
        expected_error=None,
        expected_text="ok",
    ),
    _PayloadCase(
        name="malformed_json",
        content_type="application/json",
        body=b'{"id":',
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): response body is not valid JSON "
            "(Expecting value: line 1 column 7 (char 6)). Response body: '{\"id\":'"
        ),
    ),
    _PayloadCase(
        name="wrong_shape_json",
        content_type="application/json",
        body=b'{"detail":"gateway broke"}',
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): JSON payload is missing the required 'choices' array. "
            'Response body: \'{"detail":"gateway broke"}\''
        ),
    ),
    _PayloadCase(
        name="error_envelope",
        content_type="application/json",
        body=_BAD_KEY_ENVELOPE,
        expected_error="Error code: 200 - Incorrect API key.",
    ),
    _PayloadCase(
        name="streaming_empty",
        content_type="application/json",
        body=b"",
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): streaming response body is blank. "
            "Response body: <empty>"
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_valid",
        content_type="text/event-stream",
        body=_valid_chat_completion_sse_body(),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_html",
        content_type="text/html",
        body=b"<html><body>bad gateway</body></html>",
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'text/html'): streaming response body starts with '<'; "
            "expected an SSE event stream. Response body: '<html><body>bad gateway</body></html>'"
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_malformed_event_json",
        content_type="text/event-stream",
        body=b'data: {"bad":\n\n',
        expected_error=(
            "Chat Completions API returned invalid stream event JSON "
            "(Expecting value: line 1 column 8 (char 7)). Event data: '{\"bad\":'"
        ),
        stream=True,
    ),
    _PayloadCase(
        name="wrong_type_choices",
        content_type="application/json",
        body=b'{"id":"chatcmpl_test","object":"chat.completion","choices":"oops"}',
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): JSON payload 'choices' is str; expected an array. "
            'Response body: \'{"id":"chatcmpl_test","object":"chat.completion","choices":"oops"}\''
        ),
    ),
    _PayloadCase(
        name="streaming_sse_labeled_json",
        content_type="application/json",
        body=_valid_chat_completion_sse_body(),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_sse_leading_padding",
        content_type="application/octet-stream",
        body=b"\xef\xbb\xbf\r\n: keepalive\n\n" + _valid_chat_completion_sse_body(),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_json_error_labeled_sse",
        content_type="text/event-stream",
        body=b'{"detail":"gateway broke"}',
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'text/event-stream'): streaming response body starts with '{'; "
            "expected an SSE event stream. "
            'Response body: \'{"detail":"gateway broke"}\''
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_error_envelope",
        content_type="application/json",
        body=_BAD_KEY_ENVELOPE,
        expected_error="Error code: 200 - Incorrect API key.",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_event_missing_choices",
        content_type="text/event-stream",
        body=b'data: {"id":"chatcmpl_test"}\n\n',
        expected_error=(
            "Chat Completions API returned a stream event whose 'choices' is NoneType; expected an array. "
            'Event data: \'{"id":"chatcmpl_test"}\''
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_plain_text_error",
        content_type="text/plain",
        body=b"upstream connect error or disconnect/reset before headers",
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'text/plain'): streaming response body starts with 'u'; "
            "expected an SSE event stream. "
            "Response body: 'upstream connect error or disconnect/reset before headers'"
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_scalar_json",
        content_type="application/json",
        body=b"null",
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): streaming response body starts with 'n'; "
            "expected an SSE event stream. Response body: 'null'"
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_gzip_json_error",
        content_type="application/json",
        content_encoding="gzip",
        body=gzip.compress(b'{"detail":"gateway broke"}'),
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'application/json'): streaming response body starts with '{'; "
            "expected an SSE event stream. "
            'Response body: \'{"detail":"gateway broke"}\''
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_gzip_sse",
        content_type="text/event-stream",
        content_encoding="gzip",
        body=gzip.compress(_valid_chat_completion_sse_body()),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_no_events",
        content_type="text/event-stream",
        body=_KEEPALIVE_ONLY_SSE,
        expected_error=(
            "Chat Completions API returned an invalid response "
            "(HTTP 200, Content-Type 'text/event-stream'): stream ended without any events. "
            f"Response body: {_KEEPALIVE_ONLY_SSE.decode()!r}"
        ),
        stream=True,
    ),
    _PayloadCase(
        name="streaming_sse_bom_before_data",
        content_type="text/event-stream",
        body=b"\xef\xbb\xbf" + _valid_chat_completion_sse_body(),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_gzip_sse_bom_before_data",
        content_type="text/event-stream",
        content_encoding="gzip",
        body=gzip.compress(b"\xef\xbb\xbf" + _valid_chat_completion_sse_body()),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_colon_less_sse_field_first",
        content_type="text/event-stream",
        body=b"x-proxy\n" + _valid_chat_completion_sse_body(),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
    _PayloadCase(
        name="streaming_unknown_sse_field_first",
        content_type="text/event-stream",
        body=b"x.proxy: ignored\n" + _valid_chat_completion_sse_body(),
        expected_error=None,
        expected_text="ok",
        stream=True,
    ),
)

_PROVIDERS = (
    _ProviderCase(provider="openai"),
    _ProviderCase(provider="deepseek-openai"),
    _ProviderCase(provider="glm-openai"),
)


async def _subscribe_to_terminal_event(bus: EventBus) -> tuple[asyncio.Future[Error | InvocationMessage], list[Error]]:
    loop = asyncio.get_running_loop()
    terminal: asyncio.Future[Error | InvocationMessage] = loop.create_future()
    errors: list[Error] = []

    async def _on_error(event: Error) -> None:
        errors.append(event)
        if not terminal.done():
            terminal.set_result(event)

    async def _on_agent_message(event: InvocationMessage) -> None:
        if event.is_final and not terminal.done():
            terminal.set_result(event)

    await bus.subscribe(Error, _on_error)
    await bus.subscribe(InvocationMessage, _on_agent_message)
    return terminal, errors


@pytest.mark.asyncio
@pytest.mark.parametrize("case", _CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("provider_case", _PROVIDERS, ids=lambda case: case.provider)
async def test_provider_payload_user_visible_result(
    case: _PayloadCase,
    provider_case: _ProviderCase,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the real SDK parser and assert the terminal text surfaced by AgentEngine."""
    requests: list[httpx.Request] = []

    def _respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        headers = {"content-type": case.content_type}
        if case.content_encoding is not None:
            headers["content-encoding"] = case.content_encoding
        if case.stream:
            # ``stream=`` keeps httpx from buffering the body eagerly, so the SDK
            # really reads through the replay stream the validator installs.
            return httpx.Response(200, headers=headers, stream=httpx.ByteStream(case.body))
        return httpx.Response(200, headers=headers, content=case.body)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_respond))

    import chrys.service.llm.clients as clients_module

    monkeypatch.setattr(clients_module, "_build_profile_http_client", lambda *args, **kwargs: http_client)

    model_profile = ModelProfile(
        id=f"integration-{provider_case.provider}",
        name=f"integration-{provider_case.provider}",
        provider=provider_case.provider,
        api_style="chat_completions",
        model_id="test-model",
        base_url="https://provider.example/v1",
        api_key="test-key",
        http_max_retries=0,
        stream=case.stream,
    )
    registry = ModelProfileRegistry()
    registry.register(model_profile)
    bus = EventBus()
    terminal, errors = await _subscribe_to_terminal_event(bus)
    profile = AgentProfile(
        name="chat-completion-payload-test",
        instructions="Reply briefly.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    engine: AgentEngine = agent_engine(
        bus,
        settings=Settings(model_profile=model_profile.id),
        model_registry=registry,
    )

    try:
        await engine.start(profile)
        await bus.publish(UserMessage(text="hello"))
        await wait_for(terminal.done, timeout=ENGINE_TURN_TIMEOUT, description="terminal engine event")
        event = terminal.result()

        assert len(requests) == 1
        assert requests[0].url.path == "/v1/chat/completions"
        request_payload = json.loads(requests[0].content)
        assert request_payload["model"] == "test-model"
        assert request_payload["stream"] is case.stream

        if case.expected_error is not None:
            assert isinstance(event, Error)
            assert event.code == "executor_error"
            assert event.message == case.expected_error
            assert event.display_message is None
        else:
            assert isinstance(event, InvocationMessage) and event.origin.kind == "turn"
            assert event.text == case.expected_text
            assert errors == []
    finally:
        await engine.shutdown()
        await http_client.aclose()
