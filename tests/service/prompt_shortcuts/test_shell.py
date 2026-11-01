# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for a prompt shortcut's ``!`command``` placeholders: run at send time, output spliced in."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import psutil
import pytest

import chrys.service.prompt_shortcuts.shell as shell_module
from chrys.service.prompt_shortcuts.shell import (
    SHELL_OUTPUT_LIMIT_BYTES,
    ShellInterpolationError,
    interpolate_shell,
    run_shell_command,
    shell_commands,
)
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell syntax")


@pytest.fixture
def posix_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run placeholders under ``/bin/sh`` whatever the developer's login shell is."""
    monkeypatch.setenv("SHELL", "/bin/sh")


def test_shell_commands_lists_every_placeholder_in_order_with_duplicates() -> None:
    text = "a !`git status` b !`ls -la` c !`git status`"
    assert shell_commands(text) == ["git status", "ls -la", "git status"]


def test_shell_commands_without_placeholders_is_empty() -> None:
    assert shell_commands("plain `code` and ! bang") == []


async def test_text_without_placeholders_is_returned_unchanged(tmp_path: Path) -> None:
    text = "Nothing to run here: `code` and !bang\n"
    assert await interpolate_shell(text, cwd=str(tmp_path)) == text


@pytest.mark.usefixtures("posix_shell")
async def test_a_command_with_quotes_runs_as_written_in_every_shell(tmp_path: Path) -> None:
    """``cmd.exe`` and ``sh`` alike: a quoted path with a space is one path, and the redirection writes to it."""
    target = tmp_path / "a folder" / "out.txt"
    target.parent.mkdir()

    await interpolate_shell(f'Wrote: !`echo ran > "{target}"`', cwd=str(tmp_path))

    assert target.read_text(encoding="utf-8").strip() == "ran"


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_each_placeholder_is_replaced_by_its_output_in_order(tmp_path: Path) -> None:
    text = "1:!`echo one` 2:!`echo two` 3:!`echo one`"
    assert await interpolate_shell(text, cwd=str(tmp_path)) == "1:one 2:two 3:one"


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_output_is_stdout_then_stderr(tmp_path: Path) -> None:
    text = "[!`printf err >&2; printf out`]"
    assert await interpolate_shell(text, cwd=str(tmp_path)) == "[outerr]"


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_non_zero_exit_still_substitutes_its_output(tmp_path: Path) -> None:
    text = "result: !`echo failed; exit 3`"
    assert await interpolate_shell(text, cwd=str(tmp_path)) == "result: failed"


@posix_only
@pytest.mark.usefixtures("posix_shell")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("printf 'x\\n'", "a x b"),
        ("printf 'x\\r\\n'", "a x b"),
        ("printf 'x\\n\\n'", "a x\n b"),
        ("printf 'x'", "a x b"),
        ("printf 'l1\\nl2\\n'", "a l1\nl2 b"),
    ],
    ids=["lf", "crlf", "two-lf", "none", "multiline"],
)
async def test_exactly_one_trailing_line_end_is_removed(tmp_path: Path, command: str, expected: str) -> None:
    assert await interpolate_shell(f"a !`{command}` b", cwd=str(tmp_path)) == expected


