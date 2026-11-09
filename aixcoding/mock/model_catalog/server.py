# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local mock server for the server-owned model catalog (dev tool, never shipped).

Stands in for whatever backend will eventually publish the catalog, so the
client's sync path — fetch, validate, replace wholesale, skip on unchanged
version — can be exercised locally.

The catalog is served on the reference server's dispatch path. The client
appends this path to the base URL from ``aixcoding.toml`` or the deployment
tier; a full endpoint override must include the path explicitly.

One port serves both backends the client needs before it can sync: the catalog
on the reference dispatch path, and the device-code login flow on the auth
mock's paths (``/api/v1/...``, ``/device/...``), which is why this listens on
7777 — the port ``AIXCODING_ENVIRONMENT=local`` already names. ``--no-auth``
serves the catalog alone.

Usage::

    uv run python aixcoding/mock/model_catalog/server.py [--port 7777]
                  [--catalog FILE] [--mode {ok,empty,invalid,error,slow,stale}]
                  [--no-auth] [--auth-mode {auto,manual}]

    AIXCODING_ENVIRONMENT=local uv run aixcoding-cli

Endpoints::

    GET  /llm/api/v1/continue-config/dispatch   catalog payload for the current mode
    POST /mock/control   {"mode": ..., "delay": N}
    POST /api/v1/auth/device/code, /api/v1/auth/device/token, /api/v1/user/info
    GET  /device/verify  (login mock, unless --no-auth)
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# Run as a script, ``sys.path[0]`` is this file's own directory, so the repo
# root — the only place ``aixcoding`` is importable from — is missing. The
# auth mock is self-contained and never hits this; pytest has the root already.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from aixcoding.mock.auth.server import (
    API_PREFIX as AUTH_API_PREFIX,
)
from aixcoding.mock.auth.server import (
    VERIFY_ROUTE as AUTH_VERIFY_ROUTE,
)
from aixcoding.mock.auth.server import (
    MockAuthConfig,
    MockAuthState,
)
from aixcoding.mock.auth.server import (
    build_handler_class as build_auth_handler_class,
)
from aixcoding.mock.model_catalog.modes import (
    DEFAULT_DELAY,
    MODES,
    build_payload,
    delay_for,
    status_for,
)
from aixcoding.mock.model_catalog.source import CatalogSource, CatalogSourceError
from aixcoding.mock.model_catalog.state import (
    CatalogMockState,
    UnknownModeError,
)

logger = logging.getLogger("chrys.mock_model_catalog")

#: Dispatch path appended to a configured base; full endpoint overrides include it explicitly.
CATALOG_ROUTE = "/llm/api/v1/continue-config/dispatch"
CONTROL_ROUTE = "/mock/control"
#: The OpenAI-style endpoint a synced profile's ``apiBase`` points at: with
#: ``apiBase = http://127.0.0.1:7777/v1`` the SDK POSTs here, so a local run's
#: model requests land on this mock instead of a real gateway.
LLM_ROUTE = "/v1/chat/completions"

DEFAULT_HOST = "127.0.0.1"
#: The auth mock's port, because this is one server now: the client logs in
#: before it can sync a catalog, so both mock backends answer on it. The client's
#: ``AIXCODING_ENVIRONMENT=local`` already names :7777, which is why the mock
#: moved to it rather than the client.
DEFAULT_PORT = 7777

# Loopback only, per the aixcoding/mock/ conventions; ``create_server`` rejects
# anything else at startup.
_LOOPBACK_HOSTS = ("127.0.0.1", "::1")

#: Paths owned by the auth mock (its API prefix and its verify page); every
#: other path is this mock's own routing.
AUTH_ROUTE_PREFIXES = (AUTH_API_PREFIX, "/device")

MAX_CONTROL_BYTES = 64 * 1024

#: A chat request carries the whole message history, so its body dwarfs a
#: control one; 16 MiB sits far above any real request while staying bounded.
MAX_LLM_BODY_BYTES = 16 * 1024 * 1024

#: Headers whose value is mostly hidden in the log: a catalog request carries
#: the caller's token, and an access log is not a credential store.
_MASKED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
)

#: A credential arrives under many names besides ``Authorization`` —
#: ``X-Access-Token``, ``X-Api-Key``, ``X-Session-Token`` and whatever the next
#: gateway invents — so a name is also matched by what it contains. Anything
#: that slips past both would be logged in the clear, which is the one thing
#: this function must never do.
_CREDENTIAL_NAME_PARTS = ("auth", "token", "key", "secret", "credential", "password", "cookie", "signature")


def _is_credential_header(name: str) -> bool:
    """Return whether *name* names a credential, by exact match or by content."""
    lowered = name.lower()
    return lowered in _MASKED_HEADERS or any(part in lowered for part in _CREDENTIAL_NAME_PARTS)


