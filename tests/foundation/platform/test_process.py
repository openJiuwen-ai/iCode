# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for chrys.foundation.platform.process — managed_subprocess context manager."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.platform import process as process_mod
from chrys.foundation.platform.process import (
    ManagedStdioProcess,
    SubprocessStoppedError,
    _parse_linux_proc_stat,
    _process_listing_has_stopped_group_member,
    _process_statuses_have_stopped_session_member,
    _ProcessStatus,
    managed_subprocess,
    resolve_windows_comspec,
    serialize_windows_batch_command,
    spawn_managed_stdio_process,
    wait_for_subprocess,
    windows_hidden_subprocess_kwargs,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


def _process_state(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-o", "state=", "-p", str(pid)],
        capture_output=True,
        check=False,
        text=True,
    )
    state = result.stdout.strip()
    return state[:1] if state else None


def test_process_listing_detects_stopped_member_by_pgid() -> None:
    listing = b"""
    Ss       100
    S+       200
    T        300
    tN       300
    R        400
    """

    assert _process_listing_has_stopped_group_member(listing, 300)
    assert not _process_listing_has_stopped_group_member(listing, 200)
    assert not _process_listing_has_stopped_group_member(listing, 999)


def test_linux_proc_stat_parser_handles_comm_with_spaces_and_parens() -> None:
    status = _parse_linux_proc_stat("1234 (python (worker) one) T 1 222 333 0 -1 0 0 0")

    assert status == _ProcessStatus(pid=1234, state="T", pgid=222, session_id=333)


def test_process_statuses_detect_stopped_member_by_session() -> None:
    statuses = [
        _ProcessStatus(pid=1, state="Ss", pgid=100, session_id=100),
        _ProcessStatus(pid=2, state="S", pgid=200, session_id=300),
        _ProcessStatus(pid=3, state="T", pgid=201, session_id=300),
        _ProcessStatus(pid=4, state="tN", pgid=400, session_id=500),
    ]

    assert _process_statuses_have_stopped_session_member(statuses, 300)
    assert not _process_statuses_have_stopped_session_member(statuses, 100)
    assert _process_statuses_have_stopped_session_member(statuses, 500)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX session APIs (os.getsid) unavailable on Windows")
def test_signal_process_session_rejects_own_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "linux")
    monkeypatch.setattr(process_mod.os, "getsid", lambda _pid: 300)

    assert not process_mod.signal_process_session(300, signal.SIGKILL)


def _session_members(monkeypatch: pytest.MonkeyPatch, statuses: list[_ProcessStatus]) -> None:
    monkeypatch.setattr(process_mod.sys, "platform", "linux")
    monkeypatch.setattr(process_mod.os, "getsid", lambda _pid: 999)
    monkeypatch.setattr(process_mod.os, "getpgrp", lambda: 999)
    monkeypatch.setattr(process_mod, "_iter_posix_process_statuses", lambda: iter(statuses))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX session APIs (os.getsid) unavailable on Windows")
def test_signal_process_session_signals_each_session_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _session_members(
        monkeypatch,
        [
            _ProcessStatus(pid=1, state="S", pgid=200, session_id=300),
            _ProcessStatus(pid=2, state="S", pgid=100, session_id=300),
            _ProcessStatus(pid=3, state="S", pgid=0, session_id=300),
            _ProcessStatus(pid=4, state="S", pgid=400, session_id=400),
        ],
    )
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(process_mod.os, "killpg", lambda pgid, signum: calls.append((pgid, signum)))

    assert process_mod.signal_process_session(300, 15)
    assert calls == [(100, 15), (200, 15)]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX session APIs (os.getsid) unavailable on Windows")
def test_signal_process_session_signals_members_of_a_refused_group(monkeypatch: pytest.MonkeyPatch) -> None:
    """macOS may refuse a group signal that each member of the group accepts."""
    _session_members(
        monkeypatch,
        [
            _ProcessStatus(pid=11, state="S", pgid=100, session_id=300),
            _ProcessStatus(pid=12, state="S", pgid=100, session_id=300),
        ],
    )

    def refuse(_pgid: int, _signum: int) -> None:
        raise PermissionError

    signalled: list[int] = []
    monkeypatch.setattr(process_mod.os, "killpg", refuse)
    monkeypatch.setattr(process_mod.os, "kill", lambda pid, _signum: signalled.append(pid))

    assert process_mod.signal_process_session(300, 9)
    assert signalled == [11, 12]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX session APIs (os.getsid/os.getpgid) unavailable on Windows")
def test_process_session_status_falls_back_to_ps_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = b"10 S\n11 T\n"

    monkeypatch.setattr(process_mod.sys, "platform", "darwin")
    monkeypatch.setattr(process_mod.shutil, "which", lambda name: "/bin/ps" if name == "ps" else None)
    monkeypatch.setattr(process_mod.subprocess, "run", lambda *_args, **_kwargs: _Result())
    monkeypatch.setattr(process_mod.os, "getsid", lambda pid: 300 if pid in {10, 11} else 999)
    monkeypatch.setattr(process_mod.os, "getpgid", lambda pid: {10: 100, 11: 101}[pid])

    assert process_mod._process_session_has_stopped_member(300)


def _still_running(process: Any) -> bool:
    """Whether *process* (a ``psutil.Process`` captured while alive) still runs; a zombie has ended.

    It can exit between any two reads, so a process that vanishes while read has ended too.
    """
    import psutil

    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _stub_subprocess_spawn(monkeypatch: pytest.MonkeyPatch, spawn: object) -> None:
    """Point the process module's ``asyncio.create_subprocess_exec`` at *spawn*, leaving asyncio itself alone."""
    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), create_subprocess_exec=spawn)
    monkeypatch.setattr(process_mod, "asyncio", shadow)


