# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace slash command arguments complete and submit from the chat composer."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from textual import events
from textual.widgets import Button, OptionList, Static

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.workspaces.picker import WorkspacesScreen
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.chrome.suggestion_list import SuggestionList
from chrys.foundation.events.types import WorkspaceChange
from tests.support.event_capture import capture_events
from tests.support.rollback_spec import drain
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, ENGINE_TURN_TIMEOUT, wait_for, with_wait_deadline


async def test_named_workspace_status_label_follows_locale(live_workspace_chat, monkeypatch):
    from chrys.app.tui import i18n as tui_i18n

    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    pilot, main, _engine, _registry, current, _target = live_workspace_chat
    status = main.query_one(StatusBar)
    label = status.query_one("#workspace-label", Static)
    assert pilot.app.size.width >= 81
    assert status._workspace_indicator.label == current.name
    for locale, expected in (("en", "Workspace"), ("zh-Hans", "工作区"), ("en", "Workspace")):
        pilot.app.locale_controller.switch_locale(locale)
        await wait_for(lambda expected=expected: label.render().plain == expected, pilot=pilot)
        assert label.display is True
        assert status._workspace_indicator.label == current.name


@pytest.mark.parametrize("ready", ["no-snapshot", "stale-order"])
async def test_workspace_switch_completion_keeps_unspecified_without_order(workspace_main, monkeypatch, ready):
    _app, _pilot, main = workspace_main
    picker = main._workspace_picker
    if ready == "no-snapshot":
        monkeypatch.setattr(picker, "_snapshot", lambda: None)
    else:
        monkeypatch.setattr(picker, "_order", lambda: None)
    assert picker.argument_choices("switch") == [("Unspecified", "  Unspecified")]
    assert picker.argument_choices("delete") == []


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_completion_follows_picker_cwd_and_recent_use_order(live_workspace_chat, tmp_path):
    pilot, main, _engine, registry, current, target = live_workspace_chat
    composer = main.query_one(InputBar)
    composer.value = "/workspace switch Target"
    await composer.action_submit()
    await wait_for(
        lambda: pilot.app.screen is main and not composer.agent_loading and main._workspace_cwd() == target.primary_cwd,
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )

    recent_dir = tmp_path / "recent"
    await asyncio.to_thread(recent_dir.mkdir)
    recent = await asyncio.to_thread(registry.create, "Recent", str(recent_dir))
    await asyncio.to_thread(registry.touch, recent.id)
    main._refresh_workspace()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and snapshot.last_used_at.get(recent.id),
        pilot=pilot,
    )

    suggestions = main.query_one(SuggestionList)
    composer.value = ""
    composer.focus_input()
    await pilot.press(*"/workspace delete ")
    assert composer.value == "/workspace delete "
    await wait_for(lambda: suggestions.is_visible, pilot=pilot)
    assert suggestions._values == [target.name, recent.name, current.name]

    composer.value = ""
    await pilot.press(*"/workspace switch ")
    assert suggestions.is_visible
    switch_choices = list(suggestions._values)
    await pilot.press("escape", "enter")
    app = pilot.app
    await wait_for(lambda: isinstance(app.screen, WorkspacesScreen) and app.screen.is_mounted, pilot=pilot)
    options = app.screen.query_one(OptionList)
    await wait_for(lambda: options.option_count == 5, pilot=pilot)
    by_id = {workspace.id: workspace.name for workspace in (current, target, recent)}
    by_id["unspecified"] = "Unspecified"
    picker_names = [by_id[option.id] for option in options.options if option.id != "create"]
    assert picker_names == [target.name, recent.name, current.name, "Unspecified"]
    assert switch_choices == picker_names


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_completion_rereads_a_workspace_created_in_another_window(live_workspace_chat, tmp_path):
    pilot, main, _engine, registry, _current, _target = live_workspace_chat
    suggestions = main.query_one(SuggestionList)
    composer = main.query_one(InputBar)
    elsewhere_dir = tmp_path / "elsewhere"
    await asyncio.to_thread(elsewhere_dir.mkdir)
    # Another window writes the list; this window gets no event for it.
    elsewhere = await asyncio.to_thread(registry.create, "Elsewhere", str(elsewhere_dir))
    composer.value = ""
    composer.focus_input()
    await pilot.press(*"/workspace switch ")
    await wait_for(lambda: suggestions.is_visible and elsewhere.name in suggestions._values, pilot=pilot)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_reread_landing_after_the_user_moved_keeps_their_row_for_enter(
    live_workspace_chat, tmp_path, monkeypatch
):
    from threading import Event

    from chrys.app.tui.screens.main import screen as screen_module
    from chrys.app.tui.util.workspace_order import read_workspace_order

    pilot, main, _engine, registry, _current, target = live_workspace_chat
    for name in ("next", "elsewhere"):
        await asyncio.to_thread((tmp_path / name).mkdir)
    following = await asyncio.to_thread(registry.create, "Target Next", str(tmp_path / "next"))
    main._refresh_workspace()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and snapshot.workspace(following.id) is not None,
        pilot=pilot,
    )
    started, release = Event(), Event()

    def held_read(*args):
        started.set()
        release.wait(10)
        return read_workspace_order(*args)

    monkeypatch.setattr(screen_module, "read_workspace_order", held_read)
    # Another window adds an unrelated workspace, so the reread changes the list this window holds.
    elsewhere = await asyncio.to_thread(registry.create, "Elsewhere", str(tmp_path / "elsewhere"))
    suggestions = main.query_one(SuggestionList)
    composer = main.query_one(InputBar)
    composer.value = ""
    composer.focus_input()
    try:
        await pilot.press(*"/workspace delete Target")
        await wait_for(lambda: started.is_set() and suggestions._values == ["Target", "Target Next"], pilot=pilot)
        await pilot.press("down")
        assert suggestions.chosen_value == "Target Next"
    finally:
        release.set()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and snapshot.workspace(elsewhere.id) is not None,
        pilot=pilot,
    )
    await pilot.pause()
    assert suggestions.chosen_value == "Target Next"
    await pilot.press("enter")
    assert composer.value == "/workspace delete Target Next"
    snapshot = registry.snapshot()
    assert snapshot.workspace(target.id) is not None
    assert snapshot.workspace(following.id) is not None


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_reread_that_empties_the_list_drops_the_rows_nobody_chose(live_workspace_chat, monkeypatch):
    from threading import Event

    from chrys.app.tui.screens.main import screen as screen_module
    from chrys.app.tui.util.workspace_order import read_workspace_order

    pilot, main, _engine, registry, current, target = live_workspace_chat
    started, release = Event(), Event()

    def held_read(*args):
        started.set()
        release.wait(10)
        return read_workspace_order(*args)

    monkeypatch.setattr(screen_module, "read_workspace_order", held_read)
    suggestions = main.query_one(SuggestionList)
    composer = main.query_one(InputBar)
    composer.value = ""
    composer.focus_input()
    try:
        await pilot.press(*"/workspace delete Tar")
        await wait_for(lambda: started.is_set() and suggestions._values == ["Target"], pilot=pilot)
        # Another window deletes every workspace while this window still lists them.
        for workspace in (current, target):
            await asyncio.to_thread(registry.delete, workspace.id)
    finally:
        release.set()
    await wait_for(lambda: (snapshot := main._workspace_snapshot) is not None and not snapshot.workspaces, pilot=pilot)
    await pilot.pause()
    assert suggestions._values == [None]
    await pilot.press("enter")
    assert composer.value == ""
    assert not registry.snapshot().workspaces


