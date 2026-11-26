# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Claude Code.

Read (https://code.claude.com/docs/en/mcp, "MCP installation scopes"), lowest
precedence first, a same-named server replacing an earlier one:

- ``user``: top-level ``mcpServers`` of ``~/.claude.json`` (or
  ``$CLAUDE_CONFIG_DIR/.claude.json``; a relative folder is found from the
  workspace);
- ``project``: ``mcpServers`` of ``.mcp.json`` in the workspace and its ancestors
  (not the filesystem root), with closer directories taking precedence;
- ``local``: ``projects[<project>].mcpServers`` of the same ``.claude.json``, where
  the project is the workspace's Git root (for a linked worktree, the main
  checkout every worktree shares, or the bare repository), else the workspace,
  looked up as Claude Code keys it: in NFC Unicode form, with ``/`` on Windows.

Values in ``env`` and ``headers`` may reference ``${VAR}`` or ``${VAR:-default}``;
iCode drops the default and warns about the affected fields. Missing or empty
variables cause a connection error in iCode.
``CLAUDE_PROJECT_DIR``, which Claude Code sets for its stdio servers, must be
set by the user. Claude Code reads credential variables as empty in a remote
server's headers; a header referencing one is not imported, so the import
can't start sending a credential Claude Code withholds. ``timeout`` is in
milliseconds.

A server Claude Code would not start is imported turned off
(https://code.claude.com/docs/en/mcp, "Disable a server without removing it",
"Project server approvals and workspace trust"): any server named in the
project's ``disabledMcpServers`` (what ``/mcp`` writes), and a ``.mcp.json``
server that is rejected (``disabledMcpjsonServers`` in any settings file or the
project's entry) or not approved. Approvals (``enabledMcpjsonServers``,
``enableAllProjectMcpServers``) count from the user's settings, and from the
project's entry and the workspace's settings files only once the user trusts
the project (``hasTrustDialogAccepted`` in its entry, not a folder above it):
a repository can't approve its own servers. Approvals and rejections match a server whose
name differs only in characters other than letters, digits, ``_`` and ``-``,
as in Claude Code. The highest-priority explicit ``enableAllProjectMcpServers``
controls blanket approval. A legacy project ``true`` supplies a local approval
that an explicit value in the local settings file can override.
As ``j4o`` migrates it, it is a value of the current local settings file, set only where that
file leaves the key unset, and the entry's approvals count only when its
``disabledMcpjsonServers`` is absent or a list of names.
Managed settings are not read.

The local settings file (``.claude/settings.local.json``) is the project folder's: Claude Code
2.1.211 moved it there from the workspace and still reads the workspace's one beneath it
(``kGo``/``jDe`` merge it first), so the project folder's explicit values win and the lists of
both count. Claude Code keeps it in the workspace when the project folder is the home folder, or
it, its ``.git`` or its ``.claude`` is not the user's (POSIX owners), and on Windows (``Sj``,
``_gd``); the project folder's file then only adds rejections and tool denials.

Claude Code skips ``.mcp.json`` in a connector-downloads folder, where it saves files from
claude.ai connectors (``MHt``): a folder named ``connector-downloads`` (in any case, with
trailing dots, spaces or a ``:stream`` suffix, ignoring invisible format characters; ``a0t``), or
one inside it, that its marker files show to be its downloads folder (``Rle``, ``Vyt``), and the
sandbox's browser-download folders. iCode goes by the name alone and reports each such file it
skips; settings files there still restrict but do not approve. It also skips Linux autofs map
roots (``i3``), which iCode does not detect.

A ``.mcp.json`` Claude Code fails to read or parse is reported and skipped, the other files kept
(``_Wn``): it also skips files over 2 MiB and anything but a regular file. A regular file that
only iCode's reader refuses (not UTF-8, which Claude Code decodes with U+FFFD, past Python's
parsing limits, or not pinned by the secure open) could still replace any definition below it, so
none of those is imported.

