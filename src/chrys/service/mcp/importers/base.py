# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Contract and shared conversion for importing MCP servers from other applications.

An importer reads another application's config files and describes each MCP
server it finds as a :class:`SourceServer`: connection fields, values split
into literal text and variable references, and what the import leaves out.
:func:`convert` alone turns that into an iCode :class:`MCPServerConfig`, or
into a refusal when iCode can't hold a value as written. Importers only read:
they never run commands, connect to servers or read a variable's current value.
Results carry codes, never prose, so the UI localizes them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.platform.files import secure_open_regular_binary
from chrys.foundation.util.header_charset import header_name_charset_error, header_value_charset_error
from chrys.service.mcp.validation import MCP_PROGRESSIVE_CONTROL_TOOL_NAMES, validate_mcp_tool_name_prefix
from chrys.service.profiles.agents.schema import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

# Generous: an application's config file may also hold per-project state. It only stops an
# unbounded read of something that is not a config file.
MAX_SOURCE_BYTES = 16 * 1024 * 1024

# The longest request timeout iCode's MCP client can wait, which it holds as a timedelta.
MAX_REQUEST_TIMEOUT_SECONDS = timedelta.max.days * 86_400


class WarningKind(StrEnum):
    """Something the import changed or left out that the user must act on."""

    NOT_IMPORTED = "not_imported"
    """Source settings with no iCode equivalent; ``items`` names them."""
    AUTH_NOT_IMPORTED = "auth_not_imported"
    """OAuth or provider sign-in, which only the source application holds."""
    VARIABLE_REQUIRED = "variable_required"
    """Referenced variables that must be set and nonempty in iCode; ``items`` names them."""
    TOOLS_RESTRICTED = "tools_restricted"
    """Tool rules iCode can't express, so the server is imported with no tools."""


class Problem(StrEnum):
    """Why a server can't be imported."""

    UNSUPPORTED_CONNECTION = "unsupported_connection"
    """A transport iCode doesn't run; ``transport`` names it."""
    REMOTE_ENVIRONMENT = "remote_environment"
    """A server the source runs in a remote environment, never on this computer."""
    VARIABLE_NOT_SUPPORTED = "variable_not_supported"
    """A variable where iCode reads none: a URL, command, argument or server name."""
    VALUE_FROM_FILE_OR_COMMAND = "value_from_file_or_command"
    """A value the source reads from a file or a command, which the import never does."""
    VALUE_NOT_REPRESENTABLE = "value_not_representable"
    """Any other value iCode can't hold as written."""


class SourceErrorKind(StrEnum):
    """Why a config file that exists gave no servers."""

    UNREADABLE = "unreadable"
    UNPARSABLE = "unparsable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class ImportWarning:
    """Names settings or variables, never their values: warnings are saved in the server's description."""

    kind: WarningKind
    items: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class McpSourceError:
    path: str
    kind: SourceErrorKind
    field: str = ""
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True, slots=True)
class Var:
    """A reference to an environment variable in a source value."""

    name: str


type Value = tuple[str | Var, ...]


@dataclass(frozen=True, slots=True)
class SourceServer:
    """One server as the source configures it, in iCode's terms.

    ``env_path`` and ``headers_path`` are the source setting paths of ``env``
    and ``headers``, so a refusal names the exact source setting. Set
    ``blocked_field`` when the source writes a value iCode can't hold at all,
    and ``blocked_problem`` to say why.
    """

    name: str
    source_path: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: Mapping[str, Value] = field(default_factory=dict)
    url: str = ""
    headers: Mapping[str, Value] = field(default_factory=dict)
    enabled: bool = True
    description: str = ""
    timeout_seconds: float | None = None
    allowed_tools: tuple[str, ...] | None = None
    progressive: bool = False
    always_load: tuple[str, ...] = ()
    warnings: tuple[ImportWarning, ...] = ()
    blocked_field: str = ""
    blocked_problem: Problem = Problem.VALUE_NOT_REPRESENTABLE
    env_path: str = ""
    headers_path: str = ""


