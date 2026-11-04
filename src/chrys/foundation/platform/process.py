# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Async subprocess utilities.

Provides ``managed_subprocess`` for proper transport cleanup on Windows
(CPython issue #43884) and ``decode_subprocess_output`` for robust
decoding of subprocess byte output with a strong UTF-8 preference.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import ctypes
import errno
import functools
import ntpath
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import AsyncGenerator, Awaitable, Iterable, Iterator
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Never, cast

from chrys.foundation.platform.c_api import declare_functions, struct_fields
from chrys.foundation.platform.child_reap import install_stopped_child_reap_fix
from chrys.foundation.platform.runtime_paths import is_windows_store_python_stub
from chrys.foundation.text.encoding import decode_bytes, is_mostly_text
from chrys.foundation.util.once_close import finish_close

# Every subprocess in the app is spawned through this module, and the shim has to
# be in place before the first one starts, so install it on import.  See
# chrys.foundation.platform.child_reap for what it works around.
install_stopped_child_reap_fix()

_CP_UTF8 = 65001
_STOPPED_PROCESS_POLL_INTERVAL = 1.0
_PROCESS_WAIT_CLEANUP_TIMEOUT = 2.0
_LATE_START_GRACE = 30.0
"""How long a start a cancel stopped waiting for may still finish and have its tree ended; then it is given up."""
_LATE_CHILD_ENDINGS: set[asyncio.Task[None]] = set()
"""Tree endings for starts that finished after their cancel went on; each removes itself when done."""
_WINDOWS_TREE_KILL_TIMEOUT = 2.0


class SubprocessStoppedError(RuntimeError):
    """Raised when a POSIX subprocess group/session enters job-control stopped state."""


class MissingWorkingDirectoryError(OSError):
    """A child could not start because its working directory no longer exists.

    A spawn into a deleted cwd fails with the same ``FileNotFoundError`` as a
    missing executable on POSIX (``NotADirectoryError``/``WinError 267`` on
    Windows). This is deliberately NOT a ``FileNotFoundError`` subclass, so
    "executable not found" handlers do not swallow it.
    """

    def __init__(self, path: str) -> None:
        super().__init__(errno.ENOENT, "working directory no longer exists", path)
        self.path = path

    def __str__(self) -> str:
        return f"working directory no longer exists: {self.path}"


def raise_if_missing_cwd(cwd: object, error: OSError) -> None:
    """Re-raise a failed spawn as :class:`MissingWorkingDirectoryError` when *cwd* is gone.

    Called from an ``except OSError`` around a spawn. Checking the directory
    after the failure covers both platforms without parsing errno, winerror
    or ``filename``; when *cwd* still exists this returns and the caller
    re-raises the original error.
    """
    if isinstance(error, MissingWorkingDirectoryError) or not isinstance(cwd, str | os.PathLike):
        return
    path = os.fspath(cwd)
    if isinstance(path, str) and path and not os.path.isdir(path):
        raise MissingWorkingDirectoryError(path) from error


@dataclass(frozen=True)
class WindowsShellCommand:
    """A command line for ``cmd.exe /c``, handed to it as written: :func:`managed_subprocess`'s one argument.

    An argument list reaches a Windows child joined by ``subprocess.list2cmdline``,
    which escapes every quote in it as ``\\"``; ``cmd.exe`` does not read that
    escape, so ``echo x > "a b"`` would print its redirection instead. The trusted
    ``cmd.exe`` (:func:`resolve_windows_comspec`) gets the command as ``/c "<command>"``,
    the form ``subprocess`` gives a shell command. Windows only.
    """

    command: str


@dataclass(frozen=True)
class _ProcessStatus:
    pid: int
    state: str
    pgid: int
    session_id: int


@dataclass(frozen=True)
class _WindowsProcessAPI:
    """Typed handle for the lazily loaded Win32 process API."""

    kernel32: ctypes.CDLL


class _StartupInfoW(ctypes.Structure):
    """``STARTUPINFOW``."""

    _fields_ = struct_fields(
        (wintypes.DWORD, "cb"),
        (wintypes.LPWSTR, "lpReserved lpDesktop lpTitle"),
        (wintypes.DWORD, "dwX dwY dwXSize dwYSize dwXCountChars dwYCountChars dwFillAttribute dwFlags"),
        (wintypes.WORD, "wShowWindow cbReserved2"),
        (ctypes.POINTER(ctypes.c_ubyte), "lpReserved2"),
        (wintypes.HANDLE, "hStdInput hStdOutput hStdError"),
    )


class _StartupInfoExW(ctypes.Structure):
    """``STARTUPINFOEXW``: the startup info plus a process/thread attribute list."""

    _fields_ = struct_fields((_StartupInfoW, "StartupInfo"), (wintypes.LPVOID, "lpAttributeList"))


class _ProcessInformation(ctypes.Structure):
    """``PROCESS_INFORMATION``, filled in by ``CreateProcessW``."""

    _fields_ = struct_fields((wintypes.HANDLE, "hProcess hThread"), (wintypes.DWORD, "dwProcessId dwThreadId"))


class _IoCounters(ctypes.Structure):
    """``IO_COUNTERS``."""

    _fields_ = struct_fields(
        (ctypes.c_ulonglong, "ReadOperationCount WriteOperationCount OtherOperationCount"),
        (ctypes.c_ulonglong, "ReadTransferCount WriteTransferCount OtherTransferCount"),
    )


class _BasicLimitInformation(ctypes.Structure):
    """``JOBOBJECT_BASIC_LIMIT_INFORMATION``."""

    _fields_ = struct_fields(
        (ctypes.c_longlong, "PerProcessUserTimeLimit PerJobUserTimeLimit"),
        (wintypes.DWORD, "LimitFlags"),
        (ctypes.c_size_t, "MinimumWorkingSetSize MaximumWorkingSetSize"),
        (wintypes.DWORD, "ActiveProcessLimit"),
        (ctypes.c_size_t, "Affinity"),
        (wintypes.DWORD, "PriorityClass SchedulingClass"),
    )


class _ExtendedLimitInformation(ctypes.Structure):
    """``JOBOBJECT_EXTENDED_LIMIT_INFORMATION``, the job's kill-on-close setting among them."""

    _fields_ = struct_fields(
        (_BasicLimitInformation, "BasicLimitInformation"),
        (_IoCounters, "IoInfo"),
        (ctypes.c_size_t, "ProcessMemoryLimit JobMemoryLimit PeakProcessMemoryUsed PeakJobMemoryUsed"),
    )


_JOB_LISTED_PROCESSES = 256


class _JobProcessIdList(ctypes.Structure):
    """``JOBOBJECT_BASIC_PROCESS_ID_LIST``, with room for the first processes of a job."""

    _fields_ = struct_fields(
        (wintypes.DWORD, "NumberOfAssignedProcesses NumberOfProcessIdsInList"),
        (ctypes.c_size_t * _JOB_LISTED_PROCESSES, "ProcessIdList"),
    )


@functools.cache
def _windows_uses_utf8() -> bool:
    """Return True if the Windows console output code page is UTF-8.

    Uses ``GetConsoleOutputCP`` (the OEM output code page) rather than
    ``locale.getencoding`` (the ANSI code page) because subprocess
    output follows the console code page, not the ANSI one.
    """
    if sys.platform != "win32":
        return True
    try:
        return ctypes.windll.kernel32.GetConsoleOutputCP() == _CP_UTF8
    except AttributeError, OSError:
        return False


@functools.cache
def _windows_console_encoding() -> str | None:
    """Return the Python codec name for the Windows console output code page.

    E.g. ``"cp936"`` for GBK (Simplified Chinese), ``"cp932"`` for
    Shift-JIS (Japanese), ``"cp949"`` for EUC-KR (Korean).

    Returns ``None`` on non-Windows platforms, when the code page is
    UTF-8, or when the code page cannot be determined.
    """
    if sys.platform != "win32":
        return None
    try:
        cp = ctypes.windll.kernel32.GetConsoleOutputCP()
        if cp == _CP_UTF8 or cp == 0:
            return None
        return f"cp{cp}"
    except AttributeError, OSError:
        return None


_CREATE_NEW_CONSOLE = 0x00000010
_CREATE_SUSPENDED = 0x00000004
_ERROR_ACCESS_DENIED = 5
_NTSTATUS_WIN32_ACCESS_DENIED = 0xC0070005
_RESUME_THREAD_FAILED = 0xFFFFFFFF
_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_WINDOWS_BATCH_SUFFIXES = {".bat", ".cmd"}
_WINDOWS_BATCH_FORBIDDEN = {'"', "%", "!", "^", "\r", "\n"}


def windows_hidden_subprocess_kwargs() -> dict[str, Any]:
    """Return subprocess kwargs that spawn a hidden child with its *own* console.

    Intended for synchronous ``subprocess.run`` callers — unpack with
    ``**windows_hidden_subprocess_kwargs()``. Returns ``{}`` off Windows.

    On Windows we pair:

    - ``creationflags=CREATE_NEW_CONSOLE`` — the child gets its own
      console, so it cannot call ``SetConsoleMode()`` on the parent's
      console (which would reset mouse / keyboard flags and break the
      TUI).  ``CREATE_NO_WINDOW`` is **not** safe here: it makes the
      child share the parent's console.
    - ``startupinfo`` with ``STARTF_USESHOWWINDOW | SW_HIDE`` — suppress
      the new console window so it doesn't flash on screen.
    """
    if sys.platform != "win32":
        return {}
    import subprocess as _sp

    si = _sp.STARTUPINFO()
    si.dwFlags |= _sp.STARTF_USESHOWWINDOW
    si.wShowWindow = _sp.SW_HIDE
    return {"creationflags": _CREATE_NEW_CONSOLE, "startupinfo": si}


_windows_hidden_subprocess_kwargs = windows_hidden_subprocess_kwargs
"""Backward-compatible alias for callers predating the public helper."""


def finalize_subprocess_transport(proc: asyncio.subprocess.Process) -> None:
    """Close an asyncio subprocess transport, including Proactor pipe transports."""
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(OSError):
            transport.close()


def resolve_windows_comspec(parent_env: dict[str, str] | None = None) -> str:
    """Resolve a trusted absolute ``cmd.exe`` from the parent environment."""
    source = os.environ if parent_env is None else parent_env
    casefolded = {key.casefold(): value for key, value in source.items()}
    candidate = casefolded.get("comspec", "")
    if (
        type(candidate) is str
        and ntpath.isabs(candidate)
        and not any(character in candidate for character in _WINDOWS_BATCH_FORBIDDEN | {"\x00"})
    ):
        return ntpath.normpath(candidate)
    system_root = casefolded.get("systemroot", r"C:\Windows")
    if (
        type(system_root) is not str
        or not ntpath.isabs(system_root)
        or any(character in system_root for character in _WINDOWS_BATCH_FORBIDDEN | {"\x00"})
    ):
        system_root = r"C:\Windows"
    return ntpath.normpath(ntpath.join(system_root, "System32", "cmd.exe"))


def serialize_windows_batch_command(shim_path: str, args: Iterable[str]) -> str:
    """Serialize the representable subset accepted by the ACP batch shim boundary."""
    values = [shim_path, *args]
    for value in values:
        if type(value) is not str:
            raise TypeError("Windows batch command values must be exact strings.")
        if "\x00" in value:
            raise ValueError("Windows batch command values cannot contain NUL.")
        if any(character in value for character in _WINDOWS_BATCH_FORBIDDEN):
            raise ValueError("Windows batch command contains characters that cannot be represented safely.")
        if value.endswith("\\"):
            raise ValueError("Windows batch command cannot safely represent a trailing backslash.")
    if not ntpath.isabs(shim_path):
        raise ValueError("Windows batch shim path must be absolute.")
    return " ".join(f'"{value}"' for value in values)


@dataclass
class ManagedStdioProcess:
    """Owned subprocess with real asyncio streams and process-tree termination."""

    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    stderr: asyncio.StreamReader
    pid: int
    process_group_id: int | None = None
    _process: asyncio.subprocess.Process | None = None
    _windows_process_handle: int | None = None
    _windows_job_handle: int | None = None
    _windows_returncode: int | None = None
    _windows_stdout_transport: asyncio.BaseTransport | None = None
    _windows_stderr_transport: asyncio.BaseTransport | None = None
    _windows_wait_task: asyncio.Task[int] | None = None

    @property
    def returncode(self) -> int | None:
        if self._process is not None:
            return self._process.returncode
        return self._windows_returncode

    async def wait(self) -> int:
        """Wait for the retained process and return its exit code."""
        if self._process is not None:
            return await self._process.wait()
        if self._windows_returncode is not None:
            return self._windows_returncode
        waiter = self._windows_wait_task
        if waiter is None:
            if self._windows_process_handle is None:
                raise RuntimeError("Windows process handle has already been released.")
            # One registered wait per process: the loop's completion port completes
            # it when the process exits, with no thread blocked in WaitForSingleObject,
            # and every wait() call awaits that same task.
            waiter = asyncio.create_task(
                _windows_process_exit(self._windows_process_handle),
                name="chrys.platform.windows-process-wait",
            )
            waiter.add_done_callback(_consume_task_exception)
            self._windows_wait_task = waiter
        # asyncio.wait never cancels the watched task: a caller-scoped timeout
        # interrupts this wait() while the registered wait stays put for the
        # next caller to resume awaiting.
        await asyncio.wait({waiter})
        self._windows_returncode = waiter.result()
        return self._windows_returncode

    def close_stdin(self) -> None:
        """Send EOF to the child without waiting for its process exit."""
        try:
            self.stdin.write_eof()
        except AttributeError, NotImplementedError, OSError, RuntimeError:
            self.stdin.close()

    def terminate_tree(self) -> None:
        """Terminate the owned process tree using retained identities only."""
        if self._process is not None:
            if self.process_group_id is not None:
                terminate_process_group(self.process_group_id)
            elif self._process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    self._process.terminate()
            return
        _terminate_windows_retained_process(self._windows_job_handle, self._windows_process_handle, exit_code=1)

    def kill_tree(self) -> None:
        """Kill the owned process tree using retained identities only."""
        if self._process is not None:
            if self.process_group_id is not None:
                kill_process_group(self.process_group_id)
            elif self._process.returncode is None:
                with contextlib.suppress(ProcessLookupError, OSError):
                    self._process.kill()
            return
        _terminate_windows_retained_process(self._windows_job_handle, self._windows_process_handle, exit_code=9)

    def live_processes(self) -> dict[int, float]:
        """The processes of the owned tree still alive, each with the CPU seconds it used so far.

        Those of its Job on Windows, but the console hosts the system started
        for them; those of its session elsewhere. Blocking: run it in a thread.
        """
        if self._process is not None:
            return session_processes(self.pid)
        if self._windows_job_handle is None:
            return {}
        return _windows_job_processes(self._windows_job_handle)

    def close_transports(self) -> None:
        """Close owned stream transports and release retained process handles."""
        self.stdin.close()
        if self._process is not None:
            finalize_subprocess_transport(self._process)
            return
        if self._windows_stdout_transport is not None:
            self._windows_stdout_transport.close()
            self._windows_stdout_transport = None
        if self._windows_stderr_transport is not None:
            self._windows_stderr_transport.close()
            self._windows_stderr_transport = None
        process_handle = self._windows_process_handle
        job_handle = self._windows_job_handle
        self._windows_process_handle = None
        self._windows_job_handle = None
        # The job handle is never waited on and KILL_ON_JOB_CLOSE makes closing
        # it the final kill switch, so it always closes inline. The process
        # handle may still be registered with the completion port — closing a
        # handle under a registered wait is undefined — so its close waits for
        # the wait task to finish: the proactor unregisters the wait before the
        # task can complete, whether the process exited or loop shutdown
        # cancelled the task. A wait the loop never resumes (loop closed) leaks
        # the handle until process exit; leak beats undefined.
        _close_windows_handle(job_handle)
        waiter = self._windows_wait_task
        if waiter is None or waiter.done():
            _close_windows_handle(process_handle)
            return
        waiter.add_done_callback(lambda _task: _close_windows_handle(process_handle))


async def spawn_managed_stdio_process(
    command: str,
    *args: str,
    cwd: str,
    env: dict[str, str],
    limit: int,
    parent_env: dict[str, str] | None = None,
) -> ManagedStdioProcess:
    """Spawn an owned stdio child with POSIX-session or Windows-Job semantics."""
    if type(command) is not str or type(cwd) is not str:
        raise TypeError("Subprocess command and cwd must be exact strings.")
    if "\x00" in command or "\x00" in cwd:
        raise ValueError("Subprocess command and cwd cannot contain NUL.")
    if any(type(arg) is not str for arg in args):
        raise TypeError("Subprocess arguments must be exact strings.")
    if any("\x00" in arg for arg in args):
        raise ValueError("Subprocess arguments cannot contain NUL.")

    from chrys.foundation.platform import get_platform

    if get_platform().is_windows:
        try:
            return await _spawn_windows_managed_stdio_process(
                command,
                args,
                cwd=cwd,
                env=env,
                limit=limit,
                parent_env=parent_env,
            )
        except OSError as exc:
            raise_if_missing_cwd(cwd, exc)
            raise

    try:
        process = await _create_owned_subprocess(
            command,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=limit,
            start_new_session=True,
        )
    except OSError as exc:
        raise_if_missing_cwd(cwd, exc)
        raise
    if process.stdin is None or process.stdout is None or process.stderr is None or process.pid is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise RuntimeError("Managed subprocess did not expose complete stdio pipes.")
    return ManagedStdioProcess(
        stdin=process.stdin,
        stdout=process.stdout,
        stderr=process.stderr,
        pid=process.pid,
        process_group_id=process.pid,
        _process=process,
    )


def terminate_process_group(process_group_id: int) -> bool:
    """Send SIGTERM to a POSIX process group without signaling Chrys's own group."""
    from chrys.foundation.platform import get_platform

    if get_platform().is_windows or process_group_id <= 0:
        return False
    posix_os = cast(Any, os)
    with contextlib.suppress(OSError):
        if process_group_id == posix_os.getpgrp():
            return False
    try:
        posix_os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError, PermissionError, OSError:
        return False
    return True


@functools.cache
def _windows_process_api() -> _WindowsProcessAPI:
    """Load the Win32 process, Job Object, and attribute-list functions once."""
    kernel32 = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    declare_functions(
        kernel32,
        {
            "CreateJobObjectW": (wintypes.HANDLE, [wintypes.LPVOID, wintypes.LPCWSTR]),
            "SetInformationJobObject": (
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
            ),
            "InitializeProcThreadAttributeList": (
                wintypes.BOOL,
                [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_size_t)],
            ),
            # List, flags, attribute, value and its size, then the reserved previous-value and return-size slots.
            "UpdateProcThreadAttribute": (
                wintypes.BOOL,
                [
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    ctypes.c_size_t,
                    wintypes.LPVOID,
                    ctypes.c_size_t,
                    wintypes.LPVOID,
                    wintypes.LPVOID,
                ],
            ),
            "DeleteProcThreadAttributeList": (None, [wintypes.LPVOID]),
            "CreateProcessW": (
                wintypes.BOOL,
                [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    wintypes.LPVOID,
                    wintypes.LPVOID,
                    wintypes.BOOL,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.LPCWSTR,
                    ctypes.POINTER(_StartupInfoExW),
                    ctypes.POINTER(_ProcessInformation),
                ],
            ),
            "AssignProcessToJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.HANDLE]),
            "ResumeThread": (wintypes.DWORD, [wintypes.HANDLE]),
            "SetHandleInformation": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]),
            "QueryInformationJobObject": (
                wintypes.BOOL,
                [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPDWORD],
            ),
            "TerminateJobObject": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            "TerminateProcess": (wintypes.BOOL, [wintypes.HANDLE, wintypes.UINT]),
            "GetExitCodeProcess": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
            "CloseHandle": (wintypes.BOOL, [wintypes.HANDLE]),
        },
    )
    return _WindowsProcessAPI(kernel32=kernel32)


