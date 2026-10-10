# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent profile loader — load agent profile definitions from YAML files and directories."""

from __future__ import annotations

import logging
import math
import ntpath
import re
from dataclasses import dataclass
from pathlib import PurePath, PureWindowsPath
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.text.encoding import decode_bytes
from chrys.foundation.tool_kinds import strip_legacy_kind_prefix
from chrys.service.mcp.validation import (
    MCP_PROGRESSIVE_CONTROL_TOOL_NAMES,
    validate_mcp_tool_loading_policy,
    validate_mcp_tool_name_prefix,
)
from chrys.service.profiles.agents.schema import (
    DEFAULT_SUB_AGENT_CONCURRENCY,
    AcpAgentConfig,
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    MCPServerConfig,
    MemoryConfig,
    ModelConfig,
    ShellFilterConfig,
    SkillConfig,
    SkillResourceConfig,
    SkillsConfig,
    SkillScriptConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.coercion import coerce_bool

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class AgentProfileLoadError(Exception):
    """Raised when an agent profile YAML file cannot be loaded or validated."""


_VALID_TRANSPORTS = {"stdio", "http"}
_VALID_APPROVAL_RULES = frozenset({"auto", "require", "skip"})
_SKILL_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,63}$")
_MAX_SKILL_DESCRIPTION_LENGTH = 1024
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ACP_DEPTH_ENV = "CHRYS_ACP_SUBAGENT_DEPTH"
_ACP_RESULT_MODES = frozenset({"last_segment", "transcript"})


def _mapping_section(raw: object, section: str) -> dict[str, Any]:
    """Return a YAML section mapping or raise a profile load error."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        msg = f"Agent profile section '{section}' must be a mapping, got {type(raw).__name__}"
        raise AgentProfileLoadError(msg)
    return cast("dict[str, Any]", raw)


def _list_section(raw: object, section: str) -> list[Any]:
    """Return a YAML section list or raise a profile load error."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        msg = f"Agent profile section '{section}' must be a list, got {type(raw).__name__}"
        raise AgentProfileLoadError(msg)
    return raw


def _mapping_entry(raw: object, section: str) -> dict[str, Any]:
    """Return a list entry mapping or raise a profile load error."""
    if not isinstance(raw, dict):
        msg = f"Agent profile section '{section}' entries must be mappings, got {type(raw).__name__}"
        raise AgentProfileLoadError(msg)
    return cast("dict[str, Any]", raw)


def _string_list_field(raw: object, field: str) -> list[str]:
    """Parse a list-of-strings field without accepting scalar iterables."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        msg = f"Agent profile field '{field}' must be a list of strings; got {raw!r}"
        raise AgentProfileLoadError(msg)
    return list(cast("list[str]", raw))


def _instructions_field(raw: object, field: str) -> str:
    """Parse instructions text; a list of strings joins one item per line.

    Any other value would reach the prompt as its Python repr.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if not isinstance(raw, list):
        msg = f"Agent profile field '{field}' must be a string or a list of strings; got {type(raw).__name__}"
        raise AgentProfileLoadError(msg)
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, str):
            # YAML reads an unquoted `- Note: text` or `- Heading:` item as a mapping.
            msg = (
                f"Agent profile field '{field}' must be a string or a list of strings; item {index} is a "
                f"{type(item).__name__} (quote list items that contain ': ' or end with ':')"
            )
            raise AgentProfileLoadError(msg)
    return "\n".join(cast("list[str]", raw))


def _approval_rule(raw: object, field: str) -> Literal["auto", "require", "skip"]:
    """Parse an approval rule without YAML truthiness coercion."""
    if not isinstance(raw, str) or raw not in _VALID_APPROVAL_RULES:
        allowed = ", ".join(sorted(_VALID_APPROVAL_RULES))
        msg = f"Agent profile field '{field}' must be one of: {allowed}; got {raw!r}"
        raise AgentProfileLoadError(msg)
    return cast("Literal['auto', 'require', 'skip']", raw)


