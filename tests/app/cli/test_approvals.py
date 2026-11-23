# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Remembered grants remain manageable across sessions and feature toggles."""

from __future__ import annotations

import errno
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

import chrys.service.approval.grant_store as grant_store
from chrys.app.cli import app, approvals
from chrys.foundation.config.settings import resolve_sessions_dir
from chrys.foundation.models.session_env import SessionEnvironment
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import SecureFileError, atomic_write_owner_only_text
from chrys.kernel.middleware import FunctionInvocationContext
from chrys.service.approval.grant_store import GRANTS_FILE, ApprovalGrantStore, session_grants_path
from chrys.service.approval.reuse import ApprovalReuseService
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


def session_store(config_dir: Path, session_id: str) -> ApprovalGrantStore:
    """A store for one session folder, created on demand and separate from the fixture's."""
    path = session_grants_path(config_dir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return ApprovalGrantStore(path)


def remember_session_grant(grants, session_id: str) -> ApprovalGrantStore:
    """Remember one session grant for *session_id* and return the store holding it."""
    binding, candidate = grants
    store = session_store(binding.runtime.platform.config_dir, session_id)
    scoped = replace(candidate, context=replace(candidate.context, session_id=session_id))
    assert ApprovalReuseService(binding.service.store, store).remember(scoped, "EXACT_SESSION")
    return store


def stored_scopes(store: ApprovalGrantStore) -> list[str]:
    """The scopes a store still holds; an unreadable store reads as empty, as the CLI does."""
    return [row["scope"] for row in store.load()]


def session_folder(binding, name: str) -> Path:
    """The on-disk session folder called *name*, which need not be a session ID."""
    return resolve_sessions_dir(binding.runtime.platform.config_dir, create=False) / name


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
    # CLI output stays English whatever the display language.
    approvals.bootstrap_runtime.return_value.settings.locale = "zh-Hans"
    for name in ("Missing", candidate.context.workdir):
        assert approvals.main(["list", "--workspace", name]) == 1
        assert f"No workspace matches {name}." in capsys.readouterr().err


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


def test_cli_reports_an_unreadable_store_instead_of_an_empty_one(grants, capsys):
    binding, _ = grants
    atomic_write_owner_only_text(binding.service.store.path, "{")
    assert approvals.main(["list"]) == 1
    streams = capsys.readouterr()
    assert "No remembered approvals." not in streams.out
    assert "could not be read" in streams.err
    assert "clear --all" in streams.err
    assert approvals.main(["clear", "--all"]) == 0
    capsys.readouterr()  # consume the clear output so it cannot satisfy the next assertion
    assert approvals.main(["list"]) == 0
    assert "No remembered approvals." in capsys.readouterr().out


def test_cli_scopes_the_recovery_hint_for_a_broken_session_store(grants, capsys):
    binding, _ = grants
    healthy = remember_session_grant(grants, "abcdef012345")
    # A folder named after a full ID plus a suffix: `--session` strips the dash and
    # truncates to twelve characters, so the hint would address the healthy session
    # and leave the broken folder untouched.
    broken = session_folder(binding, "abcdef012345-backup")
    broken.mkdir(parents=True)
    atomic_write_owner_only_text(broken / GRANTS_FILE, "{")

    assert approvals.main(["list"]) == 1
    streams = capsys.readouterr()
    assert "No remembered approvals." not in streams.out
    assert "could not be read" in streams.err
    assert "clear --session abcdef012345" not in streams.err
    assert str(broken / GRANTS_FILE) in streams.err
    assert stored_scopes(healthy) == ["SESSION"]


def test_cli_hint_for_a_round_tripping_session_name_repairs_that_store(grants, capsys):
    binding, _ = grants
    config_dir = binding.runtime.platform.config_dir
    # "sessiona" is the short folder of the fixture's "session-a", so the hint maps back.
    broken = session_grants_path(config_dir, "session-a")
    atomic_write_owner_only_text(broken, "{")
    other = remember_session_grant(grants, "abcdef012345")

    assert approvals.main(["list"]) == 1
    assert "clear --session sessiona" in capsys.readouterr().err
    assert approvals.main(["clear", "--session", "sessiona"]) == 0
    assert capsys.readouterr().out == "Cleared selected grants.\n"
    assert stored_scopes(ApprovalGrantStore(broken)) == []
    assert stored_scopes(other) == ["SESSION"]
    assert [rule.scope for rule in binding.service.rules()] == ["WORKDIR"]


@pytest.mark.parametrize("action", ["list", "clear"])
def test_cli_rejects_a_session_folder_that_maps_to_another_store(grants, capsys, action):
    binding, _ = grants
    healthy = remember_session_grant(grants, "abcdef012345")
    session_folder(binding, "abcdef012345-backup").mkdir(parents=True)
    with pytest.raises(SystemExit) as error:
        approvals.main([action, "--session", "abcdef012345-backup"])
    assert error.value.code == 2
    assert "Session ID is ambiguous" in capsys.readouterr().err
    assert stored_scopes(healthy) == ["SESSION"]


def test_cli_keeps_session_grants_when_a_shared_store_holds_a_directory(grants, capsys):
    binding, _ = grants
    healthy = remember_session_grant(grants, "abcdef012345")
    shared = binding.runtime.platform.config_dir / GRANTS_FILE
    shared.unlink()
    shared.mkdir()

    assert approvals.main(["list"]) == 1
    streams = capsys.readouterr()
    assert str(shared) in streams.err
    assert "not a regular file" in streams.err
    assert "clear --all" not in streams.err

    assert approvals.main(["clear", "--all"]) == 1
    streams = capsys.readouterr()
    assert streams.out == ""
    assert "could not be cleared" in streams.err
    assert str(shared) in streams.err
    assert shared.is_dir()
    assert stored_scopes(healthy) == ["SESSION"]


def test_cli_json_diagnostics_stay_parseable_when_a_store_is_unreadable(grants, capsys):
    binding, _ = grants
    shared = binding.runtime.platform.config_dir / GRANTS_FILE
    atomic_write_owner_only_text(shared, "{")

    assert approvals.main(["list", "--json"]) == 1
    streams = capsys.readouterr()
    assert [row["scope"] for row in json.loads(streams.out)] == ["SESSION"]
    diagnostics = [json.loads(line) for line in streams.err.splitlines() if line.strip()]
    assert [record["code"] for record in diagnostics] == ["approval_store_unreadable"]
    assert str(shared) in diagnostics[0]["error"]


def test_cli_reports_an_incomplete_revoke_lookup_as_json(grants, capsys):
    binding, _ = grants
    shared = binding.runtime.platform.config_dir / GRANTS_FILE
    atomic_write_owner_only_text(shared, "{")

    assert approvals.main(["revoke", "0" * 32, "--json"]) == 1
    diagnostics = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.strip()]
    # "Not found" would be a guess while a store that may hold the id went unread.
    assert [record["code"] for record in diagnostics] == [
        "approval_store_unreadable",
        "approval_grant_lookup_incomplete",
    ]


