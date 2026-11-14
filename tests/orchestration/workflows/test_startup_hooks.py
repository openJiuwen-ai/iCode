# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run cancellation includes blocking startup hooks and their owned subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import sys
from pathlib import Path
from unittest.mock import create_autospec

import psutil
import pytest
from filelock import FileLock

from chrys.foundation.events.types import WorkflowRunAccepted, WorkflowRunFinished, WorkflowRunStarted
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.orchestration.workflows.hooks import WorkflowSessionHooks
from chrys.orchestration.workflows.runner import WorkflowRunner
from chrys.orchestration.workflows.worker_client import WorkflowWorkerClient
from chrys.service.analytics import analyze_trajectory
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.store import RunRecord, read_run_events
from tests.orchestration.workflows._hosting import (
    confirm,
    hold_workflow_deadline,
    make_host,
    make_project,
    run,
    write_workflow,
)
from tests.support.processes import published_pid
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("cancellation", ["user_cancel", "deadline_exceeded", "shutdown"])
@pytest.mark.parametrize(
    "hook_event", [HookEvent.SESSION_START, HookEvent.SESSION_RESTORED, HookEvent.WORKFLOW_RUN_START]
)
async def test_blocking_startup_hook_is_cancelled_and_drained(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation: str, hook_event: HookEvent
) -> None:
    project = make_project(tmp_path)
    node_marker, hook_marker = tmp_path / "node-ran", tmp_path / "hook-pid"
    write_workflow(
        project,
        "review",
        python_workflow(
            f"from pathlib import Path\ndef check(text):\n    Path({str(node_marker)!r}).touch()\n    return text\n",
            "check",
        ),
    )
    hook = HookConfig(
        id="blocking-start",
        event=hook_event,
        run=HookRun(
            type="command",
            argv=[
                sys.executable,
                "-c",
                (
                    f"import os, time; from pathlib import Path; marker = Path({str(hook_marker)!r}); "
                    "pending = marker.with_suffix('.tmp'); pending.write_text(str(os.getpid())); pending.replace(marker); "
                    "time.sleep(3600)"
                ),
            ],
        ),
        execution=HookExecution(mode="blocking", timeout_seconds=3600),
    )

    async def build(factory, *, project_root, project_hooks_enabled, session_id, request_id=""):
        return HookManager(file=HooksFile(hooks=[hook]), hooks_dir=tmp_path / "hooks")

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    workers = []
    real_launch = WorkflowWorkerClient.launch

    async def launch(**kwargs):
        worker = await real_launch(**kwargs)
        workers.append(psutil.Process(worker._process.pid))
        return worker

    monkeypatch.setattr(WorkflowWorkerClient, "launch", create_autospec(real_launch, side_effect=launch))
    expire = hold_workflow_deadline(monkeypatch, 3601) if cancellation == "deadline_exceeded" else None
    host = make_host(tmp_path, project=project)
    task = None
    hook_process = None
    try:
        await confirm(host, "review")
        if hook_event is HookEvent.SESSION_RESTORED:
            await run(host, "review")
            session_id = host.workflow_session_id
            await host.shutdown()
            node_marker.unlink()
            host = make_host(tmp_path, project=project)
            await host.load_workflow_session(session_id)
        task = asyncio.create_task(run(host, "review", timeout=3601 if expire is not None else 0))
        hook_pid: int | None = None

        def hook_started() -> bool:
            nonlocal hook_pid
            hook_pid = published_pid(hook_marker)
            return hook_pid is not None or task.done()

        await wait_for(
            hook_started,
            timeout=ENGINE_TURN_TIMEOUT,
            description="real startup hook has entered its blocking command",
        )
        if task.done():
            await task
        assert hook_pid is not None
        hook_process = psutil.Process(hook_pid)
        if expire is not None:
            expire.set()
        elif cancellation == "shutdown":
            await asyncio.wait_for(host.shutdown(), timeout=ENGINE_TURN_TIMEOUT)
        else:
            await host.cancel_workflow()
        result, events = await asyncio.wait_for(asyncio.shield(task), timeout=ENGINE_TURN_TIMEOUT)
        assert result.outcome.value == "cancelled"
        assert result.reason == ("" if cancellation == "user_cancel" else cancellation)
        assert len([event for event in events if isinstance(event, WorkflowRunStarted)]) == 1
        assert len([event for event in events if isinstance(event, WorkflowRunFinished)]) == 1
        assert not node_marker.exists()
        assert not hook_process.is_running()
        assert workers and all(not process.is_running() for process in workers)
        assert host.engine.workflows.active_source is None
        store = JsonFileStateStore(tmp_path / "sessions")
        with FileLock(store.active_lock_path(host.workflow_session_id), timeout=0):
            pass
    finally:
        # Keep a regression failure bounded even if the old runner cannot cancel
        # the hook: kill only this test's child before draining its owner.
        if hook_process is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                hook_process.kill()
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    session_dir = host.workflow_session_dir
    assert session_dir is not None
    events = read_trajectory(trajectory_events_path(session_dir)).events
    starts = [event for event in events if event.event_type == EventType.HOOK_OPERATION_STARTED]
    finishes = [event for event in events if event.event_type == EventType.HOOK_OPERATION_FINISHED]
    assert len(starts) == len(finishes) == 1
    assert starts[0].payload["hook_event"] == str(hook_event)
    assert finishes[0].operation_id == starts[0].operation_id
    assert finishes[0].payload["outcome"] == "cancelled"
    assert starts[0].parent_operation_id == (result.run_id if hook_event is HookEvent.WORKFLOW_RUN_START else None)


