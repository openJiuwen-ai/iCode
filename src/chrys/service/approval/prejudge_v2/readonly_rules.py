# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Declarative shell policy matchers; no legacy command filter dependencies."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


def validate_match(raw: object, *, depth: int = 0) -> None:
    """Reject unknown matchers and malformed policy values at load time."""
    if depth > 8 or not isinstance(raw, dict) or len(raw) != 1:
        raise ValueError("A policy match must contain exactly one supported field")
    field, value = next(iter(raw.items()))
    if field in {"all", "any"}:
        if not isinstance(value, list) or not 1 <= len(value) <= 64:
            raise ValueError(f"{field} expects 1..64 match expressions")
        for part in value:
            validate_match(part, depth=depth + 1)
        return
    if field == "cli":
        if not isinstance(value, dict) or not value or "command" not in value:
            raise ValueError("cli expects an object containing command")
        supported = {
            "command",
            "subcommand",
            "subcommand_in",
            "global_flags",
            "global_values",
            "require_globals",
            "flags",
            "value_flags",
            "flag_prefixes",
            "short_patterns",
            "short_clusters",
            "subargs_in",
            "subargs_prefix",
            "require_flags",
            "require_any_flags",
            "forbid_flags",
            "forbid_prefixes",
            "forbid_short_cluster",
            "positionals_with_flag",
            "allow_any_options",
            "allow_positionals",
        }
        if set(value) - supported or type(value["command"]) is not str or not value["command"]:
            raise ValueError("Invalid cli command or unknown cli property")
        for k, v in value.items():
            if k == "command":
                continue
            if k in {"allow_any_options", "allow_positionals"}:
                if type(v) is not bool:
                    raise ValueError(f"{k} must be boolean")
            elif k in {"subcommand", "short_clusters", "forbid_short_cluster"}:
                if type(v) is not str or not v:
                    raise ValueError(f"{k} must be a string")
            elif k in {"subargs_in"}:
                if not isinstance(v, list) or any(
                    not isinstance(x, list) or any(type(y) is not str for y in x) for x in v
                ):
                    raise ValueError(f"{k} expects lists of strings")
            elif k in {"subargs_prefix"}:
                if not isinstance(v, list) or any(type(x) is not str for x in v):
                    raise ValueError(f"{k} expects strings")
            elif not isinstance(v, list) or len(v) > 256 or any(type(x) is not str or not x for x in v):
                raise ValueError(f"{k} expects a list of strings")
        if "short_patterns" in value:
            for pattern in value["short_patterns"]:
                if len(pattern) > 128:
                    raise ValueError("CLI regex too long")
                re.compile(pattern)
        return
    if field == "argv_in":
        if not isinstance(value, list) or not 1 <= len(value) <= 1024:
            raise ValueError("argv_in expects a nonempty list of argv lists")
        if any(
            not isinstance(seq, list) or not seq or any(type(t) is not str or not t for t in seq)
            for seq in value
        ):
            raise ValueError("argv_in expects nonempty string vectors")
        return
    if field == "argument_at_regex":
        if (
            not isinstance(value, list)
            or len(value) != 2
            or type(value[0]) is not int
            or value[0] < 1
            or type(value[1]) is not str
        ):
            raise ValueError("argument_at_regex expects [one-based-index, pattern]")
        re.compile(value[1])
        return
    if field == "args_regex":
        if type(value) is not str or len(value) > 256:
            raise ValueError("args_regex expects a short string pattern")
        re.compile(value)
        return
    if field == "args_max_positionals":
        if type(value) is not int or value < 0 or value > 100:
            raise ValueError("args_max_positionals expects a nonnegative integer")
        return
    if field in {"command_in", "args_none_of", "args_prefix_none_of", "env_names_allowed"}:
        if (
            not isinstance(value, list)
            or not 1 <= len(value) <= 256
            or any(type(x) is not str or not x for x in value)
        ):
            raise ValueError(f"{field} expects nonempty string tokens")
        return
    raise ValueError(f"Unsupported structured matcher: {field!r}")


