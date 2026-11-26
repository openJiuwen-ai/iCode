# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""opencode: where servers are found and how each entry converts."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import pytest

from chrys.service.mcp.importers import FieldUse, ImportContext, ImportWarning, Problem, SourceErrorKind
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


def test_a_line_comment_ends_at_a_carriage_return() -> None:
    text = '{\r  // note\r  "a": 1\r}'

    assert json.loads(strip_jsonc(text)) == {"a": 1}
    assert strip_jsonc(text).count("\r") == text.count("\r")


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
    assert candidate.warnings == ()
    for key, value in expected.items():
        assert getattr(candidate.config, key) == value


def test_only_references_in_source_text_become_live(tmp_path: Path) -> None:
    _write(
        _global(tmp_path),
        r"""{"mcp": {"srv": {"type": "remote", "url": "https://x", "headers": {
            "X-Token": "literal \u007benv:TOKEN} / {env:TOKEN}",
            "X-File": "{file:/literal\u007d",
            "X-Other": "{\u0065nv:OTHER}"
        }}}}""",
    )

    (candidate,) = _discover(tmp_path).candidates

    assert candidate.config is not None
    assert candidate.config.headers == {
        "X-Token": "literal {env:TOKEN} / {{TOKEN}}",
        "X-File": "{file:/literal}",
        "X-Other": "{env:OTHER}",
    }


def test_encoded_dot_path_segments_require_manual_setup(tmp_path: Path) -> None:
    url = "https://example.test/team/%2E%2e/mcp"
    remote = {"type": "remote", "headers": {"X-Test": "demo"}}
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(
        _global(home),
        {
            "mcp": {
                "svc": {**remote, "url": "https://example.test/global"},
                "query": {**remote, "url": "https://example.test/team/report%2ejson?next=/%2e%2e/mcp"},
            },
        },
    )
    _write(workspace / "opencode.json", {"mcp": {"svc": {**remote, "url": url}}})

    result = _discover(home, workspace)

    assert result.source_errors == ()
    candidates = {candidate.name: candidate for candidate in result.candidates}
    svc = candidates["svc"]
    assert (svc.config, svc.problem, svc.problem_field, svc.field_use) == (
        None,
        Problem.VALUE_NOT_REPRESENTABLE,
        "mcp.svc.url",
        FieldUse.URL,
    )
    query = candidates["query"].config
    assert query is not None
    assert query.url == "https://example.test/team/report%2ejson?next=/%2e%2e/mcp"


@pytest.mark.parametrize("explicit", [None, "0"])
def test_opencode_command_keeps_bun_runtime_mode(tmp_path: Path, explicit: str | None) -> None:
    env = {} if explicit is None else {"BUN_BE_BUN": explicit}
    candidate = _only(tmp_path, {"type": "local", "command": ["opencode", "server.ts"], "environment": env})

    assert candidate.config is not None
    assert candidate.config.env == {"BUN_BE_BUN": "1" if explicit is None else explicit}


def test_the_experimental_timeout_applies_to_servers_without_their_own(tmp_path: Path) -> None:
    candidate = _only(tmp_path, {"type": "local", "command": ["run"]}, experimental={"mcp_timeout": 90_000})

    assert candidate.config is not None
    assert candidate.config.request_timeout == 90


