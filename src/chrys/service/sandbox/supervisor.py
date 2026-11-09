# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Start the bundled sandbox server and wait until it is healthy.

The engine package is imported only inside the function that resolves the
bundled policy file. Importing this module does not load that package.
The child is started with stdin detached and is retained so it can be
stopped; a failed start closes it. This module does not run the user's
command on the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from chrys.foundation.util.once_close import finish_close
from chrys.service.sandbox.backend import SandboxError
from chrys.service.sandbox.client import open_client

_HEALTH_TIMEOUT_SECONDS = 30.0
_HEALTH_POLL_SECONDS = 0.05
_PROBE_TIMEOUT_SECONDS = 2.0


class _LauncherProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...


@dataclass
class _RunningServer:
    url: str
    proc: _LauncherProcess
    closer: contextlib.AsyncExitStack


_server: _RunningServer | None = None
_lock: asyncio.Lock | None = None


def uds_http_base() -> str:
    """Base URL paired with an UDS transport. The host is not dialed."""
    return "http://jiuwenbox"


def _gate() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _launcher_argv() -> list[str]:
    return [sys.executable, "-m", "jiuwenbox.server.launcher"]


def _bundled_policy_path() -> str:
    """Path of the vendored code-agent policy shipped inside the engine package."""
    from importlib.resources import as_file, files

    import jiuwenbox

    resource = files(jiuwenbox).joinpath("configs", "code-agent-policy.yaml")
    with as_file(resource) as path:
        if not path.is_file():
            raise SandboxError("Bundled sandbox policy file is missing.")
        resolved = str(path)
    if not os.path.isfile(resolved):
        raise SandboxError("Bundled sandbox policy file is missing.")
    return resolved


def _choose_listen_url() -> str:
    """Reserve an ephemeral port on 127.0.0.1 and return its http URL.

    The launcher rejects port 0, so the port is chosen here. The socket is
    closed before the child binds it. If something else takes the port in
    that gap, startup fails closed instead of running the command on the host.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    port: object = None
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    except OSError:
        raise SandboxError("Sandbox server could not reserve a local port.") from None
    finally:
        sock.close()
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SandboxError("Sandbox server could not reserve a local port.")
    return f"http://127.0.0.1:{port}"


def _child_env(policy_path: str, listen: str, token: str) -> dict[str, str]:
    env = dict(os.environ)
    env["JIUWENBOX_POLICY_PATH"] = policy_path
    env["JIUWENBOX_LISTEN"] = listen
    if token:
        env["JIUWENBOX_API_TOKEN"] = token
    else:
        env.pop("JIUWENBOX_API_TOKEN", None)
    return env


@asynccontextmanager
async def _spawn_launcher(env: dict[str, str]) -> AsyncGenerator[_LauncherProcess]:
    from chrys.foundation.platform.process import managed_subprocess

    async with managed_subprocess(
        *_launcher_argv(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    ) as proc:
        yield proc


async def _discard(stack: contextlib.AsyncExitStack) -> None:
    await finish_close(asyncio.create_task(stack.aclose()))


async def _close_running() -> None:
    global _server
    running = _server
    _server = None
    if running is not None:
        await _discard(running.closer)


async def _shutdown() -> None:
    """Stop a retained child. Tests call this; a live server stays up otherwise."""
    async with _gate():
        await _close_running()


async def ensure_started(token: str, *, health_timeout: float = _HEALTH_TIMEOUT_SECONDS) -> str:
    """Start the bundled server if this process does not already have one.

    Returns the http URL the child is listening on. A non-empty ``token`` is
    given to the child as its API token and is not written into errors.
    """
    global _server
    async with _gate():
        running = _server
        if running is not None and running.proc.returncode is None:
            return running.url
        await _close_running()
        listen = _choose_listen_url()
        env = _child_env(_bundled_policy_path(), listen, token)
        stack = contextlib.AsyncExitStack()
        try:
            proc = await stack.enter_async_context(_spawn_launcher(env))
            await _wait_until_healthy(listen, token, proc, health_timeout=health_timeout)
        except BaseException as exc:
            await _discard(stack)
            if isinstance(exc, OSError):
                raise SandboxError("Sandbox server failed to start.") from None
            raise
        _server = _RunningServer(url=listen, proc=proc, closer=stack)
        return listen


async def _wait_until_healthy(
    base_url: str,
    token: str,
    proc: _LauncherProcess,
    *,
    health_timeout: float,
) -> None:
    deadline = time.monotonic() + health_timeout
    while True:
        if proc.returncode is not None:
            raise SandboxError("Sandbox server exited before it became healthy.")
        if await _health_ok(base_url, token):
            return
        if time.monotonic() >= deadline:
            raise SandboxError("Sandbox server did not become healthy.")
        await asyncio.sleep(_HEALTH_POLL_SECONDS)


async def _health_ok(base_url: str, token: str) -> bool:
    import httpx

    payload: object = None
    try:
        async with open_client(base_url, token, _PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get("/health")
            if response.status_code != 200:
                return False
            payload = response.json()
    except SandboxError:
        raise
    except httpx.HTTPError, OSError, ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("status") == "ok"
