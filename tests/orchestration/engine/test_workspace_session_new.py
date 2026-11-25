# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit SessionNew keeps the outgoing session until target validation commits."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.events.types import (
    Error,
    SessionClear,
    SessionFork,
    SessionForked,
    SessionNew,
    SessionReady,
    SessionRestore,
    Warning,
)
from chrys.foundation.models.workspace import WorkspaceConfirmation
from chrys.orchestration.engine import session_lifecycle
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.profiles.agents.schema import MCPServerConfig
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.paths import MissingWorkspacePrimary
from chrys.service.workspaces.registry import WorkspaceRegistryError
from tests.support.event_capture import capture_event_sequence
from tests.support.rollback_spec import ask
from tests.support.waiting import wait_for
from tests.support.workspace_engine import workspace_case as workspace_case
from tests.support.workspace_registry import make_workdir_alias, make_workdirs, retarget_workdir_alias


async def _session_with_options(workspace_case, tmp_path):
    a, b, d = make_workdirs(tmp_path, "a", "b", "d")
    registry = workspace_case.registry
    first = registry.create("A", str(a))
    target = registry.create("B", str(b))
    engine = await workspace_case(a, workspace_id=first.id)
    await ask(engine, "A has history")
    return engine, first, target, d


@pytest.mark.parametrize("action", ["exit", "new", "reset"])
async def test_empty_workspace_session_membership_follows_directory_cleanup(workspace_case, tmp_path, action):
    registry = workspace_case.registry
    store = JsonFileStateStore(tmp_path / "paired", workspace_registry=registry)
    workspace = registry.create("Team", str(tmp_path))
    engine = await workspace_case(tmp_path, workspace_id=workspace.id, store=store)
    session_id = engine.session_id
    session_dir = store.session_dir(session_id)
    if action != "exit":
        session_dir.mkdir(exist_ok=True)
    assert registry.snapshot().workspace_id_for_session(session_id) == workspace.id

    if action == "reset":
        assert await engine.lifecycle.reset_session_to_welcome(session_id)
        assert engine.session_id == session_id
        assert registry.snapshot().workspace_id_for_session(session_id) == workspace.id
        return
    if action == "new":
        await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)
        assert not session_dir.exists()
        assert not registry.snapshot().maps_session(session_id)
        session_id = engine.session_id
        session_dir = store.session_dir(session_id)
        session_dir.mkdir(exist_ok=True)
        assert registry.snapshot().workspace_id_for_session(session_id) == workspace.id

    await engine.shutdown()
    assert not session_dir.exists()
    assert not registry.snapshot().maps_session(session_id)


async def test_empty_session_cleanup_removes_membership_from_its_own_registry(workspace_case, tmp_path):
    engine, _ = await workspace_case.start_selected(tmp_path)
    session_id = engine.session_id
    session_dir = workspace_case.store.session_dir(session_id)
    session_dir.mkdir(exist_ok=True)

    await engine.shutdown()

    assert not session_dir.exists()
    assert not workspace_case.registry.snapshot().maps_session(session_id)


@pytest.mark.parametrize("failure", ["missing", "broken", "settings", "replaced"])
async def test_explicit_new_refuses_before_committing(workspace_case, tmp_path, monkeypatch, failure):
    engine, first, target, directory = await _session_with_options(workspace_case, tmp_path)
    registry = workspace_case.registry
    event = SessionNew(workspace_id=target.id)
    if failure == "missing":
        (tmp_path / "b").rmdir()
    elif failure == "broken":
        registry.path.write_text("broken", encoding="utf-8")
    elif failure == "settings":
        monkeypatch.setattr(
            session_lifecycle,
            "load_settings",
            create_autospec(load_settings, side_effect=OSError("settings unavailable")),
        )
    else:
        proof = WorkspaceConfirmation.capture(first.id, str(directory))
        event = SessionNew(start_unspecified=True, primary_cwd=str(directory), confirmation=proof)
        prepare = engine.permits.prepare_session_transition

        async def replace_before_permit(*args, **kwargs):
            directory.rename(tmp_path / "original")
            directory.mkdir()
            return await prepare(*args, **kwargs)

        monkeypatch.setattr(engine.permits, "prepare_session_transition", replace_before_permit)
    before = (engine.session_id, engine.session_generation, engine.session.workspace_plan, engine.current.loaded)
    async with capture_event_sequence(engine.event_bus, Error, SessionReady) as events:
        if failure == "replaced":
            await engine.event_bus.publish(event)
        else:
            with pytest.raises((OSError, ValueError)):
                await engine.event_bus.publish(event, raise_handler_errors=True)
    assert (
        engine.session_id,
        engine.session_generation,
        engine.session.workspace_plan,
        engine.current.loaded,
    ) == before
    assert engine.workspace.id == first.id
    assert [event.code for event in events if isinstance(event, Error)] == ["session_new_failed"]
    assert not [event for event in events if isinstance(event, SessionReady)]
    assert not engine.permits.gate_lock.locked()


