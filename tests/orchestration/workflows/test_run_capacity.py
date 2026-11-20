# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Wide graphs queue before body deadlines and agent construction, and cancellation drains those queues."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.worker_client as worker_client_module
from chrys.foundation.events.types import (
    WorkflowCancelRequest,
    WorkflowNodeAnswer,
    WorkflowNodeAskUser,
    WorkflowNodeOutput,
    WorkflowNodeStateChanged,
    WorkflowRunFinished,
)
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.orchestration.workflows.runner import MAX_CONCURRENT_AGENT_ATTEMPTS, WorkflowRunner
from chrys.orchestration.workflows.worker_client import WorkflowWorkerClient, _Pending
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.workflows.protocol import LIMITS
from chrys.service.workflows.scheduler import Activate, AttemptRef
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, wait_for, with_wait_deadline

pytestmark = pytest.mark.asyncio


def _fanout(kind: str, width: int) -> bytes:
    lines = [
        "import threading",
        "from chrys.workflows import WorkflowBuilder",
        "gate = threading.Event()",
        "def seed(text):\n    return text",
        "def sync(value, ctx):\n    ctx.emit('entered')\n    gate.wait()\n    return 'done'",
        "async def ask(value, ctx):\n    await ctx.ask('Release?')\n    gate.set()\n    return 'done'",
        "wf = WorkflowBuilder('wide')",
        "start = wf.python('start', seed)",
        "wf.start(start)",
    ]
    for index in range(width):
        body = f"agent('n{index}', profile={PROFILE!r})" if kind == "agent" else f"python('n{index}', {kind})"
        lines += [f"node = wf.{body}", "wf.edge(start, node)", "wf.output(node)"]
    if kind == "sync":
        lines += ["release = wf.python('release', ask)", "wf.edge(start, release)", "wf.output(release)"]
    lines.append("workflow = wf.build()")
    return ("\n".join(lines) + "\n").encode()


def _observe_dispatch(monkeypatch: pytest.MonkeyPatch, width: int) -> tuple[asyncio.Event, list[WorkflowRunner]]:
    ready = asyncio.Event()
    runners: list[WorkflowRunner] = []
    seen: set[str] = set()
    original = WorkflowRunner._activate

    async def activate(self: WorkflowRunner, decision: Activate) -> None:
        if not runners:
            runners.append(self)
        if decision.ref.node_id.startswith("n"):
            seen.add(decision.ref.node_id)
            if len(seen) == width:
                ready.set()
        await original(self, decision)

    monkeypatch.setattr(WorkflowRunner, "_activate", create_autospec(original, side_effect=activate))
    return ready, runners


