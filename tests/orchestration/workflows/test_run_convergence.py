# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Task failures, cancellation races, and phase diagnostics through a real workflow run."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import WorkflowNodeStateChanged, WorkflowRunFinished
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.orchestration.workflows.agent_node import AgentAttemptResult, WorkflowAgentShell
from chrys.orchestration.workflows.runner import WorkflowRunner
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.scheduler import NodeStateChanged
from chrys.service.workflows.store import WorkflowStorageFailed, read_node_diagnostics, read_run_events
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.workflow_workers import python_workflow

pytestmark = pytest.mark.asyncio

SIMPLE = python_workflow("def fn(value):\n    return value\n", "fn")
AGENT = (
    "from chrys.workflows import WorkflowBuilder\n"
    "wf = WorkflowBuilder('agent')\n"
    f"node = wf.agent('agent', profile={PROFILE!r})\n"
    "wf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
).encode()
LOOP = b"""from chrys.workflows import WorkflowBuilder
count = 0
def body(scope):
    node = scope.python('body', lambda value: value)
    return node, node
def until(value):
    global count
    count += 1
    print('until', count)
    return count == 2
wf = WorkflowBuilder('loop')
node = wf.loop('loop', body, until=until, max_iterations=2)
wf.start(node)
wf.output(node)
workflow = wf.build()
"""
OUTGOING = b"""from chrys.workflows import WorkflowBuilder
def body(value):
    print('body output')
    return value
def condition(value):
    print('condition output')
    raise RuntimeError('condition failed')
wf = WorkflowBuilder('conditions')
a = wf.python('a', body)
b = wf.python('b', body)
wf.start(a)
wf.edge(a, b, when=condition)
wf.output(b)
workflow = wf.build()
"""


@pytest.mark.parametrize(
    "owner,method,source",
    [
        (WorkflowRunner, WorkflowRunner._run_python, SIMPLE),
        (WorkflowRunner, WorkflowRunner._on_state, SIMPLE),
        (WorkflowRunner, WorkflowRunner._evaluate_outgoing, OUTGOING),
        (WorkflowRunner, WorkflowRunner._evaluate_until, LOOP),
        (WorkflowAgentShell, WorkflowAgentShell.run, AGENT),
        (WorkflowAgentShell, WorkflowAgentShell.close, AGENT),
    ],
)
async def test_unexpected_task_failure_commits_one_terminal_and_releases_the_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    owner: type,
    method: Callable[..., Awaitable[None]],
    source: bytes,
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])

    async def close_then_fail(self: WorkflowAgentShell) -> None:
        await method(self)
        raise RuntimeError("injected task defect")

    failure = close_then_fail if method is WorkflowAgentShell.close else RuntimeError("injected task defect")
    monkeypatch.setattr(owner, method.__name__, create_autospec(method, side_effect=failure))
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        result, events = await asyncio.wait_for(run(host, "wf"), timeout=10)
        assert result.reason == "internal_error"
        assert "injected task defect" in result.error
        assert len([e for e in events if isinstance(e, WorkflowRunFinished)]) == 1
        assert host.engine.execution() == ExecutionSnapshot("idle")
        assert host.workflow_session_dir is not None
        records = read_run_events(run_dir(host.workflow_session_dir, result.run_id)).events
        assert sum(e.payload.get("reason") == "internal_error" for e in records) == 1
    finally:
        await host.shutdown()


@pytest.mark.parametrize("storage", [False, True], ids=["unexpected-cancel", "required-record-failure"])
async def test_agent_cancel_and_storage_failure_settle_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: bool
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    original = WorkflowAgentShell.run
    replacement = (
        create_autospec(original, side_effect=WorkflowStorageFailed("archive failed"))
        if storage
        else create_autospec(original, return_value=AgentAttemptResult(kind="cancelled"))
    )
    monkeypatch.setattr(WorkflowAgentShell, "run", replacement)
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", AGENT)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        result, _events = await asyncio.wait_for(run(host, "wf"), timeout=10)
        assert result.outcome.value == ("storage_failed" if storage else "node_failed")
        assert result.error == ("archive failed" if storage else "agent pass was cancelled")
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


