# Copyright (c) 2024 Anthropic, PBC
# Copyright (c) Microsoft. All rights reserved.
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Contains code adapted from the Model Context Protocol Python SDK and Microsoft Agent Framework (MIT License; see NOTICE).

"""Streamable HTTP MCP transport."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any

from chrys.foundation.errors import clean_error_message
from chrys.foundation.util.env_templates import EnvVarResolutionError, command_handles_in
from chrys.foundation.util.header_charset import header_value_charset_error
from chrys.foundation.util.httpx_helpers import BYPASS_PROXY_MOUNTS
from chrys.service.mcp._tool_mixins import _NoPrePagePingMixin, _StructuredContentFallbackMixin
from chrys.service.mcp.owned import LOCAL_HTTP_FAILURE_ERROR_DATA, MCPStreamableHTTPTool

logger = logging.getLogger(__name__)

_HTTP_HEADERS_OVERRIDE: ContextVar[dict[str, str] | None] = ContextVar("mcp_http_headers_override", default=None)


def _url_origin(url: Any) -> tuple[str, str, int | None]:
    port = url.port
    if port is None:
        port = 443 if url.scheme == "https" else 80 if url.scheme == "http" else None
    return (url.scheme, url.host or "", port)


@asynccontextmanager
async def _chrys_streamable_http_client(
    url: str,
    *,
    http_client: Any = None,
    terminate_on_close: bool = True,
    request_timeout: float | None = None,
) -> Any:
    """Streamable HTTP client that wakes pending requests on POST failures.

    Upstream ``StreamableHTTPTransport`` logs exceptions raised by POST
    request tasks, but the pending ``ClientSession.send_request`` can remain
    blocked waiting for a JSON-RPC response.  Convert request-scoped HTTP
    failures into JSON-RPC errors so initialization/list-tools fail through
    the normal MCP error path instead of hanging the agent build.

    Compatibility note: this is intentionally a narrow patch over MCP SDK
    1.30.0's ``StreamableHTTPTransport._handle_post_request`` private hook.
    If those private symbols move, fall back to the stock SDK client so a
    dependency update degrades to upstream behavior instead of breaking all
    HTTP MCP connections.
    """
    try:
        import anyio
        from mcp.client.streamable_http import StreamableHTTPTransport, _raise_for_unfollowed_redirect
        from mcp.shared._httpx_utils import create_mcp_http_client, stream_within_origin
        from mcp.shared.message import SessionMessage
        from mcp.types import CONNECTION_CLOSED, ErrorData, JSONRPCError, JSONRPCMessage, JSONRPCRequest
    except ImportError:
        logger.warning(
            "HTTP MCP POST-failure wakeup patch is disabled because MCP SDK internals changed.",
            exc_info=True,
        )
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(
            url,
            http_client=http_client,
            terminate_on_close=terminate_on_close,
        ) as transport:
            yield transport
        return

    if not hasattr(StreamableHTTPTransport, "_handle_post_request"):
        logger.warning("HTTP MCP POST-failure wakeup patch is disabled because MCP SDK internals changed.")
        from mcp.client.streamable_http import streamable_http_client

        async with streamable_http_client(
            url,
            http_client=http_client,
            terminate_on_close=terminate_on_close,
        ) as transport:
            yield transport
        return

    class _ChrysStreamableHTTPTransport(StreamableHTTPTransport):
        def __init__(self, url: str, *, request_timeout: float | None = None) -> None:
            super().__init__(url)
            self._request_timeout = request_timeout

        @property
        def _post_timeout(self) -> float | None:
            if self._request_timeout is None:
                return None
            margin = min(0.25, self._request_timeout / 4)
            return max(0.001, self._request_timeout - margin)

        async def _handle_post_request(self, ctx: Any) -> None:
            from httpx import Timeout, TimeoutException, codes
            from mcp.client.streamable_http import CONTENT_TYPE, JSON, SSE

            headers = self._prepare_headers()
            message = ctx.session_message.message
            is_initialization = self._is_initialization_request(message)
            stream_kwargs: dict[str, Any] = {
                "json": message.model_dump(by_alias=True, mode="json", exclude_none=True),
                "headers": headers,
            }
            post_timeout = self._post_timeout
            if post_timeout is not None:
                stream_kwargs["timeout"] = Timeout(post_timeout)

            try:
                async with stream_within_origin(ctx.client, "POST", self.url, **stream_kwargs) as response:
                    if response.status_code == 202:
                        return

                    if response.status_code == 404:
                        if isinstance(message.root, JSONRPCRequest):
                            await self._send_session_terminated_error(ctx.read_stream_writer, message.root.id)
                        return

                    _raise_for_unfollowed_redirect(response)
                    response.raise_for_status()
                    if is_initialization:
                        self._maybe_extract_session_id_from_response(response)

                    if isinstance(message.root, JSONRPCRequest):
                        content_type = response.headers.get(CONTENT_TYPE, "").lower()
                        if content_type.startswith(JSON):
                            await self._handle_json_response(response, ctx.read_stream_writer, is_initialization)
                        elif content_type.startswith(SSE):
                            await self._handle_sse_response(response, ctx, is_initialization)
                        else:
                            await self._handle_unexpected_content_type(content_type, ctx.read_stream_writer)
            except Exception as exc:
                root = getattr(message, "root", None)
                if isinstance(root, JSONRPCRequest):
                    detail = clean_error_message(exc)
                    code = codes.REQUEST_TIMEOUT if isinstance(exc, TimeoutException) else CONNECTION_CLOSED
                    error = JSONRPCError(
                        jsonrpc="2.0",
                        id=root.id,
                        error=ErrorData(
                            code=code,
                            message=f"HTTP MCP request failed: {detail}",
                            data=LOCAL_HTTP_FAILURE_ERROR_DATA,
                        ),
                    )
                    await ctx.read_stream_writer.send(SessionMessage(message=JSONRPCMessage(error)))
                    return
                raise

    read_stream_writer, read_stream = anyio.create_memory_object_stream[Any](0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream[Any](0)

    client_provided = http_client is not None
    client = http_client or create_mcp_http_client()
    transport = _ChrysStreamableHTTPTransport(url, request_timeout=request_timeout)

    async with anyio.create_task_group() as tg:
        try:
            async with contextlib.AsyncExitStack() as stack:
                if not client_provided:
                    await stack.enter_async_context(client)

                def start_get_stream() -> None:
                    tg.start_soon(transport.handle_get_stream, client, read_stream_writer)

                tg.start_soon(
                    transport.post_writer,
                    client,
                    write_stream_reader,
                    read_stream_writer,
                    write_stream,
                    start_get_stream,
                    tg,
                )

                try:
                    yield read_stream, write_stream, transport.get_session_id
                finally:
                    if transport.session_id and terminate_on_close:
                        await transport.terminate_session(client)
                    tg.cancel_scope.cancel()
        finally:
            await read_stream_writer.aclose()
            await write_stream.aclose()


class _HTTPMCPTool(_NoPrePagePingMixin, _StructuredContentFallbackMixin, MCPStreamableHTTPTool):
    """MCPStreamableHTTPTool with TLS/proxy options and static headers.

    Four profile knobs:

    * ``verify_ssl=False`` — disables TLS cert validation. Insecure;
      local/self-signed dev only.
    * ``bypass_proxy=True`` — disables environment-derived proxy transports
      for this server.
    * ``headers`` — static per-server headers (typically auth tokens)
      attached by a same-origin request hook, so they reach **every**
      request including ``initialize`` and ``list_tools`` without leaking
      across cross-origin redirects.
    * ``resolve_commands`` — ``headers`` were resolved from a profile, so a
      ``{{!command}}`` handle in them stands for a token.  Headers an ACP
      client supplied are literal: they get no token and run no command.

    When any knob is non-default we pre-build the ``httpx.AsyncClient``
    and hand it to the parent via ``self._httpx_client``.

    When all knobs are default and no ``http_client`` is supplied, we
    skip the pre-build entirely so upstream defaults stay in force.
    """

    def __init__(
        self,
        *args: Any,
        verify_ssl: bool = True,
        bypass_proxy: bool = False,
        headers: dict[str, str] | None = None,
        resolve_commands: bool = False,
        **kwargs: Any,
    ) -> None:
        self._verify_ssl: bool = verify_ssl
        self._bypass_proxy: bool = bypass_proxy
        self._static_headers: dict[str, str] = dict(headers) if headers else {}
        self._headers_hold_commands: bool = resolve_commands and bool(command_handles_in(self._static_headers.values()))
        self._owned_httpx_client: Any = None
        self._inject_headers_hook: Callable[[Any], Awaitable[None]] | None = None
        super().__init__(*args, **kwargs)

    async def __aenter__(self) -> Any:
        if self._headers_hold_commands:
            from chrys.service.command_credentials import ensure_fresh

            # The transport quotes a value it refuses in its error, which here would hold a token.
            for name, value in self._static_headers.items():
                problem = header_value_charset_error(name, value)
                if problem:
                    raise EnvVarResolutionError(problem)
            # Before the connection starts counting: a command's time isn't the server's.
            await ensure_fresh(self._static_headers.values())
        if self._needs_prebuild() and self._httpx_client is None:
            client = self._build_httpx_client()
            self._owned_httpx_client = client
            self._httpx_client = client
        try:
            return await super().__aenter__()
        except BaseException:
            await self._close_owned_httpx_client()
            raise

    def get_mcp_client(self) -> Any:
        """Build Streamable HTTP transport with request-failure wakeups."""
        http_client = self._httpx_client
        if http_client is None and self._needs_prebuild():
            http_client = self._build_httpx_client()
            self._owned_httpx_client = http_client
            self._httpx_client = http_client
        if http_client is not None and self._static_headers:
            self._ensure_header_hook(http_client)

        return _chrys_streamable_http_client(
            url=self.url,
            http_client=http_client,
            terminate_on_close=self.terminate_on_close if self.terminate_on_close is not None else True,
            request_timeout=float(self.request_timeout) if self.request_timeout is not None else None,
        )

    def _ensure_header_hook(self, http_client: Any) -> None:
        if self._inject_headers_hook is not None:
            return

        from httpx import URL

        from chrys.service.command_credentials import substitute

        target_origin = _url_origin(URL(self.url))

        async def _inject_headers(request: Any) -> None:
            # httpx carries request headers over to a redirect target: a
            # cross-origin hop must not receive the per-server secrets.
            if _url_origin(request.url) != target_origin:
                for key in self._static_headers:
                    request.headers.pop(key, None)
                return
            for key, value in self._static_headers.items():
                request.headers[key] = substitute(value) if self._headers_hold_commands else value

        self._inject_headers_hook = _inject_headers
        http_client.event_hooks["request"].append(self._inject_headers_hook)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await super().__aexit__(exc_type, exc_value, traceback)
        finally:
            await self._close_owned_httpx_client()

    def _needs_prebuild(self) -> bool:
        return (
            not self._verify_ssl or self._bypass_proxy or bool(self._static_headers) or self.request_timeout is not None
        )

    def _build_httpx_client(self) -> Any:
        """Construct an ``httpx.AsyncClient`` mirroring MCP defaults.

        We construct ``httpx.AsyncClient`` directly (rather than via
        ``mcp.shared._httpx_utils.create_mcp_http_client``) because we
        need to thread ``verify=`` through, which the factory does not
        accept.  Timeout constants are pulled from the same module so we
        stay aligned with upstream defaults; if that
        private module changes shape, we fall back to the historical
        literals (30.0s connect / 300.0s SSE read) instead of crashing.

        When ``request_timeout`` is set we narrow connect/write/pool to
        it so a dead HTTP server fails inside the user's bound instead
        of waiting out the 30s httpx default — Linux returns ECONNREFUSED
        instantly so this only bites on Windows, but the unbounded
        connect violates the user's intent on every platform.  SSE read
        stays at ``MCP_DEFAULT_SSE_READ_TIMEOUT`` so long-lived streams
        aren't capped by the per-request bound.
        """
        from httpx import AsyncClient, Timeout

        try:
            from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
        except ImportError:
            logger.warning(
                "MCP default timeout constants are unavailable (mcp.shared._httpx_utils changed); "
                "using literal fallbacks (30.0s connect / 300.0s SSE read).",
                exc_info=True,
            )
            MCP_DEFAULT_TIMEOUT = 30.0
            MCP_DEFAULT_SSE_READ_TIMEOUT = 300.0

        connect_timeout = float(self.request_timeout) if self.request_timeout is not None else MCP_DEFAULT_TIMEOUT
        kwargs: dict[str, Any] = {
            "follow_redirects": False,
            "timeout": Timeout(connect_timeout, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            "verify": self._verify_ssl,
        }
        if self._bypass_proxy:
            kwargs["mounts"] = dict(BYPASS_PROXY_MOUNTS)
        if self._headers_hold_commands:
            from chrys.service.command_credentials import CommandCredentialAuth

            kwargs["auth"] = CommandCredentialAuth(self._static_headers.values())
        return AsyncClient(**kwargs)

    async def _close_owned_httpx_client(self) -> None:
        client = self._owned_httpx_client
        if client is None:
            return
        self._owned_httpx_client = None
        # Drop MCPStreamableHTTPTool's reference too so a second __aenter__ rebuilds
        # under fresh env (and doesn't reuse a closed client).
        if self._httpx_client is client:
            self._httpx_client = None
        # Header injection is attached once per client.  Clear the sentinel so
        # the hook re-attaches to the next owned client on re-enter.
        self._inject_headers_hook = None
        with contextlib.suppress(Exception):
            await client.aclose()
