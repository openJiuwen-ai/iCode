# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Remembered grants remain manageable across sessions and feature toggles."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from chrys.app.cli import app, approvals
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.approval.grant_store import session_grants_path
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.state.store import JsonFileStateStore
from chrys.service.tools.builtins.shell import ShellTools
from chrys.service.workspaces.registry import WorkspaceRegistry


@pytest.fixture
def grants(tmp_path, monkeypatch):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    runtime = replace(runtime, platform=replace(runtime.platform, config_dir=tmp_path / "config"))
    monkeypatch.setattr(approvals, "get_platform", lambda: runtime.platform)
    monkeypatch.setattr(
        approvals,
        "bootstrap_runtime",
        create_autospec(
            approvals.bootstrap_runtime,
            return_value=SimpleNamespace(
                warnings=[], loaded=SimpleNamespace(trust_conflicts=()), settings=SimpleNamespace(locale="en")
            ),
        ),
    )
    tools = ShellTools(runtime).tools()
    binding = ApprovalReuseBinding(runtime, tools)
    candidate = binding.prepare(
        FunctionInvocationContext(tools[0], {"command": "npm run test"}), reusable=True
    ).candidate
    assert candidate is not None
    assert binding.service.remember(candidate, "EXACT_SESSION")
    assert binding.service.remember(candidate, "EXACT_WORKSPACE")
    return binding, candidate


def test_cli_lists_and_revokes_grants(grants, capsys, monkeypatch):
    binding, candidate = grants
    monkeypatch.setattr(sys, "argv", ["icode", "approvals", "list", "--json"])
    assert app.main() == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["scope"] for row in rows} == {"SESSION", "WORKDIR"}
    for row in rows:
        assert approvals.main(["revoke", row["id"]]) == 0
    assert not binding.service.match(candidate)
    assert approvals.main(["revoke", rows[0]["id"]]) == 1


def test_cli_lists_commands_as_shell_text(grants, capsys):
    assert approvals.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(line.endswith("  exact npm run test") for line in lines)
    assert all("workdir=" in line for line in lines)
    assert any("WORKDIR" in line for line in lines)
    assert all("WORKSPACE" not in line for line in lines)


@pytest.mark.parametrize("argv", [[], ["--json"]])
def test_cli_lists_when_no_action_is_given(grants, capsys, argv):
    assert approvals.main(argv) == 0
    out = capsys.readouterr().out
    if argv:
        assert {row["scope"] for row in json.loads(out)} == {"SESSION", "WORKDIR"}
    else:
        assert len(out.splitlines()) == 2


def test_cli_usage_names_the_singular_command_and_public_filters(capsys):
    with pytest.raises(SystemExit):
        approvals.main(["--help"])
    help_text = capsys.readouterr().out
    assert help_text.startswith("usage: icode approval ")
    assert "--workspace" in help_text
    assert "--workdir DIR" in help_text
    assert "--project" not in help_text


@pytest.mark.parametrize("filter_args", [["--session", "session-a"], ["--workdir"], ["--all"]])
def test_cli_selectively_clears_grants(grants, filter_args):
    binding, candidate = grants
    if filter_args == ["--workdir"]:
        filter_args = [*filter_args, candidate.context.workdir]
    assert approvals.main(["clear", *filter_args]) == 0
    remaining = binding.service.rules()
    assert [rule.scope for rule in remaining] == (["WORKDIR"] if filter_args[0] == "--session" else [])


def test_cli_requires_an_explicit_clear_scope(grants):
    binding, _ = grants
    with pytest.raises(SystemExit) as error:
        approvals.main(["clear"])
    assert error.value.code == 2
    assert len(binding.service.rules()) == 2


@pytest.mark.parametrize("command", [["list"], ["clear"], ["clear", "--all"], ["revoke", "grant-id"]])
@pytest.mark.parametrize("flag", ["--workspace", "--workdir", "--project", "--session"])
def test_cli_rejects_empty_filters_before_startup(grants, capsys, command, flag):
    binding, _ = grants
    with pytest.raises(SystemExit) as error:
        approvals.main([*command, flag, ""])
    assert error.value.code == 2
    canonical = "--workdir" if flag == "--project" else flag
    assert f"{canonical} requires a non-empty" in capsys.readouterr().err
    approvals.bootstrap_runtime.assert_not_called()
    assert len(binding.service.rules()) == 2


