# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Terminal launcher helpers for opening additional Chrys windows."""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.process import managed_subprocess

_SSH_ENV_VARS = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
_HOSTED_WORKSPACE_ENV_VARS = ("GITPOD_WORKSPACE_ID", "REPL_ID")
_HOSTED_WORKSPACE_FLAG_ENV_VARS = ("CODESPACES", "CLOUD_SHELL")
_TEXTUAL_WEB_DRIVER = "textual.drivers.web_driver:WebDriver"
_LINUX_DESKTOP_ENV_VARS = ("DISPLAY", "WAYLAND_DISPLAY", "MIR_SOCKET")
_CORE_GRAPHICS_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CORE_FOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_TUI_SUBPROCESS_COMMAND = "__tui_subprocess__"
_GHOSTTY_APP_NAME = "Ghostty"
_QUICK_LAUNCH_TIMEOUT_SECONDS = 0.25

_TERMINAL_LAUNCH_UNAVAILABLE = msg(
    "tui.terminal.launch.unavailable",
    fallback="Opening a new {app} window is not available in the current environment.",
)
_TERMINAL_LAUNCH_MACOS_FAILED = msg(
    "tui.terminal.launch.macos_failed",
    fallback="Could not open Terminal: {error}",
)
_TERMINAL_LAUNCH_NO_REPLY = msg(
    "tui.terminal.launch.no_reply", fallback="The terminal did not confirm whether the window opened."
)
_TERMINAL_LAUNCH_WINDOWS_FAILED = msg(
    "tui.terminal.launch.windows_failed",
    fallback="Could not open a new command window: {error}",
)
_TERMINAL_LAUNCH_UNSUPPORTED = msg(
    "tui.terminal.launch.unsupported",
    fallback="No supported terminal emulator was found.",
)
_TERMINAL_LAUNCH_EXITED = msg(
    "tui.terminal.launch.exited",
    fallback="Could not open {label}: launcher exited with status {failure_code}.",
)


class TerminalLaunchError(RuntimeError):
    """Raised when Chrys cannot open a new terminal window."""

    def __init__(self, message: str, *, display_message: MessageRef | None = None) -> None:
        self.display_message = display_message
        super().__init__(message)


def is_remote_ssh_session(env: Mapping[str, str] | None = None) -> bool:
    """Return True when Chrys is running inside an SSH login session."""
    values = os.environ if env is None else env
    return any(bool(values.get(name)) for name in _SSH_ENV_VARS)


