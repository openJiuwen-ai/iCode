# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The sidebar Workspaces page lists, opens and refreshes sessions by workspace."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.widgets import Button, OptionList, TabbedContent, Tree

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.workspace_branch import WorkspaceBranchController
from chrys.app.tui.screens.workspaces.edit import WorkspaceEditDialog
from chrys.app.tui.screens.workspaces.picker import WorkspacesScreen
from chrys.app.tui.terminal.panel import ShellPanel
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets import EnhancedInput
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.workspaces import SavedWorkspaceSession, WorkspacePanel
from chrys.foundation.events.types import (
    BackgroundTasksChanged,
    SessionNew,
    SessionReady,
    SessionRestore,
)
from chrys.foundation.models.background_tasks import BackgroundTaskView
from chrys.service.state.store import JsonFileStateStore
from tests.support.event_capture import capture_events
from tests.support.pilot_barrier import screen_is_settled
from tests.support.rollback_spec import PROFILE
from tests.support.sessions_browser import FakeSessionStore
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import (
    cache_workspace_page,
    click_when_settled,
    current_row_label,
    resize_when_settled,
    settle_workspace_page_reads,
    workspace_session_titles,
)
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    MAIN_SCREEN_BUILD_TIMEOUT,
    wait_for,
    with_wait_deadline,
)


