# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A command that kills its own shim or fastpath worker still leaves nothing running.

The command runs as the daemon's uid, so it can SIGKILL the subreaper it
runs under. Its processes then reparent to the pid namespace's init,
out of the daemon's tree, and ``setsid`` ones out of reach of
``killpg``. The daemon then kills every process outside its own tree
and ancestry (``_sweep_orphans``), but only in a pid namespace the
runtime created for the sandbox: on a host the same rule would kill
unrelated processes.

The sweep's rule runs here on a made-up process table. The end-to-end
cases run the real daemon handlers in a private pid namespace from
``unshare``, with a stand-in for bubblewrap's init as pid 1, and capture
the stray descendant with ``psutil`` from the host while it is alive.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import os
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import psutil
import pytest

from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="reads /proc and needs Linux pid namespaces")
_SIGKILL = 9


class _ProcessTable:
    """A process table whose ``kill`` behaves like the kernel's for the sweep.

    A killed process's children reparent to pid 1. ``forks_on_kill`` maps a
    pid to a child it forked after the sweep's scan, which reparents to
    pid 1 when its parent dies. With ``fork_bomb`` every kill leaves a new
    orphan behind.
    """

    def __init__(
        self,
        parents: dict[int, int],
        *,
        forks_on_kill: dict[int, int] | None = None,
        fork_bomb: bool = False,
    ) -> None:
        self.parents = dict(parents)
        self.killed: list[int] = []
        self._forks_on_kill = dict(forks_on_kill or {})
        self._fork_bomb = fork_bomb
        self._next_pid = 1000

    def read(self) -> dict[int, int]:
        return dict(self.parents)

    def kill(self, pid: int, signum: int) -> None:
        assert signum == _SIGKILL
        if pid not in self.parents:
            raise ProcessLookupError(pid)
        self.killed.append(pid)
        del self.parents[pid]
        for child, ppid in list(self.parents.items()):
            if ppid == pid:
                self.parents[child] = 1
        if pid in self._forks_on_kill:
            self.parents[self._forks_on_kill.pop(pid)] = 1
        if self._fork_bomb:
            self._next_pid += 1
            self.parents[self._next_pid] = 1


class _Clock:
    def __init__(self, step: float) -> None:
        self.now = 0.0
        self._step = step
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        value = self.now
        self.now += self._step
        return value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _sweep(table: _ProcessTable, self_pid: int, clock: _Clock | None = None) -> bool:
    from jiuwenbox.supervisor import sandbox_daemon

    clock = clock or _Clock(step=0.0)
    return sandbox_daemon._sweep_namespace_orphans(
        table.read,
        table.kill,
        self_pid,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )


def test_sweep_kills_orphans_and_spares_the_daemon_its_ancestors_and_live_commands() -> None:
    table = _ProcessTable(
        {
            1: 0,  # bubblewrap's init
            5: 1,  # an ancestor between init and the daemon
            2: 5,  # the daemon
            10: 2,  # a running command's live shim
            11: 10,  # its command
            12: 10,  # a setsid descendant the live shim caught
            13: 2,  # a fastpath worker
            14: 13,  # its request
            20: 1,  # orphans of a dead shim
            21: 20,
            22: 21,
            30: 5,  # an orphan that reparented to the nearer ancestor
        }
    )

    assert _sweep(table, self_pid=2) is True

    assert sorted(table.killed) == [20, 21, 22, 30]
    assert sorted(table.parents) == [1, 2, 5, 10, 11, 12, 13, 14]


def test_sweep_runs_rounds_for_processes_forked_while_it_kills() -> None:
    table = _ProcessTable({1: 0, 2: 1, 20: 1}, forks_on_kill={20: 40})
    clock = _Clock(step=0.0)

    assert _sweep(table, self_pid=2, clock=clock) is True

    assert table.killed == [20, 40]
    assert sorted(table.parents) == [1, 2]
    assert len(clock.sleeps) == 2


def test_sweep_stops_after_its_budget() -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    table = _ProcessTable({1: 0, 2: 1, 20: 1}, fork_bomb=True)
    clock = _Clock(step=sandbox_daemon._TREE_KILL_SECONDS / 4)

    assert _sweep(table, self_pid=2, clock=clock) is False

    # The deadline is read once, then once per round after its kills.
    assert len(table.killed) == 4
    assert clock.sleeps == [sandbox_daemon._TREE_KILL_ROUND_SECONDS] * 3
    assert table.parents[2] == 1