async def test_windows_process_tree_cleanup_runs_taskkill_as_a_hidden_async_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    class _Exited:
        returncode = 0

        async def wait(self) -> int:
            return 0

    async def spawn(*argv: str, **kwargs: object) -> _Exited:
        calls.append((argv, kwargs))
        return _Exited()

    hidden = {"creationflags": process_mod._CREATE_NEW_CONSOLE, "startupinfo": "hidden-startupinfo"}
    monkeypatch.setattr(process_mod.sys, "platform", "win32")
    monkeypatch.setattr(process_mod, "windows_hidden_subprocess_kwargs", lambda: dict(hidden))
    monkeypatch.setattr(process_mod.shutil, "which", lambda name: "C:\\Windows\\System32\\taskkill.exe")
    _stub_subprocess_spawn(monkeypatch, spawn)

    assert await process_mod.kill_windows_process_tree(1234)
    [(argv, kwargs)] = calls
    assert argv == ("C:\\Windows\\System32\\taskkill.exe", "/PID", "1234", "/T", "/F")
    devnull = asyncio.subprocess.DEVNULL
    assert kwargs == {"stdin": devnull, "stdout": devnull, "stderr": devnull, **hidden}


async def test_windows_process_tree_kill_gives_up_on_a_taskkill_past_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[asyncio.subprocess.Process] = []

    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(*argv, **kwargs)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(
        process_mod, "_windows_tree_kill_argv", lambda _pid: [sys.executable, "-c", "import time; time.sleep(60)"]
    )
    monkeypatch.setattr(process_mod, "_WINDOWS_TREE_KILL_TIMEOUT", 0.2)
    _stub_subprocess_spawn(monkeypatch, spawn)

    assert await process_mod.kill_windows_process_tree(1234) is False
    [killer] = spawned
    await wait_for(lambda: killer.returncode is not None, description="the stuck taskkill to be killed")


def test_public_hidden_subprocess_helper_preserves_legacy_alias() -> None:
    public = windows_hidden_subprocess_kwargs()
    private = process_mod._windows_hidden_subprocess_kwargs()

    # STARTUPINFO has no __eq__, so on real Windows the two fresh instances
    # must be compared by their semantic fields, not object identity.
    assert public.keys() == private.keys()
    assert public.get("creationflags") == private.get("creationflags")
    public_startup = public.get("startupinfo")
    private_startup = private.get("startupinfo")
    if private_startup is None:
        assert public_startup is None
    else:
        assert public_startup.dwFlags == private_startup.dwFlags
        assert public_startup.wShowWindow == private_startup.wShowWindow


def test_windows_comspec_uses_parent_env_and_absolute_fallback() -> None:
    assert resolve_windows_comspec({"ComSpec": r"D:\Windows\System32\cmd.exe"}) == r"D:\Windows\System32\cmd.exe"
    assert resolve_windows_comspec({"SystemRoot": r"C:\Windows"}) == r"C:\Windows\System32\cmd.exe"
    assert resolve_windows_comspec({"ComSpec": "relative.exe", "SystemRoot": "relative"}) == (
        r"C:\Windows\System32\cmd.exe"
    )
    assert resolve_windows_comspec({"SystemRoot": r"C:\bad%root"}) == r"C:\Windows\System32\cmd.exe"


def test_windows_application_resolution_uses_only_the_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    present = {
        r"C:\tools\agent.CMD",
        r"C:\parent-cwd\agent.cmd",
        r"C:\ws\bin\agent.EXE",
        r"C:\ws\rel\tool.exe",
    }
    monkeypatch.setattr(os.path, "isfile", lambda candidate: candidate in present)

    resolved = process_mod._windows_resolve_application(
        "agent",
        {"PATH": r"C:\missing;C:\tools", "PATHEXT": ".EXE;.CMD"},
        r"C:\ws",
    )
    assert resolved == r"C:\tools\agent.CMD"

    default_pathext = process_mod._windows_resolve_application(
        "agent",
        {"PATH": r"C:\tools"},
        r"C:\ws",
    )
    assert default_pathext == r"C:\tools\agent.CMD"

    case_variant_keys = process_mod._windows_resolve_application(
        "agent",
        {"Path": r"C:\tools", "Pathext": ".CMD"},
        r"C:\ws",
    )
    assert case_variant_keys == r"C:\tools\agent.CMD"

    relative_path_entry = process_mod._windows_resolve_application(
        "agent",
        {"PATH": "bin", "PATHEXT": ".EXE"},
        r"C:\ws",
    )
    assert relative_path_entry == r"C:\ws\bin\agent.EXE"

    unresolved = process_mod._windows_resolve_application("agent", {"PATH": r"C:\missing"}, r"C:\ws")
    assert unresolved == "agent"

    relative = process_mod._windows_resolve_application(r"rel\tool.exe", {}, r"C:\ws")
    assert relative == r"C:\ws\rel\tool.exe"


def test_windows_application_resolution_passes_over_the_store_python_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    placeholder = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps\python3.EXE"
    installed = r"C:\Python313\python3.EXE"
    present = {placeholder, installed}
    monkeypatch.setattr(os.path, "isfile", lambda candidate: candidate in present)
    monkeypatch.setattr(process_mod, "is_windows_store_python_stub", lambda candidate: candidate == placeholder)
    path = r"C:\Users\u\AppData\Local\Microsoft\WindowsApps;C:\Python313"

    assert process_mod._windows_resolve_application("python3", {"PATH": path}, r"C:\ws") == installed

    present.remove(installed)
    assert process_mod._windows_resolve_application("python3", {"PATH": path}, r"C:\ws") == "python3"
    # A path the profile names is used as given.
    assert process_mod._windows_resolve_application(placeholder, {"PATH": path}, r"C:\ws") == placeholder


