# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared catalog: metadata, safe file operations, history and preview cancellation cleanup."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import psutil
import pytest

import chrys.service.workflows.layout as layout_module
import chrys.service.workflows.store as store_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import WorkflowPreviewProgress
from chrys.foundation.models.workflow_session import WorkflowDraft, WorkflowPins, WorkspaceSnapshot
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.orchestration.workflows.preview import PreparedWorkflow
from chrys.orchestration.workflows.worker_client import WorkflowWorkerClient
from chrys.service.workflows.artifacts import read_node_records, read_run_source, session_runs
from chrys.service.workflows.discovery import SOURCE_KIND_BUILTIN, WorkflowSource, global_workflows_dir
from chrys.service.workflows.ledger import ledger_path
from chrys.service.workflows.protocol import LIMITS
from chrys.service.workflows.store import NODE_RECORD_INPUT, NODE_RECORD_OUTPUT, RunSpec, WorkflowRunStore
from tests.orchestration.workflows._hosting import make_workdir, write_workflow
from tests.service.workflows.test_store import header
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow

SOURCE = python_workflow("def fn(text):\n    return text\n", "fn")


async def test_catalog_preview_title_confirmation_staleness_and_delete(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    path = write_workflow(project, "mine", SOURCE)
    bus = EventBus()
    stages: list[WorkflowPreviewProgress] = []

    async def progress(event: WorkflowPreviewProgress) -> None:
        stages.append(event)

    await bus.subscribe(WorkflowPreviewProgress, progress)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project, bus=bus)
    source = catalog.discover().find("mine")
    assert source is not None and catalog.title(source) is None
    assert catalog.ledger().recorded(source.canonical_path, source.source_kind) is None
    preview = await catalog.preview("mine", request_id="preview1", trust=True)
    assert [event.stage for event in stages] == ["definition", "environment", "graph", "ready"]
    assert all(event.request_id == "preview1" and event.workflow_id == "mine" for event in stages)
    assert stages[-1].title == preview.title and stages[-1].node_count == len(preview.manifest["nodes"])
    draft = WorkflowDraft("mine", WorkspaceSnapshot.capture(Workspace.from_cwd(str(project))))
    request = PreparedWorkflow(draft, preview).run_request(input_text="review", request_id="run1", timeout=12)
    assert request.target is draft
    assert request.pins == WorkflowPins(
        source.identity, preview.spec_digest, preview.environment.environment_fingerprint
    )
    assert (request.input_text, request.request_id, request.timeout, request.session_id) == ("review", "run1", 12, None)
    assert catalog.title(source) == "t"
    assert catalog.ledger().recorded(source.canonical_path, source.source_kind) is None
    catalog.confirm(preview)
    assert catalog.ledger().is_confirmed(preview.ledger_entry())
    assert catalog.ledger().recorded(source.canonical_path, source.source_kind).title == "t"
    assert catalog.is_current(preview)
    write_workflow(project, "mine", SOURCE + b"# changed\n")
    changed = catalog.discover().find("mine")
    assert changed is not None and changed.entry_sha256 != preview.load.entry_digest
    assert not catalog.is_current(preview)
    catalog.delete(str(path.resolve()))
    assert not path.exists() and catalog.ledger().recorded(source.canonical_path, source.source_kind) is None


async def test_builtin_preview_needs_no_confirmation_and_cannot_be_deleted(tmp_path: Path) -> None:
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=make_workdir(tmp_path))
    source = catalog.discover().find("demo-workflow")
    assert source is not None and source.source_kind == SOURCE_KIND_BUILTIN
    assert catalog.title(source) == "Workflow Demo · Project Tour"
    preview = await catalog.preview("demo-workflow")
    catalog.confirm(preview)
    assert not ledger_path(catalog.config_dir).exists()
    with pytest.raises(ValueError):
        catalog.delete(source.canonical_path)


@pytest.mark.parametrize("location", ["outside.py", "nested/inside.py", "_private.py", "notes.txt", "directory.py"])
def test_delete_refuses_non_discovery_files(tmp_path: Path, location: str) -> None:
    project = make_workdir(tmp_path)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    directory = global_workflows_dir(catalog.config_dir)
    path = (tmp_path if location == "outside.py" else directory) / location
    path.parent.mkdir(parents=True, exist_ok=True)
    if location == "directory.py":
        path.mkdir()
    else:
        path.write_text("untouched")
    with pytest.raises(ValueError):
        catalog.delete(str(path.resolve()))
    assert path.exists()