def _acp_string(raw: object, field: str, *, reject_nul: bool = False) -> str:
    if type(raw) is not str:
        msg = f"Agent profile field 'acp.{field}' must be a string; got {raw!r}"
        raise AgentProfileLoadError(msg)
    if reject_nul and "\0" in raw:
        msg = f"Agent profile field 'acp.{field}' must not contain an embedded NUL"
        raise AgentProfileLoadError(msg)
    return raw


def _acp_timeout(
    raw: object,
    field: str,
    *,
    default: float,
    allow_zero: bool,
) -> float:
    if type(raw) not in {int, float}:
        logger.warning("Invalid acp.%s %r; using default %s", field, raw, default)
        return default
    try:
        value = float(cast("int | float", raw))
    except OverflowError:
        # YAML ints are arbitrary-precision; one absurd timeout must not
        # abort loading the whole profile registry.
        logger.warning("Invalid acp.%s %r; using default %s", field, raw, default)
        return default
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        logger.warning("Invalid acp.%s %r; using default %s", field, raw, default)
        return default
    return value


def _parse_acp(raw: object) -> AcpAgentConfig:
    """Parse the ACP discriminator with strict per-field type enforcement."""
    raw_map = _mapping_section(raw, "acp")
    defaults = AcpAgentConfig()

    command = _acp_string(raw_map.get("command", defaults.command), "command", reject_nul=True)
    cwd = _acp_string(raw_map.get("cwd", defaults.cwd), "cwd", reject_nul=True)
    session_mode = _acp_string(raw_map.get("session_mode", defaults.session_mode), "session_mode")
    model_id = _acp_string(raw_map.get("model_id", defaults.model_id), "model_id")

    raw_args = raw_map.get("args", defaults.args)
    if type(raw_args) is not list:
        msg = f"Agent profile field 'acp.args' must be a list of strings; got {raw_args!r}"
        raise AgentProfileLoadError(msg)
    args = [_acp_string(value, f"args[{index}]", reject_nul=True) for index, value in enumerate(raw_args)]

    raw_env = raw_map.get("env", defaults.env)
    if type(raw_env) is not dict:
        msg = f"Agent profile field 'acp.env' must be a mapping of strings; got {raw_env!r}"
        raise AgentProfileLoadError(msg)
    env: dict[str, str] = {}
    env_keys: set[str] = set()
    for key, value in raw_env.items():
        if type(key) is not str or _ENV_NAME_RE.fullmatch(key) is None:
            msg = f"Agent profile field 'acp.env' has invalid environment variable name {key!r}"
            raise AgentProfileLoadError(msg)
        folded = key.casefold()
        if folded == _ACP_DEPTH_ENV.casefold():
            msg = f"Agent profile field 'acp.env' reserves {_ACP_DEPTH_ENV!r} for {APP_DISPLAY_NAME}"
            raise AgentProfileLoadError(msg)
        if folded in env_keys:
            msg = f"Agent profile field 'acp.env' has case-insensitive duplicate key {key!r}"
            raise AgentProfileLoadError(msg)
        env_keys.add(folded)
        env[key] = _acp_string(value, f"env.{key}", reject_nul=True)

    raw_options = raw_map.get("config_options", defaults.config_options)
    if type(raw_options) is not dict:
        msg = f"Agent profile field 'acp.config_options' must be a mapping; got {raw_options!r}"
        raise AgentProfileLoadError(msg)
    config_options: dict[str, str | bool] = {}
    for key, value in raw_options.items():
        if type(key) is not str or not key:
            msg = f"Agent profile field 'acp.config_options' keys must be non-empty strings; got {key!r}"
            raise AgentProfileLoadError(msg)
        if type(value) not in {str, bool}:
            msg = (
                "Agent profile field 'acp.config_options' values must be strings or booleans; "
                f"got {value!r} for {key!r}"
            )
            raise AgentProfileLoadError(msg)
        config_options[key] = value

    allow_external_cwd = raw_map.get("allow_external_cwd", defaults.allow_external_cwd)
    if type(allow_external_cwd) is not bool:
        msg = f"Agent profile field 'acp.allow_external_cwd' must be a boolean; got {allow_external_cwd!r}"
        raise AgentProfileLoadError(msg)
    best_effort_options = raw_map.get("best_effort_options", defaults.best_effort_options)
    if type(best_effort_options) is not bool:
        msg = f"Agent profile field 'acp.best_effort_options' must be a boolean; got {best_effort_options!r}"
        raise AgentProfileLoadError(msg)
    result_mode = raw_map.get("result_mode", defaults.result_mode)
    if type(result_mode) is not str or result_mode not in _ACP_RESULT_MODES:
        msg = f"Agent profile field 'acp.result_mode' must be one of: last_segment, transcript; got {result_mode!r}"
        raise AgentProfileLoadError(msg)

    return AcpAgentConfig(
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        allow_external_cwd=allow_external_cwd,
        session_mode=session_mode,
        model_id=model_id,
        config_options=config_options,
        best_effort_options=best_effort_options,
        result_mode=result_mode,
        handshake_timeout_seconds=_acp_timeout(
            raw_map.get("handshake_timeout_seconds", defaults.handshake_timeout_seconds),
            "handshake_timeout_seconds",
            default=defaults.handshake_timeout_seconds,
            allow_zero=False,
        ),
        idle_timeout_seconds=_acp_timeout(
            raw_map.get("idle_timeout_seconds", defaults.idle_timeout_seconds),
            "idle_timeout_seconds",
            default=defaults.idle_timeout_seconds,
            allow_zero=True,
        ),
    )


