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

A relative folder or file in a variable is found from the workspace.
``OPENCODE_DISABLE_PROJECT_CONFIG`` (``true`` or ``1``) skips the files of the
Git root and the folders below it. ``OPENCODE_CONFIG_CONTENT`` and managed
settings are not read.

Files are JSON with comments and trailing commas. References are recognized
in the source text before JSON decoding, as in OpenCode. ``{env:NAME}`` in
``environment``, ``headers`` and ``cwd`` values becomes an iCode reference.
opencode reads an unset variable as empty, while iCode refuses
to start the server. A value
read from a file (``{file:path}``) or a variable anywhere else can't be
imported. A server's ``timeout`` is in milliseconds. The fallback is the
last file's ``experimental.mcp_timeout`` or equivalent table-level
``mcp.timeout``, then opencode's 60 seconds. V1 compatibility accepts the
``mcp.servers`` container and certain per-server phase timeouts.

As in opencode, a later file's entry merges into an earlier same-named one,
object by object, its other values replacing; so ``{"enabled": false}`` alone
turns off a server an earlier file defines. A server's file is the last one
that sets its ``type``. A rejected file in the regular global layer drops that
layer. A rejected overlay makes the source configuration unavailable.
A file the importer cannot read may contain applicable policy and blocks the
source. A transport reference blocks its server and any uncertain replacement in
the same file. A name reference also blocks servers it could name. The file
can still contribute independent servers and tighter enablement or tool restrictions.

When a top-level ``tools`` or ``permission`` rule may hide a server's tools,
import sets the server's scope to no tools and asks the user to choose them.
OpenCode applies top-level ``permission`` to every agent
(``packages/opencode/src/agent/agent.ts:138``), turns ``tools`` into rules
that ``permission`` overrides key by key (``packages/opencode/src/config/config.ts:567-577``),
and hides a tool when the last rule matching it denies every pattern
(``packages/opencode/src/permission/index.ts:186-212``); a bare action means
``{"*": action}`` (``packages/core/src/v1/config/permission.ts:40-41``).
A later ``*`` or ``<server>_*`` rule that hides nothing clears earlier hides
for that server. Later files override the value of a rule key in its original
position. Agent-level rules and ``OPENCODE_PERMISSION`` are not read.
OpenCode replaces each UTF-16 code unit outside ``[A-Za-z0-9_-]`` with ``_``
before joining the server and tool names (``packages/opencode/src/mcp/catalog.ts:117-119``).

Checked against https://github.com/anomalyco/opencode tag ``v1.19.6``
(commit f4e363c829), latest V1 tag checked 2026-11-24. The files read are listed in
``packages/opencode/src/config/config.ts:272-274, 415-445`` and
``packages/opencode/src/config/paths.ts``. Each is checked on its own, then
folded with remeda's ``mergeDeep`` (``packages/opencode/src/config/config.ts:42, 188-198``).
An ``mcp`` entry is a full server or ``{enabled}`` only, and ``experimental.mcp_timeout``
is in milliseconds (``packages/core/src/v1/config/config.ts:113-115, 182-184``,
``packages/core/src/v1/config/mcp.ts``). Tool calls fall back to the MCP SDK's
60-second request timeout (``packages/opencode/src/mcp/index.ts:661-684``,
``packages/opencode/src/mcp/catalog.ts:54-66``). Local ``cwd``
uses ``path.resolve(baseDir, mcp.cwd)`` when nonempty, otherwise ``baseDir``
(``packages/opencode/src/mcp/index.ts:346``); static paths are normalized
lexically on import but stay relative to the session, and ``~`` stays literal.
Variable cwd values stay deferred with a warning: future values can introduce
literal tildes, POSIX symlink traversal or Windows drive-relative paths that
iCode resolves differently or refuses. Import never reads those values.
opencode 2
(``v2.x`` tags, npm ``@opencode/cli``) is not followed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

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
    SourceReaderRefusal,
    SourceReadError,
    SourceServer,
    Value,
    ValueReason,
    WarningKind,
    convert,
    folders_down_to,
    git_root,
    is_str_list,
    is_str_mapping,
    lexical_cwd,
    literal,
    located,
    merge_entries,
    missing_fields,
    normalize_js_server_name,
    parse_json_object,
    positive_number,
    read_text,
    split_variables,
    variables,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

