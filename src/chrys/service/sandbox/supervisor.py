# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Start the bundled sandbox server and wait until it is healthy.

The engine package is imported only inside the function that resolves the
bundled policy file. Importing this module does not load that package.
The child is started with stdin detached. Its stdout and stderr are read
into a short tail so a failed start can include one line of the engine's
own error, and that line never includes the bearer token. The child is
retained so it can be stopped; a failed start closes it. Stopping sends
SIGTERM to the launcher process group and waits up to five seconds so the
server can run its lifespan shutdown, then closes the child. A child that
is still alive is killed by that close. Each start creates a bearer token
and keeps it only in memory. This module does not run the user's command
on the host.

Each child keeps the ``sandbox.network`` value it was started with. The
caller passes the value from its own settings, so a change reaches a
session when its agent reloads, like the other sandbox settings. A command
asking for another value makes a new child the current one. The replaced
child takes no new commands and is stopped once the last command holding
it (``leased_server``) finishes, so a network change never ends a running
command. The new server does not know the old sandbox ids; the backend
gets a 404 for a cached id and creates a replacement. A configured
``sandbox.url`` never reaches this module, so ``sandbox.network`` does not
apply to that service.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import NoReturn, Protocol

from chrys.foundation.util.once_close import finish_close
from chrys.service.sandbox.backend import SandboxError
from chrys.service.sandbox.client import open_client

_HEALTH_TIMEOUT_SECONDS = 30.0
_HEALTH_POLL_SECONDS = 0.05
_PROBE_TIMEOUT_SECONDS = 2.0
# Each child stream keeps this many trailing bytes. A failed start quotes one
# line from that tail; the rest is discarded.
_LAUNCHER_OUTPUT_BYTES = 8 * 1024
_FAILURE_LINE_CHARS = 200
# The child has already exited. Wait only long enough for the pipes to deliver
# the bytes already written, and give up if a grandchild still holds one open.
_EXIT_DRAIN_SECONDS = 1.0
_READ_CHUNK_BYTES = 4096
# uvicorn runs lifespan shutdown on SIGTERM. After this, the context close
# SIGKILLs a child that is still alive. ``managed_subprocess`` skips that kill
# once ``returncode`` is set, so a launcher that exited on SIGTERM is not
# killed again.
_STOP_GRACE_SECONDS = 5.0


class _ReturnCode(Protocol):
    @property
    def returncode(self) -> int | None: ...


class _ChildProcess(_ReturnCode, Protocol):
    @property
    def pid(self) -> int | None: ...

    def wait(self) -> Awaitable[int]: ...


class _LauncherProcess(_ReturnCode, Protocol):
    async def startup_output(self) -> tuple[str, str]:
        """Captured stderr and stdout, either of which may be empty."""

    async def request_exit(self) -> None:
        """SIGTERM the process group and wait until it exits or the grace passes."""


@dataclass
class _RunningServer:
    url: str
    proc: _LauncherProcess
    closer: contextlib.AsyncExitStack
    # The only configured input to the child env. The policy path is fixed by
    # the install, and the listen URL and token are new on every start.
    network_mode: str
    # Omitted from ``repr`` so a log of this object cannot print the bearer token.
    token: str = field(repr=False)
    # Commands inside ``leased_server`` for this child.
    leases: int = 0


_NETWORK_MODES = frozenset({"offline", "guarded", "host"})

# ``_server`` takes new commands. ``_retired`` holds replaced children that a
# command still leases; the last lease to end stops the child.
_server: _RunningServer | None = None
_retired: list[_RunningServer] = []
_lock: asyncio.Lock | None = None


def sandbox_engine_home_dirname() -> str:
    """Directory name of the sandbox engine home, under the account home."""
    return ".jiuwenbox"


def uds_http_base() -> str:
    """Base URL paired with an UDS transport. The host is not dialed."""
    return "http://jiuwenbox"