An explicit MCP tool denial in the settings files above imports the affected
server with no tools. Claude Code can deny individual tools, which iCode
cannot map without a tool catalog.

The scopes, variables and approvals are documented above and in
https://code.claude.com/docs/en/settings-reference. Rechecked on 2026-11-24
against tag ``v2.1.331`` (commit bd07f2a121) in
https://github.com/anthropics/claude-code and the installed 2.1.331 binary.
The public repository provides the changelog. The installed bundle supplies the
configuration loader, including project-key normalization to NFC, and the rules
for blanking credential variables in remote headers.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from dataclasses import replace
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
    SourceReaderRefusal,
    SourceReadError,
    SourceReadLimitError,
    SourceServer,
    Value,
    ValueReason,
    WarningKind,
    by_name,
    convert,
    folders_down_to,
    git_root,
    is_str_list,
    is_str_mapping,
    linked_common_dir,
    located,
    missing_fields,
    normalize_js_server_name,
    positive_number,
    read_json_object,
    split_variables,
    variables,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

# Claude Code skips a project ``.mcp.json`` larger than this (``fwe`` in ``_Wn``).
_MCP_JSON_MAX_BYTES = 2 * 1024 * 1024
# A folder name Claude Code takes for its connector-downloads folder, after lowercasing, long s (U+017F) read
# as ``s`` and these invisible format characters dropped (``a0t``, ``m6o``).
_DOWNLOADS_NAME_RE = re.compile(r"connector-downloads[. ]*(?::.*)?")
_DOWNLOADS_INVISIBLE_RE = re.compile("[\u200c-\u200f\u202a-\u202e\u206a-\u206f\ufeff]")
# The sandbox folders browsers save downloads in, which Claude Code also skips.
_BROWSER_DOWNLOADS_ROOTS = ("/mnt/user-data/browser-downloads", "/mnt/user-data/desktop-browser-downloads")
_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(?P<default>[^}]*))?\}")
_MCP_DENY_RE = re.compile(r"mcp__[A-Za-z0-9_-]+(?:__\*)?")

