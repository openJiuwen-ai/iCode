# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Independent W/C/N/U workspace rules exercised through the production engine."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from itertools import pairwise
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings_store import SettingsHandle
from chrys.foundation.events.types import (
    AgentLoadFailed,
    Error,
    InvocationContentFinished,
    SessionRestore,
    SideQuestionRequested,
    UserInterrupt,
    UserMessage,
    UserRollback,
    WorkspaceChange,
    WorkspaceChangeFinished,
    WorkspaceOperation,
    WorkspaceUpdated,
)
from chrys.foundation.i18n import Localizer
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace, WorkspaceConfirmation, session_workdirs
from chrys.foundation.models.workspace_operations import WorkspaceDefinitionEdit
from chrys.foundation.util.lock import FileLock
from chrys.kernel import Message
from chrys.orchestration.engine import workspaces as engine_workspaces
from chrys.service.llm import side_question
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces import registry as registry_module
from chrys.service.workspaces.paths import MissingWorkspacePrimary
from tests.support.components import plan_workspace
from tests.support.event_capture import capture_event_sequence
from tests.support.rollback_spec import PROFILE, ask, drain
from tests.support.symlinks import symlink_or_skip
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workspace_engine import (
    capture_async_results,
    side_answer_settled,
    start_workspace_engine,
    workspace_selection,
)
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


@pytest.mark.parametrize(
    "invalid", ["other-workspace", "unspecified", "foreign-session", "no-runtime", "no-session-id"]
)
async def test_membership_alignment_refuses_a_stale_or_unavailable_runtime(
    workspace_case, tmp_path, monkeypatch, invalid
):
    engine, workspace = await workspace_case.start_selected(tmp_path)
    original = engine.current.require_loaded()
    before = workspace_case.registry.path.read_bytes()
    request = WorkspaceChange(
        workspace_operation=WorkspaceOperation.ALIGN_MEMBERSHIP,
        workspace_id=workspace.id,
        session_id=engine.session_id,
        report_error=False,
    )
    with monkeypatch.context() as patch:
        if invalid == "other-workspace":
            request.workspace_id = "ws_" + "f" * 32
        elif invalid == "unspecified":
            request.workspace_id = None
        elif invalid == "foreign-session":
            request.session_id = "another-session"
        elif invalid == "no-runtime":
            patch.setattr(engine.current, "loaded", None)
        else:
            patch.setattr(engine.session, "session_id", None)
            request.session_id = None
        async with capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as events:
            await engine.event_bus.publish(request, raise_handler_errors=True)
    (terminal,) = events
    assert terminal.error is not None
    assert terminal.workspace_update is None
    assert workspace_case.registry.path.read_bytes() == before
    assert engine.current.require_loaded() is original


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
    async def scoped(profile, *, session_id, parent_session_id, session_dir, request_role):
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


@asynccontextmanager
async def _hold_registry_lock(registry, monkeypatch, *, duration=None):
    path = registry.path.with_suffix(".json.lock")
    loop = asyncio.get_running_loop()
    held = asyncio.Event()
    attempted = loop.create_future()
    attempted_in_thread = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with FileLock(path):
            loop.call_soon_threadsafe(held.set)
            if attempted_in_thread.wait(ENGINE_TURN_TIMEOUT):
                release.wait(duration if duration is not None else ENGINE_TURN_TIMEOUT)

    acquire = FileLock.acquire

    def observe(lock: FileLock) -> None:
        if lock.path == path and not attempted.done():
            attempted.set_result(loop.time())
            attempted_in_thread.set()
        acquire(lock)

    holder = asyncio.create_task(asyncio.to_thread(hold))
    try:
        await wait_for(lambda: held.is_set() or holder.done(), timeout=ENGINE_TURN_TIMEOUT)
        if holder.done():
            await holder
        assert held.is_set()
        with monkeypatch.context() as patch:
            patch.setattr(FileLock, "acquire", observe)
            yield attempted, release
    finally:
        release.set()
        attempted_in_thread.set()
        await holder


