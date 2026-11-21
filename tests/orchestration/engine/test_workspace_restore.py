# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Registry membership, explicit selections and rollback use the baselines in O/R/N."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import (
    Error,
    SessionNew,
    SessionRestore,
    SessionRestored,
    SetApprovalMode,
    UserRollback,
    WorkspaceChange,
    WorkspaceChangeFinished,
    WorkspaceOperation,
)
from chrys.foundation.models.side_questions import SideQuestionEntry
from chrys.foundation.models.workspace import WorkspaceConfirmation
from chrys.kernel import Content, Message
from chrys.orchestration.engine import session_lifecycle
from chrys.orchestration.session_host import resolve_restore_session_id
from chrys.service.context.compaction import spill
from chrys.service.context.compaction.scoped import ScopedGroup
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mcp.adapter import MCPAdapter
from chrys.service.profiles.agents.schema import MCPServerConfig, ToolsConfig
from chrys.service.session.side_questions import SideQuestionSessionSeed, load_side_questions
from chrys.service.tools.background import crash
from chrys.service.workspaces import registry as registry_module
from chrys.service.workspaces.registry import WorkspaceRegistry, WorkspaceRegistryError
from tests.support import workspace_engine
from tests.support.background_tasks import HeldProcess
from tests.support.event_capture import capture_event_sequence, capture_events
from tests.support.rollback_spec import PROFILE, ask
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workspace_engine import (
    before_await,
    select_workspace,
    start_workspace_engine,
    workspace_restore,
    workspace_selection,
)
from tests.support.workspace_engine import workspace_case as workspace_case
from tests.support.workspace_registry import (
    choose_before_restore_lock,
    make_workdir_alias,
    make_workdirs,
    registry_disk_full,
    retarget_workdir_alias,
    save_chat_session,
)
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry
from tests.support.workspace_registry import workspace_store as workspace_store


async def test_btw_only_workspace_choice_survives_shutdown_and_recovery_restore(
    workspace_case, agent_engine, monkeypatch, tmp_path
) -> None:
    """N9/G5: a newer recovery copy must retain the choice made in a btw-only conversation."""
    project = workspace_case.registry.create("Selected", str(tmp_path))
    store = workspace_case.store
    entry = SideQuestionEntry("2026-11-01T15:08:47+00:00", "Side question", "Side answer")
    await store.save_side_question(
        "target",
        entry,
        SideQuestionSessionSeed(agent_profile=PROFILE.name, primary_cwd=str(tmp_path)),
    )
    await asyncio.to_thread(
        store.save_recovery_session,
        "target",
        {"messages": []},
        agent_profile=PROFILE.name,
        primary_cwd=str(tmp_path),
    )
    with monkeypatch.context() as scoped:
        engine = await start_workspace_engine(agent_engine, scoped, tmp_path, store=store)
        await engine.event_bus.publish(
            SessionRestore(require_leave_confirmation=False, session_id="target"), raise_handler_errors=True
        )
        assert engine.session.recovered_from_sidecar
        assert engine.session.workspace_id is None

        await select_workspace(engine.event_bus, project.id, require_leave_confirmation=False)
        await engine.shutdown()

    reopened = await workspace_case(tmp_path)
    await reopened.event_bus.publish(
        SessionRestore(require_leave_confirmation=False, session_id="target"), raise_handler_errors=True
    )
    assert reopened.session.recovered_from_sidecar
    assert reopened.session.workspace_id == project.id
    assert reopened.session.session_workspace is not None and reopened.session.session_workspace.id == project.id
    assert reopened.history_messages == []
    assert load_side_questions(store.session_dir("target"), "target") == (entry,)


async def test_workspace_rollback_pins_install_after_definition_edit(workspace_case, tmp_path, monkeypatch):
    primary, historical, extra = make_workdirs(tmp_path, "primary", "historical", "extra")
    registry = workspace_case.registry
    workspace = registry.create("Live", str(primary))
    engine = await workspace_case(historical)
    await ask(engine, "historical turn")
    await select_workspace(engine.event_bus, workspace.id)
    await ask(engine, "installed turn")
    historical.rmdir()
    registry.update(workspace.id, name="Edited", primary=str(primary), folders=[str(extra)])
    registry_bytes = registry.path.read_bytes()

    def unexpected_registry_read(self):
        pytest.fail("Rollback must use the already installed workspace without reading the registry")

    with monkeypatch.context() as patch:
        patch.setattr(WorkspaceRegistry, "snapshot", unexpected_registry_read)
        await engine.event_bus.publish(UserRollback(target_turn=1), raise_handler_errors=True)

    assert engine.session.turn_number == 1
    assert engine.workspace == workspace
    assert registry.path.read_bytes() == registry_bytes
    assert [message.text for message in engine.history_messages if message.role == "user"] == ["historical turn"]


