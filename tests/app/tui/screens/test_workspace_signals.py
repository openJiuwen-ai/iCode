# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace choices through the main screen and its directory dialogs."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker
from chrys.app.tui.screens.main.session_handlers import RestoreRequest
from chrys.foundation.events import types as ev
from chrys.kernel import Message
from chrys.service.workspaces import registry as registry_module
from tests.app.tui.behaviors.test_missing_working_dir_flows import _top_screen
from tests.app.tui.screens.test_prompt_shortcut_send import running_session, send_plain, turn_finished, wait_ready
from tests.support.async_tasks import wait_for_completion, wait_for_started
from tests.support.event_capture import capture_events
from tests.support.toasts import shown_toasts
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for
from tests.support.workspace_engine import WorkspaceBoundary, select_workspace
from tests.support.workspace_engine import workspace_case as workspace_case
from tests.support.workspace_registry import make_workdirs


def _dialog_text(dialog: ConfirmDialog) -> str:
    return str(dialog.query_one("#confirm-message", Static).content)


async def _select(session, workspace_id):
    await select_workspace(session.bus, workspace_id)
    await wait_ready(session)


async def test_declining_an_old_restore_leaves_the_newer_loading_dialog_open(
    workspace_case, agent_engine, tmp_path, monkeypatch
):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        main = session.main
        handler = main._sessions
        registry = workspace_case.attach(session.engine).registry
        primary, target = make_workdirs(tmp_path, "primary", "target")
        workspace = registry.create("Team", str(primary))
        registry.set_session_workspace("saved", workspace.id)
        store = handler.state_store
        assert store is not None
        await store.save_session("newer", {"messages": [Message("user", ["newer history"])]}, primary_cwd=str(target))

        old_confirmation, new_loading = WorkspaceBoundary(), WorkspaceBoundary()

        async def decline_workspace(*_args):
            await old_confirmation.wait()
            return False, None

        monkeypatch.setattr(
            handler, "_callbacks", replace(handler._callbacks, confirm_workspace_leave=decline_workspace)
        )
        begin = main._events.begin_session_restore_load

        async def hold_loading(session_id: str) -> None:
            await begin(session_id)
            await new_loading.wait()

        monkeypatch.setattr(main._events, "begin_session_restore_load", hold_loading)
        old = asyncio.create_task(handler.do_session_restore("saved", primary_cwd=str(target)))
        newer = None
        try:
            await wait_for_started(old_confirmation.entered, old, description="old restore waits for confirmation")
            newer = asyncio.create_task(handler.do_session_restore("newer"))
            await wait_for_started(new_loading.entered, newer, description="newer restore opens its dialog")
            dialog = main._events._agent_load_dialog
            assert dialog is not None and session.app.screen is dialog

            old_confirmation.open()
            assert await wait_for_completion(old) is RestoreRequest.DECLINED
            assert main._events._agent_load_dialog is dialog and session.app.screen is dialog
            assert main._state.run.agent_loading
            assert main._state.session.restoring_session

            new_loading.open()
            assert await wait_for_completion(newer) is RestoreRequest.REQUESTED
            assert session.engine.session_id == "newer"
            assert registry.snapshot().workspace_id_for_session("saved") == workspace.id
            assert not main._state.run.agent_loading
            assert not main._state.session.restoring_session
        finally:
            old_confirmation.open()
            new_loading.open()
            await asyncio.gather(*(task for task in (old, newer) if task is not None), return_exceptions=True)


@pytest.mark.parametrize(
    ("entry", "button"),
    [("chdir", "confirm-yes"), ("subtitle", "confirm-no"), ("chdir", "confirm-alternate")],
)
async def test_directory_entries_confirm_workspace_departure(
    workspace_case,
    agent_engine,
    tmp_path,
    monkeypatch,
    entry,
    button,
):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        engine, main, pilot = session.engine, session.main, session.pilot
        registry = workspace_case.attach(engine).registry
        primary, target = make_workdirs(tmp_path, "primary", "target")
        workspace = registry.create("Team", str(primary))
        await _select(session, workspace.id)
        await send_plain(session, "keep this conversation")
        await turn_finished(session)
        sid = engine.session_id
        session.bar.value = "draft kept"
        if entry == "subtitle":
            main._workspace_actions.open_working_dir_picker()
            (await _top_screen(pilot, FilePicker)).dismiss(str(target))
        else:
            main._workspace_actions.start_chdir(str(target))
        dialog = await _top_screen(pilot, ConfirmDialog)
        assert dialog._title == "Leave workspace?"
        message = _dialog_text(dialog)
        assert "Team" in message and str(target) in message
        assert registry.snapshot().workspace_id_for_session(sid) == workspace.id
        await click_when_settled(pilot, f"#{button}")
        await wait_for(
            lambda: (
                session.app.screen is main and (button == "confirm-no" or engine.workspace.primary_cwd == str(target))
            ),
            pilot=pilot,
            description="workspace choice is applied",
        )
        if button == "confirm-yes":
            assert engine.session_id == sid
            assert engine.workspace.id == ""
            assert registry.snapshot().workspace_id_for_session(sid) is None
        elif button == "confirm-no":
            assert engine.session_id == sid
            assert engine.workspace.id == workspace.id
            assert registry.snapshot().workspace_id_for_session(sid) == workspace.id
        else:
            assert engine.session_id != sid
            assert engine.history_messages == []
            assert registry.snapshot().workspace_id_for_session(sid) == workspace.id
            await session.bus.publish(ev.SessionRestore(session_id=sid), raise_handler_errors=True)
            assert engine.session_id == sid
            assert engine.workspace.primary_cwd == str(primary)
        assert any(message.text == "keep this conversation" for message in engine.history_messages)
        if button != "confirm-alternate":
            assert session.bar.value == "draft kept"


