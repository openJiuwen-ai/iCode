# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real workflow runs reach the session recorder and nest agent work inside node attempts."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.service.analytics import Precision, SessionCounterSamples, analyze_trajectory
from chrys.service.analytics.export import analysis_json, perfetto_trace
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.trajectory.session import trajectory_events_path
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, patch_runtime, run, write_workflow
from tests.support.trajectory_invariants import assert_trajectory_accounted, assert_trajectory_operation_settlement


async def test_workflow_run_records_all_nodes_and_retries_with_agent_parentage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = b"""
from chrys.workflows import WorkflowBuilder, Retry
from pathlib import Path
def prepare(text):
    marker = Path('attempt')
    if not marker.exists():
        marker.write_text('attempted')
        raise ValueError('try again')
    return text.text + ' prepared'
wf = WorkflowBuilder('recorded')
a = wf.python('prepare', prepare, retry=Retry(max_attempts=2))
b = wf.agent('agent', profile='Headless')
wf.start(a)
wf.chain(a, b)
wf.output(b)
workflow = wf.build()
"""
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), MockChatClient(responses=[MockResponse(text="done")])])
    project = make_workdir(tmp_path)
    write_workflow(project, "recorded", source)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "recorded")
        result, _events = await run(host, "recorded", input_text="private input")
        assert result.outcome.value == "completed"
        session_dir = host.workflow_session_dir
        assert session_dir is not None
    finally:
        await host.shutdown()
    path = trajectory_events_path(session_dir)
    read = read_trajectory(path)
    assert_trajectory_accounted(read)
    assert read.unsupported_event_count == 0 and not read.corrupt_lines
    events = read.events
    assert_trajectory_operation_settlement(events)
    starts = [e for e in events if e.event_type == EventType.WORKFLOW_NODE_STARTED]
    assert [(e.payload["node"], e.payload["activation"], e.payload["attempt"]) for e in starts] == [
        ("prepare", "prepare@iter#1", 1),
        ("prepare", "prepare@iter#1", 2),
        ("agent", "agent@iter#1", 1),
    ]
    finishes = [e for e in events if e.event_type == EventType.WORKFLOW_NODE_FINISHED]
    assert [e.payload["outcome"] for e in finishes] == ["failed", "completed", "completed"]
    assert [e.operation_id for e in finishes] == [e.operation_id for e in starts]
    (run_start,) = [e for e in events if e.event_type == EventType.WORKFLOW_RUN_STARTED]
    (run_finish,) = [e for e in events if e.event_type == EventType.WORKFLOW_RUN_FINISHED]
    assert {e.parent_operation_id for e in starts} == {run_start.operation_id}
    assert run_start.operation_id == run_finish.operation_id == result.run_id
    (agent,) = [e for e in events if e.event_type == EventType.MODEL_RUN_STARTED]
    (agent_finish,) = [e for e in events if e.event_type == EventType.MODEL_RUN_FINISHED]
    assert agent.parent_operation_id == agent_finish.parent_operation_id == starts[-1].operation_id
    assert starts[-1].monotonic_ns <= agent.monotonic_ns <= agent_finish.monotonic_ns <= finishes[-1].monotonic_ns
    (cycle,) = [e for e in events if e.event_type == EventType.MODEL_CYCLE_STARTED]
    assert cycle.parent_operation_id == agent.operation_id
    assert run_start.sequence < starts[0].sequence < run_finish.sequence
    assert "private input" not in path.read_text()
    analysis = analyze_trajectory(path)
    assert analysis.diagnostics.unsupported_event_count == 0

    (workflow,) = analysis.workflow_runs
    assert workflow.run_id == result.run_id and workflow.workflow_id == "recorded"
    assert workflow.outcome == "completed"
    assert workflow.elapsed_ns.precision is Precision.EXACT
    assert workflow.elapsed_ns.value == run_finish.monotonic_ns - run_start.monotonic_ns > 0
    nodes = [op for op in workflow.operations if op.family == "workflow.node"]
    assert len(nodes) == 3 and all(op.depth == 1 and op.precision is Precision.EXACT for op in nodes)
    agent_op = next(op for op in workflow.operations if op.family == "model.run")
    assert agent_op.depth == 2 and agent_op.duration_ns > 0
    assert analysis.overview is not None and analysis.overview.elapsed_ns == workflow.elapsed_ns
    assert analysis.overview.compute_cp_ns.precision is Precision.MISSING
    assert analysis.overview.compute_cp_ns.value is None
    assert analysis.token_usage is None and analysis.change_verification is None

    # A crash leaves a valid prefix with an open run. Never present it as exact zero.
    incomplete = tmp_path / "incomplete.jsonl"
    lines = path.read_text().splitlines(keepends=True)
    import json

    incomplete.write_text("".join(line for line in lines if json.loads(line)["sequence"] < run_finish.sequence))
    interrupted = analyze_trajectory(incomplete)
    assert len(interrupted.workflow_runs) == 1
    assert interrupted.workflow_runs[0].elapsed_ns.value is None
    assert interrupted.overview is not None and interrupted.overview.elapsed_ns.precision is not Precision.EXACT

    samples = SessionCounterSamples({}, {})
    payload = analysis_json(analysis, samples)
    assert payload["workflow_runs"][0]["run_id"] == result.run_id
    trace = perfetto_trace(analysis, samples)
    spans = [event for event in trace["traceEvents"] if event["ph"] == "X"]
    assert sum(event["cat"] == "workflow.run" for event in spans) == 1
    assert sum(event["cat"] == "workflow.node" for event in spans) == 3
    assert all(event["ts"] >= 0 and event["dur"] >= 0 for event in spans)
