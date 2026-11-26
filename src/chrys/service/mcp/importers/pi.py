# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in pi, the coding agent (https://github.com/earendil-works/pi).

Read: ``mcp.json`` in pi's agent folder (``$PI_CODING_AGENT_DIR``, default
``~/.pi/agent``), then ``.pi/mcp.json`` in the workspace once the user trusts
it, as pi does: the nearest of the workspace's real folder and the folders
above it that ``trust.json`` in the agent folder decides, else
``defaultProjectTrust: "always"`` in the agent folder's ``settings.json`` (pi
otherwise asks, so an undecided project is not read). A relative
``$PI_CODING_AGENT_DIR`` is found from the workspace. A project server replaces
the global one of the same name; a project entry with none of ``command``,
``url`` and ``type`` only changes the ``enabled``, ``exposure`` and
``toolExposure`` of the global server it names. Basic fields are checked
before replacing an earlier definition; an invalid project entry reports an
error and leaves the global server intact.
OAuth callback and metadata URLs are checked before applying a replacement.

Values in ``env`` and ``headers`` may reference ``$NAME`` or ``${NAME}``; ``$$``
and ``$!`` are a literal ``$`` and ``!``. A value starting with ``!`` runs a
command in pi, which the import does not translate: such servers are listed
for manual setup. Home expansion in ``command`` and ``args`` uses Node's lexical
path normalization, without following symlinks. Pi can negotiate OAuth when
an HTTP server requests it. Import warns about explicit sign-in settings.

``exposure`` maps to iCode's loading: ``direct`` loads every tool,
``codemode`` and ``deferred`` load on demand, ``hidden`` offers none.
``toolExposure`` entries naming one tool become iCode's tool lists where they
can; a rule hiding tools iCode can't express imports the server with no tools.
``timeout`` is in seconds, default 60.

Checked against https://github.com/earendil-works/pi tag ``v1.6.0`` (commit
faac585884), read 2026-11-24. Paths are under ``packages/coding-agent/src``:
the files and project overrides (``extensions/mcp/config.ts``), trust
(``core/trust-manager.ts``, ``core/project-trust.ts``, ``utils/paths.ts``), the
variables and ``!`` commands (``core/resolve-config-value.ts``), the server
fields and exposure (``core/mcp-servers.ts``, delegating shared validation to
``packages/mcp/src/config.ts``) and ``~`` expansion
(``extensions/mcp/runtime.ts``).
``cwd`` must be a string: null and false are invalid, not defaults.
It is a literal path (no variable or command expansion): ``createDefaultTransport``
expands its home prefix, then resolves it lexically from the session folder.
Import keeps ordinary paths relative; an empty string, like absence, means
that session folder. Remaining tildes are shielded from iCode's expansion.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from chrys.foundation.platform import get_platform
from chrys.service.mcp.importers.base import (
    FieldUse,
    ImportContext,
    ImportWarning,
    McpImportResult,
    McpSourceError,
    Problem,
    SourceErrorKind,
    SourceReaderRefusal,
    SourceReadError,
    SourceReadLimitError,
    SourceServer,
    Value,
    ValueReason,
    Var,
    WarningKind,
    convert,
    is_str_list,
    is_str_mapping,
    lexical_cwd,
    literal,
    located,
    missing_fields,
    positive_number,
    read_json_object,
)
from chrys.service.mcp.validation import validate_mcp_http_url

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import PurePath

    from pydantic_core import Url

_NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
_VARIABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_COMMON_HTTP_HOST_RE = re.compile(r"[A-Za-z0-9._-]+|[A-Fa-f0-9:.]+")
_URL_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "[::1]"})
_DEFAULT_TIMEOUT_SECONDS = 60.0
_OVERRIDE_KEYS = frozenset({"enabled", "exposure", "toolExposure"})
_OAUTH_URL_FIELDS = ("callbackUrl", "authServerMetadataUrl")
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


class _Read(Protocol):
    def __call__(self, path: Path, *, strict: bool = False) -> dict[str, Any] | None:
        """The object in *path*, empty when missing. Propagate importer-specific read refusals."""
        ...


