# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancel and timeout each kill one foreground command's process group.

The commands are in-process fakes. Nothing starts jiuwenbox, bubblewrap,
or a real grandchild: Windows cannot signal a process group, so the tests
patch ``os.killpg`` and hold the pipe write ends the way a descendant
would. They fail if the daemon only counts in-flight execs, signals
something other than that command's pid, or drops stdout already read.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable

import pytest

_SIGKILL = getattr(signal, "SIGKILL", 9)
_KEPT_STDOUT = b"kept-output\n"


class _HoldingProc:
    """A Popen stand-in whose pipes stay open until the group is released.

    ``communicate`` drops partial reads and does not notice ``killpg``.
    That is the old timeout path: a test that still ends up there sees
    empty stdout and no group signal.
    """

    def __init__(self, pid: int, stdout: bytes, *, running: bool) -> None:
        self.pid = pid
        self.args = ["fake", str(pid)]
        self.returncode: int | None = None if running else 0
        self.stdin = None
        self.direct_killed = False
        self.group_signaled = False
        out_read, out_write = os.pipe()
        err_read, err_write = os.pipe()
        if stdout:
            os.write(out_write, stdout)
        self._write_fds = [out_write, err_write]
        self.stdout = os.fdopen(out_read, "rb", buffering=0)
        self.stderr = os.fdopen(err_read, "rb", buffering=0)
        self._released = threading.Event()
        if not running:
            self._close_writes()
            self._released.set()

    @property
    def pipes_open(self) -> bool:
        return bool(self._write_fds)

    def _close_writes(self) -> None:
        for fd in self._write_fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._write_fds.clear()

    def release_group(self) -> None:
        """Close the write ends, as a killed process group would."""
        self.group_signaled = True
        if self.returncode is None:
            self.returncode = -9
        self._close_writes()
        self._released.set()

    def note_group_signal(self) -> None:
        """Record the group signal and leave the pipes open.

        Descendants that still hold stdout keep the drain from seeing EOF.
        Bytes the daemon has already read must still be returned.
        """
        self.group_signaled = True
        if self.returncode is None:
            self.returncode = -9

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.direct_killed = True

    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        del input
        deadline = None if timeout is None else time.monotonic() + float(timeout)
        while self.returncode is None and not self._released.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self.args, timeout)
            remaining = 0.05
            if deadline is not None:
                remaining = min(remaining, max(0.0, deadline - time.monotonic()))
            if self._released.wait(remaining):
                break
        return b"", b""

    def close(self) -> None:
        self.release_group()
        for stream in (self.stdout, self.stderr):
            with contextlib.suppress(OSError):
                stream.close()


class _ExecSession:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, on_signal: Callable[[_HoldingProc], None]) -> None:
        from jiuwenbox.supervisor import sandbox_daemon

        self.daemon = sandbox_daemon
        self.calls: list[tuple[int, int]] = []
        self.procs: dict[tuple[str, ...], _HoldingProc] = {}
        self._ready: dict[tuple[str, ...], threading.Event] = {}
        self._specs: dict[tuple[str, ...], tuple[int, bytes, bool]] = {}
        self.threads: list[threading.Thread] = []
        self.sockets: list[socket.socket] = []
        self.errors: list[BaseException] = []
        self._on_signal = on_signal

        def killpg(pid: int, sig: int) -> None:
            self.calls.append((pid, sig))
            proc = next(item for item in self.procs.values() if item.pid == pid)
            self._on_signal(proc)

        def popen(command: list[str], **_kwargs: object) -> _HoldingProc:
            key = tuple(command)
            pid, stdout, running = self._specs[key]
            proc = _HoldingProc(pid, stdout, running=running)
            self.procs[key] = proc
            self._ready[key].set()
            return proc

        monkeypatch.setattr(sandbox_daemon.os, "killpg", killpg, raising=False)
        monkeypatch.setattr(sandbox_daemon.subprocess, "Popen", popen)

    def start(
        self,
        command: list[str],
        *,
        pid: int,
        stdout: bytes,
        running: bool,
        timeout: float | None,
    ) -> tuple[socket.socket, threading.Event]:
        key = tuple(command)
        self._specs[key] = (pid, stdout, running)
        self._ready[key] = threading.Event()
        daemon_end, client_end = socket.socketpair()
        self.sockets.extend((daemon_end, client_end))
        header: dict[str, object] = {"command": list(command)}
        if timeout is not None:
            header["timeout"] = timeout
        done = threading.Event()

        def run() -> None:
            try:
                self.daemon._handle_exec(daemon_end, header, self.daemon.DaemonState())
            except Exception as exc:
                self.errors.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=run, name=f"exec-{command[0]}")
        self.threads.append(thread)
        thread.start()
        return client_end, done

    def wait_ready(self, command: list[str]) -> _HoldingProc:
        key = tuple(command)
        if not self._ready[key].wait(3):
            raise AssertionError(f"{command} was not spawned")
        return self.procs[key]

    def close(self) -> None:
        for proc in self.procs.values():
            proc.release_group()
        for thread in self.threads:
            thread.join(timeout=2)
        for sock in self.sockets:
            with contextlib.suppress(OSError):
                sock.close()
        for proc in self.procs.values():
            proc.close()