def _raise_windows_process_error(api: _WindowsProcessAPI, message: str) -> None:
    error = cast(Any, ctypes).get_last_error()
    raise OSError(error, message, None, error)


def _windows_create_job(api: _WindowsProcessAPI) -> int:
    job = api.kernel32.CreateJobObjectW(None, None)
    if not job:
        _raise_windows_process_error(api, "Unable to create a Windows Job Object.")
    info = _ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not api.kernel32.SetInformationJobObject(
        job,
        9,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        api.kernel32.CloseHandle(job)
        _raise_windows_process_error(api, "Unable to configure a Windows Job Object.")
    return int(job)


def _windows_environment_block(env: dict[str, str]) -> object:
    values = [f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].casefold())]
    return ctypes.create_unicode_buffer("\x00".join(values) + "\x00\x00")


_WINDOWS_DEFAULT_PATHEXT = (".COM", ".EXE", ".BAT", ".CMD")


def _windows_resolve_application(command: str, env: dict[str, str], cwd: str) -> str:
    """Resolve a Windows command against the child environment only.

    ``shutil.which`` is deliberately avoided: on Windows it prepends the
    *parent* process's current directory and reads ``PATHEXT`` from the parent
    environment, either of which can resolve a different program than the
    child's configured ``PATH`` names.
    """
    if ntpath.dirname(command):
        # Never ntpath.abspath here: on Windows it resolves drive-relative
        # paths ("C:tools\\agent.exe") through the PARENT process's per-drive
        # cwd (_getfullpathname). ntpath.join anchors same-drive and
        # driveless paths at the child cwd; a cross-drive drive-relative
        # result stays non-absolute and the spawn path rejects it.
        candidate = command if ntpath.isabs(command) else ntpath.join(cwd, command)
        return ntpath.normpath(candidate)
    # The child env is casefold-deduplicated but keeps the base env's original
    # key casing, so PATH/PATHEXT must be looked up case-insensitively.
    lookup = {key.casefold(): value for key, value in env.items()}
    raw_pathext = lookup.get("pathext", "")
    extensions = (
        tuple(
            extension if extension.startswith(".") else f".{extension}"
            for extension in (part.strip() for part in raw_pathext.split(";"))
            if extension
        )
        or _WINDOWS_DEFAULT_PATHEXT
    )
    has_extension = bool(ntpath.splitext(command)[1])
    for raw_directory in (lookup.get("path") or "").split(";"):
        directory = raw_directory.strip().strip('"')
        if not directory:
            continue
        # Relative PATH entries belong to the child: anchor them at the child
        # cwd, never at the Chrys process's own current directory.
        directory = ntpath.normpath(ntpath.join(cwd, directory))
        if not ntpath.isabs(directory):
            # A cross-drive drive-relative entry ("C:bin") is only resolvable
            # through the parent's per-drive cwd state; skip it entirely so
            # not even the isfile probe consults parent state.
            continue
        base = ntpath.join(directory, command)
        candidates = (base,) if has_extension else tuple(base + extension for extension in extensions)
        for candidate in candidates:
            if os.path.isfile(candidate) and not is_windows_store_python_stub(candidate):
                return ntpath.normpath(candidate)
    # Unresolved bare names are returned as-is so the spawn path fails closed
    # with a deterministic not-found error instead of searching implicit
    # locations such as the current directory.
    return ntpath.normpath(command)


