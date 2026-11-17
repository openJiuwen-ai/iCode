# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-platform detection — OS, architecture, shell, paths.

Provides a cached ``get_platform()`` singleton that any module can import
for lightweight platform checks without needing ``SessionEnvironment``.
"""

from __future__ import annotations

import ctypes
import functools
import ntpath
import os
import platform
import shutil
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from chrys.foundation.platform.c_api import declare_functions
from chrys.foundation.platform.paths import real_path
from chrys.foundation.platform.runtime_paths import is_app_execution_alias


@dataclass(frozen=True)
class ShellInfo:
    """Detected shell information."""

    name: str  # e.g. "bash", "zsh", "pwsh", "powershell", "cmd"
    path: str  # e.g. "/bin/bash", "C:\\Windows\\System32\\cmd.exe"
    args: list[str]  # e.g. ["-c"] for bash, ["-Command"] for pwsh/powershell
    version: str = ""  # e.g. "7.5.0", "5.1.22621.4391"


@dataclass(frozen=True)
class PlatformInfo:
    """Detected platform information."""

    os_name: str  # "windows", "macos", "linux"
    os_version: str  # marketing version: "15.4.1" (macOS), "11" (Win), "22.04" (Ubuntu)
    arch: str  # "arm64", "amd64", "x86", or raw machine string
    shell: ShellInfo
    config_dir: Path  # ~/.chrys or %APPDATA%/chrys, by its real path (paths.real_path)
    data_dir: Path  # same as config_dir for now
    extra_shells: tuple[ShellInfo, ...] = ()  # additional shells (e.g. Git Bash on Windows)

    # --- OS convenience properties ---

    @property
    def is_windows(self) -> bool:
        return self.os_name == "windows"

    @property
    def is_macos(self) -> bool:
        return self.os_name == "macos"

    @property
    def is_linux(self) -> bool:
        return self.os_name == "linux"

    @property
    def os_display(self) -> str:
        """Human-friendly OS name (``"macOS"`` / ``"Windows"`` / ``"Linux"``)."""
        return {"macos": "macOS", "windows": "Windows", "linux": "Linux"}.get(self.os_name, self.os_name.title())

    @property
    def display_label(self) -> str:
        """Short ``"macOS-arm64"``-style label for headers and status UIs."""
        return f"{self.os_display}-{self.arch}" if self.arch else self.os_display

    # --- Architecture convenience properties ---

    @property
    def is_arm64(self) -> bool:
        return self.arch == "arm64"

    @property
    def is_amd64(self) -> bool:
        return self.arch == "amd64"

    @property
    def is_x86(self) -> bool:
        return self.arch == "x86"


def _detect_arch() -> str:
    """Detect CPU architecture, normalized to common names."""
    machine = platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86_64", "amd64"):
        return "amd64"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return machine  # fallback to raw value


def _detect_os_version(system: str) -> str:
    """Detect the user-facing OS version (marketing version, not kernel release).

    On macOS, ``platform.release()`` returns the Darwin kernel release
    (e.g. ``"25.4.0"``) — not the macOS version users recognize. This
    helper picks the right per-OS source for a human-meaningful version
    string and falls back to ``platform.release()`` if the preferred
    source is unavailable.

    Args:
        system: Lowercased ``platform.system()`` value
            (``"darwin"`` / ``"windows"`` / ``"linux"``).
    """
    try:
        if system == "darwin":
            return platform.mac_ver()[0] or platform.release()
        if system == "windows":
            win = platform.win32_ver()
            return win[1] or win[0] or platform.release()
        if system == "linux":
            try:
                info = platform.freedesktop_os_release()
            except OSError, AttributeError:
                return platform.release()
            return info.get("VERSION_ID") or info.get("VERSION", "") or platform.release()
    except Exception:
        pass
    return platform.release()


def conventional_config_dir() -> Path:
    """``~/.chrys``, or ``%APPDATA%/chrys`` on Windows, as the user knows it — possibly a link.

    Files are opened through :attr:`PlatformInfo.config_dir`, which is this with
    its links followed; only a command acting on the entry itself, such as
    removing it, wants this name.
    """
    if platform.system().lower() == "windows":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "chrys"
    return Path.home() / ".chrys"


def detect_platform() -> PlatformInfo:
    """Detect the current platform and return unified info."""
    system = platform.system().lower()
    arch = _detect_arch()
    os_version = _detect_os_version(system)

    extra_shells: tuple[ShellInfo, ...] = ()

    if system == "windows":
        from concurrent.futures import ThreadPoolExecutor

        os_name = "windows"
        # Detect default shell and Git Bash in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            shell_future = pool.submit(_detect_windows_shell)
            git_bash_future = pool.submit(_detect_git_bash)
            shell = shell_future.result()
            git_bash = git_bash_future.result()
        if git_bash is not None:
            extra_shells = (git_bash,)
    elif system == "darwin":
        os_name = "macos"
        shell = _detect_unix_shell()
    else:
        os_name = "linux"
        shell = _detect_unix_shell()

    # The owner-only file helpers refuse a link anywhere on the path they open,
    # so a home or config folder reached through one (a symlink, or a junction
    # such as AppData moved to another drive) would fail each of them. Named by
    # its real path here, the folder is linkless for everything built on it.
    config_dir = real_path(conventional_config_dir())
    return PlatformInfo(
        os_name=os_name,
        arch=arch,
        shell=shell,
        config_dir=config_dir,
        data_dir=config_dir,
        extra_shells=extra_shells,
        os_version=os_version,
    )


@functools.cache
def get_platform() -> PlatformInfo:
    """Cached platform singleton — safe to call from any module."""
    return detect_platform()


def safe_getcwd() -> str:
    """Return the current working directory, falling back to ``~`` if it no longer exists."""
    try:
        return os.getcwd()
    except OSError:
        return os.path.expanduser("~")


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


@dataclass(frozen=True)
class _Win32ClipboardAPI:
    """Typed handles for the lazily loaded Win32 clipboard libraries."""

    user32: ctypes.CDLL
    kernel32: ctypes.CDLL


@functools.lru_cache(maxsize=1)
def _win32_clipboard_api() -> _Win32ClipboardAPI:
    """Load user32/kernel32 once and pin argtypes/restype for clipboard calls.

    Cached so the per-call overhead is one dict lookup. Lazily constructed so
    non-Windows platforms never touch ``ctypes.WinDLL``.
    """
    windows_ctypes = cast(Any, ctypes)
    user32 = windows_ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    declare_functions(
        user32,
        {
            "OpenClipboard": (wintypes.BOOL, [wintypes.HWND]),
            "CloseClipboard": (wintypes.BOOL, []),
            "EmptyClipboard": (wintypes.BOOL, []),
            "IsClipboardFormatAvailable": (wintypes.BOOL, [wintypes.UINT]),
            "GetClipboardData": (wintypes.HANDLE, [wintypes.UINT]),
            "SetClipboardData": (wintypes.HANDLE, [wintypes.UINT, wintypes.HANDLE]),
            "CreateWindowExW": (
                wintypes.HWND,
                [
                    wintypes.DWORD,
                    wintypes.LPCWSTR,
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.HWND,
                    wintypes.HMENU,
                    wintypes.HINSTANCE,
                    wintypes.LPVOID,
                ],
            ),
            "DestroyWindow": (wintypes.BOOL, [wintypes.HWND]),
        },
    )
    declare_functions(
        kernel32,
        {
            "GlobalAlloc": (wintypes.HGLOBAL, [wintypes.UINT, ctypes.c_size_t]),
            "GlobalLock": (wintypes.LPVOID, [wintypes.HGLOBAL]),
            "GlobalUnlock": (wintypes.BOOL, [wintypes.HGLOBAL]),
            "GlobalFree": (wintypes.HGLOBAL, [wintypes.HGLOBAL]),
        },
    )
    return _Win32ClipboardAPI(user32=user32, kernel32=kernel32)


def _win32_error() -> OSError:
    """Build an OSError from the Win32 thread-local error state."""
    windows_ctypes = cast(Any, ctypes)
    return windows_ctypes.WinError(windows_ctypes.get_last_error())


def _open_clipboard_with_retry(
    api: _Win32ClipboardAPI, attempts: int = 5, delay: float = 0.01, *, owner: int | None = None
) -> None:
    """Acquire the clipboard. Retries on transient ERROR_ACCESS_DENIED."""
    import time

    for i in range(attempts):
        if api.user32.OpenClipboard(owner):
            return
        if i == attempts - 1:
            raise _win32_error()
        time.sleep(delay)


@contextmanager
def _clipboard_owner(api: _Win32ClipboardAPI) -> Generator[int]:
    """Give writes a real HWND owned by the calling thread, without a visible window.

    EmptyClipboard with an OpenClipboard(NULL) owner does not meet the
    SetClipboardData contract. A message-only STATIC window needs no custom
    window procedure; eager text survives its destruction after CloseClipboard.
    """
    window = api.user32.CreateWindowExW(0, "STATIC", None, 0, 0, 0, 0, 0, -3, None, None, None)
    if not window:
        raise _win32_error()
    try:
        yield window
    finally:
        api.user32.DestroyWindow(window)


def _clipboard_copy_windows(text: str) -> None:
    # Direct Win32 SetClipboardData(CF_UNICODETEXT, ...) — no subprocess, no
    # codepage ambiguity, no UTF-16 BOM leaking into the clipboard payload
    # (which is what ``clip.exe`` does, leaving a U+FEFF on every paste).
    api = _win32_clipboard_api()
    user32, kernel32 = api.user32, api.kernel32

    data = text.encode("utf-16-le") + b"\x00\x00"  # CF_UNICODETEXT requires NUL-terminated UTF-16-LE
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise _win32_error()
    with ExitStack() as allocation:
        allocation.callback(kernel32.GlobalFree, h)
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            raise _win32_error()
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(h)

        with _clipboard_owner(api) as owner:
            _open_clipboard_with_retry(api, owner=owner)
            try:
                if not user32.EmptyClipboard():
                    raise _win32_error()
                if not user32.SetClipboardData(CF_UNICODETEXT, h):
                    raise _win32_error()
                allocation.pop_all()  # ownership transferred to the clipboard
            finally:
                user32.CloseClipboard()


def clipboard_copy(text: str) -> None:
    """Copy text to the system clipboard, ignoring unavailable clipboard backends."""
    import contextlib
    import subprocess

    info = get_platform()
    with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError, OSError):
        if info.is_windows:
            _clipboard_copy_windows(text)
        elif info.is_macos:
            subprocess.run(["pbcopy"], input=text.encode(), check=True, timeout=2)
        else:
            for cmd in (["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
                try:
                    subprocess.run(cmd, input=text.encode(), check=True, timeout=2)
                    return
                except FileNotFoundError:
                    continue


def _clipboard_paste_windows() -> str:
    # Direct Win32 GetClipboardData(CF_UNICODETEXT). Mirrors
    # ``_clipboard_copy_windows`` and avoids PowerShell's trailing-newline
    # quirk and codepage-dependent stdout decoding.
    api = _win32_clipboard_api()
    user32, kernel32 = api.user32, api.kernel32

    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""

    _open_clipboard_with_retry(api)
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            # CF_UNICODETEXT is NUL-terminated UTF-16-LE; wstring_at follows the NUL.
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(h)
            # Don't GlobalFree — the handle is owned by the clipboard, not us.
    finally:
        user32.CloseClipboard()


def clipboard_paste() -> str:
    """Read text from system clipboard (cross-platform)."""
    import subprocess

    from chrys.foundation.platform.process import decode_subprocess_output

    try:
        info = get_platform()
        if info.is_macos:
            r = subprocess.run(["pbpaste"], stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=2)
            return decode_subprocess_output(r.stdout)
        if info.is_windows:
            return _clipboard_paste_windows()
        for cmd in (["xclip", "-selection", "clipboard", "-o"], ["xsel", "--clipboard", "--output"]):
            try:
                r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, check=True, timeout=2)
                return decode_subprocess_output(r.stdout)
            except FileNotFoundError:
                continue
    except subprocess.SubprocessError, FileNotFoundError, OSError:
        pass
    return ""


def _get_shell_version(shell_path: str, name: str) -> str:
    """Try to get the shell version string. Returns empty string on failure."""
    import contextlib
    import subprocess

    from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

    win_kwargs = windows_hidden_subprocess_kwargs()

    with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError, OSError):
        if name in ("pwsh", "powershell"):
            r = subprocess.run(
                [shell_path, "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
                # Startup probe: the process stdin (the ACP protocol pipe
                # under ACP, before its reader even starts) must not reach
                # the child.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                **win_kwargs,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        elif name != "cmd":
            r = subprocess.run(
                [shell_path, "--version"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                **win_kwargs,
            )
            if r.returncode == 0:
                # Extract version from output like "bash 5.2.37" or "zsh 5.9"
                for token in r.stdout.strip().split():
                    if token[0:1].isdigit():
                        return token
    return ""


def _is_executable_file(path: str) -> bool:
    """Return whether *path* points to an executable regular file."""
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _detect_unix_shell() -> ShellInfo:
    """Detect the default shell on macOS/Linux.

    Prefers ``$SHELL`` if it points to an executable binary, then probes common
    shell names via ``shutil.which()`` so we work on distros where shells live
    in non-standard locations (NixOS, Alpine, etc.). If ``$SHELL`` is stale and
    ``PATH`` is sparse, also try common absolute locations before falling back
    to ``/bin/sh``.
    """
    shell_env = os.environ.get("SHELL")
    if shell_env and _is_executable_file(shell_env):
        shell_path = shell_env
    else:
        resolved = None
        shell_names: list[str] = []
        if shell_env:
            shell_names.append(Path(shell_env).name)
        shell_names.extend(name for name in ("bash", "sh", "zsh", "dash", "ash") if name not in shell_names)
        for candidate in shell_names:
            resolved = shutil.which(candidate)
            if resolved and _is_executable_file(resolved):
                break
            resolved = None
        if resolved:
            shell_path = resolved
        else:
            shell_dirs = ("/bin", "/usr/bin", "/usr/local/bin")
            candidate_names: list[str] = []
            if shell_env:
                candidate_names.append(Path(shell_env).name)
            candidate_names.extend(name for name in ("bash", "zsh", "sh", "dash", "ash") if name not in candidate_names)
            absolute_candidates = [f"{directory}/{name}" for name in candidate_names for directory in shell_dirs]
            shell_path = next(
                (candidate for candidate in absolute_candidates if _is_executable_file(candidate)), "/bin/sh"
            )
    name = Path(shell_path).name
    version = _get_shell_version(shell_path, name)
    return ShellInfo(name=name, path=shell_path, args=["-c"], version=version)


def windows_program_files_dirs() -> tuple[str, ...]:
    """Return candidate Program Files directories on Windows.

    Uses the ``PROGRAMFILES`` / ``PROGRAMFILES(X86)`` environment variables
    (set by Windows itself and honouring the actual system install drive).
    If they are missing — e.g. in stripped-down environments — synthesises
    them from ``SystemDrive`` (e.g. ``D:``) rather than assuming ``C:``.
    """
    # ``os.environ`` is case-insensitive on Windows, so the canonical
    # uppercase names work regardless of the actual env var casing.
    dirs: list[str] = []
    seen: set[str] = set()
    system_drive = os.environ.get("SYSTEMDRIVE", "")
    candidates = (
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("PROGRAMW6432"),
        f"{system_drive}\\Program Files" if system_drive else None,
        f"{system_drive}\\Program Files (x86)" if system_drive else None,
    )
    for path in candidates:
        if path and path not in seen:
            seen.add(path)
            dirs.append(path)
    return tuple(dirs)


def is_wsl_bash(path: str) -> bool:
    """Whether *path* is a Windows ``bash`` that starts WSL, which runs a script inside Linux.

    System32's ``bash.exe`` is WSL's launcher, and a ``bash`` behind an App Execution Alias is a packaged (Store)
    WSL's: Git, MSYS2 and Cygwin are never packaged.
    """
    system32_bash = ntpath.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "bash.exe")
    return ntpath.normcase(ntpath.normpath(path)) == ntpath.normcase(system32_bash) or is_app_execution_alias(path)


def _detect_git_bash() -> ShellInfo | None:
    """Detect Git Bash on Windows.

    Searches common Git for Windows install paths and PATH.  WSL bash is
    excluded by checking that the resolved path contains "Git" and is not
    WSL's (a user name can contain "git").
    """
    import contextlib
    import subprocess
    from concurrent.futures import ThreadPoolExecutor

    candidates: list[str] = []

    # Known Git for Windows install paths
    for base in windows_program_files_dirs():
        path = os.path.join(base, "Git", "bin", "bash.exe")
        if os.path.isfile(path):
            candidates.append(path)

    # Check PATH — only accept if path looks like Git Bash (not WSL)
    which_bash = shutil.which("bash")
    if which_bash and "git" in which_bash.lower() and not is_wsl_bash(which_bash):
        candidates.append(which_bash)

    if not candidates:
        return None

    bash_path = candidates[0]

    # Try to get git version from the same installation
    git_path = os.path.join(os.path.dirname(bash_path), "git.exe")
    has_git = os.path.isfile(git_path)

    def _get_git_version() -> str:
        if not has_git:
            return ""
        from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs

        with contextlib.suppress(subprocess.SubprocessError, FileNotFoundError, OSError):
            r = subprocess.run(
                [git_path, "--version"],
                # Startup probe: never hand the child the process stdin —
                # under ACP that is the protocol pipe, and this runs before
                # its reader starts.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                **windows_hidden_subprocess_kwargs(),
            )
            if r.returncode == 0:
                return r.stdout.strip()
        return ""

    # Run bash --version and git --version in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        bash_ver_future = pool.submit(_get_shell_version, bash_path, "bash")
        git_ver_future = pool.submit(_get_git_version)
        bash_version = bash_ver_future.result()
        git_version = git_ver_future.result()

    version = f"{bash_version} ({git_version})" if bash_version and git_version else bash_version or git_version
    return ShellInfo(name="git_bash", path=bash_path, args=["-c"], version=version)


def _detect_windows_shell() -> ShellInfo:
    """Detect the default shell on Windows.

    Preference order:
    1. ``pwsh`` (PowerShell 7+ / cross-platform) — newest, best UX
    2. ``powershell`` (Windows PowerShell 5.1, ships with Windows)
    3. ``cmd`` (fallback)
    """
    # PowerShell 7+ (cross-platform, installed separately)
    pwsh = shutil.which("pwsh")
    if pwsh:
        version = _get_shell_version(pwsh, "pwsh")
        return ShellInfo(name="pwsh", path=pwsh, args=["-NoProfile", "-Command"], version=version)
    # Windows PowerShell 5.1 (built-in, always at this path on modern Windows)
    ps5 = shutil.which("powershell")
    if ps5:
        version = _get_shell_version(ps5, "powershell")
        return ShellInfo(name="powershell", path=ps5, args=["-NoProfile", "-Command"], version=version)
    # Fallback to cmd.  Prefer COMSPEC; fall back to SystemRoot (Windows
    # always sets this to the actual install path, e.g. ``D:\Windows``)
    # before assuming the drive letter.
    cmd = os.environ.get("COMSPEC")
    if not cmd:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        cmd = os.path.join(system_root, "System32", "cmd.exe")
    return ShellInfo(name="cmd", path=cmd, args=["/c"])