def _parse_mcp_servers(raw: object) -> list[MCPServerConfig]:
    configs: list[MCPServerConfig] = []
    for raw_entry in _list_section(raw, "tools.mcp"):
        entry = _mapping_entry(raw_entry, "tools.mcp")
        if "name" not in entry:
            msg = "MCP server missing required 'name' field"
            raise AgentProfileLoadError(msg)
        if "transport" not in entry:
            msg = f"MCP server '{entry.get('name', '?')}' missing required 'transport' field"
            raise AgentProfileLoadError(msg)
        transport = entry["transport"]
        if transport not in _VALID_TRANSPORTS:
            msg = f"MCP server '{entry.get('name', '?')}' has invalid transport '{transport}', must be one of {_VALID_TRANSPORTS}"
            raise AgentProfileLoadError(msg)
        allowed_tools = (
            None
            if entry.get("allowed_tools") is None
            else _string_list_field(entry.get("allowed_tools"), "tools.mcp.allowed_tools")
        )
        use_progressive_disclosure = coerce_bool(entry.get("use_progressive_disclosure"), default=False)
        tool_name_prefix = entry.get("tool_name_prefix", "")
        prefix_error = validate_mcp_tool_name_prefix(
            tool_name_prefix,
            generated_suffixes=MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if use_progressive_disclosure else (),
        )
        if prefix_error is not None:
            msg = f"MCP server '{entry.get('name', '?')}' has invalid tool name prefix: {prefix_error}"
            raise AgentProfileLoadError(msg)
        always_load = _string_list_field(entry.get("always_load"), "tools.mcp.always_load")
        policy_errors = validate_mcp_tool_loading_policy(
            allowed_tools=allowed_tools,
            use_progressive_disclosure=use_progressive_disclosure,
            always_load=always_load,
        )
        if policy_errors:
            msg = f"MCP server '{entry.get('name', '?')}' has invalid tool loading policy: {' '.join(policy_errors)}"
            raise AgentProfileLoadError(msg)
        max_tool_result_tokens = entry.get("max_tool_result_tokens")
        if not (
            max_tool_result_tokens is None
            or (type(max_tool_result_tokens) is int and (max_tool_result_tokens == 0 or max_tool_result_tokens >= 100))
        ):
            msg = (
                f"MCP server '{entry.get('name', '?')}' has invalid max_tool_result_tokens; "
                "expected null, 0, or an integer of at least 100"
            )
            raise AgentProfileLoadError(msg)
        configs.append(
            MCPServerConfig(
                name=entry["name"],
                transport=transport,
                # stdio
                command=entry.get("command", ""),
                args=entry.get("args", []),
                env=entry.get("env", {}) if transport == "stdio" else {},
                encoding=entry.get("encoding"),
                # http
                url=entry.get("url", ""),
                headers=entry.get("headers", {}),
                resolve_header_templates=coerce_bool(entry.get("resolve_header_templates"), default=True),
                terminate_on_close=entry.get("terminate_on_close"),
                verify_ssl=coerce_bool(entry.get("verify_ssl"), default=True),
                bypass_proxy=coerce_bool(entry.get("bypass_proxy"), default=False),
                # common
                enabled=entry.get("enabled", entry.get("enable", True)),
                description=entry.get("description", ""),
                tool_name_prefix=tool_name_prefix,
                allowed_tools=allowed_tools,
                use_progressive_disclosure=use_progressive_disclosure,
                always_load=always_load,
                request_timeout=entry.get("timeout", entry.get("request_timeout")),
                max_tool_result_tokens=max_tool_result_tokens,
                load_prompts=entry.get("load_prompts", True),
                expose_instructions=coerce_bool(entry.get("expose_instructions"), default=True),
            )
        )
    return configs


