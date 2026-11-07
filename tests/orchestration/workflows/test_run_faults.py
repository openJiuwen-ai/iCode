# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow run refusals and faults through the host: admission, the freeze, deadlines, lost workers, storage, shutdown."""

from __future__ import annotations

import asyncio
import contextlib
import errno
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import psutil
import pytest

import chrys.orchestration.workflows.coordinator as coordinator_module
import chrys.orchestration.workflows.preview as preview_module
import chrys.service.workflows.environment as environment_module
import chrys.service.workflows.store as store_module
from chrys.foundation.errors.provider import ProviderResponseError
from chrys.foundation.events.types import (
    Error,
    Event,
    InvocationProgress,
    InvocationStarted,
    InvocationToolCallStart,
    SessionClear,
    SessionDelete,
    SessionFork,
    SessionNew,
    SessionRestore,
    SessionRestored,
    SettingsReload,
    UsageUpdate,
    UserMessage,
    UserRetry,
    UserRollback,
    WorkflowNodeAskUser,
    WorkflowNodeOutput,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
    WorkflowRunFinished,
    WorkflowRunRejected,
    WorkflowRunRequest,
    WorkflowRunStarted,
)
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.util.session_ids import session_short_id
from chrys.kernel import Content, UsageDetails
from chrys.orchestration.engine.execution import PreAdmissionPreparationTracker
from chrys.orchestration.engine.run.coordinator import TurnCoordinator
from chrys.orchestration.session_host import HeadlessRunError, WorkflowRunRejectedError, WorkflowRunTimeoutError
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.orchestration.workflows.preview import PreparedWorkflow
from chrys.orchestration.workflows.worker_client import WorkflowWorkerClient
from chrys.service.agent_middleware.events.sub_agent_events import SubAgentEventMiddleware
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.session.persistence import SessionPersistence
from chrys.service.workflows.interpreter import InterpreterError
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.orphans import read_run_terminal
from chrys.service.workflows.protocol import LIMITS
from chrys.service.workflows.store import (
    NODE_RECORD_OUTPUT,
    WorkflowRunStore,
    WorkflowStorageFailed,
    read_node_value,
)
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    hold_workflow_deadline,
    make_host,
    make_profile,
    make_project,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.event_capture import assert_display_message, capture_event_sequence
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline
from tests.support.workflow_workers import python_workflow

pytestmark = pytest.mark.asyncio

CHAIN = python_workflow("def fn(text):\n    return text.text + '!'\n", "fn")
SLEEPER = python_workflow("import asyncio\nasync def fn(value, ctx):\n    await asyncio.sleep(3600)\n", "fn")
STUCK = python_workflow("import time\ndef fn(text):\n    time.sleep(3600)\n", "fn")
SLEEPER_SECOND = python_workflow(
    "import asyncio\ndef fn(text):\n    return text.text + '!'\nasync def sleeper(value, ctx):\n    await asyncio.sleep(3600)\n",
    "fn",
    "sleeper",
)
EMITTER_SECOND = python_workflow(
    "import asyncio\ndef fn(value, ctx):\n    ctx.emit('hello')\n    return value.text.text + '!'\n"
    "async def sleeper(value, ctx):\n    await asyncio.sleep(3600)\n",
    "fn",
    "sleeper",
)
ASKER = python_workflow("async def fn(value, ctx):\n    return await ctx.ask('Continue?')\n", "fn")
STUCK_SHORT = STUCK.replace(b"wf.python('fn', fn)", b"wf.python('fn', fn, timeout=0.3)")
RAISES = python_workflow("def fn(text):\n    raise ValueError('boom')\n", "fn")
EXITS = python_workflow("import os\ndef fn(text):\n    os._exit(3)\n", "fn")
LOOP_ONCE = (
    b"from chrys.workflows import WorkflowBuilder\n"
    b"\n"
    b"def body(scope):\n"
    b"    node = scope.python('fn', lambda text: text.text + '!')\n"
    b"    return node, node\n"
    b"\n"
    b"wf = WorkflowBuilder('loop')\n"
    b"_loop = wf.loop('loop', body, until=lambda value: True, max_iterations=3)\n"
    b"wf.start(_loop)\n"
    b"wf.output(_loop)\n"
    b"workflow = wf.build()\n"
)


def _agent_workflow(agent: str, model: str | None = None) -> bytes:
    model_arg = f", model={model!r}" if model else ""
    return (
        "from chrys.workflows import WorkflowBuilder\n"
        "wf = WorkflowBuilder('agents')\n"
        f"_review = wf.agent('review', profile={agent!r}{model_arg})\n"
        "wf.start(_review)\nwf.output(_review)\nworkflow = wf.build()\n"
    ).encode()


def _track_workers(monkeypatch: pytest.MonkeyPatch) -> list[psutil.Process]:
    """Every worker the host launches, as a process handle that keeps its identity across pid reuse."""
    processes: list[psutil.Process] = []
    real_launch = WorkflowWorkerClient.launch

    async def _launch(**kwargs: Any) -> WorkflowWorkerClient:
        client = await real_launch(**kwargs)
        processes.append(psutil.Process(client._process.pid))
        return client

    monkeypatch.setattr(preview_module.WorkflowWorkerClient, "launch", _launch)
    return processes


async def _workers_gone(processes: list[psutil.Process]) -> None:
    await wait_for(lambda: not any(p.is_running() for p in processes), description="every worker exited")


@pytest.mark.parametrize(
    ("source", "code", "needs_confirmation"),
    [
        (_agent_workflow("Nobody"), "agent_profile_missing", True),
        (_agent_workflow(PROFILE, "nope"), "model_unresolvable", True),
        (
            b"import os\nif os.environ.get('CHRYS_TEST_BREAK_LOAD'):\n    raise RuntimeError('broken file')\n" + CHAIN,
            "load_failed",
            True,
        ),
        (b"# /// script\n# dependencies = ['requests']\n# ///\n" + CHAIN, "not_confirmed", False),
    ],
)
async def test_a_rejected_request_leaves_the_lease_idle_and_no_worker_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: bytes, code: str, needs_confirmation: bool
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "wf", source)
    host = make_host(tmp_path, project=project)
    try:
        if needs_confirmation:
            await confirm(host, "wf")
        # A file that loaded at its confirmation and fails at the run's load: an unconfirmed one is never loaded.
        monkeypatch.setenv("CHRYS_TEST_BREAK_LOAD", "1")
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "wf", input_text="x")
        assert rejected.value.event.error == code
        assert rejected.value.event.message
        assert host.engine.execution() == ExecutionSnapshot("idle")
        assert host.engine.workflows.active_run_id is None
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_duplicate_request_ids_replay_the_reply_and_start_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "chain")
        bus = host.event_bus
        async with capture_event_sequence(bus, WorkflowRunAccepted, WorkflowRunRejected, WorkflowRunStarted) as events:
            await bus.publish(
                WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="r1")
            )
            await wait_for(
                lambda: of_type(events, WorkflowRunAccepted), description="r1 accepted", timeout=ENGINE_TURN_TIMEOUT
            )
            await host.engine.workflows.wait_idle()
            first = of_type(events, WorkflowRunAccepted)[0]
            assert first.request_id == "r1"

            await bus.publish(
                WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="r1")
            )
            await host.engine.workflows.wait_idle()
            replies = of_type(events, WorkflowRunAccepted)
            assert len(replies) == 2
            assert replies[1] is first
            assert len(of_type(events, WorkflowRunStarted)) == 1

            await bus.publish(
                WorkflowRunRequest(target=host.workflow_target("ghost", new_session=True), request_id="r2")
            )
            await wait_for(lambda: of_type(events, WorkflowRunRejected), description="r2 rejected")
            await host.engine.workflows.wait_idle()
            await bus.publish(
                WorkflowRunRequest(target=host.workflow_target("ghost", new_session=True), request_id="r2")
            )
            rejected = of_type(events, WorkflowRunRejected)
            assert [(r.request_id, r.error) for r in rejected] == [("r2", "workflow_not_found")] * 2
            assert rejected[1] is rejected[0]

            await bus.publish(WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id=""))
            await bus.publish(WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id=""))
            blank = [r for r in of_type(events, WorkflowRunRejected) if r.request_id == ""]
            assert [r.error for r in blank] == ["invalid_request"] * 2
            assert blank[0] is not blank[1]
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_the_run_deadline_cancels_the_run_and_the_worker_dies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    expire = hold_workflow_deadline(monkeypatch, 3600)
    project = make_project(tmp_path)
    write_workflow(project, "stuck", STUCK)
    host = make_host(tmp_path, project=project)

    async def on_running(event: WorkflowNodeStateChanged) -> None:
        if event.state == "running":
            expire.set()

    await host.event_bus.subscribe(WorkflowNodeStateChanged, on_running)
    try:
        await confirm(host, "stuck")
        result, events = await run(host, "stuck", input_text="x", timeout=3600)
        assert (result.outcome.value, result.reason, result.node_id) == ("cancelled", "deadline_exceeded", "")
        assert result.duration < 60
        assert [e.state for e in of_type(events, WorkflowNodeStateChanged)] == ["running", "cancelled"]
        finished = of_type(events, WorkflowRunFinished)[0]
        assert (finished.outcome, finished.reason) == ("cancelled", "deadline_exceeded")
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        terminal = read_run_terminal(run_dir(session_dir, result.run_id))
        assert (terminal.outcome, terminal.reason) == ("cancelled", "deadline_exceeded")
        await _workers_gone(workers)
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, on_running)
        await host.shutdown()