def test_windows_application_resolution_never_consults_parent_drive_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_abspath(_path: str) -> str:
        raise AssertionError("resolution consulted the parent process's per-drive cwd state")

    monkeypatch.setattr(process_mod.ntpath, "abspath", forbidden_abspath)

    # A cross-drive drive-relative command cannot be anchored from the child
    # configuration alone: it must come back non-absolute so the spawn path
    # rejects it, instead of ntpath.abspath resolving it through the parent's
    # per-drive current directory.
    cross_drive = process_mod._windows_resolve_application(r"C:tools\agent.exe", {}, r"D:\ws")
    assert cross_drive == r"C:tools\agent.exe"
    assert not process_mod.ntpath.isabs(cross_drive)

    same_drive = process_mod._windows_resolve_application(r"C:tools\agent.exe", {}, r"C:\ws")
    assert same_drive == r"C:\ws\tools\agent.exe"

    probes: list[str] = []

    def recording_isfile(candidate: str) -> bool:
        probes.append(candidate)
        return False

    monkeypatch.setattr(os.path, "isfile", recording_isfile)
    unresolved = process_mod._windows_resolve_application(
        "agent",
        {"PATH": r"C:bin;D:\present", "PATHEXT": ".EXE"},
        r"D:\ws",
    )
    assert unresolved == "agent"
    assert probes == [r"D:\present\agent.EXE"]


def test_windows_batch_serializer_quotes_representable_values() -> None:
    assert serialize_windows_batch_command(r"C:\Tools\agent.cmd", ["hello world", "&"]) == (
        '"C:\\Tools\\agent.cmd" "hello world" "&"'
    )


@pytest.mark.parametrize("value", ['"', "%", "!", "^", "\n", "tail\\", "\x00"])
def test_windows_batch_serializer_rejects_unrepresentable_values(value: str) -> None:
    with pytest.raises(ValueError):
        serialize_windows_batch_command(r"C:\Tools\agent.cmd", [value])


@pytest.mark.parametrize("shim", [r"C:\bad%path\agent.cmd", r"relative\agent.cmd"])
def test_windows_batch_serializer_validates_shim_path(shim: str) -> None:
    with pytest.raises(ValueError):
        serialize_windows_batch_command(shim, [])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_spawn_managed_stdio_process_owns_posix_session(tmp_path) -> None:
    process = await spawn_managed_stdio_process(
        sys.executable,
        "-c",
        "import os,sys; sys.stdout.write(str(os.getpgrp()))",
        cwd=str(tmp_path),
        env=dict(os.environ),
        limit=64 * 1024,
    )
    assert isinstance(process, ManagedStdioProcess)
    try:
        output = await process.stdout.read()
        assert int(output) == process.pid
        assert await process.wait() == 0
    finally:
        process.close_transports()


async def test_spawn_managed_stdio_process_has_real_bidirectional_streams(tmp_path) -> None:
    process = await spawn_managed_stdio_process(
        sys.executable,
        "-c",
        "import sys; data=sys.stdin.buffer.readline(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush()",
        cwd=str(tmp_path),
        env=dict(os.environ),
        limit=64 * 1024,
    )
    try:
        process.stdin.write(b"round trip\n")
        await process.stdin.drain()
        assert await process.stdout.readline() == b"round trip\n"
        process.close_stdin()
        assert await process.wait() == 0
    finally:
        process.close_transports()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_posix_managed_process_group_terminates_surviving_descendant(tmp_path) -> None:
    ready = tmp_path / "grandchild.ready"
    terminated = tmp_path / "grandchild.terminated"
    grandchild = textwrap.dedent(
        f"""
        import pathlib
        import signal
        import sys
        import time

        ready = pathlib.Path({str(ready)!r})
        terminated = pathlib.Path({str(terminated)!r})

        def stop(_signum, _frame):
            terminated.write_text("terminated", encoding="utf-8")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        ready.write_text("ready", encoding="utf-8")
        while True:
            time.sleep(1)
        """
    )
    parent = textwrap.dedent(
        f"""
        import pathlib
        import subprocess
        import sys
        import time

        ready = pathlib.Path({str(ready)!r})
        subprocess.Popen([sys.executable, "-c", {grandchild!r}])
        while not ready.exists():
            time.sleep(0.01)
        """
    )
    process = await spawn_managed_stdio_process(
        sys.executable,
        "-c",
        parent,
        cwd=str(tmp_path),
        env=dict(os.environ),
        limit=64 * 1024,
    )
    try:
        await wait_for(ready.exists, description="surviving descendant startup", timeout=ENGINE_TURN_TIMEOUT)
        process.terminate_tree()
        await wait_for(terminated.exists, description="surviving descendant termination")
        await process.wait()
    finally:
        process.kill_tree()
        process.close_transports()


_PARENT_OF_A_SLEEPER = textwrap.dedent(
    """
    import subprocess
    import sys
    import time

    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    print(child.pid, flush=True)
    time.sleep(30)
    """
)


def _test_kernel32() -> Any:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel32.WaitForSingleObject.restype = ctypes.c_ulong
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    kernel32.IsProcessInJob.restype = ctypes.c_int
    kernel32.GetProcessId.argtypes = [ctypes.c_void_p]
    kernel32.GetProcessId.restype = ctypes.c_ulong
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.TerminateProcess.restype = ctypes.c_int
    return kernel32


