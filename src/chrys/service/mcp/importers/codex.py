# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Codex.

Read (https://developers.openai.com/codex/mcp), lowest precedence first:
``config.toml`` in ``$CODEX_HOME`` (default ``~/.codex``), then
``.codex/config.toml`` in the Git root and in the workspace. As in Codex, a
later file's entry merges into an earlier same-named one, table by table, its
other values replacing; so a project can turn off or narrow a user server by
naming only the settings it changes. A server's file is the last one that sets
its ``command`` or ``url``.

Values in ``env`` and ``http_headers`` are literal. ``env_http_headers`` and
``bearer_token_env_var`` name variables, which become iCode references; Codex
leaves out a header whose variable is unset, iCode needs it set, hence a
warning. A deny-only tool filter (``disabled_tools`` without
``enabled_tools``) can't be expressed, so the server is imported with no
tools. ``tool_timeout_sec`` defaults to Codex's 300 seconds.

Checked against https://github.com/openai/codex tag ``rust-v0.166.0``
(commit 6ff1e1ca3b), read 2026-11-04; paths are under ``codex-rs``: the files
read (``config/src/loader/mod.rs``), how they merge
(``config/src/merge.rs``, read again 2026-11-10), the server fields
(``config/src/mcp_types.rs``), header variables and bearer tokens
(``rmcp-client/src/utils.rs``) and the 300-second default
(``codex-mcp/src/rmcp_client.rs:122``).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

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
    convert,
    is_str_list,
    is_str_mapping,
    literal,
    merge_entries,
    missing_fields,
    positive_number,
    project_folders,
    read_text,
)

_VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# An HTTP header name (RFC 9110 token); Codex skips a header with any other name.
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_DEFAULT_TOOL_TIMEOUT = 300.0

_READ = {
    "command",
    "args",
    "env",
    "env_vars",
    "url",
    "http_headers",
    "env_http_headers",
    "bearer_token_env_var",
    "enabled",
    "enabled_tools",
    "disabled_tools",
    "auth",
    "oauth",
    "oauth_resource",
    "environment_id",
}
# A zero timeout has no iCode equivalent: the server keeps iCode's default, with a warning.
_TIMEOUT = "tool_timeout_sec"