@posix_only
@pytest.mark.usefixtures("posix_shell")
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("printf '\\033[31mred\\033[0m\\nab\\rcd\\n'", "red\ncd"),
        ("printf '\\033[1;32mok\\033[0m'", "ok"),
        ("printf ' 10%%\\r 50%%\\r100%%\\n'", "100%"),
        ("printf '\\033[33mwarn\\033[0m' >&2; printf 'out '", "out warn"),
    ],
    ids=["color-and-redraw", "color-only", "progress-redraws", "stderr-color"],
)
async def test_colors_and_carriage_return_redraws_are_cleaned(tmp_path: Path, command: str, expected: str) -> None:
    assert await run_shell_command(command, cwd=str(tmp_path)) == expected


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_commands_run_in_the_given_cwd(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "marker.txt").write_text("here", encoding="utf-8")
    out = await interpolate_shell("!`cat marker.txt`", cwd=str(workdir))
    assert out == "here"


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_a_packaged_icode_puts_its_own_runtime_last_on_the_commands_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In a packaged iCode a command finds the user's tools (``python``, ``uv``), never the bundled runtime's."""
    runtime_dir = str(Path(sys.executable).parent)
    user_bin = str(tmp_path / "user" / "bin")
    monkeypatch.setenv("CHRYS_FORCE_FROZEN", "1")
    monkeypatch.setenv("PATH", os.pathsep.join([runtime_dir, user_bin]))

    out = await interpolate_shell('!`printf %s "$PATH"`', cwd=str(tmp_path))

    assert out.split(os.pathsep) == [user_bin, runtime_dir]


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_large_output_is_cut_to_head_and_tail_with_a_truncation_note(tmp_path: Path) -> None:
    raw_size = SHELL_OUTPUT_LIMIT_BYTES * 4
    command = f"i=0; while [ $i -lt {raw_size // 8} ]; do printf 'abcdefg\\n'; i=$((i+1)); done; printf 'TAILEND'"
    out = await interpolate_shell(f"!`{command}`", cwd=str(tmp_path))

    assert len(out) < raw_size
    assert out.startswith("abcdefg")
    assert "TAILEND" in out
    footer = out[out.index("TAILEND") + len("TAILEND") :].lower()
    assert any(word in footer for word in ("truncat", "limit", "not kept", "omitted"))


def _pid_command(pid_file: Path) -> str:
    # Written atomically so a reader never sees a partial pid.
    return f"echo $$ > '{pid_file}.tmp' && mv '{pid_file}.tmp' '{pid_file}'; sleep 30; echo done"


async def _capture_running(pid_file: Path, task: asyncio.Task[str]) -> list[psutil.Process]:
    await wait_for(lambda: pid_file.exists() or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="command started")
    assert not task.done()
    shell = psutil.Process(int(pid_file.read_text(encoding="utf-8")))
    await wait_for(lambda: shell.children(recursive=True), description="the shell started its sleep")
    return [shell, *shell.children(recursive=True)]


async def _assert_all_gone(processes: list[psutil.Process]) -> None:
    def gone() -> bool:
        return not any(process.is_running() for process in processes)

    await wait_for(gone, timeout=ENGINE_TURN_TIMEOUT, description="every process of the command ended")


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_command_past_the_timeout_fails_with_timeout_and_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shell_module, "SHELL_TIMEOUT_SECONDS", 1.0)
    pid_file = tmp_path / "pid"
    command = _pid_command(pid_file)
    task = asyncio.create_task(interpolate_shell(f"before !`{command}` after", cwd=str(tmp_path)))
    processes = await _capture_running(pid_file, task)

    with pytest.raises(ShellInterpolationError) as caught:
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)

    assert caught.value.reason == "timeout"
    assert caught.value.command == command
    await _assert_all_gone(processes)


@pytest.mark.usefixtures("posix_shell")
async def test_command_that_cannot_start_fails_with_start_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = str(tmp_path / "no-such-shell")
    monkeypatch.setattr(shell_module, "shell_argv", lambda command: [missing, "-c", command])

    with pytest.raises(ShellInterpolationError) as caught:
        await interpolate_shell("x !`echo hi` y", cwd=str(tmp_path))

    assert caught.value.reason == "start_failed"
    assert caught.value.command == "echo hi"


@pytest.mark.usefixtures("posix_shell")
async def test_command_no_process_can_be_given_fails_with_start_failed(tmp_path: Path) -> None:
    # No OS passes a NUL in a process's arguments.
    with pytest.raises(ShellInterpolationError) as caught:
        await interpolate_shell("x !`echo a\x00b` y", cwd=str(tmp_path))

    assert caught.value.reason == "start_failed"
    assert caught.value.command == "echo a\x00b"


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_cancelling_the_interpolation_kills_the_running_command(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    task = asyncio.create_task(interpolate_shell(f"!`{_pid_command(pid_file)}`", cwd=str(tmp_path)))
    processes = await _capture_running(pid_file, task)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)

    await _assert_all_gone(processes)


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_one_failing_command_fails_the_whole_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shell_module, "SHELL_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(ShellInterpolationError) as caught:
        await interpolate_shell("!`echo ok` !`sleep 30`", cwd=str(tmp_path))
    assert caught.value.command == "sleep 30"


def _python_writes(stdout_bytes: int, stderr_bytes: int) -> str:
    script = (
        f"import sys; sys.stdout.write('o' * {stdout_bytes}); sys.stdout.flush(); "
        f"sys.stderr.write('e' * {stderr_bytes}); sys.stderr.flush()"
    )
    return f"'{sys.executable}' -c \"{script}\""


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_small_stdout_and_stderr_are_joined_unchanged(tmp_path: Path) -> None:
    assert await run_shell_command("echo hi; echo err >&2", cwd=str(tmp_path)) == "hi\nerr"


@posix_only
@pytest.mark.usefixtures("posix_shell")
@pytest.mark.parametrize(
    ("stdout_bytes", "stderr_bytes"),
    [(12000, 12000), (40000, 40000), (SHELL_OUTPUT_LIMIT_BYTES * 3, 10)],
    ids=["each-under-limit", "each-over-limit", "stdout-only-over"],
)
async def test_stdout_and_stderr_share_one_output_limit(tmp_path: Path, stdout_bytes: int, stderr_bytes: int) -> None:
    out = await run_shell_command(_python_writes(stdout_bytes, stderr_bytes), cwd=str(tmp_path))
    dropped = stdout_bytes + stderr_bytes - SHELL_OUTPUT_LIMIT_BYTES

    body, _, footer = out.rpartition("\n")
    assert footer == f"[Output capture limit reached: {dropped} bytes from the middle were not kept.]"
    head, gap, tail = body.partition(f"\n[... {dropped} bytes omitted ...]\n")
    assert gap
    assert head.startswith("o")
    assert tail.endswith("e" * min(stderr_bytes, len(tail)))
    assert set(head + tail) <= {"o", "e"}
    # Head and tail together hold the limit, not a limit per stream.
    assert len(head) + len(tail) == SHELL_OUTPUT_LIMIT_BYTES
    # stdout comes first: no stdout byte follows a stderr byte.
    kept = head + tail
    assert "eo" not in kept


@posix_only
@pytest.mark.usefixtures("posix_shell")
async def test_output_exactly_at_the_limit_is_kept_whole(tmp_path: Path) -> None:
    half = SHELL_OUTPUT_LIMIT_BYTES // 2
    out = await run_shell_command(_python_writes(half, SHELL_OUTPUT_LIMIT_BYTES - half), cwd=str(tmp_path))
    assert out == "o" * half + "e" * (SHELL_OUTPUT_LIMIT_BYTES - half)
