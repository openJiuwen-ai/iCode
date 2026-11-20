# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""pi: where servers are found and how each entry converts."""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from chrys.foundation.util.env_templates import resolve_env_templates
from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind, pi
from chrys.service.mcp.importers import WarningKind as Kind
from chrys.service.mcp.importers.pi import PiSource
from tests.support.symlinks import symlink_or_skip

if TYPE_CHECKING:
    from chrys.service.mcp.importers import McpImportCandidate


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")


def _global(home: Path) -> Path:
    return home / ".pi" / "agent" / "mcp.json"


def _discover(home: Path, workspace: Path | None = None, environ: dict[str, str] | None = None):
    return PiSource().discover(ImportContext(str(workspace) if workspace else None, home, environ or {}))


def _only(home: Path, entry: dict[str, Any]) -> McpImportCandidate:
    _write(_global(home), {"mcpServers": {"srv": entry}})
    (candidate,) = _discover(home).candidates
    return candidate


@pytest.mark.parametrize("exposure", ["hidden", "deferred"])
@pytest.mark.parametrize("tool", ["read,delete", " read", ""])
def test_unrepresentable_tool_policy_refuses_the_original_setting(tmp_path: Path, exposure: str, tool: str) -> None:
    candidate = _only(tmp_path, {"command": "run", "exposure": exposure, "toolExposure": {tool: "direct"}})

    assert candidate.config is None
    assert candidate.problem is Problem.VALUE_NOT_REPRESENTABLE
    assert candidate.problem_field == "mcpServers.srv.toolExposure"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"command": "~/bin/srv", "args": ["~/data"], "env": {"A": "$TOKEN", "B": "${X}-$$-$!"}, "timeout": 1.5},
            {"command": "{home}/bin/srv", "args": ["{home}/data"], "env": {"A": "{{TOKEN}}", "B": "{{X}}-$-!"}},
        ),
        (
            {
                "url": "https://x",
                "headers": {"Authorization": "Bearer ${KEY}"},
                "description": "Docs",
                "enabled": False,
            },
            {
                "transport": "http",
                "headers": {"Authorization": "Bearer {{KEY}}"},
                "description": "Docs",
                "enabled": False,
            },
        ),
        ({"command": "run"}, {"use_progressive_disclosure": True, "allowed_tools": None, "request_timeout": 60}),
        ({"command": "run", "exposure": "direct"}, {"use_progressive_disclosure": False}),
        ({"command": "run", "exposure": "hidden"}, {"allowed_tools": []}),
        (
            {"command": "run", "exposure": "hidden", "toolExposure": {"read": "deferred", "list": "direct"}},
            {"allowed_tools": ["read", "list"], "use_progressive_disclosure": True, "always_load": ["list"]},
        ),
        (
            {"command": "run", "toolExposure": {"search": "direct"}},
            {"use_progressive_disclosure": True, "always_load": ["search"]},
        ),
    ],
    ids=["stdio", "http", "default-exposure", "direct", "hidden", "hidden-with-exceptions", "always-load"],
)
def test_entries_convert_to_icode_servers(tmp_path: Path, entry: dict[str, Any], expected: dict[str, Any]) -> None:
    candidate = _only(tmp_path, entry)

    assert candidate.config is not None
    assert candidate.warnings == ()
    for key, value in expected.items():
        wanted = value.replace("{home}", str(tmp_path)) if isinstance(value, str) else value
        if isinstance(value, str) and value.startswith("{home}"):
            wanted = os.path.normpath(wanted)
        if isinstance(value, list):
            wanted = [os.path.normpath(item.replace("{home}", str(tmp_path))) for item in value]
        assert getattr(candidate.config, key) == wanted


@pytest.mark.parametrize(
    ("entry", "warnings", "allowed"),
    [
        (
            {"command": "run", "exposure": "direct", "toolExposure": {"rm": "hidden"}},
            (ImportWarning(Kind.TOOLS_RESTRICTED),),
            [],
        ),
        (
            {"command": "run", "exposure": "direct", "toolExposure": {"big_*": "deferred"}},
            (ImportWarning(Kind.TOOL_LOADING, ("mcpServers.srv.toolExposure.big_*",)),),
            None,
        ),
        ({"url": "https://x", "oauth": {}}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),), None),
        ({"url": "https://x", "auth": {"provider": "github"}}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),), None),
        ({"command": "run", "cwd": "/srv"}, (), None),
    ],
    ids=["hidden-tool", "pattern", "oauth", "provider", "cwd"],
)
def test_what_the_import_changes_is_reported(
    tmp_path: Path, entry: dict[str, Any], warnings: tuple[ImportWarning, ...], allowed: list[str] | None
) -> None:
    candidate = _only(tmp_path, entry)

    assert candidate.config is not None
    assert candidate.warnings == warnings
    assert candidate.config.allowed_tools == allowed


