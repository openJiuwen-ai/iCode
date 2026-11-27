# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""`PtyProcess`: real programs on a POSIX pseudo-terminal, and a scripted ConPTY for the Windows half."""

from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import suppress
from pathlib import Path
from typing import Protocol

import psutil
import pytest

import chrys.app.tui.terminal.pty_process as pty_process_module
from chrys.app.tui.terminal.pty_process import PtyProcess, spawn_pty_process
from chrys.foundation.platform import get_platform
from tests.support.pty_masters import linux_style_master
from tests.support.waiting import wait_for

_READ_DEADLINE = 15.0

posix_only = pytest.mark.skipif(get_platform().is_windows, reason="POSIX pseudo-terminals")


async def _read_through(process: PtyProcess, marker: str) -> str:
    """Everything the program writes up to and including ``marker``."""
    text = ""
    async with asyncio.timeout(_READ_DEADLINE):
        while marker not in text:
            chunk = await process.read()
            assert chunk, f"program ended before writing {marker!r}; it wrote {text!r}"
            text += chunk
    return text


# -- POSIX ---------------------------------------------------------------------------------------


class RunPython(Protocol):
    async def __call__(self, code: str, columns: int = 80, lines: int = 24) -> PtyProcess: ...


@pytest.fixture
async def run_python(tmp_path: Path) -> AsyncIterator[RunPython]:
    """Start Python programs on pseudo-terminals, and end whatever is left of them afterwards."""
    started: list[PtyProcess] = []

    async def run(code: str, columns: int = 80, lines: int = 24) -> PtyProcess:
        env = {**os.environ, "CHRYS_PTY_TEST": "passed along"}
        process = await spawn_pty_process(
            [sys.executable, "-c", code], env=env, cwd=str(tmp_path), columns=columns, lines=lines
        )
        started.append(process)
        return process

    yield run
    for process in started:
        process.kill()
        await process.close()


@posix_only
async def test_posix_program_starts_at_the_given_size_in_the_given_place(run_python: RunPython, tmp_path: Path) -> None:
    process = await run_python(
        "import os; size = os.get_terminal_size();"
        "print(f'{size.columns}x{size.lines} {os.path.realpath(os.getcwd())} {os.environ[\"CHRYS_PTY_TEST\"]}', end='|')",
        91,
        17,
    )

    output = await _read_through(process, "|")

    assert output == f"91x17 {os.path.realpath(tmp_path)} passed along|"
    assert process.size == (91, 17)
    assert process.repaints_after_resize is False


@posix_only
async def test_posix_character_split_between_two_reads_is_decoded_whole(
    run_python: RunPython,
) -> None:
    process = await run_python(
        "import os, tty\ntty.setraw(0)\nos.write(1, b'A\\xe4\\xb8')\nos.read(0, 1)\nos.write(1, b'\\xad|')\n"
    )

    # The program waits for a key before it sends the character's last byte.
    assert await _read_through(process, "A") == "A"
    assert await process.write("x") is True
    assert await _read_through(process, "|") == "中|"

    async with asyncio.timeout(_READ_DEADLINE):
        assert await process.read() == ""
        assert await process.read() == ""


@posix_only
async def test_posix_bytes_that_are_not_utf8_become_replacement_characters(
    run_python: RunPython,
) -> None:
    process = await run_python("import os; os.write(1, b'ok\\xffend|')")

    assert await _read_through(process, "|") == "ok\ufffdend|"


def _descriptors_open_on(terminal: str) -> list[int]:
    """Which of this process's descriptors are open on the terminal of that name."""
    found = []
    for name in os.listdir("/dev/fd"):
        # One of them was the listing's own, and most are not terminals at all.
        with suppress(OSError):
            if os.ttyname(int(name)) == terminal:
                found.append(int(name))
    return found


def _is_open(fd: int) -> bool:
    try:
        os.get_inheritable(fd)
    except OSError:
        return False
    return True


@posix_only
async def test_posix_terminal_stays_open_on_our_side_until_the_program_is_gone(run_python: RunPython) -> None:
    # macOS throws away what a terminal still holds once its last slave closes, and on a busy loop
    # that was the last words of a program that had already exited. With a slave open on our side,
    # the program's exit waits for them to be read.
    process = await run_python(
        "import os, tty\ntty.setraw(0)\nos.write(1, f'{os.ttyname(1)}|'.encode())\nos.read(0, 1)\n"
    )
    terminal = (await _read_through(process, "|")).removesuffix("|")

    (ours,) = _descriptors_open_on(terminal)

    assert await process.write("x") is True
    async with asyncio.timeout(_READ_DEADLINE):
        while await process.read():
            pass
    # Not for longer than that: on Linux the master says nothing of a program that ended until then.
    # Asked of the descriptor, since macOS no longer names the terminal of one whose program is gone.
    await wait_for(lambda: not _is_open(ours), description="our slave to be closed")


