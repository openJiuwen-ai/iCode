# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Predefined shell command filter presets."""

from __future__ import annotations

from chrys.service.tools.builtins.shell_command_filtering.policy import ShellCommandFilter

# ---------------------------------------------------------------------------
# Predefined filter presets
# ---------------------------------------------------------------------------

_READONLY_COMMANDS = frozenset(
    {
        # filesystem navigation
        "ls",
        "find",
        "tree",
        "pwd",
        "cd",
        "realpath",
        "readlink",
        "basename",
        "dirname",
        "dir",
        # file viewing
        "cat",
        "tac",
        "head",
        "tail",
        "less",
        "more",
        "wc",
        "file",
        "stat",
        # text processing (read-only use)
        "grep",
        "rg",
        "awk",
        "sed",
        "sort",
        "uniq",
        "cut",
        "tr",
        "diff",
        "comm",
        "xargs",
        # system info
        "uname",
        "whoami",
        "date",
        "env",
        "printenv",
        "which",
        "type",
        "id",
        "hostname",
        "ps",
        "df",
        "du",
        "free",
        "uptime",
        "arch",
        "md5sum",
        "sha256sum",
        "shasum",
        # version control
        "git",
        # language tools
        "python3",
        "python",
        "node",
        "npm",
        "npx",
        "cargo",
        "go",
        "java",
        "ruby",
        "pip",
        "pip3",
        "uv",
        "ruff",
        "mypy",
        "pytest",
        "dotnet",
        # misc
        "echo",
        "printf",
        "true",
        "false",
        "test",
        "expr",
        "seq",
        "jq",
        "yq",
        "curl",
        "wget",
        # PowerShell cmdlets (read-only)
        "Get-ChildItem",
        "Get-Content",
        "Get-Item",
        "Get-ItemProperty",
        "Get-Location",
        "Set-Location",
        "Select-String",
        "Select-Object",
        "Where-Object",
        "ForEach-Object",
        "Sort-Object",
        "Group-Object",
        "Measure-Object",
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

READONLY = ShellCommandFilter(
    whitelist=_READONLY_COMMANDS,
    allow_redirections=False,
    allow_subshells=False,
)

UNRESTRICTED = ShellCommandFilter()

_PRESETS: dict[str, ShellCommandFilter] = {
    "read_only": READONLY,
    "unrestricted": UNRESTRICTED,
}


def get_preset(name: str) -> ShellCommandFilter | None:
    """Look up a predefined filter by name."""
    return _PRESETS.get(name)


def preset_names() -> tuple[str, ...]:
    """Return every name :func:`get_preset` recognizes, in declaration order.

    A profile naming anything else must be rejected where it is read: the
    builder ignores an unknown name, which leaves every command allowed.
    """
    return tuple(_PRESETS)
