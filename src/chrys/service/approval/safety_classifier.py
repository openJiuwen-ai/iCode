# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safety classification helpers for approval gating."""

from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

_SENSITIVE_TARGET_RE = re.compile(
    r"""(?ix)
    (^|[\s'"`=:/\\*?])
    (
        \.env(?:\.(?!example|sample|template)\w[\w.-]*)?
        | \.envrc
        | proc[/\\](?:self|thread-self|\d+)[/\\]environ
        | \.git[/\\](?:config(?:\.worktree)?|hooks)
        | \.git-credentials
        | \.config[/\\]git[/\\]credentials
        | \.aws[/\\]credentials
        | aws[_-]?credentials
        | \.azure[/\\](?:accessTokens\.json|access_tokens\.json|msal_token_cache\.(?:json|bin)|service_principal_entries\.json)
        | \.config[/\\]gcloud[/\\]application_default_credentials\.json
        | \.config[/\\]gh[/\\]hosts\.ya?ml
        | \.kube[/\\]config
        | \.ssh[/\\](?:id_rsa|id_ed25519|id_ecdsa|id_dsa)
        | id_rsa|id_ed25519|id_ecdsa|id_dsa
        | \.npmrc|\.pypirc|\.netrc
        | \.docker[/\\]config\.json
        | credentials(?:\.json|\.ini|\.ya?ml)?
        | cookies(?:\.sqlite|\.txt)?
        | login\.keychain(?:-db)?|keychain(?:-db)?
        | bank[_-]?account|credit[_-]?card|card[_-]?number|payment[_-]?method
        | crypto[_-]?wallet|wallet\.dat
    )
    (?=$|[\s'"`;|&<>/\\*?])
    """
)
_ENV_DUMP_RE = re.compile(
    r"""(?ix)
    (^|[;&|\r\n]\s*)
    (
        printenv(?:$|[\s;&|])
        | env(?:$|[\s;&|](?![A-Za-z_][A-Za-z0-9_]*=))
        | env(?:\s+[A-Za-z_][A-Za-z0-9_]*=(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|(?:\\.|[^\s;&|])+))+\s*(?:$|[;&|])
        | env(?:\s+[A-Za-z_][A-Za-z0-9_]*=(?:"(?:\\.|[^"])*"|'(?:\\.|[^'])*'|(?:\\.|[^\s;&|])+))+\s+(?:env|printenv)(?:$|[\s;&|])
    )
    """
)
_POWERSHELL_ENV_ACCESS_RE = re.compile(
    r"""(?ix)
    (
        (^|[;&|\r\n]\s*)
        (?:Get-ChildItem|gci|dir|ls|Get-Item|gi|Get-Content|gc|cat|type)\b
        (?:(?![;&|]).)*\sEnv:(?:[\w:*?./\\-]+)?
        (?=$|[\s;&|])
        | \$Env:[A-Za-z_][A-Za-z0-9_]*
        | \$\{Env:[A-Za-z_][A-Za-z0-9_]*\}
    )
    """
)
_SENSITIVE_ENV_NAME_RE = re.compile(
    r"""(?ix)
    (^|_)
    (
        api[_-]?key
        | access[_-]?key
        | secret
        | token
        | password
        | passwd
        | credential
        | credentials
        | private[_-]?key
        | session
        | cookie
        | auth
    )
    (_|$)
    """
)
_WINDOWS_ENV_REFERENCE_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_POSIX_ENV_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*")
_POSIX_COMMAND_OPERATORS = frozenset({";", "&", "&&", "|", "|&", "||", "\n"})
_POSIX_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "tcsh", "csh"})
_POSIX_SHELL_LONG_OPTIONS_WITH_VALUES = frozenset({"--init-file", "--rcfile"})
_POSIX_SHELL_SHORT_OPTIONS_WITH_VALUES = frozenset({"-O", "-o", "+O", "+o"})
_SUDO_OPTIONS_WITH_VALUES = frozenset(
    {
        "-C",
        "-c",
        "-D",
        "-g",
        "-h",
        "-p",
        "-R",
        "-r",
        "-T",
        "-t",
        "-U",
        "-u",
        "--askpass",
        "--chdir",
        "--close-from",
        "--group",
        "--host",
        "--login-class",
        "--prompt",
        "--role",
        "--type",
        "--user",
    }
)
_ENV_OPTIONS_WITH_VALUES = frozenset({"-u", "--unset", "-C", "--chdir"})
_MAX_SHELL_INDIRECTION_ANALYSIS_CHARS = 8192
_MAX_SHELL_SENSITIVE_RECURSION = 3


def target_may_access_sensitive_data(value: str) -> bool:
    """Return True when a command or path targets credentials/account data."""
    return bool(_SENSITIVE_TARGET_RE.search(value))


def _env_name_may_be_sensitive(name: str) -> bool:
    """Return True for environment variable names likely to contain secrets."""
    return bool(_SENSITIVE_ENV_NAME_RE.search(name))


