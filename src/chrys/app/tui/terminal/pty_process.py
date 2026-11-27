# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A program running on a pseudo-terminal, the same to its owner on every platform.

POSIX gives the owner a file descriptor to read and write. Windows gives it a ConPTY, which is a
terminal emulator in its own right: it keeps a screen, and after a resize it sends that screen
again. `PtyProcess` is what the two have in common, and `repaints_after_resize` the one difference
an owner has to know about.
"""

from __future__ import annotations

import asyncio
import codecs
import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from contextlib import suppress
from functools import partial
from time import monotonic, sleep
from typing import Final, Protocol

from chrys.app.tui.terminal._pty_backend import (
    IS_WINDOWS,
    WinPTYProtocol,
    kill_posix_process,
    load_winpty,
    open_pty,
    prepare_child_pty,
    resize_pty,
    set_pty_nonblocking,
)
from chrys.foundation.platform.pty_output import PtyOutputProtocol

# A program painting a screen writes it in pieces. Shown piece by piece that is a frame each and a
# flicker between them, so a read keeps collecting while more arrives within `_BURST_GAP`, for at
# most `_BURST_WINDOW`: one frame at 60 Hz, which is as long as output may wait to be seen.
_BURST_GAP: Final = 1 / 100
_BURST_WINDOW: Final = 1 / 60
_READ_SIZE: Final = 64 * 1024


# pywinpty loads with this module, which only a starting shell imports.
WINPTY_FACTORY, WINPTY_ERRORS = load_winpty()


_CONPTY_ERRORS: Final = (OSError, RuntimeError, *WINPTY_ERRORS)

_INPUT_ERRORS: Final = "chrys.terminal-input"


def _encode_what_is_not_text(error: UnicodeError) -> tuple[bytes, int]:
    """A byte that ``surrogateescape`` carried into the ``str`` is that byte; nothing else is anything.

    A pointer report in xterm's original format counts columns in bytes, and those from 128 on
    reach us the way the file system's names do. Any other lone surrogate stands for nothing.
    """
    if not isinstance(error, UnicodeEncodeError):
        raise error
    lone = error.object[error.start : error.end]
    return bytes(ord(character) - 0xDC00 for character in lone if 0xDC80 <= ord(character) <= 0xDCFF), error.end


codecs.register_error(_INPUT_ERRORS, _encode_what_is_not_text)


class PtyProcess(Protocol):
    """A running program and the terminal it is attached to."""

    @property
    def size(self) -> tuple[int, int] | None:
        """Columns and lines the program was last told of, or ``None`` if telling it failed."""

    @property
    def repaints_after_resize(self) -> bool:
        """Whether the terminal between us and the program sends its screen again once resized."""

    async def read(self) -> str:
        """The program's next output. Empty once it has exited and everything has been read."""

    async def write(self, data: str) -> bool:
        """Type ``data`` into the program. Returns whether all of it was taken.

        A byte that is not text is spelled the way the ``surrogateescape`` error handler spells it.
        """

    async def resize(self, columns: int, lines: int) -> bool:
        """Tell the program its terminal has a new size."""

    def kill(self) -> None:
        """End the program, and everything it started, without waiting for it to finish."""

    async def close(self) -> None:
        """Release the terminal once the program is done with it."""


async def spawn_pty_process(
    argv: Sequence[str], *, env: Mapping[str, str], cwd: str, columns: int, lines: int
) -> PtyProcess:
    """Start ``argv`` on a new pseudo-terminal of the given size."""
    if IS_WINDOWS:
        return _ConPtyProcess.spawn(argv, env=env, cwd=cwd, columns=columns, lines=lines)
    return await _PosixPtyProcess.spawn(argv, env=env, cwd=cwd, columns=columns, lines=lines)


