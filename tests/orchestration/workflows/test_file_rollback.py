# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow file rollback uses Run IDs and preserves execution history."""

from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import pytest

from chrys.foundation.events.types import WorkflowRollbackRequest, WorkflowRollbackResult
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationProvenance, MutationSource, SnapshotSkipReason
from chrys.service.state.store import JsonFileStateStore
from chrys.service.state.workflow import WorkflowSessionState
from chrys.service.workflows.artifacts import session_runs
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.event_capture import capture_event_sequence
from tests.support.workflow_history import workflow_state


@pytest.mark.parametrize("checkpoint_fails", [False, True])
async def test_confirmed_rollback_restores_run_boundary_and_rejects_stale_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, checkpoint_fails: bool
) -> None:
    project = make_project(tmp_path)
    target = project / "target.txt"
    target.write_text("before")
    clients = [MockChatClient(responses=[])]
    for content in ("one", "two", "three"):
        clients.append(
            MockChatClient(
                responses=[
                    MockResponse(
                        tool_calls=[
                            ("write_file", "write", {"path": str(target), "content": content, "overwrite": True})
                        ]
                    ),
                    MockResponse(text="done"),
                ]
            )
        )
    patch_runtime(monkeypatch, clients, builtin_tools=True)
    write_workflow(
        project,
        "write",
        b"""
from chrys.workflows import WorkflowBuilder
wf = WorkflowBuilder('write')
node = wf.agent('writer', profile='Headless')
wf.start(node)
wf.output(node)
workflow = wf.build()
""",
    )
    host = make_host(tmp_path, project=project, profiles=[make_profile(builtins=["filesystem.write"])])
    results: list[WorkflowRollbackResult] = []

    async def record_result(event: WorkflowRollbackResult) -> None:
        results.append(event)

    await host.event_bus.subscribe(WorkflowRollbackResult, record_result)
    try:
        await confirm(host, "write")
        first, _ = await run(host, "write")
        second, _ = await run(host, "write")
        session_id = host.workflow_session_id
        assert target.read_text() == "two"

        async def request(run_id: str, token: str = "") -> WorkflowRollbackResult:
            await host.event_bus.publish(
                WorkflowRollbackRequest(session_id=session_id, run_id=run_id, request_id=str(len(results)), token=token)
            )
            return results[-1]

        assert host.workflow_session_dir is not None
        checkpoint = host.workflow_session_dir / "session.json"
        saved = checkpoint.read_bytes()
        preview = await request(second.run_id)
        assert not preview.error and preview.paths == (str(target),)
        assert checkpoint.read_bytes() == saved
        assert target.read_text() == "two"
        target.write_text("edited after preview")
        stale_files = await request(second.run_id, preview.token)
        assert stale_files.error and not stale_files.applied
        assert target.read_text() == "edited after preview"
        preview = await request(second.run_id)
        blobs = set((host.workflow_session_dir / "mutations").iterdir())
        applied = await request(second.run_id, preview.token)
        assert applied.applied and not applied.error and applied.changed == 1
        assert target.read_text() == "one"
        assert set((host.workflow_session_dir / "mutations").iterdir()) == blobs
        # Duplicate delivery must not repeat a destructive file operation.
        target.write_text("manual edit")
        await host.event_bus.publish(
            WorkflowRollbackRequest(
                session_id=session_id, run_id=second.run_id, request_id=applied.request_id, token=preview.token
            )
        )
        assert target.read_text() == "manual edit"
        missing = await request("missing-run")
        assert missing.error and not missing.applied
        history = session_runs(host.workflow_session_dir)
        assert len(history) == 2 and all(
            record.terminal is not None and record.terminal.outcome == "completed" for record in history
        )
        preview = await request(first.run_id)
        await run(host, "write")
        stale = await request(first.run_id, preview.token)
        assert stale.error and not stale.applied and target.read_text() == "three"
        preview = await request(first.run_id)
        if checkpoint_fails:
            monkeypatch.setattr(
                JsonFileStateStore,
                "save_workflow_session",
                create_autospec(JsonFileStateStore.save_workflow_session, side_effect=OSError("disk full")),
            )
        applied = await request(first.run_id, preview.token)
        assert applied.applied and target.read_text() == "before"
        assert ("checkpoint failed" in applied.error) is checkpoint_fails
        assert len(session_runs(host.workflow_session_dir)) == 3
    finally:
        await host.shutdown()