def _posix_command_references_sensitive_env(command: str) -> bool:
    """Return True when POSIX shell syntax expands a sensitive env var."""
    in_single = False
    in_double = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if char != "$" or in_single:
            index += 1
            continue

        if index + 1 < len(command) and command[index + 1] == "{":
            match = re.match(r"\{([A-Za-z_][A-Za-z0-9_]*)", command[index + 1 :])
            if match is not None and _env_name_may_be_sensitive(match.group(1)):
                return True
            index += 1
            continue

        match = re.match(r"[A-Za-z_][A-Za-z0-9_]*", command[index + 1 :])
        if match is not None and _env_name_may_be_sensitive(match.group(0)):
            return True
        index += 1

    return False


def _windows_command_references_sensitive_env(command: str) -> bool:
    """Return True when cmd-style syntax expands a sensitive env var."""
    return any(_env_name_may_be_sensitive(match.group(1)) for match in _WINDOWS_ENV_REFERENCE_RE.finditer(command))


def _read_posix_parenthesized_payload(command: str, start: int) -> tuple[str, int] | None:
    """Return payload/end index for a POSIX ``$(...)`` or ``<(...)`` body."""
    depth = 1
    in_single = False
    in_double = False
    escaped = False
    index = start
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if not in_single and char == "(":
            depth += 1
        elif not in_single and char == ")":
            depth -= 1
            if depth == 0:
                return command[start:index], index
        index += 1
    return None


def _read_posix_backtick_payload(command: str, start: int) -> tuple[str, int] | None:
    """Return payload/end index for a POSIX backtick substitution."""
    escaped = False
    payload: list[str] = []
    index = start
    while index < len(command):
        char = command[index]
        if escaped:
            payload.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\":
            escaped = True
            index += 1
            continue
        if char == "`":
            return "".join(payload), index
        payload.append(char)
        index += 1
    return None


def _posix_substitution_payloads(command: str) -> list[str]:
    """Return POSIX command/process substitution payloads that will execute."""
    payloads: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if in_single:
            index += 1
            continue
        if command.startswith(("$(", "<("), index):
            payload = _read_posix_parenthesized_payload(command, index + 2)
            if payload is not None:
                body, end = payload
                payloads.append(body)
                index = end + 1
                continue
            return payloads
        if char == "`":
            payload = _read_posix_backtick_payload(command, index + 1)
            if payload is not None:
                body, end = payload
                payloads.append(body)
                index = end + 1
                continue
            return payloads
        index += 1
    return payloads


def _posix_newlines_as_operators(command: str) -> str:
    """Return command text with unquoted newlines normalized to semicolons."""
    normalized: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            normalized.append(char)
            escaped = False
            index += 1
            continue
        if char == "\\" and not in_single:
            normalized.append(char)
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            normalized.append(char)
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            normalized.append(char)
            index += 1
            continue
        if char in "\r\n" and not in_single and not in_double:
            normalized.append(" ; ")
            if char == "\r" and index + 1 < len(command) and command[index + 1] == "\n":
                index += 2
            else:
                index += 1
            continue
        normalized.append(char)
        index += 1
    return "".join(normalized)


