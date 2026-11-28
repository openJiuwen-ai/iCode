# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Main-screen workspace wiring shares services and preserves focus and literal failures."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.content import Content
from textual.widgets import Button, OptionList, Static, TabbedContent, TextArea, Tree
from textual.widgets._toast import Toast

from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.file_picker import FilePicker
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.app.tui.screens.workspaces.edit import WorkspaceEditDialog
from chrys.app.tui.screens.workspaces.picker import WorkspacesScreen
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets import EnhancedInput
from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.messages import UserMessage as ChatUserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.workspaces import WorkspacePanel
from chrys.foundation.events.types import (
    BackgroundTasksChanged,
    SessionNew,
    SessionReady,
    UserMessage,
    WorkspaceChange,
    WorkspaceChangeFinished,
    WorkspaceOperation,
    WorkspaceUpdated,
)
from chrys.foundation.models.background_tasks import BackgroundTaskView
from chrys.foundation.models.workspace import WorkingDir
from chrys.service.workspaces import registry as workspace_registry
from tests.support.async_tasks import running_task
from tests.support.event_capture import capture_events
from tests.support.pilot_barrier import screen_is_settled
from tests.support.rollback_spec import drain
from tests.support.symlinks import symlink_or_skip
from tests.support.tui_helpers import (
    click_when_settled,
    resize_when_settled,
)
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    wait_for,
    with_wait_deadline,
)
from tests.support.workspace_registry import retarget_workdir_alias


def _picker_ready(app) -> bool:
    screen = app.screen
    return (
        isinstance(screen, WorkspacesScreen) and screen.is_mounted and bool(screen.query_one(OptionList).option_count)
    )


async def _choose_row_action(pilot, workspace, text: str) -> None:
    app = pilot.app
    await wait_for(lambda: _picker_ready(app), pilot=pilot)
    options = app.screen.query_one(OptionList)
    options.highlighted = options.get_option_index(workspace.id)
    options.scroll_to_highlight()
    await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
    for line in range(options.content_region.height):
        rendered = options.render_line(line).text
        if workspace.name in rendered and text in rendered:
            x = options.content_region.x - options.region.x + cell_len(rendered[: rendered.rindex(text)])
            await click_when_settled(pilot, options, offset=(x, line))
            return
    raise AssertionError(f"Workspace row has no {text} action")


async def _open_workspace_editor(pilot, main, workspace) -> None:
    main._workspace_picker.open()
    await _choose_row_action(pilot, workspace, "Edit")
    await wait_for(
        lambda: isinstance(pilot.app.screen, WorkspaceEditDialog) and pilot.app.screen.is_mounted, pilot=pilot
    )


async def _close_workspace_picker(pilot, main) -> None:
    await wait_for(lambda: _picker_ready(pilot.app), pilot=pilot)
    await pilot.press("escape")
    await wait_for(lambda: pilot.app.screen is main and not main._workspace_picker._open, pilot=pilot)


@pytest.mark.usefixtures("frozen_earlier_heap")
async def test_populated_main_workspace_overlays_do_not_restyle_recompose_or_relayout(workspace_main, monkeypatch):
    app, pilot, main = workspace_main
    panel = main.query_one(ChatPanel)
    transcript = []
    for index in range(24):
        transcript.extend(
            (
                ChatUserMessage(f"Question {index}\nwith a second line"),
                AgentMessage(f"## Answer {index}\n\nA paragraph with `code` and **emphasis**."),
            )
        )
    await panel.mount(*transcript)
    await wait_for(
        lambda: (
            len(panel.walk_children()) >= 96 and all(markdown.source for markdown in panel.query(VirtualizedMarkdown))
        ),
        pilot=pilot,
        description="populated workspace underlay",
    )
    composer = main.query_one(InputBar)
    composer.focus_input()
    await wait_for(lambda: composer.query_one(TextArea).has_focus and screen_is_settled(app, main), pilot=pilot)

    styles = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
    layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
    footer = main.query_one(ChrysFooter)
    recompose = create_autospec(footer.recompose, side_effect=footer.recompose)
    monkeypatch.setattr(main, "update_node_styles", styles)
    monkeypatch.setattr(main, "_refresh_layout", layout)
    monkeypatch.setattr(footer, "recompose", recompose)

    await click_when_settled(pilot, "#workspace-tag")
    await _close_workspace_picker(pilot, main)
    await click_when_settled(pilot, "#workspace-tag")
    current = main._state.workspace.membership
    assert current is not None
    await _choose_row_action(pilot, current, "Edit")
    await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
    await pilot.press("escape")
    await _close_workspace_picker(pilot, main)
    await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)

    styles.assert_not_called()
    layout.assert_not_called()
    recompose.assert_not_called()


async def test_workspace_tag_resize_and_refresh_do_not_probe_directories(workspace_main, tmp_path, monkeypatch):
    _app, pilot, main = workspace_main
    current = main._state.workspace.membership
    assert current is not None
    reference = str(tmp_path / "missing")
    folders = (current.primary_cwd, reference)
    definition = replace(current, working_dirs=[WorkingDir(reference)])
    main._state.workspace.membership = definition
    status = main.query_one(StatusBar)
    tag = status.query_one("#workspace-tag", Static)
    loop_thread = threading.get_ident()
    folder_calls: list[tuple[str, str, int]] = []

    # Labelled by hand: Windows ``os.path.isdir`` is ``nt._path_isdir``, so ``__name__`` differs per platform.
    def track(name, probe):
        def tracked(path, *args, **kwargs):
            if path in folders:
                folder_calls.append((name, path, threading.get_ident()))
            return probe(path, *args, **kwargs)

        return tracked

    monkeypatch.setattr(os, "stat", track("stat", os.stat))
    monkeypatch.setattr(os, "lstat", track("lstat", os.lstat))
    monkeypatch.setattr(os.path, "isdir", track("isdir", os.path.isdir))

    main._refresh_workspace()
    await wait_for(
        lambda: isinstance(tag.tooltip, Content) and f"{reference} (missing)" in tag.tooltip.plain, pilot=pilot
    )
    assert {path for name, path, thread in folder_calls if name == "isdir" and thread != loop_thread} == set(folders)
    folder_calls.clear()

    await resize_when_settled(pilot, 60, 36)
    status.set_profile("Other agent")
    main._state.workspace.membership = replace(definition, name="Renamed")
    main._refresh_workspace()
    assert isinstance(tag.tooltip, Content) and tag.tooltip.plain.startswith("Renamed\n")
    assert [(name, path) for name, path, thread in folder_calls if thread == loop_thread] == []


