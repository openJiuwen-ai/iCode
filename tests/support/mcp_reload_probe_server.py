# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An MCP server whose next start a test controls through files in ``--state-dir``.

Read once at start, so a running process keeps what it started with while the
next one differs: that is how a test makes a reconnect fail, or change the
schema, beside a connection that stays healthy.

- ``starts``: this process appends its pid.
- ``calls``: the ``probe`` tool appends ``<pid> <label>``.
- ``refuse``: present, the process exits before the handshake.
- ``hold``: present, the process waits before the handshake until the file is gone.
- ``extra_parameter``: present, ``probe`` also takes an ``extra`` parameter.
- ``instructions``: the server instructions, when present.
- ``no_ping``: present, the server does not implement ping (it answers -32601).
- ``token`` (HTTP only): the one ``Authorization`` value accepted; others get 401.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server.fastmcp import FastMCP
from starlette.responses import PlainTextResponse


def _build_server(state: Path) -> FastMCP:
    instructions = state / "instructions"
    server = FastMCP(
        "reload-probe",
        instructions=instructions.read_text(encoding="utf-8") if instructions.exists() else None,
        host="127.0.0.1",
        streamable_http_path="/mcp",
        log_level="ERROR",
    )

    def record(label: str) -> str:
        with (state / "calls").open("a", encoding="utf-8") as calls:
            calls.write(f"{os.getpid()} {label}\n")
        return f"answered by {os.getpid()}"

    def probe(label: str = "") -> str:
        """Report which process answered."""
        return record(label)

    def probe_with_extra(label: str = "", extra: str = "") -> str:
        """Report which process answered."""
        return record(label)

    server.add_tool(probe_with_extra if (state / "extra_parameter").exists() else probe, name="probe")
    if (state / "no_ping").exists():
        server._mcp_server.request_handlers.pop(types.PingRequest, None)
    return server


class _RequireToken:
    """Plain ASGI, since a buffering middleware would hold the server's event stream back."""

    def __init__(self, app, *, token: Path) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and self._token.exists():
            sent = dict(scope["headers"]).get(b"authorization", b"")
            if sent.decode("latin-1") != self._token.read_text(encoding="utf-8"):
                await PlainTextResponse("unauthorized", status_code=401)(scope, receive, send)
                return
        await self._app(scope, receive, send)


def _serve_http(server: FastMCP, state: Path, port_file: Path) -> None:
    app = server.streamable_http_app()
    app.add_middleware(_RequireToken, token=state / "token")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port_file.write_text(str(listener.getsockname()[1]), encoding="utf-8")
    uvicorn.Server(uvicorn.Config(app, log_level="error")).run(sockets=[listener])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--port-file")
    args = parser.parse_args()
    state = Path(args.state_dir)
    with (state / "starts").open("a", encoding="utf-8") as starts:
        starts.write(f"{os.getpid()}\n")
    if (state / "refuse").exists():
        sys.exit(3)
    while (state / "hold").exists():
        time.sleep(0.02)
    server = _build_server(state)
    if args.port_file:
        _serve_http(server, state, Path(args.port_file))
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
