# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Independent W/C/N/U workspace rules exercised through the production engine."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

import pytest

from chrys.foundation.config.settings_store import SettingsHandle
from chrys.foundation.events.types import (
    AgentLoadFailed,
    Error,
    InvocationContentFinished,
    SessionRestore,
    SideQuestionRequested,
    WorkspaceChange,
    WorkspaceChangeFinished,
    WorkspaceOperation,
)
from chrys.foundation.models.workspace import Workspace, WorkspaceConfirmation, session_workdirs
from chrys.kernel import Message
from chrys.service.llm import side_question
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.paths import MissingWorkspacePrimary
from tests.support.components import plan_workspace
from tests.support.event_capture import capture_event_sequence
from tests.support.rollback_spec import PROFILE, ask
from tests.support.symlinks import symlink_or_skip
from tests.support.workspace_engine import side_answer_settled, start_workspace_engine, workspace_selection
from tests.support.workspace_engine import workspace_case as workspace_case
from tests.support.workspace_registry import (
    corrupt_workspace_registry,
    make_workdir_alias,
    make_workdirs,
    retarget_workdir_alias,
)
from tests.support.workspace_registry import default_workspace_registry as default_workspace_registry


async def test_workspace_plan_rejects_a_missing_matching_target_cwd(workspace_case, tmp_path):
    primary = tmp_path / "primary"
    primary.mkdir()
    engine, workspace = await workspace_case.start_selected(primary)
    snapshot = workspace_case.registry.snapshot()
    primary.rmdir()

    with pytest.raises(MissingWorkspacePrimary):
        engine.lifecycle.workspaces.plan(
            workspace=workspace,
            snapshot=snapshot,
            session_id=engine.session_id,
            restoring=True,
            target_cwd=workspace.primary_cwd,
            use_target_cwd=True,
        )


async def test_reselect_and_new_session_resolve_registry_links_without_changing_live_roots(workspace_case, tmp_path):
    from chrys.foundation.events.types import SessionNew

    primary = tmp_path / "primary"
    primary.mkdir()
    first, second, alias = make_workdir_alias(tmp_path, "first", "second", "alias")
    missing = str(tmp_path / "missing")
    workspace = workspace_case.registry.create("Team", str(primary), [str(alias), missing])
    engine = await workspace_case(primary, workspace_id=workspace.id)
    sid = engine.session_id
    installed = engine.current.require_loaded()
    await engine.event_bus.publish(workspace_selection(workspace.id), raise_handler_errors=True)
    assert engine.current.require_loaded() is installed

    retarget_workdir_alias(alias, second)
    assert [directory.path for directory in installed.runtime.working_dirs] == [str(first.resolve())]
    await engine.event_bus.publish(workspace_selection(workspace.id), raise_handler_errors=True)
    refreshed = engine.current.require_loaded()
    assert refreshed is not installed
    assert engine.session_id == sid
    assert [directory.path for directory in refreshed.runtime.working_dirs] == [str(second.resolve())]

    retarget_workdir_alias(alias, first)
    await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)
    assert engine.session_id != sid
    assert engine.session.workspace_id == workspace.id
    assert engine.workspace.primary_cwd == str(primary.resolve())
    assert [directory.path for directory in engine.workspace.working_dirs] == [str(first.resolve())]

    corrupt_workspace_registry(workspace_case.registry)
    await engine.event_bus.publish(SessionNew(), raise_handler_errors=True)
    runtime = engine.current.require_loaded().runtime
    assert [directory.path for directory in runtime.working_dirs] == [str(first.resolve())]
    assert runtime.missing_working_dirs == (missing,)