def _parse_skill_resources(raw: object, section: str) -> list[SkillResourceConfig]:
    resources: list[SkillResourceConfig] = []
    for raw_entry in _list_section(raw, section):
        entry = _mapping_entry(raw_entry, section)
        if "name" not in entry:
            msg = f"Skill resource in '{section}' missing required 'name' field"
            raise AgentProfileLoadError(msg)
        resources.append(
            SkillResourceConfig(
                name=entry["name"],
                description=entry.get("description", ""),
                content=entry.get("content", ""),
                path=entry.get("path", ""),
            )
        )
    return resources


def _parse_skill_scripts(raw: object, section: str) -> list[SkillScriptConfig]:
    scripts: list[SkillScriptConfig] = []
    for raw_entry in _list_section(raw, section):
        entry = _mapping_entry(raw_entry, section)
        if "name" not in entry:
            msg = f"Skill script in '{section}' missing required 'name' field"
            raise AgentProfileLoadError(msg)
        scripts.append(
            SkillScriptConfig(
                name=entry["name"],
                description=entry.get("description", ""),
                path=entry.get("path", ""),
            )
        )
    return scripts


def _validate_inline_skill_metadata(entry: dict[str, Any], index: int) -> None:
    """Validate inline profile skill metadata against the Agent Skills specification rules."""
    location = f"skills.inline[{index}]"
    name = entry["name"]
    if not isinstance(name, str) or not name.strip():
        msg = f"{location}.name must be a non-empty string"
        raise AgentProfileLoadError(msg)
    if not _SKILL_NAME_RE.fullmatch(name):
        msg = (
            f"{location}.name '{name}' is not a valid skill name; "
            "use 1-64 lowercase letters, numbers, and single hyphens, with no leading or trailing hyphen."
        )
        raise AgentProfileLoadError(msg)

    description = entry.get("description", "")
    if not isinstance(description, str):
        msg = f"{location}.description must be a string"
        raise AgentProfileLoadError(msg)
    if not description.strip():
        msg = f"{location}.description is required for inline skills"
        raise AgentProfileLoadError(msg)
    if len(description) > _MAX_SKILL_DESCRIPTION_LENGTH:
        msg = f"{location}.description must be {_MAX_SKILL_DESCRIPTION_LENGTH} characters or fewer"
        raise AgentProfileLoadError(msg)


def _parse_skills(raw: object) -> SkillsConfig:
    if raw is None:
        return SkillsConfig()
    raw_map = _mapping_section(raw, "skills")
    inline: list[SkillConfig] = []
    for index, raw_entry in enumerate(_list_section(raw_map.get("inline", []), "skills.inline"), start=1):
        entry = _mapping_entry(raw_entry, "skills.inline")
        if "name" not in entry:
            msg = "Inline skill missing required 'name' field"
            raise AgentProfileLoadError(msg)
        _validate_inline_skill_metadata(entry, index)
        inline.append(
            SkillConfig(
                name=entry["name"],
                description=entry.get("description", ""),
                instructions=_instructions_field(entry.get("instructions"), f"skills.inline[{index}].instructions"),
                resources=_parse_skill_resources(entry.get("resources", []), "skills.inline.resources"),
                scripts=_parse_skill_scripts(entry.get("scripts", []), "skills.inline.scripts"),
            )
        )
    return SkillsConfig(
        paths=raw_map.get("paths", []),
        inline=inline,
        script_timeout=raw_map.get("script_timeout", 300),
        script_extensions=raw_map.get("script_extensions", [".py", ".sh", ".ps1"]),
        auto_load_user_agents_skills=raw_map.get("auto_load_user_agents_skills", True),
        auto_load_cwd_agents_skills=raw_map.get("auto_load_cwd_agents_skills", True),
    )