@pytest.mark.parametrize("cancel", [False, True], ids=["release", "cancel"])
async def test_ninth_sync_body_waits_before_its_deadline_and_cancels_without_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    width = LIMITS.worker_thread_pool_size
    all_ready, runners = _observe_dispatch(monkeypatch, width + 1)
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "wide", _fanout("sync", width + 1))
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    entered: set[str] = set()
    occupied = asyncio.Event()
    asked = asyncio.Event()
    asks: list[WorkflowNodeAskUser] = []
    timed: set[str] = set()
    original_deadline = WorkflowWorkerClient._deadline

    async def deadline(
        self: WorkflowWorkerClient,
        request: Awaitable[dict[str, Any]],
        pending: _Pending,
        ref: AttemptRef,
        timeout: float | None,
    ) -> dict[str, Any]:
        if ref.node_id.startswith("n"):
            timed.add(ref.node_id)
        return await original_deadline(self, request, pending, ref, timeout)

    monkeypatch.setattr(WorkflowWorkerClient, "_deadline", create_autospec(original_deadline, side_effect=deadline))

    async def output(event: WorkflowNodeOutput) -> None:
        if event.kind == "emit":
            entered.add(event.node_id)
            if len(entered) == width:
                occupied.set()

    async def question(event: WorkflowNodeAskUser) -> None:
        asks.append(event)
        asked.set()

    await host.event_bus.subscribe(WorkflowNodeOutput, output)
    await host.event_bus.subscribe(WorkflowNodeAskUser, question)
    task = None
    try:
        await confirm(host, "wide")
        task = asyncio.create_task(run(host, "wide"))
        await asyncio.wait_for(asyncio.gather(all_ready.wait(), occupied.wait(), asked.wait()), 10)
        assert len(entered) == len(timed) == width
        runner = runners[0]
        if cancel:
            await host.event_bus.publish(
                WorkflowCancelRequest(run_id=runner.run_id, session_id=host.workflow_session_id)
            )
        else:
            ask = asks[0]
            await host.event_bus.publish(
                WorkflowNodeAnswer(
                    run_id=ask.run_id,
                    node_id=ask.node_id,
                    activation_id=ask.activation_id,
                    request_id=ask.request_id,
                    answers=(AskUserAnswer(values=("go",)),),
                    session_id=host.workflow_session_id,
                )
            )
        result, _events = await asyncio.wait_for(task, 15)
        assert result.outcome.value == ("cancelled" if cancel else "completed")
        assert len(entered) == len(timed) == (width if cancel else width + 1)
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "width,total_limit",
    [(9, 8), (6, 2)],
    ids=["wide-normal-lane", "lower-total-limit"],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_wide_async_fanout_queues_at_the_effective_request_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, width: int, total_limit: int
) -> None:
    # Saturate both limit orderings with a small real graph. Hundreds of
    # per-node durable writes turn this queue test into a Windows disk benchmark.
    body_limit = 4
    monkeypatch.setattr(worker_client_module, "MAX_IN_FLIGHT_REQUESTS", body_limit)
    monkeypatch.setattr(worker_client_module, "LIMITS", replace(LIMITS, max_pending_requests=total_limit))
    capacity = min(body_limit, total_limit)
    assert width > max(body_limit, total_limit)
    all_ready, runners = _observe_dispatch(monkeypatch, width)
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "wide", _fanout("ask", width))
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    occupied = asyncio.Event()
    asks: list[WorkflowNodeAskUser] = []
    released = False

    async def answer(event: WorkflowNodeAskUser) -> None:
        await host.event_bus.publish(
            WorkflowNodeAnswer(
                run_id=event.run_id,
                node_id=event.node_id,
                activation_id=event.activation_id,
                request_id=event.request_id,
                answers=(AskUserAnswer(values=("go",)),),
                session_id=host.workflow_session_id,
            )
        )

    async def question(event: WorkflowNodeAskUser) -> None:
        asks.append(event)
        if len(asks) == capacity:
            occupied.set()
        if released:
            await answer(event)

    await host.event_bus.subscribe(WorkflowNodeAskUser, question)
    task = None
    try:
        await confirm(host, "wide")
        task = asyncio.create_task(run(host, "wide"))
        # Each phase uses what remains of the same test-wide deadline, so slow
        # durable writes do not impose an extra, shorter readiness deadline.
        await wait_for(
            lambda: task.done() or all_ready.is_set(),
            timeout=ENGINE_TEST_WAIT_TIMEOUT,
            description=f"all {width} fanout nodes dispatched",
        )
        if task.done():
            await task  # Surface an early failure instead of timing out on readiness.
        assert all_ready.is_set()
        await wait_for(
            lambda: task.done() or occupied.is_set(),
            timeout=ENGINE_TEST_WAIT_TIMEOUT,
            description=f"{capacity} request slots occupied",
        )
        if task.done():
            await task
        assert occupied.is_set()
        worker = runners[0]._worker
        assert len(asks) == capacity
        assert worker._in_flight["body"] == capacity
        assert len(worker._pending) == capacity
        released = True
        for event in tuple(asks):
            await answer(event)
        await wait_for(
            task.done, timeout=ENGINE_TEST_WAIT_TIMEOUT, description="queued fanout nodes completed after release"
        )
        result, events = await task
        assert result.outcome.value == "completed"
        assert len(asks) == len(result.outputs) == width
        assert not [
            event for event in events if isinstance(event, WorkflowNodeStateChanged) and event.state == "failed"
        ]
        assert [event.outcome for event in events if isinstance(event, WorkflowRunFinished)] == ["completed"]
        assert host.engine.execution() == ExecutionSnapshot("idle")
        assert worker.lost is not None  # normal teardown closes the worker
        assert worker._process.returncode is not None
        assert worker._in_flight == {"body": 0, "eval": 0, "sync": 0}
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("cancel", [False, True], ids=["release", "cancel"])
async def test_agent_gate_covers_construction_and_cancellation_does_not_open_a_queued_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    width = MAX_CONCURRENT_AGENT_ATTEMPTS + 1
    all_ready, runners = _observe_dispatch(monkeypatch, width)
    clients = [MockChatClient(responses=[])] + [
        MockChatClient(responses=[MockResponse(text="done")]) for _ in range(width)
    ]
    patch_runtime(monkeypatch, clients)
    project = make_workdir(tmp_path)
    write_workflow(project, "wide", _fanout("agent", width))
    host = make_host(tmp_path, project=project)
    occupied = asyncio.Event()
    release = asyncio.Event()
    opened: list[WorkflowAgentShell] = []
    original = WorkflowAgentShell.open

    async def open_shell(self: WorkflowAgentShell, prompt: str, *, attempt: int = 1) -> None:
        opened.append(self)
        if len(opened) == MAX_CONCURRENT_AGENT_ATTEMPTS:
            occupied.set()
        await release.wait()
        await original(self, prompt, attempt=attempt)

    monkeypatch.setattr(WorkflowAgentShell, "open", create_autospec(original, side_effect=open_shell))
    task = None
    try:
        await confirm(host, "wide")
        task = asyncio.create_task(run(host, "wide"))
        await asyncio.wait_for(asyncio.gather(all_ready.wait(), occupied.wait()), 10)
        assert len(opened) == MAX_CONCURRENT_AGENT_ATTEMPTS
        assert len(clients) == width  # queued construction has not created a provider client
        if cancel:
            await host.event_bus.publish(
                WorkflowCancelRequest(run_id=runners[0].run_id, session_id=host.workflow_session_id)
            )
        else:
            release.set()
        result, _events = await asyncio.wait_for(task, 15)
        assert result.outcome.value == ("cancelled" if cancel else "completed")
        assert len(opened) == (MAX_CONCURRENT_AGENT_ATTEMPTS if cancel else width)
        assert len(clients) == (width if cancel else 0)
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
