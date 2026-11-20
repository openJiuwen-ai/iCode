# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session settings serialize with their owner without blocking unrelated executions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import (
    ApprovalModeUpdated,
    SetApprovalMode,
    WorkflowModelChangeRequest,
    WorkflowModelChangeResult,
)
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.service.approval.policy import ApprovalMode
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, run, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow


async def test_approval_change_does_not_wait_for_the_workflow_owner_to_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "echo", python_workflow("def echo(value):\n    return value\n", "echo"))
    host = make_host(tmp_path, project=project)
    closing, release = asyncio.Event(), asyncio.Event()
    real_release = WorkflowSessionOwner._release_guard
    execution = None

    async def release_guard(owner: WorkflowSessionOwner) -> None:
        if owner.trajectory is not None:
            closing.set()
            await release.wait()
        await real_release(owner)

    monkeypatch.setattr(
        WorkflowSessionOwner, "_release_guard", create_autospec(real_release, side_effect=release_guard)
    )
    try:
        await confirm(host, "echo")
        execution = asyncio.create_task(run(host, "echo"))
        await wait_for(lambda: closing.is_set() or execution.done(), timeout=ENGINE_TURN_TIMEOUT)
        if execution.done():
            await execution
        assert closing.is_set()
        async with capture_event_sequence(host.event_bus, ApprovalModeUpdated) as updates:
            await host.event_bus.publish(SetApprovalMode(mode="auto", persist=False), raise_handler_errors=True)
            assert [(event.session_id, event.mode) for event in updates] == [(host.session_id, "auto")]
            assert host.engine.approval_mode is ApprovalMode.AUTO
            assert not execution.done()
        release.set()
        await execution
    finally:
        release.set()
        if execution is not None:
            await asyncio.gather(execution, return_exceptions=True)
        await host.shutdown()


@pytest.mark.parametrize("same_session", [False, True])
async def test_model_change_is_fenced_only_by_its_own_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_session: bool
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "echo", python_workflow("def echo(value):\n    return value\n", "echo"))
    host = make_host(tmp_path, project=project)
    registry = host.engine.model_registry
    assert registry is not None
    registry.register(ModelProfile(id="second", name="Second", provider="mock", model_id="second"))
    entered, release = asyncio.Event(), asyncio.Event()
    real_open = WorkflowSessionOwner.open
    execution = None

    async def open_owner(owner: WorkflowSessionOwner, *, reconcile: bool = False) -> None:
        await real_open(owner, reconcile=reconcile)
        if reconcile:
            entered.set()
            await release.wait()

    try:
        await confirm(host, "echo")
        await run(host, "echo")
        session_id = host.workflow_session_id
        monkeypatch.setattr(WorkflowSessionOwner, "open", create_autospec(real_open, side_effect=open_owner))
        execution = asyncio.create_task(run(host, "echo", new_session=not same_session))
        await wait_for(lambda: entered.is_set() or execution.done())
        if execution.done():
            await execution
        assert entered.is_set()
        async with capture_event_sequence(host.event_bus, WorkflowModelChangeResult) as replies:
            await host.event_bus.publish(
                WorkflowModelChangeRequest(session_id=session_id, profile_id="second", request_id="model"),
                raise_handler_errors=True,
            )
        assert len(replies) == 1
        assert bool(replies[0].error) is same_session
        state = await JsonFileStateStore(tmp_path / "sessions").load_workflow_session(session_id)
        assert state is not None and state.model is not None
        assert state.model.profile_id == ("mock-profile" if same_session else "second")
        release.set()
        await execution
    finally:
        release.set()
        if execution is not None:
            await asyncio.gather(execution, return_exceptions=True)
        await host.shutdown()