def _parse_shell_filter(raw: object) -> ShellFilterConfig | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        # Shorthand: shell_filter: read_only
        return ShellFilterConfig(preset=raw)
    raw_map = _mapping_section(raw, "tools.shell_filter")
    return ShellFilterConfig(
        preset=raw_map.get("preset", ""),
        mode=raw_map.get("mode", "whitelist"),
        commands=raw_map.get("commands", []),
        allow_redirections=raw_map.get("allow_redirections", True),
        allow_subshells=raw_map.get("allow_subshells", True),
    )


def _parse_tools(raw: object) -> ToolsConfig:
    if raw is None:
        return ToolsConfig()
    from chrys.service.tools.builtins.web.config import mapping, parse_fetch, parse_search

    known_keys = {"builtins", "custom", "mcp", "shell_filter", "web_search", "web_fetch"}
    # YAML comments routinely leave sections behind as null. Older versions also
    # ignored unknown tool keys and categories, so retain the understood subset
    # instead of dropping an otherwise usable user override during an upgrade.
    if isinstance(raw, dict):
        unknown_keys = sorted(key for key in raw if isinstance(key, str) and key not in known_keys)
        if unknown_keys:
            logger.warning("Ignoring unknown agent tool keys: %s", ", ".join(unknown_keys))
        raw = {
            key: value
            for key, value in raw.items()
            if value is not None and (not isinstance(key, str) or key in known_keys)
        }
    try:
        raw_map = mapping(
            raw,
            known_keys,
            "tools",
            nullable=frozenset({"shell_filter"}),
        )
        builtins = raw_map.get("builtins", [])
        if not isinstance(builtins, list):
            raise ValueError("tools.builtins must be a list")
        # A stray "-" loads as null. Unknown names stay: the registry ignores them, and
        # keeping them means a save from this version never deletes a newer version's tool.
        if any(not isinstance(category, str) for category in builtins):
            logger.warning("Ignoring non-string entries in tools.builtins")
        raw_map["builtins"] = [category for category in builtins if isinstance(category, str)]

        search_raw = raw_map.get("web_search")
        if isinstance(search_raw, dict) and isinstance(search_raw.get("mode"), bool):
            search_raw = {**search_raw, "mode": "auto" if search_raw["mode"] else "off"}
        fetch_raw = raw_map.get("web_fetch")
        if isinstance(fetch_raw, dict) and isinstance(fetch_raw.get("mode"), bool):
            fetch_raw = {**fetch_raw, "mode": "on" if fetch_raw["mode"] else "off"}
        web_search = parse_search(search_raw) if "web_search" in raw_map else None
        web_fetch = parse_fetch(fetch_raw) if "web_fetch" in raw_map else None
    except ValueError as err:
        raise AgentProfileLoadError(str(err)) from err
    return ToolsConfig(
        builtins=raw_map.get("builtins", []),
        custom=raw_map.get("custom", []),
        mcp=_parse_mcp_servers(raw_map.get("mcp", [])),
        shell_filter=_parse_shell_filter(raw_map.get("shell_filter")),
        web_search=web_search,
        web_fetch=web_fetch,
    )