@pytest.mark.parametrize("restoring", [False, True], ids=["new-session", "restore"])
async def test_visible_workspace_sidebar_loads_snapshot_cached_before_session_ready(tmp_path, monkeypatch, restoring):
    """a new session binds the page on SessionReady; a restore waits until it is committed."""
    monkeypatch.setattr(WorkspaceBranchController, "start", lambda _self, _cwd: None)
    store = JsonFileStateStore(tmp_path / "sessions")
    workspace = store.workspace_registry.create("Startup workspace", str(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", state_store=store)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(
            lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
            pilot=pilot,
            timeout=MAIN_SCREEN_BUILD_TIMEOUT,
        )
        main = app.screen
        panel = main.query_one(WorkspacePanel)
        tree = panel.query_one(Tree)
        await wait_for(
            lambda: is_widget_shown_on_active_screen(panel) and main._workspace_snapshot is not None,
            pilot=pilot,
        )
        assert not main._state.workspace.workspace_known
        assert not tree.display

        main._set_restoring_session(restoring)
        await main._services.bus.publish(
            SessionReady(session_id="current", primary_cwd=str(tmp_path), workspace=workspace),
            raise_handler_errors=True,
        )
        await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
        if restoring:
            # Not yet the restored chat: the page neither reads nor names a current chat.
            await pilot.pause()
            assert not tree.display
            assert panel._registry is None
            # SessionRestored clears the flag and names the chat before it refreshes the workspace.
            main._set_restoring_session(False)
            main._view_adapter.set_chat_session_id("current")
            main._refresh_session_workspace()
        await settle_workspace_page_reads(panel)

        assert tree.display
        assert any(node.data == f"__workspace__:{workspace.id}" for node in tree.root.children)
        assert "current" in workspace_session_titles(panel)


@pytest.mark.parametrize(("action", "expected"), [("Edit", "Renamed target"), ("Delete", "")])
async def test_workspace_sidebar_reflects_picker_changes_before_close(live_workspace_chat, action, expected):
    pilot, main, _engine, _registry, _current, target = live_workspace_chat
    app = pilot.app
    await resize_when_settled(pilot, 170, 36)
    main.query_one(SidebarPanel).query_one(TabbedContent).active = "tab-workspace"
    tree = main.query_one(WorkspacePanel).query_one(Tree)
    target_group = f"__workspace__:{target.id}"

    def target_name() -> str:
        label = next((node.label for node in tree.root.children if node.data == target_group), "")
        return str(label).lstrip().partition(" ▾")[0]

    await wait_for(lambda: target_name() == target.name, pilot=pilot)
    composer = main.query_one(InputBar)
    composer.value = "/workspace switch"
    await composer.action_submit()
    await wait_for(lambda: isinstance(app.screen, WorkspacesScreen) and app.screen.is_mounted, pilot=pilot)
    picker = app.screen
    options = picker.query_one(OptionList)
    await wait_for(lambda: options.option_count > 0, pilot=pilot)
    options.highlighted = options.get_option_index(target.id)
    await wait_for(lambda: screen_is_settled(app, picker), pilot=pilot)
    line, rendered = next(
        (index, options.render_line(index).text)
        for index in range(options.content_region.height)
        if target.name in options.render_line(index).text and action in options.render_line(index).text
    )
    x = options.content_region.x - options.region.x + cell_len(rendered[: rendered.rindex(action)])
    await click_when_settled(pilot, options, offset=(x, line))

    if action == "Edit":
        await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
        app.screen.query_one("#workspace-name", EnhancedInput).value = "Renamed target"
        await click_when_settled(pilot, "#workspace-save")
    else:
        await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
        await click_when_settled(pilot, "#confirm-yes")

    await wait_for(
        lambda: isinstance(app.screen, WorkspacesScreen) and app.screen is not picker and app.screen.is_mounted,
        pilot=pilot,
    )
    reopened = app.screen
    await wait_for(
        lambda: target_name() == expected,
        pilot=pilot,
        description=f"the sidebar reflects {action.lower()} while the picker is open",
    )
    assert app.screen is reopened


async def test_workspace_tree_retains_keyboard_focus_only_in_shell_mode(workspace_main, monkeypatch):
    """The workspace page follows ConversationToc's chat/shell focus contract."""
    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    requests = []

    async def record_restore(event: SessionRestore):
        requests.append(event)

    panel = sidebar.query_one(WorkspacePanel)
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    await wait_for(lambda: is_widget_shown_on_active_screen(panel) and not panel._refresh_pending, pilot=pilot)
    await settle_workspace_page_reads(panel)
    now = datetime.now(UTC)
    workspace = panel._workspace
    assert workspace is not None
    cache_workspace_page(
        panel,
        (workspace,),
        {workspace.id: [SavedWorkspaceSession("first", "First", now), SavedWorkspaceSession("second", "Second", now)]},
        render=True,
    )
    tree = panel.query_one(Tree)
    await wait_for(lambda: tree.display and screen_is_settled(app, main), pilot=pilot)
    main.set_focus(None)
    tree.focus()
    await pilot.pause()
    await wait_for(lambda: main.query_one(InputBar).has_focus_within, pilot=pilot)
    assert main.focused is not tree
    monkeypatch.setattr(main.query_one(ShellPanel), "_start_shell", lambda _cwd: None)
    main.shell_mode_state = True
    tree.focus()
    await wait_for(lambda: main.focused is tree, pilot=pilot)
    await main._services.bus.subscribe(SessionRestore, record_restore)
    tree.cursor_line = next(index for index, node in enumerate(tree.root.children) if node.data == "first")
    await pilot.press("down", "enter")
    await wait_for(lambda: len(requests) == 1, pilot=pilot)
    assert requests[0].session_id == "second"


async def _click_workspace_symbol(pilot, panel, group: str, symbol: str) -> None:
    """Click a rendered action using the cells the user sees, without widget internals."""
    tree = panel.query_one(Tree)

    def action_is_ready() -> bool:
        return (
            pilot.app.screen is panel.screen
            and tree.display
            and screen_is_settled(pilot.app, panel.screen)
            and any(
                str(node.label).lstrip().startswith(group + " ▾")
                and symbol in str(node.label)
                and node.label.cell_len == tree.scrollable_content_region.width
                for node in tree.root.children
            )
        )

    await wait_for(action_is_ready, pilot=pilot, description=f"the {symbol} action for {group} is laid out")
    line, node = next(
        (line, node)
        for line, node in enumerate(tree.root.children)
        if str(node.label).lstrip().startswith(group + " ▾")
    )
    column = str(node.label).index(symbol)
    await click_when_settled(
        pilot,
        tree,
        offset=(
            tree.content_region.x - tree.region.x + column,
            tree.content_region.y - tree.region.y + line - tree.scroll_offset.y,
        ),
    )


@pytest.mark.parametrize("width", [80, 120])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_sidebar_group_actions_hit_rendered_cells(live_workspace_chat, tmp_path, width):
    pilot, main, engine, registry, current, target = live_workspace_chat
    app = pilot.app
    await resize_when_settled(pilot, width, 36)
    panel = main.query_one(WorkspacePanel)
    tree = panel.query_one(Tree)
    groups = {current.name: current, target.name: target}
    for index in range(5):
        name = f"Team {index:02}"
        directory = tmp_path / name
        await asyncio.to_thread(directory.mkdir)
        groups[name] = await asyncio.to_thread(registry.create, name, str(directory))
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)

    def painted_cell(label: str, symbol: str):
        region = tree.scrollable_content_region
        strips = main._compositor.render_strips()[region.y : region.bottom]
        for line, strip in enumerate(strips):
            row = strip.crop(region.x, region.right).text
            if row.lstrip().startswith(label) and symbol in row:
                return region.x + cell_len(row[: row.index(symbol)]), region.y + line
        return None

    async def click_cell(position):
        x, y = position
        await click_when_settled(pilot, tree, offset=(x - tree.region.x, y - tree.region.y))

    async def exercise_group(name):
        label = name + " ▾"
        await wait_for(lambda: screen_is_settled(app, main) and painted_cell(label, "✎"), pilot=pilot)
        pencil = painted_cell(label, "✎")
        plus = painted_cell(label, "+")
        assert pencil is not None and plus is not None
        await click_cell(pencil)
        await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
        assert app.screen.query_one("#workspace-name", EnhancedInput).value == name
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        before = engine.session_id
        await click_cell(plus)
        await wait_for(
            lambda: engine.session_id != before and main._state.workspace.workspace_id == groups[name].id,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await settle_workspace_page_reads(panel)
        return plus[0], pencil[0]

    await wait_for(lambda: screen_is_settled(app, main) and painted_cell("Show more", "S"), pilot=pilot)
    assert not tree.show_vertical_scrollbar
    columns_without_scrollbar = await exercise_group(current.name)
    assert await exercise_group(target.name) == columns_without_scrollbar

    await wait_for(lambda: screen_is_settled(app, main) and painted_cell("Show more", "S"), pilot=pilot)
    await click_cell(painted_cell("Show more", "S"))
    await settle_workspace_page_reads(panel)
    await wait_for(lambda: len(panel._group_nodes) == len(groups), pilot=pilot)
    await resize_when_settled(pilot, width, 22)
    await wait_for(lambda: screen_is_settled(app, main) and tree.show_vertical_scrollbar, pilot=pilot)
    last = "Team 04"
    assert painted_cell(last + " ▾", "✎") is None
    tree.scroll_end(animate=False, immediate=True)
    await wait_for(lambda: tree.scroll_y > 0, pilot=pilot)
    assert await exercise_group(last) == columns_without_scrollbar


@pytest.mark.parametrize("action", ["new", "edit"])
@pytest.mark.parametrize("busy", ["running", "loading"])
async def test_workspace_page_actions_share_session_busy_feedback(workspace_main, action, busy):
    """workspace + and pencil both explain running/loading refusals and keep the current chat."""
    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    panel = sidebar.query_one(WorkspacePanel)
    requests = await capture_events(main._services.bus, SessionNew)
    workspace = main._state.workspace.membership
    assert workspace is not None
    before = main.chat_session_id
    main._state.run.agent_running = busy == "running"
    main._set_agent_loading(busy == "loading")
    try:
        await _click_workspace_symbol(pilot, panel, "Workspace", "+" if action == "new" else "✎")
        message = (
            "Cannot switch sessions while agent is busy"
            if action == "new"
            else "Cannot edit a workspace while agent is busy"
        )
        await wait_for(lambda: any(notice.message == message for notice in app._notifications), pilot=pilot)
        assert requests == []
        assert app.screen is main
        assert main.chat_session_id == before
    finally:
        main._state.run.agent_running = False
        main._set_agent_loading(False)
    # The same rendered control acts again as soon as the session is idle.
    await _click_workspace_symbol(pilot, panel, "Workspace", "+" if action == "new" else "✎")
    if action == "new":
        await wait_for(lambda: bool(requests), pilot=pilot)
        assert requests[0].workspace_id == workspace.id
    else:
        await wait_for(lambda: isinstance(app.screen, WorkspaceEditDialog) and app.screen.is_mounted, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main and main.query_one(InputBar).has_focus_within, pilot=pilot)


@pytest.mark.parametrize("unspecified", [False, True], ids=["workspace-plus", "unspecified-plus"])
async def test_workspace_page_new_session_asks_before_stopping_background_tasks(workspace_main, unspecified):
    """both + links use /new's confirmation; cancel keeps the current session."""
    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    panel = sidebar.query_one(WorkspacePanel)
    requests = await capture_events(main._services.bus, SessionNew)
    workspace = main._state.workspace.membership
    assert workspace is not None
    if unspecified:
        await main._services.state_store.save_session("plain", {"messages": []}, title="Plain")
        panel.refresh_sessions()
        await settle_workspace_page_reads(panel)
    previous = main.chat_session_id
    await main._services.bus.publish(
        BackgroundTasksChanged(
            session_id=previous,
            tasks=(
                BackgroundTaskView(
                    "task", "server", "", str(main._services.workspaces.path.parent), "user", 1, (), "", 1
                ),
            ),
        ),
        raise_handler_errors=True,
    )
    group = "Unspecified" if unspecified else "Workspace"
    await _click_workspace_symbol(pilot, panel, group, "+")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    assert requests == []
    await click_when_settled(pilot, "#confirm-no")
    await wait_for(lambda: app.screen is main, pilot=pilot)
    assert requests == []
    assert main.chat_session_id == previous
    await _click_workspace_symbol(pilot, panel, group, "+")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    await click_when_settled(pilot, "#confirm-yes")
    await wait_for(lambda: bool(requests), pilot=pilot)
    assert requests[0].workspace_id == (None if unspecified else workspace.id)


async def test_workspace_page_opens_sessions_of_any_workspace_and_refuses_unlisted_ones(
    workspace_main, tmp_path, monkeypatch
):
    """every listed session opens, whatever its workspace; while the agent loads a click explains
    why nothing happens; a queued click on one no longer listed does nothing."""
    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    panel = sidebar.query_one(WorkspacePanel)
    registry = main._services.workspaces
    other_cwd = tmp_path / "other-workspace"
    other_cwd.mkdir()
    other = registry.create("Other", str(other_cwd))
    now = datetime.now(UTC)
    panel._workspaces = registry.snapshot().workspaces
    panel._ordered = panel._workspaces
    panel._saved = {other.id: [SavedWorkspaceSession("elsewhere", "Elsewhere", now)]}
    panel._render_rows()
    started = []
    monkeypatch.setattr(main, "_start_worker", started.append)
    before = len(app._notifications)
    main._workspace_session_selected(WorkspacePanel.SessionSelected("elsewhere"))
    assert len(started) == 1
    main._set_agent_loading(True)
    try:
        main._workspace_session_selected(WorkspacePanel.SessionSelected("elsewhere"))
    finally:
        main._set_agent_loading(False)
    assert len(started) == 1
    panel._saved = {}
    panel._render_rows()
    main._workspace_session_selected(WorkspacePanel.SessionSelected("elsewhere"))
    assert len(started) == 1
    await pilot.pause()
    assert [notice.message for notice in app._notifications][before:] == ["Cannot switch sessions while agent is busy"]


@pytest.mark.parametrize("submitting", ["no", "before-click", "after-click"])
async def test_workspace_sidebar_session_click_explains_an_inflight_submit(workspace_main, monkeypatch, submitting):
    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    panel = sidebar.query_one(WorkspacePanel)
    now = datetime.now(UTC)
    workspace_id = main._state.workspace.workspace_id
    panel._saved = {workspace_id: [SavedWorkspaceSession("other", "Other", now)]}
    panel._render_rows()
    flashes = create_autospec(main._view_adapter.flash_submit_still_sending)
    monkeypatch.setattr(main._view_adapter, "flash_submit_still_sending", flashes)
    requests = await capture_events(main._services.bus, SessionRestore)
    notices = len(app._notifications)
    main._state.submit.active = submitting == "before-click"
    main._workspace_session_selected(WorkspacePanel.SessionSelected("other"))
    main._state.submit.active = submitting != "no"
    for worker in list(main.workers):
        if worker.node is main:
            await worker.wait()
    await pilot.pause()
    if submitting != "no":
        flashes.assert_called_once_with()
        assert requests == []
        assert len(app._notifications) == notices
    else:
        flashes.assert_not_called()
        assert [request.session_id for request in requests] == ["other"]


async def test_workspace_page_refreshes_for_a_switch_made_under_a_dialog(workspace_main, monkeypatch):
    """A session change updates the workspace page while the loading dialog is open."""
    from textual.screen import ModalScreen

    from chrys.service.state.store import ChatSessionMeta

    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    workspace_id = main._state.workspace.workspace_id

    titles = {"left": "Left", "entered": "Entered"}
    for session_id in titles:
        main._services.workspaces.set_session_workspace(session_id, workspace_id)

    store = FakeSessionStore(0)
    store.sessions = [
        ChatSessionMeta(
            session_id=session_id,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            agent_profile="",
            agent_display_name="",
            message_count=1,
            title=title,
        )
        for session_id, title in titles.items()
    ]

    monkeypatch.setattr(main._services.state_store, "open_session_listing", store.open_session_listing)
    panel = sidebar.query_one(WorkspacePanel)
    main._view_adapter.set_chat_session_id("left")
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    main._refresh_workspace()
    panel.refresh_sessions()
    await wait_for(lambda: set(workspace_session_titles(panel)) == {"entered", "left"}, pilot=pilot)
    # A restore covers the page with its loading dialog while the main screen keeps repainting.
    dialog = ModalScreen()
    app.push_screen(dialog)
    await wait_for(lambda: app.screen is dialog, pilot=pilot)
    titles["left"] = "Left, saved on leaving"
    main._view_adapter.set_session_title_state(generated=titles["left"])
    main._view_adapter.set_chat_session_id("entered")
    main._refresh_workspace()
    await wait_for(lambda: workspace_session_titles(panel).get("left") == "Left, saved on leaving", pilot=pilot)
    assert app.screen is dialog
    assert "entered" in workspace_session_titles(panel)


async def test_a_chats_first_save_survives_a_list_read_that_started_before_it(workspace_main, tmp_path, monkeypatch):
    """the read adds the save made while it ran, with no second read; leaving the chat shows an ordinary row."""
    from chrys.foundation.events.types import SessionSaved

    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    store = main._services.state_store
    registry = main._services.workspaces
    await store.save_session("neighbour", {"messages": []}, primary_cwd=str(tmp_path), title="Neighbour")
    registry.set_session_workspace("neighbour", main._state.workspace.workspace_id)
    view = main._view_adapter
    view.set_chat_session_id("fresh")
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    main._refresh_workspace()
    panel = sidebar.query_one(WorkspacePanel)
    panel.refresh_sessions()
    await wait_for(lambda: panel.can_restore_session("neighbour"), pilot=pilot)
    await settle_workspace_page_reads(panel)
    original = store.open_session_listing
    captured, release = asyncio.Event(), asyncio.Event()
    reads: list[str] = []

    async def held(*, kind):
        reads.append(kind)
        listing = await original(kind=kind)
        captured.set()
        await release.wait()
        return listing

    monkeypatch.setattr(store, "open_session_listing", held)
    panel.refresh_sessions()
    await wait_for(captured.is_set, pilot=pilot)

    saved = await store.save_session("fresh", {"messages": []}, primary_cwd=str(tmp_path), title="Fresh")
    assert saved is not None and saved.updated_at is not None
    await app._bus.publish(
        SessionSaved(session_id="fresh", updated_at=saved.updated_at, last_surface=saved.last_surface),
        raise_handler_errors=True,
    )
    release.set()
    await settle_workspace_page_reads(panel)
    view.set_chat_session_id("neighbour")
    main._refresh_workspace()
    await settle_workspace_page_reads(panel)
    assert panel.can_restore_session("fresh")
    assert reads == ["chat"]


@pytest.mark.parametrize("transparent_first", [False, True], ids=["opaque", "transparent-then-opaque"])
async def test_workspace_page_reads_again_after_a_full_screen_covered_it(
    workspace_main, tmp_path, monkeypatch, transparent_first
):
    """A chat another window saves while a full screen (such as the diff) covers the page shows once it closes."""
    from textual.screen import ModalScreen, Screen

    from chrys.app.tui.util.workspace_order import WorkspaceOrder

    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    store = main._services.state_store
    registry = main._services.workspaces
    workspace_id = main._state.workspace.workspace_id
    await store.save_session("neighbour", {"messages": []}, primary_cwd=str(tmp_path), title="Neighbour")
    registry.set_session_workspace("neighbour", workspace_id)
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    main._refresh_workspace()
    panel = sidebar.query_one(WorkspacePanel)
    panel.refresh_sessions()
    await wait_for(lambda: "neighbour" in workspace_session_titles(panel), pilot=pilot)
    await settle_workspace_page_reads(panel)

    snapshot = registry.snapshot()
    old_order = WorkspaceOrder(snapshot, main._workspace_cwd(), workspace_id, panel._ordered)
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(store, "open_session_listing", listing)
    if transparent_first:
        dialog = ModalScreen()
        app.push_screen(dialog)
        await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot)
    cover = Screen()
    app.push_screen(cover)
    await wait_for(lambda: app.screen is cover and cover.is_mounted, pilot=pilot)
    # A handoff may carry the registry from before the other window's save.
    panel.workspace_order_changed(old_order)
    # Another window saves: no event reaches this one.
    await store.save_session("elsewhere", {"messages": []}, primary_cwd=str(tmp_path), title="Elsewhere")
    registry.set_session_workspace("elsewhere", workspace_id)
    await settle_workspace_page_reads(panel)
    listing.assert_not_called()
    app.pop_screen()
    if transparent_first:
        await wait_for(lambda: app.screen is dialog, pilot=pilot)
        app.pop_screen()
    await wait_for(lambda: app.screen is main, pilot=pilot)

    await wait_for(lambda: "elsewhere" in workspace_session_titles(panel), pilot=pilot)
    await settle_workspace_page_reads(panel)
    assert "elsewhere" in [row.session_id for row in panel._saved[workspace_id]]
    assert "elsewhere" not in [row.session_id for row in panel._saved.get("__unspecified__", ())]
    assert listing.call_count == 1