def _rename_workspace(workspace: Workspace, name: str = "Renamed") -> WorkspaceChange:
    return workspace_selection(workspace.id, edit_workspace=WorkspaceDefinitionEdit(workspace, name, ()))


@pytest.mark.parametrize("operation", ["rename", "same_name", "delete", "switch", "unspecify", "align"])
async def test_workspace_write_keeps_the_loop_responsive_under_lock_contention(
    workspace_case, tmp_path, monkeypatch, operation
):
    primary, other_primary = tmp_path / "team", tmp_path / "other"
    primary.mkdir()
    other_primary.mkdir()
    engine, workspace = await workspace_case.start_selected(primary)
    registry = workspace_case.registry
    other = registry.create("Other", str(other_primary))
    original = engine.current.require_loaded()
    if operation == "align":
        registry.set_session_workspace(engine.session_id, other.id)
    name = workspace.name if operation == "same_name" else "Renamed"
    request = {
        "delete": lambda: WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY, delete_workspace=workspace),
        "switch": lambda: workspace_selection(other.id),
        "unspecify": lambda: WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY),
        "align": lambda: WorkspaceChange(
            workspace_operation=WorkspaceOperation.ALIGN_MEMBERSHIP, workspace_id=workspace.id
        ),
    }.get(operation, lambda: _rename_workspace(workspace, name))()
    loop = asyncio.get_running_loop()
    async with _hold_registry_lock(registry, monkeypatch, duration=0.3) as (attempted, _):
        ticks = []

        def tick() -> None:
            nonlocal timer
            if attempted.done():
                ticks.append(loop.time())
            timer = loop.call_later(0.005, tick)

        timer = loop.call_soon(tick)
        try:
            async with capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as events:
                await engine.event_bus.publish(request, raise_handler_errors=True)
        finally:
            timer.cancel()
        finished_at = loop.time()

    assert attempted.done()
    samples = [attempted.result(), *ticks, finished_at]
    max_gap = max(after - before for before, after in pairwise(samples))
    assert max_gap < 0.03, f"Maximum event-loop gap: {max_gap * 1000:.1f} ms"
    (terminal,) = events
    assert terminal.error is None
    assert (engine.current.require_loaded() is original) == (operation in ("same_name", "align"))
    snapshot = registry.snapshot()
    if operation in ("switch", "unspecify", "align"):
        expected = other if operation == "switch" else workspace if operation == "align" else None
        assert engine.session.session_workspace == expected
        assert snapshot.sessions.get(engine.session.session_id) == (expected.id if expected else None)
        return
    saved = snapshot.workspace(workspace.id)
    assert engine.session.session_workspace == saved
    assert (saved.name if saved is not None else None) == (None if operation == "delete" else name)


async def test_editing_the_current_workspace_probes_its_folders_once_off_the_loop(
    workspace_case, tmp_path, monkeypatch
):
    primary = tmp_path / "team"
    primary.mkdir()
    engine, workspace = await workspace_case.start_selected(primary)
    registry = workspace_case.registry
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    probes: list[int] = []
    held, release = threading.Event(), threading.Event()
    probe = registry_module._probe_folders

    def held_probe(*args, **kwargs):
        probes.append(threading.get_ident())
        held.set()
        release.wait(ENGINE_TURN_TIMEOUT)
        return probe(*args, **kwargs)

    monkeypatch.setattr(registry_module, "_probe_folders", held_probe)
    async with capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as events:
        change = asyncio.create_task(engine.event_bus.publish(_rename_workspace(workspace), raise_handler_errors=True))
        await wait_for(held.is_set, timeout=ENGINE_TURN_TIMEOUT)
        # The loop keeps running while a slow mount holds the folder check.
        ticks = []
        for _ in range(5):
            ticks.append(loop.time())
            await asyncio.sleep(0.005)
        assert max(after - before for before, after in pairwise(ticks)) < 0.05
        assert not change.done()
        release.set()
        await change
    (terminal,) = events
    assert terminal.error is None
    assert probes and loop_thread not in probes
    assert len(probes) == 1
    assert registry.snapshot().workspace(workspace.id).name == "Renamed"


