# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace samples stay independent of the main screen's physical-path order."""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from textual.widgets import TabbedContent

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.workspace_branch import WorkspaceBranchController
from chrys.app.tui.util.workspace_order import WorkspaceOrder, read_workspace_order
from chrys.app.tui.widgets.sidebar import workspaces as workspaces_module
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.workspaces import WorkspacePanel
from chrys.foundation.events.types import SessionReady, SessionRestored
from chrys.kernel import Message
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces import paths as workspace_paths
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import settle_workspace_page_reads, workspace_session_titles
from tests.support.waiting import MAIN_SCREEN_BUILD_TIMEOUT, wait_for
from tests.support.workspace_registry import save_chat_session


async def test_workspace_read_retains_a_title_changed_during_a_round_trip(workspace_main, tmp_path, monkeypatch):
    """A → B → A invalidates the old A sample even though the final current id is A again."""
    _app, pilot, main = workspace_main
    panel = main.query_one(WorkspacePanel)
    store, registry = main._services.state_store, main._services.workspaces
    await store.save_session("current", {"messages": []}, title="Alpha", primary_cwd=str(tmp_path))
    await store.save_session("beta", {"messages": []}, title="Beta", primary_cwd=str(tmp_path))
    registry.set_session_workspace("beta", main._state.workspace.workspace_id)
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)
    await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
    original = store.open_session_listing
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []

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
        main._view_adapter.set_chat_session_id("beta")
        main._refresh_workspace()
        saved = await store.save_session("beta", {"messages": []}, title="Renamed", primary_cwd=str(tmp_path))
        assert saved is not None and saved.updated_at is not None
        panel.update_current_title("beta", "Renamed")
        panel.current_session_saved("beta", saved.updated_at, saved.last_surface)
        assert workspace_session_titles(panel)["beta"] == "Renamed"
        main._view_adapter.set_chat_session_id("current")
        main._refresh_workspace()
    finally:
        release.set()
        await settle_workspace_page_reads(panel)
    assert workspace_session_titles(panel)["beta"] == "Renamed"
    assert reads == ["chat", "chat"]


async def test_workspace_page_updates_while_main_order_waits_on_a_deleted_folder(workspace_main, tmp_path, monkeypatch):
    """Deleting a lost mount unblocks local rows without waiting for the main screen's order read."""
    _app, pilot, main = workspace_main
    panel = main.query_one(WorkspacePanel)
    store, registry = main._services.state_store, main._services.workspaces
    slow = tmp_path / "slow"
    slow.mkdir()
    lost = registry.create("Lost mount", str(slow))
    entered, release = threading.Event(), threading.Event()
    identity = workspace_paths.path_identity
    loop_thread = threading.get_ident()

    def held(path):
        if str(path) == str(slow):
            assert threading.get_ident() != loop_thread
            entered.set()
            if not release.wait(10):
                raise TimeoutError("The test did not release the workspace directory")
        return identity(path)

    monkeypatch.setattr(workspace_paths, "path_identity", held)
    main._refresh_workspace()
    panel.refresh_sessions()
    try:
        await wait_for(entered.is_set, pilot=pilot)
        # Give the sidebar's first sample time to reach ordering on the old implementation.
        await pilot.pause()
        external_registry = WorkspaceRegistry(registry.path)
        external_registry.delete(lost.id)
        external_store = JsonFileStateStore(store.session_dir("probe").parent, workspace_registry=external_registry)
        await external_store.save_session("external", {"messages": []}, title="External", primary_cwd=str(tmp_path))
        external_registry.set_session_workspace("external", main._state.workspace.workspace_id)
        tabs = main.query_one(SidebarPanel).query_one(TabbedContent)
        tabs.active = "tab-debug"
        await pilot.pause()
        tabs.active = "tab-workspace"
        await wait_for(lambda: "external" in workspace_session_titles(panel), pilot=pilot, timeout=1)
        assert f"__workspace__:{lost.id}" not in panel._group_nodes
        assert not release.is_set()
        assert main._workspace_refresh_pending
    finally:
        release.set()
        await settle_workspace_page_reads(panel)
        await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)


