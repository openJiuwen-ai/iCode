# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fork tab discovery and acknowledgement at the terminal process boundary."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.terminal import fork_tab
from chrys.foundation.i18n.formatting import format_message


def test_detect_requires_a_local_ghostty_with_tty_support(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fork_tab, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(fork_tab.os, "ttyname", lambda fd: "/dev/ttys003" if fd == 0 else None, raising=False)
    monkeypatch.setattr(fork_tab, "is_remote_execution_environment", lambda: False)
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    for version in ("1.3.1", "unknown", ""):
        monkeypatch.setenv("TERM_PROGRAM_VERSION", version)
        assert fork_tab.detect_fork_tab_target() is None
    monkeypatch.setenv("TERM_PROGRAM_VERSION", "1.4.0")
    assert fork_tab.detect_fork_tab_target() == fork_tab.ForkTabTarget("Ghostty", "/dev/ttys003")
    monkeypatch.setattr(fork_tab, "is_remote_execution_environment", lambda: True)
    assert fork_tab.detect_fork_tab_target() is None


@pytest.mark.parametrize(
    ("output", "err", "code", "message"),
    [
        ("opened\n", "", 0, None),
        ("missing\n", "", 0, "Could not uniquely identify"),
        ("", "AppleEvent error (-1743)", 1, "denied permission"),
        ("", "automation disabled", 1, "automation disabled"),
    ],
)
async def test_open_waits_for_host_reply_and_passes_paths_as_data(monkeypatch, output, err, code, message) -> None:
    argv = ["/Applications/My iCode's/icode", "--session", "full-fork-id"]
    monkeypatch.setattr(fork_tab, "build_macos_fork_argv", lambda session: argv if session == argv[-1] else [])
    run = create_autospec(
        fork_tab.run_macos_terminal_script, return_value=subprocess.CompletedProcess([], code, output, err)
    )
    monkeypatch.setattr(fork_tab, "run_macos_terminal_script", run)
    cwd = "/space's 项目"
    reply = await fork_tab.ForkTabTarget("Terminal", "/dev/ttys003").open("full-fork-id", cwd)
    script, tty, command = run.await_args.args
    assert tty == "/dev/ttys003"
    assert shlex.split(command) == ["cd", cwd, "&&", *argv]
    assert cwd not in script
    if message is None:
        assert reply is None
    else:
        assert reply is not None and message in format_message(reply)


@pytest.mark.skipif(sys.platform == "win32", reason="macOS launches the fork through a POSIX shell")
@pytest.mark.parametrize(
    ("shell_name", "rc_name", "rc_line"),
    [("zsh", ".zshrc", "export FORK_TEST_VALUE=loaded"), ("tcsh", ".tcshrc", "setenv FORK_TEST_VALUE loaded")],
)
async def test_ghostty_starts_with_interactive_shell_configuration(
    monkeypatch, tmp_path, shell_name, rc_name, rc_line
) -> None:
    import shutil

    shell = shutil.which(shell_name)
    if not shell:
        pytest.skip(f"requires {shell_name}")
    work = tmp_path / "project's !folder"
    work.mkdir()
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ZDOTDIR", str(tmp_path))
    (tmp_path / rc_name).write_text(f"{rc_line}\ncd /\n")
    monkeypatch.setattr(
        fork_tab, "build_macos_fork_argv", lambda session: ["/bin/sh", "-c", 'printf "%s %s" "$FORK_TEST_VALUE" "$PWD"']
    )

    async def run_script(script, tty, cwd, command):
        assert "new tab in sourceWindow" in script
        child = await asyncio.to_thread(
            subprocess.run, shlex.split(command), stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True
        )
        assert child.stdout == f"loaded {work.resolve()}"
        return subprocess.CompletedProcess([], 0, "opened", "")

    monkeypatch.setattr(fork_tab, "run_macos_terminal_script", run_script)
    assert await fork_tab.ForkTabTarget("Ghostty", "/dev/test").open("fork-id", str(work)) is None


@pytest.mark.skipif(sys.platform != "darwin", reason="exercises Terminal.app's interactive shell command")
async def test_terminal_tab_preserves_bang_in_csh_directory_and_arguments(monkeypatch, tmp_path) -> None:
    work = tmp_path / "user's !notes"
    work.mkdir()
    argv = ["/bin/sh", "-c", 'printf "%s\\n%s" "$PWD" "$1"', "sh", "user's !fork"]
    monkeypatch.setattr(fork_tab, "build_macos_fork_argv", lambda session: argv)
    monkeypatch.setenv("HOME", str(tmp_path))

    async def run_script(script, tty, command):
        assert "do script in new tab startCommand next to matchedTab" in script
        child = await asyncio.to_thread(
            subprocess.run,
            ["/bin/tcsh", "-f", "-i", "-c", command],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
        )
        assert child.stdout == f"{work.resolve()}\nuser's !fork"
        return subprocess.CompletedProcess([], 0, "opened", "")

    monkeypatch.setattr(fork_tab, "run_macos_terminal_script", run_script)
    assert await fork_tab.ForkTabTarget("Terminal", "/dev/test").open("fork-id", str(work)) is None