@pytest.mark.parametrize("key", ["enter", "escape"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_an_empty_argument_list_fills_when_its_reread_finds_a_workspace(
    live_workspace_chat, tmp_path, monkeypatch, key
):
    from threading import Event

    from chrys.app.tui.screens.main import screen as screen_module
    from chrys.app.tui.util.workspace_order import read_workspace_order

    pilot, main, _engine, registry, current, _target = live_workspace_chat
    suggestions = main.query_one(SuggestionList)
    composer = main.query_one(InputBar)
    composer.value = ""
    composer.focus_input()
    # The current workspace lists one reference folder; with none, this window's list is empty.
    await asyncio.to_thread(registry.update, current.id, name=current.name, primary=current.primary_cwd, folders=[])
    main._refresh_workspace()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and not snapshot.workspace(current.id).working_dirs,
        pilot=pilot,
    )
    added = tmp_path / "added-reference"
    await asyncio.to_thread(added.mkdir)
    started, release = Event(), Event()

    def held_read(*args):
        started.set()
        release.wait(10)
        return read_workspace_order(*args)

    monkeypatch.setattr(screen_module, "read_workspace_order", held_read)
    # Another window adds a reference folder; this window last read none.
    await asyncio.to_thread(
        registry.update, current.id, name=current.name, primary=current.primary_cwd, folders=[str(added)]
    )
    try:
        await pilot.press(*"/workspace remove-folder ")
        # The list stays open on its no-results row while the reread runs.
        await wait_for(
            lambda: started.is_set() and suggestions.is_visible and suggestions._values == [None], pilot=pilot
        )
        if key == "escape":
            await pilot.press("escape")
            assert not suggestions.is_visible
    finally:
        release.set()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and snapshot.workspace(current.id).working_dirs,
        pilot=pilot,
    )
    await pilot.pause()
    if key == "escape":
        assert not suggestions.is_visible
        return
    assert suggestions.is_visible and suggestions._values == [str(added)]
    await pilot.press("enter")
    assert composer.value == f"/workspace remove-folder {added}"
    assert [folder.path for folder in registry.snapshot().workspace(current.id).working_dirs] == [str(added)]


