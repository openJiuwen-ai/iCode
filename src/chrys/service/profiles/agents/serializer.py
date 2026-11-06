# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent profile serializer — convert AgentProfile dataclasses to YAML-ready dicts."""

from __future__ import annotations

import logging
import threading
from dataclasses import MISSING, fields
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.text.yaml_io import dump_yaml
from chrys.service.profiles.agents.loader import (
    AgentProfileLoadError,
    is_filename_safe_profile_name,
    load_profile_from_yaml,
)
from chrys.service.tools.builtins.web.config import provider_fields

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.profiles.agents.schema import AgentProfile

logger = logging.getLogger(__name__)

# MCP field key renames: dataclass field name → YAML key
_MCP_KEY_MAP = {"request_timeout": "timeout"}


def _dc_to_dict(obj: Any) -> dict[str, Any]:
    """Recursively convert a dataclass to a dict, omitting fields at their default values.

    Nested dataclasses are recursed into; empty nested dicts are dropped.
    Lists of dataclasses are serialized element-wise.
    """
    result: dict[str, Any] = {}
    for f in fields(obj):
        val = getattr(obj, f.name)
        # Skip if value matches the field default
        if f.default is not MISSING and val == f.default:
            continue
        if f.default_factory is not MISSING:
            try:
                if val == f.default_factory():
                    continue
            except TypeError:
                pass
        # Recurse into nested dataclasses
        if hasattr(val, "__dataclass_fields__"):
            nested = _dc_to_dict(val)
            if nested:
                result[f.name] = nested
        elif isinstance(val, list) and val and hasattr(val[0], "__dataclass_fields__"):
            result[f.name] = [_dc_to_dict(item) for item in val]
        elif isinstance(val, dict):
            result[f.name] = {
                key: _dc_to_dict(item) if hasattr(item, "__dataclass_fields__") else item for key, item in val.items()
            }
        else:
            result[f.name] = val
    return result


# Fields that should always be serialized even when at their default value.
_ALWAYS_EMIT = {"approval"}


def profile_to_dict(profile: AgentProfile) -> dict[str, Any]:
    """Serialize a full AgentProfile to a dict suitable for ``yaml.dump()``.

    Uses generic dataclass introspection so new schema fields are
    automatically included without updating this module.
    """
    d = _dc_to_dict(profile)
    # Force-emit ``id`` (even when empty) and position it directly after ``name``
    # for stable, human-readable YAML output.  The registry should always set
    # an id before save, so the empty case is purely defensive.
    d.pop("id", None)
    ordered: dict[str, Any] = {}
    if "name" in d:
        ordered["name"] = d.pop("name")
    ordered["id"] = profile.id
    ordered.update(d)
    d = ordered
    compaction = d.get("compaction")
    if isinstance(compaction, dict):
        compaction.pop("enabled", None)
        if not compaction:
            d.pop("compaction")
    # Force-emit fields that must always appear in YAML
    for key in _ALWAYS_EMIT:
        if key not in d:
            val = getattr(profile, key, None)
            if val is not None and hasattr(val, "__dataclass_fields__"):
                nested: dict[str, Any] = {}
                for f in fields(val):
                    nested[f.name] = getattr(val, f.name)
                d[key] = nested
    if profile.acp is not None and "acp" not in d:
        d["acp"] = {}
    # Apply MCP key renames (e.g. request_timeout → timeout)
    for mcp in d.get("tools", {}).get("mcp", []):
        if mcp.get("transport") == "http":
            mcp.pop("env", None)
        for src, dst in _MCP_KEY_MAP.items():
            if src in mcp:
                mcp[dst] = mcp.pop(src)
    # Shell filter preset shorthand: {"preset": "read_only"} → "read_only"
    tools = d.get("tools", {})
    sf = tools.get("shell_filter")
    if isinstance(sf, dict) and sf.keys() == {"preset"}:
        tools["shell_filter"] = sf["preset"]
    # Search providers: only what each type reads, and a preset's shorthand.
    web_search = profile.tools.web_search
    serialized_providers = tools.get("web_search", {}).get("providers", {})
    if web_search is not None and web_search.providers:
        for provider_id, provider in web_search.providers.items():
            if provider_id in serialized_providers:
                serialized_providers[provider_id] = provider_fields(provider)
    return d


# ── Public API ───────────────────────────────────────────────────────


def _user_profiles_dir() -> Path:
    """Return the user agent profiles directory."""
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "agents"


def _require_filename_safe_name(name: str) -> None:
    """Refuse names that would resolve ``<name>.yaml`` outside the profiles directory."""
    if not is_filename_safe_profile_name(name):
        msg = f"Agent profile name {name!r} is not filename-safe; use a bare name, not a path"
        raise ValueError(msg)


def stored_profile_name(name: str) -> str | None:
    """Read the owner of the default save/delete destination, or None if absent.

    Registry entries and their loading directory do not establish ownership of
    the file being modified. Load errors propagate so callers can refuse when
    ownership is unknown, including when the file disappears during loading.
    """
    _require_filename_safe_name(name)
    target = _user_profiles_dir() / f"{name}.yaml"
    try:
        # Only an absent directory entry is safe to treat as no owner.
        # Directories, broken symlinks and read failures must fail closed.
        target.lstat()
    except FileNotFoundError:
        return None
    except OSError as err:
        raise AgentProfileLoadError(f"Cannot inspect agent profile file {target}: {err}") from err
    return load_profile_from_yaml(target).name


# A normalization must compare and rewrite against the same revision as the editor.
profile_write_lock = threading.RLock()


def save_profile(profile: AgentProfile, *, target_dir: Path | None = None) -> Path:
    """Serialize *profile* to YAML and write to ``{target_dir}/{name}.yaml``.

    ``target_dir`` defaults to ``~/.chrys/agents/`` (the user profiles dir).
    Returns the path written.

    Raises:
        ValueError: If ``profile.name`` is not filename-safe.
    """
    _require_filename_safe_name(profile.name)
    out_dir = target_dir if target_dir is not None else _user_profiles_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{profile.name}.yaml"
    data = profile_to_dict(profile)
    text = dump_yaml(data)
    with profile_write_lock:
        atomic_write_owner_only_text(path, text)
    logger.debug("Saved agent profile to %s", path)
    return path


def delete_profile(name: str) -> bool:
    """Delete the user profile YAML file for *name*.

    Returns True if the file existed and was removed, False otherwise.
    Built-in profiles cannot be deleted via this function.

    Raises:
        ValueError: If ``name`` is not filename-safe.
    """
    _require_filename_safe_name(name)
    path = _user_profiles_dir() / f"{name}.yaml"
    with profile_write_lock:
        if path.is_file():
            path.unlink()
            logger.debug("Deleted agent profile %s", path)
            return True
    return False
