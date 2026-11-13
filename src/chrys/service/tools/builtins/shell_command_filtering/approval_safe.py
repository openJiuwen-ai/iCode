# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval-safe shell command classification.

This module is the security boundary for skipping a shell approval. It is
deliberately stricter than the best-effort ``read_only`` shell filter:

- only a single simple POSIX command is considered;
- the command text must contain no shell operators, redirections, command or
  parameter substitution, unquoted globs/braces/tilde, or history characters;
- single-quoted text is literal and double-quoted text is accepted only when it
  contains no expansion (``$``, backtick, backslash or ``!``);
- the resulting argv is validated per command and, when auto-approved, executed
  without a shell so the validated argv is exactly what runs.

A command that does not satisfy every check must require approval. Windows
shells (pwsh, powershell, cmd) are intentionally not auto-approved yet: their
argument grammar needs a separate parser, and a wrong guess there would be a
security regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from chrys.service.tools.builtins.shell_command_filtering.readonly.commands import _is_safe_special_command

ExecutionMode = Literal["exec", "shell"]

_POSIX_SHELLS = frozenset({"", "bash", "dash", "fish", "git_bash", "ksh", "sh", "tcsh", "csh", "zsh"})
"""Shells whose argument grammar this module models. ``git_bash`` is POSIX-like."""

_MAX_COMMAND_CHARS = 32_768
"""Do not spend time validating an absurdly long command; approval is cheaper."""

_ALLOWED_PUNCTUATION = frozenset("-_./:=+,@%")
"""Characters that cannot change shell word splitting or trigger expansion/execution.

``%`` is literal in POSIX shells and is kept for ``printf``/``date``-style
formats. ``@``/``,``/``+`` are literal too. A path containing any other
punctuation (glob, brace, tilde, quote, backslash, ``$``, backtick, history or
control character) is not auto-approved.
"""

_APPROVAL_SAFE_EXTERNAL = frozenset(
    {
        # Filesystem inspection and text processing. Every command here reads
        # files or stdin only; redirections are rejected before classification.
        "basename",
        "cat",
        "comm",
        "cut",
        "df",
        "diff",
        "dirname",
        "du",
        "expr",
        "free",
        "grep",
        "head",
        "id",
        "jq",
        "ls",
        "md5sum",
        "ps",
        "realpath",
        "readlink",
        "seq",
        "sha256sum",
        "shasum",
        "stat",
        "tac",
        "tail",
        "tr",
        "uname",
        "uptime",
        "wc",
        "whoami",
    }
)
"""Commands whose every argument is read-only and cannot execute another program.

Deliberately absent: ``rg`` (``--pre`` executes a command), ``less``/``more``
(interactive ``!`` and ``LESSOPEN``), ``tree`` (``-o`` writes a file), ``date``
(``-s`` sets the clock), ``hostname`` (a bare argument sets the name), ``cd`` and
``type`` (shell builtins), and every command whose option surface is large enough
to need the validators in ``readonly.commands`` (git, sed, awk, find, env,
xargs, sort, uniq, python/node/curl version probes, ...).
"""

_POSIX_SHELL_BUILTINS = frozenset({"echo", "printf", "pwd", "test", "true", "false", "which"})
"""Safe builtins that must keep shell semantics (for example, ``echo -e``).

They are executed through the shell, not by direct exec. The command text still
passed the no-expansion check, and the approval-safe environment strips shell
startup hooks (``BASH_ENV``/``ENV``/exported functions), so no user code runs
before the builtin.
"""


@dataclass(frozen=True, slots=True)
class SafeShellCommand:
    """A command that may run without an approval dialog."""

    argv: tuple[str, ...]
    execution: ExecutionMode
    rule: str


@dataclass(frozen=True, slots=True)
class ApprovalShellDecision:
    """The approval decision for one shell command.

    ``safe`` is true only when ``command`` is set. ``reason`` is for the
    approval audit/debug log and never contains the command text.
    """

    command: SafeShellCommand | None = None
    reason: str = ""

    @property
    def safe(self) -> bool:
        return self.command is not None


def classify_shell_for_approval(command: str, *, shell_name: str = "") -> ApprovalShellDecision:
    """Classify *command* for the approval auto-approval boundary.

    The function is pure: it does not read settings, the filesystem or the
    environment. Approval and execution can therefore make the same decision
    without a time-of-check/time-of-use gap.
    """
    if shell_name not in _POSIX_SHELLS:
        return ApprovalShellDecision(reason=f"shell_not_supported:{shell_name or 'unknown'}")

    argv = _simple_posix_argv(command)
    if argv is None:
        return ApprovalShellDecision(reason="shell_syntax")

    name = argv[0]
    args = list(argv[1:])
    if name in _APPROVAL_SAFE_EXTERNAL:
        return ApprovalShellDecision(
            command=SafeShellCommand(argv=argv, execution="exec", rule=f"unconditional:{name}")
        )
    if name in _POSIX_SHELL_BUILTINS:
        return ApprovalShellDecision(command=SafeShellCommand(argv=argv, execution="shell", rule=f"builtin:{name}"))
    if _is_safe_special_command(name, args):
        return ApprovalShellDecision(command=SafeShellCommand(argv=argv, execution="exec", rule=f"validator:{name}"))
    return ApprovalShellDecision(reason=f"not_approval_safe:{name}")


def _simple_posix_argv(command: str) -> tuple[str, ...] | None:
    """Return argv for a single simple POSIX command, or ``None``.

    Rejecting is the safe answer: approval is always available, while a missed
    expansion is an unapproved command execution. Quotes are handled explicitly:
    single quotes make every character literal; double quotes may not contain
    anything the shell would expand or escape.
    """
    if not command or len(command) > _MAX_COMMAND_CHARS:
        return None

    argv: list[str] = []
    current: list[str] = []
    quoting: Literal["none", "single", "double"] = "none"
    saw_token = False

    for char in command:
        if quoting == "single":
            if char == "'":
                quoting = "none"
                continue
            if char in "\r\n":
                return None
            current.append(char)
            continue

        if quoting == "double":
            if char == '"':
                quoting = "none"
                continue
            # Double quotes still expand $, backticks and history, and process
            # backslash escapes; none of them belong in an auto-approved word.
            if char in "$`\\!":
                return None
            if char in "\r\n":
                return None
            current.append(char)
            continue

        if char in " \t":
            if saw_token:
                if not current:
                    return None
                argv.append("".join(current))
                current = []
                saw_token = False
            continue
        if char == "'":
            quoting = "single"
            saw_token = True
            continue
        if char == '"':
            quoting = "double"
            saw_token = True
            continue
        if char.isalnum() and char.isprintable():
            current.append(char)
            saw_token = True
            continue
        if char in _ALLOWED_PUNCTUATION:
            current.append(char)
            saw_token = True
            continue
        # Everything else (backslash, $, backtick, globs, braces, brackets,
        # tilde, operators, redirections, history, control characters) is a
        # shell feature that this boundary does not model.
        return None

    if quoting != "none":
        return None
    if saw_token:
        if not current:
            return None
        argv.append("".join(current))
    return tuple(argv) or None


__all__ = [
    "ApprovalShellDecision",
    "ExecutionMode",
    "SafeShellCommand",
    "classify_shell_for_approval",
]