class _PosixPtyProcess:
    repaints_after_resize = False

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        reader: asyncio.StreamReader,
        transport: asyncio.BaseTransport,
        write_fd: int,
        slave_fd: int,
        size: tuple[int, int] | None,
    ) -> None:
        self._process = process
        self._reader = reader
        self._transport = transport
        self._write_fd: int | None = write_fd
        # macOS throws away what a terminal still holds once its last slave closes: a program's last
        # words, if it exits before the loop gets round to reading them. While we hold a slave too,
        # its exit waits for them to be read instead.
        self._slave_fd: int | None = slave_fd
        self._slave_release = asyncio.create_task(self._release_slave_after_exit())
        self._write_lock = asyncio.Lock()
        self._writable: asyncio.Future[None] | None = None
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.size = size

    @classmethod
    async def spawn(
        cls, argv: Sequence[str], *, env: Mapping[str, str], cwd: str, columns: int, lines: int
    ) -> _PosixPtyProcess:
        master, slave = open_pty()
        write_fd = -1
        try:
            set_pty_nonblocking(master)
            # The event loop takes the master over for reading and lets nobody else wait on it, so
            # writing gets a descriptor of its own.
            write_fd = os.dup(master)
            # Sized before the program starts: it must never see the 0x0 a new terminal has.
            size = (columns, lines) if resize_pty(slave, columns, lines) else None
            from chrys.foundation import guard

            with guard.current().controlling_terminal():
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=slave,
                    stdout=slave,
                    stderr=slave,
                    env=env,
                    cwd=cwd,
                    preexec_fn=partial(prepare_child_pty, slave),
                )
        except BaseException:
            for fd in (master, slave, write_fd):
                if fd >= 0:
                    with suppress(OSError):
                        os.close(fd)
            raise
        reader = asyncio.StreamReader(_READ_SIZE)
        master_file = os.fdopen(master, "rb", 0)
        try:
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: PtyOutputProtocol(reader), master_file
            )
        except BaseException:
            master_file.close()
            os.close(write_fd)
            os.close(slave)
            kill_posix_process(process.pid, fallback_to_process=True)
            with suppress(Exception):
                await process.wait()
            raise
        return cls(process, reader, transport, write_fd, slave, size)

    async def read(self) -> str:
        while True:
            data = await self._read_burst()
            text = self._decoder.decode(data, final=not data)
            if text or not data:
                return text
            # Only the first bytes of a character so far; the rest is on its way.

    async def _read_burst(self) -> bytes:
        reader = self._reader
        first = await reader.read(_READ_SIZE)
        if not first:
            return b""
        chunks = [first]
        room = _READ_SIZE - len(first)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _BURST_WINDOW
        with suppress(TimeoutError):
            while room > 0:
                async with asyncio.timeout_at(min(deadline, loop.time() + _BURST_GAP)):
                    chunk = await reader.read(room)
                if not chunk:
                    # The end; the next read will say so.
                    break
                chunks.append(chunk)
                room -= len(chunk)
        return b"".join(chunks)

    async def write(self, data: str) -> bool:
        remaining = memoryview(data.encode("utf-8", _INPUT_ERRORS))
        async with self._write_lock:
            while remaining:
                if (fd := self._write_fd) is None:
                    return False
                try:
                    written = os.write(fd, remaining)
                except BlockingIOError:
                    # The program is not reading as fast as we write (a large paste). Dropping
                    # the rest would cut the paste short; waiting costs nothing.
                    await self._wait_until_writable(fd)
                except OSError:
                    return False
                else:
                    remaining = remaining[written:]
        return True

    async def _wait_until_writable(self, fd: int) -> None:
        loop = asyncio.get_running_loop()
        self._writable = ready = loop.create_future()
        loop.add_writer(fd, lambda: ready.done() or ready.set_result(None))
        try:
            await ready
        finally:
            self._writable = None
            if self._write_fd == fd:
                # Otherwise `_release` closed it, and the number may be someone else's by now.
                loop.remove_writer(fd)

    async def resize(self, columns: int, lines: int) -> bool:
        if self._write_fd is None or not resize_pty(self._write_fd, columns, lines):
            return False
        self.size = (columns, lines)
        return True

    def kill(self) -> None:
        if self._process.returncode is None:
            # The group, not just the shell: whatever it was running goes with it. Once the
            # program has been reaped its pid may be another program's, which must not get this.
            kill_posix_process(self._process.pid, fallback_to_process=True)
        self._release()

    async def close(self) -> None:
        self._release()
        with suppress(Exception):
            await self._process.wait()
        await asyncio.wait({self._slave_release})

    async def _release_slave_after_exit(self) -> None:
        with suppress(Exception):
            await self._process.wait()
        self._close_slave()

    def _close_slave(self) -> None:
        if (fd := self._slave_fd) is not None:
            self._slave_fd = None
            with suppress(OSError):
                os.close(fd)

    def _release(self) -> None:
        # Closing the transport ends a read in progress, which is how the owner of a killed
        # process finds out. There may be no loop left to do it on, if the app is shutting down.
        with suppress(RuntimeError):
            self._transport.close()
        with suppress(RuntimeError):
            self._slave_release.cancel()
        self._close_slave()
        if (fd := self._write_fd) is None:
            return
        self._write_fd = None
        if (ready := self._writable) is not None:
            with suppress(RuntimeError, OSError, ValueError):
                asyncio.get_running_loop().remove_writer(fd)
            if not ready.done():
                # Wake the write that was waiting, to find the descriptor gone.
                ready.set_result(None)
        with suppress(OSError):
            os.close(fd)


