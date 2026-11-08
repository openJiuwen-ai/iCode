# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``agent-config/1.0``: the effective agent configuration, redacted once.

This is the half of reproduction that lives outside the repository — the profile
body, the model parameters, the skill roster, the MCP server list and the
effective hook set. The contract is ``design/otel-reporting/agent-config-sync.md``
(a closed set: nothing outside its field list is collected, and a source that
cannot be located is reported as ``absent`` rather than guessed at).

Two rules shape the whole module:

* the payload is content-addressed, so it must be byte-stable for one
  configuration — the profile body is parsed and canonically re-serialized
  (sorted keys, no comments, uniform indent) and the capture moment rides the
  frame envelope instead of the payload;
* credentials and connection facts never leave the machine: keys are matched by
  segment (``apiKey`` hits, ``maxTokens`` does not), their values — subtree
  included — become ``<redacted>`` and only the key *names* are listed.
"""

from __future__ import annotations

import json
import logging
import platform as host_platform
import re
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Final

import yaml

from chrys.foundation.reporting.frames import canonical_json_bytes, sha256_hex

logger = logging.getLogger(__name__)

SCHEMA_NAME: Final = "agent-config/1.0"

MAX_PAYLOAD_BYTES: Final = 256 * 1024
MAX_PROFILE_BYTES: Final = 64 * 1024

REDACTED_PLACEHOLDER: Final = "<redacted>"

CREDENTIAL_KEY_SEGMENTS: Final = frozenset(
    {
        "key",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "bearer",
        "private",
        "cookie",
    }
)
"""Key segments whose values are credentials; a hit redacts the whole subtree."""

CREDENTIAL_KEYS_EXPLICIT: Final = frozenset(
    {
        "api_key",
        "apikey",
        "api-key",
        "access_token",
        "refresh_token",
        "secret_key",
        "private_key",
    }
)
"""Compact spellings the segment rule cannot see (``apikey`` has no ``key`` segment)."""

CONNECTION_KEY_SEGMENTS: Final = frozenset(
    {
        "address",
        "args",
        "arguments",
        "baseurl",
        "cmd",
        "command",
        "env",
        "endpoint",
        "extraheaders",
        "headers",
        "httpheaders",
        "proxy",
        "uri",
        "url",
    }
)
"""Key segments that name a connection: command lines, endpoints, environments.

