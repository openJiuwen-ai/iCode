# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The low-disk watcher stops what writes to a nearly full workspace.

A sandbox with a cgroup loses only its running commands. A sandbox without
one is killed whole, once per lifecycle. The unit tests fake the free-space
probe and the kill; the Linux tests kill real process trees, one without a
PID namespace and one inside bubblewrap's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import subprocess
import sys
import types
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import pytest

from tests.support.waiting import wait_for

if TYPE_CHECKING:
    import psutil
    from jiuwenbox.server.runtime.process import ProcessRuntime

_SANDBOX_ID = "sb-disk"
_BIND = "/work"
_LINUX_ONLY = pytest.mark.skipif(sys.platform != "linux", reason="process trees are read from Linux /proc")


@pytest.fixture
def jiuwenbox_importable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the vendored server import on Windows.

    ``pwd`` and ``grp`` are imported at module scope. The stubs only exist
    so those imports succeed; no account lookup runs.
    """
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


class _Daemon:
    """The part of the daemon's ``Popen`` the watcher reads."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


def _set_free_bytes(monkeypatch: pytest.MonkeyPatch, free: int) -> None:
    from jiuwenbox.supervisor import disk_floor

    monkeypatch.setattr(disk_floor, "free_bytes", lambda _path: free)


def _runtime(daemon: Any) -> ProcessRuntime:
    from jiuwenbox.server.runtime.process import ProcessRuntime

    runtime = ProcessRuntime()
    runtime._policy_binds[_SANDBOX_ID] = [{"host_path": _BIND, "sandbox_path": _BIND}]
    runtime._daemon_socket_ready[_SANDBOX_ID] = True
    runtime._processes[_SANDBOX_ID] = daemon
    return runtime


def _record_calls(monkeypatch: pytest.MonkeyPatch) -> tuple[list[int], list[object]]:
    from jiuwenbox.server.runtime import process as process_module

    tree_kills: list[int] = []
    command_kills: list[object] = []
    monkeypatch.setattr(process_module, "_kill_sandbox_tree", tree_kills.append)
    monkeypatch.setattr(process_module.cgroup_module, "kill_live_commands", command_kills.append)
    return tree_kills, command_kills


