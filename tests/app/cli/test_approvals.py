# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Remembered grants remain manageable across sessions and feature toggles."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from chrys.app.cli import app, approvals
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.approval.grant_store import session_grants_path
from chrys.service.approval.reuse_binding import ApprovalReuseBinding
from chrys.service.state.store import JsonFileStateStore
from chrys.service.tools.builtins.shell import ShellTools


@pytest.fixture
def grants(tmp_path, monkeypatch):
    runtime = SessionEnvironment.capture("session-a", Workspace.from_cwd(str(tmp_path)))
    runtime = replace(runtime, platform=replace(runtime.platform, config_dir=tmp_path / "config"))
    monkeypatch.setattr(approvals, "get_platform", lambda: runtime.platform)
    monkeypatch.setattr(approvals, "bootstrap_runtime", create_autospec(approvals.bootstrap_runtime))
    tools = ShellTools(runtime).tools()
    binding = ApprovalReuseBinding(runtime, tools)
    candidate = binding.prepare(
        FunctionInvocationContext(tools[0], {"command": "npm run test"}), reusable=True
    ).candidate
    assert candidate is not None
    assert binding.service.remember(candidate, "EXACT_SESSION")
    assert binding.service.remember(candidate, "EXACT_PROJECT")
    return binding, candidate


def test_cli_lists_and_revokes_grants(grants, capsys, monkeypatch):
    binding, candidate = grants
    monkeypatch.setattr(sys, "argv", ["icode", "approvals", "list", "--json"])
    assert app.main() == 0
    rows = json.loads(capsys.readouterr().out)
    assert {row["scope"] for row in rows} == {"SESSION", "PROJECT"}
    for row in rows:
        assert approvals.main(["revoke", row["id"]]) == 0
    assert not binding.service.match(candidate)
    assert approvals.main(["revoke", rows[0]["id"]]) == 1


def test_cli_lists_commands_as_shell_text(grants, capsys):
    assert approvals.main(["list"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(line.endswith("  exact npm run test") for line in lines)


@pytest.mark.parametrize("argv", [[], ["--json"]])
def test_cli_lists_when_no_action_is_given(grants, capsys, argv):
    assert approvals.main(argv) == 0
    out = capsys.readouterr().out
    if argv:
        assert {row["scope"] for row in json.loads(out)} == {"SESSION", "PROJECT"}
    else:
        assert len(out.splitlines()) == 2


def test_cli_usage_names_the_singular_command(capsys):
    with pytest.raises(SystemExit):
        approvals.main(["--help"])
    assert capsys.readouterr().out.startswith("usage: icode approval ")


@pytest.mark.parametrize("filter_args", [["--session", "session-a"], ["--project"], ["--all"]])
def test_cli_selectively_clears_grants(grants, filter_args):
    binding, candidate = grants
    if filter_args == ["--project"]:
        filter_args = [*filter_args, candidate.context.project_id]
    assert approvals.main(["clear", *filter_args]) == 0
    remaining = binding.service.rules()
    assert [rule.scope for rule in remaining] == (["PROJECT"] if filter_args[0] == "--session" else [])


def test_cli_requires_an_explicit_clear_scope(grants):
    binding, _ = grants
    with pytest.raises(SystemExit) as error:
        approvals.main(["clear"])
    assert error.value.code == 2
    assert len(binding.service.rules()) == 2


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
    assert {rule.scope for rule in rebuilt.service.rules()} == {"SESSION", "PROJECT"}
    fork_id = store.fork_session(binding.session_id)
    assert not (store.session_dir(fork_id) / session_path.name).exists()
    await store.delete_session(binding.session_id)
    assert not session_path.exists()
    assert [rule.scope for rule in rebuilt.service.rules()] == ["PROJECT"]
    assert rebuilt.service.match(candidate)


def test_cli_reports_an_unreadable_store_instead_of_an_empty_one(grants, capsys):
    binding, _ = grants
    atomic_write_owner_only_text(binding.service.store.path, "{")
    assert approvals.main(["list"]) == 1
    streams = capsys.readouterr()
    assert "No remembered approvals." not in streams.out
    assert "could not be read" in streams.err
    assert "clear --all" in streams.err
    assert approvals.main(["clear", "--all"]) == 0
    out = capsys.readouterr().out
    assert approvals.main(["list"]) == 0
    assert "No remembered approvals." in capsys.readouterr().out or out
