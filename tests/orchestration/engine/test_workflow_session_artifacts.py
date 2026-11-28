# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow session checkpoints own artifact retention and discoverability."""

from __future__ import annotations

import json
from pathlib import Path

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.trajectory.ids import new_analytics_id
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.service.state._session_files import session_dir_candidates, session_dir_has_artifacts
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workflows.layout import HEADER_FILE, WORKFLOWS_DIR, run_dir
from chrys.service.workflows.store import RunHeader


def _header(session_id: str) -> RunHeader:
    return RunHeader(
        run_id=new_analytics_id(),
        session_id=session_id,
        workflow_id="wf",
        source_kind="workdir",
        canonical_path="/p/wf.py",
        title="t",
        input_excerpt="go",
        entry_digest="e" * 64,
        manifest_digest="m" * 64,
        schema_version=1,
        spec_digest="s" * 64,
    )


def _plant_run(session_dir: Path) -> Path:
    path = run_dir(session_dir, new_analytics_id())
    path.mkdir(parents=True)
    (path / HEADER_FILE).write_text(json.dumps(_header(session_dir.name).to_dict()), encoding="utf-8")
    return path


async def test_cleanup_empty_session_dir_removes_a_session_whose_workflows_dir_holds_no_run(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "aborted-only"
    session_dir = store.session_dir("aborted-only")
    (session_dir / WORKFLOWS_DIR / "aborted").mkdir(parents=True)

    await engine.lifecycle.cleanup_empty_session_dir()

    assert not session_dir.exists()


def test_run_files_without_a_session_checkpoint_are_not_restorable(tmp_path: Path) -> None:
    session_dir = tmp_path / "aborted"
    _plant_run(session_dir)
    assert session_dir_has_artifacts(session_dir) is False
    assert session_dir_candidates(tmp_path) == []


async def test_workflow_session_checkpoint_preserves_its_run_artifacts(tmp_path: Path) -> None:
    from chrys.service.state.workflow import WorkflowSessionState
    from tests.support.workflow_history import workflow_state

    store = JsonFileStateStore(tmp_path)
    await store.save_workflow_session("workflow", WorkflowSessionState.decode(workflow_state(tmp_path)))
    session_dir = store.session_dir("workflow")
    _plant_run(session_dir)
    engine = assemble_agent_engine(EventBus(), settings=Settings(), state_store=store)
    engine.session.session_id = "workflow"
    await engine.lifecycle.cleanup_empty_session_dir()
    assert session_dir.exists()
    assert session_dir_candidates(tmp_path) == [session_dir]
