# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Admission identity, diagnostics and hook ownership at real session boundaries."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.workflows.coordinator as coordinator_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.types import Warning
from chrys.foundation.platform import get_platform
from chrys.foundation.platform.files import atomic_write_owner_only_bytes
from chrys.orchestration.session_hooks import SessionHookFactory
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.orchestration.workflows.preview import WorkflowPreviewError
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.schema import HooksFile
from chrys.service.llm.mock import MockChatClient
from chrys.service.workflows.discovery import global_workflows_dir
from tests.orchestration.workflows._hosting import confirm, make_host, make_project, patch_runtime, run, write_workflow
from tests.support.workflow_workers import python_workflow

SOURCE = python_workflow("def check(text):\n    return text\n", "check")


async def test_shadowed_source_is_rejected_before_environment_or_module_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    global_dir = global_workflows_dir(get_platform().config_dir)
    global_dir.mkdir(parents=True, exist_ok=True)
    # Elevated Windows runners otherwise create a file owned by Administrators,
    # which workflow discovery correctly rejects as foreign-owned.
    atomic_write_owner_only_bytes(global_dir / "review.py", SOURCE)
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        await run(host, "review")
        marker = project / "loaded"
        shadow = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode() + SOURCE
        write_workflow(project, "review", shadow)
        prepared = await host.preview_workflow(host.workflow_target("review", new_session=True), trust=True)
        host.confirm_workflow(prepared)
        marker.unlink()
        with pytest.raises(WorkflowPreviewError, match="another workflow source"):
            await host.preview_workflow(host.workflow_target("review"))
        assert not marker.exists()
        real_prepare = coordinator_module.prepare_workflow_environment
        forbidden = create_autospec(
            real_prepare, side_effect=AssertionError("must reject before preparing environment")
        )
        monkeypatch.setattr(coordinator_module, "prepare_workflow_environment", forbidden)
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await run(host, "review")
        assert rejected.value.event.error == "spec_changed"
        forbidden.assert_not_called()
        assert not marker.exists()
    finally:
        await host.shutdown()


async def test_session_hooks_outlive_runs_and_history_browsing_is_passive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "review", SOURCE)
    calls = []
    original_fire = HookManager.fire

    async def fire(manager, event, payload, **kwargs):
        calls.append((event, dict(payload)))
        return await original_fire(manager, event, payload, **kwargs)

    async def build(factory, *, project_root, project_hooks_enabled, session_id, request_id=""):
        return HookManager(file=HooksFile(), hooks_dir=tmp_path / "hooks")

    monkeypatch.setattr(HookManager, "fire", create_autospec(original_fire, side_effect=fire))
    monkeypatch.setattr(SessionHookFactory, "__call__", create_autospec(SessionHookFactory.__call__, side_effect=build))
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "review")
        await run(host, "review")
        session_id = host.workflow_session_id
        await host.load_workflow_session(session_id)
        await run(host, "review")
        assert [event for event, _ in calls] == [
            HookEvent.SESSION_START,
            HookEvent.WORKFLOW_RUN_START,
            HookEvent.WORKFLOW_RUN_END,
            HookEvent.WORKFLOW_RUN_START,
            HookEvent.WORKFLOW_RUN_END,
        ]
    finally:
        await host.shutdown()
    assert calls[-1][0] is HookEvent.SESSION_END
    restored = make_host(tmp_path, project=project)
    before = len(calls)
    try:
        await restored.load_workflow_session(session_id)
        assert len(calls) == before
        await run(restored, "review")
        # ``session_restored`` alone says the session came back; a resume fires no
        # ``session_start``, so an existing hook sees the same events it always did.
        assert calls[before][0] is HookEvent.SESSION_RESTORED
        assert calls[before][1]["restored_session_id"] == session_id
        assert calls[before + 1][0] is HookEvent.WORKFLOW_RUN_START
    finally:
        await restored.shutdown()
    assert calls[-1][0] is HookEvent.SESSION_END
    assert all(payload["session_id"] == session_id for _, payload in calls)


async def test_admission_warning_is_delivered_before_acceptance_with_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_project(tmp_path)
    write_workflow(project, "review", SOURCE)
    hooks = project / ".chrys" / "hooks"
    hooks.mkdir()
    (hooks / "hooks.yaml").write_text("hooks: [invalid")
    host = make_host(
        tmp_path, project=project, settings=Settings(model_profile="mock-profile", project_hooks_enabled=True)
    )
    try:
        await confirm(host, "review")
        result, events = await run(host, "review")
        assert result.outcome.value == "completed"
        warning = next(event for event in events if isinstance(event, Warning))
        assert warning.code == "project_hooks_config_invalid" and warning.request_id
        assert warning.session_id == host.workflow_session_id
    finally:
        await host.shutdown()
