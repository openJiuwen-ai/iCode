# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Chat and workflow execution share one in-memory approval mode for the launch."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import ApprovalModeUpdated, SetApprovalMode
from chrys.orchestration.workflows.runner import WorkflowRunner
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.service.approval.policy import ApprovalMode
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, run, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("mode", list(ApprovalMode))
@pytest.mark.parametrize("interactive", [False, True])
async def test_workflows_share_launch_mode_across_changes_new_sessions_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: ApprovalMode, interactive: bool
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "echo", python_workflow("def echo(value):\n    return value\n", "echo"))
    observed: list[ApprovalMode] = []
    real_run = WorkflowRunner.run

    async def observe(runner, input_text, *, startup, record_end):
        observed.append(runner._resources.approval_mode())
        return await real_run(runner, input_text, startup=startup, record_end=record_end)

    monkeypatch.setattr(WorkflowRunner, "run", create_autospec(real_run, side_effect=observe))
    host = make_host(tmp_path, project=project, allow_user_interaction=interactive, approval_mode=mode)
    store = JsonFileStateStore(tmp_path / "sessions")
    changed = ApprovalMode.MANUAL if mode is ApprovalMode.BYPASS else ApprovalMode.BYPASS
    try:
        default = host.engine.settings.default_approval_mode
        await confirm(host, "echo")
        await run(host, "echo")
        session_id = host.workflow_session_id
        await host.event_bus.publish(SetApprovalMode(mode=changed.value, persist=False), raise_handler_errors=True)
        await run(host, "echo")
        await run(host, "echo", new_session=True)
        for identity in (session_id, host.workflow_session_id):
            state = await store.load_workflow_session(identity)
            assert state is not None and "approval_mode" not in state.encode()
        assert observed == [mode, changed, changed]
        assert host.engine.approval_mode is changed
        assert host.engine.settings.default_approval_mode == default
    finally:
        await host.shutdown()
    restored = make_host(tmp_path, project=project, allow_user_interaction=interactive, approval_mode=mode)
    try:
        await restored.load_workflow_session(session_id)
        await run(restored, "echo")
        assert observed[-1] is mode
    finally:
        await restored.shutdown()


async def test_workflow_scoped_approval_requests_are_ignored_and_launch_changes_do_not_save_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "echo", python_workflow("def echo(value):\n    return value\n", "echo"))
    host = make_host(tmp_path, project=project)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "echo")
        await run(host, "echo")
        session_id = host.workflow_session_id
        checkpoint = store.session_dir(session_id) / "session.json"
        before = checkpoint.read_bytes()
        save = create_autospec(JsonFileStateStore.save_workflow_session)
        monkeypatch.setattr(JsonFileStateStore, "save_workflow_session", save)
        async with capture_event_sequence(host.event_bus, ApprovalModeUpdated) as updates:
            for identity in (session_id, "unknown-session"):
                await host.event_bus.publish(
                    SetApprovalMode(session_id=identity, mode="manual", persist=False), raise_handler_errors=True
                )
            assert not updates
            assert host.engine.approval_mode is ApprovalMode.BYPASS
            for mode in ("manual", "bypass", "manual"):
                await host.event_bus.publish(SetApprovalMode(mode=mode, persist=False), raise_handler_errors=True)
                assert host.engine.approval_mode is ApprovalMode(mode)
            assert [event.mode for event in updates] == ["manual", "bypass", "manual"]
            assert all(event.session_id == host.session_id for event in updates)
        save.assert_not_called()
        assert checkpoint.read_bytes() == before
    finally:
        await host.shutdown()


async def test_mode_changes_during_admission_are_visible_before_the_runner_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "echo", python_workflow("def echo(value):\n    return value\n", "echo"))
    host = make_host(tmp_path, project=project)
    entered, release = asyncio.Event(), asyncio.Event()
    real_open, real_run = WorkflowSessionOwner.open, WorkflowRunner.run
    observed: list[ApprovalMode] = []

    async def open_owner(owner: WorkflowSessionOwner, *, reconcile: bool = False) -> None:
        await real_open(owner, reconcile=reconcile)
        entered.set()
        await release.wait()

    async def observe(runner, input_text, *, startup, record_end):
        observed.append(runner._resources.approval_mode())
        return await real_run(runner, input_text, startup=startup, record_end=record_end)

    monkeypatch.setattr(WorkflowSessionOwner, "open", create_autospec(real_open, side_effect=open_owner))
    monkeypatch.setattr(WorkflowRunner, "run", create_autospec(real_run, side_effect=observe))
    execution = None
    try:
        await confirm(host, "echo")
        execution = asyncio.create_task(run(host, "echo"))
        await wait_for(lambda: entered.is_set() or execution.done(), description="workflow admission is suspended")
        if execution.done():
            await execution
        assert entered.is_set()
        for mode in ("manual", "bypass", "manual"):
            await host.event_bus.publish(SetApprovalMode(mode=mode, persist=False), raise_handler_errors=True)
        release.set()
        result, _ = await execution
        assert result.outcome.value == "completed"
        assert observed == [ApprovalMode.MANUAL]
    finally:
        release.set()
        if execution is not None:
            await asyncio.gather(execution, return_exceptions=True)
        await host.shutdown()