@pytest.mark.parametrize("outcome", ["timeout", "closing", "disk-full"])
async def test_membership_alignment_fails_strictly_without_changing_runtime(
    workspace_case, tmp_path, monkeypatch, outcome
):
    primary, other_primary = make_workdirs(tmp_path, "primary", "other")
    engine, workspace = await workspace_case.start_selected(primary)
    registry = workspace_case.registry
    other = registry.create("Other", str(other_primary))
    registry.set_session_workspace(engine.session_id, other.id)
    before = registry.path.read_bytes()
    installed = engine.current.require_loaded()
    request = WorkspaceChange(
        workspace_operation=WorkspaceOperation.ALIGN_MEMBERSHIP,
        workspace_id=workspace.id,
        session_id=engine.session_id,
        report_error=False,
    )
    with monkeypatch.context() as patch:
        if outcome == "disk-full":

            def fail_write(*args, **kwargs):
                raise OSError("registry disk full")

            patch.setattr(registry_module, "atomic_write_text", fail_write)
        async with (
            capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as events,
            _hold_registry_lock(registry, monkeypatch) as (attempted, release),
            asyncio.TaskGroup() as group,
        ):
            task = group.create_task(engine.event_bus.publish(request, raise_handler_errors=True))
            await wait_for(lambda: attempted.done() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
            assert attempted.done() and not task.done()
            if outcome == "closing":
                patch.setattr(engine.session, "shutting_down", True)
                release.set()
            elif outcome == "disk-full":
                release.set()
            await task
    (terminal,) = events
    assert terminal.error is not None
    assert terminal.workspace_update is None
    if outcome == "timeout":
        assert "Timed out acquiring lock" in terminal.error.message
    elif outcome == "disk-full":
        assert "registry disk full" in terminal.error.message
    else:
        assert "session changed" in terminal.error.message.lower()
    assert registry.path.read_bytes() == before
    assert engine.current.require_loaded() is installed
    assert engine.workspace == workspace


@pytest.mark.parametrize("outcome", ["timeout", "cancel", "stale_baseline"])
async def test_current_workspace_rename_refuses_to_commit_after_waiting_on_the_lock(
    workspace_case, tmp_path, monkeypatch, engine_services, outcome
):
    engine, workspace = await workspace_case.start_selected(tmp_path)
    registry = workspace_case.registry
    original = engine.current.require_loaded()
    before = registry.path.read_bytes()
    discarded = []
    capture_async_results(monkeypatch, engine.loader, "discard_candidate", lambda _, build: discarded.append(build))
    loop = asyncio.get_running_loop()
    async with (
        capture_event_sequence(engine.event_bus, WorkspaceChangeFinished) as events,
        _hold_registry_lock(registry, monkeypatch) as (attempted, release),
        asyncio.TaskGroup() as group,
    ):
        task = group.create_task(engine.event_bus.publish(_rename_workspace(workspace)))
        await wait_for(lambda: attempted.done() or task.done(), timeout=ENGINE_TURN_TIMEOUT)
        assert attempted.done() and not task.done()
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if outcome == "stale_baseline":
                engine_services(engine).workspace_change_tracker.invalidate()
                release.set()
            await task
        elapsed = loop.time() - attempted.result()

    (terminal,) = events
    assert terminal.workspace_update is None
    assert terminal.error is not None
    if outcome == "timeout":
        lock_path = registry.path.with_suffix(".json.lock")
        assert 0.99 <= elapsed < 1.5
        assert terminal.error.code == "workspace_selection_failed"
        assert terminal.error.message == f"Workspace list could not be saved: Timed out acquiring lock {lock_path}"
    elif outcome == "cancel":
        assert terminal.error.code == "workspace_change_cancelled"
    else:
        assert terminal.error.message == "Workspace changes changed during preparation."
    (candidate,) = discarded
    assert candidate.loaded is not original
    assert engine.current.require_loaded() is original
    assert registry.path.read_bytes() == before


async def test_cancelling_at_registry_publication_keeps_the_renamed_runtime(workspace_case, tmp_path, monkeypatch):
    engine, workspace = await workspace_case.start_selected(tmp_path)
    registry = workspace_case.registry
    original = engine.current.require_loaded()
    write = registry_module.atomic_write_text
    published = False

    def cancel_after_write(path, text):
        nonlocal published
        write(path, text)
        if path == registry.path and not published:
            published = True
            task = asyncio.current_task()
            assert task is not None
            task.cancel()

    monkeypatch.setattr(registry_module, "atomic_write_text", cancel_after_write)
    task = asyncio.create_task(engine.event_bus.publish(_rename_workspace(workspace)))
    with pytest.raises(asyncio.CancelledError):
        await task

    saved = registry.snapshot().workspace(workspace.id)
    assert saved is not None and saved.name == "Renamed"
    assert engine.session.session_workspace == saved
    assert engine.current.require_loaded() is not original


async def test_cancelling_while_recording_use_does_not_wait_and_still_announces(workspace_case, tmp_path, monkeypatch):
    primary, target_primary = make_workdirs(tmp_path, "team", "other")
    engine, _ = await workspace_case.start_selected(primary)
    target = workspace_case.registry.create("Other", str(target_primary))
    entered, release = threading.Event(), threading.Event()
    abandonable = engine_workspaces.run_abandonable

    def held_isdir(path):
        entered.set()
        release.wait(ENGINE_TURN_TIMEOUT)
        return os.path.isdir(path)

    async def hold_use_check(name, fn, *args):
        if fn is os.path.isdir and args == (str(target_primary),):
            return await abandonable(name, held_isdir, *args)
        return await abandonable(name, fn, *args)

    monkeypatch.setattr(engine_workspaces, "run_abandonable", hold_use_check)
    try:
        async with capture_event_sequence(engine.event_bus, WorkspaceUpdated) as events:
            task = asyncio.create_task(engine.event_bus.publish(workspace_selection(target.id)))
            await wait_for(entered.is_set)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert not release.is_set()
            assert engine.session.session_workspace == target
            assert [event.workspace for event in events if isinstance(event, WorkspaceUpdated)][-1] == target
    finally:
        release.set()


async def test_switch_plans_reference_folders_off_the_event_loop(workspace_case, tmp_path, monkeypatch):
    primary, target_primary, reference = make_workdirs(tmp_path, "team", "other", "slow-mount")
    engine, _ = await workspace_case.start_selected(primary)
    target = workspace_case.registry.create("Other", str(target_primary), [str(reference)])
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    on_loop: list[bool] = []
    resolve = engine_workspaces.resolve_workspace_directory

    def held_resolve(path):
        if path == str(reference):
            on_loop.append(threading.get_ident() == loop_thread)
            entered.set()
            release.wait(ENGINE_TURN_TIMEOUT)
        return resolve(path)

    monkeypatch.setattr(engine_workspaces, "resolve_workspace_directory", held_resolve)
    try:
        task = asyncio.create_task(engine.event_bus.publish(workspace_selection(target.id)))
        await wait_for(lambda: entered.is_set() or task.done())
        assert entered.is_set()
        # The loop keeps answering while the reference folder does not.
        await asyncio.wait_for(asyncio.sleep(0.01), 1)
        assert on_loop == [False]
        release.set()
        await asyncio.wait_for(task, ENGINE_TURN_TIMEOUT)
    finally:
        release.set()
    assert engine.session.session_workspace == target
    assert [directory.path for directory in engine.workspace.working_dirs] == [str(reference)]


def _last_user_text(client: MockChatClient) -> str:
    """The newest user message of the newest request, where catalogs ride."""
    messages, _options = client.call_history[-1]
    return [message.text for message in messages if message.role == "user"][-1]


@pytest.mark.parametrize("operation", ["rename", "delete"])
async def test_failed_open_after_edit_reports_that_the_definition_was_saved(
    workspace_case, tmp_path, monkeypatch, operation
):
    engine, workspace = await workspace_case.start_selected(tmp_path)
    runtime = engine.current.require_loaded()
    monkeypatch.setattr(
        engine.loader, "install", create_autospec(engine.loader.install, side_effect=RuntimeError("opening failed"))
    )
    request = (
        WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY, delete_workspace=workspace)
        if operation == "delete"
        else _rename_workspace(workspace)
    )
    async with capture_event_sequence(engine.event_bus, Error) as errors:
        await engine.event_bus.publish(request)
    (reported,) = errors
    assert reported.code == "workspace_membership_saved_open_failed"
    english, chinese = {
        "rename": (
            "The definition of workspace “Renamed” was saved, but applying it to this session failed: opening failed",
            "工作区“Renamed”的定义已保存",
        ),
        "delete": (
            "Workspace “Team” was deleted, but applying it to this session failed: opening failed",
            "工作区“Team”已删除",
        ),
    }[operation]
    assert reported.message == english
    assert reported.display_message is not None
    assert Localizer("zh-Hans").render(reported.display_message).startswith(chinese)
    saved = workspace_case.registry.snapshot().workspace(workspace.id)
    assert (saved.name if saved is not None else None) == ("Renamed" if operation == "rename" else None)
    assert engine.current.require_loaded() is runtime