async def test_a_node_timeout_on_a_stuck_body_fails_the_node_and_kills_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "stuck", STUCK_SHORT)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "stuck")
        result, events = await run(host, "stuck", input_text="x")
        assert (result.outcome.value, result.node_id) == ("node_failed", "fn")
        failed = [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "failed"]
        assert [(e.node_id, e.error_class) for e in failed] == [("fn", "python_timeout")]
        assert "0.3s" in failed[0].error
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_a_python_exception_fails_the_run_with_its_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "raises", RAISES)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "raises")
        result, events = await run(host, "raises", input_text="x")
        assert (result.outcome.value, result.node_id, result.error) == ("node_failed", "fn", "ValueError: boom")
        states = [(e.state, e.error_class) for e in of_type(events, WorkflowNodeStateChanged)]
        assert states == [("running", ""), ("failed", "python_exception")]
        finished = of_type(events, WorkflowRunFinished)[0]
        assert (finished.outcome, finished.node_id, finished.error) == ("node_failed", "fn", "ValueError: boom")
    finally:
        await host.shutdown()


async def test_a_lost_worker_ends_the_run_as_worker_lost(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "exits", EXITS)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "exits")
        result, events = await run(host, "exits", input_text="x")
        assert result.outcome.value == "worker_lost"
        assert of_type(events, WorkflowRunFinished)[0].outcome == "worker_lost"
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_a_worker_lost_while_a_node_awaits_retry_ends_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing is outstanding on the worker while a node awaits a manual retry: the loss must still be noticed."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "raises", RAISES)
    host = make_host(tmp_path, project=project, allow_user_interaction=True)
    awaiting = asyncio.Event()

    async def _on_state(event: WorkflowNodeStateChanged) -> None:
        if event.state == "awaiting_retry":
            awaiting.set()

    await host.event_bus.subscribe(WorkflowNodeStateChanged, _on_state)
    try:
        await confirm(host, "raises")
        caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("raises"), input_text="x"))
        await awaiting.wait()
        workers[-1].kill()  # the run's worker; the preview's, tracked first, is long gone
        result = await caller
        assert (result.outcome.value, result.reason) == ("worker_lost", "")
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_a_worker_lost_under_an_agent_only_run_ends_it_as_worker_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent pass needs no worker call, yet the run is still bound to its worker: a dead one ends the run."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "agent", _agent_workflow(PROFILE))
    host = make_host(tmp_path, project=project)
    release = asyncio.Event()

    async def _kill_the_worker(event: InvocationStarted) -> None:
        if event.origin.kind == "workflow_node":
            workers[-1].kill()  # the run's worker; the preview's, tracked first, is long gone
            await release.wait()  # the pass stays open until the run's terminal aborts it

    await host.event_bus.subscribe(InvocationStarted, _kill_the_worker)
    try:
        await confirm(host, "agent")
        result = await host.run_workflow_until_final(host.workflow_target("agent"), input_text="x")
        assert (result.outcome.value, result.reason) == ("worker_lost", "")
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        release.set()
        await host.shutdown()


async def test_a_storage_failure_after_acceptance_ends_the_run_as_storage_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    real_append = WorkflowRunStore.append
    appended: list[str] = []

    async def _append(self: WorkflowRunStore, event_type: str, *args: Any, **kwargs: Any) -> int:
        appended.append(event_type)
        if len(appended) > 2:  # the run started and the first node ran; the disk goes away under the next record
            raise WorkflowStorageFailed("disk full")
        return await real_append(self, event_type, *args, **kwargs)

    try:
        await confirm(host, "chain")
        monkeypatch.setattr(WorkflowRunStore, "append", _append)
        result, events = await run(host, "chain", input_text="x")
        assert result.outcome.value == "storage_failed"
        finished = of_type(events, WorkflowRunFinished)[0]
        assert (finished.outcome, finished.degraded, finished.seq, finished.last_written_seq) == (
            "storage_failed",
            True,
            None,
            2,
        )
        assert appended[:3] == ["workflow.log.run.started", "workflow.log.node.state", "workflow.log.node.output"]
        await _workers_gone(workers)
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_a_storage_failure_at_open_rejects_the_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)

    def _open(*_args: Any, **_kwargs: Any) -> WorkflowRunStore:
        raise WorkflowStorageFailed("read-only session directory")

    try:
        await confirm(host, "chain")
        monkeypatch.setattr(WorkflowRunStore, "open", _open)
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "chain", input_text="x")
        assert (rejected.value.event.error, rejected.value.event.message) == (
            "storage_failed",
            "read-only session directory",
        )
        await _workers_gone(workers)
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def _start_on_the_bus(host: Any, workflow_id: str, request_id: str, events: list[Event]) -> str:
    await host.event_bus.publish(
        WorkflowRunRequest(target=host.workflow_target(workflow_id, new_session=True), request_id=request_id)
    )
    await wait_for(
        lambda: of_type(events, WorkflowRunStarted), description="the run started", timeout=ENGINE_TURN_TIMEOUT
    )
    started = of_type(events, WorkflowRunStarted)[0]
    await host.load_workflow_session(started.session_id)
    return started.run_id