@dataclass(frozen=True, slots=True)
class McpImportCandidate:
    """A server found in a source: ``config`` when it can be imported, else ``problem`` says why not."""

    name: str
    source_path: str
    config: MCPServerConfig | None
    problem: Problem | None = None
    problem_field: str = ""
    warnings: tuple[ImportWarning, ...] = ()
    # The transport the source declares, when the problem is that iCode doesn't run it.
    transport: str = ""


@dataclass(frozen=True, slots=True)
class McpImportResult:
    candidates: tuple[McpImportCandidate, ...] = ()
    source_errors: tuple[McpSourceError, ...] = ()


@dataclass(frozen=True, slots=True)
class ImportContext:
    """Where an importer looks: the workspace for project files, home and env for user files."""

    workspace_cwd: str | None
    home: Path
    environ: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def current(cls, workspace_cwd: str | None) -> ImportContext:
        return cls(workspace_cwd=workspace_cwd, home=Path.home(), environ=dict(os.environ))


class McpImportSource(Protocol):
    """An application whose MCP servers can be imported."""

    @property
    def source_id(self) -> str: ...

    @property
    def display_name(self) -> str:
        """Product name shown as is (not localized)."""
        ...

    def detect(self, ctx: ImportContext) -> bool:
        """Cheap check that this application has config files to read; never parses them."""
        ...

    def discover(self, ctx: ImportContext) -> McpImportResult:
        """Read the application's config files; blocking file I/O, run it off the event loop."""
        ...


class SourceReadError(Exception):
    def __init__(
        self, kind: SourceErrorKind, *, field: str = "", line: int | None = None, column: int | None = None
    ) -> None:
        super().__init__(kind.value)
        self.kind = kind
        self.field = field
        self.line = line
        self.column = column

    def at(self, path: Path) -> McpSourceError:
        return McpSourceError(str(path), self.kind, self.field, self.line, self.column)


def read_text(path: Path) -> str | None:
    """The text of *path*; ``None`` when it does not exist. Raises :class:`SourceReadError` when unusable.

    A link is followed, as the owning application follows it (dotfile managers link these files).
    """
    if not os.path.exists(path):
        return None
    try:
        # The leaf is opened no-follow, so open the link's target, still checked to be a regular file.
        with secure_open_regular_binary(Path(os.path.realpath(path))) as handle:
            raw = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError as exc:
        raise SourceReadError(SourceErrorKind.UNREADABLE) from exc
    if len(raw) > MAX_SOURCE_BYTES:
        raise SourceReadError(SourceErrorKind.UNREADABLE)
    try:
        # Every source application reads its files as UTF-8 (a leading BOM skipped); a guessed encoding
        # would import other values.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise SourceReadError(SourceErrorKind.UNPARSABLE) from exc


def parse_json_object(text: str) -> dict[str, Any]:
    """The JSON object *text* holds; a blank file holds nothing. Raises :class:`SourceReadError` otherwise."""
    text = text.removeprefix("\ufeff")
    if not text.strip():
        return {}
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SourceReadError(SourceErrorKind.UNPARSABLE, line=exc.lineno, column=exc.colno) from exc
    except (ValueError, RecursionError) as exc:
        raise SourceReadError(SourceErrorKind.UNPARSABLE) from exc
    if not isinstance(document, dict):
        raise SourceReadError(SourceErrorKind.INVALID)
    return document


def read_json_object(path: Path) -> dict[str, Any] | None:
    text = read_text(path)
    return None if text is None else parse_json_object(text)


def git_root(path: str) -> str | None:
    """The nearest folder at or above *path* holding ``.git``: a directory, or a file in a worktree or submodule."""
    try:
        folder = Path(os.path.realpath(path))
    except OSError, ValueError:
        return None
    for candidate in (folder, *folder.parents):
        if os.path.lexists(candidate / ".git"):
            return str(candidate)
    return None


def project_folders(workspace_cwd: str | None) -> list[Path]:
    """The folders project files are read from, lowest precedence first: the Git root, then the workspace."""
    if not workspace_cwd:
        return []
    workspace = Path(workspace_cwd)
    root = git_root(workspace_cwd)
    if root is None or Path(root) == Path(os.path.realpath(workspace_cwd)):
        return [workspace]
    return [Path(root), workspace]


