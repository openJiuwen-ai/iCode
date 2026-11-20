# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow completion includes the final session checkpoint, after execution resources drain."""

from __future__ import annotations

import asyncio
import errno
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import UsageUpdate, WorkflowNodeStateChanged, WorkflowRunFinished
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.kernel import UsageDetails
from chrys.orchestration.workflows.runner import WorkflowRunResult
from chrys.orchestration.workflows.session import WorkflowSessionOwner
from chrys.orchestration.workflows.worker_client import WorkflowWorkerClient
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.protocol import LIMITS
from chrys.service.workflows.store import RunRecord
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    write_workflow,
)
from tests.support.event_capture import capture_event_sequence
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
@pytest.mark.parametrize("save_fails", [False, True])
async def test_run_terminal_waits_for_session_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: str,
    save_fails: bool,
) -> None:
    project = make_workdir(tmp_path)
    target = project / "target.txt"
    target.write_text("before")
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(responses=[]),
            MockChatClient(
                responses=[
                    MockResponse(
                        tool_calls=[
                            ("write_file", "write", {"path": str(target), "content": "after", "overwrite": True})
                        ],
                        usage_details=UsageDetails(input_token_count=20, output_token_count=1),
                    ),
                    MockResponse(text="done", usage_details=UsageDetails(input_token_count=7, output_token_count=1)),
                ]
            ),
        ],
        builtin_tools=True,
    )
    body = {
        "completed": "return text",
        "failed": "raise ValueError('node failed')",
        "cancelled": "await asyncio.Event().wait()",
    }[terminal]
    write_workflow(
        project,
        "write",
        (
            "import asyncio\n"
            "from chrys.workflows import WorkflowBuilder\n"
            f"async def finish(text):\n    {body}\n"
            "wf = WorkflowBuilder('write')\n"
            "writer = wf.agent('writer', profile='Headless')\n"
            "last = wf.python('last', finish)\n"
            "wf.start(writer)\nwf.chain(writer, last)\nwf.output(last)\nworkflow = wf.build()\n"
        ).encode(),
    )
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.write"])])
    store = JsonFileStateStore(tmp_path / "sessions")
    checkpoint_entered, release_checkpoint = asyncio.Event(), asyncio.Event()
    save_calls = 0
    workers_closed: list[int] = []
    checkpoint_usage: list[int] = []
    real_save, real_close = WorkflowSessionOwner.save, WorkflowWorkerClient.close

    async def save(owner: WorkflowSessionOwner) -> None:
        nonlocal save_calls
        save_calls += 1
        if save_calls > 1:
            checkpoint_usage.append(owner.session.runtime_meta.total_session_tokens)
            checkpoint_entered.set()
            await release_checkpoint.wait()
            if save_fails:
                raise OSError(errno.ENOSPC, "No space left on device")
        await real_save(owner)

    async def close(worker: WorkflowWorkerClient, *, grace: float = LIMITS.shutdown_grace) -> None:
        await real_close(worker, grace=grace)
        workers_closed.append(worker._process.pid)

    async def cancel_last(event: WorkflowNodeStateChanged) -> None:
        if terminal == "cancelled" and event.node_id == "last" and event.state == "running":
            await host.cancel_workflow()

    await host.event_bus.subscribe(WorkflowNodeStateChanged, cancel_last)
    caller: asyncio.Task[WorkflowRunResult] | None = None
    try:
        await confirm(host, "write")
        monkeypatch.setattr(WorkflowSessionOwner, "save", create_autospec(real_save, side_effect=save))
        monkeypatch.setattr(WorkflowWorkerClient, "close", create_autospec(real_close, side_effect=close))
        async with capture_event_sequence(host.event_bus, WorkflowRunFinished, UsageUpdate) as events:
            caller = asyncio.create_task(
                host.run_workflow_until_final(host.workflow_target("write"), input_text="write the file")
            )
            await wait_for(
                lambda: checkpoint_entered.is_set() or caller.done(),
                description="final checkpoint started",
                timeout=ENGINE_TURN_TIMEOUT,
            )
            if caller.done():
                await caller
            assert checkpoint_entered.is_set()
            assert not of_type(events, WorkflowRunFinished)
            assert target.read_text() == "after" and checkpoint_usage == [29]
            assert len(workers_closed) == 1
            assert host.engine.workflows.active_run_id is not None
            assert host.engine.workflows.result(host.engine.workflows.active_run_id) is None
            assert of_type(events, UsageUpdate)[-1].total_session_tokens == 29
            release_checkpoint.set()
            result = await caller
            (finished,) = of_type(events, WorkflowRunFinished)

        expected = "storage_failed" if save_fails else "node_failed" if terminal == "failed" else terminal
        assert finished.outcome == result.outcome.value == expected
        assert finished.error == result.error
        assert save_calls == 2
        if save_fails:
            assert "Session checkpoint" in result.error and "No space left on device" in result.error
        if terminal == "failed":
            assert "node failed" in result.error
        assert host.engine.execution() == ExecutionSnapshot("idle")
        assert host.engine.workflows.result(result.run_id) is result
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, result.run_id)
        assert read_run_terminal(directory).outcome == expected
        journal = read_trajectory(directory / "events.jsonl")
        (record,) = [event for event in journal.events if event.event_type == RunRecord.RUN_FINISHED]
        assert record.payload["outcome"] == expected and record.sequence == finished.seq
        trace = read_trajectory(trajectory_events_path(session_dir))
        (run_finish,) = [event for event in trace.events if event.event_type == EventType.WORKFLOW_RUN_FINISHED]
        assert run_finish.payload["outcome"] == expected
        state = (await store.load_workflow_session(host.workflow_session_id)).encode()
        assert state is not None
        assert SessionRuntimeMetadata.from_state_dict(state).total_session_tokens == (0 if save_fails else 29)
        restored = MutationTracker.deserialize(state["chrys_mutations"], SnapshotStore(session_dir))
        assert restored.get_file_edit_snapshots() == ([] if save_fails else [("before", "after")])
    finally:
        release_checkpoint.set()
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, cancel_last)
        await host.shutdown()
        if caller is not None:
            await asyncio.gather(caller, return_exceptions=True)