class CodexSource:
    @property
    def source_id(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    def detect(self, ctx: ImportContext) -> bool:
        return any(path.is_file() for path in _files(ctx))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        entries: dict[str, tuple[dict[str, Any], str]] = {}
        for path in _files(ctx):
            try:
                document = _read_toml(path)
            except SourceReadError as exc:
                errors.append(exc.at(path))
                continue
            table = document.get("mcp_servers", {})
            if not isinstance(table, dict):
                errors.append(McpSourceError(str(path), SourceErrorKind.INVALID, field="mcp_servers"))
                continue
            for name, entry in table.items():
                if not isinstance(entry, dict):
                    continue
                base, base_path = entries.get(name, ({}, str(path)))
                if entry.keys() & {"command", "url"}:
                    base_path = str(path)
                entries[name] = (merge_entries(base, entry), base_path)
        found = (_server(name, entry, path) for name, (entry, path) in entries.items())
        return McpImportResult(tuple(convert(server) for server in found if server is not None), tuple(errors))


def _files(ctx: ImportContext) -> list[Path]:
    home = Path(ctx.environ.get("CODEX_HOME") or ctx.home / ".codex")
    if not home.is_absolute() and ctx.workspace_cwd:
        home = Path(ctx.workspace_cwd) / home
    user = home / "config.toml"
    projects = [folder / ".codex" / "config.toml" for folder in project_folders(ctx.workspace_cwd)]
    return [user, *(path for path in projects if path != user)]


def _read_toml(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SourceReadError(SourceErrorKind.UNPARSABLE, line=exc.lineno, column=exc.colno) from exc
    except RecursionError as exc:
        raise SourceReadError(SourceErrorKind.UNPARSABLE) from exc


def _server(name: str, entry: dict[str, Any], path: str) -> SourceServer | None:
    """The server Codex would run, or ``None`` for an entry it rejects."""
    prefix = f"mcp_servers.{name}"
    if not _shape_is_valid(entry):
        return None
    warnings: list[ImportWarning | None] = [missing_fields(entry, (*_READ, _TIMEOUT), prefix)]
    stdio = "command" in entry
    headers, blocked = ({}, "") if stdio else _headers(entry, prefix)
    problem = Problem.VALUE_NOT_REPRESENTABLE
    if remote := _remote_setting(entry, prefix):
        blocked, problem = remote, Problem.REMOTE_ENVIRONMENT
    if not stdio:
        required = [*entry.get("env_http_headers", {}).values()]
        warnings.append(ImportWarning(WarningKind.VARIABLE_REQUIRED, tuple(required)) if required else None)
        if entry.get("auth") in ("oauth", "chatgpt", "ema_auth") or "oauth" in entry or "oauth_resource" in entry:
            warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
    allowed: tuple[str, ...] | None = None
    enabled_tools, disabled_tools = entry.get("enabled_tools"), entry.get("disabled_tools", [])
    if enabled_tools is not None:
        allowed = tuple(tool for tool in enabled_tools if tool not in disabled_tools)
    elif disabled_tools:
        allowed = ()
        warnings.append(ImportWarning(WarningKind.TOOLS_RESTRICTED))
    timeout = entry.get(_TIMEOUT, _DEFAULT_TOOL_TIMEOUT)
    if timeout == 0:
        warnings.append(ImportWarning(WarningKind.NOT_IMPORTED, (f"{prefix}.{_TIMEOUT}",)))
    return SourceServer(
        name,
        path,
        "stdio" if stdio else "http",
        command=entry.get("command", ""),
        args=tuple(entry.get("args", [])),
        env={key: literal(text) for key, text in entry.get("env", {}).items()},
        url=entry.get("url", ""),
        headers=headers,
        enabled=entry.get("enabled", True),
        timeout_seconds=positive_number(timeout),
        allowed_tools=allowed,
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=blocked,
        blocked_problem=problem,
        env_path=f"{prefix}.env",
        headers_path=f"{prefix}.http_headers",
    )


def _remote_setting(entry: dict[str, Any], prefix: str) -> str:
    """The setting that runs the server in a remote environment, if any."""
    if entry.get("environment_id", "local") != "local":
        return f"{prefix}.environment_id"
    for index, item in enumerate(entry.get("env_vars", [])):
        if isinstance(item, dict) and item.get("source") == "remote":
            return f"{prefix}.env_vars[{index}]"
    return ""


def _shape_is_valid(entry: dict[str, Any]) -> bool:
    """Codex's own checks on the settings the import reads."""
    if ("command" in entry) == ("url" in entry):
        return False
    strings = ("command", "url", "bearer_token_env_var", "environment_id")
    if any(key in entry and not isinstance(entry[key], str) for key in strings):
        return False
    if any(key in entry and not is_str_list(entry[key]) for key in ("args", "enabled_tools", "disabled_tools")):
        return False
    if any(key in entry and not is_str_mapping(entry[key]) for key in ("env", "http_headers", "env_http_headers")):
        return False
    if not isinstance(entry.get("enabled", True), bool) or not isinstance(entry.get("env_vars", []), list):
        return False
    timeout = entry.get(_TIMEOUT, 0)
    return not isinstance(timeout, bool) and isinstance(timeout, int | float) and timeout >= 0


def _headers(entry: dict[str, Any], prefix: str) -> tuple[dict[str, Value], str]:
    """The headers Codex sends, and the setting to refuse when it can't be imported.

    A variable header replaces a literal one of the same name, and a bearer
    token replaces ``Authorization``. Two names differing only in case with
    different values have no stable winner in Codex, so the import refuses them.
    """
    effective: dict[str, tuple[str, Value]] = {}
    for group, values in (
        ("http_headers", {key: literal(text) for key, text in entry.get("http_headers", {}).items()}),
        ("env_http_headers", {key: (Var(text),) for key, text in entry.get("env_http_headers", {}).items()}),
    ):
        seen: dict[str, Value] = {}
        for key, value in values.items():
            if not _HEADER_NAME.fullmatch(key):
                continue
            if isinstance(value[0], Var) and not _VARIABLE.fullmatch(value[0].name):
                return {}, f"{prefix}.{group}.{key}"
            if seen.setdefault(key.lower(), value) != value:
                return {}, f"{prefix}.{group}.{key}"
            effective[key.lower()] = (key, value)
    if "bearer_token_env_var" in entry:
        variable = entry["bearer_token_env_var"]
        if not _VARIABLE.fullmatch(variable):
            return {}, f"{prefix}.bearer_token_env_var"
        effective["authorization"] = ("Authorization", ("Bearer ", Var(variable)))
    return dict(effective.values()), ""