async def test_workspace_page_notices_an_opaque_screen_above_a_transparent_dialog(
    workspace_main, tmp_path, monkeypatch
):
    """The main screen was suspended by the dialog already; a later opaque cover still invalidates the page."""
    from textual.screen import ModalScreen, Screen

    app, pilot, main = workspace_main
    panel = main.query_one(WorkspacePanel)
    await settle_workspace_page_reads(panel)
    store, registry = main._services.state_store, main._services.workspaces
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(store, "open_session_listing", listing)
    dialog, cover = ModalScreen(), Screen()
    app.push_screen(dialog)
    await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot)
    app.push_screen(cover)
    await wait_for(lambda: app.screen is cover and cover.is_mounted, pilot=pilot)
    await store.save_session("external", {"messages": []}, primary_cwd=str(tmp_path), title="External")
    registry.set_session_workspace("external", main._state.workspace.workspace_id)
    await settle_workspace_page_reads(panel)
    listing.assert_not_called()
    app.pop_screen()
    await wait_for(lambda: app.screen is dialog, pilot=pilot)
    await settle_workspace_page_reads(panel)
    assert "external" in workspace_session_titles(panel)
    assert listing.call_count == 1
    app.pop_screen()
    await wait_for(lambda: app.screen is main, pilot=pilot)
    await settle_workspace_page_reads(panel)
    assert "external" in workspace_session_titles(panel)
    assert listing.call_count == 1


