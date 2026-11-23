# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Historical projections retain journal evidence without inventing live execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from chrys.app.tui.widgets.workflow.records import read_observed_run
from chrys.foundation.events import types as events
from chrys.service.workflows.artifacts import WorkflowRunRecord
from chrys.service.workflows.store import (
    RunHeader,
    RunRecord,
    RunSpec,
    WorkflowRunStore,
    read_run_events,
    read_run_header,
)


@pytest.mark.parametrize("finished", [False, True])
async def test_replay_preserves_timestamps_emits_and_terminal_reconciliation(tmp_path: Path, finished: bool) -> None:
    started = datetime(2026, 1, 1, tzinfo=UTC)
    directory = tmp_path / uuid4().hex
    header = RunHeader(
        run_id=directory.name,
        session_id=str(uuid4()),
        workflow_id="example",
        source_kind="workdir",
        canonical_path=str(tmp_path / "workflow.py"),
        title="Example",
        input_excerpt="",
        entry_digest="e" * 64,
        manifest_digest="m" * 64,
        schema_version=1,
        spec_digest="s" * 64,
        started_at=started.isoformat(),
    )
    store = WorkflowRunStore.open(
        spec=RunSpec(manifest={"nodes": [], "edges": []}, environment={}, resolved_nodes=()),
        input_text="",
        run_dir=directory,
        header=header,
        source=b"# Archived\n",
    )
    try:
        await store.append(
            RunRecord.NODE_STATE,
            {
                "node": "check",
                "activation": "check@iter#1",
                "attempt": 1,
                "state": "awaiting_retry",
                "iteration": 3,
                "failure_phase": "until",
                "error": "failure",
                "error_class": "RuntimeError",
            },
        )
        await store.append(
            RunRecord.NODE_OUTPUT,
            {
                "node": "check",
                "activation": "check@iter#1",
                "attempt": 1,
                "kind": "emit",
                "ordinal": 1,
                "summary": "Still available after restore",
            },
        )
        for request in ("answered", "cancelled", "pending"):
            ref = {"node": request, "activation": request, "attempt": 2}
            await store.append(RunRecord.NODE_ASK, {**ref, "request": request, "prompt": "Continue?"})
            if request == "answered":
                await store.append(RunRecord.NODE_ANSWER, {**ref, "request": request, "answer": "Yes"})
            elif request == "cancelled":
                await store.append(
                    RunRecord.NODE_STATE,
                    {**ref, "state": "cancelled", "iteration": 0, "failure_phase": ""},
                )
        if finished:
            await store.finish("cancelled", {})
    finally:
        await store.close()
    persisted = read_run_events(directory).events
    restored_header = read_run_header(directory)
    if finished:
        # Untrusted header fields cannot override the log terminal.
        restored_header.update(outcome="orphaned", reason="owner_lost")
    run = read_observed_run(WorkflowRunRecord(directory, restored_header))
    assert run.started.timestamp == started
    node_fact = next(fact for fact in run.facts if isinstance(fact, events.WorkflowNodeStateChanged))
    assert (node_fact.iteration, node_fact.failure_phase) == (3, "until")
    assert node_fact.timestamp == datetime.fromisoformat(persisted[0].occurred_at)
    assert node_fact.seq == persisted[0].sequence and node_fact.error_class == "RuntimeError"
    emit = next(fact for fact in run.facts if isinstance(fact, events.WorkflowNodeOutput))
    assert emit.summary_text == "Still available after restore"
    assert emit.timestamp == datetime.fromisoformat(persisted[1].occurred_at)
    assert emit.seq == persisted[1].sequence
    assert run.finished is not None and run.finished.outcome == ("cancelled" if finished else "orphaned")
    assert run.facts[-1] == run.finished
    assert run.nodes["check"].state == ("awaiting_retry" if finished else "cancelled")
    assert not run.journals
    assert not run.questions
    assert run.question_states == {
        "answered": "answered",
        "cancelled": "cancelled",
        "pending": "cancelled" if finished else "abandoned",
    }
    assert run.answers["answered"].answer == "Yes"
    assert set(run.question_history) == {"answered", "cancelled", "pending"}


def test_recent_facts_are_bounded_while_revisions_keep_advancing() -> None:
    from chrys.app.tui.widgets.workflow.projector import RECENT_FACT_CAPACITY, WorkflowProjector

    projector = WorkflowProjector()
    projector.record(events.WorkflowRunStarted(run_id="run"))
    for number in range(RECENT_FACT_CAPACITY + 5):
        projector.record(
            events.WorkflowNodeStateChanged(
                run_id="run",
                node_id=f"node-{number}",
                activation_id=f"activation-{number}",
                state="completed",
            )
        )
    run = projector.current
    assert run is not None
    assert len(run.facts) == RECENT_FACT_CAPACITY
    assert run.fact_count == RECENT_FACT_CAPACITY + 6
    assert len(run.attempts) == RECENT_FACT_CAPACITY + 5
    assert isinstance(run.facts[0], events.WorkflowNodeStateChanged) and run.facts[0].node_id == "node-5"
    before = run.fact_count
    projector.record(events.WorkflowRunFinished(run_id="run", outcome="completed"))
    assert run.fact_count == before + 1 and len(run.facts) == RECENT_FACT_CAPACITY
    assert run.facts[-1] is run.finished
