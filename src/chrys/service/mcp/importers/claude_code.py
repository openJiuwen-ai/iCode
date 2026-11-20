# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Import MCP servers configured in Claude Code.

Read (https://code.claude.com/docs/en/mcp, "MCP installation scopes"), lowest
precedence first, a same-named server replacing an earlier one:

- ``user``: top-level ``mcpServers`` of ``~/.claude.json`` (or
  ``$CLAUDE_CONFIG_DIR/.claude.json``; a relative folder is found from the
  workspace);
- ``project``: ``mcpServers`` of ``.mcp.json`` in the Git root, then in the workspace;
- ``local``: ``projects[<project>].mcpServers`` of the same ``.claude.json``, where
  the project is the workspace's Git root (for a linked worktree, the main
  checkout every worktree shares, or the bare repository), else the workspace,
  looked up as Claude Code keys it: in NFC Unicode form, with ``/`` on Windows.

Values in ``env`` and ``headers`` may reference ``${VAR}`` or ``${VAR:-default}``;
iCode has no default syntax, so the default is dropped. Missing or empty
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
as in Claude Code. Managed settings are not read.

Checked against the docs above and
https://code.claude.com/docs/en/settings-reference: the scopes and variables
on 2026-11-04 (Claude Code 2.1.318), turning servers off and approvals on
2026-11-11. The project key, the credential variables and trust were read
from the Claude Code 2.1.318 bundle, as the docs name only examples of them.
The public repository was checked again on 2026-11-12: latest tag v2.1.322
(commit 6e1e44f643), main at 927b66497b. Its changelog reports MCP OAuth
redirect hardening, but it does not contain the full configuration loader.
The bundle checks above remain
the basis for project keys, credential variables and trust; the NFC form of the
project key was read from the Claude Code 2.1.330 binary on 2026-11-23.
Recheck those when Claude Code changes where it keeps servers or how it turns
them off.
"""

from __future__ import annotations

import os
import re
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
    SourceReadError,
    SourceServer,
    Value,
    ValueReason,
    WarningKind,
    by_name,
    convert,
    git_root,
    is_str_list,
    is_str_mapping,
    linked_common_dir,
    located,
    missing_fields,
    path_matches,
    positive_number,
    project_folders,
    read_json_object,
    split_variables,
    variables,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_DOLLAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")

# The variables Claude Code reads as empty in a remote server's headers, in any case and with an
# ``INPUT_`` prefix too [doc: mcp, "Credential variables that read as empty", which names examples;
# the full set read from the 2.1.318 bundle]. Every ``CLAUDE_`` name is covered by the pattern below.
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
_NAME_CHARACTER_RE = re.compile(r"[^a-zA-Z0-9_-]")

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

        def read(path: Path) -> dict[str, Any]:
            try:
                return read_json_object(path) or {}
            except SourceReadError as exc:
                errors.append(exc.at(path))
                return {}

        config_path, settings_path = _user_files(ctx)
        config = read(config_path)
        key, project = _project_entry(config, ctx.workspace_cwd) if ctx.workspace_cwd else ("", {})
        user = list(_servers(config.get("mcpServers"), config_path))
        shared = [server for path in _project_files(ctx) for server in _servers(read(path).get("mcpServers"), path)]
        local = list(_servers(project.get("mcpServers"), config_path))
        repo_settings = _repo_settings_files(ctx, key)
        trusted = project.get("hasTrustDialogAccepted") is True
        rejected = _mcpjson_names(project.get("disabledMcpjsonServers"))
        # Older Claude Code kept approvals in the project's entry; it now moves them to the local
        # settings, which count only in a trusted project.
        approved: set[str] = set()
        # ``enableAllProjectMcpServers``, kept apart from the names so no listed name can stand for it.
        approve_all = False

        def approve(settings: Mapping[str, Any]) -> None:
            nonlocal approve_all
            approved.update(_mcpjson_names(settings.get("enabledMcpjsonServers")))
            approve_all = approve_all or settings.get("enableAllProjectMcpServers") is True

        if trusted:
            approve(project)
        user_settings = read(settings_path)
        rejected |= _mcpjson_names(user_settings.get("disabledMcpjsonServers"))
        approve(user_settings)
        for path in repo_settings:
            settings = read(path)
            rejected |= _mcpjson_names(settings.get("disabledMcpjsonServers"))
            # A repository's own files can approve its servers only once the user trusts its folder.
            if trusted:
                approve(settings)
        disabled = _names(project.get("disabledMcpServers"))

        def off(server: SourceServer, from_mcp_json: bool) -> bool:
            if server.name in disabled:
                return True
            name = _mcpjson_name(server.name)
            return from_mcp_json and (name in rejected or not (approve_all or name in approved))

        layers = ((user, False), (shared, True), (local, False))
        servers = (replace(s, enabled=False) if off(s, mcp_json) else s for group, mcp_json in layers for s in group)
        return McpImportResult(tuple(convert(server) for server in by_name(servers)), tuple(errors))


def _user_files(ctx: ImportContext) -> tuple[Path, Path]:
    """The user's ``.claude.json`` and settings file: in ``$CLAUDE_CONFIG_DIR`` if set, else in the home folder."""
    folder = ctx.environ.get("CLAUDE_CONFIG_DIR")
    if not folder:
        return ctx.home / ".claude.json", ctx.home / ".claude" / "settings.json"
    path = located(folder, ctx)
    return path / ".claude.json", path / "settings.json"