async def test_workspace_page_rereads_when_current_chat_changes_during_a_read(workspace_main, tmp_path, monkeypatch):
    """A CLI chat excluded under the old current id must be retained when it becomes current mid-read."""
    from chrys.foundation.models.session_surface import SessionSurface

    _app, pilot, main = workspace_main
    panel = main.query_one(WorkspacePanel)
    await settle_workspace_page_reads(panel)
    store, registry = main._services.state_store, main._services.workspaces
    workspace = main._state.workspace.membership
    await store.save_session(
        "new-current", {"messages": []}, primary_cwd=str(tmp_path), title="Saved CLI", last_surface=SessionSurface.CLI
    )
    registry.set_session_workspace("new-current", workspace.id)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []
    original = store.open_session_listing

    async def held(*, kind):
        reads.append(kind)
        listing = await original(kind=kind)
        entered.set()
        await release.wait()
        return listing

    monkeypatch.setattr(store, "open_session_listing", held)
    panel.refresh_sessions()
    try:
        await wait_for(entered.is_set, pilot=pilot)
        panel.set_session(workspace, store, "new-current", "Runtime CLI", registry=registry)
        await pilot.pause()
        assert reads == ["chat"]
    finally:
        release.set()
        await settle_workspace_page_reads(panel)
    assert reads == ["chat", "chat"]
    assert panel._current_saved is not None and panel._current_saved.session_id == "new-current"
    assert panel._current_listed is False
    assert panel._row_nodes["new-current"].current
    assert workspace_session_titles(panel)["new-current"] == "Runtime CLI"