def _parse_approval(raw: object) -> ApprovalConfig:
    if raw is None:
        return ApprovalConfig()
    raw_map = _mapping_section(raw, "approval")
    defaults = ApprovalConfig()
    raw_overrides = raw_map.get("overrides")
    if raw_overrides is None:
        overrides = dict(defaults.overrides)
    else:
        if not isinstance(raw_overrides, dict):
            msg = f"Agent profile section 'approval.overrides' must be a mapping, got {type(raw_overrides).__name__}"
            raise AgentProfileLoadError(msg)
        overrides = {}
        for key, value in raw_overrides.items():
            if not isinstance(key, str) or not key:
                msg = f"Agent profile section 'approval.overrides' keys must be non-empty strings, got {key!r}"
                raise AgentProfileLoadError(msg)
            # Keys are bare kinds, ``kind.tool_name`` compounds, or bare tool
            # names; legacy pre-P1 keys carried a ``chrys.`` prefix.
            normalized_key = strip_legacy_kind_prefix(key, allow_tool_suffix=True, source="approval.overrides")
            overrides[normalized_key] = _approval_rule(value, f"approval.overrides.{normalized_key}")
    return ApprovalConfig(
        default=_approval_rule(raw_map.get("default", defaults.default), "approval.default"),
        overrides=overrides,
        user_can_override=raw_map.get("user_can_override", defaults.user_can_override),
    )


def _parse_model(raw: object, source: str = "") -> ModelConfig:
    if raw is None:
        return ModelConfig()
    raw_map = _mapping_section(raw, "model")
    profile_id = (raw_map.get("profile_id") or "").strip()
    # Legacy key ``profile`` (previously stored a model-profile *name*) is
    # ignored — the new schema is id-only.  Emit a one-line warning so
    # users know to re-select their model profile via the TUI.
    if not profile_id and raw_map.get("profile"):
        logger.warning(
            "Ignoring legacy `model.profile` field in %s — replace with `model.profile_id: <id>` "
            "or re-select the model profile in the agent configuration screen.",
            source or "agent profile YAML",
        )
    return ModelConfig(profile_id=profile_id)


def _parse_compaction(raw: object) -> CompactionConfig:
    if raw is None:
        return CompactionConfig()
    raw_map = _mapping_section(raw, "compaction")
    defaults = CompactionConfig()
    legacy_keys = {"reserved_context_pct", "compaction_target_pct", "force_compress_pct"}
    reserved = raw_map.get("reserved_context_pct")
    legacy_disabled = isinstance(reserved, int | float) and not isinstance(reserved, bool) and reserved >= 1.0
    if legacy_disabled:
        logger.warning(
            "Compaction thresholds are now derived from the model profile; this profile stays disabled "
            "via the legacy reserved_context_pct >= 1.0 switch for this load"
        )
    ignored_legacy = (legacy_keys & raw_map.keys()) - ({"reserved_context_pct"} if legacy_disabled else set())
    if ignored_legacy:
        logger.info(
            "Ignoring obsolete compaction threshold field(s): %s; thresholds are derived from the model profile",
            ", ".join(sorted(ignored_legacy)),
        )
    last_words_template = raw_map.get("last_words_template", defaults.last_words_template)
    if not isinstance(last_words_template, str):
        msg = f"Agent profile field 'compaction.last_words_template' must be a string; got {last_words_template!r}"
        raise AgentProfileLoadError(msg)
    phase4_side_call_token_budget = raw_map.get(
        "phase4_side_call_token_budget",
        defaults.phase4_side_call_token_budget,
    )
    if phase4_side_call_token_budget is None:
        # Explicit ``null`` keeps its pre--1 meaning: use the default.
        phase4_side_call_token_budget = defaults.phase4_side_call_token_budget
    if (
        not isinstance(phase4_side_call_token_budget, int)
        or isinstance(phase4_side_call_token_budget, bool)
        or phase4_side_call_token_budget < -1
    ):
        msg = (
            "Agent profile field 'compaction.phase4_side_call_token_budget' must be "
            f"an integer >= -1 (-1 means unlimited) or null; got {phase4_side_call_token_budget!r}"
        )
        raise AgentProfileLoadError(msg)
    return CompactionConfig(
        enabled=not legacy_disabled,
        last_words_template=last_words_template,
        last_words_max_output_tokens=raw_map.get(
            "last_words_max_output_tokens",
            defaults.last_words_max_output_tokens,
        ),
        phase4_side_call_token_budget=phase4_side_call_token_budget,
    )