class _HttpEndpoint(Enum):
    INVALID = auto()
    MANUAL = auto()
    PORTABLE = auto()


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
        return (_agent_folder(ctx) / "mcp.json").is_file() or (
            ctx.workspace_cwd is not None and (Path(ctx.workspace_cwd) / ".pi" / "mcp.json").is_file()
        )

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []

        def read(path: Path, *, strict: bool = False) -> dict[str, Any] | None:
            try:
                return read_json_object(path, strict=strict) or {}
            except SourceReadError as exc:
                errors.append(exc.at(path))
                if isinstance(exc, SourceReadLimitError | SourceReaderRefusal):
                    raise
                return None

        agent = _agent_folder(ctx)
        files = [agent / "mcp.json"]
        project = Path(ctx.workspace_cwd) / ".pi" / "mcp.json" if ctx.workspace_cwd else None
        if project is not None and project.is_file():
            try:
                if _trusted(ctx.workspace_cwd or "", agent, read, errors):
                    files.append(project)
            except SourceReadLimitError, SourceReaderRefusal:
                # Pi may trust this project and use overrides we cannot evaluate.
                return McpImportResult(source_errors=tuple(errors))
        entries: dict[str, tuple[dict[str, Any], SourceServer]] = {}
        for position, path in enumerate(files):
            try:
                document = read_json_object(path, strict=True)
            except SourceReadError as exc:
                errors.append(exc.at(path))
                # Either layer may affect the other through overrides or namespace conflicts.
                if isinstance(exc, SourceReadLimitError | SourceReaderRefusal):
                    return McpImportResult(source_errors=tuple(errors))
                continue
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
    return located(_expand_home(folder, ctx.home), ctx) if folder else ctx.home / ".pi" / "agent"


def _trusted(workspace_cwd: str, agent: Path, read: _Read, errors: list[McpSourceError]) -> bool:
    """Whether pi loads the workspace's project files without asking.

    As pi looks it up: from the workspace's real folder up, each folder decided by
    the first entry in the file naming it as a path that is true or false.
    """
    store = agent / "trust.json"
    decisions = read(store, strict=True)
    # A store that can't be read, or that pi refuses for a value other than true, false or null,
    # trusts nothing, whatever the default says; the error is listed so the user can repair it.
    if decisions is None:
        return False
    invalid = [key for key, value in decisions.items() if value is not None and not isinstance(value, bool)]
    if invalid:
        errors.extend(McpSourceError(str(store), SourceErrorKind.INVALID, field=key) for key in invalid)
        return False
    try:
        folder = os.path.realpath(workspace_cwd)
    except OSError, ValueError:
        folder = workspace_cwd
    path = ntpath if get_platform().is_windows else posixpath
    keyed = [(_comparison_key(key), value) for key, value in decisions.items() if value is not None]
    while True:
        name = _comparison_key(folder)
        decision = next((value for key, value in keyed if key == name), None)
        if decision is not None:
            return decision
        parent = path.dirname(folder)
        if parent == folder:
            return (read(agent / "settings.json") or {}).get("defaultProjectTrust") == "always"
        folder = parent


def _comparison_key(path: str) -> str:
    """*path* as pi compares the absolute paths it writes: normalized, lower-cased on Windows."""
    return ntpath.normpath(path).lower() if get_platform().is_windows else posixpath.normpath(path)


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


def _valid_oauth(value: object) -> bool:
    """Check OAuth settings that Pi can reject before choosing the server."""
    if not isinstance(value, dict):
        return False
    port = value.get("callbackPort", 1)
    if not isinstance(port, int | float) or isinstance(port, bool) or not 1 <= port <= 65535 or port != int(port):
        return False
    strings = ("clientId", "clientSecret", "scope", "clientName", *_OAUTH_URL_FIELDS)
    if not all(isinstance(value.get(key, ""), str) for key in strings) or not value.get("clientName", "pi").strip():
        return False
    registration = value.get("clientRegistration", "auto")
    if not (
        registration in ("auto", "dcr") or (registration == "cimd" and not value.keys() & {"clientId", "clientName"})
    ):
        return False
    if "callbackUrl" in value and not _valid_oauth_callback(
        value["callbackUrl"], value.get("callbackPort"), cimd=registration == "cimd"
    ):
        return False
    if "authServerMetadataUrl" in value:
        metadata = _http_url(value["authServerMetadataUrl"])
        return metadata is not None and (metadata.scheme == "https" or metadata.host in _LOOPBACK_HOSTS)
    return True