# The variables Claude Code reads as empty in a remote server's headers, in any case and with an
# ``INPUT_`` prefix too [doc: mcp, "Credential variables that read as empty", which names examples;
# the names were checked against the 2.1.331 bundle]. Every ``CLAUDE_`` name is covered below.
_CREDENTIAL_NAMES = frozenset({
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_RUNTIME_TOKEN", "ACTIONS_RUNTIME_URL",
    "AGENT_PROXY_AUTH_TOKEN", "ALL_INPUTS", "ALL_PROXY", "ANACONDA_API_TOKEN", "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_AWS_API_KEY", "ANTHROPIC_CUSTOM_HEADERS", "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_AUTH_TOKEN", "ANTHROPIC_IDENTITY_TOKEN", "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ARTIFACTS_CREDENTIALPROVIDER_ACCESSTOKEN", "ARTIFACTS_CREDENTIALPROVIDER_EXTERNAL_FEED_ENDPOINTS",
    "AWS_BEARER_TOKEN_BEDROCK", "AWS_CONTAINER_AUTHORIZATION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_WEB_IDENTITY_TOKEN_FILE", "AZURE_AUTH_LOCATION", "AZURE_CLIENT_CERTIFICATE_PASSWORD",
    "AZURE_CLIENT_CERTIFICATE_PATH", "AZURE_CLIENT_SECRET", "AZURE_FEDERATED_TOKEN_FILE", "AZURE_PASSWORD",
    "BINSTAR_API_TOKEN", "CARGO_REGISTRY_TOKEN", "CCR_AGENT_PROXY_TOKEN_FILE_DESCRIPTOR", "CI_DEPLOY_USER",
    "CI_REGISTRY_USER", "CLOUDSDK_AUTH_ACCESS_TOKEN", "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
    "CLOUDSDK_AUTH_AUTHORIZATION_TOKEN_FILE", "CODEARTIFACT_AUTH_TOKEN", "COMPOSER_AUTH", "CONAN_LOGIN_USERNAME",
    "CONAN_PASSWORD", "CONSUL_HTTP_AUTH", "CONSUL_HTTP_TOKEN", "DEFAULT_WORKFLOW_TOKEN", "DISCORD_WEBHOOK",
    "DISCORD_WEBHOOK_URL", "ENVIRONMENT_SERVICE_KEY", "FASTLANE_SESSION", "FLIT_PASSWORD", "FLIT_USERNAME",
    "GEM_HOST_API_KEY", "GOAUTH", "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_GHA_CREDS_PATH",
    "GOOGLE_OAUTH_ACCESS_TOKEN", "GOPROXY", "HATCH_INDEX_AUTH", "HATCH_INDEX_USER", "HTTPS_PROXY", "HTTP_PROXY",
    "IDENTITY_HEADER", "JF_USER", "MATCH_GIT_BASIC_AUTHORIZATION", "MATURIN_PASSWORD", "MATURIN_PYPI_TOKEN",
    "MATURIN_USERNAME", "MCP_CLIENT_SECRET", "MCP_XAA_IDP_CLIENT_SECRET", "MSI_SECRET", "MS_TEAMS_WEBHOOK_URI",
    "NODE_AUTH_TOKEN", "NOMAD_HTTP_AUTH", "NOMAD_TOKEN", "NPM_TOKEN", "NUGET_AUTH_TOKEN", "OVERRIDE_GITHUB_TOKEN",
    "PIP_EXTRA_INDEX_URL", "PIP_INDEX_URL", "PYPI_API_TOKEN", "PYPI_TOKEN", "SELF_HOSTED_RUNNER_ENVIRONMENT_SECRET",
    "SELF_HOSTED_RUNNER_POOL_SECRET", "SLACK_WEBHOOK", "SLACK_WEBHOOK_URL", "SONARQUBE_SCANNER_PARAMS",
    "SONAR_SCANNER_JSON_PARAMS", "SONAR_TOKEN", "SSH_SIGNING_KEY", "TEAMS_WEBHOOK_URL", "TWINE_PASSWORD",
    "TWINE_USERNAME", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX_URL", "UV_INDEX", "UV_INDEX_URL", "UV_PUBLISH_PASSWORD",
    "UV_PUBLISH_TOKEN", "UV_PUBLISH_USERNAME", "VAULT_AUTH_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_TOKEN",
    "VSS_NUGET_ACCESSTOKEN", "VSS_NUGET_EXTERNAL_FEED_ENDPOINTS",
})  # fmt: skip
# Claude Code's own variables, telemetry settings, Git config injected through the environment and
# package registries' per-host credentials (the bundle's rules, widened to whole families).
_CREDENTIAL_PATTERN = re.compile(
    r"CLAUDE_.*|OTEL_.*|GIT_CONFIG_(?:PARAMETERS|(?:KEY|VALUE)_\d+)|CARGO_REGISTRIES_.*"
    r"|(?:ORG_GRADLE_PROJECT|POETRY_PYPI_TOKEN|POETRY_HTTP_BASIC|CONAN_LOGIN_USERNAME|CONAN_PASSWORD)_.*|BUNDLE_.*__.*"
)

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
        config_path, _ = _user_files(ctx)
        return config_path.is_file() or any(path.is_file() for path in _project_files(ctx))

    def discover(self, ctx: ImportContext) -> McpImportResult:
        errors: list[McpSourceError] = []

        def read_policy(path: Path) -> dict[str, Any] | None:
            try:
                return read_json_object(path) or {}
            except SourceReadError as exc:
                errors.append(exc.at(path))
                return None

        config_path, settings_path = _user_files(ctx)
        config = read_policy(config_path)
        if config is None:
            # This file contains both server definitions and the project's enablement and trust.
            return McpImportResult(source_errors=tuple(errors))
        key, project = _project_entry(config, ctx.workspace_cwd) if ctx.workspace_cwd else ("", {})
        trusted = project.get("hasTrustDialogAccepted") is True
        downloads = _downloads_folders(ctx.workspace_cwd, key) if ctx.workspace_cwd else ()
        rejected = _mcpjson_names(project.get("disabledMcpjsonServers"))
        approved: set[str] = set()
        # ``enableAllProjectMcpServers``, kept apart from the names so no listed name can stand for it.
        approve_all = False

        def approve(settings: Mapping[str, Any]) -> None:
            nonlocal approve_all
            approved.update(_mcpjson_names(settings.get("enabledMcpjsonServers")))
            blanket_approval = settings.get("enableAllProjectMcpServers")
            if isinstance(blanket_approval, bool):
                approve_all = blanket_approval

        def repository_approves(path: Path) -> bool:
            # A repository's own files can approve its servers only once the user trusts its folder,
            # and Claude Code reads no settings from a downloads folder.
            return trusted and not _is_inside(path.parent.parent, downloads)

        user_settings = read_policy(settings_path)
        if user_settings is None:
            return McpImportResult(source_errors=tuple(errors))
        shared_path = Path(ctx.workspace_cwd) / ".claude" / "settings.json" if ctx.workspace_cwd else None
        shared_settings = read_policy(shared_path) if shared_path else {}
        if shared_settings is None:
            return McpImportResult(source_errors=tuple(errors))
        rejected |= _mcpjson_names(user_settings.get("disabledMcpjsonServers"))
        rejected |= _mcpjson_names(shared_settings.get("disabledMcpjsonServers"))
        tool_denials = _mcp_tool_denials(user_settings) | _mcp_tool_denials(shared_settings)
        approve(user_settings)
        if shared_path and repository_approves(shared_path):
            approve(shared_settings)
        # Approvals older Claude Code kept in the project's entry, which it moves into the current
        # local settings file (``j4o``) only when the entry's rejections are a list of names.
        migrates = _migrates_entry_approvals(project)
        if trusted and migrates:
            approved.update(_mcpjson_names(project.get("enabledMcpjsonServers")))
        local_files = _local_settings_files(ctx, key)
        current = max((i for i, (_, read) in enumerate(local_files) if read), default=None)
        for index, (path, read_by_claude_code) in enumerate(local_files):
            settings = read_policy(path)
            if settings is None:
                # A policy file can restrict tools on any server.
                return McpImportResult(source_errors=tuple(errors))
            rejected |= _mcpjson_names(settings.get("disabledMcpjsonServers"))
            tool_denials.update(_mcp_tool_denials(settings))
            if read_by_claude_code and repository_approves(path):
                approve(settings)
                # The migration sets a legacy ``true`` only where the current file leaves the key unset.
                legacy_all = project.get("enableAllProjectMcpServers") is True
                if index == current and migrates and legacy_all and "enableAllProjectMcpServers" not in settings:
                    approve_all = True
        disabled = _names(project.get("disabledMcpServers"))

        def off(server: SourceServer, from_mcp_json: bool) -> bool:
            if server.name in disabled:
                return True
            name = _mcpjson_name(server.name)
            return from_mcp_json and (name in rejected or not (approve_all or name in approved))

        layers = [(list(_servers(config.get("mcpServers"), config_path)), False)]
        for path in _project_files(ctx):
            if _is_inside(path.parent, downloads):
                # It could replace an approved server's command by name; Claude Code reports it too.
                if os.path.lexists(path):
                    errors.append(McpSourceError(str(path), SourceErrorKind.DOWNLOADS_FOLDER))
                continue
            try:
                document = read_json_object(path, limit=_MCP_JSON_MAX_BYTES)
            except SourceReadLimitError:
                # Claude Code skips it too, keeping the other files.
                errors.append(McpSourceError(str(path), SourceErrorKind.TOO_LARGE))
                continue
            except SourceReadError as exc:
                errors.append(exc.at(path))
                if isinstance(exc, SourceReaderRefusal) and path.is_file():
                    # Claude Code may read this file, which could replace any lower-priority definition.
                    layers.clear()
                continue
            if document is not None:
                layers.append((list(_servers(document.get("mcpServers"), path)), True))
        layers.append((list(_servers(project.get("mcpServers"), config_path)), False))
        servers = (replace(s, enabled=False) if off(s, mcp_json) else s for group, mcp_json in layers for s in group)
        candidates = tuple(convert(_restrict_tools(server, tool_denials)) for server in by_name(servers))
        return McpImportResult(candidates, tuple(errors))


