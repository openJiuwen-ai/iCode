# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Codex: where servers are found and how each entry converts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers import ImportContext, ImportWarning, Problem, SourceErrorKind, codex
from chrys.service.mcp.importers import WarningKind as Kind
from chrys.service.mcp.importers.base import MAX_REQUEST_TIMEOUT_SECONDS
from chrys.service.mcp.importers.codex import CodexSource
from tests.support.symlinks import symlink_or_skip

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
        # A `codex mcp login` sign-in lives outside the file.
        ('url = "https://x"\n', (ImportWarning(Kind.AUTH_NOT_IMPORTED),), {}),
        # iCode passes on its whole environment but these two, so they must be set.
        (
            'command = "run"\nenv_vars = ["PATH", "PYTHONPATH", { name = "PYTHONHOME", source = "local" }]\n',
            (ImportWarning(Kind.VARIABLE_REQUIRED, ("PYTHONPATH", "PYTHONHOME")),),
            {"env": {"PYTHONPATH": "{{PYTHONPATH}}", "PYTHONHOME": "{{PYTHONHOME}}"}},
        ),
        (
            'command = "run"\nenv_vars = ["PYTHONPATH"]\nenv = { PYTHONPATH = "/x" }\n',
            (),
            {"env": {"PYTHONPATH": "/x"}},
        ),
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
    ids=[
        "optional-variable",
        "provider-auth",
        "no-headers",
        "passed-variables",
        "passed-variable-set",
        "deny-only",
        "zero-timeout",
        "unread-settings",
    ],
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


def _repo(path: Path) -> Path:
    """A Git checkout Codex recognizes as a project root: its ``.git`` folder holds ``HEAD``."""
    _write(path / ".git" / "HEAD", "ref: refs/heads/main\n")
    return path


def _trust(folder: Path, level: str = "trusted") -> str:
    # A literal key: a Windows path's backslashes are not escapes.
    return f"[projects.'{folder}']\ntrust_level = \"{level}\"\n"


