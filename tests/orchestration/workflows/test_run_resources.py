# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run-owned mutation periods and event delivery survive retries and history browsing."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.agent_node_build as agent_node_build_module
from chrys.foundation.events.types import WorkflowRunAccepted, WorkflowRunFinished, WorkflowRunStarted
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.workflow_workers import python_workflow


async def test_file_writes_share_one_period_across_retries_then_open_a_new_period(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The timeout is retried in place; its production backoff would only add seconds.
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    project = make_workdir(tmp_path)
    file = project / "target.txt"
    file.write_text("before")
    client = MockChatClient(responses=[])
    writes = [
        MockResponse(tool_calls=[("write_file", f"write_{i}", {"path": str(file), "content": text, "overwrite": True})])
        for i, text in enumerate(("attempt", "after", "second run"))
    ]
    monkeypatch.setattr(
        client,
        "_next_response",
        create_autospec(
            client._next_response, side_effect=[writes[0], TimeoutError("retry"), writes[1], MockResponse(text="done")]
        ),
    )
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), client, MockChatClient(responses=[writes[2], MockResponse(text="done")])],
        builtin_tools=True,
    )
    write_workflow(
        project,
        "write",
        b"""
from chrys.workflows import WorkflowBuilder, Retry
wf = WorkflowBuilder('write')
node = wf.agent('writer', profile='Headless', retry=Retry(max_attempts=2, backoff=0))
wf.start(node)
wf.output(node)
workflow = wf.build()
""",
    )
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.write"])])
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "write")
        first, _ = await run(host, "write", input_text="write the file")
        assert first.outcome.value == "completed" and file.read_text() == "after"
        second, _ = await run(host, "write", input_text="write it again")
        assert second.outcome.value == "completed" and file.read_text() == "second run"
        state = (await store.load_workflow_session(host.workflow_session_id)).encode()
        assert state is not None
        mutations = state["chrys_mutations"]
        assert [
            (period["period_index"], period["run_id"], len(period["mutations"])) for period in mutations["runs"]
        ] == [
            (1, first.run_id, 2),
            (2, second.run_id, 1),
        ]
        directory = host.workflow_session_dir
        assert directory is not None
        restored = MutationTracker.deserialize(mutations, SnapshotStore(directory))
        assert restored.get_file_edit_snapshots() == [
            ("before", "attempt"),
            ("attempt", "after"),
            ("after", "second run"),
        ]
    finally:
        await host.shutdown()


async def test_history_selection_does_not_change_the_executing_event_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        previous, _ = await run(host, "review")
        previous_session = host.workflow_session_id

        async def stale_terminal(event: WorkflowRunStarted) -> None:
            await host.event_bus.publish(WorkflowRunFinished(session_id=event.session_id, run_id=previous.run_id))

        await host.event_bus.subscribe(WorkflowRunStarted, stale_terminal)
        events = []
        async with asyncio.timeout(10):
            async for event in host.iter_workflow_events(host.workflow_target("review", new_session=True)):
                events.append(event)
                if isinstance(event, WorkflowRunAccepted):
                    assert event.session_id != previous_session
                    await host.load_workflow_session(previous_session)
        accepted = next(event for event in events if isinstance(event, WorkflowRunAccepted))
        (terminal,) = [event for event in events if isinstance(event, WorkflowRunFinished)]
        assert (terminal.session_id, terminal.run_id) == (accepted.session_id, accepted.run_id)
        assert host.workflow_session_id == previous_session
        assert host.engine.workflows.result(terminal.run_id) is not None
    finally:
        await host.shutdown()