async def test_workspace_page_reuses_an_unchanged_handoff_without_list_reads(workspace_main, monkeypatch):
    """Transparent dialogs and a handoff of the committed registry do not invalidate the session list."""
    from textual.screen import ModalScreen

    from chrys.app.tui.util.workspace_order import WorkspaceOrder

    app, pilot, main = workspace_main
    panel = main.query_one(WorkspacePanel)
    await settle_workspace_page_reads(panel)
    store = main._services.state_store
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(store, "open_session_listing", listing)
    dialog = ModalScreen()
    app.push_screen(dialog)
    await wait_for(lambda: app.screen is dialog and dialog.is_mounted, pilot=pilot)
    app.pop_screen()
    await wait_for(lambda: app.screen is main, pilot=pilot)
    await settle_workspace_page_reads(panel)
    listing.assert_not_called()
    panel.workspace_order_changed(
        WorkspaceOrder(panel._loaded_snapshot, main._workspace_cwd(), panel._workspace.id, panel._ordered)
    )
    await settle_workspace_page_reads(panel)
    listing.assert_not_called()


@pytest.mark.parametrize("change", ["custom-title", "generated-title", "saved"])
async def test_workspace_page_patches_the_current_row(workspace_main, tmp_path, monkeypatch, change):
    """A normal save or title event updates one group with no registry or listing reads."""
    from chrys.foundation.events.types import SessionSaved, SessionTitleUpdated
    from chrys.kernel import Message

    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    store = main._services.state_store
    registry = main._services.workspaces
    workspace_id = main._state.workspace.workspace_id
    await store.save_session("current", {"messages": []}, primary_cwd=str(tmp_path), title="Before title")
    await store.save_session("neighbour", {"messages": []}, primary_cwd=str(tmp_path), title="Neighbour")
    await store.save_session("loose", {"messages": []}, primary_cwd=str(tmp_path), title="Loose")
    registry.set_session_workspace("current", workspace_id)
    registry.set_session_workspace("neighbour", workspace_id)
    view = main._view_adapter
    view.set_chat_session_id("current")
    view.set_session_title_state(generated="Before title")
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    main._refresh_workspace()
    panel = sidebar.query_one(WorkspacePanel)
    panel.refresh_sessions()
    await wait_for(lambda: set(workspace_session_titles(panel)) == {"current", "neighbour", "loose"}, pilot=pilot)
    await settle_workspace_page_reads(panel)
    tree = panel.query_one(Tree)
    untouched = next(node for node in tree.root.children if node.data == "loose")
    nodes = list(tree.root.children)
    before = panel._row_nodes["current"].updated_at
    assert list(workspace_session_titles(panel))[:2] == ["neighbour", "current"]
    snapshot = create_autospec(registry.snapshot, side_effect=registry.snapshot)
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(registry, "snapshot", snapshot)
    monkeypatch.setattr(store, "open_session_listing", listing)
    if change == "custom-title":
        await main._sessions.apply_custom_session_title("New title", "current")
    elif change == "generated-title":
        await app._bus.publish(SessionTitleUpdated(session_id="current", title="New title"), raise_handler_errors=True)
    else:
        saved = await store.save_session(
            "current", {"messages": [Message("user", ["hello"])]}, primary_cwd=str(tmp_path), title="Before title"
        )
        assert saved is not None and saved.updated_at is not None and saved.updated_at > before
        await app._bus.publish(
            SessionSaved(session_id="current", updated_at=saved.updated_at), raise_handler_errors=True
        )
    expected = "Before title" if change == "saved" else "New title"
    assert main.chat_session_title == expected
    assert workspace_session_titles(panel)["current"] == expected
    await pilot.pause()
    tree = panel.query_one(Tree)
    assert current_row_label(panel).startswith("  " + expected)
    assert expected in "\n".join(tree.render_line(y).text for y in range(tree.size.height))
    await settle_workspace_page_reads(panel)
    snapshot.assert_not_called()
    listing.assert_not_called()
    assert next(node for node in tree.root.children if node.data == "loose") is untouched
    # Rows trade places in their nodes: the tree keeps its structure, so nothing lays out the screen again.
    assert len(tree.root.children) == len(nodes) and all(a is b for a, b in zip(tree.root.children, nodes, strict=True))
    if change == "saved":
        assert panel._row_nodes["current"].updated_at == saved.updated_at
        assert list(workspace_session_titles(panel))[:2] == ["current", "neighbour"]
    else:
        assert panel._row_nodes["current"].updated_at == before
        assert list(workspace_session_titles(panel))[:2] == ["neighbour", "current"]


