# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude Code: where servers are found and how each entry converts."""

from __future__ import annotations

import json
import ntpath
import os
import sys
import unicodedata
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, BinaryIO

import pytest

from chrys.foundation.platform.files import SecureFileError
from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind, base, claude_code
from chrys.service.mcp.importers import WarningKind as Kind
from chrys.service.mcp.importers.claude_code import ClaudeCodeSource
from tests.support.symlinks import symlink_or_skip

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.service.mcp.importers import McpImportCandidate


def _write(path: Path, document: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document) if not isinstance(document, str) else document, encoding="utf-8")


def _discover(home: Path, workspace: Path | None = None, environ: dict[str, str] | None = None):
    ctx = ImportContext(str(workspace) if workspace else None, home, environ or {})
    return ClaudeCodeSource().discover(ctx)


def _only(home: Path, entry: dict[str, Any]) -> McpImportCandidate:
    _write(home / ".claude.json", {"mcpServers": {"srv": entry}})
    (candidate,) = _discover(home).candidates
    return candidate


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (
            {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "${TOKEN}"}, "timeout": 1500},
            {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "pkg"],
                "env": {"TOKEN": "{{TOKEN}}"},
                "request_timeout": 2,
            },
        ),
        (
            {"type": "streamable-http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${API_KEY}"}},
            {"transport": "http", "url": "https://x/mcp", "headers": {"Authorization": "Bearer {{API_KEY}}"}},
        ),
        ({"command": "run", "timeout": 500}, {"request_timeout": None}),
        ({"command": "run", "env": {"LITERAL": "{{x}}"}}, {"env": {"LITERAL": "{{{{x}}}}"}}),
    ],
    ids=["stdio", "http", "sub-second-timeout", "literal-braces"],
)
def test_entries_convert_to_icode_servers(tmp_path: Path, entry: dict[str, Any], expected: dict[str, Any]) -> None:
    candidate = _only(tmp_path, entry)

    assert candidate.config is not None
    assert candidate.warnings == ()
    for key, value in expected.items():
        assert getattr(candidate.config, key) == value


