# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Safety classification helpers for approval gating."""

from __future__ import annotations

import json
import os
import re
import shlex
from collections.abc import Iterable, Sequence
from typing import Any

_SENSITIVE_TARGET_RE = re.compile(
    r"""(?ix)
    (^|[\s'"`=:/\\])
    (
        \.env(?:\.(?!example|sample|template)\w[\w.-]*)?
        | \.envrc
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
    (?=$|[\s'"`;|&<>/\\])
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
POSIX_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish", "tcsh", "csh"})
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
_CARRIER_OPTIONS = {
    "command": ({"-p", "-v", "-V"}, set()),
    "env": ({"-i", "--ignore-environment", "-0", "--null"}, _ENV_OPTIONS_WITH_VALUES),
    "nice": (set(), {"-n", "--adjustment"}),
    "sudo": (
        {
            "-A",
            "--askpass",
            "-b",
            "--background",
            "-E",
            "--preserve-env",
            "-H",
            "--set-home",
            "-n",
            "--non-interactive",
            "-P",
            "--preserve-groups",
            "-S",
            "--stdin",
        },
        _SUDO_OPTIONS_WITH_VALUES - {"--askpass"},
    ),
    "timeout": ({"--foreground", "--preserve-status", "-v", "--verbose"}, {"-s", "--signal", "-k", "--kill-after"}),
    "xargs": (
        {"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose", "-x", "--exit"},
        {"-d", "--delimiter", "-n", "--max-args", "-P", "--max-procs", "-s", "--max-chars"},
    ),
}


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


def _read_posix_parenthesized_payload(command: str, start: int, *, decoded_carriers: bool) -> tuple[str, int] | None:
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
        delimiter = not in_single and (not decoded_carriers or not in_double)
        if delimiter and char == "(":
            depth += 1
        elif delimiter and char == ")":
            depth -= 1
            if depth == 0:
                return command[start:index], index
        index += 1
    return None


def _read_posix_backtick_payload(command: str, start: int, *, decoded_carriers: bool) -> tuple[str, int] | None:
    """Return payload/end index for a POSIX backtick substitution."""
    escaped = False
    payload: list[str] = []
    index = start
    while index < len(command):
        char = command[index]
        if escaped:
            if decoded_carriers and char not in "\\$`":
                payload.append("\\")
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


def _posix_source_parts(command: str, *, decoded_carriers: bool) -> tuple[list[str], list[str | None]]:
    """Find substitutions and, for Pre-Judge, split at unquoted command boundaries."""
    segments: list[str] = []
    payloads: list[str | None] = []
    in_single = False
    in_double = False
    escaped = False
    at_word_start = True
    index = 0
    start = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            if char != "\n":
                at_word_start = False
            index += 1
            continue
        if char == "\\" and not in_single:
            escaped = True
            index += 1
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            at_word_start = False
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            at_word_start = False
            index += 1
            continue
        if in_single:
            index += 1
            continue
        process_start = (
            not in_double and command.startswith(("<(", ">("), index)
            if decoded_carriers
            else command.startswith("<(", index)
        )
        parenthesized = command.startswith("$(", index) or process_start
        if parenthesized:
            payload = _read_posix_parenthesized_payload(command, index + 2, decoded_carriers=decoded_carriers)
        elif char == "`":
            payload = _read_posix_backtick_payload(command, index + 1, decoded_carriers=decoded_carriers)
        else:
            payload = None
        if payload is not None:
            body, end = payload
            payloads.append(body)
            at_word_start = False
            index = end + 1
            continue
        if parenthesized or char == "`":
            if decoded_carriers:
                payloads.append(None)
            break

        if decoded_carriers and not in_double:
            if char == "#" and at_word_start:
                segments.append(command[start:index])
                end = command.find("\n", index)
                index = len(command) if end == -1 else end + 1
                start = index
                continue
            if char in ";|\r\n" or (
                char == "&" and command[index - 1 : index] not in {"<", ">"} and command[index + 1 : index + 2] != ">"
            ):
                segments.append(command[start:index])
                start = index + 1
            at_word_start = char in " \t\r\n;|&<>"
        index += 1
    if decoded_carriers:
        segments.append(command[start:index])
    return segments, payloads


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


def _posix_env_command_target_index(segment: Sequence[str], index: int) -> int:
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


def _posix_command_builtin_target_index(segment: Sequence[str], index: int) -> int:
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


def _posix_sudo_target_index(segment: Sequence[str], index: int) -> int:
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


def _carrier_target_index(segment: Sequence[str], index: int, carrier: str) -> int | None:
    """Return the command index, len(segment) for a known no-command form, or None."""
    flags, values = _CARRIER_OPTIONS[carrier]
    index += 1
    while index < len(segment) and segment[index].startswith("-"):
        option = segment[index]
        index += 1
        if option == "--":
            break
        if carrier != "command" and option in {"--help", "--version"}:
            return len(segment)
        if (carrier == "nice" and option[1:].isdecimal()) or (carrier == "env" and option == "-"):
            continue
        if option == "-":
            index -= 1
            break

        value = None
        takes_value = False
        if option.startswith("--"):
            name, separator, attached = option.partition("=")
            if name in values:
                takes_value = True
                value = attached if separator else None
            elif option not in flags:
                return None
        else:
            for end, char in enumerate(option[1:], start=2):
                name = "-" + char
                if name in values:
                    takes_value = True
                    value = option[end:] if end < len(option) else None
                    break
                if name not in flags:
                    return None
                if carrier == "command" and name in {"-v", "-V"}:
                    return len(segment)
        if takes_value:
            if value is None:
                if index >= len(segment):
                    return None
                value = segment[index]
                index += 1
            if not value or value == "--":
                return None

    if carrier in {"env", "sudo"}:
        while index < len(segment) and _POSIX_ENV_ASSIGNMENT_RE.fullmatch(segment[index]):
            index += 1
        if index >= len(segment):
            return None
    if carrier == "timeout":
        if index + 1 >= len(segment) or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)[smhd]?", segment[index]):
            return None
        index += 1
    # xargs has a fixed default command (echo). nice and command are inert without a command.
    return index


def normalize_executable_name(word: str) -> str:
    """Group potential carriers across filesystems, including case-sensitive ones."""
    return word.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")


def _is_potential_carrier(head: str) -> bool:
    return head in _CARRIER_OPTIONS or head in POSIX_SHELL_NAMES or head in {"eval", "printenv"}


def _is_shell_builtin(segment: Sequence[str], index: int, name: str) -> bool:
    """Require the exact builtin spelling in the shell or after its command builtin."""
    return segment[index] == name and all(
        word in {"command", "--"} or re.fullmatch(r"-p+", word) for word in segment[:index]
    )


def _posix_unwrapped_command_index(segment: Sequence[str], *, decoded_carriers: bool) -> int | None:
    """Return the executable position after recognized wrappers."""
    index = 0
    while index < len(segment):
        while index < len(segment) and _POSIX_ENV_ASSIGNMENT_RE.fullmatch(segment[index]):
            index += 1
        if index >= len(segment):
            return index

        word = segment[index]
        wrapper_name = normalize_executable_name(word) if decoded_carriers else os.path.basename(word)
        if decoded_carriers and wrapper_name in _CARRIER_OPTIONS:
            if wrapper_name == "command" and not _is_shell_builtin(segment, index, "command"):
                return None
            target = _carrier_target_index(segment, index, wrapper_name)
            if target is None:
                return None
            if target < len(segment) and segment[target].startswith(("$", "`")):
                return None
            # An external wrapper executes its command operand, even if it contains '='.
            if target < len(segment) and _POSIX_ENV_ASSIGNMENT_RE.fullmatch(segment[target]):
                return target
            index = target
            continue
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


def _posix_shell_payloads(
    segments: Iterable[Sequence[str]], *, shell_name: str, decoded_carriers: bool
) -> list[tuple[str, str] | None]:
    """Extract arguments that a recognized command interprets as shell source."""
    payloads: list[tuple[str, str] | None] = []
    for segment in segments:
        index = _posix_unwrapped_command_index(segment, decoded_carriers=decoded_carriers)
        if index is None:
            payloads.append(None)
            continue
        if index >= len(segment):
            continue

        word = segment[index]
        nested_shell = normalize_executable_name(word) if decoded_carriers else os.path.basename(word)
        if decoded_carriers and nested_shell == "eval":
            if not _is_shell_builtin(segment, index, "eval"):
                payloads.append(None)
                continue
            args = segment[index + 1 :]
            if args and args[0] == "--":
                args = args[1:]
            if args:
                # The Bash eval builtin explicitly joins its arguments as source.
                payloads.append((shell_name, " ".join(args)))
            continue

        if nested_shell not in POSIX_SHELL_NAMES:
            continue

        option_index = index + 1
        while option_index < len(segment):
            option = segment[option_index]
            if option == "-c" or (option.startswith("-") and not option.startswith("--") and "c" in option[1:]):
                if option_index + 1 < len(segment):
                    payloads.append((nested_shell, segment[option_index + 1]))
                elif decoded_carriers:
                    payloads.append(None)
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


def _source_payloads_may_access_sensitive_data(
    payloads: Iterable[tuple[str, str] | None], *, depth: int, decoded_carriers: bool
) -> bool | None:
    unresolved = False
    for payload in payloads:
        if payload is None:
            unresolved = True
            continue
        nested_shell, command = payload
        sensitive = _shell_command_may_access_sensitive_data(
            command, shell_name=nested_shell, depth=depth, decoded_carriers=decoded_carriers
        )
        if sensitive is True:
            return True
        unresolved |= sensitive is None
    return None if unresolved else False


def _posix_command_indirection_may_access_sensitive_data(
    command: str, *, shell_name: str, depth: int, decoded_carriers: bool
) -> bool | None:
    """Return True if shell indirection executes sensitive env access."""
    if depth >= _MAX_SHELL_SENSITIVE_RECURSION:
        return None if decoded_carriers else False
    if len(command) > _MAX_SHELL_INDIRECTION_ANALYSIS_CHARS:
        return True
    source_segments, substitutions = _posix_source_parts(command, decoded_carriers=decoded_carriers)
    if decoded_carriers:
        segments: list[list[str]] = []
        for segment in source_segments:
            lexer = shlex.shlex(segment, posix=True, punctuation_chars="<>")
            lexer.whitespace_split = True
            lexer.commenters = ""
            words: list[str] = []
            try:
                words.extend(lexer)
            except ValueError:
                # Keep the known prefix so a malformed carrier is still unresolved.
                head = next((word for word in words if not _POSIX_ENV_ASSIGNMENT_RE.fullmatch(word)), "")
                if _is_potential_carrier(normalize_executable_name(head)):
                    substitutions.append(None)
            else:
                segments.append(words)
    else:
        segments = _posix_command_segments(command)
    payloads = _posix_shell_payloads(segments, shell_name=shell_name, decoded_carriers=decoded_carriers)
    payloads.extend(None if payload is None else (shell_name, payload) for payload in substitutions)
    return _source_payloads_may_access_sensitive_data(payloads, depth=depth + 1, decoded_carriers=decoded_carriers)


def _shell_command_may_access_sensitive_data(
    command: str, *, shell_name: str, depth: int, decoded_carriers: bool = False
) -> bool | None:
    """Recursive implementation for shell sensitive-data classification."""
    powershell_env_access = shell_name in ("pwsh", "powershell") and _POWERSHELL_ENV_ACCESS_RE.search(command)
    posix_env_access = shell_name in ("", "bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "csh", "git_bash")
    windows_env_access = shell_name in ("", "cmd")
    if (
        _ENV_DUMP_RE.search(command)
        or powershell_env_access
        or (posix_env_access and _posix_command_references_sensitive_env(command))
        or (windows_env_access and _windows_command_references_sensitive_env(command))
        or target_may_access_sensitive_data(command)
    ):
        return True
    if posix_env_access:
        return _posix_command_indirection_may_access_sensitive_data(
            command, shell_name=shell_name, depth=depth, decoded_carriers=decoded_carriers
        )
    return False


def shell_command_may_access_sensitive_data(command: str, *, shell_name: str = "") -> bool:
    """Return True when a read-only-looking shell command should still be judged."""
    return _shell_command_may_access_sensitive_data(command, shell_name=shell_name, depth=0) is True


def prejudge_source_may_access_sensitive_data(command: str, *, shell_name: str) -> bool | None:
    """Inspect source with Pre-Judge's carrier rules and preserve uncertainty."""
    return _shell_command_may_access_sensitive_data(
        command, shell_name=shell_name, depth=0, decoded_carriers=shell_name in {"bash", "git_bash"}
    )


def shell_argv_may_access_sensitive_data(
    argv: tuple[str, ...], *, shell_name: str, complete: bool = True
) -> bool | None:
    """Inspect decoded argv, returning None when a carrier's command cannot be located."""
    head = normalize_executable_name(argv[0]) if argv and shell_name in {"bash", "git_bash"} else ""
    if not complete:
        if _is_potential_carrier(head):
            return None
        return False
    for token in argv:
        if target_may_access_sensitive_data(token):
            return True
        if shell_name in {"powershell", "pwsh", "git_bash"}:
            # A drive prefix, provider qualifier or stream suffix may border a path component.
            normalized = "/".join(part.rstrip(" .") for part in re.split(r"[/:\\]", token))
            if target_may_access_sensitive_data(normalized):
                return True
    if head:
        if head == "printenv":
            return True
        if head == "env":
            target = next((arg for arg in argv[1:] if not _POSIX_ENV_ASSIGNMENT_RE.fullmatch(arg)), "")
            if not target or target.startswith("-") or normalize_executable_name(target) in {"env", "printenv"}:
                return True
        return _source_payloads_may_access_sensitive_data(
            _posix_shell_payloads((argv,), shell_name=shell_name, decoded_carriers=True),
            depth=0,
            decoded_carriers=True,
        )
    return False


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