async def _assert_job_ends_the_sleeper(process: ManagedStdioProcess, kernel32: Any) -> None:
    child_pid = int((await process.stdout.readline()).decode("ascii").strip())
    child_handle = kernel32.OpenProcess(0x00100000, False, child_pid)
    assert child_handle
    try:
        process.kill_tree()
        await process.wait()
        assert kernel32.WaitForSingleObject(child_handle, 5000) == 0
    finally:
        process.kill_tree()
        kernel32.CloseHandle(child_handle)
        process.close_transports()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects only")
async def test_windows_managed_job_terminates_surviving_descendant(tmp_path) -> None:
    process = await spawn_managed_stdio_process(
        sys.executable,
        "-c",
        _PARENT_OF_A_SLEEPER,
        cwd=str(tmp_path),
        env=dict(os.environ),
        limit=64 * 1024,
    )
    await _assert_job_ends_the_sleeper(process, _test_kernel32())


def _refuse_the_job_attribute(
    attempts: list[tuple[int, ...]], *, error: int, refused_jobs: list[int] | None = None
) -> Any:
    """A ``_windows_create_process`` that fails its first call with *error*, then creates for real.

    *refused_jobs* collects the job the refused call named.
    """
    real_create = process_mod._windows_create_process

    def create(
        api: Any,
        application: str,
        command_line_text: str,
        *,
        flags: int,
        environment: object,
        cwd: str,
        stdio: tuple[int, int, int],
        attributes: tuple[Any, ...],
        process_info: Any,
    ) -> int | None:
        attempts.append(tuple(attribute for attribute, _value, _failure in attributes))
        if len(attempts) == 1:
            if refused_jobs is not None:
                refused_jobs.extend(
                    int(value[0])
                    for attribute, value, _failure in attributes
                    if attribute == process_mod._PROC_THREAD_ATTRIBUTE_JOB_LIST
                )
            return error
        return real_create(
            api,
            application,
            command_line_text,
            flags=flags,
            environment=environment,
            cwd=cwd,
            stdio=stdio,
            attributes=attributes,
            process_info=process_info,
        )

    return create


_JOB_AND_HANDLES = (process_mod._PROC_THREAD_ATTRIBUTE_JOB_LIST, process_mod._PROC_THREAD_ATTRIBUTE_HANDLE_LIST)


@pytest.mark.parametrize(
    ("error", "retries"),
    [
        pytest.param(5, True, id="win32-access-denied"),
        pytest.param(0xC0070005, True, id="ntstatus-unsigned-access-denied"),
        pytest.param(-1073283067, True, id="ntstatus-signed-access-denied"),
        pytest.param(2, False, id="file-not-found"),
        pytest.param(87, False, id="invalid-parameter"),
        pytest.param(-1073741819, False, id="access-violation-same-low-word"),
    ],
)
async def test_windows_spawn_access_denied_fallback_preserves_retry_failure(
    monkeypatch: pytest.MonkeyPatch, error: int, retries: bool
) -> None:
    api = SimpleNamespace(kernel32=SimpleNamespace(SetHandleInformation=lambda handle, mask, flags: 1))
    retry_error = 87
    create = create_autospec(process_mod._windows_create_process, side_effect=[error, retry_error])
    create_job = create_autospec(process_mod._windows_create_job, side_effect=[101, 102])
    closed: list[int | None] = []
    monkeypatch.setattr(process_mod, "_windows_process_api", lambda: api)
    monkeypatch.setattr(process_mod, "_windows_create_job", create_job)
    monkeypatch.setattr(process_mod, "_make_windows_pipe_pairs", lambda: ((11, 12), (21, 22), (31, 32)))
    monkeypatch.setattr(process_mod, "_windows_create_process", create)
    monkeypatch.setattr(process_mod, "_close_windows_handle", closed.append)

    # Every create fails before it can return a process handle; no fake handle reaches an OS wait.
    with pytest.raises(OSError) as raised:
        await process_mod._spawn_windows_managed_stdio_process(
            r"C:\Program Files\WindowsApps\Microsoft.PowerShell\pwsh.exe",
            ("-NoProfile", "-Command", "Write-Output ok"),
            cwd="C:\\",
            env={},
            limit=64 * 1024,
            parent_env=None,
        )

    first = create.call_args_list[0].kwargs
    assert tuple(attribute for attribute, _value, _failure in first["attributes"]) == _JOB_AND_HANDLES
    assert not first["flags"] & process_mod._CREATE_SUSPENDED
    assert create.call_count == (2 if retries else 1)
    assert create_job.call_count == (2 if retries else 1)
    assert raised.value.strerror == (
        "Unable to create the Windows subprocess even without the Job Object attribute."
        if retries
        else "Unable to create the Windows subprocess."
    )
    reported_error = raised.value.winerror if sys.platform == "win32" else raised.value.errno
    assert reported_error == (retry_error if retries else error)
    if retries:
        second = create.call_args_list[1].kwargs
        assert second["flags"] == first["flags"] | process_mod._CREATE_SUSPENDED
        assert second["attributes"] == (first["attributes"][1],)
        assert [handle for handle in closed if handle is not None] == [101, 102, 11, 12, 21, 22, 31, 32]
    else:
        assert [handle for handle in closed if handle is not None] == [101, 11, 12, 21, 22, 31, 32]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects only")