async def test_workspace_new_reports_the_save_without_interrupting_f1(live_workspace_chat, monkeypatch):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    app = pilot.app
    composer = main.query_one(InputBar)
    composer.value = "An existing conversation"
    await composer.action_submit()
    await wait_for(lambda: main.has_messages_state, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    await drain(engine)

    entered, release = threading.Event(), threading.Event()
    ui_registry = main._services.workspaces
    create = ui_registry.create

    def held_create(name, primary, folders=()):
        entered.set()
        if not release.wait(ENGINE_TURN_TIMEOUT):
            raise TimeoutError("test did not release the workspace save")
        return create(name, primary, folders)

    monkeypatch.setattr(ui_registry, "create", create_autospec(create, side_effect=held_create))
    composer.value = "/workspace new Late"
    await composer.action_submit()
    try:
        await wait_for(
            entered.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="the workspace save starts"
        )
        await pilot.press("f1")
        await wait_for(lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted, pilot=pilot)
        sessions = app.screen
    finally:
        release.set()
    message = "Created workspace “Late”. You can enter it from the workspace picker."

    def reported() -> bool:
        return any(item.message == message for item in app._notifications)

    await wait_for(
        lambda: reported() or (isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted),
        pilot=pilot,
    )
    assert app.screen is sessions
    assert not any(isinstance(screen, ConfirmDialog) for screen in app.screen_stack)
    assert reported()
    snapshot = await asyncio.to_thread(registry.snapshot)
    assert any(workspace.name == "Late" for workspace in snapshot.workspaces)
    assert snapshot.workspace_id_for_session(engine.session_id) == current.id


async def test_workspace_tag_click_opens_the_picker_unless_the_agent_is_busy(workspace_main):
    """the tag opens the picker on the main screen's workspaces; while loading it says why not."""
    from textual.widgets import OptionList

    from tests.support.tui_helpers import click_when_settled

    app, pilot, main = workspace_main
    tag = main.query_one(StatusBar).query_one("#workspace-tag")
    await click_when_settled(pilot, tag)
    await wait_for(lambda: _picker_ready(app), pilot=pilot)
    options = app.screen.query_one(OptionList)
    assert main._state.workspace.workspace_id in [option.id for option in options.options]
    await pilot.press("escape")
    await wait_for(lambda: app.screen is main, pilot=pilot)
    before = len(app._notifications)
    main._set_agent_loading(True)
    try:
        await click_when_settled(pilot, tag)
        await wait_for(lambda: len(app._notifications) > before, pilot=pilot)
        assert [notice.message for notice in app._notifications][before:] == [
            "Cannot change workspace while agent is busy"
        ]
        assert app.screen is main
        assert not any(isinstance(screen, WorkspacesScreen) for screen in app.screen_stack)
    finally:
        main._set_agent_loading(False)


@pytest.mark.parametrize("entry", ["tag", "slash"])
@pytest.mark.parametrize("close_early", [False, True], ids=["marks-missing", "discards-after-escape"])
async def test_slow_primary_does_not_block_picker_rows(workspace_main, tmp_path, monkeypatch, entry, close_early):
    app, pilot, main = workspace_main
    registry = main._services.workspaces
    current = main._state.workspace.membership
    assert current is not None
    path, reference = tmp_path / "slow", tmp_path / "reference"
    await asyncio.to_thread(path.mkdir)
    await asyncio.to_thread(reference.mkdir)
    other = await asyncio.to_thread(registry.create, "Other workspace", str(path), [str(reference)])
    main._refresh_workspace()
    await wait_for(
        lambda: main._workspace_snapshot is not None and main._workspace_snapshot.workspace(other.id) == other,
        pilot=pilot,
    )

    panel = main.query_one(ChatPanel)
    await panel.mount(
        *(row for i in range(24) for row in (ChatUserMessage(f"Question {i}"), AgentMessage(f"## Answer {i}")))
    )
    await wait_for(lambda: screen_is_settled(app, main), pilot=pilot)

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    isdir = os.path.isdir
    check_on_loop = []

    # Windows ``os.path.isdir`` does not go through ``os.stat``, so the folder check itself is held.
    def held_isdir(target):
        if os.fspath(target) == str(path):
            if threading.get_ident() == loop_thread:
                check_on_loop.append(target)
            elif not entered.is_set():
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("test did not release picker folder check")
                finished.set()
                return False
        return isdir(target)

    monkeypatch.setattr(os.path, "isdir", held_isdir)
    layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
    restyle = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
    monkeypatch.setattr(main, "_refresh_layout", layout)
    monkeypatch.setattr(main, "update_node_styles", restyle)

    def lines(options):
        return [options.render_line(y).text.strip() for y in range(options.content_region.height)]

    warning = "Primary folder missing"
    try:
        if entry == "tag":
            await click_when_settled(pilot, main.query_one(StatusBar).query_one("#workspace-tag"))
        else:
            composer = main.query_one(InputBar)
            composer.value = "/workspace switch"
            await composer.action_submit()
        await wait_for(lambda: entered.is_set() or check_on_loop, pilot=pilot)
        assert not check_on_loop
        picker = app.screen
        assert isinstance(picker, WorkspacesScreen) and picker.is_mounted
        options = picker.query_one(OptionList)
        await wait_for(lambda: any(line.endswith(" · 1 reference folder") for line in lines(options)), pilot=pilot)
        assert [row.id for row in options.options] == [current.id, other.id, "unspecified", "create"]
        assert any(current.name in line for line in lines(options))
        assert any(other.name in line for line in lines(options))
        assert not any(warning in line for line in lines(options))
        layout.assert_not_called()
        restyle.assert_not_called()
        if close_early:
            prompt = options.get_option(other.id).prompt
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main and not main._workspace_picker._open, pilot=pilot)
            main._workspace_picker.open()
            await wait_for(
                lambda: _picker_ready(app) and app.screen is not picker,
                pilot=pilot,
            )
            reopened = app.screen.query_one(OptionList)
            reopened_prompt = reopened.get_option(other.id).prompt
    finally:
        release.set()

    await wait_for(finished.is_set, pilot=pilot)
    if close_early:
        await pilot.pause()
        assert picker not in app.screen_stack
        assert options.get_option(other.id).prompt is prompt
        assert reopened.get_option(other.id).prompt is reopened_prompt
        assert not any(warning in line for line in lines(reopened))
    else:
        await wait_for(lambda: any(warning in line for line in lines(options)), pilot=pilot)
    await pilot.press("escape")
    await wait_for(lambda: app.screen is main and not main._workspace_picker._open, pilot=pilot)


@pytest.mark.parametrize("entry", ["sidebar-edit", "switch-name"])
@pytest.mark.parametrize("return_to_main", [False, True])
async def test_prepared_workspace_dialog_does_not_open_after_sessions_overlay(
    live_workspace_chat, monkeypatch, entry, return_to_main
):
    pilot, main, engine, _, current, target = live_workspace_chat
    app = pilot.app
    registry = main._services.workspaces
    main._state.run.has_messages = True
    entered, release = threading.Event(), threading.Event()
    snapshot = registry.snapshot

    def hold_first_snapshot():
        if not entered.is_set():
            entered.set()
            if not release.wait(ENGINE_TURN_TIMEOUT):
                raise TimeoutError("test did not release workspace snapshot")
        return snapshot()

    monkeypatch.setattr(registry, "snapshot", hold_first_snapshot)
    try:
        if entry == "sidebar-edit":
            main.query_one(WorkspacePanel).post_message(WorkspacePanel.EditRequested(target))
            worker_name = "_edit"
        else:
            main._workspace_picker.run_command("switch Target")
            worker_name = "_select_name"
        await wait_for(entered.is_set, pilot=pilot)
        (worker,) = [worker for worker in app.workers if worker.name == worker_name]
        assert not worker.is_finished
        await pilot.press("f1")
        await wait_for(lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted, pilot=pilot)
        overlay = app.screen
        if return_to_main:
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)
    finally:
        release.set()

    expected = main if return_to_main else overlay
    await wait_for(lambda: worker.is_finished or app.screen is not expected, pilot=pilot)
    assert app.screen is expected and worker.is_finished
    await worker.wait()
    assert engine.workspace == current


