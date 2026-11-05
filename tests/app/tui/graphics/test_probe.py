# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Asking the terminal what it is before Textual reads its input."""

from __future__ import annotations

import ctypes
import os
import sys
import threading
from collections import deque
from dataclasses import dataclass, field
from types import ModuleType
from typing import TYPE_CHECKING, Any

import pytest

from chrys.app.tui.graphics import probe
from chrys.app.tui.graphics.probe import InputRecord, KeyEventRecord, TerminalReport, key_text, parse_replies

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_DCS, _ST = "\x1bP", "\x1b\\"
_GHOSTTY = f"{_DCS}>|ghostty 1.4.0{_ST}\x1b[6;34;16t\x1b[?62;22;52c"
_GHOSTTY_NAME = f"{_DCS}>|ghostty 1.4.0{_ST}\x1b[?62;22;52c"
_GHOSTTY_CELL_SIZE = "\x1b[6;34;16t\x1b[?62;22;52c"
_WEZTERM_NAME = f"{_DCS}>|WezTerm 20240203-110809-5046fc22{_ST}\x1b[?65;4;6;18;22c"
_WEZTERM_CELL_SIZE = "\x1b[6;38;19t\x1b[?65;4;6;18;22c"
_WINDOWS_TERMINAL = "\x1b[?61;4;6;7;14;21;22;23;24;28;32;42;52c"


@pytest.mark.parametrize(
    ("replies", "expected"),
    [
        pytest.param(_GHOSTTY, TerminalReport("ghostty 1.4.0", frozenset({62, 22, 52}), (16, 34)), id="ghostty"),
        pytest.param(
            f"{_DCS}>|ghostty 1.4.1-main+0123abc{_ST}\x1b[?62;22;52c",
            TerminalReport("ghostty 1.4.1-main+0123abc", frozenset({62, 22, 52})),
            id="ghostty-dev-build",
        ),
        pytest.param(
            f"{_DCS}>|kitty(0.49.2){_ST}\x1b[6;34;16t\x1b[?62;52;c",
            TerminalReport("kitty(0.49.2)", frozenset({62, 52}), (16, 34)),
            id="kitty",
        ),
        pytest.param(
            f"{_DCS}>|iTerm2 3.7.6{_ST}\x1b[6;17;7t\x1b[?64;1;2;4;6;17;18;21;22;52c",
            TerminalReport("iTerm2 3.7.6", frozenset({64, 1, 2, 4, 6, 17, 18, 21, 22, 52}), (7, 17)),
            id="iterm2",
        ),
        pytest.param(
            f"{_DCS}>|WezTerm 20240203-110809-5046fc22{_ST}\x1b[6;38;19t\x1b[?65;4;6;18;22c",
            TerminalReport("WezTerm 20240203-110809-5046fc22", frozenset({65, 4, 6, 18, 22}), (19, 38)),
            id="wezterm",
        ),
        pytest.param(
            f"{_DCS}>|xterm.js(6.1.0-beta.304){_ST}\x1b[6;17;8t\x1b[?1;2c",
            TerminalReport("xterm.js(6.1.0-beta.304)", frozenset({1, 2}), (8, 17)),
            id="vscode",
        ),
        pytest.param(
            f"\x1b[6;20;10t{_WINDOWS_TERMINAL}",
            TerminalReport("", frozenset({61, 4, 6, 7, 14, 21, 22, 23, 24, 28, 32, 42, 52}), (10, 20)),
            id="windows-terminal",
        ),
        pytest.param(
            "\x1b[?61;6;7;21;22;23;24;28;32;42c",
            TerminalReport("", frozenset({61, 6, 7, 21, 22, 23, 24, 28, 32, 42})),
            id="windows-terminal-1.21",
        ),
        pytest.param(f"{_DCS}>|tmux 3.8{_ST}\x1b[?1;2;4c", TerminalReport("tmux 3.8", frozenset({1, 2, 4})), id="tmux"),
        pytest.param("\x1b[?1;2c", TerminalReport("", frozenset({1, 2})), id="apple-terminal"),
        pytest.param(
            f"ab{_DCS}>|ghostty 1.4.0{_ST}x\x1b[Ay\x1b[?62c",
            TerminalReport("ghostty 1.4.0", frozenset({62})),
            id="keys-typed-between",
        ),
        pytest.param(
            "\x1b[6;0;10t\x1b[6;20;9999t\x1b[?1c", TerminalReport("", frozenset({1})), id="cell-size-out-of-range"
        ),
    ],
)
def test_replies_parse_into_a_report(replies: str, expected: TerminalReport) -> None:
    assert parse_replies(replies) == expected


