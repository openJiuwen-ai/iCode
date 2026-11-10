# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""opencode: where servers are found and how each entry converts."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind
from chrys.service.mcp.importers import WarningKind as Kind
from chrys.service.mcp.importers.base import SourceReadError
from chrys.service.mcp.importers.opencode import OpenCodeSource, strip_jsonc

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.mcp.importers import McpImportCandidate


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document if isinstance(document, str) else json.dumps(document), encoding="utf-8")


def _discover(home: Path, workspace: Path | None = None, environ: dict[str, str] | None = None):
    return OpenCodeSource().discover(ImportContext(str(workspace) if workspace else None, home, environ or {}))


def _global(home: Path) -> Path:
    return home / ".config" / "opencode" / "opencode.json"


def _only(home: Path, entry: dict[str, Any], **document: Any) -> McpImportCandidate:
    _write(_global(home), {"mcp": {"srv": entry}, **document})
    (candidate,) = _discover(home).candidates
    return candidate


def test_jsonc_comments_and_trailing_commas_are_read_but_strings_are_kept() -> None:
    text = '{\n  // note\n  "a": "http://x // y", /* block\n */ "b": [1, 2,],\n}'

    assert json.loads(strip_jsonc(text)) == {"a": "http://x // y", "b": [1, 2]}
    assert strip_jsonc(text).count("\n") == text.count("\n")


@pytest.mark.parametrize("text", ['{"a": 1} /* note', "{} /*/"], ids=["unclosed", "slash-star-slash"])
def test_an_unclosed_block_comment_is_unparsable(text: str) -> None:
    with pytest.raises(SourceReadError) as caught:
        strip_jsonc(text)

    assert caught.value.kind is SourceErrorKind.UNPARSABLE


def test_unclosed_strings_and_comments_are_scanned_once() -> None:
    # Retrying from each quote or "/*" to the end would take minutes on these.
    unclosed_string = '"' + '\\"' * 100_000
    assert strip_jsonc(unclosed_string) == unclosed_string
    with pytest.raises(SourceReadError):
        strip_jsonc("/*" * 100_000)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"type": "local", "command": ["npx", "-y", "pkg"], "environment": {"T": "{env:TOKEN}"}, "timeout": 1500},
            {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "pkg"],
                "env": {"T": "{{TOKEN}}"},
                "request_timeout": 2,
            },
        ),
        (
            {"type": "remote", "url": "https://x", "headers": {"Authorization": "Bearer {env:KEY}"}, "enabled": False},
            {
                "transport": "http",
                "headers": {"Authorization": "Bearer {{KEY}}"},
                "enabled": False,
                "request_timeout": 60,
            },
        ),
        ({"type": "remote", "url": "https://x", "oauth": False}, {"url": "https://x"}),
    ],
    ids=["local", "remote", "oauth-off"],
)
def test_entries_convert_to_icode_servers(tmp_path: Path, entry: dict[str, Any], expected: dict[str, Any]) -> None:
    candidate = _only(tmp_path, entry)

    assert candidate.config is not None
    assert [w for w in candidate.warnings if w.kind is not Kind.VARIABLE_REQUIRED] == []
    for key, value in expected.items():
        assert getattr(candidate.config, key) == value


def test_the_experimental_timeout_applies_to_servers_without_their_own(tmp_path: Path) -> None:
    candidate = _only(tmp_path, {"type": "local", "command": ["run"]}, experimental={"mcp_timeout": 90_000})

    assert candidate.config is not None
    assert candidate.config.request_timeout == 90


@pytest.mark.parametrize(
    ("entry", "warnings"),
    [
        ({"type": "remote", "url": "https://x", "oauth": {}}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),)),
        # opencode signs in with OAuth whenever the server asks, unless told not to.
        ({"type": "remote", "url": "https://x"}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),)),
        ({"type": "remote", "url": "https://x", "oauth": False}, ()),
        (
            {"type": "local", "command": ["run"], "cwd": "/x"},
            (ImportWarning(Kind.NOT_IMPORTED, ("mcp.srv.cwd",)),),
        ),
        (
            {"type": "local", "command": ["run"], "environment": {"A": "{env:T}", "B": "{env:U}-{env:T}"}},
            (ImportWarning(Kind.VARIABLE_REQUIRED, ("T", "U")),),
        ),
        (
            {"type": "remote", "url": "https://x", "headers": {"Authorization": "Bearer {env:KEY}"}},
            (ImportWarning(Kind.VARIABLE_REQUIRED, ("KEY",)),),
        ),
    ],
    ids=["oauth", "oauth-by-default", "oauth-off", "unread-setting", "environment-variables", "header-variable"],
)
def test_what_the_import_leaves_out_is_reported(
    tmp_path: Path, entry: dict[str, Any], warnings: tuple[ImportWarning, ...]
) -> None:
    assert _only(tmp_path, entry).warnings == warnings


