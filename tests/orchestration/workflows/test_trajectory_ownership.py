# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Idle workflow hosts release both execution and recording leases for the next owner."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import WorkflowRunAccepted
from chrys.foundation.trajectory.context import current_trajectory
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.lease import WRITER_LEASE_FILE_NAME, WriterLease
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.foundation.util.lock import FileLock
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.service.analytics import analyze_trajectory
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HooksFile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import SessionTrajectory, trajectory_events_path
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, run, write_workflow
from tests.support.trajectory_invariants import assert_trajectory_accounted, assert_trajectory_operation_settlement
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_two_open_hosts_can_alternate_runs_without_losing_trajectory(tmp_path: Path, terminal: str) -> None:
    project = make_workdir(tmp_path)
    body = "raise ValueError('failure')" if terminal == "failed" else "return text"
    write_workflow(project, "review", python_workflow(f"def check(text):\n    {body}\n", "check"))
    first = make_host(tmp_path, project=project)
    second = make_host(tmp_path, project=project)
    results = []

    async def cancel_first(event: WorkflowRunAccepted) -> None:
        await first.cancel_workflow()

    async def cancel_second(event: WorkflowRunAccepted) -> None:
        await second.cancel_workflow()

    if terminal == "cancelled":
        await first.event_bus.subscribe(WorkflowRunAccepted, cancel_first)
        await second.event_bus.subscribe(WorkflowRunAccepted, cancel_second)
    try:
        await confirm(first, "review")
        for index, host in enumerate((first, second, first, second)):
            if index == 1:
                await second.load_workflow_session(first.workflow_session_id)
            result, _ = await run(host, "review")
            assert result.outcome.value == ("node_failed" if terminal == "failed" else terminal)
            results.append(result)
            directory = host.workflow_session_dir
            assert directory is not None
            # This is the execution handoff boundary, not host shutdown.
            with FileLock(
                JsonFileStateStore(tmp_path / "sessions").active_lock_path(host.workflow_session_id), timeout=0
            ):
                assert not WriterLease.is_held_elsewhere(
                    trajectory_events_path(directory).parent / WRITER_LEASE_FILE_NAME
                )
            read = read_trajectory(trajectory_events_path(directory))
            assert_trajectory_accounted(read)
            assert_trajectory_operation_settlement(read.events)
            starts = [event for event in read.events if event.event_type == EventType.WORKFLOW_RUN_STARTED]
            finishes = [event for event in read.events if event.event_type == EventType.WORKFLOW_RUN_FINISHED]
            assert [event.operation_id for event in starts] == [item.run_id for item in results]
            assert [event.operation_id for event in finishes] == [item.run_id for item in results]
            assert len({event.runtime_id for event in starts}) == len(results)
        analysis = analyze_trajectory(trajectory_events_path(directory))
        assert len(analysis.workflow_runs) == 4
    finally:
        await first.event_bus.unsubscribe(WorkflowRunAccepted, cancel_first)
        await second.event_bus.unsubscribe(WorkflowRunAccepted, cancel_second)
        await second.shutdown()
        await first.shutdown()


async def test_run_guard_is_held_until_trajectory_writer_finishes_closing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    closing, release = asyncio.Event(), asyncio.Event()
    original_close = SessionTrajectory.close

    async def close(recorder, *, reason):
        closing.set()
        await release.wait()
        return await original_close(recorder, reason=reason)

    monkeypatch.setattr(SessionTrajectory, "close", create_autospec(original_close, side_effect=close))
    task = None
    try:
        await confirm(host, "review")
        task = asyncio.create_task(run(host, "review"))
        await wait_for(
            lambda: closing.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT, description="writer close started"
        )
        if task.done():
            await task
        assert closing.is_set()
        directory = host.workflow_session_dir
        assert directory is not None
        lock = FileLock(JsonFileStateStore(tmp_path / "sessions").active_lock_path(host.workflow_session_id), timeout=0)
        with pytest.raises(TimeoutError), lock:
            pass
        assert WriterLease.is_held_elsewhere(trajectory_events_path(directory).parent / WRITER_LEASE_FILE_NAME)
        release.set()
        result, _ = await asyncio.wait_for(asyncio.shield(task), timeout=ENGINE_TURN_TIMEOUT)
        assert result.outcome.value == "completed"
        with lock:
            assert not WriterLease.is_held_elsewhere(trajectory_events_path(directory).parent / WRITER_LEASE_FILE_NAME)
    finally:
        release.set()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        await host.shutdown()


async def test_idle_host_end_hooks_do_not_acquire_writer_during_another_hosts_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    managers = []
    end_entered, release_end = asyncio.Event(), asyncio.Event()
    end_contexts = []
    close_task = None
    real_fire = HookManager.fire

    async def build(factory, *, workdir, workdir_hooks_enabled, session_id, request_id=""):
        manager = HookManager(file=HooksFile(), hooks_dir=tmp_path / "hooks")
        managers.append(manager)
        return manager

    async def fire(
        manager,
        event,
        payload,
        *,
        scope="turn",
        target_operation_id=None,
        trajectory_context=None,
        origin=None,
        operation=None,
    ):
        if event is HookEvent.SESSION_END and manager is managers[0]:
            end_contexts.append(current_trajectory())
            end_entered.set()
            await release_end.wait()
        return await real_fire(
            manager,
            event,
            payload,
            scope=scope,
            target_operation_id=target_operation_id,
            trajectory_context=trajectory_context,
            origin=origin,
            operation=operation,
        )

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    monkeypatch.setattr(HookManager, "fire", create_autospec(real_fire, side_effect=fire))
    first, second = make_host(tmp_path, project=project), make_host(tmp_path, project=project)

    async def close_first(event: WorkflowRunAccepted) -> None:
        nonlocal close_task
        close_task = asyncio.create_task(first.shutdown())
        await wait_for(
            lambda: end_entered.is_set() or close_task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="idle host end hook dispatched during the next host's admission",
        )
        if close_task.done():
            await close_task
        assert end_entered.is_set()

    try:
        await confirm(first, "review")
        original, _ = await run(first, "review")
        await second.load_workflow_session(first.workflow_session_id)
        await second.event_bus.subscribe(WorkflowRunAccepted, close_first)
        following, _ = await asyncio.wait_for(run(second, "review"), timeout=ENGINE_TURN_TIMEOUT)
        assert following.outcome.value == "completed"
        assert end_contexts == [None]  # the other host owns the guard before activating its writer
        assert close_task is not None and not close_task.done()
        directory = second.workflow_session_dir
        assert directory is not None
        read = read_trajectory(trajectory_events_path(directory))
        assert_trajectory_accounted(read)
        assert_trajectory_operation_settlement(read.events)
        assert [event.operation_id for event in read.events if event.event_type == EventType.WORKFLOW_RUN_STARTED] == [
            original.run_id,
            following.run_id,
        ]
    finally:
        release_end.set()
        await second.event_bus.unsubscribe(WorkflowRunAccepted, close_first)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await first.shutdown()
        await second.shutdown()
