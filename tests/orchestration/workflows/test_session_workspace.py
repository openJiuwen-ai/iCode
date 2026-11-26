# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow workspace ownership survives Chat changes, persistence and direct API requests."""

from __future__ import annotations

import asyncio
import json
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.events.types import (
    WorkflowRunAccepted,
    WorkflowRunRejected,
    WorkflowRunRequest,
    WorkspaceChange,
)
from chrys.foundation.models.workflow_session import WorkflowIdentity, WorkflowSessionSelection, WorkspaceSnapshot
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.orchestration.workflows.agent_node import WorkflowAgentShell
from chrys.service.approval.formal import build_formal_user_prompt
from chrys.service.approval.review_types import ApprovalJudgeInput
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile
from chrys.service.state.locks import ActiveSessionGuard
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    of_type,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.acp_fixtures import STUB_SCRIPT
from tests.support.event_capture import capture_event_sequence
from tests.support.secure_files import plant_owner_only_bytes
from tests.support.workflow_workers import python_workflow
from tests.support.workspace_engine import capture_async_results, select_workspace
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry
from tests.support.workspace_registry import make_workdirs


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
        assert state is not None and state["workspace"] == {
            key: value for key, value in asdict(expected).items() if key not in {"id", "name"}
        }
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


@pytest.mark.parametrize("own_cwd", [False, True])
@pytest.mark.parametrize("backend", ["kernel", "acp"])
async def test_agent_build_uses_only_the_confirmed_workflow_scope(registry, tmp_path, monkeypatch, own_cwd, backend):
    from chrys.orchestration.workflows import agent_node_build
    from chrys.service.tools import packs

    client = MockChatClient(responses=[MockResponse(text="done")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), client])
    primary = make_workdir(tmp_path)
    related, latest = make_workdirs(tmp_path, "related", "latest")
    workspace = registry.create("Team Chat", str(primary), [str(related)])
    external = AgentProfile(name="External", acp=AcpAgentConfig(command=sys.executable, args=[str(STUB_SCRIPT)]))
    profile = "Headless" if backend == "kernel" else external.name
    write_workflow(
        primary,
        "agent",
        (
            "from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('agent')\n"
            f"a = wf.agent('agent', profile={profile!r})\nwf.start(a)\nwf.output(a)\nworkflow = wf.build()\n"
        ).encode(),
    )
    workflow_cwd = tmp_path / "workflow" if own_cwd else primary
    if own_cwd:
        workflow_cwd.mkdir()
        write_workflow(
            workflow_cwd,
            "agent",
            (primary / ".chrys" / "workflows" / "agent.py").read_bytes(),
        )
    host = make_host(tmp_path, project=primary, profiles=[make_profile(), external])
    tool_dirs = []
    approval_roots = []
    approval_contexts = []
    acp_dirs = []
    broker = agent_node_build.AcpPermissionBroker
    resolve = agent_node_build.resolve_acp_spec

    def observe_tools(_result, build):
        tool_dirs.append([directory.path for directory in build.runtime.working_dirs])

    def observe_approval(_result, _conversation, inputs):
        approval_roots.append(inputs["workspace_roots"])
        approval_contexts.append(inputs["turn_context"])

    def acp_approval(**kwargs):
        approval_roots.append(kwargs["workspace_roots"])
        return broker(**kwargs)

    def resolve_spec(*args, **kwargs):
        spec = resolve(*args, **kwargs)
        acp_dirs.append(spec.additional_directories)
        return spec

    try:
        await host.start()
        await select_workspace(host.event_bus, workspace.id)
        assert host.engine.workspace.id == workspace.id
        registry.update(workspace.id, name="Renamed", primary=str(primary), folders=[str(latest)])
        target = host.workflow_target("agent", new_session=True)
        assert target.workspace.materialize() == Workspace(str(primary), [WorkingDir(str(related))])
        capture_async_results(monkeypatch, packs, "assemble_tools", observe_tools)
        capture_async_results(monkeypatch, agent_node_build, "create_approval", observe_approval)
        monkeypatch.setattr(agent_node_build, "AcpPermissionBroker", acp_approval)
        monkeypatch.setattr(agent_node_build, "resolve_acp_spec", resolve_spec)
        confirmed_dirs = [str(related)]
        if own_cwd:
            confirmed = tmp_path / "confirmed"
            confirmed.mkdir()
            confirmed_dirs = [str(confirmed)]
            target = host.workflow_target(
                "agent", new_session=True, workspace=Workspace(str(workflow_cwd), [WorkingDir(str(confirmed))])
            )
        preview = await host.preview_workflow(target, trust=True)
        host.confirm_workflow(preview)
        result = await host.run_workflow_until_final(target, input_text="Check scope.")
        assert result.outcome.value == "completed"
        assert approval_roots == [[str(workflow_cwd), *confirmed_dirs]]
        if backend == "acp":
            assert acp_dirs == [tuple(str(Path(path).resolve()) for path in confirmed_dirs)]
        else:
            assert tool_dirs == [confirmed_dirs]
            messages, _ = client.call_history[0]
            prompt = "\n".join(message.text or "" for message in messages)
            assert f"Working directory: {json.dumps(str(workflow_cwd), ensure_ascii=False)}" in prompt
            assert all(json.dumps(path, ensure_ascii=False) in prompt for path in confirmed_dirs)
            assert json.dumps(str(latest), ensure_ascii=False) not in prompt
            (turn_context,) = approval_contexts
            formal_prompt = build_formal_user_prompt(
                ApprovalJudgeInput(
                    tool_name="read_file",
                    tool_kind="filesystem.read",
                    args={"path": "target.txt"},
                    user_message=turn_context.user_message,
                    user_messages=turn_context.user_messages,
                    workspace_roots=[],
                ),
                history=messages,
            )
            assert [json.loads(row) for row in formal_prompt.splitlines()[1:3]] == [
                {"user": "Check scope."},
                {"workflow_input": "Check scope."},
            ]
            if own_cwd:
                assert json.dumps(str(related), ensure_ascii=False) not in prompt
                assert json.dumps(str(primary), ensure_ascii=False) not in prompt
        state = await JsonFileStateStore(tmp_path / "sessions").load_workflow_session(host.workflow_session_id)
        assert state.workspace == target.workspace
    finally:
        await host.shutdown()