def test_replies_are_incomplete_until_the_device_attributes() -> None:
    assert parse_replies(_GHOSTTY.removesuffix("\x1b[?62;22;52c")) is None
    assert parse_replies(_GHOSTTY[:-1]) is None


class _Input:
    """Key events arriving at given times, read as the probe reads them, on a clock that never sleeps."""

    def __init__(self, events: list[tuple[float, str]]) -> None:
        self.now = 0.0
        self.events = deque(events)

    def clock(self) -> float:
        return self.now

    def read(self, timeout: float) -> str | None:
        if self.events and self.events[0][0] <= self.now + timeout:
            at, typed = self.events.popleft()
            self.now = max(self.now, at)
            return typed
        self.now += timeout
        return ""


def _typed(at: float, text: str) -> list[tuple[float, str]]:
    return [(at, char) for char in text]


def test_reading_stops_at_the_device_attributes_and_leaves_later_keys() -> None:
    keys = _Input([*_typed(0.01, _GHOSTTY), *_typed(0.02, "hi")])

    assert probe.read_report(keys.read, clock=keys.clock) == parse_replies(_GHOSTTY)
    assert "".join(typed for _, typed in keys.events) == "hi"


def test_a_terminal_that_answers_nothing_is_given_up_on_quickly() -> None:
    keys = _Input(_typed(probe.QUIET_SECONDS + 0.1, _GHOSTTY))

    assert probe.read_report(keys.read, clock=keys.clock) is None
    assert keys.now == pytest.approx(probe.QUIET_SECONDS)


def test_a_terminal_that_has_answered_is_waited_for() -> None:
    late = probe.REPLY_SECONDS - 0.1
    keys = _Input([*_typed(1.0, f"{_DCS}>|ghostty 1.4.0{_ST}"), *_typed(late, "\x1b[?62c")])

    assert probe.read_report(keys.read, clock=keys.clock) == TerminalReport("ghostty 1.4.0", frozenset({62}))
    assert keys.now == pytest.approx(late)


def test_keys_alone_do_not_extend_the_wait() -> None:
    keys = _Input([*_typed(1.0, "\x1b[A\x1b\\t"), *_typed(2.0, "\x1b[?62c")])

    assert probe.read_report(keys.read, clock=keys.clock) is None
    assert keys.now == pytest.approx(probe.QUIET_SECONDS)


def test_ended_input_gives_no_report() -> None:
    assert probe.read_report(lambda _timeout: None) is None


# POSIX: a pseudo-terminal stands in for the terminal Textual will run in.


