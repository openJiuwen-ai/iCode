# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validation helpers for MCP configuration."""

from __future__ import annotations

import re
from collections.abc import Collection

MAX_MCP_EXPOSED_TOOL_NAME_LENGTH = 64
MCP_PROGRESSIVE_CONTROL_TOOL_NAMES = ("list_mcp_tools", "load_tool", "unload_tool")

_PROVIDER_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TOOL_NAME_PREFIX_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")


def _normalize_mcp_name(name: str) -> str:
    # Keep the exposed name portable across model providers. MCP servers may
    # advertise a broader character set, but OpenAI-compatible function tools
    # reject periods and other punctuation before the first model response.
    return re.sub(r"[^A-Za-z0-9_-]", "-", name)


def _build_prefixed_mcp_name(normalized_name: str, tool_name_prefix: str | None) -> str:
    if not tool_name_prefix:
        return normalized_name
    normalized_prefix = _normalize_mcp_name(tool_name_prefix).rstrip("_.-")
    if not normalized_prefix:
        return normalized_name
    trimmed_name = normalized_name.lstrip("_.-")
    return f"{normalized_prefix}_{trimmed_name}" if trimmed_name else normalized_prefix


def _normalized_tool_name_prefix(tool_name_prefix: str | None) -> str:
    return _normalize_mcp_name(tool_name_prefix).rstrip("_.-") if tool_name_prefix else ""


def mcp_config_name_aliases(name: str, *, tool_name_prefix: str | None) -> tuple[str, ...]:
    """Return every configuration spelling that selects the same MCP tool as *name*."""
    prefix = _normalized_tool_name_prefix(tool_name_prefix)
    if not prefix:
        return (name,)
    aliases = [name]
    if _normalize_mcp_name(name) == name:
        # An original name that carries punctuation loses it in the exposed name,
        # which the runtime then never matches, so only portable names gain one.
        exposed = _build_prefixed_mcp_name(name, prefix)
        if exposed != name:
            aliases.append(exposed)
    marker = f"{prefix}_"
    if name.startswith(marker):
        original = name[len(marker) :].lstrip("_.-")
        if original and original not in aliases:
            aliases.append(original)
    return tuple(aliases)


def mcp_config_names_outside_scope(
    names: Collection[str],
    permitted: Collection[str],
    *,
    tool_name_prefix: str | None,
) -> list[str]:
    """Return the *names* none of whose configuration spellings is permitted."""
    allowed = set(permitted)
    prefix = _normalized_tool_name_prefix(tool_name_prefix)
    if prefix:
        # A permitted original name also permits the exposed name built from it:
        # stripping the prefix cannot recover an original whose leading ``_`` or
        # ``-`` the exposed name trimmed. Names the runtime never accepts as a
        # configuration spelling (one that lost punctuation) gain nothing.
        exposed_names = [
            _build_prefixed_mcp_name(name, prefix) for name in allowed if _normalize_mcp_name(name) == name
        ]
        allowed.update(exposed_names)
    return [
        name for name in names if allowed.isdisjoint(mcp_config_name_aliases(name, tool_name_prefix=tool_name_prefix))
    ]


def validate_mcp_exposed_tool_name(value: object) -> str | None:
    """Return an error when a final MCP tool name is not provider-portable."""
    if not isinstance(value, str):
        return "tool name must be a string."
    if not value:
        return "tool name must not be empty."
    if len(value) > MAX_MCP_EXPOSED_TOOL_NAME_LENGTH:
        return f"tool name {value!r} is {len(value)} characters; the maximum is {MAX_MCP_EXPOSED_TOOL_NAME_LENGTH}."
    if not _PROVIDER_SAFE_TOOL_NAME_RE.fullmatch(value):
        return f"tool name {value!r} may contain only letters, numbers, underscores, and hyphens."
    return None


def validate_mcp_tool_name_prefix(
    value: object,
    *,
    generated_suffixes: Collection[str] = (),
) -> str | None:
    """Return a validation error for an MCP tool-name prefix, or ``None`` if valid."""
    if value is None:
        return None
    if not isinstance(value, str):
        return "tool name prefix must be a string."
    if not value:
        return None
    if value != value.strip():
        return "tool name prefix must not include leading or trailing whitespace."
    if not _TOOL_NAME_PREFIX_RE.fullmatch(value):
        return (
            "tool name prefix may contain only letters, numbers, underscores, and hyphens, "
            "and must start and end with a letter or number."
        )
    if len(value) > MAX_MCP_EXPOSED_TOOL_NAME_LENGTH:
        return f"tool name prefix is {len(value)} characters; the maximum is {MAX_MCP_EXPOSED_TOOL_NAME_LENGTH}."
    for suffix in generated_suffixes:
        generated_name = f"{value}_{suffix}"
        if error := validate_mcp_exposed_tool_name(generated_name):
            return f"tool name prefix produces an invalid generated control: {error} Shorten Tool Name Prefix."
    return None


def validate_mcp_tool_loading_policy(
    *,
    allowed_tools: list[str] | None,
    use_progressive_disclosure: bool,
    always_load: list[str],
    tool_name_prefix: str | None,
) -> list[str]:
    """Validate the catalog -> loading strategy -> initial subset hierarchy."""
    errors: list[str] = []
    if always_load and not use_progressive_disclosure:
        errors.append("always_load requires use_progressive_disclosure=true.")
    if allowed_tools == [] and use_progressive_disclosure:
        errors.append("progressive disclosure requires at least one permitted tool.")
    if allowed_tools is not None:
        outside_scope = mcp_config_names_outside_scope(
            always_load,
            allowed_tools,
            tool_name_prefix=tool_name_prefix,
        )
        if outside_scope:
            errors.append(f"always_load tools must also appear in allowed_tools: {', '.join(outside_scope)}.")
    return errors
