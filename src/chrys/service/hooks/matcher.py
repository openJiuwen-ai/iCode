# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Hook matcher — decides whether a given hook fires for a given payload.

Match rules are AND across the populated clauses; an empty match block
fires on every event of the hook's type.  Tool-related clauses
(``tool_kind`` / ``tool_name`` / ``args``) require a ``tool`` payload, so
they match only tool events.

Regex patterns from ``match.args.<name>.regex`` are compiled once per
process and cached on the :class:`HookArgMatch` instance.  The loader
already leaves out an enabled hook whose pattern does not compile and
reports it; a pattern that still fails here is logged once and treated
as a non-match, so one bad hook never crashes the whole event dispatch.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from chrys.service.hooks.retired_shell_args import matched_arg_name

if TYPE_CHECKING:
    from chrys.service.hooks.schema import HookArgMatch, HookConfig, HookMatch

logger = logging.getLogger(__name__)

REGEX_COMPILE_ERRORS: tuple[type[Exception], ...] = (re.error, OverflowError, RecursionError)
"""What ``re.compile`` raises for a pattern it cannot compile: ``re.error``,
plus ``OverflowError`` for an out-of-range repeat count and ``RecursionError``
for very deep nesting."""


def matches(hook: HookConfig, payload: dict[str, Any]) -> bool:
    """Return ``True`` if *hook* should fire for *payload*.

    *payload* is the dict that the manager will hand to the subprocess
    (see :mod:`chrys.service.hooks.events` for the envelope shape).  All fields
    are optional — the matcher inspects only what's present.
    """
    if not hook.enabled:
        return False
    return _match_clause(hook.match, payload)


def _match_clause(clause: HookMatch, payload: dict[str, Any]) -> bool:
    profile = payload.get("profile")

    if clause.profile is not None and profile != clause.profile:
        return False
    if clause.profiles and profile not in clause.profiles:
        return False

    tool = payload.get("tool") or {}
    if clause.tool_kind is not None and tool.get("kind") != clause.tool_kind:
        return False
    if clause.tool_name is not None and tool.get("name") != clause.tool_name:
        return False

    if clause.args:
        tool_args = tool.get("args") or {}
        if not isinstance(tool_args, dict):
            return False
        for name, arg_match in clause.args.items():
            # DEPRECATED: matched_arg_name reads a retired shell argument name as its new one.
            if not _match_arg(arg_match, tool_args.get(matched_arg_name(tool, name))):
                return False

    return True


def _match_arg(arg_match: HookArgMatch, value: Any) -> bool:
    """Apply a single :class:`HookArgMatch` to a value.

    All set clauses combine with AND, like :class:`HookMatch`.  When no
    clauses are set, the matcher requires the value to be present (the
    clause was written, after all — falsy "we just want the field" is
    spelled by setting ``equals``).
    """
    if value is None:
        return False
    text = value if isinstance(value, str) else str(value)

    if arg_match.equals is not None and text != arg_match.equals:
        return False
    if arg_match.contains is not None and arg_match.contains not in text:
        return False
    if arg_match.regex is not None:
        pattern = _get_compiled_regex(arg_match)
        if pattern is None or not pattern.search(text):
            return False
    return True


def _get_compiled_regex(arg_match: HookArgMatch) -> re.Pattern[str] | None:
    """Compile and cache ``arg_match.regex`` on the instance.

    Returns ``None`` if the pattern is invalid (logged once per pattern).
    The compiled regex is stored on private dataclass fields so equality /
    repr stay unaffected.
    """
    if arg_match._regex_compiled:
        return arg_match._compiled_regex

    try:
        compiled = re.compile(arg_match.regex or "")
    except REGEX_COMPILE_ERRORS as exc:
        logger.warning("Invalid regex in hook match: %r — %s", arg_match.regex, exc)
        compiled = None  # type: ignore[assignment]
    arg_match._compiled_regex = compiled
    arg_match._regex_compiled = True
    return compiled