class _Terminal:
    """The far end of a pty: answers each of ``answers``' queries in turn, once it has arrived."""

    def __init__(self, master: int, slave: int, answers: tuple[tuple[str, str], ...]) -> None:
        self.received = b""
        self.modes_while_asking: list[list[Any]] = []
        """The tty's mode as each query arrived."""
        self._master = master
        self._slave = slave
        self._answers = answers
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="fake-terminal", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import select
        import termios

        wanted = 0
        for queries, reply in self._answers:
            wanted += len(queries.encode())
            while len(self.received) < wanted:
                if self._stop.is_set():
                    return
                if select.select([self._master], [], [], 0.05)[0]:
                    self.received += os.read(self._master, 1024)
            self.modes_while_asking.append(termios.tcgetattr(self._slave))
            os.write(self._master, reply.encode())

    def join(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        assert not self._thread.is_alive()


def _mode(fd: int) -> list[object]:
    """The tty's mode, but for the flag BSD kernels set on each return to canonical mode."""
    import termios

    mode = termios.tcgetattr(fd)
    mode[3] &= ~getattr(termios, "PENDIN", 0)
    return mode


@dataclass
class _Pty:
    master: int
    slave: int
    foreground: bool = True
    terminals: list[_Terminal] = field(default_factory=list)

    def answer(self, *answers: tuple[str, str]) -> _Terminal:
        """Start the terminal's end, answering each query with its reply; keys typed go in a reply."""
        terminal = _Terminal(self.master, self.slave, answers)
        self.terminals.append(terminal)
        return terminal


@pytest.fixture
def terminal_pty(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Pty]:
    """A pty as stdin and stderr, in the foreground process group unless ``foreground`` is cleared."""
    if sys.platform == "win32":
        pytest.skip("POSIX pseudo-terminals")
    master, slave = os.openpty()
    pty = _Pty(master, slave)
    stream = os.fdopen(slave, "rb", buffering=0, closefd=False)
    monkeypatch.setattr(sys, "__stdin__", stream)
    monkeypatch.setattr(sys, "__stderr__", stream)
    shadow = ModuleType("os")
    shadow.__dict__.update(vars(os))
    group = os.getpgrp()
    shadow.tcgetpgrp = lambda _fd: group if pty.foreground else group + 1  # type: ignore[attr-defined]
    monkeypatch.setattr(probe, "os", shadow)
    try:
        yield pty
    finally:
        for terminal in pty.terminals:
            terminal.join()
        stream.close()
        os.close(slave)
        os.close(master)


def test_the_terminal_is_asked_and_its_mode_restored(terminal_pty: _Pty) -> None:
    import select
    import termios

    master, slave = terminal_pty.master, terminal_pty.slave
    flow_control = termios.tcgetattr(slave)
    flow_control[0] |= termios.IXON
    termios.tcsetattr(slave, termios.TCSANOW, flow_control)
    before = _mode(slave)
    terminal = terminal_pty.answer(
        (probe.QUERIES, _GHOSTTY_NAME), (probe.CELL_SIZE_QUERIES, f"{_GHOSTTY_CELL_SIZE}typed")
    )

    report = probe.query_terminal()
    terminal.join()

    assert report == parse_replies(_GHOSTTY)
    # The tty gives no cell size, so the terminal that named itself is asked for it.
    assert terminal.received == (probe.QUERIES + probe.CELL_SIZE_QUERIES).encode()
    # While asking: no echo, no line editing, no flow control (Ctrl+S would hold the queries back), and
    # Ctrl+C still interrupts.
    assert len(terminal.modes_while_asking) == 2
    for mode in terminal.modes_while_asking:
        iflag, lflag = mode[0], mode[3]
        assert not iflag & termios.IXON
        assert not lflag & (termios.ICANON | termios.ECHO)
        assert lflag & termios.ISIG
    # Nothing typed was echoed onto the screen.
    assert not select.select([master], [], [], 0)[0]
    assert _mode(slave) == before
    # The keys typed after the replies are left for Textual, which switches to raw mode as the probe does.
    raw = termios.tcgetattr(slave)
    raw[3] &= ~(termios.ICANON | termios.ECHO)
    raw[6][termios.VMIN] = raw[6][termios.VTIME] = 0
    termios.tcsetattr(slave, termios.TCSANOW, raw)
    assert select.select([slave], [], [], 1)[0]
    assert os.read(slave, 64) == b"typed"


def test_a_tty_that_gives_the_cell_size_is_asked_only_what_the_terminal_is(terminal_pty: _Pty) -> None:
    import fcntl
    import select
    import struct
    import termios

    fcntl.ioctl(terminal_pty.master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 800, 480))
    terminal = terminal_pty.answer((probe.QUERIES, _GHOSTTY_NAME))

    report = probe.query_terminal()
    terminal.join()

    assert report == TerminalReport("ghostty 1.4.0", frozenset({62, 22, 52}))
    assert probe.terminal_cell_pixels(terminal_pty.slave) == (10, 20)
    assert terminal.received == probe.QUERIES.encode()
    assert not select.select([terminal_pty.master], [], [], 0)[0]


def test_a_reply_left_unread_by_an_earlier_program_is_dropped(terminal_pty: _Pty) -> None:
    import select
    import termios

    quiet = termios.tcgetattr(terminal_pty.slave)
    quiet[3] &= ~termios.ECHO
    termios.tcsetattr(terminal_pty.slave, termios.TCSANOW, quiet)
    os.write(terminal_pty.master, b"\x1b[?1;2c")
    terminal = terminal_pty.answer(
        (probe.QUERIES, _GHOSTTY_NAME), (probe.CELL_SIZE_QUERIES, f"{_GHOSTTY_CELL_SIZE}typed")
    )

    report = probe.query_terminal()
    terminal.join()

    assert report == parse_replies(_GHOSTTY)
    # What is left for Textual is the keys typed after the replies, not the earlier program's reply.
    raw = termios.tcgetattr(terminal_pty.slave)
    raw[3] &= ~termios.ICANON
    raw[6][termios.VMIN] = raw[6][termios.VTIME] = 0
    termios.tcsetattr(terminal_pty.slave, termios.TCSANOW, raw)
    assert select.select([terminal_pty.slave], [], [], 1)[0]
    assert os.read(terminal_pty.slave, 64) == b"typed"