@pytest.mark.parametrize("reclassify", [False, True])
async def test_preview_saves_only_changed_attribution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reclassify: bool
) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    session_id = str(uuid4())
    tracker = MutationTracker(SnapshotStore(store.session_dir(session_id)))
    target = tmp_path / "target"
    target.write_text("before")
    tracker.start_workflow_run("run")
    mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.WRITE_FILE, "write")
    assert mutation is not None
    target.write_text("after")
    tracker.record_after(mutation)
    state = WorkflowSessionState.decode(workflow_state(tmp_path, run_count=1, latest_run_id="run"))
    state.mutations = tracker.serialize()
    await store.save_workflow_session(session_id, state)
    checkpoint = store.session_dir(session_id) / "session.json"
    before = checkpoint.read_bytes()

    def classify(coordinator, restored, *, force=False, fallback_root=None):
        if not reclassify:
            return False
        restored.log.periods[0].mutations[0].provenance = MutationProvenance.FOREIGN
        return True

    monkeypatch.setattr(
        MutationCoordinator, "reclassify", create_autospec(MutationCoordinator.reclassify, side_effect=classify)
    )
    host = make_host(tmp_path, project=tmp_path)
    try:
        async with capture_event_sequence(host.event_bus, WorkflowRollbackResult) as replies:
            await host.engine.workflows.on_rollback(
                WorkflowRollbackRequest(session_id=session_id, run_id="run", request_id="preview")
            )
        assert len(replies) == 1 and not replies[0].error
        assert (checkpoint.read_bytes() != before) is reclassify
        assert bool(replies[0].exclusions) is reclassify
        saved = await store.load_workflow_session(session_id)
        assert saved is not None and saved.mutations is not None
        restored = MutationTracker.deserialize(saved.mutations, tracker.store)
        assert (restored.log.periods[0].mutations[0].provenance is MutationProvenance.FOREIGN) is reclassify
    finally:
        await host.shutdown()


async def test_rollback_probe_uses_effective_snapshot_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from chrys.foundation import platform
    from chrys.foundation.config.process_env import freeze_process_env
    from chrys.foundation.config.settings_store import load_settings

    config = tmp_path / "config"
    config.mkdir()
    (config / "settings.yaml").write_text("mutations:\n  snapshot:\n    max_file_mb: 1\n")
    fake = replace(platform.get_platform(), config_dir=config)
    monkeypatch.setattr(platform, "get_platform", lambda: fake)
    project = make_project(tmp_path)
    chat = tmp_path / "chat"
    chat.mkdir()
    freeze_process_env()
    store = JsonFileStateStore(tmp_path / "sessions")
    session_id = str(uuid4())
    tracker = MutationTracker(SnapshotStore(store.session_dir(session_id)))
    target = project / "target"
    target.write_text("before")
    tracker.start_workflow_run("run")
    mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.WRITE_FILE, "write")
    assert mutation is not None
    target.write_bytes(b"x" * (2 * 1024 * 1024))
    tracker.record_after(mutation)
    state = WorkflowSessionState.decode(workflow_state(project, run_count=1, latest_run_id="run"))
    state.mutations = tracker.serialize()
    await store.save_workflow_session(session_id, state)
    observed = []
    probe = SnapshotStore.probe

    def capture(snapshots: SnapshotStore, path: str):
        result = probe(snapshots, path)
        observed.append((snapshots.policy.max_file_bytes, result.skip_reason))
        return result

    monkeypatch.setattr(SnapshotStore, "probe", create_autospec(probe, side_effect=capture))
    host = make_host(tmp_path, project=chat, loaded_settings=load_settings(project_root=chat))
    try:
        async with capture_event_sequence(host.event_bus, WorkflowRollbackResult) as replies:
            await host.engine.workflows.on_rollback(
                WorkflowRollbackRequest(session_id=session_id, run_id="run", request_id="preview")
            )
            assert len(replies) == 1 and not replies[0].error
            await host.engine.workflows.on_rollback(
                WorkflowRollbackRequest(
                    session_id=session_id, run_id="run", request_id="commit", token=replies[0].token
                )
            )
        assert len(replies) == 2 and replies[1].applied and not replies[1].error
        assert observed == [(1024 * 1024, SnapshotSkipReason.TOO_LARGE)]
        assert target.read_text() == "before"
    finally:
        await host.shutdown()