@pytest.mark.parametrize(
    ("entry", "warnings"),
    [
        (
            {"command": "run", "env": {"A": "${A:-1}", "DIR": "${CLAUDE_PROJECT_DIR}/x"}},
            (ImportWarning(Kind.VARIABLE_DEFAULTS_DROPPED, ("mcpServers.srv.env.A",)),),
        ),
        (
            {"type": "http", "url": "https://x", "oauth": {"clientId": "c"}, "headersHelper": "get-headers"},
            (
                ImportWarning(Kind.AUTH_NOT_IMPORTED),
                ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.headersHelper",)),
            ),
        ),
        (
            {"type": "http", "url": "https://x", "headers": {"Authorization": "Bearer ${anthropic_api_key}", "X": "1"}},
            (ImportWarning(Kind.AUTH_HEADERS, ("mcpServers.srv.headers.Authorization",)),),
        ),
        (
            {"command": "run", "alwaysLoad": True, "cwd": "/x"},
            (ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.cwd",)),),
        ),
        ({"type": "http", "url": "https://x"}, ()),
    ],
    ids=["variable-default", "auth-and-helper", "credential-header", "unknown-field", "no-headers"],
)
def test_what_the_import_leaves_out_is_reported(
    tmp_path: Path, entry: dict[str, Any], warnings: tuple[ImportWarning, ...]
) -> None:
    candidate = _only(tmp_path, entry)

    assert candidate.config is not None
    assert candidate.warnings == warnings


@pytest.mark.parametrize(
    ("variable", "imported"),
    [
        ("NPM_TOKEN", False),
        ("CLAUDE_CODE_OAUTH_TOKEN", False),
        ("MCP_CLIENT_SECRET", False),
        ("INPUT_twine_password", False),
        ("OTEL_EXPORTER_OTLP_HEADERS", False),
        ("CARGO_REGISTRIES_MINE_TOKEN", False),
        ("GITHUB_TOKEN", True),
    ],
)
def test_a_header_referencing_a_credential_variable_is_not_imported(
    tmp_path: Path, variable: str, imported: bool
) -> None:
    headers = {"Authorization": f"Bearer ${{{variable}}}", "X": "1"}
    candidate = _only(tmp_path, {"type": "http", "url": "https://x", "headers": headers})

    assert candidate.config is not None
    expected = {"Authorization": f"Bearer {{{{{variable}}}}}", "X": "1"} if imported else {"X": "1"}
    assert candidate.config.headers == expected


@pytest.mark.parametrize(
    ("entry", "problem", "field"),
    [
        ({"type": "sse", "url": "https://x"}, Problem.UNSUPPORTED_CONNECTION, ""),
        ({"type": "ws", "url": "wss://x"}, Problem.UNSUPPORTED_CONNECTION, ""),
        ({"type": "http", "url": "https://${HOST}/mcp"}, Problem.VARIABLE_NOT_SUPPORTED, "mcpServers.srv.url"),
        ({"command": "run", "args": ["--dir", "${HOME}"]}, Problem.VARIABLE_NOT_SUPPORTED, "mcpServers.srv.args[1]"),
        ({"command": "run", "env": {"A": ""}}, Problem.VALUE_NOT_REPRESENTABLE, "mcpServers.srv.env.A"),
    ],
    ids=["sse", "ws", "url-variable", "args-variable", "empty-env"],
)
def test_entries_icode_cannot_hold_are_listed_as_unsupported(
    tmp_path: Path, entry: dict[str, Any], problem: Problem, field: str
) -> None:
    candidate = _only(tmp_path, entry)

    assert (candidate.config, candidate.problem, candidate.problem_field) == (None, problem, field)


@pytest.mark.parametrize(
    "entry",
    [{"url": "https://x"}, {"type": "sdk"}, {"command": ""}, {"command": "run", "args": "x"}, "not an object"],
    ids=["url-without-type", "sdk", "empty-command", "args-not-a-list", "not-an-object"],
)
def test_entries_claude_code_skips_are_not_listed(tmp_path: Path, entry: object) -> None:
    _write(tmp_path / ".claude.json", {"mcpServers": {"srv": entry}})

    assert _discover(tmp_path).candidates == ()


def test_local_beats_project_beats_user_for_the_same_name(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    workspace = repo / "pkg"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir()
    _write(
        home / ".claude.json",
        {
            "mcpServers": {"shared": {"command": "user"}, "mine": {"command": "user"}},
            "projects": {repo.as_posix(): {"mcpServers": {"shared": {"command": "local"}}}},
        },
    )
    _write(repo / ".mcp.json", {"mcpServers": {"shared": {"command": "root"}, "team": {"command": "root"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {"team": {"command": "workspace"}}})

    candidates = {c.name: c for c in _discover(home, workspace).candidates}

    assert {name: c.config.command for name, c in candidates.items() if c.config} == {
        "mine": "user",
        "team": "workspace",
        "shared": "local",
    }
    assert candidates["team"].source_path == str(workspace / ".mcp.json")


def test_project_servers_are_read_from_ancestor_folders_nearest_first_for_precedence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    module = repo / "module"
    workspace = module / "pkg"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir(parents=True)
    _write(tmp_path / ".mcp.json", {"mcpServers": {"outside": {"command": "outer"}}})
    _write(repo / ".mcp.json", {"mcpServers": {"docs": {"command": "repo"}}})
    _write(module / ".mcp.json", {"mcpServers": {"docs": {"command": "module"}}})

    candidates = {c.name: c for c in _discover(tmp_path / "home", workspace).candidates}

    assert {name: c.config.command for name, c in candidates.items() if c.config} == {
        "docs": "module",
        "outside": "outer",
    }
    assert candidates["docs"].source_path == str(module / ".mcp.json")


def test_servers_turned_off_in_claude_code_are_imported_turned_off(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    workspace = repo / "pkg"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir()
    run = {"command": "run"}
    _write(
        home / ".claude.json",
        {
            "mcpServers": {"user-off": run, "user-on": run},
            "projects": {
                repo.as_posix(): {
                    "mcpServers": {"local-off": run},
                    # /mcp turns off any server; approvals cover only .mcp.json servers.
                    "disabledMcpServers": ["user-off", "local-off", "team-toggled-off"],
                    "disabledMcpjsonServers": ["rejected-in-prompt", "user-on"],
                }
            },
        },
    )
    team = [
        "rejected-in-prompt",
        "rejected-by-user",
        "rejected-by-team",
        "rejected-by-me",
        "team-toggled-off",
        "team-on",
    ]
    _write(repo / ".mcp.json", {"mcpServers": dict.fromkeys(team, run)})
    _write(
        home / ".claude" / "settings.json",
        {"disabledMcpjsonServers": ["rejected-by-user"], "enableAllProjectMcpServers": True},
    )
    _write(workspace / ".claude" / "settings.json", {"disabledMcpjsonServers": ["rejected-by-team"]})
    _write(repo / ".claude" / "settings.local.json", {"disabledMcpjsonServers": ["rejected-by-me"]})

    enabled = {c.name: c.config.enabled for c in _discover(home, workspace).candidates if c.config}

    assert enabled == {
        "user-off": False,
        "user-on": True,
        "local-off": False,
        "rejected-in-prompt": False,
        "rejected-by-user": False,
        "rejected-by-team": False,
        "rejected-by-me": False,
        "team-toggled-off": False,
        "team-on": True,
    }


def test_mcp_tool_denials_restrict_only_matching_servers(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    _write(
        home / ".claude.json",
        {"mcpServers": {name: {"command": "run"} for name in ("git.tools", "git_tools_backup", "data", "docs")}},
    )
    _write(
        home / ".claude" / "settings.json",
        {"permissions": {"deny": ["mcp__git_tools__*", "Bash"], "ask": ["mcp__git_tools_backup"]}},
    )
    _write(workspace / ".claude" / "settings.json", {"permissions": {"deny": ["mcp__data__delete"]}})
    _write(workspace / ".claude" / "settings.local.json", {"permissions": {"deny": ["mcp__docs"]}})

    result = _discover(home, workspace)
    assert result.source_errors == ()
    assert {c.name: c.config.allowed_tools for c in result.candidates if c.config} == {
        "git.tools": [],
        "git_tools_backup": None,
        "data": [],
        "docs": [],
    }
    warning = ImportWarning(Kind.TOOLS_RESTRICTED, ("permissions.deny",))
    assert {c.name: c.warnings for c in result.candidates} == {
        "git.tools": (warning,),
        "git_tools_backup": (),
        "data": (warning,),
        "docs": (warning,),
    }
    assert all(c.config is not None and c.config.enabled for c in result.candidates)


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_a_repository_cannot_approve_its_own_servers_until_its_folder_is_trusted(tmp_path: Path, trusted: bool) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    # Older Claude Code kept approvals in the project's entry, now moved to the trust-gated local settings.
    entry = {"enabledMcpjsonServers": ["approved-in-entry"], "hasTrustDialogAccepted": trusted}
    _write(home / ".claude.json", {"projects": {repo.as_posix(): entry}})
    names = ["approved-by-user", "approved-by-repo", "approved-locally", "approved-in-entry", "pending"]
    _write(repo / ".mcp.json", {"mcpServers": {name: {"command": "run"} for name in names}})
    _write(home / ".claude" / "settings.json", {"enabledMcpjsonServers": ["approved-by-user"]})
    _write(repo / ".claude" / "settings.json", {"enabledMcpjsonServers": ["approved-by-repo"]})
    _write(repo / ".claude" / "settings.local.json", {"enabledMcpjsonServers": ["approved-locally"]})

    enabled = {c.name: c.config.enabled for c in _discover(home, repo).candidates if c.config}

    assert enabled == {
        "approved-by-user": True,
        "approved-by-repo": trusted,
        "approved-locally": trusted,
        "approved-in-entry": trusted,
        "pending": False,
    }


def test_trusting_a_folder_above_the_project_does_not_let_the_repository_approve_servers(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repos" / "repo"
    (repo / ".git").mkdir(parents=True)
    # Claude Code reads approvals from a repository's own settings only when the project itself is trusted.
    _write(home / ".claude.json", {"projects": {(tmp_path / "repos").as_posix(): {"hasTrustDialogAccepted": True}}})
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(repo / ".claude" / "settings.json", {"enabledMcpjsonServers": ["srv"]})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


def test_approvals_and_rejections_match_names_as_claude_code_compares_them(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    names = ["my.api", "your api", "their/api", "plugin:x", "plugin_y", "a😀b", "my😀api"]
    _write(repo / ".mcp.json", {"mcpServers": {name: {"command": "run"} for name in names}})
    _write(
        home / ".claude" / "settings.json",
        {
            "enabledMcpjsonServers": ["my_api", "your_api", "their_api", "plugin_x", "plugin:y", "a__b", "my_api"],
            "disabledMcpjsonServers": ["your-api", "their_api"],
        },
    )

    enabled = {c.name: c.config.enabled for c in _discover(home, repo).candidates if c.config}

    # Other characters compare as "_", one per UTF-16 unit (two for an emoji); a plugin's name only as written.
    assert enabled == {
        "my.api": True,
        "your api": True,
        "their/api": False,
        "plugin:x": False,
        "plugin_y": False,
        "a😀b": True,
        "my😀api": False,
    }


def test_trust_kept_under_another_path_to_the_project_does_not_count(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    symlink_or_skip(tmp_path / "alias", repo, target_is_directory=True)
    # Claude Code looks the project up under its real path only.
    _write(home / ".claude.json", {"projects": {(tmp_path / "alias").as_posix(): {"hasTrustDialogAccepted": True}}})
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(repo / ".claude" / "settings.json", {"enabledMcpjsonServers": ["srv"]})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


@pytest.mark.parametrize("spelling", ["exact", "other-unicode-form"])
def test_trust_kept_under_another_unicode_spelling_does_not_count(tmp_path: Path, spelling: str) -> None:
    home, repo = tmp_path / "home", tmp_path / "caf\u00e9"
    (repo / ".git").mkdir(parents=True)
    # Where the filesystem tells the spellings apart, the other one is another folder.
    key = repo.as_posix() if spelling == "exact" else unicodedata.normalize("NFD", repo.as_posix())
    _write(home / ".claude.json", {"projects": {key: {"hasTrustDialogAccepted": True}}})
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(repo / ".claude" / "settings.json", {"enabledMcpjsonServers": ["srv"]})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is (spelling == "exact")


def test_a_decomposed_folder_name_is_looked_up_in_the_nfc_form_claude_code_keys_it_by(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / unicodedata.normalize("NFD", "caf\u00e9")
    (repo / ".git").mkdir(parents=True)
    # Claude Code NFC-normalizes the folder it starts in, on every platform, before keying the project.
    key = unicodedata.normalize("NFC", repo.resolve().as_posix())
    _write(
        home / ".claude.json",
        {"mcpServers": {"user": {"command": "run"}}, "projects": {key: {"disabledMcpServers": ["user"]}}},
    )

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


@pytest.mark.parametrize("drive", ["C", "c"], ids=["same-drive-case", "other-drive-case"])
def test_on_windows_the_key_spelled_as_claude_code_writes_it_wins_over_another_case(
    monkeypatch: pytest.MonkeyPatch, drive: str
) -> None:
    monkeypatch.setattr(claude_code, "get_platform", lambda: SimpleNamespace(is_windows=True))
    # The folder was renamed from ``Repo`` to ``repo``; Claude Code kept the old key, ahead of the new one,
    # with the drive letter as Claude Code was started.
    old, current = f"{drive}:/Work/Repo", f"{drive}:/Work/repo"
    config = {"projects": {old: {"hasTrustDialogAccepted": True}, current: {"disabledMcpServers": ["a"]}}}

    assert claude_code._entry(config, "C:\\Work\\repo") == {"disabledMcpServers": ["a"]}
    del config["projects"][current]
    with monkeypatch.context() as windows:
        windows.setattr(os.path, "normcase", ntpath.normcase)
        assert claude_code._entry(config, "C:\\Work\\repo") is None


def test_an_untrusted_repository_cannot_approve_all_its_servers(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(repo / ".claude" / "settings.json", {"enableAllProjectMcpServers": True})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_local_false_overrides_blanket_approval_in_a_trusted_project(tmp_path: Path, trusted: bool) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(home / ".claude.json", {"projects": {repo.as_posix(): {"hasTrustDialogAccepted": trusted}}})
    _write(home / ".claude" / "settings.json", {"enableAllProjectMcpServers": True, "enabledMcpjsonServers": ["named"]})
    _write(repo / ".claude" / "settings.local.json", {"enableAllProjectMcpServers": False})
    _write(repo / ".mcp.json", {"mcpServers": {"docs": {"command": "run"}, "named": {"command": "run"}}})

    enabled = {c.name: c.config.enabled for c in _discover(home, repo).candidates if c.config}

    assert enabled == {"docs": not trusted, "named": True}


@pytest.mark.parametrize(
    ("legacy", "local", "trusted", "enabled"),
    [
        pytest.param(True, None, True, True, id="legacy-true-overrides-user-and-shared"),
        pytest.param(False, None, True, True, id="legacy-false-preserves-user-and-shared-approval"),
        pytest.param(True, False, True, False, id="local-false-overrides-legacy"),
        pytest.param(False, True, True, True, id="local-true-overrides-legacy"),
        pytest.param(True, None, False, False, id="legacy-true-requires-trust"),
    ],
)
def test_legacy_blanket_approval_has_local_precedence(
    tmp_path: Path, legacy: bool, local: bool | None, trusted: bool, enabled: bool
) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    entry = {"hasTrustDialogAccepted": trusted, "enableAllProjectMcpServers": legacy}
    _write(home / ".claude.json", {"projects": {repo.as_posix(): entry}})
    _write(home / ".claude" / "settings.json", {"enableAllProjectMcpServers": not legacy})
    _write(repo / ".claude" / "settings.json", {"enableAllProjectMcpServers": not legacy})
    _write(repo / ".claude" / "settings.local.json", {} if local is None else {"enableAllProjectMcpServers": local})
    _write(repo / ".mcp.json", {"mcpServers": {"docs": {"command": "run"}}})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is enabled


@pytest.mark.parametrize(
    ("rejections", "enabled"),
    [
        pytest.param(None, True, id="no-rejections"),
        pytest.param(["other"], True, id="named-rejections"),
        # Claude Code moves no approval from an entry whose rejections are not all names.
        pytest.param(["other", 1], False, id="unmovable-rejections"),
        pytest.param("other", False, id="rejections-not-a-list"),
    ],
)
def test_legacy_blanket_approval_lands_in_the_current_local_file_above_the_workspaces_older_one(
    tmp_path: Path, rejections: object, enabled: bool
) -> None:
    home, repo, workspace = _repository(tmp_path)
    entry: dict[str, Any] = {"hasTrustDialogAccepted": True, "enableAllProjectMcpServers": True}
    if rejections is not None:
        entry["disabledMcpjsonServers"] = rejections
    _write(home / ".claude.json", {"projects": {repo.as_posix(): entry}})
    _write(workspace / ".claude" / "settings.local.json", {"enableAllProjectMcpServers": False})
    _write(workspace / ".mcp.json", {"mcpServers": {"docs": {"command": "run"}}})

    (candidate,) = _discover(home, workspace).candidates

    assert candidate.config is not None
    # On Windows the workspace's file is the current one, and its explicit false stands.
    assert candidate.config.enabled is (enabled and sys.platform != "win32")


def test_legacy_approved_names_need_movable_rejections(tmp_path: Path) -> None:
    home, repo, _ = _repository(tmp_path)
    entry = {"hasTrustDialogAccepted": True, "enabledMcpjsonServers": ["docs"], "disabledMcpjsonServers": "x"}
    _write(home / ".claude.json", {"projects": {repo.as_posix(): entry}})
    _write(repo / ".mcp.json", {"mcpServers": {"docs": {"command": "run"}}})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


def test_an_empty_approved_name_does_not_approve_every_server(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}, "ok": {"command": "run"}}})
    _write(home / ".claude" / "settings.json", {"enabledMcpjsonServers": ["", "ok"]})

    enabled = {c.name: c.config.enabled for c in _discover(home, repo).candidates if c.config}

    assert enabled == {"srv": False, "ok": True}


def _linked_worktree(main: Path, worktree: Path, *, backlink: Path | None = None) -> None:
    """A main checkout and a worktree linked to it, as ``git worktree add`` writes them."""
    admin = main / ".git" / "worktrees" / worktree.name
    admin.mkdir(parents=True)
    (main / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{backlink or worktree / '.git'}\n", encoding="utf-8")
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")


def test_a_linked_worktree_reads_what_claude_code_keeps_for_the_main_checkout(tmp_path: Path) -> None:
    home, main, worktree = tmp_path / "home", tmp_path / "main", tmp_path / "wt"
    _linked_worktree(main, worktree)
    project = {"mcpServers": {"mine": {"command": "local"}}, "disabledMcpServers": ["mine"]}
    _write(home / ".claude.json", {"projects": {main.as_posix(): project}})

    (candidate,) = _discover(home, worktree).candidates

    assert candidate.config is not None
    assert (candidate.name, candidate.config.command, candidate.config.enabled) == ("mine", "local", False)


@pytest.mark.parametrize("bare", ["proj.git", "proj/.bare"])
def test_a_bare_repositorys_worktree_reads_what_claude_code_keeps_for_the_repository(tmp_path: Path, bare: str) -> None:
    home, common, worktree = tmp_path / "home", tmp_path / bare, tmp_path / "main"
    admin = common / "worktrees" / "main"
    admin.mkdir(parents=True)
    (common / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (admin / "commondir").write_text("../..\n", encoding="utf-8")
    (admin / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {admin}\n", encoding="utf-8")
    project = {"mcpServers": {"mine": {"command": "local"}}, "disabledMcpServers": ["user"]}
    _write(
        home / ".claude.json", {"mcpServers": {"user": {"command": "run"}}, "projects": {common.as_posix(): project}}
    )

    enabled = {c.name: c.config.enabled for c in _discover(home, worktree).candidates if c.config}

    assert enabled == {"user": False, "mine": True}


def test_a_worktree_the_main_checkout_does_not_list_reads_nothing_kept_for_it(tmp_path: Path) -> None:
    home, main, worktree = tmp_path / "home", tmp_path / "main", tmp_path / "wt"
    # The main checkout's records name another folder, so the .git file is not its worktree's.
    _linked_worktree(main, worktree, backlink=tmp_path / "elsewhere" / ".git")
    _write(home / ".claude.json", {"projects": {main.as_posix(): {"mcpServers": {"mine": {"command": "local"}}}}})

    assert _discover(home, worktree).candidates == ()


def test_claude_config_dir_moves_the_global_file(tmp_path: Path) -> None:
    _write(tmp_path / "custom" / ".claude.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(tmp_path / ".claude.json", {"mcpServers": {"other": {"command": "run"}}})

    result = _discover(tmp_path, environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "custom")})

    assert [c.name for c in result.candidates] == ["srv"]


def _shared_file_case(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A user server ``srv``, a local server and a workspace ``.mcp.json`` that redefines ``srv``."""
    home, workspace = tmp_path / "home", tmp_path / "ws"
    workspace.mkdir()
    key = workspace.resolve().as_posix()
    _write(
        home / ".claude.json",
        {"mcpServers": {"srv": {"command": "user"}}, "projects": {key: {"mcpServers": {"local": {"command": "run"}}}}},
    )
    return home, workspace, workspace / ".mcp.json"


def _broken_shared_file(shared: Path, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    redefinition = {"mcpServers": {"srv": {"command": "shared"}}}
    if case == "malformed":
        _write(shared, '{"mcpServers": ')
    elif case == "over-claude-code-limit":
        monkeypatch.setattr(claude_code, "_MCP_JSON_MAX_BYTES", 512)
        _write(shared, {**redefinition, "padding": " " * 512})
    elif case == "directory":
        shared.mkdir()
    elif case == "not-utf-8":
        shared.write_bytes(json.dumps({**redefinition, "note": "x"}).encode().replace(b'"x"', b'"\xff"'))
    else:
        _write(shared, redefinition)
        refused, secure_open = shared.resolve(), base.secure_open_regular_binary

        def refuse(path: Path) -> BinaryIO:
            if path == refused:
                raise SecureFileError("refused")
            return secure_open(path)

        monkeypatch.setattr(base, "secure_open_regular_binary", refuse)


@pytest.mark.parametrize(
    ("case", "kind", "kept"),
    [
        pytest.param("malformed", SourceErrorKind.UNPARSABLE, True, id="malformed"),
        pytest.param("over-claude-code-limit", SourceErrorKind.TOO_LARGE, True, id="over-claude-code-limit"),
        pytest.param("directory", SourceErrorKind.UNREADABLE, True, id="directory"),
        # Claude Code reads what only iCode's reader refuses, so the file may replace any lower definition.
        pytest.param("not-utf-8", SourceErrorKind.UNPARSABLE, False, id="not-utf-8"),
        pytest.param("refused-regular-file", SourceErrorKind.UNREADABLE, False, id="refused-regular-file"),
    ],
)
def test_a_shared_file_claude_code_skips_keeps_the_other_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str, kind: SourceErrorKind, kept: bool
) -> None:
    home, workspace, shared = _shared_file_case(tmp_path)
    _broken_shared_file(shared, case, monkeypatch)

    result = _discover(home, workspace)

    assert [c.name for c in result.candidates] == (["srv", "local"] if kept else ["local"])
    if kept:
        assert result.candidates[0].config is not None
        assert result.candidates[0].config.command == "user"
    (error,) = result.source_errors
    assert (error.path, error.kind) == (str(shared), kind)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_a_shared_file_without_read_permission_is_skipped_and_the_other_files_kept(tmp_path: Path) -> None:
    home, workspace, shared = _shared_file_case(tmp_path)
    _write(shared, {"mcpServers": {"srv": {"command": "shared"}}})
    shared.chmod(0)
    try:
        if os.access(shared, os.R_OK):
            pytest.skip("this user can read a file without read permission")
        result = _discover(home, workspace)
    finally:
        shared.chmod(0o600)

    assert [(c.name, c.config.command if c.config else None) for c in result.candidates] == [
        ("srv", "user"),
        ("local", "run"),
    ]
    (error,) = result.source_errors
    assert (error.path, error.kind) == (str(shared), SourceErrorKind.UNREADABLE)


def _repository(tmp_path: Path, *, trusted: bool = True) -> tuple[Path, Path, Path]:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(home / ".claude.json", {"projects": {repo.as_posix(): {"hasTrustDialogAccepted": trusted}}})
    return home, repo, repo / "sub"


def test_the_project_folders_local_settings_override_the_workspaces_older_ones(tmp_path: Path) -> None:
    home, repo, workspace = _repository(tmp_path)
    _write(repo / ".claude" / "settings.local.json", {"enableAllProjectMcpServers": True})
    # Where Claude Code kept them before 2.1.211; still read beneath the project folder's.
    _write(
        workspace / ".claude" / "settings.local.json",
        {"enableAllProjectMcpServers": False, "enabledMcpjsonServers": ["listed"], "disabledMcpjsonServers": ["no"]},
    )
    _write(workspace / ".mcp.json", {"mcpServers": {name: {"command": "run"} for name in ("docs", "listed", "no")}})

    enabled = {c.name: c.config.enabled for c in _discover(home, workspace).candidates if c.config}

    # On Windows Claude Code keeps the local settings in the session folder.
    assert enabled == {"docs": sys.platform != "win32", "listed": True, "no": False}


def test_local_settings_stay_in_the_workspace_when_the_project_folder_is_the_home_folder(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspace = repo / "sub"
    (repo / ".git").mkdir(parents=True)
    _write(repo / ".claude.json", {"projects": {repo.as_posix(): {"hasTrustDialogAccepted": True}}})
    _write(
        repo / ".claude" / "settings.local.json",
        {"enableAllProjectMcpServers": True, "enabledMcpjsonServers": ["listed"], "disabledMcpjsonServers": ["no"]},
    )
    _write(workspace / ".claude" / "settings.local.json", {"enabledMcpjsonServers": ["mine"]})
    _write(
        workspace / ".mcp.json", {"mcpServers": {name: {"command": "run"} for name in ("docs", "listed", "no", "mine")}}
    )

    enabled = {c.name: c.config.enabled for c in _discover(repo, workspace).candidates if c.config}

    # Claude Code doesn't read the project folder's file then: it only adds restrictions.
    assert enabled == {"docs": False, "listed": False, "no": False, "mine": True}


@pytest.mark.parametrize(
    "folder",
    [
        pytest.param("connector-downloads", id="exact"),
        pytest.param(
            "Connector-Downloads. ",
            id="case-and-trailing",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="Windows drops trailing dots and spaces"),
        ),
        pytest.param("connector-\u200cdownloads", id="invisible-character"),
    ],
)
def test_project_files_in_a_connector_downloads_folder_are_skipped_and_reported(tmp_path: Path, folder: str) -> None:
    home, repo, _ = _repository(tmp_path)
    downloads = repo / folder
    workspace = downloads / "inner"
    _write(home / ".claude" / "settings.json", {"enableAllProjectMcpServers": True})
    _write(home / ".claude.json", {
        "mcpServers": {"tool": {"command": "user"}},
        "projects": {repo.as_posix(): {"hasTrustDialogAccepted": True}},
    })  # fmt: skip
    _write(repo / ".mcp.json", {"mcpServers": {"repo": {"command": "run"}}})
    _write(downloads / ".mcp.json", {"mcpServers": {"tool": {"command": "downloaded"}}})
    _write(workspace / ".mcp.json", {"mcpServers": {"inner": {"command": "downloaded"}}})

    result = _discover(home, workspace)

    assert {c.name: c.config.command for c in result.candidates if c.config} == {"tool": "user", "repo": "run"}
    assert [(e.path, e.kind) for e in result.source_errors] == [
        (str(downloads / ".mcp.json"), SourceErrorKind.DOWNLOADS_FOLDER),
        (str(workspace / ".mcp.json"), SourceErrorKind.DOWNLOADS_FOLDER),
    ]


def test_a_folder_only_starting_with_the_downloads_name_is_read(tmp_path: Path) -> None:
    home, repo, _ = _repository(tmp_path)
    workspace = repo / "connector-downloads-archive"
    _write(home / ".claude" / "settings.json", {"enableAllProjectMcpServers": True})
    _write(workspace / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})

    result = _discover(home, workspace)

    assert [c.name for c in result.candidates] == ["srv"]
    assert result.source_errors == ()


def test_settings_in_a_connector_downloads_folder_restrict_but_do_not_approve(tmp_path: Path) -> None:
    home, repo, _ = _repository(tmp_path)
    workspace = repo / "connector-downloads"
    _write(repo / ".mcp.json", {"mcpServers": {"approved": {"command": "run"}, "denied": {"command": "run"}}})
    _write(workspace / ".claude" / "settings.json", {"enabledMcpjsonServers": ["approved"]})
    _write(
        workspace / ".claude" / "settings.local.json",
        {"enabledMcpjsonServers": ["approved"], "permissions": {"deny": ["mcp__denied"]}},
    )
    _write(repo / ".claude" / "settings.local.json", {"enabledMcpjsonServers": ["denied"]})

    result = _discover(home, workspace)

    assert {c.name: (c.config.enabled, c.config.allowed_tools) for c in result.candidates if c.config} == {
        "approved": (False, None),
        # The project folder's own file still approves on POSIX, where Claude Code reads it.
        "denied": (sys.platform != "win32", []),
    }


@pytest.mark.parametrize("policy", [".claude.json", ".claude/settings.json", "ws/.claude/settings.local.json"])
def test_an_unreadable_policy_does_not_offer_servers_without_its_restrictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    workspace = tmp_path / "ws"
    _write(workspace / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(tmp_path / ".claude/settings.json", {"enableAllProjectMcpServers": True})
    assert (candidate := _discover(tmp_path, workspace).candidates[0]).config is not None
    assert candidate.config.enabled
    path = tmp_path / policy
    value = {
        ".claude.json": {"projects": {workspace.resolve().as_posix(): {"disabledMcpServers": ["srv"]}}},
        ".claude/settings.json": {"disabledMcpjsonServers": ["srv"]},
        "ws/.claude/settings.local.json": {"permissions": {"deny": ["mcp__srv"]}},
    }
    _write(path, {**value[policy], "padding": " " * 512})
    monkeypatch.setattr(base, "MAX_SOURCE_BYTES", 512)

    result = _discover(tmp_path, workspace)

    assert result.candidates == ()
    (error,) = result.source_errors
    assert (error.path, error.kind) == (str(path), SourceErrorKind.UNREADABLE)


def test_detect_needs_a_config_file(tmp_path: Path) -> None:
    workspace = tmp_path / "repo" / "module" / "pkg"
    workspace.mkdir(parents=True)
    source, ctx = ClaudeCodeSource(), ImportContext(str(workspace), tmp_path, {})
    assert not source.detect(ctx)

    _write(workspace.parent / ".mcp.json", {})

    assert source.detect(ctx)
