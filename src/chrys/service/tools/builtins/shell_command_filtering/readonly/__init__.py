# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read-only shell command analysis for approval auto-bypass."""

from __future__ import annotations

import re

from chrys.service.tools.builtins.shell_command_filtering.readonly.commands import _is_safe_special_command
from chrys.service.tools.builtins.shell_command_filtering.readonly.syntax import (
    _has_unsafe_redirection,
    _subshell_targets,
)
from chrys.service.tools.builtins.shell_command_filtering.tokenizer import (
    _SHELL_OPERATORS,
    _has_unquoted_for_shell,
    _operator_tokens,
    _ShellToken,
)

# ---------------------------------------------------------------------------
# Safe read-only detection (for approval auto-bypass)
# ---------------------------------------------------------------------------

_SAFE_READONLY_COMMANDS = frozenset(
    {
        # filesystem navigation (read-only)
        "ls",
        "pwd",
        "cd",
        "realpath",
        "readlink",
        "basename",
        "dirname",
        # file viewing (read-only)
        "cat",
        "tac",
        "head",
        "tail",
        "wc",
        "stat",
        # text processing (read-only — redirections blocked separately)
        "grep",
        "cut",
        "tr",
        "diff",
        "comm",
        # system info (read-only)
        "uname",
        "whoami",
        "printenv",
        "which",
        "type",
        "id",
        "ps",
        "df",
        "du",
        "free",
        "uptime",
        "arch",
        "md5sum",
        "sha256sum",
        "shasum",
        # safe utilities
        "echo",
        "printf",
        "true",
        "false",
        "test",
        "expr",
        "seq",
        "jq",
        # Windows cmd read-only
        "dir",
        "fc",
        "findstr",
        "ver",
        "where",
        # PowerShell read-only cmdlets
        "Compare-Object",
        "Get-ChildItem",
        "Get-Content",
        "Get-Item",
        "Get-ItemProperty",
        "Get-Location",
        "Get-Module",
        "Get-PSDrive",
        "Get-Process",
        "Get-Service",
        "Get-Command",
        "Get-Help",
        "Get-Alias",
        "Get-Date",
        "Get-Host",
        "Get-Variable",
        "Get-Member",
        "Test-Path",
        "Resolve-Path",
        "Split-Path",
        "Join-Path",
        "Select-String",
        "Select-Object",
        "Where-Object",
        "Sort-Object",
        "Group-Object",
        "Measure-Object",
        "Out-String",
        "Out-Null",
        "Format-Table",
        "Format-List",
        "ConvertFrom-Json",
        "ConvertTo-Json",
        "Write-Output",
        "Write-Host",
    }
)
"""Strictly safe read-only commands for approval auto-bypass.

Commands in this set are accepted after the shared syntax checks pass.
Commands that can mutate state, execute arbitrary code, or need narrower
argument handling are excluded from this unconditional allow-list:

- ``git`` — push, commit, checkout, reset can modify repo; selected
  read-only subcommands are allowed by a command-specific validator
- ``env`` — can execute an arbitrary child program; only bare env
  inspection forms are allowed by a command-specific validator
- ``python``, ``node``, etc. — arbitrary code execution
- ``curl``, ``wget`` — can POST data or overwrite files
- ``sed``, ``awk`` — in-place edit (``-i``) or internal output redirection;
  narrow print forms are allowed by command-specific validators
- ``xargs`` — executes arbitrary commands
- ``yq`` — ``-i`` flag modifies files in-place
- ``ForEach-Object`` — PowerShell script blocks can run destructive cmdlets

Commands such as ``env``, ``find`` and ``git`` are handled by command-specific
validators in ``readonly.commands``.
"""