class _ConPtyProcess:
    repaints_after_resize = True

    def __init__(self, pty: WinPTYProtocol, size: tuple[int, int]) -> None:
        self._pty: WinPTYProtocol | None = pty
        self._pid: int | None = pty.pid
        self._output: asyncio.Queue[str] = asyncio.Queue()
        self._ended = False
        self._write_lock = asyncio.Lock()
        self.size: tuple[int, int] | None = size
        # One thread for the life of the program. A thread per read costs more than the read.
        threading.Thread(
            target=_pump_conpty_output,
            args=(pty, self._output, asyncio.get_running_loop()),
            daemon=True,
            name="chrys-winpty-reader",
        ).start()

    @classmethod
    def spawn(
        cls, argv: Sequence[str], *, env: Mapping[str, str], cwd: str, columns: int, lines: int
    ) -> _ConPtyProcess:
        pty = WINPTY_FACTORY(columns, lines)
        # CreateProcess takes the environment as one block: NUL after each entry, NUL at the end.
        environment = "\0".join(f"{name}={value}" for name, value in env.items()) + "\0\0"
        pty.spawn(subprocess.list2cmdline(argv), cwd=cwd, env=environment)
        return cls(pty, (columns, lines))

    async def read(self) -> str:
        if self._ended:
            return ""
        chunks = [await self._output.get()]
        with suppress(asyncio.QueueEmpty):
            while chunks[-1]:
                chunks.append(self._output.get_nowait())
        if not chunks[-1]:
            # The empty chunk is the pump's goodbye. What came before it is still to be shown.
            self._ended = True
        return "".join(chunks)

    async def write(self, data: str) -> bool:
        async with self._write_lock:
            if (pty := self._pty) is None:
                return False
            # A ConPTY is typed into in text. What is not text cannot be said to it at all.
            text = data if data.isascii() else data.encode("utf-8", "ignore").decode("utf-8")
            try:
                await asyncio.to_thread(pty.write, text)
            except _CONPTY_ERRORS:
                return False
        return True

    async def resize(self, columns: int, lines: int) -> bool:
        if (pty := self._pty) is None:
            return False
        try:
            # ConPTY rewraps its whole screen here, which is slow enough to be felt on the event loop.
            await asyncio.to_thread(pty.set_size, columns, lines)
        except _CONPTY_ERRORS:
            return False
        self.size = (columns, lines)
        return True

    def kill(self) -> None:
        if self._pid is not None:
            with suppress(OSError):
                os.kill(self._pid, signal.SIGTERM)
        self._release()
        # The pump may not have said goodbye yet, and a reader must not wait for it.
        self._output.put_nowait("")

    async def close(self) -> None:
        self._release()

    def _release(self) -> None:
        self._pty = None
        self._pid = None


def _pump_conpty_output(pty: WinPTYProtocol, output: asyncio.Queue[str], loop: asyncio.AbstractEventLoop) -> None:
    """Move a ConPTY's output to ``output``, a burst at a time, ending with an empty string.

    Runs in a thread of its own. The reads poll instead of blocking because the pipe outlives the
    program: the PTY object holds it open, so a blocking read would never return after the program
    exits, and nothing would get to ask whether it is still alive.
    """

    def deliver(text: str) -> None:
        # The loop may be gone already, if the app is shutting down.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(output.put_nowait, text)

    while pty.isalive():
        try:
            first = pty.read(blocking=False)
        except (EOFError, *_CONPTY_ERRORS):
            break
        if not first:
            sleep(_BURST_GAP)
            continue
        chunks = [first]
        deadline = monotonic() + _BURST_WINDOW
        paused = False
        while monotonic() < deadline and pty.isalive():
            try:
                chunk = pty.read(blocking=False)
            except Exception:
                break
            if chunk:
                chunks.append(chunk)
                paused = False
            elif paused:
                break
            else:
                # Nothing ready. One pause for more, and the burst is over if none comes.
                paused = True
                sleep(_BURST_GAP)
        deliver("".join(chunks))
    # What the program wrote on its way out is still in the pipe.
    with suppress(Exception):
        while last := pty.read(blocking=False):
            deliver(last)
    deliver("")
