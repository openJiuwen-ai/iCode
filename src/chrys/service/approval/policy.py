# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Approval policy engine — determines whether a tool call needs user approval."""

from __future__ import annotations

import logging
from enum import Enum
from typing import TYPE_CHECKING, Literal, cast

from chrys.foundation.tool_approval import declares_requires_approval
from chrys.foundation.tool_kinds import KIND_WEB_FETCH, KIND_WEB_SEARCH, TOOL_KINDS, get_tool_kind

if TYPE_CHECKING:
    from chrys.kernel import FunctionTool
    from chrys.service.profiles.agents.schema import ApprovalConfig

logger = logging.getLogger(__name__)

type ApprovalRule = Literal["auto", "require", "skip"]

_APPROVAL_RULES = frozenset({"auto", "require", "skip"})

# These kinds arrived after profiles could already name MCP tools called web_search
# or web_fetch with a bare key, so the key keeps matching the tool name as well as the
# kind: an existing override must never stop applying.
_KIND_AND_TOOL_NAME_KEYS = frozenset({KIND_WEB_SEARCH, KIND_WEB_FETCH})


def _fail_closed_rule(value: object, *, source: str) -> ApprovalRule:
    """Normalize trusted config types while failing closed on invalid runtime data."""
    if isinstance(value, str) and value in _APPROVAL_RULES:
        return cast("ApprovalRule", value)
    logger.error("Invalid approval rule %r for %s; requiring approval", value, source)
    return "require"


def _split_kind_tool_override(key: str, known_kinds: set[str]) -> tuple[str, str] | None:
    """Split ``kind.tool_name`` using the longest known kind prefix."""
    for kind in sorted(known_kinds, key=len, reverse=True):
        prefix = f"{kind}."
        if key.startswith(prefix) and len(key) > len(prefix):
            return kind, key[len(prefix) :]
    return None


def _warn_shadowed_tool_name_override(key: str, tools: list[FunctionTool] | None, *, in_toolset: bool) -> None:
    """Warn when a kind-valued override key shadows a tool with that literal name.

    Tool kinds are reserved words in the override-key namespace: a bare key
    equal to a kind never resolves as a tool-name override, so a tool
    literally named like a kind (e.g. an unprefixed MCP tool ``context``) can
    only be targeted via the ``kind.tool_name`` form (``mcp.context``).
    """
    shadowed = next((t for t in tools or [] if t.name == key), None)
    if shadowed is None:
        return
    shadowed_kind = get_tool_kind(shadowed) or ""
    if shadowed_kind == key:
        # Canonical tools such as web_search intentionally share their name
        # with their kind. The bare spelling is natural and unambiguous there.
        return
    hint = f" Use '{shadowed_kind}.{key}' to target the tool by name." if shadowed_kind else ""
    if in_toolset:
        logger.warning(
            "Approval override key %r is a tool kind and applies to all tools of that kind, "
            "not to the tool named %r.%s",
            key,
            key,
            hint,
        )
    else:
        logger.warning(
            "Approval override key %r is a reserved tool kind with no such tools in the initial toolset; "
            "it may apply to runtime-added tools of that kind, but does NOT apply to the tool named %r.%s",
            key,
            key,
            hint,
        )


class ApprovalMode(Enum):
    """Approval mode for tool calls.

    MANUAL — every tool call that requires approval shows a dialog.
    AUTO   — an LLM judge evaluates the call; only flagged calls show a dialog.
    BYPASS — all tool calls are silently auto-approved.
    """

    MANUAL = "manual"
    AUTO = "auto"
    BYPASS = "bypass"

    @classmethod
    def from_string(cls, value: str | None, default: ApprovalMode | None = None) -> ApprovalMode:
        """Parse a string into an :class:`ApprovalMode`, falling back on invalid input.

        ``default`` is returned when ``value`` is empty or doesn't match any
        enum member.  When ``default`` is ``None``, :attr:`MANUAL` is used.
        """
        fallback = default if default is not None else cls.MANUAL
        if not value:
            return fallback
        try:
            return cls(value.strip().lower())
        except ValueError:
            return fallback