def _make_windows_pipe_pairs() -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]]:
    from asyncio import windows_utils

    pairs: list[tuple[int, int]] = []
    try:
        while len(pairs) < 3:
            pairs.append(cast(Any, windows_utils).pipe(duplex=True, overlapped=(True, False)))
    except BaseException:
        for pair in pairs:
            for handle in pair:
                _close_windows_handle(handle)
        raise
    return pairs[0], pairs[1], pairs[2]


async def _wrap_windows_parent_pipes(
    stdin_parent: int,
    stdout_parent: int,
    stderr_parent: int,
    *,
    limit: int,
) -> tuple[
    asyncio.StreamWriter,
    asyncio.StreamReader,
    asyncio.StreamReader,
    asyncio.BaseTransport,
    asyncio.BaseTransport,
]:
    from asyncio import windows_utils

    loop = asyncio.get_running_loop()
    stdin_pipe = cast(Any, windows_utils).PipeHandle(stdin_parent)
    stdout_pipe = cast(Any, windows_utils).PipeHandle(stdout_parent)
    stderr_pipe = cast(Any, windows_utils).PipeHandle(stderr_parent)
    stdin_transport = None
    stdout_transport = None
    stderr_transport = None
    try:
        stdout = asyncio.StreamReader(limit=limit)
        stdout_protocol = asyncio.StreamReaderProtocol(stdout)
        stdout_transport, _ = await loop.connect_read_pipe(lambda: stdout_protocol, stdout_pipe)

        stderr = asyncio.StreamReader(limit=limit)
        stderr_protocol = asyncio.StreamReaderProtocol(stderr)
        stderr_transport, _ = await loop.connect_read_pipe(lambda: stderr_protocol, stderr_pipe)

        stdin_protocol = asyncio.streams.FlowControlMixin(loop=loop)
        stdin_transport, _ = await loop.connect_write_pipe(
            lambda: stdin_protocol,
            stdin_pipe,
        )
        stdin = asyncio.StreamWriter(stdin_transport, stdin_protocol, None, loop)
        return stdin, stdout, stderr, stdout_transport, stderr_transport
    except BaseException:
        for transport in (stdin_transport, stdout_transport, stderr_transport):
            if transport is not None:
                transport.close()
        for pipe, transport in (
            (stdin_pipe, stdin_transport),
            (stdout_pipe, stdout_transport),
            (stderr_pipe, stderr_transport),
        ):
            if transport is None:
                pipe.close()
        raise