async def test_membership_failure_uses_acknowledgement_dialog(workspace_case, agent_engine, tmp_path, monkeypatch):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        engine, main, pilot = session.engine, session.main, session.pilot
        registry = workspace_case.attach(engine).registry
        workspace = registry.create("Team", engine.workspace.primary_cwd)
        await _select(session, workspace.id)
        outside = tmp_path / "outside"
        outside.mkdir()

        def refuse(*args, **kwargs):
            raise OSError("membership disk full")

        monkeypatch.setattr(registry_module, "atomic_write_text", refuse)
        worker = main._start_worker(
            lambda: session.bus.publish(ev.WorkspaceChange(primary_cwd=str(outside), require_leave_confirmation=False))
        )
        dialog = await _top_screen(pilot, ConfirmDialog)
        message = _dialog_text(dialog)
        assert "Team" in message and "membership disk full" in message
        assert dialog._cancel_label is None
        await pilot.press("escape")
        await worker.wait()
        assert session.app.screen is main
        assert engine.workspace.primary_cwd == str(outside)
        assert registry.snapshot().workspace_id_for_session(engine.session_id) == workspace.id


async def test_empty_tui_same_directory_keeps_definition_until_reselected(
    workspace_case, agent_engine, tmp_path, monkeypatch
):
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        engine, main = session.engine, session.main
        registry = workspace_case.attach(engine).registry
        primary = engine.workspace.primary_cwd
        first, second = make_workdirs(tmp_path, "first", "second")
        workspace = registry.create("Original", primary, [str(first)])
        await _select(session, workspace.id)
        session.bar.value = "unsubmitted draft"
        registry.update(workspace.id, name="Renamed", primary=primary, folders=[str(second)])

        worker = main._workspace_actions.start_chdir(primary)
        if worker is not None:
            await worker.wait()
        assert main._state.workspace.workspace_name == "Original"
        assert [directory.path for directory in engine.workspace.working_dirs] == [str(first)]

        await _select(session, workspace.id)
        assert main._state.workspace.workspace_name == "Renamed"
        assert [directory.path for directory in engine.workspace.working_dirs] == [str(second)]
        assert session.bar.value == "unsubmitted draft"
        assert engine.history_messages == []


async def test_tui_shows_a_missing_workspace_reference_on_each_load(
    workspace_case, agent_engine, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    async with running_session(agent_engine, tmp_path, monkeypatch) as session:
        registry = workspace_case.attach(session.engine).registry
        primary, available, missing, also_missing = make_workdirs(
            tmp_path, "primary", "available", "missing", "also-missing"
        )
        workspace = registry.create(
            "Project workspace", str(primary), [str(available), str(missing), str(also_missing)]
        )
        missing.rmdir()
        also_missing.rmdir()
        warnings = await capture_events(session.bus, ev.Warning)

        for load_count in (1, 2):
            await _select(session, workspace.id)
            assert sum(event.code == "workspace_reference_missing" for event in warnings) == load_count
            await session.pilot.pause()
            notices = [
                toast.message
                for toast in shown_toasts(session.app)
                if toast.severity == "warning" and workspace.name in toast.message and str(missing) in toast.message
            ]
            assert len(notices) == load_count
            assert str(also_missing) in notices[-1]
            assert [directory.path for directory in session.engine.workspace.working_dirs] == [str(available)]
            if load_count == 1:
                for text in ("first turn in the workspace", "second turn in the workspace"):
                    await send_plain(session, text, typed=False)
                    await turn_finished(session)
                    assert sum(event.code == "workspace_reference_missing" for event in warnings) == 1
                await _select(session, registry.create("Other workspace", str(primary)).id)