async def test_the_session_is_frozen_while_a_run_is_active(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "sleeper")
        await confirm(host, "chain")
        bus = host.event_bus
        session_id = host.session_id
        async with capture_event_sequence(
            bus, Error, WorkflowRunRejected, WorkflowRunStarted, WorkflowRunFinished, WorkflowNodeStateChanged
        ) as events:
            run_id = await _start_on_the_bus(host, "sleeper", "r1", events)
            assert host.engine.execution() == ExecutionSnapshot(
                "workflow", run_id=run_id, cancellable=True, request_id="r1"
            )
            assert host.engine.is_turn_lifecycle_active
            assert host.engine.workflows.active_run_id == run_id

            await bus.publish(UserMessage(text="hello", session_id=session_id))
            await bus.publish(UserRetry(session_id=session_id))
            await bus.publish(UserRollback(target_turn=0, session_id=session_id))
            await bus.publish(SessionNew(session_id=session_id))
            await bus.publish(SessionFork(session_id=session_id))
            await bus.publish(SessionClear(session_id=session_id))
            await bus.publish(SessionDelete(session_id=session_id))
            await bus.publish(SessionDelete(session_id=session_short_id(session_id)))  # as the TUI names it
            await bus.publish(SettingsReload(session_id=session_id))
            await bus.publish(
                WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="r2")
            )

            assert_display_message(of_type(events, Error)[2], "engine.rollback.workflow_active")
            errors = [(e.code, e.message) for e in of_type(events, Error)]
            assert errors == [
                ("workflow_active", "A workflow run is active. Cancel the workflow run first."),
                ("workflow_active", "A workflow run is active. Cancel the workflow run first."),
                ("workflow_active", "Cannot roll back while a workflow run is active. Cancel the workflow run first."),
                (
                    "workflow_active",
                    "Cannot start a new session while a workflow run is active. Cancel the workflow run first.",
                ),
                (
                    "workflow_active",
                    "Cannot fork the session while a workflow run is active. Cancel the workflow run first.",
                ),
                (
                    "workflow_active",
                    "Cannot clear the session while a workflow run is active. Cancel the workflow run first.",
                ),
                (
                    "workflow_active",
                    "Cannot delete the active session while a workflow run is active. Cancel the workflow run first.",
                ),
                (
                    "workflow_active",
                    "Cannot delete the active session while a workflow run is active. Cancel the workflow run first.",
                ),
                (
                    "runtime_mutation_busy",
                    "Cannot rebuild while a workflow run is active. Cancel the workflow run first.",
                ),
            ]
            assert [(r.request_id, r.error) for r in of_type(events, WorkflowRunRejected)] == [
                ("r2", "workflow_active")
            ]
            assert all(e.session_id == session_id for e in of_type(events, Error))
            assert host.session_id == session_id
            assert not of_type(events, WorkflowRunFinished)

            await wait_for(
                lambda: [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "running"],
                description="the sleeper node is running",
            )
            await host.cancel_workflow()
            await wait_for(lambda: of_type(events, WorkflowRunFinished), description="the run was cancelled")
            await host.engine.workflows.wait_idle()
            finished = of_type(events, WorkflowRunFinished)[0]
            assert (finished.run_id, finished.outcome, finished.reason) == (run_id, "cancelled", "")
            assert [e.state for e in of_type(events, WorkflowNodeStateChanged)] == ["running", "cancelled"]
        assert host.engine.execution() == ExecutionSnapshot("idle")
        assert not host.engine.is_turn_lifecycle_active

        # The freeze lifted with the run: the next request is admitted and runs to completion.
        result, _events = await run(host, "chain", input_text="again", new_session=True)
        assert [(o.node_id, o.value.text) for o in result.outputs] == [("fn", "again!")]
    finally:
        await host.shutdown()


async def test_a_live_turn_rejects_a_workflow_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "chain")
        lease = host.engine.turns.turn_state.lease
        lease.run_task = asyncio.create_task(asyncio.sleep(3600))
        try:
            assert host.engine.execution() == ExecutionSnapshot("turn", cancellable=True)
            with pytest.raises(WorkflowRunRejectedError) as rejected:
                await run(host, "chain", input_text="x")
            assert rejected.value.event.error == "turn_active"
        finally:
            lease.run_task.cancel()
            await asyncio.gather(lease.run_task, return_exceptions=True)
            lease.release_run_task()
        result, _events = await run(host, "chain", input_text="x")
        assert result.outcome.value == "completed"
    finally:
        await host.shutdown()


async def test_digest_mismatches_and_an_edited_file_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        preview = await confirm(host, "chain")
        for changed, code in [
            (replace(preview, spec_digest="nope"), "spec_changed"),
            (
                replace(preview, environment=replace(preview.environment, environment_fingerprint="nope")),
                "environment_changed",
            ),
        ]:
            with pytest.raises(WorkflowRunRejectedError) as rejected:
                await host.run_workflow_until_final(
                    PreparedWorkflow(host.workflow_target("chain"), changed), input_text="x"
                )
            assert rejected.value.event.error == code
        result = await host.run_workflow_until_final(
            PreparedWorkflow(host.workflow_target("chain"), preview), input_text="x"
        )
        assert result.outcome.value == "completed"

        # An edited file is refused before it is loaded, so its spec digest is never computed: pinned or
        # not, the refusal is the confirmation's.
        write_workflow(project, "chain", CHAIN.replace(b"'!'", b"'?'"))
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "chain", input_text="x")
        assert rejected.value.event.error == "not_confirmed"
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await host.run_workflow_until_final(
                PreparedWorkflow(host.workflow_target("chain"), preview), input_text="x"
            )
        assert rejected.value.event.error == "not_confirmed"
    finally:
        await host.shutdown()


async def test_shutdown_during_a_run_cancels_it_as_a_shutdown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    await host.start()
    await confirm(host, "sleeper")
    async with capture_event_sequence(host.event_bus, WorkflowRunStarted, WorkflowRunFinished) as events:
        run_id = await _start_on_the_bus(host, "sleeper", "r1", events)
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        await host.shutdown()
        finished = of_type(events, WorkflowRunFinished)
        assert [(f.run_id, f.outcome, f.reason) for f in finished] == [(run_id, "cancelled", "shutdown")]
    terminal = read_run_terminal(run_dir(session_dir, run_id))
    assert (terminal.outcome, terminal.reason) == ("cancelled", "shutdown")
    await _workers_gone(workers)
    assert host.engine.workflows.active_run_id is None


async def test_abandoning_the_event_iterator_cancels_the_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "sleeper")
        async with (
            capture_event_sequence(host.event_bus, WorkflowRunFinished) as events,
            contextlib.aclosing(host.iter_workflow_events(host.workflow_target("sleeper"), input_text="x")) as stream,
        ):
            async for event in stream:
                if isinstance(event, WorkflowRunStarted):
                    break
        # aclosing() ran the iterator's cleanup: the run is cancelled and awaited before the block exits.
        assert [(f.outcome, f.reason) for f in of_type(events, WorkflowRunFinished)] == [("cancelled", "shutdown")]
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


def _held_agent(clients: int) -> list[MockChatClient]:
    """The session's client plus *clients* agent-node clients whose model call never returns on its own."""
    return [MockChatClient(responses=[])] + [
        MockChatClient(responses=[MockResponse(text="never", delay=3600)]) for _ in range(clients)
    ]