def _user_files(ctx: ImportContext) -> tuple[Path, Path]:
    """The user's ``.claude.json`` and settings file: in ``$CLAUDE_CONFIG_DIR`` if set, else in the home folder."""
    folder = ctx.environ.get("CLAUDE_CONFIG_DIR")
    if not folder:
        return ctx.home / ".claude.json", ctx.home / ".claude" / "settings.json"
    path = located(folder, ctx)
    return path / ".claude.json", path / "settings.json"


def _project_files(ctx: ImportContext) -> list[Path]:
    if not ctx.workspace_cwd:
        return []
    return [folder / ".mcp.json" for folder in folders_down_to(ctx.workspace_cwd, None) if folder != folder.parent]


def _local_settings_files(ctx: ImportContext, key: str) -> list[tuple[Path, bool]]:
    """The local settings files, lowest precedence first, each with whether Claude Code reads it.

    The workspace's, then the project folder's (the main checkout for a linked worktree) where Claude
    Code keeps them there; otherwise the project folder's file, read only for its restrictions, comes
    first.
    """
    if not ctx.workspace_cwd:
        return []
    workspace = Path(ctx.workspace_cwd) / ".claude" / "settings.local.json"
    if not key or Path(key) == _real(ctx.workspace_cwd):
        return [(workspace, True)]
    project = Path(key) / ".claude" / "settings.local.json"
    if _keeps_local_settings(key, ctx.home):
        return [(workspace, True), (project, True)]
    return [(project, False), (workspace, True)]


