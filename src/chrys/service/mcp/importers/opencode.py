# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in opencode.

Read (https://opencode.ai/docs/config/), lowest precedence first:

- ``config.json``, ``opencode.json`` and ``opencode.jsonc`` in the global
  folder ``$XDG_CONFIG_HOME/opencode`` (default ``~/.config/opencode``);
- the file ``$OPENCODE_CONFIG`` names;
- ``opencode.json`` and ``opencode.jsonc`` in every folder from the Git root
  (the filesystem root outside a repository) down to the workspace;
- ``opencode.json`` and ``opencode.jsonc`` in each ``.opencode`` folder in the
  same folders, the workspace's first (so a folder above it wins, as in
  opencode), then in ``~/.opencode`` and in ``$OPENCODE_CONFIG_DIR``.

``OPENCODE_DISABLE_PROJECT_CONFIG`` (``true`` or ``1``) skips the files of the
Git root and the folders below it. ``OPENCODE_CONFIG_CONTENT`` and managed
settings are not read.

Files are JSON with comments and trailing commas. ``{env:NAME}`` in
``environment`` and ``headers`` values becomes an iCode reference, with a
warning to set it: opencode reads an unset variable as empty, iCode refuses
to start the server. A value
read from a file (``{file:path}``) or a variable anywhere else can't be
imported. ``timeout`` is in milliseconds, falling back to
``experimental.mcp_timeout`` (the last file setting it wins) and then
opencode's 60 seconds.

As in opencode, a later file's entry merges into an earlier same-named one,
object by object, its other values replacing; so ``{"enabled": false}`` alone
turns off a server an earlier file defines. A server's file is the last one
that sets its ``type``.