async def test_workspace_startup_accepts_the_first_main_order_during_its_only_list_read(tmp_path, monkeypatch):
    """A late first order changes grouping/order, not the startup session sample's read budget."""
    monkeypatch.setattr(WorkspaceBranchController, "start", lambda _self, _cwd: None)
    store = JsonFileStateStore(tmp_path / "sessions")
    registry = store.workspace_registry
    current = registry.create("Current", str(tmp_path))
    other_path = tmp_path / "other"
    other_path.mkdir()
    registry.create("Other", str(other_path))
    await store.save_session("saved", {"messages": []}, title="Saved", primary_cwd=str(tmp_path))
    registry.set_session_workspace("saved", current.id)
    listing_entered, release_listing, release_order = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_listing = store.open_session_listing
    original_order = MainScreen._read_workspace_snapshot
    reads = []

    async def held_listing(*, kind):
        reads.append(kind)
        listing = await original_listing(kind=kind)
        listing_entered.set()
        await release_listing.wait()
        return listing

    async def held_order(self):
        await release_order.wait()
        await original_order(self)

    monkeypatch.setattr(store, "open_session_listing", held_listing)
    monkeypatch.setattr(MainScreen, "_read_workspace_snapshot", held_order)
    app = make_chrys_app(tmp_path / "sessions", state_store=store)
    async with app.run_test(size=(120, 36)) as pilot:
        try:
            await wait_for(
                lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
                pilot=pilot,
                timeout=MAIN_SCREEN_BUILD_TIMEOUT,
            )
            main = app.screen
            if not isinstance(main, MainScreen):
                raise AssertionError("The main screen was replaced during startup")
            assert main._workspace_order is None
            await main._services.bus.publish(
                SessionReady(session_id="current", primary_cwd=str(tmp_path), workspace=current),
                raise_handler_errors=True,
            )
            panel = main.query_one(WorkspacePanel)
            await wait_for(listing_entered.is_set, pilot=pilot)
            release_order.set()
            await wait_for(lambda: main._workspace_order is not None, pilot=pilot)
            release_listing.set()
            await settle_workspace_page_reads(panel)
            assert reads == ["chat"]
            assert "saved" in workspace_session_titles(panel)
            assert main._workspace_order is not None and panel._ordered == main._workspace_order.workspaces
        finally:
            release_order.set()
            release_listing.set()


async def test_workspace_page_uses_a_new_main_order_of_the_same_snapshot_without_reads(
    workspace_main, tmp_path, monkeypatch
):
    """A cwd-only order handoff is synchronous and performs neither a listing nor registry read."""
    _app, pilot, main = workspace_main
    other_path = tmp_path / "other"
    other_path.mkdir()
    registry, store = main._services.workspaces, main._services.state_store
    registry.create("Other", str(other_path))
    main._refresh_workspace()
    await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
    panel = main.query_one(WorkspacePanel)
    await settle_workspace_page_reads(panel)
    assert main._workspace_order is not None
    snapshot = main._workspace_order.snapshot
    order = WorkspaceOrder(
        snapshot, main._workspace_cwd(), main._state.workspace.workspace_id, tuple(reversed(panel._ordered))
    )
    assert order.workspaces != panel._ordered
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    read_registry = create_autospec(registry.snapshot, side_effect=registry.snapshot)
    monkeypatch.setattr(store, "open_session_listing", listing)
    monkeypatch.setattr(registry, "snapshot", read_registry)
    main._apply_workspace_order(order)
    await settle_workspace_page_reads(panel)
    assert main._workspace_order is order
    assert panel._ordered == main._workspace_order.workspaces
    listing.assert_not_called()
    read_registry.assert_not_called()