def _cli_matches(rule: Mapping[str, Any], argv: Sequence[str]) -> bool:
    if not argv or argv[0] != rule["command"]:
        return False
    remaining = list(argv[1:])
    consumed_globals: list[str] = []
    while remaining and remaining[0].startswith("-"):
        flag = remaining[0]
        if flag in rule.get("global_flags", ()):
            consumed_globals.append(flag)
            remaining.pop(0)
        elif flag in rule.get("global_values", ()):
            if len(remaining) < 2:
                return False
            consumed_globals.append(flag)
            del remaining[:2]
        elif any(flag.startswith(f"{opt}=") for opt in rule.get("global_values", ()) if opt.startswith("--")):
            consumed_globals.append(flag.split("=", 1)[0])
            remaining.pop(0)
        else:
            return False
    if not set(rule.get("require_globals", ())).issubset(consumed_globals):
        return False
    if not remaining:
        return False
    sub = remaining.pop(0)
    if "subcommand" in rule and sub != rule["subcommand"]:
        return False
    if "subcommand_in" in rule and sub not in rule["subcommand_in"]:
        return False
    if "subargs_in" in rule and remaining not in rule["subargs_in"]:
        return False
    if "subargs_prefix" in rule and (not remaining or remaining[0] not in rule["subargs_prefix"]):
        return False
    if "subargs_in" in rule and not any(
        k in rule
        for k in (
            "flags", "flag_prefixes", "value_flags", "short_patterns",
            "forbid_flags", "forbid_prefixes", "require_flags",
            "require_any_flags", "allow_any_options", "positionals_with_flag",
        )
    ):
        return True
    if rule.get("allow_any_options", False):
        options = []
        for arg in remaining:
            if arg == "--":
                break
            if arg.startswith("-"):
                options.append(arg)
        if any(
            arg in rule.get("forbid_flags", ())
            or any(arg.startswith(prefix) for prefix in rule.get("forbid_prefixes", ()))
            for arg in options
        ):
            return False
        if rule.get("forbid_short_cluster") and any(
            arg.startswith("-")
            and not arg.startswith("--")
            and any(c in rule["forbid_short_cluster"] for c in arg[1:])
            for arg in options
        ):
            return False
    else:
        allowed = set(rule.get("flags", ()))
        value_flags = set(rule.get("value_flags", ()))
        prefixes = tuple(rule.get("flag_prefixes", ()))
        short = set(rule.get("short_clusters", ""))
        short_patterns = tuple(re.compile(p) for p in rule.get("short_patterns", ()))
        i = 0
        while i < len(remaining):
            arg = remaining[i]
            if arg == "--":
                break
            if arg in allowed:
                i += 1
            elif arg in value_flags:
                if i + 1 >= len(remaining):
                    return False
                i += 2
            elif arg.startswith("-") and prefixes and arg.startswith(prefixes):
                i += 1
            elif arg.startswith("-") and any(p.fullmatch(arg) for p in short_patterns):
                i += 1
            elif (
                arg.startswith("-")
                and not arg.startswith("--")
                and len(arg) > 2
                and short
                and all(c in short for c in arg[1:])
            ):
                i += 1
            elif arg.startswith("-"):
                return False
            else:
                i += 1
    option_args = remaining[: remaining.index("--") if "--" in remaining else len(remaining)]
    opts = [x for x in option_args if x.startswith("-")]
    if not set(rule.get("require_flags", ())).issubset(opts):
        return False
    if rule.get("require_any_flags") and not set(rule["require_any_flags"]).intersection(opts):
        return False
    if "positionals_with_flag" in rule and not set(rule["positionals_with_flag"]).intersection(opts):
        # Permit a no-positional read, e.g. 'git branch' or 'git tag'.
        if any(x != "--" and not x.startswith("-") for x in remaining):
            return False
    if rule.get("allow_positionals", True) is False and any(
        x != "--" and not x.startswith("-") for x in remaining
    ):
        return False
    return True


def matches(raw: Mapping[str, Any], argv: Sequence[str], *, env_names: Sequence[str] = ()) -> bool:
    """Evaluate one compiled structural condition on AST-derived command words."""
    field, value = next(iter(raw.items()))
    if field == "all":
        return all(matches(p, argv, env_names=env_names) for p in value)
    if field == "any":
        return any(matches(p, argv, env_names=env_names) for p in value)
    if field == "command_in":
        return bool(argv) and argv[0] in value
    if field == "argv_in":
        return list(argv) in value
    if field == "args_none_of":
        return not set(argv[1:]).intersection(value)
    if field == "args_prefix_none_of":
        return not any(a.startswith(p) for a in argv[1:] for p in value)
    if field == "args_regex":
        return all(re.fullmatch(value, arg) is not None for arg in argv[1:])
    if field == "argument_at_regex":
        i, pattern = value
        return len(argv) > i and re.fullmatch(pattern, argv[i]) is not None
    if field == "args_max_positionals":
        return sum(not arg.startswith("-") for arg in argv[1:]) <= value
    if field == "env_names_allowed":
        return all(
            name in value or any(name.startswith(p[:-1]) for p in value if p.endswith("*"))
            for name in env_names
        )
    if field == "cli":
        return _cli_matches(value, argv)
    raise ValueError(f"Unsupported matcher {field!r}")
