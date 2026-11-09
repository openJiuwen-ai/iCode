# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the HTTP MCP transport: the patched streamable client, _HTTPMCPTool options, header hooks, and sockets."""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import os
import socket
import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import chrys.service.mcp._http_transport as http_transport_module
from chrys.service.mcp._http_transport import (
    _chrys_streamable_http_client,
    _HTTPMCPTool,
    _url_origin,
)
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.mcp.owned import LOCAL_HTTP_FAILURE_ERROR_DATA, MCPStreamableHTTPTool
from chrys.service.profiles.agents.schema import MCPServerConfig
from tests.service.mcp._helpers import block_import
from tests.support.gc_isolation import earlier_tests_heap_frozen

# ---------------------------------------------------------------------------
# _chrys_streamable_http_client — POST-failure wake-up and stock-client fallbacks
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _post_failure_transport(
    message: Any,
    exc: BaseException,
    *,
    session_id: str | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> Iterator[MagicMock]:
    """Patch the SDK transport so ``post_writer`` routes ``message`` through
    ``_handle_post_request`` against an httpx client whose ``send`` raises ``exc``.

    Yields the mock client.  ``session_id`` stamps the transport session and
    starts the GET stream first (as a real post-initialize writer would);
    ``on_error`` receives an exception ``_handle_post_request`` re-raises
    instead of letting it escape the writer task.
    """
    from mcp.client.streamable_http import StreamableHTTPTransport
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage

    client = MagicMock()

    async def post_writer(
        self: Any,
        client_arg: object,
        write_stream_reader: object,
        read_stream_writer: object,
        write_stream: object,
        start_get_stream: Any,
        tg: object,
    ) -> None:
        if session_id is not None:
            self.session_id = session_id
            start_get_stream()
        ctx = SimpleNamespace(
            client=client_arg,
            session_message=SessionMessage(message=JSONRPCMessage(message)),
            read_stream_writer=read_stream_writer,
        )
        try:
            await self._handle_post_request(ctx)
        except Exception as caught:
            if on_error is None:
                raise
            on_error(caught)

    async def handle_get_stream(self: object, client_arg: object, read_stream_writer: object) -> None:
        return None

    with (
        patch.object(client, "send", new=AsyncMock(side_effect=exc)),
        patch.object(StreamableHTTPTransport, "post_writer", new=post_writer),
        patch.object(StreamableHTTPTransport, "handle_get_stream", new=handle_get_stream),
    ):
        yield client


async def test_streamable_http_post_failure_wakes_pending_request() -> None:
    """A failing HTTP POST task must wake the pending request with JSON-RPC error."""
    from mcp.client.streamable_http import StreamableHTTPTransport
    from mcp.types import CONNECTION_CLOSED, JSONRPCError, JSONRPCRequest

    terminated: list[tuple[str | None, object]] = []

    async def terminate_session(self: Any, client_arg: object) -> None:
        terminated.append((self.session_id, client_arg))

    request = JSONRPCRequest(jsonrpc="2.0", id=7, method="tools/list")
    with (
        _post_failure_transport(request, RuntimeError("post exploded"), session_id="sess-1") as client,
        patch.object(StreamableHTTPTransport, "terminate_session", new=terminate_session),
    ):
        async with _chrys_streamable_http_client(
            "http://mcp.example/mcp",
            http_client=client,
            terminate_on_close=True,
        ) as (read_stream, _write_stream, get_session_id):
            received = await asyncio.wait_for(read_stream.receive(), timeout=20.0)
            assert get_session_id() == "sess-1"

    error = received.message.root
    assert isinstance(error, JSONRPCError)
    assert error.id == 7
    assert error.error.code == CONNECTION_CLOSED
    assert "post exploded" in error.error.message
    # Marked local, so the tool loop never hands this detail to the model.
    assert error.error.data == LOCAL_HTTP_FAILURE_ERROR_DATA
    assert terminated == [("sess-1", client)]


async def test_a_server_that_stops_answering_the_session_delete_cannot_hold_the_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp.client.streamable_http import StreamableHTTPTransport
    from mcp.types import JSONRPCRequest

    terminating = asyncio.Event()

    async def terminate_session(self: Any, client_arg: object) -> None:
        terminating.set()
        await asyncio.Event().wait()

    async def open_and_close(client: Any) -> None:
        async with _chrys_streamable_http_client(
            "http://mcp.example/mcp", http_client=client, terminate_on_close=True
        ) as (read_stream, _write_stream, _get_session_id):
            await read_stream.receive()

    monkeypatch.setattr(http_transport_module, "_SESSION_TERMINATE_TIMEOUT_SECONDS", 0.01)
    request = JSONRPCRequest(jsonrpc="2.0", id=7, method="tools/list")
    with (
        _post_failure_transport(request, RuntimeError("post exploded"), session_id="sess-1") as client,
        patch.object(StreamableHTTPTransport, "terminate_session", new=terminate_session),
    ):
        await asyncio.wait_for(open_and_close(client), timeout=20.0)

    assert terminating.is_set()


async def test_streamable_http_post_failure_uses_readable_empty_exception_label() -> None:
    """The synthetic JSON-RPC error should not inherit blank transport messages."""
    from mcp.types import CONNECTION_CLOSED, JSONRPCError, JSONRPCRequest

    class ReadError(Exception):
        pass

    request = JSONRPCRequest(jsonrpc="2.0", id=8, method="tools/list")
    with _post_failure_transport(request, ReadError(TimeoutError())) as client:
        async with _chrys_streamable_http_client(
            "http://mcp.example/mcp",
            http_client=client,
            terminate_on_close=False,
        ) as (read_stream, _write_stream, _get_session_id):
            received = await asyncio.wait_for(read_stream.receive(), timeout=20.0)

    error = received.message.root
    assert isinstance(error, JSONRPCError)
    assert error.id == 8
    assert error.error.code == CONNECTION_CLOSED
    assert error.error.message == "HTTP MCP request failed: Read failed (ReadError)"


async def test_streamable_http_post_failure_for_non_request_reraises() -> None:
    """Only request messages get synthetic JSON-RPC errors; other POST failures still raise."""
    from mcp.types import JSONRPCNotification

    raised = asyncio.Event()
    seen: list[str] = []

    def record(exc: BaseException) -> None:
        seen.append(str(exc))
        raised.set()

    notification = JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized")
    with _post_failure_transport(notification, RuntimeError("notify failed"), on_error=record) as client:
        async with _chrys_streamable_http_client("http://mcp.example/mcp", http_client=client) as _transport:
            await asyncio.wait_for(raised.wait(), timeout=20.0)

    assert seen == ["notify failed"]


async def test_streamable_http_client_falls_back_when_private_hook_is_missing() -> None:
    """If the SDK private hook disappears, use the stock SDK client instead."""
    import mcp.client.streamable_http as streamable_http

    client = MagicMock()
    calls: list[tuple[str, object, bool]] = []

    class _TransportWithoutPrivateHook:
        pass

    @asynccontextmanager
    async def fallback_client(url: str, *, http_client: object = None, terminate_on_close: bool = True) -> Any:
        calls.append((url, http_client, terminate_on_close))
        yield "fallback-transport"

    with (
        patch.object(streamable_http, "StreamableHTTPTransport", _TransportWithoutPrivateHook),
        patch.object(streamable_http, "streamable_http_client", fallback_client),
    ):
        async with _chrys_streamable_http_client(
            "http://mcp.example/mcp",
            http_client=client,
            terminate_on_close=False,
        ) as transport:
            assert transport == "fallback-transport"

    assert calls == [("http://mcp.example/mcp", client, False)]


async def test_streamable_http_client_falls_back_when_session_message_moves() -> None:
    """If the SDK message wrapper moves, use the stock SDK client instead."""
    import builtins

    import mcp.client.streamable_http as streamable_http

    client = MagicMock()
    calls: list[tuple[str, object, bool]] = []

    @asynccontextmanager
    async def fallback_client(url: str, *, http_client: object = None, terminate_on_close: bool = True) -> Any:
        calls.append((url, http_client, terminate_on_close))
        yield "fallback-transport"

    with (
        patch.object(builtins, "__import__", side_effect=block_import("mcp.shared.message", "SessionMessage")),
        patch.object(streamable_http, "streamable_http_client", fallback_client),
    ):
        async with _chrys_streamable_http_client(
            "http://mcp.example/mcp",
            http_client=client,
            terminate_on_close=False,
        ) as transport:
            assert transport == "fallback-transport"

    assert calls == [("http://mcp.example/mcp", client, False)]


# ---------------------------------------------------------------------------
# _HTTPMCPTool — same-origin header scope
# ---------------------------------------------------------------------------


def _redirecting_transport() -> tuple[Any, list[tuple[str, dict[str, str]]]]:
    """An httpx transport that 302-redirects ``origin.test`` to ``foreign.test``.

    Returns the transport and the ``(url, headers)`` pairs it saw, in order.
    """
    import httpx

    seen: list[tuple[str, dict[str, str]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers)))
        if request.url.host == "origin.test":
            return httpx.Response(302, headers={"Location": "https://foreign.test/mcp"})
        return httpx.Response(200)

    return httpx.MockTransport(handler), seen