@pytest.mark.parametrize("failure", ["missing-before", "primary-disappears", "cancel"])
async def test_workspace_rollback_at_the_commit_boundary(
    workspace_case, tmp_path, monkeypatch, engine_services, failure
):
    primary = tmp_path / "primary"
    primary.mkdir()
    missing_before = failure == "missing-before"
    if missing_before:
        (tmp_path / "replacement").mkdir()
    store = workspace_case.store
    engine, workspace = await workspace_case.start_selected(primary, "App")
    kept, removed = ("first", "second") if missing_before else ("keep this", "remove this")
    await ask(engine, kept)
    await ask(engine, removed)
    sid = engine.session_id
    primary_file = store.session_dir(sid) / "session.json"
    command = UserRollback(target_turn=1)
    if missing_before:
        stored = primary_file.read_bytes()
        registry = workspace_case.registry
        registry_bytes = registry.path.read_bytes()
        primary.rmdir()
        errors = await capture_events(engine.event_bus, Error)
        await engine.event_bus.publish(command, raise_handler_errors=True)
        assert [error.code for error in errors] == ["workspace_primary_missing"]
        assert str(primary) in errors[0].message
        assert engine.session.turn_number == 2
        assert primary_file.read_bytes() == stored
        assert registry.path.read_bytes() == registry_bytes
        return

    bytes_at_swap = []

    async def late_failure_after_swap(**kwargs):
        bytes_at_swap.append(primary_file.read_bytes())
        if failure == "cancel":
            raise asyncio.CancelledError
        primary.rmdir()

    monkeypatch.setattr(engine_services(engine).trajectory_recorder, "rollback", late_failure_after_swap)
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await engine.event_bus.publish(command, raise_handler_errors=True)
    else:
        await engine.event_bus.publish(command, raise_handler_errors=True)

    assert engine.session.turn_number == 1
    assert engine.workspace.primary_cwd == str(primary)
    assert engine.session.workspace_id == workspace.id
    assert [message.text for message in engine.history_messages if message.role == "user"] == ["keep this"]
    assert primary_file.read_bytes() == bytes_at_swap[0]
    state = await store.load_session(sid)
    assert [message.text for message in state["messages"] if message.role == "user"] == ["keep this"]


