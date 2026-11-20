# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP retries and human waits belong to the current workflow node attempt."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.agent_node_build as agent_node_build_module
from chrys.foundation.events.types import (
    ApprovalRequest,
    ApprovalResponse,
    AskUserAnswer,
    AskUserResponse,
    QuestionToUser,
    SetApprovalMode,
    WorkflowNodeRetryRequest,
    WorkflowNodeStateChanged,
    WorkflowRunAccepted,
)
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.service.acp_client.client import AcpAgentClient
from chrys.service.acp_client.errors import AcpConnectError
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile
from chrys.service.trajectory.session import trajectory_events_path
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, run, write_workflow
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.trajectory_invariants import assert_trajectory_accounted, assert_trajectory_operation_settlement
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT


@pytest.mark.parametrize("scenario", ["permission", "ask_user"])
async def test_acp_waits_rebind_to_each_attempt_after_successful_body_and_failed_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    project = make_workdir(tmp_path)
    external = AgentProfile(
        name="External",
        acp=AcpAgentConfig(
            command=sys.executable,
            args=[str(STUB_SCRIPT)],
            env={"CHRYS_ACP_STUB_SCENARIO": scenario},
            idle_timeout_seconds=0,
        ),
    )
    write_workflow(
        project,
        "review",
        b"""
from chrys.workflows import WorkflowBuilder
evaluations = 0
def flaky(value):
    global evaluations
    evaluations += 1
    if evaluations == 1:
        raise RuntimeError('retry the edge')
    return True
def done(value):
    return value
wf = WorkflowBuilder('retry')
node = wf.agent('node', profile='External')
end = wf.python('done', done)
wf.edge(node, end, when=flaky)
wf.start(node)
wf.output(end)
workflow = wf.build()
""",
    )
    connects = 0
    real_connect = AcpAgentClient.connect

    async def connect(client):
        nonlocal connects
        connects += 1
        if connects % 2:
            raise AcpConnectError("injected handshake failure")
        return await real_connect(client)

    monkeypatch.setattr(AcpAgentClient, "connect", create_autospec(real_connect, side_effect=connect))
    # Each refused handshake is still scheduled as a retry, without the production connect backoff's wait.
    monkeypatch.setattr(agent_node_build_module, "RETRY_BACKOFF_SCHEDULE", (0,))
    host = make_host(tmp_path, project=project, profiles=[external], allow_user_interaction=True)

    async def set_manual(event: WorkflowRunAccepted) -> None:
        await host.event_bus.publish(SetApprovalMode(mode="manual", persist=False))

    async def approve(event: ApprovalRequest) -> None:
        await host.event_bus.publish(
            ApprovalResponse(request_id=event.request_id, approved=True, session_id=event.session_id)
        )

    async def answer(event: QuestionToUser) -> None:
        await host.event_bus.publish(
            AskUserResponse(
                request_id=event.request_id, answers=(AskUserAnswer(values=("yes",)),), session_id=event.session_id
            )
        )

    async def retry(event: WorkflowNodeStateChanged) -> None:
        if event.state == "awaiting_retry":
            assert event.attempt == 1
            await host.event_bus.publish(
                WorkflowNodeRetryRequest(
                    session_id=event.session_id,
                    run_id=event.run_id,
                    node_id=event.node_id,
                    activation_id=event.activation_id,
                    expected_failed_attempt=event.attempt,
                )
            )

    await host.event_bus.subscribe(WorkflowRunAccepted, set_manual)
    await host.event_bus.subscribe(ApprovalRequest, approve)
    await host.event_bus.subscribe(QuestionToUser, answer)
    await host.event_bus.subscribe(WorkflowNodeStateChanged, retry)
    try:
        await confirm(host, "review")
        # Not one turn: two node attempts, each opening with a refused handshake, and the worker and
        # both agents are interpreters to spawn.
        result, _ = await asyncio.wait_for(run(host, "review"), timeout=ENGINE_TEST_WAIT_TIMEOUT)
        assert result.outcome.value == "completed"
        session_dir = host.workflow_session_dir
        assert session_dir is not None
    finally:
        await host.shutdown()
    assert connects == 4
    read = read_trajectory(trajectory_events_path(session_dir))
    assert_trajectory_accounted(read)
    assert_trajectory_operation_settlement(read.events)
    nodes = [
        event
        for event in read.events
        if event.event_type == EventType.WORKFLOW_NODE_STARTED and event.payload["node"] == "node"
    ]
    assert [event.payload["attempt"] for event in nodes] == [1, 2]
    retry_starts = [event for event in read.events if event.event_type == EventType.RETRY_SCHEDULED]
    waits = [
        event
        for event in read.events
        if event.event_type == (EventType.APPROVAL_REQUESTED if scenario == "permission" else EventType.WAIT_STARTED)
    ]
    assert len(retry_starts) == len(waits) == 2
    assert [event.parent_operation_id for event in retry_starts] == [event.operation_id for event in nodes]
    assert [event.parent_operation_id for event in waits] == [event.operation_id for event in nodes]
    assert all(event.actor.role == "workflow_node" for event in (*retry_starts, *waits))
    for node, retry_start, wait in zip(nodes, retry_starts, waits, strict=True):
        finish = next(
            event
            for event in read.events
            if event.event_type == EventType.WORKFLOW_NODE_FINISHED and event.operation_id == node.operation_id
        )
        assert node.sequence < retry_start.sequence < wait.sequence < finish.sequence