def _parse_memory(raw: object) -> MemoryConfig:
    if raw is None:
        return MemoryConfig()
    raw_map = _mapping_section(raw, "memory")
    return MemoryConfig(
        files=list(raw_map.get("files", [])),
        folders=list(raw_map.get("folders", [])),
    )


def _parse_sub_agents(raw: object) -> SubAgentsConfig:
    if raw is None:
        return SubAgentsConfig()
    raw_map = _mapping_section(raw, "sub_agents")
    agents = [
        SubAgentRef(
            profile=entry["profile"],
            tool_name=entry.get("tool_name", ""),
            tool_description=entry.get("tool_description", ""),
            max_concurrency=entry.get("max_concurrency", DEFAULT_SUB_AGENT_CONCURRENCY),
        )
        for entry in (
            _mapping_entry(raw_entry, "sub_agents.agents")
            for raw_entry in _list_section(raw_map.get("agents", []), "sub_agents.agents")
        )
        if "profile" in entry
    ]
    return SubAgentsConfig(
        max_total_concurrency=raw_map.get("max_total_concurrency", DEFAULT_SUB_AGENT_CONCURRENCY),
        agents=agents,
    )


def is_filename_safe_profile_name(name: str) -> bool:
    """Return True when ``name`` can serve as a bare ``<name>.yaml`` stem.

    Every persistence path resolves a user profile as ``<name>.yaml`` under
    the profiles directory, so a name carrying separators, drive or root
    prefixes, ``.``/``..``, or NUL would escape that directory.  The
    resulting filename must also be legal on every platform a config
    directory can be shared with: Windows device names (``CON``, ``NUL``,
    ``COM1`` …), NTFS stream syntax (``foo:bar``), wildcard and control
    characters are rejected regardless of the current OS.  The ACP write
    path uses the same predicate.
    """
    if not name or name != name.strip() or name in {".", ".."} or "\0" in name:
        return False
    posix_path = PurePath(name)
    windows_path = PureWindowsPath(name)
    if (
        posix_path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or posix_path.name != name
        or windows_path.name != name
    ):
        return False
    return not ntpath.isreserved(f"{name}.yaml")


