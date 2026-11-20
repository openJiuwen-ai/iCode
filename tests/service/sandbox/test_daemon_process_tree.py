# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ending an exec kills every process it started, and only those, without a cgroup.

These run the real in-sandbox daemon handlers on this host, with no
cgroup: each command starts under the subreaper shim (or a fastpath
worker). The commands daemonize a grandchild (fork, ``setsid``, fork
again), which leaves the command's session and process group, so
``killpg`` cannot reach it. Each test captures that grandchild with
``psutil`` while it is alive and requires it to be gone once the exec
has answered; a survivor would also write its marker file later.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import struct
import sys
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="the tree kill reads /proc and needs PR_SET_CHILD_SUBREAPER",
)

# Long enough for the grandchild to start and be captured on a loaded
# runner before the daemon's timeout fires.
_EXEC_TIMEOUT = 4

# The grandchild redirects its stdio to /dev/null, so it holds none of the
# exec's pipes, writes its pid and the leader's session id, then sleeps and
# writes the marker unless it is killed first. Once that pid is written the
# leader sleeps ``LEADER_SLEEP`` seconds. Inputs come from the environment
# so the fastpath can run the same code.
_DAEMONIZE = """
import os, time
pid_file = os.environ["PID_FILE"]
leader_sid = os.getsid(0)
if os.fork() == 0:
    os.setsid()
    if os.fork() == 0:
        null = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(null, fd)
        with open(pid_file + ".tmp", "w") as fh:
            fh.write("%d %d" % (os.getpid(), leader_sid))
        os.rename(pid_file + ".tmp", pid_file)
        time.sleep(30)
        open(os.environ["MARKER"], "w").close()
        os._exit(0)
    os._exit(0)
os.wait()
# Ending the exec kills the grandchild: wait until it recorded its pid,
# or until the test has captured it (ACK_FILE).
while not os.path.exists(os.environ.get("ACK_FILE") or pid_file):
    time.sleep(0.01)
print("leader", flush=True)
time.sleep(float(os.environ["LEADER_SLEEP"]))
"""


def _daemonize_header(tmp_path: Path, name: str, *, leader_sleep: float, ack: bool = False) -> dict[str, Any]:
    env = {
        "PID_FILE": str(tmp_path / f"{name}.pid"),
        "MARKER": str(tmp_path / f"{name}.marker"),
        "LEADER_SLEEP": str(leader_sleep),
    }
    if ack:
        # The leader exits only after the test captured the grandchild.
        env["ACK_FILE"] = str(tmp_path / f"{name}.ack")
    return {"command": ["python3", "-c", _DAEMONIZE], "env": env}


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    buf = bytearray()
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise AssertionError(f"socket closed after {len(buf)}/{size} bytes")
        buf.extend(chunk)
    return bytes(buf)


def _read_response(sock: socket.socket) -> dict[str, Any]:
    sock.settimeout(ENGINE_TURN_TIMEOUT)
    (size,) = struct.unpack(">I", _recv_exact(sock, 4))
    parsed = json.loads(_recv_exact(sock, size).decode("utf-8"))
    if not isinstance(parsed, dict):
        raise AssertionError(f"response was not an object: {parsed!r}")
    return parsed


def _call_now(handler: Callable[[socket.socket, dict[str, Any]], None], header: dict[str, Any]) -> dict[str, Any]:
    """Serve one request that answers without waiting (``bg_status``) on this thread."""
    daemon_end, client = socket.socketpair()
    with daemon_end, client:
        handler(daemon_end, header)
        return _read_response(client)


class _Exec:
    """One request served by a daemon handler on its own thread."""

    def __init__(self, handler: Callable[..., None], header: dict[str, Any], *extra: object) -> None:
        self._daemon_end, self.client = socket.socketpair()
        self.done = threading.Event()
        self.errors: list[BaseException] = []

        def run() -> None:
            try:
                handler(self._daemon_end, header, *extra)
            except BaseException as exc:
                self.errors.append(exc)
            finally:
                self.done.set()

        self._thread = threading.Thread(target=run, name="daemon-request", daemon=True)
        self._thread.start()

    async def response(self) -> dict[str, Any]:
        return await asyncio.to_thread(_read_response, self.client)

    def drop_client(self) -> None:
        with contextlib.suppress(OSError):
            self.client.close()

    async def close(self) -> None:
        self.drop_client()
        await asyncio.to_thread(self._thread.join, ENGINE_TURN_TIMEOUT)
        with contextlib.suppress(OSError):
            self._daemon_end.close()