_SOURCE_REFERENCE_RE = re.compile(r"\{(?:env|file):[^}]+\}")
_REFERENCE_RE = re.compile(r"\x00!?(?:env|file):[^}]*\}")
_ENV_RE = re.compile(r"\x00env:([A-Za-z_][A-Za-z0-9_]*)\}")
_FILE_RE = re.compile(r"\x00!?file:[^}]*\}")
_REFERENCE_START_RE = re.compile(r"\x00!?")
_DEFAULT_TIMEOUT_SECONDS = 60.0
_LOCAL_FIELDS = ("type", "command", "environment", "enabled", "timeout", "cwd")
_REMOTE_FIELDS = ("type", "url", "headers", "enabled", "timeout", "oauth")
_SERVER_FIELDS = frozenset((*_LOCAL_FIELDS, *_REMOTE_FIELDS))
_TIMEOUT_PHASES = ("startup", "catalog", "execution")
_OAUTH_STRING_FIELDS = ("clientId", "clientSecret", "scope", "redirectUri")
_NATIVE_OAUTH_KEYS = {
    "client_id": "clientId",
    "client_secret": "clientSecret",
    "callback_port": "callbackPort",
    "redirect_uri": "redirectUri",
    "scope": "scope",
}

# JSONC strings, comments, and source references outside strings. An unclosed string or block
# comment runs to the end of the text so the scanner does not repeatedly try to match its contents.
_JSONC_TOKEN_RE = re.compile(r'("(?:\\.|[^"\\])*"?)|//[^\r\n]*|/\*.*?(?:\*/|\Z)|(\{(?:env|file):)', re.DOTALL)
_TRAILING_COMMA_RE = re.compile(r'("(?:\\.|[^"\\])*"?)|,(?=\s*[}\]])', re.DOTALL)
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"?', re.DOTALL)


class _ReferencedText(str):
    """A decoded JSON string with the locations of its source-spelled references."""

    marked: str

    def __new__(cls, marked: str) -> _ReferencedText:
        text = super().__new__(cls, _REFERENCE_START_RE.sub("{", marked))
        text.marked = marked
        return text