async def test_startup_observer_failure_still_publishes_terminal_and_releases_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    real_event = WorkflowSessionHooks.run_event

    async def run_event(hooks, session_id, event, **values):
        if event is HookEvent.WORKFLOW_RUN_START:
            raise RuntimeError("injected startup failure")
        return await real_event(hooks, session_id, event, **values)

    monkeypatch.setattr(WorkflowSessionHooks, "run_event", create_autospec(real_event, side_effect=run_event))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        result, events = await asyncio.wait_for(run(host, "review"), timeout=ENGINE_TURN_TIMEOUT)
        assert result.reason == "internal_error"
        assert len([event for event in events if isinstance(event, WorkflowRunFinished)]) == 1
        assert host.engine.workflows.active_source is None
    finally:
        await host.shutdown()


@pytest.mark.parametrize("cancel_at", [WorkflowRunAccepted, WorkflowRunStarted])
@pytest.mark.parametrize("shutdown", [False, True])
@pytest.mark.parametrize("blocking_event", [None, HookEvent.SESSION_START, HookEvent.WORKFLOW_RUN_START])
async def test_immediate_cancellation_keeps_paired_run_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel_at: type,
    shutdown: bool,
    blocking_event: HookEvent | None,
) -> None:
    project = make_project(tmp_path)
    marker = tmp_path / "node-ran"
    write_workflow(
        project,
        "review",
        python_workflow(
            f"from pathlib import Path\ndef check(text):\n    Path({str(marker)!r}).touch()\n    return text\n", "check"
        ),
    )
    hooks_seen = []
    interrupted = []
    original_fire = HookManager.fire

    async def fire(manager, event, payload, **kwargs):
        hooks_seen.append(event)
        if event is blocking_event:
            try:
                await asyncio.Event().wait()
            finally:
                interrupted.append(event)
        return await original_fire(manager, event, payload, **kwargs)

    async def build(factory, *, project_root, project_hooks_enabled, session_id, request_id=""):
        return HookManager(file=HooksFile(), hooks_dir=tmp_path / "hooks")

    monkeypatch.setattr(HookManager, "fire", create_autospec(original_fire, side_effect=fire))
    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    terminal = asyncio.Event()
    on_terminal = WorkflowRunner._on_terminal

    def settled(runner, decision):
        on_terminal(runner, decision)
        terminal.set()

    monkeypatch.setattr(WorkflowRunner, "_on_terminal", create_autospec(on_terminal, side_effect=settled))
    host = make_host(tmp_path, project=project)

    async def cancel_inline(event):
        if shutdown:
            await host.shutdown()
        else:
            await host.cancel_workflow()
        # Keep the callback open until cancellation has actually settled. This
        # reproduces the lost-start case without relying on event-loop timing.
        await asyncio.wait_for(terminal.wait(), timeout=ENGINE_TURN_TIMEOUT)

    await host.event_bus.subscribe(cancel_at, cancel_inline)
    try:
        await confirm(host, "review")
        result, events = await asyncio.wait_for(run(host, "review"), timeout=ENGINE_TURN_TIMEOUT)
        assert result.outcome.value == "cancelled"
        assert result.reason == ("shutdown" if shutdown else "")
        assert not marker.exists()
        assert [type(event) for event in events if isinstance(event, (WorkflowRunStarted, WorkflowRunFinished))] == [
            WorkflowRunStarted,
            WorkflowRunFinished,
        ]
        assert hooks_seen[:3] == [HookEvent.SESSION_START, HookEvent.WORKFLOW_RUN_START, HookEvent.WORKFLOW_RUN_END]
        assert interrupted == ([] if blocking_event is None else [blocking_event])
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        history = read_run_events(run_dir(session_dir, result.run_id))
        assert [event.event_type for event in history.events if event.event_type.startswith("workflow.")] == [
            RunRecord.RUN_STARTED,
            RunRecord.RUN_FINISHED,
        ]
    finally:
        await host.event_bus.unsubscribe(cancel_at, cancel_inline)
        await host.shutdown()
    recorded = read_trajectory(trajectory_events_path(session_dir)).events
    run_events = [
        event
        for event in recorded
        if event.event_type in (EventType.WORKFLOW_RUN_STARTED, EventType.WORKFLOW_RUN_FINISHED)
    ]
    assert [event.event_type for event in run_events] == [
        EventType.WORKFLOW_RUN_STARTED,
        EventType.WORKFLOW_RUN_FINISHED,
    ]
    assert all(event.operation_id == result.run_id for event in run_events)
    analysis = analyze_trajectory(trajectory_events_path(session_dir))
    assert [(item.run_id, item.outcome) for item in analysis.workflow_runs] == [(result.run_id, "cancelled")]