def _migrates_entry_approvals(project: Mapping[str, Any]) -> bool:
    """Whether Claude Code moves the approvals in the project's entry: its rejections, if any, are all names."""
    rejections = project.get("disabledMcpjsonServers")
    return rejections is None or (isinstance(rejections, list) and all(isinstance(n, str) for n in rejections))


def _keeps_local_settings(key: str, home: Path) -> bool:
    """Whether Claude Code keeps the local settings in the project folder *key*, not in the workspace.

    Not in the home folder, not on Windows (it cannot check owners there), and only in a folder whose
    ``.git`` and ``.claude`` entries, like the folder itself, belong to the user.
    """
    if Path(key) == _real(str(home)):
        return False
    if sys.platform == "win32":
        return False
    uid = os.geteuid()
    try:
        if os.stat(key).st_uid != uid or os.lstat(os.path.join(key, ".git")).st_uid != uid:
            return False
    except OSError, ValueError:
        return False
    try:
        return os.lstat(os.path.join(key, ".claude")).st_uid == uid
    except FileNotFoundError:
        return True
    except OSError, ValueError:
        return False


def _downloads_folders(*folders: str) -> tuple[Path, ...]:
    """The real folders on the way to *folders*, as spelled and as resolved, that may hold Claude Code's downloads."""
    found: list[Path] = []
    for folder in folders:
        for spelling in dict.fromkeys((Path(folder), _real(folder))):
            found.extend(_real(str(p)) for p in (spelling, *spelling.parents) if _is_downloads_folder(p))
    return tuple(dict.fromkeys(found))


