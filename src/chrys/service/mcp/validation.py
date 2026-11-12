# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Validation helpers for MCP configuration."""

from __future__ import annotations

import ntpath
import re
from collections.abc import Collection

MAX_MCP_EXPOSED_TOOL_NAME_LENGTH = 64
MCP_PROGRESSIVE_CONTROL_TOOL_NAMES = ("list_mcp_tools", "load_tool", "unload_tool")

_PROVIDER_SAFE_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_TOOL_NAME_PREFIX_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_-]*[A-Za-z0-9])?$")


def is_windows_drive_relative_path(path: str) -> bool:
    """Whether Windows resolves *path* from a drive's current folder (for example ``C:tools``)."""
    return bool(ntpath.splitdrive(path)[0]) and not ntpath.isabs(path)


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
) -> list[str]:
    """Validate the catalog -> loading strategy -> initial subset hierarchy."""
    errors: list[str] = []
    if always_load and not use_progressive_disclosure:
        errors.append("always_load requires use_progressive_disclosure=true.")
    if allowed_tools == [] and use_progressive_disclosure:
        errors.append("progressive disclosure requires at least one permitted tool.")
    if allowed_tools is not None:
        outside_scope = [name for name in always_load if name not in set(allowed_tools)]
        if outside_scope:
            errors.append(f"always_load tools must also appear in allowed_tools: {', '.join(outside_scope)}.")
    return errors
