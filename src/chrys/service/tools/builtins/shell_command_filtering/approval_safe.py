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
from chrys.service.tools.builtins.shell_command_filtering.readonly.git import _strip_git_global_options

ExecutionMode = Literal["exec", "shell"]

_POSIX_SHELLS = frozenset({"bash", "dash", "git_bash", "ksh", "sh", "zsh"})
"""Shells whose argument grammar this module models. ``git_bash`` is POSIX-like.

``fish``/``csh``/``tcsh`` are deliberately absent: fish treats backslash as an
escape inside single quotes, and csh/tcsh expand history there. Their grammar
does not match :func:`_simple_posix_argv`, so they require approval. An empty
name is unknown, not POSIX.
"""

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
        "ls",
        "md5sum",
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
(``-s`` sets the clock), ``hostname`` (a bare argument sets the name), ``jq``
(``env``/``$ENV`` reads the environment, and ``-f``/``-L`` read files), ``ps``
(some options print another process's environment), ``go version`` (the Go
toolchain may download a module-selected version), ``cd`` and ``type`` (shell
builtins), and every command whose option surface is large enough to need the
validators in ``readonly.commands`` (git, sed, awk, find, env, xargs, sort,
uniq, python/node/curl version probes, ...).
"""

_POSIX_SHELL_BUILTINS = frozenset({"echo", "printf", "pwd", "test", "true", "false", "which"})
"""Safe builtins that must keep shell semantics (for example, ``echo -e``).

They are executed through the shell, not by direct exec. The command text still
passed the no-expansion check. The approval-safe environment strips the
repository-injectable startup hooks (``BASH_ENV``/``ENV``/exported functions,
and ``ZDOTDIR`` for zsh), so a repository cannot run code before the builtin.
zsh still reads the system and user startup files ``/etc/zshenv`` and
``$HOME/.zshenv``; those are trusted user configuration, not repository input.
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


def _git_option_present(args: list[str], names: frozenset[str]) -> bool:
    """Return whether one of *names* is an option before the first ``--``."""
    for arg in args:
        if arg == "--":
            return False
        if arg in names:
            return True
    return False


def simple_command_argv(command: str, *, shell_name: str = "") -> tuple[str, ...] | None:
    """Return a simple command's argv, independent of whether it can be auto-approved.

    Sensitive-target checks need the executor's words even when the command name
    is not on the approval-safe allowlist. Only the shell syntax and quoting
    rules are applied here; command policy is the classifier's next step.
    """
    if shell_name not in _POSIX_SHELLS:
        return None
    argv = _simple_posix_argv(command)
    if argv is None:
        return None
    if shell_name == "zsh" and any(arg.startswith("=") for arg in argv):
        return None
    return argv


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
    if shell_name == "zsh" and any(arg.startswith("=") for arg in argv):
        # zsh expands a word starting with ``=`` to a command path, even after
        # quotes; the parsed argv is not what zsh would execute.
        return ApprovalShellDecision(reason="zsh_equals_expansion")

    name = argv[0]
    args = list(argv[1:])
    if name in _APPROVAL_SAFE_EXTERNAL:
        return _safe_command(argv, execution="exec", rule=f"unconditional:{name}", shell_name=shell_name)
    if name in _POSIX_SHELL_BUILTINS:
        return _safe_command(argv, execution="shell", rule=f"builtin:{name}", shell_name=shell_name)
    if name == "git":
        # Strip global options first: stacked flags must not hide a ``config``
        # subcommand from the guard. The sanitized Git environment hides
        # global/system config; letting it auto-approve would silently show an
        # empty view, so read it under approval instead.
        stripped = _strip_git_global_options(args)
        if stripped is not None:
            subcommand = stripped[0] if stripped else ""
            if subcommand == "config":
                return ApprovalShellDecision(reason="not_approval_safe:git-config")
            if subcommand == "status":
                # Worktree comparisons can run clean/process filters from
                # attributes and config; read them under approval.
                return ApprovalShellDecision(reason="not_approval_safe:git-worktree")
            if subcommand == "diff" and not _git_option_present(stripped[1:], frozenset({"--cached", "--staged"})):
                # Only index/tree diffs are filter-free; a worktree diff is not.
                return ApprovalShellDecision(reason="not_approval_safe:git-worktree")
    if name == "env":
        # ``env`` with no child prints the environment, and ``env NAME=`` still
        # dumps it. The read-only filter keeps its best-effort ``env`` rule, but
        # this security boundary never auto-approves it.
        return ApprovalShellDecision(reason="not_approval_safe:env")
    if name == "pytest":
        # Which pytest runs comes from PATH. pytest 8 loads plugins and conftest
        # even for ``--version``; only pytest 9 skips that. Require approval.
        return ApprovalShellDecision(reason="not_approval_safe:pytest")
    if name == "xargs":
        # GNU xargs has optional-value options and a child-command suffix whose
        # parsing differs by implementation; keep it behind approval.
        return ApprovalShellDecision(reason="not_approval_safe:xargs")
    if name == "find":
        # BSD/GNU find parse leading options with getopt, so a path can be
        # attached to a combined short option, and predicate arguments can look
        # like actions. A flat token scan cannot reliably separate paths,
        # predicate values, and actions; keep find behind approval.
        return ApprovalShellDecision(reason="not_approval_safe:find")
    if name in ("pip", "pip3"):
        # pip's effective behavior depends on config files and environment
        # (find-links, isolated mode, logging) that this boundary cannot pin;
        # keep every pip invocation behind approval.
        return ApprovalShellDecision(reason="not_approval_safe:pip")
    if _is_safe_special_command(name, args):
        return _safe_command(argv, execution="exec", rule=f"validator:{name}", shell_name=shell_name)
    return ApprovalShellDecision(reason=f"not_approval_safe:{name}")


def _safe_command(
    argv: tuple[str, ...], *, execution: ExecutionMode, rule: str, shell_name: str
) -> ApprovalShellDecision:
    """Build a safe decision, keeping Git Bash on its shell for tool lookup.

    Git Bash's ``ls``/``cat``/``stat`` live in its own ``usr/bin`` and are not
    necessarily on the parent process ``PATH``. The command text still passed
    the no-expansion check, so running it through Git Bash preserves the old
    lookup semantics without reopening shell expansion.
    """
    if shell_name == "git_bash":
        execution = "shell"
    return ApprovalShellDecision(command=SafeShellCommand(argv=argv, execution=execution, rule=rule))


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
    "simple_command_argv",
]
