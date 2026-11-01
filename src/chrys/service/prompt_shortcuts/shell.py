# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run a prompt shortcut's ``!`command``` placeholders and splice in their output.

opencode runs each ``!`command``` in the shell when the command is sent and
replaces it with what the command printed, a non-zero exit included. iCode
does the same at send time, without the one line end the output finishes
with, and bounds every command: it is given
:data:`SHELL_TIMEOUT_SECONDS` and its output, stdout then stderr, is cut to
the head and tail of :data:`SHELL_OUTPUT_LIMIT_BYTES` in all. A command that cannot start or runs out of
time fails the whole interpolation, so nothing is sent with a hole in it.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from chrys.foundation.platform.output_capture import (
    BoundedCapture,
    CapturedOutput,
    capture_limit_footer,
    drain_process_pipes,
)
from chrys.foundation.platform.process import (
    SubprocessStoppedError,
    WindowsShellCommand,
    managed_subprocess,
    wait_for_subprocess,
)
from chrys.foundation.platform.runtime_paths import build_shell_subprocess_env
from chrys.foundation.text.tool_output import process_carriage_returns, strip_ansi

SHELL_TIMEOUT_SECONDS = 10.0
SHELL_OUTPUT_LIMIT_BYTES = 16 * 1024
SHELL_CONCURRENCY = 2
"""Commands run at once, as in opencode."""

_SHELL_RE = re.compile(r"!`([^`]+)`")


class ShellInterpolationError(Exception):
    """A ``!`command``` that could not run to completion.

    *reason* is ``timeout``, ``stopped`` or ``start_failed``.
    """

    def __init__(self, command: str, reason: str, detail: str = "") -> None:
        super().__init__(f"{command}: {reason} {detail}".strip())
        self.command = command
        self.reason = reason
        self.detail = detail


def shell_commands(text: str) -> list[str]:
    """Return the commands of *text*'s ``!`command``` placeholders, in order."""
    return [match.group(1) for match in _SHELL_RE.finditer(text)]


def shell_argv(command: str) -> tuple[str | WindowsShellCommand, ...]:
    # The user's shell: ``$SHELL`` (``/bin/sh``), or ``cmd.exe`` given the command as
    # written, quotes included (``echo x > "a b"``).
    if sys.platform == "win32":
        return (WindowsShellCommand(command),)
    return (os.environ.get("SHELL") or "/bin/sh", "-c", command)


async def run_shell_command(command: str, *, cwd: str) -> str:
    """Run *command* in the user's shell from *cwd* and return its output, stdout then stderr."""
    stdout = BoundedCapture(SHELL_OUTPUT_LIMIT_BYTES)
    stderr = BoundedCapture(SHELL_OUTPUT_LIMIT_BYTES)
    try:
        async with managed_subprocess(
            *shell_argv(command),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            # As the shell tool's: the user's own commands find the user's tools, never the
            # packaged runtime's, and print plain text for the message.
            env=build_shell_subprocess_env(),
        ) as proc:
            await wait_for_subprocess(
                drain_process_pipes(proc, stdout, stderr),
                timeout=SHELL_TIMEOUT_SECONDS,
                process_group_id=proc.pid if sys.platform != "win32" else None,
            )
    except TimeoutError as exc:
        raise ShellInterpolationError(command, "timeout") from exc
    except SubprocessStoppedError as exc:
        raise ShellInterpolationError(command, "stopped") from exc
    except OSError as exc:
        raise ShellInterpolationError(command, "start_failed", exc.strerror or "") from exc
    except ValueError as exc:
        # Arguments no process can be given: a NUL, or text the OS cannot encode.
        raise ShellInterpolationError(command, "start_failed", str(exc)) from exc
    combined = _combined(stdout.snapshot(), stderr.snapshot())
    # Like a shell's ``$(…)``, the line end the output finishes with is not part
    # of it: ``Last commit: !`git log -1``` stays on one line.
    # As every other reader of a command's output: colors and progress redraws
    # (``ls --color``, a carriage-return spinner) never reach the message.
    output = _without_final_newline(combined.text(_clean))
    footer = capture_limit_footer([combined])
    if not footer:
        return output
    # The note about the cut goes on a line of its own, never glued to the output.
    note = footer.lstrip("\n")
    return f"{output}\n{note}" if output else note


def _combined(*captures: CapturedOutput) -> CapturedOutput:
    """One capture of the streams in order, within :data:`SHELL_OUTPUT_LIMIT_BYTES` in all.

    Each stream was cut to the limit on its own; cutting what they kept, joined,
    with the same head/tail split keeps every gap inside the new one: a stream
    that lost its middle kept a full limit, all of it head or tail here.
    """
    joined = BoundedCapture(SHELL_OUTPUT_LIMIT_BYTES)
    for capture in captures:
        joined.feed(capture.head + capture.tail)
    kept = joined.snapshot()
    return CapturedOutput(kept.head, kept.tail, sum(capture.seen for capture in captures))


def _clean(text: str) -> str:
    return process_carriage_returns(strip_ansi(text))


def _without_final_newline(text: str) -> str:
    if text.endswith("\r\n"):
        return text[:-2]
    return text.removesuffix("\n")


async def interpolate_shell(text: str, *, cwd: str) -> str:
    """Replace every ``!`command``` in *text* with its output; raise :class:`ShellInterpolationError` on a failure."""
    commands = shell_commands(text)
    if not commands:
        return text
    limit = asyncio.Semaphore(SHELL_CONCURRENCY)

    async def run(command: str) -> str:
        async with limit:
            return await run_shell_command(command, cwd=cwd)

    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(run(command)) for command in commands]
    except* ShellInterpolationError as failures:
        # The first failure stands for the rest; the group cancelled the others.
        raise failures.exceptions[0] from None
    outputs = iter([task.result() for task in tasks])
    return _SHELL_RE.sub(lambda _match: next(outputs), text)
