# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Route snapshots ride request extensions only: never on the wire, never replacing other extensions."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from chrys.foundation.errors import ROUTE_EXTENSION_KEY, Origin, RouteFacts
from chrys.service.llm.clients import create_client
from chrys.service.llm.proxy_route import ProxyRouter
from chrys.service.llm.route_facts import build_route_hooks
from chrys.service.profiles.models.schema import ModelProfile

_SESSION_ID = "wire-session"
_CHAT = [{"role": "user", "content": "hi"}]
_OPENAI_REPLY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
_ANTHROPIC_REPLY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@dataclass(frozen=True, slots=True)
class _Reply:
    status: int
    body: bytes = b"{}"
    headers: tuple[tuple[str, str], ...] = ()


def _json(status: int, payload: dict[str, Any]) -> _Reply:
    return _Reply(status, json.dumps(payload).encode())


@dataclass(slots=True)
class _WireServer:
    """A loopback HTTP/1.1 server that keeps each request's raw bytes and answers from a script."""

    replies: list[_Reply]
    raw: list[bytes] = field(default_factory=list)
    url: str = ""

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while head := await reader.readuntil(b"\r\n\r\n"):
                length = next(
                    (
                        int(line.split(b":", 1)[1])
                        for line in head.split(b"\r\n")
                        if line.lower().startswith(b"content-length:")
                    ),
                    0,
                )
                self.raw.append(head + await reader.readexactly(length))
                reply = self.replies.pop(0)
                extra = "".join(f"{name}: {value}\r\n" for name, value in reply.headers)
                writer.write(
                    f"HTTP/1.1 {reply.status} X\r\nContent-Type: application/json\r\n{extra}"
                    f"Content-Length: {len(reply.body)}\r\n\r\n".encode()
                    + reply.body
                )
                await writer.drain()
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()


@contextlib.asynccontextmanager
async def _serve(*replies: _Reply) -> AsyncIterator[_WireServer]:
    wire = _WireServer(list(replies))
    server = await asyncio.start_server(wire.handle, "127.0.0.1", 0)
    wire.url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield wire
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


@pytest.fixture
def direct_env(monkeypatch: pytest.MonkeyPatch, clear_proxy_env: Callable[[], None]) -> None:
    # With no proxy env at all, httpx falls back to the OS proxy settings.
    clear_proxy_env()
    monkeypatch.setenv("NO_PROXY", "*")


def _profile(provider: str, base_url: str, *, max_retries: int = 0) -> ModelProfile:
    return ModelProfile(
        id="p",
        name="p",
        provider=provider,
        model_id="test-model",
        api_key="sk-test",
        base_url=base_url if provider == "anthropic" else f"{base_url}/v1",
        http_max_retries=max_retries,
    )


def _sdk(stack: Any, provider: str) -> Any:
    raw = stack.inner.inner
    return raw.sdk_client


def _is_route_hook(hook: Callable[..., Any]) -> bool:
    return hook.__qualname__.startswith("build_route_hooks.")


async def _send(sdk: Any, provider: str) -> None:
    if provider == "anthropic":
        await sdk.messages.create(model="test-model", max_tokens=8, messages=_CHAT)
    else:
        await sdk.chat.completions.create(model="test-model", messages=_CHAT)


@pytest.mark.usefixtures("direct_env")
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_route_hook_adds_nothing_to_the_wire(provider: str) -> None:
    reply = _json(200, _ANTHROPIC_REPLY if provider == "anthropic" else _OPENAI_REPLY)
    async with _serve(reply, reply) as wire:
        routed = await create_client(_profile(provider, wire.url), session_id=_SESSION_ID)
        control = await create_client(_profile(provider, wire.url), session_id=_SESSION_ID)
        try:
            control_http: httpx.AsyncClient = _sdk(control, provider)._client
            control_http.event_hooks = {
                event: [hook for hook in hooks if not _is_route_hook(hook)]
                for event, hooks in control_http.event_hooks.items()
            }
            assert {
                event: [hook.__qualname__ for hook in hooks] for event, hooks in control_http.event_hooks.items()
            } == {
                "request": ["build_request_tracking_hooks.<locals>.prepared"],
                "response": ["build_request_tracking_hooks.<locals>.received"],
            }
            await _send(_sdk(routed, provider), provider)
            await _send(_sdk(control, provider), provider)
        finally:
            await routed.aclose()
            await control.aclose()

    routed_bytes, control_bytes = wire.raw

    # Request line, every header (name, value, order) and body, byte for byte.
    # Attempt identity deliberately differs; only route hooks are under comparison.
    def without_attempt_header(raw: bytes) -> list[bytes]:
        return [line for line in raw.split(b"\r\n") if not line.lower().startswith(b"chrys-request-attempt-id:")]

    assert without_attempt_header(routed_bytes) == without_attempt_header(control_bytes)


class _RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.extensions: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.extensions.append(dict(request.extensions))
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


@pytest.mark.usefixtures("direct_env")
async def test_route_hook_keeps_existing_extensions() -> None:
    traced: list[str] = []

    async def trace(event_name: str, _info: dict[str, Any]) -> None:
        traced.append(event_name)

    transport = _RecordingTransport(httpx.AsyncHTTPTransport())
    router = ProxyRouter.from_client_config(bypass_proxy=False)
    async with (
        _serve(_json(200, {})) as wire,
        httpx.AsyncClient(transport=transport, event_hooks=build_route_hooks(router)) as client,
    ):
        request = client.build_request("POST", f"{wire.url}/v1", json={}, extensions={"trace": trace})
        assert (await client.send(request)).status_code == 200

    [seen] = transport.extensions
    assert seen["timeout"] == httpx.Timeout(5.0).as_dict()
    assert seen["trace"] is trace
    assert traced
    assert isinstance(seen[ROUTE_EXTENSION_KEY], RouteFacts)


@pytest.mark.usefixtures("direct_env")
async def test_each_sdk_retry_and_redirect_gets_a_fresh_snapshot() -> None:
    async with _serve(_json(200, _OPENAI_REPLY)) as other:
        failed = _Reply(500, b'{"error": {"message": "boom"}}', (("retry-after-ms", "1"),))
        redirect = _Reply(302, b"", (("Location", f"{other.url}/v1/chat/completions"),))
        async with _serve(failed, redirect) as first:
            stack = await create_client(_profile("openai", first.url, max_retries=1), session_id=_SESSION_ID)
            snapshots: list[object] = []

            async def record(request: httpx.Request) -> None:
                snapshots.append(request.extensions.get(ROUTE_EXTENSION_KEY))

            try:
                sdk = _sdk(stack, "openai")
                http_client: httpx.AsyncClient = sdk._client
                http_client.event_hooks["request"].append(record)
                await _send(sdk, "openai")
            finally:
                await stack.aclose()

    first_origin = Origin("http", "127.0.0.1", httpx.URL(first.url).port or 0)
    other_origin = Origin("http", "127.0.0.1", httpx.URL(other.url).port or 0)
    assert snapshots == [
        RouteFacts(first_origin, None, first_hop_reached=False),
        # The SDK's retry, after the 500 recorded the first hop as reached.
        RouteFacts(first_origin, None, first_hop_reached=True),
        # The redirect to a first hop that never answered before.
        RouteFacts(other_origin, None, first_hop_reached=False),
    ]
    assert len(first.raw) == 2
    assert len(other.raw) == 1
