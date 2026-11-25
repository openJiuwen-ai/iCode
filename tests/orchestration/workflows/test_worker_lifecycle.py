# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker deadlines retain diagnostics, and leaked sync capacity queues before timing."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.worker_client as worker_client
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.orchestration.workflows.worker_client import WorkerRpcError
from chrys.service.llm.mock import MockChatClient
from chrys.service.workflows.protocol import LIMITS
from chrys.service.workflows.scheduler import AttemptRef
from chrys.service.workflows.sdk import WorkflowValue
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.orchestration.workflows.conftest import FAKE_WORKER, Launcher
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow

pytestmark = pytest.mark.asyncio


def ref(node: str, activation: str) -> AttemptRef:
    return AttemptRef("run", node, activation, 1)


async def test_leaked_sync_capacity_queues_before_deadlines_and_leaves_async_admission_available(
    launch: Launcher, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await launch(host_path=FAKE_WORKER)
    await client.run_python(ref("leaky", "leaked"), WorkflowValue(""), blocking=True)
    capacity = LIMITS.worker_thread_pool_size - 1
    assert client._capacity["sync"] == capacity
    queued = asyncio.Event()
    original = client._capacity_changed.wait

    async def waiting() -> None:
        queued.set()
        await original()

    monkeypatch.setattr(client._capacity_changed, "wait", create_autospec(original, side_effect=waiting))
    tasks = [
        asyncio.create_task(client.run_python(ref("hang", f"held-{i}"), WorkflowValue(""), blocking=True))
        for i in range(capacity)
    ]
    try:
        await wait_for(lambda: client._in_flight["sync"] == capacity, description="remaining sync slots occupied")
        extra = asyncio.create_task(
            client.run_python(ref("ok", "queued"), WorkflowValue(""), blocking=True, timeout=0.5)
        )
        tasks.append(extra)
        await wait_for(queued.is_set, description="sync work queued before registration")
        assert not any(p.key and "queued" in p.key for p in client._pending.values())
        result = await client.run_python(ref("ok", "async"), WorkflowValue(""), blocking=False, timeout=1)
        assert result.value.text == "ok"
        assert not extra.done()
        await client.cancel(ref("hang", "held-0"))
        assert (await extra).value.text == "ok"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


async def test_load_deadline_closes_the_worker(
    launch: Launcher, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = await launch()
    monkeypatch.setattr(worker_client, "LIMITS", replace(LIMITS, load_timeout=0.2))
    with pytest.raises(WorkerRpcError, match="load did not finish") as timed_out:
        await client.load(
            b"import threading\nthreading.Event().wait()\n", filename=str(workspace / "wf.py"), workspace=workspace
        )
    assert timed_out.value.data["reason"] == worker_client.LOAD_TIMED_OUT
    assert client._process.returncode is not None
    assert client._closing is not None and client._closing.done()
    assert not client._pending


async def test_admission_load_deadline_rejects_and_releases_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    source = python_workflow(
        "import os, threading\nif os.environ.get('CHRYS_TEST_BLOCK_LOAD'):\n    threading.Event().wait()\ndef fn(value):\n    return value\n",
        "fn",
    )
    write_workflow(project, "wf", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "wf")
        monkeypatch.setenv("CHRYS_TEST_BLOCK_LOAD", "1")
        monkeypatch.setattr(worker_client, "LIMITS", replace(LIMITS, load_timeout=0.2))
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "wf")
        assert rejected.value.event.error == "load_failed"
        assert "load did not finish" in rejected.value.event.message
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


@pytest.mark.parametrize("phase", ["outgoing", "until", "combine"])
async def test_successful_and_timed_out_evaluations_retain_stdout(
    launch: Launcher, workspace: Path, phase: str
) -> None:
    from chrys.orchestration.workflows.worker_client import AttemptTimeout
    from chrys.service.workflows.sdk import SourceValue

    source = b"""import threading
from chrys.workflows import WorkflowBuilder

def evaluate(value):
    print('evaluation output')
    if value.text == 'block':
        threading.Event().wait()
    return True

def combine(sources):
    evaluate(sources[0].value)
    return 'combined'

def body(scope):
    node = scope.python('body', lambda value: value)
    return node, node

wf = WorkflowBuilder('evaluations')
a = wf.python('a', lambda value: value)
loop = wf.loop('loop', body, until=evaluate, max_iterations=1)
z = wf.python('z', lambda value: value)
wf.start(a)
wf.edge(a, loop, when=evaluate)
wf.join([loop], z, combine=combine)
wf.output(z)
workflow = wf.build()
"""
    client = await launch()
    await client.load(source, filename=str(workspace / "wf.py"), workspace=workspace)

    async def evaluate(value: str):
        if phase == "outgoing":
            return await client.eval_outgoing(ref("a", value), WorkflowValue(value), ("a->loop",), timeout=0.5)
        if phase == "until":
            return await client.eval_loop_until(ref("loop", value), 1, WorkflowValue(value), timeout=0.5)
        return await client.combine(
            ref("join:z", value), (SourceValue("loop", "source", WorkflowValue(value)),), timeout=0.5
        )

    result = await evaluate("success")
    assert result.stdout.text == "evaluation output\n"
    with pytest.raises(AttemptTimeout) as timed_out:
        await evaluate("block")
    assert timed_out.value.stdout.text == "evaluation output\n"
    assert timed_out.value.leaked_thread
    assert not client._pending