def _redirecting_mock_client() -> tuple[Any, list[tuple[str, dict[str, str]]]]:
    """A redirect-following httpx client on :func:`_redirecting_transport`."""
    import httpx

    transport, seen = _redirecting_transport()
    return httpx.AsyncClient(transport=transport, follow_redirects=True), seen


def test_url_origin_compares_scheme_host_and_effective_port() -> None:
    """Static headers ride only requests whose origin matches the configured URL."""
    from httpx import URL

    assert _url_origin(URL("http://h.example/a")) == _url_origin(URL("http://h.example:80/b"))
    assert _url_origin(URL("https://h.example/a")) == _url_origin(URL("https://h.example:443/b"))
    assert _url_origin(URL("http://h.example/a")) != _url_origin(URL("https://h.example/a"))
    assert _url_origin(URL("http://h.example/a")) != _url_origin(URL("http://h.example:8080/a"))
    assert _url_origin(URL("http://h.example/a")) != _url_origin(URL("http://other.example/a"))


async def test_built_client_strips_static_headers_when_redirects_are_explicitly_enabled() -> None:
    """The header hook still scopes secrets when a caller explicitly enables redirects."""
    import httpx

    transport, seen = _redirecting_transport()
    real_client = httpx.AsyncClient

    def _client_on_mock_transport(*, follow_redirects: bool, timeout: httpx.Timeout, verify: bool) -> httpx.AsyncClient:
        return real_client(transport=transport, follow_redirects=follow_redirects, timeout=timeout, verify=verify)

    tool = _HTTPMCPTool(name="h", url="https://origin.test/mcp", headers={"Authorization": "Bearer s", "X-Key": "k"})
    with (
        patch("httpx.AsyncClient", side_effect=_client_on_mock_transport),
        patch("chrys.service.mcp._http_transport._chrys_streamable_http_client", return_value=object()),
    ):
        tool.get_mcp_client()

    client = tool._owned_httpx_client
    assert client is not None
    try:
        response = await client.get("https://origin.test/mcp", follow_redirects=True)
    finally:
        await tool._close_owned_httpx_client()

    assert response.status_code == 200
    assert [url for url, _headers in seen] == ["https://origin.test/mcp", "https://foreign.test/mcp"]
    assert seen[0][1]["authorization"] == "Bearer s"
    assert seen[0][1]["x-key"] == "k"
    assert "authorization" not in seen[1][1]
    assert "x-key" not in seen[1][1]


