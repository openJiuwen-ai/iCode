# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Starting another run hydrates the workflow session's durable resources under its lock."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workflow_session import WorkflowIdentity, WorkflowModelSelection
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Message
from chrys.orchestration.workflows import session as session_module
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.service.context.compaction.scoped import ScopedGroup
from chrys.service.context.compaction.spill import (
    SpillQuota,
    dropped_turn_relative_path,
    write_spill_batch,
)
from chrys.service.session.persistence import SessionPersistence
from chrys.service.state.store import JsonFileStateStore
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.history import read_workflow_meta
from chrys.service.workflows.orphans import read_run_terminal
from tests.support.waiting import wait_for
from tests.support.workflow_history import record_workflow_run

_BINDING = WorkflowIdentity("review", "/project/review.py", "workdir")


async def _hooks(*, workdir: str, workdir_hooks_enabled: bool, session_id: str, request_id: str = "") -> None:
    return None


async def _owner(tmp_path: Path) -> WorkflowSessionOwner:
    bus = EventBus()
    store = JsonFileStateStore(tmp_path / "sessions")
    session_id = str(uuid4())
    await store.save_workflow_session(
        session_id,
        WorkflowSessionState.decode(
            {
                "identity": asdict(_BINDING),
                "workspace": asdict(Workspace.from_cwd(str(tmp_path))),
                "summary": {"run_count": 0, "latest_run_id": ""},
            }
        ),
    )
    return WorkflowSessionOwner(
        bus=bus,
        persistence=SessionPersistence(store, bus),
        session_id=session_id,
        workspace=Workspace.from_cwd(str(tmp_path)),
    )


async def _prepare(owner: WorkflowSessionOwner) -> None:
    await owner.open(reconcile=True)
    await owner.prepare(
        identity=_BINDING,
        run_id=uuid4().hex,
        title="Review",
        settings=Settings(mutation_coordination=False),
        model_registry=None,
        has_agents=False,
        hooks=_hooks,
    )


@pytest.mark.parametrize("outcome,expected", [("", "orphaned"), ("completed", "completed")])
async def test_prepare_reconciles_previous_runs_before_reusing_session(
    tmp_path: Path, outcome: str, expected: str
) -> None:
    owner = await _owner(tmp_path)
    session = owner.session
    assert session.session_dir is not None and session.session_id is not None
    directory = session.session_dir / "workflows" / uuid4().hex
    await record_workflow_run(directory, session_id=session.session_id, title="Review", outcome=outcome)
    try:
        await _prepare(owner)
        assert session.guard.owns(session.session_id)
        assert read_run_terminal(directory).outcome == expected
        assert read_workflow_meta(directory, active=True).status == (
            "interrupted" if expected == "orphaned" else "completed"
        )
    finally:
        await owner.close()


async def test_prepare_restores_spill_quota_and_removes_partial_records(tmp_path: Path) -> None:
    owner = await _owner(tmp_path)
    directory = owner.session.session_dir
    assert directory is not None and owner.session.session_id is not None
    previous_quota = SpillQuota()
    spilled = write_spill_batch(
        directory,
        previous_quota,
        [ScopedGroup("review-output", "assistant_text", (Message("assistant", ["saved output"]),), True)],
        record_dir=dropped_turn_relative_path(1),
        absolute_turn=1,
        round_number=1,
        session_id=owner.session.session_id,
    )
    record = directory / spilled.entries[0].relative_path
    partial = record.with_name("orphan.md")
    partial.write_text("partial", encoding="utf-8")
    try:
        await _prepare(owner)
        assert owner.session.spill_quota.storage_available
        assert owner.session.spill_quota.spent_bytes == previous_quota.spent_bytes == record.stat().st_size
        assert not partial.exists()
    finally:
        await owner.close()


@pytest.mark.parametrize("routine", ["reconcile_orphaned_runs", "reconcile_spill_storage"])
async def test_auxiliary_reconciliation_failure_preserves_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, routine: str
) -> None:
    owner = await _owner(tmp_path)
    real = (
        session_module.reconcile_orphaned_runs
        if routine == "reconcile_orphaned_runs"
        else session_module.reconcile_spill_storage
    )
    monkeypatch.setattr(session_module, routine, create_autospec(real, side_effect=OSError("unreadable artifacts")))
    try:
        await _prepare(owner)
        assert owner.identity == _BINDING
        assert owner.session.spill_quota.storage_available == (routine != "reconcile_spill_storage")
    finally:
        await owner.close()


