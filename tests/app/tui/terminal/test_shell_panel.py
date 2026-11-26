# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The shell panel: when it starts, restarts and resizes its shell, and what it tells its screen.

Wherever Textual's message flow or geometry is the contract, the panel runs in a real app around a
stand-in for the shell process. A decision that depends on nothing but the panel's own state is
asked of the panel directly.
"""

from __future__ import annotations

import asyncio
import base64
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest
from textual import on
from textual.app import App, ComposeResult
from textual.message import Message
from textual.notifications import SeverityLevel

import chrys.app.tui.terminal.panel as panel_module
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.support.gc_freeze import GcFreezeBlockReason
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.terminal.pty_process import PtyProcess
from chrys.app.tui.terminal.shell import Shell, ShellFinished
from chrys.app.tui.terminal.widget import Terminal
from chrys.foundation.i18n import Localizer, MessageRef
from chrys.foundation.platform import PlatformInfo, ShellInfo, get_platform
from tests.support.tui_helpers import delay_resize_dispatch, resize_when_settled, rich_plain
from tests.support.waiting import wait_for


class FakeShell:
    """Stands in for one shell process: records what the panel asks of it."""

    def __init__(
        self,
        terminal: Terminal,
        working_directory: str,
        shell_command: str,
        on_error: Callable[[MessageRef], None] | None,
        *,
        repaints_after_resize: bool,
        ready_at_start: bool,
    ) -> None:
        self.terminal = terminal
        self.working_directory = working_directory
        self.shell_command = shell_command
        self.on_error = on_error
        self.is_finished = False
        self.pty_size: tuple[int, int] | None = None
        self.ready = asyncio.Event()
        self.ready_waiters = 0
        self.close_gate: asyncio.Event | None = None
        self.starts = 0
        self.sent: list[tuple[str, int, int]] = []
        self.resizes: list[tuple[int, int]] = []
        self.interrupts = 0
        self.terminations = 0
        self.closes = 0
        self._repaints_after_resize = repaints_after_resize
        self._ready_at_start = ready_at_start

    def start(self) -> None:
        self.starts += 1
        self.terminal.host_repaints = self._repaints_after_resize
        if self._ready_at_start:
            self.become_ready()

    def become_ready(self) -> None:
        """The process exists now, at the size the terminal had when it was started."""
        self.pty_size = (self.terminal.width, self.terminal.height)
        self.ready.set()

    def exit(self) -> None:
        """The program ended by itself, which a shell announces through its terminal."""
        self._end()
        self.terminal.post_message(ShellFinished())

    async def wait_for_ready(self) -> None:
        self.ready_waiters += 1
        await self.ready.wait()

    async def send(self, command: str, width: int, height: int) -> None:
        self.sent.append((command, width, height))

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def resize(self, width: int, height: int) -> None:
        self.resizes.append((width, height))
        self.pty_size = (width, height)

    def terminate(self) -> None:
        self.terminations += 1
        self._end()

    async def close(self) -> None:
        self.closes += 1
        self._end()
        if self.close_gate is not None:
            await self.close_gate.wait()

    def _end(self) -> None:
        self.is_finished = True
        self.pty_size = None
        self.ready.set()


class ShellFactory:
    """Stands in for the `Shell` class: makes the panel's shells and keeps them for the test."""

    def __init__(self) -> None:
        self.shells: list[FakeShell] = []
        self.repaints_after_resize = False
        self.ready_at_start = True

    def __call__(
        self,
        terminal: Terminal,
        working_directory: str,
        shell_command: str = "",
        on_error: Callable[[MessageRef], None] | None = None,
    ) -> FakeShell:
        shell = FakeShell(
            terminal,
            working_directory,
            shell_command,
            on_error,
            repaints_after_resize=self.repaints_after_resize,
            ready_at_start=self.ready_at_start,
        )
        self.shells.append(shell)
        return shell

    @property
    def only(self) -> FakeShell:
        assert len(self.shells) == 1
        return self.shells[0]


@pytest.fixture
def shells(monkeypatch: pytest.MonkeyPatch) -> ShellFactory:
    factory = ShellFactory()
    monkeypatch.setattr(panel_module, "Shell", factory)
    return factory


@dataclass(frozen=True)
class Notified:
    message: str
    severity: SeverityLevel
    markup: bool