def test_oauth_metadata_beside_an_authorization_header_needs_no_warning(tmp_path: Path) -> None:
    candidate = _only(tmp_path, {"url": "https://x", "oauth": {}, "headers": {"authorization": "Bearer $T"}})

    assert candidate.warnings == ()


def test_http_cwd_is_reported_as_not_imported(tmp_path: Path) -> None:
    candidate = _only(tmp_path, {"url": "https://x", "cwd": "/unused", "headers": {"Authorization": "token"}})
    assert candidate.config is not None
    assert candidate.config.cwd == ""
    assert candidate.warnings == (ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.cwd",)),)


def test_a_value_from_a_command_is_listed_for_manual_setup(tmp_path: Path) -> None:
    candidate = _only(tmp_path, {"url": "https://x", "headers": {"X": "1", "Authorization": "!op read token"}})

    assert (candidate.config, candidate.problem, candidate.problem_field) == (
        None,
        Problem.VALUE_FROM_FILE_OR_COMMAND,
        "mcpServers.srv.headers.Authorization",
    )


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "sse", "url": "https://x"},
        {"url": "ftp://x"},
        {"url": "example.com/mcp"},
        {"command": "run", "exposure": "sometimes"},
        {"command": "run", "exposure": ["direct"]},
        {"command": "run", "exposure": {}},
        {"command": "run", "toolExposure": {"a": ["direct"]}},
        {"command": "run", "toolExposure": {"a": {}}},
        {"command": "run", "timeout": 0},
        {"description": "nothing to run"},
    ],
    ids=[
        "sse",
        "bad-url",
        "no-scheme",
        "bad-exposure",
        "exposure-list",
        "exposure-object",
        "tool-exposure-list",
        "tool-exposure-object",
        "zero-timeout",
        "no-transport",
    ],
)
def test_entries_pi_rejects_are_not_listed(tmp_path: Path, entry: dict[str, Any]) -> None:
    _write(_global(tmp_path), {"mcpServers": {"srv": entry, "good": {"command": "run"}}})

    assert [c.name for c in _discover(tmp_path).candidates] == ["good"]


def test_project_servers_replace_global_ones_and_overrides_only_change_exposure(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(
        _global(tmp_path),
        {
            "mcpServers": {
                "a": {"command": "global", "cwd": "a-dir"},
                "b": {"command": "global", "env": {"K": "v"}, "cwd": "b-dir"},
            }
        },
    )
    _write(
        workspace / ".pi" / "mcp.json",
        {
            "mcpServers": {
                "a": {"enabled": False, "exposure": "direct"},
                "b": {"command": "project"},
                "c": {"enabled": True},
            }
        },
    )
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})

    candidates = {c.name: c for c in _discover(tmp_path, workspace).candidates}

    a, b = candidates["a"].config, candidates["b"].config
    assert set(candidates) == {"a", "b"}
    assert a is not None and b is not None
    assert (a.command, a.enabled, a.use_progressive_disclosure) == ("global", False, False)
    assert (b.command, b.env, b.cwd) == ("project", {}, "")
    assert a.cwd == "a-dir"