def _gate() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _launcher_argv() -> list[str]:
    """Argv for the bundled server.

    ``-P`` keeps the process cwd and the script directory off ``sys.path``,
    so a project file with the engine package's name cannot become the
    launcher. The interpreter is 3.14, where ``-P`` exists.
    """
    return [sys.executable, "-P", "-m", "jiuwenbox.server.launcher"]


def _launcher_cwd() -> str:
    """Working directory for the bundled server process.

    The interpreter prefix is not the process cwd and not the workspace.
    Combined with ``-P``, Python does not put that directory on
    ``sys.path``. The launcher is still imported from the prefix's
    site-packages.
    """
    return sys.prefix


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


class _OutputTail:
    """The last ``limit`` bytes written to one child stream."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._data = bytearray()

    def extend(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._data.extend(chunk)
        extra = len(self._data) - self._limit
        if extra > 0:
            del self._data[:extra]

    def text(self) -> str:
        return bytes(self._data).decode("utf-8", errors="replace")


class _CapturedLauncher:
    """A child plus the tails of its stdout and stderr.

    ``preset_stderr`` and ``preset_stdout`` are text the child object already
    holds (a test double). A live pipe's tail wins when it has any bytes.
    """

    def __init__(
        self,
        proc: _ChildProcess,
        drain: asyncio.Task[None],
        stderr: _OutputTail,
        stdout: _OutputTail,
        preset_stderr: str,
        preset_stdout: str,
    ) -> None:
        self._proc = proc
        self._drain = drain
        self._stderr = stderr
        self._stdout = stdout
        self._preset_stderr = preset_stderr
        self._preset_stdout = preset_stdout

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode

    async def request_exit(self) -> None:
        await _request_exit(self._proc)

    async def startup_output(self) -> tuple[str, str]:
        if self._proc.returncode is not None and not self._drain.done():
            await asyncio.wait({self._drain}, timeout=_EXIT_DRAIN_SECONDS)
        stderr = self._stderr.text() or self._preset_stderr
        stdout = self._stdout.text() or self._preset_stdout
        return stderr, stdout


def _as_reader(value: object) -> asyncio.StreamReader | None:
    if isinstance(value, asyncio.StreamReader):
        return value
    return None


def _text_output(value: object) -> str:
    if isinstance(value, str):
        return value
    return ""


def _plain_line(raw: str) -> str:
    kept: list[str] = []
    for char in raw:
        if char.isspace():
            kept.append(" ")
            continue
        code = ord(char)
        if code < 0x20 or 0x7F <= code <= 0x9F:
            continue
        kept.append(char)
    return " ".join("".join(kept).split()).strip("�").strip()


def _last_line(text: str, token: str) -> str:
    """Last non-empty line of ``text``, with ``token`` removed and the line shortened."""
    if token:
        text = text.replace(token, "")
    chosen = ""
    for raw in text.splitlines():
        line = _plain_line(raw)
        if line:
            chosen = line
    if len(chosen) > _FAILURE_LINE_CHARS:
        chosen = chosen[: _FAILURE_LINE_CHARS - 1].rstrip() + "…"
    return chosen


def _failure_line(stderr: str, stdout: str, token: str) -> str:
    """One short line from stderr, or from stdout when stderr has nothing usable."""
    line = _last_line(stderr, token)
    if line:
        return line
    return _last_line(stdout, token)


async def _pump_stream(stream: asyncio.StreamReader | None, tail: _OutputTail) -> None:
    if stream is None:
        return
    while True:
        try:
            chunk = await stream.read(_READ_CHUNK_BYTES)
        except OSError:
            return
        if not chunk:
            return
        tail.extend(chunk)


async def _drain_launcher_output(
    stdout: object,
    stderr: object,
    stdout_tail: _OutputTail,
    stderr_tail: _OutputTail,
) -> None:
    await asyncio.gather(
        _pump_stream(_as_reader(stdout), stdout_tail),
        _pump_stream(_as_reader(stderr), stderr_tail),
    )


def _network_mode(value: str) -> str:
    """``value`` when it names a mode, else ``guarded``, the setting's default.

    ``host`` does not set a child variable. ``offline`` and ``guarded`` do.
    """
    if value in _NETWORK_MODES:
        return value
    return "guarded"


def _child_env(
    policy_path: str,
    listen: str,
    token: str,
    *,
    network_mode: str = "host",
) -> dict[str, str]:
    env = dict(os.environ)
    env["JIUWENBOX_POLICY_PATH"] = policy_path
    env["JIUWENBOX_LISTEN"] = listen
    if token:
        env["JIUWENBOX_API_TOKEN"] = token
    else:
        env.pop("JIUWENBOX_API_TOKEN", None)
    if network_mode in {"offline", "guarded"}:
        env["JIUWENBOX_NETWORK_MODE"] = network_mode
    else:
        env.pop("JIUWENBOX_NETWORK_MODE", None)
    return env


@asynccontextmanager
async def _spawn_launcher(env: dict[str, str]) -> AsyncGenerator[_LauncherProcess]:
    """Start the launcher with stdin detached and a bounded tail of its output.

    stdout and stderr are pipes, not inherited descriptors. Each stream keeps
    an 8 KiB tail for the startup error line. A string already stored on the
    child (a test double) is used when that stream is not a pipe.
    """
    from chrys.foundation.platform.process import managed_subprocess

    stderr_tail = _OutputTail(_LAUNCHER_OUTPUT_BYTES)
    stdout_tail = _OutputTail(_LAUNCHER_OUTPUT_BYTES)
    async with managed_subprocess(
        *_launcher_argv(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=_launcher_cwd(),
    ) as proc:
        drain = asyncio.create_task(
            _drain_launcher_output(proc.stdout, proc.stderr, stdout_tail, stderr_tail),
            name="chrys.sandbox.launcher-output",
        )
        captured = _CapturedLauncher(
            proc,
            drain,
            stderr_tail,
            stdout_tail,
            _text_output(proc.stderr),
            _text_output(proc.stdout),
        )
        try:
            yield captured
        finally:
            if not drain.done():
                drain.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await drain


def _signal_launcher(pid: int) -> None:
    """SIGTERM the launcher process group. Windows has no such group; SIGTERM ends that process."""
    if pid <= 0:
        return
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)
        return
    from chrys.foundation.platform.process import terminate_process_group

    terminate_process_group(pid)


async def _request_exit(proc: _ChildProcess) -> None:
    """SIGTERM ``proc`` and wait out the grace. A timeout leaves it for the kill backstop."""
    if proc.returncode is not None:
        return
    pid = proc.pid
    if pid is None or pid <= 0:
        return
    _signal_launcher(pid)
    try:
        await asyncio.wait_for(proc.wait(), _STOP_GRACE_SECONDS)
    except TimeoutError:
        return


async def _close_retained(stack: contextlib.AsyncExitStack, proc: _LauncherProcess | None) -> None:
    """SIGTERM and wait, then close. The close SIGKILLs a child that is still alive.

    Callers run this as the body of a ``finish_close`` task. A waiter cancel
    must not cut the wait short and must not skip the close.
    """
    try:
        if proc is not None:
            await proc.request_exit()
    finally:
        await stack.aclose()


async def _discard(stack: contextlib.AsyncExitStack, proc: _LauncherProcess | None = None) -> None:
    await finish_close(asyncio.create_task(_close_retained(stack, proc)))


async def _retire_current() -> None:
    """Take new commands off the current child; stop it unless one still leases it."""
    global _server
    running = _server
    _server = None
    if running is None:
        return
    if running.leases:
        _retired.append(running)
        return
    await _discard(running.closer, running.proc)


async def shutdown() -> None:
    """Stop every retained bundled server, leased or not.

    Process exit calls this when no other runtime is still live. A replaced
    server that a command still leases is left running by a network change;
    the last lease, and this call, both stop it by SIGTERM and then close.
    """
    await _shutdown()


async def _shutdown() -> None:
    """Stop every retained child, leased or not."""
    await finish_close(asyncio.create_task(_shutdown_retained()))


async def _shutdown_retained() -> None:
    global _server
    async with _gate():
        children = [child for child in (_server, *_retired) if child is not None]
        _server = None
        _retired.clear()
        for child in children:
            await _close_retained(child.closer, child.proc)


async def ensure_started(
    *,
    network_mode: str = "guarded",
    health_timeout: float = _HEALTH_TIMEOUT_SECONDS,
) -> tuple[str, str]:
    """Return the URL and token of a live bundled server with ``network_mode``.

    This takes no lease; ``run_shell`` uses ``leased_server``.
    """
    async with _gate():
        running = await _current_server(_network_mode(network_mode), health_timeout)
    return running.url, running.token


@asynccontextmanager
async def leased_server(
    network_mode: str,
    *,
    health_timeout: float = _HEALTH_TIMEOUT_SECONDS,
) -> AsyncGenerator[tuple[str, str]]:
    """Hold a live bundled server with ``network_mode`` for one command.

    Yields its URL and bearer token. While the lease is held, a command with
    another mode can make a new child current, but this child is not
    stopped; the last lease on a replaced child stops it on exit.
    """
    async with _gate():
        running = await _current_server(_network_mode(network_mode), health_timeout)
        running.leases += 1
    try:
        yield running.url, running.token
    finally:
        running.leases -= 1
        if running.leases == 0 and running in _retired:
            _retired.remove(running)
            await _discard(running.closer, running.proc)


async def _current_server(network_mode: str, health_timeout: float) -> _RunningServer:
    """The current child when it is live with ``network_mode``; otherwise a new one.

    Called with the gate held. A current child that exited or runs another
    mode is retired first. The token is created with
    ``secrets.token_urlsafe`` on each start and kept only on the retained
    server. It is given to the child solely as ``JIUWENBOX_API_TOKEN``. The
    user's ``sandbox.token`` is not read and is not copied into the child.
    The token is not written into errors.
    """
    global _server
    running = _server
    if running is not None and running.proc.returncode is None and running.network_mode == network_mode:
        return running
    await _retire_current()
    listen = _choose_listen_url()
    # Fresh credential for this child. Not the user's sandbox.token, and
    # not placed in argv, a policy file, or the listen URL.
    token = secrets.token_urlsafe()
    env = _child_env(
        _bundled_policy_path(),
        listen,
        token,
        network_mode=network_mode,
    )
    proc: _LauncherProcess | None = None
    stack = contextlib.AsyncExitStack()
    try:
        proc = await stack.enter_async_context(_spawn_launcher(env))
        await _wait_until_healthy(listen, token, proc, health_timeout=health_timeout)
    except BaseException as exc:
        await _discard(stack, proc)
        if isinstance(exc, OSError):
            raise SandboxError("Sandbox server failed to start.") from None
        raise
    _server = _RunningServer(url=listen, token=token, proc=proc, closer=stack, network_mode=network_mode)
    return _server


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
            await _fail_startup("Sandbox server exited before it became healthy.", proc, token)
        if await _health_ok(base_url, token):
            return
        if time.monotonic() >= deadline:
            await _fail_startup("Sandbox server did not become healthy.", proc, token)
        await asyncio.sleep(_HEALTH_POLL_SECONDS)


async def _fail_startup(message: str, proc: _LauncherProcess, token: str) -> NoReturn:
    """Raise ``message``, appending one launcher line when the child printed one."""
    stderr, stdout = await proc.startup_output()
    line = _failure_line(stderr, stdout, token)
    if line:
        raise SandboxError(f"{message} {line}")
    raise SandboxError(message)


async def _health_ok(base_url: str, token: str) -> bool:
    import httpx

    payload: object = None
    try:
        async with open_client(base_url, token, _PROBE_TIMEOUT_SECONDS, trust_env=False) as client:
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
