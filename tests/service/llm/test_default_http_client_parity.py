# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The HTTP client Chrys builds for a default profile matches the one each SDK builds itself.

Chrys builds every profile's pool so it can own and close it; a default
profile must still connect exactly as the SDK would have.  The only allowed
difference is ``base_url``, which the SDKs never rely on: they send absolute
URLs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anthropic
import certifi
import httpx
import openai
import pytest

from chrys.service.llm.clients import _build_profile_http_client, create_client
from chrys.service.profiles.models.schema import ModelProfile

_BASE_URLS = {"openai": "https://provider.example/v1", "anthropic": "https://provider.example"}
_PROXY_ENVS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _profile(provider: str) -> ModelProfile:
    return ModelProfile(
        id="p",
        name="p",
        provider=provider,
        model_id="test-model",
        api_key="sk-test",
        base_url=_BASE_URLS[provider],
        http_max_retries=0,
    )


def _timeout(profile: ModelProfile) -> httpx.Timeout:
    return httpx.Timeout(
        connect=profile.http_connect_timeout,
        read=profile.http_read_timeout,
        write=profile.http_read_timeout,
        pool=profile.http_read_timeout,
    )


def _sdk_built_http_client(provider: str, timeout: httpx.Timeout) -> httpx.AsyncClient:
    if provider == "anthropic":
        return anthropic.AsyncAnthropic(api_key="sk-test", base_url=_BASE_URLS[provider], timeout=timeout)._client
    return openai.AsyncOpenAI(api_key="sk-test", base_url=_BASE_URLS[provider], timeout=timeout)._client


def _pool_facts(transport: Any) -> dict[str, Any]:
    pool = transport._pool
    proxy_url = getattr(pool, "_proxy_url", None)
    return {
        "pool": type(pool).__name__,
        "max_connections": pool._max_connections,
        "max_keepalive_connections": pool._max_keepalive_connections,
        "keepalive_expiry": pool._keepalive_expiry,
        "http1": pool._http1,
        "http2": pool._http2,
        "retries": pool._retries,
        "socket_options": pool._socket_options,
        "verify_mode": pool._ssl_context.verify_mode,
        "check_hostname": pool._ssl_context.check_hostname,
        "cert_store": pool._ssl_context.cert_store_stats(),
        "proxy_url": None if proxy_url is None else (proxy_url.scheme, proxy_url.host, proxy_url.port),
    }


def _client_facts(client: httpx.AsyncClient) -> dict[str, Any]:
    return {
        "timeout": client.timeout,
        "follow_redirects": client.follow_redirects,
        "max_redirects": client.max_redirects,
        "trust_env": client.trust_env,
        "headers": dict(client.headers),
        "transport": _pool_facts(client._transport),
        "mounts": {
            pattern.pattern: None if transport is None else _pool_facts(transport)
            for pattern, transport in client._mounts.items()
        },
    }


def _hook_names(client: httpx.AsyncClient) -> dict[str, list[str]]:
    return {event: [hook.__qualname__ for hook in hooks] for event, hooks in client.event_hooks.items()}


@pytest.fixture
def clean_network_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (*_PROXY_ENVS, *(name.lower() for name in _PROXY_ENVS), "SSL_CERT_FILE", "SSL_CERT_DIR"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _proxies(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://https-proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://all-proxy.example:8080")
    monkeypatch.setenv("NO_PROXY", "localhost,.internal.example")


def _ca_bundle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pem = Path(certifi.where()).read_text(encoding="utf-8")
    end = "-----END CERTIFICATE-----"
    first = pem[pem.index("-----BEGIN CERTIFICATE-----") : pem.index(end) + len(end)]
    bundle = tmp_path / "ca.pem"
    bundle.write_text(first + "\n", encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(bundle))


_ENVIRONMENTS = {"default": lambda _monkeypatch, _tmp_path: None, "proxies": _proxies, "ca_bundle": _ca_bundle}


@pytest.mark.parametrize("environment", sorted(_ENVIRONMENTS))
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_chrys_built_client_matches_the_sdk_default(
    provider: str, environment: str, clean_network_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _ENVIRONMENTS[environment](clean_network_env, tmp_path)
    profile = _profile(provider)
    timeout = _timeout(profile)
    sdk_built = _sdk_built_http_client(provider, timeout)
    chrys_built = _build_profile_http_client(profile, timeout)
    try:
        sdk_facts = _client_facts(sdk_built)
        assert _client_facts(chrys_built) == sdk_facts
        # The allowed differences: base_url, and Chrys's route hooks.
        assert chrys_built.base_url != sdk_built.base_url
        assert chrys_built.base_url == httpx.URL("")
        assert sdk_built.event_hooks == {"request": [], "response": []}
        assert _hook_names(chrys_built) == {
            "request": ["build_route_hooks.<locals>.stamp", "build_request_tracking_hooks.<locals>.prepared"],
            "response": ["build_route_hooks.<locals>.record", "build_request_tracking_hooks.<locals>.received"],
        }
    finally:
        await sdk_built.aclose()
        await chrys_built.aclose()

    if environment == "proxies":
        assert {pattern: facts and facts["proxy_url"] for pattern, facts in sdk_facts["mounts"].items()} == {
            "https://": (b"http", b"https-proxy.example", 3128),
            "all://": (b"http", b"all-proxy.example", 8080),
            "all://localhost": None,
            "all://*.internal.example": None,
        }
    if environment == "ca_bundle":
        assert sdk_facts["transport"]["cert_store"]["x509_ca"] == 1
    if provider == "anthropic":
        assert sdk_facts["transport"]["socket_options"]


class _Stop(Exception):
    """Raised by the request hook so no request leaves the process."""


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("openai", "https://provider.example/v1/chat/completions"),
        ("anthropic", "https://provider.example/v1/messages"),
    ],
)
async def test_sdk_requests_use_absolute_urls(provider: str, url: str) -> None:
    stack = await create_client(_profile(provider))
    raw = stack.inner.inner
    sdk = raw.sdk_client
    http_client: httpx.AsyncClient = sdk._client
    seen: list[httpx.URL] = []

    async def record(request: httpx.Request) -> None:
        seen.append(request.url)
        raise _Stop

    http_client.event_hooks = {"request": [record], "response": []}
    try:
        with pytest.raises(Exception):  # noqa: B017 - the SDK wraps the hook's error in its own type
            if provider == "anthropic":
                await sdk.messages.create(
                    model="test-model", max_tokens=1, messages=[{"role": "user", "content": "hi"}]
                )
            else:
                await sdk.chat.completions.create(model="test-model", messages=[{"role": "user", "content": "hi"}])
    finally:
        await stack.aclose()

    assert http_client.base_url == httpx.URL("")
    assert seen == [httpx.URL(url)]