class _Harness:
    def __init__(self) -> None:
        self.requests: list[_Exec] = []
        self.captured: list[psutil.Process] = []

    def exec(self, header: dict[str, Any]) -> _Exec:
        from jiuwenbox.supervisor import sandbox_daemon

        request = _Exec(sandbox_daemon._handle_exec, header, sandbox_daemon.DaemonState())
        self.requests.append(request)
        return request

    def call(self, handler: Callable[..., None], header: dict[str, Any]) -> _Exec:
        request = _Exec(handler, header)
        self.requests.append(request)
        return request

    async def capture(self, pid_file: str) -> psutil.Process:
        path = Path(pid_file)
        await wait_for(path.exists, timeout=ENGINE_TURN_TIMEOUT, description=f"{path.name} written")
        pid, leader_sid = (int(field) for field in path.read_text(encoding="utf-8").split())
        proc = psutil.Process(pid)
        self.captured.append(proc)
        assert proc.is_running()
        # It left the command's session (and so its process group): only
        # the tree kill reaches it.
        assert os.getsid(pid) != leader_sid
        return proc


@pytest.fixture
async def harness() -> AsyncIterator[_Harness]:
    harness = _Harness()
    try:
        yield harness
    finally:
        for request in harness.requests:
            await request.close()
        for proc in harness.captured:
            with contextlib.suppress(psutil.Error):
                proc.kill()


def _assert_gone(proc: psutil.Process, marker: str) -> None:
    assert not proc.is_running()
    assert not Path(marker).exists()


async def test_timeout_kills_a_child_that_keeps_the_pipes_after_its_leader_exits(
    harness: _Harness, tmp_path: Path
) -> None:
    pid_file = tmp_path / "child.pid"
    request = harness.exec(
        {
            "command": [
                "sh",
                "-c",
                'sleep 30 & echo $! > "$PID_FILE.tmp" && mv "$PID_FILE.tmp" "$PID_FILE"; exit 0',
            ],
            "env": {"PID_FILE": str(pid_file)},
            "timeout": _EXEC_TIMEOUT,
        }
    )
    await wait_for(pid_file.exists, timeout=ENGINE_TURN_TIMEOUT, description="child pid written")
    child = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
    harness.captured.append(child)

    response = await request.response()

    assert response["exit_code"] == 124
    assert response["error"] == "timeout"
    assert not child.is_running()
    assert request.errors == []


async def test_timeout_kills_a_setsid_descendant(harness: _Harness, tmp_path: Path) -> None:
    header = _daemonize_header(tmp_path, "timeout", leader_sleep=30)
    header["timeout"] = _EXEC_TIMEOUT
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])

    response = await request.response()

    assert response["exit_code"] == 124
    assert response["stdout"] == "leader\n"
    _assert_gone(grandchild, header["env"]["MARKER"])
    assert request.errors == []


async def test_cancel_kills_a_setsid_descendant(harness: _Harness, tmp_path: Path) -> None:
    header = _daemonize_header(tmp_path, "cancel", leader_sleep=30)
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])
    assert not request.done.is_set()

    request.drop_client()
    await wait_for(request.done.is_set, timeout=ENGINE_TURN_TIMEOUT, description="cancelled exec ended")

    _assert_gone(grandchild, header["env"]["MARKER"])
    assert request.errors == []


async def test_finished_exec_leaves_no_daemonized_descendant(harness: _Harness, tmp_path: Path) -> None:
    header = _daemonize_header(tmp_path, "finished", leader_sleep=0, ack=True)
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])
    Path(header["env"]["ACK_FILE"]).touch()

    response = await request.response()

    assert response["exit_code"] == 0
    assert response["stdout"] == "leader\n"
    _assert_gone(grandchild, header["env"]["MARKER"])


async def test_timeout_leaves_another_command_running(harness: _Harness, tmp_path: Path) -> None:
    other_header = _daemonize_header(tmp_path, "other", leader_sleep=30)
    other = harness.exec(other_header)
    other_grandchild = await harness.capture(other_header["env"]["PID_FILE"])
    header = _daemonize_header(tmp_path, "timed", leader_sleep=30)
    header["timeout"] = _EXEC_TIMEOUT
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])

    response = await request.response()

    assert response["exit_code"] == 124
    _assert_gone(grandchild, header["env"]["MARKER"])
    assert other_grandchild.is_running()
    assert not other.done.is_set()

    other.drop_client()
    await wait_for(other.done.is_set, timeout=ENGINE_TURN_TIMEOUT, description="other exec ended")
    _assert_gone(other_grandchild, other_header["env"]["MARKER"])


async def test_spawn_failure_reports_the_exec_error(harness: _Harness) -> None:
    request = harness.exec({"command": ["jiuwenbox-no-such-command"]})

    response = await request.response()

    assert response["ok"] is False
    assert response["exit_code"] == 127
    assert response["error"] == "spawn_failed"
    assert response["stderr"] == (
        "failed to spawn command: [Errno 2] No such file or directory: 'jiuwenbox-no-such-command'"
    )