async def test_cancelling_a_run_aborts_the_live_agent_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, _held_agent(1))
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "agent", _agent_workflow(PROFILE))
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "agent")
        async with capture_event_sequence(
            host.event_bus, WorkflowRunStarted, WorkflowRunFinished, WorkflowNodeStateChanged, InvocationStarted
        ) as events:
            run_id = await _start_on_the_bus(host, "agent", "r1", events)
            await wait_for(
                lambda: [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "running"],
                description="the agent node is running",
            )
            running = of_type(events, WorkflowNodeStateChanged)[0]
            assert running.invocation_id
            await wait_for(
                lambda: [
                    e for e in of_type(events, InvocationStarted) if e.origin.invocation_id == running.invocation_id
                ],
                description="the node's invocation started under the pinned invocation id",
            )
            await host.cancel_workflow()
            await wait_for(lambda: of_type(events, WorkflowRunFinished), description="the run was cancelled")
            await host.engine.workflows.wait_idle()
            finished = of_type(events, WorkflowRunFinished)[0]
            assert (finished.run_id, finished.outcome, finished.reason) == (run_id, "cancelled", "")
            states = [(e.state, e.invocation_id) for e in of_type(events, WorkflowNodeStateChanged)]
            assert states == [("running", running.invocation_id), ("cancelled", running.invocation_id)]
            started = [e for e in of_type(events, InvocationStarted) if e.origin.kind == "workflow_node"]
            assert [(e.origin.invocation_id, e.origin.session_id) for e in started] == [
                (running.invocation_id, running.session_id)
            ]
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_the_run_deadline_aborts_a_live_agent_node(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch_runtime(monkeypatch, _held_agent(1))
    expire = hold_workflow_deadline(monkeypatch, 7200)
    project = make_project(tmp_path)
    write_workflow(project, "agent", _agent_workflow(PROFILE))
    host = make_host(tmp_path, project=project)

    async def on_started(event: InvocationStarted) -> None:
        if event.origin.kind == "workflow_node":
            expire.set()

    await host.event_bus.subscribe(InvocationStarted, on_started)
    try:
        await confirm(host, "agent")
        result, events = await run(host, "agent", input_text="x", timeout=7200)
        assert (result.outcome.value, result.reason) == ("cancelled", "deadline_exceeded")
        assert [e.state for e in of_type(events, WorkflowNodeStateChanged)] == ["running", "cancelled"]
    finally:
        await host.event_bus.unsubscribe(InvocationStarted, on_started)
        await host.shutdown()


# -- admission races: shutdown, cancel and duplicates that land while a request is being admitted ---


def _hold_admission(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Park every admission at the SDK step until *release* is set; *entered* marks the first arrival."""
    entered = asyncio.Event()
    release = asyncio.Event()
    real_materialize = coordinator_module.materialize_runtime_sdk

    async def _materialize(config_dir: Path) -> Any:
        entered.set()
        await release.wait()
        return await real_materialize(config_dir)

    monkeypatch.setattr(coordinator_module, "materialize_runtime_sdk", _materialize)
    return entered, release


async def test_a_shutdown_behind_a_queued_request_answers_it_and_frees_the_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    entered, _release = _hold_admission(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    await host.start()
    await confirm(host, "chain")
    async with capture_event_sequence(host.event_bus, WorkflowRunRejected, WorkflowRunAccepted) as events:
        await host.event_bus.publish(
            WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="queued")
        )
        await wait_for(
            entered.is_set,
            timeout=ENGINE_TEST_WAIT_TIMEOUT,
            description="admission reached the held boundary",
        )
        await host.shutdown()
    assert [(r.request_id, r.error) for r in of_type(events, WorkflowRunRejected)] == [("queued", "shutting_down")]
    assert not of_type(events, WorkflowRunAccepted)
    assert host.engine.execution() == ExecutionSnapshot("idle")
    assert host.engine.workflows.active_run_id is None


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_cancel_during_admission_rejects_without_waiting_for_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    entered, release = _hold_admission(monkeypatch)
    project = make_project(tmp_path)
    marker = tmp_path / "node-ran"
    write_workflow(
        project,
        "marks",
        python_workflow(
            f"from pathlib import Path\ndef fn(text):\n    Path({str(marker)!r}).write_text('ran')\n    return text\n",
            "fn",
        ),
    )
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "marks")
        async with capture_event_sequence(
            host.event_bus, WorkflowRunRejected, WorkflowRunAccepted, WorkflowRunFinished, WorkflowNodeStateChanged
        ) as events:
            await host.event_bus.publish(
                WorkflowRunRequest(target=host.workflow_target("marks", new_session=True), request_id="r1")
            )
            await wait_for(
                entered.is_set,
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="admission reached the held boundary",
            )
            run_id = host.engine.workflows.active_run_id
            assert run_id is not None
            await host.cancel_workflow()
            await host.cancel_workflow()  # repeated cancellation must not interrupt cleanup or the reply
            await wait_for(
                lambda: host.engine.workflows.active_run_id is None,
                description="cancelled admission released its lease",
            )
            await host.engine.workflows.wait_idle()
        assert not release.is_set()  # admission remains blocked; cancellation itself must release the caller
        assert host.engine.workflows.result(run_id) is None
        assert [(r.request_id, r.error) for r in of_type(events, WorkflowRunRejected)] == [("r1", "cancelled")]
        assert not of_type(events, WorkflowRunAccepted)
        assert not of_type(events, WorkflowRunFinished)
        assert not of_type(events, WorkflowNodeStateChanged)
        assert not marker.exists()
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
        release.set()
        result, _events = await run(host, "marks", input_text="next")
        assert result.outcome.value == "completed"
        assert marker.exists()
    finally:
        await host.shutdown()


async def test_a_shutdown_mid_admission_rejects_the_hosted_caller_as_shutting_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    entered, _release = _hold_admission(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    await host.start()
    await confirm(host, "chain")
    caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("chain"), input_text="x"))
    await wait_for(
        entered.is_set,
        timeout=ENGINE_TEST_WAIT_TIMEOUT,
        description="admission reached the held boundary",
    )
    await host.shutdown()
    with pytest.raises(WorkflowRunRejectedError) as rejected:
        await caller
    assert rejected.value.event.error == "shutting_down"
    assert host.engine.execution() == ExecutionSnapshot("idle")


@pytest.mark.parametrize("shutdown_in_reply", [False, True])
@pytest.mark.parametrize("deadline", [False, True], ids=["user_cancel", "deadline"])
async def test_cancelling_a_stalled_load_drains_the_worker_and_replies_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, shutdown_in_reply: bool, deadline: bool
) -> None:
    """A real module load blocks after confirmation; repeated cancels cannot cut its cleanup or reply short."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    marker = tmp_path / "loading"
    source = (
        "import os, threading\n"
        "from pathlib import Path\n"
        "if os.environ.get('CHRYS_TEST_BLOCK_LOAD'):\n"
        f"    Path({str(marker)!r}).write_text('loading')\n"
        "    threading.Event().wait()\n"
    ).encode() + CHAIN
    write_workflow(project, "blocked", source)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    closing = asyncio.Event()
    release_cleanup = asyncio.Event()
    closed = asyncio.Event()
    reply_returned = asyncio.Event()
    expire = hold_workflow_deadline(monkeypatch, 3600)
    real_close = WorkflowWorkerClient.close

    async def _close(self: WorkflowWorkerClient, *, grace: float = LIMITS.shutdown_grace) -> None:
        closing.set()
        await release_cleanup.wait()
        await real_close(self, grace=grace)
        closed.set()

    async def _on_rejected(event: WorkflowRunRejected) -> None:
        if event.error == "cancelled":
            await host.cancel_workflow()  # a subscriber cannot cancel the reply carrying its own refusal
            if shutdown_in_reply:
                await host.shutdown()  # nor may shutdown wait for the task publishing this reply
            reply_returned.set()

    caller: asyncio.Task[Any] | None = None
    try:
        await host.start()
        await confirm(host, "blocked")
        await confirm(host, "chain")
        monkeypatch.setenv("CHRYS_TEST_BLOCK_LOAD", "1")
        monkeypatch.setattr(WorkflowWorkerClient, "close", _close)
        await host.event_bus.subscribe(WorkflowRunRejected, _on_rejected)
        async with capture_event_sequence(
            host.event_bus, WorkflowRunRejected, WorkflowRunAccepted, WorkflowRunFinished, WorkflowNodeStateChanged
        ) as events:
            caller = asyncio.create_task(
                host.run_workflow_until_final(host.workflow_target("blocked"), timeout=3600 if deadline else 0)
            )
            await wait_for(
                lambda: marker.exists() or caller.done(),
                description="the worker entered module loading",
                timeout=ENGINE_TURN_TIMEOUT,
            )
            if caller.done():
                await caller
            assert marker.exists()
            assert host.engine.execution().cancellable
            if deadline:
                expire.set()
            else:
                await host.cancel_workflow()
            await wait_for(lambda: closing.is_set() or caller.done(), description="cancellation started worker cleanup")
            assert closing.is_set() and not caller.done()
            await host.cancel_workflow()
            release_cleanup.set()
            await wait_for(caller.done, timeout=15, description="cancelled load answered its caller after cleanup")
            if deadline:
                with pytest.raises(WorkflowRunTimeoutError, match="timed out during admission"):
                    await caller
            else:
                with pytest.raises(WorkflowRunRejectedError) as rejected:
                    await caller
                assert rejected.value.event.error == "cancelled"
        assert closed.is_set() and reply_returned.is_set()
        assert len(of_type(events, WorkflowRunRejected)) == 1
        assert not of_type(events, WorkflowRunAccepted)
        assert not of_type(events, WorkflowRunFinished)
        assert not of_type(events, WorkflowNodeStateChanged)
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
        if not shutdown_in_reply:
            result, _events = await run(host, "chain", input_text="next")
            assert result.outcome.value == "completed"
    finally:
        release_cleanup.set()
        await host.shutdown()
        if caller is not None:
            await asyncio.gather(caller, return_exceptions=True)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_duplicate_of_an_in_flight_request_is_answered_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    entered, release = _hold_admission(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "chain")
        request = WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="same")
        async with capture_event_sequence(
            host.event_bus, WorkflowRunAccepted, WorkflowRunRejected, WorkflowRunStarted, WorkflowRunFinished
        ) as events:
            await host.event_bus.publish(request)
            await wait_for(
                entered.is_set,
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="admission reached the held boundary",
            )
            await host.event_bus.publish(request)  # the retransmit lands while the first is still being admitted
            release.set()
            # This boundary spans the worker subprocess boot and the durable
            # run records; the 5s default was exceeded on a loaded Windows CI
            # shard, so it waits on what is left of the test's shared budget.
            await wait_for(
                lambda: of_type(events, WorkflowRunFinished),
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="the run finished",
            )
            await host.engine.workflows.wait_idle()
        accepted = of_type(events, WorkflowRunAccepted)
        assert [a.request_id for a in accepted] == ["same"]
        assert not of_type(events, WorkflowRunRejected)
        assert [s.run_id for s in of_type(events, WorkflowRunStarted)] == [accepted[0].run_id]
        assert [(f.run_id, f.outcome) for f in of_type(events, WorkflowRunFinished)] == [
            (accepted[0].run_id, "completed")
        ]
    finally:
        await host.shutdown()


async def test_a_refused_hosted_request_leaves_the_active_run_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "sleeper")
        async with capture_event_sequence(
            host.event_bus, WorkflowRunStarted, WorkflowRunFinished, WorkflowNodeStateChanged
        ) as events:
            run_id = await _start_on_the_bus(host, "sleeper", "original", events)
            await wait_for(
                lambda: [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "running"],
                description="the sleeper node is running",
            )
            with pytest.raises(WorkflowRunRejectedError) as rejected:
                await host.run_workflow_until_final(host.workflow_target("sleeper"))
            assert rejected.value.event.error == "workflow_active"
            # The refused caller's cleanup must not give up a run it never owned.
            assert host.engine.workflows.active_run_id == run_id
            assert host.engine.execution() == ExecutionSnapshot(
                "workflow", run_id=run_id, cancellable=True, request_id="original"
            )
            assert not of_type(events, WorkflowRunFinished)
            await host.cancel_workflow()
            await wait_for(lambda: of_type(events, WorkflowRunFinished), description="the run was cancelled")
            await host.engine.workflows.wait_idle()
        assert [(f.run_id, f.outcome, f.reason) for f in of_type(events, WorkflowRunFinished)] == [
            (run_id, "cancelled", "")
        ]
    finally:
        await host.shutdown()