@pytest.mark.parametrize("after_scan", [False, True])
async def test_main_workflow_session_and_run_round_trip(tmp_path, after_scan):
    from chrys import __version__
    from chrys.foundation.config.settings import resolve_sessions_dir
    from chrys.foundation.platform import get_platform
    from chrys.orchestration.startup import bootstrap_runtime
    from chrys.service.workflows.store import RunHeader, RunSpec, WorkflowRunStore, read_run_header

    fixtures = Path(__file__).parents[2] / "support" / "fixtures" / "workspace_legacy"
    config_dir = get_platform().config_dir
    store = JsonFileStateStore(resolve_sessions_dir(config_dir))
    session_id = "legacy-workflow"
    session = store.session_dir(session_id) / "session.json"
    session.parent.mkdir(parents=True)
    if after_scan:
        bootstrap_runtime(apply_patches=False, setup_telemetry=False)
        assert list((config_dir / "backups").glob(".workdir-*.done"))
    original = (fixtures / "workflow-session.json").read_bytes()
    plant_owner_only_bytes(session, original)

    before = (fixtures / "workflow-run.json").read_bytes()
    source = session.parent / "workflows" / "old-run"
    source.mkdir(parents=True)
    run = source / "run.json"
    plant_owner_only_bytes(run, before)

    bootstrap_runtime(apply_patches=False, setup_telemetry=False)
    state = await store.load_workflow_session(session_id)
    assert state.workspace.primary_cwd == "/repo"
    assert [directory.path for directory in state.workspace.working_dirs] == ["/repo", "/shared"]
    assert state.identity.source_kind == "workdir"
    backups = config_dir / "backups" / __version__ / "sessions" / session.parent.name
    if after_scan:
        assert session.read_bytes() == original
        assert run.read_bytes() == before
        assert not backups.exists()
    else:
        assert (backups / session.name).read_bytes() == original
        assert (backups / run.relative_to(session.parent)).read_bytes() == before

    await store.save_workflow_session(session_id, state, title="Review")
    expected = json.loads(original)["state"]
    expected["identity"]["source_kind"] = "workdir"
    serialized = json.loads(session.read_bytes())["state"]
    assert serialized == expected

    header = read_run_header(source)
    assert header["run_id"] == state.latest_run_id
    assert header["source_kind"] == "workdir"
    destination = tmp_path / "round-trip"
    run_store = WorkflowRunStore.open(
        destination,
        header=RunHeader(**header),
        spec=RunSpec({}, {}),
        input_text="Review a change",
        source=b"# review\n",
    )
    try:
        assert read_run_header(destination) == header
    finally:
        await run_store.close()