@pytest.mark.parametrize(
    ("trust", "settings", "read"),
    [
        ({}, {}, False),
        ({}, {"defaultProjectTrust": "always"}, True),
        ({}, {"defaultProjectTrust": "ask"}, False),
        ({"parent": True}, {}, True),
        ({"parent": True, "workspace": None}, {}, True),
        ({"parent": True, "workspace": False}, {"defaultProjectTrust": "always"}, False),
    ],
    ids=["undecided", "always", "ask", "parent-trusted", "unset-entry-skipped", "nearest-decides"],
)
def test_the_project_file_is_read_only_once_the_user_trusts_it(
    tmp_path: Path, trust: dict[str, bool | None], settings: dict[str, str], read: bool
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "parent" / "ws"
    folders = {"parent": tmp_path / "parent", "workspace": workspace}
    _write(home / ".pi" / "agent" / "trust.json", {str(folders[name]): value for name, value in trust.items()})
    _write(home / ".pi" / "agent" / "settings.json", settings)
    _write(_global(home), {"mcpServers": {"a": {"command": "global"}}})
    _write(workspace / ".pi" / "mcp.json", {"mcpServers": {"a": {"command": "project"}, "b": {"command": "run"}}})

    commands = {c.name: c.config.command for c in _discover(home, workspace).candidates if c.config}

    assert commands == ({"a": "project", "b": "run"} if read else {"a": "global"})


def test_an_http_server_ignores_settings_only_a_command_reads(tmp_path: Path) -> None:
    candidate = _only(tmp_path, {"url": "https://x", "args": None, "headers": {"Authorization": "Bearer $T"}})

    assert candidate.config is not None
    assert (candidate.config.url, candidate.config.command, candidate.config.args) == ("https://x", "", [])


def test_the_agent_folder_variable_moves_the_global_file(tmp_path: Path) -> None:
    _write(tmp_path / "custom" / "mcp.json", {"mcpServers": {"srv": {"command": "run"}}})

    result = _discover(tmp_path, environ={"PI_CODING_AGENT_DIR": str(tmp_path / "custom")})

    assert [c.name for c in result.candidates] == ["srv"]


def test_a_file_with_comments_is_reported(tmp_path: Path) -> None:
    _write(_global(tmp_path), '{"mcpServers": {} // pi reads strict JSON\n}')

    (error,) = _discover(tmp_path).source_errors

    assert (error.kind, error.line) == (SourceErrorKind.UNPARSABLE, 1)


@pytest.mark.parametrize(
    "text",
    ["${INVALID:-$TOKEN}", "${INVALID:-${TOKEN}}", "${INVALID:-$$}", "${INVALID:-$!}", "${$TOKEN}", "${9}"],
)
@pytest.mark.parametrize("field", ["env", "headers"])
@pytest.mark.parametrize("token_set", [False, True])
def test_invalid_braced_blocks_stay_literal_at_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, field: str, token_set: bool
) -> None:
    if token_set:
        monkeypatch.setenv("TOKEN", "fictional-secret")
    else:
        monkeypatch.delenv("TOKEN", raising=False)
    entry = {"command": "run"} if field == "env" else {"url": "https://x"}
    candidate = _only(tmp_path, {**entry, field: {"X": text}})

    assert candidate.config is not None
    values = candidate.config.env if field == "env" else candidate.config.headers
    assert resolve_env_templates(values["X"], location="imported value") == text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("$$TOKEN", "$TOKEN"),
        ("$!TOKEN", "!TOKEN"),
        ("$TOKEN/${TOKEN}", "fictional/fictional"),
        ("${INVALID:-$TOKEN", "${INVALID:-fictional"),
        ("${INVALID:-$TOKEN}:$TOKEN", "${INVALID:-$TOKEN}:fictional"),
        ("${INVALID:-${TOKEN} $TOKEN}", "${INVALID:-${TOKEN} fictional}"),
        ("$9/$", "$9/$"),
    ],
)
def test_templates_resume_at_pi_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, expected: str
) -> None:
    monkeypatch.setenv("TOKEN", "fictional")
    candidate = _only(tmp_path, {"command": "run", "env": {"X": text}})

    assert candidate.config is not None
    assert resolve_env_templates(candidate.config.env["X"], location="imported env") == expected


def test_home_expansion_collapses_parents_before_following_symlinks(tmp_path: Path) -> None:
    home, other = tmp_path / "home", tmp_path / "other" / "dir"
    home.mkdir()
    other.mkdir(parents=True)
    symlink_or_skip(home / "link", other, target_is_directory=True)
    (home / "server").write_text("intended program", encoding="utf-8")
    (other.parent / "server").write_text("different program", encoding="utf-8")

    candidate = _only(home, {"command": "~/link/../server", "args": ["~/link/../server", "link/../server"]})

    assert candidate.config is not None
    assert candidate.config.command == str(home / "server")
    assert candidate.config.args == [str(home / "server"), "link/../server"]
    assert Path(candidate.config.command).read_text(encoding="utf-8") == "intended program"