@pytest.mark.parametrize("entry", ["chdir", "restore"])
async def test_failed_open_reports_that_implicit_departure_was_saved(workspace_case, tmp_path, monkeypatch, entry):
    primary, destination = make_workdirs(tmp_path, "primary", "destination")
    registry = workspace_case.registry
    engine, workspace = await workspace_case.start_selected(primary)
    await ask(engine, "save the session")
    sid = engine.session_id
    runtime = engine.current.require_loaded()
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
    if entry == "chdir":
        assert f"This run is at {primary}" in reported.message
        assert engine.current.loaded is runtime
        assert engine.session_id == sid
        await ask(engine, "continue in the old workspace")
        assert engine.session.turn_number == 2
    else:
        assert "There is no active session in this run" in reported.message
        assert engine.current.loaded is None
        assert engine.session_id is None


@pytest.mark.parametrize("name", ["Team", "Unspecified", 'Unspecified (left workspace "Team")'])
async def test_a_recorded_workspace_departure_is_not_added_to_the_next_runtime_reminder(workspace_case, tmp_path, name):
    primary, other = make_workdirs(tmp_path, "primary", "other")
    registry = workspace_case.registry
    workspace = registry.create("Team" if name == "Unspecified" else name, str(primary))
    if name == "Unspecified":
        # Definitions written before this name was reserved can still be opened and left.
        data = json.loads(await asyncio.to_thread(registry.path.read_bytes))
        data["workspaces"][0]["name"] = name
        await asyncio.to_thread(registry.path.write_text, json.dumps(data), encoding="utf-8")
    client = MockChatClient(
        responses=[
            MockResponse(text="first"),
            MockResponse(tool_calls=[("sleep", "wait-1", {"seconds": 0})]),
            MockResponse(text="answer"),
        ]
    )
    engine = await workspace_case(primary, workspace_id=workspace.id, client=client)
    await ask(engine, "first turn")
    sid = engine.session_id
    await engine.event_bus.publish(
        WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY), raise_handler_errors=True
    )

    notice = f'left workspace "{name}"'
    await ask(engine, "after leaving")
    assert _last_user_text(client).count(notice) == 1
    await ask(engine, "same directory")
    assert "[Runtime Environment]" not in _last_user_text(client)
    await engine.event_bus.publish(WorkspaceChange(primary_cwd=str(other)), raise_handler_errors=True)
    await ask(engine, "after changing directory")
    assert "[Runtime Environment]" in _last_user_text(client)
    assert notice not in _last_user_text(client)
    messages, _ = client.call_history[-1]
    assert sum(message.text.count(notice) for message in messages if message.role == "user") == 1

    await engine.shutdown()
    reopened = await workspace_case(other, client=client)
    await reopened.event_bus.publish(SessionRestore(session_id=sid), raise_handler_errors=True)
    await ask(reopened, "after reopen")
    assert notice not in _last_user_text(client)


