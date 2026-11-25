# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Subprocess launch hygiene and owner-only file ACL helpers.

Every non-interactive child gets ``stdin=subprocess.DEVNULL`` at its call site
(iCode's stdin is an ACP JSON-RPC pipe). On Windows the child must get its own
hidden console: sharing the parent's console lets a child reset console modes
and corrupt the TUI.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .logging import log

_CREATE_NEW_CONSOLE = 0x00000010
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
    """Return the current Windows process user's SID string (``S-1-...``), or ``None``."""
    global _CACHED_WIN_SID, _CACHED_WIN_SID_TRIED
    if sys.platform != "win32":
        return None
    if _CACHED_WIN_SID_TRIED:
        return _CACHED_WIN_SID
    _CACHED_WIN_SID_TRIED = True
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p

        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL

        TOKEN_QUERY = 0x0008
        TOKEN_USER = 1

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
            return None
        try:
            needed = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(needed))
            if needed.value <= 0:
                return None
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(token, TOKEN_USER, buf, needed.value, ctypes.byref(needed)):
                return None
            # TOKEN_USER starts with SID_AND_ATTRIBUTES { PSID Sid; DWORD Attributes; }
            sid_ptr = ctypes.c_void_p.from_buffer(buf).value
            if not sid_ptr:
                return None
            sid_str = ctypes.c_wchar_p()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str)):
                return None
            try:
                _CACHED_WIN_SID = str(sid_str.value) if sid_str.value else None
                return _CACHED_WIN_SID
            finally:
                if sid_str.value is not None:
                    kernel32.LocalFree(ctypes.cast(sid_str, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(token)
    except Exception:
        return None


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


def pid_alive(pid: int) -> bool:
    """Whether that process is running; a read-only probe that starts nothing."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED => process exists
        try:
            exit_code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True
