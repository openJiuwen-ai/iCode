# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex: where servers are found and how each entry converts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind
from chrys.service.mcp.importers import WarningKind as Kind
from chrys.service.mcp.importers.base import MAX_REQUEST_TIMEOUT_SECONDS
from chrys.service.mcp.importers.codex import CodexSource

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.mcp.importers import McpImportCandidate


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _discover(home: Path, workspace: Path | None = None, environ: dict[str, str] | None = None):
    return CodexSource().discover(ImportContext(str(workspace) if workspace else None, home, environ or {}))


def _only(home: Path, body: str) -> McpImportCandidate:
    _write(home / ".codex" / "config.toml", "[mcp_servers.srv]\n" + body)
    (candidate,) = _discover(home).candidates
    return candidate


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            'command = "npx"\nargs = ["-y", "pkg"]\nenv = { LITERAL = "{{x}}" }\ntool_timeout_sec = 12.5\n',
            {"transport": "stdio", "args": ["-y", "pkg"], "env": {"LITERAL": "{{{{x}}}}"}, "request_timeout": 13},
        ),
        (
            'url = "https://x/mcp"\nbearer_token_env_var = "API_KEY"\nhttp_headers = { authorization = "x", X = "1" }\n',
            {"transport": "http", "headers": {"X": "1", "Authorization": "Bearer {{API_KEY}}"}, "request_timeout": 300},
        ),
        ('command = "run"\nenabled = false\n', {"enabled": False}),
        (
            'command = "run"\nenabled_tools = ["a", "b"]\ndisabled_tools = ["b"]\n',
            {"allowed_tools": ["a"]},
        ),
        ('url = "https://x"\nhttp_headers = { "bad name" = "1", X = "2" }\n', {"headers": {"X": "2"}}),
        (f'command = "run"\ntool_timeout_sec = 1{"0" * 400}\n', {"request_timeout": MAX_REQUEST_TIMEOUT_SECONDS}),
    ],
    ids=[
        "stdio",
        "bearer-beats-authorization",
        "disabled",
        "enabled-minus-disabled",
        "invalid-header-name",
        "huge-timeout",
    ],
)
def test_entries_convert_to_icode_servers(tmp_path: Path, body: str, expected: dict[str, Any]) -> None:
    candidate = _only(tmp_path, body)

    assert candidate.config is not None
    assert candidate.warnings == ()
    for key, value in expected.items():
        assert getattr(candidate.config, key) == value


@pytest.mark.parametrize(
    ("body", "warnings", "expected"),
    [
        (
            'url = "https://x"\nenv_http_headers = { "X-Token" = "TOKEN" }\nhttp_headers = { "x-token" = "lit" }\n',
            (ImportWarning(Kind.VARIABLE_REQUIRED, ("TOKEN",)),),
            {"headers": {"X-Token": "{{TOKEN}}"}},
        ),
        ('url = "https://x"\nauth = "chatgpt"\n', (ImportWarning(Kind.AUTH_NOT_IMPORTED),), {}),
        ('command = "run"\ndisabled_tools = ["rm"]\n', (ImportWarning(Kind.TOOLS_RESTRICTED),), {"allowed_tools": []}),
        (
            'command = "run"\ntool_timeout_sec = 0\n',
            (ImportWarning(Kind.NOT_IMPORTED, ("mcp_servers.srv.tool_timeout_sec",)),),
            {"request_timeout": None},
        ),
        (
            'command = "run"\ncwd = "/x"\nstartup_timeout_sec = 5\ndefault_tools_approval_mode = "approve"\n',
            (
                ImportWarning(
                    Kind.NOT_IMPORTED,
                    (
                        "mcp_servers.srv.cwd",
                        "mcp_servers.srv.startup_timeout_sec",
                        "mcp_servers.srv.default_tools_approval_mode",
                    ),
                ),
            ),
            {},
        ),
    ],
    ids=["optional-variable", "provider-auth", "deny-only", "zero-timeout", "unread-settings"],
)
def test_what_the_import_changes_is_reported(
    tmp_path: Path, body: str, warnings: tuple[ImportWarning, ...], expected: dict[str, Any]
) -> None:
    candidate = _only(tmp_path, body)

    assert candidate.config is not None
    assert candidate.warnings == warnings
    for key, value in expected.items():
        assert getattr(candidate.config, key) == value