class PanelApp(App[None]):
    """One shell panel filling the screen, under an app that keeps what reaches it."""

    def __init__(self, working_directory: str | None = None, *, locale: str | None = None) -> None:
        super().__init__()
        if locale is not None:
            self.locale_controller = LocaleController(localizer=Localizer(locale))
        self.panel = ShellPanel(working_directory=working_directory)
        self.panel_messages: list[Message] = []
        self.leaked: list[Message] = []
        self.notifications: list[Notified] = []

    def compose(self) -> ComposeResult:
        yield self.panel

    @property
    def terminal(self) -> Terminal:
        return self.panel.query_one(Terminal)

    @on(ShellPanel.DirectoryChanged)
    @on(ShellPanel.CommandExecuted)
    @on(ShellPanel.Exited)
    def _record(self, message: Message) -> None:
        self.panel_messages.append(message)

    @on(Terminal.DirectoryChanged)
    @on(Terminal.CommandSubmitted)
    @on(Terminal.SizeChanged)
    @on(ShellFinished)
    def _record_leak(self, message: Message) -> None:
        self.leaked.append(message)

    def messages_of[MessageT: Message](self, kind: type[MessageT]) -> list[MessageT]:
        return [message for message in self.panel_messages if isinstance(message, kind)]

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        self.notifications.append(Notified(message, severity, markup))
        super().notify(message, title=title, severity=severity, timeout=timeout, markup=markup)


class FakeTimer:
    def __init__(self) -> None:
        self.stops = 0

    def stop(self) -> None:
        self.stops += 1


class TimerRecorder:
    """Stands in for `Widget.set_timer` on a panel that is not running."""

    def __init__(self) -> None:
        self.requests: list[tuple[float, Callable[[], Awaitable[None]]]] = []
        self.timers: list[FakeTimer] = []

    def __call__(self, delay: float, callback: Callable[[], Awaitable[None]]) -> FakeTimer:
        self.requests.append((delay, callback))
        self.timers.append(FakeTimer())
        return self.timers[-1]


def _report(code: int, text: str) -> str:
    """What a shell's hooks print: 2025 carries the directory, 2026 the command line."""
    payload = base64.b64encode(text.encode()).decode()
    return f"\x1b]{code};b64:{payload}\x1b\\"


def _border_text(label: str | None) -> str:
    """The panel's title or subtitle as it reads, markup or not."""
    assert label is not None
    return rich_plain(label)


def _screen(terminal: Terminal) -> list[str]:
    return [text for row in terminal.emulator.buffer.rows if (text := row.text.rstrip())]


def _visible_size(terminal: Terminal) -> tuple[int, int]:
    width, height = terminal.scrollable_content_region.size
    return width, height


def _idle_shell(terminal: Terminal, *, pty_size: tuple[int, int] | None) -> FakeShell:
    shell = FakeShell(terminal, "", "", None, repaints_after_resize=False, ready_at_start=True)
    shell.pty_size = pty_size
    return shell


def _panel_with_shell(terminal: Terminal, *, pty_size: tuple[int, int] | None) -> tuple[ShellPanel, FakeShell]:
    panel = ShellPanel()
    shell = _idle_shell(terminal, pty_size=pty_size)
    panel._terminal = terminal
    panel._shell = cast("Shell", shell)
    return panel, shell


async def _shown(app: PanelApp, shells: ShellFactory) -> FakeShell:
    """Show the panel and wait until its newest shell knows the size now on display."""
    app.panel.show()
    shell = shells.shells[-1]
    terminal = app.terminal
    await wait_for(
        lambda: shell.pty_size == _visible_size(terminal) == (terminal.width, terminal.height),
        description="the shell to be told the visible size",
    )
    return shell


async def _panel_is_idle(panel: ShellPanel) -> None:
    """Wait until the panel has handled everything queued for it before this call."""
    handled = asyncio.Event()
    assert panel.call_later(handled.set)
    await wait_for(handled.is_set, description="the panel to work through its queue")


# -- starting and keeping the shell ----------------------------------------------------------------