def _is_downloads_folder(folder: Path) -> bool:
    """Whether *folder* may be Claude Code's downloads folder, judged by its name alone.

    Claude Code skips a folder with this name only once ``Vyt`` confirms it is its downloads
    folder: the sandbox's downloads root, a folder under its ``projects/<name>.<16 hex>/``, or one
    holding the ``.gitignore`` or ``download-<8 hex>`` entries it writes, minus a Git-root
    ownership exception, after expanding Windows short names; a failed check counts as downloads.
    Those markers and exceptions are undocumented internals that can change in any release, so the
    import does not replicate them. The two possible mistakes differ: skipping an ordinary project that
    happens to use this name is listed in the import window and fixed by renaming or moving the
    folder, while reading a real downloads folder would let a downloaded ``.mcp.json`` silently
    replace an approved server's command, since approvals match by name.
    """
    if folder.as_posix() in _BROWSER_DOWNLOADS_ROOTS:
        return True
    name = folder.name
    folded = _DOWNLOADS_INVISIBLE_RE.sub("", name.lower().replace("\u017f", "s"))
    return len(name) >= len("connector-downloads") and _DOWNLOADS_NAME_RE.fullmatch(folded) is not None


def _is_inside(path: Path, folders: Iterable[Path]) -> bool:
    real = _real(str(path))
    return any(real == folder or folder in real.parents for folder in folders)


def _real(path: str) -> Path:
    try:
        return Path(os.path.realpath(path))
    except OSError, ValueError:
        return Path(path)


def _project_key(workspace_cwd: str) -> str:
    """The folder Claude Code keys the workspace's project by.

    The Git root, except in a linked worktree: then the main checkout, or for a bare repository's
    worktree the bare repository's folder.
    """
    root = git_root(workspace_cwd)
    if root is None:
        try:
            return os.path.realpath(workspace_cwd)
        except OSError, ValueError:
            return workspace_cwd
    common = linked_common_dir(root)
    if common is None:
        return root
    if os.path.basename(common) == ".git":
        return os.path.dirname(common)
    # A bare repository's folder, unless it holds a .git of its own (then Claude Code keeps the worktree).
    return root if os.path.lexists(os.path.join(common, ".git")) else common


def _project_entry(config: Mapping[str, Any], workspace_cwd: str) -> tuple[str, dict[str, Any]]:
    """The project's folder and what Claude Code keeps for it in ``projects``."""
    key = _project_key(workspace_cwd)
    # Claude Code NFC-normalizes the folder it starts in before keying the project by it; the key
    # stays as found for the files read from that folder.
    project = _entry(config, unicodedata.normalize("NFC", key))
    return key, project if project is not None else {}


def _entry(config: Mapping[str, Any], folder: str) -> dict[str, Any] | None:
    projects = config.get("projects")
    if not isinstance(projects, dict):
        return None
    # Claude Code writes a Windows key with ``/`` and looks it up exactly, letter case included: after a
    # folder's case changed, its old key may still be there, ahead of the current one.
    windows = get_platform().is_windows
    spelled = folder.replace("\\", "/") if windows else folder
    project = projects.get(spelled)
    if project is None and windows:
        # The drive letter's case follows how each app was started (``c:`` or ``C:``); the rest is the folder's.
        project = next((value for key, value in projects.items() if _drive_folded(key) == _drive_folded(spelled)), None)
    return project if isinstance(project, dict) else None


def _drive_folded(path: str) -> str:
    return path[0].lower() + path[1:] if path[1:2] == ":" else path


def _mcpjson_names(value: object) -> set[str]:
    return {_mcpjson_name(name) for name in _names(value)}


def _mcpjson_name(name: str) -> str:
    """*name* as Claude Code compares ``.mcp.json`` approvals: other characters as ``_``; plugins as written."""
    return name if name.startswith("plugin:") else _normalized_mcp_name(name)


def _normalized_mcp_name(name: str) -> str:
    normalized = normalize_js_server_name(name)
    return re.sub(r"_+", "_", normalized).strip("_") if name.startswith("claude.ai ") else normalized


def _names(value: object) -> set[str]:
    return {name for name in value if isinstance(name, str)} if isinstance(value, list) else set()


def _mcp_tool_denials(settings: Mapping[str, Any]) -> set[str]:
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        return set()
    return {rule for rule in _names(permissions.get("deny")) if _MCP_DENY_RE.fullmatch(rule)}


