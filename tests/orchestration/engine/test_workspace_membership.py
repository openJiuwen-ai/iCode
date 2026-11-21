# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Explicit membership operations update the registry without rewriting conversations."""

from __future__ import annotations

import asyncio
import json

import pytest

from chrys.foundation.events.types import (
    Error,
    SessionFork,
    SessionForked,
    SessionNew,
    SessionRestore,
    SessionRestored,
    WorkspaceChange,
    WorkspaceOperation,
)
from chrys.foundation.platform import get_platform
from chrys.kernel import Message
from chrys.service.session.persistence import open_active_session_store
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces import registry as registry_module
from tests.support.async_tasks import wait_for_started
from tests.support.event_capture import capture_event_sequence
from tests.support.rollback_spec import PROFILE, ask
from tests.support.workspace_engine import select_workspace, workspace_restore
from tests.support.workspace_engine import workspace_case as workspace_case
from tests.support.workspace_registry import make_workdirs, registry_disk_full, save_chat_session


def _assert_no_membership(store: JsonFileStateStore, session_id: str) -> None:
    envelope = json.loads((store.session_dir(session_id) / "session.json").read_text(encoding="utf-8"))
    assert not {"workspace_id", "workspace_name"} & envelope["meta"].keys()


async def test_restore_updates_only_the_registry_paired_with_the_session_store(workspace_case, tmp_path):
    active_cwd, isolated_cwd, destination = make_workdirs(tmp_path, "active", "isolated", "destination")
    active = open_active_session_store()
    isolated = JsonFileStateStore(tmp_path / "isolated-sessions")
    session_id = "same-session"
    for store, cwd, name in ((active, active_cwd, "Active"), (isolated, isolated_cwd, "Isolated")):
        registry = store.workspace_registry
        workspace = registry.create(name, str(cwd))
        await save_chat_session(store, session_id, cwd, profile=PROFILE.name, messages=[Message("user", [name])])
        registry.set_session_workspace(session_id, workspace.id)
    global_path = get_platform().config_dir / "workspaces.json"
    assert active.workspace_registry.path == global_path
    before = global_path.read_bytes()

    engine = await workspace_case(destination, store=isolated)
    restore = SessionRestore(session_id=session_id, primary_cwd=str(destination), require_leave_confirmation=False)
    async with capture_event_sequence(engine.event_bus, Error, SessionRestored) as events:
        await engine.event_bus.publish(restore, raise_handler_errors=True)
    (restored,) = events
    assert isinstance(restored, SessionRestored)
    assert engine.workspace.primary_cwd == str(destination)
    assert engine.session.workspace_id is None
    assert engine.history_messages[0].text == "Isolated"
    assert not isolated.workspace_registry.snapshot().maps_session(session_id)
    assert global_path.read_bytes() == before

    engine = await workspace_case(destination, store=active)
    await engine.event_bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
    assert engine.workspace.primary_cwd == str(active_cwd)
    assert engine.workspace.name == "Active"
    assert engine.history_messages[0].text == "Active"
    assert active.workspace_registry.snapshot().workspace_id_for_session(session_id) == engine.workspace.id


async def test_implicit_new_survives_membership_write_failure(workspace_case, monkeypatch, tmp_path):
    registry = workspace_case.registry
    engine, workspace = await workspace_case.start_selected(tmp_path, "Current")

    with monkeypatch.context() as failure:
        failure.setattr(registry_module, "atomic_write_text", registry_disk_full)
        await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)

    assert engine.workspace.id == workspace.id
    assert registry.snapshot().workspace_id_for_session(engine.session_id) is None
    await ask(engine, "the new chat still works")
    assert any(message.text == "the new chat still works" for message in engine.history_messages)


async def test_workspace_operations_leave_saved_conversation_bytes_and_mtime_unchanged(
    workspace_case, tmp_path
) -> None:
    """Selection, definition changes, leaving and deletion only edit the registry."""
    root, extra = make_workdirs(tmp_path, "root", "extra")
    registry = workspace_case.registry
    project = registry.create("Original", str(root))
    store = workspace_case.store
    engine = await workspace_case(root)
    await ask(engine, "keep the conversation unchanged")
    session_id = engine.session_id
    path = store.session_dir(session_id) / "session.json"
    before = path.read_bytes(), path.stat().st_mtime_ns

    async def select() -> None:
        await select_workspace(engine.event_bus, project.id, require_leave_confirmation=False)

    def assert_unchanged() -> None:
        _assert_no_membership(store, session_id)
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before

    await select()
    assert_unchanged()
    assert registry.snapshot().workspace_id_for_session(session_id) == project.id

    registry.update(project.id, name="Renamed", primary=str(root))
    await select()
    assert_unchanged()

    registry.update(project.id, name="Renamed", primary=str(root), folders=[str(extra)])
    await select()
    assert_unchanged()

    await engine.event_bus.publish(
        WorkspaceChange(require_leave_confirmation=False, workspace_operation=WorkspaceOperation.UNSPECIFY),
        raise_handler_errors=True,
    )
    assert_unchanged()
    assert registry.snapshot().workspace_id_for_session(session_id) is None

    await select()
    registry.delete(project.id)
    assert engine.session.workspace_id == project.id
    assert_unchanged()
    assert registry.snapshot().workspace_id_for_session(session_id) is None