_DANGEROUS_ARGUMENTS = frozenset(
    {
        # Flags that trigger destructive behavior
        "-delete",
        "--delete",
        # Destructive commands (may appear as args to find -exec, etc.)
        "rm",
        "rmdir",
        "mv",
        "cp",
        "mkdir",
        "touch",
        "chmod",
        "chown",
        "chgrp",
        "ln",
        "tee",
        "kill",
        "pkill",
        "killall",
        "dd",
        "shred",
        "truncate",
        "mkfs",
        # PowerShell destructive cmdlets
        "Remove-Item",
        "Move-Item",
        "Copy-Item",
        "Rename-Item",
        "New-Item",
        "Set-Content",
        "Add-Content",
        "Clear-Content",
        "Out-File",
        "Stop-Process",
        "Start-Process",
        "Invoke-Expression",
        "Invoke-Command",
    }
)
"""Tokens that indicate system-altering intent when found anywhere in a command.

Used as a second layer of defence alongside ``_SAFE_READONLY_COMMANDS``.
If *any* token in the full command matches this set, approval is required
even when all command names are individually safe.  This catches patterns
like ``find . -delete`` and ``find . -exec rm {} \\;``.
"""

_COMMAND_SEPARATORS = frozenset({"|", "|&", "&&", "||", "&", ";"})
_POSIX_FOR_SHELLS = frozenset({"", "bash", "dash", "git_bash", "sh", "zsh"})
_POSIX_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_POSIX_CONTROL_WORDS = frozenset({"for", "while", "until", "case", "if", "then", "else", "elif", "fi", "do", "done"})
_SAFE_ENV_ASSIGNMENT_NAMES = frozenset({"CLICOLOR", "CLICOLOR_FORCE", "LANG", "LANGUAGE", "NO_COLOR", "TERM", "TZ"})
_SAFE_ENV_ASSIGNMENT_PREFIXES = ("LC_",)
_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=.*$")


def _is_unquoted_token(token: _ShellToken, text: str) -> bool:
    return not token.quoted and token.text == text


def _command_token_segments_from_tokens(tokens: list[_ShellToken]) -> list[list[_ShellToken]]:
    """Split shell tokens into simple-command token groups."""
    segments: list[list[_ShellToken]] = []
    current: list[_ShellToken] = []
    for token in tokens:
        if not token.quoted and token.text in _COMMAND_SEPARATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)

    if current:
        segments.append(current)
    return segments


def _is_env_assignment_token(token: _ShellToken) -> bool:
    return not token.quoted and _ENV_ASSIGNMENT_RE.fullmatch(token.text) is not None


def _is_safe_env_assignment_token(token: _ShellToken) -> bool:
    match = _ENV_ASSIGNMENT_RE.fullmatch(token.text) if not token.quoted else None
    if match is None:
        return False
    name = match.group(1)
    return name in _SAFE_ENV_ASSIGNMENT_NAMES or name.startswith(_SAFE_ENV_ASSIGNMENT_PREFIXES)


def _segment_command_and_args(segment: list[_ShellToken]) -> tuple[str, list[str]] | None:
    idx = 0
    while idx < len(segment) and _is_env_assignment_token(segment[idx]):
        if not _is_safe_env_assignment_token(segment[idx]):
            return None
        idx += 1
    if idx >= len(segment):
        return None
    return segment[idx].text, [token.text for token in segment[idx + 1 :]]


def _tokens_are_safe_readonly(tokens: list[_ShellToken]) -> bool:
    segments = _command_token_segments_from_tokens(tokens)
    if not segments:
        return False

    for segment in segments:
        parsed = _segment_command_and_args(segment)
        if parsed is None:
            return False
        cmd, args = parsed
        if cmd in _SAFE_READONLY_COMMANDS:
            continue
        if not _is_safe_special_command(cmd, args):
            return False

    return not any(token.text in _DANGEROUS_ARGUMENTS for token in tokens)


def _has_powershell_script_block(tokens: list[_ShellToken]) -> bool:
    return any(not token.quoted and ("{" in token.text or "}" in token.text) for token in tokens)


def _has_shell_parenthesized_execution(tokens: list[_ShellToken]) -> bool:
    return any(not token.quoted and token.text in {"(", ")"} for token in tokens)


