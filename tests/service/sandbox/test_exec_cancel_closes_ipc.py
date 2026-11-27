# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancelling one exec closes only that call's daemon IPC socket.

The daemon killpg's a command when its IPC connection fully closes. These
tests drive ``ProcessRuntime`` with ``socket.socketpair`` and never start
bubblewrap or a process group. The tests that wait out a blocked read
after the socket closes run on Linux only. The sandbox runs only on Linux,
and macOS does not wake that read the same way. A regression that leaves the
cancelled call's socket open fails the wait for that close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import types
from collections.abc import Callable, Iterator
from concurrent.futures import Executor, ThreadPoolExecutor
from pathlib import Path

import pytest

from tests.support.waiting import wait_for

_SANDBOX_ID = "sb-cancel"
_CANCELLED = ("sleep", "one")
_KEPT = ("sleep", "two")
_ROUTE = ("sleep", "route")
_LINUX_ONLY = pytest.mark.skipif(
    sys.platform != "linux",
    reason="blocked-read socket close is only asserted on Linux",
)


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows.

    ``pwd`` and ``grp`` are imported at module scope. The stubs only exist
    so those imports succeed; no account lookup runs.
    """
    import os

    if not hasattr(os, "geteuid"):
        monkeypatch.setattr(os, "geteuid", lambda: 0, raising=False)

    def _missing(*_args: object) -> None:
        raise KeyError(_args)

    def _install(name: str) -> None:
        if name in sys.modules:
            return
        try:
            __import__(name)
        except ModuleNotFoundError:
            module = types.ModuleType(name)
            if name == "pwd":
                module.getpwuid = _missing  # type: ignore[attr-defined]
                module.getpwnam = _missing  # type: ignore[attr-defined]
            else:
                module.getgrnam = _missing  # type: ignore[attr-defined]
            monkeypatch.setitem(sys.modules, name, module)

    _install("pwd")
    _install("grp")


class _Alive:
    def poll(self) -> None:
        return None


class _Pair:
    def __init__(self) -> None:
        self.local, self.peer = socket.socketpair()
        self.peer.setblocking(False)
        self.buf = bytearray()

    def drain(self) -> None:
        while True:
            try:
                chunk = self.peer.recv(65536)
            except BlockingIOError:
                return
            if not chunk:
                return
            self.buf.extend(chunk)

    def command(self) -> tuple[str, ...] | None:
        self.drain()
        if len(self.buf) < 4:
            return None
        (size,) = struct.unpack(">I", self.buf[:4])
        if size > 1_000_000 or len(self.buf) < 4 + size:
            return None
        try:
            payload = json.loads(self.buf[4 : 4 + size])
        except json.JSONDecodeError:
            return None
        command = payload.get("command")
        if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            return None
        return tuple(command)

    def closed(self) -> bool:
        # ``SHUT_WR`` only half-closes. ``SHUT_RDWR`` wakes a blocked recv
        # the way production cancel does. Only ``close()`` drops the fd,
        # which is what this check reads.
        return self.local.fileno() == -1

    def close(self) -> None:
        # Match ``_ExecIpcSlot.cancel_and_close``: wake a blocked recv,
        # then drop the fd. ``SHUT_RDWR`` alone does not count as closed.
        for sock in (self.local, self.peer):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
            with contextlib.suppress(OSError):
                sock.close()


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jiuwenbox.server.runtime.process import ProcessRuntime, _DaemonExecCall

        self.runtime = ProcessRuntime()
        self.pairs: list[_Pair] = []
        self.finished: list[tuple[str, ...]] = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self._lock = threading.Lock()
        self._stopped = False
        runtime = self.runtime
        original = runtime._exec_via_daemon_blocking

        def connect(_path: Path) -> object:
            self.entered.set()
            if not self.release.is_set() and not self.release.wait(5):
                raise TimeoutError("connect was not released")
            pair = _Pair()
            with self._lock:
                self.pairs.append(pair)
                stopped = self._stopped
            if stopped:
                pair.close()
                raise ConnectionError("test stopped")
            return pair.local

        def wrapped(call: _DaemonExecCall) -> object:
            try:
                return original(call)
            finally:
                with self._lock:
                    self.finished.append(tuple(call.command))

        monkeypatch.setattr(runtime, "_connect_daemon_socket", connect)
        monkeypatch.setattr(runtime, "_exec_via_daemon_blocking", wrapped)

    def hold_connect(self) -> None:
        self.entered.clear()
        self.release.clear()

    def pairs_snapshot(self) -> list[_Pair]:
        with self._lock:
            return list(self.pairs)

    def finished_commands(self) -> list[tuple[str, ...]]:
        with self._lock:
            return list(self.finished)

    def pair_for(self, command: tuple[str, ...]) -> _Pair | None:
        for pair in self.pairs_snapshot():
            if pair.command() == command:
                return pair
        return None

    def close_all(self) -> None:
        with self._lock:
            self._stopped = True
            pairs = list(self.pairs)
        self.release.set()
        for pair in pairs:
            pair.close()


def _arm_daemon(harness: _Harness, tmp_path: Path) -> None:
    from jiuwenbox.supervisor.daemon_ipc import SANDBOX_CONTROL_SOCKET_NAME

    control = tmp_path / "control"
    control.mkdir()
    (control / SANDBOX_CONTROL_SOCKET_NAME).write_bytes(b"")
    runtime = harness.runtime
    runtime._control_dirs[_SANDBOX_ID] = control
    runtime._daemon_socket_ready[_SANDBOX_ID] = True
    runtime._processes[_SANDBOX_ID] = _Alive()  # type: ignore[assignment]
    runtime._exec_concurrency_limit = 4
    runtime._exec_semaphore = None


async def _drain(task: asyncio.Task[object]) -> None:
    if not task.done():
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


async def _shutdown(harness: _Harness, tasks: list[asyncio.Task[object]], expected: int) -> None:
    harness.close_all()
    for task in tasks:
        await _drain(task)
    await wait_for(
        lambda: len(harness.finished_commands()) >= expected,
        description="daemon exec workers finished",
    )


def _exec_request(command: tuple[str, ...]) -> object:
    from jiuwenbox.server.runtime.base import RuntimeExecRequest

    return RuntimeExecRequest(command=list(command), timeout=30)


@_LINUX_ONLY
async def test_cancel_closes_only_that_exec_socket(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    _arm_daemon(harness, tmp_path)
    runtime = harness.runtime
    cancelled = asyncio.create_task(runtime.exec(_SANDBOX_ID, _exec_request(_CANCELLED)))  # type: ignore[arg-type]
    kept = asyncio.create_task(runtime.exec(_SANDBOX_ID, _exec_request(_KEPT)))  # type: ignore[arg-type]
    try:
        await wait_for(
            lambda: harness.pair_for(_CANCELLED) is not None and harness.pair_for(_KEPT) is not None,
            description="both exec requests reached the daemon socket",
        )
        cancelled_pair = harness.pair_for(_CANCELLED)
        kept_pair = harness.pair_for(_KEPT)
        assert cancelled_pair is not None
        assert kept_pair is not None
        cancelled.cancel()
        await wait_for(cancelled_pair.closed, description="cancelled exec socket closed")
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await wait_for(
            lambda: _CANCELLED in harness.finished_commands(),
            description="cancelled exec worker finished",
        )
        assert kept_pair.local.fileno() != -1
        assert _KEPT not in harness.finished_commands()
        assert runtime._daemon_socket_ready[_SANDBOX_ID] is True
    finally:
        await _shutdown(harness, [cancelled, kept], 2)


async def test_cancel_before_connect_closes_the_new_socket(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _Harness(monkeypatch)
    _arm_daemon(harness, tmp_path)
    harness.hold_connect()
    task = asyncio.create_task(
        harness.runtime.exec(_SANDBOX_ID, _exec_request(_CANCELLED))  # type: ignore[arg-type]
    )
    try:
        await wait_for(harness.entered.is_set, description="exec reached connect")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        harness.release.set()
        await wait_for(
            lambda: bool(harness.pairs_snapshot()) and harness.pairs_snapshot()[0].closed(),
            description="socket closed after connect lost the cancel race",
        )
        await wait_for(
            lambda: len(harness.finished_commands()) == 1,
            description="exec worker finished without leaving the command running",
        )
        assert harness.pairs_snapshot()[0].command() is None
        assert harness.runtime._daemon_socket_ready[_SANDBOX_ID] is True
    finally:
        await _shutdown(harness, [task], 1)


def _route_to_harness(harness: _Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve the sandbox routes from a manager whose runtime is ``harness``."""
    from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.sandbox_manager import SandboxManager

    class _Audit:
        def log(self, *_args: object, **_kwargs: object) -> None:
            return None

    manager = SandboxManager(
        runtime=harness.runtime,
        policy_engine=PolicyEngine(policies_dir=tmp_path / "policies"),
        audit_logger=_Audit(),  # type: ignore[arg-type]
        state_dir=tmp_path / "state",
        policy_path=tmp_path / "missing-policy.yaml",
        update_policy_path=tmp_path / "missing-updates.yaml",
    )
    manager._sandboxes[_SANDBOX_ID] = SandboxRef(
        id=_SANDBOX_ID,
        phase=SandboxPhase.READY,
        runtime="process",
    )

    async def _restart(sandbox_id: str, *, only_from: object = None) -> object:
        raise AssertionError(f"cancel restarted {sandbox_id} from {only_from!r}")

    monkeypatch.setattr(manager, "restart_sandbox", _restart)
    monkeypatch.setattr(sandbox_routes, "_mgr", lambda: manager)