@pytest.mark.parametrize("recorded", [False, True], ids=["before-recording", "after-recording"])
async def test_discarding_a_cancelled_turn_allows_the_workspace_departure_to_be_reported_again(
    workspace_case, tmp_path, monkeypatch, recorded
):
    primary, other = make_workdirs(tmp_path, "primary", "other")
    workspace = workspace_case.registry.create("Team", str(primary))
    client = MockChatClient(
        responses=[MockResponse(text="first"), MockResponse(text="answer", delay=30 if recorded else 0)]
    )
    engine = await workspace_case(primary, workspace_id=workspace.id, client=client)
    await ask(engine, "first turn")
    await engine.event_bus.publish(
        WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY), raise_handler_errors=True
    )
    prepared = asyncio.Event()
    release = asyncio.Event()

    async def hold_before_request(prompt: str, *, keep: bool = False):
        prepared.set()
        await release.wait()
        return ()

    with monkeypatch.context() as patch:
        if not recorded:
            middleware = engine.current.require_loaded().reminder_middleware
            patch.setattr(middleware, "refresh_extension_context", hold_before_request)
        try:
            await engine.event_bus.publish(UserMessage(text="cancel this turn"), raise_handler_errors=True)
            if recorded:
                await wait_for(lambda: client.call_count == 2, timeout=ENGINE_TURN_TIMEOUT, description="model call")
                assert 'left workspace "Team"' in _last_user_text(client)
            else:
                await asyncio.wait_for(prepared.wait(), timeout=ENGINE_TURN_TIMEOUT)
                assert client.call_count == 1
            await engine.event_bus.publish(UserInterrupt(), raise_handler_errors=True)
        finally:
            release.set()
        await drain(engine)

    (cancelled,) = [message for message in engine.history_messages if message.text == "cancel this turn"]
    items = cancelled.additional_properties.get(HistoryMarkerKind.SYSTEM_REMINDERS_KEY, [])
    assert any('left workspace "Team"' in item["text"] for item in items) == recorded
    await engine.event_bus.publish(WorkspaceChange(primary_cwd=str(other)), raise_handler_errors=True)
    await ask(engine, "after cancelling")
    assert all(message.text != "cancel this turn" for message in engine.history_messages)
    assert 'left workspace "Team"' in _last_user_text(client)


