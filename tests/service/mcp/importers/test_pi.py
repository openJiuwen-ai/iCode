# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""pi: where servers are found and how each entry converts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind
from chrys.service.mcp.importers import WarningKind as Kind
from chrys.service.mcp.importers.pi import PiSource

if TYPE_CHECKING:
    from pathlib import Path

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
        if isinstance(value, list):
            wanted = [item.replace("{home}", str(tmp_path)) for item in value]
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
            (ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.toolExposure.big_*",)),),
            None,
        ),
        ({"url": "https://x", "oauth": {}}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),), None),
        ({"url": "https://x", "auth": {"provider": "github"}}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),), None),
        ({"command": "run", "cwd": "/srv"}, (ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.cwd",)),), None),
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
        {"mcpServers": {"a": {"command": "global"}, "b": {"command": "global", "env": {"K": "v"}}}},
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
    assert (b.command, b.env) == ("project", {})


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