def test_a_silent_terminal_gives_no_report(monkeypatch: pytest.MonkeyPatch, terminal_pty: _Pty) -> None:
    monkeypatch.setattr(probe, "QUIET_SECONDS", 0.2)
    before = _mode(terminal_pty.slave)

    assert probe.query_terminal() is None
    assert _mode(terminal_pty.slave) == before


def test_a_background_process_does_not_ask(terminal_pty: _Pty) -> None:
    import select

    terminal_pty.foreground = False

    assert probe.query_terminal() is None
    assert not select.select([terminal_pty.master], [], [], 0)[0]


def test_input_that_is_not_a_terminal_is_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == "win32":
        pytest.skip("POSIX streams")
    read, write = os.pipe()
    try:
        with os.fdopen(read, "rb", buffering=0, closefd=False) as stream:
            monkeypatch.setattr(sys, "__stdin__", stream)
            assert probe.query_terminal() is None
    finally:
        os.close(read)
        os.close(write)


# Windows: a console whose input buffer holds the terminal's replies as key presses.

_INPUT, _OUTPUT = -10, -11


def _replies(answers: dict[str, str]) -> Callable[[str], str]:
    """A terminal answering each of ``answers``' queries with its reply, and others with nothing."""
    return lambda queries: answers.get(queries, "")


def _old_conpty(queries: str) -> str:
    """A ConPTY before Windows Terminal 1.22's: it answers device attributes at once, drops the
    terminal's XTVERSION reply and passes on the terminal's cell size after."""
    attributes = "\x1b[?1;0c" if "\x1b[c" in queries else ""
    return attributes + ("\x1b[6;20;10t" if "\x1b[16t" in queries else "")


class _Console:
    """Console handles with modes and an input queue that ``answer(queries)`` fills as each write is
    made; waiting for input advances its clock."""

    def __init__(
        self, answer: Callable[[str], str], *, input_mode: int | None = 0x1F7, output_mode: int | None = 0x3
    ) -> None:
        self.now = 0.0
        self.events: deque[str] = deque()
        self.answer = answer
        self.modes = {_INPUT: input_mode, _OUTPUT: output_mode}
        self.modes_while_asking: dict[int, int | None] = {}
        self.refuse: set[int] = set()
        self.written: list[str] = []

    def clock(self) -> float:
        return self.now

    def write(self, text: str) -> None:
        self.modes_while_asking = dict(self.modes)
        self.written.append(text)
        self.events.extend(self.answer(text))

    def mode(self, handle: int) -> int | None:
        return self.modes[handle]

    def set_mode(self, handle: int, mode: int) -> bool:
        if handle in self.refuse:
            return False
        self.modes[handle] = mode
        return True

    def flush(self, handle: int) -> None:
        assert handle == _INPUT
        self.events.clear()

    def wait(self, handle: int, timeout: float) -> bool | None:
        assert handle == _INPUT
        if self.events:
            return True
        self.now += timeout
        return False

    def read_key(self, handle: int) -> str | None:
        assert handle == _INPUT
        return self.events.popleft()


def test_the_console_is_asked_and_its_modes_restored() -> None:
    console = _Console(_replies({probe.QUERIES: f"{_WINDOWS_TERMINAL}typed"}))

    report = probe.query_console(console, console.write, clock=console.clock)

    assert report == parse_replies(_WINDOWS_TERMINAL)
    assert console.written == [probe.QUERIES]
    # Replies arrive as VT input; the queries reach the terminal only through VT processing.
    assert console.modes_while_asking == {_INPUT: 0x200, _OUTPUT: 0x3 | 0x4}
    assert console.modes == {_INPUT: 0x1F7, _OUTPUT: 0x3}
    assert "".join(console.events) == "typed"


