# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace notices reach headless output without owning the run."""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import replace
from functools import partial
from pathlib import Path

import pytest

from chrys.app.cli import run as run_cli
from chrys.foundation.events.types import Warning
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Message
from chrys.orchestration.session_host import ChrysSessionHost
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.workspaces import registry as registry_module
from tests.app.cli.test_run import FakeHost, _patch_runtime, _prepared_runtime
from tests.orchestration import _session_host_helpers as host_helpers
from tests.support.workspace_engine import mock_headless_workspace_restore, patch_headless_host
from tests.support.workspace_registry import (
    choose_before_restore_lock,
    make_workdir_alias,
    make_workdirs,
    move_profile_to_legacy_path,
    registry_disk_full,
    retarget_workdir_alias,
    save_chat_session,
)
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry
from tests.support.workspace_registry import workspace_store as workspace_store

TARGET = "0f1e2d3c-4b5a-4968-8776-655443322110"
MISSING = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


@pytest.mark.parametrize("where", ["start", "turn"])
@pytest.mark.parametrize("code", ["workspace_left", "workspace_membership_write_failed"])
def test_run_json_prints_workspace_notices_once(monkeypatch, capsys, where, code) -> None:
    """O7/C11: --json prints a workspace notice from a restore's startup or from the turn once, and nothing else."""
    _patch_runtime(monkeypatch)
    events = [
        Warning(code=code, message="workspace warning"),
        Warning(code="other_warning", message="unrelated warning"),
    ]
    argv = ["hello", "-a", "Headless", "--json"]
    if where == "start":
        monkeypatch.setattr(FakeHost, "start_events", events)
        argv += ["-s", TARGET]
    else:

        class WorkspaceHost(FakeHost):
            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.engine.workspace = Workspace("cwd", id="team", name="Team")

        monkeypatch.setattr(run_cli, "ChrysSessionHost", WorkspaceHost)
        monkeypatch.setattr(FakeHost, "run_events", events)
    assert run_cli.main(argv) == 0

    assert [json.loads(line) for line in capsys.readouterr().err.splitlines()] == [
        {"warning": "workspace warning", "code": code}
    ]


@pytest.mark.parametrize("destination", ["alias", "member"])
async def test_run_session_cwd_keeps_workspace_only_at_same_physical_primary(
    registry, store, monkeypatch, tmp_path, destination
):
    from tests.support.symlinks import symlink_or_skip

    primary, member = make_workdirs(tmp_path, "primary", "member")
    child = primary / "child"
    child.mkdir()
    alias = tmp_path / "alias"
    if destination == "alias":
        symlink_or_skip(alias, primary, target_is_directory=True)
    definition = registry.create("Team", str(primary), [str(member)])
    await store.save_session(TARGET, {"messages": []}, primary_cwd=str(primary))
    registry.set_session_workspace(TARGET, definition.id)
    settings, models = host_helpers._make_settings_and_models()
    host_helpers._patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])], [])
    target = {"primary": primary, "alias": alias, "child": child, "member": member}[destination]
    host = ChrysSessionHost(
        profile_name="Headless",
        session_id=TARGET,
        surface=SessionSurface.CLI,
        cwd=str(target.resolve()),
        settings=settings,
        model_registry=models,
        agent_registry=host_helpers._make_agent_registry(host_helpers._make_profile()),
        state_store=store,
    )
    try:
        await host.start()
        expected_id = definition.id if destination in {"primary", "alias"} else None
        assert host.engine.session.workspace_id == expected_id
        assert registry.snapshot().workspace_id_for_session(TARGET) == expected_id
        assert host.engine.workspace.primary_cwd == str(target.resolve())
        if destination in {"primary", "alias"}:
            async with asyncio.timeout(5):
                await host.restore_session(host.session_id)
            assert host.engine.session.workspace_id == definition.id
    finally:
        await host.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 directory creation strips trailing spaces")
