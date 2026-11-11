# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Claude Code: where servers are found and how each entry converts."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind
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
            (ImportWarning(Kind.VARIABLE_REQUIRED, ("A", "CLAUDE_PROJECT_DIR")),),
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
            (ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.headers.Authorization",)),),
        ),
        (
            {"command": "run", "alwaysLoad": True, "cwd": "/x"},
            (ImportWarning(Kind.NOT_IMPORTED, ("mcpServers.srv.cwd",)),),
        ),
        # Claude Code signs in with OAuth whenever such a server asks.
        ({"type": "http", "url": "https://x"}, (ImportWarning(Kind.AUTH_NOT_IMPORTED),)),
    ],
    ids=["required-variables", "auth-and-helper", "credential-header", "unknown-field", "no-headers"],
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
            "projects": {str(repo): {"mcpServers": {"shared": {"command": "local"}}}},
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
                str(repo): {
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


@pytest.mark.parametrize("trusted", [False, True], ids=["untrusted", "trusted"])
def test_a_repository_cannot_approve_its_own_servers_until_its_folder_is_trusted(tmp_path: Path, trusted: bool) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    # Older Claude Code kept approvals in the project's entry, now moved to the trust-gated local settings.
    entry = {"enabledMcpjsonServers": ["approved-in-entry"], "hasTrustDialogAccepted": trusted}
    _write(home / ".claude.json", {"projects": {str(repo): entry}})
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
    _write(home / ".claude.json", {"projects": {str(tmp_path / "repos"): {"hasTrustDialogAccepted": True}}})
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
    _write(home / ".claude.json", {"projects": {str(tmp_path / "alias"): {"hasTrustDialogAccepted": True}}})
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(repo / ".claude" / "settings.json", {"enabledMcpjsonServers": ["srv"]})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


def test_an_untrusted_repository_cannot_approve_all_its_servers(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _write(repo / ".mcp.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(repo / ".claude" / "settings.json", {"enableAllProjectMcpServers": True})

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.enabled is False


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
    _write(home / ".claude.json", {"projects": {str(main): project}})

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
    _write(home / ".claude.json", {"mcpServers": {"user": {"command": "run"}}, "projects": {str(common): project}})

    enabled = {c.name: c.config.enabled for c in _discover(home, worktree).candidates if c.config}

    assert enabled == {"user": False, "mine": True}


def test_a_worktree_the_main_checkout_does_not_list_reads_nothing_kept_for_it(tmp_path: Path) -> None:
    home, main, worktree = tmp_path / "home", tmp_path / "main", tmp_path / "wt"
    # The main checkout's records name another folder, so the .git file is not its worktree's.
    _linked_worktree(main, worktree, backlink=tmp_path / "elsewhere" / ".git")
    _write(home / ".claude.json", {"projects": {str(main): {"mcpServers": {"mine": {"command": "local"}}}}})

    assert _discover(home, worktree).candidates == ()


def test_claude_config_dir_moves_the_global_file(tmp_path: Path) -> None:
    _write(tmp_path / "custom" / ".claude.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(tmp_path / ".claude.json", {"mcpServers": {"other": {"command": "run"}}})

    result = _discover(tmp_path, environ={"CLAUDE_CONFIG_DIR": str(tmp_path / "custom")})

    assert [c.name for c in result.candidates] == ["srv"]


def test_a_broken_file_is_reported_and_the_others_still_listed(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _write(tmp_path / ".claude.json", {"mcpServers": {"srv": {"command": "run"}}})
    _write(workspace / ".mcp.json", '{"mcpServers": ')

    result = _discover(tmp_path, workspace)

    assert [c.name for c in result.candidates] == ["srv"]
    (error,) = result.source_errors
    assert (error.path, error.kind) == (str(workspace / ".mcp.json"), SourceErrorKind.UNPARSABLE)


def test_detect_needs_a_config_file(tmp_path: Path) -> None:
    source, ctx = ClaudeCodeSource(), ImportContext(str(tmp_path / "ws"), tmp_path, {})
    assert not source.detect(ctx)

    _write(tmp_path / "ws" / ".mcp.json", {})

    assert source.detect(ctx)