async def test_side_question_keeps_workspace_but_does_not_receive_its_context_or_tools(
    workspace_case, monkeypatch, tmp_path
) -> None:
    """N9: btw is attached to the parent while its actual model request stays isolated."""
    engine, _ = await workspace_case.start_selected(tmp_path, "ProjectContextSentinel")
    before = (engine.workspace, engine.session.workspace_id, engine.session.session_workspace, engine.current.loaded)
    client = MockChatClient([MockResponse(text="Side answer")])

    @asynccontextmanager
    async def scoped(profile, *, session_id, parent_session_id, session_dir):
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(side_question, "scoped_client", scoped)
    ended = asyncio.Event()

    async def finished(event: InvocationContentFinished) -> None:
        ended.set()

    await engine.event_bus.subscribe(InvocationContentFinished, finished)
    request = SideQuestionRequested(session_id=engine.session_id, request_id="side", question="What does this mean?")
    await engine.event_bus.publish(request, raise_handler_errors=True)
    assert request.accepted
    await side_answer_settled(engine)
    assert ended.is_set()

    assert (
        engine.workspace,
        engine.session.workspace_id,
        engine.session.session_workspace,
        engine.current.loaded,
    ) == before
    assert client.call_count == 1
    messages, options = client._call_history[0]
    assert "ProjectContextSentinel" not in "\n".join(message.text for message in messages)
    assert str(tmp_path) not in "\n".join(message.text for message in messages)
    assert not options.get("tools")


@pytest.mark.parametrize("location", ["primary", "related", "deep", "outside", "missing"])
async def test_select_workspace_landing_preserves_history_and_only_clears_extra_roots_when_moved(
    workspace_case, tmp_path, location
) -> None:
    """W3/W4/W5: the cwd itself must exist to preserve cwd and A."""
    primary, related, outside, extra = (tmp_path / part for part in ("primary", "related", "outside", "extra"))
    deep = related / "nested" / "deep"
    for path in (primary, deep, outside, extra):
        path.mkdir(parents=True)
    root = {
        "primary": primary,
        "related": related,
        "deep": deep,
        "outside": outside,
        "missing": related / "gone",
    }[location]
    engine = await workspace_case(root if location != "missing" else related, additional=[extra])
    if location == "missing":
        engine.session.install_workspace(
            plan_workspace(Workspace(primary_cwd=str(root), working_dirs=engine.workspace.working_dirs))
        )
    before_history = {"messages": [Message("user", ["keep this conversation"])], "compressed_msgs": []}
    engine.current.require_loaded().bindings.backend.history_state = before_history
    project = workspace_case.registry.create("App", str(primary), [str(related)])
    event = workspace_selection(project.id, require_leave_confirmation=False)
    await engine.event_bus.publish(event, raise_handler_errors=True)

    assert engine.session.workspace_id == project.id
    assert engine.session.session_workspace is not None and engine.session.session_workspace.id == project.id
    assert engine.workspace.primary_cwd == str(primary)
    assert [directory.path for directory in engine.workspace.working_dirs] == [str(related)]
    assert session_workdirs(engine.workspace) == []
    assert (
        engine.current.require_loaded().bindings.backend.history_state["messages"][0].text == "keep this conversation"
    )


async def test_tui_leave_requires_matching_confirmation(registry, agent_engine, monkeypatch, tmp_path):
    primary, target = make_workdirs(tmp_path, "primary", "target")
    workspace = registry.create("Team", str(primary))
    store = JsonFileStateStore(tmp_path / "sessions", workspace_registry=registry)
    engine = await start_workspace_engine(agent_engine, monkeypatch, primary, workspace_id=workspace.id, store=store)
    confirmation = WorkspaceConfirmation.capture(workspace.id, str(target))
    target.rename(tmp_path / "previous-target")
    target.mkdir()
    async with capture_event_sequence(engine.event_bus, Error) as errors:
        await engine.event_bus.publish(
            WorkspaceChange(primary_cwd=str(target), confirmation=confirmation),
            raise_handler_errors=True,
        )
    assert [error.code for error in errors] == ["workspace_leave_confirmation_required"]
    assert engine.workspace.primary_cwd == str(primary)
    assert registry.snapshot().workspace_id_for_session(engine.session_id) == workspace.id