def folders_down_to(workspace_cwd: str, top: str | None) -> list[Path]:
    """Every folder from *top* (else the filesystem root) down to the workspace, the workspace last as given."""
    try:
        folder = Path(os.path.realpath(workspace_cwd))
        stop = Path(os.path.realpath(top)) if top else Path(folder.anchor)
    except OSError, ValueError:
        return [Path(workspace_cwd)]
    chain = [folder]
    while folder != stop and folder.parent != folder:
        folder = folder.parent
        chain.append(folder)
    chain.reverse()
    chain[-1] = Path(workspace_cwd)
    return chain


# Git keeps a worktree's pointer files small; anything larger is not one.
_MAX_GIT_POINTER_BYTES = 64 * 1024


def _git_pointer(path: str) -> str | None:
    """The stripped text of a small regular file Git writes (not a link), else ``None``."""
    try:
        if os.path.islink(path) or not os.path.isfile(path) or os.path.getsize(path) > _MAX_GIT_POINTER_BYTES:
            return None
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip() or None
    except OSError, UnicodeDecodeError, ValueError:
        return None


def linked_common_dir(root: str) -> str | None:
    """The common Git folder of the linked worktree *root*, as its ``.git`` file spells it, read from Git's files only.

    *root*'s ``.git`` file points at ``<common>/worktrees/<name>``; ``<common>`` counts once that
    folder's own records point back at *root* (``gitdir``) and at ``<common>`` (``commondir``,
    spelled as the pointer spells it and naming the same real folder, as Git writes it): Claude
    Code and Codex both check this before keying settings or trust by the main checkout, so a
    ``.git`` file can't borrow another repository's. ``None`` for anything else (a main checkout,
    a submodule, a broken link).
    """
    dot_git = os.path.join(root, ".git")
    pointer = _git_pointer(dot_git)
    if pointer is None or not pointer.startswith("gitdir:"):
        return None
    git_dir = os.path.normpath(os.path.join(root, pointer.removeprefix("gitdir:").strip()))
    try:
        if not os.path.isdir(git_dir) or os.path.islink(git_dir):
            return None
        real_git_dir = os.path.realpath(git_dir)
        if os.path.basename(os.path.dirname(real_git_dir)) != "worktrees":
            return None
        real_common = os.path.dirname(os.path.dirname(real_git_dir))
        back = _git_pointer(os.path.join(real_git_dir, "gitdir"))
        common = _git_pointer(os.path.join(real_git_dir, "commondir"))
        if back is None or common is None:
            return None
        registered = os.path.join(real_git_dir, back)
        if os.path.basename(registered) != ".git":
            return None
        if os.path.realpath(os.path.dirname(registered)) != os.path.realpath(root):
            return None
        if os.path.realpath(os.path.join(real_git_dir, common)) != real_common:
            return None
    except OSError, ValueError:
        return None
    spelled = os.path.dirname(os.path.dirname(git_dir))
    # Claude Code compares the spelling too: through links, ``..`` can reach the same folder by another name.
    if os.path.basename(os.path.dirname(git_dir)) != "worktrees":
        return None
    return spelled if os.path.normpath(os.path.join(git_dir, common)) == spelled else None


def main_checkout(root: str) -> str | None:
    """The main checkout of the repository whose working tree is *root*, as Codex finds it for trust.

    *root* itself when its ``.git`` is a folder. For a linked worktree, the folder holding its
    common Git folder, once that folder's own ``.git`` (a folder, or a file pointing elsewhere as
    with ``--separate-git-dir``) is that same common folder. ``None`` for anything else.
    """
    dot_git = os.path.join(root, ".git")
    if os.path.isdir(dot_git) and not os.path.islink(dot_git):
        return root
    common = linked_common_dir(root)
    if common is None:
        return None
    main = os.path.dirname(common)
    main_git = os.path.join(main, ".git")
    try:
        if not os.path.isdir(main_git):
            pointer = _git_pointer(main_git)
            if pointer is None or not pointer.startswith("gitdir:"):
                return None
            main_git = os.path.join(main, pointer.removeprefix("gitdir:").strip())
        if os.path.realpath(main_git) != os.path.realpath(common):
            return None
    except OSError, ValueError:
        return None
    return main