@pytest.mark.parametrize("failure", ["write", "build"])
async def test_cross_session_select_failure_does_not_install_target(workspace_case, tmp_path, monkeypatch, failure):
    registry, store = workspace_case.registry, workspace_case.store
    workspace = registry.create("App", str(tmp_path))
    await store.save_session("target", {"messages": []}, agent_profile=PROFILE.name, primary_cwd=str(tmp_path))
    engine = await workspace_case(tmp_path)
    await ask(engine, "active history")
    old_id = engine.session_id
    engine.history_messages.append(Message("user", ["only in memory"]))
    request = workspace_restore("target", workspace.id)

    if failure == "build":
        monkeypatch.setattr(
            engine.loader, "build", create_autospec(engine.loader.build, side_effect=RuntimeError("build failed"))
        )
        with pytest.raises(WorkspaceRegistryError, match="build failed"):
            await engine.event_bus.publish(request, raise_handler_errors=True)
    else:
        import errno

        replace_file = os.replace

        def disk_full(source, target, *args, **kwargs):
            if Path(target) == registry.path:
                raise OSError(errno.ENOSPC, "registry disk full")
            return replace_file(source, target, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", disk_full)
            with pytest.raises(WorkspaceRegistryError, match="registry disk full"):
                await engine.event_bus.publish(request, raise_handler_errors=True)

    assert engine.session_id == old_id
    assert engine.history_messages[-1].text == "only in memory"
    assert engine.session.guard.owns(old_id)
    assert registry.snapshot().workspace_id_for_session("target") == (workspace.id if failure == "build" else None)
    assert engine.session.turn_number == 1
    await ask(engine, "continue the outgoing session")
    assert engine.session.turn_number == 2
    assert any(message.text == "only in memory" for message in engine.history_messages)


async def test_strict_restore_reports_membership_saved_before_open_failed(workspace_case, tmp_path, monkeypatch):
    running, destination = make_workdirs(tmp_path, "running", "destination")
    registry, store = workspace_case.registry, workspace_case.store
    before = registry.create("Before", str(running))
    after = registry.create("After", str(destination))
    await save_chat_session(store, "target", running, profile=PROFILE.name, messages=[Message("user", ["saved"])])
    registry.set_session_workspace("target", before.id)
    engine = await workspace_case(running)

    async def fail_to_close():
        raise RuntimeError("close failed")

    request = workspace_restore("target", after.id)
    with monkeypatch.context() as patch:
        patch.setattr(engine.lifecycle, "close_session", fail_to_close)
        with pytest.raises(WorkspaceRegistryError, match="Membership was saved for After") as failure:
            await engine.event_bus.publish(request, raise_handler_errors=True)

    assert f"This run is at {running}" in str(failure.value)
    assert "Reopening uses After" in str(failure.value)
    assert registry.snapshot().workspace_id_for_session("target") == after.id
    assert engine.workspace.primary_cwd == str(running)
    await engine.event_bus.publish(SessionRestore(session_id="target"), raise_handler_errors=True)
    assert engine.workspace.primary_cwd == str(destination)
    assert [message.text for message in engine.history_messages if message.role == "user"] == ["saved"]


async def test_reopening_uses_the_workspace_primary_validated_by_the_tui(workspace_case, tmp_path, monkeypatch):
    original, moved, link = make_workdir_alias(tmp_path, "original", "moved", "link")
    registry = workspace_case.registry
    workspace = registry.create("Team", str(link))
    engine = await workspace_case(original, workspace_id=workspace.id)
    await ask(engine, "save an open session")
    sid = engine.session_id
    retarget_workdir_alias(link, moved)
    opened_cwd = session_lifecycle.validate_workspace_primary(workspace.primary_cwd)
    snapshot = registry.snapshot()
    link.unlink()

    def unexpected_validation(*args):
        pytest.fail("the TUI already validated the primary from this snapshot")

    monkeypatch.setattr(session_lifecycle, "validate_workspace_primary", unexpected_validation)
    await engine.event_bus.publish(
        SessionRestore(session_id=sid, workspace_snapshot=snapshot, opened_cwd=opened_cwd), raise_handler_errors=True
    )
    assert engine.current.require_loaded().runtime.cwd == str(moved)
    assert registry.snapshot().workspace_id_for_session(sid) == workspace.id


async def test_in_place_restore_to_replacement_keeps_background_tasks(workspace_case, tmp_path, monkeypatch):
    primary, replacement = make_workdirs(tmp_path, "primary", "replacement")
    engine, workspace = await workspace_case.start_selected(primary)
    await ask(engine, "save an open session")
    sid = engine.session_id
    tasks = engine.session.background_tasks
    assert tasks is not None
    monkeypatch.setattr(crash, "_members", lambda _pid: ())
    process = HeldProcess()
    task = tasks.adopt(process, command="serve", trigger="timeout", cwd=str(primary))
    assert task is not None
    try:
        primary.rename(tmp_path / "moved")
        await engine.event_bus.publish(
            SessionRestore(
                session_id=sid,
                primary_cwd=str(replacement),
                confirmation=WorkspaceConfirmation.capture(workspace.id, str(replacement)),
            ),
            raise_handler_errors=True,
        )

        assert engine.session_id == sid
        assert engine.workspace.primary_cwd == str(replacement)
        assert engine.session.workspace_id is None
        assert engine.session.background_tasks is tasks
        assert tasks.running == [task]
        assert process.signals == []
        await engine.shutdown()
        assert task.outcome == "stopped at session end"
        assert process.closed
    finally:
        await tasks.close()


def _refuse_membership_write(*args, **kwargs):
    raise registry_module._write_failed(OSError("membership write refused"))


async def test_refused_in_place_select_preserves_spill_before_catalog_append(workspace_case, tmp_path, monkeypatch):
    registry = workspace_case.registry
    target = registry.create("Target", str(tmp_path))
    engine = await workspace_case(tmp_path)
    await ask(engine, "save a turn")
    active_id = engine.session_id
    session_dir = engine.session.session_dir
    record_dir = spill.dropped_turn_relative_path(1)
    entered, released = threading.Event(), threading.Event()
    append = spill._append_catalog_line

    def append_after_release(root, record):
        entered.set()
        released.wait()
        append(root, record)

    monkeypatch.setattr(spill, "_append_catalog_line", append_after_release)
    monkeypatch.setattr(WorkspaceRegistry, "set_session_workspace", _refuse_membership_write)
    worker = asyncio.create_task(
        asyncio.to_thread(
            spill.write_spill_batch,
            session_dir,
            engine.session.spill_quota,
            [ScopedGroup("g", "assistant", (Message("assistant", [Content.from_text("archive")]),), True)],
            record_dir=record_dir,
            absolute_turn=1,
            round_number=1,
            session_id=active_id,
        )
    )
    try:
        await wait_for(
            lambda: entered.is_set() or worker.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="the spill worker reached its catalog append",
        )
        if worker.done():
            await worker
        assert entered.is_set()
        assert not worker.done()
        (archive,) = (session_dir / record_dir).glob(f"*{spill.RECORD_FILE_SUFFIX}")
        content = archive.read_bytes()
        errors = await capture_events(engine.event_bus, Error)
        await engine.event_bus.publish(
            workspace_restore(active_id, target.id),
            raise_handler_errors=True,
        )
        assert any("membership write refused" in event.message for event in errors)
        assert engine.session_id == active_id
        assert archive.read_bytes() == content
    finally:
        released.set()
        result = await worker
    assert not result.unexpected_io_failure
    assert archive.read_bytes() == content
    relative = archive.relative_to(session_dir).as_posix()
    assert result.entries[0].relative_path == relative
    assert any(row.relative_path == relative for row in spill.catalog_live_records(session_dir))


async def test_strict_restore_defers_session_file_repairs_until_membership_is_written(
    workspace_case, tmp_path, monkeypatch
):
    registry, store = workspace_case.registry, workspace_case.store
    workspace = registry.create("Target", str(tmp_path))
    await save_chat_session(store, "target", tmp_path, profile=PROFILE.name, messages=[Message("user", ["saved"])])
    primary, recovery = store._session_file("target"), store._recovery_file("target")
    primary.write_text("broken primary", encoding="utf-8")
    recovery.write_text("broken recovery", encoding="utf-8")
    engine = await workspace_case(tmp_path)
    old_id = engine.session_id
    request = workspace_restore("target", workspace.id)
    profile = replace(
        PROFILE, tools=replace(PROFILE.tools, mcp=[MCPServerConfig(name="target", transport="stdio", command="unused")])
    )
    profile_path = engine.loader._agent_registry.save(profile)
    legacy_path = profile_path.with_name("legacy.yaml")
    original = profile_path.read_text(encoding="utf-8")
    legacy_path.write_text(
        "# preserve user comment\n"
        + "".join(line for line in original.splitlines(keepends=True) if not line.startswith("id:")),
        encoding="utf-8",
    )
    profile_path.unlink()
    before_agents = {path.name: path.read_bytes() for path in legacy_path.parent.glob("*.yaml")}
    connect = create_autospec(MCPAdapter.connect_all, return_value=[])
    monkeypatch.setattr(MCPAdapter, "connect_all", connect)

    with monkeypatch.context() as patch:
        patch.setattr(WorkspaceRegistry, "set_session_workspace", _refuse_membership_write)
        with pytest.raises(WorkspaceRegistryError, match="membership write refused"):
            await engine.event_bus.publish(request, raise_handler_errors=True)
        assert engine.session_id == old_id
        assert primary.read_text(encoding="utf-8") == "broken primary"
        assert recovery.read_text(encoding="utf-8") == "broken recovery"
        assert registry.snapshot().workspace_id_for_session("target") is None
        assert {path.name: path.read_bytes() for path in legacy_path.parent.glob("*.yaml")} == before_agents
        connect.assert_not_awaited()

    await engine.event_bus.publish(request, raise_handler_errors=True)
    assert engine.session_id == "target"
    assert engine.workspace.id == workspace.id
    assert json.loads(primary.read_text(encoding="utf-8"))["meta"]["primary_cwd"] == str(tmp_path)
    assert not recovery.exists()
    assert profile_path.exists()
    assert not legacy_path.exists()
    connect.assert_awaited()


async def test_selector_and_restore_open_backup_when_primary_is_invalid_utf8(workspace_case, tmp_path):
    store = workspace_case.store
    await save_chat_session(
        store, "target", tmp_path, profile=PROFILE.name, messages=[Message("user", ["from backup"])]
    )
    primary = store._session_file("target")
    primary.replace(primary.with_suffix(".json.bak"))
    primary.write_bytes(b"\xff")
    selected = await resolve_restore_session_id("target", store, repair=False)
    assert selected == "target"
    assert primary.read_bytes() == b"\xff"

    engine = await workspace_case(tmp_path)
    await engine.event_bus.publish(SessionRestore(session_id=selected), raise_handler_errors=True)
    assert engine.session_id == selected
    assert any(message.text == "from backup" for message in engine.history_messages)


@pytest.mark.parametrize("operation", [WorkspaceOperation.SELECT, WorkspaceOperation.UNSPECIFY])
async def test_workspace_restore_keeps_the_turn_finished_at_the_fence(workspace_case, monkeypatch, tmp_path, operation):
    from chrys.foundation.events.types import UserMessage
    from tests.support.rollback_spec import drain

    primary = tmp_path / "primary"
    primary.mkdir()
    workspace = workspace_case.registry.create("Team", str(primary))
    client = MockChatClient(
        responses=[
            MockResponse(text="old answer"),
            MockResponse(text="new answer"),
            MockResponse(text="follow-up answer"),
        ]
    )
    selecting = operation is WorkspaceOperation.SELECT
    engine = await workspace_case(
        tmp_path if selecting else primary,
        workspace_id=None if selecting else workspace.id,
        client=client,
    )
    await ask(engine, "saved before selection")
    sid = engine.session_id
    results = await capture_events(engine.event_bus, WorkspaceChangeFinished)

    save = workspace_case.gate(workspace_case.store, "save_session")

    async def finish_turn():
        save.open()
        await drain(engine)

    for method in ("prepare_session_transition", "acquire_rebuild_permit"):
        before_await(monkeypatch, engine.permits, method, finish_turn)
    await engine.event_bus.publish(UserMessage(text="finished at the selection fence"), raise_handler_errors=True)
    try:
        await asyncio.wait_for(save.entered.wait(), timeout=ENGINE_TURN_TIMEOUT)
        request = SessionRestore(
            session_id=sid,
            workspace_operation=operation,
            workspace_id=workspace.id if selecting else None,
        )
        await engine.event_bus.publish(request, raise_handler_errors=True)
    finally:
        save.open()
        await drain(engine)

    (terminal,) = results
    assert terminal.request_id == request.event_id
    assert terminal.error is None
    assert engine.session_id == sid
    assert engine.workspace.id == (workspace.id if selecting else "")
    assert [message.text for message in engine.history_messages if message.text][-2:] == [
        "finished at the selection fence",
        "new answer",
    ]

    await ask(engine, "follow up after the workspace change")
    sent, _ = client.call_history[-1]
    assert "finished at the selection fence" in " ".join(message.text for message in sent)
    assert "new answer" in [message.text for message in sent if message.role == "assistant"]


@pytest.mark.parametrize("membership", ["replaced", "removed"])
async def test_tui_restore_rejects_confirmation_after_membership_changes(
    workspace_case, tmp_path, monkeypatch, membership
):
    old_root, new_root, replacement = make_workdirs(tmp_path, "old", "new", "replacement")
    registry, store = workspace_case.registry, workspace_case.store
    old = registry.create("Old", str(old_root))
    current = registry.create("Current", str(new_root))
    sid = "target"
    await store.save_session(sid, {"messages": []}, agent_profile=PROFILE.name, primary_cwd=str(old_root))
    registry.set_session_workspace(sid, old.id)
    engine = await workspace_case(tmp_path)
    active_id = engine.session_id
    current_id = current.id if membership == "replaced" else None
    acquire = engine.session.guard.acquire_for_restore

    def acquire_after_move(session_id, **kwargs):
        choose_before_restore_lock(store, registry, sid, current_id)
        return acquire(session_id, **kwargs)

    monkeypatch.setattr(engine.session.guard, "acquire_for_restore", acquire_after_move)
    async with capture_event_sequence(engine.event_bus, Error, SessionRestored) as outcomes:
        await engine.event_bus.publish(
            SessionRestore(
                session_id=sid,
                primary_cwd=str(replacement),
                confirmation=WorkspaceConfirmation.capture(old.id, str(replacement)),
            ),
            raise_handler_errors=True,
        )
    (error,) = outcomes
    assert isinstance(error, Error)
    assert error.code == "workspace_leave_confirmation_required"
    assert error.session_id == sid
    assert engine.session_id == active_id
    assert registry.snapshot().workspace_id_for_session(sid) == current_id


@pytest.mark.parametrize("reopened", [False, True], ids=["this-run", "reopened"])
async def test_failed_membership_does_not_prevent_workspace_folder_file_rollback(
    registry, store, agent_engine, monkeypatch, tmp_path, reopened
) -> None:
    primary, related = make_workdirs(tmp_path, "primary", "related")
    target = related / "note.txt"
    original = b"Original file contents.\n"
    target.write_bytes(original)
    project = registry.create("Current", str(primary), [str(related)])
    monkeypatch.setattr(workspace_engine, "PROFILE", replace(PROFILE, tools=ToolsConfig(builtins=["filesystem.write"])))
    client = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[
                    ("write_file", "edit-note", {"path": str(target), "content": "Changed.\n", "overwrite": True})
                ]
            ),
            MockResponse(text="Edited the note."),
            MockResponse(text="A second turn."),
        ]
    )

    async def roll_back(current) -> None:
        await current.event_bus.publish(UserRollback(target_turn=0, revert_changes=True), raise_handler_errors=True)
        assert current.session_id == session_id
        assert current.session.turn_number == 0
        assert target.read_bytes() == original
        assert current.session.workspace_id == (None if reopened else project.id)
        assert current.session.session_workspace == (None if reopened else live_workspace)

    with monkeypatch.context() as runtime:
        engine = await start_workspace_engine(
            agent_engine, runtime, primary, workspace_id=project.id, store=store, client=client
        )
        live_workspace = engine.session.session_workspace

        with runtime.context() as failed_write:
            failed_write.setattr(registry_module, "atomic_write_text", registry_disk_full)
            await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)
        session_id = engine.session_id
        assert engine.session.session_workspace == live_workspace
        assert registry.snapshot().workspace_id_for_session(session_id) is None
        await engine.event_bus.publish(SetApprovalMode(mode="bypass", persist=False), raise_handler_errors=True)
        await ask(engine, "Change the note in the other project folder.")
        assert target.read_bytes() == b"Changed.\n"
        await ask(engine, "Keep the change for now.")
        if not reopened:
            await roll_back(engine)
        await engine.shutdown()

    if reopened:
        restored = await start_workspace_engine(agent_engine, monkeypatch, primary, store=store, client=client)
        await restored.event_bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
        assert restored.session.session_workspace is None
        await roll_back(restored)
        await restored.shutdown()


@pytest.mark.parametrize("select", [False, True])
async def test_only_workspace_selection_rejects_a_baseline_changed_at_install(
    workspace_case, tmp_path, monkeypatch, engine_services, select
):
    primary = tmp_path / "primary"
    primary.mkdir()
    workspace = workspace_case.registry.create("Team", str(primary))
    engine = await workspace_case(tmp_path)
    tracker = engine_services(engine).workspace_change_tracker
    build = engine.loader.build

    async def update_baseline(*args, **kwargs):
        candidate = await build(*args, **kwargs)
        tracker.invalidate()
        return candidate

    monkeypatch.setattr(engine.loader, "build", update_baseline)
    errors = await capture_events(engine.event_bus, Error)
    request = workspace_selection(workspace.id) if select else WorkspaceChange(primary_cwd=str(primary))
    await engine.event_bus.publish(request)
    if not select:
        assert errors == []
        assert engine.workspace.primary_cwd == str(primary)
        return

    (error,) = errors
    assert error.code == "workspace_membership_saved_open_failed"
    assert "Reopening uses Team" in error.message
    assert engine.workspace.primary_cwd == str(tmp_path)
    assert workspace_case.registry.snapshot().workspace_id_for_session(engine.session_id) == workspace.id
