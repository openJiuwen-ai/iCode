# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow workspace ownership survives Chat changes, persistence and direct API requests."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import WorkflowRunAccepted, WorkflowRunRejected, WorkflowRunRequest, WorkspaceChange
from chrys.foundation.models.workflow_session import WorkflowIdentity, WorkflowSessionSelection, WorkspaceSnapshot
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_workdir,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.event_capture import capture_event_sequence
from tests.support.workflow_workers import python_workflow


async def test_created_session_keeps_its_project_after_chat_changes_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    write_workflow(
        project,
        "here",
        python_workflow("from pathlib import Path\ndef here(text):\n    return str(Path.cwd())\n", "here"),
    )
    host = make_host(tmp_path, project=project)
    try:
        await confirm(host, "here")
        first, _ = await run(host, "here")
        session_id = host.workflow_session_id
        await host.start()
        await host.event_bus.publish(WorkspaceChange(primary_cwd=str(elsewhere)), raise_handler_errors=True)
        assert host.engine.workspace is not None and host.engine.workspace.primary_cwd == str(elsewhere)
        assert host.workflow_catalog.workdir == project
        second, _ = await run(host, "here")
        assert host.workflow_session_id == session_id
        assert first.outputs[0].value.text == second.outputs[0].value.text == str(project)
        assert host.engine.workspace.primary_cwd == str(elsewhere)
    finally:
        await host.shutdown()


async def test_full_workspace_reaches_agents_and_restores_from_another_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    shared = tmp_path / "shared"
    shared.mkdir()
    reference = shared / "guide.md"
    reference.write_text("reference")
    workspace = Workspace(str(project), [WorkingDir(str(shared), label="shared library")], [str(reference)])
    expected = deepcopy(workspace)
    patch_runtime(
        monkeypatch,
        [MockChatClient(responses=[]), *[MockChatClient(responses=[MockResponse(text="done")]) for _ in range(3)]],
    )
    write_workflow(
        project,
        "agent",
        b"from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\na = wf.agent('agent', profile='Headless')\nwf.start(a)\nwf.output(a)\nworkflow = wf.build()\n",
    )
    observed: list[Workspace] = []
    real_open = WorkflowAgentShell.open

    async def open_shell(shell: WorkflowAgentShell, prompt: str, *, attempt: int = 1) -> None:
        observed.append(deepcopy(shell._resources.workspace))
        await real_open(shell, prompt, attempt=attempt)

    monkeypatch.setattr(WorkflowAgentShell, "open", create_autospec(real_open, side_effect=open_shell))
    host = make_host(tmp_path, project=project, workspace=workspace)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "agent")
        first, events = await run(host, "agent")
        assert first.outcome.value == "completed"
        (accepted,) = of_type(events, WorkflowRunAccepted)
        assert accepted.selection is not None and accepted.selection.workspace.materialize() == expected
        session_id = host.workflow_session_id
        # The caller still owns its mutable Workspace; neither it nor the event owns the session's copy.
        workspace.working_dirs.clear()
        workspace.reference_files.clear()
        with pytest.raises(FrozenInstanceError):
            accepted.selection.workspace.primary_cwd = str(shared)
        second, _ = await run(host, "agent")
        assert second.outcome.value == "completed"
        state = (await store.load_workflow_session(session_id)).encode()
        meta = await store.load_session_meta(session_id)
        assert state is not None and state["workspace"] == asdict(expected)
        assert meta is not None and meta.working_dirs == [str(shared)]
    finally:
        await host.shutdown()
    restored = make_host(tmp_path, project=shared)
    try:
        await restored.load_workflow_session(session_id)
        assert restored.workflow_catalog.workdir == project
        third, _ = await run(restored, "agent")
        assert third.outcome.value == "completed"
        assert observed == [expected] * 3
    finally:
        await restored.shutdown()