@pytest.mark.parametrize(
    ("entry", "problem", "field"),
    [
        ({"type": "remote", "url": "https://{env:HOST}/mcp"}, Problem.VARIABLE_NOT_SUPPORTED, "mcp.srv.url"),
        ({"type": "local", "command": ["run", "{env:DIR}"]}, Problem.VARIABLE_NOT_SUPPORTED, "mcp.srv.command[1]"),
        ({"type": "local", "command": ["run", "{file:~/a}"]}, Problem.VALUE_FROM_FILE_OR_COMMAND, "mcp.srv.command[1]"),
        (
            {"type": "local", "command": ["run"], "environment": {"T": "{file:~/.token}"}},
            Problem.VALUE_FROM_FILE_OR_COMMAND,
            "mcp.srv.environment.T",
        ),
        (
            {"type": "remote", "url": "https://x", "headers": {"X": "{env:not-a-name}"}},
            Problem.VALUE_NOT_REPRESENTABLE,
            "mcp.srv.headers.X",
        ),
    ],
    ids=["url-variable", "command-variable", "command-file", "file-reference", "bad-variable-name"],
)
def test_values_icode_cannot_hold_are_listed_as_unsupported(
    tmp_path: Path, entry: dict[str, Any], problem: Problem, field: str
) -> None:
    candidate = _only(tmp_path, entry)

    assert (candidate.config, candidate.problem, candidate.problem_field) == (None, problem, field)


def test_a_variable_in_the_server_name_is_listed_as_unsupported(tmp_path: Path) -> None:
    _write(_global(tmp_path), {"mcp": {"{env:NAME}": {"type": "local", "command": ["run"]}}})

    (candidate,) = _discover(tmp_path).candidates

    assert (candidate.problem, candidate.problem_field) == (Problem.VARIABLE_NOT_SUPPORTED, "mcp.{env:NAME}")


@pytest.mark.parametrize(
    "entry",
    [
        {"type": "local", "command": []},
        {"type": "local", "command": "run"},
        {"type": "remote"},
        {"type": "sse", "url": "https://x"},
        {"type": "local", "command": ["run"], "timeout": -1},
    ],
    ids=["empty-command", "command-not-a-list", "no-url", "unknown-type", "bad-timeout"],
)
def test_entries_opencode_rejects_are_not_listed(tmp_path: Path, entry: dict[str, Any]) -> None:
    _write(_global(tmp_path), {"mcp": {"srv": entry}})

    assert _discover(tmp_path).candidates == ()


