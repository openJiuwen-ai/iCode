# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chrome/Edge subprocess ownership, discovery and the browser-level endpoint.

The plugin fully owns the browser process it starts - chrys never sees a handle.
On Windows the child is created suspended, placed in a Job object with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` and only then resumed, so nothing spawned
between ``Popen`` returning and the job being attached can escape; killing the
job kills the whole tree. On POSIX the child gets its own session (process
group) and a ``PR_SET_PDEATHSIG`` that is re-checked immediately, so it never
outlives the plugin.

The launch sequence is fixed and mirrors the design (3.2 / 11.3):

1. ``reclaim_orphans`` - recover a browser left by a previous generation.
2. ``discover_chrome`` (or ``cfg.executable``) - never at ``initialize`` time.
3. spawn with ``stdin=DEVNULL`` and stdout/stderr redirected to a **file**.
4. wait for ``DevToolsActivePort`` then GET ``/json/version`` for the browser
   WebSocket. ``--remote-allow-origins`` is never added.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import ctypes
import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Final, Literal, cast

from .._proc import pid_alive, rotate_log_if_needed, which_trusted as _which_trusted, windows_hidden_kwargs
from ..config import CHROME_READY_TIMEOUT, BrowserConfig
from ..errors import Unavailable
from ..logging import log

#: How often the two readiness/reaping polls tick (design: 50 ms).
_POLL_INTERVAL: Final = 0.05
#: Grace given to a browser to die on its own before the tree is force-killed.
_KILL_TIMEOUT: Final = 5.0
#: Bounded flush window for a graceful ``Browser.close`` / ``SIGTERM`` exit so
#: Chrome/Edge can flush persistent cookies and profile state to disk before force-kill.
_GRACEFUL_CLOSE_TIMEOUT: Final = 3.0
#: Timeout for the tiny ``/json/version`` HTTP probe.
_HTTP_TIMEOUT: Final = 5.0

#: Windows: ``CREATE_SUSPENDED`` so the job can be attached before any child runs.
_CREATE_SUSPENDED: Final = 0x00000004

#: POSIX: ``prctl`` option and signal used for ``PR_SET_PDEATHSIG``.
_PR_SET_PDEATHSIG: Final = 1
_SIGKILL: Final = 9

_CHROME_WHICH_NAMES: Final = (
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
)

_WHICH_NAMES: Final = (
    *_CHROME_WHICH_NAMES,
    "msedge",
    "microsoft-edge",
)

_LIVE_CHILDREN_LOCK = threading.Lock()
_LIVE_CHILDREN: dict[int, ChildProcess] = {}
_ATEXIT_REGISTERED = False


def _register_live_child(child: ChildProcess) -> None:
    global _ATEXIT_REGISTERED
    with _LIVE_CHILDREN_LOCK:
        _LIVE_CHILDREN[id(child)] = child
        if not _ATEXIT_REGISTERED:
            atexit.register(_cleanup_live_children)
            _ATEXIT_REGISTERED = True


def _unregister_live_child(child: ChildProcess) -> None:
    with _LIVE_CHILDREN_LOCK:
        _LIVE_CHILDREN.pop(id(child), None)


def _cleanup_live_children() -> None:
    """Gracefully close (then force-kill if needed) any Owned browsers still alive on host exit."""
    with _LIVE_CHILDREN_LOCK:
        children = list(_LIVE_CHILDREN.values())
        _LIVE_CHILDREN.clear()
    for child in children:
        with contextlib.suppress(Exception):
            child.terminate_tree_sync(timeout=1.5, graceful_wait=_GRACEFUL_CLOSE_TIMEOUT)


def _request_graceful_close_sync(pid: int, browser_ws: str) -> bool:
    """Ask a running browser to flush its profile and exit politely; return True if a signal/command was sent."""
    sent = False
    if browser_ws:
        with contextlib.suppress(Exception):
            asyncio.run(asyncio.wait_for(_send_browser_close(browser_ws), timeout=1.5))
            sent = True
    if not sent and sys.platform != "win32":
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            sent = True
    return sent


def _wait_popen_bounded(popen: subprocess.Popen[Any], timeout: float) -> bool:
    """Wait for *popen* up to *timeout* seconds; return True if exited (never leaks worker thread)."""
    try:
        popen.wait(timeout=max(0.0, timeout))
        return True
    except subprocess.TimeoutExpired:
        return False


@dataclass
class ChildProcess:
    """Ownership wrapper around the browser ``Popen`` (comparable by pid).

    ``Popen`` is kept in a ``compare=False`` field so equality/hash of the
    dataclass never touches the live handle.
    """

    pid: int
    start_time: float
    _popen: subprocess.Popen = field(compare=False, repr=False)
    _job: int | None = field(default=None, compare=False, repr=False)
    _terminated: bool = field(default=False, compare=False, repr=False)
    _pid_file: Path | None = field(default=None, compare=False, repr=False)
    _browser_ws: str = field(default="", compare=False, repr=False)
    _watchdog_fd: int | None = field(default=None, compare=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, compare=False, repr=False)

    @property
    def alive(self) -> bool:
        """True while the child has not exited."""
        return self._popen.poll() is None

    def _claim_termination(self) -> bool:
        with self._lock:
            if self._terminated:
                return False
            self._terminated = True
            return True

    def _take_job(self) -> int | None:
        with self._lock:
            job = self._job
            return job

    def _close_watchdog(self) -> None:
        with self._lock:
            fd = self._watchdog_fd
            self._watchdog_fd = None
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)

    async def wait_for_exit(self, *, timeout: float = _GRACEFUL_CLOSE_TIMEOUT) -> bool:
        """Wait up to *timeout* seconds for the browser to exit cleanly after ``Browser.close``."""
        with self._lock:
            if self._terminated:
                return True
        popen = self._popen
        if popen.poll() is None and timeout > 0:
            await asyncio.to_thread(_wait_popen_bounded, popen, timeout)
        if popen.poll() is not None:
            self._claim_termination()
            _unregister_live_child(self)
            self._close_watchdog()
            self._close_job()
            if self._pid_file is not None:
                _remove(self._pid_file)
            return True
        return False

    def terminate_tree_sync(
        self,
        *,
        timeout: float = _KILL_TIMEOUT,
        graceful_wait: float = 0.0,
    ) -> None:
        """Synchronously close (gracefully if requested, else force-kill) the process tree; idempotent."""
        if not self._claim_termination():
            return
        _unregister_live_child(self)
        popen = self._popen
        try:
            if popen.poll() is None and graceful_wait > 0 and self._browser_ws:
                if _request_graceful_close_sync(self.pid, self._browser_ws):
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        popen.wait(timeout=graceful_wait)
            if popen.poll() is None:
                if sys.platform == "win32":
                    _terminate_windows(self.pid, self._take_job())
                else:
                    _posix_kill_group(self.pid)
                try:
                    popen.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(Exception):
                        popen.kill()
        except Exception as exc:  # teardown must never propagate
            log(f"终止 Chrome 进程树失败（pid={self.pid}）：{exc!r}")
        finally:
            self._close_watchdog()
            self._close_job()
            if self._pid_file is not None:
                _remove(self._pid_file)

    async def terminate_tree(self, *, timeout: float = _KILL_TIMEOUT) -> None:
        """Kill the child's whole process tree; idempotent and never raises."""
        if not self._claim_termination():
            return
        _unregister_live_child(self)
        popen = self._popen
        try:
            if popen.poll() is not None:
                return
            if sys.platform == "win32":
                _terminate_windows(self.pid, self._take_job())
            else:
                _posix_kill_group(self.pid)
            exited = await asyncio.to_thread(_wait_popen_bounded, popen, timeout)
            if not exited:
                with contextlib.suppress(Exception):
                    popen.kill()
        except Exception as exc:  # teardown must never propagate
            log(f"终止 Chrome 进程树失败（pid={self.pid}）：{exc!r}")
        finally:
            self._close_watchdog()
            self._close_job()
            if self._pid_file is not None:
                _remove(self._pid_file)

    def _close_job(self) -> None:
        with self._lock:
            job = self._job
            self._job = None
        if job is not None:
            _win_close_handle(job)


@dataclass(frozen=True)
class LaunchResult:
    """A ready-to-use browser endpoint plus the child that backs it."""

    endpoint: str  # http://127.0.0.1:<port>
    browser_ws: str  # ws://.../devtools/browser/<id>
    child: ChildProcess


# --------------------------------------------------------------------- boot id


def boot_id() -> str:
    """A per-boot identifier (Linux only; empty elsewhere)."""
    if sys.platform.startswith("linux"):
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


# ------------------------------------------------------------------ discovery


def _sha256_of(path: str) -> str:
    """SHA265 of the executable, streamed; empty string when unreadable."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return ""
    return digest.hexdigest()


def _report(exe: str) -> str:
    """Log the chosen exe path with its SHA256 and return it."""
    digest = _sha256_of(exe)
    if digest:
        log(f"发现浏览器可执行文件：{exe}（sha256={digest}）")
    else:
        log(f"发现浏览器可执行文件：{exe}（sha256 不可读，跳过摘要）")
    return exe


def _fixed_candidates(*, include_edge: bool = True) -> list[str]:
    """Step 1: the standard install locations, in priority order."""
    if sys.platform == "win32":
        candidates: list[str] = []
        for root in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if root:
                candidates.append(str(Path(root) / "Google" / "Chrome" / "Application" / "chrome.exe"))
        if include_edge:
            for edge_root in (
                os.environ.get("ProgramFiles(x86)"),
                os.environ.get("ProgramFiles"),
            ):
                if edge_root:
                    path = str(Path(edge_root) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
                    if path not in candidates:
                        candidates.append(path)
            prog = os.environ.get("ProgramFiles")
            if prog:
                candidates.append(str(Path(prog) / "Chromium" / "Application" / "chrome.exe"))
        return candidates
    if sys.platform == "darwin":
        candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        if include_edge:
            candidates.append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        return candidates
    candidates = [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    if include_edge:
        candidates.append("/usr/bin/microsoft-edge")
    return candidates


def _registry_candidate() -> str | None:
    """Step 2: the trusted registry (Windows HKLM only; HKCU is not trusted)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover - winreg exists on Windows
        return None
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "")
    except OSError:
        return None
    candidate = str(value).strip().strip('"')
    if candidate and os.path.isfile(candidate):
        return candidate
    return None


def find_browser_executable(
    *,
    purpose: Literal["automation", "bridge", "chrome_devtools"] = "automation",
    log_result: bool = True,
) -> Path | None:
    """Find a browser executable for *purpose*: fixed paths -> trusted HKLM registry -> trusted ``which``.

    ``chrome_devtools`` searches Chrome/Chromium only (for Chrome 144+ native DevTools
    remote debugging); ``automation`` and ``bridge`` include Edge across all platforms so
    Owned launch and Bridge diagnostics always agree.
    """
    include_edge = purpose != "chrome_devtools"
    which_names = _WHICH_NAMES if include_edge else _CHROME_WHICH_NAMES
    for candidate in _fixed_candidates(include_edge=include_edge):
        if os.path.isfile(candidate):
            return Path(_report(candidate) if log_result else candidate)
    registered = _registry_candidate()
    if registered:
        return Path(_report(registered) if log_result else registered)
    for name in which_names:
        found = _which_trusted(name)
        if found:
            return Path(_report(found) if log_result else found)
    return None


def discover_chrome() -> str | None:
    """Find a browser exe: fixed paths -> trusted HKLM registry -> ``which``."""
    found = find_browser_executable(purpose="automation", log_result=True)
    return str(found) if found is not None else None


# --------------------------------------------------------------------- argv


def chrome_argv(exe: str, profile: Path, *, headless: bool) -> list[str]:
    """The exact launch argv (order is part of the contract). No allow-origins."""
    argv = [
        exe,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--hide-crash-restore-bubble",
        "--window-size=1280,900",
    ]
    if headless:
        argv.append("--headless=new")
    argv.append("about:blank")
    return argv


# ------------------------------------------------------------- win32 ctypes

# Win32 ABI constants for JobObject kill-on-close & suspended-thread resume:
# JOBOBJECT_EXTENDED_LIMIT_INFORMATION is 144 bytes on 64-bit Windows (112 on 32-bit),
# with LimitFlags (uint32) at offset 16. THREADENTRY32 is 28 bytes on both architectures,
# with dwSize at offset 0, th32ThreadID at offset 8, and th32OwnerProcessID at offset 12.
_WIN_KILL_ON_CLOSE: Final = 0x2000
_WIN_EXTENDED_LIMIT_CLASS: Final = 9
_WIN_EXTENDED_LIMIT_BYTES: Final = 144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112
_WIN_LIMIT_FLAGS_OFFSET: Final = 16
_WIN_THREAD_ENTRY_BYTES: Final = 28
_WIN_INVALID_HANDLE: Final = ctypes.c_void_p(-1).value

_WIN_KERNEL32_SPECS: Final[tuple[tuple[str, list[Any], Any], ...]] = (
    ("OpenProcess", [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
    ("GetExitCodeProcess", [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)], ctypes.c_int),
    ("CreateJobObjectW", [ctypes.c_void_p, ctypes.c_wchar_p], ctypes.c_void_p),
    ("SetInformationJobObject", [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
    ("AssignProcessToJobObject", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
    ("TerminateJobObject", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
    ("TerminateProcess", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_int),
    ("CloseHandle", [ctypes.c_void_p], ctypes.c_int),
    ("CreateToolhelp32Snapshot", [ctypes.c_uint32, ctypes.c_uint32], ctypes.c_void_p),
    ("Thread32First", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
    ("Thread32Next", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
    ("OpenThread", [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32], ctypes.c_void_p),
    ("ResumeThread", [ctypes.c_void_p], ctypes.c_uint32),
)


def _kernel32() -> Any:
    """Bind required Win32 kernel32 APIs with explicit pointer-width signatures."""
    dll = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    for fn_name, argtypes, restype in _WIN_KERNEL32_SPECS:
        fn = getattr(dll, fn_name)
        fn.argtypes = argtypes
        fn.restype = restype
    return dll


def _win_create_kill_job() -> int | None:
    """Allocate a Win32 Job object configured to terminate member processes on close."""
    dll = _kernel32()
    job_handle = dll.CreateJobObjectW(None, None)
    if not job_handle:
        return None
    buf = ctypes.create_string_buffer(_WIN_EXTENDED_LIMIT_BYTES)
    struct.pack_into("<I", buf, _WIN_LIMIT_FLAGS_OFFSET, _WIN_KILL_ON_CLOSE)
    applied = dll.SetInformationJobObject(job_handle, _WIN_EXTENDED_LIMIT_CLASS, buf, len(buf))
    if not applied:
        dll.CloseHandle(job_handle)
        return None
    return int(job_handle)


def _win_assign_to_job(job: int, pid: int) -> bool:
    """Bind *pid* to *job* using PROCESS_SET_QUOTA | PROCESS_TERMINATE."""
    dll = _kernel32()
    access = 0x0100 | 0x0001
    proc_handle = dll.OpenProcess(access, False, pid)
    if not proc_handle:
        return False
    try:
        return bool(dll.AssignProcessToJobObject(job, proc_handle))
    finally:
        dll.CloseHandle(proc_handle)


_WIN_RESUME_FAILED: Final = 0xFFFFFFFF


def _win_resume_main_thread(pid: int) -> bool:
    """Locate the first thread owned by *pid* via Toolhelp32 and resume it; return whether it resumed."""
    dll = _kernel32()
    snap_handle = dll.CreateToolhelp32Snapshot(0x00000004, 0)
    if not snap_handle or int(snap_handle) in (_WIN_INVALID_HANDLE, -1):
        return False
    try:
        raw_entry = ctypes.create_string_buffer(_WIN_THREAD_ENTRY_BYTES)
        struct.pack_into("<I", raw_entry, 0, _WIN_THREAD_ENTRY_BYTES)
        has_entry = bool(dll.Thread32First(snap_handle, raw_entry))
        while has_entry:
            tid, owner_pid = struct.unpack_from("<II", raw_entry, 8)
            if owner_pid == pid:
                thread_handle = dll.OpenThread(0x0002, False, tid)
                if not thread_handle:
                    return False
                try:
                    previous_count = dll.ResumeThread(thread_handle)
                    return int(previous_count) != _WIN_RESUME_FAILED
                finally:
                    dll.CloseHandle(thread_handle)
            has_entry = bool(dll.Thread32Next(snap_handle, raw_entry))
    finally:
        dll.CloseHandle(snap_handle)
    return False


def _win_close_handle(handle: int) -> None:
    kernel32 = _kernel32()
    kernel32.CloseHandle(handle)


def _terminate_windows(pid: int, job: int | None) -> None:
    """Kill the tree: prefer the Job object, else ``taskkill /F /T`` with ``TerminateProcess`` fallback."""
    kernel32 = _kernel32()
    if job is not None:
        kernel32.TerminateJobObject(job, 1)
        return
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **windows_hidden_kwargs(),
        )
        if result.returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        pass
    process_terminate = 0x0001
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if handle:
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)


def _win_process_exists(pid: int) -> bool:
    return pid_alive(pid)


# ------------------------------------------------------------------- spawn

_LINUX_PRCTL: Any | None = None
_LINUX_PRCTL_PROBED = False


def _get_linux_prctl() -> Any | None:
    """Resolve ``libc.prctl`` in the parent BEFORE fork() to avoid post-fork loader deadlocks."""
    global _LINUX_PRCTL, _LINUX_PRCTL_PROBED
    if _LINUX_PRCTL_PROBED:
        return _LINUX_PRCTL
    _LINUX_PRCTL_PROBED = True
    if not sys.platform.startswith("linux"):
        return None
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        _LINUX_PRCTL = libc.prctl
    except Exception:
        _LINUX_PRCTL = None
    return _LINUX_PRCTL


def _preexec_pdeathsig(parent_pid: int | None = None) -> None:  # pragma: no cover - runs in the forked child
    """POSIX pre-exec hook: die with the parent, closing the fork/race window."""
    if not sys.platform.startswith("linux"):
        return
    prctl = _LINUX_PRCTL
    if prctl is None:
        try:
            prctl = ctypes.CDLL(None, use_errno=True).prctl
        except Exception:
            return
    try:
        prctl(_PR_SET_PDEATHSIG, _SIGKILL, 0, 0, 0)
    except Exception:  # best effort; the process group is the fallback
        return
    expected_ppid = parent_pid if parent_pid is not None else 1
    if parent_pid is not None and os.getppid() != expected_ppid:
        os._exit(1)
    elif parent_pid is None and os.getppid() == 1:
        os._exit(1)


def _start_macos_watchdog(child_pid: int) -> int | None:  # pragma: no cover - macOS only
    """Spawn a tiny watchdog that kills *child_pid*'s process group on parent EOF."""
    if sys.platform != "darwin":
        return None
    try:
        read_fd, write_fd = os.pipe()
        script = (
            "import os, signal, sys\n"
            "try:\n"
            "    os.read(int(sys.argv[1]), 1)\n"
            "except OSError:\n"
            "    pass\n"
            "try:\n"
            "    os.killpg(int(sys.argv[2]), signal.SIGKILL)\n"
            "except OSError:\n"
            "    pass\n"
        )
        subprocess.Popen(
            [sys.executable, "-c", script, str(read_fd), str(int(child_pid))],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(read_fd,),
            start_new_session=True,
        )
        os.close(read_fd)
        return write_fd
    except Exception:
        return None


def _spawn_windows(argv: list[str], log_file: BinaryIO) -> ChildProcess:
    popen = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        creationflags=_CREATE_SUSPENDED,
    )
    job: int | None = None
    try:
        job = _win_create_kill_job()
        if job is not None and not _win_assign_to_job(job, popen.pid):
            log("无法将 Chrome 挂入 Job 对象，改用进程级终止兜底")
            _win_close_handle(job)
            job = None
        if not _win_resume_main_thread(popen.pid):
            raise Unavailable(f"无法恢复挂起的浏览器主线程（pid={popen.pid}）")
        return ChildProcess(pid=popen.pid, start_time=time.time(), _popen=popen, _job=job)
    except BaseException:
        with contextlib.suppress(Exception):
            _terminate_windows(popen.pid, job)
        with contextlib.suppress(Exception):
            popen.kill()
        if job is not None:
            with contextlib.suppress(Exception):
                _win_close_handle(job)
        raise


def _spawn_posix(argv: list[str], log_file: BinaryIO) -> ChildProcess:
    preexec = None
    if sys.platform.startswith("linux"):
        _get_linux_prctl()
        parent_pid = os.getpid()

        def _child_setup() -> None:
            _preexec_pdeathsig(parent_pid)

        preexec = _child_setup
    popen = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        start_new_session=True,
        preexec_fn=preexec,
    )
    watchdog_fd = _start_macos_watchdog(popen.pid) if sys.platform == "darwin" else None
    return ChildProcess(pid=popen.pid, start_time=time.time(), _popen=popen, _job=None, _watchdog_fd=watchdog_fd)


def _spawn(argv: list[str], *, log_path: Path) -> ChildProcess:
    """Start the browser: ``stdin=DEVNULL``, stdout/stderr into a file (no pipe)."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    rotate_log_if_needed(log_path)
    try:
        with open(log_path, "ab", buffering=0) as log_file:
            child = _spawn_windows(argv, log_file) if sys.platform == "win32" else _spawn_posix(argv, log_file)
    except OSError as exc:
        raise Unavailable(f"无法启动浏览器可执行文件（{argv[0]}）：{exc}", pre_dispatch=True) from exc
    _register_live_child(child)
    return child


def _posix_kill_group(pid: int) -> None:
    posix_os = cast(Any, os)
    try:
        posix_os.killpg(posix_os.getpgid(pid), _SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            os.kill(pid, _SIGKILL)


# ----------------------------------------------------------- HTTP / endpoint


async def _read_http_body(reader: asyncio.StreamReader) -> tuple[dict[str, str], bytes]:
    """Read status + headers and the ``Content-Length`` body from *reader*."""
    status_line = await reader.readline()
    if not status_line:
        raise Unavailable("DevTools HTTP 响应为空")
    parts = status_line.decode("latin-1", "replace").split()
    status = int(parts[1]) if len(parts) > 1 else 0
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        text = line.decode("latin-1", "replace").rstrip("\r\n")
        if ":" in text:
            name, value = text.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    if status != 200:
        raise Unavailable(f"DevTools /json/version 返回 HTTP {status}")
    length = int(headers.get("content-length", "0") or "0")
    body = await reader.readexactly(length) if length else await reader.read()
    return headers, body


async def _fetch_browser_ws(host: str, port: int) -> str:
    """GET ``/json/version`` on one loopback address and read the browser WS url."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), _HTTP_TIMEOUT)
    host_header = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    try:
        request = (
            "GET /json/version HTTP/1.1\r\n"
            f"Host: {host_header}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await asyncio.wait_for(writer.drain(), _HTTP_TIMEOUT)
        _, body = await asyncio.wait_for(_read_http_body(reader), _HTTP_TIMEOUT)
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
    payload = json.loads(body.decode("utf-8"))
    url = str(payload.get("webSocketDebuggerUrl", ""))
    if not url:
        raise Unavailable("DevTools /json/version 未返回 webSocketDebuggerUrl")
    return url


async def _browser_ws_endpoint(port: int) -> str:
    """Probe IPv4 then IPv6 loopback for the browser-level WebSocket URL."""
    last_error = "未知错误"
    for host in ("127.0.0.1", "::1"):
        try:
            return await _fetch_browser_ws(host, port)
        except Exception as exc:  # try the next loopback address
            last_error = f"{host}: {exc!r}"
    raise Unavailable(f"无法从 /json/version 获取浏览器级端点（{last_error}）")


async def _await_devtools_port(port_file: Path, child: ChildProcess, ready_timeout: float) -> int:
    """Poll ``DevToolsActivePort`` until Chrome publishes its port, or fail."""
    deadline = asyncio.get_running_loop().time() + ready_timeout
    while True:
        lines = _read_port_file(port_file)
        if lines:
            return lines
        if not child.alive:
            raise Unavailable("浏览器进程在就绪前退出（详见 state_dir/chrome.log）")
        if asyncio.get_running_loop().time() >= deadline:
            raise Unavailable(f"等待 DevToolsActivePort 超时（{int(ready_timeout)}s）")
        await asyncio.sleep(_POLL_INTERVAL)


def _read_port_file(port_file: Path) -> int | None:
    try:
        text = port_file.read_text(encoding="utf-8")
    except OSError:
        return None
    first = text.splitlines()[0].strip() if text.strip() else ""
    if not first:
        return None
    try:
        return int(first)
    except ValueError:
        return None


def _write_pid_file(path: Path, child: ChildProcess) -> None:
    path.write_text(f"{child.pid} {child.start_time} {boot_id()}", encoding="utf-8")


# ---------------------------------------------------------------- orphan sweep


def _parse_pid_line(text: str) -> tuple[int, float] | None:
    parts = text.split()
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), float(parts[1])
    except ValueError:
        return None


def _process_exists(pid: int) -> bool:
    return pid_alive(pid)


def _process_cmdline(pid: int) -> str | list[str] | None:
    """Best-effort command line (or exact argv list on Linux) of *pid*; ``None`` when unreadable."""
    if sys.platform == "win32":
        return _win_cmdline(pid)
    if sys.platform == "darwin":
        return _ps_cmdline(pid)
    return _proc_cmdline(pid)


def _proc_cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return [part.decode("utf-8", "replace") for part in raw.split(b"\x00") if part]


def _ps_cmdline(pid: int) -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            **windows_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout or None


def _win_cmdline(pid: int) -> str | None:
    """Read a process command line on Windows (CIM first for Win11, ``wmic`` fallback); ``None`` means 'unknown, keep'."""
    try:
        ps_result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; "
                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            stdin=subprocess.DEVNULL,
            **windows_hidden_kwargs(),
        )
        if ps_result.returncode == 0 and ps_result.stdout.strip():
            return ps_result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        result = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={int(pid)}", "get", "CommandLine"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            stdin=subprocess.DEVNULL,
            **windows_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) <= 1:  # only the "CommandLine" header, or nothing at all
        return None
    return " ".join(lines[1:])


async def _graceful_close(profile: Path) -> None:
    """Ask a lingering browser to close politely (one best-effort attempt)."""
    port = _read_port_file(profile / "DevToolsActivePort")
    if port is None:
        return
    with contextlib.suppress(Exception):
        browser_ws = await _browser_ws_endpoint(port)
        await _send_browser_close(browser_ws)


async def _send_browser_close(browser_ws: str) -> None:
    from .websocket import WSClient

    ws = await WSClient.connect(browser_ws, open_timeout=_HTTP_TIMEOUT)
    try:
        await ws.send(json.dumps({"id": 1, "method": "Browser.close", "params": {}}))
    finally:
        with contextlib.suppress(Exception):
            await ws.aclose()


async def _wait_until_gone(pid: int, *, timeout: float) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if not _process_exists(pid):
            return True
        if asyncio.get_running_loop().time() >= deadline:
            return not _process_exists(pid)
        await asyncio.sleep(_POLL_INTERVAL)


def _remove(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()


def _cmdline_matches_profile(cmdline: str | list[str], profile: Path) -> bool:
    """Require an exact ``--user-data-dir=<profile>`` argument match (handles spaces; never prefix-matches)."""
    target = f"--user-data-dir={profile}"
    if isinstance(cmdline, list):
        return any(arg == target for arg in cmdline)
    import shlex

    text = str(cmdline or "")
    # 1. Exact quoted form bounded by whitespace or string boundaries.
    for quote in ('"', "'"):
        quoted = f"{quote}{target}{quote}"
        idx = text.find(quoted)
        while idx != -1:
            before_ok = idx == 0 or text[idx - 1].isspace()
            end = idx + len(quoted)
            after_ok = end == len(text) or text[end].isspace()
            if before_ok and after_ok:
                return True
            idx = text.find(quoted, idx + 1)
    # 2. Unquoted form (e.g. macOS `ps -o command=`): must start at a token boundary
    #    and end at end-of-string or immediately before the next Chrome `--flag` (or ` about:blank`).
    idx = text.find(target)
    while idx != -1:
        before_ok = idx == 0 or text[idx - 1].isspace()
        end = idx + len(target)
        rest = text[end:]
        after_ok = rest == "" or rest.startswith(" --") or rest.startswith(" about:")
        if before_ok and after_ok:
            return True
        idx = text.find(target, idx + 1)
    # 3. Standard shell token split fallback for simple paths without spaces.
    try:
        tokens = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        tokens = text.split()
    return any(token.strip("\"'") == target for token in tokens)


async def reclaim_orphans(state_dir: Path) -> None:
    """Recover a browser left by a previous generation; never blocks startup."""
    try:
        await _reclaim_orphans(state_dir)
    except Exception as exc:  # reaping must not abort the launch
        log(f"孤儿回收失败（已忽略）：{exc!r}")


async def _reclaim_orphans(state_dir: Path) -> None:
    pid_file = state_dir / "chrome.pid"
    if not pid_file.exists():
        return
    try:
        text = pid_file.read_text(encoding="utf-8")
    except OSError:
        return
    parsed = _parse_pid_line(text)
    if parsed is None:
        log("残留 chrome.pid 无法解析，已清理")
        _remove(pid_file)
        return
    pid, _start_time = parsed
    profile = state_dir / "chrome"
    if not _process_exists(pid):
        log(f"残留 chrome.pid 指向的进程已不存在（pid={pid}），清理")
        _remove(pid_file)
        return
    cmdline = await asyncio.to_thread(_process_cmdline, pid)
    if cmdline is None:
        log(f"无法读取 pid={pid} 的命令行，保守起见不回收以免误杀")
        return
    if not _cmdline_matches_profile(cmdline, profile):
        log(f"pid={pid} 的命令行不含精确的 --user-data-dir={profile} 参数，绝不误杀")
        return
    log(f"回收上一代浏览器进程树（pid={pid}）")
    await _graceful_close(profile)
    if not await _wait_until_gone(pid, timeout=_KILL_TIMEOUT):
        log(f"pid={pid} 未在 {int(_KILL_TIMEOUT)}s 内退出，强制杀进程树")
        if sys.platform == "win32":
            _terminate_windows(pid, None)
        else:
            _posix_kill_group(pid)
        await _wait_until_gone(pid, timeout=2.0)
    _remove(profile / "DevToolsActivePort")
    _remove(pid_file)


# ------------------------------------------------------------------ launch


async def launch_chrome(
    cfg: BrowserConfig,
    state_dir: Path,
    *,
    ready_timeout: float = CHROME_READY_TIMEOUT,
) -> LaunchResult:
    """Reclaim orphans, start Chrome and return the live browser-level endpoint."""
    await reclaim_orphans(state_dir)
    exe = cfg.executable or discover_chrome()
    if not exe:
        raise Unavailable(
            "未找到 Chrome 或 Edge，请先安装其中一个。若已安装在非标准路径，请设置 ICODE_BROWSER_EXECUTABLE",
            pre_dispatch=True,
        )

    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = cfg.chrome_profile_dir
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(state_dir, 0o700)
            os.chmod(profile, 0o700)
    port_file = profile / "DevToolsActivePort"
    _remove(port_file)

    argv = chrome_argv(exe, profile, headless=cfg.headless)
    child = _spawn(argv, log_path=state_dir / "chrome.log")
    pid_file = state_dir / "chrome.pid"
    child._pid_file = pid_file
    try:
        _write_pid_file(pid_file, child)
        port = await _await_devtools_port(port_file, child, ready_timeout)
        browser_ws = await _browser_ws_endpoint(port)
        child._browser_ws = browser_ws
    except BaseException:
        await child.terminate_tree()
        raise
    return LaunchResult(endpoint=f"http://127.0.0.1:{port}", browser_ws=browser_ws, child=child)