@pytest.mark.parametrize("operation", [WorkspaceOperation.SELECT, WorkspaceOperation.UNSPECIFY])
async def test_registry_failure_refuses_membership_operation_and_keeps_user_work(
    workspace_case,
    monkeypatch,
    tmp_path,
    operation,
):
    root, outside = make_workdirs(tmp_path, "root", "outside")
    registry, store = workspace_case.registry, workspace_case.store
    current = registry.create("Current", str(root))
    other = registry.create("Other", str(outside))
    engine = await workspace_case(root, workspace_id=current.id)
    await ask(engine, "work that must survive failure")
    sid = engine.session_id
    saved = store.session_dir(sid) / "session.json"
    before = saved.read_bytes()
    registry_before = registry.path.read_bytes()

    with monkeypatch.context() as failure:
        failure.setattr(registry_module, "atomic_write_text", registry_disk_full)
        async with capture_event_sequence(engine.event_bus, Error) as errors:
            await engine.event_bus.publish(
                WorkspaceChange(
                    require_leave_confirmation=False,
                    workspace_operation=operation,
                    workspace_id=other.id if operation is WorkspaceOperation.SELECT else None,
                ),
                raise_handler_errors=True,
            )

    assert [error.code for error in errors] == ["workspace_selection_failed"]
    assert "registry disk full" in errors[0].message
    assert engine.session_id == sid
    assert (engine.workspace.id, engine.workspace.primary_cwd) == (current.id, str(root))
    assert saved.read_bytes() == before
    assert registry.path.read_bytes() == registry_before
    await ask(engine, "continue after refusal")
    state = await store.load_session(sid)
    assert [message.text for message in state["messages"] if message.role == "user"] == [
        "work that must survive failure",
        "continue after refusal",
    ]


@pytest.mark.parametrize("raises", [False, True])
async def test_trajectory_fork_degradation_keeps_the_session_copy(
    workspace_case, tmp_path, monkeypatch, caplog, raises
):
    from chrys.service.trajectory import fork as trajectory_fork

    registry = workspace_case.registry
    engine, workspace = await workspace_case.start_selected(tmp_path)
    await ask(engine, "conversation to fork")
    before = registry.snapshot().sessions.copy()

    async def fail_fork(**kwargs):
        if raises:
            raise RuntimeError("trajectory fork failed")
        return False

    monkeypatch.setattr(trajectory_fork, "record_fork", fail_fork)
    async with capture_event_sequence(engine.event_bus, Error, SessionForked) as events:
        await engine.event_bus.publish(SessionFork(session_id=engine.session_id), raise_handler_errors=True)
    (fork,) = events
    assert isinstance(fork, SessionForked)
    assert await workspace_case.store.load_session(fork.new_session_id) is not None
    assert registry.snapshot().sessions == {**before, fork.new_session_id: workspace.id}
    if raises:
        assert "Failed to record trajectory fork" in caplog.text


async def test_workspace_fork_does_not_overwrite_a_selection_after_child_publication(workspace_case, tmp_path):
    primary, destination = make_workdirs(tmp_path, "primary", "destination")
    registry = workspace_case.registry
    inherited = registry.create("Parent", str(primary))
    selected = registry.create("Other window", str(destination))
    other_store = JsonFileStateStore(tmp_path / "sessions")
    other_window = await workspace_case(destination, store=other_store)
    engine = await workspace_case(primary, workspace_id=inherited.id)
    await ask(engine, "conversation to fork")
    gate = workspace_case.gate(engine.lifecycle._trajectory_recorder, "fork")
    async with capture_event_sequence(engine.event_bus, SessionForked) as forks:
        request = asyncio.create_task(engine.event_bus.publish(SessionFork(session_id=engine.session_id)))
        try:
            await wait_for_started(
                gate.entered, request, description="child published before trajectory and membership"
            )
            (child,) = [
                meta.session_id for meta in await other_store.list_sessions() if meta.session_id != engine.session_id
            ]
            assert not registry.snapshot().maps_session(child)
            restore = workspace_restore(child, selected.id)
            async with capture_event_sequence(other_window.event_bus, Error, SessionRestored) as events:
                await other_window.event_bus.publish(restore, raise_handler_errors=True)
            (restored,) = events
            assert isinstance(restored, SessionRestored)
            assert restored.session_id == child
            assert registry.snapshot().workspace_id_for_session(child) == selected.id
        finally:
            gate.open()
            await request

    assert [fork.new_session_id for fork in forks] == [child]
    assert other_window.session_id == child
    assert registry.snapshot().workspace_id_for_session(child) == selected.id