def _restrict_tools(server: SourceServer, denials: set[str]) -> SourceServer:
    namespace = f"mcp__{_normalized_mcp_name(server.name)}"
    prefix = f"{namespace}__"
    if (
        namespace in denials
        or f"{prefix}*" in denials
        or any(rule.startswith(prefix) and not rule.endswith("*") and len(rule) > len(prefix) for rule in denials)
    ):
        warning = ImportWarning(WarningKind.TOOLS_RESTRICTED, ("permissions.deny",))
        return replace(server, allowed_tools=(), warnings=(*server.warnings, warning))
    return server


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
    blocked, use = next(
        (
            (f"{prefix}.{field}", use)
            for field, value, use in (
                ("command", command, FieldUse.COMMAND),
                *((f"args[{i}]", arg, FieldUse.ARGUMENT) for i, arg in enumerate(args)),
            )
            if _DOLLAR_RE.search(value)
        ),
        ("", FieldUse.VALUE),
    )
    values = {key: _value(text) for key, text in env.items()}
    warnings = [
        missing_fields(entry, _STDIO_FIELDS, prefix),
        _default_warning(env.items(), f"{prefix}.env"),
    ]
    return SourceServer(
        name,
        path,
        "stdio",
        command=command,
        args=tuple(args),
        command_path=f"{prefix}.command",
        arg_paths=tuple(f"{prefix}.args[{i}]" for i in range(len(args))),
        env=values,
        timeout_seconds=_timeout(entry),
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=blocked,
        blocked_problem=Problem.VARIABLE_NOT_SUPPORTED,
        blocked_reason=ValueReason.VARIABLE,
        blocked_use=use,
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
        if any(is_credential_variable(variable) for variable in variables(value)):
            dropped.append(f"{prefix}.headers.{key}")
        else:
            values[key] = value
    warnings = [
        ImportWarning(WarningKind.AUTH_HEADERS, tuple(dropped)) if dropped else None,
        ImportWarning(WarningKind.AUTH_NOT_IMPORTED) if "oauth" in entry else None,
        missing_fields(entry, _HTTP_FIELDS, prefix),
        _default_warning(((key, headers[key]) for key in values), f"{prefix}.headers"),
    ]
    return SourceServer(
        name,
        path,
        "http",
        url=url,
        url_path=f"{prefix}.url",
        headers=values,
        timeout_seconds=_timeout(entry),
        warnings=tuple(warning for warning in warnings if warning is not None),
        blocked_field=f"{prefix}.url" if _DOLLAR_RE.search(url) else "",
        blocked_problem=Problem.VARIABLE_NOT_SUPPORTED,
        blocked_reason=ValueReason.VARIABLE,
        blocked_use=FieldUse.URL,
        headers_path=f"{prefix}.headers",
    )


def is_credential_variable(name: str) -> bool:
    """Whether Claude Code reads ``${name}`` as empty in a remote server's headers.

    In any case (on Windows ``${anthropic_api_key}`` resolves to ``ANTHROPIC_API_KEY``) and with the
    ``INPUT_`` prefix GitHub Actions adds.
    """
    upper = name.upper().removeprefix("INPUT_")
    return upper in _CREDENTIAL_NAMES or _CREDENTIAL_PATTERN.fullmatch(upper) is not None


def _value(text: str) -> Value:
    return split_variables(text, _DOLLAR_RE)


def _default_warning(values: Iterable[tuple[str, str]], prefix: str) -> ImportWarning | None:
    fields = tuple(
        f"{prefix}.{key}"
        for key, text in values
        if any(match.group("default") is not None for match in _DOLLAR_RE.finditer(text))
    )
    return ImportWarning(WarningKind.VARIABLE_DEFAULTS_DROPPED, fields) if fields else None


def _timeout(entry: dict[str, Any]) -> float | None:
    """``timeout`` in milliseconds as seconds; Claude Code ignores values under one second."""
    milliseconds = positive_number(entry.get("timeout"))
    return milliseconds / 1000 if milliseconds is not None and milliseconds >= 1000 else None