@pytest.mark.parametrize(
    ("entry", "choice"),
    [
        ("picker", "move"),
        ("picker", "new"),
        ("picker", "cancel"),
        ("picker", "empty"),
        ("unspecified", "new"),
        ("name", "move"),
        ("cd", "move"),
        ("cd", "new"),
        ("cd", "cancel"),
        ("cd", "empty"),
        ("cd", "empty-cancel"),
        ("cd-primary", "same"),
        ("directory", "empty-cancel"),
        ("cd-unassigned", "move"),
        ("missing-new", "new"),
    ],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_switch_acts_on_the_selected_chat_and_folder(live_workspace_chat, monkeypatch, entry, choice):
    pilot, main, engine, registry, current, target = live_workspace_chat
    app = pilot.app
    composer = main.query_one(InputBar)
    if entry == "cd-unassigned":
        await engine.event_bus.publish(WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY))
    empty = choice.startswith("empty")
    if not empty and entry != "cd-primary":
        composer.value = "An existing conversation"
        await composer.action_submit()
        await wait_for(lambda: main.has_messages_state, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        await drain(engine)
    before = main.chat_session_id
    before_workspace = None if entry == "cd-unassigned" else current.id
    assert before
    picker_id = None
    picker = None
    destination = current.primary_cwd if entry in {"cd-primary", "unspecified"} else target.primary_cwd
    workspace_requests = await capture_events(engine.event_bus, WorkspaceChange)
    missing_primary = entry == "missing-new"
    if missing_primary:
        await asyncio.to_thread(Path(destination).rmdir)
    if entry in {"picker", "unspecified", "missing-new"}:
        composer.value = "/workspace switch"
        await composer.action_submit()
        await wait_for(lambda: _picker_ready(app), pilot=pilot)
        picker = app.screen
        options = picker.query_one(OptionList)
        picker_id = "unspecified" if entry == "unspecified" else target.id
        await pilot.press("home", *("down",) * options.get_option_index(picker_id), "enter")
    elif entry == "directory":
        panel = main.query_one(ChatPanel)
        await click_when_settled(pilot, panel, offset=(panel.region.width - 5, panel.region.height - 1))
        await wait_for(lambda: isinstance(app.screen, FilePicker) and app.screen.is_mounted, pilot=pilot)
        app.screen.dismiss(destination)
    else:
        composer.value = {
            "name": "/workspace switch Target",
            "cd": f"/cd {destination}",
            "cd-primary": f"/cd {destination}",
            "cd-unassigned": f"/cd {destination}",
        }[entry]
        await composer.action_submit()

    if choice == "same" or (empty and entry not in {"cd", "directory"}) or entry == "cd-unassigned":
        if choice == "same":
            await wait_for(
                lambda: (
                    registry.snapshot().workspace_id_for_session(before) == current.id
                    or (isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted)
                ),
                pilot=pilot,
            )
        else:
            await wait_for(
                lambda: (
                    main.chat_workspace_cwd == destination
                    or (isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted)
                ),
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
            )
        assert not isinstance(app.screen, ConfirmDialog)
    elif not missing_primary:
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        dialog = app.screen
        shown = dialog.query_one("#confirm-message", Static).render().plain
        if entry in {"cd", "directory"}:
            title = "Leave workspace “Current”?"
            body = (
                f"{destination} is not in workspace “Current”.\n"
                "• Move this session: change its working directory to this folder and make it Unspecified."
            )
            if not empty:
                body += (
                    "\n• New session: open a new Unspecified session in this folder. "
                    "The current session stays where it is."
                )
        else:
            title, body = _confirmation_copy(
                "unspecified" if entry == "unspecified" else "switch-other", destination, "Current"
            )
        assert dialog.query_one("#confirm-container").border_title == title
        assert shown == body
        assert [button.label.plain for button in dialog.query(Button)] == [
            "Move this session",
            *([] if empty else ["New session"]),
            "Cancel",
        ]
        await click_when_settled(
            pilot,
            {
                "move": "#confirm-yes",
                "new": "#confirm-alternate",
                "cancel": "#confirm-no",
                "empty": "#confirm-yes",
                "empty-cancel": "#confirm-no",
            }[choice],
        )

    stayed = choice in {"cancel", "empty-cancel", "same"} or missing_primary
    if missing_primary:
        await wait_for(
            lambda: any(str(destination) in toast.render().plain for toast in app.screen.query(Toast)), pilot=pilot
        )
        assert not any(isinstance(screen, ConfirmDialog) for screen in app.screen_stack)
    if picker is not None and stayed and choice != "same":
        await wait_for(
            lambda: _picker_ready(app),
            pilot=pilot,
        )
        options = app.screen.query_one(OptionList)
        assert options.get_option_at_index(options.highlighted).id == picker_id
        assert not any(isinstance(screen, WorkspaceEditDialog) for screen in app.screen_stack)
    else:
        await wait_for(lambda: app.screen is main, pilot=pilot)

    expected_cwd = current.primary_cwd if stayed or entry == "unspecified" else destination
    if stayed:
        expected_workspace = before_workspace
    elif entry in {"unspecified", "cd", "cd-unassigned"}:
        expected_workspace = None
    else:
        expected_workspace = target.id
    await wait_for(
        lambda: (
            engine.workspace.primary_cwd == expected_cwd
            and engine.session.workspace_id == expected_workspace
            and main.chat_workspace_cwd == expected_cwd
            and (main.chat_session_id != before) is (choice == "new" and not stayed)
            and main.chat_session_id == engine.session_id
        ),
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    snapshot = registry.snapshot()
    assert snapshot.workspace_id_for_session(before) == (before_workspace if choice == "new" else expected_workspace)
    assert snapshot.workspace_id_for_session(main.chat_session_id) == expected_workspace
    if (entry == "cd" and choice in {"empty", "move"}) or entry == "cd-unassigned":
        # The engine's inline handler installs the workspace before later subscribers run.
        await wait_for(
            lambda: any(event.primary_cwd == destination for event in workspace_requests),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the directory change reaches the event collector",
        )
        changes = [event for event in workspace_requests if event.primary_cwd == destination]
        for event in changes:
            assert event.require_leave_confirmation, (
                "Directory changes must allow the engine to refuse an unconfirmed departure"
            )
            confirmed = entry == "cd" and choice in {"empty", "move"}
            assert (event.confirmation.source_workspace_id if event.confirmation else None) == (
                current.id if confirmed else None
            )
            assert (event.confirmation.primary_cwd if event.confirmation else "") == (destination if confirmed else "")


@pytest.mark.parametrize("entry", ["missing", "after-turn"])
async def test_directory_entries_ask_before_leaving_the_workspace(workspace_main, tmp_path, entry):
    """Directory pickers, including missing-folder recovery, use the same departure dialog."""
    app, pilot, main = workspace_main
    main.has_messages_state = True
    destination = tmp_path.parent / (tmp_path.name + "-chosen")
    destination.mkdir()
    main._set_workspace_cwd(str(tmp_path / "missing"))
    before = (main._state.workspace.workspace_id, main._state.workspace.membership, main._state.workspace.current_cwd)
    if entry == "after-turn":
        main._workspace_actions.check_after_turn()
    else:
        main._workspace_actions.prompt_missing_working_dir("submit")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    await click_when_settled(pilot, app.screen.query_one("#confirm-yes", Button))
    await wait_for(lambda: isinstance(app.screen, FilePicker) and app.screen.is_mounted, pilot=pilot)
    app.screen.dismiss(str(destination))
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    message = str(app.screen.query_one("#confirm-message", Static).render())
    assert "is not in workspace “Workspace”." in message
    await click_when_settled(pilot, app.screen.query_one("#confirm-no", Button))
    await wait_for(lambda: app.screen is main, pilot=pilot)
    await main.workers.wait_for_complete()
    assert (
        main._state.workspace.workspace_id,
        main._state.workspace.membership,
        main._state.workspace.current_cwd,
    ) == before
    assert main._state.workspace.current_cwd != str(destination)


@pytest.mark.parametrize(
    ("locale", "destination_kind", "description", "description_zh"),
    [
        ("en", "outside", "", ""),
        ("en", "primary-child", "inside the primary folder", "位于主文件夹内"),
        ("en", "reference", "a reference folder", "是参考文件夹"),
        ("en", "reference-child", "inside a reference folder", "位于参考文件夹内"),
        ("zh-Hans", "reference-child", "inside a reference folder", "位于参考文件夹内"),
    ],
)
async def test_cd_explains_the_workspace_relationship_for_an_empty_session(
    workspace_main, tmp_path, locale, destination_kind, description, description_zh
):
    app, pilot, main = workspace_main
    app.locale_controller.switch_locale(locale)
    source = main._state.workspace.membership
    assert source is not None
    primary = Path(source.primary_cwd)
    reference = tmp_path.parent / f"{tmp_path.name}-reference"
    primary_child = primary / "primary-child"
    reference_child = reference / "reference-child"
    primary_child.mkdir()
    reference_child.mkdir(parents=True)
    source = replace(source, working_dirs=[WorkingDir(str(reference))])
    await main._services.bus.publish(
        SessionReady(session_id=main.chat_session_id, primary_cwd=str(primary), workspace=source),
        raise_handler_errors=True,
    )

    outside = tmp_path.with_name(f"{tmp_path.name}-outside")
    outside.mkdir()
    target = {
        "outside": outside,
        "primary-child": primary_child,
        "reference": reference,
        "reference-child": reference_child,
    }[destination_kind]
    composer = main.query_one(InputBar)
    composer.value = f"/cd {target}"
    await composer.action_submit()
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    dialog = app.screen
    if locale == "en":
        title = "Leave workspace “Workspace”?"
        relationship = f"{target} is not in workspace “Workspace”."
        if description:
            relationship = (
                f"{target} is already in workspace “Workspace” ({description}).\n"
                "This session can already use it without changing its working directory."
            )
        move = "• Move this session: change its working directory to this folder and make it Unspecified."
        buttons = ["Move this session", "Cancel"]
    else:
        title = "离开工作区“Workspace”？"  # noqa: RUF001 - exact localized prose
        relationship = f"{target} 不在工作区“Workspace”中。"
        if description_zh:
            relationship = (
                f"{target} 已在工作区“Workspace”中（{description_zh}）。\n"  # noqa: RUF001 - exact localized prose
                "当前会话不切换工作目录也能使用它。"
            )
        move = "• 移动这个会话：工作目录改为这个文件夹，变为未指定。"  # noqa: RUF001 - exact localized prose
        buttons = ["移动这个会话", "取消"]
    assert dialog.query_one("#confirm-container").border_title == title
    assert dialog.query_one("#confirm-message", Static).render().plain == f"{relationship}\n{move}"
    assert [button.label.plain for button in dialog.query(Button)] == buttons
    await click_when_settled(pilot, "#confirm-no")
    await wait_for(lambda: app.screen is main, pilot=pilot)


async def test_missing_primary_keeps_picker_path_and_allows_rename(live_workspace_chat):
    pilot, main, engine, registry, current, target = live_workspace_chat
    app = pilot.app
    await asyncio.to_thread(Path(target.primary_cwd).rmdir)
    main._workspace_picker.open()
    await wait_for(lambda: _picker_ready(app), pilot=pilot)
    options = app.screen.query_one(OptionList)
    warning = "Primary folder missing"

    def details():
        return [options.render_line(line).text.strip() for line in range(options.content_region.height)]

    await wait_for(lambda: any(warning in line for line in details()), pilot=pilot)
    options.highlighted = options.get_option_index(target.id)
    await pilot.press("enter")
    notice = f"The primary folder of workspace “Target”, {target.primary_cwd}, does not exist."
    await wait_for(lambda: any(item.message == notice for item in app._notifications), pilot=pilot)
    assert engine.workspace.id == current.id
    await wait_for(lambda: _picker_ready(app), pilot=pilot)
    await pilot.press("escape")
    await wait_for(lambda: app.screen is main and not main._workspace_picker._open, pilot=pilot)
    await _open_workspace_editor(pilot, main, target)
    await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
    app.screen.query_one("#workspace-name", EnhancedInput).value = "Renamed offline workspace"
    await click_when_settled(pilot, "#workspace-save")
    await _close_workspace_picker(pilot, main)
    assert registry.snapshot().workspace(target.id).name == "Renamed offline workspace"


@pytest.mark.parametrize(("missing", "locale"), [(False, "en"), (True, "en"), (True, "zh-Hans")])
async def test_entering_workspace_toasts_once_about_missing_references(live_workspace_chat, tmp_path, missing, locale):
    pilot, main, _engine, registry, _current, target = live_workspace_chat
    pilot.app.locale_controller.switch_locale(locale)
    path = tmp_path / "reference"
    await asyncio.to_thread(path.mkdir)
    await asyncio.to_thread(
        registry.update, target.id, name=target.name, primary=target.primary_cwd, folders=[str(path)], expected=target
    )
    if missing:
        await asyncio.to_thread(path.rmdir)
    finished = await capture_events(main._services.bus, WorkspaceChangeFinished)
    composer = main.query_one(InputBar)
    composer.value = "/workspace switch Target"
    await composer.action_submit()
    await wait_for(
        lambda: main._state.workspace.workspace_id == target.id and bool(finished),
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    warning_prefix = "Workspace “Target” has a missing reference folder"
    expected = f"{warning_prefix}: {path}"
    if locale == "zh-Hans":
        warning_prefix = "工作区“Target”有 1 个参考文件夹不存在"
        expected = f"{warning_prefix}：{path}"  # noqa: RUF001 - exact localized prose
    warnings = [notice for notice in pilot.app._notifications if notice.message.startswith(warning_prefix)]
    assert [(notice.message, notice.severity) for notice in warnings] == ([(expected, "warning")] if missing else [])


async def test_buddy_name_completion_fills_the_composer_for_the_argument(workspace_main):
    from random import Random

    from chrys.app.features.buddy import actions as buddy_actions
    from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList

    _, pilot, main = workspace_main
    await asyncio.to_thread(buddy_actions.hatch, Random(1))
    composer = main.query_one(InputBar)
    composer.focus_input()
    await pilot.press(*"/buddy nam")
    suggestions = main.query_one(SuggestionList)
    await wait_for(lambda: suggestions.is_visible, pilot=pilot)
    await pilot.press("enter")
    assert composer.value == "/buddy name "


@pytest.mark.parametrize("entry", ["status", "command"])
@pytest.mark.parametrize("assigned", [False, True], ids=["unspecified", "named"])
async def test_reselecting_current_workspace_closes_picker_without_changes(live_workspace_chat, assigned, entry):
    pilot, main, engine, registry, current, _target = live_workspace_chat
    app = pilot.app
    if not assigned:
        await engine.event_bus.publish(WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY))
        await wait_for(lambda: app.screen is main, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    before = registry.path.read_bytes(), engine.session_id, engine.workspace, tuple(app._notifications)
    changes = await capture_events(engine.event_bus, WorkspaceChange)
    new_sessions = await capture_events(engine.event_bus, SessionNew)
    if entry == "status":
        await click_when_settled(pilot, "#workspace-tag")
    else:
        composer = main.query_one(InputBar)
        composer.value = "/workspace switch"
        await composer.action_submit()
    await wait_for(lambda: _picker_ready(app), pilot=pilot)
    options = app.screen.query_one(OptionList)
    assert options.get_option_at_index(options.highlighted).id == (current.id if assigned else "unspecified")
    await pilot.press("enter")
    await wait_for(lambda: app.screen is main and not main._workspace_picker._open, pilot=pilot)
    assert changes == [] and new_sessions == []
    assert (registry.path.read_bytes(), engine.session_id, engine.workspace, tuple(app._notifications)) == before


@pytest.mark.parametrize("entry", ["picker", "command"])
@pytest.mark.parametrize("assigned", [False, True], ids=["unspecified", "named"])
async def test_reselecting_current_workspace_repairs_saved_membership_without_confirmation(
    live_workspace_chat, monkeypatch, tmp_path, assigned, entry
):
    pilot, main, engine, registry, current, target = live_workspace_chat
    if not assigned:
        await engine.event_bus.publish(WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY))
        await wait_for(lambda: pilot.app.screen is main, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    from chrys.orchestration.engine.loader import AgentLoader

    failed = await capture_events(engine.event_bus, WorkspaceChangeFinished)
    with monkeypatch.context() as patch:
        patch.setattr(AgentLoader, "build", create_autospec(AgentLoader.build, side_effect=RuntimeError("load failed")))
        composer = main.query_one(InputBar)
        composer.value = "/workspace switch Target"
        await composer.action_submit()
        await wait_for(lambda: bool(failed), pilot=pilot)
    await wait_for(
        lambda: (
            isinstance(pilot.app.screen, AgentLoadDialog)
            and pilot.app.screen.is_mounted
            and pilot.app.screen.query_one("#agent-load-ok", Button).display
        ),
        pilot=pilot,
    )
    await click_when_settled(pilot, "#agent-load-ok")
    await wait_for(lambda: pilot.app.screen is main and not main._state.run.agent_loading, pilot=pilot)
    assert failed[-1].error is not None
    assert registry.snapshot().workspace_id_for_session(engine.session_id) == target.id
    if assigned:
        added = tmp_path / "added-reference"
        await asyncio.to_thread(added.mkdir)
        changed = await asyncio.to_thread(
            registry.update,
            current.id,
            name=current.name,
            primary=current.primary_cwd,
            folders=[*(folder.path for folder in current.working_dirs), str(added)],
        )
    main._state.run.has_messages = True
    confirm = create_autospec(
        main._workspace_picker._confirm_switch, side_effect=AssertionError("unexpected confirmation")
    )
    monkeypatch.setattr(main._workspace_picker, "_confirm_switch", confirm)
    installed = engine.current.require_loaded()
    before = engine.workspace
    view_before = (main._state.workspace.membership, main._workspace_cwd())
    changes = await capture_events(engine.event_bus, WorkspaceChange)
    updates = await capture_events(engine.event_bus, WorkspaceUpdated)
    notifications = tuple(pilot.app._notifications)

    if entry == "picker":
        await click_when_settled(pilot, "#workspace-tag")
        await wait_for(lambda: _picker_ready(pilot.app), pilot=pilot)
        await pilot.press("enter")
    else:
        composer = main.query_one(InputBar)
        composer.value = f"/workspace switch {current.name if assigned else 'Unspecified'}"
        await composer.action_submit()
    await wait_for(lambda: pilot.app.screen is main and not main._workspace_picker._open, pilot=pilot)
    await wait_for(lambda: bool(changes), pilot=pilot)

    assert len(changes) == 1
    assert changes[0].workspace_operation is WorkspaceOperation.ALIGN_MEMBERSHIP
    assert updates == []
    assert workspace_registry.WorkspaceRegistry(registry.path).snapshot().workspace_id_for_session(
        engine.session_id
    ) == (current.id if assigned else None)
    assert engine.current.require_loaded() is installed
    assert engine.workspace is before
    assert installed.runtime.cwd == before.primary_cwd
    assert tuple(installed.runtime.working_dirs) == tuple(before.working_dirs)
    assert (main._state.workspace.membership, main._workspace_cwd()) == view_before
    if assigned:
        assert registry.snapshot().workspace(current.id) == changed
    assert tuple(pilot.app._notifications) == notifications
    confirm.assert_not_called()


async def test_create_edit_delete_workspace_from_selector(live_workspace_chat):
    """Creation, rename and deletion use the live engine and the same saved membership."""
    pilot, main, engine, registry, _current, target = live_workspace_chat
    app = pilot.app
    session_id = engine.session_id
    main._workspace_picker.open()
    await wait_for(lambda: _picker_ready(app), pilot=pilot)
    options = app.screen.query_one(OptionList)
    options.highlighted = options.get_option_index("create")
    await pilot.press("enter")
    await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
    dialog = app.screen
    dialog.query_one("#workspace-name", EnhancedInput).value = "Review"
    await click_when_settled(pilot, "#workspace-save")
    await wait_for(
        lambda: (
            app.screen is main
            and (membership := main._state.workspace.membership) is not None
            and membership.name == "Review"
        ),
        pilot=pilot,
    )
    created = registry.snapshot().session_workspace(session_id)
    assert created is not None
    assert not any("To add reference folders" in toast.message for toast in app._notifications)

    main._workspace_picker.open()
    await _choose_row_action(pilot, created, "Edit")
    await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
    dialog = app.screen
    assert not dialog.query_one("#workspace-primary", Static).can_focus
    dialog.query_one("#workspace-name", EnhancedInput).value = "Review renamed"
    await click_when_settled(pilot, "#workspace-save")
    await wait_for(
        lambda: (membership := main._state.workspace.membership) is not None and membership.name == "Review renamed",
        pilot=pilot,
    )
    assert engine.session_id == session_id
    assert registry.snapshot().session_workspace(session_id).name == "Review renamed"

    await _choose_row_action(pilot, registry.snapshot().workspace(created.id), "Delete")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    await click_when_settled(pilot, "#confirm-yes")
    await wait_for(
        lambda: (
            isinstance(app.screen, WorkspacesScreen)
            and app.screen.is_mounted
            and main._state.workspace.membership is None
        ),
        pilot=pilot,
    )
    assert registry.snapshot().workspace(created.id) is None
    assert registry.snapshot().workspace(target.id) == target
    assert registry.snapshot().workspace_id_for_session(session_id) is None
    assert engine.session_id == session_id
    assert Path(created.primary_cwd).is_dir()


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_delete_is_rejected_during_prompt_submit(live_workspace_chat, monkeypatch):
    pilot, main, engine, registry, _current, target = live_workspace_chat
    entered, release = asyncio.Event(), asyncio.Event()
    evaluate = engine.turns._evaluate_user_prompt_submit

    async def hold_prompt_hook(operation, *, injected, target_operation_id=None, trajectory_context=None):
        entered.set()
        await release.wait()
        return await evaluate(
            operation, injected=injected, target_operation_id=target_operation_id, trajectory_context=trajectory_context
        )

    monkeypatch.setattr(engine.turns, "_evaluate_user_prompt_submit", hold_prompt_hook)
    composer = main.query_one(InputBar)
    before = registry.path.read_bytes()
    composer.focus_input()
    composer.value = "Send this first"
    await pilot.press("enter")
    try:
        await wait_for(entered.is_set, pilot=pilot)
        assert main._state.submit.active and not main._state.run.agent_running and not engine.execution_busy()
        composer.value = f"/cd {target.primary_cwd}"
        await pilot.press("enter")
        await wait_for(
            lambda: any(
                notice.message == "Cannot change directory while agent is busy" for notice in pilot.app._notifications
            ),
            pilot=pilot,
        )
        assert pilot.app.screen is main
        composer.value = "/workspace delete Target"
        await pilot.press("enter")
        await wait_for(
            lambda: (
                any(
                    notice.message == "Cannot change workspace while agent is busy"
                    for notice in pilot.app._notifications
                )
                or registry.snapshot().workspace(target.id) is None
            ),
            pilot=pilot,
        )
        unchanged_while_held = registry.path.read_bytes() == before
        refused_while_held = any(
            notice.message == "Cannot change workspace while agent is busy" for notice in pilot.app._notifications
        )
    finally:
        release.set()
        if entered.is_set():
            await wait_for(lambda: not main._state.submit.active and main.has_messages_state, pilot=pilot)
            await drain(engine)
    await pilot.pause()
    assert unchanged_while_held, "the registry changed while the submit hook was still blocked"
    assert refused_while_held
    assert registry.path.read_bytes() == before


async def test_delete_reports_busy_if_the_chat_changes_before_apply(live_workspace_chat, monkeypatch):
    from chrys.app.tui.screens.main import workspace_picker_actions as actions

    pilot, main, engine, registry, current, _target = live_workspace_chat
    prepared = threading.Event()
    release = threading.Event()
    manage = actions.manage_workspace

    def hold_before_apply(registry, command, **kwargs):
        result = manage(registry, command, **kwargs)
        prepared.set()
        if not release.wait(ENGINE_TURN_TIMEOUT):
            raise TimeoutError("test did not release workspace command")
        return result

    monkeypatch.setattr(actions, "manage_workspace", hold_before_apply)
    changes = await capture_events(main._services.bus, WorkspaceChange)
    composer = main.query_one(InputBar)
    composer.value = "/workspace delete Current"
    await composer.action_submit()
    try:
        await wait_for(prepared.is_set, pilot=pilot)
        assert registry.snapshot().workspace(current.id) == current
        main._state.run.agent_running = True
        release.set()
        await wait_for(
            lambda: any(
                toast.message == "Cannot change workspace while agent is busy" for toast in pilot.app._notifications
            ),
            pilot=pilot,
        )
        assert changes == []
        assert engine.workspace.id == current.id
    finally:
        release.set()
        main._state.run.agent_running = False


def _workspace_command_submitter(pilot, main):
    composer = main.query_one(InputBar)

    async def submit(command: str, message: str) -> None:
        await wait_for(
            lambda: pilot.app.screen is main and not composer.agent_loading,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="the main screen can accept the next workspace command",
        )
        before = {item.identity for item in pilot.app._notifications}
        composer.value = command
        await composer.action_submit()
        await wait_for(
            lambda: any(item.message == message for item in pilot.app._notifications if item.identity not in before),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description=message,
        )

    return submit


async def test_multiline_workspace_draft_is_sent_as_chat(live_workspace_chat):
    pilot, main, _engine, registry, current, _target = live_workspace_chat
    sent_to_model = await capture_events(main._services.bus, UserMessage)
    composer = main.query_one(InputBar)
    text = "/workspace add-folder /tmp\nsecond line"
    composer.value = text
    await composer.action_submit()
    await wait_for(lambda: [message.text for message in sent_to_model] == [text], pilot=pilot)
    assert registry.snapshot().workspace(current.id) == current


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_slash_commands_explain_usage_and_create_workspace(live_workspace_chat):
    pilot, main, engine, registry, current, _target = live_workspace_chat
    sid = engine.session_id
    sent_to_model = await capture_events(main._services.bus, UserMessage)
    await wait_for(lambda: ("Current", "◦ Current") in main._workspace_picker.argument_choices("delete"), pilot=pilot)
    submit = _workspace_command_submitter(pilot, main)
    await submit(
        "/workspace",
        "Usage\n/workspace switch [<name>]\n/workspace new <name>\n/workspace rename <new name>\n"
        "/workspace add-folder <path>\n/workspace remove-folder <path>\n/workspace delete <name>\n"
        "The primary folder cannot be changed. "
        "Create a new workspace to use a different primary folder.",
    )
    assert pilot.app.screen is main
    await submit(
        "/workspace new My Workspace",
        "Created workspace “My Workspace”.\nTo add reference folders, use /workspace add-folder <path>.",
    )
    created = registry.snapshot().session_workspace(sid)
    assert created is not None and created.name == "My Workspace"
    assert Path(created.primary_cwd).samefile(current.primary_cwd) and created.working_dirs == []
    await wait_for(lambda: main._state.workspace.workspace_id == created.id, pilot=pilot)
    assert engine.session_id == sid
    assert sent_to_model == []


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_slash_commands_rename_and_edit_reference_folders(live_workspace_chat, tmp_path):
    pilot, main, engine, _registry, current, _target = live_workspace_chat
    reference = tmp_path / "Reference Files"
    reference.mkdir()
    original_folders = [Path(folder.path).resolve() for folder in current.working_dirs]
    sid = engine.session_id
    sent_to_model = await capture_events(main._services.bus, UserMessage)
    submit = _workspace_command_submitter(pilot, main)
    await submit("/workspace rename Renamed", "Renamed the workspace to “Renamed”.")
    await wait_for(
        lambda: (
            engine.workspace.name == "Renamed"
            and (membership := main._state.workspace.membership) is not None
            and membership.name == "Renamed"
        ),
        pilot=pilot,
    )
    await submit("/workspace add-folder ../Reference Files\n", f"Added reference folder {reference}.")
    await wait_for(
        lambda: (
            [Path(folder.path).resolve() for folder in engine.workspace.working_dirs]
            == [*original_folders, reference.resolve()]
        ),
        pilot=pilot,
    )
    await submit("/workspace remove-folder ../Reference Files", f"Removed reference folder {reference}.")
    await wait_for(lambda: engine.workspace.working_dirs == current.working_dirs, pilot=pilot)
    assert engine.session_id == sid
    assert sent_to_model == []


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_slash_commands_switch_delete_and_respect_session_state(live_workspace_chat):
    pilot, main, engine, registry, current, _target = live_workspace_chat
    composer = main.query_one(InputBar)
    sid = engine.session_id
    sent_to_model = await capture_events(main._services.bus, UserMessage)
    submit = _workspace_command_submitter(pilot, main)
    await submit("/workspace rename switch", "Renamed the workspace to “switch”.")
    await wait_for(lambda: pilot.app.screen is main and not composer.agent_loading, pilot=pilot)
    composer.value = "/workspace switch Unspecified"
    await composer.action_submit()
    await wait_for(
        lambda: main._state.workspace.membership is None and pilot.app.screen is main and not composer.agent_loading,
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    composer.value = "/workspace switch SWITCH"
    await composer.action_submit()
    await wait_for(lambda: main._state.workspace.workspace_id == current.id, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    membership = main._state.workspace.membership
    assert membership is not None and membership.name == "switch"
    assert membership == registry.snapshot().workspace(current.id)
    await submit("/workspace delete SWITCH", "Deleted workspace “switch”. This session is now Unspecified.")
    await wait_for(
        lambda: (
            pilot.app.screen is main
            and not composer.agent_loading
            and main._state.workspace.membership is None
            and engine.session.workspace_id is None
        ),
        pilot=pilot,
    )
    assert registry.snapshot().workspace(current.id) is None
    assert Path(engine.workspace.primary_cwd).samefile(current.primary_cwd)
    await submit(
        "/workspace rename No workspace",
        "This session is not in a workspace. Use /workspace switch <name> to enter one.",
    )
    main._state.run.agent_running = True
    try:
        await submit(
            "/workspace new Busy",
            "/workspace is not available while agent is running",
        )
    finally:
        main._state.run.agent_running = False
    assert "Busy" not in [item.name for item in registry.snapshot().workspaces]
    assert engine.session_id == sid
    assert sent_to_model == []


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_picker_and_commands_explain_edits_that_apply_on_reopen(live_workspace_chat, tmp_path):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    alias = tmp_path / "workspace alias"
    symlink_or_skip(alias, Path(current.primary_cwd), target_is_directory=True)
    registered = registry.create("Aliased", str(alias))
    await engine.event_bus.publish(
        WorkspaceChange(workspace_operation=WorkspaceOperation.SELECT, workspace_id=registered.id),
        raise_handler_errors=True,
    )
    await wait_for(
        lambda: main._state.workspace.workspace_id == registered.id and pilot.app.screen is main, pilot=pilot
    )
    runtime = engine.current.require_loaded()
    retargeted = tmp_path / "retargeted"
    retargeted.mkdir()
    retarget_workdir_alias(alias, retargeted)
    notice = "Workspace changes will take effect the next time you open this session."
    changes = await capture_events(main._services.bus, WorkspaceChange)

    await _open_workspace_editor(pilot, main, registered)
    await wait_for(
        lambda: isinstance(pilot.app.screen, WorkspaceEditDialog) and pilot.app.screen.is_mounted,
        pilot=pilot,
    )
    pilot.app.screen.query_one("#workspace-name", EnhancedInput).value = "Edited in dialog"
    await click_when_settled(pilot, "#workspace-save")
    await wait_for(lambda: any(notice in toast.render().plain for toast in main.query(Toast)), pilot=pilot)
    assert registry.snapshot().workspace(registered.id).name == "Edited in dialog"
    await _close_workspace_picker(pilot, main)

    reference = tmp_path / "Reference Files"
    reference.mkdir()
    composer = main.query_one(InputBar)
    for command, result in [
        ("rename Edited by command", "Renamed the workspace to “Edited by command”."),
        (f"add-folder {reference}", "Added reference folder"),
        (f"remove-folder {reference}", "Removed reference folder"),
    ]:
        composer.value = f"/workspace {command}"
        await composer.action_submit()
        await wait_for(
            lambda result=result: any(
                result in toast.render().plain and notice in toast.render().plain for toast in main.query(Toast)
            ),
            pilot=pilot,
        )

    saved = registry.snapshot().workspace(registered.id)
    assert saved.name == "Edited by command" and saved.working_dirs == []
    assert engine.current.require_loaded() is runtime
    assert Path(engine.workspace.primary_cwd).samefile(current.primary_cwd)
    assert [change.edit_workspace.name for change in changes if change.edit_workspace is not None] == [
        "Edited in dialog",
        "Edited by command",
        "Edited by command",
        "Edited by command",
    ]


@pytest.mark.parametrize("entry", ["picker", "command"])
@pytest.mark.parametrize("cancel_new_session", [False, True], ids=["switch", "new-session"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_cancel_switch_after_creating_workspace_keeps_definition(live_workspace_chat, entry, cancel_new_session):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    app = pilot.app
    main._state.run.has_messages = True
    await resize_when_settled(pilot, 170, 36)
    main.query_one(SidebarPanel).query_one(TabbedContent).active = "tab-workspace"
    panel = main.query_one(WorkspacePanel)
    await wait_for(lambda: is_widget_shown_on_active_screen(panel), pilot=pilot)
    session_id = engine.session_id
    if cancel_new_session:
        await main._services.bus.publish(
            BackgroundTasksChanged(
                session_id=session_id,
                tasks=(BackgroundTaskView("task", "server", "", current.primary_cwd, "user", 1, (), "", 1),),
            ),
            raise_handler_errors=True,
        )
    if entry == "command":
        composer = main.query_one(InputBar)
        composer.value = "/workspace new New team"
        await composer.action_submit()
    else:
        main._workspace_picker.open()
        await wait_for(lambda: _picker_ready(app), pilot=pilot)
        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index("create")
        await pilot.press("enter")
        await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
        app.screen.query_one("#workspace-name", EnhancedInput).value = "New team"
        await click_when_settled(pilot, "#workspace-save")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    confirmation = app.screen
    assert confirmation.query_one("#confirm-container").border_title == "Created workspace “New team”"
    assert confirmation.query_one("#confirm-message", Static).render().plain == (
        "Move the current session into “New team”?\n"
        "• Move this session: bring the conversation along. Its working directory stays the same.\n"
        "• New session: open a new session in “New team”. The current session stays where it is."
    )
    await wait_for(
        lambda: any(str(node.label).lstrip().startswith("New team ▾") for node in panel.query_one(Tree).root.children),
        pilot=pilot,
        description="the sidebar shows the new workspace while the move is undecided",
    )
    assert any(workspace.name == "New team" for workspace in registry.snapshot().workspaces)
    assert app.screen is confirmation
    if cancel_new_session:
        switch_dialog = app.screen
        await click_when_settled(pilot, "#confirm-alternate")
        await wait_for(
            lambda: isinstance(app.screen, ConfirmDialog) and app.screen is not switch_dialog and app.screen.is_mounted,
            pilot=pilot,
        )
    await pilot.press("escape")
    await wait_for(lambda: app.screen is main, pilot=pilot)
    await wait_for(
        lambda: any(
            "Created workspace “New team”. You can enter it from the workspace picker." in toast.render().plain
            for toast in main.query(Toast)
        ),
        pilot=pilot,
    )
    assert engine.session_id == session_id
    assert registry.snapshot().session_workspace(session_id) == current
    assert any(workspace.name == "New team" for workspace in registry.snapshot().workspaces)
    assert not any("To add reference folders" in notice.message for notice in app._notifications)


@pytest.mark.parametrize("entry", ["command", "picker-delete", "edit-delete"])
async def test_delete_current_workspace_with_missing_primary(live_workspace_chat, monkeypatch, tmp_path, entry):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    app = pilot.app
    sid = engine.session_id
    monkeypatch.chdir(tmp_path)
    await asyncio.to_thread(Path(current.primary_cwd).rmdir)
    composer = main.query_one(InputBar)

    async def command(text):
        composer.value = text
        await composer.action_submit()

    if entry == "command":
        await command("/workspace delete Current")
    elif entry == "edit-delete":
        await _open_workspace_editor(pilot, main, current)
        await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
        await click_when_settled(pilot, "#workspace-delete")
    else:
        main._workspace_picker.open()
        await wait_for(lambda: _picker_ready(app), pilot=pilot)
        options = app.screen.query_one(OptionList)
        options.highlighted = options.get_option_index(current.id)
        await wait_for(lambda: screen_is_settled(app, app.screen), pilot=pilot)
        line, label = next(
            (index, options.render_line(index).text)
            for index in range(options.content_region.height)
            if "Current" in options.render_line(index).text and "Delete" in options.render_line(index).text
        )
        x = options.content_region.x - options.region.x + cell_len(label[: label.rindex("Delete")])
        await click_when_settled(pilot, options, offset=(x, line))
    if entry != "command":
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        await click_when_settled(pilot, "#confirm-yes")

    success = "Deleted workspace “Current”. This session is now Unspecified."
    await wait_for(lambda: any(toast.message == success for toast in app._notifications), pilot=pilot)
    if entry in {"picker-delete", "edit-delete"}:
        await wait_for(lambda: _picker_ready(app), pilot=pilot)
        await pilot.press("escape")
    await wait_for(lambda: app.screen is main, pilot=pilot)
    snapshot = registry.snapshot()
    assert snapshot.workspace(current.id) is None and snapshot.workspace_id_for_session(sid) is None
    assert engine.session_id == sid and engine.session.workspace_id is None
    assert engine.workspace.primary_cwd == current.primary_cwd
    tooltip = main.query_one("#workspace-tag", Static).tooltip
    assert isinstance(tooltip, Content) and tooltip.plain == "Unspecified"
    assert not any("the next time you open" in toast.message for toast in app._notifications)
    await command("/workspace delete Current")
    await wait_for(
        lambda: any(toast.message == "Workspace “Current” is already absent." for toast in app._notifications),
        pilot=pilot,
    )


@pytest.mark.parametrize("entry", ["edit", "rename", "unspecified"])
async def test_current_workspace_changes_apply_with_missing_primary(live_workspace_chat, monkeypatch, tmp_path, entry):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    monkeypatch.chdir(tmp_path)
    await asyncio.to_thread(Path(current.primary_cwd).rmdir)
    if entry == "edit":
        await _open_workspace_editor(pilot, main, current)
        await wait_for(
            lambda: isinstance(pilot.app.screen, WorkspaceEditDialog) and pilot.app.screen.is_mounted, pilot=pilot
        )
        pilot.app.screen.query_one("#workspace-name", EnhancedInput).value = "Renamed"
        await click_when_settled(pilot, "#workspace-save")
    else:
        composer = main.query_one(InputBar)
        composer.value = "/workspace " + ("rename Renamed" if entry == "rename" else "switch Unspecified")
        await composer.action_submit()
    if entry == "unspecified":
        await wait_for(lambda: engine.session.workspace_id is None, pilot=pilot)
        assert registry.snapshot().workspace_id_for_session(engine.session_id) is None
    else:
        await wait_for(
            lambda: (membership := main._state.workspace.membership) is not None and membership.name == "Renamed",
            pilot=pilot,
        )
        assert engine.workspace.name == registry.snapshot().workspace(current.id).name == "Renamed"
    assert engine.workspace.primary_cwd == current.primary_cwd


def _confirmation_copy(kind, other, current_name):
    """Exact title and body for the real switch integration cases."""
    if kind == "unspecified":
        return (
            f"Leave workspace “{current_name}”?",
            (
                "• Move this session: make it Unspecified. Its working directory stays the same.\n"
                "• New session: open a new Unspecified session. The current session stays where it is."
            ),
        )
    return (
        "Switch to workspace “Target”?",
        (
            f"• Move this session: bring the conversation along. Its working directory changes to {other}.\n"
            "• New session: open a new session in “Target”. The current session stays where it is."
        ),
    )


@pytest.mark.parametrize("choice", ["empty", "move", "new"])
async def test_new_workspace_command_shows_chinese_reference_hint_after_joining(live_workspace_chat, choice):
    pilot, main, engine, registry, _, _ = live_workspace_chat
    app = pilot.app
    app.locale_controller.switch_locale("zh-Hans")
    main._state.run.has_messages = choice != "empty"
    previous = engine.session_id
    composer = main.query_one(InputBar)
    composer.value = "/workspace new Team"
    await composer.action_submit()
    if choice != "empty":
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        await click_when_settled(pilot, "#confirm-yes" if choice == "move" else "#confirm-alternate")
    expected = "已创建工作区“Team”。\n要添加参考文件夹，可以用 /workspace add-folder <路径>。"  # noqa: RUF001 - exact localized prose
    await wait_for(lambda: any(toast.message == expected for toast in app._notifications), pilot=pilot)
    assert registry.snapshot().session_workspace(engine.session_id).name == "Team"
    assert (engine.session_id != previous) is (choice == "new")


@pytest.mark.parametrize("command", ["delete Current", "rename Renamed"])
async def test_workspace_definition_survives_a_failed_runtime_preparation(live_workspace_chat, monkeypatch, command):
    from chrys.orchestration.engine.loader import AgentLoader

    pilot, main, engine, registry, current, _ = live_workspace_chat
    original = registry.path.read_bytes()
    monkeypatch.setattr(
        AgentLoader,
        "build",
        create_autospec(AgentLoader.build, side_effect=RuntimeError("workspace preparation failed")),
    )
    finished = await capture_events(engine.event_bus, WorkspaceChangeFinished)
    composer = main.query_one(InputBar)
    composer.value = f"/workspace {command}"
    await composer.action_submit()
    await wait_for(lambda: bool(finished), pilot=pilot)
    assert finished[-1].error is not None and "workspace preparation failed" in finished[-1].error.message
    assert registry.path.read_bytes() == original
    assert registry.snapshot().session_workspace(engine.session_id) == current
    assert engine.workspace == current


async def test_workspace_edit_commit_failure_returns_to_form(live_workspace_chat, monkeypatch):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    app = pilot.app
    await _open_workspace_editor(pilot, main, current)
    await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
    dialog = app.screen
    name = dialog.query_one(EnhancedInput)
    name.value = "Draft name"
    save = dialog.query_one("#workspace-save", Button)
    await wait_for(lambda: not save.disabled, pilot=pilot)

    original = engine.current.require_loaded()
    write = create_autospec(workspace_registry.atomic_write_text, side_effect=OSError("simulated disk full"))
    error = dialog.query_one("#workspace-error", Static)
    expected = "Workspace list could not be saved: simulated disk full"
    with monkeypatch.context() as patch:
        patch.setattr(workspace_registry, "atomic_write_text", write)
        await click_when_settled(pilot, save)
        await wait_for(
            lambda: error.render().plain == expected and not save.disabled, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
        )

    assert app.screen is dialog
    assert not any(isinstance(screen, AgentLoadDialog) for screen in app.screen_stack)
    assert not any(notice.message == expected for notice in app._notifications)
    assert not main._state.run.agent_loading and not save.disabled
    assert name.value == "Draft name"
    assert engine.current.require_loaded() is original
    assert registry.snapshot().workspace(current.id) == current


@pytest.mark.parametrize("action", ["switch", "cd"])
async def test_switch_preparation_runs_off_loop_and_does_not_cover_a_new_dialog(
    live_workspace_chat, monkeypatch, action
):
    from chrys.app.tui.screens.main import workspace_actions, workspace_picker_actions

    pilot, main, _, _, current, target = live_workspace_chat
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()

    def hold():
        assert threading.get_ident() != loop_thread
        entered.set()
        if not release.wait(5):
            raise TimeoutError("workspace preparation was not released")

    if action == "switch":
        resolve = workspace_picker_actions.resolve_workspace_directory

        def prepare(path):
            hold()
            return resolve(path)

        monkeypatch.setattr(workspace_picker_actions, "resolve_workspace_directory", prepare)
        request = main._workspace_picker._switch(target, main._workspace_picker._owner())
    else:
        message = workspace_actions.WorkspaceController._chdir_leave_message

        def prepare_message(resolved, workspace, *, offer_new):
            hold()
            return message(resolved, workspace, offer_new=offer_new)

        monkeypatch.setattr(
            workspace_actions.WorkspaceController, "_chdir_leave_message", staticmethod(prepare_message)
        )
        request = main._workspace_actions.confirm_workspace_leave(target.primary_cwd)
    async with running_task(request) as task:
        try:
            await wait_for(lambda: entered.is_set() or task.done(), pilot=pilot)
            if task.done():
                await task
            assert entered.is_set() and not task.done()
            overlay = ConfirmDialog(title="Other operation", message="Keep this dialog")
            pilot.app.push_screen(overlay)
            await wait_for(lambda: pilot.app.screen is overlay and overlay.is_mounted, pilot=pilot)
        finally:
            release.set()
        result = await task
        assert result == (False if action == "switch" else (False, None))
        assert pilot.app.screen is overlay
        assert main._state.workspace.workspace_id == current.id


async def test_workspace_command_in_workflow_explains_the_same_lock_as_cd(live_workspace_chat):
    from chrys.app.tui.screens.dialogs.confirm import NoticeDialog

    pilot, main, _, _, _, _ = live_workspace_chat
    from tests.app.tui.screens.main._workflow_support import workflow_selection

    main._set_workflow_mode(True)
    main._workflow.session_view.selection = workflow_selection(main)
    main._workspace_picker.run_command("")
    await wait_for(lambda: isinstance(pilot.app.screen, NoticeDialog) and pilot.app.screen.is_mounted, pilot=pilot)
    assert main._workflow.browser._workspace_notice is pilot.app.screen


async def test_edit_dialog_reference_folders_update_current_runtime_and_membership(live_workspace_chat, tmp_path):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    reference = tmp_path / "Added through dialog"
    reference.mkdir()
    sid = engine.session_id
    await _open_workspace_editor(pilot, main, current)
    await wait_for(
        lambda: isinstance(pilot.app.screen, WorkspaceEditDialog) and pilot.app.screen.is_mounted, pilot=pilot
    )
    dialog = pilot.app.screen
    await click_when_settled(pilot, "#workspace-add-folder")
    await wait_for(lambda: isinstance(pilot.app.screen, FilePicker) and pilot.app.screen.is_mounted, pilot=pilot)
    pilot.app.screen.dismiss(str(reference))
    await wait_for(lambda: pilot.app.screen is dialog and str(reference) in dialog._folders, pilot=pilot)
    await click_when_settled(pilot, "#workspace-save")
    expected = replace(current, working_dirs=[*current.working_dirs, WorkingDir(str(reference))])
    await wait_for(
        lambda: _picker_ready(pilot.app) and engine.workspace == expected,
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    await _close_workspace_picker(pilot, main)
    await drain(engine)
    assert engine.session_id == sid
    assert main._state.workspace.membership == expected
    assert registry.snapshot().session_workspace(sid) == expected


@pytest.mark.parametrize("operation", ["delete-current", "edit-other"])
async def test_picker_reopens_on_operated_row_or_current_workspace(live_workspace_chat, tmp_path, operation):
    pilot, main, _engine, registry, current, _target = live_workspace_chat
    app = pilot.app
    for index in range(12):
        third_path = tmp_path / f"third-{index:02}"
        await asyncio.to_thread(third_path.mkdir)
        third = await asyncio.to_thread(registry.create, f"Another {index:02}", str(third_path))
    main._workspace_picker.open()
    # Act on rows the picker lists, as a user does: the registry writes above bypassed every window.
    await wait_for(lambda: _picker_ready(app) and third.id in app.screen._workspaces, pilot=pilot)
    first_picker = app.screen
    if operation == "delete-current":
        first_picker._row_action("delete", current.id)
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        await click_when_settled(pilot, "#confirm-yes")
    else:
        first_picker._row_action("edit", third.id)
        await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
        app.screen.query_one("#workspace-name", EnhancedInput).value = "Another renamed"
        await click_when_settled(pilot, "#workspace-save")
    await wait_for(
        lambda: _picker_ready(app) and app.screen is not first_picker, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
    )
    reopened = app.screen.query_one(OptionList)
    expected = third.id if operation == "edit-other" else "unspecified"
    assert reopened.has_focus
    assert reopened.get_option_at_index(reopened.highlighted).id == expected
    if operation == "delete-current":
        assert registry.snapshot().workspace(current.id) is None
        assert main._state.workspace.workspace_id is None
    else:
        assert registry.snapshot().workspace(third.id).name == "Another renamed"
        await wait_for(
            lambda: any("Another renamed" in reopened.render_line(y).text for y in range(reopened.size.height)),
            pilot=pilot,
        )