@pytest.mark.parametrize(
    ("windows", "home", "value", "expected"),
    [
        (False, "/home/me", "~/link/../server", "/home/me/server"),
        (False, "/home/me", "~//link/../server/", "/home/me/server/"),
        (False, "/home/me", "~/", "/home/me"),
        (False, "/", "~/server", "/server"),
        (False, "/home/me", r"~\link\..\server", r"~\link\..\server"),
        (True, r"C:\Users\me", r"~\link\..\server", r"C:\Users\me\server"),
        (True, r"C:\Users\me", "~//link/../server/", "C:\\Users\\me\\server\\"),
        (True, r"C:\Users\me", "~/", r"C:\Users\me"),
        (True, r"C:\Users\me", "~//D:/server", r"C:\Users\me\D:\server"),
    ],
)
def test_home_expansion_matches_node_join(
    monkeypatch: pytest.MonkeyPatch, windows: bool, home: str, value: str, expected: str
) -> None:
    monkeypatch.setattr(pi, "get_platform", lambda: SimpleNamespace(is_windows=windows))

    home_path = PureWindowsPath(home) if windows else PurePosixPath(home)
    assert pi._expand_home(value, home_path) == expected


def test_home_expansion_normalizes_the_agent_folder_before_reading(tmp_path: Path) -> None:
    home, other = tmp_path / "home", tmp_path / "other" / "dir"
    home.mkdir()
    other.mkdir(parents=True)
    symlink_or_skip(home / "link", other, target_is_directory=True)
    _write(home / "agent" / "mcp.json", {"mcpServers": {"srv": {"command": "intended"}}})
    _write(other.parent / "agent" / "mcp.json", {"mcpServers": {"srv": {"command": "different"}}})

    (candidate,) = _discover(home, environ={"PI_CODING_AGENT_DIR": "~/link/../agent"}).candidates

    assert candidate.config is not None
    assert candidate.config.command == "intended"


@pytest.mark.parametrize(
    "entry",
    [
        {"enabled": "false"},
        {"exposure": "invalid"},
        {"toolExposure": {"read": 1}},
        {"env": {"X": "not-an-override"}},
        {"command": "project", "enabled": "false"},
        {"command": "project", "args": "not-an-array"},
        {"command": "project", "cwd": None},
        {"command": "project", "type": None},
        {"command": "project", "type": ""},
        {"url": "ftp://x"},
        {"url": "example.com/mcp"},
        {"url": "https://x", "auth": {"provider": "github"}},
        {"url": "https://x", "oauth": False},
        {"url": "https://x", "auth": {}},
    ],
)
def test_invalid_project_entries_keep_the_valid_global_server(tmp_path: Path, entry: dict[str, Any]) -> None:
    workspace = tmp_path / "ws"
    project = workspace / ".pi" / "mcp.json"
    _write(_global(tmp_path), {"mcpServers": {"srv": {"command": "global", "env": {"X": "kept"}}}})
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})
    _write(project, {"mcpServers": {"srv": entry}})

    result = _discover(tmp_path, workspace)

    (candidate,) = result.candidates
    assert candidate.config is not None
    assert (candidate.config.command, candidate.config.env) == ("global", {"X": "kept"})
    assert candidate.source_path == str(_global(tmp_path))
    (error,) = result.source_errors
    assert (error.path, error.kind, error.field) == (str(project), SourceErrorKind.INVALID, "mcpServers.srv")


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "project", "env": {"X": "!get-token"}},
        {"command": "project", "env": {"X": " leading-space"}},
        {"exposure": "hidden", "toolExposure": {"read,write": "direct"}},
    ],
)
def test_valid_but_unrepresentable_project_entries_still_replace_global_servers(
    tmp_path: Path, entry: dict[str, Any]
) -> None:
    workspace = tmp_path / "ws"
    _write(_global(tmp_path), {"mcpServers": {"srv": {"command": "global"}}})
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})
    _write(workspace / ".pi" / "mcp.json", {"mcpServers": {"srv": entry}})

    result = _discover(tmp_path, workspace)

    (candidate,) = result.candidates
    assert candidate.config is None
    assert candidate.problem is not None
    assert result.source_errors == ()


@pytest.mark.parametrize(
    ("fields", "warn"),
    [
        ({}, True),
        ({"headers": {"X-Client": "Pi"}}, True),
        ({"headers": {"Authorization": "token"}}, False),
        ({"headers": {"aUtHoRiZaTiOn": "token"}}, False),
        ({"oauth": {}, "headers": {"authorization": "token"}}, False),
        ({"auth": {"provider": "github"}, "headers": {"Authorization": "token"}}, True),
    ],
)
def test_authentication_warnings_follow_pi_defaults(tmp_path: Path, fields: dict[str, Any], warn: bool) -> None:
    candidate = _only(tmp_path, {"url": "https://x", **fields})

    assert candidate.config is not None
    assert candidate.warnings == ((ImportWarning(Kind.AUTH_NOT_IMPORTED),) if warn else ())