async def test_exit_status_stdin_and_output_pass_through_the_shim(harness: _Harness) -> None:
    payload = b"fed through stdin"
    exited = harness.exec({"command": ["sh", "-c", "cat; echo err >&2; exit 7"], "stdin_size": len(payload)})
    exited.client.sendall(payload)
    signaled = harness.exec({"command": ["sh", "-c", "kill -TERM $$"]})

    exited_response = await exited.response()
    signaled_response = await signaled.response()

    assert exited_response["exit_code"] == 7
    assert exited_response["stdout"] == payload.decode()
    assert exited_response["stderr"] == "err\n"
    assert signaled_response["exit_code"] == -15


async def test_background_kill_ends_a_setsid_descendant(harness: _Harness, tmp_path: Path) -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    job_id = f"tree-kill-{tmp_path.name}"
    header = _daemonize_header(tmp_path, "bg-kill", leader_sleep=30)
    header["job_id"] = job_id
    try:
        started = await harness.call(sandbox_daemon._handle_exec_background, header).response()
        assert started["ok"] is True
        assert started["running"] is True
        leader = psutil.Process(started["pid"])
        harness.captured.append(leader)
        grandchild = await harness.capture(header["env"]["PID_FILE"])

        killed = await harness.call(sandbox_daemon._handle_bg_kill, {"job_id": job_id}).response()

        assert killed["killed"] is True
        assert killed["reason"] == "ok"
        assert not leader.is_running()
        _assert_gone(grandchild, header["env"]["MARKER"])
    finally:
        job = sandbox_daemon._bg_jobs.pop(job_id, None)
        if isinstance(job, sandbox_daemon._BgJob):
            job.proc.finish()


async def test_background_job_reported_stopped_leaves_no_descendant(harness: _Harness, tmp_path: Path) -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    job_id = f"tree-stopped-{tmp_path.name}"
    header = _daemonize_header(tmp_path, "bg-stopped", leader_sleep=0, ack=True)
    header["job_id"] = job_id
    try:
        started = await harness.call(sandbox_daemon._handle_exec_background, header).response()
        assert started["ok"] is True
        grandchild = await harness.capture(header["env"]["PID_FILE"])
        Path(header["env"]["ACK_FILE"]).touch()
        status: dict[str, Any] = {}

        def stopped() -> bool:
            status.update(_call_now(sandbox_daemon._handle_bg_status, {"job_id": job_id}))
            return status["running"] is False

        await wait_for(stopped, timeout=ENGINE_TURN_TIMEOUT, description="background job reported stopped")

        assert status["exit_code"] == 0
        _assert_gone(grandchild, header["env"]["MARKER"])
    finally:
        job = sandbox_daemon._bg_jobs.pop(job_id, None)
        if isinstance(job, sandbox_daemon._BgJob):
            job.proc.finish()


@pytest.fixture
def fastpath_pool(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from jiuwenbox.supervisor import sandbox_daemon

    monkeypatch.setenv(sandbox_daemon.FASTPATH_ENV, "1")
    pool = sandbox_daemon.ForkServerPool()
    monkeypatch.setattr(sandbox_daemon, "_FORK_POOL", pool)
    try:
        yield pool
    finally:
        pool.shutdown()


async def test_fastpath_timeout_kills_a_setsid_descendant(
    harness: _Harness, tmp_path: Path, fastpath_pool: Any
) -> None:
    header = _daemonize_header(tmp_path, "fast-timeout", leader_sleep=30)
    header["timeout"] = _EXEC_TIMEOUT
    header["python_fastpath"] = True
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])

    response = await request.response()

    assert fastpath_pool.stats.snapshot()["hits"] == 1
    assert response["exit_code"] == 124
    _assert_gone(grandchild, header["env"]["MARKER"])


async def test_fastpath_cancel_kills_a_setsid_descendant(harness: _Harness, tmp_path: Path, fastpath_pool: Any) -> None:
    header = _daemonize_header(tmp_path, "fast-cancel", leader_sleep=30)
    header["python_fastpath"] = True
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])

    request.drop_client()
    await wait_for(request.done.is_set, timeout=ENGINE_TURN_TIMEOUT, description="cancelled exec ended")

    assert fastpath_pool.stats.snapshot()["hits"] == 1
    _assert_gone(grandchild, header["env"]["MARKER"])


async def test_fastpath_finished_exec_leaves_no_daemonized_descendant(
    harness: _Harness, tmp_path: Path, fastpath_pool: Any
) -> None:
    header = _daemonize_header(tmp_path, "fast-finished", leader_sleep=0, ack=True)
    header["python_fastpath"] = True
    request = harness.exec(header)
    grandchild = await harness.capture(header["env"]["PID_FILE"])
    Path(header["env"]["ACK_FILE"]).touch()

    response = await request.response()

    assert fastpath_pool.stats.snapshot()["hits"] == 1
    assert response["exit_code"] == 0
    assert response["stdout"] == "leader\n"
    _assert_gone(grandchild, header["env"]["MARKER"])