async def test_terminal_keeps_its_scrollbar_gutter_open_inside_the_panel(shells: ShellFactory) -> None:
    assert "scrollbar-gutter: stable;" in Terminal.DEFAULT_CSS

    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        await _shown(app, shells)
        terminal = app.terminal

        assert terminal.styles.scrollbar_gutter == "stable"
        assert terminal.max_scroll_y == 0
        assert terminal.scrollable_content_region.width == terminal.content_region.width - 1


async def test_panel_has_no_shell_until_shown_and_keeps_it_while_hidden(shells: ShellFactory, tmp_path: Path) -> None:
    workspace = str(tmp_path / "workspace")
    app = PanelApp(workspace)
    async with app.run_test(size=(60, 20)):
        panel = app.panel
        assert not panel.is_visible
        assert shells.shells == []
        assert not panel.has_live_shell
        assert panel.gc_freeze_block_reason() is None

        shell = await _shown(app, shells)

        assert panel.is_visible
        assert panel.gc_freeze_block_reason() is GcFreezeBlockReason.SHELL_VISIBLE
        assert shell.terminal is app.terminal
        assert shell.working_directory == workspace
        assert shell.shell_command == get_platform().shell.path
        assert shell.starts == 1
        assert _border_text(panel.border_title) == panel.shell_name == get_platform().shell.name
        assert _border_text(panel.border_subtitle) == workspace

        panel.hide()

        assert not panel.is_visible
        assert panel.gc_freeze_block_reason() is None
        assert panel.has_live_shell
        assert (shell.terminations, shell.closes, shell.is_finished) == (0, 0, False)

        await _shown(app, shells)

        assert shells.only is shell
        assert shell.starts == 1