async def test_explicit_new_cancel_during_settings_load_preserves_a(workspace_case, tmp_path, monkeypatch):
    engine, first, target, _ = await _session_with_options(workspace_case, tmp_path)
    original = session_lifecycle.load_settings
    entered, released = threading.Event(), threading.Event()

    def hold_settings(**kwargs):
        entered.set()
        if not released.wait(5):
            raise RuntimeError("Test did not release settings loading")
        return original(**kwargs)

    monkeypatch.setattr(session_lifecycle, "load_settings", create_autospec(load_settings, side_effect=hold_settings))
    before = (engine.session_id, engine.session_generation, engine.session.workspace_plan)
    task = asyncio.create_task(engine.event_bus.publish(SessionNew(workspace_id=target.id), raise_handler_errors=True))
    try:
        await wait_for(lambda: entered.is_set() or task.done(), description="settings staged")
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (engine.session_id, engine.session_generation, engine.session.workspace_plan) == before
        assert engine.workspace.id == first.id
    finally:
        released.set()
        if not task.done():
            await task


@pytest.mark.parametrize("action", ["new", "clear"])
async def test_missing_workspace_primary_refuses_replacement_before_closing(workspace_case, tmp_path, action):
    primary = tmp_path / "primary"
    primary.mkdir()
    engine, workspace = await workspace_case.start_selected(primary)
    await ask(engine, "keep this session")
    session_id = engine.session_id
    primary.rename(tmp_path / "moved")

    async with capture_event_sequence(engine.event_bus, Error) as errors:
        if action == "new":
            with pytest.raises(MissingWorkspacePrimary):
                await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)
        else:
            await engine.event_bus.publish(SessionClear(session_id=session_id), raise_handler_errors=True)

    assert [event.code for event in errors] == [f"session_{action}_failed"]
    (error,) = errors
    assert error.display_message is not None
    assert error.display_message.definition.key == "workspaces.primary_missing"
    assert engine.session_id == session_id
    assert workspace_case.registry.snapshot().workspace_id_for_session(session_id) == workspace.id
    assert await workspace_case.store.load_session(session_id) is not None


@pytest.mark.parametrize("action", ["new", "clear", "fork"])
@pytest.mark.parametrize("deleted", [False, True])
async def test_implicit_session_uses_latest_definition_or_warns_once(workspace_case, tmp_path, action, deleted):
    engine, first, _, reference = await _session_with_options(workspace_case, tmp_path)
    registry = workspace_case.registry
    outgoing = engine.session_id
    if deleted:
        registry.delete(first.id)
    else:
        registry.update(first.id, name="Updated", primary=first.primary_cwd, folders=[str(reference)])
    assert engine.workspace.name == "A"

    async with capture_event_sequence(engine.event_bus, SessionForked, Warning) as events:
        if action == "fork":
            await engine.event_bus.publish(SessionFork(session_id=outgoing), raise_handler_errors=True)
            (forked,) = [event for event in events if isinstance(event, SessionForked)]
            assert engine.session_id == outgoing
            assert engine.workspace.name == "A"
            await engine.event_bus.publish(SessionRestore(session_id=forked.new_session_id), raise_handler_errors=True)
        else:
            await engine.event_bus.publish(
                SessionNew() if action == "new" else SessionClear(session_id=outgoing), raise_handler_errors=True
            )

    assert engine.session_id != outgoing
    assert engine.workspace.name == ("" if deleted else "Updated")
    assert [directory.path for directory in engine.workspace.working_dirs if not directory.is_primary] == (
        [] if deleted else [str(reference)]
    )
    warnings = [event for event in events if isinstance(event, Warning)]
    assert len(warnings) == int(deleted)
    if deleted:
        assert "A" in warnings[0].message