async def test_workspace_order_handoff_moves_existing_rows_without_a_list_read(workspace_main, tmp_path, monkeypatch):
    """An external reassignment changes groups while retaining the committed session sample."""
    _app, pilot, main = workspace_main
    registry, store = main._services.workspaces, main._services.state_store
    other_path = tmp_path / "other"
    other_path.mkdir()
    other = registry.create("Other", str(other_path))
    await store.save_session("moved", {"messages": []}, title="Moved", primary_cwd=str(tmp_path))
    registry.set_session_workspace("moved", main._state.workspace.workspace_id)
    main._refresh_workspace()
    panel = main.query_one(WorkspacePanel)
    panel.refresh_sessions()
    await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
    await settle_workspace_page_reads(panel)
    external = WorkspaceRegistry(registry.path)
    external.set_session_workspace("moved", other.id)
    order = read_workspace_order(registry, main._workspace_cwd(), main._state.workspace.workspace_id)
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(store, "open_session_listing", listing)
    main._apply_workspace_order(order)
    await settle_workspace_page_reads(panel)
    assert "moved" in [row.session_id for row in panel._saved[other.id]]
    assert "moved" not in [row.session_id for row in panel._saved.get(main._state.workspace.workspace_id, ())]
    assert panel._ordered == order.workspaces
    listing.assert_not_called()


async def test_workspace_regroup_retains_a_current_save_made_while_it_reads_registry(
    workspace_main, tmp_path, monkeypatch
):
    """A saved title and timestamp survive a registry handoff without reopening the session sample."""
    _app, pilot, main = workspace_main
    registry, store = main._services.workspaces, main._services.state_store
    await store.save_session("current", {"messages": []}, title="Before", primary_cwd=str(tmp_path))
    registry.set_session_workspace("current", main._state.workspace.workspace_id)
    panel = main.query_one(WorkspacePanel)
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)
    await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
    other_path = tmp_path / "other"
    other_path.mkdir()
    registry.create("Other", str(other_path))
    order = read_workspace_order(registry, main._workspace_cwd(), main._state.workspace.workspace_id)
    entered, release = threading.Event(), threading.Event()
    original = registry.snapshot

    def held():
        snapshot = original()
        entered.set()
        if not release.wait(10):
            raise TimeoutError("The test did not release the registry")
        return snapshot

    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(store, "open_session_listing", listing)
    monkeypatch.setattr(registry, "snapshot", held)
    main._apply_workspace_order(order)
    try:
        await wait_for(entered.is_set, pilot=pilot)
        saved = await store.save_session("current", {"messages": []}, title="New title", primary_cwd=str(tmp_path))
        assert saved is not None and saved.updated_at is not None
        panel.update_current_title("current", "New title")
        panel.current_session_saved("current", saved.updated_at, saved.last_surface)
    finally:
        release.set()
        await settle_workspace_page_reads(panel)
    current = next(row for row in panel._saved[main._state.workspace.workspace_id] if row.session_id == "current")
    assert current.title == "New title" and current.updated_at == saved.updated_at
    assert workspace_session_titles(panel)["current"] == "New title"
    listing.assert_not_called()


async def test_a_restored_chat_the_page_never_read_keeps_its_saved_time(workspace_main, tmp_path):
    """Opening a chat another window saved shows its saved time and place, not the moment it was opened."""
    _app, pilot, main = workspace_main
    store, registry = main._services.state_store, main._services.workspaces
    workspace_id = main._state.workspace.workspace_id
    panel = main.query_one(WorkspacePanel)
    external_registry = WorkspaceRegistry(registry.path)
    external = JsonFileStateStore(store.session_dir("probe").parent, workspace_registry=external_registry)
    older = await external.save_session("older", {"messages": []}, title="Older", primary_cwd=str(tmp_path))
    external_registry.set_session_workspace("older", workspace_id)
    await store.save_session("newer", {"messages": []}, title="Newer", primary_cwd=str(tmp_path))
    registry.set_session_workspace("newer", workspace_id)
    assert older is not None and older.updated_at is not None
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)
    # As if the page had read the list before the other window saved "older".
    panel._saved = {workspace_id: [row for row in panel._saved.get(workspace_id, []) if row.session_id != "older"]}
    assert panel._saved_entry("older") is None

    await main._services.bus.publish(
        SessionRestored(session_id="older", primary_cwd=str(tmp_path)), raise_handler_errors=True
    )
    await wait_for(lambda: panel._current_id == "older" and panel._current_listed is True, pilot=pilot)
    await settle_workspace_page_reads(panel)
    assert panel._current_saved is not None and panel._current_saved.updated_at == older.updated_at
    assert [row.session_id for row in panel._saved[workspace_id]] == ["newer", "older"]