``base_url`` and ``http_headers`` hit through their ``url``/``headers``
segments; the compound single-token spellings (``baseurl``, ``httpheaders``,
``extraheaders``, ``arguments``) are listed because they carry no boundary.
"""

SENSITIVE_KEY_SEGMENTS: Final = CREDENTIAL_KEY_SEGMENTS | CONNECTION_KEY_SEGMENTS
"""Every segment that makes a key sensitive."""

SENSITIVE_KEY_SUFFIXES: Final = CREDENTIAL_KEY_SEGMENTS | CREDENTIAL_KEYS_EXPLICIT
"""Suffixes that make a hyphen/underscore spelling sensitive (``x-api-key``)."""

JSON_STRING_KEYS: Final = frozenset({"chat_options", "chatoptions"})
"""Keys whose string value holds nested JSON (the model file's ``chat_options``).

The keys inside that string are not payload keys, so name matching cannot see
them: the value is parsed, redacted by the same rules and re-serialized.
"""

_KEY_SEGMENT_RE: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")
"""Splits a key at camelCase and separator boundaries: ``base_url`` → base, url."""

_ARCHITECTURES: Final = {
    "amd64": "amd64",
    "x64": "amd64",
    "x86_64": "amd64",
    "arm64": "arm64",
    "aarch64": "arm64",
    "ia32": "ia32",
    "x86": "ia32",
}

_MODEL_IDENTITY_FIELDS: Final = frozenset({"name", "id"})
"""The two fields that name the model profile; everything else is a parameter."""

_PROFILE_EXTENSIONS: Final = (".yaml", ".yml")


@dataclass(frozen=True, slots=True)
class SessionFacts:
    """The runtime facts one ``agent.env`` capture is built from.

    Everything here comes from the session's own runtime snapshot (the
    ``SessionReady`` / ``AgentRuntimeUpdated`` payload), never from a re-read of
    the session file. ``None`` where the session has not said (the caller holds
    the whole object optional): those sections report ``absent``.
    """

    model_profile_id: str = ""
    model_provider: str = ""
    model_id: str = ""
    skills: tuple[tuple[str, str], ...] = ()
    """(name, source directory) per session skill; the directory is runtime fact."""
    hooks_count: int = 0
    hook_events: tuple[str, ...] = ()
    hook_sources: tuple[tuple[str, str], ...] = ()
    """(scope, source path) per hooks document the runtime merged."""


@dataclass(frozen=True, slots=True)
class SkillFact:
    """One session skill as the payload names it."""

    name: str
    source: str
    digest: str


@dataclass(frozen=True, slots=True)
class ProfileFacts:
    """One located profile.

    ``document`` is the parsed document **before redaction** (the MCP
    connection digests are computed from it); ``body`` is the redacted,
    canonically serialized form the payload ships.
    """

    document: dict[str, object]
    body: str
    redacted_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentEnvironment:
    """Everything ``agent-config/1.0`` describes, as the process already knows it."""

    profile_name: str = ""
    profile_yaml: str = ""
    profile_found: bool = False
    profile_redacted_keys: tuple[str, ...] = ()
    model_profile_id: str = ""
    model_provider: str = ""
    model_id: str = ""
    model_params: dict[str, object] = field(default_factory=dict)
    model_found: bool = False
    skills: tuple[SkillFact, ...] = ()
    skills_known: bool = False
    mcp_servers: tuple[dict[str, str], ...] = ()
    mcp_declared: bool = False
    hooks_count: int = 0
    hook_events: tuple[str, ...] = ()
    hooks_known: bool = False
    settings_digest: str = ""


def platform_string() -> str:
    """``<os>-<arch>`` with the arch spelled the way the contract spells it."""
    os_name = {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    arch = host_platform.machine().lower()
    return f"{os_name}-{_ARCHITECTURES.get(arch, arch)}"


def _key_segments(key: str) -> set[str]:
    return {segment.lower() for segment in _KEY_SEGMENT_RE.findall(key)}


def is_sensitive_key(key: str) -> bool:
    """Whether *key* names a credential or a connection fact.

    Three rules, in this order, exactly as the authority spells them: a compact
    spelling that has no usable boundary (``apikey``), a segment hit — so
    ``apiKey`` and ``api_key`` hit while ``maxTokens`` — ``max`` plus
    ``tokens`` — keeps its value, because modelling parameters are
    configuration, not secrets — and a ``_``/``-`` suffix spelling
    (``x-api-key``).
    """
    normalized = key.lower()
    if normalized in CREDENTIAL_KEYS_EXPLICIT:
        return True
    if _key_segments(key) & SENSITIVE_KEY_SEGMENTS:
        return True
    return any(normalized.endswith((f"_{suffix}", f"-{suffix}")) for suffix in SENSITIVE_KEY_SUFFIXES)


def redact_value(value: object) -> tuple[object, list[str]]:
    """Replace every sensitive key's value — subtree included — with a placeholder.

    Returns the redacted copy and the key names it replaced. Mappings keep their
    keys (so "the key is redacted" stays distinguishable from "the key is
    absent"), lists are walked item by item, and anything else passes through.
    A ``chat_options``-style string is parsed and redacted as the structure it
    is; a value that does not parse becomes the placeholder itself.
    """
    redacted: list[str] = []
    return _redact(value, redacted), redacted


def _redact(value: object, redacted: list[str]) -> object:
    if isinstance(value, dict):
        result: dict[object, object] = {}
        for key, item in value.items():
            if isinstance(key, str) and is_sensitive_key(key):
                redacted.append(key)
                result[key] = REDACTED_PLACEHOLDER
            elif isinstance(key, str) and isinstance(item, str) and key.lower() in JSON_STRING_KEYS:
                result[key] = _redact_json_string(key, item, redacted)
            else:
                result[key] = _redact(item, redacted)
        return result
    if isinstance(value, list):
        return [_redact(item, redacted) for item in value]
    return value


def _redact_json_string(key: str, text: str, redacted: list[str]) -> str:
    """Redact a nested JSON document carried as a string, then re-serialize it.

    Shipping it unparsed would mean treating "there might be credentials in a
    structure nobody looked at" as "there are none".
    """
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    if not isinstance(parsed, (dict, list)):
        redacted.append(key)
        return REDACTED_PLACEHOLDER
    return canonical_json_bytes(_redact(parsed, redacted)).decode("utf-8")


def _yaml_files(directory: Path) -> list[Path]:
    """``*.yaml`` and ``*.yml`` side by side, in a stable order."""
    try:
        files = sorted(path for path in directory.iterdir() if path.is_file() and path.suffix in _PROFILE_EXTENSIONS)
    except OSError:
        return []
    return files


def _parse_yaml_file(path: Path) -> dict[str, object] | None:
    """One bounded read and parse; anything unreadable or non-mapping is absent."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")[:MAX_PROFILE_BYTES]
        document = yaml.safe_load(text)
    except OSError, yaml.YAMLError:
        return None
    return document if isinstance(document, dict) else None