@pytest.mark.parametrize(
    "oauth",
    [None, False, [], "not-an-object"],
)
def test_invalid_oauth_settings_are_source_errors(tmp_path: Path, oauth: object) -> None:
    _write(_global(tmp_path), {"mcpServers": {"bad": {"url": "https://x", "oauth": oauth}}})

    result = _discover(tmp_path)

    assert result.candidates == ()
    (error,) = result.source_errors
    assert (error.path, error.kind, error.field) == (str(_global(tmp_path)), SourceErrorKind.INVALID, "mcpServers.bad")


@pytest.mark.parametrize(
    "oauth",
    [
        {"clientId": "id", "clientSecret": "$TOKEN", "scope": "read write"},
        {"clientRegistration": "cimd", "callbackUrl": "http://localhost/callback"},
    ],
)
def test_unmigrated_oauth_options_are_not_revalidated_without_an_override(
    tmp_path: Path, oauth: dict[str, Any]
) -> None:
    candidate = _only(tmp_path, {"url": "https://x", "oauth": oauth})

    assert candidate.config is not None
    assert candidate.warnings == (ImportWarning(Kind.AUTH_NOT_IMPORTED),)


def test_an_exposure_override_keeps_global_provider_auth(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(_global(tmp_path), {"mcpServers": {"srv": {"url": "https://x", "auth": {"provider": "github"}}}})
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})
    _write(workspace / ".pi" / "mcp.json", {"mcpServers": {"srv": {"enabled": False}}})

    result = _discover(tmp_path, workspace)

    (candidate,) = result.candidates
    assert candidate.config is not None
    assert candidate.config.enabled is False
    assert candidate.warnings == (ImportWarning(Kind.AUTH_NOT_IMPORTED),)
    assert result.source_errors == ()


def test_a_project_namespace_clash_keeps_the_loaded_global_server(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(_global(tmp_path), {"mcpServers": {"a-b": {"command": "global"}}})
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})
    _write(workspace / ".pi" / "mcp.json", {"mcpServers": {"a_b": {"command": "project"}}})

    result = _discover(tmp_path, workspace)

    assert [candidate.name for candidate in result.candidates] == ["a-b"]
    (error,) = result.source_errors
    assert error.field == "mcpServers.a_b"


def test_partial_http_override_preserves_its_priority_with_unmigrated_oauth(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(
        _global(tmp_path),
        {"mcpServers": {"srv": {"url": "https://x", "oauth": {"clientRegistration": "cimd"}}}},
    )
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})
    _write(workspace / ".pi" / "mcp.json", {"mcpServers": {"srv": {"enabled": False}}})

    result = _discover(tmp_path, workspace)

    (candidate,) = result.candidates
    assert candidate.config is not None
    assert candidate.config.enabled is False
    assert candidate.warnings == (ImportWarning(Kind.AUTH_NOT_IMPORTED),)
    assert candidate.source_path == str(_global(tmp_path))
    assert result.source_errors == ()


@pytest.mark.parametrize(
    ("fields", "url"),
    [
        ({}, "https://project.example/mcp"),
        ({"oauth": {"clientId": "id"}}, "https://project.example/mcp"),
        # URL details are copied as written; only the scheme's letter case changes.
        ({"url": "HTTPS://Project.example/Case?Key=Value"}, "https://Project.example/Case?Key=Value"),
        ({"url": "http://127.1/mcp"}, "http://127.1/mcp"),
    ],
)
def test_http_project_servers_replace_global_ones(tmp_path: Path, fields: dict[str, Any], url: str) -> None:
    workspace = tmp_path / "ws"
    project = workspace / ".pi" / "mcp.json"
    _write(_global(tmp_path), {"mcpServers": {"srv": {"command": "global"}}})
    _write(tmp_path / ".pi" / "agent" / "trust.json", {str(workspace): True})
    _write(project, {"mcpServers": {"srv": {"url": "https://project.example/mcp", **fields}}})

    result = _discover(tmp_path, workspace)

    (candidate,) = result.candidates
    assert candidate.source_path == str(project)
    assert result.source_errors == ()
    assert candidate.config is not None
    assert (candidate.config.transport, candidate.config.url) == ("http", url)