def test_sweep_without_a_process_table_kills_nothing() -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    killed: list[int] = []

    def kill(pid: int, signum: int) -> None:
        killed.append(pid)

    assert sandbox_daemon._sweep_namespace_orphans(lambda: None, kill, 2) is False
    table = _ProcessTable({1: 0, 20: 1})
    assert _sweep(table, self_pid=2) is False
    assert killed == []
    assert table.killed == []


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_sweep_is_a_no_op_without_the_namespace_signal(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    calls: list[tuple[Any, ...]] = []

    def record(*args: Any, **kwargs: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(sandbox_daemon, "_sweep_namespace_orphans", record)
    monkeypatch.setenv(sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV, "placeholder")
    if value is None:
        monkeypatch.delenv(sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV)
    else:
        monkeypatch.setenv(sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV, value)

    assert sandbox_daemon._sweep_orphans() is False
    assert calls == []


@_LINUX_ONLY
def test_sweep_runs_with_the_namespace_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.supervisor import sandbox_daemon

    calls: list[tuple[Any, ...]] = []

    def record(*args: Any, **kwargs: Any) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(sandbox_daemon, "_sweep_namespace_orphans", record)
    monkeypatch.setenv(sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV, "1")

    assert sandbox_daemon._sweep_orphans() is True
    assert calls == [(sandbox_daemon._live_parents, os.kill, os.getpid())]


def _last_env_op(argv: list[str], key: str) -> tuple[str, ...] | None:
    last: tuple[str, ...] | None = None
    for index, arg in enumerate(argv):
        if arg == "--setenv" and argv[index + 1] == key:
            last = (arg, argv[index + 2])
        elif arg == "--unsetenv" and argv[index + 1] == key:
            last = (arg,)
    return last


def test_bwrap_claims_a_private_pid_namespace_only_when_it_unshares_one() -> None:
    from jiuwenbox.supervisor.bwrap import BwrapConfig
    from jiuwenbox.supervisor.daemon_ipc import PRIVATE_PID_NAMESPACE_ENV

    private = BwrapConfig(command=["true"], unshare_pid=True, env={PRIVATE_PID_NAMESPACE_ENV: "0"})
    shared = BwrapConfig(command=["true"], unshare_pid=False, env={PRIVATE_PID_NAMESPACE_ENV: "1"})

    private_argv = private.to_args()
    shared_argv = shared.to_args()

    assert "--unshare-pid" in private_argv
    assert _last_env_op(private_argv, PRIVATE_PID_NAMESPACE_ENV) == ("--setenv", "1")
    assert "--unshare-pid" not in shared_argv
    # bwrap passes the host environment through, so an inherited value is removed.
    assert _last_env_op(shared_argv, PRIVATE_PID_NAMESPACE_ENV) == ("--unsetenv",)


def test_daemon_and_runtime_agree_on_the_namespace_signal() -> None:
    from jiuwenbox.supervisor import daemon_ipc, sandbox_daemon

    assert sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV == daemon_ipc.PRIVATE_PID_NAMESPACE_ENV


# --- end to end, in a private pid namespace ---------------------------------

_UNSHARE = ["unshare", "--user", "--map-current-user", "--pid", "--fork", "--mount-proc", "--kill-child"]
# Long enough for the descendant to start and be captured on a loaded
# runner before the daemon's timeout fires.
_EXEC_TIMEOUT = 4

# Under ``unshare`` this starts as pid 1 of the namespace and stands in for
# bubblewrap's init: it reaps whatever reparents to it until its child,
# the daemon (pid 2), exits. Under bubblewrap it already is pid 2. The
# daemon serves the requests in ``argv[1]``, prints a report, and exits
# once its stdin closes; the namespace (and anything left in it) ends
# with pid 1.
_NAMESPACE_DAEMON = r"""
import json, os, socket, struct, sys, threading, time

spec = json.loads(sys.argv[1])
if os.getpid() == 1:
    daemon = os.fork()
    if daemon:
        while True:
            pid, status = os.wait()
            if pid == daemon:
                os._exit(0 if status == 0 else 1)

os.environ.update(spec["daemon_env"])
from jiuwenbox.supervisor import sandbox_daemon as d

WAIT = spec["wait"]


def wait_file(path):
    deadline = time.monotonic() + WAIT
    while not os.path.exists(path):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)
    return True


def recv_exact(sock, size):
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def response(sock):
    sock.settimeout(WAIT)
    (size,) = struct.unpack(">I", recv_exact(sock, 4))
    return json.loads(recv_exact(sock, size))


def serve(handler, header, *extra):
    daemon_end, client = socket.socketpair()
    thread = threading.Thread(target=handler, args=(daemon_end, header) + extra, daemon=True)
    thread.start()
    return client, thread


report = {}
others = [serve(d._handle_exec, header, d.DaemonState()) for header in spec["others"]]
header = spec["header"]
killed_file = header["env"]["KILLED_FILE"]
mode = spec["mode"]
if mode == "timeout":
    client, _thread = serve(d._handle_exec, header, d.DaemonState())
    report["response"] = response(client)
    report["subreaper_killed"] = os.path.exists(killed_file)
elif mode == "cancel":
    client, thread = serve(d._handle_exec, header, d.DaemonState())
    report["subreaper_killed"] = wait_file(killed_file)
    client.close()
    thread.join(WAIT)
    report["handler_done"] = not thread.is_alive()
elif mode == "fastpath":
    client, _thread = serve(d._handle_exec, header, d.DaemonState())
    report["response"] = response(client)
    report["subreaper_killed"] = os.path.exists(killed_file)
    report["fastpath"] = d._FORK_POOL.stats.snapshot()
elif mode == "background":
    client, _thread = serve(d._handle_exec_background, header)
    report["started"] = response(client)
    report["subreaper_killed"] = wait_file(killed_file)
    client, _thread = serve(d._handle_bg_kill, {"job_id": header["job_id"]})
    report["response"] = response(client)
parents = d._live_parents()
# Left behind: anything init (pid 1) holds besides the daemon.
report["stray"] = sorted(pid for pid, ppid in parents.items() if ppid == 1 and pid != os.getpid())
print(json.dumps(report), flush=True)
sys.stdin.read()
"""

# The command starts a ``setsid`` descendant that writes its pid (in the
# namespace), then sleeps and writes the marker unless it is killed first.
# Once the test captured it (ACK_FILE), the command SIGKILLs its shim
# (``$PPID``), records that (KILLED_FILE) and keeps running.
_KILLS_ITS_SHIM = """
setsid sh -c 'echo $$ > "$PID_FILE.tmp" && mv "$PID_FILE.tmp" "$PID_FILE"; sleep 30; echo late > "$MARKER"' \
    </dev/null >/dev/null 2>&1 &
while [ ! -e "$ACK_FILE" ]; do sleep 0.01; done
kill -KILL $PPID
touch "$KILLED_FILE"
sleep 30
"""

# The same for a fastpath request: its parent is the worker.
_KILLS_ITS_WORKER = """
import os, signal, time
worker = os.getppid()
if os.fork() == 0:
    os.setsid()
    null = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    with open(os.environ["PID_FILE"] + ".tmp", "w") as fh:
        fh.write(str(os.getpid()))
    os.rename(os.environ["PID_FILE"] + ".tmp", os.environ["PID_FILE"])
    time.sleep(30)
    open(os.environ["MARKER"], "w").close()
    os._exit(0)
while not os.path.exists(os.environ["ACK_FILE"]):
    time.sleep(0.01)
os.kill(worker, signal.SIGKILL)
open(os.environ["KILLED_FILE"], "w").close()
time.sleep(30)
"""

# Another command that keeps running: a setsid descendant under its live shim.
_KEEPS_RUNNING = """
setsid sh -c 'echo $$ > "$PID_FILE.tmp" && mv "$PID_FILE.tmp" "$PID_FILE"; sleep 30' </dev/null >/dev/null 2>&1 &
sleep 30
"""


def _launch_argv(launcher: str, command: list[str]) -> list[str]:
    """``command`` in a private pid namespace, from ``unshare`` or as the runtime builds bubblewrap's."""
    if launcher == "unshare":
        return [*_UNSHARE, *command]
    from jiuwenbox.supervisor.bwrap import BwrapConfig

    config = BwrapConfig(
        command=command,
        unshare_net=False,
        unshare_ipc=False,
        unshare_cgroup=False,
        unshare_uts=False,
        rw_binds=[("/", "/")],
    )
    return config.to_args()


@functools.cache
def _launcher_works(launcher: str) -> bool:
    if shutil.which(launcher) is None:
        return False
    try:
        probe = subprocess.run(
            _launch_argv(launcher, ["true"]),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=ENGINE_TURN_TIMEOUT,
            check=False,
        )
    except OSError, subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0


def _command_env(tmp_path: Path, name: str) -> dict[str, str]:
    return {
        "PID_FILE": str(tmp_path / f"{name}.pid"),
        "MARKER": str(tmp_path / f"{name}.marker"),
        "ACK_FILE": str(tmp_path / f"{name}.ack"),
        "KILLED_FILE": str(tmp_path / f"{name}.killed"),
    }


def _host_process(root: psutil.Process, ns_pid: int) -> psutil.Process:
    """The descendant of ``root`` whose pid in its own namespace is ``ns_pid``."""
    for proc in root.children(recursive=True):
        try:
            status = Path(f"/proc/{proc.pid}/status").read_text(encoding="utf-8")
        except OSError:
            continue
        for line in status.splitlines():
            if line.startswith("NSpid:") and int(line.split()[-1]) == ns_pid:
                return proc
    raise AssertionError(f"no process with namespace pid {ns_pid} under {root.pid}")


class _NamespaceDaemon:
    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path
        self._log = tmp_path / "daemon.log"
        self._proc: asyncio.subprocess.Process | None = None
        self._root: psutil.Process | None = None
        self.captured: list[psutil.Process] = []

    async def start(
        self,
        mode: str,
        header: dict[str, Any],
        others: list[dict[str, Any]] | None = None,
        *,
        launcher: str = "unshare",
    ) -> None:
        from jiuwenbox.supervisor import sandbox_daemon

        if not await asyncio.to_thread(_launcher_works, launcher):
            pytest.skip(f"{launcher} cannot create unprivileged user and pid namespaces here")
        daemon_env = {sandbox_daemon.FASTPATH_ENV: "1"}
        if launcher == "unshare":
            daemon_env[sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV] = "1"
        spec = {
            "mode": mode,
            "header": header,
            "others": others or [],
            "wait": ENGINE_TURN_TIMEOUT,
            "daemon_env": daemon_env,
        }
        env = dict(os.environ)
        env.pop(sandbox_daemon.PRIVATE_PID_NAMESPACE_ENV, None)
        for key in ("HOME", "USERPROFILE", "APPDATA"):
            env[key] = str(self._tmp_path)
        argv = _launch_argv(launcher, [sys.executable, "-c", _NAMESPACE_DAEMON, json.dumps(spec)])
        log = await asyncio.to_thread(self._log.open, "wb")
        with log:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=log,
                env=env,
                cwd=self._tmp_path,
            )
        self._root = psutil.Process(self._proc.pid)

    async def capture(self, env: dict[str, str]) -> psutil.Process:
        """Capture the descendant that left the command's session while it is alive."""
        root = self._root
        assert root is not None
        pid_file = Path(env["PID_FILE"])
        await wait_for(pid_file.exists, timeout=ENGINE_TURN_TIMEOUT, description=f"{pid_file.name} written")
        proc = _host_process(root, int(pid_file.read_text(encoding="utf-8")))
        self.captured.append(proc)
        assert proc.is_running()
        assert os.getsid(proc.pid) == proc.pid
        return proc

    async def report(self) -> dict[str, Any]:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), timeout=ENGINE_TURN_TIMEOUT)
        if not line:
            raise AssertionError(f"namespace daemon exited: {self._log.read_text(encoding='utf-8', errors='replace')}")
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        return parsed

    async def close(self) -> None:
        proc = self._proc
        if proc is not None and proc.returncode is None:
            if proc.stdin is not None:
                proc.stdin.close()
            try:
                await asyncio.wait_for(proc.wait(), timeout=ENGINE_TURN_TIMEOUT)
            except TimeoutError:
                proc.kill()
                await proc.wait()
        for captured in self.captured:
            with contextlib.suppress(psutil.Error):
                captured.kill()