@pytest.mark.parametrize("mode", ["workflow", "alternate-screen"])
async def test_hidden_workspace_page_retains_runtime_updates_until_its_mode_is_restored(
    workspace_main, monkeypatch, mode
):
    """Workflow and fullscreen suppression never revive an old title after pending storage completes."""
    import asyncio

    from chrys.service.state.session_listing import SessionListing

    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    view = main._view_adapter
    view.set_chat_session_id("current")
    view.set_session_title_state(generated="Before title")
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    main._refresh_workspace()
    panel = sidebar.query_one(WorkspacePanel)
    await settle_workspace_page_reads(panel)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pending(*, kind):
        entered.set()
        await release.wait()
        return SessionListing(kind, ())

    monkeypatch.setattr(main._services.state_store, "open_session_listing", pending)
    panel.refresh_sessions()
    try:
        await wait_for(entered.is_set, pilot=pilot)
        if mode == "workflow":
            sidebar.set_workflow_mode(True)
        else:
            sidebar.suppress_visibility("alternate-screen", True)
        view.set_session_title_state(generated="New title")
        await wait_for(lambda: not is_widget_shown_on_active_screen(panel), pilot=pilot)
        assert not release.is_set()
        assert workspace_session_titles(panel)["current"] == "Before title"
        release.set()
        await settle_workspace_page_reads(panel)
        if mode == "workflow":
            sidebar.set_workflow_mode(False)
        else:
            sidebar.suppress_visibility("alternate-screen", False)
        await wait_for(lambda: is_widget_shown_on_active_screen(panel), pilot=pilot)
        assert app.screen is main
        await wait_for(lambda: workspace_session_titles(panel)["current"] == "New title", pilot=pilot)
        tree = panel.query_one(Tree)
        assert current_row_label(panel).startswith("  New title")
        assert "New title" in "\n".join(tree.render_line(y).text for y in range(tree.size.height))
    finally:
        release.set()
        await settle_workspace_page_reads(panel)