def _safe_posix_for_header(tokens: list[_ShellToken], header_end: int) -> bool:
    if header_end <= 3:
        return False
    if tokens[1].quoted or _POSIX_IDENTIFIER_RE.fullmatch(tokens[1].text) is None:
        return False
    if not _is_unquoted_token(tokens[2], "in"):
        return False
    return not any((not token.quoted and token.text in _SHELL_OPERATORS) for token in tokens[3:header_end])


def _is_safe_simple_posix_for(tokens: list[_ShellToken], *, shell_name: str) -> bool:
    if shell_name not in _POSIX_FOR_SHELLS or len(tokens) < 7 or not _is_unquoted_token(tokens[0], "for"):
        return False

    header_end = 3
    while header_end < len(tokens) and not _is_unquoted_token(tokens[header_end], ";"):
        header_end += 1
    if header_end >= len(tokens) or header_end + 1 >= len(tokens):
        return False
    if not _safe_posix_for_header(tokens, header_end) or not _is_unquoted_token(tokens[header_end + 1], "do"):
        return False

    body_start = header_end + 2
    done_index: int | None = None
    for idx in range(body_start + 1, len(tokens)):
        if _is_unquoted_token(tokens[idx], "done") and _is_unquoted_token(tokens[idx - 1], ";"):
            done_index = idx
            break

    if done_index is None or done_index - 1 <= body_start:
        return False

    body_tokens = tokens[body_start : done_index - 1]
    if any(not token.quoted and token.text in _POSIX_CONTROL_WORDS for token in body_tokens):
        return False
    if not _tokens_are_safe_readonly(body_tokens):
        return False

    trailing_tokens = tokens[done_index + 1 :]
    if trailing_tokens and not _tokens_are_safe_readonly(trailing_tokens):
        return False

    return not any(token.text in _DANGEROUS_ARGUMENTS for token in tokens)


def is_safe_readonly_command(command: str, *, shell_name: str = "") -> bool:
    """Check whether *command* consists entirely of safe read-only operations.

    Returns ``True`` only when ALL of the following hold:

    1. No unsafe redirections. Output to the shell's null device and fd
       duplication/closure are allowed; normal file redirections are not.
    2. No subshells or command substitutions (``$(...)``, backticks).
    3. Every individual command name (after splitting on pipes, ``&&``,
       ``||``, ``;``) is in the ``_SAFE_READONLY_COMMANDS`` set.
    4. No token in the full command matches ``_DANGEROUS_ARGUMENTS``
       (catches destructive flags like ``-delete`` and destructive
       commands like ``rm`` appearing as arguments).

    This is a best-effort analysis, not a security boundary: it models shell
    syntax only well enough to answer "does this look read-only?", not "can
    this text become an executable option after expansion?". Approval uses
    ``classify_shell_for_approval`` in ``approval_safe.py``; do not use this
    function to skip a user approval.
    """
    # Reject redirections that can touch normal files. Harmless fd/null
    # redirections such as ``2>/dev/null`` and ``2>&1`` are allowed.
    if _has_unsafe_redirection(command, shell_name=shell_name) is not None:
        return False

    # Reject subshells / command substitutions using shell-specific syntax.
    subshell_targets = _subshell_targets(shell_name)
    if subshell_targets and _has_unquoted_for_shell(command, subshell_targets, shell_name=shell_name) is not None:
        return False

    try:
        tokens = _operator_tokens(command, shell_name=shell_name)
    except ValueError:
        return False  # malformed quoting — not safe to auto-approve

    if _has_shell_parenthesized_execution(tokens):
        return False

    if shell_name in ("pwsh", "powershell") and _has_powershell_script_block(tokens):
        return False

    if tokens and _is_unquoted_token(tokens[0], "for"):
        return _is_safe_simple_posix_for(tokens, shell_name=shell_name)

    return _tokens_are_safe_readonly(tokens)