def _park_the_prompt(monkeypatch: pytest.MonkeyPatch, *, registered: bool) -> tuple[asyncio.Event, asyncio.Event]:
    """Park the next prompt inside its pre-admission preparation: before it is registered, or right after."""
    parked = asyncio.Event()
    release = asyncio.Event()
    real_start = TurnCoordinator._start_pre_admission_preparation

    async def _start(self: TurnCoordinator, tracker: PreAdmissionPreparationTracker) -> None:
        if registered:
            await real_start(self, tracker)
        parked.set()
        await release.wait()
        if not registered:
            await real_start(self, tracker)

    monkeypatch.setattr(TurnCoordinator, "_start_pre_admission_preparation", _start)
    return parked, release


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_prompt_that_finishes_preparing_under_a_held_run_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MockChatClient(responses=[MockResponse(text="turn done")])
    patch_runtime(monkeypatch, [client])
    parked, release = _park_the_prompt(monkeypatch, registered=False)
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "sleeper")
        lease = host.engine.turns.turn_state.lease
        async with capture_event_sequence(
            host.event_bus, Error, WorkflowRunAccepted, WorkflowRunRejected, WorkflowRunFinished
        ) as events:
            prompt = asyncio.create_task(host.event_bus.publish(UserMessage(text="hello", session_id=host.session_id)))
            await wait_for(
                parked.is_set,
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="prompt preparation reached the held boundary",
            )
            assert not lease.pre_admission_preparations
            await host.event_bus.publish(
                WorkflowRunRequest(target=host.workflow_target("sleeper", new_session=True), request_id="wf")
            )
            await wait_for(
                lambda: of_type(events, WorkflowRunAccepted),
                description="the run was accepted",
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
            )
            release.set()  # the prompt registers its preparation and reaches admission under the held run
            await prompt
            assert lease.workflow is not None
            assert not lease.pre_admission_preparations
            assert lease.run_task is None
            assert [(e.code, e.message) for e in of_type(events, Error)] == [
                ("workflow_active", "A workflow run is active. Cancel the workflow run first.")
            ]
            await host.cancel_workflow()
            await wait_for(lambda: of_type(events, WorkflowRunFinished), description="the run was cancelled")
            await host.engine.workflows.wait_idle()
        assert not of_type(events, WorkflowRunRejected)
        assert client.call_count == 0
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_a_registered_preparation_refuses_the_workflow_request_as_a_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = MockChatClient(responses=[MockResponse(text="turn done")])
    patch_runtime(monkeypatch, [client])
    parked, release = _park_the_prompt(monkeypatch, registered=True)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "chain")
        lease = host.engine.turns.turn_state.lease
        async with capture_event_sequence(
            host.event_bus, Error, WorkflowRunAccepted, WorkflowRunRejected, WorkflowRunFinished
        ) as events:
            prompt = asyncio.create_task(host.event_bus.publish(UserMessage(text="hello", session_id=host.session_id)))
            await wait_for(
                parked.is_set,
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="prompt preparation reached the held boundary",
            )
            assert lease.pre_admission_preparations
            assert host.engine.execution() == ExecutionSnapshot("idle")  # preparing is not yet a turn
            await host.event_bus.publish(
                WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="wf")
            )
            assert [(r.request_id, r.error) for r in of_type(events, WorkflowRunRejected)] == [("wf", "turn_active")]
            release.set()
            await prompt
            await host.engine.wait_for_run_task()
        assert not of_type(events, WorkflowRunAccepted)
        assert not of_type(events, Error)
        assert client.call_count == 1
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


# -- deadlines and cancellation while an agent shell is still opening -----------------------