class OpenCodeSource:
    @property
    def source_id(self) -> str:
        return "opencode"

    @property
    def display_name(self) -> str:
        return "OpenCode"

    def detect(self, ctx: ImportContext) -> bool:
        return any(path.is_file() for path, _ in _files(ctx))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []
        entries: dict[str, tuple[dict[str, Any], str]] = {}
        tools: dict[str, Any] = {}
        permission: dict[str, Any] = {}
        blocked: dict[str, McpImportCandidate] = {}
        referenced_names: list[tuple[McpImportCandidate, tuple[str, str]]] = []
        conditional_rules: list[dict[str, tuple[bool, str]]] = []
        timeout = _DEFAULT_TIMEOUT_SECONDS
        for path, global_file in _files(ctx):
            try:
                layer, rejected = _read_layer(path)
            except SourceReadError as exc:
                error = exc.at(path)
                if isinstance(exc, SourceReaderRefusal) or exc.kind is SourceErrorKind.UNREADABLE:
                    # An unreadable file can still restrict servers defined in other files.
                    return McpImportResult((), (*errors, error))
                layer, rejected = None, [error]
            if rejected:
                errors.extend(rejected)
                if not global_file:
                    return McpImportResult((), tuple(errors))
                # OpenCode falls back to defaults for its entire regular global configuration.
                entries.clear()
                tools.clear()
                permission.clear()
                blocked.clear()
                referenced_names.clear()
                conditional_rules.clear()
                timeout = _DEFAULT_TIMEOUT_SECONDS
            if layer is None or (global_file and errors):
                continue
            file_tools, file_permission = layer.tools, layer.permission
            if layer.referenced_rules:
                conditional_rules.append(layer.referenced_rules)
            issue = next(iter(layer.unresolved.values()), None)
            if issue is None and layer.rule_refusal is not None:
                issue = _refused_candidate("", str(path), layer.rule_refusal)
            if issue is not None:
                # If substitution rejects this file, its allow rules never apply. Keep only the
                # restrictions it could add, alongside those from the independently readable files.
                conditional_rules.append(_tool_rules(file_tools, file_permission))
            else:
                tools = merge_entries(tools, file_tools)
                permission = merge_entries(permission, file_permission)
                if layer.timeout is not None:
                    timeout = layer.timeout / 1000
            blocked.update(layer.unresolved)
            referenced_names.extend(
                (candidate, _reference_edges(name))
                for name, candidate in layer.unresolved.items()
                if candidate.field_use is FieldUse.NAME
            )
            for name, entry in layer.entries.items():
                base, base_path = entries.get(name, ({}, str(path)))
                if issue is not None and base:
                    if "type" in entry:
                        blocked.setdefault(name, replace(issue, name=name))
                    if entry.get("enabled") is not False:
                        continue
                    entry = {"enabled": False}
                if "type" in entry:
                    base_path = str(path)
                entries[name] = (merge_entries(base, entry), base_path)
        rules = _tool_rules(tools, permission)
        candidates: list[McpImportCandidate] = []
        for key in dict.fromkeys((*entries, *blocked)):
            # A key holding a reference is a str subclass carrying its source spelling; what
            # leaves the importer is plain text (display sanitizers accept only exact str).
            name = str(key)
            refusal = blocked.get(name)
            if refusal is None:
                refusal = next(
                    (candidate for candidate, edges in referenced_names if _could_be_name(name, edges)), None
                )
            if refusal is not None:
                candidates.append(replace(refusal, name=name))
            else:
                entry, path = entries[name]
                if (server := _server(name, entry, path, timeout)) is not None:
                    for applicable in (rules, *conditional_rules):
                        server = _restrict_tools(server, applicable)
                    candidates.append(convert(server))
        return McpImportResult(tuple(candidates), tuple(errors))


@dataclass
class _Layer:
    entries: dict[str, dict[str, Any]]
    unresolved: dict[str, McpImportCandidate]
    tools: dict[str, Any]
    permission: dict[str, Any]
    timeout: float | None
    referenced_rules: dict[str, tuple[bool, str]]
    rule_refusal: Refusal | None


def _read_layer(path: Path) -> tuple[_Layer | None, list[McpSourceError]]:
    text = read_text(path)
    if text is None:
        return None, []
    document = _parse_document(text)
    table = document.get("mcp", {})
    if not isinstance(table, dict):
        return None, [McpSourceError(str(path), SourceErrorKind.INVALID, field="mcp")]
    global_timeout = _global_timeout(table.get("timeout"))
    entries, unresolved, invalid = _decoded_entries(table, str(path), global_timeout)
    if invalid:
        return None, [McpSourceError(str(path), SourceErrorKind.INVALID, field=field) for field in invalid]
    tools, permission = document.get("tools"), document.get("permission")
    if isinstance(permission, str):
        permission = {"*": permission}
    static_tools, referenced_tools, tools_refusal = _partition_rules("tools", tools)
    static_permission, referenced_permission, permission_refusal = _partition_rules("permission", permission)
    return _Layer(
        entries,
        unresolved,
        static_tools,
        static_permission,
        _file_timeout(document, global_timeout),
        _referenced_tool_denials(referenced_tools, referenced_permission),
        tools_refusal or permission_refusal,
    ), []