def _posix_command_segments(command: str) -> list[list[str]]:
    """Split a POSIX command into coarse token segments separated by operators."""
    try:
        lexer = shlex.shlex(_posix_newlines_as_operators(command), posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _POSIX_COMMAND_OPERATORS:
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _posix_env_command_target_index(segment: list[str], index: int) -> int:
    """Return index after an ``env`` wrapper and its options/assignments."""
    index += 1
    while index < len(segment):
        token = segment[index]
        if _POSIX_ENV_ASSIGNMENT_RE.fullmatch(token):
            index += 1
            continue
        if token in _ENV_OPTIONS_WITH_VALUES:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    return index


def _posix_command_builtin_target_index(segment: list[str], index: int) -> int:
    """Return index after a POSIX ``command`` wrapper."""
    index += 1
    while index < len(segment):
        token = segment[index]
        if token == "--":
            return index + 1
        if token.startswith("-"):
            index += 1
            continue
        break
    return index


def _sudo_option_consumes_value(option: str) -> bool:
    """Return True for sudo options whose argument is the next token."""
    option_name = option.split("=", 1)[0]
    if option_name in _SUDO_OPTIONS_WITH_VALUES:
        return "=" not in option
    return (
        option.startswith("-")
        and not option.startswith("--")
        and option[-1:] in {"C", "c", "D", "g", "h", "p", "R", "r", "T", "t", "U", "u"}
    )


def _posix_sudo_target_index(segment: list[str], index: int) -> int:
    """Return index after a ``sudo`` wrapper and its options."""
    index += 1
    while index < len(segment):
        token = segment[index]
        if token == "--":
            index += 1
            break
        if _POSIX_ENV_ASSIGNMENT_RE.fullmatch(token):
            index += 1
            continue
        if token.startswith("-"):
            index += 2 if _sudo_option_consumes_value(token) else 1
            continue
        break

    while index < len(segment) and _POSIX_ENV_ASSIGNMENT_RE.fullmatch(segment[index]):
        index += 1
    return index


def _posix_unwrapped_command_index(segment: list[str]) -> int:
    """Return the command index after leading env/command/sudo wrappers."""
    index = 0
    while index < len(segment):
        while index < len(segment) and _POSIX_ENV_ASSIGNMENT_RE.fullmatch(segment[index]):
            index += 1
        if index >= len(segment):
            return index

        wrapper_name = os.path.basename(segment[index])
        if wrapper_name == "env":
            index = _posix_env_command_target_index(segment, index)
            continue
        if wrapper_name == "command":
            index = _posix_command_builtin_target_index(segment, index)
            continue
        if wrapper_name == "sudo":
            index = _posix_sudo_target_index(segment, index)
            continue
        return index
    return index


def _posix_shell_c_payloads(command: str) -> list[tuple[str, str]]:
    """Return ``(shell_name, payload)`` for POSIX shell ``-c`` invocations."""
    payloads: list[tuple[str, str]] = []
    for segment in _posix_command_segments(command):
        index = _posix_unwrapped_command_index(segment)
        if index >= len(segment):
            continue

        shell_name = os.path.basename(segment[index])
        if shell_name not in _POSIX_SHELL_NAMES:
            continue

        option_index = index + 1
        while option_index < len(segment):
            option = segment[option_index]
            if option == "-c" or (option.startswith("-") and not option.startswith("--") and "c" in option[1:]):
                if option_index + 1 < len(segment):
                    payloads.append((shell_name, segment[option_index + 1]))
                break
            if option == "--":
                break
            if option in _POSIX_SHELL_SHORT_OPTIONS_WITH_VALUES:
                option_index += 2
                continue
            if option.startswith("--"):
                option_name = option.split("=", 1)[0]
                option_index += 1 if "=" in option or option_name not in _POSIX_SHELL_LONG_OPTIONS_WITH_VALUES else 2
                continue
            if not option.startswith("-"):
                break
            option_index += 1
    return payloads


def _posix_command_indirection_may_access_sensitive_data(command: str, *, shell_name: str, depth: int) -> bool:
    """Return True if shell indirection executes sensitive env access."""
    if depth >= _MAX_SHELL_SENSITIVE_RECURSION:
        return False
    if len(command) > _MAX_SHELL_INDIRECTION_ANALYSIS_CHARS:
        return True
    for payload in _posix_substitution_payloads(command):
        if _shell_command_may_access_sensitive_data(payload, shell_name=shell_name, depth=depth + 1):
            return True
    for nested_shell_name, payload in _posix_shell_c_payloads(command):
        if _shell_command_may_access_sensitive_data(payload, shell_name=nested_shell_name, depth=depth + 1):
            return True
    return False


def _shell_command_may_access_sensitive_data(command: str, *, shell_name: str, depth: int) -> bool:
    """Recursive implementation for shell sensitive-data classification."""
    powershell_env_access = shell_name in ("pwsh", "powershell") and _POWERSHELL_ENV_ACCESS_RE.search(command)
    posix_env_access = shell_name in ("", "bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "git_bash")
    windows_env_access = shell_name in ("", "cmd")
    return bool(
        _ENV_DUMP_RE.search(command)
        or powershell_env_access
        or (posix_env_access and _posix_command_references_sensitive_env(command))
        or (
            posix_env_access
            and _posix_command_indirection_may_access_sensitive_data(command, shell_name=shell_name, depth=depth)
        )
        or (windows_env_access and _windows_command_references_sensitive_env(command))
        or target_may_access_sensitive_data(command)
    )


def shell_command_may_access_sensitive_data(command: str, *, shell_name: str = "") -> bool:
    """Return True when a read-only-looking shell command should still be judged."""
    return _shell_command_may_access_sensitive_data(command, shell_name=shell_name, depth=0)


def _parsed_args(raw_args: Any) -> dict[str, Any]:
    """Return dict-like tool arguments when possible."""
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            parsed_args = json.loads(raw_args)
        except json.JSONDecodeError, TypeError:
            return {}
        return parsed_args if isinstance(parsed_args, dict) else {}
    try:
        return dict(raw_args) if raw_args else {}
    except TypeError, ValueError:
        return {}


def path_arg_may_access_sensitive_data(raw_args: Any) -> bool:
    """Return True when tool arguments include a sensitive path target."""
    parsed_args = _parsed_args(raw_args)
    path = parsed_args.get("path", "") if isinstance(parsed_args, dict) else ""
    return isinstance(path, str) and target_may_access_sensitive_data(path)


def shell_arg_may_access_sensitive_data(raw_args: Any, *, shell_name: str = "") -> bool:
    """Return True when shell tool arguments access sensitive data."""
    parsed_args = _parsed_args(raw_args)
    command = parsed_args.get("command", "") if isinstance(parsed_args, dict) else ""
    return isinstance(command, str) and shell_command_may_access_sensitive_data(command, shell_name=shell_name)
