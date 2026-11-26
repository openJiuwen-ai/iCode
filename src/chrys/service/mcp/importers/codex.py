# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Codex.

Read (https://developers.openai.com/codex/mcp), lowest precedence first:
``config.toml`` in ``$CODEX_HOME`` (default ``~/.codex``; a relative folder is
found from the workspace), then
``.codex/config.toml`` in every folder from the project root (the nearest
folder holding one of ``project_root_markers``, default ``.git``) down to the
workspace. As in Codex, a project file is read only from a folder the user
trusts (``projects."<folder>".trust_level = "trusted"`` in the user file, for
the folder itself, else the project root, else the repository's main
checkout), so a repository can't add or change servers on its own. A later
file's entry merges into an earlier same-named one, table by table, its other
values replacing; so a project can turn off or narrow a user server by naming
only the settings it changes. A server's file is the last one that sets its
``command`` or ``url``. A trusted config file that can't be read or parsed
leaves the effective servers unknown, so there are no import candidates.

Values in ``env`` and ``http_headers`` are literal. ``env_http_headers`` and
``bearer_token_env_var`` name variables, which become iCode references; Codex
leaves out a header whose variable is unset, while iCode reports a connection
error. Tool exposure omissions and deny-only tool filters can't be expressed,
so the server is imported with no tools. ``tool_timeout_sec`` defaults to
Codex's 300 seconds. ``env_vars`` names variables Codex passes on from its own
environment; iCode passes on nearly all of its environment, so they are left
out. iCode never passes on ``PYTHONHOME`` and ``PYTHONPATH``: naming either
there, unless ``env`` sets it, warns that a setting was not copied, and the
user can set it in Env.

An omitted ``cwd`` uses the session folder. An explicit relative ``cwd``
(including ``.``) instead starts at Codex's process launch folder, even when
its workspace is elsewhere; import preserves the relative path for iCode to resolve from the session folder. ``~`` and shell variables are literal paths.

Checked against https://github.com/openai/codex tag ``rust-v0.172.0``
(commit 3d86e43b07), read 2026-11-24. Paths are under ``codex-rs``: the files
read and trust (``config/src/loader/mod.rs``), how they merge
(``config/src/merge.rs``), the server fields
(``config/src/mcp_types.rs``), header variables and bearer tokens
(``rmcp-client/src/utils.rs``) and the 300-second default
(``codex-mcp/src/rmcp_client.rs``, ``DEFAULT_TOOL_TIMEOUT``).
The explicit ``cwd`` is passed to the subprocess unchanged; only its absence
gets the launcher's session-folder fallback
(``LocalStdioServerLauncher::launch_server`` in ``rmcp-client/src/stdio_server_launcher.rs`` and
``codex-mcp/src/rmcp_client.rs``). Local Codex 0.167.2 probes on
2026-11-12, through app-server threads with different ``cwd`` values and
``codex exec -C`` with different workspaces, confirmed: absent uses the
workspace, ``.`` and other relative paths use the launch folder, and an
absolute path stays fixed. Neither the config folder nor the importer's
current process folder establishes the original Codex launch folder.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

from chrys.foundation.platform import get_platform
from chrys.service.mcp.importers.base import (
    FieldUse,
    ImportContext,
    ImportWarning,
    McpImportCandidate,
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
    is_str_list,
    is_str_mapping,
    literal,
    located,
    main_checkout,
    merge_entries,
    missing_fields,
    positive_number,
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
    "cwd",
    "url",
    "http_headers",
    "env_http_headers",
    "bearer_token_env_var",
    "enabled",
    "enabled_tools",
    "disabled_tools",
    "omit_tools_from",
    "auth",
    "oauth",
    "oauth_resource",
    "environment_id",
}
_STDIO_ONLY = ("args", "env", "env_vars", "cwd")
_HTTP_ONLY = (
    "bearer_token_env_var",
    "http_headers_helper",
    "http_headers",
    "env_http_headers",
    "oauth",
    "oauth_resource",
    "auth",
)
_TOOL_EXPOSURES = {"direct", "deferred", "code_mode"}
# A zero timeout has no iCode equivalent: the server keeps iCode's default, with a warning.
_TIMEOUT = "tool_timeout_sec"
# What iCode leaves out of the environment a stdio server inherits. Codex passing one on
# can't be copied: a reference fails when it is unset and would bring back iCode's own value.
_NOT_INHERITED = frozenset({"PYTHONHOME", "PYTHONPATH"})


class CodexSource:
    @property
    def source_id(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "ChatGPT"

    def detect(self, ctx: ImportContext) -> bool:
        return _user_file(ctx).is_file()

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        entries: dict[str, tuple[object, str]] = {}
        user_path = _user_file(ctx)
        try:
            user = _read_toml(user_path)
        except SourceReadError as exc:
            return McpImportResult((), (exc.at(user_path),))
        documents: list[tuple[Path, dict[str, Any]]] = [(user_path, user)]
        for path in _project_files(ctx, user, user_path):
            try:
                documents.append((path, _read_toml(path)))
            except SourceReadError as exc:
                errors.append(exc.at(path))
        if errors:
            return McpImportResult((), tuple(errors))

        invalid_table: McpSourceError | None = None
        for path, document in documents:
            if "mcp_servers" not in document:
                continue
            table = document["mcp_servers"]
            if not isinstance(table, dict):
                # A scalar replaces the server map. A later table can restore it, starting empty.
                entries.clear()
                invalid_table = McpSourceError(str(path), SourceErrorKind.INVALID, field="mcp_servers")
                continue
            invalid_table = None
            for name, entry in table.items():
                if not isinstance(entry, dict):
                    # A scalar replaces the entire server table and must shadow the old definition.
                    entries[name] = (entry, str(path))
                    continue
                base, base_path = entries.get(name, ({}, str(path)))
                if not isinstance(base, dict):
                    base, base_path = {}, str(path)
                elif entry.keys() & {"command", "url"}:
                    base_path = str(path)
                entries[name] = (merge_entries(base, entry), base_path)
        if invalid_table is not None:
            return McpImportResult((), (invalid_table,))
        candidates: list[McpImportCandidate] = []
        for name, (entry, path) in entries.items():
            if not isinstance(entry, dict):
                errors.append(McpSourceError(path, SourceErrorKind.INVALID, field=f"mcp_servers.{name}"))
            elif field := _transport_conflict(entry):
                # An overlay may supply the conflicting field without replacing the command or URL.
                for source_path, document in reversed(documents):
                    table = document.get("mcp_servers")
                    source_entry = table.get(name) if isinstance(table, dict) else None
                    if isinstance(source_entry, dict) and field in source_entry:
                        path = str(source_path)
                        break
                errors.append(McpSourceError(path, SourceErrorKind.INVALID, field=f"mcp_servers.{name}.{field}"))
            elif (server := _server(name, entry, path)) is not None:
                candidates.append(convert(server))
        return McpImportResult(tuple(candidates), tuple(errors))


def _user_file(ctx: ImportContext) -> Path:
    """``config.toml`` in ``$CODEX_HOME``, default ``~/.codex``."""
    folder = ctx.environ.get("CODEX_HOME")
    return (located(folder, ctx) if folder else ctx.home / ".codex") / "config.toml"


def _project_files(ctx: ImportContext, user: dict[str, Any], user_path: Path) -> list[Path]:
    """The project files Codex reads, lowest precedence first: those in trusted folders."""
    if not ctx.workspace_cwd:
        return []
    projects = user.get("projects")
    levels = {
        key: entry["trust_level"]
        for key, entry in (projects.items() if isinstance(projects, dict) else ())
        if isinstance(entry, dict) and entry.get("trust_level") is not None
    }
    markers = user.get("project_root_markers", [".git"])
    # Codex walks the workspace's folders as spelled, nearest first; with no marker the project is
    # the workspace alone.
    workspace = Path(os.path.abspath(ctx.workspace_cwd))
    ancestors = [workspace, *workspace.parents]
    root = _marker_root(ancestors, markers if is_str_list(markers) else [".git"]) or workspace
    repo = _marker_root(ancestors, [".git"])
    # Codex knows no repository folder where it finds no main checkout (a submodule, say).
    main = main_checkout(str(repo)) if repo else None
    fallback = [root, *([Path(main)] if main else [])]
    files: list[Path] = []
    for folder in reversed(ancestors[: ancestors.index(root) + 1]):
        codex = folder / ".codex"
        if codex.is_dir() and not _same_folder(codex, user_path.parent) and _trusted(levels, [folder, *fallback]):
            files.append(codex / "config.toml")
    return files


def _same_folder(first: Path, second: Path) -> bool:
    """Whether both name one real folder, as Codex skips ``$CODEX_HOME`` among project folders."""
    try:
        return os.path.realpath(first) == os.path.realpath(second)
    except OSError, ValueError:
        return False


def _marker_root(ancestors: list[Path], markers: list[str]) -> Path | None:
    """The nearest folder holding a marker; a ``.git`` folder counts only with a ``HEAD``."""
    for folder in ancestors:
        for marker in markers:
            path = folder / marker
            if os.path.lexists(path) and not (marker == ".git" and path.is_dir() and not (path / "HEAD").exists()):
                return folder
    return None


def _trusted(levels: dict[str, object], folders: list[Path]) -> bool:
    """Codex's trust decision: the first of *folders* with a trust level decides."""
    for folder in folders:
        level = _trust_level(levels, folder)
        if level is not None:
            return level == "trusted"
    return False


def _trust_level(levels: dict[str, object], folder: Path) -> object:
    """The level Codex finds for *folder*: under its real path, else under its spelling as given.

    Keys are looked up exactly, so a key naming another path to the same folder is a separate entry,
    as in Codex. On Windows Codex lowercases (ASCII only) what it looks up: a key written that way
    wins, then the first key, in sort order, equal but for case.
    """
    try:
        real = os.path.realpath(folder)
    except OSError, ValueError:
        real = str(folder)
    windows = get_platform().is_windows
    for spelling in dict.fromkeys((real, str(folder))):
        lookup = _ascii_lower(spelling) if windows else spelling
        if lookup in levels:
            return levels[lookup]
        if windows:
            matches = sorted(key for key in levels if _ascii_lower(key) == lookup)
            if matches:
                return levels[matches[0]]
    return None


def _ascii_lower(text: str) -> str:
    return "".join(character.lower() if character.isascii() else character for character in text)


def _read_toml(path: Path) -> dict[str, Any]:
    text = read_text(path)
    if text is None:
        return {}
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SourceReadError(SourceErrorKind.UNPARSABLE, line=exc.lineno, column=exc.colno) from exc
    except (ValueError, RecursionError) as exc:
        # An integer too long to convert raises a plain ValueError.
        raise SourceReadError(SourceErrorKind.UNPARSABLE) from exc


def _server(name: str, entry: dict[str, Any], path: str) -> SourceServer | None:
    """The server Codex would run, or ``None`` for an entry it rejects."""
    prefix = f"mcp_servers.{name}"
    if not _shape_is_valid(entry):
        return None
    stdio = "command" in entry
    omitted = {"startup_timeout_sec": WarningKind.STARTUP_WAIT, "default_tools_approval_mode": WarningKind.APPROVAL}
    warnings: list[ImportWarning | None] = [missing_fields(entry, (*_READ, _TIMEOUT, *omitted), prefix)]
    warnings.extend(ImportWarning(kind, (f"{prefix}.{key}",)) for key, kind in omitted.items() if key in entry)
    headers, blocked, reason = ({}, "", ValueReason.INVALID) if stdio else _headers(entry, prefix)
    problem = Problem.VALUE_NOT_REPRESENTABLE
    if remote := _remote_setting(entry, prefix):
        blocked, problem = remote, Problem.REMOTE_ENVIRONMENT
    env: dict[str, Value] = {key: literal(text) for key, text in entry.get("env", {}).items()}
    if stdio:
        passed = [name for name in _passed_variables(entry) if name.upper() in _NOT_INHERITED]
        windows = get_platform().is_windows
        explicit = {key.upper() if windows else key for key in env}
        if any((name.upper() if windows else name) not in explicit for name in passed):
            # One "not copied" warning lists every such setting.
            unread = warnings[0]
            warnings[0] = ImportWarning(
                WarningKind.NOT_IMPORTED, (*(unread.items if unread else ()), f"{prefix}.env_vars")
            )
    elif entry.get("auth") in ("oauth", "chatgpt", "ema_auth") or "oauth" in entry or "oauth_resource" in entry:
        warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
    allowed: tuple[str, ...] | None = None
    enabled_tools, disabled_tools = entry.get("enabled_tools"), entry.get("disabled_tools", [])
    if entry.get("omit_tools_from", []):
        # A name allowlist cannot preserve the surfaces on which Codex exposes those names.
        allowed = ()
        warnings.append(ImportWarning(WarningKind.TOOLS_RESTRICTED, (f"{prefix}.omit_tools_from",)))
    elif enabled_tools is not None:
        allowed = tuple(tool for tool in enabled_tools if tool not in disabled_tools)
    elif disabled_tools:
        allowed = ()
        warnings.append(ImportWarning(WarningKind.TOOLS_RESTRICTED))
    timeout = entry.get(_TIMEOUT, _DEFAULT_TOOL_TIMEOUT)
    if timeout == 0:
        warnings.append(ImportWarning(WarningKind.DEFAULT_TIMEOUT, (f"{prefix}.{_TIMEOUT}",)))
    return SourceServer(
        name,
        path,
        "stdio" if stdio else "http",
        command=entry.get("command", ""),
        args=tuple(entry.get("args", [])),
        command_path=f"{prefix}.command",
        arg_paths=tuple(f"{prefix}.args[{i}]" for i in range(len(entry.get("args", [])))),
        env=env,
        url=entry.get("url", ""),
        url_path=f"{prefix}.url",
        headers=headers,
        enabled=entry.get("enabled", True),
        timeout_seconds=positive_number(timeout),
        allowed_tools=allowed,
        allowed_tools_path=f"{prefix}.enabled_tools",
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=blocked,
        blocked_problem=problem,
        blocked_reason=reason,
        blocked_use=FieldUse.HEADER_VALUE,
        env_path=f"{prefix}.env",
        headers_path=f"{prefix}.http_headers",
        cwd=literal(entry["cwd"]) if stdio and "cwd" in entry else None,
        cwd_path=f"{prefix}.cwd",
    )


def _remote_setting(entry: dict[str, Any], prefix: str) -> str:
    """The setting that runs the server in a remote environment, if any."""
    if entry.get("environment_id", "local") != "local":
        return f"{prefix}.environment_id"
    for index, item in enumerate(entry.get("env_vars", [])):
        if isinstance(item, dict) and item.get("source") == "remote":
            return f"{prefix}.env_vars[{index}]"
    return ""


def _passed_variables(entry: dict[str, Any]) -> list[str]:
    """The names in ``env_vars``: each a name, or a table with a ``name``."""
    names = [
        item if isinstance(item, str) else item.get("name")
        for item in entry.get("env_vars", [])
        if isinstance(item, str | dict)
    ]
    return [name for name in names if isinstance(name, str) and _VARIABLE.fullmatch(name)]


def _transport_conflict(entry: dict[str, Any]) -> str:
    """The first transport setting Codex rejects for the declared connection type."""
    if ("command" in entry) == ("url" in entry):
        return ""
    if "bearer_token" in entry:
        return "bearer_token"
    incompatible = _HTTP_ONLY if "command" in entry else _STDIO_ONLY
    return next((field for field in incompatible if field in entry), "")


def _shape_is_valid(entry: dict[str, Any]) -> bool:
    """Check the types of settings the import reads."""
    if ("command" in entry) == ("url" in entry):
        return False
    strings = ("command", "url", "bearer_token_env_var", "environment_id", "cwd")
    if any(key in entry and not isinstance(entry[key], str) for key in strings):
        return False
    if any(key in entry and not is_str_list(entry[key]) for key in ("args", "enabled_tools", "disabled_tools")):
        return False
    omissions = entry.get("omit_tools_from", [])
    if not is_str_list(omissions) or not _TOOL_EXPOSURES.issuperset(omissions):
        return False
    if any(key in entry and not is_str_mapping(entry[key]) for key in ("env", "http_headers", "env_http_headers")):
        return False
    if not isinstance(entry.get("enabled", True), bool) or not isinstance(entry.get("env_vars", []), list):
        return False
    timeout = entry.get(_TIMEOUT, 0)
    return not isinstance(timeout, bool) and isinstance(timeout, int | float) and timeout >= 0


def _headers(entry: dict[str, Any], prefix: str) -> tuple[dict[str, Value], str, ValueReason]:
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
                return {}, f"{prefix}.{group}.{key}", ValueReason.VARIABLE
            if seen.setdefault(key.lower(), value) != value:
                return {}, f"{prefix}.{group}.{key}", ValueReason.HEADER_CONFLICT
            effective[key.lower()] = (key, value)
    if "bearer_token_env_var" in entry:
        variable = entry["bearer_token_env_var"]
        if not _VARIABLE.fullmatch(variable):
            return {}, f"{prefix}.bearer_token_env_var", ValueReason.VARIABLE
        effective["authorization"] = ("Authorization", ("Bearer ", Var(variable)))
    return dict(effective.values()), "", ValueReason.INVALID