def _parse_document(text: str) -> dict[str, Any]:
    """Tag references before decoding so escaped braces and names stay literal."""
    text = strip_jsonc(text)
    if not _SOURCE_REFERENCE_RE.search(text):
        return parse_json_object(text)
    if r"\u0000" in text:
        # A source NUL could be confused with the tag marking a live reference.
        raise SourceReadError(SourceErrorKind.UNREADABLE)

    def mark_string(string: re.Match[str]) -> str:
        def mark_reference(reference: re.Match[str]) -> str:
            token = reference[0]
            # OpenCode looks up the raw name. An escaped name cannot become a portable live reference.
            tag = r"\u0000!" if "\\" in token else r"\u0000"
            return tag + token[1:]

        return _SOURCE_REFERENCE_RE.sub(mark_reference, string[0])

    document = parse_json_object(_STRING_RE.sub(mark_string, text))
    try:
        restored: dict[str, Any] = _restore_reference_text(document)
    except RecursionError as exc:
        raise SourceReadError(SourceErrorKind.UNREADABLE) from exc
    return restored


def _restore_reference_text(value: Any) -> Any:
    if isinstance(value, str) and "\x00" in value:
        return _ReferencedText(value)
    if isinstance(value, list):
        return [_restore_reference_text(item) for item in value]
    if isinstance(value, dict):
        restored = {_restore_reference_text(key): _restore_reference_text(item) for key, item in value.items()}
        if len(restored) != len(value):
            raise SourceReadError(SourceErrorKind.UNREADABLE)
        return restored
    return value


def _marked_references(text: str) -> str:
    return text.marked if isinstance(text, _ReferencedText) else ""


def _global_timeout(value: object) -> dict[str, Any] | None:
    """Recognize the timeout object before decoding the remaining server entries."""
    if not isinstance(value, dict) or _is_direct_server(value) or (value and not value.keys() & _TIMEOUT_PHASES):
        return None
    return value if _valid_phase_timeout(value) else None


def _is_direct_server(entry: dict[str, Any]) -> bool:
    return any(key in entry and not isinstance(entry[key], dict) for key in ("type", "enabled"))


def _valid_phase_timeout(value: dict[str, Any]) -> bool:
    return all(_positive_int(value[key]) for key in value.keys() & _TIMEOUT_PHASES)


def _lower_timeout(value: Mapping[str, Any]) -> float | None:
    # OpenCode lowers only equal catalog and execution timeouts without a startup override.
    catalog = value.get("catalog")
    if "startup" not in value and catalog == value.get("execution"):
        return positive_number(catalog)
    return None


def _file_timeout(document: Mapping[str, Any], global_timeout: Mapping[str, Any] | None) -> float | None:
    experimental = document.get("experimental", {})
    if not isinstance(experimental, dict):
        return None
    if "mcp_timeout" in experimental:
        return positive_number(experimental["mcp_timeout"])
    return _lower_timeout(global_timeout) if global_timeout is not None else None


def _decoded_entries(
    table: dict[str, Any], path: str, global_timeout: dict[str, Any] | None
) -> tuple[dict[str, dict[str, Any]], dict[str, McpImportCandidate], list[str]]:
    """Lower nested and phase-aware definitions before interpreting the V1 server or enabled switch."""
    decoded: dict[str, dict[str, Any]] = {}
    unresolved: dict[str, McpImportCandidate] = {}
    invalid: list[str] = []
    nested = table.get("servers")
    envelope = isinstance(nested, dict) and not _is_direct_server(nested)
    flat = dict(table)
    if global_timeout is not None:
        flat.pop("timeout")
    if envelope:
        flat.pop("servers")
    for prefix, entries in (("mcp", flat), ("mcp.servers", nested if envelope else {})):
        for name, raw_entry in entries.items():
            if prefix == "mcp.servers" and name in flat:
                continue
            field = f"{prefix}.{name}"
            if not isinstance(raw_entry, dict):
                if prefix == "mcp":
                    invalid.append(field)
                continue
            kind = raw_entry.get("type")
            fields = [(field, name, FieldUse.NAME)]
            if isinstance(kind, str):
                fields.append((f"{field}.type", kind, FieldUse.VALUE))
            if refusal := _first_refusal(fields):
                unresolved[name] = _refused_candidate(name, path, refusal)
                continue
            entry = _normalize_server(raw_entry, nested=prefix == "mcp.servers")
            if entry is None:
                continue
            if _whole_server(entry):
                fields = _LOCAL_FIELDS if entry["type"] == "local" else _REMOTE_FIELDS
                # An ignored transport field must not become active if another file changes the type.
                decoded[name] = {
                    key: value for key, value in entry.items() if key in fields or key not in _SERVER_FIELDS
                }
            elif isinstance(entry.get("enabled"), bool):
                decoded[name] = {"enabled": entry["enabled"]}
            else:
                invalid.append(field)
    return decoded, unresolved, invalid