@_LINUX_ONLY
async def test_http_disconnect_closes_only_that_exec_socket(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.routes.sandbox import ExecRequest
    from starlette.responses import Response

    harness = _Harness(monkeypatch)
    _arm_daemon(harness, tmp_path)
    _route_to_harness(harness, tmp_path, monkeypatch)

    class _HttpRequest:
        def __init__(self) -> None:
            self.gone = asyncio.Event()
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            if self.calls == 1:
                return False
            await self.gone.wait()
            return True

    http_request = _HttpRequest()
    route_task = asyncio.create_task(
        sandbox_routes.exec_in_sandbox(
            _SANDBOX_ID,
            ExecRequest(command=list(_ROUTE), timeout_seconds=30),
            http_request,  # type: ignore[arg-type]
        )
    )
    kept = asyncio.create_task(
        harness.runtime.exec(_SANDBOX_ID, _exec_request(_KEPT))  # type: ignore[arg-type]
    )
    try:
        await wait_for(
            lambda: harness.pair_for(_ROUTE) is not None and harness.pair_for(_KEPT) is not None,
            description="route exec and second exec reached the daemon socket",
        )
        route_pair = harness.pair_for(_ROUTE)
        kept_pair = harness.pair_for(_KEPT)
        assert route_pair is not None
        assert kept_pair is not None
        http_request.gone.set()
        await wait_for(route_pair.closed, description="disconnected exec socket closed")
        result = await route_task
        assert isinstance(result, Response)
        assert result.status_code == 499
        await wait_for(
            lambda: _ROUTE in harness.finished_commands(),
            description="disconnected exec worker finished",
        )
        assert kept_pair.local.fileno() != -1
        assert _KEPT not in harness.finished_commands()
        assert harness.runtime._daemon_socket_ready[_SANDBOX_ID] is True
        assert http_request.calls >= 1
    finally:
        await _shutdown(harness, [route_task, kept], 2)


def _exec_http_request(token: str) -> bytes:
    body = json.dumps({"command": list(_ROUTE), "timeout_seconds": 30}).encode("utf-8")
    head = (
        f"POST /api/v1/sandboxes/{_SANDBOX_ID}/exec HTTP/1.1\r\n"
        "Host: jiuwenbox\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    )
    return head.encode("ascii") + body


@_LINUX_ONLY
async def test_client_drop_through_the_served_app_closes_the_exec_socket(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production app, middleware included, sees a dropped client.

    iCode never POSTs ``/stop`` on cancel; closing the connection is the only
    cancel signal. A middleware that hides ``http.disconnect`` from the route
    leaves the exec socket open, and the wait for its close fails.
    """
    import uvicorn
    from jiuwenbox.server.app import app as jiuwenbox_app

    monkeypatch.setenv("JIUWENBOX_API_TOKEN", "sekret")
    harness = _Harness(monkeypatch)
    _arm_daemon(harness, tmp_path)
    _route_to_harness(harness, tmp_path, monkeypatch)

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            # The test owns the main thread's signal handlers.
            yield

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    address = listener.getsockname()[:2]
    server = _Server(uvicorn.Config(jiuwenbox_app, lifespan="off", log_config=None, timeout_graceful_shutdown=5))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    writer: asyncio.StreamWriter | None = None
    try:
        await wait_for(lambda: server.started or serving.done(), description="jiuwenbox app serving")
        if serving.done():
            await serving
        _reader, writer = await asyncio.open_connection(*address)
        writer.write(_exec_http_request("sekret"))
        await writer.drain()
        await wait_for(
            lambda: harness.pair_for(_ROUTE) is not None,
            description="served exec reached the daemon socket",
        )
        route_pair = harness.pair_for(_ROUTE)
        assert route_pair is not None
        writer.close()
        await writer.wait_closed()
        await wait_for(route_pair.closed, description="dropped client's exec socket closed")
        await wait_for(
            lambda: _ROUTE in harness.finished_commands(),
            description="dropped client's exec worker finished",
        )
    finally:
        if writer is not None:
            writer.close()
        harness.close_all()
        server.should_exit = True
        await asyncio.wait_for(serving, 15)
        listener.close()
        if harness.pairs_snapshot():
            await wait_for(
                lambda: _ROUTE in harness.finished_commands(),
                description="served exec worker finished",
            )


def _write_end_closed(read_fd: int) -> bool:
    """Whether every write end of the pipe is closed: the read end then sees EOF."""
    try:
        return os.read(read_fd, 1) == b""
    except BlockingIOError:
        return False


@pytest.mark.skipif(sys.platform == "win32", reason="cgroup fds are passed on POSIX hosts")
@pytest.mark.parametrize("kind", ["exec", "background"])
async def test_a_call_cancelled_while_queued_closes_its_cgroup_fds(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
) -> None:
    """A cancelled executor future never runs its work item, so the worker cannot close the fds."""
    from jiuwenbox.server.runtime.base import RuntimeBackgroundExecRequest, RuntimeExecRequest
    from jiuwenbox.server.runtime.process import ProcessRuntime

    runtime = ProcessRuntime()
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    command_cgroup = object()
    released: list[object] = []
    ran: list[str] = []
    monkeypatch.setattr(runtime, "_disk_floor_result", lambda _sandbox_id: None)
    monkeypatch.setattr(runtime, "_writable_binds_are_low", lambda _sandbox_id: False)
    monkeypatch.setattr(runtime, "_reap_background_processes", lambda _sandbox_id: None)
    monkeypatch.setattr(runtime, "_control_socket_host_path", lambda _sandbox_id: tmp_path / "control.sock")
    monkeypatch.setattr(runtime, "_daemon_ipc_available", lambda _sandbox_id: True)
    monkeypatch.setattr(runtime, "_take_command_cgroup_fds", lambda _sandbox_id: (command_cgroup, [write_fd], None))
    monkeypatch.setattr(runtime, "_release_command_cgroup", released.append)
    monkeypatch.setattr(runtime, "_run_daemon_exec", lambda _call: ran.append("exec"))
    monkeypatch.setattr(runtime, "_exec_background_via_daemon_blocking", lambda _call: ran.append("background"))

    pool = ThreadPoolExecutor(max_workers=1)
    unblock = threading.Event()
    pool.submit(unblock.wait)
    loop = asyncio.get_running_loop()
    queued: list[object] = []
    submit = loop.run_in_executor
    workers = (runtime._run_daemon_exec, runtime._exec_background_via_daemon_blocking)

    def run_in_pool(executor: Executor | None, func: Callable[..., object], *args: object) -> asyncio.Future[object]:
        if func not in workers:
            return submit(executor, func, *args)
        queued.append(func)
        return submit(pool, func, *args)

    monkeypatch.setattr(loop, "run_in_executor", run_in_pool)
    if kind == "exec":
        call = runtime._exec_via_daemon(_SANDBOX_ID, RuntimeExecRequest(command=["true"]))
    else:
        call = runtime.exec_background(_SANDBOX_ID, RuntimeBackgroundExecRequest(command=["true"], job_id="job"))
    task = asyncio.ensure_future(call)
    try:
        await wait_for(lambda: bool(queued) or task.done(), description="call is queued behind the busy worker")
        if task.done():
            await task
        assert not _write_end_closed(read_fd)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _write_end_closed(read_fd)
        assert [command for command in released if command is not None] == [command_cgroup]
    finally:
        unblock.set()
        await asyncio.to_thread(pool.shutdown, wait=True)
        os.close(read_fd)
    assert ran == []


@pytest.mark.skipif(sys.platform == "win32", reason="the sandbox daemon runs on POSIX hosts")
def test_a_failed_pipe_closes_the_command_cgroup_fds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The handler already dropped its copies, so ``_start_command`` is their last owner."""
    from jiuwenbox.supervisor import sandbox_daemon

    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    pipes: list[int] = []

    def pipe() -> tuple[int, int]:
        if pipes:
            raise OSError(24, "Too many open files")
        pair = os.pipe()
        pipes.extend(pair)
        return pair

    shadow = types.ModuleType("os")
    shadow.__dict__.update(os.__dict__)
    shadow.pipe = pipe  # type: ignore[attr-defined]
    monkeypatch.setattr(sandbox_daemon, "os", shadow)
    token = sandbox_daemon._COMMAND_CGROUP_FDS.set((write_fd,))
    try:
        with pytest.raises(OSError, match="Too many open files"):
            sandbox_daemon._start_command(
                ["true"], env={}, workdir=None, stdin=subprocess.DEVNULL, output=subprocess.DEVNULL, cgroup_procs=[]
            )
    finally:
        sandbox_daemon._COMMAND_CGROUP_FDS.reset(token)
    try:
        assert _write_end_closed(read_fd)
        for fd in pipes:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(read_fd)