async def test_switching_chats_without_a_registry_file_reads_nothing(tmp_path, monkeypatch):
    """With no workspaces the registry reads as a new empty snapshot each time; the page treats it as unchanged."""
    from chrys.app.tui.widgets.sidebar import workspaces

    monkeypatch.setattr(WorkspaceBranchController, "start", lambda _self, _cwd: None)
    store = JsonFileStateStore(tmp_path / "sessions")
    registry = store.workspace_registry
    await store.save_session("first", {"messages": []}, title="First", primary_cwd=str(tmp_path))
    await store.save_session("second", {"messages": []}, title="Second", primary_cwd=str(tmp_path))
    assert not registry.path.exists()
    app = make_chrys_app(tmp_path / "sessions", state_store=store)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(
            lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
            pilot=pilot,
            timeout=MAIN_SCREEN_BUILD_TIMEOUT,
        )
        main = app.screen
        assert isinstance(main, MainScreen)
        await main._services.bus.publish(
            SessionReady(session_id="first", primary_cwd=str(tmp_path), workspace=None), raise_handler_errors=True
        )
        panel = main.query_one(WorkspacePanel)
        await wait_for(lambda: "second" in workspace_session_titles(panel), pilot=pilot)
        await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
        await settle_workspace_page_reads(panel)
        listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
        regroups = []
        regroup = workspaces._regroup
        monkeypatch.setattr(store, "open_session_listing", listing)
        monkeypatch.setattr(workspaces, "_regroup", lambda *args: regroups.append(args) or regroup(*args))
        for session_id in ("second", "first", "second"):
            main._view_adapter.set_chat_session_id(session_id)
            main._refresh_workspace()
            await wait_for(lambda: not main._workspace_refresh_pending, pilot=pilot)
            await settle_workspace_page_reads(panel)
        assert panel._current_id == "second"
        assert not registry.path.exists()
        listing.assert_not_called()
        assert regroups == []


@pytest.mark.parametrize("registry_changed", [True, False], ids=["registry-changed", "same-registry"])
async def test_a_failed_list_read_is_retried_by_the_next_order_handoff(
    workspace_main, tmp_path, monkeypatch, registry_changed
):
    """After a failed read the rows are stale; a registry change reads them again instead of hiding the error."""
    _app, _pilot, main = workspace_main
    registry, store = main._services.workspaces, main._services.state_store
    panel = main.query_one(WorkspacePanel)
    original = store.open_session_listing
    calls = []

    async def failing_once(*, kind):
        calls.append(kind)
        if len(calls) == 1:
            raise OSError("sessions folder unavailable")
        return await original(kind=kind)

    monkeypatch.setattr(store, "open_session_listing", failing_once)
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)
    assert panel._registry_error is not None
    await store.save_session("later", {"messages": []}, title="Later", primary_cwd=str(tmp_path))
    if registry_changed:
        registry.set_session_workspace("later", main._state.workspace.workspace_id)
    main._apply_workspace_order(
        read_workspace_order(registry, main._workspace_cwd(), main._state.workspace.workspace_id)
    )
    await settle_workspace_page_reads(panel)
    assert calls == ["chat", "chat"]
    assert panel._registry_error is None
    assert "later" in workspace_session_titles(panel)