def test_low_disk_without_a_cgroup_kills_the_sandbox_once(
    jiuwenbox_importable: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    tree_kills, command_kills = _record_calls(monkeypatch)
    _set_free_bytes(monkeypatch, 10)
    daemon = _Daemon(4321)
    runtime = _runtime(daemon)

    with caplog.at_level(logging.WARNING):
        runtime._enforce_disk_floor()
        runtime._enforce_disk_floor()

    assert tree_kills == [4321]
    assert command_kills == []
    assert runtime._daemon_socket_ready[_SANDBOX_ID] is False
    warnings = [record for record in caplog.records if _SANDBOX_ID in record.getMessage()]
    assert len(warnings) == 1

    daemon.returncode = -9
    runtime._enforce_disk_floor()
    assert tree_kills == [4321]

    runtime._processes[_SANDBOX_ID] = _Daemon(5678)  # type: ignore[assignment]
    runtime._enforce_disk_floor()
    assert tree_kills == [4321], "a daemon still starting up is left alone"
    runtime._daemon_socket_ready[_SANDBOX_ID] = True
    runtime._enforce_disk_floor()
    assert tree_kills == [4321, 5678]


def test_low_disk_with_a_cgroup_kills_only_the_commands(
    jiuwenbox_importable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenbox.supervisor.cgroup import CgroupBackend, CgroupHandle

    tree_kills, command_kills = _record_calls(monkeypatch)
    _set_free_bytes(monkeypatch, 10)
    runtime = _runtime(_Daemon(4321))
    handle = CgroupHandle(backend=CgroupBackend.V2, sandbox_id=_SANDBOX_ID, paths=[], supervisor_paths=[])
    runtime._cgroup_handles[_SANDBOX_ID] = handle

    runtime._enforce_disk_floor()
    runtime._enforce_disk_floor()

    assert command_kills == [handle, handle]
    assert tree_kills == []
    assert runtime._daemon_socket_ready[_SANDBOX_ID] is True


def test_enough_space_stops_nothing(jiuwenbox_importable: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenbox.supervisor import disk_floor

    tree_kills, command_kills = _record_calls(monkeypatch)
    _set_free_bytes(monkeypatch, disk_floor.DISK_FREE_FLOOR_BYTES)
    runtime = _runtime(_Daemon(4321))

    runtime._enforce_disk_floor()

    assert tree_kills == []
    assert command_kills == []
    assert runtime._daemon_socket_ready[_SANDBOX_ID] is True


@pytest.fixture
def owned_trees() -> Iterator[list[subprocess.Popen[bytes]]]:
    """Kill whatever a real-process test started, even when it fails."""
    import psutil

    roots: list[subprocess.Popen[bytes]] = []
    yield roots
    for root in roots:
        if root.poll() is not None:
            continue
        try:
            leader = psutil.Process(root.pid)
            members = [leader, *leader.children(recursive=True)]
        except psutil.NoSuchProcess:
            members = []
        for member in members:
            with contextlib.suppress(psutil.NoSuchProcess):
                member.kill()
        root.wait(timeout=10)


def _gone(proc: psutil.Process) -> bool:
    import psutil

    try:
        return proc.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


async def _detached_command(root: subprocess.Popen[bytes]) -> psutil.Process:
    """Wait for the ``setsid`` sleep under ``root`` and return it."""
    import psutil

    found: list[psutil.Process] = []

    def _probe() -> bool:
        try:
            children = psutil.Process(root.pid).children(recursive=True)
            found[:] = [child for child in children if child.name() == "sleep"]
            return bool(found) and os.getsid(found[0].pid) != os.getsid(root.pid)
        except psutil.NoSuchProcess, ProcessLookupError:
            return False

    await wait_for(_probe, description="the detached sleep started")
    return found[0]


async def _kill_tree_on_low_disk(
    monkeypatch: pytest.MonkeyPatch,
    owned_trees: list[subprocess.Popen[bytes]],
    argv: list[str],
) -> None:
    root = await asyncio.to_thread(
        subprocess.Popen,
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    owned_trees.append(root)
    command = await _detached_command(root)
    _set_free_bytes(monkeypatch, 10)
    runtime = _runtime(root)

    await asyncio.to_thread(runtime._enforce_disk_floor)

    await wait_for(lambda: _gone(command), description="the detached command was killed")
    assert await asyncio.to_thread(root.wait, 10) == -9


@_LINUX_ONLY
async def test_low_disk_kill_reaches_a_detached_command_without_a_pid_namespace(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
    owned_trees: list[subprocess.Popen[bytes]],
) -> None:
    """``setsid`` leaves the daemon's process group, so a group kill alone misses it."""
    await _kill_tree_on_low_disk(monkeypatch, owned_trees, ["sh", "-c", "setsid sleep 300 & wait"])


def _bwrap_pid_namespace_argv() -> list[str] | None:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return None
    argv = [
        bwrap,
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--unshare-user",
        "--unshare-pid",
        "--die-with-parent",
    ]  # fmt: skip
    probe = subprocess.run([*argv, "true"], stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=30)
    return argv if probe.returncode == 0 else None


@_LINUX_ONLY
async def test_low_disk_kill_ends_a_command_inside_a_bwrap_pid_namespace(
    jiuwenbox_importable: None,
    monkeypatch: pytest.MonkeyPatch,
    owned_trees: list[subprocess.Popen[bytes]],
) -> None:
    argv = await asyncio.to_thread(_bwrap_pid_namespace_argv)
    if argv is None:
        pytest.skip("bubblewrap cannot create a user and PID namespace here")
    await _kill_tree_on_low_disk(monkeypatch, owned_trees, [*argv, "sh", "-c", "setsid sleep 300 & wait"])
