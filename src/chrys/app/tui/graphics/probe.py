# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Ask the terminal what it is before Textual takes its input.

Textual's input parser types a reply it does not know into the prompt as keys, so the replies are
read here, before the app starts. XTVERSION (``CSI > 0 q``) names the terminal and its version, and
primary device attributes (``CSI c``), which every terminal answers, and answers after the others,
end the exchange; an attribute ``4`` says it draws Sixel. A terminal that named itself is then asked
its cell size (``CSI 16 t``) in a second exchange, where the tty does not give it. Not before: a
ConPTY older than Windows Terminal 1.22's, such as the one Windows ships, answers the device
attributes itself and passes other queries on, so the terminal's answers would come after the
exchange had ended and reach the app as keys; that ConPTY drops the XTVERSION reply, so it is never
asked the second time. The kitty graphics query is not sent: terminals without APC
(Apple Terminal, the Linux console) print it, and tmux makes it the pane title.

Input already queued is dropped before each exchange: a reply an earlier program left unread would
end it early. Input is then read one key event at a time, so keys typed after the last reply stay
queued for Textual; keys typed before it are dropped. A terminal that answers nothing within
``QUIET_SECONDS`` is taken as one that does not answer; once it has answered something it is given
``REPLY_SECONDS`` for the rest. A reply later than that may show on screen and reaches Textual as
keys.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Final, Protocol

from chrys.foundation.platform.c_api import declare_functions, struct_fields

if TYPE_CHECKING:
    from collections.abc import Callable

QUERIES: Final = "\x1b[>0q\x1b[c"
"""XTVERSION, then device attributes."""
CELL_SIZE_QUERIES: Final = "\x1b[16t\x1b[c"
"""The cell size, then device attributes."""
QUIET_SECONDS: Final = 1.5
REPLY_SECONDS: Final = 5.0
MAX_CELL_PIXELS: Final = 512
"""Larger cell sizes are taken as wrong."""

_VERSION = re.compile(r"\x1bP>\|([^\x1b]*)\x1b\\")
_CELL_PIXELS = re.compile(r"\x1b\[6;(\d+);(\d+)t")
_ATTRIBUTES = re.compile(r"\x1b\[\?([\d;]*)c")

_STD_INPUT_HANDLE: Final = -10
_STD_OUTPUT_HANDLE: Final = -11
_ENABLE_PROCESSED_OUTPUT: Final = 0x0001
_ENABLE_VIRTUAL_TERMINAL_PROCESSING: Final = 0x0004
_ENABLE_VIRTUAL_TERMINAL_INPUT: Final = 0x0200
_KEY_EVENT: Final = 0x0001
_WAIT_OBJECT_0: Final = 0
_WAIT_TIMEOUT: Final = 0x102


@dataclass(frozen=True)
class TerminalReport:
    """What the terminal answered."""

    version: str = ""
    """The XTVERSION reply (``ghostty 1.4.0``, ``kitty(0.49.2)``), or "" when there was none."""
    attributes: frozenset[int] = frozenset()
    """Primary device attributes."""
    cell_pixels: tuple[int, int] | None = None
    """Cell width and height from ``CSI 16 t``: device pixels, points in iTerm2, a virtual 10 by 20 in
    Windows Terminal."""


def parse_replies(text: str) -> TerminalReport | None:
    """The report ``text`` holds, or None until it holds the device attributes, the last reply."""
    attributes = _ATTRIBUTES.search(text)
    if attributes is None:
        return None
    version = _VERSION.search(text)
    cells = _CELL_PIXELS.search(text)
    cell_pixels = None
    if cells is not None:
        height, width = int(cells[1]), int(cells[2])
        if 0 < width <= MAX_CELL_PIXELS and 0 < height <= MAX_CELL_PIXELS:
            cell_pixels = (width, height)
    return TerminalReport(
        version="" if version is None else version[1],
        attributes=frozenset(int(value) for value in attributes[1].split(";") if value),
        cell_pixels=cell_pixels,
    )


def read_report(
    read: Callable[[float], str | None], *, clock: Callable[[], float] = time.monotonic
) -> TerminalReport | None:
    """Read replies with ``read`` until they are complete, or None when time runs out first.

    ``read(timeout)`` returns what one key event typed, "" when ``timeout`` seconds passed with none or
    the event typed nothing, and None when the input has ended.
    """
    started = clock()
    deadline = started + QUIET_SECONDS
    answered = False
    text = ""
    while (remaining := deadline - clock()) > 0:
        typed = read(remaining)
        if typed is None:
            return None
        text += typed
        if (report := parse_replies(text)) is not None:
            return report
        if not answered and (_VERSION.search(text) or _CELL_PIXELS.search(text)):
            answered = True
            deadline = started + REPLY_SECONDS
    return None


