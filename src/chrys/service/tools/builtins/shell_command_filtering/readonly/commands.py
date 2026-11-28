# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Command-specific read-only validators."""

from __future__ import annotations

import re

from chrys.service.tools.builtins.shell_command_filtering.readonly.git import _is_safe_git

_VERSION_HELP_ARGS_BY_COMMAND = {
    "curl": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "dotnet": frozenset({("--version",), ("--info",), ("--list-sdks",), ("--list-runtimes",), ("--help",), ("-h",)}),
    "java": frozenset({("-version",), ("--version",), ("-help",), ("--help",), ("-h",)}),
    "mypy": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "node": frozenset({("--version",), ("-v",), ("--help",), ("-h",)}),
    "npm": frozenset({("--version",), ("-v",), ("--help",), ("-h",)}),
    "npx": frozenset({("--version",), ("-v",), ("--help",), ("-h",)}),
    "pip": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "pip3": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    # pytest --help/-h runs plugin and conftest loading; only the version probe is inert.
    "pytest": frozenset({("--version",), ("-V",)}),
    "python": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "python3": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "ruby": frozenset({("--version",), ("-v",), ("--help",), ("-h",)}),
    "ruff": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "uv": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "wget": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
    "yq": frozenset({("--version",), ("-V",), ("--help",), ("-h",)}),
}
_PIP_READONLY_SUBCOMMANDS = frozenset({"freeze", "show"})
_PIP_LIST_BLOCKED_ARGS = frozenset({"--outdated", "-o", "--uptodate", "-u"})
_PIP_LIST_BLOCKED_LONG_ARGS = frozenset({"--outdated", "--uptodate"})
_PIP_WRITE_OR_EXEC_OPTIONS = ("--log", "--log-file", "--python")
"""``--log*`` writes an arbitrary path; ``--python`` runs an arbitrary program."""
_RUFF_MUTATING_ARGS = frozenset({"--fix", "--fix-only", "--add-noqa", "--unsafe-fixes"})
_RUFF_MUTATING_PREFIXES = ("--fix=", "--fix-only=", "--add-noqa=", "--unsafe-fixes=")
_RUFF_NO_CACHE_ARGS = frozenset({"--no-cache", "-n"})
_RUFF_NON_FIXING_CHECK_ARGS = frozenset({"--no-fix", "--diff"})
_UNIQ_OPTIONS_NO_VALUE = frozenset(
    {
        "-c",
        "--count",
        "-d",
        "--repeated",
        "-D",
        "--all-repeated",
        "-i",
        "--ignore-case",
        "-u",
        "--unique",
        "-z",
        "--zero-terminated",
        "--help",
        "--version",
    }
)
_UNIQ_OPTIONS_WITH_VALUE = frozenset({"-f", "--skip-fields", "-s", "--skip-chars", "-w", "--check-chars"})
_PS_SAFE_ARGS = frozenset({(), ("aux",), ("-f",), ("-l",)})
"""``ps`` forms that only list process metadata; ``e``/``E``/``-o`` can print environments."""
_UNIQ_OPTION_PREFIXES_WITH_VALUE = ("-f", "-s", "-w", "--skip-fields=", "--skip-chars=", "--check-chars=")
_SED_PRINT_SCRIPT_RE = re.compile(r"^(?:\d+|\$)?(?:,(?:\d+|\$))?[p=]$")
_SED_SAFE_SUBSTITUTION_FLAGS = frozenset("0123456789gpiImM")
_AWK_FIELD_PRINT_SCRIPT_RE = re.compile(r"^\{\s*print\s+\$[0-9]+\s*\}$")
_FIND_BLOCKED_ACTIONS = frozenset({"-delete", "-execdir", "-ok", "-okdir"})
_FIND_BLOCKED_WRITE_ACTIONS = frozenset({"-fls", "-fprint", "-fprint0", "-fprintf"})
_FIND_EXEC_TERMINATORS = frozenset({";", "+"})
_READONLY_ARG_COMMANDS = frozenset({"cat", "grep", "head", "md5sum", "sha256sum", "shasum", "stat", "tail", "wc"})
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_XARGS_OPTIONS_NO_VALUE = frozenset({"-0", "--null", "-r", "--no-run-if-empty", "-t", "--verbose"})
_XARGS_OPTIONS_WITH_VALUE = frozenset(
    {
        "-d",
        "--delimiter",
        "-E",
        "--eof",
        "-I",
        "--replace",
        "-L",
        "--max-lines",
        "-n",
        "--max-args",
        "-P",
        "--max-procs",
        "-s",
        "--max-chars",
    }
)
_XARGS_OPTION_PREFIXES_WITH_VALUE = (
    "--delimiter=",
    "--eof=",
    "--max-args=",
    "--max-chars=",
    "--max-lines=",
    "--max-procs=",
    "--replace=",
)