@pytest.mark.parametrize(
    "mode", ["inherit", "workspace", "unspecified", "unspecified-with-directory", "write-failure", "build-failure"]
)
async def test_explicit_new_installs_one_target_after_commit(workspace_case, tmp_path, monkeypatch, mode):
    engine, first, target, directory = await _session_with_options(workspace_case, tmp_path)
    registry = workspace_case.registry
    outgoing = engine.session_id
    if mode == "inherit":
        event = SessionNew()
        expected = first
    elif mode.startswith("unspecified"):
        event = SessionNew(start_unspecified=True, primary_cwd=str(directory) if mode.endswith("directory") else "")
        expected = None
    else:
        event = SessionNew(workspace_id=target.id)
        expected = target
    if mode == "write-failure":

        def fail_write(registry, session_id, workspace_id):
            raise WorkspaceRegistryError("mapping failed", session_lifecycle.NEW_MEMBERSHIP_WRITE_FAILED.bind(name="B"))

        monkeypatch.setattr(
            type(registry),
            "set_session_workspace",
            create_autospec(type(registry).set_session_workspace, side_effect=fail_write),
        )
    if mode == "build-failure":
        monkeypatch.setattr(
            engine.loader, "build", create_autospec(engine.loader.build, side_effect=RuntimeError("build failed"))
        )
    async with capture_event_sequence(engine.event_bus, Error, SessionReady, Warning) as events:
        if mode == "build-failure":
            with pytest.raises(RuntimeError, match="build failed"):
                await engine.event_bus.publish(event, raise_handler_errors=True)
        else:
            await engine.event_bus.publish(event, raise_handler_errors=True)
    assert engine.session_id != outgoing
    assert registry.snapshot().workspace_id_for_session(outgoing) == first.id
    ready = [item for item in events if isinstance(item, SessionReady)]
    if mode == "build-failure":
        assert ready == []
        assert engine.current.loaded is None
        assert registry.snapshot().workspace_id_for_session(engine.session_id) is None
        assert any(isinstance(item, Error) and item.code == "session_new_failed" for item in events)
        return
    (landed,) = ready
    assert landed.session_id == engine.session_id
    assert (landed.workspace.id if landed.workspace is not None else None) == (expected.id if expected else None)
    assert landed.primary_cwd == (
        expected.primary_cwd if expected else str(directory) if event.primary_cwd else first.primary_cwd
    )
    assert landed.working_dirs == []
    assert not any(isinstance(item, Error) for item in events)
    assert [(item.code, item.session_id) for item in events if isinstance(item, Warning)] == (
        [("workspace_membership_write_failed", landed.session_id)] if mode == "write-failure" else []
    )
    assert registry.snapshot().workspace_id_for_session(engine.session_id) == (
        expected.id if expected and mode != "write-failure" else None
    )
    if mode == "workspace":
        assert registry.snapshot().last_used_at[target.id]


async def test_invalid_new_session_parameters_publish_error(workspace_case, tmp_path):
    engine = await workspace_case(tmp_path, store=None)
    outgoing = engine.session_id
    async with capture_event_sequence(engine.event_bus, Error, SessionReady) as events:
        await engine.event_bus.publish(SessionNew(primary_cwd=str(tmp_path)))
    assert [(event.code, event.session_id) for event in events] == [("session_new_failed", outgoing)]
    assert engine.session_id == outgoing


@pytest.mark.parametrize("action", ["new", "clear", "fork"])
@pytest.mark.parametrize("workspace_launch", [False, True], ids=["main-unspecified", "workspace"])
async def test_implicit_session_keeps_settings_after_its_directory_symlink_moves(
    workspace_case, tmp_path, monkeypatch, action, workspace_launch
):

    first, second, link = make_workdir_alias(tmp_path, "first", "second", "link")
    registry = workspace_case.registry
    workspace = registry.create("Team", str(link))
    engine = await workspace_case(first, workspace_id=workspace.id if workspace_launch else None)
    await ask(engine, "previous session")
    retarget_workdir_alias(link, second)
    installed = engine.settings_handle.base

    def fail_settings(**kwargs):
        pytest.fail("same-root implicit new read settings from disk")

    monkeypatch.setattr(session_lifecycle, "load_settings", fail_settings)
    profile = engine.session.agent_profile
    mcp_profile = replace(
        profile, tools=replace(profile.tools, mcp=[MCPServerConfig(name="test", transport="stdio", command="unused")])
    )
    engine.loader._agent_registry.register(mcp_profile)
    hook_build = create_autospec(engine.loader.build_hook_manager, side_effect=engine.loader.build_hook_manager)
    connect = create_autospec(MCPAdapter.connect_all, return_value=[])
    monkeypatch.setattr(engine.loader, "build_hook_manager", hook_build)
    monkeypatch.setattr(MCPAdapter, "connect_all", connect)
    if action == "fork":
        async with capture_event_sequence(engine.event_bus, SessionForked) as events:
            await engine.event_bus.publish(SessionFork(session_id=engine.session_id), raise_handler_errors=True)
        (forked,) = events
        meta = await workspace_case.store.load_session_meta(forked.new_session_id)
        assert meta is not None
        assert meta.primary_cwd == str(first)
        assert registry.snapshot().workspace_id_for_session(forked.new_session_id) == (
            workspace.id if workspace_launch else None
        )
        hook_build.assert_not_awaited()
        connect.assert_not_awaited()
        return

    await engine.event_bus.publish(
        SessionNew() if action == "new" else SessionClear(session_id=engine.session_id), raise_handler_errors=True
    )
    runtime = engine.current.require_loaded().runtime
    assert runtime.cwd == str(first)
    assert engine.settings_handle.base == installed
    assert hook_build.await_args.kwargs["workdir"] == str(first)
    assert connect.await_args.args[0]._stdio_cwd == str(first)