@pytest.mark.parametrize("named", ["link", "dotdot"])
async def test_tui_leave_runs_in_the_folder_as_named(registry, agent_engine, monkeypatch, tmp_path, named):
    """Like /chdir in an Unspecified session, a link keeps its name; a `..` resolves as the platform does."""
    primary, physical, _ = make_workdirs(tmp_path, "primary", "physical", "sibling")
    make_workdirs(physical, "child", "sibling")
    alias = tmp_path / "alias"
    symlink_or_skip(alias, physical / "child", target_is_directory=True)
    target = alias if named == "link" else alias / ".." / "sibling"
    expected = str(alias) if named == "link" else os.path.realpath(target)
    workspace = registry.create("Team", str(primary))
    store = JsonFileStateStore(tmp_path / "sessions", workspace_registry=registry)
    engine = await start_workspace_engine(agent_engine, monkeypatch, primary, workspace_id=workspace.id, store=store)

    confirmation = WorkspaceConfirmation.capture(workspace.id, str(target))
    await engine.event_bus.publish(
        WorkspaceChange(primary_cwd=str(target), confirmation=confirmation), raise_handler_errors=True
    )

    assert (engine.workspace.primary_cwd, engine.workspace.id) == (expected, "")


async def test_change_rejects_a_confirmation_from_a_workspace_already_left(workspace_case, tmp_path):
    primary, target = make_workdirs(tmp_path, "primary", "target")
    engine, workspace = await workspace_case.start_selected(primary)
    confirmation = WorkspaceConfirmation.capture(workspace.id, str(target))
    await engine.event_bus.publish(WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY))

    request = WorkspaceChange(primary_cwd=str(target), confirmation=confirmation)
    async with capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as results:
        await engine.event_bus.publish(request)

    (result,) = results
    assert result.error is not None
    assert result.error.code == "workspace_leave_confirmation_required"
    assert engine.workspace.primary_cwd == str(primary)


async def test_selecting_the_installed_workspace_rebuilds_after_restore_build_failure(
    workspace_case, tmp_path, monkeypatch
):
    primary = tmp_path / "primary"
    primary.mkdir()
    registry, store = workspace_case.registry, workspace_case.store
    workspace = registry.create("Team", str(primary))
    await store.save_session("target", {"messages": []}, agent_profile=PROFILE.name, primary_cwd=str(primary))
    registry.set_session_workspace("target", workspace.id)
    engine = await workspace_case(tmp_path)

    async def fail_build(*args, **kwargs):
        raise RuntimeError("restore build failed")

    with monkeypatch.context() as patch:
        patch.setattr(engine.loader, "build", fail_build)
        with pytest.raises(RuntimeError, match="restore build failed"):
            await engine.event_bus.publish(SessionRestore(session_id="target"), raise_handler_errors=True)
    # The failed restore must not leave the target's identity behind for a later rebuild to save over.
    assert engine.session.session_id != "target"
    assert engine.current.loaded is None

    async with capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as results:
        await engine.event_bus.publish(workspace_selection(workspace.id))

    (result,) = results
    assert result.error is None
    assert engine.current.loaded is not None


async def test_first_start_does_not_write_membership_when_profile_scan_fails(
    registry, agent_engine, monkeypatch, tmp_path
):
    from chrys.foundation.events.bus import EventBus
    from chrys.service.profiles.reload import ProfileReload
    from tests.support.workspace_engine import mock_workspace_runtime

    workspace = registry.create("Team", str(tmp_path))
    loaded, profiles, models = mock_workspace_runtime(monkeypatch)
    engine = agent_engine(
        EventBus(),
        loaded_settings=loaded,
        agent_registry=profiles,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions", workspace_registry=registry),
    )

    def fail_scan(_profiles):
        raise PermissionError("profile scan denied")

    monkeypatch.setattr(ProfileReload, "scan", fail_scan)
    with pytest.raises((PermissionError, ValueError), match="profile scan denied"):
        await engine.start(PROFILE, workspace_id=workspace.id)

    assert not registry.snapshot().sessions