def canonical_profile_body(document: dict[str, object]) -> ProfileFacts | None:
    """The redacted, canonically serialized profile body.

    One configuration written two ways — different key order, comments, indent —
    must produce the same bytes, so the document is parsed and re-serialized
    rather than shipped as written. A body that cannot be canonically serialized
    (mixed key types) is reported as absent rather than shipped unstable.
    """
    redacted, keys = redact_value(document)
    try:
        body = yaml.safe_dump(redacted, sort_keys=True, default_flow_style=False)
    except yaml.YAMLError:
        return None
    return ProfileFacts(document=document, body=body, redacted_keys=tuple(sorted(set(keys))))


def load_profile(name: str, config_dir: Path) -> ProfileFacts | None:
    """The effective profile for *name*, or ``None`` when it cannot be located.

    Matched like the runtime matches it: ``agents/*.yaml|yml`` by filename stem
    (case-insensitive), falling back to the document's own ``name:``; a user
    profile shadows the builtin of the same name.
    """
    if not name:
        return None
    for directory in (config_dir / "agents", _builtin_profiles_dir()):
        document = _match_profile_document(directory, name)
        if document is not None:
            return canonical_profile_body(document)
    return None


def _match_profile_document(directory: Path, name: str) -> dict[str, object] | None:
    files = _yaml_files(directory)
    for path in files:
        if path.stem.casefold() == name.casefold():
            return _parse_yaml_file(path)
    for path in files:
        document = _parse_yaml_file(path)
        if document is None:
            continue
        stored = document.get("name")
        if isinstance(stored, str) and stored.casefold() == name.casefold():
            return document
    return None


def _builtin_profiles_dir() -> Path:
    """The profile YAMLs shipped with the package, resolved from this file."""
    return Path(__file__).resolve().parents[1] / "profiles" / "agents" / "builtins"


def mcp_server_facts(profile_document: dict[str, object] | None) -> tuple[dict[str, str], ...]:
    """``{name, transport?, connectionDigest?}`` per profile ``tools.mcp[]`` entry.

    The effective source is the profile document the runtime loaded. Digests are
    computed from the unredacted connection facts — stdio takes
    ``sha256(canonical JSON([command, ...args]))``, http takes
    ``sha256(canonical JSON([url]))`` — so the plaintext never enters the
    payload, where redaction could only hide it. A field the entry does not
    name is omitted rather than filled in, exactly as the reference producer
    emits it.
    """
    entries = profile_mcp_entries(profile_document)
    if not entries:
        return ()
    servers: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        server = {"name": name}
        transport = entry.get("transport")
        if not isinstance(transport, str):
            transport = entry.get("type")
        if isinstance(transport, str) and transport:
            server["transport"] = transport
        digest = _mcp_connection_digest(entry)
        if digest:
            server["connectionDigest"] = digest
        servers.append(server)
    return tuple(servers)


