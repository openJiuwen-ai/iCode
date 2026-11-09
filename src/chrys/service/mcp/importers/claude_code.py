# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Claude Code.

Read (https://code.claude.com/docs/en/mcp, "MCP installation scopes"), lowest
precedence first, a same-named server replacing an earlier one:

- ``user``: top-level ``mcpServers`` of ``~/.claude.json`` (or
  ``$CLAUDE_CONFIG_DIR/.claude.json``);
- ``project``: ``mcpServers`` of ``.mcp.json`` in the Git root, then in the workspace;
- ``local``: ``projects[<Git root or workspace>].mcpServers`` of the same ``.claude.json``.

Values in ``env`` and ``headers`` may reference ``${VAR}`` or ``${VAR:-default}``;
iCode has no default syntax, so the default is dropped with a warning.
``CLAUDE_PROJECT_DIR``, which Claude Code sets for its stdio servers, must be
set by the user. Claude Code never sends a few credential variables to MCP
servers; a header referencing one is not imported, so the import can't start
sending the user's own credentials. ``timeout`` is in milliseconds.

A server the user turned off in Claude Code is imported turned off
(https://code.claude.com/docs/en/mcp, "Disable a server without removing it"
and "Project scope"): a ``.mcp.json`` server named in ``disabledMcpjsonServers``
of any settings file or of the project's ``.claude.json`` entry, and any other
server named in the project's ``disabledMcpServers``. Managed settings are not read.

Checked against the docs above and
https://code.claude.com/docs/en/settings-reference: the scopes, variables and
credential variables on 2026-11-04 (Claude Code 2.1.318), turning servers off
on 2026-11-10 (Claude Code 2.1.321). Recheck those sections when Claude Code
changes where it keeps servers or how it turns them off.
"""

from __future__ import annotations

import os
import re
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from chrys.service.mcp.importers.base import (
    ImportContext,
    ImportWarning,
    McpImportResult,
    McpSourceError,
    Problem,
    SourceReadError,
    SourceServer,
    Value,
    WarningKind,
    by_name,
    convert,
    git_root,
    is_str_list,
    is_str_mapping,
    missing_fields,
    positive_number,
    project_folders,
    read_json_object,
    split_variables,
    variables,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_DEFAULT_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*):-[^}]*\}")

# Claude Code reads these as empty in a remote server's headers [doc: mcp, "Credential variables that read as empty"].
CREDENTIAL_VARIABLES = frozenset(
    {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "HTTPS_PROXY", "NPM_TOKEN"}
)
CLAUDE_PROJECT_DIR = "CLAUDE_PROJECT_DIR"

# Read and dropped without a note: ``alwaysLoad`` only changes when Claude Code loads tools, and
# the rest are Claude Code's own bookkeeping.
_SILENT = ("type", "timeout", "alwaysLoad", "bareElicitationCapability", "discoveryCache", "role")
_STDIO_FIELDS = (*_SILENT, "command", "args", "env")
_HTTP_FIELDS = (*_SILENT, "url", "headers", "oauth")


class ClaudeCodeSource:
    @property
    def source_id(self) -> str:
        return "claude_code"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    def detect(self, ctx: ImportContext) -> bool:
        return _config_file(ctx).is_file() or any(path.is_file() for path in _project_files(ctx))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []

        def read(path: Path) -> dict[str, Any]:
            try:
                return read_json_object(path) or {}
            except SourceReadError as exc:
                errors.append(exc.at(path))
                return {}

        config_path = _config_file(ctx)
        config = read(config_path)
        project = _project_entry(config, ctx.workspace_cwd) if ctx.workspace_cwd else {}
        user = list(_servers(config.get("mcpServers"), config_path))
        shared = [server for path in _project_files(ctx) for server in _servers(read(path).get("mcpServers"), path)]
        local = list(_servers(project.get("mcpServers"), config_path))
        rejected = _names(project.get("disabledMcpjsonServers"))
        for path in _settings_files(ctx):
            rejected |= _names(read(path).get("disabledMcpjsonServers"))
        disabled = _names(project.get("disabledMcpServers"))
        # A .mcp.json server is turned off by disabledMcpjsonServers, any other by disabledMcpServers.
        layers = ((user, disabled), (shared, rejected), (local, disabled))
        servers = (replace(s, enabled=False) if s.name in off else s for group, off in layers for s in group)
        return McpImportResult(tuple(convert(server) for server in by_name(servers)), tuple(errors))


def _config_folder(ctx: ImportContext) -> Path | None:
    """``$CLAUDE_CONFIG_DIR``, if set; Claude Code keeps its files there instead of the home folder."""
    folder = ctx.environ.get("CLAUDE_CONFIG_DIR")
    if not folder:
        return None
    path = Path(folder)
    if not path.is_absolute() and ctx.workspace_cwd:
        path = Path(ctx.workspace_cwd) / path
    return path


def _config_file(ctx: ImportContext) -> Path:
    return (_config_folder(ctx) or ctx.home) / ".claude.json"


def _project_files(ctx: ImportContext) -> list[Path]:
    return [folder / ".mcp.json" for folder in project_folders(ctx.workspace_cwd)]


def _settings_files(ctx: ImportContext) -> list[Path]:
    """The settings files that can reject ``.mcp.json`` servers: the user's, the workspace's shared
    one, and the local one, which Claude Code keeps in the Git root (in the workspace before 2.1.211).
    """
    files = [(_config_folder(ctx) or ctx.home / ".claude") / "settings.json"]
    if ctx.workspace_cwd:
        files.append(Path(ctx.workspace_cwd) / ".claude" / "settings.json")
        files += [folder / ".claude" / "settings.local.json" for folder in project_folders(ctx.workspace_cwd)]
    return files


def _project_entry(config: Mapping[str, Any], workspace_cwd: str) -> dict[str, Any]:
    """What Claude Code keeps for the workspace's project: keyed by its Git root, else the workspace."""
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return {}
    folder = git_root(workspace_cwd) or workspace_cwd
    project = projects.get(folder)
    if project is None:
        wanted = _path_key(folder)
        project = next((value for key, value in projects.items() if _path_key(key) == wanted), None)
    return project if isinstance(project, dict) else {}


def _names(value: object) -> set[str]:
    return {name for name in value if isinstance(name, str)} if isinstance(value, list) else set()


def _path_key(path: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(path))
    except OSError, ValueError:
        return path


def _servers(servers: object, path: Path) -> Iterator[SourceServer]:
    if not isinstance(servers, dict):
        return
    for name, entry in servers.items():
        if isinstance(entry, dict) and name.strip() and (server := _server(name, entry, str(path))) is not None:
            yield server


def _server(name: str, entry: dict[str, Any], path: str) -> SourceServer | None:
    """The server Claude Code would run, or ``None`` for an entry it skips."""
    declared = entry.get("type", "stdio" if "url" not in entry else None)
    if declared == "stdio":
        return _stdio(name, entry, path)
    if declared in ("http", "streamable-http"):
        return _http(name, entry, path)
    if declared in ("sse", "ws"):
        return SourceServer(name, path, declared)
    # A url with no type, an SDK server or an unknown type: Claude Code skips it too.
    return None


def _stdio(name: str, entry: dict[str, Any], path: str) -> SourceServer | None:
    command, args, env = entry.get("command"), entry.get("args", []), entry.get("env", {})
    if not isinstance(command, str) or not command.strip() or not is_str_list(args) or not is_str_mapping(env):
        return None
    prefix = f"mcpServers.{name}"
    blocked = next(
        (
            f"{prefix}.{field}"
            for field, value in (("command", command), *((f"args[{i}]", arg) for i, arg in enumerate(args)))
            if _DOLLAR_RE.search(value)
        ),
        "",
    )
    values = {key: _value(text) for key, text in env.items()}
    warnings = [*_variable_warnings(env, values), missing_fields(entry, _STDIO_FIELDS, prefix)]
    return SourceServer(
        name,
        path,
        "stdio",
        command=command,
        args=tuple(args),
        env=values,
        timeout_seconds=_timeout(entry),
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=blocked,
        blocked_problem=Problem.VARIABLE_NOT_SUPPORTED,
        env_path=f"{prefix}.env",
    )


def _http(name: str, entry: dict[str, Any], path: str) -> SourceServer | None:
    url, headers = entry.get("url"), entry.get("headers", {})
    if not isinstance(url, str) or not url.strip() or not is_str_mapping(headers):
        return None
    prefix = f"mcpServers.{name}"
    values: dict[str, Value] = {}
    dropped: list[str] = []
    for key, text in headers.items():
        value = _value(text)
        # In any case: on Windows ${anthropic_api_key} resolves to ANTHROPIC_API_KEY.
        if any(variable.upper() in CREDENTIAL_VARIABLES for variable in variables(value)):
            dropped.append(f"{prefix}.headers.{key}")
        else:
            values[key] = value
    warnings = [
        *_variable_warnings({key: headers[key] for key in values}, values),
        ImportWarning(WarningKind.NOT_IMPORTED, tuple(dropped)) if dropped else None,
        ImportWarning(WarningKind.AUTH_NOT_IMPORTED) if "oauth" in entry else None,
        missing_fields(entry, _HTTP_FIELDS, prefix),
    ]
    return SourceServer(
        name,
        path,
        "http",
        url=url,
        headers=values,
        timeout_seconds=_timeout(entry),
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=f"{prefix}.url" if _DOLLAR_RE.search(url) else "",
        blocked_problem=Problem.VARIABLE_NOT_SUPPORTED,
        headers_path=f"{prefix}.headers",
    )


def _value(text: str) -> Value:
    return split_variables(text, _DOLLAR_RE)


def _variable_warnings(texts: Mapping[str, str], values: Mapping[str, Value]) -> Iterator[ImportWarning]:
    """Variables iCode needs set: those whose default is dropped, and ``CLAUDE_PROJECT_DIR``."""
    required = [match.group(1) for text in texts.values() for match in _DEFAULT_RE.finditer(text)]
    required += [name for value in values.values() for name in variables(value) if name == CLAUDE_PROJECT_DIR]
    if required:
        yield ImportWarning(WarningKind.VARIABLE_REQUIRED, tuple(dict.fromkeys(required)))


def _timeout(entry: dict[str, Any]) -> float | None:
    """``timeout`` in milliseconds as seconds; Claude Code ignores values under one second."""
    milliseconds = positive_number(entry.get("timeout"))
    return milliseconds / 1000 if milliseconds is not None and milliseconds >= 1000 else None