def _normalize_server(entry: dict[str, Any], *, nested: bool) -> dict[str, Any] | None:
    """Lower settings V1 accepts through its compatibility layer."""
    if nested and isinstance(entry.get("enabled"), bool) and "disabled" not in entry:
        return entry
    timeout = entry.get("timeout", {})
    oauth = entry.get("oauth")
    native_oauth = isinstance(oauth, dict) and bool(oauth.keys() & (_NATIVE_OAUTH_KEYS.keys() - {"scope"}))
    if not (
        nested or "disabled" in entry or "codemode" in entry or native_oauth or isinstance(entry.get("timeout"), dict)
    ):
        return entry
    lowered = {key: value for key, value in entry.items() if key not in ("disabled", "codemode", "timeout", "enabled")}
    if lowered.get("type") == "remote" and isinstance(oauth, dict):
        lowered["oauth"] = {value: oauth[key] for key, value in _NATIVE_OAUTH_KEYS.items() if key in oauth}
    if (
        not isinstance(timeout, dict)
        or not _valid_phase_timeout(timeout)
        or not all(isinstance(entry.get(key, False), bool) for key in ("disabled", "codemode"))
        or not _whole_server(lowered)
    ):
        # Invalid flat definitions still go through the V1 decoder. Invalid nested definitions
        # are omitted by the compatibility layer before that decoder sees the file.
        return None if nested else entry
    if (milliseconds := _lower_timeout(timeout)) is not None:
        lowered["timeout"] = milliseconds
    lowered["enabled"] = entry.get("enabled", not entry.get("disabled", False))
    return lowered


def _tool_rules(tools: Mapping[str, Any], permission: Mapping[str, Any]) -> dict[str, tuple[bool, str]]:
    """Each rule key's (hides, field), in order: ``tools`` as rules, then ``permission`` over them by key."""
    rules = {key: (not value, f"tools.{key}") for key, value in tools.items() if isinstance(value, bool)}
    for key, value in permission.items():
        if isinstance(value, dict):
            if not value:
                continue
            # Each entry is a rule and the last one decides; only a deny for every pattern hides a tool.
            pattern, action = next(reversed(value.items()))
            value = action if pattern == "*" else "allow"
        if isinstance(value, str):
            rules[key] = (value == "deny", f"permission.{key}")
    return rules


def _reference_edges(text: str) -> tuple[str, str]:
    """The literal start and end of a name containing a reference."""
    parts = _REFERENCE_RE.split(_marked_references(text))
    return parts[0], parts[-1]


def _could_be_name(name: str, edges: tuple[str, str]) -> bool:
    start, end = edges
    return len(name) >= len(start) + len(end) and name.startswith(start) and name.endswith(end)


def _partition_rules(section: str, rules: object) -> tuple[dict[str, Any], dict[str, Any], Refusal | None]:
    static: dict[str, Any] = {}
    referenced: dict[str, Any] = {}
    first_refusal = None
    if not isinstance(rules, dict):
        return static, referenced, first_refusal
    for name, value in rules.items():
        parts = (name, *value.keys(), *value.values()) if isinstance(value, dict) else (name, value)
        fields = [(f"{section}.{name}", part, FieldUse.VALUE) for part in parts if isinstance(part, str)]
        if refusal := _first_refusal(fields):
            referenced[name] = value
            first_refusal = first_refusal or refusal
        else:
            static[name] = value
    return static, referenced, first_refusal