def test_cli_reports_an_unknown_revoke_id_as_json(grants, capsys):
    assert approvals.main(["revoke", "0" * 32, "--json"]) == 1
    out = capsys.readouterr()
    assert out.out == ""
    [record] = [json.loads(line) for line in out.err.splitlines() if line.strip()]
    assert record["code"] == "approval_grant_not_found"


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_cli_does_not_suggest_a_clear_that_cannot_be_written(grants, capsys):
    binding, _ = grants
    shared = binding.runtime.platform.config_dir / GRANTS_FILE
    atomic_write_owner_only_text(shared, "{")
    # A lock file already exists, so the failure below is the parent's permissions,
    # not the inability to create the sidecar.
    shared.with_name(shared.name + ".lock").write_bytes(b"")
    directory = shared.parent
    directory.chmod(0o500)
    try:
        assert approvals.main(["list"]) == 1
        streams = capsys.readouterr()
        assert "not writable" in streams.err or "cannot be opened" in streams.err
        assert "clear --all" not in streams.err

        assert approvals.main(["clear", "--all"]) == 1
        streams = capsys.readouterr()
        assert "could not be cleared:" in streams.err
        assert str(shared) in streams.err
        assert "Some approval stores could not be cleared." not in streams.err
    finally:
        directory.chmod(0o700)


@pytest.mark.skipif(os.name == "nt", reason="Windows file names cannot contain a newline")
def test_cli_keeps_a_hostile_session_folder_name_on_one_line(grants, capsys):
    from chrys.app.cli.display import one_line

    assert "\n" not in one_line("evil\nfolder")
    binding, _ = grants
    broken = session_folder(binding, "evil\nfolder")
    broken.mkdir(parents=True)
    atomic_write_owner_only_text(broken / GRANTS_FILE, "{")

    assert approvals.main(["list"]) == 1
    # The log line the library writes itself is not part of the diagnostic block.
    lines = [line for line in capsys.readouterr().err.splitlines() if line.strip() and "no grants reused" not in line]
    assert len(lines) == 3
    assert all("evil" in line for line in lines[1:])


def test_windows_clear_target_verifies_the_owner_sid(grants, monkeypatch):
    binding, _ = grants
    store = binding.service.store
    opened: list[Path] = []

    def open_verified(path: Path) -> None:
        opened.append(path)

    monkeypatch.setattr(grant_store, "verify_owner_identity_path", open_verified)
    store._verify_windows_target(0)
    assert opened == [store.path]

    def deny(path: Path) -> None:
        raise SecureFileError(errno.EACCES, "denied")

    monkeypatch.setattr(grant_store, "verify_owner_identity_path", deny)
    with pytest.raises(SecureFileError, match="owner could not be verified"):
        store._verify_windows_target(0)

    # A directory is rejected on its attributes alone: opening it would only fail later.
    monkeypatch.setattr(grant_store, "verify_owner_identity_path", pytest.fail)
    with pytest.raises(SecureFileError, match="not a regular file"):
        store._verify_windows_target(0x10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
@pytest.mark.parametrize(("data", "cleared"), [('{"version": 1, "rules": []}\n', True), ("{", False)])
def test_clearing_respects_a_read_only_directory_with_an_existing_lock(grants, data, cleared):
    binding, _ = grants
    store = binding.service.store
    atomic_write_owner_only_text(store.path, data)
    store.lock_path.write_bytes(b"")
    before = store.path.read_bytes()
    directory = store.path.parent
    directory.chmod(0o500)
    try:
        # An empty store needs no write, so a read-only parent and an existing lock
        # cannot fail it; a broken one must still report the failure.
        assert store.clear() is cleared
        assert store.path.read_bytes() == before
    finally:
        directory.chmod(0o700)