def profile_mcp_entries(profile_document: dict[str, object] | None) -> list[object] | None:
    """The profile's declared ``tools.mcp`` list, or ``None`` when it declares none.

    ``None`` is the "nothing was declared" fact the ``sections.mcp`` state
    reports as absent; an empty list is a profile that declares no server.
    """
    if not profile_document:
        return None
    tools = profile_document.get("tools")
    if not isinstance(tools, dict):
        return None
    entries = tools.get("mcp")
    return entries if isinstance(entries, list) else None


def _mcp_connection_digest(entry: dict[str, object]) -> str:
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        command = entry.get("cmd")
    if isinstance(command, str) and command:
        raw_args = entry.get("args")
        args = [item for item in raw_args if isinstance(item, str)] if isinstance(raw_args, list) else []
        return sha256_hex(canonical_json_bytes([command, *args]))
    url = entry.get("url") if isinstance(entry.get("url"), str) else entry.get("uri")
    return sha256_hex(canonical_json_bytes([url])) if isinstance(url, str) and url else ""


def model_profile_document(model_profile_id: str, config_dir: Path) -> dict[str, object] | None:
    """``models/*.yaml|yml`` matched by document ``id``, falling back to the stem."""
    if not model_profile_id:
        return None
    wanted = model_profile_id.strip().casefold()
    stem_match: dict[str, object] | None = None
    for path in _yaml_files(config_dir / "models"):
        document = _parse_yaml_file(path)
        if document is None:
            continue
        document_id = document.get("id")
        if isinstance(document_id, str) and document_id.casefold() == wanted:
            return document
        if stem_match is None and path.stem.casefold() == wanted:
            stem_match = document
    return stem_match


def model_params(document: dict[str, object] | None) -> dict[str, object]:
    """The model profile's fields minus its identity; credentials redact on serialization."""
    if not document:
        return {}
    return {key: value for key, value in document.items() if isinstance(key, str) and key not in _MODEL_IDENTITY_FIELDS}


def skill_facts(skills: Sequence[tuple[str, str]], config_dir: Path) -> tuple[SkillFact, ...]:
    """Name, source and SKILL.md digest per session skill.

    ``source`` is ``config`` or ``linked`` for the two roots every session
    discovers (the configuration directory and the shared ``~/.agents``
    directory) and empty for everything else — project trees and inline profile
    skills — whose location the contract deliberately does not name. The digest
    is of the SKILL.md bytes; unreadable content leaves it empty rather than
    pretending a name digest describes content it never read.
    """
    from chrys.service.skills.adapter import user_agents_dir

    config_root = (config_dir / "skills").expanduser()
    linked_root = (user_agents_dir() / "skills").expanduser()
    facts: list[SkillFact] = []
    for name, source_dir in skills:
        source = ""
        if source_dir:
            directory = Path(source_dir).expanduser()
            if _is_within(directory, config_root):
                source = "config"
            elif _is_within(directory, linked_root):
                source = "linked"
        facts.append(SkillFact(name=name, source=source, digest=_skill_md_digest(source_dir)))
    return tuple(facts)


def _skill_md_digest(source_dir: str) -> str:
    if not source_dir:
        return ""
    try:
        return sha256_hex((Path(source_dir) / "SKILL.md").read_bytes())
    except OSError:
        return ""