def _has_arg_with_prefix(args: list[str], prefixes: tuple[str, ...]) -> bool:
    return any(any(arg.startswith(prefix) for prefix in prefixes) for arg in args)


def _is_safe_version_help_command(command: str, args: list[str]) -> bool:
    return tuple(args) in _VERSION_HELP_ARGS_BY_COMMAND.get(command, frozenset())


def _is_pip_blocked_option(arg: str) -> bool:
    """Return whether *arg* can write a file or launch another program through pip."""
    name = _option_name(arg)
    return name.startswith("--") and any(option.startswith(name) for option in _PIP_WRITE_OR_EXEC_OPTIONS)


def _is_safe_pip(args: list[str]) -> bool:
    if not args:
        return False
    subcommand = args[0]
    if any(_is_pip_blocked_option(arg) for arg in args[1:]):
        return False
    if subcommand == "list":
        return not any(
            arg in _PIP_LIST_BLOCKED_ARGS
            or _has_single_dash_option_char(arg, frozenset({"o", "u"}))
            or any(_is_long_option_abbreviation(arg, blocked) for blocked in _PIP_LIST_BLOCKED_LONG_ARGS)
            for arg in args[1:]
        )
    return subcommand in _PIP_READONLY_SUBCOMMANDS


def _is_safe_ruff(args: list[str]) -> bool:
    if _is_safe_version_help_command("ruff", args) or not args:
        return bool(args)

    subcommand = args[0]
    sub_args = args[1:]
    if (
        _has_ruff_mutating_arg(sub_args)
        or _has_ruff_output_file_arg(sub_args)
        or _has_arg_with_prefix(sub_args, _RUFF_MUTATING_PREFIXES)
    ):
        return False
    if subcommand == "check":
        return _has_any_ruff_arg(sub_args, _RUFF_NO_CACHE_ARGS) and _has_any_ruff_arg(
            sub_args, _RUFF_NON_FIXING_CHECK_ARGS
        )
    if subcommand == "format":
        return _has_any_ruff_arg(sub_args, frozenset({"--check", "--diff"})) and _has_any_ruff_arg(
            sub_args, _RUFF_NO_CACHE_ARGS
        )
    return False


def _option_name(arg: str) -> str:
    return arg.split("=", 1)[0]


def _is_long_option_abbreviation(arg: str, option: str) -> bool:
    name = _option_name(arg)
    return len(name) > 2 and name.startswith("--") and option.startswith(name)


def _has_single_dash_option_char(arg: str, option_chars: frozenset[str]) -> bool:
    """Return whether a single-dash short-option cluster contains any target option."""
    return arg.startswith("-") and not arg.startswith("--") and any(ch in option_chars for ch in arg[1:])


def _has_any_ruff_arg(args: list[str], values: frozenset[str]) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if arg in values:
            return True
    return False