@pytest.mark.parametrize(
    ("action", "argument", "key"),
    [
        pytest.param("switch", "partial", "enter", id="switch-partial-enter"),
        pytest.param("delete", "partial", "enter", id="delete-partial-enter"),
        pytest.param("delete", "exact", "enter", id="delete-exact-enter"),
        pytest.param("delete", "empty", "enter", id="delete-empty-enter"),
        pytest.param("delete", "partial", "tab", id="delete-partial-tab"),
    ],
)
async def test_named_workspace_argument_completion(live_workspace_chat, action, argument, key):
    pilot, main, engine, registry, current, target = live_workspace_chat
    # A workspace name may itself be an action; completion must keep it as the argument.
    renamed = await asyncio.to_thread(registry.update, target.id, name="switch", primary=target.primary_cwd)
    main._refresh_workspace()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and snapshot.workspace(target.id) == renamed,
        pilot=pilot,
    )
    composer = main.query_one(InputBar)
    suggestions = main.query_one(SuggestionList)
    composer.focus_input()
    value = {"partial": "swi", "exact": renamed.name, "empty": ""}[argument]
    await pilot.press(*f"/workspace {action} {value}")
    await wait_for(lambda: suggestions.is_visible and suggestions.mode == "subcommands", pilot=pilot)
    selected = current if argument == "empty" else renamed
    assert suggestions._values == ([current.name, renamed.name] if argument == "empty" else [renamed.name])
    assert suggestions.render().plain.splitlines() == (
        [f"◦ {current.name}", f"  {renamed.name}"] if argument == "empty" else [f"  {renamed.name}"]
    )
    await pilot.press(key)
    if argument != "exact":
        assert composer.value == f"/workspace {action} {selected.name}"
        assert engine.session.workspace_id == current.id
        assert registry.snapshot().workspaces == (current, renamed)
        assert pilot.app.screen is main
        if key == "tab":
            assert not suggestions.is_visible
        if argument == "empty":
            return
        await pilot.press("enter")
    assert composer.value == ""
    if action == "switch":
        await wait_for(lambda: engine.session.workspace_id == target.id, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        assert registry.snapshot().workspaces == (current, renamed)
    else:
        await wait_for(lambda: registry.snapshot().workspaces == (current,), pilot=pilot)
        assert registry.snapshot().workspace(target.id) is None
        assert engine.workspace == current


async def test_switch_workspace_completion_does_not_execute_a_highlighted_longer_name(live_workspace_chat, tmp_path):
    pilot, main, engine, registry, current, previous_target = live_workspace_chat
    directory = tmp_path / "shorter"
    await asyncio.to_thread(directory.mkdir)
    longer = await asyncio.to_thread(
        registry.update, previous_target.id, name="Target 2", primary=previous_target.primary_cwd
    )
    target = await asyncio.to_thread(registry.create, "Target", str(directory))
    main._refresh_workspace()
    await wait_for(
        lambda: (snapshot := main._workspace_snapshot) is not None and snapshot.workspace(target.id) == target,
        pilot=pilot,
    )
    composer = main.query_one(InputBar)
    suggestions = main.query_one(SuggestionList)
    composer.focus_input()
    await pilot.press(*"/workspace switch target")
    assert suggestions._values == [target.name, longer.name]
    await pilot.press("down", "enter")
    assert composer.value == "/workspace switch Target 2"
    assert engine.session.workspace_id == current.id

    composer.value = ""
    await pilot.press(*"/workspace switch Target", "enter")
    assert composer.value == ""
    await wait_for(lambda: engine.session.workspace_id == target.id, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)


@pytest.mark.parametrize("folded", ["command", "argument", "none"])
async def test_workspace_delete_executes_only_after_its_command_is_visible(live_workspace_chat, folded):
    pilot, main, _engine, registry, current, target = live_workspace_chat
    composer = main.query_one(InputBar)
    chat = composer.query_one("#chat-input")
    command = f"/workspace delete {target.name}"
    composer.focus_input()
    if folded == "none":
        await pilot.press(*command, "enter")
    else:
        prefix = "/workspace delete " if folded == "argument" else ""
        composer.value = prefix
        pasted = command.removeprefix(prefix) + "\n" * 7
        chat.post_message(events.Paste(pasted))
        await wait_for(lambda: composer.value.startswith(prefix + "[Pasted text #1"), pilot=pilot)

        await pilot.press("enter")

        assert registry.snapshot().workspaces == (current, target)
        assert composer.value == command + "\n" * 7
        assert not composer.draft().references
        await pilot.press("enter")

    await wait_for(lambda: registry.snapshot().workspaces == (current,), pilot=pilot)
    assert composer.value == ""


@pytest.mark.parametrize("action", ["switch", "delete"])
async def test_workspace_enter_completes_actions_and_esc_submits_draft(live_workspace_chat, action):
    pilot, main, _engine, _registry, current, _target = live_workspace_chat
    app = pilot.app
    composer = main.query_one(InputBar)
    suggestions = main.query_one(SuggestionList)
    composer.focus_input()
    await pilot.press(*"/workspace")
    await wait_for(lambda: suggestions.is_visible and "workspace" in suggestions._values, pilot=pilot)
    await pilot.press("enter")
    assert app.screen is main and composer.value == "/workspace "
    assert suggestions.mode == "subcommands"
    assert suggestions._values == ["switch", "new", "rename", "add-folder", "remove-folder", "delete"]
    if action == "delete":
        await pilot.press(*"del")
    await pilot.press("enter")
    assert app.screen is main and composer.value == f"/workspace {action} "
    expected = ["◦ Current", "  Target"]
    if action == "switch":
        expected.append("  Unspecified")
    assert suggestions.mode == "subcommands" and suggestions.render().plain.splitlines() == expected
    await pilot.press("escape")
    assert not suggestions.is_visible and composer.value == f"/workspace {action} "
    await pilot.press("enter")
    if action == "switch":
        await wait_for(lambda: isinstance(app.screen, WorkspacesScreen) and app.screen.is_mounted, pilot=pilot)
        options = app.screen.query_one(OptionList)
        await wait_for(lambda: options.option_count > 0, pilot=pilot)
        assert options.get_option_at_index(options.highlighted).id == current.id
    else:
        await wait_for(
            lambda: any(notice.message == "Usage: /workspace delete <name>" for notice in app._notifications),
            pilot=pilot,
        )
        assert app.screen is main


@pytest.mark.parametrize(
    ("locale", "unspecified", "buttons"),
    [
        ("en", "Unspecified", ["Move this session", "New session", "Cancel"]),
        ("zh-Hans", "未指定", ["移动这个会话", "新会话", "取消"]),
    ],
    ids=["en", "zh-Hans"],
)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_switch_completion_confirms_with_history_and_marks_unspecified(
    live_workspace_chat, locale, unspecified, buttons
):
    pilot, main, engine, registry, current, _target = live_workspace_chat
    app = pilot.app
    app.locale_controller.switch_locale(locale)
    composer = main.query_one(InputBar)
    composer.value = "An existing conversation"
    await composer.action_submit()
    await wait_for(lambda: main.has_messages_state, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    await drain(engine)
    session_id = engine.session_id

    composer.focus_input()
    await pilot.press(*"/workspace switch ")
    suggestions = main.query_one(SuggestionList)
    await wait_for(lambda: suggestions.is_visible and suggestions.mode == "subcommands", pilot=pilot)
    assert suggestions.render().plain.splitlines() == ["◦ Current", "  Target", f"  {unspecified}"]
    assert suggestions._values == ["Current", "Target", unspecified]
    await pilot.press("down", "down", "enter")
    assert composer.value == f"/workspace switch {unspecified}"
    await pilot.press("enter")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    assert [button.label.plain for button in app.screen.query(Button)] == buttons
    changes = await capture_events(engine.event_bus, WorkspaceChange)
    await click_when_settled(pilot, "#confirm-no")
    await wait_for(lambda: app.screen is main, pilot=pilot)
    assert changes == []
    assert engine.session_id == session_id
    assert engine.session.workspace_id == current.id
    assert registry.snapshot().workspace_id_for_session(session_id) == current.id
    composer.value = f"/workspace switch {unspecified}"
    await composer.action_submit()
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    assert [button.label.plain for button in app.screen.query(Button)] == buttons
    await click_when_settled(pilot, "#confirm-yes")
    await wait_for(
        lambda: app.screen is main and main._state.workspace.membership is None,
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    assert engine.session_id == session_id

    composer.focus_input()
    await pilot.press(*"/workspace switch ")
    assert suggestions.is_visible
    assert suggestions.render().plain.splitlines() == ["  Current", "  Target", f"◦ {unspecified}"]
    await pilot.press("escape")
    composer.value = ""
    await pilot.press(*"/workspace ")
    assert suggestions.is_visible and suggestions.mode == "subcommands"
    assert suggestions._values == ["switch", "new", "delete"]


@pytest.mark.parametrize("exact", [False, True], ids=["empty-argument", "full-path"])
async def test_workspace_remove_folder_completion_removes_only_the_reference_folder(live_workspace_chat, exact):
    pilot, main, engine, registry, current, _target = live_workspace_chat
    composer = main.query_one(InputBar)
    composer.focus_input()
    (reference,) = current.working_dirs
    command = "/workspace remove-folder "
    await pilot.press(*(command + (reference.path if exact else "")))
    suggestions = main.query_one(SuggestionList)
    await wait_for(lambda: suggestions.is_visible and suggestions.mode == "subcommands", pilot=pilot)
    assert suggestions.render().plain.splitlines() == [reference.path]
    await pilot.press("enter")
    if not exact:
        assert composer.value == command + reference.path
        assert registry.snapshot().workspace(current.id) == current
        await pilot.press("enter")
    assert composer.value == ""
    await wait_for(lambda: not engine.workspace.working_dirs, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    saved = registry.snapshot().workspace(current.id)
    assert saved is not None and saved.working_dirs == []
    assert saved.primary_cwd == current.primary_cwd


@pytest.mark.skipif(sys.platform == "win32", reason="Win32 directory creation strips trailing spaces")
@pytest.mark.parametrize("use_completion", [False, True], ids=["typed", "completed"])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_folder_commands_round_trip_a_path_with_trailing_space(live_workspace_chat, use_completion):
    pilot, main, engine, registry, current, _ = live_workspace_chat
    (original,) = current.working_dirs
    spaced = Path(original.path + " ")
    await asyncio.to_thread(spaced.mkdir, exist_ok=True)
    if await asyncio.to_thread(spaced.samefile, original.path):
        pytest.skip("The filesystem treats paths with and without a trailing space as the same directory")

    composer = main.query_one(InputBar)
    composer.value = f"/workspace add-folder {spaced}"
    await composer.action_submit()
    await wait_for(
        lambda: (
            pilot.app.screen is main
            and [folder.path for folder in engine.workspace.working_dirs] == [original.path, str(spaced)]
        ),
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )

    command = f"/workspace remove-folder {spaced}"
    if use_completion:
        await wait_for(
            lambda: (str(spaced), str(spaced)) in main._workspace_picker.argument_choices("remove-folder"), pilot=pilot
        )
        composer.focus_input()
        await pilot.press("/")
        composer.value = command
        suggestions = main.query_one(SuggestionList)
        await wait_for(lambda: suggestions.is_visible and suggestions._values == [str(spaced)], pilot=pilot)
        await pilot.press("enter")
    else:
        composer.value = command
        await composer.action_submit()

    await wait_for(
        lambda: pilot.app.screen is main and engine.workspace.working_dirs == [original],
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    assert registry.snapshot().workspace(current.id).working_dirs == [original]


async def test_sessions_open_while_a_message_is_being_prepared(live_workspace_chat):
    pilot, main, _engine, _registry, _current, _target = live_workspace_chat
    from chrys.app.tui.screens.sessions import SessionsScreen

    main._state.submit.active = True
    try:
        main._navigation.sessions()
        await wait_for(
            lambda: isinstance(pilot.app.screen, SessionsScreen) and pilot.app.screen.is_mounted, pilot=pilot
        )
    finally:
        main._state.submit.active = False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX folder names may contain line breaks")
async def test_remove_folder_does_not_offer_a_multiline_argument(live_workspace_chat, tmp_path):
    pilot, main, _, registry, current, _ = live_workspace_chat
    paths = [tmp_path / name for name in ("good", "bad\npath", "bad\rpath")]
    for path in paths:
        await asyncio.to_thread(path.mkdir)
    updated = await asyncio.to_thread(
        registry.update,
        current.id,
        name=current.name,
        primary=current.primary_cwd,
        folders=tuple(map(str, paths)),
    )
    main._refresh_workspace()
    await wait_for(
        lambda: main._workspace_snapshot is not None and main._workspace_snapshot.workspace(current.id) == updated,
        pilot=pilot,
    )
    assert main._workspace_picker.argument_choices("remove-folder") == [(str(paths[0]), str(paths[0]))]


async def test_picker_snapshot_survives_an_older_main_read(live_workspace_chat, tmp_path, monkeypatch):
    from threading import Event

    from chrys.app.tui.screens.main import screen as main_module

    pilot, main, _, registry, _, _ = live_workspace_chat
    await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
    entered, release = Event(), Event()
    read = main_module.read_workspace_order
    first = True

    def held_read(registry, cwd, current_id, cached=None):
        nonlocal first
        order = read(registry, cwd, current_id, cached)
        if first:
            first = False
            entered.set()
            if not release.wait(10):
                raise TimeoutError("old workspace read was not released")
        return order

    monkeypatch.setattr(main_module, "read_workspace_order", held_read)
    main._refresh_workspace()
    try:
        await wait_for(entered.is_set, pilot=pilot)
        folder = tmp_path / "new-workspace"
        await asyncio.to_thread(folder.mkdir)
        added = await asyncio.to_thread(registry.create, "New snapshot", str(folder))
        main._workspace_picker.open()
        await wait_for(
            lambda: (
                isinstance(pilot.app.screen, WorkspacesScreen)
                and pilot.app.screen.is_mounted
                and pilot.app.screen.query_one(OptionList).option_count == 5
            ),
            pilot=pilot,
        )
        assert main._workspace_snapshot.workspace(added.id) == added
    finally:
        release.set()
    await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
    assert main._workspace_snapshot.workspace(added.id) == added
    assert (added.name, "  " + added.name) in main._workspace_picker.argument_choices("delete")


async def test_a_delete_suggestion_does_not_delete_a_same_named_replacement(live_workspace_chat, tmp_path):
    """Another window deletes the listed workspace and reuses its name: Enter deletes nothing."""
    from chrys.service.workspaces.registry import WorkspaceRegistry

    pilot, main, _engine, registry, _current, target = live_workspace_chat
    app = pilot.app
    await wait_for(
        lambda: any(pair[0] == target.name for pair in main._workspace_picker.argument_choices("delete")), pilot=pilot
    )
    composer = main.query_one(InputBar)
    suggestions = main.query_one(SuggestionList)
    composer.focus_input()
    await pilot.press(*"/workspace delete Target")
    await wait_for(lambda: suggestions._values == ["Target"], pilot=pilot)
    replacement_dir = tmp_path / "replacement"
    replacement_dir.mkdir()
    other_window = WorkspaceRegistry(registry.path)
    await asyncio.to_thread(other_window.delete, target.id)
    replacement = await asyncio.to_thread(other_window.create, target.name, str(replacement_dir))
    await asyncio.to_thread(other_window.set_session_workspace, "other-chat", replacement.id)
    await pilot.press("enter")
    refused = (
        "Workspace “Target” was replaced elsewhere, so nothing was deleted. Check the workspace list and try again."
    )
    await wait_for(
        lambda: any(notice.message == refused for notice in app._notifications),
        pilot=pilot,
        timeout=ENGINE_TURN_TIMEOUT,
    )
    snapshot = registry.snapshot()
    assert snapshot.workspace(replacement.id) is not None
    assert snapshot.maps_session("other-chat")

    # The refusal refreshes the list, so running it again deletes the workspace now listed.
    await wait_for(lambda: main._workspace_picker._snapshot().workspace(replacement.id) is not None, pilot=pilot)
    composer.focus_input()
    await pilot.press(*"/workspace delete Target")
    await wait_for(lambda: suggestions._values == ["Target"], pilot=pilot)
    await pilot.press("enter")
    await wait_for(
        lambda: registry.snapshot().workspace(replacement.id) is None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
    )


async def test_workspace_candidates_leave_when_the_draft_no_longer_names_the_command(live_workspace_chat):
    pilot, main, _engine, registry, _current, target = live_workspace_chat
    composer = main.query_one(InputBar)
    suggestions = main.query_one(SuggestionList)
    composer.focus_input()
    await pilot.press(*"/workspace delete Tar")
    await wait_for(
        lambda: suggestions.is_visible and suggestions.render().plain.splitlines() == ["  Target"], pilot=pilot
    )
    await pilot.press(*["backspace"] * len(" delete Tar"), "backspace")
    assert composer.value == "/workspac"
    await wait_for(lambda: suggestions.mode == "commands" and "workspace" in suggestions._values, pilot=pilot)
    await pilot.press("enter")
    assert composer.value == "/workspace "
    assert suggestions.mode == "subcommands"
    assert registry.snapshot().workspace(target.id) == target
