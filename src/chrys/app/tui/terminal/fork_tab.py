# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Open a fork next to the originating macOS terminal, identified by its TTY."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chrys.app.tui.terminal.launcher import (
    build_macos_fork_argv,
    is_remote_execution_environment,
    macos_login_shell_argv,
    macos_shell_command,
    run_macos_terminal_script,
)
from chrys.foundation.i18n import MessageRef, msg

TAB_UNAVAILABLE = msg(
    "tui.fork_session.tab.unavailable",
    fallback="This terminal cannot identify the originating window for a new tab.",
)
_TAB_SOURCE_GONE = msg(
    "tui.fork_session.tab.source_gone", fallback="Could not uniquely identify the originating terminal."
)
_TAB_TIMEOUT = msg("tui.fork_session.tab.timeout", fallback="The terminal did not confirm whether the tab opened.")
_TAB_DENIED = msg("tui.fork_session.tab.denied", fallback="macOS denied permission to control the terminal.")
_TAB_FAILED = msg("tui.fork_session.tab.failed", fallback="The terminal could not open the tab: {error}")

_GHOSTTY_SCRIPT = """
on run argv
    set {sourceTTY, startDir, startCommand} to argv
    tell application id "com.mitchellh.ghostty"
        set matchedID to missing value
        repeat with win in windows
            set sourceWindow to window id (id of win)
            if sourceTTY is in (tty of every terminal of sourceWindow) then
                if matchedID is not missing value then return "missing"
                set matchedID to id of sourceWindow
            end if
        end repeat
        if matchedID is not missing value then
            set sourceWindow to window id matchedID
            if sourceTTY is not in (tty of every terminal of sourceWindow) then return "missing"
            set cfg to new surface configuration
            set initial working directory of cfg to startDir
            set command of cfg to startCommand
            new tab in sourceWindow with configuration cfg
            return "opened"
        end if
    end tell
    return "missing"
end run
"""
_TERMINAL_SCRIPT = """
on run argv
    set {sourceTTY, startCommand} to argv
    tell application id "com.apple.Terminal"
        set matchedTab to missing value
        repeat with win in windows
            repeat with sourceTab in tabs of (window id (id of win))
                if tty of sourceTab is sourceTTY and (count of processes of sourceTab) > 0 then
                    if matchedTab is not missing value then return "missing"
                    set matchedTab to contents of sourceTab
                end if
            end repeat
        end repeat
        if matchedTab is not missing value then
            if tty of matchedTab is not sourceTTY or (count of processes of matchedTab) = 0 then return "missing"
            do script in new tab startCommand next to matchedTab
            return "opened"
        end if
    end tell
    return "missing"
end run
"""


@dataclass(frozen=True)
class ForkTabTarget:
    host: Literal["Ghostty", "Terminal"]
    tty: str

    async def open(self, session_id: str, cwd: str) -> MessageRef | None:
        """Return only after the host acknowledges creating the tab or reports an error."""
        try:
            argv = await asyncio.to_thread(build_macos_fork_argv, session_id)
            if self.host == "Ghostty":
                command = shlex.join(macos_login_shell_argv(argv, cwd=cwd))
                result = await run_macos_terminal_script(_GHOSTTY_SCRIPT, self.tty, cwd, command)
            else:
                command = macos_shell_command(argv, cwd=cwd)
                result = await run_macos_terminal_script(_TERMINAL_SCRIPT, self.tty, command)
        except TimeoutError:
            return _TAB_TIMEOUT.bind()
        except OSError as exc:
            return _TAB_FAILED.bind(error=str(exc))
        if result.returncode:
            if "(-1743)" in result.stderr:
                return _TAB_DENIED.bind()
            return _TAB_FAILED.bind(error=result.stderr.strip())
        if result.stdout.strip() != "opened":
            return _TAB_SOURCE_GONE.bind()
        return None


def detect_fork_tab_target() -> ForkTabTarget | None:
    if sys.platform != "darwin" or is_remote_execution_environment():
        return None
    program = os.environ.get("TERM_PROGRAM")
    if program == "Apple_Terminal":
        # Older Terminal.app dictionaries only support opening a new window.
        dictionary = Path("/System/Applications/Utilities/Terminal.app/Contents/Resources/Terminal.sdef")
        try:
            if b'code="trmxdstb"' not in dictionary.read_bytes():
                return None
        except OSError:
            return None
        host: Literal["Ghostty", "Terminal"] = "Terminal"
    elif program == "ghostty":
        # The terminal TTY property was introduced in Ghostty 1.4.
        version = os.environ.get("TERM_PROGRAM_VERSION", "").split(".")
        if len(version) < 2 or not all(part.isdecimal() for part in version[:2]):
            return None
        if (int(version[0]), int(version[1])) < (1, 4):
            return None
        host = "Ghostty"
    else:
        return None
    try:
        return ForkTabTarget(host, os.ttyname(0))
    except OSError:
        return None