async def test_a_registry_readable_again_reads_the_chats_it_could_not_group(workspace_main, tmp_path, monkeypatch):
    """An unreadable registry groups no chats; once it reads again the page lists them instead of regrouping none."""
    _app, _pilot, main = workspace_main
    registry, store = main._services.workspaces, main._services.state_store
    panel = main.query_one(WorkspacePanel)
    await store.save_session("kept", {"messages": []}, title="Kept", primary_cwd=str(tmp_path))
    registry.set_session_workspace("kept", main._state.workspace.workspace_id)
    good = registry.path.read_text(encoding="utf-8")
    registry.path.write_text("{not json", encoding="utf-8")
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)
    assert panel._loaded_snapshot is not None and panel._loaded_snapshot.error is not None
    assert "kept" not in workspace_session_titles(panel)
    listing = create_autospec(store.open_session_listing, side_effect=store.open_session_listing)
    monkeypatch.setattr(store, "open_session_listing", listing)
    registry.path.write_text(good, encoding="utf-8")
    main._apply_workspace_order(
        read_workspace_order(registry, main._workspace_cwd(), main._state.workspace.workspace_id)
    )
    await settle_workspace_page_reads(panel)
    assert listing.call_count == 1
    assert panel._registry_error is None
    assert "kept" in workspace_session_titles(panel)


async def test_an_order_handed_over_during_a_read_regroups_after_it(workspace_main, tmp_path, monkeypatch):
    """A chat moved to B and back while the page reads B ends in A once the read commits."""
    _app, pilot, main = workspace_main
    store, registry = main._services.state_store, main._services.workspaces
    first = main._state.workspace.workspace_id
    other_path = tmp_path / "other"
    other_path.mkdir()
    second = registry.create("Other", str(other_path)).id
    external = WorkspaceRegistry(registry.path)
    await store.save_session("chat", {"messages": []}, title="Chat", primary_cwd=str(tmp_path))
    external.set_session_workspace("chat", first)
    panel = main.query_one(WorkspacePanel)

    def hand_over() -> None:
        main._apply_workspace_order(read_workspace_order(registry, main._workspace_cwd(), first))

    hand_over()
    panel.refresh_sessions()
    await settle_workspace_page_reads(panel)
    assert "chat" in [row.session_id for row in panel._saved.get(first, ())]
    external.set_session_workspace("chat", second)
    grouped, release = threading.Event(), threading.Event()
    original = workspaces_module._entries_by_group

    def held(listing, snapshot, current_id):
        result = original(listing, snapshot, current_id)
        grouped.set()
        if not release.wait(10):
            raise TimeoutError("The test did not release the read")
        return result

    monkeypatch.setattr(workspaces_module, "_entries_by_group", held)
    panel.refresh_sessions()
    try:
        await wait_for(grouped.is_set, pilot=pilot)
        external.set_session_workspace("chat", first)
        external.touch(first)
        hand_over()
    finally:
        release.set()
        await settle_workspace_page_reads(panel)
    assert "chat" in [row.session_id for row in panel._saved.get(first, ())]
    assert "chat" not in [row.session_id for row in panel._saved.get(second, ())]


async def test_a_failing_row_update_does_not_cut_a_restore_short(workspace_main, tmp_path, monkeypatch):
    """The page's row update is a side effect: the restored chat still replays when it raises."""
    _app, pilot, main = workspace_main
    store = main._services.state_store
    await save_chat_session(store, "older", tmp_path, messages=[Message("user", ["question"])])
    panel = main.query_one(WorkspacePanel)

    replay = AsyncMock(wraps=main._view_adapter.replay_history)
    monkeypatch.setattr(main._view_adapter, "replay_history", replay)
    failing = Mock(side_effect=RuntimeError("row update failed"))
    monkeypatch.setattr(panel, "current_session_saved", failing)
    await main._services.bus.publish(
        SessionRestored(session_id="older", primary_cwd=str(tmp_path)), raise_handler_errors=True
    )
    await wait_for(lambda: panel._current_id == "older", pilot=pilot)
    failing.assert_called_once()
    replay.assert_awaited_once()