def is_remote_execution_environment(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the active frontend is hosted away from this desktop."""
    values = os.environ if env is None else env
    if is_remote_ssh_session(values) or values.get("TEXTUAL_DRIVER") == _TEXTUAL_WEB_DRIVER:
        return True
    if any(bool(values.get(name)) for name in _HOSTED_WORKSPACE_ENV_VARS):
        return True
    return any(_env_flag_is_true(values.get(name)) for name in _HOSTED_WORKSPACE_FLAG_ENV_VARS)


def can_access_local_desktop(env: Mapping[str, str] | None = None) -> bool:
    """Return whether this process is attached to a desktop that can show windows.

    ``env`` overrides environment-marker inputs only. The Windows window-station
    and macOS Quartz probes always inspect the current process's real session.
    """
    values = os.environ if env is None else env
    if is_remote_execution_environment(values):
        return False
    platform = get_platform()
    if platform.is_windows:
        return _windows_desktop_shell_is_visible()
    if platform.is_macos:
        return _macos_window_server_session_is_available()
    return any(bool(values.get(name)) for name in _LINUX_DESKTOP_ENV_VARS)


def can_open_new_chrys_window(env: Mapping[str, str] | None = None) -> bool:
    """Return whether the current host can reasonably open a local Chrys window."""
    return can_access_local_desktop(env)


def build_chrys_tui_argv(session_id: str) -> list[str]:
    """Return argv that opens Chrys TUI and restores *session_id*.

    A sandboxed window opens a sandboxed window: the new process decides for
    itself at start, and a ``--sandbox`` given on this one's command line would
    otherwise be lost.
    """
    from chrys.foundation import guard

    sandbox = guard.fork_flags()
    return [*_entrypoint_argv(), _TUI_SUBPROCESS_COMMAND, "--session", session_id, *sandbox]


def build_macos_fork_argv(session_id: str) -> list[str]:
    # The terminal host creates the child independently of this process.
    from chrys.foundation.config.settings import SESSION_ROOT_DIR_ENV_VAR, resolve_session_root_dir

    return [
        "/usr/bin/env",
        f"HOME={Path.home()}",
        f"{SESSION_ROOT_DIR_ENV_VAR}={resolve_session_root_dir(create=False)}",
        *build_chrys_tui_argv(session_id),
    ]


def macos_login_shell_argv(argv: list[str], *, cwd: str | None) -> list[str]:
    # A terminal's custom-command API skips shell startup. Load the user's
    # normal interactive environment before restoring the fork's storage and cwd.
    shell_env = [f"HOME={Path.home()}"]
    shell_env.extend(f"{name}={os.environ[name]}" for name in ("ZDOTDIR", "XDG_CONFIG_HOME") if os.environ.get(name))
    shell = os.environ.get("SHELL") or "/bin/zsh"
    command = macos_shell_command(argv, cwd=cwd)
    match Path(shell).name:
        case "csh" | "tcsh":
            # csh disallows -l alongside -c. Pass the POSIX command as data
            # so csh history expansion cannot reinterpret a '!' in a path.
            shell_argv = [shell, "-i", "-c", 'exec /bin/sh -c "$argv[1]"', command]
        case "zsh" | "bash" | "sh" | "dash" | "ksh" | "fish":
            shell_argv = [shell, "-l", "-i", "-c", command]
        case _:
            shell_argv = ["/bin/sh", "-c", command]
    return ["/usr/bin/env", *shell_env, *shell_argv]


async def run_macos_terminal_script(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    # Only the AppleEvent sender belongs to this request. The terminal host
    # and the resumed session it starts have their own lifetimes.
    argv = ["/usr/bin/osascript", "-", *args]
    async with managed_subprocess(
        *argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    ) as proc:
        stdout, stderr = await asyncio.wait_for(proc.communicate(script.encode()), timeout=120)
        return subprocess.CompletedProcess(
            argv, await proc.wait(), stdout.decode(errors="replace"), stderr.decode(errors="replace")
        )


async def launch_new_chrys_window(session_id: str, *, cwd: str | None = None) -> None:
    """Open a new local terminal window running Chrys for *session_id*."""
    if not await asyncio.to_thread(can_open_new_chrys_window):
        display_message = _TERMINAL_LAUNCH_UNAVAILABLE.bind(app=APP_DISPLAY_NAME)
        raise TerminalLaunchError(format_message(display_message), display_message=display_message)
    platform = get_platform()
    if platform.is_macos:
        argv = await asyncio.to_thread(build_macos_fork_argv, session_id)
        await _launch_macos(argv, cwd=cwd)
        return
    argv = build_chrys_tui_argv(session_id)
    launch = _launch_windows if platform.is_windows else _launch_linux
    await asyncio.to_thread(launch, argv, cwd=cwd)


def _env_flag_is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _macos_window_server_session_is_available() -> bool:
    """Return whether this process belongs to a macOS Quartz GUI session."""
    import ctypes

    try:
        core_graphics = ctypes.CDLL(_CORE_GRAPHICS_PATH)
        core_foundation = ctypes.CDLL(_CORE_FOUNDATION_PATH)
        copy_current_session = core_graphics.CGSessionCopyCurrentDictionary
        copy_current_session.argtypes = ()
        copy_current_session.restype = ctypes.c_void_p
        release = core_foundation.CFRelease
        release.argtypes = (ctypes.c_void_p,)
        release.restype = None
    except AttributeError, OSError:
        return False

    session = copy_current_session()
    if not session:
        return False
    release(session)
    return True


def _windows_desktop_shell_is_visible() -> bool:
    """Return whether this Windows process owns a visible window station with Explorer's shell window."""
    import ctypes
    from ctypes import wintypes
    from typing import Any, cast

    class UserObjectFlags(ctypes.Structure):
        _fields_ = (("inherit", wintypes.BOOL), ("reserved", wintypes.BOOL), ("flags", wintypes.DWORD))

    try:
        windows_ctypes = cast(Any, ctypes)
        user32 = windows_ctypes.WinDLL("user32", use_last_error=True)
    except OSError:
        return False
    get_window_station = user32.GetProcessWindowStation
    get_window_station.argtypes = ()
    get_window_station.restype = wintypes.HANDLE
    window_station = get_window_station()
    if not window_station:
        return False

    get_user_object_info = user32.GetUserObjectInformationW
    get_user_object_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_user_object_info.restype = wintypes.BOOL
    flags = UserObjectFlags()
    needed = wintypes.DWORD()
    if not get_user_object_info(window_station, 1, ctypes.byref(flags), ctypes.sizeof(flags), ctypes.byref(needed)):
        return False

    get_shell_window = user32.GetShellWindow
    get_shell_window.argtypes = ()
    get_shell_window.restype = wintypes.HWND
    return bool(flags.flags & 1) and bool(get_shell_window())


def _entrypoint_argv() -> list[str]:
    pyapp_launcher = os.environ.get("PYAPP")
    if pyapp_launcher and pyapp_launcher != "1" and Path(pyapp_launcher).is_file():
        return [pyapp_launcher]
    if _looks_like_python(sys.executable):
        return [sys.executable, "-m", "chrys.app.cli.app"]
    return [sys.argv[0]]


def _looks_like_python(executable: str) -> bool:
    name = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name.startswith(("python", "pypy"))


async def _launch_macos(argv: list[str], *, cwd: str | None) -> None:
    if _is_ghostty_session() and await asyncio.to_thread(_try_launch_macos_ghostty, argv, cwd=cwd):
        return
    if _is_wezterm_session() and await asyncio.to_thread(
        _try_launch_wezterm_window, macos_login_shell_argv(argv, cwd=cwd), cwd=cwd
    ):
        return
    if _is_iterm_session():
        command = shlex.join(macos_login_shell_argv(argv, cwd=cwd))
        script = (
            'tell application "iTerm2"\n activate\n create window with default profile command startCommand\nend tell'
        )
    else:
        command = macos_shell_command(argv, cwd=cwd)
        script = 'tell application "Terminal"\n do script startCommand\n activate\nend tell'
    script = f"on run argv\nset startCommand to item 1 of argv\n{script}\nend run"
    try:
        result = await run_macos_terminal_script(script, command)
    except TimeoutError as exc:
        display_message = _TERMINAL_LAUNCH_NO_REPLY.bind()
        raise TerminalLaunchError(format_message(display_message), display_message=display_message) from exc
    except OSError as exc:
        display_message = _TERMINAL_LAUNCH_MACOS_FAILED.bind(error=str(exc))
        raise TerminalLaunchError(format_message(display_message), display_message=display_message) from exc
    if result.returncode:
        display_message = _TERMINAL_LAUNCH_MACOS_FAILED.bind(error=result.stderr.strip() or str(result.returncode))
        raise TerminalLaunchError(format_message(display_message), display_message=display_message)


def _is_ghostty_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_contains("ghostty", env=env) or _env_has_any(("GHOSTTY_BIN_DIR", "GHOSTTY_RESOURCES_DIR"), env=env)


def _try_launch_macos_ghostty(argv: list[str], *, cwd: str | None) -> bool:
    ghostty_args = ["-e", *macos_login_shell_argv(argv, cwd=cwd)]
    ghostty_path = _ghostty_cli_path()
    if ghostty_path is not None:
        args = [ghostty_path, *ghostty_args]
    else:
        args = ["/usr/bin/open", "-na", _GHOSTTY_APP_NAME, "--args", *ghostty_args]
    return _try_spawn(args, verify_quick_exit=True)


def _ghostty_cli_path(env: Mapping[str, str] | None = None) -> str | None:
    values = os.environ if env is None else env
    bin_dir = values.get("GHOSTTY_BIN_DIR", "")
    if bin_dir:
        candidate = Path(bin_dir) / "ghostty"
        if candidate.exists():
            return str(candidate)
    return shutil.which("ghostty")


def _is_iterm_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_contains("iterm", env=env)


def _is_wezterm_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_contains("wezterm", env=env) or _env_has_any(("WEZTERM_EXECUTABLE", "WEZTERM_PANE"), env=env)


def _try_launch_wezterm_window(argv: list[str], *, cwd: str | None) -> bool:
    wezterm = shutil.which("wezterm")
    if wezterm is None:
        return False
    args = [wezterm, "cli", "spawn", "--new-window"]
    if cwd:
        args.extend(["--cwd", cwd])
    args.extend(["--", *argv])
    return _try_spawn(args, verify_quick_exit=True)


def _launch_windows(argv: list[str], *, cwd: str | None) -> None:
    if _is_windows_terminal_session() and _try_launch_windows_terminal_window(argv, cwd=cwd):
        return
    comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    try:
        _spawn_or_raise(
            [comspec, "/c", "start", "", *argv],
            cwd=cwd,
            label="command window",
            verify_quick_exit=True,
        )
    except OSError as exc:
        display_message = _TERMINAL_LAUNCH_WINDOWS_FAILED.bind(error=str(exc))
        raise TerminalLaunchError(format_message(display_message), display_message=display_message) from exc


def _is_windows_terminal_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_has_any(("WT_SESSION", "WT_PROFILE_ID"), env=env)


def _try_launch_windows_terminal_window(argv: list[str], *, cwd: str | None) -> bool:
    wt = shutil.which("wt")
    if wt is None:
        return False
    args = [wt, "-w", "new", "new-tab"]
    if cwd:
        args.extend(["-d", cwd])
    args.extend(argv)
    return _try_spawn(args, verify_quick_exit=True)


def _launch_linux(argv: list[str], *, cwd: str | None) -> None:
    if _is_wezterm_session() and _try_launch_wezterm_window(argv, cwd=cwd):
        return
    if _is_kitty_session() and _try_launch_kitty_window(argv, cwd=cwd):
        return
    if _is_gnome_terminal_session() and _try_launch_gnome_terminal_window(argv, cwd=cwd):
        return
    if _is_konsole_session() and _try_launch_konsole_window(argv, cwd=cwd):
        return

    launchers = (
        ("x-terminal-emulator", ["-e", *argv]),
        ("gnome-terminal", ["--", *argv]),
        ("konsole", ["-e", *argv]),
        ("xterm", ["-e", *argv]),
    )
    for executable, args in launchers:
        path = shutil.which(executable)
        if path is None:
            continue
        try:
            if not _try_spawn(
                [path, *args],
                cwd=cwd,
                verify_quick_exit=True,
            ):
                continue
        except OSError:
            continue
        return
    display_message = _TERMINAL_LAUNCH_UNSUPPORTED.bind()
    raise TerminalLaunchError(format_message(display_message), display_message=display_message)


def _is_kitty_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_has_any(("KITTY_LISTEN_ON",), env=env)


def _try_launch_kitty_window(argv: list[str], *, cwd: str | None) -> bool:
    kitty = shutil.which("kitty")
    if kitty is None:
        return False
    args = [kitty, "@", "launch", "--type=os-window"]
    if cwd:
        args.extend(["--cwd", cwd])
    args.extend(["--", *argv])
    return _try_spawn(args, verify_quick_exit=True)


def _is_gnome_terminal_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_has_any(("GNOME_TERMINAL_SCREEN", "GNOME_TERMINAL_SERVICE"), env=env)


def _try_launch_gnome_terminal_window(argv: list[str], *, cwd: str | None) -> bool:
    terminal = shutil.which("gnome-terminal")
    if terminal is None:
        return False
    args = [terminal, "--window", "--", *argv]
    return _try_spawn(args, cwd=cwd, verify_quick_exit=True)


def _is_konsole_session(env: Mapping[str, str] | None = None) -> bool:
    return _env_has_any(("KONSOLE_VERSION", "KONSOLE_DBUS_SESSION", "KONSOLE_DBUS_WINDOW"), env=env)


def _try_launch_konsole_window(argv: list[str], *, cwd: str | None) -> bool:
    konsole = shutil.which("konsole")
    if konsole is None:
        return False
    args = [konsole, "--separate", "-e", *argv]
    return _try_spawn(args, cwd=cwd, verify_quick_exit=True)


def _env_contains(needle: str, *, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    lower_needle = needle.lower()
    terminal_markers = (
        values.get("TERM_PROGRAM", ""),
        values.get("LC_TERMINAL", ""),
        values.get("TERMINAL_EMULATOR", ""),
        values.get("TERM", ""),
    )
    return any(lower_needle in marker.lower() for marker in terminal_markers)


def _env_has_any(names: tuple[str, ...], *, env: Mapping[str, str] | None = None) -> bool:
    values = os.environ if env is None else env
    return any(bool(values.get(name)) for name in names)


def macos_shell_command(argv: list[str], *, cwd: str | None) -> str:
    def quote_arg(value: str) -> str:
        # Interactive csh expands history even inside single quotes. Escape
        # literal exclamation marks outside the quoted segments in every shell.
        return r"\!".join(shlex.quote(part) for part in value.split("!"))

    command = " ".join(map(quote_arg, argv))
    if not cwd:
        return command
    return f"cd {quote_arg(cwd)} && {command}"


def _try_spawn(args: list[str], *, cwd: str | None = None, verify_quick_exit: bool = False) -> bool:
    try:
        process = _spawn(args, cwd=cwd)
    except OSError:
        return False
    if verify_quick_exit:
        return _quick_launch_failure_code(process) is None
    return True


def _spawn_or_raise(
    args: list[str],
    *,
    cwd: str | None = None,
    label: str,
    verify_quick_exit: bool = False,
) -> None:
    process = _spawn(args, cwd=cwd)
    if verify_quick_exit:
        failure_code = _quick_launch_failure_code(process)
        if failure_code is not None:
            display_message = _TERMINAL_LAUNCH_EXITED.bind(label=label, failure_code=failure_code)
            raise TerminalLaunchError(format_message(display_message), display_message=display_message)


def _spawn(args: list[str], *, cwd: str | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # noqa: S603
        args,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _quick_launch_failure_code(process: subprocess.Popen[bytes]) -> int | None:
    try:
        return_code = process.wait(timeout=_QUICK_LAUNCH_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return None
    return return_code if return_code != 0 else None