def _could_expand_to(text: str, target: str) -> bool:
    return text == target or bool(
        _REFERENCE_RE.search(_marked_references(text)) and _could_be_name(target, _reference_edges(text))
    )


def _referenced_tool_denials(tools: Mapping[str, Any], permission: Mapping[str, Any]) -> dict[str, tuple[bool, str]]:
    """Possible denials survive separately because substitution can change which rule they replace."""
    rules = {key: (True, f"tools.{key}") for key, value in tools.items() if value is False}
    for key, value in permission.items():
        patterns = value.items() if isinstance(value, dict) else (("*", value),)
        if any(
            _could_expand_to(pattern, "*") and isinstance(action, str) and _could_expand_to(action, "deny")
            for pattern, action in patterns
        ):
            rules[key] = (True, f"permission.{key}")
    return rules


def _can_match_tool(pattern: str, prefix: str) -> bool:
    """Whether the rule can match the prefix followed by a nonempty normalized tool name."""
    if not re.fullmatch(r"[A-Za-z0-9_*?-]+", pattern):
        return False
    head, star, _ = pattern.partition("*")
    # A star can consume the rest of the prefix. Without one, the rule must also name a tool.
    if not star and len(head) <= len(prefix):
        return False
    return all(token in ("?", char) for token, char in zip(head, prefix, strict=False))


def _restrict_tools(server: SourceServer, rules: Mapping[str, tuple[bool, str]]) -> SourceServer:
    prefix = f"{normalize_js_server_name(server.name)}_"
    windows = get_platform().is_windows
    if windows:
        prefix = prefix.lower()
    full_scope = ("*", f"{prefix}*")
    fields: list[str] = []
    for rule, (hides, field) in rules.items():
        reference = _REFERENCE_RE.search(_marked_references(rule))
        pattern = f"{rule[: reference.start()]}*" if reference else rule
        if windows and pattern.isascii():
            pattern = pattern.lower()
        if not hides:
            if pattern in full_scope:
                fields.clear()
            continue
        if _can_match_tool(pattern, prefix):
            fields.append(field)
    if not fields:
        return server
    return replace(
        server,
        allowed_tools=(),
        warnings=(*server.warnings, ImportWarning(WarningKind.TOOLS_RESTRICTED, tuple(fields))),
    )


def _whole_server(entry: dict[str, Any]) -> bool:
    """Check a server's own transport fields before allowing a file to affect the merge."""
    if not isinstance(entry.get("enabled", True), bool) or ("timeout" in entry and not _positive_int(entry["timeout"])):
        return False
    if entry.get("type") == "local":
        return (
            is_str_list(entry.get("command"))
            and isinstance(entry.get("cwd", ""), str)
            and is_str_mapping(entry.get("environment", {}))
        )
    if entry.get("type") == "remote":
        return (
            isinstance(entry.get("url"), str)
            and is_str_mapping(entry.get("headers", {}))
            and _valid_oauth(entry.get("oauth", False))
        )
    return False


def _valid_oauth(value: object) -> bool:
    if value is False:
        return True
    if not isinstance(value, dict):
        return False
    port = value.get("callbackPort", 1)
    return (
        _positive_int(port)
        and port <= 65535
        and all(isinstance(value.get(key, ""), str) for key in _OAUTH_STRING_FIELDS)
    )


def _positive_int(value: object) -> bool:
    number = positive_number(value)
    return number is not None and number.is_integer()


