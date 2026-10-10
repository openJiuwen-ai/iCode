# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the ``create_client`` factory and its provider-specific helpers.

The real Anthropic / OpenAI SDKs are monkey-patched to pure record-and-return
stand-ins so we can verify *how* they're called without performing any
network I/O or requiring valid API keys.
"""

from __future__ import annotations

import errno
import os
import ssl
import sys
from typing import Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.errors import is_deterministic_connection_error
from chrys.foundation.util.chrys_headers import (
    MODEL_ID_HEADER,
    PARENT_SESSION_ID_HEADER,
    SESSION_ID_HEADER,
    X_PARENT_SESSION_ID_HEADER,
    X_SESSION_ID_HEADER,
)
from chrys.foundation.util.env_templates import EnvVarResolutionError
from chrys.kernel import ChatMiddlewareLayer, ToolLoopLayer
from chrys.service.llm.anthropic_messages import AnthropicMessagesClient
from chrys.service.llm.chat_completions import ChatCompletionsClient
from chrys.service.llm.chat_completions.decode import decode_completion
from chrys.service.llm.clients import (
    _build_anthropic_sdk_client,
    _build_openai_sdk_client,
    _build_profile_http_client,
    _resolve_profile_api_key,
    create_client,
    effective_model_base_url,
)
from chrys.service.llm.openai_responses import ResponsesApiClient
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.network_faults import SimulatedWindowsError


def _profile(
    provider: str = "openai",
    *,
    api_style: str = "chat_completions",
    api_key: str = "",
    base_url: str = "",
    http_headers: str = "",
    model_id: str = "test-model",
    verify_ssl: bool = True,
    bypass_proxy: bool = False,
) -> ModelProfile:
    return ModelProfile(
        id="p",
        name="p",
        provider=provider,
        api_style=api_style,  # type: ignore[arg-type]
        model_id=model_id,
        api_key=api_key,
        base_url=base_url,
        http_headers=http_headers,
        verify_ssl=verify_ssl,
        bypass_proxy=bypass_proxy,
    )


def test_resolve_profile_api_key_uses_env_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_PROFILE_API_KEY", "sk-template")

    assert _resolve_profile_api_key(_profile(api_key="{{CHRYS_PROFILE_API_KEY}}")) == "sk-template"


def test_resolve_profile_api_key_missing_template_raises_before_provider_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fallback")
    monkeypatch.delenv("CHRYS_MISSING_PROFILE_API_KEY", raising=False)

    with pytest.raises(EnvVarResolutionError) as info:
        _resolve_profile_api_key(_profile(api_key="{{CHRYS_MISSING_PROFILE_API_KEY}}"))

    message = str(info.value)
    assert "CHRYS_MISSING_PROFILE_API_KEY" in message
    assert "model profile 'p' API Key" in message
    assert "sk-fallback" not in message


@pytest.mark.parametrize(
    ("provider", "base_url_env", "default_base_url"),
    [
        ("openai", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
        ("anthropic", "ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        ("deepseek-openai", "DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        ("glm-openai", "ZAI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
    ],
)
def test_effective_model_base_url_uses_provider_default(
    provider: str,
    base_url_env: str,
    default_base_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(base_url_env, raising=False)

    assert effective_model_base_url(_profile(provider=provider)) == default_base_url


def test_effective_model_base_url_prefers_profile_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.com")

    assert effective_model_base_url(_profile(provider="openai")) == "https://env.example.com"
    assert (
        effective_model_base_url(_profile(provider="openai", base_url="https://profile.example.com"))
        == "https://profile.example.com"
    )


def test_effective_model_base_url_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        effective_model_base_url(_profile(provider="azure"))


@pytest.mark.parametrize(
    ("provider", "fallback_env"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("deepseek-openai", "DEEPSEEK_API_KEY"),
        ("glm-openai", "ZAI_API_KEY"),
    ],
)
async def test_create_client_missing_api_key_template_raises_before_provider_fallback(
    provider: str,
    fallback_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(fallback_env, "sk-fallback")
    monkeypatch.delenv("CHRYS_MISSING_PROFILE_API_KEY", raising=False)

    with pytest.raises(EnvVarResolutionError) as info:
        await create_client(_profile(provider=provider, api_key="{{CHRYS_MISSING_PROFILE_API_KEY}}"))

    message = str(info.value)
    assert "CHRYS_MISSING_PROFILE_API_KEY" in message
    assert "sk-fallback" not in message


# ───────────────────────── wire-charset validation ────────────────────


async def test_create_client_rejects_non_ascii_profile_api_key() -> None:
    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="openai", api_key="sk-abc▼def"))

    message = str(info.value)
    assert "Model profile 'p'" in message
    assert "API key" in message
    assert "position 7" in message
    assert "sk-abc" not in message
    assert "▼" not in message


async def test_create_client_rejects_non_ascii_api_key_from_env_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_PROFILE_API_KEY", "sk-abc密钥")

    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="openai", api_key="{{CHRYS_PROFILE_API_KEY}}"))

    message = str(info.value)
    assert "API key" in message
    assert "position 7" in message
    assert "密钥" not in message


@pytest.mark.parametrize(
    ("provider", "fallback_env"),
    [
        ("openai", "OPENAI_API_KEY"),
        ("anthropic", "ANTHROPIC_API_KEY"),
        ("deepseek-openai", "DEEPSEEK_API_KEY"),
        ("glm-openai", "ZAI_API_KEY"),
    ],
)
async def test_create_client_rejects_non_ascii_provider_fallback_key(
    provider: str,
    fallback_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(fallback_env, "sk-fallback▼")

    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider=provider))

    message = str(info.value)
    assert "API key" in message
    assert "sk-fallback" not in message


async def test_create_client_rejects_outer_space_provider_fallback_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fallback env key with a trailing space fails h11 locally; reject it up front."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback ")

    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="openai"))

    message = str(info.value)
    assert "API key" in message
    assert "ends with a space" in message
    assert "sk-fallback" not in message


async def test_create_client_rejects_non_ascii_model_id() -> None:
    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="openai", api_key="sk-ok", model_id="gpt▼4o"))

    message = str(info.value)
    assert "Model ID" in message
    assert "U+25BC" in message
    # Reported once under its own field name, not again as the
    # Chrys-Model-Id header value.
    assert MODEL_ID_HEADER not in message


async def test_create_client_rejects_empty_model_id() -> None:
    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="openai", api_key="sk-ok", model_id=""))

    assert "Model ID is empty" in str(info.value)


async def test_create_client_rejects_non_ascii_http_header_value() -> None:
    profile = _profile(provider="openai", api_key="sk-ok", http_headers='{"X-Custom-Auth": "secret▼value"}')

    with pytest.raises(ValueError) as info:
        await create_client(profile)

    message = str(info.value)
    assert "'X-Custom-Auth'" in message
    assert "position 7" in message
    assert "secret" not in message
    assert "▼" not in message


async def test_create_client_rejects_non_ascii_header_value_from_env_template(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_TEST_HEADER_VALUE", "值")
    profile = _profile(
        provider="openai",
        api_key="sk-ok",
        http_headers='{"X-Custom-Auth": "{{CHRYS_TEST_HEADER_VALUE}}"}',
    )

    with pytest.raises(ValueError) as info:
        await create_client(profile)

    message = str(info.value)
    assert "'X-Custom-Auth'" in message
    assert "值" not in message


async def test_create_client_rejects_invalid_http_header_name() -> None:
    profile = _profile(provider="openai", api_key="sk-ok", http_headers='{"X Custom": "v"}')

    with pytest.raises(ValueError) as info:
        await create_client(profile)

    assert "Header name" in str(info.value)


async def test_create_client_rejects_non_ascii_anthropic_auth_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Anthropic SDK reads its bearer token after Chrys builds profile headers."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token▼")

    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="anthropic"))

    message = str(info.value)
    assert "'Authorization'" in message
    assert "provider SDK headers" in message
    assert "token" not in message
    assert "▼" not in message


@pytest.mark.parametrize(
    ("provider", "custom_headers_env"),
    [
        ("anthropic", "ANTHROPIC_CUSTOM_HEADERS"),
        ("openai", "OPENAI_CUSTOM_HEADERS"),
        ("deepseek-openai", "OPENAI_CUSTOM_HEADERS"),
        ("glm-openai", "OPENAI_CUSTOM_HEADERS"),
    ],
)
async def test_create_client_rejects_non_ascii_sdk_custom_header(
    provider: str,
    custom_headers_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(custom_headers_env, "X-Sdk-Secret: secret值")

    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider=provider, api_key="sk-ok"))

    message = str(info.value)
    assert "'X-Sdk-Secret'" in message
    assert "provider SDK headers" in message
    assert "secret" not in message
    assert "值" not in message


@pytest.mark.parametrize(
    ("env_name", "header_name"),
    [
        ("OPENAI_ORG_ID", "OpenAI-Organization"),
        ("OPENAI_PROJECT_ID", "OpenAI-Project"),
    ],
)
async def test_create_client_rejects_non_ascii_openai_identity_header(
    env_name: str,
    header_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(env_name, "identity▼")

    with pytest.raises(ValueError) as info:
        await create_client(_profile(provider="openai", api_key="sk-ok"))

    message = str(info.value)
    assert repr(header_name) in message
    assert "provider SDK headers" in message
    assert "identity" not in message
    assert "▼" not in message


async def test_create_client_mock_skips_wire_charset_validation() -> None:
    from chrys.service.llm.mock import MockChatClient

    client = await create_client(_profile(provider="mock", api_key="▼", model_id="模型"))

    assert isinstance(client, MockChatClient)


async def test_create_client_threads_tool_result_ceiling_to_mock_loop() -> None:
    client = await create_client(
        _profile(provider="mock"),
        tool_result_ceiling_tokens=4_000,
    )

    assert client._tool_loop.tool_result_ceiling_tokens == 4_000


# ───────────────────────── spy classes ────────────────────────────────


class _OwnedHttpClient:
    """Stands in for the profile-owned ``httpx.AsyncClient``."""

    async def aclose(self) -> None:
        pass


class _Spy:
    """Records construction args; swallows everything. Used in place of SDK classes."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs
        self.auth_headers: dict[str, str] = {}
        self.default_headers = kwargs.get("default_headers") or {}

    async def aclose(self) -> None:
        pass