@pytest.mark.parametrize(
    ("body", "problem", "field"),
    [
        ('command = "run"\nenvironment_id = "cloud"\n', Problem.REMOTE_ENVIRONMENT, "mcp_servers.srv.environment_id"),
        (
            'command = "run"\nenv_vars = ["B", { name = "A", source = "remote" }]\n',
            Problem.REMOTE_ENVIRONMENT,
            "mcp_servers.srv.env_vars[1]",
        ),
        (
            'url = "https://x"\nhttp_headers = { A = "1", a = "2" }\n',
            Problem.VALUE_NOT_REPRESENTABLE,
            "mcp_servers.srv.http_headers.a",
        ),
        (
            'url = "https://x"\nenv_http_headers = { A = "not-a-name" }\n',
            Problem.VALUE_NOT_REPRESENTABLE,
            "mcp_servers.srv.env_http_headers.A",
        ),
        ('command = "run"\nenv = { A = " " }\n', Problem.VALUE_NOT_REPRESENTABLE, "mcp_servers.srv.env.A"),
    ],
    ids=["remote-environment", "remote-variable", "header-case-conflict", "bad-variable-name", "blank-env"],
)
def test_entries_icode_cannot_hold_are_listed_as_unsupported(
    tmp_path: Path, body: str, problem: Problem, field: str
) -> None:
    candidate = _only(tmp_path, body)

    assert (candidate.config, candidate.problem, candidate.problem_field) == (None, problem, field)


@pytest.mark.parametrize(
    "body",
    ['command = "run"\nurl = "https://x"\n', 'args = ["x"]\n', "command = 1\n", 'command = "run"\nenabled = "yes"\n'],
    ids=["both-transports", "no-transport", "command-not-a-string", "enabled-not-a-bool"],
)
def test_entries_codex_rejects_are_not_listed(tmp_path: Path, body: str) -> None:
    _write(tmp_path / ".codex" / "config.toml", "[mcp_servers.srv]\n" + body)

    assert _discover(tmp_path).candidates == ()


def test_project_files_merge_into_same_named_user_servers(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    workspace = repo / "pkg"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir()
    user = (
        '[mcp_servers.a]\ncommand = "user"\nargs = ["x", "y"]\nenv = { A = "1", B = "1" }\n'
        '[mcp_servers.off]\ncommand = "user"\n'
        '[mcp_servers.web]\nurl = "https://user"\nhttp_headers = { X = "1" }\n'
    )
    _write(home / ".codex" / "config.toml", user)
    root = '[mcp_servers.a]\ncommand = "root"\nargs = ["z"]\nenv = { B = "2" }\n[mcp_servers.off]\nenabled = false\n'
    _write(repo / ".codex" / "config.toml", root)
    _write(workspace / ".codex" / "config.toml", '[mcp_servers.web]\nurl = "https://workspace"\n')

    candidates = {c.name: c for c in _discover(home, workspace).candidates}

    a, off, web = (candidates[name].config for name in ("a", "off", "web"))
    assert a is not None and off is not None and web is not None
    # Tables merge key by key; a list or a value replaces the earlier one.
    assert (a.command, a.args, a.env) == ("root", ["z"], {"A": "1", "B": "2"})
    assert off.enabled is False
    assert (web.url, web.headers) == ("https://workspace", {"X": "1"})
    # The file shown is the last one that sets the connection.
    assert candidates["off"].source_path == str(home / ".codex" / "config.toml")
    assert candidates["web"].source_path == str(workspace / ".codex" / "config.toml")


def test_codex_home_moves_the_user_file(tmp_path: Path) -> None:
    _write(tmp_path / "custom" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    result = _discover(tmp_path, environ={"CODEX_HOME": str(tmp_path / "custom")})

    assert [c.name for c in result.candidates] == ["srv"]


@pytest.mark.parametrize(
    ("text", "kind", "field"),
    [
        ("[mcp_servers\n", SourceErrorKind.UNPARSABLE, ""),
        ('mcp_servers = "x"\n', SourceErrorKind.INVALID, "mcp_servers"),
    ],
    ids=["bad-toml", "servers-not-a-table"],
)
def test_broken_files_are_reported(tmp_path: Path, text: str, kind: SourceErrorKind, field: str) -> None:
    _write(tmp_path / ".codex" / "config.toml", text)

    (error,) = _discover(tmp_path).source_errors

    assert (error.kind, error.field) == (kind, field)
    assert kind is not SourceErrorKind.UNPARSABLE or error.line == 1


def test_a_file_nested_too_deep_to_parse_is_reported_and_the_others_still_listed(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    _write(tmp_path / ".codex" / "config.toml", "a = " + "[" * 5000 + "]" * 5000 + "\n")
    _write(workspace / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    result = _discover(tmp_path, workspace)

    assert [c.name for c in result.candidates] == ["srv"]
    assert [e.kind for e in result.source_errors] == [SourceErrorKind.UNPARSABLE]
