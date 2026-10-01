# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Static simple-command tokens for SESSION reuse, with explicit dialect limits."""

from __future__ import annotations

import shlex
import string

_POSIX = {"bash", "sh", "dash", "zsh", "ksh", "git_bash"}
_WINDOWS = {"cmd", "powershell", "pwsh"}
_BARE = frozenset(string.ascii_letters + string.digits + "_./:+-= \t")
_RESERVED = {
    "if",
    "then",
    "else",
    "elif",
    "fi",
    "for",
    "while",
    "until",
    "do",
    "done",
    "case",
    "esac",
    "in",
    "function",
    "time",
    "coproc",
    "select",
    "foreach",
    "switch",
    "try",
    "catch",
    "finally",
    "trap",
    "begin",
    "process",
    "end",
    "class",
    "enum",
    "filter",
    "data",
    "workflow",
    "parallel",
    "sequence",
    "call",
    "rem",
    "goto",
    "return",
    "break",
    "continue",
    "exit",
    "repeat",
    "throw",
    "param",
    "using",
    "configuration",
}


def normalize_simple_command(command: object, shell: str) -> tuple[str, ...] | None:
    """Keep every ordered token; unsupported syntax returns to ordinary approval.

    POSIX supports static single/double quotes, but no escapes or expansion.
    Windows supports bare words/paths only; never feed Windows text to shlex.
    """
    if not isinstance(command, str) or not command or shell not in _POSIX | _WINDOWS:
        return None
    if shell in _WINDOWS:
        if any(char not in _BARE | {"\\"} for char in command):
            return None
        tokens = tuple(command.split())
        # PowerShell expression mode and dot-sourcing are not bare commands.
        if (
            shell != "cmd"
            and tokens
            and (tokens[0] == "." or tokens[0][0] in string.digits + "+-" or tokens[0].startswith("::"))
        ):
            return None
    else:
        quote = ""
        for index, char in enumerate(command):
            if char in "\r\n\0" or char == "\\":
                return None
            if quote:
                if char == quote:
                    quote = ""
                elif quote == '"' and char in "$`":
                    return None
            elif char in "'\"":
                quote = char
            elif char not in _BARE | {"@", "%", ","}:
                return None
            elif shell == "zsh" and char == "=" and (index == 0 or command[index - 1] in " \t"):
                # zsh expands an unquoted =command word via its command table.
                return None
        if quote:
            return None
        tokens = tuple(shlex.split(command, posix=True))
    if (
        not tokens
        or not tokens[0]
        or (tokens[0].lower() if shell in _WINDOWS else tokens[0]) in _RESERVED
        or "=" in tokens[0]
    ):
        return None
    return tokens
