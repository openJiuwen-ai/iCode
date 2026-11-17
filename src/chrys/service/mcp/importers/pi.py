# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in pi, the coding agent (https://github.com/earendil-works/pi).

Read: ``mcp.json`` in pi's agent folder (``$PI_CODING_AGENT_DIR``, default
``~/.pi/agent``), then ``.pi/mcp.json`` in the workspace once the user trusts
it, as pi does: the nearest of the workspace and the folders above it that
``trust.json`` in the agent folder decides, else ``defaultProjectTrust:
"always"`` in the agent folder's ``settings.json`` (pi otherwise asks, so an
undecided project is not read). A project server replaces the global one of
the same name; a project entry with none of ``command``, ``url`` and ``type``
only changes the ``enabled``, ``exposure`` and ``toolExposure`` of the global
server it names. Basic fields are checked before replacing an earlier definition;
an invalid project entry reports an error and leaves the global server intact.
OAuth options and URL details beyond an ``http``/``https`` scheme are not
validated: they are copied as written.

Values in ``env`` and ``headers`` may reference ``$NAME`` or ``${NAME}``; ``$$``
and ``$!`` are a literal ``$`` and ``!``. A value starting with ``!`` runs a
command in pi, which the import does not translate: such servers are listed
for manual setup. Home expansion in ``command`` and ``args`` uses Node's lexical
path normalization, without following symlinks. HTTP servers without ``auth``
or an Authorization header default to OAuth, whose sign-in cannot be imported.

``exposure`` maps to iCode's loading: ``direct`` loads every tool,
``codemode`` and ``deferred`` load on demand, ``hidden`` offers none.
``toolExposure`` entries naming one tool become iCode's tool lists where they
can; a rule hiding tools iCode can't express imports the server with no tools.
``timeout`` is in seconds, default 60.

