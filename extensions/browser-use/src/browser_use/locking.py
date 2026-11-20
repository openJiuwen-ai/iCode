# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-process mutual exclusion for the state directory.

Multiple host processes (each spawning its own copy of the plugin) must never
drive the same Chromium profile at once; the user-data-dir singleton only
forwards launch arguments and exits, which would leave the CDP endpoint
unreachable. A single OS-level advisory lock on ``state_dir/instance.lock``
guarantees one live owner per state directory, and the kernel releases it when
the owner dies - so a crashed plugin cannot wedge the next start.

The lock file also records the owner's PID, process start time and boot id for
diagnostics.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import LOCK_WAIT
from .errors import Unavailable

try:  # pragma: no cover - exercised by the platform-specific branches
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]

OWNER_META_FILE = "instance.owner.json"
_WIN_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_WIN_STILL_ACTIVE = 259


@dataclass(frozen=True)
class LockProbe:
    """Read-only snapshot of whether a state directory's instance lock is held."""

    locked: bool
    state: str
    holder_pid: int | None = None
    holder_alive: bool | None = None
    held_seconds: int | None = None


@dataclass
class InstanceLock:
    """A held advisory lock; call :meth:`release` to drop it (or just exit)."""

    path: Path
    _fd: int
    owner_path: Path | None = None
    pid: int = 0

    def release(self) -> None:
        if self._fd < 0:
            return
        try:
            _remove_own_owner_meta(self.owner_path, self.pid)
            if fcntl is not None:  # pragma: no cover - POSIX
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(self._fd)
            finally:
                self._fd = -1


def _write_owner(path: Path, fd: int) -> Path:
    now = time.time()
    pid = os.getpid()
    boot_id = _boot_id()
    payload = f"{pid} {now:.3f} {boot_id}\n"
    with open(fd, "w", encoding="utf-8", closefd=False) as handle:
        handle.write(payload)
        handle.flush()
    owner_path = path.with_name(OWNER_META_FILE)
    meta = {"pid": pid, "started_at": now, "boot_id": boot_id}
    with contextlib.suppress(OSError):
        tmp = path.with_name(f".{OWNER_META_FILE}.{pid}.tmp")
        with _restrictive_umask():
            tmp.write_text(json.dumps(meta), encoding="utf-8")
        os.replace(tmp, owner_path)
    return owner_path


def _remove_own_owner_meta(owner_path: Path | None, pid: int) -> None:
    if owner_path is None or not pid:
        return
    try:
        data = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("pid") == pid:
        with contextlib.suppress(OSError):
            owner_path.unlink()