def test_later_files_win_for_same_named_servers(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    workspace = repo / "pkg"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir()
    local = {"type": "local"}
    _write(_global(home), {"mcp": {"a": {**local, "command": ["global"]}, "b": {**local, "command": ["global"]}}})
    _write(repo / "opencode.jsonc", '{"mcp": {"a": {"type": "local", "command": ["root"]}, }}  // project')
    _write(workspace / ".opencode" / "opencode.json", {"mcp": {"b": {**local, "command": ["dot-opencode"]}}})

    commands = {c.name: c.config.command for c in _discover(home, workspace).candidates if c.config}

    assert commands == {"a": "root", "b": "dot-opencode"}


def test_every_folder_from_the_git_root_down_to_the_workspace_is_read(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    workspace = repo / "a" / "b"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir(parents=True)
    remote = {"type": "remote", "headers": {"X": "1"}}
    _write(_global(home), {"mcp": {"api": {**remote, "url": "https://prod"}, "b": {**remote, "url": "https://g"}}})
    _write(tmp_path / "opencode.json", {"mcp": {"above": {**remote, "url": "https://above"}}})
    _write(repo / "a" / "opencode.json", {"mcp": {"api": {"url": "https://dev"}}})
    # .opencode folders are read nearest first, so the one higher up wins, as in opencode.
    _write(repo / ".opencode" / "opencode.json", {"mcp": {"b": {"url": "https://root-folder"}}})
    _write(workspace / ".opencode" / "opencode.json", {"mcp": {"b": {"url": "https://workspace-folder"}}})

    urls = {c.name: c.config.url for c in _discover(home, workspace).candidates if c.config}

    assert urls == {"api": "https://dev", "b": "https://root-folder"}


def test_the_home_folder_and_the_environment_add_files(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    local = {"type": "local"}
    _write(workspace / "opencode.json", {"mcp": {"project": {**local, "command": ["project"]}}})
    _write(home / ".opencode" / "opencode.json", {"mcp": {"home": {**local, "command": ["home"]}}})
    _write(tmp_path / "custom.json", {"mcp": {"custom": {**local, "command": ["custom"]}}})
    _write(tmp_path / "dir" / "opencode.jsonc", {"mcp": {"dir": {**local, "command": ["dir"]}}})
    environ = {"OPENCODE_CONFIG": str(tmp_path / "custom.json"), "OPENCODE_CONFIG_DIR": str(tmp_path / "dir")}

    assert [c.name for c in _discover(home, workspace, environ).candidates] == ["custom", "project", "home", "dir"]
    environ["OPENCODE_DISABLE_PROJECT_CONFIG"] = "true"
    assert [c.name for c in _discover(home, workspace, environ).candidates] == ["custom", "home", "dir"]


@pytest.mark.parametrize(
    ("suffix", "command"), [("", "project"), (os.sep, "global")], ids=["same", "trailing-separator"]
)
def test_the_config_dir_variable_spelling_the_global_folder_reads_it_first(
    tmp_path: Path, suffix: str, command: str
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    _write(_global(home), {"mcp": {"srv": {"type": "local", "command": ["global"]}}})
    _write(workspace / ".opencode" / "opencode.json", {"mcp": {"srv": {"type": "local", "command": ["project"]}}})
    # opencode compares the folders as text: spelled any other way, the global folder is read again, last.
    environ = {"OPENCODE_CONFIG_DIR": str(_global(home).parent) + suffix}

    (candidate,) = _discover(home, workspace, environ).candidates

    assert candidate.config is not None
    assert candidate.config.command == command


def test_later_files_merge_into_same_named_servers(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    full = {"type": "local", "command": ["global", "-x"], "environment": {"A": "1", "B": "1"}, "enabled": False}
    _write(_global(home), {"mcp": {"srv": full, "off": {"type": "local", "command": ["run"]}}})
    project = {
        "srv": {"type": "local", "command": ["project"], "environment": {"B": "2"}},
        "off": {"enabled": False},
        "unknown": {"enabled": False},
    }
    _write(workspace / "opencode.json", {"mcp": project})

    candidates = {c.name: c for c in _discover(home, workspace).candidates}

    # Objects merge key by key; a list or a value replaces the earlier one.
    srv, off = candidates["srv"].config, candidates["off"].config
    assert srv is not None and off is not None
    assert (srv.command, srv.args, srv.env, srv.enabled) == ("project", [], {"A": "1", "B": "2"}, False)
    assert candidates["srv"].source_path == str(workspace / "opencode.json")
    assert not off.enabled
    assert candidates["off"].source_path == str(_global(home))
    assert "unknown" not in candidates


def test_the_last_experimental_timeout_applies_to_servers_from_every_file(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(home / ".config" / "opencode" / "config.json", {"experimental": {"mcp_timeout": 10_000}})
    _write(_global(home), {"experimental": {"mcp_timeout": 90_000}})
    _write(workspace / "opencode.json", {"mcp": {"srv": {"type": "local", "command": ["run"]}}})

    (candidate,) = _discover(home, workspace).candidates

    assert candidate.config is not None
    assert candidate.config.request_timeout == 90


def test_a_broken_file_is_reported_and_the_others_still_listed(tmp_path: Path) -> None:
    _write(tmp_path / ".config" / "opencode" / "config.json", '{"mcp": ')
    _write(_global(tmp_path), {"mcp": {"srv": {"type": "local", "command": ["run"]}}, "permission": {"edit": "deny"}})
    _write(tmp_path / ".config" / "opencode" / "opencode.jsonc", {"mcp": []})

    result = _discover(tmp_path)

    assert [c.name for c in result.candidates] == ["srv"]
    assert result.candidates[0].warnings == ()
    assert [(e.kind, e.field) for e in result.source_errors] == [
        (SourceErrorKind.UNPARSABLE, ""),
        (SourceErrorKind.INVALID, "mcp"),
    ]