def _valid_oauth_callback(url: str, port: int | float | None, *, cimd: bool) -> bool:
    callback = _http_url(url)
    if callback is None or callback.scheme != "http" or callback.host not in _LOOPBACK_HOSTS:
        return False
    if callback.query or callback.fragment or (port is not None and callback.port not in (80, port)):
        return False
    return not cimd or (callback.host != "[::1]" and callback.path == "/callback")


def _http_url(url: str) -> Url | None:
    """Validate with the WHATWG URL rules that Pi uses to choose a server."""
    from pydantic_core import Url, ValidationError

    try:
        # Node's URL constructor replaces unpaired UTF-16 surrogates before parsing.
        parsed = Url(_URL_SURROGATE_RE.sub("�", url))
    except ValidationError:
        return None
    return parsed if parsed.scheme in ("http", "https") else None


def _http_endpoint(url: str, *, provider_auth: bool) -> _HttpEndpoint:
    """Check Pi's endpoint before selecting it and flag spellings that can change the target."""
    parsed = _http_url(url)
    if parsed is None or (provider_auth and parsed.scheme == "http" and parsed.host not in _LOOPBACK_HOSTS):
        return _HttpEndpoint.INVALID
    text = url.replace("\t", "").replace("\r", "").replace("\n", "").strip()
    target = re.split(r"[?#]", text, maxsplit=1)[0]
    try:
        original = urlsplit(target)
    except ValueError:
        return _HttpEndpoint.MANUAL
    if (
        not original.netloc
        or "\\" in target
        or not _COMMON_HTTP_HOST_RE.fullmatch(original.hostname or "")
        or _URL_SURROGATE_RE.search(url)
    ):
        return _HttpEndpoint.MANUAL
    return _HttpEndpoint.PORTABLE


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
    endpoint = _HttpEndpoint.PORTABLE
    if isinstance(entry.get("url"), str) and kind in ("", "http", "streamable-http"):
        transport = "http"
        endpoint = _http_endpoint(entry["url"], provider_auth="auth" in entry)
        if (
            endpoint is _HttpEndpoint.INVALID
            or not is_str_mapping(entry.get("headers", {}))
            or not _valid_oauth(entry.get("oauth", {}))
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
        if "url" in entry:
            # Pi's runtime chooses HTTP on the presence of a URL, even for validated STDIO entries.
            return SourceServer(
                name,
                path,
                transport,
                blocked_field=f"{prefix}.url",
                blocked_reason=ValueReason.INVALID,
                blocked_use=FieldUse.URL,
            )
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
    if transport == "http" and ("auth" in entry or ("oauth" in entry and not authorized)):
        warnings.append(ImportWarning(WarningKind.AUTH_NOT_IMPORTED))
    server = SourceServer(
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
        url_path=f"{prefix}.url",
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
    if endpoint is _HttpEndpoint.MANUAL and not blocked and validate_mcp_http_url(server.url.strip().lower()) is None:
        return replace(
            server,
            blocked_field=server.url_path,
            blocked_problem=Problem.VALUE_NOT_REPRESENTABLE,
            blocked_reason=ValueReason.INVALID,
            blocked_use=FieldUse.URL,
        )
    return server


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
    warning = WarningKind.TOOL_LOADING
    if base == _HIDDEN:
        access.allowed = tuple(granted)
        access.progressive = _ON_DEMAND in granted.values()
        access.always_load = tuple(t for t, k in granted.items() if k == _FULL) if access.progressive else ()
        warning = WarningKind.TOOL_SCOPE if granted else WarningKind.TOOLS_RESTRICTED
    else:
        access.progressive = base == _ON_DEMAND
        access.always_load = tuple(always)
    if unread:
        access.warnings.append(ImportWarning(warning, tuple(unread)))
    return access