async def test_panel_without_a_working_directory_starts_where_the_process_is(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        shell = await _shown(app, shells)

        assert shell.working_directory == os.getcwd()
        assert _border_text(app.panel.border_subtitle) == os.getcwd()


# -- what the shell reports ------------------------------------------------------------------------


async def test_directory_reports_are_passed_on_once_per_change(shells: ShellFactory, tmp_path: Path) -> None:
    start, first, second = (str(tmp_path / name) for name in ("start", "with space; 中文", "second"))
    app = PanelApp(start)
    async with app.run_test(size=(60, 20)):
        await _shown(app, shells)
        app.leaked.clear()

        # A shell reports ahead of every prompt, whether or not it moved.
        await app.terminal.write(_report(2025, start) + _report(2025, first) + _report(2025, first))
        await app.terminal.write(_report(2025, second))
        await wait_for(
            lambda: [message.path for message in app.messages_of(ShellPanel.DirectoryChanged)][-1:] == [second],
            description="the last directory report to reach the app",
        )

        assert [message.path for message in app.messages_of(ShellPanel.DirectoryChanged)] == [first, second]
        assert _border_text(app.panel.border_subtitle) == second
        assert app.leaked == []


async def test_command_reports_are_passed_on_and_kept_until_drained(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        await _shown(app, shells)
        app.leaked.clear()

        await app.terminal.write(_report(2026, "git status") + _report(2026, "echo [bold]x | sort"))
        await wait_for(
            lambda: len(app.messages_of(ShellPanel.CommandExecuted)) == 2,
            description="both command reports to reach the app",
        )

        commands = ["git status", "echo [bold]x | sort"]
        assert [message.command for message in app.messages_of(ShellPanel.CommandExecuted)] == commands
        assert app.panel.drain_commands() == commands
        assert app.panel.drain_commands() == []
        assert app.leaked == []


@pytest.mark.parametrize(
    ("locale", "expected"),
    [
        (None, "Unable to start shell: [Errno 2] no [such/] shell"),
        ("zh-Hans", "无法启动 Shell：[Errno 2] no [such/] shell"),  # noqa: RUF001
    ],
)
async def test_shell_that_cannot_start_is_reported_as_literal_error_text(
    monkeypatch: pytest.MonkeyPatch, locale: str | None, expected: str
) -> None:
    async def spawn(argv: Sequence[str], *, env: Mapping[str, str], cwd: str, columns: int, lines: int) -> PtyProcess:
        raise OSError("[Errno 2] no [such/] shell")

    monkeypatch.setattr("chrys.app.tui.terminal.pty_process.spawn_pty_process", spawn)
    app = PanelApp(locale=locale)
    async with app.run_test(size=(60, 20), notifications=True) as pilot:
        app.panel.show()
        await wait_for(lambda: app.notifications, description="the failure to be reported")

        assert app.notifications == [Notified(expected, "error", False)]
        # Taken for markup, those brackets would fail to draw.
        await wait_for(lambda: len(app.screen.query("Toast")) == 1, pilot=pilot, description="the toast to show")
        await wait_for(lambda: app.messages_of(ShellPanel.Exited), description="the panel to say its shell is gone")
        assert app.leaked == []


# -- restarting ------------------------------------------------------------------------------------


async def test_shell_that_exits_is_announced_and_replaced_where_it_last_reported(
    shells: ShellFactory, tmp_path: Path
) -> None:
    start, moved = str(tmp_path / "start"), str(tmp_path / "moved")
    app = PanelApp(start)
    async with app.run_test(size=(60, 20)):
        first = await _shown(app, shells)
        await app.terminal.write(_report(2025, moved))
        await wait_for(lambda: app.messages_of(ShellPanel.DirectoryChanged), description="the directory report")
        app.leaked.clear()

        first.exit()
        await wait_for(lambda: app.messages_of(ShellPanel.Exited), description="the exit to reach the app")

        assert len(app.messages_of(ShellPanel.Exited)) == 1
        assert app.leaked == []
        assert shells.only is first

        assert not app.panel.has_live_shell
        second = await _shown(app, shells)

        assert second is not first
        assert second.working_directory == moved
        assert second.starts == 1
        assert _border_text(app.panel.border_subtitle) == moved


@pytest.mark.parametrize("host_repaints", [False, True], ids=["posix-pty", "conpty"])
async def test_replacement_shell_gets_a_blank_terminal_only_from_a_host_that_repaints(
    shells: ShellFactory, host_repaints: bool
) -> None:
    shells.repaints_after_resize = host_repaints
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        first = await _shown(app, shells)
        terminal = app.terminal
        assert terminal.host_repaints is host_repaints
        await terminal.write("from the first shell\r\n")
        emulator = terminal.emulator

        first.exit()
        await wait_for(lambda: app.messages_of(ShellPanel.Exited), description="the exit to reach the app")
        await _shown(app, shells)

        if host_repaints:
            # ConPTY addresses a screen of its own from the top; ours has to match it.
            assert terminal.emulator is not emulator
            assert _screen(terminal) == []
            assert (terminal.width, terminal.height) == (emulator.columns, emulator.lines)
        else:
            assert terminal.emulator is emulator
            assert _screen(terminal) == ["from the first shell"]


async def test_directory_change_while_hidden_restarts_the_shell_there(shells: ShellFactory, tmp_path: Path) -> None:
    old, new = str(tmp_path / "old"), str(tmp_path / "new")
    app = PanelApp(old)
    async with app.run_test(size=(60, 20)):
        first = await _shown(app, shells)
        app.panel.hide()

        await app.panel.change_directory(new)

        # Typing `cd` into a shell nobody is looking at could land in whatever it is running.
        assert first.sent == []
        assert first.closes == 1
        assert shells.only is first

        second = await _shown(app, shells)

        assert second.working_directory == new
        assert second.sent == []
        assert _border_text(app.panel.border_subtitle) == new
        assert app.messages_of(ShellPanel.Exited) == []

        # What was asked for is used once. The next restart is back to where the shell says it is.
        await app.terminal.write(_report(2025, old))
        await wait_for(lambda: app.messages_of(ShellPanel.DirectoryChanged), description="the directory report")
        second.exit()
        await wait_for(lambda: app.messages_of(ShellPanel.Exited), description="the exit to reach the app")
        third = await _shown(app, shells)
        assert third.working_directory == old


async def test_directory_change_while_hidden_starts_a_repainting_host_on_a_blank_terminal(
    shells: ShellFactory, tmp_path: Path
) -> None:
    shells.repaints_after_resize = True
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        await _shown(app, shells)
        await app.terminal.write("from the first shell\r\n")
        app.panel.hide()

        await app.panel.change_directory(str(tmp_path))
        await _shown(app, shells)

        assert _screen(app.terminal) == []


async def test_directory_change_while_visible_is_typed_into_the_running_shell(
    shells: ShellFactory, tmp_path: Path
) -> None:
    old, new = str(tmp_path / "old"), str(tmp_path / "a b")
    app = PanelApp(old)
    async with app.run_test(size=(60, 20)):
        shell = await _shown(app, shells)

        await app.panel.change_directory(new)

        assert shell.sent == [(app.panel._cd_command(new), *_visible_size(app.terminal))]
        assert shells.only is shell
        assert (shell.terminations, shell.closes) == (0, 0)
        # The shell says where it ended up; until then nothing claims it moved.
        assert _border_text(app.panel.border_subtitle) == old
        assert app.messages_of(ShellPanel.DirectoryChanged) == []


async def test_directory_change_after_the_shell_exited_waits_for_the_next_shell(
    shells: ShellFactory, tmp_path: Path
) -> None:
    start, reported, wanted = (str(tmp_path / name) for name in ("start", "reported", "wanted"))
    app = PanelApp(start)
    async with app.run_test(size=(60, 20)):
        first = await _shown(app, shells)
        await app.terminal.write(_report(2025, reported))
        first.exit()
        await wait_for(lambda: app.messages_of(ShellPanel.Exited), description="the exit to reach the app")

        await app.panel.change_directory(wanted)

        assert first.sent == []
        assert shells.only is first

        second = await _shown(app, shells)

        # Asked for outranks last reported.
        assert second.working_directory == wanted
        assert _border_text(app.panel.border_subtitle) == wanted


async def test_showing_the_panel_while_the_old_shell_is_still_closing_keeps_the_new_one(
    shells: ShellFactory, tmp_path: Path
) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        first = await _shown(app, shells)
        app.panel.hide()
        first.close_gate = asyncio.Event()

        change = asyncio.create_task(app.panel.change_directory(str(tmp_path)))
        try:
            await wait_for(lambda: first.closes == 1 or change.done(), description="the old shell to start closing")
            assert not change.done()
            second = await _shown(app, shells)
        finally:
            first.close_gate.set()
            await change

        await app.panel.send_interrupt()

        assert second.working_directory == str(tmp_path)
        assert (first.interrupts, second.interrupts) == (0, 1)


# -- talking to the shell --------------------------------------------------------------------------


async def test_commands_go_to_the_shell_with_the_size_on_display(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        shell = await _shown(app, shells)

        await app.panel.send_command("ls -la")
        await app.panel.send_interrupt()

        assert shell.sent == [("ls -la", *_visible_size(app.terminal))]
        assert shell.interrupts == 1


async def test_panel_without_a_shell_has_nobody_to_talk_to() -> None:
    panel = ShellPanel()

    await panel.send_command("ls")
    await panel.send_interrupt()
    await panel.close()
    panel.stop()
    panel.prepare_for_gc_freeze()
    panel.after_gc_freeze()

    assert panel.drain_commands() == []
    assert not panel.is_alternate_screen


@pytest.mark.parametrize(
    ("shell_name", "path", "command"),
    [
        ("zsh", "/tmp/a b/it's", "cd '/tmp/a b/it'\"'\"'s'"),
        ("bash", "/tmp/$HOME; rm -rf x", "cd '/tmp/$HOME; rm -rf x'"),
        ("fish", "/tmp/plain", "cd /tmp/plain"),
        ("pwsh", "C:\\Users\\O'Brien", "Set-Location -LiteralPath 'C:\\Users\\O''Brien'"),
        ("PowerShell", "C:\\Program Files\\$x", "Set-Location -LiteralPath 'C:\\Program Files\\$x'"),
        ("cmd", "D:\\Program Files\\a&b", 'cd /d "D:\\Program Files\\a&b"'),
        ("C:\\Windows\\System32\\cmd.exe", "D:\\work", 'cd /d "D:\\work"'),
    ],
)
def test_directory_change_is_spelled_for_the_shell_in_use(
    monkeypatch: pytest.MonkeyPatch, shell_name: str, path: str, command: str
) -> None:
    platform = replace(get_platform(), shell=ShellInfo(name=shell_name, path=f"/opt/{shell_name}", args=[]))

    def fake_platform() -> PlatformInfo:
        return platform

    monkeypatch.setattr(panel_module, "get_platform", fake_platform)
    panel = ShellPanel()

    assert panel.shell_name == shell_name
    assert panel._cd_command(path) == command


# -- size ------------------------------------------------------------------------------------------


async def test_showing_the_panel_tells_the_shell_the_size_on_display_once(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        shell = await _shown(app, shells)
        await _panel_is_idle(app.panel)

        # The shell started at the size of a terminal nobody had laid out yet.
        visible = _visible_size(app.terminal)
        assert visible != (80, 24)
        assert shell.resizes == [visible]
        assert app.panel._pending_resize is None
        assert app.panel._resize_timer is None
        assert app.leaked == []


@pytest.mark.parametrize("late_resize", [False, True])
async def test_resize_while_hidden_is_caught_up_with_when_shown_again(
    shells: ShellFactory, monkeypatch: pytest.MonkeyPatch, late_resize: bool
) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)) as pilot:
        if late_resize:
            delay_resize_dispatch(app, monkeypatch, 0.25)
        shell = await _shown(app, shells)
        terminal = app.terminal
        before = _visible_size(terminal)
        app.panel.hide()

        await resize_when_settled(pilot, 100, 30)
        await _panel_is_idle(app.panel)

        # Hidden, the terminal is laid out empty, which is not a size to tell a program.
        assert (terminal.width, terminal.height) == before
        assert shell.resizes == [before]

        await _shown(app, shells)

        after = (before[0] + 40, before[1] + 10)
        assert _visible_size(terminal) == after
        assert (terminal.width, terminal.height) == after
        assert shell.resizes == [before, after]


@pytest.mark.parametrize("late_resize", [False, True])
async def test_catching_up_rewraps_the_screen_even_for_a_host_that_repaints(
    shells: ShellFactory, monkeypatch: pytest.MonkeyPatch, late_resize: bool
) -> None:
    shells.repaints_after_resize = True
    app = PanelApp()
    async with app.run_test(size=(60, 20)) as pilot:
        if late_resize:
            delay_resize_dispatch(app, monkeypatch, 0.25)
        await _shown(app, shells)
        terminal = app.terminal
        line = "x" * (terminal.width + 10)
        await terminal.write(line)
        assert _screen(terminal) == [line[: terminal.width], line[terminal.width :]]
        app.panel.hide()

        await resize_when_settled(pilot, 100, 20)
        await _shown(app, shells)

        # The host saw none of the sizes in between, so its repaint will not account for them.
        assert _screen(terminal) == [line]


@pytest.mark.parametrize("late_resize", [False, True])
async def test_catching_up_waits_for_a_layout_the_refresh_callback_ran_ahead_of(
    shells: ShellFactory, monkeypatch: pytest.MonkeyPatch, late_resize: bool
) -> None:
    shells.repaints_after_resize = True
    app = PanelApp()
    async with app.run_test(size=(60, 20)) as pilot:
        if late_resize:
            delay_resize_dispatch(app, monkeypatch, 0.25)
        await _shown(app, shells)
        terminal = app.terminal
        line = "x" * (terminal.width + 10)
        await terminal.write(line)
        app.panel.hide()
        await resize_when_settled(pilot, 100, 20)
        panel = app.panel

        def ahead_of_layout(callback: Callable[..., object], *args: object, **kwargs: object) -> bool:
            # A screen can run its after-refresh callbacks while the panel's layout request still
            # waits in its queue. The panel's next idle sends that request and runs this at once.
            panel.call_next(callback, *args, **kwargs)
            return True

        monkeypatch.setattr(panel, "call_after_refresh", ahead_of_layout)
        await _shown(app, shells)

        assert _screen(terminal) == [line]


async def test_resize_on_display_reaches_the_shell_after_the_debounce(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)) as pilot:
        shell = await _shown(app, shells)
        terminal = app.terminal
        before = _visible_size(terminal)

        await pilot.resize_terminal(90, 25)
        after = (before[0] + 30, before[1] + 5)
        await wait_for(lambda: shell.pty_size == after, description="the debounced resize to reach the shell")

        assert (terminal.width, terminal.height) == after
        assert shell.resizes == [before, after]
        assert app.panel._pending_resize is None
        assert app.panel._resize_timer is None


async def test_size_is_not_sent_to_a_shell_that_is_not_ready_or_no_longer_on_display(shells: ShellFactory) -> None:
    shells.ready_at_start = False
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        app.panel.show()
        shell = shells.only
        await wait_for(lambda: shell.ready_waiters == 1, description="the panel to wait for its shell")
        assert shell.resizes == []

        app.panel.hide()
        shell.become_ready()
        await _panel_is_idle(app.panel)

        assert shell.resizes == []


def test_size_change_the_shell_has_not_heard_of_is_debounced(monkeypatch: pytest.MonkeyPatch) -> None:
    terminal = Terminal(size=(80, 24))
    # No size yet: the process does not exist, or telling it failed. 80x24 is not to be assumed.
    panel, shell = _panel_with_shell(terminal, pty_size=None)
    timers = TimerRecorder()
    monkeypatch.setattr(panel, "set_timer", timers)

    panel._on_terminal_size_changed(Terminal.SizeChanged(terminal, 80, 24))

    assert panel._pending_resize == (80, 24)
    assert timers.requests == [(panel_module._RESIZE_DEBOUNCE_S, panel._apply_pending_resize)]
    assert shell.resizes == []

    terminal.update_size(100, 30)
    panel._on_terminal_size_changed(Terminal.SizeChanged(terminal, 100, 30))

    # One timer at a time, for the newest size.
    assert panel._pending_resize == (100, 30)
    assert [timer.stops for timer in timers.timers] == [1, 0]


@pytest.mark.parametrize(
    ("terminal_size", "pty_size", "finished", "reason"),
    [
        ((100, 20), (80, 24), False, "a newer size is on display already"),
        ((100, 10), (100, 10), False, "the shell has been told this size"),
        ((100, 10), (80, 24), True, "the shell is gone"),
    ],
)
def test_size_change_with_nothing_to_tell_the_shell_is_dropped(
    monkeypatch: pytest.MonkeyPatch,
    terminal_size: tuple[int, int],
    pty_size: tuple[int, int],
    finished: bool,
    reason: str,
) -> None:
    terminal = Terminal(size=terminal_size)
    panel, shell = _panel_with_shell(terminal, pty_size=pty_size)
    shell.is_finished = finished
    timers = TimerRecorder()
    monkeypatch.setattr(panel, "set_timer", timers)

    panel._on_terminal_size_changed(Terminal.SizeChanged(terminal, 100, 10))

    assert timers.requests == [], reason
    assert panel._pending_resize is None, reason


async def test_pending_resize_is_applied_only_while_it_is_still_the_size_on_display() -> None:
    terminal = Terminal(size=(100, 20))
    panel, shell = _panel_with_shell(terminal, pty_size=(80, 24))

    panel._pending_resize = (100, 10)
    await panel._apply_pending_resize()

    assert shell.resizes == []
    assert panel._pending_resize is None

    panel._pending_resize = (100, 20)
    await panel._apply_pending_resize()

    assert shell.resizes == [(100, 20)]
    assert panel._pending_resize is None
    assert panel._resize_timer is None


# -- going away ------------------------------------------------------------------------------------


async def test_removing_the_panel_closes_its_shell(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        shell = await _shown(app, shells)

        await app.panel.remove()

        assert shell.closes == 1
        await app.panel.send_interrupt()
        assert shell.interrupts == 0


async def test_leaving_the_app_closes_the_shell(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        shell = await _shown(app, shells)

    assert shell.closes == 1


async def test_stopping_the_panel_ends_the_shell_without_waiting_for_it(shells: ShellFactory) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)) as pilot:
        shell = await _shown(app, shells)
        await pilot.resize_terminal(90, 25)
        await wait_for(lambda: app.terminal.width != shell.resizes[0][0], description="the new size to be laid out")

        app.panel.stop()

        assert (shell.terminations, shell.closes) == (1, 0)
        assert app.panel._resize_timer is None


# -- GC freeze -------------------------------------------------------------------------------------


async def test_gc_freeze_hooks_hand_the_render_cache_over_to_the_terminal(
    shells: ShellFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = PanelApp()
    async with app.run_test(size=(60, 20)):
        calls: list[str] = []
        monkeypatch.setattr(app.terminal, "detach_render_cache", lambda: calls.append("detach"))
        monkeypatch.setattr(app.terminal, "renew_render_cache", lambda: calls.append("renew"))

        app.panel.prepare_for_gc_freeze()
        app.panel.after_gc_freeze()
        app.panel.prepare_for_gc_freeze()
        app.panel.abort_gc_freeze()

        assert calls == ["detach", "renew", "detach", "renew"]