def path_matches(key: str, folder: str) -> bool:
    """Whether a path an application stored as a key names *folder*, as given or as its real path.

    The key itself is not resolved: an application looks up the spelling it computes, so a key
    naming another path to the same folder is a different entry (and may decide differently).
    """
    if key == folder:
        return True
    try:
        spellings = {_path_key(folder), _path_key(os.path.realpath(folder))}
        return _path_key(key) in spellings
    except OSError, ValueError:
        return False


def _path_key(path: str) -> str:
    return os.path.normcase(unicodedata.normalize("NFC", os.path.normpath(path)))


def by_name(servers: Iterable[SourceServer]) -> list[SourceServer]:
    """*servers* listed in precedence order, lowest first, with a later same-named server replacing an earlier one."""
    named: dict[str, SourceServer] = {}
    for server in servers:
        named.pop(server.name, None)
        named[server.name] = server
    return list(named.values())


def merge_entries(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """*overlay* over *base*, as Codex and opencode layer their files: objects merge key by key, other values replace.

    Merged without recursion: a file may nest as deep as its parser allows.
    """
    merged = dict(base)
    pending = [(merged, overlay)]
    while pending:
        target, source = pending.pop()
        for key, value in source.items():
            previous = target.get(key)
            if isinstance(previous, dict) and isinstance(value, dict):
                target[key] = dict(previous)
                pending.append((target[key], value))
            else:
                target[key] = value
    return merged


def is_str_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def is_str_mapping(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(item, str) for item in value.values())


def positive_number(value: object) -> float | None:
    """*value* when it is a positive finite number (not a bool), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return math.inf if value > 0 else None
    return number if number > 0 and not math.isnan(number) else None


def missing_fields(entry: Mapping[str, Any], known: Iterable[str], path: str) -> ImportWarning | None:
    """A NOT_IMPORTED warning naming the settings of *entry* the import does not read."""
    known_set = set(known)
    unknown = [f"{path}.{key}" for key in entry if key not in known_set]
    return ImportWarning(WarningKind.NOT_IMPORTED, tuple(unknown)) if unknown else None


def literal(text: str) -> Value:
    return (text,)


def convert(server: SourceServer) -> McpImportCandidate:
    """*server* as an importable candidate, or a refusal naming the first value iCode can't hold."""

    def refuse(problem: Problem, problem_field: str = "") -> McpImportCandidate:
        return McpImportCandidate(server.name, server.source_path, None, problem=problem, problem_field=problem_field)

    if server.transport not in ("stdio", "http"):
        return replace(refuse(Problem.UNSUPPORTED_CONNECTION), transport=server.transport)
    if server.blocked_field:
        return refuse(server.blocked_problem, server.blocked_field)
    stdio = server.transport == "stdio"
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    if stdio:
        for key, value in server.env.items():
            text = render(value)
            # The configuration editor trims values and refuses empty ones.
            if text is None or not text.strip() or text != text.strip():
                return refuse(Problem.VALUE_NOT_REPRESENTABLE, f"{server.env_path}.{key}")
            env[key] = text
    else:
        for key, value in server.headers.items():
            text = render(value)
            if text is not None:
                # As fetch trims them, which the other applications send with.
                text = text.strip(" \t\r\n")
            # The editor refuses empty values; iCode's HTTP client sends only valid names and printable ASCII values.
            if text is None or not text or header_name_charset_error(key) or header_value_charset_error(key, text):
                return refuse(Problem.VALUE_NOT_REPRESENTABLE, f"{server.headers_path}.{key}")
            headers[key] = text
    config = MCPServerConfig(
        name=server.name,
        transport=server.transport,
        # Only the settings of the server's own transport.
        command=server.command if stdio else "",
        args=list(server.args) if stdio else [],
        env=env,
        url="" if stdio else server.url,
        headers=headers,
        enabled=server.enabled,
        description=server.description,
        allowed_tools=None if server.allowed_tools is None else list(server.allowed_tools),
        use_progressive_disclosure=server.progressive,
        always_load=list(server.always_load),
        request_timeout=None if server.timeout_seconds is None else request_timeout_seconds(server.timeout_seconds),
    )
    return McpImportCandidate(
        server.name,
        server.source_path,
        with_tool_name_prefix(config),
        warnings=merge_warnings(server.warnings),
    )


def merge_warnings(warnings: Iterable[ImportWarning]) -> tuple[ImportWarning, ...]:
    """One warning per kind, in first-seen order, with the items of all of them, each once."""
    merged: dict[WarningKind, dict[str, None]] = {}
    for warning in warnings:
        merged.setdefault(warning.kind, {}).update(dict.fromkeys(warning.items))
    return tuple(ImportWarning(kind, tuple(items)) for kind, items in merged.items())


def request_timeout_seconds(seconds: float) -> int:
    """A positive timeout in seconds as iCode's request timeout: rounded up, at most what the client can wait."""
    if seconds == math.inf:
        return MAX_REQUEST_TIMEOUT_SECONDS
    return min(math.ceil(seconds), MAX_REQUEST_TIMEOUT_SECONDS)


def render(value: Value) -> str | None:
    """*value* as an iCode value: variables as ``{{NAME}}``, literal ``{{``/``}}`` escaped.

    ``None`` when iCode would read it back differently: iCode escapes braces
    only in pairs, so an odd run of ``{`` right before a variable joins its
    ``{{``, and a ``}}`` right after one is read with its ``}}``.
    """
    parts = _merge_literals(value)
    for index, part in enumerate(parts):
        if not isinstance(part, Var):
            continue
        before = _literal_at(parts, index - 1)
        after = _literal_at(parts, index + 1)
        if (len(before) - len(before.rstrip("{"))) % 2 or after.startswith("}}"):
            return None
    return "".join(
        "{{" + part.name + "}}" if isinstance(part, Var) else part.replace("{{", "{{{{").replace("}}", "}}}}")
        for part in parts
    )


def variables(value: Value) -> list[str]:
    return [part.name for part in value if isinstance(part, Var)]


def split_variables(text: str, pattern: re.Pattern[str]) -> Value:
    """*text* split at each match of *pattern*, whose first group names the variable."""
    parts: list[str | Var] = []
    position = 0
    for match in pattern.finditer(text):
        parts.extend((text[position : match.start()], Var(match.group(1))))
        position = match.end()
    parts.append(text[position:])
    return tuple(parts)


def _literal_at(parts: Sequence[str | Var], index: int) -> str:
    part = parts[index] if 0 <= index < len(parts) else ""
    return part if isinstance(part, str) else ""


def _merge_literals(value: Value) -> list[str | Var]:
    merged: list[str | Var] = []
    for part in value:
        if isinstance(part, str) and merged and isinstance(merged[-1], str):
            merged[-1] += part
        elif part != "":
            merged.append(part)
    return merged


# Characters other than these become ``_`` when a server name becomes its tools' prefix.
_TOOL_NAME_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
# A prefix of at most 24 characters leaves at least 39 of a tool name's 64 for the tool's own name.
_MAX_PREFIX_LENGTH = 24
_HASH_LENGTH = 6


def with_tool_name_prefix(config: MCPServerConfig) -> MCPServerConfig:
    """*config* with a tool name prefix made from its server name, unless it has one.

    The source applications name a server's tools after it, so their users run
    several servers with the same tool names; without a prefix, iCode refuses
    the clash. A name that can't be used as it is (characters replaced or
    trimmed, or too long) gets a hash of the name, so two names that end up
    alike still differ. A prefix iCode would refuse is not set.
    """
    if config.tool_name_prefix:
        return config
    prefix = _TOOL_NAME_UNSAFE_RE.sub("_", config.name).strip("_-")
    if prefix != config.name or len(prefix) > _MAX_PREFIX_LENGTH:
        digest = hashlib.sha256(config.name.encode("utf-8", "surrogatepass")).hexdigest()[:_HASH_LENGTH]
        prefix = prefix[: _MAX_PREFIX_LENGTH - _HASH_LENGTH - 1].rstrip("_-")
        prefix = f"{prefix}-{digest}" if prefix else f"mcp_{digest}"
    generated = MCP_PROGRESSIVE_CONTROL_TOOL_NAMES if config.use_progressive_disclosure else ()
    if validate_mcp_tool_name_prefix(prefix, generated_suffixes=generated) is not None:
        return config
    return replace(config, tool_name_prefix=prefix)
