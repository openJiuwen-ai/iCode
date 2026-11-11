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
server it names.

Values in ``env`` and ``headers`` may reference ``$NAME`` or ``${NAME}``; ``$$``
and ``$!`` are a literal ``$`` and ``!``. A value starting with ``!`` runs a
command in pi, which the import does not translate: such servers are listed
for manual setup. ``~`` in ``command`` and ``args`` is expanded, as pi does.

``exposure`` maps to iCode's loading: ``direct`` loads every tool,
``codemode`` and ``deferred`` load on demand, ``hidden`` offers none.
``toolExposure`` entries naming one tool become iCode's tool lists where they
can; a rule hiding tools iCode can't express imports the server with no tools.
``timeout`` is in seconds, default 60.

Checked against https://github.com/earendil-works/pi tag ``v1.5.0`` (commit
f9a263655), read 2026-11-04; paths are under ``packages/coding-agent/src``:
the files and project overrides (``extensions/mcp/config.ts:92-157``), trust
(``core/trust-manager.ts``, ``core/project-trust.ts``, read 2026-11-11), the
variables and ``!`` commands (``core/resolve-config-value.ts``), the server
fields and exposure (``core/mcp-servers.ts:260-317``) and ``~`` expansion
(``extensions/mcp/runtime.ts:89-121``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.service.mcp.importers.base import (
    ImportContext,
    ImportWarning,
    McpImportResult,
    McpSourceError,
    Problem,
    SourceErrorKind,
    SourceReadError,
    SourceServer,
    Value,
    Var,
    WarningKind,
    by_name,
    convert,
    folders_down_to,
    is_str_list,
    is_str_mapping,
    missing_fields,
    path_matches,
    positive_number,
    read_json_object,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_VARIABLE_RE = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*)|([$!]))")
_DEFAULT_TIMEOUT_SECONDS = 60.0
_OVERRIDE_KEYS = frozenset({"enabled", "exposure", "toolExposure"})
_READ = ("type", "command", "args", "env", "url", "headers", "enabled", "description", "timeout", "oauth", "auth")
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
        entries: dict[str, tuple[dict[str, Any], str]] = {}
        for position, path in enumerate(files):
            document = read(path)
            if document is None:
                continue
            servers = document.get("mcpServers", {})
            if not isinstance(servers, dict):
                errors.append(McpSourceError(str(path), SourceErrorKind.INVALID, field="mcpServers"))
                continue
            for name, entry in servers.items():
                if not isinstance(entry, dict) or not _NAME_RE.fullmatch(name):
                    continue
                if position and not entry.keys() & {"command", "url", "type"}:
                    # In the project file, an override changes only how a global server is offered.
                    if name in entries and not entry.keys() - _OVERRIDE_KEYS:
                        base, base_path = entries[name]
                        entries[name] = ({**base, **entry}, base_path)
                    continue
                entries.pop(name, None)
                entries[name] = (entry, str(path))
        found = [_server(name, entry, path, ctx.home) for name, (entry, path) in entries.items()]
        candidates = tuple(convert(server) for server in by_name(server for server in found if server is not None))
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


def _expand_home(text: str, home: Path) -> str:
    return str(home) + text[1:] if text == "~" or text.startswith(("~/", "~\\")) else text


def _is_exposure(value: object) -> bool:
    return isinstance(value, str) and value in _EXPOSURES


def _server(name: str, entry: dict[str, Any], path: str, home: Path) -> SourceServer | None:
    """The server pi would run, or ``None`` for an entry it rejects."""
    prefix = f"mcpServers.{name}"
    kind = entry.get("type")
    exposure = entry.get("exposure", "codemode")
    rules = entry.get("toolExposure", {})
    timeout = positive_number(entry.get("timeout")) if "timeout" in entry else _DEFAULT_TIMEOUT_SECONDS
    if (
        not _is_exposure(exposure)
        or not isinstance(rules, dict)
        or not all(_is_exposure(rule) for rule in rules.values())
        or not isinstance(entry.get("enabled", True), bool)
        or not isinstance(entry.get("description", ""), str)
        or timeout is None
    ):
        return None
    if isinstance(entry.get("url"), str) and kind in (None, "http", "streamable-http"):
        transport = "http"
        if not entry["url"].startswith(("http://", "https://")) or not is_str_mapping(entry.get("headers", {})):
            return None
    elif isinstance(entry.get("command"), str) and "url" not in entry and kind in (None, "stdio"):
        transport = "stdio"
        if not is_str_list(entry.get("args", [])) or not is_str_mapping(entry.get("env", {})):
            return None
    else:
        return None
    texts: Mapping[str, str] = entry.get("env" if transport == "stdio" else "headers", {})
    group = f"{prefix}.{'env' if transport == 'stdio' else 'headers'}"
    values = {key: _value(text) for key, text in texts.items()}
    blocked = next((f"{group}.{key}" for key, text in texts.items() if text.startswith("!")), "")
    access = _tool_access(exposure, rules, prefix)
    warnings: list[ImportWarning | None] = [*access.warnings, _unread(entry, prefix)]
    authorized = any(key.lower() == "authorization" for key in texts)
    if transport == "http" and (entry.get("auth") or ("oauth" in entry and not authorized)):
        warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
    return SourceServer(
        name,
        path,
        transport,
        command=_expand_home(entry["command"], home) if transport == "stdio" else "",
        args=tuple(_expand_home(arg, home) for arg in entry.get("args", [])) if transport == "stdio" else (),
        env=values if transport == "stdio" else {},
        url=entry.get("url", ""),
        headers=values if transport == "http" else {},
        enabled=entry.get("enabled", True),
        description=entry.get("description", ""),
        timeout_seconds=timeout,
        allowed_tools=access.allowed,
        progressive=access.progressive,
        always_load=access.always_load,
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=blocked,
        blocked_problem=Problem.VALUE_FROM_FILE_OR_COMMAND,
        env_path=group,
        headers_path=group,
    )


def _unread(entry: dict[str, Any], prefix: str) -> ImportWarning | None:
    # The default working folder is pi's start folder, as iCode starts servers in its workspace.
    known = (*_READ_EXPOSURE, "cwd") if entry.get("cwd") in ("", ".") else _READ_EXPOSURE
    return missing_fields(entry, known, prefix)


def _value(text: str) -> Value:
    parts: list[str | Var] = []
    position = 0
    for match in _VARIABLE_RE.finditer(text):
        braced, bare, escaped = match.groups()
        parts.extend((text[position : match.start()], escaped or Var(braced or bare)))
        position = match.end()
    parts.append(text[position:])
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
        access.warnings.append(ImportWarning(WarningKind.NOT_IMPORTED, tuple(unread)))
    if base == _HIDDEN:
        access.allowed = tuple(granted)
        access.progressive = _ON_DEMAND in granted.values()
        access.always_load = tuple(t for t, k in granted.items() if k == _FULL) if access.progressive else ()
    else:
        access.progressive = base == _ON_DEMAND
        access.always_load = tuple(always)
    return access