def _ask(ask: Callable[[str], TerminalReport | None], *, cell_size_known: bool) -> TerminalReport | None:
    """What the terminal is, from ``ask(queries)``; one that named itself is asked its cell size too,
    unless that is known."""
    report = ask(QUERIES)
    if report is None or not report.version or cell_size_known:
        return report
    sized = ask(CELL_SIZE_QUERIES)
    return report if sized is None else replace(report, cell_pixels=sized.cell_pixels)


def query_terminal() -> TerminalReport | None:
    """Ask the terminal Textual will run in, or None when it cannot be asked or did not answer."""
    if sys.platform == "win32":
        console = _Kernel32Console.load()
        return None if console is None else query_console(console, _write_stdout)
    return _query_tty()


def _query_tty() -> TerminalReport | None:
    """Ask through stdin and stderr, the streams Textual's POSIX driver reads and writes."""
    if sys.platform == "win32":
        return None
    import select
    import termios

    try:
        if sys.__stdin__ is None or sys.__stderr__ is None:
            return None
        in_fd, out_fd = sys.__stdin__.fileno(), sys.__stderr__.fileno()
        # In the background, changing the terminal's mode would stop the process (SIGTTOU).
        if not os.isatty(in_fd) or not os.isatty(out_fd) or os.tcgetpgrp(in_fd) != os.getpgrp():
            return None
        saved = termios.tcgetattr(in_fd)
    except OSError, ValueError, termios.error:
        return None

    def read(timeout: float) -> str | None:
        if not select.select([in_fd], [], [], timeout)[0]:
            return ""
        data = os.read(in_fd, 1)
        return data.decode("latin-1") if data else None

    def ask(queries: str) -> TerminalReport | None:
        termios.tcflush(in_fd, termios.TCIFLUSH)
        _write_all(out_fd, queries.encode())
        return read_report(read)

    # Keys arrive unechoed as typed, Ctrl+C still interrupts, and a read returns what has arrived. Ctrl+S
    # cannot hold the queries back: without flow control, Linux restarts output it stopped.
    special = list(saved[6])
    special[termios.VMIN] = special[termios.VTIME] = 0
    mode = [
        saved[0] & ~termios.IXON,
        *saved[1:3],
        saved[3] & ~(termios.ICANON | termios.ECHO),
        *saved[4:6],
        special,
    ]
    try:
        termios.tcsetattr(in_fd, termios.TCSANOW, mode)
        return _ask(ask, cell_size_known=terminal_cell_pixels(in_fd) is not None)
    except OSError, termios.error:
        return None
    finally:
        with contextlib.suppress(OSError, termios.error):
            termios.tcsetattr(in_fd, termios.TCSANOW, saved)


def _write_all(fd: int, data: bytes) -> None:
    while data:
        data = data[os.write(fd, data) :]


