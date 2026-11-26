# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess launch hygiene and owner-only file ACL helpers.

Every non-interactive child gets ``stdin=subprocess.DEVNULL`` at its call site
(iCode's stdin is an ACP JSON-RPC pipe). On Windows the child must get its own
hidden console: sharing the parent's console lets a child reset console modes
and corrupt the TUI.
"""

from __future__ import annotations

import contextlib
import csv
import io
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .logging import log

_CREATE_NEW_CONSOLE = 0x00000010
_SYNCHRONIZE_ACCESS = 0x00100000
_ERROR_ACCESS_DENIED = 5
_WAIT_TIMEOUT = 0x00000102
_CACHED_WIN_SID: str | None = None
_CACHED_WIN_SID_TRIED = False


def _system32_exe(name: str) -> str:
    """Return an absolute System32 executable path on Windows to avoid CWD hijacking."""
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(system_root) / "System32" / name)


def which_trusted(name: str) -> str | None:
    """Resolve *name* on PATH without Windows CWD or PATHEXT (.cmd/.bat) hijacking."""
    if os.name != "nt":
        return shutil.which(name)
    exe_name = f"{name.removesuffix('.exe')}.exe"
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        cleaned = entry.strip().strip('"')
        if not cleaned or not os.path.isabs(cleaned):
            continue
        candidate = Path(cleaned) / exe_name
        if candidate.is_file():
            return str(candidate)
    return None


def windows_hidden_kwargs() -> dict[str, Any]:
    """``subprocess`` keyword arguments that hide the child window on Windows."""
    if sys.platform != "win32":
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": info, "creationflags": _CREATE_NEW_CONSOLE}


def current_user_sid() -> str | None:
    """Return the current Windows user's SID string (``S-1-...``), cached once per process."""
    global _CACHED_WIN_SID, _CACHED_WIN_SID_TRIED
    if sys.platform != "win32":
        return None
    if _CACHED_WIN_SID_TRIED:
        return _CACHED_WIN_SID
    _CACHED_WIN_SID_TRIED = True
    try:
        completed = subprocess.run(
            [_system32_exe("whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **windows_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0 or not completed.stdout:
        return None
    row = next(csv.reader(io.StringIO(completed.stdout.strip())), [])
    candidate = row[1].strip() if len(row) >= 2 else ""
    if candidate.startswith("S-1-"):
        _CACHED_WIN_SID = candidate
    return _CACHED_WIN_SID


def harden_private(path: Path, *, is_dir: bool = False, strict: bool = False) -> bool:
    """Restrict *path* to the current user (POSIX 0600/0700; Windows SID ACL via icacls).

    Returns ``True`` when the OS permission hardening succeeded. When *strict* is
    True, raises ``OSError`` if tightening fails.
    """
    if sys.platform != "win32":
        mode = 0o700 if is_dir else 0o600
        try:
            os.chmod(path, mode)
            return True
        except OSError:
            if strict:
                raise
            return False

    sid = current_user_sid()
    principal = f"*{sid}" if sid else (os.environ.get("USERNAME") or os.environ.get("USER") or "")
    if not principal:
        if strict:
            raise OSError(f"cannot determine Windows user SID/name to harden {path}")
        return False
    grant = f"{principal}:(OI)(CI)F" if is_dir else f"{principal}:F"
    icacls_exe = _system32_exe("icacls.exe")
    try:
        result = subprocess.run(
            [icacls_exe, str(path), "/inheritance:r", "/grant:r", grant],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=5,
            check=False,
            **windows_hidden_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if strict:
            raise OSError(f"icacls failed for {path}: {exc}") from exc
        log(f"could not restrict {path} to the current user: {exc}")
        return False
    if result.returncode != 0:
        if strict:
            raise OSError(f"icacls exited {result.returncode} for {path}")
        log(f"could not restrict {path} to the current user: icacls exited {result.returncode}")
        return False
    return True


def atomic_write_private(
    path: Path,
    content: str,
    *,
    strict: bool = False,
    reject_symlink: bool = False,
) -> bool:
    """Atomically write *content* to *path* via a same-directory hardened temp file.

    Hardens the temporary file before writing sensitive content on Windows (and
    via mode 0o600 at creation on POSIX), fsyncs, then replaces *path*.
    Returns whether OS permission hardening succeeded.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if reject_symlink and path.is_symlink():
        raise OSError(f"refusing to write through symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    hardened = False
    try:
        if sys.platform == "win32":
            os.close(fd)
            fd = -1
            # Harden empty file BEFORE writing sensitive bytes so no window exposes secrets.
            hardened = harden_private(temporary, is_dir=False, strict=strict)
            with open(temporary, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        else:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            hardened = harden_private(temporary, is_dir=False, strict=strict)
        if reject_symlink and path.is_symlink():
            raise OSError(f"refusing to replace symlink: {path}")
        os.replace(temporary, path)
        return hardened
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise


def rotate_log_if_needed(path: Path, *, max_bytes: int = 5 * 1024 * 1024) -> None:
    """Rotate *path* to ``<name>.1`` once it reaches *max_bytes* (best-effort)."""
    try:
        if path.is_file() and path.stat().st_size >= max_bytes:
            backup = path.with_name(f"{path.name}.1")
            os.replace(path, backup)
    except OSError:
        return


def _win_pid_running(pid: int) -> bool:
    """Non-blocking ``WaitForSingleObject`` probe against a Windows process handle."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    open_proc = dll.OpenProcess
    open_proc.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_proc.restype = wintypes.HANDLE
    wait_one = dll.WaitForSingleObject
    wait_one.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_one.restype = wintypes.DWORD
    close_handle = dll.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    proc_handle = open_proc(_SYNCHRONIZE_ACCESS, False, pid)
    if not proc_handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        return int(wait_one(proc_handle, 0)) == _WAIT_TIMEOUT
    finally:
        close_handle(proc_handle)


def pid_alive(pid: int) -> bool:
    """Whether *pid* currently refers to a live OS process without spawning anything."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_running(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False
    return True