async def test_a_cancel_right_after_acceptance_does_not_wait_out_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)

    async def _cancel_on_accept(_event: WorkflowRunAccepted) -> None:
        await host.cancel_workflow()

    await host.event_bus.subscribe(WorkflowRunAccepted, _cancel_on_accept)
    try:
        await confirm(host, "chain")
        started = time.monotonic()
        result, events = await run(host, "chain", input_text="x", timeout=3600)
        elapsed = time.monotonic() - started
        assert (result.outcome.value, result.reason) == ("cancelled", "")
        assert elapsed < 60
        assert not [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "running"]
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_a_cancel_while_the_agent_shell_opens_still_closes_the_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "agent", _agent_workflow(PROFILE))
    host = make_host(tmp_path, project=project)
    opening = asyncio.Event()
    release = asyncio.Event()
    closed: list[WorkflowAgentShell] = []
    real_close = WorkflowAgentShell.close

    async def _hold_mount(event: InvocationStarted) -> None:
        if event.origin.kind == "workflow_node":
            opening.set()
            await release.wait()

    async def _close(shell: WorkflowAgentShell) -> None:
        closed.append(shell)
        await real_close(shell)

    monkeypatch.setattr(WorkflowAgentShell, "close", _close)
    await host.event_bus.subscribe(InvocationStarted, _hold_mount)
    try:
        await confirm(host, "agent")
        caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("agent"), input_text="x"))
        await wait_for(
            opening.is_set,
            timeout=ENGINE_TEST_WAIT_TIMEOUT,
            description="agent shell reached the held opening boundary",
        )
        await host.cancel_workflow()
        result = await caller
        assert (result.outcome.value, result.reason) == ("cancelled", "")
        assert len(closed) == 1
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        release.set()
        await host.shutdown()


async def test_a_node_record_lost_in_the_terminal_batch_still_ends_the_run_as_storage_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop's output record and the completed terminal come out of one scheduler batch: the loss wins."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "loop", LOOP_ONCE)
    host = make_host(tmp_path, project=project)
    real_write = store_module.atomic_write_owner_only_bytes
    lost: list[str] = []

    def _write(path: Path, payload: bytes, *, create_parents: bool = True) -> None:
        if path.parent.name == "nodes" and path.name.startswith("loop@iter#1.1.output."):
            lost.append(path.name)
            raise OSError(errno.ENOSPC, "No space left on device", str(path))
        real_write(path, payload, create_parents=create_parents)

    try:
        await confirm(host, "loop")
        monkeypatch.setattr(store_module, "atomic_write_owner_only_bytes", _write)
        result, events = await run(host, "loop", input_text="x")
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, result.run_id)
        assert len(lost) == 1
        assert (result.outcome.value, result.error) == ("storage_failed", "A node record could not be written.")
        assert read_run_terminal(directory).outcome == "storage_failed"
        assert [(f.outcome, f.error, f.degraded) for f in of_type(events, WorkflowRunFinished)] == [
            ("storage_failed", "A node record could not be written.", False)
        ]
        assert read_node_value(directory, "loop@iter#1", 1, NODE_RECORD_OUTPUT) is None
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_a_cancel_issued_inside_a_run_event_handler_does_not_deadlock_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Node events are published under the runner's lock: a subscriber cancelling inline must not wait for it."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    cancelled_at: list[str] = []

    async def _cancel_inline(event: WorkflowNodeStateChanged) -> None:
        if event.state == "running":
            cancelled_at.append(event.activation_id)
            await host.cancel_workflow()  # returns without the lock: the cancel is queued behind this batch

    await host.event_bus.subscribe(WorkflowNodeStateChanged, _cancel_inline)
    try:
        await confirm(host, "sleeper")
        result = await host.run_workflow_until_final(host.workflow_target("sleeper"), input_text="x")
        assert cancelled_at == ["fn@iter#1"]
        assert (result.outcome.value, result.reason) == ("cancelled", "")
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_a_shutdown_refuses_the_request_queued_behind_the_run_it_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing is declared before the run is drained: a requester waiting for the lease is refused, not admitted."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    host = make_host(tmp_path, project=project)
    await host.start()
    await confirm(host, "sleeper")
    async with capture_event_sequence(
        host.event_bus, WorkflowRunStarted, WorkflowRunFinished, WorkflowRunAccepted, WorkflowRunRejected
    ) as events:
        run_id = await _start_on_the_bus(host, "sleeper", "first", events)

        async def _next_run() -> None:
            await host.engine.workflows.wait_idle()
            await host.event_bus.publish(
                WorkflowRunRequest(target=host.workflow_target("sleeper", new_session=True), request_id="second")
            )

        # Eager: the follower is parked on the lease before the shutdown starts draining it.
        follower = asyncio.create_task(_next_run(), eager_start=True)
        await host.shutdown()
        await follower
    assert [(f.run_id, f.outcome, f.reason) for f in of_type(events, WorkflowRunFinished)] == [
        (run_id, "cancelled", "shutdown")
    ]
    assert [(r.request_id, r.error) for r in of_type(events, WorkflowRunRejected)] == [("second", "shutting_down")]
    assert [a.request_id for a in of_type(events, WorkflowRunAccepted)] == ["first"]
    assert host.engine.execution() == ExecutionSnapshot("idle")
    assert host.engine.workflows.active_run_id is None
    await _workers_gone(workers)


async def test_a_request_during_a_session_restore_is_refused_rather_than_admitted_under_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The restore's guard holds across its reads: no run slips in to have its live journal reconciled as an orphan."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_load = SessionPersistence.load_session

    async def _load(
        self: SessionPersistence, session_id: str, *, prefer_recovery: bool = False
    ) -> dict[str, Any] | None:
        state = await real_load(self, session_id, prefer_recovery=prefer_recovery)
        entered.set()
        await release.wait()  # the restore has read the saved state and is about to fence and rebuild
        return state

    try:
        await confirm(host, "chain")
        await host.run_until_final("Save a chat session")
        first, _events = await run(host, "chain", input_text="saved")
        session_id = host.session_id
        monkeypatch.setattr(SessionPersistence, "load_session", _load)
        async with capture_event_sequence(
            host.event_bus, WorkflowRunRejected, WorkflowRunAccepted, SessionRestored, Error
        ) as events:
            restore = asyncio.create_task(host.event_bus.publish(SessionRestore(session_id=session_id)))
            await wait_for(
                entered.is_set,
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="session restore reached the held boundary",
            )
            await host.event_bus.publish(
                WorkflowRunRequest(target=host.workflow_target("chain", new_session=True), request_id="under-restore")
            )
            assert [(r.request_id, r.error) for r in of_type(events, WorkflowRunRejected)] == [
                ("under-restore", "engine_busy")
            ]
            assert host.engine.workflows.active_run_id is None
            release.set()
            await restore
            assert [e.session_id for e in of_type(events, SessionRestored)] == [session_id]
            assert not of_type(events, WorkflowRunAccepted)
            assert not of_type(events, Error)
        # The restore over, the lease is free again and the earlier record was never touched.
        second, _events = await run(host, "chain", input_text="after")
        assert second.outcome.value == "completed"
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        assert read_run_terminal(run_dir(session_dir, first.run_id)).outcome == "completed"
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        release.set()
        await host.shutdown()