@pytest.mark.parametrize(
    ("entry", "warnings"),
    [
        (
            {"type": "remote", "url": "https://x", "oauth": {"callbackPort": 19876, "scope": "read"}},
            (ImportWarning(Kind.AUTH_NOT_IMPORTED),),
        ),
        ({"type": "remote", "url": "https://x"}, ()),
        ({"type": "remote", "url": "https://x", "oauth": False}, ()),
        (
            {"type": "local", "command": ["run"], "cwd": "/x"},
            (),
        ),
        (
            {"type": "local", "command": ["run"], "environment": {"A": "{env:T}", "B": "{env:U}-{env:T}"}},
            (),
        ),
        (
            {"type": "remote", "url": "https://x", "headers": {"Authorization": "Bearer {env:KEY}"}},
            (),
        ),
    ],
    ids=["oauth", "no-auth-settings", "oauth-off", "cwd-imported", "environment-variables", "header-variable"],
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


def test_every_candidate_name_is_plain_text_and_siblings_stay_importable(tmp_path: Path) -> None:
    # Display sanitizers accept only exact str; a str subclass from a referenced key broke the pane.
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(_global(home), {"mcp": {"team_api": {"type": "local", "command": ["run"]}}})
    _write(
        workspace / "opencode.json",
        {
            "mcp": {
                "team_{env:X}": {"type": "local", "command": ["run"]},
                "ok": {"type": "local", "command": ["ok"]},
                "late_{env:Y}": {"enabled": False},
            }
        },
    )

    result = _discover(home, workspace)

    assert result.source_errors == ()
    assert {candidate.name for candidate in result.candidates} == {"team_{env:X}", "ok", "late_{env:Y}", "team_api"}
    assert all(type(candidate.name) is str for candidate in result.candidates)
    candidates = {candidate.name: candidate for candidate in result.candidates}
    assert candidates["team_{env:X}"].problem is Problem.VARIABLE_NOT_SUPPORTED
    assert candidates["team_api"].problem is Problem.VARIABLE_NOT_SUPPORTED
    ok = candidates["ok"].config
    assert ok is not None and ok.command == "ok"


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


@pytest.mark.parametrize(
    ("project_rules", "expected"),
    [
        ({"open_*": True}, {"github.com": ("tools.github_com_*",), "team🚀api": ("tools.team__api_*",), "open": ()}),
        (
            {"open_*": True, "*": False, "github_com_read": True},
            {
                "github.com": ("tools.github_com_*", "tools.*"),
                "team🚀api": ("tools.team__api_*", "tools.*"),
                "open": ("tools.*",),
            },
        ),
        (
            {"*": False, "github_com_*": True},
            {
                "github.com": ("tools.*",),
                "team🚀api": ("tools.team__api_*", "tools.*"),
                "open": ("tools.open_*", "tools.*"),
            },
        ),
    ],
    ids=["same-key-override", "global-disable", "same-key-override-keeps-position"],
)
def test_recognized_tool_disables_close_the_server_tool_scope(tmp_path: Path, project_rules, expected) -> None:
    home, workspace = tmp_path / "home", tmp_path / "repo"
    (workspace / ".git").mkdir(parents=True)
    _write(
        _global(home),
        {
            "mcp": {
                "github.com": {"type": "local", "command": ["run"]},
                "team🚀api": {"type": "local", "command": ["run"]},
                "open": {"type": "remote", "url": "https://example.com/mcp"},
            },
            "tools": {"github_com_*": False, "team__api_*": False, "open_*": False, "edit": False},
        },
    )
    _write(workspace / "opencode.json", {"tools": project_rules})

    candidates = _discover(home, workspace).candidates

    assert {candidate.name for candidate in candidates} == expected.keys()
    for candidate in candidates:
        fields = expected[candidate.name]
        assert candidate.config is not None
        assert candidate.config.allowed_tools == ([] if fields else None)
        assert candidate.warnings == ((ImportWarning(Kind.TOOLS_RESTRICTED, fields),) if fields else ())


@pytest.mark.parametrize(
    ("rules", "github_fields", "gitlab_fields"),
    [
        ({"tools": {"*_delete": False}}, ("tools.*_delete",), ("tools.*_delete",)),
        ({"tools": {"git?ub_delete": False}}, ("tools.git?ub_delete",), ()),
        ({"tools": {"*": False, "github_*": True}}, (), ("tools.*",)),
        ({"tools": {"github_*": True, "*": False}}, ("tools.*",), ("tools.*",)),
        ({"tools": {"*": False, "github_read": True}}, ("tools.*",), ("tools.*",)),
        ({"permission": {"github_*": "deny"}}, ("permission.github_*",), ()),
        ({"permission": "deny"}, ("permission.*",), ("permission.*",)),
        (
            {"permission": {"github_*": {"*": "deny", "x": "allow"}, "gitlab_*": {"x": "allow", "*": "deny"}}},
            (),
            ("permission.gitlab_*",),
        ),
        (
            {"tools": {"github_*": False, "*_delete": False}, "permission": {"github_*": "ask"}},
            ("tools.*_delete",),
            ("tools.*_delete",),
        ),
    ],
    ids=[
        "cross-server-disable",
        "single-character-wildcard",
        "server-enable-after-global-disable",
        "global-disable-after-server-enable",
        "partial-enable",
        "permission-deny",
        "permission-shorthand",
        "permission-last-pattern-decides",
        "permission-overrides-tools-in-place",
    ],
)
def test_tool_scope_uses_ordered_rules(
    tmp_path: Path, rules: dict[str, object], github_fields: tuple[str, ...], gitlab_fields: tuple[str, ...]
) -> None:
    local = {"type": "local", "command": ["run"]}
    _write(_global(tmp_path), {"mcp": {"github": local, "gitlab": local}, **rules})

    github, gitlab = _discover(tmp_path).candidates

    for candidate, fields in ((github, github_fields), (gitlab, gitlab_fields)):
        assert candidate.config is not None
        assert candidate.config.allowed_tools == ([] if fields else None)
        assert candidate.warnings == ((ImportWarning(Kind.TOOLS_RESTRICTED, fields),) if fields else ())


@pytest.mark.parametrize(
    ("rule", "fields"),
    [
        ({"{env:TEAM_SERVER}_*": "deny"}, {"team": "{env:TEAM_SERVER}_*", "other": "{env:TEAM_SERVER}_*"}),
        ({"team_{env:TOOL}": "deny"}, {"team": "team_{env:TOOL}"}),
        ({"team_*": "{env:ACTION}"}, {"team": "team_*"}),
        ({"team_*": {"{env:PATTERN}": "deny"}}, {"team": "team_*"}),
    ],
    ids=["whole-server-prefix", "fixed-server-prefix", "action", "argument-pattern"],
)
def test_unresolved_permission_can_only_narrow_tool_scope(
    tmp_path: Path, rule: dict[str, object], fields: dict[str, str]
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    local = {"type": "local", "command": ["run"]}
    _write(_global(home), {"mcp": {"team": local, "other": local}})
    _write(workspace / "opencode.json", {"permission": rule})

    result = _discover(home, workspace)

    assert result.source_errors == ()
    assert {candidate.name for candidate in result.candidates} == {"team", "other"}
    for candidate in result.candidates:
        assert candidate.config is not None
        field = fields.get(candidate.name)
        assert candidate.config.allowed_tools == ([] if field else None)
        assert candidate.warnings == (
            (ImportWarning(Kind.TOOLS_RESTRICTED, (f"permission.{field}",)),) if field else ()
        )


def test_every_folder_from_the_git_root_down_to_the_workspace_is_read(tmp_path: Path) -> None:
    home, repo = tmp_path / "home", tmp_path / "repo"
    workspace = repo / "a" / "b"
    (repo / ".git").mkdir(parents=True)
    workspace.mkdir(parents=True)
    remote = {"type": "remote", "headers": {"X": "1"}}
    _write(_global(home), {"mcp": {"api": {**remote, "url": "https://prod"}, "b": {**remote, "url": "https://g"}}})
    _write(tmp_path / "opencode.json", {"mcp": {"above": {**remote, "url": "https://above"}}})
    _write(repo / "a" / "opencode.json", {"mcp": {"api": {"type": "remote", "url": "https://dev"}}})
    # .opencode folders are read nearest first, so the one higher up wins, as in opencode.
    _write(repo / ".opencode" / "opencode.json", {"mcp": {"b": {"type": "remote", "url": "https://root-folder"}}})
    _write(
        workspace / ".opencode" / "opencode.json", {"mcp": {"b": {"type": "remote", "url": "https://workspace-folder"}}}
    )

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
    full = {
        "type": "local",
        "command": ["global", "-x"],
        "environment": {"A": "1", "B": "1"},
        "enabled": False,
        "cwd": "global-dir",
    }
    _write(_global(home), {"mcp": {"srv": full, "off": {"type": "local", "command": ["run"]}}})
    project = {
        "srv": {"type": "local", "command": ["project"], "environment": {"B": "2"}, "cwd": "project-dir"},
        "off": {"enabled": False},
        "unknown": {"enabled": False},
    }
    _write(workspace / "opencode.json", {"mcp": project})

    candidates = {c.name: c for c in _discover(home, workspace).candidates}

    # Objects merge key by key; a list or a value replaces the earlier one.
    srv, off = candidates["srv"].config, candidates["off"].config
    assert srv is not None and off is not None
    assert (srv.command, srv.args, srv.env, srv.enabled) == ("project", [], {"A": "1", "B": "2"}, False)
    assert srv.cwd == "project-dir"
    assert candidates["srv"].source_path == str(workspace / "opencode.json")
    assert not off.enabled
    assert candidates["off"].source_path == str(_global(home))
    assert "unknown" not in candidates


def test_an_entry_without_a_type_only_switches_the_server_on_or_off(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    remote = {"type": "remote", "url": "https://real", "headers": {"Authorization": "Bearer {env:KEY}"}}
    _write(_global(home), {"mcp": {"srv": {**remote, "enabled": False}}})
    # opencode reads an entry without a type as its enabled switch alone, its other keys ignored.
    _write(workspace / "opencode.json", {"mcp": {"srv": {"url": "https://elsewhere", "enabled": True}}})

    result = _discover(home, workspace)

    (candidate,) = result.candidates
    assert candidate.config is not None
    assert (candidate.config.url, candidate.config.enabled) == ("https://real", True)
    assert candidate.source_path == str(_global(home))
    assert result.source_errors == ()


def test_ignored_headers_do_not_carry_over_when_a_server_switches_transport(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(
        _global(home),
        {"mcp": {"srv": {"type": "local", "command": ["run"], "headers": {"X": "local-only"}}}},
    )
    _write(workspace / "opencode.json", {"mcp": {"srv": {"type": "remote", "url": "https://real"}}})

    (candidate,) = _discover(home, workspace).candidates

    assert candidate.config is not None
    assert (candidate.config.url, candidate.config.headers) == ("https://real", {})


@pytest.mark.parametrize(
    ("bad_entry", "globally_enabled"),
    [
        ({"type": "remote", "headers": {"X": "1"}}, True),
        ({"type": "remote", "url": "https://other", "oauth": {"callbackPort": 0}}, False),
    ],
    ids=["missing-url", "invalid-oauth-port"],
)
def test_a_rejected_project_file_does_not_offer_a_global_fallback(
    tmp_path: Path, bad_entry: dict[str, Any], globally_enabled: bool
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(
        _global(home),
        {
            "mcp": {"existing": {"type": "remote", "url": "https://real", "enabled": globally_enabled}},
            "permission": {"*": "allow" if globally_enabled else "deny"},
        },
    )
    _write(
        workspace / "opencode.json",
        {
            "mcp": {"existing": {"enabled": not globally_enabled}, "bad": bad_entry},
            "permission": {"*": "deny" if globally_enabled else "allow"},
        },
    )

    result = _discover(home, workspace)

    assert result.candidates == ()
    assert [(e.path, e.kind, e.field) for e in result.source_errors] == [
        (str(workspace / "opencode.json"), SourceErrorKind.INVALID, "mcp.bad")
    ]


@pytest.mark.parametrize(
    ("extra", "expected_name", "timeout"),
    [
        ({"timeout": {"catalog": 1000, "execution": 1000}}, None, 1),
        ({"timeout": {"catalog": 1000, "execution": 2500}}, None, 60),
        ({"timeout": {"type": "local", "command": ["run"]}}, "timeout", 60),
        ({"local": {"type": "local", "command": ["run"], "oauth": True}}, "local", 60),
        ({"servers": {}}, None, 60),
        ({"servers": {"type": "local", "command": ["run"]}}, "servers", 60),
    ],
    ids=[
        "global-timeout",
        "different-timeouts",
        "server-named-timeout",
        "unused-local-oauth",
        "empty-envelope",
        "server-named-servers",
    ],
)
def test_a_valid_project_file_keeps_its_server_and_tool_restrictions(
    tmp_path: Path, extra: dict[str, Any], expected_name: str | None, timeout: float
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(_global(home), {"mcp": {"existing": {"type": "remote", "url": "https://real"}}})
    _write(
        workspace / "opencode.json",
        {"mcp": {"existing": {"enabled": False}, **extra}, "permission": {"existing_*": "deny"}},
    )

    result = _discover(home, workspace)

    assert result.source_errors == ()
    assert [c.name for c in result.candidates] == (["existing", expected_name] if expected_name else ["existing"])
    config = result.candidates[0].config
    assert config is not None
    assert (config.enabled, config.allowed_tools, config.request_timeout) == (False, [], timeout)


@pytest.mark.parametrize(
    ("phase_timeout", "seconds"),
    [({"catalog": 5000, "execution": 5000}, 5), ({"catalog": 1000, "execution": 2000}, 60)],
    ids=["representable", "source-default"],
)
def test_nested_and_flat_phase_timeouts_keep_connection_settings(
    tmp_path: Path, phase_timeout: dict[str, int], seconds: int
) -> None:
    nested = {"type": "local", "command": ["nested"], "disabled": True, "timeout": phase_timeout}
    flat = {"type": "remote", "url": "https://real", "timeout": phase_timeout, "enabled": False}
    _write(_global(tmp_path), {"mcp": {"flat": flat, "servers": {"nested": nested, "flat": nested}}})

    result = _discover(tmp_path)

    assert result.source_errors == ()
    flat_candidate, nested_candidate = result.candidates
    assert flat_candidate.config is not None and nested_candidate.config is not None
    assert (flat_candidate.config.url, nested_candidate.config.command) == ("https://real", "nested")
    for candidate in (flat_candidate, nested_candidate):
        assert candidate.config is not None
        assert (candidate.config.enabled, candidate.config.request_timeout) == (False, seconds)


@pytest.mark.parametrize("reference_in", ["transport", "permission"])
def test_unresolved_layer_does_not_relax_siblings(tmp_path: Path, reference_in: str) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    original = {"type": "local", "command": ["run"]}
    _write(
        _global(home),
        {
            "mcp": {"unknown": original, "off": {**original, "enabled": False}, "replaced": original},
            "permission": {"off_*": "deny"},
        },
    )
    mcp = {
        "off": {"enabled": True},
        "replaced": {"type": "remote", "url": "https://other"},
        "independent": original,
    }
    permission = {"off_*": "allow", "independent_*": "deny"}
    if reference_in == "transport":
        mcp["unknown"] = {"type": "{env:TRANSPORT}", "url": "https://real"}
        refused_names, problem_field = ("unknown", "replaced"), "mcp.unknown.type"
    else:
        permission["unknown_*"] = "{env:ACTION}"
        refused_names, problem_field = ("replaced",), "permission.unknown_*"
    _write(workspace / "opencode.json", {"mcp": mcp, "permission": permission})

    result = _discover(home, workspace)

    assert result.source_errors == ()
    candidates = {candidate.name: candidate for candidate in result.candidates}
    for name in refused_names:
        refused = candidates[name]
        assert (refused.config, refused.problem, refused.problem_field) == (
            None,
            Problem.VARIABLE_NOT_SUPPORTED,
            problem_field,
        )
    off, independent = candidates["off"].config, candidates["independent"].config
    assert off is not None and independent is not None
    assert (off.enabled, off.allowed_tools) == (False, [])
    assert (independent.command, independent.allowed_tools) == ("run", [])


def test_unresolved_override_name_blocks_possible_matches(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    local = {"type": "local", "command": ["run"]}
    _write(_global(home), {"mcp": {"team_api": local, "other_api": local}})
    name = "team_{env:SERVER_NAME}"
    _write(workspace / "opencode.json", {"mcp": {name: {"enabled": False}}})

    result = _discover(home, workspace)

    assert result.source_errors == ()
    candidates = {candidate.name: candidate for candidate in result.candidates}
    for key in (name, "team_api"):
        refused = candidates[key]
        assert (refused.config, refused.problem, refused.problem_field, refused.field_use) == (
            None,
            Problem.VARIABLE_NOT_SUPPORTED,
            f"mcp.{name}",
            FieldUse.NAME,
        )
    other = candidates["other_api"].config
    assert other is not None and other.enabled


def test_the_last_global_timeout_applies_to_servers_from_every_file(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(home / ".config" / "opencode" / "config.json", {"experimental": {"mcp_timeout": 10_000}})
    _write(
        _global(home),
        {"experimental": {"mcp_timeout": 90_000}, "mcp": {"timeout": {"catalog": 2000, "execution": 2000}}},
    )
    _write(workspace / "opencode.json", {"mcp": {"srv": {"type": "local", "command": ["run"]}}})

    (candidate,) = _discover(home, workspace).candidates

    assert candidate.config is not None
    assert candidate.config.request_timeout == 90


def test_a_broken_global_file_drops_the_global_layer_but_keeps_project_servers(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    _write(home / ".config" / "opencode" / "config.json", '{"mcp": ')
    _write(_global(home), {"mcp": {"global": {"type": "local", "command": ["run"]}}})
    _write(workspace / "opencode.json", {"mcp": {"project": {"type": "local", "command": ["run"]}}})

    result = _discover(home, workspace)

    assert [c.name for c in result.candidates] == ["project"]
    assert [(e.kind, e.field) for e in result.source_errors] == [(SourceErrorKind.UNPARSABLE, "")]


def test_project_servers_are_withheld_when_a_global_reference_could_disable_them(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    global_path = _global(home)
    _write(global_path, '{"mcp":{"svc":{"enabled":{env:MCP_ENABLED}}}}')
    _write(workspace / "opencode.json", {"mcp": {"svc": {"type": "local", "command": ["run"]}}})

    result = _discover(home, workspace, {"MCP_ENABLED": "false"})

    assert result.candidates == ()
    assert [(error.path, error.kind) for error in result.source_errors] == [
        (str(global_path), SourceErrorKind.UNREADABLE)
    ]


def test_project_servers_are_withheld_when_global_policy_has_invalid_utf8(tmp_path: Path) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    global_path = _global(home)
    global_path.parent.mkdir(parents=True)
    global_path.write_bytes(b'{"username":"\xff","permission":{"docs_*":"deny"}}')
    _write(workspace / "opencode.json", {"mcp": {"docs": {"type": "local", "command": ["run"]}}})

    result = _discover(home, workspace)

    assert result.candidates == ()
    assert [(error.path, error.kind) for error in result.source_errors] == [
        (str(global_path), SourceErrorKind.UNPARSABLE)
    ]


def test_project_servers_are_withheld_when_global_policy_exceeds_the_import_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, workspace = tmp_path / "home", tmp_path / "ws"
    global_path = _global(home)
    _write(global_path, json.dumps({"permission": {"project_*": "deny"}}) + " " * 256)
    _write(workspace / "opencode.json", {"mcp": {"project": {"type": "local", "command": ["run"]}}})
    monkeypatch.setattr("chrys.service.mcp.importers.base.MAX_SOURCE_BYTES", 256)

    result = _discover(home, workspace)

    assert result.candidates == ()
    assert [(error.path, error.kind) for error in result.source_errors] == [
        (str(global_path), SourceErrorKind.UNREADABLE)
    ]
