# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real, inert workflow artifacts for history browser tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from chrys.service.workflows.store import RunHeader, RunRecord, RunSpec, WorkflowRunStore


async def record_workflow_run(directory: Path, *, session_id: str, title: str, outcome: str) -> None:
    header = RunHeader(
        run_id=directory.name,
        session_id=session_id,
        workflow_id="archived",
        source_kind="workdir",
        canonical_path=str(directory.parent / "removed-workflow.py"),
        title=title,
        input_excerpt="workflow input only",
        entry_digest="e" * 64,
        manifest_digest="m" * 64,
        schema_version=1,
        spec_digest="s" * 64,
        started_at=datetime.now(UTC).isoformat(),
    )
    store = WorkflowRunStore.open(
        spec=RunSpec(
            manifest={
                "nodes": [{"id": "check", "kind": "python", "callable": {"name": "check"}}],
                "edges": [],
            },
            environment={},
            resolved_nodes=(),
        ),
        input_text="workflow input only",
        run_dir=directory,
        header=header,
        source=b"# Archived workflow source\n",
    )
    try:
        await store.append(
            RunRecord.NODE_STATE,
            {
                "node": "check",
                "activation": "check@iter#1",
                "attempt": 1,
                "state": "completed",
                "iteration": 0,
                "failure_phase": "",
            },
        )
        store.write_node_value("check@iter#1", 1, "input", {"value": {"text": "workflow input only", "data": None}})
        store.write_node_value(
            "check@iter#1", 1, "output", {"value": {"text": "Archived workflow output", "data": None}}
        )
        if outcome:
            await store.finish(outcome, {"outputs": [{"node": "check", "activation": "check@iter#1", "attempt": 1}]})
    finally:
        await store.close()


def workflow_state(cwd: Path, *, run_count: int = 0, latest_run_id: str = "") -> dict:
    """A valid workflow session envelope state for store-boundary tests."""
    from chrys.foundation.models.workflow_session import WorkflowIdentity, WorkspaceSnapshot
    from chrys.foundation.models.workspace import Workspace
    from chrys.service.state.workflow import WorkflowSessionState

    return WorkflowSessionState(
        WorkflowIdentity("archived", str(cwd / "archived.py"), "project"),
        WorkspaceSnapshot.capture(Workspace.from_cwd(str(cwd))),
        run_count=run_count,
        latest_run_id=latest_run_id,
    ).encode()