async def _assert_empty_api_key_provider(value: Any) -> None:
    assert callable(value)
    assert await value() == ""


# ───────────────────────── _build_anthropic_sdk_client ───────────────


def test_anthropic_async_client_includes_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.service.llm.sdk_anthropic.RetryGuardedAsyncAnthropic", _Spy)

    client = _build_anthropic_sdk_client(
        api_key="ak-test",
        base_url="https://api.anthropic.example.com",
        timeout="t",
        max_retries=3,
        default_headers={"X": "Y"},
    )

    assert isinstance(client, _Spy)
    assert client.kwargs["api_key"] == "ak-test"
    assert client.kwargs["base_url"] == "https://api.anthropic.example.com"
    assert client.kwargs["timeout"] == "t"
    assert client.kwargs["max_retries"] == 3
    assert client.kwargs["default_headers"] == {"X": "Y"}


def test_anthropic_async_client_omits_empty_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty api_key and base_url must not be passed (so SDK env fallback runs)."""
    monkeypatch.setattr("chrys.service.llm.sdk_anthropic.RetryGuardedAsyncAnthropic", _Spy)

    client = _build_anthropic_sdk_client(
        api_key="",
        base_url="",
        timeout=None,
        max_retries=1,
        default_headers=None,
    )

    assert "api_key" not in client.kwargs
    assert "base_url" not in client.kwargs
    # Still includes the always-passed kwargs
    assert client.kwargs["timeout"] is None
    assert client.kwargs["max_retries"] == 1
    assert client.kwargs["default_headers"] is None


def test_anthropic_async_client_accepts_owned_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.service.llm.sdk_anthropic.RetryGuardedAsyncAnthropic", _Spy)
    http_client = object()

    client = _build_anthropic_sdk_client(
        api_key="",
        base_url="",
        timeout=None,
        max_retries=1,
        default_headers=None,
        http_client=http_client,
    )

    assert client.kwargs["http_client"] is http_client


# ───────────────────────── _build_openai_sdk_client ──────────────────


def test_openai_async_client_uses_explicit_key_and_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = _build_openai_sdk_client(
        api_key="sk-profile",
        base_url="https://api.openai.example.com",
        timeout=1.0,
        max_retries=2,
        default_headers=None,
    )

    assert client.kwargs["api_key"] == "sk-profile"
    assert client.kwargs["base_url"] == "https://api.openai.example.com"


def test_openai_async_client_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty profile api_key/base_url → read from env."""
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.example.com")

    client = _build_openai_sdk_client(
        api_key="",
        base_url="",
        timeout=1.0,
        max_retries=2,
        default_headers=None,
    )

    assert client.kwargs["api_key"] == "sk-env"
    assert client.kwargs["base_url"] == "https://env.example.com"