def _has_ruff_mutating_arg(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if arg in _RUFF_MUTATING_ARGS:
            return True
    return False


def _has_ruff_output_file_arg(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            return False
        if _has_single_dash_option_char(arg, frozenset({"o"})):
            return True
        if arg == "--output-file" or arg.startswith("--output-file="):
            return True
    return False


def _is_sort_blocked_arg(arg: str) -> bool:
    if _sort_short_cluster_has_output_file(arg):
        return True
    return _is_long_option_abbreviation(arg, "--output") or _is_long_option_abbreviation(arg, "--compress-program")


def _sort_short_cluster_has_output_file(arg: str) -> bool:
    if not arg.startswith("-") or arg.startswith("--") or arg == "-":
        return False

    for ch in arg[1:]:
        if ch == "o":
            return True
        if ch in {"k", "t", "S", "T"}:
            return False
    return False


def _is_safe_sort(args: list[str]) -> bool:
    for arg in args:
        if arg == "--":
            return True
        if _is_sort_blocked_arg(arg):
            return False
    return True


def _is_safe_uniq(args: list[str]) -> bool:
    positionals = 0
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            positionals += len(args) - i - 1
            break
        if arg in _UNIQ_OPTIONS_NO_VALUE:
            i += 1
            continue
        if arg in _UNIQ_OPTIONS_WITH_VALUE:
            if i + 1 >= len(args):
                return False
            i += 2
            continue
        if any(arg.startswith(prefix) for prefix in _UNIQ_OPTION_PREFIXES_WITH_VALUE):
            i += 1
            continue
        if arg.startswith("-"):
            return False
        positionals += 1
        if positionals > 1:
            return False
        i += 1
    return positionals <= 1


def _is_safe_readonly_arg_command(command: str, args: list[str]) -> bool:
    if command not in _READONLY_ARG_COMMANDS:
        return False
    if command == "grep":
        return not any(arg in {"--label", "-mmap", "--mmap"} for arg in args)
    return True


def _consume_find_exec(args: list[str], start: int) -> int | None:
    command_start = start + 1
    end = command_start
    while end < len(args) and args[end] not in _FIND_EXEC_TERMINATORS:
        end += 1
    if end >= len(args) or end == command_start:
        return None

    exec_command = args[command_start]
    exec_args = [arg for arg in args[command_start + 1 : end] if arg != "{}"]
    if not _is_safe_readonly_arg_command(exec_command, exec_args):
        return None
    return end + 1


def _is_safe_find(args: list[str]) -> bool:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in _FIND_BLOCKED_ACTIONS or arg in _FIND_BLOCKED_WRITE_ACTIONS:
            return False
        if arg == "-exec":
            next_i = _consume_find_exec(args, i)
            if next_i is None:
                return False
            i = next_i
            continue
        i += 1
    return True


def _is_safe_env(args: list[str]) -> bool:
    return all(_ENV_ASSIGNMENT_RE.fullmatch(arg) is not None for arg in args)


def _is_safe_ps(args: list[str]) -> bool:
    """Only exact process-listing forms; environment/field options go to approval."""
    return tuple(args) in _PS_SAFE_ARGS


def _xargs_command_start(args: list[str]) -> int | None:
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            return i + 1
        if arg in _XARGS_OPTIONS_NO_VALUE:
            i += 1
            continue
        if arg in _XARGS_OPTIONS_WITH_VALUE:
            if i + 1 >= len(args):
                return None
            i += 2
            continue
        if any(arg.startswith(prefix) for prefix in _XARGS_OPTION_PREFIXES_WITH_VALUE):
            i += 1
            continue
        if arg.startswith("-"):
            return None
        return i
    return i


def _is_safe_xargs(args: list[str]) -> bool:
    command_start = _xargs_command_start(args)
    if command_start is None:
        return False
    if command_start >= len(args):
        return True
    return _is_safe_readonly_arg_command(args[command_start], args[command_start + 1 :])


def _consume_sed_section(script: str, start: int, delimiter: str) -> int | None:
    i = start
    while i < len(script):
        ch = script[i]
        if ch == "\\" and i + 1 < len(script):
            i += 2
            continue
        if ch == delimiter:
            return i + 1
        if ch in "\r\n":
            return None
        i += 1
    return None


def _is_safe_sed_substitution_script(script: str) -> bool:
    if len(script) < 4 or script[0] != "s":
        return False
    delimiter = script[1]
    if delimiter.isalnum() or delimiter.isspace() or delimiter in {"\\", "\r", "\n"}:
        return False

    replacement_start = _consume_sed_section(script, 2, delimiter)
    if replacement_start is None:
        return False
    flags_start = _consume_sed_section(script, replacement_start, delimiter)
    if flags_start is None:
        return False

    return all(flag in _SED_SAFE_SUBSTITUTION_FLAGS for flag in script[flags_start:])


def _is_safe_sed(args: list[str]) -> bool:
    scripts: list[str] = []
    saw_quiet = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-n", "--quiet", "--silent"}:
            saw_quiet = True
            i += 1
            continue
        if arg == "-e":
            if i + 1 >= len(args):
                return False
            scripts.append(args[i + 1])
            i += 2
            continue
        if arg.startswith("-e") and len(arg) > 2:
            scripts.append(arg[2:])
            i += 1
            continue
        if arg.startswith("-"):
            return False
        if scripts:
            break
        scripts.append(arg)
        i += 1
        break

    remaining_files = args[i:]
    if not scripts or any(file_arg.startswith("-") for file_arg in remaining_files):
        return False
    if saw_quiet:
        return all(_SED_PRINT_SCRIPT_RE.fullmatch(script) for script in scripts)
    return all(_is_safe_sed_substitution_script(script) for script in scripts)


def _is_safe_awk(args: list[str]) -> bool:
    if not args:
        return False
    script = args[0]
    return (
        _AWK_FIELD_PRINT_SCRIPT_RE.fullmatch(script) is not None
        and not script.startswith("-")
        and all(not file_arg.startswith("-") for file_arg in args[1:])
    )


def _is_safe_special_command(command: str, args: list[str]) -> bool:
    if command == "awk":
        return _is_safe_awk(args)
    if command == "env":
        return _is_safe_env(args)
    if command == "find":
        return _is_safe_find(args)
    if command == "git":
        return _is_safe_git(args)
    if command in {"pip", "pip3"}:
        return _is_safe_version_help_command(command, args) or _is_safe_pip(args)
    if command == "ps":
        return _is_safe_ps(args)
    if command == "ruff":
        return _is_safe_ruff(args)
    if command == "sed":
        return _is_safe_sed(args)
    if command == "sort":
        return _is_safe_sort(args)
    if command == "uniq":
        return _is_safe_uniq(args)
    if command == "xargs":
        return _is_safe_xargs(args)
    return _is_safe_version_help_command(command, args)