@pytest.fixture
async def namespace_daemon(tmp_path: Path) -> AsyncIterator[_NamespaceDaemon]:
    if sys.platform != "linux":
        pytest.skip("needs Linux pid namespaces")
    daemon = _NamespaceDaemon(tmp_path)
    try:
        yield daemon
    finally:
        await daemon.close()


async def _assert_gone(proc: psutil.Process, marker: str) -> None:
    def gone() -> bool:
        try:
            return proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running()
        except psutil.NoSuchProcess:
            return True

    await wait_for(gone, timeout=ENGINE_TURN_TIMEOUT, description=f"pid {proc.pid} killed")
    assert not Path(marker).exists()


def _shim_header(env: dict[str, str], **extra: Any) -> dict[str, Any]:
    return {"command": ["sh", "-c", _KILLS_ITS_SHIM], "env": env, **extra}


async def _ack(env: dict[str, str]) -> None:
    await asyncio.to_thread(Path(env["ACK_FILE"]).touch)


# Under bubblewrap the daemon learns about its pid namespace only from
# ``BwrapConfig.to_args``, as in a real sandbox.
@pytest.mark.parametrize("launcher", ["unshare", "bwrap"])
async def test_timeout_kills_a_setsid_descendant_after_the_command_killed_its_shim(
    namespace_daemon: _NamespaceDaemon, tmp_path: Path, launcher: str
) -> None:
    env = _command_env(tmp_path, "timeout")
    await namespace_daemon.start("timeout", _shim_header(env, timeout=_EXEC_TIMEOUT), launcher=launcher)
    descendant = await namespace_daemon.capture(env)
    await _ack(env)

    report = await namespace_daemon.report()

    assert report["subreaper_killed"] is True
    assert report["response"]["exit_code"] == 124
    assert report["response"]["error"] == "timeout"
    await _assert_gone(descendant, env["MARKER"])
    assert report["stray"] == []