def _files(ctx: ImportContext) -> list[tuple[Path, bool]]:
    """Configuration files with whether each belongs to OpenCode's regular global layer."""
    environ = ctx.environ
    names = ("opencode.json", "opencode.jsonc")
    # Folders keep opencode's spelling, which it compares and orders by; each is found only to be read.
    global_folder = str(Path(environ.get("XDG_CONFIG_HOME") or ctx.home / ".config") / "opencode")
    files = [(located(global_folder, ctx) / name, True) for name in ("config.json", *names)]
    if custom := environ.get("OPENCODE_CONFIG"):
        files.append((located(custom, ctx), False))
    # Folders in opencode's order, each kept where it first appears, compared as opencode compares
    # them, as text: the global folder first, so OPENCODE_CONFIG_DIR spelling it is read there.
    folders = [global_folder]
    if ctx.workspace_cwd and environ.get("OPENCODE_DISABLE_PROJECT_CONFIG", "").lower() not in ("true", "1"):
        chain = folders_down_to(ctx.workspace_cwd, git_root(ctx.workspace_cwd))
        files += [(folder / name, False) for folder in chain for name in names]
        folders += [str(folder / ".opencode") for folder in reversed(chain)]
    folders.append(str(ctx.home / ".opencode"))
    config_dir = environ.get("OPENCODE_CONFIG_DIR") or None
    if config_dir is not None:
        folders.append(config_dir)
    read = [folder for folder in dict.fromkeys(folders) if folder.endswith(".opencode") or folder == config_dir]
    return files + [(located(folder, ctx) / name, False) for folder in read for name in names]


def strip_jsonc(text: str) -> str:
    """*text* as plain JSON: comments blanked (keeping line breaks), trailing commas dropped.

    Raises :class:`SourceReadError` for a block comment that is never closed.
    """

    def blank(match: re.Match[str]) -> str:
        if match.group(1):
            return match.group(1)
        if match.group(2):
            # OpenCode substitutes before parsing. This token can affect file-wide policy.
            raise SourceReadError(SourceErrorKind.UNREADABLE)
        comment = match.group()
        if comment.startswith("/*") and (len(comment) < 4 or not comment.endswith("*/")):
            raise SourceReadError(SourceErrorKind.UNPARSABLE)
        return re.sub(r"[^\r\n]", " ", comment)

    return _TRAILING_COMMA_RE.sub(lambda m: m.group(1) or " ", _JSONC_TOKEN_RE.sub(blank, text))


def _server(name: str, entry: dict[str, Any], path: str, default_timeout: float) -> SourceServer | None:
    """Convert a merged, decoded entry with a usable command or URL."""
    prefix = f"mcp.{name}"
    enabled = entry.get("enabled", True)
    timeout = entry["timeout"] / 1000 if "timeout" in entry else default_timeout
    if entry.get("type") == "local":
        command, env = entry["command"], entry.get("environment", {})
        if not command or not command[0]:
            return None
        values, value_refusal = _values(env, f"{prefix}.environment", FieldUse.ENV_VALUE)
        if command[0] == "opencode":
            values.setdefault("BUN_BE_BUN", literal("1"))
        cwd_values, cwd_refusal = (
            _values({"cwd": entry["cwd"]}, prefix, FieldUse.CWD) if entry.get("cwd") else ({}, None)
        )
        cwd = cwd_values.get("cwd")
        if cwd is not None:
            if not variables(cwd):
                cwd = (lexical_cwd(entry["cwd"]),)
            elif any(part == ".." for part in re.split(r"[/\\]" if get_platform().is_windows else "/", entry["cwd"])):
                # A variable can span path components; normalizing across it now
                # or leaving link/.. to POSIX later would change the source path.
                cwd_refusal = (
                    f"{prefix}.cwd",
                    Problem.VALUE_NOT_REPRESENTABLE,
                    ValueReason.VARIABLE_PARENT,
                    FieldUse.CWD,
                )
        fields = [
            (prefix, name, FieldUse.NAME),
            *(
                (f"{prefix}.command[{i}]", part, FieldUse.COMMAND if i == 0 else FieldUse.ARGUMENT)
                for i, part in enumerate(command)
            ),
        ]
        field, problem, reason, use = (
            _first_refusal(fields)
            or value_refusal
            or cwd_refusal
            or ("", Problem.VALUE_NOT_REPRESENTABLE, ValueReason.INVALID, FieldUse.VALUE)
        )
        warnings = [
            missing_fields(entry, _LOCAL_FIELDS, prefix),
            ImportWarning(WarningKind.OPENCODE_VARIABLE_CWD, (f"{prefix}.cwd",))
            if cwd is not None and variables(cwd)
            else None,
        ]
        return SourceServer(
            name,
            path,
            "stdio",
            command=command[0],
            args=tuple(command[1:]),
            command_path=f"{prefix}.command[0]",
            arg_paths=tuple(f"{prefix}.command[{i}]" for i in range(1, len(command))),
            env=values,
            enabled=enabled,
            timeout_seconds=timeout,
            warnings=tuple(warning for warning in warnings if warning is not None),
            blocked_field=field,
            blocked_problem=problem,
            blocked_reason=reason,
            blocked_use=use,
            env_path=f"{prefix}.environment",
            cwd=cwd,
            cwd_path=f"{prefix}.cwd",
        )
    if entry.get("type") == "remote":
        url, headers = entry["url"], entry.get("headers", {})
        if not url.strip():
            return None
        values, value_refusal = _values(headers, f"{prefix}.headers", FieldUse.HEADER_VALUE)
        fields = [(prefix, name, FieldUse.NAME), (f"{prefix}.url", url, FieldUse.URL)]
        field, problem, reason, use = (
            _first_refusal(fields)
            or value_refusal
            or ("", Problem.VALUE_NOT_REPRESENTABLE, ValueReason.INVALID, FieldUse.VALUE)
        )
        warnings = [missing_fields(entry, _REMOTE_FIELDS, prefix)]
        oauth = entry.get("oauth")
        if oauth is not None and oauth is not False:
            warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
        return SourceServer(
            name,
            path,
            "http",
            url=url,
            url_path=f"{prefix}.url",
            headers=values,
            enabled=enabled,
            timeout_seconds=timeout,
            warnings=tuple(warning for warning in warnings if warning is not None),
            blocked_field=field,
            blocked_problem=problem,
            blocked_reason=reason,
            blocked_use=use,
            headers_path=f"{prefix}.headers",
        )
    return None