@pytest.mark.parametrize("transition", ["reopen", "rollback"])
async def test_workspace_departure_follows_the_restored_runtime_reminder(workspace_case, tmp_path, transition):
    workspace = workspace_case.registry.create("Team", str(tmp_path))
    client = MockChatClient(responses=[MockResponse(text="answer")])
    engine = await workspace_case(tmp_path, workspace_id=workspace.id, client=client)
    await ask(engine, "show the workspace")
    assert "  Workspace: Team" in _last_user_text(client).splitlines()

    leave = WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY)
    await engine.event_bus.publish(leave, raise_handler_errors=True)
    if transition == "reopen":
        session_id = engine.session_id
        await engine.shutdown()
        engine = await workspace_case(tmp_path, client=client)
        await engine.event_bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
    else:
        await ask(engine, "show the departure")
        assert 'left workspace "Team"' in _last_user_text(client)
        await engine.event_bus.publish(UserRollback(target_turn=1), raise_handler_errors=True)

    assert engine.session.workspace_id is None
    await ask(engine, "show the departure from the workspace still in view")
    assert 'left workspace "Team"' in _last_user_text(client)


async def test_workspace_departure_is_not_reported_for_a_workspace_the_model_never_saw(workspace_case, tmp_path):
    workspace = workspace_case.registry.create("Team", str(tmp_path))
    client = MockChatClient(responses=[MockResponse(text="answer")])
    engine = await workspace_case(tmp_path, client=client)
    await ask(engine, "show the unassigned environment")
    assert "  Workspace: Team" not in _last_user_text(client)

    await engine.event_bus.publish(workspace_selection(workspace.id), raise_handler_errors=True)
    assert engine.session.workspace_id == workspace.id
    await engine.event_bus.publish(
        WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY), raise_handler_errors=True
    )
    await ask(engine, "still unassigned")
    assert "left workspace" not in _last_user_text(client)