@pytest.mark.parametrize("first,second", [("", "deadline_exceeded"), ("deadline_exceeded", ""), ("shutdown", "")])
async def test_first_cancel_wins_while_the_scheduler_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, first: str, second: str
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    original = WorkflowRunner._on_state
    tasks: list[asyncio.Task[None]] = []

    async def state(self: WorkflowRunner, decision: NodeStateChanged) -> None:
        await original(self, decision)
        if decision.state.value == "running":
            task = self.cancel(reason=first)
            assert task is not None
            assert self.cancel(reason=second) is task
            tasks.append(task)

    monkeypatch.setattr(WorkflowRunner, "_on_state", create_autospec(original, side_effect=state))
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", SIMPLE)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        result, _events = await run(host, "wf")
        assert result.reason == first
        assert result.outcome.value == "cancelled"
        assert len(tasks) == 1 and tasks[0].done()
    finally:
        await host.shutdown()


@pytest.mark.parametrize(
    "source,node,expected",
    [
        (OUTGOING, "a", [("body", 0, "body output\n"), ("outgoing", 0, "condition output\n")]),
        (LOOP, "loop", [("until", 1, "until 1\n"), ("until", 2, "until 2\n")]),
    ],
)
async def test_diagnostics_retain_body_conditions_and_each_loop_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: bytes, node: str, expected: list[tuple[str, int, str]]
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        result, events = await run(host, "wf")
        state = next(e for e in events if isinstance(e, WorkflowNodeStateChanged) and e.node_id == node)
        assert host.workflow_session_dir is not None
        diagnostic = read_node_diagnostics(run_dir(host.workflow_session_dir, result.run_id), state.activation_id, 1)
        assert diagnostic is not None
        assert [(d["phase"], d["iteration"], d["stdout"]["text"]) for d in diagnostic["phases"]] == expected
        if node == "a":
            assert not diagnostic["phases"][0]["traceback"]
            assert "condition failed" in diagnostic["phases"][1]["traceback"]
            failed = next(e for e in events if isinstance(e, WorkflowNodeStateChanged) and e.state == "failed")
            assert failed.failure_phase == "outgoing"
    finally:
        await host.shutdown()


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
async def test_node_timeout_persists_the_terminal_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, asynchronous: bool
) -> None:
    body = (
        "import asyncio\nasync def fn(value):\n    print('progress before timeout')\n    await asyncio.Event().wait()\n"
        if asynchronous
        else "import threading\ndef fn(value):\n    print('progress before timeout')\n    threading.Event().wait()\n"
    )
    source = python_workflow(body, "fn").replace(b"wf.python('fn', fn)", b"wf.python('fn', fn, timeout=1)")
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        result, events = await run(host, "wf")
        failed = next(e for e in events if isinstance(e, WorkflowNodeStateChanged) and e.state == "failed")
        assert failed.error_class == "python_timeout"
        assert host.workflow_session_dir is not None
        diagnostic = read_node_diagnostics(run_dir(host.workflow_session_dir, result.run_id), failed.activation_id, 1)
        assert diagnostic is not None
        assert diagnostic["phases"][0]["stdout"]["text"] == "progress before timeout\n"
    finally:
        await host.shutdown()


@pytest.mark.parametrize("malformed", [False, True], ids=["worker-loss", "invalid-verdict"])
async def test_worker_loss_and_decode_errors_keep_their_reason_in_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool
) -> None:
    from chrys.orchestration.workflows.worker_client import WorkerLostError, WorkflowWorkerClient

    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    original = WorkflowWorkerClient._evaluate
    replacement = (
        create_autospec(original, return_value={"verdict": "true"})
        if malformed
        else create_autospec(original, side_effect=WorkerLostError("worker exited with status 7: native failure"))
    )
    monkeypatch.setattr(WorkflowWorkerClient, "_evaluate", replacement)
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", LOOP)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        result, events = await run(host, "wf")
        assert result.outcome.value == "worker_lost"
        assert ("until verdict must be a boolean" if malformed else "native failure") in result.error
        terminal = next(e for e in events if isinstance(e, WorkflowRunFinished))
        assert terminal.error == result.error
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_abandoned_startup_keeps_elapsed_duration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import chrys.orchestration.workflows.runner as runner_module

    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "wf", SIMPLE)
    host = make_host(tmp_path, project=project)
    clock = [10.0]
    monkeypatch.setattr(runner_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    async def fail(self: WorkflowRunner, step: Callable[[], Awaitable[None]]) -> None:
        await step()
        clock[0] = 12.5
        raise RuntimeError("startup failure")

    monkeypatch.setattr(
        WorkflowRunner, "_startup_step", create_autospec(WorkflowRunner._startup_step, side_effect=fail)
    )
    try:
        await confirm(host, "wf")
        result, events = await run(host, "wf")
        assert result.reason == "internal_error" and result.duration == 2.5
        terminal = next(event for event in events if isinstance(event, WorkflowRunFinished))
        assert terminal.duration == 2.5
    finally:
        await host.shutdown()