def test_a_console_terminal_that_named_itself_is_asked_its_cell_size() -> None:
    console = _Console(_replies({probe.QUERIES: _WEZTERM_NAME, probe.CELL_SIZE_QUERIES: f"{_WEZTERM_CELL_SIZE}typed"}))

    report = probe.query_console(console, console.write, clock=console.clock)

    assert report == TerminalReport("WezTerm 20240203-110809-5046fc22", frozenset({65, 4, 6, 18, 22}), (19, 38))
    assert console.written == [probe.QUERIES, probe.CELL_SIZE_QUERIES]
    assert console.modes == {_INPUT: 0x1F7, _OUTPUT: 0x3}
    assert "".join(console.events) == "typed"


def test_an_unanswered_cell_size_keeps_what_the_terminal_said() -> None:
    console = _Console(_replies({probe.QUERIES: _WEZTERM_NAME}))

    report = probe.query_console(console, console.write, clock=console.clock)

    assert report == TerminalReport("WezTerm 20240203-110809-5046fc22", frozenset({65, 4, 6, 18, 22}))
    assert console.written == [probe.QUERIES, probe.CELL_SIZE_QUERIES]
    assert console.now == pytest.approx(probe.QUIET_SECONDS)


def test_an_old_conpty_leaves_no_late_reply_for_the_app() -> None:
    console = _Console(_old_conpty)

    report = probe.query_console(console, console.write, clock=console.clock)

    assert report == TerminalReport("", frozenset({1, 0}))
    assert console.written == [probe.QUERIES]
    assert not console.events


def test_a_reply_left_unread_in_the_console_is_dropped() -> None:
    console = _Console(_replies({probe.QUERIES: f"{_WINDOWS_TERMINAL}typed"}))
    console.events.extend("\x1b[?1;0c")

    report = probe.query_console(console, console.write, clock=console.clock)

    assert report == parse_replies(_WINDOWS_TERMINAL)
    assert "".join(console.events) == "typed"


def test_a_silent_console_gives_no_report() -> None:
    console = _Console(_replies({}))

    assert probe.query_console(console, console.write, clock=console.clock) is None
    assert console.now == pytest.approx(probe.QUIET_SECONDS)
    assert console.modes == {_INPUT: 0x1F7, _OUTPUT: 0x3}


@pytest.mark.parametrize("handle", [_INPUT, _OUTPUT])
def test_a_console_that_refuses_a_mode_is_not_asked(handle: int) -> None:
    console = _Console(_replies({probe.QUERIES: _WINDOWS_TERMINAL}))
    console.refuse.add(handle)

    assert probe.query_console(console, console.write, clock=console.clock) is None
    assert console.written == []
    assert console.modes == {_INPUT: 0x1F7, _OUTPUT: 0x3}


@pytest.mark.parametrize("handle", [_INPUT, _OUTPUT])
def test_redirected_streams_are_not_asked(handle: int) -> None:
    console = _Console(_replies({probe.QUERIES: _WINDOWS_TERMINAL}))
    console.modes[handle] = None

    assert probe.query_console(console, console.write, clock=console.clock) is None
    assert console.written == []


@pytest.mark.skipif(sys.platform != "win32", reason="kernel32")
def test_kernel32_has_the_console_calls() -> None:
    console = probe._Kernel32Console.load()

    assert console is not None
    for handle in (_INPUT, _OUTPUT):
        mode = console.mode(handle)
        assert mode is None or isinstance(mode, int)


def test_input_records_have_the_windows_layout() -> None:
    assert ctypes.sizeof(KeyEventRecord) == 16
    assert KeyEventRecord.UnicodeChar.offset == 10
    assert ctypes.sizeof(InputRecord) == 20
    assert InputRecord.Event.offset == 4


@pytest.mark.parametrize(
    ("event_type", "key_down", "unit", "expected"),
    [
        pytest.param(1, 1, ord("c"), "c", id="key-press"),
        pytest.param(1, 0, ord("c"), "", id="key-release"),
        pytest.param(1, 1, 0, "", id="key-without-text"),
        pytest.param(2, 1, ord("c"), "", id="mouse"),
    ],
)
def test_only_key_presses_type_text(event_type: int, key_down: int, unit: int, expected: str) -> None:
    record = InputRecord()
    record.EventType = event_type
    record.Event.KeyEvent.bKeyDown = key_down
    record.Event.KeyEvent.UnicodeChar = unit

    assert key_text(record) == expected