class ApprovalPolicy:
    """Evaluates whether a tool call requires user approval.

    Resolution order:
    1. User session-level override (if allowed by profile)
    2. Kind-qualified tool override (``kind.tool_name``)
    3. Bare exact tool-name override
    4. Kind-only override
    5. The called tool's own ``requires_approval`` declaration
       (:mod:`chrys.foundation.tool_approval`): require
    6. Profile-level default

    Resolution is independent of YAML mapping order.  Tool kind and the
    declaration are accepted at invocation time so tools contributed by context
    providers or loaded dynamically obey the same rules as construction-time
    tools.  The initial tool list remains a compatibility fallback for callers
    that omit them and supplies reserved-name diagnostics only.

    Override keys support two formats:

    - **Kind-only** (e.g. ``shell``): applies to ALL tools with that kind.
    - **Kind + tool name** (e.g. ``filesystem.write.write_file``):
      applies only to the tool with matching kind AND name.

    Keys that don't match any tool kind are treated as bare tool-name
    overrides (exact name match).

    A declaration is a secure floor: local web search/fetch reach the network,
    and skill scripts are ordinary subprocesses with the user's OS privileges,
    not sandboxed execution, so they declare it and require approval by default
    even when the profile default is ``auto`` or ``skip``.  A matching explicit override
    (including ``auto`` or ``skip``) is the deliberate opt-out.  Kind-wide
    ``skill`` overrides count as explicit and intentionally affect all skill
    tools, including script execution; profiles that only intend to configure
    the executable boundary should use ``skill.run_skill_script``.  The policy
    does not attempt to classify opaque script arguments as filesystem paths.
    """

    def __init__(self, config: ApprovalConfig, tools: list[FunctionTool] | None = None) -> None:
        self._config = config
        self._default = _fail_closed_rule(config.default, source="approval.default")
        self._user_override: ApprovalRule | None = None
        self._initial_tool_kinds: dict[str, str] = {}
        self._initial_declared: set[str] = set()
        self._kind_tool_overrides: dict[tuple[str, str], ApprovalRule] = {}
        self._tool_overrides: dict[str, ApprovalRule] = {}
        self._kind_overrides: dict[str, ApprovalRule] = {}

        tool_kinds: set[str] = set()
        for tool in tools or []:
            kind = get_tool_kind(tool)
            if kind:
                tool_kinds.add(kind)
                self._initial_tool_kinds[tool.name] = kind
            if declares_requires_approval(tool):
                self._initial_declared.add(tool.name)

        known_kinds = tool_kinds | set(TOOL_KINDS)
        tool_names = {t.name for t in tools} if tools else set()
        for key, rule in config.overrides.items():
            safe_rule = _fail_closed_rule(rule, source=f"approval.overrides[{key!r}]")
            if key in known_kinds:
                if key in _KIND_AND_TOOL_NAME_KEYS:
                    self._tool_overrides[key] = safe_rule
                elif key in tool_names:
                    _warn_shadowed_tool_name_override(key, tools, in_toolset=key in tool_kinds)
                self._kind_overrides[key] = safe_rule
            elif split := _split_kind_tool_override(key, known_kinds):
                self._kind_tool_overrides[split] = safe_rule
            else:
                self._tool_overrides[key] = safe_rule

    def _resolve_rule(self, tool_name: str, tool_kind: str, *, declared: bool) -> ApprovalRule:
        if self._user_override is not None and self._config.user_can_override:
            return self._user_override

        if tool_kind and (rule := self._kind_tool_overrides.get((tool_kind, tool_name))):
            return rule

        if rule := self._tool_overrides.get(tool_name):
            return rule

        if tool_kind and (rule := self._kind_overrides.get(tool_kind)):
            return rule

        if declared:
            return "require"

        return self._default

    def should_require_approval(
        self, tool_name: str, tool_kind: str | None = None, *, tool_requires_approval: bool | None = None
    ) -> bool:
        """Return whether the live tool invocation requires approval.

        *tool_requires_approval* is the called tool's own declaration
        (``declares_requires_approval``); omitted, the initial tool list's is used.
        """
        resolved_kind = tool_kind if tool_kind is not None else self._initial_tool_kinds.get(tool_name, "")
        declared = tool_requires_approval if tool_requires_approval is not None else tool_name in self._initial_declared
        return self._resolve_rule(tool_name, resolved_kind, declared=declared) == "require"

    def set_user_override(self, policy: ApprovalRule | None) -> bool:
        """Set a session-level user override. Returns False if not allowed."""
        if not self._config.user_can_override:
            return False
        self._user_override = None if policy is None else _fail_closed_rule(policy, source="session approval override")
        return True