def load_profile_from_yaml(path: Path) -> AgentProfile:
    """Load an agent profile from a YAML file.

    Raises:
        AgentProfileLoadError: If the file cannot be read or is missing required fields.
    """
    try:
        text = decode_bytes(path.read_bytes(), errors="strict")
    except FileNotFoundError as e:
        raise AgentProfileLoadError(f"Agent profile file not found: {path}") from e
    except (OSError, UnicodeError) as e:
        raise AgentProfileLoadError(f"Cannot read agent profile file {path}: {e}") from e

    try:
        data = yaml.safe_load(text)
    except (yaml.YAMLError, AttributeError, LookupError, TypeError, ValueError) as e:
        # PyYAML scalar constructors can raise built-in errors for invalid values.
        raise AgentProfileLoadError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(data, dict):
        raise AgentProfileLoadError(f"Agent profile YAML must be a mapping, got {type(data).__name__} in {path}")

    if "name" not in data:
        raise AgentProfileLoadError(f"Agent profile YAML missing required 'name' field in {path}")
    raw_name = data["name"]
    if not isinstance(raw_name, str) or not raw_name.strip():
        raise AgentProfileLoadError(f"Agent profile 'name' must be a non-empty string in {path}")
    name = raw_name.strip()
    if not is_filename_safe_profile_name(name):
        raise AgentProfileLoadError(
            f"Agent profile name {name!r} in {path} is not filename-safe; use a bare name, not a path"
        )

    if "hooks" in data:
        logger.warning(
            "Ignoring legacy `hooks` section in %s. Configure lifecycle hooks in the global hooks config instead.",
            path,
        )

    raw_id = data.get("id")
    if raw_id is not None and not isinstance(raw_id, str):
        raise AgentProfileLoadError(f"Agent profile 'id' must be a string or null in {path}")
    profile_id = (raw_id or "").strip()
    acp_present = "acp" in data

    try:
        profile = AgentProfile(
            name=name,
            id=profile_id,
            display_name=data.get("display_name", ""),
            description=data.get("description", ""),
            sub_agent_only=bool(data.get("sub_agent_only", False)),
            acp=_parse_acp(data.get("acp")) if acp_present else None,
            instructions=_instructions_field(data.get("instructions"), "instructions"),
            tools=_parse_tools(data.get("tools")),
            skills=_parse_skills(data.get("skills")),
            approval=_parse_approval(data.get("approval")),
            sub_agents=_parse_sub_agents(data.get("sub_agents")),
            model=_parse_model(data.get("model"), source=str(path)),
            compaction=_parse_compaction(data.get("compaction")),
            memory=_parse_memory(data.get("memory")),
            metadata=data.get("metadata", {}),
        )
        if not acp_present:
            return profile
        if not profile.sub_agent_only:
            logger.warning("ACP profile %s must be sub-agent-only; forcing sub_agent_only=true", profile.name)
        profile.sub_agent_only = True

        conflicting: list[str] = []
        if profile.tools != ToolsConfig():
            conflicting.append("tools")
            profile.tools = ToolsConfig()
        if profile.skills != SkillsConfig():
            conflicting.append("skills")
            profile.skills = SkillsConfig()
        if profile.sub_agents.agents:
            conflicting.append("sub_agents.agents")
            profile.sub_agents = SubAgentsConfig()
        if profile.memory != MemoryConfig():
            conflicting.append("memory")
            profile.memory = MemoryConfig()
        if profile.compaction != CompactionConfig():
            conflicting.append("compaction")
            profile.compaction = CompactionConfig()
        if profile.instructions:
            conflicting.append("instructions")
            profile.instructions = ""
        if profile.approval != ApprovalConfig():
            conflicting.append("approval")
            profile.approval = ApprovalConfig()
        if profile.model.profile_id:
            conflicting.append("model.profile_id")
            profile.model = ModelConfig()
        for section in conflicting:
            logger.warning("ACP profile %s ignores conflicting section %s; reset to defaults", profile.name, section)
        return profile
    except AgentProfileLoadError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as e:
        raise AgentProfileLoadError(f"Invalid agent profile structure in {path}: {e}") from e


@dataclass(frozen=True)
class ProfileDirScan:
    """The profiles parsed from one directory, with their source paths, and the files skipped as invalid."""

    loaded: list[tuple[Path, AgentProfile]]
    invalid: list[Path]


def scan_profile_dir(directory: Path) -> ProfileDirScan:
    """Load all agent profile YAML files from a directory, recording the ones that fail to parse.

    Non-YAML files are ignored, and so are dotfiles even when their suffix is
    YAML.  Files that fail to parse are skipped with a warning and listed in
    ``invalid``.  ``*.yaml`` files come before ``*.yml`` files, each group in
    sorted order.
    """
    loaded: list[tuple[Path, AgentProfile]] = []
    invalid: list[Path] = []
    if not directory.is_dir():
        return ProfileDirScan(loaded, invalid)
    for pattern in ("*.yaml", "*.yml"):
        for path in sorted(directory.glob(pattern)):
            if path.name.startswith("."):
                continue
            try:
                loaded.append((path, load_profile_from_yaml(path)))
            except AgentProfileLoadError as exc:
                # Skip invalid files — caller can use load_profile_from_yaml for strict loading.
                # A dropped user file silently falls back to a builtin of the same name, so
                # this must be loud enough to be noticed in a support log.
                logger.error(
                    "Skipping invalid agent profile %s: %s (any builtin with this name stays active)", path, exc
                )
                invalid.append(path)
    return ProfileDirScan(loaded, invalid)


def load_profile_files_from_dir(directory: Path) -> list[tuple[Path, AgentProfile]]:
    """Load all agent profile YAML files from a directory, keeping their source paths.

    See :func:`scan_profile_dir` for the scan rules.
    """
    return scan_profile_dir(directory).loaded


def load_profiles_from_dir(directory: Path) -> list[AgentProfile]:
    """Load all agent profile YAML files from a directory.

    See :func:`scan_profile_dir` for the scan rules.
    """
    return [profile for _path, profile in load_profile_files_from_dir(directory)]