async def test_cancel_kills_a_setsid_descendant_after_the_command_killed_its_shim(
    namespace_daemon: _NamespaceDaemon, tmp_path: Path
) -> None:
    env = _command_env(tmp_path, "cancel")
    await namespace_daemon.start("cancel", _shim_header(env))
    descendant = await namespace_daemon.capture(env)
    await _ack(env)

    report = await namespace_daemon.report()

    assert report["subreaper_killed"] is True
    assert report["handler_done"] is True
    await _assert_gone(descendant, env["MARKER"])
    assert report["stray"] == []


async def test_sweep_leaves_another_running_command_alone(namespace_daemon: _NamespaceDaemon, tmp_path: Path) -> None:
    other_env = _command_env(tmp_path, "other")
    env = _command_env(tmp_path, "swept")
    other = {"command": ["sh", "-c", _KEEPS_RUNNING], "env": other_env}
    await namespace_daemon.start("timeout", _shim_header(env, timeout=_EXEC_TIMEOUT), others=[other])
    other_descendant = await namespace_daemon.capture(other_env)
    descendant = await namespace_daemon.capture(env)
    await _ack(env)

    report = await namespace_daemon.report()

    assert report["subreaper_killed"] is True
    assert report["response"]["exit_code"] == 124
    await _assert_gone(descendant, env["MARKER"])
    assert report["stray"] == []
    assert other_descendant.is_running()
    assert other_descendant.status() != psutil.STATUS_ZOMBIE