type _WindowsAttribute = tuple[int, ctypes.Array[Any], str]
"""A process/thread attribute: its id, the array it points into, and the message if Windows rejects it."""


def _windows_create_process(
    api: _WindowsProcessAPI,
    application: str,
    command_line_text: str,
    *,
    flags: int,
    environment: object,
    cwd: str,
    stdio: tuple[int, int, int],
    attributes: tuple[_WindowsAttribute, ...],
    process_info: _ProcessInformation,
) -> int | None:
    """Create the process with *attributes* into *process_info*; return the error ``CreateProcessW`` failed with, if any.

    The attribute list points into the attributes' arrays, which the caller keeps referenced.
    """
    size = ctypes.c_size_t()
    api.kernel32.InitializeProcThreadAttributeList(None, len(attributes), 0, ctypes.byref(size))
    attribute_buffer = ctypes.create_string_buffer(size.value)
    attribute_list = ctypes.cast(attribute_buffer, wintypes.LPVOID)
    if not api.kernel32.InitializeProcThreadAttributeList(attribute_list, len(attributes), 0, ctypes.byref(size)):
        _raise_windows_process_error(api, "Unable to initialize Windows process attributes.")
    try:
        for attribute, value, failure in attributes:
            if not api.kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                attribute,
                ctypes.cast(value, wintypes.LPVOID),
                ctypes.sizeof(value),
                None,
                None,
            ):
                _raise_windows_process_error(api, failure)
        stdin, stdout, stderr = stdio
        startup = _StartupInfoExW(
            _StartupInfoW(
                cb=ctypes.sizeof(_StartupInfoExW),
                dwFlags=0x00000101,  # STARTF_USESHOWWINDOW | STARTF_USESTDHANDLES
                wShowWindow=0,  # SW_HIDE
                hStdInput=stdin,
                hStdOutput=stdout,
                hStdError=stderr,
            ),
            attribute_list,
        )
        # CreateProcessW may write to the command line, so every call gets a buffer of its own.
        command_line = ctypes.create_unicode_buffer(command_line_text)
        if api.kernel32.CreateProcessW(
            application,
            command_line,
            None,
            None,
            True,
            flags,
            environment,
            cwd,
            ctypes.byref(startup),
            ctypes.byref(process_info),
        ):
            return None
        return int(cast(Any, ctypes).get_last_error())
    finally:
        api.kernel32.DeleteProcThreadAttributeList(attribute_list)


async def _spawn_windows_managed_stdio_process(
    command: str,
    args: tuple[str, ...],
    *,
    cwd: str,
    env: dict[str, str],
    limit: int,
    parent_env: dict[str, str] | None,
) -> ManagedStdioProcess:
    api = _windows_process_api()
    job_handle = _windows_create_job(api)
    pipe_pairs: tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None = None
    process_handle: int | None = None
    thread_handle: int | None = None
    in_job = False
    try:
        pipe_pairs = _make_windows_pipe_pairs()
        (stdin_parent, stdin_child), (stdout_parent, stdout_child), (stderr_parent, stderr_child) = pipe_pairs
        HANDLE_FLAG_INHERIT = 0x00000001
        for child_handle in (stdin_child, stdout_child, stderr_child):
            if not api.kernel32.SetHandleInformation(child_handle, HANDLE_FLAG_INHERIT, HANDLE_FLAG_INHERIT):
                _raise_windows_process_error(api, "Unable to make a child stdio handle inheritable.")
        for parent_handle in (stdin_parent, stdout_parent, stderr_parent):
            if not api.kernel32.SetHandleInformation(parent_handle, HANDLE_FLAG_INHERIT, 0):
                _raise_windows_process_error(api, "Unable to protect a parent stdio handle.")

        application = _windows_resolve_application(command, env, cwd)
        if not ntpath.isabs(application):
            # A relative name would make the batch-shim existence check and
            # CreateProcessW itself resolve against the parent's current
            # directory instead of the child's configured environment.
            raise FileNotFoundError(errno.ENOENT, "Windows command was not found on the configured PATH.")
        if ntpath.splitext(application)[1].casefold() in _WINDOWS_BATCH_SUFFIXES:
            if not os.path.exists(application):
                raise FileNotFoundError(errno.ENOENT, "Windows batch shim does not exist.")
            if not os.path.isfile(application):
                raise IsADirectoryError(errno.EISDIR, "Windows batch shim is not a regular file.")
            shim = ntpath.abspath(application)
            batch_command = serialize_windows_batch_command(shim, args)
            application = resolve_windows_comspec(parent_env)
            command_line_text = f'"{application}" /d /s /v:off /c "{batch_command}"'
        else:
            argv = [application, *args]
            command_line_text = subprocess.list2cmdline(argv)
        environment = _windows_environment_block(env)

        # The child joins the job before it runs and inherits only its three stdio handles.
        stdio = (stdin_child, stdout_child, stderr_child)
        job_attribute: _WindowsAttribute = (
            _PROC_THREAD_ATTRIBUTE_JOB_LIST,
            (wintypes.HANDLE * 1)(job_handle),
            "Unable to attach the Windows Job Object atomically.",
        )
        handle_attribute: _WindowsAttribute = (
            _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
            (wintypes.HANDLE * 3)(*stdio),
            "Unable to restrict inherited Windows handles.",
        )
        creation_flags = _CREATE_NEW_CONSOLE | 0x00000400 | 0x00080000
        process_info = _ProcessInformation()
        create = functools.partial(
            _windows_create_process,
            api,
            application,
            command_line_text,
            environment=environment,
            cwd=cwd,
            stdio=stdio,
            process_info=process_info,
        )
        error = create(flags=creation_flags, attributes=(job_attribute, handle_attribute))
        # The denial can also come as NTSTATUS_FROM_WIN32(ERROR_ACCESS_DENIED), which ctypes reports signed.
        suspended = error is not None and (error & 0xFFFFFFFF) in (
            _ERROR_ACCESS_DENIED,
            _NTSTATUS_WIN32_ACCESS_DENIED,
        )
        if suspended:
            # Windows refuses the job attribute for a packaged (MSIX) executable, such as PowerShell
            # installed from the Store: that child is created suspended and runs once a job holds it.
            # The job the refused attempt named refuses the child too, so the child gets a new one;
            # it is made before the old one closes, so a failure leaves one live handle to close.
            refused_job, job_handle = job_handle, _windows_create_job(api)
            _close_windows_handle(refused_job)
            error = create(flags=creation_flags | _CREATE_SUSPENDED, attributes=(handle_attribute,))
        if error is not None:
            # Says when the fallback ran too, so a report shows whether the job attribute was all it refused.
            detail = " even without the Job Object attribute" if suspended else ""
            raise OSError(error, f"Unable to create the Windows subprocess{detail}.", None, error)
        process_handle = int(process_info.hProcess)
        thread_handle = int(process_info.hThread)
        in_job = not suspended
        if suspended:
            if not api.kernel32.AssignProcessToJobObject(job_handle, process_handle):
                _raise_windows_process_error(api, "Unable to assign the Windows subprocess to its Job Object.")
            in_job = True
            if api.kernel32.ResumeThread(thread_handle) == _RESUME_THREAD_FAILED:
                _raise_windows_process_error(api, "Unable to start the Windows subprocess.")
        api.kernel32.CloseHandle(thread_handle)
        thread_handle = None
        for child_handle in stdio:
            api.kernel32.CloseHandle(child_handle)
        # _wrap_windows_parent_pipes owns the parent handles from here, including on failure.
        pipe_pairs = None
        stdin, stdout, stderr, stdout_transport, stderr_transport = await _wrap_windows_parent_pipes(
            stdin_parent,
            stdout_parent,
            stderr_parent,
            limit=limit,
        )
        return ManagedStdioProcess(
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            pid=int(process_info.dwProcessId),
            _windows_process_handle=process_handle,
            _windows_job_handle=job_handle,
            _windows_stdout_transport=stdout_transport,
            _windows_stderr_transport=stderr_transport,
        )
    except BaseException:
        # Ending the job ends nothing the job has not taken yet: a suspended child is terminated itself.
        _terminate_windows_retained_process(job_handle if in_job else None, process_handle, exit_code=1)
        _close_windows_handle(thread_handle)
        _close_windows_handle(process_handle)
        _close_windows_handle(job_handle)
        if pipe_pairs is not None:
            for pair in pipe_pairs:
                for handle in pair:
                    _close_windows_handle(handle)
        raise