async def test_a_replayed_acceptance_for_an_earlier_request_is_not_narrated_as_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retransmitted request id is answered with its original acceptance; the host keeps narrating its own run."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    replies: list[WorkflowRunAccepted] = []

    async def _record(event: WorkflowRunAccepted) -> None:
        replies.append(event)

    try:
        await confirm(host, "chain")
        first, first_events = await run(host, "chain", input_text="first")
        first_request = of_type(first_events, WorkflowRunAccepted)[0].request_id

        async def _retransmit(event: WorkflowNodeStateChanged) -> None:
            if event.state == "running":  # the second run is live: its predecessor's request id comes in again
                await host.event_bus.publish(
                    WorkflowRunRequest(
                        target=host.workflow_target("chain", new_session=True),
                        request_id=first_request,
                        input_text="first",
                    )
                )

        await host.event_bus.subscribe(WorkflowRunAccepted, _record)
        await host.event_bus.subscribe(WorkflowNodeStateChanged, _retransmit)
        second = await host.run_workflow_until_final(host.workflow_target("chain"), input_text="second")
        assert second.run_id != first.run_id
        assert [output.value.text for output in second.outputs] == ["second!"]
        second_request = replies[0].request_id
        assert second_request != first_request
        assert [(reply.request_id, reply.run_id) for reply in replies] == [
            (second_request, second.run_id),
            (first_request, first.run_id),  # replayed under the second run, and not mistaken for it
        ]
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_caller_cancelled_while_its_request_is_still_being_published_gives_up_its_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscriber behind the coordinator holds the publish open after the run was admitted; the cancel still abandons it."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", SLEEPER)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    publishing = asyncio.Event()
    release = asyncio.Event()

    async def _hold_the_publish(_event: WorkflowRunRequest) -> None:
        if publishing.is_set():
            return
        publishing.set()
        await release.wait()

    try:
        await host.start()
        await confirm(host, "sleeper")
        await confirm(host, "chain")
        await host.event_bus.subscribe(WorkflowRunRequest, _hold_the_publish)  # behind the coordinator's handler
        async with capture_event_sequence(
            host.event_bus, WorkflowRunAccepted, WorkflowRunFinished, WorkflowNodeStateChanged
        ) as events:
            caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("sleeper")))
            await wait_for(
                publishing.is_set,
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
                description="workflow request reached the held publish boundary",
            )
            await wait_for(
                lambda: any(event.state == "running" for event in of_type(events, WorkflowNodeStateChanged)),
                description="the node is running",
                timeout=ENGINE_TEST_WAIT_TIMEOUT,
            )
            run_id = host.engine.workflows.active_run_id
            assert run_id is not None
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert host.engine.workflows.active_run_id is None
            assert host.engine.execution() == ExecutionSnapshot("idle")
            assert [(event.run_id, event.outcome, event.reason) for event in of_type(events, WorkflowRunFinished)] == [
                (run_id, "cancelled", "shutdown")
            ]
        release.set()
        result, _events = await run(host, "chain", input_text="next")
        assert result.outcome.value == "completed"
    finally:
        release.set()
        await host.shutdown()


@pytest.mark.parametrize("event_type", [InvocationProgress, UsageUpdate])
async def test_a_shutdown_awaited_inside_an_agent_progress_handler_does_not_deadlock_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    event_type: type[InvocationProgress] | type[UsageUpdate],
) -> None:
    """Attempt finalization drains progress and usage; inline shutdown must not wait on itself."""
    if event_type is InvocationProgress:
        # Deliver progress during attempt finalization, so cancelling the activation
        # deterministically lands while it is draining the inline shutdown handler.
        publish_allowed = asyncio.Event()
        publish = SubAgentEventMiddleware._publish_progress
        flush = SubAgentEventMiddleware.flush_progress

        async def held_publish(self: SubAgentEventMiddleware) -> None:
            await publish_allowed.wait()
            await publish(self)

        async def release_and_flush(self: SubAgentEventMiddleware) -> None:
            publish_allowed.set()
            await flush(self)

        monkeypatch.setattr(
            SubAgentEventMiddleware, "_publish_progress", create_autospec(publish, side_effect=held_publish)
        )
        monkeypatch.setattr(
            SubAgentEventMiddleware, "flush_progress", create_autospec(flush, side_effect=release_and_flush)
        )
    usage = UsageDetails(input_token_count=10, output_token_count=5)
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="LGTM", usage_details=usage)])],
    )
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "agent", _agent_workflow(PROFILE))
    host = make_host(tmp_path, project=project)
    shut_down = asyncio.Event()

    async def _shutdown_inline(event: InvocationProgress | UsageUpdate) -> None:
        node_event = (
            event.origin.kind == "workflow_node"
            if isinstance(event, InvocationProgress)
            else event.usage_source_id != host.session_id and event.total_tokens == 15
        )
        if node_event:
            await host.shutdown()  # waited on from within, this run could never end
            shut_down.set()

    await host.event_bus.subscribe(event_type, _shutdown_inline)
    try:
        await confirm(host, "agent")
        caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("agent"), input_text="diff"))
        done, _pending = await asyncio.wait({caller}, timeout=10)
        if caller not in done:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            pytest.fail("the run never reached its terminal")
        result = caller.result()
        assert shut_down.is_set()
        assert (result.outcome.value, result.reason) == ("cancelled", "shutdown")
        assert host.engine.workflows.active_run_id is None
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_a_shutdown_awaited_inside_a_rejection_handler_still_answers_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rejection goes out on the admission task: cancelling that task under its own reply would cut the reply short."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", python_workflow("def fn(text):\n    return text.text + '!'\n", "fn"))
    host = make_host(tmp_path, project=project)
    shut_down = asyncio.Event()

    async def _shutdown_inline(event: WorkflowRunRejected) -> None:
        await host.shutdown()  # waited on from within, this reply could never be delivered
        shut_down.set()

    await host.event_bus.subscribe(WorkflowRunRejected, _shutdown_inline)
    try:
        caller = asyncio.create_task(
            host.run_workflow_until_final(host.workflow_target("chain"), input_text="x")
        )  # never confirmed
        done, _pending = await asyncio.wait({caller}, timeout=10)
        if caller not in done:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            pytest.fail("the caller never got its answer")
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            caller.result()
        assert rejected.value.event.error == "not_confirmed"
        assert shut_down.is_set()
        assert host.engine.workflows.active_run_id is None
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_a_shutdown_awaited_inside_an_agent_tool_event_handler_releases_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel's tool task publishes the event and the queued cancel ends that task under the subscriber.

    The shutdown still returns from the drain at once (a tool task is one of the pass's), and the session's
    resources and lock are released by a task of the engine's own, which outlives the cancelled subscriber.
    """
    target = tmp_path / "input.txt"
    target.write_text("hello")
    read = ("read_file", "call_1", {"path": str(target)})
    patch_runtime(
        monkeypatch,
        [
            MockChatClient(responses=[]),
            MockChatClient(responses=[MockResponse(tool_calls=[read]), MockResponse(text="done")]),
        ],
        builtin_tools=True,
    )
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "review", _agent_workflow(PROFILE))
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.read"])])
    subscriber_cancelled = asyncio.Event()
    session_ids: list[str] = []

    async def _shutdown_inline(event: InvocationToolCallStart) -> None:
        if event.origin.kind != "workflow_node":
            return
        session_ids.append(event.origin.session_id)
        try:
            await host.shutdown()  # the cancel this queues aborts the pass, and with it this task
        except asyncio.CancelledError:
            subscriber_cancelled.set()
            raise

    await host.event_bus.subscribe(InvocationToolCallStart, _shutdown_inline)
    try:
        await confirm(host, "review")
        caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("review"), input_text="diff"))
        done, _pending = await asyncio.wait({caller}, timeout=10)
        if caller not in done:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            pytest.fail("the run never reached its terminal")
        result = caller.result()
        assert (result.outcome.value, result.reason) == ("cancelled", "shutdown")
        assert subscriber_cancelled.is_set()
        assert host.engine.workflows.active_run_id is None
        assert host.engine.execution() == ExecutionSnapshot("idle")
        (session_id,) = session_ids
        assert session_id
        await wait_for(lambda: not host.engine.session.guard.owns(session_id), description="session lock released")
        assert host.engine.current.loaded is None
        await _workers_gone(workers)
    finally:
        await host.shutdown()


def _running(node_id: str) -> Callable[[Event], bool]:
    def marks(event: Event) -> bool:
        return isinstance(event, WorkflowNodeStateChanged) and event.state == "running" and event.node_id == node_id

    return marks


def _output(kind: str) -> Callable[[Event], bool]:
    def marks(event: Event) -> bool:
        return isinstance(event, WorkflowNodeOutput) and event.kind == kind

    return marks


def _any(event: Event) -> bool:
    return True


@pytest.mark.parametrize(
    ("source", "event_type", "marks", "interactive"),
    [
        pytest.param(
            SLEEPER, WorkflowNodeStateChanged, _running("fn"), False, id="first node running: by the run task"
        ),
        pytest.param(
            SLEEPER_SECOND,
            WorkflowNodeStateChanged,
            _running("sleeper"),
            False,
            id="second node running: by a node task",
        ),
        pytest.param(
            SLEEPER_SECOND,
            WorkflowNodeOutput,
            _output("final"),
            False,
            id="final output: journal lock only",
        ),
        pytest.param(EMITTER_SECOND, WorkflowNodeOutput, _output("emit"), False, id="emit: by the worker's task"),
        pytest.param(ASKER, WorkflowNodeAskUser, _any, True, id="ask: by the worker's ask task"),
    ],
)
async def test_a_shutdown_awaited_inside_a_run_event_handler_does_not_deadlock_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: bytes,
    event_type: type[Event],
    marks: Callable[[Event], bool],
    interactive: bool,
) -> None:
    """The engine cannot wait the run out from inside the run's own event: the shutdown returns, the queued cancel ends it.

    Every run event is published under a lock the cancel needs: scheduler events under the runner's, outputs
    and asks under the journal's alone; the second node of each source is still to run when the cancel lands.
    """
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "sleeper", source)
    host = make_host(tmp_path, project=project, allow_user_interaction=interactive)
    shut_down = asyncio.Event()

    async def _shutdown_inline(event: Event) -> None:
        if marks(event):
            await host.shutdown()  # waited on from within, this run could never end
            shut_down.set()

    await host.event_bus.subscribe(event_type, _shutdown_inline)
    try:
        await confirm(host, "sleeper")
        caller = asyncio.create_task(host.run_workflow_until_final(host.workflow_target("sleeper"), input_text="x"))
        done, _pending = await asyncio.wait({caller}, timeout=10)
        if caller not in done:
            caller.cancel()
            await asyncio.gather(caller, return_exceptions=True)
            pytest.fail("the run never reached its terminal")
        result = caller.result()
        assert shut_down.is_set()
        assert (result.outcome.value, result.reason) == ("cancelled", "shutdown")
        assert host.engine.workflows.active_run_id is None
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_an_unconfirmed_file_is_refused_before_its_code_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loading a workflow file runs its top-level code, so a file that is not confirmed as it is now is never loaded."""
    marker = tmp_path / "loaded"
    source = python_workflow(
        f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\ndef fn(text):\n    return text\n", "fn"
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "marker", source)
    host = make_host(tmp_path, project=project)
    try:
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "marker", input_text="x")
        assert rejected.value.event.error == "not_confirmed"
        assert not marker.exists()

        await confirm(host, "marker")  # the confirmation's preview is the load that runs it
        assert marker.read_text() == "ran"
        marker.unlink()
        write_workflow(project, "marker", source.replace(b"'ran'", b"'ran again'"))
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "marker", input_text="x")
        assert rejected.value.event.error == "not_confirmed"
        assert not marker.exists()
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