# ctypes reports CreateProcessW's last error signed, so the NTSTATUS case is negative.
@pytest.mark.parametrize("error", [5, -1073283067], ids=["win32", "ntstatus"])
async def test_windows_child_refused_the_job_attribute_runs_only_once_its_job_holds_it(
    tmp_path, monkeypatch: pytest.MonkeyPatch, error: int
) -> None:
    import ctypes

    kernel32 = _test_kernel32()
    spawn_api = process_mod._windows_process_api()
    real_assign = spawn_api.kernel32.AssignProcessToJobObject
    real_resume = spawn_api.kernel32.ResumeThread
    real_close = process_mod._close_windows_handle
    attempts: list[tuple[int, ...]] = []
    refused_jobs: list[int] = []
    closed: list[int | None] = []
    assigned: list[tuple[int, int]] = []
    held_at_resume: list[bool] = []
    suspend_counts: list[int] = []

    def assign(job: int, process: int) -> int:
        assigned.append((job, process))
        if job in refused_jobs:
            # As Windows does: the job the refused attempt named takes no process either.
            ctypes.set_last_error(process_mod._ERROR_ACCESS_DENIED)
            return 0
        return real_assign(job, process)

    def close(handle: int | None) -> None:
        closed.append(handle)
        real_close(handle)

    def resume(thread: int) -> int:
        job, process = assigned[-1]
        held = ctypes.c_int()
        assert kernel32.IsProcessInJob(process, job, ctypes.byref(held))
        held_at_resume.append(bool(held.value))
        suspend_counts.append(real_resume(thread))
        return suspend_counts[-1]

    # Denies the job attribute as `error`, as a packaged (MSIX) executable such as Store PowerShell may be refused.
    monkeypatch.setattr(
        process_mod,
        "_windows_create_process",
        _refuse_the_job_attribute(attempts, error=error, refused_jobs=refused_jobs),
    )
    monkeypatch.setattr(process_mod, "_close_windows_handle", close)
    monkeypatch.setattr(spawn_api.kernel32, "AssignProcessToJobObject", assign)
    monkeypatch.setattr(spawn_api.kernel32, "ResumeThread", resume)
    process = await spawn_managed_stdio_process(
        sys.executable,
        "-c",
        _PARENT_OF_A_SLEEPER,
        cwd=str(tmp_path),
        env=dict(os.environ),
        limit=64 * 1024,
    )
    closed_by_spawn = list(closed)
    await _assert_job_ends_the_sleeper(process, kernel32)

    assert attempts == [_JOB_AND_HANDLES, (process_mod._PROC_THREAD_ATTRIBUTE_HANDLE_LIST,)]
    # The child went to a job of its own (the refused one would not take it), and the refused one was closed.
    assert closed_by_spawn == refused_jobs
    assert held_at_resume == [True]
    assert suspend_counts == [1]  # it had not run: created suspended, resumed once its job held it


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects only")
async def test_windows_fallback_that_cannot_make_its_job_closes_the_refused_one_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_create_job = process_mod._windows_create_job
    real_close = process_mod._close_windows_handle
    made: list[int] = []
    closed: list[int | None] = []

    def create_job(api: Any) -> int:
        if made:
            raise OSError(process_mod._ERROR_ACCESS_DENIED, "Unable to create a Windows Job Object.")
        made.append(real_create_job(api))
        return made[-1]

    def close(handle: int | None) -> None:
        closed.append(handle)
        real_close(handle)

    monkeypatch.setattr(
        process_mod,
        "_windows_create_process",
        _refuse_the_job_attribute([], error=process_mod._ERROR_ACCESS_DENIED),
    )
    monkeypatch.setattr(process_mod, "_windows_create_job", create_job)
    monkeypatch.setattr(process_mod, "_close_windows_handle", close)

    with pytest.raises(OSError, match="Unable to create a Windows Job Object"):
        await spawn_managed_stdio_process(
            sys.executable, "-c", "pass", cwd=str(tmp_path), env=dict(os.environ), limit=64 * 1024
        )

    # Closed by the failure, not before it as well: its value may name another handle by then.
    [refused_job] = made
    assert closed.count(refused_job) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process creation only")