async def _windows_process_exit(process_handle: int) -> int:
    """Complete when the process handle is signalled, then read its exit code."""
    await _register_windows_process_wait(process_handle)
    return _windows_process_exit_code(process_handle)


def _register_windows_process_wait(process_handle: int) -> asyncio.Future[bool]:
    """Register the process handle with the running loop's completion port.

    The Windows spawn path connects its pipes through ``ProactorEventLoop``, whose
    ``IocpProactor.wait_for_handle`` is the wait asyncio uses for its own subprocess
    exits: a kernel wait thread posts to the port when the process ends and the loop
    completes the future itself. Cancelling the future unregisters the wait.
    """
    loop = asyncio.get_running_loop()
    proactor = getattr(loop, "_proactor", None)
    if proactor is None:
        raise RuntimeError("Windows subprocess waits need the proactor event loop that connected the pipes.")
    return proactor.wait_for_handle(process_handle)


def _windows_process_exit_code(process_handle: int) -> int:
    api = _windows_process_api()
    exit_code = wintypes.DWORD()
    if not api.kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        _raise_windows_process_error(api, "Unable to read the Windows subprocess exit code.")
    return int(exit_code.value)


def _terminate_windows_retained_process(
    job_handle: int | None,
    process_handle: int | None,
    *,
    exit_code: int,
) -> None:
    if not job_handle and not process_handle:
        return
    api = _windows_process_api()
    terminated = bool(job_handle and api.kernel32.TerminateJobObject(job_handle, exit_code))
    if not terminated and process_handle:
        api.kernel32.TerminateProcess(process_handle, exit_code)


def _consume_task_exception(task: asyncio.Task[Any]) -> None:
    """Mark abandoned waiter failures retrieved without changing await semantics."""
    if not task.cancelled():
        task.exception()


def _close_windows_handle(handle: int | None) -> None:
    if not handle:
        return
    api = _windows_process_api()
    api.kernel32.CloseHandle(handle)