type Refusal = tuple[str, Problem, ValueReason, FieldUse]


def _refused_candidate(name: str, path: str, refusal: Refusal) -> McpImportCandidate:
    field, problem, reason, use = refusal
    return McpImportCandidate(str(name), path, None, problem=problem, problem_field=field, reason=reason, field_use=use)


def _first_refusal(fields: list[tuple[str, str, FieldUse]]) -> Refusal | None:
    """The first name, command part or URL holding a reference, which iCode reads in none of them."""
    for field, text, use in fields:
        marked = _marked_references(text)
        if _FILE_RE.search(marked):
            return field, Problem.VALUE_FROM_FILE_OR_COMMAND, ValueReason.FILE, use
        if _REFERENCE_RE.search(marked):
            return field, Problem.VARIABLE_NOT_SUPPORTED, ValueReason.VARIABLE, use
    return None


def _values(texts: Mapping[str, str], path: str, use: FieldUse) -> tuple[dict[str, Value], Refusal | None]:
    """The values as iCode references, and the first setting that can't be imported, with why."""
    values: dict[str, Value] = {}
    key_use = {FieldUse.ENV_VALUE: FieldUse.ENV_NAME, FieldUse.HEADER_VALUE: FieldUse.HEADER_NAME}.get(
        use, FieldUse.NAME
    )
    for key, text in texts.items():
        if refusal := _first_refusal([(f"{path}.{key}", key, key_use)]):
            return {}, refusal
        marked = _marked_references(text)
        if _FILE_RE.search(marked):
            return {}, (f"{path}.{key}", Problem.VALUE_FROM_FILE_OR_COMMAND, ValueReason.FILE, use)
        # Left after the usable references: a variable whose name iCode can't reference.
        if _REFERENCE_RE.search(_ENV_RE.sub("", marked)):
            return {}, (f"{path}.{key}", Problem.VALUE_NOT_REPRESENTABLE, ValueReason.VARIABLE, use)
        values[key] = split_variables(marked, _ENV_RE) if marked else literal(text)
    return values, None