@pytest.mark.parametrize("flag", ["--workspace", "--workdir", "--project", "--session"])
def test_cli_rejects_all_with_a_filter(grants, capsys, flag):
    binding, _ = grants
    with pytest.raises(SystemExit) as error:
        approvals.main(["clear", "--all", flag, "scope"])
    assert error.value.code == 2
    assert "--all is only valid with an unfiltered clear" in capsys.readouterr().err
    approvals.bootstrap_runtime.assert_not_called()
    assert len(binding.service.rules()) == 2


def test_cli_intersects_session_and_workdir_filters(grants, tmp_path, capsys):
    binding, candidate = grants
    directory = candidate.context.workdir
    session_filter = ["--session", "session-a", "--workdir"]
    assert approvals.main(["list", *session_filter, directory, "--json"]) == 0
    assert [row["scope"] for row in json.loads(capsys.readouterr().out)] == ["SESSION"]
    assert approvals.main(["clear", *session_filter, str(tmp_path / "elsewhere")]) == 0
    assert {rule.scope for rule in binding.service.rules()} == {"SESSION", "WORKDIR"}
    assert approvals.main(["clear", *session_filter, directory]) == 0
    assert [rule.scope for rule in binding.service.rules()] == ["WORKDIR"]


@pytest.mark.parametrize("command", [["list"], ["clear"], ["revoke", "missing-grant"]])
def test_cli_rejects_unknown_session_without_creating_it(grants, capsys, command):
    binding, _ = grants
    missing = session_grants_path(binding.runtime.platform.config_dir, "deadbee").parent
    assert not missing.exists()
    with pytest.raises(SystemExit) as error:
        approvals.main([*command, "--session", "deadbee"])
    assert error.value.code == 2
    assert "Session directory does not exist" in capsys.readouterr().err
    assert not missing.exists()


async def test_session_restore_fork_and_delete_grant_lifecycle(grants):
    binding, candidate = grants
    session_path = binding.service.session_store.path
    store = JsonFileStateStore(session_path.parent.parent)
    await store.save_session(binding.session_id, {"messages": [], "compressed_msgs": []})
    rebuilt = ApprovalReuseBinding(binding.runtime, [])
    assert {rule.scope for rule in rebuilt.service.rules()} == {"SESSION", "WORKDIR"}
    fork_id = store.fork_session(binding.session_id)
    assert not (store.session_dir(fork_id) / session_path.name).exists()
    await store.delete_session(binding.session_id)
    assert not session_path.exists()
    assert [rule.scope for rule in rebuilt.service.rules()] == ["WORKDIR"]
    assert rebuilt.service.match(candidate)


def test_workdir_filter_accepts_removed_directories(grants):
    binding, candidate = grants
    removed = str(binding.runtime.platform.config_dir / "removed")
    removed_candidate = replace(candidate, context=replace(candidate.context, workdir=removed))
    assert binding.service.remember(removed_candidate, "EXACT_WORKSPACE")
    assert approvals.main(["clear", "--workdir", removed]) == 0
    assert not binding.service.match(removed_candidate)