async def test_windows_spawn_retries_only_a_refused_job_attribute(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[tuple[int, ...]] = []
    error_file_not_found = 2
    monkeypatch.setattr(
        process_mod,
        "_windows_create_process",
        _refuse_the_job_attribute(attempts, error=error_file_not_found),
    )

    with pytest.raises(OSError, match="Unable to create the Windows subprocess") as raised:
        await spawn_managed_stdio_process(
            sys.executable, "-c", "pass", cwd=str(tmp_path), env=dict(os.environ), limit=64 * 1024
        )

    assert raised.value.winerror == error_file_not_found
    assert attempts == [_JOB_AND_HANDLES]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Objects only")
async def test_windows_suspended_child_its_job_cannot_take_is_terminated_unrun(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ctypes

    kernel32 = _test_kernel32()
    spawn_api = process_mod._windows_process_api()
    real_resume = spawn_api.kernel32.ResumeThread
    watched: list[int] = []
    resumed: list[int] = []

    def refuse(_job: int, process: int) -> int:
        # SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE, held past the spawn's cleanup.
        watched.append(kernel32.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, kernel32.GetProcessId(process)))
        ctypes.set_last_error(process_mod._ERROR_ACCESS_DENIED)
        return 0

    def resume(thread: int) -> int:
        resumed.append(thread)
        return real_resume(thread)

    monkeypatch.setattr(
        process_mod,
        "_windows_create_process",
        _refuse_the_job_attribute([], error=process_mod._ERROR_ACCESS_DENIED),
    )
    monkeypatch.setattr(spawn_api.kernel32, "AssignProcessToJobObject", refuse)
    monkeypatch.setattr(spawn_api.kernel32, "ResumeThread", resume)
    try:
        with pytest.raises(OSError, match="Unable to assign the Windows subprocess to its Job Object") as raised:
            await spawn_managed_stdio_process(
                sys.executable, "-c", "pass", cwd=str(tmp_path), env=dict(os.environ), limit=64 * 1024
            )

        assert raised.value.winerror == process_mod._ERROR_ACCESS_DENIED
        assert resumed == []
        assert len(watched) == 1 and watched[0]
        assert kernel32.WaitForSingleObject(watched[0], 5000) == 0
        exit_code = ctypes.c_ulong()
        assert kernel32.GetExitCodeProcess(watched[0], ctypes.byref(exit_code))
        assert exit_code.value == 1  # the spawn's cleanup ended it; `pass` run would have exited 0
    finally:
        for handle in watched:
            kernel32.TerminateProcess(handle, 1)
            kernel32.CloseHandle(handle)


async def test_wait_for_subprocess_success_not_masked_by_completed_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_sleep = asyncio.sleep

    async def fake_monitor(_process_group_id: int) -> None:
        raise SubprocessStoppedError("stopped")

    async def fake_wait(
        tasks: set[asyncio.Future[object]], **_kwargs: object
    ) -> tuple[set[asyncio.Future[object]], set]:
        await real_sleep(0)
        return tasks, set()

    async def operation() -> str:
        return "ok"

    monkeypatch.setattr(process_mod, "_monitor_process_group_stopped", fake_monitor)
    monkeypatch.setattr(process_mod.asyncio, "wait", fake_wait)

    assert await wait_for_subprocess(operation(), timeout=1, process_group_id=1234) == "ok"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_managed_subprocess_does_not_kill_group_after_clean_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed_groups: list[int] = []

    def fake_kill_process_group(process_group_id: int) -> bool:
        killed_groups.append(process_group_id)
        return True

    monkeypatch.setattr(process_mod, "kill_process_group", fake_kill_process_group)

    async with managed_subprocess(
        sys.executable,
        "-c",
        "print('ok')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    ) as proc:
        stdout, stderr = await proc.communicate()

    assert stdout == b"ok\n"
    assert stderr == b""
    assert proc.returncode == 0
    assert killed_groups == []


@pytest.mark.asyncio
async def test_normal_exit_closes_transport():
    """Transport is closed after normal subprocess completion."""
    async with managed_subprocess(
        sys.executable,
        "-c",
        "print('hello')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    ) as proc:
        stdout, _ = await proc.communicate()

    assert proc.returncode == 0
    assert b"hello" in stdout
    # Transport should be closed
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        assert transport.is_closing()


@pytest.mark.asyncio
async def test_timeout_kills_and_closes():
    """Process is killed and transport closed on timeout."""
    async with managed_subprocess(
        sys.executable,
        "-c",
        "import time; time.sleep(5)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    ) as proc:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(proc.communicate(), timeout=0.5)

    # Context manager should have killed and reaped the process
    assert proc.returncode is not None
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        assert transport.is_closing()


@pytest.mark.asyncio
async def test_cancellation_kills_and_closes():
    """Process is killed and transport closed when task is cancelled."""
    started = asyncio.Event()

    async def _run():
        async with managed_subprocess(
            sys.executable,
            "-c",
            "import time; time.sleep(5)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ) as p:
            started.set()
            await p.communicate()
        return p

    task = asyncio.create_task(_run())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_nonzero_exit_code():
    """Transport is closed even on non-zero exit."""
    async with managed_subprocess(
        sys.executable,
        "-c",
        "raise SystemExit(42)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    ) as proc:
        await proc.communicate()

    assert proc.returncode == 42
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        assert transport.is_closing()


@pytest.mark.asyncio
async def test_file_not_found_propagates():
    """FileNotFoundError from missing binary propagates normally."""
    with pytest.raises(FileNotFoundError):
        async with managed_subprocess("__nonexistent_binary_xyz__") as _proc:
            pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
@pytest.mark.asyncio
async def test_timeout_kills_stopped_grandchild_holding_pipe():
    """Cleanup kills the whole process group even after the wrapper exits."""
    script = textwrap.dedent(
        """
        import os
        import signal
        import subprocess
        import sys

        subprocess.Popen([
            sys.executable,
            "-c",
            "import os, signal, time; print(os.getpid(), flush=True); os.kill(os.getpid(), signal.SIGSTOP); time.sleep(30)",
        ])
        """
    )

    child_pid = 0
    with pytest.raises(TimeoutError):
        async with managed_subprocess(
            sys.executable,
            "-c",
            script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        ) as proc:
            assert proc.stdout is not None
            child_pid = int((await proc.stdout.readline()).decode("ascii").strip())
            await asyncio.wait_for(proc.communicate(), timeout=0.5)

    assert child_pid > 0
    for _ in range(20):
        state = _process_state(child_pid)
        if state is None or state == "Z":
            break
        await asyncio.sleep(0.05)
    else:
        pytest.fail(f"stopped grandchild survived managed_subprocess cleanup with state={state!r}")


def test_terminate_process_group_uses_platform_abstraction(monkeypatch: pytest.MonkeyPatch) -> None:
    import chrys.foundation.platform as platform_mod

    monkeypatch.setattr(platform_mod, "get_platform", lambda: SimpleNamespace(is_windows=True))
    monkeypatch.setattr(
        process_mod.os,
        "killpg",
        lambda *_args: pytest.fail("killpg must not run on Windows"),
        raising=False,
    )

    assert not process_mod.terminate_process_group(12345)


def _registered_windows_process(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ManagedStdioProcess, asyncio.Future[bool], list[int], list[int]]:
    """A retained Windows process whose port wait the test completes; returns it with the registration and close logs."""
    signalled: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
    registered: list[int] = []
    closed: list[int] = []

    def fake_register(handle: int) -> asyncio.Future[bool]:
        registered.append(handle)
        return signalled

    monkeypatch.setattr(process_mod, "_register_windows_process_wait", fake_register)
    monkeypatch.setattr(process_mod, "_windows_process_exit_code", lambda handle: 42)
    monkeypatch.setattr(process_mod, "_close_windows_handle", closed.append)
    monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", lambda *_args: pytest.fail("no wait thread"))
    process = ManagedStdioProcess(
        stdin=SimpleNamespace(close=lambda: None),
        stdout=None,
        stderr=None,
        pid=123,
        _windows_process_handle=111,
        _windows_job_handle=222,
    )
    return process, signalled, registered, closed


async def test_windows_wait_shares_one_registered_wait_and_defers_handle_close_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, signalled, registered, closed = _registered_windows_process(monkeypatch)
    for _ in range(3):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await process.wait()
    # Every bounded wait resumes the same registered wait instead of
    # registering (or blocking a thread on) the handle again per call.
    assert registered == [111]

    process.close_transports()
    # The job handle (never waited on, kill-on-close) closes inline; the
    # process handle stays open while the port still waits on it.
    assert closed == [222]
    assert process._windows_process_handle is None

    signalled.set_result(True)
    assert await process.wait() == 42
    await wait_for(lambda: closed == [222, 111], description="deferred process-handle close")


async def test_windows_handle_close_survives_task_level_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process, signalled, _registered, closed = _registered_windows_process(monkeypatch)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await process.wait()
    process.close_transports()
    assert closed == [222]

    # Loop-shutdown-style cancellation reaches the registered wait, where the
    # proactor unregisters it; only then may the handle close.
    waiter = process._windows_wait_task
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert signalled.cancelled()
    await wait_for(lambda: closed == [222, 111], description="handle release after the wait unregistered")


def _stub_running_loop(monkeypatch: pytest.MonkeyPatch, loop: object) -> None:
    """Point the process module's ``asyncio.get_running_loop`` at *loop*; the test's own loop is untouched.

    The registration must never reach a real ``IocpProactor`` with a made-up
    handle: the thread pool answers an invalid handle with a fatal exception
    (``0xC000070A``), not an error return, and the xdist worker dies with it.
    """
    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio))
    shadow.get_running_loop = lambda: loop
    monkeypatch.setattr(process_mod, "asyncio", shadow)


async def test_windows_process_wait_registers_with_the_running_loops_completion_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_running_loop(monkeypatch, SimpleNamespace())
    with pytest.raises(RuntimeError, match="proactor event loop"):
        process_mod._register_windows_process_wait(111)

    registered: list[int] = []
    waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    def wait_for_handle(handle: int) -> asyncio.Future[bool]:
        registered.append(handle)
        return waiter

    _stub_running_loop(monkeypatch, SimpleNamespace(_proactor=SimpleNamespace(wait_for_handle=wait_for_handle)))
    assert process_mod._register_windows_process_wait(111) is waiter
    assert registered == [111]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_a_start_that_finishes_as_its_wait_times_out_still_has_its_tree_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded wait reports a timeout, but the start finished meanwhile: its tree is ended, not left running."""
    import psutil

    real_spawn = asyncio.create_subprocess_exec
    started: list[asyncio.subprocess.Process] = []
    connected = asyncio.Event()

    async def slow_to_connect(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_spawn(*args, **kwargs)
        started.append(proc)
        await connected.wait()
        return proc

    async def times_out_as_the_start_finishes(
        futures: set[asyncio.Future[Any]], *, timeout: float
    ) -> tuple[set[asyncio.Future[Any]], set[asyncio.Future[Any]]]:
        # The timer fires in the same turn the start completes: the wait still says nothing finished.
        connected.set()
        (spawn,) = futures
        await asyncio.wait({spawn})
        return set(), set(futures)

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), create_subprocess_exec=slow_to_connect, wait=times_out_as_the_start_finishes)
    monkeypatch.setattr(process_mod, "asyncio", shadow)

    async def run() -> None:
        async with managed_subprocess("/bin/sh", "-c", "sleep 30; echo done", stdout=asyncio.subprocess.PIPE):
            pass

    task = asyncio.create_task(run())
    await wait_for(lambda: bool(started) or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="child running")
    assert not task.done()
    shell = psutil.Process(started[0].pid)
    await wait_for(lambda: bool(shell.children()), timeout=ENGINE_TURN_TIMEOUT, description="the shell ran its sleep")
    processes = [shell, *shell.children(recursive=True)]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)

    def gone() -> bool:
        return not any(_still_running(process) for process in processes)

    await wait_for(gone, timeout=ENGINE_TURN_TIMEOUT, description="the shell and its sleep ended")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
async def test_a_start_finishing_after_its_cancel_went_on_still_has_its_tree_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cancel does not wait for a slow start, and the start is not cut short: its whole tree ends once it is up."""
    import psutil

    monkeypatch.setattr(process_mod, "_PROCESS_WAIT_CLEANUP_TIMEOUT", 0.2)
    real_spawn = asyncio.create_subprocess_exec
    started: list[asyncio.subprocess.Process] = []
    connected = asyncio.Event()
    spawns: list[asyncio.Future[Any]] = []

    async def slow_to_connect(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_spawn(*args, **kwargs)
        started.append(proc)
        await connected.wait()
        return proc

    real_ensure_future = asyncio.ensure_future

    def recording_ensure_future(awaitable: Any) -> asyncio.Future[Any]:
        future = real_ensure_future(awaitable)
        spawns.append(future)
        return future

    shadow = ModuleType("asyncio")
    shadow.__dict__.update(vars(asyncio), create_subprocess_exec=slow_to_connect, ensure_future=recording_ensure_future)
    monkeypatch.setattr(process_mod, "asyncio", shadow)

    async def run() -> None:
        async with managed_subprocess("/bin/sh", "-c", "sleep 30; echo done", stdout=asyncio.subprocess.PIPE):
            pass

    task = asyncio.create_task(run())
    await wait_for(lambda: bool(started) or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="child running")
    assert not task.done()
    shell = psutil.Process(started[0].pid)
    await wait_for(lambda: bool(shell.children()), timeout=ENGINE_TURN_TIMEOUT, description="the shell ran its sleep")
    processes = [shell, *shell.children(recursive=True)]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)
    # The cancel went on while the start was still connecting, which was left to finish.
    (spawn,) = spawns
    assert not spawn.done()
    assert all(_still_running(process) for process in processes)

    connected.set()

    def gone() -> bool:
        return not any(_still_running(process) for process in processes)

    await wait_for(gone, timeout=ENGINE_TURN_TIMEOUT, description="the shell and its sleep ended")
    assert not spawn.cancelled()
    await wait_for(lambda: not process_mod._LATE_CHILD_ENDINGS, timeout=ENGINE_TURN_TIMEOUT, description="ending done")


@pytest.mark.parametrize("starter", ["managed_subprocess", "spawn_managed_stdio_process"])
async def test_a_cancel_ends_even_when_the_start_never_finishes(
    monkeypatch: pytest.MonkeyPatch, tmp_path, starter: str
) -> None:
    """A start stuck for good (a stalled network disk) cannot hold a cancel, and is given up after a grace."""
    monkeypatch.setattr(process_mod, "_PROCESS_WAIT_CLEANUP_TIMEOUT", 0.2)
    monkeypatch.setattr(process_mod, "_LATE_START_GRACE", 0.2)
    entered = asyncio.Event()
    abandoned = asyncio.Event()

    async def never_starts(*_args: Any, **_kwargs: Any) -> asyncio.subprocess.Process:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            abandoned.set()
            raise
        raise AssertionError("unreachable")

    _stub_subprocess_spawn(monkeypatch, never_starts)
    if starter == "spawn_managed_stdio_process" and sys.platform == "win32":
        pytest.skip("the Windows stdio spawn does not start through create_subprocess_exec")

    async def run() -> None:
        if starter == "managed_subprocess":
            async with managed_subprocess("cmd", stdout=asyncio.subprocess.PIPE):
                pass
        else:
            await spawn_managed_stdio_process("cmd", cwd=str(tmp_path), env=dict(os.environ), limit=64 * 1024)

    task = asyncio.create_task(run())
    await wait_for(lambda: entered.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="start entered")
    assert not task.done()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)
    await wait_for(abandoned.is_set, timeout=ENGINE_TURN_TIMEOUT, description="the stuck start was cancelled")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups only")
@pytest.mark.parametrize("cancels", [1, 2], ids=["one-cancel", "a-second-cancel"])
@pytest.mark.parametrize("starter", ["managed_subprocess", "spawn_managed_stdio_process"])
async def test_a_cancel_while_the_child_starts_ends_everything_it_started(
    monkeypatch: pytest.MonkeyPatch, tmp_path, starter: str, cancels: int
) -> None:
    """``create_subprocess_exec`` returns after the child already runs: a cancel in that gap still ends its tree.

    A second cancel while that ending waits for the start does not cut it short.
    """
    import psutil

    real_spawn = asyncio.create_subprocess_exec
    started: list[asyncio.subprocess.Process] = []
    connected = asyncio.Event()

    async def slow_to_connect(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        proc = await real_spawn(*args, **kwargs)
        started.append(proc)
        # The child runs; its start has not returned yet.
        await connected.wait()
        return proc

    _stub_subprocess_spawn(monkeypatch, slow_to_connect)

    async def run() -> None:
        if starter == "managed_subprocess":
            async with managed_subprocess("/bin/sh", "-c", "sleep 30; echo done", stdout=asyncio.subprocess.PIPE):
                pass
        else:
            process = await spawn_managed_stdio_process(
                "/bin/sh", "-c", "sleep 30; echo done", cwd=str(tmp_path), env=dict(os.environ), limit=64 * 1024
            )
            process.close_transports()

    task = asyncio.create_task(run())
    await wait_for(lambda: bool(started) or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="child running")
    assert not task.done()
    shell = psutil.Process(started[0].pid)
    await wait_for(lambda: bool(shell.children()), timeout=ENGINE_TURN_TIMEOUT, description="the shell ran its sleep")
    processes = [shell, *shell.children(recursive=True)]

    task.cancel()
    # Lets the cancel land while the start is still held.
    await asyncio.sleep(0)
    if cancels == 2:
        task.cancel()
        await asyncio.sleep(0)
    assert not task.done()
    connected.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)

    def gone() -> bool:
        return not any(_still_running(process) for process in processes)

    await wait_for(gone, timeout=ENGINE_TURN_TIMEOUT, description="the shell and its sleep ended")