def test_delete_symlink_removes_only_link(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    target = tmp_path / "target.py"
    target.write_bytes(SOURCE)
    link = write_workflow(project, "linked", b"")
    link.unlink()
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    assert any(item.path == str(link) for item in catalog.discover().skipped)
    catalog.delete(str(link))
    assert not link.is_symlink()
    assert target.read_bytes() == SOURCE


async def test_history_reads_only_bounded_headers_then_node_records_on_demand(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    source = WorkflowSource("mine", "workdir", str(write_workflow(project, "mine", SOURCE)), SOURCE)
    session = tmp_path / "session"
    for index in range(3):
        run_header = replace(header(), canonical_path=source.canonical_path, started_at=f"2026-09-15T0{index}:00:00Z")
        store = WorkflowRunStore.open(
            spec=RunSpec(manifest={}, environment={}, resolved_nodes=()),
            input_text="go",
            run_dir=session / "workflows" / run_header.run_id,
            header=run_header,
            source=SOURCE,
        )
        try:
            store.write_node_value("fn@iter#1", 1, NODE_RECORD_INPUT, {"value": {"text": "in"}})
            store.write_node_value("fn@iter#1", 1, NODE_RECORD_OUTPUT, {"value": {"text": str(index)}})
            store.append_node_emit("fn@iter#1", 1, 1, "emitted")
            store.write_node_diagnostics("fn@iter#1", 1, phase="body", iteration=0, stdout="printed")
            await store.finish("completed", {})
        finally:
            await store.close()
    bad = session / "workflows" / "bad"
    bad.mkdir()
    atomic_write_owner_only_bytes(bad / "run.json", b"not JSON")
    records = session_runs(session)[:2]
    assert [record.header["started_at"] for record in records] == ["2026-09-15T02:00:00Z", "2026-09-15T01:00:00Z"]
    assert read_run_source(records[0].directory) == SOURCE
    node = read_node_records(records[0].directory, "fn@iter#1", 1)
    assert node.input == {"value": {"text": "in"}}
    assert node.output == {"value": {"text": "2"}}
    assert node.emits == [(1, "emitted")]
    assert node.diagnostics == {
        "phases": [
            {"phase": "body", "iteration": 0, "stdout": {"text": "printed", "truncated": False}, "traceback": ""}
        ]
    }
    assert session_runs(tmp_path / "other-session") == []
    # Header listing never opens the node artifacts or source bytes.
    (records[0].directory / "source.py").unlink()
    (records[0].directory / "nodes").rename(records[0].directory / "saved-nodes")
    assert len(session_runs(session)) == 3


def test_history_bounds_enumeration_and_header_size(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for index in range(4):
        directory = tmp_path / "session" / "workflows" / str(index)
        directory.mkdir(parents=True)
        atomic_write_owner_only_bytes(directory / "run.json", b'{"canonical_path":"/mine.py","source_kind":"workdir"}')
    monkeypatch.setattr(layout_module, "MAX_SCANNED_RUN_DIRS", 2)
    assert len(session_runs(tmp_path / "session")) == 2
    monkeypatch.setattr(store_module, "MAX_HEADER_BYTES", 8)
    assert session_runs(tmp_path / "session") == []


def test_delete_shadowed_global_file_keeps_active_source_and_history(tmp_path: Path) -> None:
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=make_workdir(tmp_path))
    global_file = global_workflows_dir(catalog.config_dir) / "mine.py"
    global_file.parent.mkdir(parents=True)
    atomic_write_owner_only_bytes(global_file, SOURCE)
    project_file = write_workflow(catalog.workdir, "mine", SOURCE)
    history = tmp_path / "session" / "workflows" / "run" / "source.py"
    history.parent.mkdir(parents=True)
    history.write_bytes(SOURCE)
    shadowed = catalog.discover().shadowed
    assert len(shadowed) == 1 and shadowed[0].source.canonical_path == str(global_file.resolve())
    catalog.delete(shadowed[0].source.canonical_path)
    assert not global_file.exists()
    assert project_file.read_bytes() == history.read_bytes() == SOURCE


@pytest.mark.parametrize("phase", ["load", "close"])
@pytest.mark.parametrize("expire", [False, True])
async def test_preview_waits_for_cleanup_on_deadline_or_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str, expire: bool
) -> None:
    project = make_workdir(tmp_path)
    marker = tmp_path / "worker-pid"
    source = f"import os, threading\nfrom pathlib import Path\nPath({str(marker)!r}).write_text(str(os.getpid()))\n"
    if phase == "load":
        source += "threading.Event().wait()\n"
    write_workflow(project, "preview", source.encode() + SOURCE)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    real_timeout = asyncio.timeout
    deadlines: list[asyncio.Timeout] = []

    def timeout(delay: float | None) -> asyncio.Timeout:
        deadline = real_timeout(delay)
        if delay == 3600:
            deadlines.append(deadline)
        return deadline

    monkeypatch.setattr(asyncio, "timeout", create_autospec(real_timeout, side_effect=timeout))
    real_close = WorkflowWorkerClient.close
    closing = asyncio.Event()
    close_reentered = asyncio.Event()
    release = asyncio.Event()

    async def close(self: WorkflowWorkerClient, *, grace: float = LIMITS.shutdown_grace) -> None:
        if closing.is_set():
            close_reentered.set()
        closing.set()
        await release.wait()
        await real_close(self, grace=grace)

    if phase == "close":
        monkeypatch.setattr(WorkflowWorkerClient, "close", create_autospec(real_close, side_effect=close))
    caller = asyncio.create_task(catalog.preview("preview", timeout=3600, trust=True))
    try:
        await wait_for(
            lambda: (marker.exists() and marker.stat().st_size > 0) or caller.done(),
            description="preview worker loading",
            timeout=ENGINE_TURN_TIMEOUT,
        )
        if caller.done():
            await caller
        worker = psutil.Process(int(marker.read_text()))
        if phase == "close":
            await wait_for(closing.is_set, description="preview worker close")
        if expire:
            deadlines[0].reschedule(asyncio.get_running_loop().time())
        else:
            caller.cancel()
        if phase == "close":
            await wait_for(close_reentered.is_set, description="cancelled preview drains its close")
            assert not caller.done()
            release.set()
        with pytest.raises(TimeoutError if expire else asyncio.CancelledError):
            await caller
        assert not worker.is_running()
        assert not ledger_path(catalog.config_dir).exists()
    finally:
        release.set()
        if not caller.done():
            caller.cancel()
        await asyncio.gather(caller, return_exceptions=True)
