# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Outgoing-edge failures retry the activation without replacing its accounting owner."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import InvocationStarted, WorkflowNodeRetryRequest, WorkflowNodeStateChanged
from chrys.kernel import UsageDetails
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile
from chrys.service.session.runtime_metadata import TOTAL_SESSION_TOKENS_KEY
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.layout import run_dir
from chrys.service.workflows.transcript import read_node_transcript, read_node_usage
from tests.orchestration.workflows._hosting import (
    PROFILE,
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.event_capture import capture_event_sequence
from tests.support.waiting import ENGINE_TURN_TIMEOUT


@pytest.mark.parametrize("backend", ["kernel", "acp"])
async def test_successful_agent_keeps_activation_accounting_when_edge_evaluation_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    project = make_workdir(tmp_path)
    reference = project / "reference.txt"
    reference.write_text("reference")
    profile = make_profile(builtins=["filesystem.read"])
    clients = [MockChatClient(responses=[])]
    trace = tmp_path / "transports.jsonl"
    if backend == "kernel":
        clients.append(
            MockChatClient(
                responses=[
                    response
                    for _ in range(2)
                    for response in (
                        MockResponse(
                            tool_calls=[("read_file", "reused", {"path": str(reference)})],
                            usage_details=UsageDetails(input_token_count=3, output_token_count=2),
                        ),
                        MockResponse(
                            text="reviewed", usage_details=UsageDetails(input_token_count=2, output_token_count=1)
                        ),
                    )
                ]
            )
        )
        profiles, profile_name = [profile], PROFILE
    else:
        # This stub reuses remote tool IDs on each transport. An existing trace
        # skips its injected first-pass failure: only the workflow edge fails.
        trace.touch()
        external = AgentProfile(
            name="External",
            acp=AcpAgentConfig(
                command=sys.executable,
                args=[str(STUB_SCRIPT)],
                env={"CHRYS_ACP_STUB_SCENARIO": "retry_reused_tool_usage", "CHRYS_ACP_STUB_FLAG_FILE": str(trace)},
            ),
        )
        profiles, profile_name = [profile, external], "External"
    patch_runtime(monkeypatch, clients, builtin_tools=True)
    write_workflow(
        project,
        "review",
        (
            "from chrys.workflows import WorkflowBuilder\n"
            "evaluations = 0\n"
            "def flaky(value):\n"
            "    global evaluations\n"
            "    evaluations += 1\n"
            "    if evaluations == 1:\n"
            "        raise RuntimeError('injected condition failure')\n"
            "    return True\n"
            "def done(value):\n"
            "    return value\n"
            "wf = WorkflowBuilder('edge retry')\n"
            f"node = wf.agent('node', profile={profile_name!r})\n"
            "end = wf.python('done', done)\n"
            "wf.edge(node, end, when=flaky)\n"
            "wf.start(node)\nwf.output(end)\nworkflow = wf.build()\n"
        ).encode(),
    )
    observations = []
    original_run = WorkflowAgentShell.run

    async def observe(shell, text, *, timeout=None, trajectory_context=None, attempt=1):
        result = await original_run(
            shell, text, timeout=timeout, trajectory_context=trajectory_context, attempt=attempt
        )
        observations.append((shell, shell.evidence, shell._acp_counters.transport_ordinal))
        return result

    monkeypatch.setattr(WorkflowAgentShell, "run", create_autospec(original_run, side_effect=observe))
    host = make_host(tmp_path, project=project, profiles=profiles, allow_user_interaction=True)

    async def retry(event: WorkflowNodeStateChanged) -> None:
        if event.state == "awaiting_retry":
            assert event.node_id == "node" and event.attempt == 1
            await host.event_bus.publish(
                WorkflowNodeRetryRequest(
                    run_id=event.run_id,
                    node_id=event.node_id,
                    activation_id=event.activation_id,
                    request_id="retry-edge",
                    expected_failed_attempt=event.attempt,
                )
            )

    await host.event_bus.subscribe(WorkflowNodeStateChanged, retry)
    try:
        await confirm(host, "review")
        async with capture_event_sequence(host.event_bus, InvocationStarted) as starts:
            result, events = await asyncio.wait_for(
                run(host, "review", input_text="Review"), timeout=ENGINE_TURN_TIMEOUT
            )
        assert result.outcome.value == "completed"
        assert len(starts) == 1 and starts[0].opening_prompt == "Review"
        assert [
            event.attempt
            for event in events
            if isinstance(event, WorkflowNodeStateChanged) and event.node_id == "node" and event.state == "running"
        ] == [1, 2]
        first, second = observations
        assert first[0] is second[0]
        assert len(first[1].passes) == 1 and len(second[1].passes) == 2
        if backend == "acp":
            assert [first[2], second[2]] == [1, 2]
            assert len(trace.read_text().splitlines()) == 2
        else:
            assert first[1].local_answered.observed == 1
            assert second[1].local_answered.observed == 2
        session_dir = host.workflow_session_dir
        assert session_dir is not None
        directory = run_dir(session_dir, result.run_id)
        for attempt in (1, 2):
            usage = read_node_usage(directory, "node@iter#1", attempt)
            transcript = read_node_transcript(directory, "node@iter#1", attempt)
            assert usage is not None and transcript is not None
            assert (usage.tool_calls, usage.usage_tokens) == (attempt, attempt * 8)
            assert (transcript.usage.tool_calls, transcript.usage.usage_tokens) == (attempt, attempt * 8)
            assert transcript.status == "completed"  # the body succeeded; its outgoing edge failed
        state = (
            await JsonFileStateStore(tmp_path / "sessions").load_workflow_session(host.workflow_session_id)
        ).encode()
        assert state is not None and state[TOTAL_SESSION_TOKENS_KEY] == 16
        if backend == "acp":
            assert len({json.loads(line)["pid"] for line in trace.read_text().splitlines()}) == 2
    finally:
        await host.event_bus.unsubscribe(WorkflowNodeStateChanged, retry)
        await host.shutdown()