async def test_openai_async_client_passes_empty_key_when_profile_and_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty profile api_key and no env must still pass an explicit api_key provider."""
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = _build_openai_sdk_client(
        api_key="",
        base_url="",
        timeout=1.0,
        max_retries=2,
        default_headers=None,
    )

    await _assert_empty_api_key_provider(client.kwargs["api_key"])


def test_openai_async_client_omits_base_url_when_neither_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither profile nor env sets base_url → key omitted (SDK uses default)."""
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = _build_openai_sdk_client(
        api_key="",
        base_url="",
        timeout=1.0,
        max_retries=2,
        default_headers=None,
    )
    assert "base_url" not in client.kwargs


def test_openai_async_client_accepts_owned_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    http_client = object()

    client = _build_openai_sdk_client(
        api_key="",
        base_url="",
        timeout=1.0,
        max_retries=2,
        default_headers=None,
        http_client=http_client,
    )

    assert client.kwargs["http_client"] is http_client


def _create_real_provider_client(
    provider: str,
    http_client: Any,
    *,
    max_retries: int,
    base_url: str | None = None,
) -> Any:
    if provider == "openai":
        return _build_openai_sdk_client(
            api_key="sk-test",
            base_url=base_url or "https://provider.example/v1",
            timeout=1.0,
            max_retries=max_retries,
            default_headers=None,
            http_client=http_client,
        )
    return _build_anthropic_sdk_client(
        api_key="sk-test",
        base_url=base_url or "https://provider.example",
        timeout=1.0,
        max_retries=max_retries,
        default_headers=None,
        http_client=http_client,
    )


async def _send_real_provider_request(provider: str, client: Any, *, stream: bool = False) -> None:
    if provider == "openai":
        await client.chat.completions.create(
            model="test-model", messages=[{"role": "user", "content": "hi"}], stream=stream
        )
        return
    await client.messages.create(
        model="test-model",
        max_tokens=1,
        messages=[{"role": "user", "content": "hi"}],
        stream=stream,
    )


def _surfaced_transport_error(provider: str, error: BaseException) -> BaseException:
    """Return the transport failure a provider SDK raised on its first attempt.

    The OpenAI guard re-raises the transport error itself; the Anthropic SDK
    raises its ``APIConnectionError`` caused by it. Either way Chrys's own
    retry lanes must reach the same no-retry verdict.
    """
    assert is_deterministic_connection_error(error)
    if provider == "openai":
        return error
    assert type(error).__name__ == "APIConnectionError"
    assert error.__cause__ is not None
    return error.__cause__


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["blocking", "streaming"])
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_sdk_does_not_retry_deterministic_tls_failure(provider: str, stream: bool) -> None:
    """The guard handles the argument-only TLS cause produced by pinned httpcore."""
    import httpcore
    import httpx

    attempts = 0

    async def _fail_tls(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        tls_error = ssl.SSLCertVerificationError(
            1,
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate",
        )
        core_error = httpcore.ConnectError(tls_error)
        core_error.__context__ = tls_error
        core_error.__suppress_context__ = True
        raise httpx.ConnectError(str(core_error), request=request) from core_error

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_tls))
    client = _create_real_provider_client(provider, http_client, max_retries=3)

    try:
        with pytest.raises(Exception) as info:
            await _send_real_provider_request(provider, client, stream=stream)
    finally:
        await client.close()

    error = _surfaced_transport_error(provider, info.value)
    assert isinstance(error, httpx.ConnectError)
    assert type(error.__cause__).__module__.partition(".")[0] == "httpcore"
    assert isinstance(error.__cause__.args[0], ssl.SSLCertVerificationError)
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_sdk_does_not_retry_real_anyio_tls_verification_failure(
    provider: str,
    local_http_server,
    self_signed_server_ssl_context,
) -> None:
    """Exercise the production AnyIO/httpcore TLS chain against a live server."""
    import httpcore
    import httpx

    timeout = httpx.Timeout(connect=10, read=5, write=5, pool=5)
    attempts = 0

    async def _count(request: httpx.Request) -> None:
        nonlocal attempts
        attempts += 1

    async with local_http_server(
        b"unused",
        scheme="https",
        ssl_context=self_signed_server_ssl_context,
    ) as server:
        http_client = httpx.AsyncClient(timeout=timeout, trust_env=False, event_hooks={"request": [_count]})
        base_url = f"{server.url}/v1" if provider == "openai" else server.url
        client = _create_real_provider_client(provider, http_client, max_retries=2, base_url=base_url)
        client._calculate_retry_timeout = lambda *_args, **_kwargs: 0.0

        try:
            with pytest.raises(Exception) as info:
                await _send_real_provider_request(provider, client)
        finally:
            await client.close()

    error = _surfaced_transport_error(provider, info.value)
    assert isinstance(error, httpx.ConnectError)
    core_error = error.__cause__
    assert isinstance(core_error, httpcore.ConnectError)
    assert any(isinstance(arg, ssl.SSLCertVerificationError) for arg in core_error.args)
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize(
    "message",
    [
        "407 Proxy Authentication Required",
        "Invalid username/password",
        "Requested NO AUTHENTICATION REQUIRED from proxy server, but got USERNAME/PASSWORD.",
    ],
)
async def test_provider_sdk_does_not_retry_proxy_authentication_failure(provider: str, message: str) -> None:
    import httpx

    attempts = 0

    async def _fail_proxy_auth(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ProxyError(message, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_proxy_auth))
    client = _create_real_provider_client(provider, http_client, max_retries=3)

    try:
        with pytest.raises(Exception) as info:
            await _send_real_provider_request(provider, client)
    finally:
        await client.close()

    assert isinstance(_surfaced_transport_error(provider, info.value), httpx.ProxyError)
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_sdk_preserves_retries_for_ambiguous_tls_alert(provider: str) -> None:
    """A peer handshake alert is not enough evidence to veto SDK retries."""
    import httpx

    attempts = 0

    async def _fail_handshake(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        tls_error = ssl.SSLError(
            1,
            "[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] ssl/tls alert handshake failure",
        )
        raise httpx.ConnectError(str(tls_error), request=request) from tls_error

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_handshake))
    client = _create_real_provider_client(provider, http_client, max_retries=2)
    client._calculate_retry_timeout = lambda *_args, **_kwargs: 0.0

    try:
        with pytest.raises(Exception) as info:
            await _send_real_provider_request(provider, client)
    finally:
        await client.close()

    assert type(info.value).__name__ == "APIConnectionError"
    assert attempts == 3