def _read_response(sock: socket.socket) -> dict[str, object]:
    sock.settimeout(3)
    header = _recv_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    body = _recv_exact(sock, size)
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"response was not an object: {parsed!r}")
    return parsed


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise AssertionError(f"socket closed after {len(buf)}/{size} bytes")
        buf.extend(chunk)
    return bytes(buf)


def _assert_group_signal(calls: list[tuple[int, int]], pid: int) -> None:
    assert calls, "command process group was not signaled"
    assert calls[0][0] == pid
    assert calls[0][1] == _SIGKILL
    assert pid != os.getpid()
    if hasattr(os, "getpgrp"):
        assert pid != os.getpgrp()


def test_cancel_kills_only_that_command_group(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _ExecSession(monkeypatch, on_signal=_HoldingProc.release_group)
    try:
        first_client, first_done = session.start(
            ["hold-a"],
            pid=71001,
            stdout=b"first\n",
            running=True,
            timeout=None,
        )
        _second_client, second_done = session.start(
            ["hold-b"],
            pid=71002,
            stdout=b"second\n",
            running=True,
            timeout=None,
        )
        first = session.wait_ready(["hold-a"])
        second = session.wait_ready(["hold-b"])
        first_client.close()

        assert first_done.wait(3), "cancelled exec did not finish"
        _assert_group_signal(session.calls, first.pid)
        assert [pid for pid, _sig in session.calls] == [first.pid]
        assert first.group_signaled is True
        assert second.group_signaled is False
        assert second.returncode is None
        assert second.pipes_open is True
        assert not second_done.is_set()
        assert session.errors == []
    finally:
        session.close()


def test_timeout_kills_command_group_and_keeps_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    # The real drain is 5s. Shorten only this wait: the pipes stay open, so
    # the daemon must return the bytes it already read when the drain ends.
    monkeypatch.setattr(sandbox_daemon, "_POST_KILL_DRAIN_SECONDS", 0.2)
    session = _ExecSession(monkeypatch, on_signal=_HoldingProc.note_group_signal)
    try:
        _second_client, second_done = session.start(
            ["hold-b"],
            pid=71012,
            stdout=b"other\n",
            running=True,
            timeout=None,
        )
        second = session.wait_ready(["hold-b"])
        first_client, first_done = session.start(
            ["hold-a"],
            pid=71011,
            stdout=_KEPT_STDOUT,
            running=True,
            timeout=0.4,
        )
        first = session.wait_ready(["hold-a"])

        assert first_done.wait(3), "timed-out exec did not finish"
        response = _read_response(first_client)
        _assert_group_signal(session.calls, first.pid)
        assert [pid for pid, _sig in session.calls] == [first.pid]
        assert response["exit_code"] == 124
        assert response["stdout"] == _KEPT_STDOUT.decode("utf-8")
        assert response["error"] == "timeout"
        stderr = response["stderr"]
        assert isinstance(stderr, str)
        assert "Command timed out" in stderr
        assert first.pipes_open is True
        assert second.group_signaled is False
        assert second.returncode is None
        assert second.pipes_open is True
        assert not second_done.is_set()
        assert session.errors == []
    finally:
        session.close()


def test_completed_exec_returns_stdout_without_a_group_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _ExecSession(monkeypatch, on_signal=_HoldingProc.release_group)
    try:
        client, done = session.start(
            ["echo-ok"],
            pid=71021,
            stdout=b"done\n",
            running=False,
            timeout=5,
        )
        session.wait_ready(["echo-ok"])
        assert done.wait(3), "finished exec did not return"
        response = _read_response(client)
        assert response["exit_code"] == 0
        assert response["stdout"] == "done\n"
        assert session.calls == []
        assert session.errors == []
    finally:
        session.close()


@pytest.mark.skipif(
    sys.platform != "linux",
    reason="write shutdown stays distinct from a full close only on Linux poll",
)
def test_write_shutdown_does_not_kill_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    session = _ExecSession(monkeypatch, on_signal=_HoldingProc.release_group)
    try:
        client, done = session.start(
            ["hold-c"],
            pid=71031,
            stdout=b"still-running\n",
            running=True,
            timeout=None,
        )
        proc = session.wait_ready(["hold-c"])
        client.shutdown(socket.SHUT_WR)
        assert not done.wait(0.3)
        assert session.calls == []
        assert proc.group_signaled is False
        assert proc.returncode is None
        assert session.errors == []
    finally:
        session.close()