def _is_within(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def hooks_settings_digest(sources: Sequence[tuple[str, str]]) -> str:
    """Digest of the hooks documents' explicit ``settings`` sections.

    The settings the runtime actually merged, project over global, and only the
    fields the documents set — defaults are behaviour, not document. The section
    never enters the payload, so a digest is all that ships.
    """
    from chrys.service.hooks.loader import load_hooks_file

    explicit: dict[str, object] = {}
    # Global first so a project field overwrites it, the merge the runtime uses.
    for _scope, source_path in sorted(sources, key=lambda item: 1 if item[0] == "project" else 0):
        try:
            loaded = load_hooks_file(Path(source_path))
        except Exception:
            logger.debug("Could not read the hooks settings of %s", source_path, exc_info=True)
            continue
        explicit.update(
            {name: value for name, value in asdict(loaded.settings).items() if name in loaded.settings_overrides}
        )
    if not explicit:
        return ""
    redacted, _keys = redact_value(explicit)
    return sha256_hex(canonical_json_bytes(redacted))


def collect_agent_environment(
    facts: SessionFacts | None,
    *,
    profile_name: str,
    config_dir: Path,
) -> AgentEnvironment:
    """Assemble the environment: the only file reads in the agent-config path.

    Runs in the collection thread. Every source that cannot be located reports
    as absent — the sections exist so "the configuration has none" and "the
    client could not read it" never collapse into each other.
    """
    profile = load_profile(profile_name, config_dir)
    model_document = model_profile_document(facts.model_profile_id if facts is not None else "", config_dir)
    declared_mcp = profile_mcp_entries(profile.document if profile is not None else None)
    # The profile reports the name its document declares, so a profile file whose
    # ``name:`` differs from its stem still labels the same way on both producers.
    document_name = profile.document.get("name") if profile is not None else None
    return AgentEnvironment(
        profile_name=document_name if isinstance(document_name, str) and document_name else profile_name,
        profile_yaml=profile.body if profile is not None else "",
        profile_found=profile is not None,
        profile_redacted_keys=profile.redacted_keys if profile is not None else (),
        model_profile_id=facts.model_profile_id if facts is not None else "",
        model_provider=facts.model_provider if facts is not None else "",
        model_id=facts.model_id if facts is not None else "",
        model_params=model_params(model_document),
        model_found=model_document is not None,
        skills=skill_facts(facts.skills, config_dir) if facts is not None else (),
        skills_known=facts is not None,
        mcp_servers=mcp_server_facts(profile.document if profile is not None else None),
        mcp_declared=declared_mcp is not None,
        hooks_count=facts.hooks_count if facts is not None else 0,
        hook_events=tuple(facts.hook_events) if facts is not None else (),
        hooks_known=facts is not None,
        settings_digest=hooks_settings_digest(facts.hook_sources) if facts is not None else hooks_settings_digest(()),
    )


def agent_environment_payload(environment: AgentEnvironment, *, app_version: str) -> tuple[bytes, list[str]]:
    """The canonical payload plus the keys it had to redact.

    Over the size budget the payload degrades to a safe subset (identity, counts
    and sections only) and says ``truncated`` instead of shipping something
    enormous; consumers must tolerate it even though the teams producer fails
    instead.
    """
    params, param_keys = redact_value(environment.model_params)
    redacted_keys = sorted({*param_keys, *environment.profile_redacted_keys})
    document: dict[str, object] = {
        "schema": SCHEMA_NAME,
        "appVersion": app_version,
        "platform": platform_string(),
        "profile": {"name": environment.profile_name, "yaml": environment.profile_yaml},
        "model": {
            "profileId": environment.model_profile_id,
            "provider": environment.model_provider,
            "modelId": environment.model_id,
            "params": params,
        },
        "skills": [
            {"name": skill.name, "source": skill.source, "digest": skill.digest} for skill in environment.skills
        ],
        "mcp": {"servers": [dict(server) for server in environment.mcp_servers]},
        "hooks": {"count": environment.hooks_count, "events": sorted(set(environment.hook_events))},
        "sections": {
            "profile": "captured" if environment.profile_found else "absent",
            "model": "captured" if environment.model_found else "absent",
            "skills": "captured" if environment.skills_known else "absent",
            "mcp": "captured" if environment.mcp_declared else "absent",
            "hooks": "captured" if environment.hooks_known else "absent",
        },
        "redacted": redacted_keys,
        "truncated": False,
    }
    # No explicit hook settings → no digest key at all: "the document sets nothing"
    # and "the digest of an empty section" are different facts, and the reference
    # producer omits the field rather than digesting ``{}``.
    if environment.settings_digest:
        document["settingsDigest"] = environment.settings_digest
    payload = canonical_json_bytes(document)
    if len(payload) <= MAX_PAYLOAD_BYTES:
        return payload, redacted_keys
    document["profile"] = {"name": environment.profile_name, "yaml": ""}
    document["model"]["params"] = {}
    document["skills"] = []
    document["mcp"]["servers"] = []
    document["truncated"] = True
    return canonical_json_bytes(document), redacted_keys
