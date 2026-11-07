# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``agent-config/1.0``: the effective agent configuration, redacted once.

This is the half of reproduction that lives outside the repository — the profile
body, the model, the tool set, the skills, the MCP servers, the hook events and
the settings digest. Credentials never enter the payload: sensitive keys are
replaced and their *names* are listed in ``redacted`` so the omission is
auditable rather than mysterious. Command lines keep only a digest.

The values come from what the process already knows (the ``SessionReady``
snapshot and the loaded settings); only the profile body is read from its file,
bounded and in the collection thread.
"""

from __future__ import annotations

import hashlib
import platform as host_platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from chrys.foundation.reporting.frames import canonical_json_bytes

SCHEMA_NAME: Final = "agent-config/1.0"

MAX_PAYLOAD_BYTES: Final = 256 * 1024
MAX_PROFILE_BYTES: Final = 64 * 1024

SENSITIVE_KEYS: Final = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "base_url",
        "command",
        "credential",
        "credentials",
        "env",
        "headers",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "private_key",
    }
)
"""Keys whose *values* never leave the machine; only their names are reported."""

_KEY_LINE = re.compile(r"^(?P<indent>\s*)(?P<dash>-\s+)?(?P<key>[A-Za-z0-9_.-]+)\s*:(?P<value>.*)$")


@dataclass(frozen=True, slots=True)
class AgentEnvironment:
    """Everything ``agent-config/1.0`` describes, as the process already holds it."""

    profile_name: str = ""
    profile_yaml: str = ""
    display_name: str = ""
    model_profile_id: str = ""
    model_provider: str = ""
    model_id: str = ""
    model_params: dict[str, object] = field(default_factory=dict)
    tool_names: tuple[str, ...] = ()
    tool_kinds: dict[str, str] = field(default_factory=dict)
    skill_names: tuple[str, ...] = ()
    mcp_servers: tuple[dict[str, str], ...] = ()
    hook_events: tuple[str, ...] = ()
    settings_digest: str = ""


def platform_string() -> str:
    """``<os>-<arch>`` — the platform, never the environment."""
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    return f"{os_name}-{host_platform.machine().lower()}"


def redact_profile_text(text: str) -> tuple[str, list[str]]:
    """Replace credential values with ``<redacted>`` and name every key removed.

    Indented children of a replaced block key are dropped too: a ``headers:``
    block's contents are themselves values, not keys.
    """
    redacted: list[str] = []
    output: list[str] = []
    skip_deeper_than: int | None = None
    for line in text.splitlines():
        match = _KEY_LINE.match(line)
        if match is None:
            if skip_deeper_than is not None and line.strip():
                continue
            output.append(line)
            continue
        indent = len(match.group("indent").expandtabs(2))
        if skip_deeper_than is not None:
            if indent > skip_deeper_than:
                continue
            skip_deeper_than = None
        key = match.group("key").lower()
        if key in SENSITIVE_KEYS or any(key.endswith(f"_{suffix}") for suffix in SENSITIVE_KEYS):
            redacted.append(match.group("key"))
            output.append(f"{match.group('indent')}{match.group('dash') or ''}{match.group('key')}: <redacted>")
            if not match.group("value").strip():
                skip_deeper_than = indent
            continue
        output.append(line)
    return "\n".join(output), redacted


def agent_environment_payload(environment: AgentEnvironment, *, app_version: str) -> tuple[bytes, list[str]]:
    """The canonical payload plus the keys it had to redact.

    Over the size budget the payload degrades to a safe subset (identity and
    counts only) and says ``truncated`` instead of sending something enormous.
    """
    profile_text, redacted = redact_profile_text(environment.profile_yaml[:MAX_PROFILE_BYTES])
    document: dict[str, object] = {
        "schema": SCHEMA_NAME,
        "appVersion": app_version,
        "platform": platform_string(),
        "profile": {"name": environment.profile_name, "yaml": profile_text},
        "model": {
            "profileId": environment.model_profile_id,
            "provider": environment.model_provider,
            "modelId": environment.model_id,
            "params": dict(environment.model_params),
        },
        "tools": {"enabled": list(environment.tool_names), "kinds": dict(environment.tool_kinds)},
        "skills": [
            {"name": name, "source": "", "digest": _digest(name)} for name in sorted(set(environment.skill_names))
        ],
        "mcp": {"servers": [dict(server) for server in environment.mcp_servers]},
        "hooks": {"count": len(environment.hook_events), "events": sorted(set(environment.hook_events))},
        "settingsDigest": environment.settings_digest,
        "redacted": sorted(set(redacted)),
        "truncated": False,
    }
    payload = canonical_json_bytes(document)
    if len(payload) <= MAX_PAYLOAD_BYTES:
        return payload, sorted(set(redacted))
    document["profile"] = {"name": environment.profile_name, "yaml": ""}
    document["tools"] = {"enabled": [], "kinds": {}}
    document["skills"] = []
    document["truncated"] = True
    return canonical_json_bytes(document), sorted(set(redacted))


def profile_yaml_text(name: str, config_dir: Path) -> str:
    """The stored body of profile *name*, or an empty string when it has no file.

    A user profile shadows a builtin, and the profile directory is caseless on
    macOS and Windows, so the file is matched by its case-folded stem.
    """
    if not name:
        return ""
    for directory in (config_dir / "agents", _builtin_profiles_dir()):
        text = _read_matching(directory, name)
        if text:
            return text
    return ""


def _builtin_profiles_dir() -> Path:
    """The profile YAMLs shipped with the package, resolved from this file."""
    return Path(__file__).resolve().parents[1] / "profiles" / "agents" / "builtins"


def _read_matching(directory: Path, name: str) -> str:
    try:
        entries = sorted(directory.glob("*.yaml"))
    except OSError:
        return ""
    wanted = name.casefold()
    for entry in entries:
        if entry.stem.casefold() != wanted:
            continue
        try:
            return entry.read_text(encoding="utf-8", errors="replace")[:MAX_PROFILE_BYTES]
        except OSError:
            return ""
    return ""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()


def command_digest(command: str) -> str:
    """Digest of a command line; the arguments themselves never ship."""
    return _digest(command) if command else ""