@pytest.mark.parametrize("failure", ["move_before_load", "move_during_load", "select_before_commit"])
async def test_precommit_workspace_failure_reports_once(workspace_case, tmp_path, monkeypatch, failure):
    primary, destination = make_workdirs(tmp_path, "primary", "destination")
    engine, workspace = await workspace_case.start_selected(primary)
    registry = workspace_case.registry
    sid = engine.session_id
    original = engine.current.require_loaded()

    def fail(*args, **kwargs):
        raise RuntimeError("settings changed during preparation")

    if failure == "select_before_commit":
        target = registry.create("Destination", str(destination))
        request = workspace_selection(target.id)
    else:
        request = WorkspaceChange(
            primary_cwd=str(destination), confirmation=WorkspaceConfirmation.capture(workspace.id, str(destination))
        )
    if failure == "move_before_load":
        monkeypatch.setattr(engine.lifecycle.workspaces, "membership_write", fail)
    else:
        monkeypatch.setattr(SettingsHandle, "check_prepared", fail)

    async with capture_event_sequence(engine.event_bus, Error, AgentLoadFailed, WorkspaceChangeFinished) as events:
        await engine.event_bus.publish(request)

    assert engine.current.require_loaded() is original
    assert registry.snapshot().workspace_id_for_session(sid) == workspace.id
    terminal = events[-1]
    assert isinstance(terminal, WorkspaceChangeFinished)
    assert terminal.request_id == request.event_id
    assert terminal.workspace_update is None
    assert terminal.error is not None
    assert terminal.error.code == "workspace_change_failed"
    assert terminal.error.message == "settings changed during preparation"
    assert terminal.session_id == sid

    assert len(events) == 2, events
    reported = events[0]
    assert reported.message == terminal.error.message
    assert reported.session_id == sid
    if failure == "move_during_load":
        assert isinstance(reported, AgentLoadFailed)
        assert reported.operation == "workspace_change"
    else:
        assert isinstance(reported, Error)
        assert reported.code == "workspace_change_failed"


def _last_user_text(client: MockChatClient) -> str:
    """The newest user message of the newest request, where catalogs ride."""
    messages, _options = client.call_history[-1]
    return [message.text for message in messages if message.role == "user"][-1]


@pytest.mark.parametrize("entry", ["chdir", "restore"])
async def test_failed_open_reports_that_implicit_departure_was_saved(workspace_case, tmp_path, monkeypatch, entry):
    primary, destination = make_workdirs(tmp_path, "primary", "destination")
    registry = workspace_case.registry
    engine, workspace = await workspace_case.start_selected(primary)
    await ask(engine, "save the session")
    sid = engine.session_id
    confirmation = WorkspaceConfirmation.capture(workspace.id, str(destination))

    def fail_install(*args, **kwargs):
        raise RuntimeError("opening failed")

    monkeypatch.setattr(engine.loader, "install", fail_install)
    event = (
        WorkspaceChange(primary_cwd=str(destination), confirmation=confirmation)
        if entry == "chdir"
        else SessionRestore(session_id=sid, primary_cwd=str(destination), confirmation=confirmation)
    )
    async with capture_event_sequence(engine.event_bus, Error) as errors:
        await engine.event_bus.publish(event)
    (reported,) = errors
    assert reported.code == "workspace_membership_saved_open_failed"
    assert "Workspace membership was cleared" in reported.message
    assert "The session reopens as Unspecified in its saved directory" in reported.message
    assert registry.snapshot().workspace_id_for_session(sid) is None


async def test_a_departure_lasts_while_running_and_disappears_after_reopen(workspace_case, tmp_path):
    registry = workspace_case.registry
    workspace = registry.create("Team", str(tmp_path))
    client = MockChatClient(responses=[MockResponse(text="answer")])
    engine = await workspace_case(tmp_path, workspace_id=workspace.id, client=client)
    await ask(engine, "first turn")
    sid = engine.session_id
    await engine.event_bus.publish(
        WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY),
        raise_handler_errors=True,
    )
    await ask(engine, "after leaving")
    assert 'left workspace "Team"' in _last_user_text(client)
    assert registry.snapshot().workspace_id_for_session(sid) is None
    await engine.shutdown()

    reopened = await workspace_case(tmp_path, client=client)
    await reopened.event_bus.publish(SessionRestore(session_id=sid), raise_handler_errors=True)
    await ask(reopened, "after reopen")
    assert 'left workspace "Team"' not in _last_user_text(client)
