# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline MCP reuse choices from the agent configuration window's drafts."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum

from chrys.app.tui.screens.agents.agent_draft_store import mcp_draft_errors_by_server
from chrys.foundation.text.reading_order import reading_order
from chrys.service.profiles.agents.schema import AgentProfile, MCPServerConfig


def mcp_name_key(name: str) -> str:
    """Match the case-insensitive names accepted by the configuration editor."""
    return name.strip().casefold()


def comparable_mcp_config(config: MCPServerConfig) -> MCPServerConfig:
    """Normalize editor semantics without resolving templates or running credentials."""
    normalized = replace(config, name=mcp_name_key(config.name))
    if config.transport == "stdio":
        return replace(
            normalized,
            url="",
            headers={},
            resolve_header_templates=True,
            terminate_on_close=None,
            verify_ssl=True,
            bypass_proxy=False,
        )
    return replace(normalized, command="", args=[], encoding=None, env={})


@dataclass(frozen=True)
class MCPChoice:
    """A configuration snapshot and the agents offering an equivalent copy."""

    config: MCPServerConfig
    sources: tuple[str, ...]


@dataclass(frozen=True)
class MCPChoices:
    """Valid reuse choices and the number of invalid source configurations omitted."""

    choices: tuple[MCPChoice, ...] = ()
    hidden_count: int = 0


def _valid_source_shape(config: MCPServerConfig) -> bool:
    """Handwritten YAML can reach the editor with values outside the dataclass annotations."""
    if not isinstance(config.name, str) or config.transport not in {"stdio", "http"}:
        return False
    active = comparable_mcp_config(config)
    if not all(
        isinstance(value, str) for value in (active.command, active.url, active.description, active.tool_name_prefix)
    ):
        return False
    if not all(
        type(value) is bool
        for value in (
            active.enabled,
            active.load_prompts,
            active.expose_instructions,
            active.use_progressive_disclosure,
            active.resolve_header_templates,
            active.verify_ssl,
            active.bypass_proxy,
        )
    ):
        return False
    if active.encoding is not None and not isinstance(active.encoding, str):
        return False
    if active.terminate_on_close is not None and type(active.terminate_on_close) is not bool:
        return False
    if any(
        value is not None and type(value) is not int
        for value in (active.request_timeout, active.max_tool_result_tokens)
    ):
        return False
    for values in (active.args, active.always_load, active.allowed_tools if active.allowed_tools is not None else []):
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            return False
    for mapping in (active.env, active.headers):
        if not isinstance(mapping, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in mapping.items()
        ):
            return False
    return True


def collect_mcp_choices(profiles: Iterable[AgentProfile]) -> MCPChoices:
    """Collect other agents' latest drafts; no registry fallback resurrects deleted sources."""
    choices: list[MCPChoice] = []
    comparable: list[MCPServerConfig] = []
    hidden_count = 0
    for profile in profiles:
        if profile.acp is not None:
            continue
        source = profile.display_name or profile.name
        if profile.display_name and profile.display_name != profile.name:
            source = f"{profile.display_name} ({profile.name})"
        configs = [config for config in profile.tools.mcp if _valid_source_shape(config)]
        hidden_count += len(profile.tools.mcp) - len(configs)
        checked_profile = replace(profile, tools=replace(profile.tools, mcp=configs))
        errors = mcp_draft_errors_by_server("", checked_profile)
        for config, server_errors in zip(configs, errors, strict=True):
            if not config.name.strip() or server_errors:
                hidden_count += 1
                continue
            normalized = comparable_mcp_config(config)
            if normalized in comparable:
                index = comparable.index(normalized)
                choice = choices[index]
                sources = tuple(sorted({*choice.sources, source}, key=reading_order))
                choices[index] = replace(choice, sources=sources)
            else:
                comparable.append(normalized)
                choices.append(MCPChoice(deepcopy(config), (source,)))
    choices.sort(key=lambda choice: (reading_order(choice.config.name), reading_order(", ".join(choice.sources))))
    return MCPChoices(tuple(choices), hidden_count)


class MCPConflictKind(StrEnum):
    """Reasons a candidate cannot be added to the current card list."""

    ADDED = "added"
    NAME = "name"
    CONNECTION = "connection"


@dataclass(frozen=True)
class MCPConflict:
    kind: MCPConflictKind
    existing_name: str


def mcp_choice_conflict(candidate: MCPServerConfig, current: Sequence[MCPServerConfig]) -> MCPConflict | None:
    """Reject duplicate names and overlapping allowlists on the same connection, even when disabled."""
    normalized = comparable_mcp_config(candidate)
    for config in current:
        if mcp_name_key(config.name) == normalized.name:
            kind = MCPConflictKind.ADDED if comparable_mcp_config(config) == normalized else MCPConflictKind.NAME
            return MCPConflict(kind, config.name)
    for config in current:
        if (
            config.transport != candidate.transport
            or config.tool_name_prefix.strip() != candidate.tool_name_prefix.strip()
        ):
            continue
        same_connection = (
            config.command.strip() == candidate.command.strip() and config.args == candidate.args
            if candidate.transport == "stdio"
            else config.url.strip() == candidate.url.strip()
        )
        if not same_connection or config.allowed_tools == [] or candidate.allowed_tools == []:
            continue
        if (
            config.allowed_tools is None
            or candidate.allowed_tools is None
            or set(config.allowed_tools) & set(candidate.allowed_tools)
        ):
            return MCPConflict(MCPConflictKind.CONNECTION, config.name)
    return None