def _project_files(ctx: ImportContext) -> list[Path]:
    return [folder / ".mcp.json" for folder in project_folders(ctx.workspace_cwd)]


def _repo_settings_files(ctx: ImportContext, key: str) -> list[Path]:
    """The repository's settings files that approve or reject ``.mcp.json`` servers.

    The workspace's shared one and its local ones, which Claude Code keeps in the project's folder
    (the main checkout for a linked worktree; in the workspace before 2.1.211).
    """
    if not ctx.workspace_cwd:
        return []
    repo = [Path(ctx.workspace_cwd) / ".claude" / "settings.json"]
    folders = [*project_folders(ctx.workspace_cwd), *([Path(key)] if key else [])]
    repo += [folder / ".claude" / "settings.local.json" for folder in dict.fromkeys(folders)]
    return repo


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
    if project is None:
        project = next((value for key, value in projects.items() if path_matches(key, folder)), None)
    return project if isinstance(project, dict) else None


def _drive_folded(path: str) -> str:
    return path[0].lower() + path[1:] if path[1:2] == ":" else path


def _mcpjson_names(value: object) -> set[str]:
    return {_mcpjson_name(name) for name in _names(value)}


def _mcpjson_name(name: str) -> str:
    """*name* as Claude Code compares ``.mcp.json`` approvals: other characters as ``_``; plugins as written."""
    if name.startswith("plugin:"):
        return name
    # Per UTF-16 code unit, as Claude Code's JavaScript replaces: a character beyond U+FFFF is two.
    normalized = _NAME_CHARACTER_RE.sub(lambda match: "_" * (2 if ord(match.group()) > 0xFFFF else 1), name)
    return re.sub(r"_+", "_", normalized).strip("_") if name.startswith("claude.ai ") else normalized


def _names(value: object) -> set[str]:
    return {name for name in value if isinstance(name, str)} if isinstance(value, list) else set()


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
    warnings = [missing_fields(entry, _STDIO_FIELDS, prefix)]
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


def _timeout(entry: dict[str, Any]) -> float | None:
    """``timeout`` in milliseconds as seconds; Claude Code ignores values under one second."""
    milliseconds = positive_number(entry.get("timeout"))
    return milliseconds / 1000 if milliseconds is not None and milliseconds >= 1000 else None
