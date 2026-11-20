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
import contextlib
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

from .._proc import windows_hidden_kwargs
from ..config import CHROME_READY_TIMEOUT, BrowserConfig
from ..errors import Unavailable
from ..logging import log

#: How often the two readiness/reaping polls tick (design: 50 ms).
_POLL_INTERVAL: Final = 0.05
#: Grace given to a browser to die on its own before the tree is force-killed.
_KILL_TIMEOUT: Final = 5.0
#: Timeout for the tiny ``/json/version`` HTTP probe.
_HTTP_TIMEOUT: Final = 5.0

#: Windows: ``CREATE_SUSPENDED`` so the job can be attached before any child runs.
_CREATE_SUSPENDED: Final = 0x00000004

#: POSIX: ``prctl`` option and signal used for ``PR_SET_PDEATHSIG``.
_PR_SET_PDEATHSIG: Final = 1
_SIGKILL: Final = 9

_WHICH_NAMES: Final = (
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "chromium",
    "chromium-browser",
    "msedge",
    "microsoft-edge",
)


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

    @property
    def alive(self) -> bool:
        """True while the child has not exited."""
        return self._popen.poll() is None

    async def terminate_tree(self, *, timeout: float = _KILL_TIMEOUT) -> None:
        """Kill the child's whole process tree; idempotent and never raises."""
        if self._terminated:
            return
        self._terminated = True
        popen = self._popen
        try:
            if popen.poll() is not None:
                self._close_job()
                return
            if sys.platform == "win32":
                _terminate_windows(self.pid, self._job)
            else:
                _posix_kill_group(self.pid)
            try:
                await asyncio.wait_for(asyncio.to_thread(popen.wait), timeout)
            except (asyncio.TimeoutError, TimeoutError):
                with contextlib.suppress(Exception):
                    popen.kill()
            self._close_job()
        except Exception as exc:  # teardown must never propagate
            log(f"终止 Chrome 进程树失败（pid={self.pid}）：{exc!r}")

    def _close_job(self) -> None:
        if self._job is not None:
            _win_close_handle(self._job)
            self._job = None


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


def _fixed_candidates() -> list[str]:
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
        edge_root = os.environ.get("ProgramFiles(x86)")
        if edge_root:
            candidates.append(str(Path(edge_root) / "Microsoft" / "Edge" / "Application" / "msedge.exe"))
        return candidates
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    return [
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ]


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


def discover_chrome() -> str | None:
    """Find a browser exe: fixed paths -> trusted HKLM registry -> ``which``."""
    for candidate in _fixed_candidates():
        if os.path.isfile(candidate):
            return _report(candidate)
    registered = _registry_candidate()
    if registered:
        return _report(registered)
    for name in _WHICH_NAMES:
        found = shutil.which(name)
        if found:
            return _report(found)
    return None


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


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _THREADENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("cntUsage", ctypes.c_uint32),
        ("th32ThreadID", ctypes.c_uint32),
        ("th32OwnerProcessID", ctypes.c_uint32),
        ("tpBasePri", ctypes.c_int32),
        ("tpDeltaPri", ctypes.c_int32),
        ("dwFlags", ctypes.c_uint32),
    ]


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value