# ---------------------------------------------------------------------------
# _HTTPMCPTool — transport options and custom httpx ctor path
# ---------------------------------------------------------------------------


class TestHTTPMCPToolTransport:
    """``_HTTPMCPTool`` pre-builds an ``httpx.AsyncClient`` when HTTP transport
    options require constructor-time settings.

    The pre-build path threads ``verify=verify_ssl`` into the constructor
    because httpx has no env-var equivalent for disabling cert verification.
    """

    HTTPX_CTOR = "httpx.AsyncClient"

    @staticmethod
    def _new_tool(
        *,
        verify_ssl: bool = True,
        bypass_proxy: bool = False,
        headers: dict[str, str] | None = None,
        request_timeout: float | None = None,
    ) -> _HTTPMCPTool:
        return _HTTPMCPTool(
            name="h",
            url="http://localhost:8080/mcp",
            verify_ssl=verify_ssl,
            bypass_proxy=bypass_proxy,
            headers=headers,
            request_timeout=request_timeout,
        )

    @staticmethod
    def _mock_client() -> MagicMock:
        return MagicMock(name="httpx-client", aclose=AsyncMock())

    async def test_ctor_failure_propagates(self) -> None:
        """If ``httpx.AsyncClient`` raises, the failure is propagated."""
        tool = self._new_tool(verify_ssl=False)

        with (
            patch(self.HTTPX_CTOR, side_effect=RuntimeError("build failed")),
            pytest.raises(RuntimeError, match="build failed"),
        ):
            await tool.__aenter__()

    async def test_super_aenter_failure_closes_owned_client(self) -> None:
        tool = self._new_tool(verify_ssl=False)

        client = self._mock_client()
        with (
            patch(self.HTTPX_CTOR, return_value=client),
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(side_effect=RuntimeError("connect fail"))),
            pytest.raises(RuntimeError, match="connect fail"),
        ):
            await tool.__aenter__()

        client.aclose.assert_awaited_once()
        assert tool._owned_httpx_client is None
        assert tool._httpx_client is None

    async def test_aexit_closes_owned_client(self) -> None:
        tool = self._new_tool(verify_ssl=False)
        client = self._mock_client()

        with (
            patch(self.HTTPX_CTOR, return_value=client),
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        with patch.object(MCPStreamableHTTPTool, "__aexit__", new=AsyncMock()):
            await tool.__aexit__(None, None, None)

        client.aclose.assert_awaited_once()
        assert tool._owned_httpx_client is None

    async def test_default_verify_and_no_env_skips_prebuild(self) -> None:
        """Default verify=True with no env → no pre-build, upstream defaults preserved."""
        tool = self._new_tool()
        with (
            patch(self.HTTPX_CTOR) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        ctor.assert_not_called()
        assert tool._httpx_client is None
        assert tool._owned_httpx_client is None

    async def test_verify_ssl_false_triggers_prebuild_with_verify_false(self) -> None:
        """``verify_ssl=False`` alone (no env) builds an httpx client with verify=False."""
        client = self._mock_client()
        tool = self._new_tool(verify_ssl=False)

        with (
            patch(self.HTTPX_CTOR, return_value=client) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        ctor.assert_called_once()
        assert ctor.call_args.kwargs["verify"] is False
        assert ctor.call_args.kwargs["follow_redirects"] is False
        assert tool._httpx_client is client
        assert tool._owned_httpx_client is client

    async def test_bypass_proxy_triggers_prebuild_with_no_proxy_mounts(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``bypass_proxy=True`` disables env-derived proxy transports without env patching."""
        client = self._mock_client()
        key = "NO_PROXY"
        monkeypatch.setenv(key, "original.example")
        seen: list[str | None] = []
        tool = self._new_tool(bypass_proxy=True)

        def _ctor(*_a: object, **_kw: object) -> MagicMock:
            seen.append(os.environ.get(key))
            return client

        with (
            patch(self.HTTPX_CTOR, side_effect=_ctor) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        ctor.assert_called_once()
        assert seen == ["original.example"]
        assert ctor.call_args.kwargs["verify"] is True
        assert ctor.call_args.kwargs["mounts"] == {
            "http://": None,
            "https://": None,
            "all://": None,
        }
        assert tool._httpx_client is client
        assert tool._owned_httpx_client is client

    async def test_bypass_proxy_reaches_origin_when_proxy_env_is_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clear_proxy_env,
        local_http_server,
    ) -> None:
        """The MCP-owned httpx client bypasses env proxies for real requests."""
        import httpx

        clear_proxy_env()

        async with (
            local_http_server(b"origin") as origin,
            local_http_server(b"proxy") as proxy,
        ):
            monkeypatch.setenv("HTTP_PROXY", proxy.url)

            control = httpx.AsyncClient(timeout=1)
            try:
                proxied = await control.get(f"{origin.url}/mcp")
            finally:
                await control.aclose()

            client = self._new_tool(bypass_proxy=True)._build_httpx_client()
            try:
                bypassed = await client.get(f"{origin.url}/mcp")
            finally:
                await client.aclose()

        assert proxied.text == "proxy"
        assert bypassed.text == "origin"
        assert proxy.hits and proxy.hits[0].startswith(f"GET {origin.url}/mcp ")
        assert origin.hits == ["GET /mcp HTTP/1.1"]

    async def test_verify_ssl_false_accepts_self_signed_https(
        self,
        monkeypatch: pytest.MonkeyPatch,
        clear_proxy_env,
        local_http_server,
        self_signed_server_ssl_context,
    ) -> None:
        """The MCP-owned httpx client threads verify=False into actual TLS handshakes."""
        import httpx

        clear_proxy_env()
        monkeypatch.setenv("NO_PROXY", "*")

        async with local_http_server(
            b"self-signed",
            scheme="https",
            ssl_context=self_signed_server_ssl_context,
        ) as server:
            verifying = self._new_tool(request_timeout=1)._build_httpx_client()
            try:
                with pytest.raises(httpx.ConnectError):
                    await verifying.get(f"{server.url}/mcp")
            finally:
                await verifying.aclose()

            client = self._new_tool(verify_ssl=False)._build_httpx_client()
            try:
                response = await client.get(f"{server.url}/mcp")
            finally:
                await client.aclose()

        assert response.text == "self-signed"
        assert server.hits == ["GET /mcp HTTP/1.1"]

    async def test_static_headers_trigger_prebuild_without_client_default_headers(self) -> None:
        """``headers`` alone (no env, default verify) must force a pre-build
        but must not pass ``headers=...`` into ``httpx.AsyncClient`` because
        httpx propagates client-level headers across cross-origin redirects.
        """
        client = self._mock_client()
        tool = self._new_tool(headers={"Authorization": "Bearer token", "X-Custom": "v"})

        with (
            patch(self.HTTPX_CTOR, return_value=client) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        ctor.assert_called_once()
        assert "headers" not in ctor.call_args.kwargs
        assert ctor.call_args.kwargs["verify"] is True
        assert tool._httpx_client is client
        assert tool._owned_httpx_client is client

    async def test_no_headers_omits_headers_kwarg(self) -> None:
        """When headers are empty, ``headers=`` must not be passed to
        ``httpx.AsyncClient`` — leaving httpx's own defaults in force."""
        client = self._mock_client()
        tool = self._new_tool(verify_ssl=False)  # force prebuild without headers

        with (
            patch(self.HTTPX_CTOR, return_value=client) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        assert "headers" not in ctor.call_args.kwargs

    async def test_request_timeout_triggers_prebuild_and_bounds_httpx_handshake(self) -> None:
        """A user request timeout should bound httpx connect/write/pool too."""
        from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT

        client = self._mock_client()
        tool = self._new_tool(request_timeout=1)

        with (
            patch(self.HTTPX_CTOR, return_value=client) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        timeout = ctor.call_args.kwargs["timeout"]
        assert timeout.connect == 1.0
        assert timeout.write == 1.0
        assert timeout.pool == 1.0
        assert timeout.read == MCP_DEFAULT_SSE_READ_TIMEOUT

    async def test_close_clears_inject_headers_sentinel(self) -> None:
        """Closing an owned client must let the next client reattach headers."""
        tool = self._new_tool(verify_ssl=False)
        client = self._mock_client()

        with (
            patch(self.HTTPX_CTOR, return_value=client),
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        async def _hook(_request: object) -> None: ...

        # Simulate request-hook installation during get_mcp_client.
        tool._inject_headers_hook = _hook

        with patch.object(MCPStreamableHTTPTool, "__aexit__", new=AsyncMock()):
            await tool.__aexit__(None, None, None)

        # Sentinel cleared so a hook installs on the next client.
        assert tool._inject_headers_hook is None
        assert tool._owned_httpx_client is None
        assert tool._httpx_client is None

    async def test_get_mcp_client_attaches_header_hook_once(self) -> None:
        """Repeated transport builds reuse the owned client and its one header hook."""
        from httpx import Request

        sentinel = object()
        client = MagicMock()
        client.event_hooks = {"request": []}
        tool = _HTTPMCPTool(name="h", url="http://localhost:8080/mcp", headers={"Authorization": "Bearer static"})

        with (
            patch(self.HTTPX_CTOR, return_value=client) as ctor,
            patch(
                "chrys.service.mcp._http_transport._chrys_streamable_http_client", return_value=sentinel
            ) as stream_client,
        ):
            first = tool.get_mcp_client()
            second = tool.get_mcp_client()

        assert first is sentinel
        assert second is sentinel
        ctor.assert_called_once()
        assert tool._httpx_client is client
        assert len(client.event_hooks["request"]) == 1
        stream_client.assert_called_with(
            url="http://localhost:8080/mcp",
            http_client=client,
            terminate_on_close=True,
            request_timeout=None,
        )

        request = Request("POST", "http://localhost:8080/mcp")
        await client.event_hooks["request"][0](request)

        assert request.headers["Authorization"] == "Bearer static"

    async def test_get_mcp_client_headers_are_same_origin_only(self) -> None:
        """Static per-server headers must not leak onto cross-origin requests."""
        from httpx import Request

        client = MagicMock()
        client.event_hooks = {"request": []}
        tool = _HTTPMCPTool(
            name="h",
            url="http://localhost:8080/mcp",
            http_client=client,
            headers={"Authorization": "Bearer static", "X-Server": "srv"},
        )

        with (
            patch(self.HTTPX_CTOR) as ctor,
            patch("chrys.service.mcp._http_transport._chrys_streamable_http_client", return_value=object()),
        ):
            tool.get_mcp_client()

        ctor.assert_not_called()
        assert len(client.event_hooks["request"]) == 1

        same_origin = Request("POST", "http://localhost:8080/mcp")
        cross_origin = Request("POST", "http://localhost:9090/mcp")
        await client.event_hooks["request"][0](same_origin)
        await client.event_hooks["request"][0](cross_origin)

        assert same_origin.headers["Authorization"] == "Bearer static"
        assert same_origin.headers["X-Server"] == "srv"
        assert "Authorization" not in cross_origin.headers
        assert "X-Server" not in cross_origin.headers

    async def test_headers_are_stripped_from_actual_cross_origin_redirect(self) -> None:
        """httpx may carry custom request headers across redirects; the hook strips the configured keys."""
        client, seen = _redirecting_mock_client()
        tool = _HTTPMCPTool(
            name="h",
            url="https://origin.test/mcp",
            http_client=client,
            headers={"X-Secret": "static", "X-Server": "srv"},
        )
        tool._ensure_header_hook(client)

        try:
            await client.get("https://origin.test/mcp")
        finally:
            await client.aclose()

        assert seen[0][1]["x-secret"] == "static"
        assert seen[0][1]["x-server"] == "srv"
        assert "x-secret" not in seen[1][1]
        assert "x-server" not in seen[1][1]

    def test_get_mcp_client_uses_custom_transport_without_headers(self) -> None:
        """No static headers: pass the current client through without creating a new one or adding a hook."""
        existing = MagicMock()
        sentinel = object()
        tool = _HTTPMCPTool(
            name="h",
            url="http://localhost:8080/mcp",
            http_client=existing,
            terminate_on_close=False,
        )

        with (
            patch(self.HTTPX_CTOR) as ctor,
            patch(
                "chrys.service.mcp._http_transport._chrys_streamable_http_client", return_value=sentinel
            ) as stream_client,
        ):
            result = tool.get_mcp_client()

        assert result is sentinel
        ctor.assert_not_called()
        assert tool._inject_headers_hook is None
        stream_client.assert_called_once_with(
            url="http://localhost:8080/mcp",
            http_client=existing,
            terminate_on_close=False,
            request_timeout=None,
        )

    def test_build_httpx_client_falls_back_to_literal_timeouts_when_mcp_utils_change(self) -> None:
        """If ``mcp.shared._httpx_utils`` loses its default constants, fall back
        to the historical literals (30.0s connect / 300.0s SSE read) instead of
        crashing every prebuilt HTTP client."""
        captured: dict[str, Any] = {}

        class _CapturingClient:
            def __init__(self, **kwargs: Any) -> None:
                captured.update(kwargs)

        tool = self._new_tool(verify_ssl=False)
        with (
            patch.object(
                builtins,
                "__import__",
                side_effect=block_import("mcp.shared._httpx_utils", "MCP_DEFAULT_TIMEOUT"),
            ),
            patch(self.HTTPX_CTOR, _CapturingClient),
        ):
            tool._build_httpx_client()

        timeout = captured["timeout"]
        assert timeout.connect == 30.0
        assert timeout.read == 300.0

    async def test_caller_provided_http_client_is_not_overridden(self) -> None:
        """A caller-supplied ``http_client`` wins over TLS and proxy settings."""
        existing = MagicMock(aclose=AsyncMock())
        tool = _HTTPMCPTool(
            name="h",
            url="http://localhost:8080/mcp",
            verify_ssl=False,
            bypass_proxy=True,
            http_client=existing,
        )

        with (
            patch(self.HTTPX_CTOR) as ctor,
            patch.object(MCPStreamableHTTPTool, "__aenter__", new=AsyncMock(return_value=tool)),
        ):
            await tool.__aenter__()

        ctor.assert_not_called()
        assert tool._httpx_client is existing
        assert tool._owned_httpx_client is None


# ---------------------------------------------------------------------------
# MCPAdapter HTTP connect failures against real loopback sockets
# ---------------------------------------------------------------------------


async def test_http_connect_to_stopped_server_fails_without_hanging() -> None:
    """Connect to a closed local port must surface as a failure, not a hang.

    The wait_for bound is intentionally generous: Windows' IOCP backend does
    not abort an in-flight connect as crisply as Linux on cancellation, so
    closed-port flows can take a couple of seconds longer than the configured
    ``request_timeout`` to fully unwind.  Linux/macOS fail in well under a
    second; the bound just keeps Windows from flaking.  The semantic check is
    "doesn't hang for many seconds" — exact timing is covered by
    ``test_http_connect_to_silent_server_fails_without_hanging``.
    """
    adapter = MCPAdapter()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

        config = MCPServerConfig(
            name="dead-http",
            transport="http",
            url=f"http://127.0.0.1:{port}/mcp",
            request_timeout=1,
        )

        # Keep the port reserved but not listening, so the connection fails
        # while no concurrent worker can claim it. Whether it is refused or
        # its SYN is dropped depends on the OS (macOS drops it).
        tools = await asyncio.wait_for(adapter.connect_all([config]), timeout=10)

    assert tools == []
    assert "dead-http" in adapter.failures


async def test_http_connect_to_silent_server_fails_without_hanging() -> None:
    """Silent server must surface as a normalized timeout within ``request_timeout``.

    Unlike the closed-port case (which depends on OS connect-refused timing),
    this path goes through our explicit ``Timeout(post_timeout)`` on the
    streamed POST, so the timing is fully under our control.  We assert
    elapsed time stays under a small ceiling so a regression where the POST
    timeout silently widens (e.g. losing the per-request timeout override
    and falling back to the SSE read default) fails the test instead of
    passing under a generous ``wait_for`` bound.
    """
    adapter = MCPAdapter()
    stop = asyncio.Event()
    writers: list[asyncio.StreamWriter] = []

    async def handle_connection(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writers.append(writer)
        await stop.wait()

    server = await asyncio.start_server(handle_connection, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        config = MCPServerConfig(
            name="silent-http",
            transport="http",
            url=f"http://127.0.0.1:{port}/mcp",
            bypass_proxy=True,
            request_timeout=1,
        )

        with earlier_tests_heap_frozen():
            start = time.monotonic()
            tools = await asyncio.wait_for(adapter.connect_all([config]), timeout=10)
            elapsed = time.monotonic() - start

        assert tools == []
        assert "silent-http" in adapter.failures
        err = adapter.failures["silent-http"]
        assert isinstance(err.cause, TimeoutError)
        assert "connection did not complete within 1s" in str(err.cause)
        # request_timeout=1 + cleanup overhead; even on slow Windows CI this
        # should land well under 5s when the explicit POST timeout fires.
        assert elapsed < 5.0, f"silent-server connect took {elapsed:.2f}s, expected < 5s"
    finally:
        stop.set()
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=0.5)
        for writer in writers:
            writer.close()
        for writer in writers:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
