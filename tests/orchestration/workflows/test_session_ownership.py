# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A workflow session owns its runs and can never become a chat or another workflow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.models.session_surface import SessionSurface
from chrys.orchestration.session_host import WorkflowRunRejectedError
from chrys.service.llm.mock import MockChatClient
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.workflows._hosting import confirm, make_host, make_workdir, patch_runtime, run, write_workflow
from tests.support.workflow_workers import python_workflow


async def test_session_binding_round_trips_and_rejected_changes_do_not_create_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    source = python_workflow("def check(text):\n    return text\n", "check")
    write_workflow(project, "review", source)
    host = make_host(tmp_path, project=project)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        with pytest.raises(WorkflowRunRejectedError):
            await run(host, "review")
        assert not host.workflow_session_id and not await store.list_sessions()
        await confirm(host, "review")
        first, _ = await run(host, "review", input_text="first")
        identity = host.workflow_session_id
        directory = host.workflow_session_dir
        assert directory is not None and host.session_id is None
        first_header = (directory / "workflows" / first.run_id / "run.json").read_bytes()
        second, _ = await run(host, "review", input_text="second")
        assert second.run_id != first.run_id and host.workflow_session_id == identity
        state = (await store.load_workflow_session(identity)).encode()
        assert state is not None and state["identity"]["workflow_id"] == "review"
        assert (directory / "workflows" / first.run_id / "run.json").read_bytes() == first_header
        write_workflow(project, "review", source + b"\n# changed revision\n")
        await confirm(host, "review")
        third, _ = await run(host, "review")
        assert host.workflow_session_id == identity
        assert (directory / "workflows" / first.run_id / "run.json").read_bytes() == first_header
        assert (directory / "workflows" / third.run_id / "source.py").read_bytes() != (
            directory / "workflows" / first.run_id / "source.py"
        ).read_bytes()
        write_workflow(project, "other", source)
        prepared = await host.preview_workflow(host.workflow_target("other", new_session=True), trust=True)
        host.confirm_workflow(prepared)
        with pytest.raises(WorkflowRunRejectedError, match="new workflow draft"):
            await run(host, "other")
        meta = await store.load_session_meta(identity)
        assert meta is not None and meta.kind == "workflow" and meta.run_count == 3
        # A new draft may bind the new revision, without changing the archived session.
        fourth, _ = await run(host, "review", new_session=True)
        assert fourth.run_id not in {first.run_id, second.run_id, third.run_id}
        assert host.workflow_session_id != identity
        assert await store.load_latest_session_id(chat_only=True) is None
    finally:
        await host.shutdown()


@pytest.mark.parametrize("surface", [SessionSurface.CLI, SessionSurface.ACP])
async def test_a_run_records_the_launch_surface_on_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, surface: SessionSurface
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project, surface=surface)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "review")
        await run(host, "review")
        identity = host.workflow_session_id
        state = await store.load_workflow_session(identity)
        assert state is not None and state.surface is surface
        meta = await store.load_session_meta(identity)
        assert meta is not None and meta.last_surface is surface
    finally:
        await host.shutdown()


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("after_write", [False, True])
async def test_admission_save_failure_does_not_create_a_run_or_empty_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, existing: bool, after_write: bool
) -> None:
    patch_runtime(monkeypatch, [MockChatClient(responses=[])])
    project = make_workdir(tmp_path)
    write_workflow(project, "review", python_workflow("def check(text):\n    return text\n", "check"))
    host = make_host(tmp_path, project=project)
    store = JsonFileStateStore(tmp_path / "sessions")
    try:
        await confirm(host, "review")
        if existing:
            await run(host, "review")
        identity = host.workflow_session_id
        original_save = JsonFileStateStore.save_workflow_session

        async def fail_save(self, session_id, state, **kwargs):
            if after_write:
                await original_save(self, session_id, state, **kwargs)
            raise OSError("test session write failure")

        monkeypatch.setattr(
            JsonFileStateStore, "save_workflow_session", create_autospec(original_save, side_effect=fail_save)
        )
        with pytest.raises(WorkflowRunRejectedError, match="test session write failure"):
            await run(host, "review")
        assert host.workflow_session_id == identity
        sessions = await store.list_sessions()
        assert len(sessions) == int(existing)
        if existing:
            assert sessions[0].run_count == 1
    finally:
        await host.shutdown()