def _kernel32() -> object:
    """Load kernel32 with correct handle-typed signatures (Windows only)."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel32.CreateJobObjectW.restype = ctypes.c_void_p
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    kernel32.SetInformationJobObject.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.Thread32First.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.Thread32Next.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.OpenThread.restype = ctypes.c_void_p
    kernel32.OpenThread.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel32.ResumeThread.argtypes = [ctypes.c_void_p]
    return kernel32


def _win_create_kill_job() -> int | None:
    """Create a Job object that kills its members when the last handle closes."""
    kernel32 = _kernel32()
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if not ok:
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _win_assign_to_job(job: int, pid: int) -> bool:
    """Attach the process to *job* (needs PROCESS_SET_QUOTA|PROCESS_TERMINATE)."""
    kernel32 = _kernel32()
    process_set_quota = 0x0100
    process_terminate = 0x0001
    handle = kernel32.OpenProcess(process_set_quota | process_terminate, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(job, handle))
    finally:
        kernel32.CloseHandle(handle)


def _win_resume_main_thread(pid: int) -> None:
    """Resume the suspended primary thread of *pid* (found via a thread snapshot)."""
    kernel32 = _kernel32()
    th32cs_snapthread = 0x00000004
    thread_suspend_resume = 0x0002
    snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
    if not snapshot or int(snapshot) in (_INVALID_HANDLE_VALUE, -1):
        return
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(_THREADENTRY32)
        if not kernel32.Thread32First(snapshot, ctypes.byref(entry)):
            return
        while True:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(thread_suspend_resume, False, entry.th32ThreadID)
                if thread:
                    kernel32.ResumeThread(thread)
                    kernel32.CloseHandle(thread)
                    return
            if not kernel32.Thread32Next(snapshot, ctypes.byref(entry)):
                return
    finally:
        kernel32.CloseHandle(snapshot)


def _win_close_handle(handle: int) -> None:
    kernel32 = _kernel32()
    kernel32.CloseHandle(handle)


def _terminate_windows(pid: int, job: int | None) -> None:
    """Kill the tree: prefer the Job object, else terminate the process."""
    kernel32 = _kernel32()
    if job is not None:
        kernel32.TerminateJobObject(job, 1)
        return
    process_terminate = 0x0001
    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if handle:
        try:
            kernel32.TerminateProcess(handle, 1)
        finally:
            kernel32.CloseHandle(handle)


def _win_process_exists(pid: int) -> bool:
    kernel32 = _kernel32()
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_uint32()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == still_active
        return False
    finally:
        kernel32.CloseHandle(handle)


# ------------------------------------------------------------------- spawn


def _preexec_pdeathsig() -> None:  # pragma: no cover - runs in the forked child
    """POSIX pre-exec hook: die with the parent, closing the fork/race window."""
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_PDEATHSIG, _SIGKILL, 0, 0, 0)
    except Exception:  # best effort; the process group is the fallback
        return
    if os.getppid() == 1:
        os._exit(1)


def _spawn_windows(argv: list[str], log_file: BinaryIO) -> ChildProcess:
    popen = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        creationflags=_CREATE_SUSPENDED,
    )
    job = _win_create_kill_job()
    if job is not None and not _win_assign_to_job(job, popen.pid):
        log("无法将 Chrome 挂入 Job 对象，改用进程级终止兜底")
        _win_close_handle(job)
        job = None
    _win_resume_main_thread(popen.pid)
    return ChildProcess(pid=popen.pid, start_time=time.time(), _popen=popen, _job=job)


def _spawn_posix(argv: list[str], log_file: BinaryIO) -> ChildProcess:
    # Avoid preexec_fn (calling ctypes/CDLL after fork() in a multi-threaded
    # process can deadlock); start_new_session=True isolates the process group.
    popen = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=log_file,
        close_fds=True,
        start_new_session=True,
    )
    return ChildProcess(pid=popen.pid, start_time=time.time(), _popen=popen, _job=None)


def _spawn(argv: list[str], *, log_path: Path) -> ChildProcess:
    """Start the browser: ``stdin=DEVNULL``, stdout/stderr into a file (no pipe)."""
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(log_path, "ab", buffering=0) as log_file:
        if sys.platform == "win32":
            return _spawn_windows(argv, log_file)
        return _spawn_posix(argv, log_file)


def _posix_kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), _SIGKILL)
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
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_process_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_cmdline(pid: int) -> str | None:
    """Best-effort command line of *pid*; ``None`` when it cannot be read."""
    if sys.platform == "win32":
        return _win_cmdline(pid)
    if sys.platform == "darwin":
        return _ps_cmdline(pid)
    return _proc_cmdline(pid)


def _proc_cmdline(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return raw.replace(b"\x00", b" ").decode("utf-8", "replace")


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
    """Read a process command line via ``wmic``; ``None`` means 'unknown, keep'."""
    try:
        result = subprocess.run(
            ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine"],
            capture_output=True,
            text=True,
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


def _cmdline_matches_profile(cmdline: str, profile: Path) -> bool:
    """Require an exact ``--user-data-dir=<profile>`` argument match (never substring)."""
    target = f"--user-data-dir={profile}"
    # Check both raw space-delimited and quoted tokens so prefix collisions
    # like ``.../chrome-2`` or editors opening ``chrome.log`` never match.
    for token in cmdline.split():
        cleaned = token.strip("\"'")
        if cleaned == target:
            return True
    return f'"{target}"' in cmdline or f"'{target}'" in cmdline


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
    cmdline = _process_cmdline(pid)
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
        raise Unavailable("未找到 Chrome 或 Edge，请先安装其中一个。若已安装在非标准路径，请设置 ICODE_BROWSER_EXECUTABLE")

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
    try:
        _write_pid_file(state_dir / "chrome.pid", child)
        port = await _await_devtools_port(port_file, child, ready_timeout)
        browser_ws = await _browser_ws_endpoint(port)
    except BaseException:
        await child.terminate_tree()
        raise
    return LaunchResult(endpoint=f"http://127.0.0.1:{port}", browser_ws=browser_ws, child=child)