async def test_cancelled_prepare_drains_reconciliation_before_releasing_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await _owner(tmp_path)
    session_id = owner.session.session_id
    assert session_id is not None
    entered, release = Event(), Event()
    real = session_module.reconcile_orphaned_runs

    def blocked(directory: Path):
        entered.set()
        assert release.wait(10), "test did not release reconciliation"
        assert owner.session.guard.owns(session_id)
        return real(directory)

    monkeypatch.setattr(session_module, "reconcile_orphaned_runs", create_autospec(real, side_effect=blocked))

    async def prepare_and_close() -> None:
        try:
            await _prepare(owner)
        finally:
            await owner.close()

    task = asyncio.create_task(prepare_and_close())
    try:
        await wait_for(entered.is_set, description="workflow reconciliation started")
        task.cancel()
        assert owner.session.guard.owns(session_id)
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not owner.session.guard.owns(session_id)


@pytest.mark.parametrize("cancel_update", [False, True])
async def test_close_waits_for_model_save_and_rejects_late_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_update: bool
) -> None:
    owner = await _owner(tmp_path)
    await _prepare(owner)
    await owner.save()
    model = WorkflowModelSelection("review", "Review", "mock")
    entered, release = asyncio.Event(), asyncio.Event()
    real_write = owner._write_state
    tasks = []

    async def write(state):
        entered.set()
        await release.wait()
        assert owner.session.guard.owns(owner.session.session_id)
        await real_write(state)

    monkeypatch.setattr(owner, "_write_state", create_autospec(real_write, side_effect=write))

    async def change() -> None:
        async with owner.edit() as available:
            assert available
            await owner.set_model(model)

    try:
        update = asyncio.create_task(change())
        tasks.append(update)
        await wait_for(entered.is_set, description="model checkpoint is writing")
        if cancel_update:
            update.cancel()
        close = asyncio.create_task(owner.close())
        tasks.append(close)
        release.set()
        results = await asyncio.gather(update, close, return_exceptions=True)
        assert results[1] is None
        assert isinstance(results[0], asyncio.CancelledError) if cancel_update else results[0] is None
        assert not owner.session.guard.owns(owner.session.session_id)
        assert owner.state is not None and owner.state.model == model
        with pytest.raises(ValueError, match="not open"):
            await owner.set_model(model)
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await owner.close()


async def test_admission_rollback_restores_the_previous_surface(tmp_path: Path) -> None:
    owner = await _owner(tmp_path)
    store = owner.persistence.state_store
    assert store is not None and owner.session.session_id is not None
    try:
        await owner.open(reconcile=True)
        owner.require_state().last_surface = SessionSurface.ACP.value
        await owner.save()
        await _prepare(owner)
        owner.require_state().last_surface = SessionSurface.CLI.value
        await owner.save()
        assert (await store.load_workflow_session(owner.session.session_id)).surface is SessionSurface.CLI

        await owner.discard_admission()

        assert owner.require_state().surface is SessionSurface.ACP
        assert (await store.load_workflow_session(owner.session.session_id)).surface is SessionSurface.ACP
    finally:
        await owner.close()


async def test_admission_rollback_removes_rejected_run(tmp_path: Path) -> None:
    owner = await _owner(tmp_path)
    try:
        await _prepare(owner)
        # A successful replacement can still be followed by caller cancellation
        # before admission commits. Roll back the uncommitted run.
        await owner.save()
        await owner.discard_admission()
        assert owner.state is not None
        assert owner.state.run_count == 0
        store = owner.persistence.state_store
        assert store is not None and owner.session.session_id is not None
        state = (await store.load_workflow_session(owner.session.session_id)).encode()
        assert state is not None
        assert state["summary"] == {"run_count": 0, "latest_run_id": ""}
        assert "approval_mode" not in state
    finally:
        await owner.close()