def terminal_cell_pixels(fd: int) -> tuple[int, int] | None:
    """The terminal's cell size in pixels from ``TIOCGWINSZ``, or None where it is not reported."""
    if sys.platform == "win32":
        return None
    import fcntl
    import struct
    import termios

    try:
        packed = fcntl.ioctl(fd, termios.TIOCGWINSZ, bytes(8))
    except OSError:
        return None
    rows, columns, width, height = struct.unpack("HHHH", packed)
    if not rows or not columns or not width or not height:
        return None
    return max(1, width // columns), max(1, height // rows)


class Console(Protocol):
    """The Windows console calls the query makes."""

    def mode(self, handle: int) -> int | None:
        """The handle's console mode, or None when it is not a console."""
        ...

    def set_mode(self, handle: int, mode: int) -> bool: ...

    def flush(self, handle: int) -> None:
        """Drop the input events already queued."""
        ...

    def wait(self, handle: int, timeout: float) -> bool | None:
        """Whether input arrived within ``timeout`` seconds; None when waiting failed."""
        ...

    def read_key(self, handle: int) -> str | None:
        """What the next input event typed ("" for any but a key press), or None when reading failed."""
        ...


def query_console(
    console: Console, write: Callable[[str], object], *, clock: Callable[[], float] = time.monotonic
) -> TerminalReport | None:
    """Ask through the standard console handles, the ones Textual's Windows driver uses.

    Input is set to VT input, as Textual sets it, so replies arrive as key presses; output gets VT
    processing, without which the queries never reach the terminal. Both modes are restored after.
    """
    input_mode, output_mode = console.mode(_STD_INPUT_HANDLE), console.mode(_STD_OUTPUT_HANDLE)
    if input_mode is None or output_mode is None:
        return None

    def read(timeout: float) -> str | None:
        ready = console.wait(_STD_INPUT_HANDLE, timeout)
        if not ready:
            return None if ready is None else ""
        return console.read_key(_STD_INPUT_HANDLE)

    def ask(queries: str) -> TerminalReport | None:
        console.flush(_STD_INPUT_HANDLE)
        write(queries)
        return read_report(read, clock=clock)

    try:
        vt_output = output_mode | _ENABLE_VIRTUAL_TERMINAL_PROCESSING | _ENABLE_PROCESSED_OUTPUT
        if not console.set_mode(_STD_OUTPUT_HANDLE, vt_output):
            return None
        if not console.set_mode(_STD_INPUT_HANDLE, _ENABLE_VIRTUAL_TERMINAL_INPUT):
            return None
        return _ask(ask, cell_size_known=False)
    except OSError:
        return None
    finally:
        console.set_mode(_STD_INPUT_HANDLE, input_mode)
        console.set_mode(_STD_OUTPUT_HANDLE, output_mode)


def _write_stdout(text: str) -> None:
    stream = sys.__stdout__
    if stream is None:
        error_message = "stdout is closed"
        raise OSError(error_message)
    stream.write(text)
    stream.flush()


class KeyEventRecord(ctypes.Structure):
    """``KEY_EVENT_RECORD``, in fixed-width types so its layout is Windows' on every platform."""

    _fields_ = struct_fields(
        (ctypes.c_int32, "bKeyDown"),
        (ctypes.c_uint16, "wRepeatCount wVirtualKeyCode wVirtualScanCode UnicodeChar"),
        (ctypes.c_uint32, "dwControlKeyState"),
    )


class _Event(ctypes.Union):
    # Every other event record is at most as large as a key event's.
    _fields_ = struct_fields((KeyEventRecord, "KeyEvent"))


class InputRecord(ctypes.Structure):
    """``INPUT_RECORD``; only key events are read."""

    _fields_ = struct_fields((ctypes.c_uint16, "EventType"), (_Event, "Event"))


def key_text(record: InputRecord) -> str:
    """What a console input record typed: a key press's UTF-16 unit, else ""."""
    key = record.Event.KeyEvent
    if record.EventType != _KEY_EVENT or not key.bKeyDown or not key.UnicodeChar:
        return ""
    return chr(key.UnicodeChar)


class _Kernel32Console:
    """``Console`` through kernel32."""

    def __init__(self, kernel32: Any) -> None:
        self._kernel32 = kernel32

    @classmethod
    def load(cls) -> _Kernel32Console | None:
        if sys.platform != "win32":
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            declare_functions(
                kernel32,
                {
                    "GetStdHandle": (wintypes.HANDLE, [wintypes.DWORD]),
                    "GetConsoleMode": (wintypes.BOOL, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]),
                    "SetConsoleMode": (wintypes.BOOL, [wintypes.HANDLE, wintypes.DWORD]),
                    "FlushConsoleInputBuffer": (wintypes.BOOL, [wintypes.HANDLE]),
                    "WaitForSingleObject": (wintypes.DWORD, [wintypes.HANDLE, wintypes.DWORD]),
                    "ReadConsoleInputW": (
                        wintypes.BOOL,
                        [wintypes.HANDLE, ctypes.POINTER(InputRecord), wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)],
                    ),
                },
            )
        except OSError, AttributeError:
            return None
        return cls(kernel32)

    def mode(self, handle: int) -> int | None:
        mode = wintypes.DWORD()
        if not self._kernel32.GetConsoleMode(self._kernel32.GetStdHandle(handle), ctypes.byref(mode)):
            return None
        return mode.value

    def set_mode(self, handle: int, mode: int) -> bool:
        return bool(self._kernel32.SetConsoleMode(self._kernel32.GetStdHandle(handle), mode))

    def flush(self, handle: int) -> None:
        self._kernel32.FlushConsoleInputBuffer(self._kernel32.GetStdHandle(handle))

    def wait(self, handle: int, timeout: float) -> bool | None:
        result = self._kernel32.WaitForSingleObject(self._kernel32.GetStdHandle(handle), max(1, round(timeout * 1000)))
        if result == _WAIT_TIMEOUT:
            return False
        return True if result == _WAIT_OBJECT_0 else None

    def read_key(self, handle: int) -> str | None:
        record, count = InputRecord(), wintypes.DWORD()
        if not self._kernel32.ReadConsoleInputW(
            self._kernel32.GetStdHandle(handle), ctypes.byref(record), 1, ctypes.byref(count)
        ):
            return None
        return key_text(record) if count.value else ""