@pytest.mark.parametrize("flag", ["--project", "--workdir"])
def test_directory_filter_lists_revokes_and_clears_session_and_shared_grants(grants, capsys, tmp_path, flag):
    binding, candidate = grants
    directory = candidate.context.workdir
    other_binding = ApprovalReuseBinding(replace(binding.runtime, session_id="session-b"), [])
    other = replace(
        candidate,
        context=replace(candidate.context, session_id=other_binding.session_id, workdir=str(tmp_path / "other")),
    )
    assert other_binding.service.remember(other, "EXACT_SESSION")
    assert approvals.main(["list", flag, directory, "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["scope"] for row in rows} == {"WORKDIR", "SESSION"}
    assert len(rows) == 2
    (shared,) = [row for row in rows if row["scope"] == "WORKDIR"]
    assert approvals.main(["revoke", shared["id"], flag, other.context.workdir]) == 1
    assert approvals.main(["revoke", shared["id"], flag, directory]) == 0
    assert [rule.scope for rule in binding.service.rules()] == ["SESSION"]
    assert approvals.main(["clear", flag, directory]) == 0
    assert not binding.service.rules()
    [remaining] = other_binding.service.rules()
    assert remaining.scope == "SESSION" and remaining.workdir == other.context.workdir


@pytest.mark.parametrize("flag", ["--project", "--workdir", "--session"])
def test_workspace_filter_excludes_directory_and_session_filters(grants, capsys, flag):
    with pytest.raises(SystemExit) as error:
        approvals.main(["list", flag, "scope", "--workspace", "Team"])
    assert error.value.code == 2
    assert "--workspace cannot be combined" in capsys.readouterr().err
    approvals.bootstrap_runtime.assert_not_called()


@pytest.mark.parametrize("selector", ["name", "prefix", "id", "deleted", "unreadable"])
def test_workspace_filter_leaves_directory_and_session_grants(grants, capsys, tmp_path, selector):
    binding, candidate = grants
    registry = WorkspaceRegistry()
    primary = tmp_path / "primary"
    primary.mkdir()
    workspace = registry.create("Team", str(primary))
    workspace_grant = replace(candidate, context=replace(candidate.context, workspace_id=workspace.id))
    assert binding.service.remember(workspace_grant, "EXACT_WORKSPACE")
    primary.rmdir()
    if selector == "deleted":
        registry.delete(workspace.id)
    if selector == "unreadable":
        registry.path.write_text("broken", encoding="utf-8")
    value = {"name": "tEaM", "prefix": workspace.id[:12]}.get(selector, workspace.id)
    assert approvals.main(["list", "--workspace", value, "--json"]) == 0
    [row] = json.loads(capsys.readouterr().out)
    assert (row["scope"], row["scope_id"]) == ("WORKSPACE", workspace.id)
    assert "workdir" not in row
    assert approvals.main(["clear", "--workspace", value]) == 0
    assert {rule.scope for rule in binding.service.rules()} == {"WORKDIR", "SESSION"}


def test_workspace_filter_reports_unknown_name_and_rejects_directories(grants, capsys):
    _, candidate = grants
    approvals.bootstrap_runtime.return_value.settings.locale = "zh-Hans"
    for name in ("Missing", candidate.context.workdir):
        assert approvals.main(["list", "--workspace", name]) == 1
        assert "该工作区已不存在" in capsys.readouterr().err


def test_workspace_filter_reports_unreadable_registry_for_names(grants, capsys):
    registry = WorkspaceRegistry()
    registry.path.parent.mkdir(parents=True, exist_ok=True)
    registry.path.write_text("broken", encoding="utf-8")
    assert approvals.main(["list", "--workspace", "Team", "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err)["code"] == "workspace_registry_unreadable"


def test_cli_reports_migration_once_on_stderr(capsys, monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    settings = get_platform().config_dir / "settings.yaml"
    atomic_write_owner_only_text(settings, "project.hooks_enabled: true\n")
    assert approvals.main(["list", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    [notice] = output.err.splitlines()
    assert json.loads(notice)["code"] == "workdir_data_migrated"
    assert approvals.main(["list", "--json"]) == 0
    assert capsys.readouterr().err == ""


def test_cli_reports_conflict_when_trust_settings_cannot_be_migrated(capsys, monkeypatch):
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    config = get_platform().config_dir
    atomic_write_owner_only_text(
        config / "settings.yaml", "project.hooks_enabled: false\nworkdir.hooks_enabled: true\n"
    )
    atomic_write_owner_only_text(config / "backups", "Backup directory unavailable\n")

    assert approvals.main(["list", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    (conflict,) = [json.loads(line) for line in output.err.splitlines()]
    assert conflict["code"] == "workdir_trust_conflict"
    assert "workdir.hooks_enabled" in conflict["warning"]