Checked against https://github.com/anomalyco/opencode tag ``v1.19.5``
(commit 810c0eba1a), read 2026-11-10 unless noted: the files read
(``packages/opencode/src/config/config.ts:272-274, 415-445``,
``packages/opencode/src/config/paths.ts``, read 2026-11-11), each checked on its
own, then folded with remeda's ``mergeDeep``
(``packages/opencode/src/config/config.ts:42, 188-198``); an ``mcp``
entry is a full server or ``{enabled}`` only, and ``experimental.mcp_timeout``
is in milliseconds (``packages/core/src/v1/config/config.ts:113-115, 182-184``,
``packages/core/src/v1/config/mcp.ts``); the 60-second default
(``packages/opencode/src/mcp/index.ts:661-684``), read 2026-11-04. opencode 2
(``v2.x`` tags, npm ``@opencode/cli``) is not followed.
"""

from __future__ import annotations

import re
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
    WarningKind,
    convert,
    folders_down_to,
    git_root,
    is_str_list,
    is_str_mapping,
    merge_entries,
    missing_fields,
    parse_json_object,
    positive_number,
    read_text,
    split_variables,
    variables,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_ENV_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
# Any reference opencode expands: a variable (whatever its name) or a file.
_REFERENCE_RE = re.compile(r"\{(?:env|file):[^}]*\}")
_FILE_RE = re.compile(r"\{file:[^}]*\}")
_DEFAULT_TIMEOUT_SECONDS = 60.0
_LOCAL_FIELDS = ("type", "command", "environment", "enabled", "timeout")
_REMOTE_FIELDS = ("type", "url", "headers", "enabled", "timeout", "oauth")

# A string, or a comment, in JSONC; and a comma before a closing bracket, outside strings. An unclosed
# string or block comment runs to the end of the text, so no position is scanned twice.
_COMMENT_RE = re.compile(r'("(?:\\.|[^"\\])*"?)|//[^\n]*|/\*.*?(?:\*/|\Z)', re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r'("(?:\\.|[^"\\])*"?)|,(?=\s*[}\]])', re.DOTALL)


class OpenCodeSource:
    @property
    def source_id(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "OpenCode"

    def detect(self, ctx: ImportContext) -> bool:
        return any(path.is_file() for path in _files(ctx))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        entries: dict[str, tuple[dict[str, Any], str]] = {}
        timeout = _DEFAULT_TIMEOUT_SECONDS
        for path in _files(ctx):
            try:
                text = read_text(path)
                document = {} if text is None else parse_json_object(strip_jsonc(text))
            except SourceReadError as exc:
                errors.append(exc.at(path))
                continue
            table = document.get("mcp", {})
            if not isinstance(table, dict):
                errors.append(McpSourceError(str(path), SourceErrorKind.INVALID, field="mcp"))
                continue
            experimental = document.get("experimental")
            fallback = positive_number(experimental.get("mcp_timeout")) if isinstance(experimental, dict) else None
            if fallback is not None:
                timeout = fallback / 1000
            for name, entry in table.items():
                if not isinstance(entry, dict):
                    continue
                base, base_path = entries.get(name, ({}, str(path)))
                if "type" in entry:
                    base_path = str(path)
                entries[name] = (merge_entries(base, entry), base_path)
        found = (_server(name, entry, path, timeout) for name, (entry, path) in entries.items())
        return McpImportResult(tuple(convert(server) for server in found if server is not None), tuple(errors))


def _files(ctx: ImportContext) -> list[Path]:
    environ = ctx.environ
    global_folder = Path(environ.get("XDG_CONFIG_HOME") or ctx.home / ".config") / "opencode"
    names = ("opencode.json", "opencode.jsonc")
    files = [global_folder / name for name in ("config.json", *names)]
    if custom := environ.get("OPENCODE_CONFIG"):
        files.append(_absolute(custom, ctx))
    # Folders in opencode's order, each kept where it first appears, compared as opencode compares
    # them, as text: the global folder first, so OPENCODE_CONFIG_DIR spelling it is read there.
    folders = [str(global_folder)]
    if ctx.workspace_cwd and environ.get("OPENCODE_DISABLE_PROJECT_CONFIG", "").lower() not in ("true", "1"):
        chain = folders_down_to(ctx.workspace_cwd, git_root(ctx.workspace_cwd))
        files += [folder / name for folder in chain for name in names]
        folders += [str(folder / ".opencode") for folder in reversed(chain)]
    folders.append(str(ctx.home / ".opencode"))
    config_dir = environ.get("OPENCODE_CONFIG_DIR") or None
    if config_dir is not None:
        folders.append(config_dir)
    read = [folder for folder in dict.fromkeys(folders) if folder.endswith(".opencode") or folder == config_dir]
    return files + [_absolute(folder, ctx) / name for folder in read for name in names]


def _absolute(path: str, ctx: ImportContext) -> Path:
    """*path* as opencode, started in the workspace, reads it."""
    return Path(ctx.workspace_cwd or "") / path if not Path(path).is_absolute() else Path(path)


def strip_jsonc(text: str) -> str:
    """*text* as plain JSON: comments blanked (keeping line breaks), trailing commas dropped.

    Raises :class:`SourceReadError` for a block comment that is never closed.
    """

    def blank(match: re.Match[str]) -> str:
        if match.group(1):
            return match.group(1)
        comment = match.group()
        if comment.startswith("/*") and (len(comment) < 4 or not comment.endswith("*/")):
            raise SourceReadError(SourceErrorKind.UNPARSABLE)
        return re.sub(r"[^\r\n]", " ", comment)

    return _TRAILING_COMMA_RE.sub(lambda m: m.group(1) or " ", _COMMENT_RE.sub(blank, text))


def _server(name: str, entry: dict[str, Any], path: str, default_timeout: float) -> SourceServer | None:
    """The server opencode would run, or ``None`` for an entry it rejects."""
    prefix = f"mcp.{name}"
    enabled = entry.get("enabled", True)
    milliseconds = positive_number(entry.get("timeout"))
    if not isinstance(enabled, bool) or ("timeout" in entry and milliseconds is None):
        return None
    timeout = milliseconds / 1000 if milliseconds is not None else default_timeout
    if entry.get("type") == "local":
        command, env = entry.get("command"), entry.get("environment", {})
        if not is_str_list(command) or not command or not command[0] or not is_str_mapping(env):
            return None
        values, value_refusal = _values(env, f"{prefix}.environment")
        fields = [(prefix, name), *((f"{prefix}.command[{i}]", part) for i, part in enumerate(command))]
        field, problem = _first_refusal(fields) or value_refusal or ("", Problem.VALUE_NOT_REPRESENTABLE)
        warnings = [missing_fields(entry, _LOCAL_FIELDS, prefix), _variables_required(values)]
        return SourceServer(
            name,
            path,
            "stdio",
            command=command[0],
            args=tuple(command[1:]),
            env=values,
            enabled=enabled,
            timeout_seconds=timeout,
            warnings=tuple(warning for warning in warnings if warning is not None),
            blocked_field=field,
            blocked_problem=problem,
            env_path=f"{prefix}.environment",
        )
    if entry.get("type") == "remote":
        url, headers = entry.get("url"), entry.get("headers", {})
        if not isinstance(url, str) or not url.strip() or not is_str_mapping(headers):
            return None
        values, value_refusal = _values(headers, f"{prefix}.headers")
        fields = [(prefix, name), (f"{prefix}.url", url)]
        field, problem = _first_refusal(fields) or value_refusal or ("", Problem.VALUE_NOT_REPRESENTABLE)
        warnings = [missing_fields(entry, _REMOTE_FIELDS, prefix), _variables_required(values)]
        # opencode signs in with OAuth whenever the server asks, unless ``oauth`` is false; so warn
        # unless headers authenticate.
        oauth = entry.get("oauth")
        if oauth is not False and (oauth is not None or not values):
            warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
        return SourceServer(
            name,
            path,
            "http",
            url=url,
            headers=values,
            enabled=enabled,
            timeout_seconds=timeout,
            warnings=tuple(warning for warning in warnings if warning is not None),
            blocked_field=field,
            blocked_problem=problem,
            headers_path=f"{prefix}.headers",
        )
    return None


def _variables_required(values: Mapping[str, Value]) -> ImportWarning | None:
    """The variables *values* reference, which opencode reads as empty when unset and iCode requires."""
    names = dict.fromkeys(name for value in values.values() for name in variables(value))
    return ImportWarning(WarningKind.VARIABLE_REQUIRED, tuple(names)) if names else None


def _first_refusal(fields: list[tuple[str, str]]) -> tuple[str, Problem] | None:
    """The first name, command part or URL holding a reference, which iCode reads in none of them."""
    for field, text in fields:
        if _FILE_RE.search(text):
            return field, Problem.VALUE_FROM_FILE_OR_COMMAND
        if _REFERENCE_RE.search(text):
            return field, Problem.VARIABLE_NOT_SUPPORTED
    return None


def _values(texts: Mapping[str, str], path: str) -> tuple[dict[str, Value], tuple[str, Problem] | None]:
    """The values as iCode references, and the first setting that can't be imported, with why."""
    values: dict[str, Value] = {}
    for key, text in texts.items():
        if refusal := _first_refusal([(f"{path}.{key}", key)]):
            return {}, refusal
        if _FILE_RE.search(text):
            return {}, (f"{path}.{key}", Problem.VALUE_FROM_FILE_OR_COMMAND)
        # Left after the usable references: a variable whose name iCode can't reference.
        if _REFERENCE_RE.search(_ENV_RE.sub("", text)):
            return {}, (f"{path}.{key}", Problem.VALUE_NOT_REPRESENTABLE)
        values[key] = split_variables(text, _ENV_RE)
    return values, None