Checked against https://github.com/earendil-works/pi tag ``v1.5.2`` (commit
b4afdddf44), read 2026-11-12; paths are under ``packages/coding-agent/src``:
the files and project overrides (``extensions/mcp/config.ts``), trust
(``core/trust-manager.ts``, ``core/project-trust.ts``), the
variables and ``!`` commands (``core/resolve-config-value.ts``), the server
fields and exposure (``core/mcp-servers.ts``, delegating shared validation to
``packages/mcp/src/config.ts:165-173``) and ``~`` expansion
(``extensions/mcp/runtime.ts:91-123``).
``cwd`` must be a string: null and false are invalid, not defaults.
It is a literal path (no variable or command expansion): runtime.ts:120
expands its home prefix, then resolves it lexically from the session folder.
Import keeps ordinary paths relative; an empty string, like absence, means
that session folder. Remaining tildes are shielded from iCode's expansion.
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.foundation.platform import get_platform
from chrys.service.mcp.importers.base import (
    FieldUse,
    ImportContext,
    ImportWarning,
    McpImportResult,
    McpSourceError,
    Problem,
    SourceErrorKind,
    SourceReadError,
    SourceServer,
    Value,
    ValueReason,
    Var,
    WarningKind,
    convert,
    folders_down_to,
    is_str_list,
    is_str_mapping,
    lexical_cwd,
    literal,
    missing_fields,
    path_matches,
    positive_number,
    read_json_object,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import PurePath

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Pi accepts only http and https URLs, in any letter case.
_HTTP_SCHEME_RE = re.compile(r"https?:", re.IGNORECASE)
_DEFAULT_TIMEOUT_SECONDS = 60.0
_OVERRIDE_KEYS = frozenset({"enabled", "exposure", "toolExposure"})
_READ = (
    "type",
    "command",
    "args",
    "env",
    "url",
    "headers",
    "enabled",
    "description",
    "timeout",
    "oauth",
    "auth",
)
_READ_EXPOSURE = (*_READ, "exposure", "toolExposure")

_FULL, _ON_DEMAND, _HIDDEN = "full", "on_demand", "hidden"
_EXPOSURES = {
    "direct": _FULL,
    "codemode": _ON_DEMAND,
    "codemode-deferred": _ON_DEMAND,
    "deferred": _ON_DEMAND,
    "hidden": _HIDDEN,
}


class PiSource:
    @property
    def source_id(self) -> str:
        return "pi"

    @property
    def display_name(self) -> str:
        return "Pi"

    def detect(self, ctx: ImportContext) -> bool:
        agent = _agent_folder(ctx)
        return (agent / "mcp.json").is_file() or (
            ctx.workspace_cwd is not None and (Path(ctx.workspace_cwd) / ".pi" / "mcp.json").is_file()
        )

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []

        def read(path: Path) -> dict[str, Any] | None:
            try:
                return read_json_object(path) or {}
            except SourceReadError as exc:
                errors.append(exc.at(path))
                return None

        agent = _agent_folder(ctx)
        files = [agent / "mcp.json"]
        project = Path(ctx.workspace_cwd) / ".pi" / "mcp.json" if ctx.workspace_cwd else None
        if project is not None and project.is_file() and _trusted(ctx.workspace_cwd or "", agent, read):
            files.append(project)
        entries: dict[str, tuple[dict[str, Any], SourceServer]] = {}
        for position, path in enumerate(files):
            document = read(path)
            if document is None:
                continue
            servers = document.get("mcpServers", {})
            if not isinstance(servers, dict):
                errors.append(McpSourceError(str(path), SourceErrorKind.INVALID, field="mcpServers"))
                continue
            for name, entry in servers.items():
                error = McpSourceError(str(path), SourceErrorKind.INVALID, field=f"mcpServers.{name}")
                if not isinstance(entry, dict) or not _NAME_RE.fullmatch(name):
                    errors.append(error)
                    continue
                override = position and not entry.keys() & {"command", "url", "type"}
                source_path = str(path)
                if override:
                    # In the project file, an override changes only how a global server is offered.
                    if name not in entries or entry.keys() - _OVERRIDE_KEYS:
                        errors.append(error)
                        continue
                    base, previous = entries[name]
                    entry = {**base, **entry}
                    source_path = previous.source_path
                server = _server(name, entry, source_path, ctx.home)
                if server is None or (position and not override and server.transport == "http" and entry.get("auth")):
                    errors.append(error)
                    continue
                if any(other != name and other.replace("-", "_") == name.replace("-", "_") for other in entries):
                    errors.append(error)
                    continue
                # Keep refusal candidates instead of silently choosing an earlier definition.
                entries[name] = (entry, server)
        candidates = tuple(convert(server) for _, server in entries.values())
        return McpImportResult(candidates, tuple(errors))


def _agent_folder(ctx: ImportContext) -> Path:
    folder = ctx.environ.get("PI_CODING_AGENT_DIR")
    agent = Path(_expand_home(folder, ctx.home)) if folder else ctx.home / ".pi" / "agent"
    if not agent.is_absolute() and ctx.workspace_cwd:
        agent = Path(ctx.workspace_cwd) / agent
    return agent


def _trusted(workspace_cwd: str, agent: Path, read: Callable[[Path], dict[str, Any] | None]) -> bool:
    """Whether pi loads the workspace's project files without asking."""
    decisions = read(agent / "trust.json") or {}
    for folder in reversed(folders_down_to(workspace_cwd, None)):
        decision = next(
            (value for key, value in decisions.items() if isinstance(value, bool) and path_matches(key, str(folder))),
            None,
        )
        if decision is not None:
            return decision
    return (read(agent / "settings.json") or {}).get("defaultProjectTrust") == "always"


def _expand_home(text: str, home: PurePath) -> str:
    if text == "~":
        return str(home)
    windows = get_platform().is_windows
    if text.startswith("~/") or (windows and text.startswith("~\\")):
        path = ntpath if windows else posixpath
        suffix = text[2:]
        # Node joins before normalizing; Python join would discard home for a rooted suffix.
        joined = str(home) + path.sep + suffix if suffix else str(home)
        normalized = path.normpath(joined)
        if not windows and normalized.startswith("//"):
            normalized = "/" + normalized.lstrip("/")
        if joined.endswith(("/", "\\") if windows else ("/",)) and not normalized.endswith(path.sep):
            normalized += path.sep
        return normalized
    return text


def _is_exposure(value: object) -> bool:
    return isinstance(value, str) and value in _EXPOSURES


def _server(name: str, entry: dict[str, Any], path: str, home: Path) -> SourceServer | None:
    """The server pi would run, checked by its basic fields only, or ``None`` for an entry it rejects."""
    prefix = f"mcpServers.{name}"
    kind = entry.get("type", "")
    exposure = entry.get("exposure", "codemode")
    rules = entry.get("toolExposure", {})
    timeout = positive_number(entry.get("timeout")) if "timeout" in entry else _DEFAULT_TIMEOUT_SECONDS
    if (
        ("type" in entry and kind not in ("stdio", "http", "streamable-http"))
        or not _is_exposure(exposure)
        or not isinstance(rules, dict)
        or not all(_is_exposure(rule) for rule in rules.values())
        or not isinstance(entry.get("enabled", True), bool)
        or not isinstance(entry.get("description", ""), str)
        or timeout is None
    ):
        return None
    if isinstance(entry.get("url"), str) and kind in ("", "http", "streamable-http"):
        transport = "http"
        if (
            not _HTTP_SCHEME_RE.match(entry["url"].strip())
            or not is_str_mapping(entry.get("headers", {}))
            or ("oauth" in entry and not isinstance(entry["oauth"], dict))
        ):
            return None
        if "auth" in entry:
            auth = entry["auth"]
            if not isinstance(auth, dict) or not isinstance(auth.get("provider"), str) or not auth["provider"]:
                return None
    elif isinstance(entry.get("command"), str) and kind in ("", "stdio"):
        transport = "stdio"
        if not is_str_list(entry.get("args", [])) or not is_str_mapping(entry.get("env", {})):
            return None
        if "cwd" in entry and not isinstance(entry["cwd"], str):
            return None
    else:
        return None
    texts: Mapping[str, str] = entry.get("env" if transport == "stdio" else "headers", {})
    group = f"{prefix}.{'env' if transport == 'stdio' else 'headers'}"
    values = {key: _value(text) for key, text in texts.items()}
    blocked = next((f"{group}.{key}" for key, text in texts.items() if text.startswith("!")), "")
    access = _tool_access(exposure, rules, prefix)
    known = (*_READ_EXPOSURE, "cwd") if transport == "stdio" else _READ_EXPOSURE
    warnings: list[ImportWarning | None] = [*access.warnings, missing_fields(entry, known, prefix)]
    authorized = any(key.lower() == "authorization" for key in texts)
    if transport == "http" and (entry.get("auth") or not authorized):
        warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
    return SourceServer(
        name,
        path,
        transport,
        command=_expand_home(entry["command"], home) if transport == "stdio" else "",
        args=tuple(_expand_home(arg, home) for arg in entry.get("args", [])) if transport == "stdio" else (),
        command_path=f"{prefix}.command",
        arg_paths=tuple(f"{prefix}.args[{i}]" for i in range(len(entry.get("args", []))))
        if transport == "stdio"
        else (),
        env=values if transport == "stdio" else {},
        url=entry.get("url", ""),
        headers=values if transport == "http" else {},
        enabled=entry.get("enabled", True),
        description=entry.get("description", ""),
        timeout_seconds=timeout,
        allowed_tools=access.allowed,
        progressive=access.progressive,
        always_load=access.always_load,
        allowed_tools_path=f"{prefix}.toolExposure",
        always_load_path=f"{prefix}.toolExposure",
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=blocked,
        blocked_problem=Problem.VALUE_FROM_FILE_OR_COMMAND,
        blocked_reason=ValueReason.COMMAND,
        blocked_use=FieldUse.ENV_VALUE if transport == "stdio" else FieldUse.HEADER_VALUE,
        env_path=group,
        headers_path=group,
        cwd=literal(_cwd(entry["cwd"], home)) if transport == "stdio" and entry.get("cwd") else None,
        cwd_path=f"{prefix}.cwd",
    )


def _cwd(text: str, home: Path) -> str:
    return lexical_cwd(_expand_home(text, home))


def _value(text: str) -> Value:
    parts: list[str | Var] = []
    position = 0
    while position < len(text):
        dollar = text.find("$", position)
        if dollar < 0:
            parts.append(text[position:])
            break
        parts.append(text[position:dollar])
        following = text[dollar + 1 : dollar + 2]
        if following in ("$", "!"):
            parts.append(following)
            position = dollar + 2
        elif following == "{" and (end := text.find("}", dollar + 2)) >= 0:
            name = text[dollar + 2 : end]
            # Invalid braced blocks are consumed whole, including any dollars inside them.
            parts.append(Var(name) if _VARIABLE_NAME_RE.fullmatch(name) else text[dollar : end + 1])
            position = end + 1
        elif following != "{" and (match := _VARIABLE_NAME_RE.match(text, dollar + 1)):
            parts.append(Var(match[0]))
            position = match.end()
        else:
            parts.append("$")
            position = dollar + 1
    return tuple(parts)


@dataclass(slots=True)
class _Access:
    allowed: tuple[str, ...] | None = None
    progressive: bool = False
    always_load: tuple[str, ...] = ()
    warnings: list[ImportWarning] = field(default_factory=list)


def _tool_access(exposure: str, rules: dict[str, str], prefix: str) -> _Access:
    """pi's exposure rules as iCode's tool lists and loading; exact tool names beat patterns in pi."""
    access = _Access()
    base = _EXPOSURES[exposure]
    granted: dict[str, str] = {}
    always: list[str] = []
    unread: list[str] = []
    for tool, rule in rules.items():
        kind = _EXPOSURES[rule]
        if kind == base:
            continue
        if kind == _HIDDEN:
            # iCode has no deny list: hiding some tools of a visible server can't be expressed.
            access.allowed = ()
            access.warnings = [ImportWarning(WarningKind.TOOLS_RESTRICTED)]
            return access
        if "*" in tool:
            unread.append(f"{prefix}.toolExposure.{tool}")
        elif base == _HIDDEN:
            granted[tool] = kind
        elif base == _ON_DEMAND and kind == _FULL:
            always.append(tool)
        else:
            unread.append(f"{prefix}.toolExposure.{tool}")
    if unread:
        access.warnings.append(ImportWarning(WarningKind.TOOL_LOADING, tuple(unread)))
    if base == _HIDDEN:
        access.allowed = tuple(granted)
        access.progressive = _ON_DEMAND in granted.values()
        access.always_load = tuple(t for t, k in granted.items() if k == _FULL) if access.progressive else ()
    else:
        access.progressive = base == _ON_DEMAND
        access.always_load = tuple(always)
    return access