@pytest.mark.parametrize("previously_confirmed", [False, True])
async def test_unconfirmed_interpreter_metadata_is_rejected_before_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, previously_confirmed: bool
) -> None:
    """Both a new file and an edited confirmed file can name executable code through their metadata."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    path = write_workflow(project, "chain", CHAIN)
    candidate = path.with_name("candidate")
    candidate.write_text("unconfirmed interpreter", encoding="utf-8")
    host = make_host(tmp_path, project=project)
    try:
        if previously_confirmed:
            await confirm(host, "chain")
        source = b"# /// script\n# [tool.chrys]\n# python = 'candidate'\n# ///\n" + CHAIN
        write_workflow(project, "chain", source)
        probe = create_autospec(
            environment_module.probe_interpreter, side_effect=InterpreterError("the unconfirmed executable ran")
        )
        monkeypatch.setattr(environment_module, "probe_interpreter", probe)
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "chain")
        assert rejected.value.event.error == "not_confirmed"
        probe.assert_not_awaited()
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_confirmed_bytes_still_require_the_recorded_environment_after_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    workers = _track_workers(monkeypatch)
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    host = make_host(tmp_path, project=project)
    try:
        preview = await confirm(host, "chain")
        prepared = create_autospec(
            coordinator_module.prepare_workflow_environment,
            return_value=replace(preview.environment, environment_fingerprint="changed"),
        )
        monkeypatch.setattr(coordinator_module, "prepare_workflow_environment", prepared)
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "chain")
        assert rejected.value.event.error == "not_confirmed"
        prepared.assert_awaited_once()
        assert len(workers) == 1  # only the confirmation preview loaded the module
        assert host.engine.execution() == ExecutionSnapshot("idle")
        await _workers_gone(workers)
    finally:
        await host.shutdown()


async def test_a_transient_failure_after_hosted_tool_calls_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kernel's whole-run gate, kept at the node boundary: hosted calls the failed exchange ran never run twice."""
    client = MockChatClient(responses=[MockResponse(text="never sent")])
    # A failed call reaches the whole-run owner only under service-side storage.
    monkeypatch.setattr(client, "STORES_BY_DEFAULT", True)

    def _failed_after_hosted_work() -> MockResponse:
        # The adapter found the failure after the provider ran hosted work it never yielded.
        raise ProviderResponseError(
            "stream_truncated",
            "the response failed",
            retryable=True,
            observed_contents=(
                Content.from_mcp_server_tool_call("mc1", "create_issue", server_name="github"),
                Content.from_mcp_server_tool_result("mc1", output=[Content.from_text("created #42")]),
            ),
        )

    monkeypatch.setattr(client, "_next_response", _failed_after_hosted_work)
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client, MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(
        project,
        "hosted",
        (
            "from chrys.workflows import Retry, WorkflowBuilder\n"
            "wf = WorkflowBuilder('agents')\n"
            f"_review = wf.agent('review', profile={PROFILE!r}, retry=Retry(max_attempts=2, backoff=0.01))\n"
            "wf.start(_review)\nwf.output(_review)\nworkflow = wf.build()\n"
        ).encode(),
    )
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "hosted")
        result, events = await run(host, "hosted", input_text="x")
        assert result.outcome.value == "node_failed"
        failed = [e for e in of_type(events, WorkflowNodeStateChanged) if e.state == "failed"]
        assert [(e.node_id, e.attempt, e.error_class) for e in failed] == [("review", 1, "agent_transient")]
        assert len(client.call_history) == 1  # the one request; a retry would have taken the next client
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()


async def test_restoring_another_session_during_a_run_is_refused_to_its_requester(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal names the session the restore asked for, so the headless restore call raises instead of waiting."""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "chain", CHAIN)
    write_workflow(project, "sleeper", SLEEPER)
    saved = make_host(tmp_path, project=project)
    await confirm(saved, "chain")
    await saved.run_until_final("Save a chat session")
    saved_session_id = saved.session_id
    await saved.shutdown()

    host = make_host(tmp_path, project=project)
    try:
        await host.start()
        await confirm(host, "sleeper")
        session_id = host.session_id
        async with capture_event_sequence(host.event_bus, Error, WorkflowRunStarted, WorkflowRunFinished) as events:
            run_id = await _start_on_the_bus(host, "sleeper", "r1", events)
            restore = asyncio.create_task(host.restore_session(saved_session_id))
            done, _pending = await asyncio.wait({restore}, timeout=10)
            if restore not in done:
                restore.cancel()
                await asyncio.gather(restore, return_exceptions=True)
                pytest.fail("the restore never heard the refusal")
            with pytest.raises(HeadlessRunError) as refused:
                restore.result()
            assert (refused.value.event.code, refused.value.event.session_id) == ("workflow_active", saved_session_id)
            assert host.session_id == session_id
            assert host.engine.execution() == ExecutionSnapshot(
                "workflow", run_id=run_id, cancellable=True, request_id="r1"
            )
            await host.cancel_workflow()
            await wait_for(lambda: of_type(events, WorkflowRunFinished), description="the run was cancelled")
        await host.engine.workflows.wait_idle()
        assert host.engine.execution() == ExecutionSnapshot("idle")
    finally:
        await host.shutdown()