@pytest.mark.parametrize("closed_first", [False, True], ids=["after-delete", "closed-while-deleting"])
async def test_workspace_page_drops_a_session_deleted_in_f1(workspace_main, tmp_path, closed_first):
    """F1 deletes straight from the store, so the page reads again once the deletion is done.

    Closing F1 while the deletion is still on its way to disk does not lose that read.
    """
    from textual.widgets import DataTable

    from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
    from chrys.app.tui.screens.sessions import SessionsScreen
    from chrys.kernel import Message
    from tests.support.tui_helpers import click_when_settled

    app, pilot, main = workspace_main
    sidebar = main.query_one(SidebarPanel)
    store = main._services.state_store
    workspace_id = main._state.workspace.workspace_id
    for session_id, title in (("current", "Current"), ("other", "Other")):
        await store.save_session(
            session_id,
            {"messages": [Message("user", ["hi"])]},
            primary_cwd=str(tmp_path),
            title=title,
        )
        main._services.workspaces.set_session_workspace(session_id, workspace_id)
    main._view_adapter.set_chat_session_id("current")
    panel = sidebar.query_one(WorkspacePanel)
    sidebar.query_one(TabbedContent).active = "tab-workspace"
    main._refresh_workspace()
    panel.refresh_sessions()
    await wait_for(lambda: "other" in workspace_session_titles(panel), pilot=pilot)

    main._navigation.sessions()
    await wait_for(
        lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted and "other" in app.screen._session_ids,
        pilot=pilot,
    )
    sessions = app.screen
    table = sessions.query_one("#sessions", DataTable)
    row = sessions._session_ids.index("other")
    table.move_cursor(row=row)
    await wait_for(lambda: table.cursor_row == row, pilot=pilot)
    await pilot.press("delete")
    await wait_for(lambda: isinstance(app.screen, ConfirmDialog) and app.screen.is_mounted, pilot=pilot)
    release = threading.Event()
    if closed_first:
        delete = store._delete_session_sync

        def held_delete(session_id: str, *, allow_active: bool = False) -> None:
            # The store's own thread, which closing F1 cannot stop.
            release.wait(10)
            delete(session_id, allow_active=allow_active)

        store._delete_session_sync = held_delete
    await click_when_settled(pilot, app.screen.query_one("#confirm-yes", Button))
    if closed_first:
        await wait_for(lambda: app.screen is sessions, pilot=pilot)
    else:
        await wait_for(lambda: app.screen is sessions and "other" not in sessions._session_ids, pilot=pilot)
    await pilot.press("escape")
    await wait_for(lambda: app.screen is main, pilot=pilot)
    release.set()

    await wait_for(lambda: "other" not in workspace_session_titles(panel), pilot=pilot)
    assert not panel.can_restore_session("other")


