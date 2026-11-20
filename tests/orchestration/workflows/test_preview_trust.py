# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preview authorization precedes interpreter probes and arbitrary module execution."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import create_autospec

import pytest

from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.orchestration.workflows.preview import WorkflowInspection, WorkflowPreviewError, WorkflowTrustDeclined
from chrys.service.workflows.environment import WorkflowEnvironmentManager
from tests.orchestration.workflows._hosting import make_workdir, write_workflow
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow


@pytest.mark.parametrize("custom_interpreter", [False, True])
async def test_untrusted_preview_never_starts_interpreter_or_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom_interpreter: bool
) -> None:
    project = make_workdir(tmp_path)
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    source += python_workflow("def fn(value):\n    return value\n", "fn")
    if custom_interpreter:
        interpreter = project / "custom-python"
        interpreter.write_text("This executable must not be started before trust.")
        source = b'# /// script\n# [tool.chrys]\n# python = "../../custom-python"\n# ///\n' + source
    write_workflow(project, "untrusted", source)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    prepare = create_autospec(WorkflowEnvironmentManager.prepare, side_effect=AssertionError("ran before trust"))
    monkeypatch.setattr(WorkflowEnvironmentManager, "prepare", prepare)
    with pytest.raises(WorkflowPreviewError, match="Trust the workflow"):
        await catalog.preview("untrusted")

    async def decline(inspection: WorkflowInspection) -> bool:
        assert inspection.source.source == source
        assert not marker.exists()
        if custom_interpreter:
            assert Path(inspection.environment.interpreter).resolve() == interpreter
        return False

    with pytest.raises(WorkflowTrustDeclined):
        await catalog.preview("untrusted", authorize=decline)
    prepare.assert_not_called()
    assert not marker.exists()
    assert catalog.ledger().recorded(str(project / ".chrys/workflows/untrusted.py"), "workdir") is None


@pytest.mark.parametrize("decision", [None, False, True])
async def test_changed_environment_requires_authorization_before_module_load(
    tmp_path: Path, decision: bool | None
) -> None:
    project = make_workdir(tmp_path)
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    source += python_workflow("def fn(value):\n    return value\n", "fn")
    write_workflow(project, "mine", source)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    preview = await catalog.preview("mine", trust=True)
    catalog.ledger().confirm(replace(preview.ledger_entry(), environment_fingerprint="0" * 64))
    marker.unlink()
    inspections = []

    async def authorize(inspection: WorkflowInspection) -> bool:
        inspections.append(inspection)
        assert not marker.exists()
        assert inspection.source == preview.source
        assert inspection.prepared_environment == preview.environment
        return bool(decision)

    if decision is None:
        with pytest.raises(WorkflowPreviewError, match="Trust the workflow"):
            await catalog.preview("mine")
    elif not decision:
        with pytest.raises(WorkflowTrustDeclined):
            await catalog.preview("mine", authorize=authorize)
    else:
        loaded = await catalog.preview("mine", authorize=authorize)
        assert loaded.source == preview.source
    assert len(inspections) == (0 if decision is None else 1)
    assert marker.exists() == (decision is True)


@pytest.mark.parametrize("stage", ["discovery", "ledger", "inspection", "rediscovery"])
async def test_preview_timeout_releases_waiter_during_filesystem_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "mine", python_workflow("def fn(value):\n    return value\n", "fn"))
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    entered, release, finished = Event(), Event(), Event()
    if stage == "inspection":
        target, name, original = WorkflowInspection, "read", WorkflowInspection.read
    elif stage == "ledger":
        target, name, original = catalog, "ledger", catalog.ledger
    else:
        target, name, original = catalog, "discover", catalog.discover
    calls = 0

    def blocked(*args):
        nonlocal calls
        calls += 1
        if calls == (2 if stage == "rediscovery" else 1):
            entered.set()
            try:
                assert release.wait(10), "test did not release the filesystem operation"
                return original(*args)
            finally:
                finished.set()
        return original(*args)

    monkeypatch.setattr(target, name, blocked)

    async def authorize(_inspection: WorkflowInspection) -> bool:
        return True

    task = asyncio.create_task(catalog.preview("mine", timeout=0.5, authorize=authorize))
    try:
        await wait_for(entered.is_set)
        await wait_for(task.done)
        with pytest.raises(TimeoutError):
            await task
        assert not finished.is_set()  # Cancellation frees the caller, not the blocked filesystem thread.
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await wait_for(finished.is_set)


async def test_human_authorization_does_not_consume_preview_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from chrys.orchestration.workflows import catalog as catalog_module

    project = make_workdir(tmp_path)
    write_workflow(project, "mine", python_workflow("def fn(value):\n    return value\n", "fn"))
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)
    preview = await catalog.preview("mine", trust=True)
    monkeypatch.setattr(
        catalog_module, "preview_workflow", create_autospec(catalog_module.preview_workflow, return_value=preview)
    )

    async def authorize(_inspection: WorkflowInspection) -> bool:
        answer = asyncio.Event()
        timer = asyncio.get_running_loop().call_later(0.7, answer.set)
        try:
            await answer.wait()
        finally:
            timer.cancel()
        return True

    assert await catalog.preview("mine", timeout=0.5, authorize=authorize) == preview


@pytest.mark.parametrize("change", ["edit", "shadow"])
async def test_source_change_during_trust_cannot_authorize_replacement(tmp_path: Path, change: str) -> None:
    project = make_workdir(tmp_path)
    config = tmp_path / "config"
    global_dir = config / "workflows"
    global_dir.mkdir(parents=True)
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    source += python_workflow("def fn(value):\n    return value\n", "fn")
    original = global_dir / "mine.py"
    atomic_write_owner_only_bytes(original, source)
    catalog = WorkflowCatalog(config_dir=config, workdir=project)

    async def approve(inspection: WorkflowInspection) -> bool:
        assert inspection.source.source == source
        if change == "edit":
            atomic_write_owner_only_bytes(original, source + b"# changed\n")
        else:
            write_workflow(project, "mine", source)
        return True

    with pytest.raises(WorkflowPreviewError, match="changed during confirmation"):
        await catalog.preview("mine", authorize=approve)
    assert not marker.exists()


async def test_explicit_trust_then_persisted_trust_execute_same_source(tmp_path: Path) -> None:
    project = make_workdir(tmp_path)
    marker = tmp_path / "executed"
    source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
    source += python_workflow("def fn(value):\n    return value\n", "fn")
    write_workflow(project, "mine", source)
    catalog = WorkflowCatalog(config_dir=tmp_path / "config", workdir=project)

    async def approve(inspection: WorkflowInspection) -> bool:
        assert inspection.source.source == source and not marker.exists()
        return True

    preview = await catalog.preview("mine", authorize=approve)
    assert marker.exists()
    catalog.confirm(preview)
    marker.unlink()
    await catalog.preview("mine")
    assert marker.exists()
    marker.unlink()
    write_workflow(project, "mine", source + b"# edited after trust\n")
    with pytest.raises(WorkflowPreviewError, match="Trust the workflow"):
        await catalog.preview("mine")
    assert not marker.exists()