@posix_only
async def test_posix_last_words_outlive_a_master_that_failed_while_nobody_was_reading(
    run_python: RunPython, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The owner is busy with what it read before when the program ends, and Linux reports that end as
    # an I/O error on the master: which a `StreamReader` raises ahead of what it had already read.
    master, master_ended = linux_style_master()
    monkeypatch.setattr(pty_process_module, "PtyOutputProtocol", master)
    process = await run_python("print('last words', end='|')")

    await wait_for(master_ended.is_set, description="the master to end", timeout=_READ_DEADLINE)

    assert await _read_through(process, "|") == "last words|"
    async with asyncio.timeout(_READ_DEADLINE):
        assert await process.read() == ""


@posix_only
async def test_posix_resize_is_seen_by_the_program(run_python: RunPython) -> None:
    process = await run_python(
        "import os, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'R')\n"
        "os.read(0, 1)\n"
        "size = os.get_terminal_size()\n"
        "os.write(1, f'{size.columns}x{size.lines}|'.encode())\n"
    )
    await _read_through(process, "R")

    assert await process.resize(100, 40) is True
    assert process.size == (100, 40)
    await process.write("x")

    assert await _read_through(process, "|") == "100x40|"


@posix_only
async def test_posix_byte_that_is_not_text_is_typed_as_that_byte(run_python: RunPython) -> None:
    """A pointer report in xterm's original format counts columns in bytes, past 127 too."""
    process = await run_python(
        "import os, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'ready|')\n"
        "typed = b''\n"
        "while len(typed) < 11:\n"
        "    typed += os.read(0, 11 - len(typed))\n"
        "os.write(1, typed.hex().encode() + b'!')\n"
    )
    await _read_through(process, "ready|")

    # A press at column 101, then the same number as a character, then half a pair that is nothing.
    assert await process.write("\x1b[M \udc85!" + "\x85" + "\ud83d" + "é.") is True

    assert await _read_through(process, "!") == b"\x1b[M \x85!\xc2\x85\xc3\xa9.".hex() + "!"


@posix_only
async def test_posix_write_larger_than_the_terminal_takes_at_once_arrives_whole(
    run_python: RunPython,
) -> None:
    payload = "".join(f"{number:06d} žluťoučký 中文\n" for number in range(12_000))
    data = payload.encode()
    assert len(data) > 300_000
    process = await run_python(
        "import hashlib, os, tty\n"
        "tty.setraw(0)\n"
        "os.write(1, b'ready|')\n"
        f"remaining = {len(data)}\n"
        "digest = hashlib.sha256()\n"
        "while remaining:\n"
        "    chunk = os.read(0, min(65536, remaining))\n"
        "    digest.update(chunk)\n"
        "    remaining -= len(chunk)\n"
        "os.write(1, digest.hexdigest().encode() + b'!')\n"
    )
    await _read_through(process, "ready|")

    async with asyncio.timeout(_READ_DEADLINE):
        assert await process.write(payload) is True

    assert await _read_through(process, "!") == f"{hashlib.sha256(data).hexdigest()}!"


@posix_only
async def test_posix_kill_ends_the_program_and_a_read_in_progress(
    run_python: RunPython,
) -> None:
    process = await run_python(
        "import os, signal\n"
        "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "os.write(1, f'{os.getpid()}|'.encode())\n"
        "signal.pause()\n"
    )
    child = psutil.Process(int((await _read_through(process, "|")).removesuffix("|")))
    reading = asyncio.create_task(process.read())
    await asyncio.sleep(0)
    assert not reading.done()

    process.kill()

    async with asyncio.timeout(_READ_DEADLINE):
        assert await reading == ""
        await process.close()
    # Reaped, not merely dead: `is_running` is false for a pid that is gone or is someone else's.
    if os.environ.get("CHRYS_TEST_SANDBOX") == "1":
        # Under the sandbox the broker is the program's parent and reaps it, an instant after this process's child.
        await wait_for(lambda: not child.is_running(), description="the broker reaped the program")
    assert not child.is_running()
    assert await process.write("x") is False
    assert await process.resize(100, 30) is False
    assert process.size == (80, 24)


@posix_only
async def test_posix_kill_after_the_program_was_reaped_signals_nobody(
    run_python: RunPython, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = await run_python("print('done', end='|')")
    await _read_through(process, "|")
    async with asyncio.timeout(_READ_DEADLINE):
        await process.close()
    signalled: list[int] = []

    def record_kill(pid: int, *, fallback_to_process: bool) -> None:
        del fallback_to_process
        signalled.append(pid)

    monkeypatch.setattr(pty_process_module, "kill_posix_process", record_kill)

    process.kill()

    # By now the pid may belong to another program.
    assert signalled == []


@posix_only
async def test_posix_close_while_a_write_waits_for_room_ends_the_write(
    run_python: RunPython,
) -> None:
    # The program never reads, so the terminal's input queue fills up and the write has to wait.
    process = await run_python("import os, signal, tty\ntty.setraw(0)\nos.write(1, b'up|')\nsignal.pause()\n")
    await _read_through(process, "up|")
    writing = asyncio.create_task(process.write("x" * 2_000_000))
    await asyncio.sleep(0)

    process.kill()

    async with asyncio.timeout(_READ_DEADLINE):
        assert await writing is False
        await process.close()


@posix_only
async def test_posix_program_that_cannot_be_started_raises_and_leaks_no_descriptors(tmp_path: Path) -> None:
    def open_descriptors() -> int:
        return len(os.listdir("/dev/fd"))

    async def spawn_missing() -> None:
        with pytest.raises(OSError):
            await spawn_pty_process(
                [str(tmp_path / "no-such-program")], env=dict(os.environ), cwd=str(tmp_path), columns=80, lines=24
            )

    # Once first, so that whatever asyncio opens for itself on first use is already open.
    await spawn_missing()
    before = open_descriptors()

    await spawn_missing()

    assert open_descriptors() == before


# -- ConPTY --------------------------------------------------------------------------------------


class FakeWinPtyError(Exception):
    pass


class FakeWinPty:
    """pywinpty's PTY as the process sees it, with the program's side in the test's hands."""

    pid = 424242

    def __init__(self, cols: int, rows: int) -> None:
        self.initial_size = (cols, rows)
        self.spawned: tuple[str, str, str] | None = None
        self.sizes: list[tuple[int, int]] = []
        self.written: list[str] = []
        self.write_error: BaseException | None = None
        self.size_error: BaseException | None = None
        self.read_error: BaseException | None = None
        self.callers: list[str] = []
        self._chunks: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._alive = threading.Event()
        self._alive.set()

    # The program's side.

    def emit(self, *chunks: str) -> None:
        for chunk in chunks:
            self._chunks.put(chunk)

    def exit(self) -> None:
        self._alive.clear()

    # pywinpty's side.

    def isalive(self) -> bool:
        return self._alive.is_set()

    def read(self, *, blocking: bool) -> str:
        assert blocking is False
        if self.read_error is not None:
            raise self.read_error
        try:
            return self._chunks.get_nowait()
        except queue.Empty:
            return ""

    def set_size(self, cols: int, rows: int) -> None:
        self.callers.append(threading.current_thread().name)
        if self.size_error is not None:
            raise self.size_error
        self.sizes.append((cols, rows))

    def spawn(self, command: str, *, cwd: str, env: str) -> None:
        self.spawned = (command, cwd, env)

    def write(self, text: str) -> None:
        self.callers.append(threading.current_thread().name)
        if self.write_error is not None:
            raise self.write_error
        self.written.append(text)


@pytest.fixture
def conpty(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[FakeWinPty]]:
    """Route `spawn_pty_process` to a scripted ConPTY; every reader thread is over by teardown."""
    created: list[FakeWinPty] = []

    def factory(cols: int, rows: int) -> FakeWinPty:
        pty = FakeWinPty(cols, rows)
        created.append(pty)
        return pty

    before = set(threading.enumerate())
    monkeypatch.setattr(pty_process_module, "IS_WINDOWS", True)
    monkeypatch.setattr(pty_process_module, "WINPTY_FACTORY", factory)
    monkeypatch.setattr(pty_process_module, "_CONPTY_ERRORS", (OSError, RuntimeError, FakeWinPtyError))
    yield created
    for pty in created:
        pty.exit()
    for thread in set(threading.enumerate()) - before:
        if thread.name == "chrys-winpty-reader":
            thread.join(timeout=10)
            assert not thread.is_alive()


async def _spawn_conpty(argv: list[str] | None = None, env: dict[str, str] | None = None) -> PtyProcess:
    return await spawn_pty_process(argv or ["pwsh"], env=env or {"A": "1"}, cwd="C:\\work", columns=120, lines=40)


async def test_conpty_spawn_passes_a_command_line_and_an_environment_block(conpty: list[FakeWinPty]) -> None:
    process = await _spawn_conpty(
        ["C:\\Program Files\\PowerShell\\pwsh.exe", "-NoExit", "-Command", 'say "hi"'],
        {"PATH": "C:\\bin", "TERM": "xterm-256color"},
    )

    [pty] = conpty
    assert pty.initial_size == (120, 40)
    assert pty.spawned == (
        subprocess.list2cmdline(["C:\\Program Files\\PowerShell\\pwsh.exe", "-NoExit", "-Command", 'say "hi"']),
        "C:\\work",
        "PATH=C:\\bin\0TERM=xterm-256color\0\0",
    )
    assert pty.spawned[0].startswith('"C:\\Program Files\\PowerShell\\pwsh.exe" -NoExit')
    assert process.size == (120, 40)
    assert process.repaints_after_resize is True


async def test_conpty_output_arrives_in_order_and_ends_with_the_program(conpty: list[FakeWinPty]) -> None:
    process = await _spawn_conpty()
    [pty] = conpty

    pty.emit("one ", "two ", "three|")
    assert await _read_through(process, "|") == "one two three|"

    pty.emit("bye")
    pty.exit()
    async with asyncio.timeout(_READ_DEADLINE):
        rest = ""
        while chunk := await process.read():
            rest += chunk
        # What the program wrote on its way out is not lost, and the end stays the end.
        assert rest == "bye"
        assert await process.read() == ""
        assert await process.read() == ""


async def test_conpty_read_failure_ends_the_output(conpty: list[FakeWinPty]) -> None:
    process = await _spawn_conpty()
    conpty[0].read_error = FakeWinPtyError("pipe closed")

    async with asyncio.timeout(_READ_DEADLINE):
        assert await process.read() == ""


async def test_conpty_write_and_resize_happen_off_the_event_loop(conpty: list[FakeWinPty]) -> None:
    process = await _spawn_conpty()
    [pty] = conpty

    assert await process.write("dir\r") is True
    assert await process.resize(100, 30) is True

    assert pty.written == ["dir\r"]
    assert pty.sizes == [(100, 30)]
    assert process.size == (100, 30)
    assert len(pty.callers) == 2
    assert threading.current_thread().name not in pty.callers


async def test_conpty_is_typed_into_in_text_only(conpty: list[FakeWinPty]) -> None:
    process = await _spawn_conpty()

    assert await process.write("dir é\udc85\ud83d中\r") is True

    assert conpty[0].written == ["dir é中\r"]


@pytest.mark.parametrize("error", [FakeWinPtyError("closed"), OSError("closed"), RuntimeError("closed")])
async def test_conpty_errors_from_a_closed_terminal_are_answers_not_exceptions(
    conpty: list[FakeWinPty], error: BaseException
) -> None:
    process = await _spawn_conpty()
    conpty[0].write_error = conpty[0].size_error = error

    assert await process.write("x") is False
    assert await process.resize(100, 30) is False
    assert process.size == (120, 40)


async def test_conpty_kill_signals_the_program_and_ends_a_read_in_progress(
    conpty: list[FakeWinPty], monkeypatch: pytest.MonkeyPatch
) -> None:
    signalled: list[tuple[int, int]] = []

    def record_kill(pid: int, signal_number: int) -> None:
        signalled.append((pid, signal_number))

    monkeypatch.setattr(pty_process_module.os, "kill", record_kill)
    process = await _spawn_conpty()
    reading = asyncio.create_task(process.read())
    await asyncio.sleep(0)
    assert not reading.done()

    process.kill()
    process.kill()

    async with asyncio.timeout(_READ_DEADLINE):
        assert await reading == ""
        assert await process.read() == ""
    assert signalled == [(FakeWinPty.pid, pty_process_module.signal.SIGTERM)]
    assert await process.write("x") is False
    assert await process.resize(100, 30) is False
    assert conpty[0].written == []
    await process.close()


async def test_conpty_that_cannot_be_created_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_factory(cols: int, rows: int) -> FakeWinPty:
        raise RuntimeError(f"no ConPTY of {cols}x{rows}")

    monkeypatch.setattr(pty_process_module, "IS_WINDOWS", True)
    monkeypatch.setattr(pty_process_module, "WINPTY_FACTORY", broken_factory)

    with pytest.raises(RuntimeError, match="no ConPTY of 120x40"):
        await _spawn_conpty()


async def test_conpty_reader_thread_ends_with_the_program(conpty: list[FakeWinPty]) -> None:
    before = {thread for thread in threading.enumerate() if thread.name == "chrys-winpty-reader"}
    await _spawn_conpty()
    [reader] = {thread for thread in threading.enumerate() if thread.name == "chrys-winpty-reader"} - before
    assert reader.daemon

    conpty[0].exit()

    await wait_for(lambda: not reader.is_alive(), description="reader thread ended")