@pytest.mark.parametrize("registered", [False, True])
def test_run_restore_preserves_trailing_space_in_validated_cwd(registry, store, monkeypatch, tmp_path, registered):
    _, primary = make_workdirs(tmp_path, "project", "project ")
    workspace_id = None
    if registered:
        workspace_id = registry.create("Team", str(primary)).id
        registry.set_session_workspace(TARGET, workspace_id)
    asyncio.run(save_chat_session(store, TARGET, primary, profile="Headless"))
    client = mock_headless_workspace_restore(monkeypatch, store)
    monkeypatch.chdir(tmp_path)

    assert run_cli.main(["hello", "-a", "Headless", "-s", TARGET, "-C", str(primary), "--json"]) == 0

    messages, _ = client.call_history[0]
    assert any(f"Working directory: {primary}\n" in message.text for message in messages)
    assert registry.snapshot().workspace_id_for_session(TARGET) == workspace_id


@pytest.mark.parametrize("write_fails", [False, True])
@pytest.mark.parametrize("restore_error", ["missing", "build"])
def test_run_does_not_execute_when_workspace_session_restore_fails(
    registry, store, monkeypatch, tmp_path, capsys, write_fails, restore_error
):
    primary = tmp_path / "primary"
    primary.mkdir()
    workspace = registry.create("Team", str(primary))
    monkeypatch.chdir(tmp_path)
    if restore_error == "build":
        asyncio.run(
            save_chat_session(
                store, MISSING, tmp_path, profile="Headless", messages=[Message("user", ["saved history"])]
            )
        )
        checkpoint = store.session_dir(MISSING) / "session.json"
        checkpoint_bytes = checkpoint.read_bytes()
    settings, models = host_helpers._make_settings_and_models()
    host_helpers._patch_runtime(monkeypatch, [MockChatClient(responses=[MockResponse(text="done")])], [])
    profiles = AgentProfileRegistry()
    profile_path, legacy_path, original = move_profile_to_legacy_path(profiles, host_helpers._make_profile())
    if restore_error == "build":
        from chrys.orchestration.engine.build import builder

        create = builder.create_client
        calls = 0

        async def fail_restore(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("restore build failed")
            return await create(*args, **kwargs)

        monkeypatch.setattr(builder, "create_client", fail_restore)
    patch_headless_host(monkeypatch, profiles, models, store)

    def prepare(*, workspace_cwd):
        assert Path(workspace_cwd) == Path.cwd() == primary
        return _prepared_runtime(settings)

    monkeypatch.setattr(run_cli.headless, "prepare_runtime", prepare)
    if write_fails:
        monkeypatch.setattr(registry_module, "atomic_write_text", registry_disk_full)

    rc = run_cli.main(["hello", "-a", "Headless", "--workspace", "Team", "-s", MISSING, "--json"])

    out = capsys.readouterr()
    assert rc == 1
    assert out.out == ""
    if restore_error == "build":
        assert checkpoint.read_bytes() == checkpoint_bytes
        error = json.loads(out.err)
        if write_fails:
            assert error["code"] == "workspace_registry_write_failed"
            assert "registry disk full" in error["error"]
            assert not registry.snapshot().sessions
            assert calls == 0
        else:
            assert error["code"] == "workspace_membership_saved_open_failed"
            assert "restore build failed" in error["error"]
            assert "Reopening uses Team" in error["error"]
            assert registry.snapshot().workspace_id_for_session(MISSING) == workspace.id
            assert calls == 1
    else:
        assert json.loads(out.err) == {"error": f"Session not found: {MISSING}", "code": "session_not_found"}
        assert not registry.snapshot().sessions
    if write_fails or restore_error == "missing":
        assert legacy_path.read_bytes() == original
        assert not profile_path.exists()


def test_run_finish_load_failure_keeps_workspace_restore_checkpoint(registry, store, monkeypatch, tmp_path, capsys):
    from chrys.orchestration.engine.loader import AgentLoader

    primary, previous, extra = make_workdirs(tmp_path, "primary", "previous", "extra")
    workspace = registry.create("Team", str(primary))
    asyncio.run(save_chat_session(store, TARGET, previous, profile="Headless", workdirs=[extra]))
    original = (store.session_dir(TARGET) / "session.json").read_bytes()
    client = mock_headless_workspace_restore(monkeypatch, store)
    monkeypatch.chdir(tmp_path)

    async def fail_finish_load(self, profile, *, operation, trajectory, old_hook_manager=None):
        assert operation == "restore"
        assert self._current.require_loaded() is not None
        raise RuntimeError("finish load failed")

    monkeypatch.setattr(AgentLoader, "finish_load", fail_finish_load)
    assert run_cli.main(["hello", "-a", "Headless", "--workspace", "Team", "-s", TARGET, "--json"]) == 1

    out = capsys.readouterr()
    assert out.out == ""
    assert "finish load failed" in json.loads(out.err)["error"]
    assert registry.snapshot().workspace_id_for_session(TARGET) == workspace.id
    assert not client.call_history
    assert (store.session_dir(TARGET) / "session.json").read_bytes() == original


@pytest.mark.parametrize(
    "failure",
    [
        "workspace_not_found",
        "workspace_registry_unreadable",
        "workspace_primary_missing",
        "workspace_selector_ambiguous",
    ],
)
def test_run_reports_workspace_launch_error_code(registry, monkeypatch, tmp_path, capsys, failure):
    _patch_runtime(monkeypatch)
    monkeypatch.chdir(tmp_path)
    primary = tmp_path / "primary"
    primary.mkdir()
    if failure != "workspace_not_found":
        registry.create("Team", str(primary))
    if failure == "workspace_registry_unreadable":
        registry.path.write_text("broken", encoding="utf-8")
    elif failure == "workspace_primary_missing":
        primary.rmdir()

    selector = "Team"
    if failure == "workspace_selector_ambiguous":
        other = tmp_path / "other"
        other.mkdir()
        registry.create("Other", str(other))
        selector = "ws_"
    assert run_cli.main(["hello", "-a", "Headless", "--workspace", selector, "-s", MISSING, "--json"]) == 1

    out = capsys.readouterr()
    assert out.out == ""
    assert json.loads(out.err)["code"] == failure
    if failure == "workspace_not_found":
        # A mistyped selector is the user's own input, so the message names it.
        assert json.loads(out.err)["error"] == f"No workspace matches {selector}."
    assert not FakeHost.instances
    assert Path.cwd() == tmp_path


def test_startup_symlink_retarget_after_chdir_keeps_the_task_working_directory(registry, store, monkeypatch, tmp_path):

    first, second, link = make_workdir_alias(tmp_path, "first", "second", "primary")
    (first / "task.txt").write_text("first task", encoding="utf-8")
    definition = registry.create("Team", str(link))
    monkeypatch.chdir(tmp_path)
    asyncio.run(store.save_session(TARGET, {"messages": []}, agent_profile="Headless", primary_cwd=str(link)))
    client = mock_headless_workspace_restore(monkeypatch, store)
    prompt = run_cli._resolve_prompt

    def retarget_after_chdir(args):
        retarget_workdir_alias(link, second)
        return prompt(args)

    monkeypatch.setattr(run_cli, "_resolve_prompt", retarget_after_chdir)
    result = run_cli.main(
        ["-a", "Headless", "--workspace", "Team", "-s", TARGET.upper(), "--task", "task.txt", "--json"]
    )
    assert result == 0
    messages, _ = client.call_history[0]
    assert any("first task" in message.text for message in messages)
    assert any(f"Working directory: {first.resolve()}" in message.text for message in messages)
    assert registry.snapshot().workspace_id_for_session(TARGET) == definition.id


def test_run_workspace_moves_saved_session_and_reports_selection(registry, store, monkeypatch, tmp_path, capsys):
    source, destination = make_workdirs(tmp_path, "source", "destination")
    first = registry.create("First", str(source))
    second = registry.create("Second", str(destination))
    sid = TARGET
    asyncio.run(store.save_session(sid, {"messages": []}, agent_profile="Headless", primary_cwd=str(source)))
    registry.set_session_workspace(sid, first.id)
    mock_headless_workspace_restore(monkeypatch, store)
    monkeypatch.chdir(source)

    assert run_cli.main(["-a", "Headless", "--workspace", second.id, "-s", sid, "continue", "--json"]) == 0

    output = capsys.readouterr()
    assert json.loads(output.out)["session_id"] == sid
    warnings = [json.loads(line) for line in output.err.splitlines() if line.startswith("{")]
    (selected,) = [warning["warning"] for warning in warnings if warning.get("code") == "workspace_selected"]
    assert "First" in selected and "Second" in selected
    assert registry.snapshot().workspace_id_for_session(sid) == second.id


def test_run_keeps_a_newer_selection_during_implicit_leave(registry, store, monkeypatch, tmp_path, capsys):
    from chrys.service.state.locks import ActiveSessionGuard

    b, c, reference = make_workdirs(tmp_path, "b", "c", "reference")
    original = registry.create("B", str(b))
    latest = registry.create("C", str(c), folders=[str(reference)])
    sid = TARGET
    asyncio.run(store.save_session(sid, {"messages": []}, agent_profile="Headless", primary_cwd=str(b)))
    registry.set_session_workspace(sid, original.id)
    client = mock_headless_workspace_restore(monkeypatch, store)
    monkeypatch.chdir(tmp_path)
    acquire = ActiveSessionGuard.acquire_for_restore

    def acquire_after_move(guard, session_id, **kwargs):
        assert session_id == sid
        choose_before_restore_lock(store, registry, sid, latest.id)
        return acquire(guard, session_id, **kwargs)

    monkeypatch.setattr(ActiveSessionGuard, "acquire_for_restore", acquire_after_move)
    assert run_cli.main(["hello", "-a", "Headless", "-s", sid, "-C", str(c), "--json"]) == 0
    assert registry.snapshot().workspace_id_for_session(sid) == latest.id
    output = capsys.readouterr()
    assert "workspace_left" in output.err
    messages, _ = client.call_history[0]
    assert not any(str(reference) in message.text for message in messages)


def test_run_with_workspace_uses_selected_settings_hooks_and_relative_task(registry, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("chrys.orchestration.startup.configure_utf8_stdio", lambda: None)
    from chrys.foundation.config.spec import Source
    from tests.support.workspace_registry import write_launch_settings

    launch, primary = make_workdirs(tmp_path, "launch", "primary")
    report = tmp_path / "hook-cwd.txt"
    write_launch_settings(primary, report, event="session_start")
    (primary / "task.txt").write_text("prompt from workspace", encoding="utf-8")
    (launch / "task.txt").write_text("wrong prompt", encoding="utf-8")
    workspace = registry.create("Team", str(primary))
    monkeypatch.chdir(launch)
    _, models = host_helpers._make_settings_and_models()
    profiles = host_helpers._make_agent_registry(host_helpers._make_profile())
    client = MockChatClient(responses=[MockResponse(text="done")])
    host_helpers._patch_runtime(monkeypatch, [client], [])

    def make_host(**kwargs):
        loaded = kwargs["loaded_settings"]
        assert loaded.settings.warn_threshold_pct == 0.73
        assert loaded.source_for("context.warn_threshold_pct").layer == Source.WORKDIR
        settings = replace(loaded.settings, model_profile="mock-profile")
        return ChrysSessionHost(
            **(kwargs | {"loaded_settings": replace(loaded, settings=settings), "model_registry": models}),
            agent_registry=profiles,
        )

    monkeypatch.setattr(run_cli, "ChrysSessionHost", make_host)
    assert run_cli.main(["--workspace", "Team", "-a", "Headless", "--task", "task.txt", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["result"] == "done"
    assert registry.snapshot().workspace_id_for_session(result["session_id"]) == workspace.id
    assert Path(report.read_text(encoding="utf-8")).samefile(primary)
    assert "prompt from workspace" in client.call_history[0][0][-1].text


@pytest.mark.parametrize("entry", ["tui", "run"])
def test_workspace_and_workdir_are_mutually_exclusive(entry, monkeypatch, tmp_path, capsys):
    args = ["--workspace", "Team", "-C", str(tmp_path)]
    if entry == "tui":
        from chrys.app.tui import app as tui_app

        monkeypatch.setattr("chrys.orchestration.startup.configure_utf8_stdio", lambda: None)
        monkeypatch.setattr(sys, "argv", ["icode", "-a", "Headless", "-m", "mock-profile", *args])
        launch, program = tui_app.main, "icode"
    else:
        launch, program = partial(run_cli.main, ["hello", *args]), "icode run"

    with pytest.raises(SystemExit) as failure:
        launch()
    assert failure.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1] == (
        f"{program}: error: argument -C: not allowed with argument --workspace"
    )