@pytest.mark.asyncio
async def test_openai_sdk_leaves_a_retry_after_beyond_two_minutes_to_chrys() -> None:
    """The OpenAI SDK waits out a ``Retry-After`` of up to two minutes; past that it does not retry."""
    import httpx

    attempts = 0

    async def _rate_limit(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        body = {"error": {"message": "rate limited", "type": "rate_limit_error", "code": "rate_limit"}}
        return httpx.Response(429, headers={"retry-after": "121"}, json=body, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_rate_limit))
    client = _create_real_provider_client("openai", http_client, max_retries=2)

    try:
        with pytest.raises(Exception) as info:
            await _send_real_provider_request("openai", client)
    finally:
        await client.close()

    assert type(info.value).__name__ == "RateLimitError"
    assert attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_sdk_preserves_retries_for_windows_buffer_exhaustion(provider: str) -> None:
    """A bare WinError 10055 (no buffer space) is a resource shortage a later attempt can outlive."""
    import httpx

    attempts = 0

    async def _fail_without_buffers(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise SimulatedWindowsError(errno.ENOBUFS, "No buffer space available", 10055)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_without_buffers))
    client = _create_real_provider_client(provider, http_client, max_retries=2)
    client._calculate_retry_timeout = lambda *_args, **_kwargs: 0.0

    try:
        with pytest.raises(Exception) as info:
            await _send_real_provider_request(provider, client)
    finally:
        await client.close()

    assert type(info.value).__name__ == "APIConnectionError"
    assert isinstance(info.value.__cause__, SimulatedWindowsError)
    assert attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_sdk_preserves_retries_for_transient_connection_failure(provider: str) -> None:
    import httpx

    attempts = 0

    async def _fail_temporarily(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("Connection refused", request=request) from ConnectionRefusedError

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_temporarily))
    client = _create_real_provider_client(provider, http_client, max_retries=2)
    client._calculate_retry_timeout = lambda *_args, **_kwargs: 0.0

    try:
        with pytest.raises(Exception) as info:
            await _send_real_provider_request(provider, client)
    finally:
        await client.close()

    assert type(info.value).__name__ == "APIConnectionError"
    assert attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_transient_retry_ignores_deterministic_error_from_outer_context(provider: str) -> None:
    """A new connection failure must not inherit the caller's stale TLS veto."""
    import httpx

    attempts = 0

    async def _fail_temporarily(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("Connection refused", request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fail_temporarily))
    client = _create_real_provider_client(provider, http_client, max_retries=2)
    client._calculate_retry_timeout = lambda *_args, **_kwargs: 0.0
    tls_error = ssl.SSLCertVerificationError(1, "certificate verify failed")

    try:
        try:
            raise tls_error
        except ssl.SSLCertVerificationError:
            with pytest.raises(Exception) as info:
                await _send_real_provider_request(provider, client)
    finally:
        await client.close()

    assert type(info.value).__name__ == "APIConnectionError"
    assert attempts == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_status_retry_ignores_deterministic_error_from_outer_context(provider: str) -> None:
    """An independent 429 must not inherit a TLS veto from its caller's handler."""
    import httpx

    attempts = 0
    error_body = (
        {"error": {"message": "rate limited", "type": "rate_limit_error", "code": "rate_limit"}}
        if provider == "openai"
        else {"type": "error", "error": {"type": "rate_limit_error", "message": "rate limited"}}
    )

    async def _rate_limit(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json=error_body, request=request)

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_rate_limit))
    client = _create_real_provider_client(provider, http_client, max_retries=2)
    client._calculate_retry_timeout = lambda *_args, **_kwargs: 0.0
    tls_error = ssl.SSLCertVerificationError(1, "certificate verify failed")

    try:
        try:
            raise tls_error
        except ssl.SSLCertVerificationError:
            with pytest.raises(Exception) as info:
                await _send_real_provider_request(provider, client)
    finally:
        await client.close()

    assert type(info.value).__name__ == "RateLimitError"
    assert attempts == 3


# ───────────────────────── profile httpx client ──────────────────────

_ROUTE_HOOKS = {
    "request": ["build_route_hooks.<locals>.stamp", "build_request_tracking_hooks.<locals>.prepared"],
    "response": ["build_route_hooks.<locals>.record", "build_request_tracking_hooks.<locals>.received"],
}


def _pop_hook_names(kwargs: dict[str, Any]) -> dict[str, list[str]]:
    """Remove the client's event hooks from *kwargs* and return their qualified names."""
    return {event: [hook.__qualname__ for hook in hooks] for event, hooks in kwargs.pop("event_hooks").items()}