async def test_background_kill_ends_a_setsid_descendant_after_the_job_killed_its_shim(
    namespace_daemon: _NamespaceDaemon, tmp_path: Path
) -> None:
    env = _command_env(tmp_path, "background")
    await namespace_daemon.start("background", _shim_header(env, job_id="orphaned-job"))
    descendant = await namespace_daemon.capture(env)
    await _ack(env)

    report = await namespace_daemon.report()

    assert report["started"]["ok"] is True
    assert report["subreaper_killed"] is True
    # The job already counts as exited: its shim died.
    assert report["response"]["reason"] == "already_exited"
    await _assert_gone(descendant, env["MARKER"])
    assert report["stray"] == []


async def test_fastpath_kills_a_setsid_descendant_after_the_request_killed_its_worker(
    namespace_daemon: _NamespaceDaemon, tmp_path: Path
) -> None:
    env = _command_env(tmp_path, "fastpath")
    header = {"command": ["python3", "-c", _KILLS_ITS_WORKER], "env": env, "python_fastpath": True}
    await namespace_daemon.start("fastpath", header)
    descendant = await namespace_daemon.capture(env)
    await _ack(env)

    report = await namespace_daemon.report()

    assert report["subreaper_killed"] is True
    assert report["fastpath"]["requests"] == 1
    assert report["response"]["error"] == "fastpath_exec_uncertain"
    await _assert_gone(descendant, env["MARKER"])
    assert report["stray"] == []
