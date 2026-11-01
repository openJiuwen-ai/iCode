# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A model profile's ``{{!command}}`` values, through ``create_client`` and each provider SDK to the wire."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

import chrys.service.command_credentials as credentials_module
import chrys.service.llm.clients as clients_module
from chrys.foundation.util.env_templates import EnvVarResolutionError, resolve_env_templates
from chrys.kernel import Message
from chrys.service.command_credentials import _CommandFailedError
from chrys.service.llm.clients import _build_profile_http_client, create_client
from chrys.service.profiles.models.options import effective_chat_options, parse_chat_options, parse_http_headers
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile
from chrys.service.session.persistence import model_profile_context_fingerprint
from tests.support.scripted_wire import ScriptedWire, route_clients_to
from tests.support.wire_cases._kit import cc_replies, cc_text

_CHAT = [{"role": "user", "content": "hi"}]
_OPENAI_STREAM = (
    b'data: {"id":"1","object":"chat.completion.chunk","created":1,"model":"m",'
    b'"choices":[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
    b"data: [DONE]\n\n"
)
_ANTHROPIC_STREAM = (
    b'event: message_start\ndata: {"type":"message_start","message":{"id":"m","type":"message","role":"assistant",'
    b'"model":"m","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}}}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)
_CREDENTIAL_HEADER = {"openai": "authorization", "anthropic": "x-api-key"}
_CREDENTIAL_VALUE = {"openai": "Bearer {}", "anthropic": "{}"}


@pytest.fixture(autouse=True)
def _no_tokens_from_other_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(credentials_module, "_credentials", {})


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """What the next command runs give, the last one for every run after it."""
    script: list[Any] = []

    async def run(_command: str) -> str:
        outcome = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(credentials_module, "_run_command", run)
    return script


def _profile(provider: str, **fields: Any) -> ModelProfile:
    return ModelProfile(
        id="p",
        name="p",
        provider=provider,
        model_id="test-model",
        base_url="https://provider.example" if provider == "anthropic" else "https://provider.example/v1",
        http_max_retries=0,
        **fields,
    )


def _answering_with(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.AsyncClient]:
    """Make the clients ``create_client`` builds answer through *handler* and return them as they are built."""
    built: list[httpx.AsyncClient] = []

    def build(profile: ModelProfile, timeout: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = _build_profile_http_client(profile, timeout, transport=httpx.MockTransport(handler), **kwargs)
        built.append(client)
        return client

    monkeypatch.setattr(clients_module, "_build_profile_http_client", build)
    return built


def _accepting(provider: str, token: str, seen: list[dict[str, str]]) -> Callable[[httpx.Request], httpx.Response]:
    stream = _ANTHROPIC_STREAM if provider == "anthropic" else _OPENAI_STREAM

    def handler(request: httpx.Request) -> httpx.Response:
        headers = {name.lower(): value for name, value in request.headers.items()}
        seen.append(headers)
        if headers.get(_CREDENTIAL_HEADER[provider]) != _CREDENTIAL_VALUE[provider].format(token):
            return httpx.Response(401, json={"error": {"message": "expired", "type": "authentication_error"}})
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    return handler


async def _stream(stack: Any, provider: str, **kwargs: Any) -> int:
    sdk = stack.inner.inner.sdk_client
    if provider == "anthropic":
        stream = await sdk.messages.create(model="test-model", max_tokens=8, messages=_CHAT, stream=True, **kwargs)
    else:
        stream = await sdk.chat.completions.create(model="test-model", messages=_CHAT, stream=True, **kwargs)
    return len([event async for event in stream])


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_a_stream_whose_token_is_refused_goes_out_again_with_the_new_one(
    provider: str, tokens: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens[:] = ["expired", "good"]
    seen: list[dict[str, str]] = []
    bodies: list[bytes] = []
    accept = _accepting(provider, "good", seen)

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return accept(request)

    _answering_with(monkeypatch, handler)

    stack = await create_client(_profile(provider, api_key="{{!tool get}}"))
    try:
        assert await _stream(stack, provider) > 0
    finally:
        await stack.aclose()

    assert [headers[_CREDENTIAL_HEADER[provider]] for headers in seen] == [
        _CREDENTIAL_VALUE[provider].format("expired"),
        _CREDENTIAL_VALUE[provider].format("good"),
    ]
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[1])["model"] == "test-model"


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_header_values_and_per_request_headers_carry_tokens_too(
    provider: str, tokens: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens[:] = ["good"]
    seen: list[dict[str, str]] = []
    _answering_with(monkeypatch, _accepting(provider, "good", seen))
    profile = _profile(
        provider,
        api_key="{{!tool get}}",
        http_headers=json.dumps({"X-Gateway": "gw {{!tool get}}"}),
        chat_options=json.dumps({"extra_headers": {"X-Tenant": "{{!tool get}}"}}),
    )

    stack = await create_client(profile)
    try:
        options = parse_chat_options(profile) or {}
        await _stream(stack, provider, extra_headers=options["extra_headers"])
    finally:
        await stack.aclose()

    assert seen[-1]["x-gateway"] == "gw good"
    assert seen[-1]["x-tenant"] == "good"


@pytest.mark.parametrize("other_has_a_token", [False, True])
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_a_cookie_the_provider_set_never_comes_back_as_a_token(
    provider: str, other_has_a_token: bool, tokens: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cookie shaped like the handle of another command, an MCP server's say, stays what it is and runs nothing."""
    tokens[:] = ["good"]
    other = resolve_env_templates("{{!mcp-key-tool}}", location="an MCP server")
    if other_has_a_token:
        await credentials_module.refresh([other])
    stream = _ANTHROPIC_STREAM if provider == "anthropic" else _OPENAI_STREAM
    cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        cookies.append(request.headers.get("cookie"))
        headers = {"content-type": "text/event-stream", "set-cookie": f"s={other}; Path=/"}
        return httpx.Response(200, headers=headers, content=stream)

    _answering_with(monkeypatch, handler)

    stack = await create_client(_profile(provider, api_key="{{!tool get}}"))
    try:
        await _stream(stack, provider)
        await _stream(stack, provider)
    finally:
        await stack.aclose()

    assert cookies == [None, f"s={other}"]
    assert (other in credentials_module._credentials) is other_has_a_token


@pytest.mark.parametrize(
    "fields",
    [
        {"chat_options": json.dumps({"extra_headers": {"X-T": "{{!tool get}}"}})},
        {"http_headers": json.dumps({"X-T": "{{!tool get}}"})},
    ],
    ids=["chat_options", "http_headers"],
)
async def test_a_command_used_only_in_a_header_still_gets_its_auth(
    fields: dict[str, str], tokens: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens[:] = ["good"]
    wire = ScriptedWire(cc_replies([cc_text("Hi.", response_id="chatcmpl-1")], stream=False))
    route_clients_to(wire.transport, monkeypatch)
    profile = _profile("openai", api_key="sk-plain", **fields)

    stack = await create_client(profile)
    try:
        await stack.inner.get_response([Message("user", ["Hi"])], options=effective_chat_options(profile) or {})
    finally:
        await stack.aclose()

    assert wire.requests[0].headers["X-T"] == "good"


async def test_a_command_that_fails_at_first_is_a_configuration_error_with_no_client_left_open(
    tokens: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens[:] = [_CommandFailedError("exited with code 1")]
    built = _answering_with(monkeypatch, lambda _request: httpx.Response(200))

    with pytest.raises(EnvVarResolutionError, match=r"model profile 'p' API Key exited with code 1"):
        await create_client(_profile("openai", api_key="{{!tool get}}"))

    assert built == []


async def test_a_profile_without_commands_gets_no_auth_so_httpx_keeps_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installed auth would turn off httpx's Basic auth for ``user:pass@`` in a URL."""
    built = _answering_with(monkeypatch, lambda _request: httpx.Response(200))

    stack = await create_client(_profile("openai", api_key="sk-plain"))
    try:
        assert built[0]._auth is None
    finally:
        await stack.aclose()


def test_chat_options_outside_extra_headers_refuse_commands() -> None:
    profile = _profile("openai", chat_options=json.dumps({"extra_body": {"token": "{{!tool get}}"}}))

    with pytest.raises(EnvVarResolutionError, match="not supported in model profile 'p' chat option"):
        parse_chat_options(profile)


@pytest.mark.parametrize(
    ("field", "parse"),
    [("http_headers", parse_http_headers), ("chat_options", parse_chat_options)],
    ids=["http_headers", "chat_options"],
)
@pytest.mark.parametrize(
    ("raw", "problem"),
    [('{"X-T": "{{!secret-tool "get"}}"}', "not valid JSON"), ('["{{!secret-tool get}}"]', "not a JSON object")],
    ids=["invalid", "not_an_object"],
)
def test_a_field_that_cannot_be_read_warns_without_quoting_it(
    field: str,
    parse: Callable[[ModelProfile], object],
    raw: str,
    problem: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="chrys.service.profiles.models.options"):
        parse(_profile("openai", **{field: raw}))

    assert f"ModelProfile 'p' {field} is {problem}" in caplog.text
    assert "secret-tool" not in caplog.text


def test_chat_options_keep_their_shape_and_error_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_ONE", raising=False)
    monkeypatch.setenv("SET_ONE", "value")
    profile = _profile("openai", chat_options=json.dumps({"a": ["{{SET_ONE}}"], "b": {"c": "{{SET_ONE}}"}, "d": 1}))
    assert parse_chat_options(profile) == {"a": ["value"], "b": {"c": "value"}, "d": 1}

    broken = _profile("openai", chat_options=json.dumps({"b": {"c": "{{MISSING_ONE}}"}}))
    with pytest.raises(EnvVarResolutionError, match=r"chat option\['b'\]\['c'\]"):
        parse_chat_options(broken)


async def test_a_new_token_changes_neither_the_session_fingerprint_nor_the_headers_it_hashes(tokens: list[Any]) -> None:
    profile = _profile(
        "openai",
        api_key="{{!tool get}}",
        http_headers=json.dumps({"X-Gateway": "{{!tool get}}"}),
        chat_options=json.dumps({"extra_headers": {"X-T": "{{!tool get}}"}}),
    )
    tokens[:] = ["first", "second"]
    handle = parse_http_headers(profile)["X-Gateway"]

    await credentials_module.ensure_fresh([handle])
    before = model_profile_context_fingerprint(profile, chat_options=parse_chat_options(profile))
    await credentials_module.refresh([handle])
    after = model_profile_context_fingerprint(profile, chat_options=parse_chat_options(profile))

    assert credentials_module.substitute(handle) == "second"
    assert before == after


def test_another_command_changes_the_session_fingerprint() -> None:
    fingerprints = {
        model_profile_context_fingerprint(_profile("openai", api_key=api_key))
        for api_key in ("{{!tool one}}", "{{!tool two}}")
    }

    assert len(fingerprints) == 2


async def test_the_token_stays_out_of_the_saved_profile(tokens: list[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    tokens[:] = ["the-real-token"]
    _answering_with(monkeypatch, lambda _request: httpx.Response(200))
    profile = _profile("openai", api_key="{{!tool get}}")
    stack = await create_client(profile)
    await stack.aclose()

    path = save_profile(profile)

    text = path.read_text(encoding="utf-8")
    assert "{{!tool get}}" in text
    assert "the-real-token" not in text
