# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow-only startup owns and drains durable hook recovery across runs."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.orchestration.workflows.hooks import WorkflowSessionHooks
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.outbox import Outbox
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HookSettings, HooksFile
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, run, write_workflow
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow


async def test_workflow_only_recovers_pending_hook_once_across_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    hooks_dir = tmp_path / "hooks"
    output = tmp_path / "recovered.json"
    hook = HookConfig(
        id="recover-me",
        event=HookEvent.WORKFLOW_RUN_END,
        run=HookRun(
            type="command",
            argv=[
                sys.executable,
                "-c",
                f"import sys; from pathlib import Path; Path({str(output)!r}).write_text(sys.stdin.read())",
            ],
        ),
        execution=HookExecution(mode="async", delivery="durable"),
    )
    config = HooksFile(hooks=[hook], settings=HookSettings(outbox_retry_age_seconds=0))
    outbox = Outbox(hooks_dir / "outbox")
    job = outbox.write_pending(
        hook_id=hook.id, event=str(hook.event), payload={"session_id": "crashed", "recovery": True}
    )
    calls = []
    real_recover = HookManager.recover_outbox

    async def build(factory, *, workdir, workdir_hooks_enabled, session_id, request_id=""):
        return HookManager(file=config, hooks_dir=hooks_dir)

    async def recover(manager):
        calls.append(manager)
        return await real_recover(manager)

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    monkeypatch.setattr(HookManager, "recover_outbox", create_autospec(real_recover, side_effect=recover))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        await run(host, "review")
        done_path = hooks_dir / "outbox" / "done" / f"{job.job_id}.json"
        await wait_for(
            done_path.exists, description="crash-leftover durable hook completed", timeout=ENGINE_TURN_TIMEOUT
        )
        completed = json.loads(done_path.read_text())
        assert completed["payload"] == job.payload and completed["retries"] == 1
        assert not (hooks_dir / "outbox" / "pending" / f"{job.job_id}.json").exists()
        await run(host, "review")
        assert len(calls) == 1
        assert host.session_id is None
    finally:
        await host.shutdown()


async def test_shutdown_cancels_and_observes_owned_recovery_before_manager_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    entered, parked = asyncio.Event(), asyncio.Event()
    order = []
    real_drain = HookManager.drain_session

    async def build(factory, *, workdir, workdir_hooks_enabled, session_id, request_id=""):
        return HookManager(file=HooksFile(), hooks_dir=tmp_path / "hooks")

    async def recover(manager):
        entered.set()
        try:
            await parked.wait()
        finally:
            order.append("recovery stopped")
        return 0

    async def drain(manager, *args, **kwargs):
        if kwargs.get("close", True):
            order.append("manager closed")
        return await real_drain(manager, *args, **kwargs)

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    monkeypatch.setattr(HookManager, "recover_outbox", create_autospec(HookManager.recover_outbox, side_effect=recover))
    monkeypatch.setattr(HookManager, "drain_session", create_autospec(real_drain, side_effect=drain))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        await run(host, "review")
        await wait_for(entered.is_set, description="outbox recovery started")
    finally:
        await host.shutdown()
    assert order == ["recovery stopped", "manager closed"]


async def test_cancel_during_reattachment_drains_old_manager_and_keeps_new_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    entered, stopped, attaching = (asyncio.Event() for _ in range(3))
    managers = []
    real_attach = WorkflowSessionHooks.attach
    attachments = 0

    async def build(factory, *, workdir, workdir_hooks_enabled, session_id, request_id=""):
        manager = HookManager(file=HooksFile(), hooks_dir=tmp_path / "hooks")
        managers.append(manager)
        return manager

    async def recover(manager):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()
        return 0

    async def attach(hooks, resources):
        nonlocal attachments
        attachments += 1
        if attachments == 2:
            attaching.set()
        await real_attach(hooks, resources)

    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    monkeypatch.setattr(HookManager, "recover_outbox", create_autospec(HookManager.recover_outbox, side_effect=recover))
    monkeypatch.setattr(WorkflowSessionHooks, "attach", create_autospec(real_attach, side_effect=attach))
    host = make_host(tmp_path, project=project)
    task = None
    try:
        await confirm(host, "review")
        await run(host, "review")
        await wait_for(entered.is_set, description="first attachment owns pending recovery")
        task = asyncio.create_task(run(host, "review"))
        await wait_for(
            lambda: attaching.is_set() or task.done(),
            timeout=ENGINE_TURN_TIMEOUT,
            description="next attachment is waiting for old recovery",
        )
        if task.done():
            await task
        assert attaching.is_set()
        await host.cancel_workflow()
        result, _events = await asyncio.wait_for(asyncio.shield(task), timeout=ENGINE_TURN_TIMEOUT)
        assert result.outcome.value == "cancelled"
        assert stopped.is_set()
        assert len(managers) == 2 and managers[0]._closed and not managers[1]._closed
    finally:
        await host.shutdown()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
    assert all(manager._closed for manager in managers)