def test_project_files_merge_into_same_named_user_servers(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    workspace = repo / "pkg"
    workspace.mkdir()
    user = (
        '[mcp_servers.a]\ncommand = "user"\nargs = ["x", "y"]\nenv = { A = "1", B = "1" }\n'
        '[mcp_servers.off]\ncommand = "user"\n'
        '[mcp_servers.web]\nurl = "https://user"\nhttp_headers = { X = "1" }\n'
    ) + _trust(repo)
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


def test_every_trusted_folder_from_the_project_root_down_to_the_workspace_is_read(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    workspace = repo / "a" / "b"
    workspace.mkdir(parents=True)
    _write(home / ".codex" / "config.toml", _trust(repo))
    for folder, command in ((tmp_path, "above"), (repo, "root"), (repo / "a", "middle"), (workspace, "workspace")):
        _write(folder / ".codex" / "config.toml", f'[mcp_servers.{command}]\ncommand = "{command}"\n')

    assert [c.name for c in _discover(home, workspace).candidates] == ["root", "middle", "workspace"]


@pytest.mark.parametrize(
    ("trust", "read"),
    [
        ("", []),
        ("untrusted-root", []),
        ("trusted-root", ["root", "workspace"]),
        ("workspace-untrusted", ["root"]),
        ("trusted-workspace", ["workspace"]),
    ],
)
def test_project_files_are_read_only_from_folders_the_user_trusts(tmp_path: Path, trust: str, read: list[str]) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    workspace = repo / "pkg"
    workspace.mkdir()
    entries = {
        "": "",
        "untrusted-root": _trust(repo, "untrusted"),
        "trusted-root": _trust(repo),
        # A folder's own entry comes before the project root's.
        "workspace-untrusted": _trust(repo) + _trust(workspace, "untrusted"),
        "trusted-workspace": _trust(workspace),
    }
    _write(home / ".codex" / "config.toml", '[mcp_servers.user]\ncommand = "user"\n' + entries[trust])
    _write(repo / ".codex" / "config.toml", '[mcp_servers.root]\ncommand = "root"\n')
    _write(workspace / ".codex" / "config.toml", '[mcp_servers.workspace]\ncommand = "workspace"\n')

    assert [c.name for c in _discover(home, workspace).candidates] == ["user", *read]


def test_a_key_naming_another_path_to_the_folder_does_not_override_its_own(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    workspace = repo / "pkg"
    workspace.mkdir()
    symlink_or_skip(tmp_path / "alias", workspace, target_is_directory=True)
    # Listed first, a trusted alias still loses to the folder's own key, which Codex looks up first.
    _write(home / ".codex" / "config.toml", _trust(tmp_path / "alias") + _trust(workspace, "untrusted"))
    _write(workspace / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    assert _discover(home, workspace).candidates == ()


@pytest.mark.parametrize(
    ("windows", "keys", "read"),
    [
        (False, {"given": "trusted", "lower": "untrusted"}, True),
        (True, {"given": "trusted", "lower": "untrusted"}, False),
        (True, {"upper": "trusted"}, True),
        (False, {"upper": "trusted"}, False),
    ],
    ids=["exact", "windows-lowercase-first", "windows-any-case", "case-sensitive"],
)
def test_trust_keys_are_looked_up_as_codex_looks_them_up(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, windows: bool, keys: dict[str, str], read: bool
) -> None:
    monkeypatch.setattr(codex, "get_platform", lambda: SimpleNamespace(is_windows=windows))
    home, repo = tmp_path / "home", _repo(tmp_path / "Repo")
    # On Windows Codex lowercases the path it looks up, then takes any key equal but for case.
    spelled = {"given": str(repo), "lower": str(repo).lower(), "upper": str(repo).upper()}
    _write(home / ".codex" / "config.toml", "".join(_trust(spelled[key], level) for key, level in keys.items()))
    _write(repo / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    assert [c.name for c in _discover(home, repo).candidates] == (["srv"] if read else [])


def test_a_project_file_cannot_change_a_user_server_unless_trusted(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    _write(home / ".codex" / "config.toml", '[mcp_servers.github]\ncommand = "github-mcp"\n')
    _write(repo / ".codex" / "config.toml", '[mcp_servers.github]\ncommand = "evil"\n')

    (candidate,) = _discover(home, repo).candidates

    assert candidate.config is not None
    assert candidate.config.command == "github-mcp"


def test_a_linked_worktree_is_trusted_through_its_main_checkout(tmp_path: Path) -> None:
    home, main, worktree = tmp_path / "home", _repo(tmp_path / "main"), tmp_path / "wt"
    admin = main / ".git" / "worktrees" / "wt"
    _write(admin / "commondir", "../..\n")
    _write(admin / "gitdir", f"{worktree / '.git'}\n")
    _write(worktree / ".git", f"gitdir: {admin}\n")
    _write(home / ".codex" / "config.toml", _trust(main))
    _write(worktree / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    assert [c.name for c in _discover(home, worktree).candidates] == ["srv"]


def test_a_worktree_of_a_separate_git_folder_is_trusted_through_the_folder_pointing_at_it(tmp_path: Path) -> None:
    home, project = tmp_path / "home", tmp_path / "proj"
    bare, worktree = project / ".bare", project / "main"
    admin = bare / "worktrees" / "main"
    _write(bare / "HEAD", "ref: refs/heads/main\n")
    _write(admin / "commondir", "../..\n")
    _write(admin / "gitdir", f"{worktree / '.git'}\n")
    _write(project / ".git", "gitdir: ./.bare\n")
    _write(worktree / ".git", f"gitdir: {admin}\n")
    _write(home / ".codex" / "config.toml", _trust(project))
    _write(worktree / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    assert [c.name for c in _discover(home, worktree).candidates] == ["srv"]


def test_a_worktree_is_not_trusted_through_a_folder_whose_git_is_another_repository(tmp_path: Path) -> None:
    home, code, worktree = tmp_path / "home", _repo(tmp_path / "code"), tmp_path / "wt"
    common = code / "x.git"
    admin = common / "worktrees" / "wt"
    _write(common / "HEAD", "ref: refs/heads/main\n")
    _write(admin / "commondir", "../..\n")
    _write(admin / "gitdir", f"{worktree / '.git'}\n")
    _write(worktree / ".git", f"gitdir: {admin}\n")
    # code holds the bare repository, but its own .git is a different one: Codex keys this worktree by no folder.
    _write(home / ".codex" / "config.toml", _trust(code))
    _write(worktree / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    assert _discover(home, worktree).candidates == ()


@pytest.mark.parametrize(("git", "read"), [("folder", ["srv"]), ("submodule", [])], ids=["checkout", "submodule"])
def test_only_a_main_checkout_lends_its_trust_to_a_project_inside_it(tmp_path: Path, git: str, read: list[str]) -> None:
    home, repo = tmp_path / "home", tmp_path / "super" / "sub"
    workspace = repo / "pkg"
    if git == "folder":
        _repo(repo)
    else:
        _write(tmp_path / "super" / ".git" / "modules" / "sub" / "HEAD", "ref: refs/heads/main\n")
        _write(repo / ".git", "gitdir: ../.git/modules/sub\n")
    _write(workspace / "pyproject.toml", "")
    # The project is pkg, by its marker; the repository around it counts only as a main checkout.
    _write(home / ".codex" / "config.toml", 'project_root_markers = ["pyproject.toml"]\n' + _trust(repo))
    _write(workspace / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    assert [c.name for c in _discover(home, workspace).candidates] == read


def test_a_project_folder_that_is_the_codex_home_is_not_read_again(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    workspace = repo / "pkg"
    workspace.mkdir()
    _write(home / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "user"\n' + _trust(repo))
    _write(repo / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "project"\nenabled = false\n')
    # Codex skips the folder it read as the user's, however it is reached.
    symlink_or_skip(workspace / ".codex", home / ".codex", target_is_directory=True)

    (candidate,) = _discover(home, workspace).candidates

    assert candidate.config is not None
    assert (candidate.config.command, candidate.config.enabled) == ("project", False)


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


@pytest.mark.parametrize(
    "text",
    ["a = " + "[" * 5000 + "]" * 5000 + "\n", "a = " + "1" * 5000 + "\n"],
    ids=["nested-too-deep", "number-too-long"],
)
def test_a_file_too_large_to_parse_is_reported_and_the_others_still_listed(tmp_path: Path, text: str) -> None:
    home, repo = tmp_path / "home", _repo(tmp_path / "repo")
    workspace = repo / "work"
    _write(home / ".codex" / "config.toml", _trust(repo))
    _write(repo / ".codex" / "config.toml", text)
    _write(workspace / ".codex" / "config.toml", '[mcp_servers.srv]\ncommand = "run"\n')

    result = _discover(home, workspace)

    assert [c.name for c in result.candidates] == ["srv"]
    assert [(e.path, e.kind) for e in result.source_errors] == [
        (str(repo / ".codex" / "config.toml"), SourceErrorKind.UNPARSABLE)
    ]
