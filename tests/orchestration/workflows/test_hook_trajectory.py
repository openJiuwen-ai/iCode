# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Session and run hooks remain recorded across run rebuilds and host shutdown."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import SessionDelete, SessionDeleted
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.service.analytics import analyze_trajectory
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile
from chrys.service.trajectory.session import trajectory_events_path
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, run, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.trajectory_invariants import assert_trajectory_accounted, assert_trajectory_operation_settlement
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("mode", ["blocking", "async"])
async def test_lifecycle_hooks_have_session_or_run_parent_and_finish_before_recorder_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    hook_events = (
        HookEvent.SESSION_START,
        HookEvent.SESSION_RESTORED,
        HookEvent.SESSION_END,
        HookEvent.WORKFLOW_RUN_START,
        HookEvent.WORKFLOW_RUN_END,
    )
    config = HooksFile(
        hooks=[
            HookConfig(
                id=str(event),
                event=event,
                run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
                execution=HookExecution(mode=mode),
            )
            for event in hook_events
        ]
    )

    async def build(factory, *, workdir, workdir_hooks_enabled, session_id, request_id=""):
        return HookManager(file=config, hooks_dir=tmp_path / "hooks")

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        first, _ = await run(host, "review")
        second, _ = await run(host, "review")
        session_id, session_dir = host.workflow_session_id, host.workflow_session_dir
        assert session_dir is not None
    finally:
        await host.shutdown()
    # Explicit restore creates another runtime for the same persistent session.
    host = make_host(tmp_path, project=project)
    try:
        await host.load_workflow_session(session_id)
        third, _ = await run(host, "review")
    finally:
        await host.shutdown()
    path = trajectory_events_path(session_dir)
    read = read_trajectory(path)
    assert_trajectory_accounted(read)
    assert_trajectory_operation_settlement(read.events)
    hooks = [event for event in read.events if event.event_type == EventType.HOOK_OPERATION_STARTED]
    ends = {event.operation_id: event for event in read.events if event.event_type == EventType.HOOK_OPERATION_FINISHED}
    assert len(hooks) == len(ends) == 10
    assert all(ends[event.operation_id].payload["outcome"] == "success" for event in hooks)
    sessions = [event for event in hooks if event.payload["hook_event"].startswith("session_")]
    assert [event.payload["hook_event"] for event in sessions] == [
        "session_start",
        "session_end",
        "session_restored",
        "session_end",
    ]
    assert all(event.parent_operation_id is None for event in sessions)
    runtime_ends = [event for event in read.events if event.event_type == EventType.RUNTIME_FINISHED]
    # Each run and idle session-end phase owns its writer only while holding
    # the session guard. Session hooks still fire once per host attachment.
    assert len(runtime_ends) == 5
    for event in sessions:
        runtime_end = next(end for end in runtime_ends if end.runtime_id == event.runtime_id)
        assert ends[event.operation_id].sequence < runtime_end.sequence
    for result in (first, second, third):
        assert result.outcome.value == "completed"
        run_hooks = [event for event in hooks if event.parent_operation_id == result.run_id]
        assert [event.payload["hook_event"] for event in run_hooks] == ["workflow_run_start", "workflow_run_end"]
        boundaries = [event for event in read.events if event.operation_id == result.run_id]
        assert len(boundaries) == 2
        assert boundaries[0].sequence < run_hooks[0].sequence
        assert ends[run_hooks[-1].operation_id].sequence < boundaries[-1].sequence
    analysis = analyze_trajectory(path)
    assert len(analysis.workflow_runs) == 3
    for workflow in analysis.workflow_runs:
        assert len([operation for operation in workflow.operations if operation.family == "hook.operation"]) == 2


async def test_deleted_session_end_hook_does_not_recreate_its_trajectory_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    marker = tmp_path / "session-ended"
    config = HooksFile(
        hooks=[
            HookConfig(
                id="session-end",
                event=HookEvent.SESSION_END,
                run=HookRun(
                    type="command",
                    argv=[sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ended')"],
                ),
                execution=HookExecution(mode="blocking"),
            )
        ]
    )

    async def build(factory, *, workdir, workdir_hooks_enabled, session_id, request_id=""):
        return HookManager(file=config, hooks_dir=tmp_path / "hooks")

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        await run(host, "review")
        directory = host.workflow_session_dir
        assert directory is not None and directory.is_dir()
        async with capture_event_sequence(host.event_bus, SessionDeleted) as deleted:
            await host.event_bus.publish(SessionDelete(session_id=host.workflow_session_id))
        assert len(deleted) == 1
        assert marker.read_text() == "ended"
        assert not directory.exists()
    finally:
        await host.shutdown()
    assert not directory.exists()