async def _managed_subprocess_gen(*args: Any, **kwargs: Any) -> AsyncGenerator[asyncio.subprocess.Process]:
    """Generator backing :func:`managed_subprocess`."""
    if (
        sys.platform != "win32"
        and "start_new_session" not in kwargs
        and "process_group" not in kwargs
        and "preexec_fn" not in kwargs
    ):
        # Own a POSIX process group so cleanup reaches grandchildren that
        # inherit pipes and outlive the wrapper process.
        kwargs["start_new_session"] = True

    # On Windows, give the child its own console so it cannot call
    # SetConsoleMode() on the *parent's* console (which would reset
    # mouse-input flags and break TUI interactions).
    #
    # CREATE_NEW_CONSOLE — child gets a separate console, so cmd.exe,
    #   pwsh, and powershell still work (they need a console for internal
    #   commands like echo, dir, etc.) but cannot touch the parent's.
    # STARTF_USESHOWWINDOW + SW_HIDE — suppress the new console window
    #   so it doesn't flash on screen.
    #
    # Why not the alternatives?
    # - CREATE_NO_WINDOW: child shares the parent's console → can modify it.
    # - DETACHED_PROCESS: child has NO console → cmd.exe / pwsh / powershell fail.
    if sys.platform == "win32" and "creationflags" not in kwargs:
        for k, v in windows_hidden_subprocess_kwargs().items():
            kwargs.setdefault(k, v)

    # Detach from the parent's stdin unless a caller asked for something else.
    # Under ACP that descriptor carries the JSON-RPC stream, and a child that
    # inherits it can consume protocol frames. Defaulting here rather than at
    # each call site means a new caller is safe by omission, which is also what
    # lets the static sweep accept the ``**kwargs`` splat below as proof.
    kwargs.setdefault("stdin", subprocess.DEVNULL)

    try:
        proc = await _create_owned_subprocess(*args, **kwargs)
    except OSError as exc:
        raise_if_missing_cwd(kwargs.get("cwd"), exc)
        raise
    process_group_id = _infer_process_group_id(proc, kwargs)
    body_raised = False
    try:
        yield proc
    except BaseException:
        body_raised = True
        raise
    finally:
        should_kill_owned_tree = body_raised or proc.returncode is None
        if process_group_id is not None and should_kill_owned_tree:
            kill_process_group(process_group_id)
        try:
            if proc.pid is not None and should_kill_owned_tree:
                # Returns at once off Windows, where the group kill above reached the tree.
                await kill_windows_process_tree(proc.pid)
        finally:
            if proc.returncode is None:
                # Keep the direct child moving if process-group cleanup was
                # unavailable or a custom setup kept the child outside the group.
                with contextlib.suppress(ProcessLookupError, OSError):
                    proc.kill()
                # Close the subprocess transport BEFORE wait() so pipe
                # transports are released immediately.  Without this,
                # wait() blocks until all inherited pipe handles close —
                # which hangs when a wrapper process (e.g. `uv run python`)
                # spawns a child that inherits the pipes and outlives the
                # killed parent.
                finalize_subprocess_transport(proc)
                with contextlib.suppress(ProcessLookupError, OSError, asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(proc.wait(), timeout=_PROCESS_WAIT_CLEANUP_TIMEOUT)
            else:
                # Process already exited — just release the transport.
                finalize_subprocess_transport(proc)


managed_subprocess = contextlib.asynccontextmanager(_managed_subprocess_gen)
"""Create a subprocess with guaranteed transport cleanup.

Wraps ``asyncio.create_subprocess_exec`` and ensures that on exit:

1. The POSIX process group is killed on abnormal exit or unfinished child
2. The direct child is killed if it hasn't exited yet
3. The direct child is waited/reaped (prevents zombie processes)
4. The subprocess transport is closed (prevents Windows Proactor
   ``ResourceWarning`` during interpreter shutdown)

Usage::

    async with managed_subprocess("rg", "--files", stdout=PIPE) as proc:
        stdout, stderr = await proc.communicate()
"""


async def wait_for_subprocess[T](
    awaitable: Awaitable[T],
    *,
    timeout: float | None,
    process_group_id: int | None = None,
    process_session_id: int | None = None,
) -> T:
    """Await subprocess work with timeout and stopped-process detection.

    ``asyncio`` only completes ``Process.wait()`` when the direct child exits;
    a POSIX child or grandchild can enter job-control stopped state and keep
    pipes or a PTY slave open forever.  When a process group or session id is
    supplied, this helper polls for stopped members and raises
    :class:`SubprocessStoppedError` as soon as one is found so callers can kill
    the owned process set.
    """
    if process_group_id is None and process_session_id is None:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    if sys.platform == "win32":
        return await asyncio.wait_for(awaitable, timeout=timeout)

    operation = asyncio.ensure_future(awaitable)
    if process_session_id is None:
        monitor = asyncio.create_task(_monitor_process_group_stopped(cast(int, process_group_id)))
    else:
        monitor = asyncio.create_task(_monitor_processes_stopped(process_group_id, process_session_id))
    try:
        done, _pending = await asyncio.wait(
            {operation, monitor},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            await _cancel_subprocess_wait(operation)
            raise TimeoutError
        if operation in done:
            return await operation

        await _cancel_subprocess_wait(operation)
        return await monitor
    except BaseException:
        if not operation.done():
            await _cancel_subprocess_wait(operation)
        raise
    finally:
        monitor.cancel()
        with contextlib.suppress(asyncio.CancelledError, SubprocessStoppedError):
            await monitor


async def _cancel_subprocess_wait(task: asyncio.Future[Any]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await asyncio.wait_for(task, timeout=_PROCESS_WAIT_CLEANUP_TIMEOUT)


def kill_process_group(process_group_id: int) -> bool:
    """Kill a POSIX process group without signaling Chrys's own group."""
    if sys.platform == "win32" or process_group_id <= 0:
        return False
    with contextlib.suppress(OSError):
        if process_group_id == os.getpgrp():
            return False
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return False
    except PermissionError:
        return False
    except OSError:
        return False
    return True


def signal_process_session(process_session_id: int, signum: int) -> bool:
    """Send *signum* to every process group in a POSIX session owned by Chrys.

    macOS can refuse a signal to a whole group (EPERM) even when each member
    accepts one on its own, so a refused group is signalled member by member.
    """
    if sys.platform == "win32" or process_session_id <= 0:
        return False
    with contextlib.suppress(OSError):
        if process_session_id == os.getsid(0):
            return False
    members = [
        status
        for status in _iter_posix_process_statuses()
        if status.session_id == process_session_id and status.pgid > 0
    ]
    own_group = os.getpgrp()
    sent = False
    for process_group_id in sorted({status.pgid for status in members} - {own_group}):
        try:
            os.killpg(process_group_id, signum)
        except PermissionError:
            for status in members:
                if status.pgid == process_group_id:
                    with contextlib.suppress(OSError):
                        os.kill(status.pid, signum)
                        sent = True
        except OSError:
            continue
        else:
            sent = True
    return sent


def session_processes(session_id: int) -> dict[int, float]:
    """The live processes of the POSIX session *session_id*, each with the CPU seconds it used so far.

    A zombie is no longer alive. A process of another user counts, with no CPU
    time: that is not ours to read. Blocking: run it in a thread.
    """
    if sys.platform == "win32" or session_id <= 0:
        return {}
    import psutil

    found: dict[int, float] = {}
    for pid in psutil.pids():
        try:
            if os.getsid(pid) != session_id:
                continue
            cpu = _cpu_seconds(psutil.Process(pid))
        except OSError, psutil.NoSuchProcess:
            continue
        if cpu is not None:
            found[pid] = cpu
    return found


def _windows_job_processes(job_handle: int) -> dict[int, float]:
    import psutil

    api = _windows_process_api()
    listing = _JobProcessIdList()
    listed = api.kernel32.QueryInformationJobObject(job_handle, 3, ctypes.byref(listing), ctypes.sizeof(listing), None)
    # A job with more processes than the list holds fills it and fails with ERROR_MORE_DATA.
    if not listed and cast(Any, ctypes).get_last_error() != 234:
        return {}
    found: dict[int, float] = {}
    for pid in listing.ProcessIdList[: listing.NumberOfProcessIdsInList]:
        try:
            process = psutil.Process(pid)
            # conhost.exe serves the console a process of the job opened, and outlives it a little.
            if process.name().casefold() == "conhost.exe":
                continue
            cpu = _cpu_seconds(process)
        except psutil.NoSuchProcess:
            continue
        except psutil.AccessDenied:
            cpu = 0.0
        if cpu is not None:
            found[pid] = cpu
    return found


def _cpu_seconds(process: Any) -> float | None:
    """The CPU seconds the psutil *process* and the children it reaped used, or None once it is a zombie."""
    import psutil

    with process.oneshot():
        try:
            if process.status() == psutil.STATUS_ZOMBIE:
                return None
            times = process.cpu_times()
        except psutil.AccessDenied:
            return 0.0
    return times.user + times.system + times.children_user + times.children_system


async def kill_windows_process_tree(pid: int) -> bool:
    """Best-effort Windows subtree kill using the platform ``taskkill`` utility.

    ``taskkill`` runs as an asyncio subprocess, whose exit the loop awaits on its
    completion port (``IocpProactor.wait_for_handle``, as for every asyncio
    subprocess), so the loop keeps serving other work meanwhile. A blocking run
    stalled the whole TUI on every hook timeout, and concurrent timeouts queued
    behind one another.

    A cancelled caller waits for the bounded kill to end before its cancellation
    goes on. Stopping ``taskkill`` part-way would leave the caller to kill only the
    direct child, and the descendants ``taskkill`` had yet to reach would outlive it.
    """
    argv = _windows_tree_kill_argv(pid)
    if argv is None:
        return False
    kill = asyncio.ensure_future(_run_windows_tree_kill(argv))
    try:
        return await asyncio.shield(kill)
    except asyncio.CancelledError:
        while not kill.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.shield(kill)
        raise


async def _run_windows_tree_kill(argv: list[str]) -> bool:
    try:
        killer = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **windows_hidden_subprocess_kwargs(),
        )
    except OSError:
        return False
    try:
        return await asyncio.wait_for(killer.wait(), timeout=_WINDOWS_TREE_KILL_TIMEOUT) == 0
    except TimeoutError:
        return False
    finally:
        if killer.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                killer.kill()
        finalize_subprocess_transport(killer)


def _windows_tree_kill_argv(pid: int) -> list[str] | None:
    if sys.platform != "win32" or pid <= 0:
        return None
    taskkill = shutil.which("taskkill")
    if taskkill is None:
        return None
    return [taskkill, "/PID", str(pid), "/T", "/F"]


async def _create_owned_subprocess(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
    """Start a subprocess that a cancel arriving while it starts cannot leave running.

    ``create_subprocess_exec`` returns only once the child's pipes are connected,
    but the child runs from the fork on. Cancelled in between, asyncio kills the
    direct child alone, and whatever it started (a shell's commands) runs on with
    no owner. So the start finishes under a shield, and a cancel that came
    meanwhile ends the child's whole tree before it is passed on. That ending
    runs as one task drained to its end: a second cancel cannot cut it short.
    """
    # Callers pass their own stdin; detached by default as in :func:`managed_subprocess`.
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    if len(args) == 1 and isinstance(args[0], WindowsShellCommand):
        if sys.platform != "win32":
            raise ValueError("A Windows shell command runs only on Windows.")
        start = asyncio.create_subprocess_shell(args[0].command, executable=resolve_windows_comspec(), **kwargs)
    else:
        start = asyncio.create_subprocess_exec(*args, **kwargs)
    spawn = asyncio.ensure_future(start)
    try:
        return await asyncio.shield(spawn)
    except asyncio.CancelledError:
        await finish_close(asyncio.create_task(_end_started_child(spawn, kwargs)))
        raise


async def _end_started_child(spawn: asyncio.Future[asyncio.subprocess.Process], kwargs: dict[str, Any]) -> None:
    """Wait a bounded time for the start a cancel interrupted, then end the child's whole tree.

    A cancel must always be able to end, so a start that does not finish in
    time (a working folder on a stalled network disk) is not waited for: the
    caller's cancel goes on, and the child's tree is ended whenever the start
    finishes (:func:`_end_child_once_started`).
    """
    await asyncio.wait({spawn}, timeout=_PROCESS_WAIT_CLEANUP_TIMEOUT)
    if not spawn.done():
        _end_child_once_started(spawn, kwargs)
        return
    if spawn.cancelled() or spawn.exception() is not None:
        # A start that failed left nothing running; the cancel is what the caller hears.
        return
    await _end_child_tree(spawn.result(), kwargs)


def _end_child_once_started(spawn: asyncio.Future[asyncio.subprocess.Process], kwargs: dict[str, Any]) -> None:
    """End the tree of a start that finishes after its cancel went on, or give the start up after a grace.

    Cancelling the start instead would end only the direct child (asyncio
    kills that one), leaving whatever it started running with no owner; that
    is the last resort, for a start still stuck after :data:`_LATE_START_GRACE`.
    """
    loop = asyncio.get_running_loop()
    give_up = loop.call_later(_LATE_START_GRACE, spawn.cancel)

    def started(future: asyncio.Future[asyncio.subprocess.Process]) -> None:
        give_up.cancel()
        # Read here, so a failure is not logged as never retrieved.
        if future.cancelled() or future.exception() is not None:
            return
        ending = loop.create_task(_end_child_tree(future.result(), kwargs))
        _LATE_CHILD_ENDINGS.add(ending)
        ending.add_done_callback(_LATE_CHILD_ENDINGS.discard)

    spawn.add_done_callback(started)


async def _end_child_tree(proc: asyncio.subprocess.Process, kwargs: dict[str, Any]) -> None:
    """End a started child and everything it started, then reap it within a bounded wait."""
    process_group_id = _infer_process_group_id(proc, kwargs)
    if process_group_id is not None:
        kill_process_group(process_group_id)
    if proc.pid is not None:
        await kill_windows_process_tree(proc.pid)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()
    finalize_subprocess_transport(proc)
    with contextlib.suppress(ProcessLookupError, OSError, TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_PROCESS_WAIT_CLEANUP_TIMEOUT)


def _infer_process_group_id(proc: asyncio.subprocess.Process, kwargs: dict[str, Any]) -> int | None:
    if sys.platform == "win32" or proc.pid is None:
        return None
    if kwargs.get("start_new_session") is True:
        return proc.pid
    process_group = kwargs.get("process_group")
    if process_group == 0:
        return proc.pid
    if isinstance(process_group, int) and process_group > 0:
        return process_group
    return None


async def _monitor_process_group_stopped(process_group_id: int) -> Never:
    while True:
        await asyncio.sleep(_STOPPED_PROCESS_POLL_INTERVAL)
        if await asyncio.to_thread(_process_group_has_stopped_member, process_group_id):
            msg = f"Subprocess process group {process_group_id} entered stopped state."
            raise SubprocessStoppedError(msg)


async def _monitor_processes_stopped(process_group_id: int | None, process_session_id: int) -> Never:
    while True:
        await asyncio.sleep(_STOPPED_PROCESS_POLL_INTERVAL)
        if await asyncio.to_thread(_processes_have_stopped_member, process_group_id, process_session_id):
            msg = f"Subprocess process session {process_session_id} entered stopped state."
            raise SubprocessStoppedError(msg)


def _processes_have_stopped_member(process_group_id: int | None, process_session_id: int) -> bool:
    if _process_session_has_stopped_member(process_session_id):
        return True
    return process_group_id is not None and _process_group_has_stopped_member(process_group_id)


def _process_group_has_stopped_member(process_group_id: int) -> bool:
    if sys.platform == "win32" or process_group_id <= 0:
        return False
    ps = shutil.which("ps")
    if ps is None:
        return False
    try:
        result = subprocess.run(  # noqa: S603
            [ps, "-e", "-o", "state=", "-o", "pgid="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=1,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return _process_listing_has_stopped_group_member(result.stdout, process_group_id)


def _process_listing_has_stopped_group_member(raw: bytes, process_group_id: int) -> bool:
    for line in raw.decode("ascii", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.rsplit(None, 1)
        if len(parts) != 2:
            continue
        state, pgid_text = parts
        try:
            pgid = int(pgid_text)
        except ValueError:
            continue
        if pgid == process_group_id and state[:1] in {"T", "t"}:
            return True
    return False


def _process_session_has_stopped_member(process_session_id: int) -> bool:
    if sys.platform == "win32" or process_session_id <= 0:
        return False
    return _process_statuses_have_stopped_session_member(_iter_posix_process_statuses(), process_session_id)


def _process_statuses_have_stopped_session_member(statuses: Iterable[_ProcessStatus], process_session_id: int) -> bool:
    return any(status.session_id == process_session_id and status.state[:1] in {"T", "t"} for status in statuses)


def _iter_posix_process_statuses() -> Iterator[_ProcessStatus]:
    if sys.platform == "win32":
        return
    if sys.platform == "linux":
        yielded = False
        for status in _iter_linux_process_statuses():
            yielded = True
            yield status
        if yielded:
            return
    yield from _iter_ps_process_statuses()


def _iter_linux_process_statuses() -> Iterator[_ProcessStatus]:
    if sys.platform != "linux":
        return
    with contextlib.suppress(OSError), os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            with contextlib.suppress(OSError, UnicodeDecodeError):
                with open(f"/proc/{entry.name}/stat", encoding="utf-8") as stat_file:
                    status = _parse_linux_proc_stat(stat_file.read())
                if status is not None:
                    yield status


def _parse_linux_proc_stat(raw: str) -> _ProcessStatus | None:
    close_paren = raw.rfind(")")
    if close_paren < 0:
        return None
    pid_text = raw[:close_paren].partition("(")[0]
    values = raw[close_paren + 1 :].split()
    if len(values) < 4:
        return None
    try:
        return _ProcessStatus(
            pid=int(pid_text.strip()),
            state=values[0],
            pgid=int(values[2]),
            session_id=int(values[3]),
        )
    except ValueError:
        return None


def _iter_ps_process_statuses() -> Iterator[_ProcessStatus]:
    ps = shutil.which("ps")
    if ps is None:
        return
    try:
        result = subprocess.run(  # noqa: S603
            [ps, "-e", "-o", "pid=", "-o", "state="],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=1,
        )
    except OSError, subprocess.SubprocessError:
        return
    if result.returncode != 0:
        return
    for pid, state in _parse_ps_pid_state_listing(result.stdout):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            posix_os = cast(Any, os)
            pgid = posix_os.getpgid(pid)
            session_id = posix_os.getsid(pid)
            yield _ProcessStatus(pid=pid, state=state, pgid=pgid, session_id=session_id)


def _parse_ps_pid_state_listing(raw: bytes) -> Iterator[tuple[int, str]]:
    for line in raw.decode("ascii", errors="ignore").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            yield int(parts[0]), parts[1]
        except ValueError:
            continue


def decode_subprocess_output(raw: bytes | bytearray) -> str:
    """Decode subprocess output bytes to str, strongly preferring UTF-8.

    The generic ``decode_bytes`` runs a multi-encoding detection pipeline
    that can misidentify ANSI-heavy terminal output as UTF-16: ESC bytes
    (0x1b) inflate the control-character ratio past the text-quality
    threshold, causing the UTF-8 fast-path to be rejected, while ASCII
    byte pairs happen to decode as valid CJK under UTF-16.

    Strategy (aligned with Ansible / CPython / xterm.js best practice):

    1. **UTF-16 probe on Windows** -- catches PowerShell / ``cmd /u``
       output that otherwise looks like valid UTF-8 with embedded NULs.
    2. **Strict UTF-8** -- fast path for the overwhelming common case.
    3. **Tolerant UTF-8** -- handles PTY reads that split multi-byte
       characters at buffer boundaries.  Uses ``surrogateescape`` to
       count bad bytes accurately (each maps 1:1 to a surrogate), then
       re-encodes and decodes with ``replace`` so the returned string
       is safe for JSON serialization and terminal rendering.
    4. **Full detection (Windows only)** -- falls back to ``decode_bytes``
       for legacy ANSI code page output (GBK, Shift-JIS, etc.).  On
       Unix/macOS the locale is always UTF-8, so detection adds risk
       of misidentification with no benefit.
    """
    if not raw:
        return ""
    if sys.platform == "win32":
        utf16_text = _decode_utf16_output(raw)
        if utf16_text is not None:
            return utf16_text
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # On Windows with a legacy code page (GBK, Shift-JIS, cp1252, …),
        # non-UTF-8 bytes are likely valid in the system encoding.  Try
        # the known console code page first (fast, accurate) before
        # falling back to statistical detection (charset_normalizer).
        # Without this, mostly-ASCII output with a few CJK characters
        # falls below the surrogate threshold and gets garbled by
        # ``errors="replace"`` before the code-page fallback is reached.
        if sys.platform == "win32" and not _windows_uses_utf8():
            cp_enc = _windows_console_encoding()
            if cp_enc:
                try:
                    decoded = raw.decode(cp_enc)
                    if _is_subprocess_text(decoded):
                        return decoded
                except UnicodeDecodeError, LookupError:
                    pass
            return decode_bytes(raw)
        # Unix/macOS: locale is always UTF-8, so non-UTF-8 bytes are
        # buffer-boundary splits, not a different encoding.  Use
        # surrogateescape to count bad bytes; replace if few enough.
        probed = raw.decode("utf-8", errors="surrogateescape")
        surrogates = sum(1 for ch in probed if "\udc80" <= ch <= "\udcff")
        if surrogates / max(len(probed), 1) < 0.1:
            return raw.decode("utf-8", errors="replace")
    return raw.decode("utf-8", errors="replace")


def _is_subprocess_text(decoded: str) -> bool:
    """Return True for regular text or NUL-delimited text records."""
    if is_mostly_text(decoded):
        return True
    if "\x00" not in decoded:
        return False

    parts = decoded.split("\x00")
    if parts and parts[-1] == "":
        parts.pop()
    if not parts or any(part == "" for part in parts):
        return False

    return is_mostly_text("".join(parts))


def subprocess_output_utf16_codec(raw: bytes | bytearray) -> str | None:
    """The UTF-16 codec :func:`decode_subprocess_output` reads *raw* with, or None when it reads bytes.

    *raw* may be the start of a stream and end inside a code unit. ``"utf-16"``
    (which drops the BOM) when *raw* starts with one.
    """
    data = bytes(raw[: len(raw) - len(raw) % 2])
    if sys.platform != "win32" or _decode_utf16_output(data) is None:
        return None
    layout = _utf16_layout(data)
    if layout is None:
        return None
    codec, bom_length = layout
    return "utf-16" if bom_length else codec


def _decode_utf16_output(raw: bytes | bytearray) -> str | None:
    """Decode subprocess output that is clearly UTF-16 text."""
    data = bytes(raw)
    layout = _utf16_layout(data)
    if layout is None:
        return None
    codec, bom_length = layout
    try:
        decoded = data[bom_length:].decode(codec)
    except UnicodeDecodeError:
        return None
    # ASCII-only UTF-16 without a BOM is indistinguishable from NUL-delimited
    # UTF-8/ASCII output.  Preserve the bytes in that ambiguous case.
    if not bom_length and decoded.isascii():
        return None
    return decoded if is_mostly_text(decoded) else None


def _utf16_layout(data: bytes) -> tuple[str, int] | None:
    """Return the UTF-16 codec *data*'s BOM or NUL layout shows, and the BOM's length."""
    if data.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", len(codecs.BOM_UTF16_LE)
    if data.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", len(codecs.BOM_UTF16_BE)
    if len(data) < 4 or b"\x00" not in data:
        return None

    even = data[0::2]
    odd = data[1::2]
    even_nul_ratio = even.count(0) / len(even)
    odd_nul_ratio = odd.count(0) / len(odd)
    if odd_nul_ratio >= 0.20 and odd_nul_ratio >= max(even_nul_ratio * 4, 0.20):
        return "utf-16-le", 0
    if even_nul_ratio >= 0.20 and even_nul_ratio >= max(odd_nul_ratio * 4, 0.20):
        return "utf-16-be", 0
    return None


def decode_split_output(head: bytes, tail: bytes, tail_offset: int) -> tuple[str, str]:
    """Decode the kept head and tail of an output stream whose middle was dropped.

    *tail_offset* is where *tail* started in the stream. One codec, chosen
    from both parts so the cut cannot flip the choice, decodes both, and only
    the edges at the cut are repaired: a character the cut left incomplete at
    the end of *head* is dropped, and *tail* starts at its first whole
    character. When no one codec fits both parts, each is decoded on its own
    with :func:`decode_subprocess_output`.
    """
    if sys.platform == "win32":
        utf16 = _decode_utf16_split(head, tail, tail_offset)
        if utf16 is not None:
            return utf16
    skipped = _utf8_continuation_prefix_length(tail)
    utf8_tail = tail[skipped:]
    if sys.platform != "win32":
        return _decode_split_with("utf-8", head, utf8_tail)

    code_page = None if _windows_uses_utf8() else _windows_console_encoding()
    utf8 = _strict_split("utf-8", head, utf8_tail)
    if utf8 is not None:
        head_text, tail_text = utf8
        repaired = skipped > 0 or len(head_text.encode("utf-8")) < len(head)
        # Bytes dropped as the pieces of a character the cut split are no
        # evidence for UTF-8 when every character kept is ASCII: the console
        # code page may read those bytes as text.
        if not (code_page and repaired and head_text.isascii() and tail_text.isascii()):
            return utf8
    if code_page:
        with contextlib.suppress(LookupError):
            # A cut inside a double-byte character leaves its trail byte first.
            parts = next(
                (parts for k in range(4) if (parts := _strict_split(code_page, head, tail[k:])) is not None), None
            )
            if parts is not None and all(_is_subprocess_text(part) for part in parts):
                return parts
    if utf8 is not None:
        return utf8
    return decode_subprocess_output(head), decode_subprocess_output(tail)


def _utf8_continuation_prefix_length(data: bytes) -> int:
    """Count the UTF-8 continuation bytes, at most three, that start *data*."""
    length = 0
    while length < min(3, len(data)) and data[length] & 0xC0 == 0x80:
        length += 1
    return length


def _strict_split(codec: str, head: bytes, tail: bytes) -> tuple[str, str] | None:
    """Decode both parts with *codec*, dropping a cut character at the end of *head*; None when either fails."""
    try:
        head_text = codecs.getincrementaldecoder(codec)("strict").decode(head, final=False)
        return head_text, codecs.getincrementaldecoder(codec)("strict").decode(tail, final=True)
    except UnicodeDecodeError:
        return None


def _decode_split_with(codec: str, head: bytes, tail: bytes) -> tuple[str, str]:
    head_text = codecs.getincrementaldecoder(codec)("replace").decode(head, final=False)
    return head_text, codecs.getincrementaldecoder(codec)("replace").decode(tail, final=True)


def _decode_utf16_split(head: bytes, tail: bytes, tail_offset: int) -> tuple[str, str] | None:
    """Decode both parts as UTF-16 when *head* clearly is UTF-16 text."""
    layout = _utf16_layout(head[: len(head) - len(head) % 2])
    if layout is None:
        return None
    codec, bom_length = layout
    tail = tail[tail_offset % 2 :]
    # The cut may split a surrogate pair; its low half cannot start a character.
    high_byte = 1 if codec == "utf-16-le" else 0
    if len(tail) >= 2 and 0xDC <= tail[high_byte] <= 0xDF:
        tail = tail[2:]
    head_text, tail_text = _decode_split_with(codec, head[bom_length:], tail)
    if not bom_length and head_text.isascii() and tail_text.isascii():
        return None
    return (head_text, tail_text) if is_mostly_text(head_text + tail_text) else None
