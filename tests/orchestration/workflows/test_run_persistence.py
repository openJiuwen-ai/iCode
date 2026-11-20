# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run-owned output capture, answer acknowledgment and cancellation-drained artifact writes."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import WorkflowNodeAnswer, WorkflowNodeAskUser, WorkflowRunFinished
from chrys.foundation.models.ask_user import AskUserAnswer
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.llm.mock import MockChatClient
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.store import RunRecord, WorkflowRunStore, read_run_events, read_run_output
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, patch_runtime, run, write_workflow
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow


async def test_load_and_native_output_are_collected_before_worker_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    source = python_workflow(
        "import os\nprint('loaded')\ndef fn(value):\n    os.write(1, b'x' * 100_000 + b'NATIVE-END')\n    return 'done'\n",
        "fn",
    )
    write_workflow(project, "native", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "native")
        result, _ = await run(host, "native")
        assert result.outcome.value == "completed"
        assert host.workflow_session_dir is not None
        output = read_run_output(run_dir(host.workflow_session_dir, result.run_id))
        assert output is not None and output["load"] == {"text": "loaded\n", "truncated": False}
        assert output["native"]["text"].endswith("NATIVE-END")
        assert output["native"]["dropped_bytes"] > 0
        assert "chrys-drain" not in output["native"]["text"]
    finally:
        await host.shutdown()


async def test_inline_duplicate_answers_are_journaled_once_before_node_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(
        project, "ask", python_workflow("async def fn(value, ctx):\n    return await ctx.ask('Continue?')\n", "fn")
    )
    host = make_host(tmp_path, project=project, allow_user_interaction=True)

    async def answer(ask: WorkflowNodeAskUser) -> None:
        for response in ("accepted", "duplicate"):
            await host.event_bus.publish(
                WorkflowNodeAnswer(
                    run_id=ask.run_id,
                    node_id=ask.node_id,
                    activation_id=ask.activation_id,
                    request_id=ask.request_id,
                    answers=(AskUserAnswer(values=(response,)),),
                )
            )

    await host.event_bus.subscribe(WorkflowNodeAskUser, answer)
    try:
        await confirm(host, "ask")
        result, _ = await run(host, "ask")
        assert result.outputs[0].value.text == "accepted"
        assert host.workflow_session_dir is not None
        records = read_run_events(run_dir(host.workflow_session_dir, result.run_id)).events
        answers = [record for record in records if record.event_type == RunRecord.NODE_ANSWER]
        assert len(answers) == 1 and answers[0].payload["answer"] == "accepted"
        output = next(record for record in records if record.event_type == RunRecord.NODE_OUTPUT)
        assert answers[0].sequence < output.sequence
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeAskUser, answer)
        await host.shutdown()


async def test_cancel_drains_thread_write_before_terminal_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "write", python_workflow("def fn(value):\n    return 'done'\n", "fn"))
    host = make_host(tmp_path, project=project)
    entered, release = threading.Event(), threading.Event()
    original = WorkflowRunStore.write_node_value
    main_thread = threading.get_ident()

    def write(self: WorkflowRunStore, activation_id: str, attempt: int, kind: str, payload: Mapping[str, Any]) -> Path:
        assert threading.get_ident() != main_thread
        if kind == "input":
            entered.set()
            assert release.wait(10)
        return original(self, activation_id, attempt, kind, payload)

    monkeypatch.setattr(WorkflowRunStore, "write_node_value", create_autospec(original, side_effect=write))
    caller = None
    terminals = []

    async def terminal(event: WorkflowRunFinished) -> None:
        terminals.append(event)

    await host.event_bus.subscribe(WorkflowRunFinished, terminal)
    try:
        await confirm(host, "write")
        caller = asyncio.create_task(run(host, "write"))
        await wait_for(
            lambda: entered.is_set() or caller.done(),
            description="node record thread entered",
            timeout=ENGINE_TURN_TIMEOUT,
        )
        if caller.done():
            await caller
        assert entered.is_set()
        host.engine.workflows.cancel_active()
        assert not terminals and not caller.done()
        assert host.engine.execution().kind == "workflow"
        release.set()
        result, _ = await caller
        assert result.outcome.value == "cancelled"
        assert len(terminals) == 1
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        release.set()
        if caller is not None:
            await asyncio.gather(caller, return_exceptions=True)
        await host.event_bus.unsubscribe(WorkflowRunFinished, terminal)
        await host.shutdown()


async def test_loop_outgoing_retry_replays_the_attempt_that_wrote_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.foundation.events.types import WorkflowNodeRetryRequest, WorkflowNodeStateChanged
    from chrys.service.workflows.records import decode_run_event
    from chrys.service.workflows.store import read_node_value

    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(
        project,
        "retry",
        b"""from chrys.workflows import WorkflowBuilder
calls = 0
def body(scope):
    node = scope.python('body', lambda value: value.text + '!')
    return node, node
def branch(value):
    global calls
    calls += 1
    if calls == 1:
        raise ValueError('retry outgoing')
    return True
wf = WorkflowBuilder('retry')
loop = wf.loop('loop', body, until=lambda value: True, max_iterations=1)
sink = wf.python('sink', lambda value: value)
wf.edge(loop, sink, when=branch)
wf.start(loop)
wf.output(loop)
workflow = wf.build()
""",
    )
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    retries = []

    async def retry(event: WorkflowNodeStateChanged) -> None:
        if event.state == "awaiting_retry":
            retries.append(event)
            await host.event_bus.publish(
                WorkflowNodeRetryRequest(
                    run_id=event.run_id,
                    node_id=event.node_id,
                    activation_id=event.activation_id,
                    request_id="retry",
                    expected_failed_attempt=event.attempt,
                )
            )

    await host.event_bus.subscribe(WorkflowNodeStateChanged, retry)
    try:
        await confirm(host, "retry")
        result, _ = await run(host, "retry", input_text="input")
        assert result.outcome.value == "completed" and len(retries) == 1
        assert host.workflow_session_dir is not None
        directory = run_dir(host.workflow_session_dir, result.run_id)
        records = read_run_events(directory).events
        terminal = next(record for record in records if record.event_type == RunRecord.RUN_FINISHED)
        restored = decode_run_event(terminal, directory=directory)
        assert isinstance(restored, WorkflowRunFinished)
        (output,) = restored.outputs
        assert output.attempt == 1
        assert read_node_value(directory, output.activation_id, output.attempt, "output")["value"]["text"] == "input!"
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, retry)
        await host.shutdown()
