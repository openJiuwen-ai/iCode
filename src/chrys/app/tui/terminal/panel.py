# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ShellPanel — dedicated terminal panel for shell mode.

Manages a persistent PTY-based Shell and Terminal widget.
Hidden by default; shown when the user enters shell mode (``!``).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import on
from textual.message import Message
from textual.widget import Widget

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.support.gc_freeze import GcFreezeBlockReason
from chrys.app.tui.terminal.shell import Shell, ShellFinished
from chrys.app.tui.terminal.widget import Terminal
from chrys.foundation.platform import get_platform, safe_getcwd

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.timer import Timer

    from chrys.foundation.i18n import MessageRef


# ConPTY set_size() triggers a full buffer reflow (10-100ms+), so use a
# longer debounce on Windows to avoid reflow storms during drag-resize.
_RESIZE_DEBOUNCE_S = 0.12 if get_platform().is_windows else 0.05


class ShellPanel(Widget):
    """Dedicated terminal panel for shell mode.

    Contains a single Terminal widget backed by a persistent Shell (PTY).
    Hidden by default via direct display state; toggled by MainScreen.
    """

    DEFAULT_CSS = """
    ShellPanel {
        width: 1fr;
        height: 1fr;
        border: round $tui-border-warning $border-opacity;
        padding: 0 0 0 1;
        border-title-align: left;
        border-title-color: $tui-border-title-warning;
        border-subtitle-align: right;
        border-subtitle-color: $tui-border-title-warning;
    }
    ShellPanel > Terminal {
        scrollbar-size: 1 1;
    }
    """

    @dataclass
    class DirectoryChanged(Message):
        """Shell working directory changed."""

        path: str

    @dataclass
    class CommandExecuted(Message):
        """User executed a shell command (captured via shell integration hooks)."""

        command: str

    @dataclass
    class Exited(Message):
        """Shell process exited."""

    def __init__(self, *, working_directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shell: Shell | None = None
        self._terminal: Terminal | None = None
        self._command_history: list[str] = []
        self._pending_cwd: str | None = None
        self._working_directory: str | None = working_directory
        platform_info = get_platform()
        self._shell_name = platform_info.shell.name
        self._shell_path = platform_info.shell.path
        self._resize_timer: Timer | None = None
        self._pending_resize: tuple[int, int] | None = None
        self._catch_up_pending = False
        self.display = False

    @property
    def shell_name(self) -> str:
        """Human-readable shell name (e.g. 'zsh', 'bash', 'pwsh', 'powershell')."""
        return self._shell_name

    def compose(self) -> ComposeResult:
        self._terminal = Terminal()
        yield self._terminal

    def on_mount(self) -> None:
        self.border_title = Text(self._shell_name)

    async def on_unmount(self) -> None:
        """Release the owned shell process when the panel leaves the DOM."""
        await self.close()

    def show(self) -> None:
        """Show the shell panel and start the shell if needed."""
        self.display = True
        # The first of these to find the panel laid out catches up. The callback can run before the
        # layout that places a panel just shown; a panel already on display gets no resize.
        self._catch_up_pending = True
        self.call_after_refresh(self._sync_visible_terminal_size)
        pending_cwd = self._pending_cwd
        if pending_cwd is not None and self._shell is not None and not self._shell.is_finished:
            self._shell.terminate()
            self._shell = None
        if self._shell is None or self._shell.is_finished:
            # ConPTY treats each child as owning a fresh screen and addresses
            # it in absolute coordinates; over the previous session's rows
            # those would land on old content. A Unix PTY has no such
            # middle layer, so there the history and cursor carry over. Only a shell
            # that ran says which it is, so the first one starts as it finds things.
            if self._terminal is not None and self._terminal.host_repaints:
                self._terminal.reset()
            cwd = pending_cwd or self._working_directory or safe_getcwd()
            self._pending_cwd = None
            self._working_directory = cwd
            self.border_subtitle = Text(cwd)
            self._start_shell(cwd)

    def hide(self) -> None:
        """Hide the shell panel (shell keeps running in background)."""
        self.display = False

    @property
    def has_live_shell(self) -> bool:
        """A hidden PTY can still own running jobs, even at a shell prompt."""
        return self._shell is not None and not self._shell.is_finished

    @property
    def is_visible(self) -> bool:
        return self.display

    @property
    def is_alternate_screen(self) -> bool:
        """True when an interactive app (top/vim) is using alternate screen."""
        return self._terminal is not None and self._terminal.alternate_screen

    def gc_freeze_block_reason(self) -> GcFreezeBlockReason | None:
        """Block freezes only while shell mode is visible."""
        return GcFreezeBlockReason.SHELL_VISIBLE if self.is_visible else None

    def prepare_for_gc_freeze(self) -> None:
        """Release hidden terminal render-cache cycles before collection."""
        if self._terminal is not None:
            self._terminal.detach_render_cache()

    def after_gc_freeze(self) -> None:
        """Renew the render LRU after the permanent generation changes."""
        if self._terminal is not None:
            self._terminal.renew_render_cache()

    def abort_gc_freeze(self) -> None:
        """Restore a detached render LRU after an incomplete hook pass."""
        self.after_gc_freeze()

    def _start_shell(self, working_directory: str | None = None) -> None:
        """Create and start a new Shell process."""
        if self._terminal is None:
            return
        cwd = working_directory or safe_getcwd()
        self._shell = Shell(
            terminal=self._terminal,
            working_directory=cwd,
            shell_command=self._shell_path,
            on_error=self._on_shell_error,
        )
        self._shell.start()

    def _on_shell_error(self, error: MessageRef) -> None:
        self.notify(render_str(widget_localizer(self), error), severity="error", markup=False)

    @on(Terminal.DirectoryChanged)
    def _on_directory_changed(self, event: Terminal.DirectoryChanged) -> None:
        """The shell's hooks report the directory ahead of every prompt, changed or not."""
        event.stop()
        if event.path == self._working_directory:
            return
        self._working_directory = event.path
        self.border_subtitle = Text(event.path)
        self.post_message(self.DirectoryChanged(path=event.path))

    @on(Terminal.CommandSubmitted)
    def _on_command_submitted(self, event: Terminal.CommandSubmitted) -> None:
        event.stop()
        self._command_history.append(event.command)
        self.post_message(self.CommandExecuted(command=event.command))

    def drain_commands(self) -> list[str]:
        """Return and clear accumulated command history."""
        commands = self._command_history.copy()
        self._command_history.clear()
        return commands

    def _cd_command(self, path: str) -> str:
        shell_name = self._shell_name.lower()
        if "powershell" in shell_name or "pwsh" in shell_name:
            escaped = path.replace("'", "''")
            return f"Set-Location -LiteralPath '{escaped}'"
        if shell_name == "cmd" or shell_name.endswith("cmd.exe"):
            escaped = path.replace('"', '""')
            return f'cd /d "{escaped}"'
        return f"cd {shlex.quote(path)}"

    async def send_command(self, command: str) -> None:
        """Send a command to the shell."""
        if self._shell is None or self._terminal is None:
            return
        width, height = self._terminal.scrollable_content_region.size
        await self._shell.send(command, width, height)

    async def on_resize(self) -> None:
        # Comes with the layout that places the panel, ahead of the terminal's report of its new
        # size, so a catch-up still pending rewraps the screen before a debounced resize.
        await self._sync_visible_terminal_size()

    async def _sync_visible_terminal_size(self) -> None:
        """Catch up terminal/PTY geometry after shell mode was hidden during an app resize."""
        terminal = self._terminal
        if terminal is None or not self.is_visible or not self._catch_up_pending:
            return
        width, height = terminal.scrollable_content_region.size
        if width <= 0 or height <= 0:
            # Not laid out yet: left pending for the panel's resize.
            return

        self._catch_up_pending = False
        terminal.update_size(width, height, immediate=True, force_reflow=True)

        shell = self._shell
        if shell is None or shell.is_finished:
            return
        self._stop_resize_timer()
        self._pending_resize = None
        await shell.wait_for_ready()
        if self._shell is shell and not shell.is_finished and self.is_visible:
            await shell.resize(terminal.width, terminal.height)

    async def send_interrupt(self) -> None:
        """Send Ctrl+C to the shell."""
        if self._shell is not None:
            await self._shell.interrupt()

    async def change_directory(self, path: str) -> None:
        """Change the shell's working directory. Subtitle stays in sync via shell hooks."""
        shell_running = self._shell is not None and not self._shell.is_finished
        if self.is_visible and shell_running:
            await self.send_command(self._cd_command(path))
            return

        # Avoid injecting a synthetic cd into a hidden or exited shell. The
        # line editor may be in a different input mode when the panel is shown
        # again, so restart it directly in the new cwd instead.
        self._pending_cwd = path
        if shell_running:
            await self.close()

    def _stop_resize_timer(self) -> None:
        if self._resize_timer is not None:
            self._resize_timer.stop()
            self._resize_timer = None

    def stop(self) -> None:
        """Sync fast-path: kill before app exit; unmount runs the async drain."""
        self._stop_resize_timer()
        if self._shell is not None:
            self._shell.terminate()

    async def close(self) -> None:
        """Terminate the shell process and wait for terminal tasks to settle."""
        self._stop_resize_timer()
        shell = self._shell
        if shell is None:
            return
        await shell.close()
        if self._shell is shell:
            self._shell = None

    @on(Terminal.SizeChanged)
    def _on_terminal_size_changed(self, event: Terminal.SizeChanged) -> None:
        """Debounce PTY resize to avoid ConPTY reflow storms during drag-resize."""
        event.stop()
        shell = self._shell
        if shell is None or shell.is_finished:
            return
        # Resize messages are queued; during drag-resize a newer terminal size
        # may already be visible by the time an older message is dispatched.
        if (event.width, event.height) != (event.terminal.width, event.terminal.height):
            return
        # _sync_visible_terminal_size may already have issued the resize directly;
        # stale queued SizeChanged messages for the same dimensions can be ignored.
        if shell.pty_size == (event.width, event.height):
            return
        self._pending_resize = (event.width, event.height)
        if self._resize_timer is not None:
            self._resize_timer.stop()
        self._resize_timer = self.set_timer(_RESIZE_DEBOUNCE_S, self._apply_pending_resize)

    async def _apply_pending_resize(self) -> None:
        """Apply the most recent pending PTY resize."""
        resize = self._pending_resize
        self._pending_resize = None
        self._resize_timer = None
        if resize is None:
            return
        terminal = self._terminal
        if terminal is not None and resize != (terminal.width, terminal.height):
            return
        if self._shell is not None and not self._shell.is_finished:
            await self._shell.resize(*resize)

    @on(ShellFinished)
    def _on_shell_finished(self, event: ShellFinished) -> None:
        """Shell process exited."""
        event.stop()
        self.post_message(self.Exited())
