# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent archive failures participate in the run's durable completion boundary."""

from __future__ import annotations

import asyncio
import errno
import json
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.service.workflows.store as store_module
from chrys.foundation.events.types import WorkflowRunFinished
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.service.llm.mock import MockChatClient
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.store import RunRecord
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.orchestration.workflows._transcript_support import ArchiveClient, source
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


@pytest.mark.parametrize(
    "backend,terminal", [("kernel", "completed"), ("kernel", "failed"), ("kernel", "cancelled"), ("acp", "completed")]
)
@pytest.mark.parametrize("record_kind", ["session", "usage"])
async def test_terminal_archive_failure_reaches_result_events_and_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str, terminal: str, record_kind: str
) -> None:
    project = make_workdir(tmp_path)
    target = project / "input.txt"
    target.write_text("readable input")
    client = ArchiveClient(target, terminal)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client], builtin_tools=True)
    profiles = [make_profile(builtins=["filesystem.read"])]
    workflow = source()
    if backend == "acp":
        profiles.append(
            AgentProfile(
                name="External",
                acp=AcpAgentConfig(command=sys.executable, args=[str(STUB_SCRIPT)]),
            )
        )
        workflow = workflow.replace(b"profile='Headless'", b"profile='External'")
    write_workflow(project, "archive", workflow)
    host = make_host(tmp_path, project=project, profiles=profiles)
    real_write = store_module.atomic_write_owner_only_bytes
    terminal_statuses: list[str] = []
    failed_paths: list[Path] = []

    def write(path: Path, payload: bytes, *, create_parents: bool = True) -> None:
        if path.parent.name == "nodes":
            if ".session." in path.name:
                status = json.loads(payload)["meta"]["status"]
                if status != "running":
                    terminal_statuses.append(status)
            if terminal_statuses and f".{record_kind}." in path.name:
                failed_paths.append(path)
                raise OSError(errno.ENOSPC, "No space left on device", str(path))
        real_write(path, payload, create_parents=create_parents)

    task = None
    try:
        await confirm(host, "archive")
        monkeypatch.setattr(
            store_module, "atomic_write_owner_only_bytes", create_autospec(real_write, side_effect=write)
        )
        task = asyncio.create_task(run(host, "archive", input_text="Read twice"))
        if terminal == "cancelled":
            await wait_for(lambda: client.waiting.is_set() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
            if task.done():
                await task
            assert client.waiting.is_set()
            await host.cancel_workflow()
        result, events = await task
        assert terminal_statuses == [terminal]
        assert len(failed_paths) == 1
        assert result.outcome.value == "storage_failed"
        assert "node@iter#1" in result.error and "No space left on device" in result.error
        (finished,) = of_type(events, WorkflowRunFinished)
        assert (finished.outcome, finished.error) == (result.outcome.value, result.error)
        assert host.engine.execution() == ExecutionSnapshot("idle")
        assert host.engine.workflows.result(result.run_id) is result
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, result.run_id)
        assert read_run_terminal(directory).outcome == "storage_failed"
        journal = read_trajectory(directory / "events.jsonl")
        (record,) = [event for event in journal.events if event.event_type == RunRecord.RUN_FINISHED]
        assert (record.payload["outcome"], record.payload["error"]) == ("storage_failed", result.error)
        trace = read_trajectory(trajectory_events_path(session_dir))
        (run_finish,) = [event for event in trace.events if event.event_type == EventType.WORKFLOW_RUN_FINISHED]
        assert run_finish.payload["outcome"] == "storage_failed"
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
