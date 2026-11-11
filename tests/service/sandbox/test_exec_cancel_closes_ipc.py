# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancelling one exec closes only that call's daemon IPC socket.

The daemon killpg's a command when its IPC connection fully closes. These
tests drive ``ProcessRuntime`` with ``socket.socketpair`` and never start
bubblewrap or a process group, so they run on Windows. A regression that
leaves the cancelled call's socket open fails the wait for that close.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct
import sys
import threading
import types
from pathlib import Path

import pytest

from tests.support.waiting import wait_for

_SANDBOX_ID = "sb-cancel"
_CANCELLED = ("sleep", "one")
_KEPT = ("sleep", "two")
_ROUTE = ("sleep", "route")


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
        import socket

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
        # ``shutdown(SHUT_WR)`` already makes the peer read empty. Only
        # ``close()`` drops this end's fd, which is what the daemon treats
        # as a full disconnect.
        return self.local.fileno() == -1

    def close(self) -> None:
        for sock in (self.local, self.peer):
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


async def test_http_disconnect_closes_only_that_exec_socket(
    jiuwenbox_importable: None,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jiuwenbox.models.sandbox import SandboxPhase, SandboxRef
    from jiuwenbox.server.policy_engine import PolicyEngine
    from jiuwenbox.server.routes import sandbox as sandbox_routes
    from jiuwenbox.server.routes.sandbox import ExecRequest
    from jiuwenbox.server.sandbox_manager import SandboxManager
    from starlette.responses import Response

    harness = _Harness(monkeypatch)
    _arm_daemon(harness, tmp_path)

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

    async def _restart(sandbox_id: str) -> object:
        raise AssertionError(f"cancel restarted {sandbox_id}")

    monkeypatch.setattr(manager, "restart_sandbox", _restart)
    monkeypatch.setattr(sandbox_routes, "_mgr", lambda: manager)

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
