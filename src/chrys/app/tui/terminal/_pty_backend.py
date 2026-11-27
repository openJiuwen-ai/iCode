# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Typed loading boundary for mutually exclusive POSIX and Windows PTY APIs."""

from __future__ import annotations

import importlib
import os
import signal
import struct
from contextlib import suppress
from functools import cache
from typing import Any, Protocol, cast

from chrys.foundation.platform import get_platform

IS_WINDOWS = get_platform().is_windows


class WinPTYProtocol(Protocol):
    """pywinpty surface used by the terminal shell."""

    pid: int

    def isalive(self) -> bool: ...

    def read(self, *, blocking: bool) -> str: ...

    def set_size(self, cols: int, rows: int) -> None: ...

    def spawn(self, command: str, *, cwd: str, env: str) -> None: ...

    def write(self, text: str) -> None: ...


class WinPTYFactory(Protocol):
    """Constructor surface exposed by pywinpty."""

    def __call__(self, cols: int, rows: int) -> WinPTYProtocol: ...


@cache
def _posix_pty_modules() -> tuple[Any, Any, Any]:
    return (
        importlib.import_module("fcntl"),
        importlib.import_module("pty"),
        importlib.import_module("termios"),
    )


def open_pty() -> tuple[int, int]:
    """Open a POSIX pseudo-terminal pair."""
    _, pty, _ = _posix_pty_modules()
    return pty.openpty()


def set_pty_nonblocking(fd: int) -> None:
    """Put a POSIX pseudo-terminal file descriptor in non-blocking mode."""
    fcntl, _, _ = _posix_pty_modules()
    posix_os = cast(Any, os)
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, flags | posix_os.O_NONBLOCK)


def prepare_child_pty(slave_fd: int) -> None:
    """Create a POSIX session and make the slave its controlling terminal."""
    fcntl, _, termios = _posix_pty_modules()
    cast(Any, os).setsid()
    fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)


def resize_pty(fd: int, cols: int, rows: int) -> bool:
    """Resize a POSIX pseudo-terminal."""
    fcntl, _, termios = _posix_pty_modules()
    try:
        size = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, size)
    except OSError:
        return False
    return True


def kill_posix_process(pid: int, *, fallback_to_process: bool) -> None:
    """Kill a POSIX process group, optionally falling back to its leader."""
    posix_os = cast(Any, os)
    posix_signal = cast(Any, signal)
    from chrys.foundation.platform.process import kill_process_group

    # Also reaches the command behind a sandbox stub (a linked session).
    if kill_process_group(pid):
        return
    try:
        posix_os.killpg(pid, posix_signal.SIGKILL)
    except ProcessLookupError, PermissionError, OSError:
        if fallback_to_process:
            with suppress(ProcessLookupError, OSError):
                posix_os.kill(pid, posix_signal.SIGKILL)


def _missing_winpty(cols: int, rows: int) -> WinPTYProtocol:
    del cols, rows
    raise RuntimeError("WinPTY is unavailable on this platform")


def load_winpty() -> tuple[WinPTYFactory, tuple[type[BaseException], ...]]:
    """Import pywinpty: its PTY factory and error types, or a factory that raises off Windows."""
    if not IS_WINDOWS:
        return _missing_winpty, ()
    module = cast(Any, importlib.import_module("winpty"))
    error_type: type[BaseException] | None = None
    with suppress(AttributeError):
        error_type = module.WinptyError
    errors: tuple[type[BaseException], ...] = ()
    if error_type is not None:
        errors = (error_type,)
    return cast("WinPTYFactory", module.PTY), errors