@pytest.mark.parametrize("change", ["primary", "additional", "reference"])
async def test_existing_session_rejects_explicit_workspace_changes_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "here", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "here")
        await run(host, "here")
        original = (await store.load_workflow_session(host.workflow_session_id)).encode()
        changed = Workspace.from_cwd(str(project))
        if change == "primary":
            changed.primary_cwd = str(tmp_path)
        elif change == "additional":
            changed.working_dirs.append(WorkingDir(str(tmp_path)))
        else:
            changed.reference_files.append(str(tmp_path / "guide.md"))
        async with capture_event_sequence(host.event_bus, WorkflowRunRejected, WorkflowRunAccepted) as events:
            await host.event_bus.publish(
                WorkflowRunRequest(
                    target=replace(host.workflow_target("here"), workspace=WorkspaceSnapshot.capture(changed)),
                    request_id="change-workspace",
                )
            )
            await host.engine.workflows.wait_idle()
        (rejected,) = of_type(events, WorkflowRunRejected)
        assert rejected.error == "workspace_locked" and "Start a new session" in rejected.message
        assert "/new" not in rejected.message
        assert not of_type(events, WorkflowRunAccepted)
        assert (await store.load_workflow_session(host.workflow_session_id)).encode() == original
        result, _ = await run(host, "here")
        assert result.outcome.value == "completed"
    finally:
        await host.shutdown()


async def test_prepared_draft_keeps_workspace_across_chat_and_browsing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    first = make_workdir(tmp_path)
    second = tmp_path / "second"
    third = tmp_path / "third"
    second.mkdir()
    third.mkdir()
    source = python_workflow("from pathlib import Path\ndef check(text):\n    return str(Path.cwd())\n", "check")
    for project in (first, second):
        write_workflow(project, "review", source)
    host = make_host(tmp_path, project=first)
    try:
        await confirm(host, "review")
        await run(host, "review")
        previous = host.workflow_session_id
        await host.start()
        await host.event_bus.publish(WorkspaceChange(primary_cwd=str(second)), raise_handler_errors=True)
        workspace = Workspace(str(second), [WorkingDir(str(third))])
        target = host.workflow_target("review", new_session=True, workspace=workspace)
        prepared = await host.preview_workflow(target, trust=True)
        assert prepared.preview.source.canonical_path == str((second / ".chrys/workflows/review.py").resolve())
        host.confirm_workflow(prepared)
        workspace.working_dirs.clear()
        await host.event_bus.publish(WorkspaceChange(primary_cwd=str(third)), raise_handler_errors=True)
        await host.load_workflow_session(previous)
        result = await host.run_workflow_until_final(prepared)
        assert result.outputs[0].value.text == str(second)
        assert host.workflow_session_id != previous
        state = (
            await JsonFileStateStore(tmp_path / "sessions").load_workflow_session(host.workflow_session_id)
        ).encode()
        assert state is not None and state["workspace"]["working_dirs"][0]["path"] == str(third)
    finally:
        await host.shutdown()


@pytest.mark.parametrize("in_use", [False, True])
async def test_session_open_failures_have_distinct_rejection_codes(tmp_path: Path, in_use: bool) -> None:
    project = make_workdir(tmp_path)
    host = make_host(tmp_path, project=project)
    session_id = "0123456789abcdef0123456789abcdef"
    selection = WorkflowSessionSelection(
        session_id,
        WorkflowIdentity("missing", str(project / ".chrys" / "workflows" / "missing.py"), "workdir"),
        WorkspaceSnapshot.capture(Workspace.from_cwd(str(project))),
    )
    guard = ActiveSessionGuard(JsonFileStateStore(tmp_path / "sessions"))
    try:
        if in_use:
            assert await asyncio.to_thread(guard.ensure, session_id)
        with pytest.raises(WorkflowRunRejectedError) as rejected:
            await host.run_workflow_until_final(selection)
        assert rejected.value.event.error == ("session_in_use" if in_use else "session_not_found")
    finally:
        guard.release()
        await host.shutdown()