#: How much of a credential both ends keep, and the length below which none of
#: it is shown: hiding only the middle of a short secret leaves little enough
#: that the ends give it away.
_MASK_HEAD = 8
_MASK_TAIL = 4
_MASK_MIN_LENGTH = 16


def _mask_value(value: str) -> str:
    """Hide the middle of *value*, keeping both ends to tell keys apart."""
    if len(value) <= _MASK_MIN_LENGTH:
        return "<redacted>"
    return f"{value[:_MASK_HEAD]}…{value[-_MASK_TAIL:]}"


def _format_auth_headers(headers: Any) -> str:
    """Render the request's credential headers, or note that it sent none.

    Only credentials are logged: what the line is for is *whether* the caller
    authenticated, not the dozen browser headers that ride along with it. The
    value stays mostly hidden — enough to tell which key was sent, not enough
    to put the secret in a log file.
    """
    lines: list[str] = []
    for name, value in headers.items():
        if _is_credential_header(name):
            lines.append(f"    {name}: {_mask_value(value)} (len={len(value)})")
    return "\n".join(lines) or "    (no credential header)"


#: What the mock answers with — generic enough to be safe in any context.
LLM_MOCK_REPLY = "Acknowledged by the chrys model-catalog mock LLM."


def _llm_completion_body(model: str) -> dict[str, Any]:
    """One minimal, valid ChatCompletion the OpenAI SDK accepts."""
    return {
        "id": "chatcmpl-chrys-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": LLM_MOCK_REPLY},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _llm_chunk(model: str, *, delta: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    """One ``chat.completion.chunk`` event for the streaming wire."""
    return {
        "id": "chatcmpl-chrys-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


class RunningCatalogMock:
    """A started mock: origin, live state and an idempotent close."""

    def __init__(
        self,
        *,
        origin: str,
        state: CatalogMockState,
        source: CatalogSource,
        server: ThreadingHTTPServer,
    ) -> None:
        self.origin = origin
        self.state = state
        self.source = source
        self._server = server
        self._closed = False

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.shutdown()
        self._server.server_close()


class CatalogMockServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the mock's state and source.

    Binds without the Windows port-hijack hole: ``HTTPServer``'s
    ``allow_reuse_address`` (SO_REUSEADDR) lets a second instance bind a port
    the first one still holds on Windows — the stale-mock-process case the
    fail-fast promise must catch. Windows binds with SO_EXCLUSIVEADDRUSE
    instead; other platforms keep SO_REUSEADDR (a Linux restart needs it to
    slip past TIME_WAIT).
    """

    daemon_threads = True
    allow_reuse_address = 0

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        super().server_bind()

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        state: CatalogMockState,
        source: CatalogSource,
        quiet: bool = False,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.state = state
        self.source = source
        self.quiet = quiet


def build_handler_class() -> type[BaseHTTPRequestHandler]:
    """Build the request handler bound to ``self.server``'s state and source."""

    class CatalogMockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ChrysModelCatalogMock/1.0"

        def do_GET(self) -> None:
            self._dispatch("GET")

        def do_POST(self) -> None:
            self._dispatch("POST")

        def log_message(self, format: str, *args: Any) -> None:
            """Route the stdlib access log through ``logging``."""
            logger.debug("mock_model_catalog %s", format % args)

        def _request_url(self) -> str:
            """Absolute URL of this request, so a log line stands alone.

            ``self.path`` is the request line's target — a path, and only a full
            URL when a proxy is involved — so the authority comes from the
            ``Host`` header, or from the bound address when it is absent.
            """
            host = self.headers.get("Host")
            if not host:
                address, port = self.server.server_address[:2]  # type: ignore[attr-defined]
                host = f"[{address}]:{port}" if ":" in str(address) else f"{address}:{port}"
            return f"http://{host}{self.path}"

        def _drain_body(self) -> None:
            """Consume an unread request body (keep-alive hygiene).

            Rejections leave before reading the body; on the HTTP/1.1
            keep-alive connection the leftover bytes would then be parsed as
            the next request line and desync the client.
            """
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return
            if length > 0:
                self.rfile.read(length)

        def _dispatch(self, method: str) -> None:
            server: CatalogMockServer = self.server  # type: ignore[assignment]
            server.state.requests += 1
            route = urlsplit(self.path).path.rstrip("/") or "/"
            if not server.quiet:
                logger.info("%s %s\n%s", method, self._request_url(), _format_auth_headers(self.headers))

            if route == CATALOG_ROUTE:
                if method != "GET":
                    self._drain_body()
                    self._send_json(405, {"error": f"{method} not allowed on {route}"})
                    return
                self._serve_catalog()
            elif route == LLM_ROUTE:
                if method != "POST":
                    self._drain_body()
                    self._send_json(405, {"error": f"{method} not allowed on {route}"})
                    return
                self._serve_llm_chat()
            elif route == CONTROL_ROUTE:
                if method != "POST":
                    self._drain_body()
                    self._send_json(405, {"error": f"{method} not allowed on {route}"})
                    return
                self._serve_control()
            else:
                self._drain_body()
                self._send_json(404, {"error": f"no mock route for {route}"})

        def _serve_catalog(self) -> None:
            server: CatalogMockServer = self.server  # type: ignore[assignment]
            state = server.state
            auth_state = getattr(server, "auth_state", None)
            if auth_state is not None:
                # catalog-auth-gating: the catalog is an authenticated endpoint.
                # Missing/unknown/revoked tokens get HTTP 401 (the client-side
                # raise_for_status path), judged against the same grant records
                # user/info uses — iterated via the grants() snapshot, never
                # the unsynchronised _grants dict. --no-auth exempts this check.
                # Tokens never expire with their grant (PROD parity, 2026-11-04).
                token = self.headers.get("token", "").strip()
                granted = bool(token) and any(
                    grant.token == token and grant.status == "granted" for grant in auth_state.grants()
                )
                if not granted:
                    self._send_json(401, {"error": "missing or invalid catalog credential"})
                    return
            delay = delay_for(state.mode, state.delay)
            if delay:
                time.sleep(delay)

            try:
                payload = server.source.load()
            except CatalogSourceError as exc:
                self._send_json(500, {"error": str(exc), "mode": state.mode})
                return

            self._send_json(
                status_for(state.mode),
                build_payload(state.mode, payload=payload, requests=state.requests),
            )

        def _serve_control(self) -> None:
            server: CatalogMockServer = self.server  # type: ignore[assignment]
            body = self._read_json_body()
            if body is None:
                self._send_json(400, {"error": "control body must be a JSON object"})
                return
            mode = body.get("mode")
            if mode is None:
                self._send_json(400, {"error": f"'mode' is required; one of {', '.join(MODES)}"})
                return
            # Validate the delay before touching state so a bad value cannot
            # leave a half-applied mode switch behind.
            delay = body.get("delay")
            if delay is not None:
                try:
                    delay = float(delay)
                except ValueError as exc:
                    self._send_json(400, {"error": f"invalid delay {body.get('delay')!r}: {exc}"})
                    return
            try:
                server.state.set_mode(str(mode), delay)
            except UnknownModeError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            logger.info("mode -> %s (revision %d)", server.state.mode, server.state.revision)
            # The snapshot is the control response: no separate state endpoint.
            self._send_json(200, server.state.snapshot())

        def _serve_llm_chat(self) -> None:
            """Answer like OpenAI.

            No extra logging here: ``_dispatch`` already logs every request
            line together with its credential headers — ``token``,
            ``Authorization``, anything auth-shaped, values masked — which is
            all the mock is watched for. The payload is not logged.
            """
            body = self._read_llm_body()
            if body is None:
                self._send_json(400, {"error": "chat request body must be a JSON object"})
                return
            model = str(body.get("model") or "mock-model")
            if body.get("stream") is True:
                self._send_llm_stream(model)
            else:
                self._send_json(200, _llm_completion_body(model))

        def _read_llm_body(self) -> dict[str, Any] | None:
            """Read one chat request body; ``MAX_LLM_BODY_BYTES`` keeps it bounded."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length > MAX_LLM_BODY_BYTES:
                return None
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return None
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except OSError, ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None

        def _send_llm_stream(self, model: str) -> None:
            """Answer a streaming request with SSE chunks, then ``[DONE]``."""
            events = (
                _llm_chunk(model, delta={"role": "assistant", "content": LLM_MOCK_REPLY}),
                _llm_chunk(model, delta={}, finish_reason="stop"),
            )
            stream = b"".join(f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode() for event in events)
            stream += b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            # No Content-Length: the stream's end is the connection's end —
            # closing below is how the reader learns it is finished.
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(stream)
            self.close_connection = True

        def _read_json_body(self) -> dict[str, Any] | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return None
            if length > MAX_CONTROL_BYTES:
                return None
            raw = self.rfile.read(length) if length else b""
            if not raw.strip():
                return {}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except OSError, ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8") + b"\n"
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return CatalogMockHandler


def build_combined_handler_class(auth_state: MockAuthState) -> type[BaseHTTPRequestHandler]:
    """One handler for both backends: auth paths go to it, the rest to us.

    The client logs in before it can sync a catalog, so a single local
    environment has to answer for both. Both parents subclass
    :class:`~http.server.BaseHTTPRequestHandler`, so the combined class just has
    to say which parent answers a request — and to arbitrate the two methods
    they spell differently.
    """
    catalog_cls = build_handler_class()
    auth_cls = build_auth_handler_class(auth_state)

    class CombinedMockHandler(catalog_cls, auth_cls):  # type: ignore[misc]
        def do_GET(self) -> None:
            if _is_auth_route(self.path):
                auth_cls.do_GET(self)
            else:
                catalog_cls.do_GET(self)

        def do_POST(self) -> None:
            if _is_auth_route(self.path):
                auth_cls.do_POST(self)
            else:
                catalog_cls.do_POST(self)

        def _send_json(self, *args: Any) -> None:
            """Both parents define this, with different signatures."""
            if _is_auth_route(self.path):
                auth_cls._send_json(self, *args)
            else:
                catalog_cls._send_json(self, *args)

        def log_message(self, format: str, *args: Any) -> None:
            if _is_auth_route(self.path):
                auth_cls.log_message(self, format, *args)
            else:
                catalog_cls.log_message(self, format, *args)

    return CombinedMockHandler


def _is_auth_route(target: str) -> bool:
    """Whether *target* belongs to the auth mock rather than this one."""
    return urlsplit(target).path.startswith(AUTH_ROUTE_PREFIXES)


def create_server(
    *,
    state: CatalogMockState | None = None,
    source: CatalogSource | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    quiet: bool = False,
    auth: MockAuthConfig | None = None,
) -> CatalogMockServer:
    """Build a stopped mock server ready for ``serve_forever()``.

    *auth* mounts the login mock on the same port (its paths, see
    ``AUTH_ROUTE_PREFIXES``); ``None`` serves the catalog alone.
    """
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The model catalog mock server may bind only to a loopback address.")
    auth_state = MockAuthState(auth) if auth is not None else None
    handler = build_combined_handler_class(auth_state) if auth_state else build_handler_class()
    server = CatalogMockServer(
        (host, port),
        handler,
        state=state or CatalogMockState(),
        source=source or CatalogSource(),
        quiet=quiet,
    )
    server.auth_state = auth_state  # type: ignore[attr-defined]
    server.auth_config = auth  # type: ignore[attr-defined]
    return server


def start(
    *,
    host: str = DEFAULT_HOST,
    port: int = 0,
    catalog_file: str | Path | None = None,
    mode: str = "ok",
    delay: float = DEFAULT_DELAY,
    quiet: bool = False,
    auth: MockAuthConfig | None = None,
) -> RunningCatalogMock:
    """Start the mock (returns once bound; the server thread is a daemon); loopback binds only.

    *auth* mounts the login mock on the same port, so one origin covers both
    backends the client talks to before it can sync a catalog.
    """
    if host not in _LOOPBACK_HOSTS:
        raise ValueError("The model catalog mock server may bind only to a loopback address.")
    state = CatalogMockState(mode=mode, delay=delay)
    source = CatalogSource(Path(catalog_file) if catalog_file else None)
    server = create_server(state=state, source=source, host=host, port=port, quiet=quiet, auth=auth)
    thread = threading.Thread(target=server.serve_forever, name="chrys-model-catalog-mock", daemon=True)
    thread.start()
    displayed = f"[{host}]" if ":" in host else host
    running = RunningCatalogMock(
        origin=f"http://{displayed}:{server.server_address[1]}",
        state=state,
        source=source,
        server=server,
    )
    if auth is not None:
        # Resolve after binding so ``--port 0`` still hands the verify page a
        # URL that points back at this instance.
        auth.base_url = running.origin
    return running


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mock_model_catalog_server",
        description="Loopback mock of the server-owned model catalog.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"bind address (default {DEFAULT_HOST})")
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"bind port (default {DEFAULT_PORT}); 0 picks a free port",
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help="catalog JSON file (array, or object with 'models'); re-read every request",
    )
    parser.add_argument("--mode", choices=MODES, default="ok", help="initial mode (default ok)")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"seconds 'slow' stalls (default {DEFAULT_DELAY})",
    )
    parser.add_argument("--quiet", action="store_true", help="log warnings and errors only")
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="do not start the auth mock alongside (the client logs in before it can sync)",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("auto", "manual"),
        default="auto",
        help="auth mock mode (default auto: no browser confirmation needed)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argument_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        auth = None if args.no_auth else MockAuthConfig(mode=args.auth_mode)
        running = start(
            host=args.host,
            port=args.port,
            catalog_file=args.catalog,
            mode=args.mode,
            delay=args.delay,
            quiet=args.quiet,
            auth=auth,
        )
    except (OSError, ValueError) as exc:
        logger.error("mock failed to start: %s", exc)
        return 1
    logger.info("serving  GET %s%s", running.origin, CATALOG_ROUTE)
    if args.auth_mode == "manual" and not args.no_auth:
        logger.info("confirm logins at %s%s", running.origin, AUTH_VERIFY_ROUTE)
    try:
        threading.Event().wait()  # The server thread is a daemon; wait for interrupt.
    except KeyboardInterrupt:
        pass
    finally:
        running.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