def _pid_alive(pid: int) -> bool | None:
    """Best-effort liveness check for a local PID (never signals or kills it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows
        kernel32 = getattr(ctypes, "windll", None)
        if kernel32 is None:
            return None
        handle = kernel32.kernel32.OpenProcess(_WIN_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            err = ctypes.get_last_error()
            # ERROR_ACCESS_DENIED (5) means the process exists under another privilege level.
            return True if err == 5 else False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == _WIN_STILL_ACTIVE
        finally:
            kernel32.kernel32.CloseHandle(handle)
    try:  # pragma: no cover - POSIX
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None


def _read_owner_meta(state_dir: Path) -> dict[str, Any]:
    """Read non-sensitive lock-holder metadata (never reads tokens or cookies)."""
    owner_path = state_dir / OWNER_META_FILE
    try:
        data = json.loads(owner_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    lock_path = state_dir / "instance.lock"
    try:
        parts = lock_path.read_text(encoding="utf-8").strip().split()
        if parts and parts[0].isdigit():
            started = float(parts[1]) if len(parts) > 1 else None
            return {"pid": int(parts[0]), "started_at": started}
    except (OSError, ValueError):
        pass
    return {}


def inspect_lock_owner(state_dir: Path) -> LockProbe:
    """Build a structured diagnostic summary for an already-known locked state dir."""
    meta = _read_owner_meta(state_dir)
    pid = meta.get("pid") if isinstance(meta.get("pid"), int) else None
    started_at = meta.get("started_at")
    held_seconds: int | None = None
    if isinstance(started_at, (int, float)) and started_at > 0:
        held_seconds = max(0, int(time.time() - float(started_at)))
    alive = _pid_alive(pid) if pid is not None else None
    if alive is True:
        state = "locked_by_live_instance"
    elif alive is False:
        state = "stale_lock"
    else:
        state = "locked_by_unknown_holder"
    return LockProbe(
        locked=True,
        state=state,
        holder_pid=pid,
        holder_alive=alive,
        held_seconds=held_seconds,
    )


def probe_instance_lock(state_dir: Path) -> LockProbe:
    """Non-blocking read-only check of whether another process holds *state_dir*."""
    lock_path = state_dir / "instance.lock"
    if not lock_path.exists():
        return LockProbe(locked=False, state="free")
    try:
        fd = os.open(lock_path, os.O_RDWR)
    except OSError:
        return inspect_lock_owner(state_dir)
    if _try_acquire(fd):
        try:
            if fcntl is not None:  # pragma: no cover - POSIX
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            os.close(fd)
        return LockProbe(locked=False, state="free")
    os.close(fd)
    return inspect_lock_owner(state_dir)


def _format_lock_error(state_dir: Path, wait: float, probe: LockProbe) -> str:
    pid_text = str(probe.holder_pid) if probe.holder_pid is not None else "unknown"
    alive_text = "true" if probe.holder_alive is True else ("false" if probe.holder_alive is False else "unknown")
    fields = [
        "reason=state_dir_locked",
        f"state={probe.state}",
        f"holder_pid={pid_text}",
        f"holder_alive={alive_text}",
    ]
    if probe.held_seconds is not None:
        fields.append(f"held_for={probe.held_seconds}s")
    advice = (
        "下一步：不要短间隔重复 browser_navigate 或 sleep，也不要用 shell 读取 bridge.json、认证 token "
        "或浏览器 Cookie 数据库；请告知用户占用状态（"
        + (f"PID {pid_text}" if probe.holder_pid is not None else "另一实例")
        + "）并由用户决定是否关闭占用会话，或改用独立 ICODE_BROWSER_STATE_DIR / Broker。"
    )
    return f"另一个插件实例正在使用状态目录 {state_dir}（等待 {int(wait)}s 超时）\n{' '.join(fields)}\n{advice}"


def _boot_id() -> str:
    """A best-effort per-boot identifier (empty where unavailable)."""
    if sys.platform.startswith("linux"):
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def _try_acquire(fd: int) -> bool:
    try:
        if fcntl is not None:  # pragma: no cover - POSIX
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        if msvcrt is not None:  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
    except OSError:
        return False
    return True


async def acquire_instance_lock(state_dir: Path, *, wait: float = LOCK_WAIT) -> InstanceLock:
    """Acquire the state-dir lock, waiting up to *wait* seconds.

    Raises :class:`Unavailable` on timeout so the model sees ``browser_unavailable``
    rather than two browsers fighting over one profile.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    with _restrictive_umask():
        path = state_dir / "instance.lock"
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        if _try_acquire(fd):
            owner_path = _write_owner(path, fd)
            return InstanceLock(path=path, _fd=fd, owner_path=owner_path, pid=os.getpid())
        if time.monotonic() >= deadline:
            os.close(fd)
            probe = inspect_lock_owner(state_dir)
            raise Unavailable(_format_lock_error(state_dir, wait, probe))
        await asyncio.sleep(0.1)


class _restrictive_umask:
    """Context manager that tightens the umask to 0o077 where supported."""

    def __enter__(self) -> None:
        self._previous: int | None = None
        if hasattr(os, "umask"):
            self._previous = os.umask(0o077)

    def __exit__(self, *exc: object) -> None:
        if self._previous is not None:
            os.umask(self._previous)