def test_profile_http_client_default_profile_builds_the_sdk_default_client(monkeypatch: pytest.MonkeyPatch) -> None:
    import openai

    calls: list[dict[str, Any]] = []

    def _ctor(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _Spy(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", _ctor)
    import httpx

    timeout = httpx.Timeout(connect=1, read=2, write=2, pool=2)

    client = _build_profile_http_client(_profile(), timeout)

    assert isinstance(client, _Spy)
    assert _pop_hook_names(calls[0]) == _ROUTE_HOOKS
    assert calls == [{"verify": True, "timeout": timeout, "follow_redirects": True}]


def test_profile_http_client_raw_http_logging_prebuilds_with_event_hooks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    import openai

    calls: list[dict[str, Any]] = []

    def _ctor(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _Spy(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", _ctor)
    import httpx

    timeout = httpx.Timeout(connect=1, read=2, write=2, pool=2)

    client = _build_profile_http_client(
        _profile(),
        timeout,
        raw_http_log_path=tmp_path / "llm_raw_http.jsonl",
        session_id="sess-raw",
    )

    assert isinstance(client, _Spy)
    assert calls[0]["verify"] is True
    assert calls[0]["timeout"] is timeout
    assert calls[0]["follow_redirects"] is True
    assert _pop_hook_names(calls[0]) == {
        "request": [*_ROUTE_HOOKS["request"], "build_raw_http_event_hooks.<locals>._request_hook"],
        "response": [*_ROUTE_HOOKS["response"], "build_raw_http_event_hooks.<locals>._response_hook"],
    }


def test_profile_http_client_verify_false_prebuilds_with_transport_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    calls: list[dict[str, Any]] = []

    def _ctor(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _Spy(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", _ctor)
    import httpx

    timeout = httpx.Timeout(connect=1, read=2, write=2, pool=2)

    client = _build_profile_http_client(_profile(verify_ssl=False), timeout)

    assert isinstance(client, _Spy)
    assert _pop_hook_names(calls[0]) == _ROUTE_HOOKS
    assert calls == [
        {
            "verify": False,
            "timeout": timeout,
            "follow_redirects": True,
        }
    ]


def test_profile_http_client_bypass_proxy_disables_proxy_mounts_without_env_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openai

    monkeypatch.setenv("NO_PROXY", "original.example")
    snapshots: list[str | None] = []

    def _ctor(**kwargs: Any) -> Any:
        snapshots.append(os.environ.get("NO_PROXY"))
        return _Spy(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", _ctor)
    import httpx

    timeout = httpx.Timeout(connect=1, read=2, write=2, pool=2)

    client = _build_profile_http_client(_profile(bypass_proxy=True), timeout)

    assert isinstance(client, _Spy)
    assert snapshots == ["original.example"]
    assert os.environ["NO_PROXY"] == "original.example"
    assert client.kwargs["verify"] is True
    assert client.kwargs["timeout"] is timeout
    assert client.kwargs["mounts"] == {
        "http://": None,
        "https://": None,
        "all://": None,
    }


def test_profile_http_client_bypass_proxy_combines_with_verify_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import httpx
    import openai

    calls: list[dict[str, Any]] = []

    def _ctor(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _Spy(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", _ctor)
    timeout = httpx.Timeout(connect=1, read=2, write=2, pool=2)

    client = _build_profile_http_client(_profile(verify_ssl=False, bypass_proxy=True), timeout)

    assert isinstance(client, _Spy)
    assert _pop_hook_names(calls[0]) == _ROUTE_HOOKS
    assert calls == [
        {
            "verify": False,
            "timeout": timeout,
            "follow_redirects": True,
            "mounts": {
                "http://": None,
                "https://": None,
                "all://": None,
            },
        }
    ]


@pytest.mark.asyncio
async def test_profile_http_client_bypass_proxy_reaches_origin_when_proxy_env_is_set(
    monkeypatch: pytest.MonkeyPatch,
    clear_proxy_env,
    local_http_server,
) -> None:
    """Owned model HTTP clients bypass env proxies for real requests."""
    import httpx

    clear_proxy_env()
    timeout = httpx.Timeout(connect=1, read=1, write=1, pool=1)

    async with (
        local_http_server(b"origin") as origin,
        local_http_server(b"proxy") as proxy,
    ):
        monkeypatch.setenv("HTTP_PROXY", proxy.url)

        control = httpx.AsyncClient(timeout=timeout)
        try:
            proxied = await control.get(f"{origin.url}/model")
        finally:
            await control.aclose()

        client = _build_profile_http_client(_profile(bypass_proxy=True), timeout)
        assert client is not None
        try:
            bypassed = await client.get(f"{origin.url}/model")
        finally:
            await client.aclose()

    assert proxied.text == "proxy"
    assert bypassed.text == "origin"
    assert proxy.hits and proxy.hits[0].startswith(f"GET {origin.url}/model ")
    assert origin.hits == ["GET /model HTTP/1.1"]


@pytest.mark.asyncio
async def test_profile_http_client_verify_ssl_false_accepts_self_signed_https(
    monkeypatch: pytest.MonkeyPatch,
    clear_proxy_env,
    local_http_server,
    self_signed_server_ssl_context,
) -> None:
    """Owned model HTTP clients thread verify=False into actual TLS handshakes."""
    import httpx

    clear_proxy_env()
    monkeypatch.setenv("NO_PROXY", "*")
    # Connect covers TCP + TLS handshake; freshly-started asyncio TLS servers
    # on Windows CI can take >1s for the first request, so a 1s connect floor
    # caused ConnectTimeout to leak past the ConnectError assertion below.
    timeout = httpx.Timeout(connect=10, read=5, write=5, pool=5)

    async with local_http_server(
        b"self-signed",
        scheme="https",
        ssl_context=self_signed_server_ssl_context,
    ) as server:
        control = httpx.AsyncClient(timeout=timeout, trust_env=False)
        try:
            with pytest.raises(httpx.ConnectError):
                await control.get(f"{server.url}/tls")
        finally:
            await control.aclose()

        client = _build_profile_http_client(_profile(verify_ssl=False), timeout)
        assert client is not None
        try:
            response = await client.get(f"{server.url}/tls")
        finally:
            await client.aclose()

    assert response.text == "self-signed"
    assert server.hits == ["GET /tls HTTP/1.1"]


# ───────────────────────── create_client dispatch ─────────────────────


def _patch_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the SDK client classes the factory constructs to the spy."""
    monkeypatch.setattr("chrys.service.llm.sdk_anthropic.RetryGuardedAsyncAnthropic", _Spy)
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)


_ASSEMBLED = "<assembled stack>"


def _capture_assembly(monkeypatch: pytest.MonkeyPatch, *, return_sdk_client: bool = False) -> dict[str, Any]:
    """Replace stack assembly with a recorder of what the factory hands it.

    The recorder returns ``_ASSEMBLED``, or the SDK client it was handed, so
    a test that sees that result knows the factory reached assembly.
    """
    from chrys.service.llm import clients

    captured: dict[str, Any] = {}

    def _record(client_cls: type[Any], sdk_client: Any, **kwargs: Any) -> Any:
        captured.update(kwargs, client_cls=client_cls, sdk_client=sdk_client)
        return sdk_client if return_sdk_client else _ASSEMBLED

    monkeypatch.setattr(clients, "_assemble_stack", create_autospec(clients._assemble_stack, side_effect=_record))
    return captured


async def test_create_client_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdks(monkeypatch)

    client = await create_client(_profile(provider="anthropic", api_key="k", model_id="claude-X"))
    raw_client = client.inner.inner

    assert isinstance(client, ToolLoopLayer)
    assert isinstance(client.inner, ChatMiddlewareLayer)
    assert isinstance(raw_client, AnthropicMessagesClient)
    assert raw_client.model == "claude-X"
    assert isinstance(raw_client.sdk_client, _Spy)
    assert client.max_iterations == 7777
    assert client.max_consecutive_errors == 10


async def test_create_client_anthropic_threads_owned_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdks(monkeypatch)

    import anthropic

    owned_http_client = _OwnedHttpClient()
    monkeypatch.setattr(anthropic, "DefaultAsyncHttpxClient", lambda **_kwargs: owned_http_client)

    client = await create_client(_profile(provider="anthropic", api_key="k", verify_ssl=False))
    inner = client.inner.inner.sdk_client

    assert inner.kwargs["http_client"] is owned_http_client


async def test_create_client_openai(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdks(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = await create_client(_profile(provider="openai", model_id="gpt-X"))
    raw_client = client.inner.inner

    assert isinstance(client, ToolLoopLayer)
    assert isinstance(raw_client, ChatCompletionsClient)
    assert raw_client.model == "gpt-X"
    assert isinstance(raw_client.sdk_client, _Spy)
    # The native SDK applies its own default endpoint.
    assert "base_url" not in raw_client.sdk_client.kwargs


async def test_create_client_openai_responses_assembles_the_responses_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdks(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    captured = _capture_assembly(monkeypatch)

    result = await create_client(_profile(provider="openai", api_style="responses", model_id="gpt-X"))

    assert result == _ASSEMBLED
    assert captured["client_cls"] is ResponsesApiClient
    assert captured["model_id"] == "gpt-X"
    assert isinstance(captured["sdk_client"], _Spy)
    assert captured["max_iterations"] == 7777
    assert captured["max_consecutive_errors"] == 10


async def test_create_client_raw_http_logging_uses_session_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _patch_sdks(monkeypatch)

    import openai

    captured_http_client_kwargs: dict[str, Any] = {}

    def _http_client_ctor(**kwargs: Any) -> Any:
        captured_http_client_kwargs.update(kwargs)
        return _Spy(**kwargs)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", _http_client_ctor)
    monkeypatch.setenv("CHRYS_DEBUG_LLM_RAW_HTTP_LOG", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    client = await create_client(
        _profile(provider="openai", model_id="gpt-X"), session_id="sess-raw", session_dir=tmp_path
    )

    inner = client.inner.inner.sdk_client
    assert isinstance(inner.kwargs["http_client"], _Spy)
    assert set(captured_http_client_kwargs["event_hooks"]) == {"request", "response"}


@pytest.mark.parametrize("provider", ["openai", "deepseek-openai", "glm-openai"])
async def test_create_client_openai_like_threads_owned_http_client(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    _patch_sdks(monkeypatch)

    import openai

    owned_http_client = _OwnedHttpClient()
    captured = _capture_assembly(monkeypatch)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **_kwargs: owned_http_client)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    result = await create_client(_profile(provider=provider, api_key="sk-fake", verify_ssl=False))

    assert result == _ASSEMBLED
    assert captured["sdk_client"].kwargs["http_client"] is owned_http_client


async def test_create_client_openai_responses_threads_owned_http_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdks(monkeypatch)

    import openai

    owned_http_client = _OwnedHttpClient()
    captured = _capture_assembly(monkeypatch)

    monkeypatch.setattr(openai, "DefaultAsyncHttpxClient", lambda **_kwargs: owned_http_client)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    result = await create_client(
        _profile(provider="openai", api_style="responses", api_key="sk-fake", verify_ssl=False)
    )

    assert result == _ASSEMBLED
    assert captured["client_cls"] is ResponsesApiClient
    assert captured["sdk_client"].kwargs["http_client"] is owned_http_client


async def test_create_client_openai_without_callbacks_rejects_a_choiceless_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``create_client`` for OpenAI returns the Chat Completions
    stack (not the DeepSeek subclass) on every path (judges, last-words,
    sub-agents, recall/compression), not only when intermediate-text callbacks
    are wired, and its decoder rejects a gateway error envelope that has no
    ``choices``."""
    from openai.types.chat.chat_completion import ChatCompletion

    from chrys.kernel import ChatClientException

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    client = await create_client(_profile(provider="openai", api_key="sk-fake", model_id="gpt-X"))

    cls = type(client.inner.inner)
    # Plain OpenAI profiles MUST NOT silently route through the DeepSeek
    # subclass — that would change provider policy (tool-gated reasoning
    # replay, legacy max_tokens output cap) for every OpenAI user.
    assert cls is ChatCompletionsClient

    # End-to-end: a 200 response with choices=None should surface as ChatClientException
    # with the gateway's error fields.
    bad = ChatCompletion.model_construct(
        id="r",
        choices=None,
        created=0,
        model="gpt-X",
        object="chat.completion",
        error={"message": "gateway oops"},
    )
    with pytest.raises(ChatClientException) as exc_info:
        decode_completion(bad, {}, variant=client.VARIANT)
    assert "gateway oops" in str(exc_info.value)


async def test_create_client_openai_with_deepseek_base_url_does_not_route_through_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider type — not URL/model-id sniffing — selects the client class.

    A user who points an OpenAI-compatible gateway at a DeepSeek-flavoured
    URL should get the plain OpenAI client unless they explicitly set
    ``provider: deepseek-openai``.  This protects against silent wire-format
    changes from heuristics."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    client = await create_client(
        _profile(
            provider="openai",
            api_key="sk-fake",
            base_url="https://api.deepseek.com",
            model_id="deepseek-reasoner",
        )
    )

    assert type(client.inner.inner) is ChatCompletionsClient


async def test_create_client_deepseek_uses_deepseek_chat_completion_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``provider: deepseek-openai`` opts into reasoning_content replay compatibility."""
    from chrys.service.llm.chat_completions import DeepSeekChatCompletionsClient

    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    client = await create_client(
        _profile(
            provider="deepseek-openai",
            api_key="sk-fake",
            model_id="deepseek-reasoner",
        )
    )

    assert type(client.inner.inner) is DeepSeekChatCompletionsClient


async def test_create_client_deepseek_responses_uses_deepseek_responses_client(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.llm.openai_responses import DeepSeekResponsesApiClient

    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    client = await create_client(
        _profile(
            provider="deepseek-openai",
            api_style="responses",
            api_key="sk-fake",
            model_id="deepseek-reasoner",
        )
    )

    assert type(client.inner.inner) is DeepSeekResponsesApiClient
    assert client.FORCES_STATELESS is True


async def test_create_client_glm_uses_glm_chat_completion_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """``provider: glm-openai`` opts into GLM preserved-thinking replay compatibility."""
    from chrys.service.llm.chat_completions import GlmChatCompletionsClient

    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    client = await create_client(
        _profile(
            provider="glm-openai",
            api_key="sk-fake",
            model_id="glm-5.2",
        )
    )

    assert type(client.inner.inner) is GlmChatCompletionsClient


async def test_create_client_glm_defaults_base_url_when_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``glm-openai`` profile with no base_url falls back to the public GLM API."""
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        captured.update(kwargs)
        return AsyncOpenAI(api_key="x")

    monkeypatch.setattr("chrys.service.llm.clients._build_openai_sdk_client", _capture)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    await create_client(_profile(provider="glm-openai", api_key="sk-glm", model_id="glm-5.2"))

    assert captured["api_key"] == "sk-glm"
    assert captured["api_key_env"] == "ZAI_API_KEY"
    assert captured["base_url_env"] == "ZAI_BASE_URL"
    assert captured["default_base_url"] == "https://open.bigmodel.cn/api/paas/v4"


async def test_create_client_deepseek_defaults_base_url_when_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``deepseek-openai`` profile with no base_url falls back to the public DeepSeek API.

    This is the primary user-facing reason to add a dedicated provider:
    once you select DeepSeek, you don't have to remember the base URL."""
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> Any:
        from openai import AsyncOpenAI

        captured.update(kwargs)
        return AsyncOpenAI(api_key="x")

    monkeypatch.setattr("chrys.service.llm.clients._build_openai_sdk_client", _capture)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    await create_client(_profile(provider="deepseek-openai", api_key="sk-ds", model_id="deepseek-chat"))

    assert captured["api_key"] == "sk-ds"
    assert captured["api_key_env"] == "DEEPSEEK_API_KEY"
    assert captured["base_url_env"] == "DEEPSEEK_BASE_URL"
    assert captured["default_base_url"] == "https://api.deepseek.com"


def test_create_client_deepseek_falls_back_to_deepseek_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty profile credentials → ``_build_openai_sdk_client`` reads DEEPSEEK_API_KEY.

    The factory pins ``api_key_env="DEEPSEEK_API_KEY"`` for this branch so
    ``OPENAI_API_KEY`` from the user's shell can never leak into a
    DeepSeek call."""
    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _Spy)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek.example.com")
    # OPENAI_* must NOT leak into the DeepSeek branch
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-should-not-leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai.example.com")

    inner = _build_openai_sdk_client(
        api_key="",
        base_url="",
        timeout=None,
        max_retries=1,
        default_headers=None,
        api_key_env="DEEPSEEK_API_KEY",
        base_url_env="DEEPSEEK_BASE_URL",
        default_base_url="https://api.deepseek.com",
    )

    assert isinstance(inner, _Spy)
    assert inner.kwargs["api_key"] == "sk-from-env"
    assert inner.kwargs["base_url"] == "https://deepseek.example.com"


@pytest.mark.parametrize(
    ("provider", "api_key_env", "base_url_env"),
    [
        ("openai", "OPENAI_API_KEY", "OPENAI_BASE_URL"),
        ("deepseek-openai", "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL"),
        ("glm-openai", "ZAI_API_KEY", "ZAI_BASE_URL"),
    ],
)
async def test_create_client_openai_like_passes_empty_key_when_profile_and_env_missing(
    provider: str,
    api_key_env: str,
    base_url_env: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenAI-compatible providers must not rely on SDK env fallback for missing keys."""

    class _StrictAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            if "api_key" not in kwargs or kwargs["api_key"] is None:
                raise RuntimeError("api_key was not passed explicitly")
            self.kwargs = kwargs
            self.auth_headers: dict[str, str] = {}
            self.default_headers = kwargs.get("default_headers") or {}

    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _StrictAsyncOpenAI)
    captured = _capture_assembly(monkeypatch, return_sdk_client=True)
    monkeypatch.delenv(api_key_env, raising=False)
    monkeypatch.delenv(base_url_env, raising=False)
    # Also clear the other OpenAI-compatible env pair so cross-provider
    # leakage cannot mask a missing provider-specific key.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    inner = await create_client(_profile(provider=provider, api_key=""))

    assert isinstance(inner, _StrictAsyncOpenAI)
    assert captured["sdk_client"] is inner
    await _assert_empty_api_key_provider(inner.kwargs["api_key"])


async def test_create_client_openai_responses_passes_empty_key_when_profile_and_env_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class _StrictAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            if "api_key" not in kwargs or kwargs["api_key"] is None:
                raise RuntimeError("api_key was not passed explicitly")
            self.kwargs = kwargs
            self.auth_headers: dict[str, str] = {}
            self.default_headers = kwargs.get("default_headers") or {}

    monkeypatch.setattr("chrys.service.llm.sdk_openai.RetryGuardedAsyncOpenAI", _StrictAsyncOpenAI)
    captured = _capture_assembly(monkeypatch, return_sdk_client=True)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    inner = await create_client(_profile(provider="openai", api_style="responses", api_key=""))

    assert isinstance(inner, _StrictAsyncOpenAI)
    assert captured["client_cls"] is ResponsesApiClient
    await _assert_empty_api_key_provider(inner.kwargs["api_key"])


async def test_create_client_mock() -> None:
    from chrys.service.llm.mock import MockChatClient

    client = await create_client(_profile(provider="mock"))
    assert isinstance(client, MockChatClient)


async def test_create_client_mock_passes_intermediate_callbacks() -> None:
    from chrys.service.llm.mock import MockChatClient

    async def _acb(_text: str) -> None:
        pass

    def _scb(_text: str) -> None:
        pass

    client = await create_client(
        _profile(provider="mock"),
        on_intermediate_text_async=_acb,
        on_intermediate_text_sync=_scb,
    )
    assert isinstance(client, MockChatClient)


async def test_create_client_unknown_provider_raises() -> None:
    with pytest.raises(ValueError, match="Unknown provider"):
        await create_client(_profile(provider="azure"))


async def test_create_client_unknown_provider_message_lists_all_providers() -> None:
    """The error message should advertise every OpenAI-compatible provider id."""
    with pytest.raises(ValueError) as exc_info:
        await create_client(_profile(provider="azure"))
    assert "deepseek-openai" in str(exc_info.value)
    assert "glm-openai" in str(exc_info.value)
    assert "openai" in str(exc_info.value)
    assert "anthropic" in str(exc_info.value)


async def test_create_client_passes_session_id_into_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session ID flows through default headers."""
    _patch_sdks(monkeypatch)

    client = await create_client(
        _profile(provider="anthropic", api_key="k"),
        session_id="sess-42",
        parent_session_id="parent-42",
    )

    # Spy for AsyncAnthropic records the headers that were passed in
    inner_anthropic_spy = client.inner.inner.sdk_client
    assert inner_anthropic_spy.kwargs["default_headers"][X_SESSION_ID_HEADER] == "sess-42"
    assert inner_anthropic_spy.kwargs["default_headers"][SESSION_ID_HEADER] == "sess-42"
    assert inner_anthropic_spy.kwargs["default_headers"][X_PARENT_SESSION_ID_HEADER] == "parent-42"
    assert inner_anthropic_spy.kwargs["default_headers"][PARENT_SESSION_ID_HEADER] == "parent-42"


async def test_create_client_anthropic_passes_model_id_header(monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    from chrys import __version__

    _patch_sdks(monkeypatch)

    client = await create_client(_profile(provider="anthropic", api_key="k", model_id="claude-header"))

    inner = client.inner.inner.sdk_client
    assert inner.kwargs["default_headers"][MODEL_ID_HEADER] == "claude-header"
    runtime = sys.version_info
    assert (
        inner.kwargs["default_headers"]["User-Agent"]
        == f"Chrys/{__version__} Python/{runtime.major}.{runtime.minor}.{runtime.micro} "
        f"AsyncAnthropic/Python {anthropic.__version__}"
    )


@pytest.mark.parametrize("provider", ["openai", "deepseek-openai", "glm-openai"])
async def test_create_client_openai_like_passes_model_id_header(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    import openai

    from chrys import __version__

    _patch_sdks(monkeypatch)
    captured = _capture_assembly(monkeypatch, return_sdk_client=True)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_BASE_URL", raising=False)

    inner = await create_client(_profile(provider=provider, api_key="sk-fake", model_id=f"{provider}-header"))

    assert isinstance(inner, _Spy)
    assert captured["sdk_client"] is inner
    assert inner.kwargs["default_headers"][MODEL_ID_HEADER] == f"{provider}-header"
    runtime = sys.version_info
    assert (
        inner.kwargs["default_headers"]["User-Agent"]
        == f"Chrys/{__version__} Python/{runtime.major}.{runtime.minor}.{runtime.micro} "
        f"AsyncOpenAI/Python {openai.__version__}"
    )


# ───────────────────────── stack assembly ─────────────────────────────


async def test_create_client_anthropic_forwards_async_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """An intermediate-text callback reaches the assembled Anthropic stack."""
    _patch_sdks(monkeypatch)
    captured = _capture_assembly(monkeypatch)

    async def _acb(_text: str) -> None:
        pass

    result = await create_client(
        _profile(provider="anthropic", api_key="k", model_id="claude-Y"),
        on_intermediate_text_async=_acb,
    )
    assert result == _ASSEMBLED
    assert captured["client_cls"] is AnthropicMessagesClient
    assert isinstance(captured["sdk_client"], _Spy)
    assert captured["model_id"] == "claude-Y"
    assert captured["on_intermediate_text_async"] is _acb
    assert captured["on_intermediate_text_sync"] is None


async def test_create_client_openai_forwards_sync_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """OpenAI profiles assemble the plain Chat Completions client; sync
    callbacks are forwarded without DeepSeek compatibility by default."""
    _patch_sdks(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-anything")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    captured = _capture_assembly(monkeypatch)

    def _scb(_text: str) -> None:
        pass

    result = await create_client(
        _profile(provider="openai", model_id="gpt-Y"),
        on_intermediate_text_sync=_scb,
        use_route_session_context=True,
    )
    assert result == _ASSEMBLED
    assert captured["model_id"] == "gpt-Y"
    assert captured["on_intermediate_text_sync"] is _scb
    assert captured["on_intermediate_text_async"] is None
    assert captured["use_route_session_context"] is True
    assert captured["client_cls"] is ChatCompletionsClient


async def test_create_client_deepseek_forwards_deepseek_chat_client_cls(monkeypatch: pytest.MonkeyPatch) -> None:
    """``provider: deepseek-openai`` assembles ``DeepSeekChatCompletionsClient``."""
    from chrys.service.llm.chat_completions import DeepSeekChatCompletionsClient

    _patch_sdks(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    captured = _capture_assembly(monkeypatch)

    result = await create_client(
        _profile(provider="deepseek-openai", model_id="deepseek-chat"),
    )

    assert result == _ASSEMBLED
    assert captured["client_cls"] is DeepSeekChatCompletionsClient
    assert captured["model_id"] == "deepseek-chat"


async def test_create_client_deepseek_forwards_deepseek_responses_client_cls(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.service.llm.openai_responses import DeepSeekResponsesApiClient

    _patch_sdks(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://deepseek-responses.test/v1")
    captured = _capture_assembly(monkeypatch)

    result = await create_client(
        _profile(
            provider="deepseek-openai",
            api_style="responses",
            model_id="deepseek-reasoner",
            http_headers='{"X-Provider": "deepseek"}',
        ),
        session_id="session-1",
        parent_session_id="parent-1",
    )

    assert result == _ASSEMBLED
    assert captured["client_cls"] is DeepSeekResponsesApiClient
    assert captured["model_id"] == "deepseek-reasoner"
    assert captured["session_id"] == "session-1"
    assert captured["parent_session_id"] == "parent-1"


async def test_create_client_deepseek_rejects_unknown_api_style(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_sdks(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")

    with pytest.raises(ValueError, match="Unknown DeepSeek api_style"):
        await create_client(_profile(provider="deepseek-openai", api_style="future"))


async def test_create_client_glm_forwards_glm_chat_client_cls(monkeypatch: pytest.MonkeyPatch) -> None:
    """``provider: glm-openai`` assembles ``GlmChatCompletionsClient``."""
    from chrys.service.llm.chat_completions import GlmChatCompletionsClient

    _patch_sdks(monkeypatch)
    monkeypatch.setenv("ZAI_API_KEY", "sk-glm")
    captured = _capture_assembly(monkeypatch)

    result = await create_client(
        _profile(provider="glm-openai", model_id="glm-5.2"),
    )

    assert result == _ASSEMBLED
    assert captured["client_cls"] is GlmChatCompletionsClient
    assert captured["model_id"] == "glm-5.2"


async def test_create_client_deepseek_forwards_intermediate_text_callbacks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stream + non-stream intermediate-text callbacks must reach the deepseek branch.

    This is one of the chrys touch surfaces that needs parity across all
    providers: real-time rendering of agent text emitted alongside tool
    calls, in both stream and non-stream modes."""
    _patch_sdks(monkeypatch)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    captured = _capture_assembly(monkeypatch)

    async def _acb(_text: str) -> None:
        pass

    def _scb(_text: str) -> None:
        pass

    result = await create_client(
        _profile(provider="deepseek-openai", model_id="deepseek-chat"),
        on_intermediate_text_async=_acb,
        on_intermediate_text_sync=_scb,
    )

    assert result == _ASSEMBLED
    assert captured["on_intermediate_text_async"] is _acb
    assert captured["on_intermediate_text_sync"] is _scb


# ───────────────────────── httpx.Timeout wiring ───────────────────────


async def test_timeout_and_max_retries_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Profile's http_* fields flow into the httpx.Timeout passed to the SDK."""
    _patch_sdks(monkeypatch)

    profile = ModelProfile(
        id="p",
        name="p",
        provider="anthropic",
        model_id="claude-Z",
        api_key="k",
        http_connect_timeout=7.5,
        http_read_timeout=42.0,
        http_max_retries=9,
    )
    client = await create_client(profile)
    inner = client.inner.inner.sdk_client
    assert inner.kwargs["max_retries"] == 9

    import httpx

    t = inner.kwargs["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.connect == 7.5
    assert t.read == 42.0
    assert t.write == 42.0
    assert t.pool == 42.0