@pytest.mark.parametrize("entry", ["session-row", "sidebar-new", "workspace-tag", "slash-command"])
async def test_workspace_sidebar_marks_current_before_background_mru_reorder(
    live_workspace_chat, monkeypatch, tmp_path, entry
):
    from tests.support.workspace_registry import save_chat_session

    pilot, main, engine, registry, current, _ = live_workspace_chat
    other_reference = tmp_path / "other-reference"
    await asyncio.to_thread(other_reference.mkdir)
    other = await asyncio.to_thread(registry.create, "Same folder", current.primary_cwd, [str(other_reference)])
    await save_chat_session(main._services.state_store, "other-chat", Path(current.primary_cwd), profile=PROFILE.name)
    await asyncio.to_thread(registry.set_session_workspace, "other-chat", other.id)
    panel = main.query_one(WorkspacePanel)
    tree = panel.query_one(Tree)
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)

    def order():
        return [node.data for node in tree.root.children if node.data in panel._group_nodes]

    source_key, target_key = (f"__workspace__:{workspace.id}" for workspace in (current, other))
    await wait_for(lambda: order()[:2] == [source_key, target_key], pilot=pilot)
    assert order()[:2] == [source_key, target_key]
    normal_render = panel._render_rows
    renders = []

    def record_render():
        normal_render()
        if panel._workspace is not None and panel._workspace.id == other.id:
            label = next(node.label for node in tree.root.children if node.data == target_key)
            renders.append(label.spans[0].style)

    monkeypatch.setattr(panel, "_render_rows", record_render)
    if entry == "session-row":
        line = next(index for index, node in enumerate(tree.root.children) if node.data == "other-chat")
        await click_when_settled(
            pilot, tree, offset=(tree.content_region.x - tree.region.x + 2, line - tree.scroll_offset.y)
        )
    elif entry == "sidebar-new":
        await _click_workspace_symbol(pilot, panel, other.name, "+")
    elif entry == "workspace-tag":
        await click_when_settled(pilot, "#workspace-tag")
        await wait_for(
            lambda: isinstance(pilot.app.screen, WorkspacesScreen) and pilot.app.screen.is_mounted, pilot=pilot
        )
        options = pilot.app.screen.query_one(OptionList)
        await wait_for(lambda: options.option_count > 0, pilot=pilot)
        options.highlighted = options.get_option_index(other.id)
        await pilot.press("enter")
    else:
        composer = main.query_one(InputBar)
        composer.value = "/workspace switch Same folder"
        await composer.action_submit()
    await wait_for(lambda: bool(renders), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
    assert engine.session.workspace_id == other.id
    if entry == "session-row":
        assert engine.session_id == "other-chat"
    # The switch highlights the target at once; the background order may or may not have landed yet.
    assert renders[0] == panel._highlight_style()
    await wait_for(lambda: order()[:2] == [target_key, source_key], pilot=pilot)
