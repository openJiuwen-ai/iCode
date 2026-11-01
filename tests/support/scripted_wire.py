# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The production client stack over a scripted HTTP transport.

``route_clients_to`` sends every request of the clients ``create_client``
builds through an ``httpx`` transport over the real provider SDKs;
``ScriptedWire`` answers them from a script (``tests/support/wire_cases/``).
"""

from __future__ import annotations

import functools
import inspect
import os
from typing import TYPE_CHECKING, Any
from unittest import mock

import httpx
import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path

    from chrys.service.profiles.models.schema import ModelProfile
    from tests.support.wire_cases import Reply

_ENV_PREFIXES = ("OPENAI_", "ANTHROPIC_", "DEEPSEEK_", "ZAI_", "CHRYS_")
_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def pin_wire_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate a test from the provider environment and the SDK backoff.

    Provider, SDK and Chrys variables and every proxy variable are removed,
    and ``NO_PROXY=*`` keeps httpx from falling back to the OS proxy.  The SDK
    backoff is zero, so a request the SDK retries is retried without waiting.
    """
    import anthropic
    import openai

    for key in list(os.environ):
        if key.startswith(_ENV_PREFIXES) or key in _PROXY_ENV:
            monkeypatch.delenv(key)
    monkeypatch.setenv("NO_PROXY", "*")

    for sdk_client in (openai.AsyncOpenAI, anthropic.AsyncAnthropic):
        no_backoff = mock.create_autospec(sdk_client._calculate_retry_timeout, return_value=0.0)
        monkeypatch.setattr(sdk_client, "_calculate_retry_timeout", no_backoff)


def route_clients_to(transport: httpx.AsyncBaseTransport, monkeypatch: pytest.MonkeyPatch) -> None:
    """Send every request of the clients ``create_client`` builds from now on through *transport*."""
    import chrys.service.llm.clients as clients_module

    # Unwrapped, so routing again replaces the earlier transport instead of wrapping it.
    build = inspect.unwrap(clients_module._build_profile_http_client)

    @functools.wraps(build)
    def build_over(
        profile: ModelProfile,
        timeout: Any,
        *,
        raw_http_log_path: Path | None = None,
        session_id: str | None = None,
        auth: httpx.Auth | None = None,
    ) -> httpx.AsyncClient:
        return build(
            profile,
            timeout,
            raw_http_log_path=raw_http_log_path,
            session_id=session_id,
            auth=auth,
            transport=transport,
        )

    monkeypatch.setattr(clients_module, "_build_profile_http_client", build_over)


class ScriptedWire:
    """Answer each request with the next scripted reply and keep the requests sent."""

    def __init__(self, replies: Sequence[Reply]) -> None:
        self._replies = list(replies)
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self._replies:
            raise AssertionError(f"unscripted request: {request.method} {request.url}")
        reply = self._replies.pop(0)
        if reply.breaks_off:
            return httpx.Response(
                reply.status, headers=list(reply.headers), stream=BrokenBody(reply.body), request=request
            )
        return httpx.Response(reply.status, headers=list(reply.headers), content=reply.body, request=request)


class BrokenBody(httpx.AsyncByteStream):
    """A response body whose connection is lost once *body* is sent."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self._body:
            yield self._body
        raise httpx.ReadError("Connection reset by peer")

    async def aclose(self) -> None:
        return None
