# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The workspace page lists every workspace with its saved sessions and refreshes while visible."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from textual.color import Color as ThemeColor
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import TabbedContent, Tree

from chrys.app.tui.util.paths import middle_cut_path
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.workspaces import (
    SavedWorkspaceSession,
    WorkspacePanel,
    _entries_by_group,
    _RowSelected,
)
from chrys.foundation.models.session_surface import SessionSurface
from chrys.service.state.session_listing import SessionListing
from chrys.service.state.store import ChatSessionMeta, JsonFileStateStore, SessionMeta
from chrys.service.workspaces.registry import Workspace, WorkspaceRegistry
from tests.support.sessions_browser import FakeSessionStore, session_listing_entry, workflow_session_meta
from tests.support.tui_helpers import (
    WidgetApp,
    cache_workspace_page,
    current_row_label,
    resize_when_settled,
    settle_workspace_page_reads,
    workspace_row_style,
    workspace_session_rows,
)
from tests.support.waiting import wait_for
from tests.support.workspace_registry import make_workdirs


def _workspace_of_current_row(panel: WorkspacePanel) -> str:
    workspace = ""
    for node in panel.query_one(Tree).root.children:
        if str(node.data).startswith("__workspace__"):
            workspace = str(node.label)
        elif node.data == panel._current_id:
            return workspace
    raise AssertionError("the current session is not listed")


@pytest.fixture
def workspace_registry(tmp_path) -> tuple[WorkspaceRegistry, Workspace]:
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    primary = tmp_path / "primary"
    primary.mkdir()
    return registry, registry.create("Workspace", str(primary))


class _RefreshPanel(WorkspacePanel):
    @property
    def is_on_screen(self) -> bool:
        raise AssertionError("Refresh must not ask Textual to arrange its full widget map")


async def test_workspace_page_lists_only_tui_sessions_like_the_session_browser(workspace_registry, tmp_path):
    """The shared default excludes CLI and ACP chats and keeps the current TUI chat."""
    registry, workspace = workspace_registry
    store = JsonFileStateStore(tmp_path / "sessions", workspace_registry=registry)
    for surface in SessionSurface:
        await store.save_session(
            surface.value,
            {"messages": []},
            title=surface.value,
            primary_cwd=workspace.primary_cwd,
            last_surface=surface,
        )
        registry.set_session_workspace(surface.value, workspace.id)
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test() as pilot:
        panel.set_session(workspace, store, "tui", "Current", registry=registry)
        panel.refresh_sessions()
        await wait_for(lambda: "tui" in panel._row_nodes, pilot=pilot)
        await settle_workspace_page_reads(panel)
        assert _node_data(panel) == [f"__workspace__:{workspace.id}", "tui"]
        assert not panel.can_restore_session("cli")
        assert not panel.can_restore_session("acp")


async def test_a_cli_chat_joins_the_list_when_a_save_here_makes_it_tui(workspace_registry, tmp_path, monkeypatch):
    """the read or save surface decides: a rename or a CLI save leaves it out, a turn here adds it, with no list read."""
    registry, workspace = workspace_registry
    store = JsonFileStateStore(tmp_path / "sessions", workspace_registry=registry)
    for session_id, surface in (("cli", SessionSurface.CLI), ("other", SessionSurface.TUI)):
        await store.save_session(
            session_id, {"messages": []}, title=session_id, primary_cwd=workspace.primary_cwd, last_surface=surface
        )
        registry.set_session_workspace(session_id, workspace.id)
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test() as pilot:
        panel.set_session(workspace, store, "cli", "", registry=registry)
        panel.refresh_sessions()
        await wait_for(lambda: "other" in panel._row_nodes, pilot=pilot)
        await settle_workspace_page_reads(panel)
        reads: list[str] = []
        original = store.open_session_listing

        async def counted(*, kind):
            reads.append(kind)
            return await original(kind=kind)

        monkeypatch.setattr(store, "open_session_listing", counted)

        def listed() -> bool:
            return any(row.session_id == "cli" for row in panel._saved.get(workspace.id, ()))

        # The read found it on the CLI surface: a rename alone leaves it out.
        panel.update_current_title("cli", "Renamed")
        assert not listed()
        kept = await store.save_session("cli", {"messages": []}, title="cli", primary_cwd=workspace.primary_cwd)
        assert kept is not None and kept.updated_at is not None and kept.last_surface is SessionSurface.CLI
        panel.current_session_saved("cli", kept.updated_at, kept.last_surface)
        await pilot.pause()
        assert not listed()
        saved = await store.save_session(
            "cli", {"messages": []}, title="cli", primary_cwd=workspace.primary_cwd, last_surface=SessionSurface.TUI
        )
        assert saved is not None and saved.updated_at is not None
        panel.current_session_saved("cli", saved.updated_at, saved.last_surface)
        assert listed()
        panel.set_session(workspace, store, "other", "", registry=registry)
        await pilot.pause()
        assert panel.can_restore_session("cli")
        # Continued in `icode run` since the page listed it: the save opening it here reports CLI.
        left = await store.save_session(
            "other", {"messages": []}, title="other", primary_cwd=workspace.primary_cwd, last_surface=SessionSurface.CLI
        )
        assert left is not None and left.updated_at is not None
        panel.current_session_saved("other", left.updated_at, left.last_surface)
        panel.set_session(workspace, store, "cli", "", registry=registry)
        await pilot.pause()
        assert not panel.can_restore_session("other")
        assert reads == []


@pytest.mark.parametrize("hidden_by", ["ancestor", "opaque-screen"])
async def test_workspace_refresh_skips_a_hidden_page(workspace_registry, tmp_path, hidden_by):
    registry, workspace = workspace_registry
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("first", {"messages": []}, title="First", primary_cwd=workspace.primary_cwd)
    registry.set_session_workspace("first", workspace.id)
    panel = _RefreshPanel()
    app = WidgetApp(lambda: Vertical(panel, id="parent"))
    async with app.run_test() as pilot:
        panel.set_session(registry.snapshot().workspace(workspace.id), store, "current", "Current", registry=registry)
        panel.refresh_sessions()
        await wait_for(lambda: panel.can_restore_session("first"), pilot=pilot)
        if hidden_by == "ancestor":
            app.query_one("#parent").display = False
        else:
            overlay = Screen()
            app.push_screen(overlay)
            await wait_for(lambda: app.screen is overlay and overlay.is_mounted, pilot=pilot)
        await wait_for(lambda: not is_widget_shown_on_active_screen(panel), pilot=pilot)
        await store.save_session("later", {"messages": []}, title="Later", primary_cwd=workspace.primary_cwd)
        registry.set_session_workspace("later", workspace.id)
        panel.refresh_sessions()
        await settle_workspace_page_reads(panel)
        assert not panel.can_restore_session("later")
        assert "later" not in _node_data(panel)
        if hidden_by == "ancestor":
            app.query_one("#parent").display = True
        else:
            await app.pop_screen()
            panel.screen_resumed()
        await wait_for(lambda: is_widget_shown_on_active_screen(panel), pilot=pilot)
        await wait_for(lambda: panel.can_restore_session("later"), pilot=pilot)
        assert set(_node_data(panel)) == {f"__workspace__:{workspace.id}", "current", "first", "later"}


async def test_relative_times_move_on_while_shown_and_the_clock_parks_while_hidden(
    workspace_registry, tmp_path, monkeypatch
):
    """each tick relabels only the rows whose time reads differently; a hidden page parks its clock."""
    registry, workspace = workspace_registry
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("first", {"messages": []}, title="First", primary_cwd=workspace.primary_cwd)
    registry.set_session_workspace("first", workspace.id)
    start = datetime.now(UTC)

    class _Clock(datetime):
        at = start

        @classmethod
        def now(cls, tz=None):
            return cls.at

    for module in ("chrys.app.tui.util.time_ago", "chrys.app.tui.widgets.sidebar.workspaces"):
        monkeypatch.setattr(f"{module}.datetime", _Clock)
    panel = _RefreshPanel()
    app = WidgetApp(lambda: Vertical(panel, id="parent"))
    async with app.run_test() as pilot:
        panel.set_session(None, store, "", "", registry=registry)
        panel.refresh_sessions()
        await wait_for(lambda: panel.can_restore_session("first"), pilot=pilot)
        await settle_workspace_page_reads(panel)
        tree = panel.query_one(Tree)
        row = next(node for node in tree.root.children if node.data == "first")
        assert "just now" in str(row.label)
        assert panel._clock is not None and panel._clock._active.is_set()

        _Clock.at = start + timedelta(minutes=5)
        panel._tick()
        assert "5m ago" in str(row.label)
        assert any(node is row for node in tree.root.children)
        label = row.label
        _Clock.at = start + timedelta(minutes=5, seconds=30)
        panel._tick()
        assert row.label is label, "a row whose time reads the same was rebuilt"

        app.query_one("#parent").display = False
        await wait_for(lambda: not is_widget_shown_on_active_screen(panel), pilot=pilot)
        _Clock.at = start + timedelta(minutes=10)
        panel._tick()
        assert not panel._clock._active.is_set(), "the hidden page's clock runs"
        assert "5m ago" in str(row.label)

        app.query_one("#parent").display = True
        panel.screen_resumed()
        await wait_for(
            lambda: (
                panel._clock._active.is_set()
                and any("10m ago" in str(node.label) for node in tree.root.children if node.data == "first")
            ),
            pilot=pilot,
        )


async def test_workspace_page_is_always_a_tab_except_in_workflow():
    """The page is there for every chat session; workflow hides it and restores the selected tab."""

    class _SidebarApp(WidgetApp):
        # A real main screen focuses its input bar first. Here the sidebar is the
        # whole app, so auto-focus would land in the Chat pane and select it.
        AUTO_FOCUS = None

    async with _SidebarApp(SidebarPanel).run_test() as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        tabs = sidebar.query_one(TabbedContent)
        await pilot.pause()
        # The workspace page is the default page.
        assert tabs.active == "tab-workspace"
        assert tabs.get_tab("tab-workspace").display is True
        sidebar.set_workflow_mode(True)
        assert tabs.active == "tab-debug"
        assert tabs.get_tab("tab-workspace").display is False
        sidebar.set_workflow_mode(False)
        assert tabs.active == "tab-workspace"
        assert tabs.get_tab("tab-workspace").display is True


@pytest.mark.parametrize(
    ("gate_at", "change", "ending"),
    [(gate, change, "success") for gate in ("snapshot", "sessions") for change in ("workspace", "session", "title")]
    + [(gate, "workspace", ending) for gate in ("snapshot", "sessions") for ending in ("failure", "cancel", "close")],
)
async def test_runtime_row_is_synchronous_while_saved_sessions_are_pending(
    workspace_registry, tmp_path, monkeypatch, gate_at, change, ending
):
    """runtime identity/title wins before IO finishes, including failed or stale reads."""
    import threading

    from textual.worker import WorkerCancelled

    registry, workspace = workspace_registry
    (other_root,) = make_workdirs(tmp_path, "other")
    other = registry.create("Other", str(other_root))
    registry.set_session_workspace("saved-a", workspace.id)
    snapshot = registry.snapshot()
    context = snapshot.workspace(workspace.id)
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("saved-a", {"messages": []}, primary_cwd=workspace.primary_cwd, title="Saved A")
    old_sessions = await store.open_session_listing(kind="chat")
    entered = threading.Event()
    released = threading.Event()
    async_release = asyncio.Event()
    failures = []
    now = datetime.now(UTC)

    def read_snapshot():
        if gate_at == "snapshot":
            entered.set()
            if not released.wait(10):
                raise TimeoutError("The test did not release the registry read")
            if ending == "failure" and not failures:
                failures.append("snapshot")
                raise OSError("Registry is unavailable")
        return snapshot

    async def sessions(*, kind=None):
        if gate_at == "sessions":
            entered.set()
            await async_release.wait()
        if gate_at == "sessions" and ending == "failure" and not failures:
            failures.append("sessions")
            raise OSError("Sessions are unavailable")
        return old_sessions

    monkeypatch.setattr(registry, "snapshot", read_snapshot)
    monkeypatch.setattr(store, "open_session_listing", sessions)
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test() as pilot:
        panel.set_session(context, store, "current-a", "Old title", registry=registry)
        cache_workspace_page(
            panel, snapshot.workspaces, {workspace.id: [SavedWorkspaceSession("saved-a", "Saved A", now)]}
        )
        panel._current_saved = SavedWorkspaceSession("current-a", "Old title", now)
        panel._render_rows()
        tree = panel.query_one(Tree)
        old_node = next(node for node in tree.root.children if node.data == "saved-a")
        # Enter on the row before the update; the tree handles it after.
        selected_before = _RowSelected(old_node)
        old_label = current_row_label(panel)
        panel.refresh_sessions()
        try:
            await wait_for(entered.is_set, pilot=pilot)
            worker = next(worker for worker in panel.workers if worker.node is panel and not worker.is_finished)
            selected_workspace = other if change == "workspace" else workspace
            selected_id = "current-b" if change in {"workspace", "session"} else "current-a"
            panel.set_session(
                snapshot.workspace(selected_workspace.id),
                store,
                selected_id,
                "New title",
                registry=registry,
            )
            # The IO is still held: no stale node or title may survive this synchronous update.
            assert not worker.is_finished
            assert _workspace_of_current_row(panel).lstrip().startswith(selected_workspace.name + " ▾")
            # The other session keeps its saved workspace, whichever workspace this chat moved to.
            expected_rows = [(selected_id, "New title", True), ("saved-a", "Saved A", False)]
            assert sorted(workspace_session_rows(panel)) == sorted(expected_rows)
            assert current_row_label(panel).startswith("  New title")
            assert "Old title" not in "".join(str(node.label) for node in tree.root.children)
            await pilot.pause()
            painted = "\n".join(tree.render_line(y).text for y in range(tree.size.height))
            assert "New title" in painted
            assert "Old title" not in painted
            if change == "title":
                assert (
                    current_row_label(panel).removeprefix("  New title").strip()
                    == old_label.removeprefix("  Old title").strip()
                )
            posted = []
            with monkeypatch.context() as messages:
                messages.setattr(panel, "post_message", lambda event: posted.append(event) or True)
                # The selection opens the row it was made on, even if the update gave its node another row.
                kept = any(node is old_node for node in tree.root.children)
                panel._selected(selected_before)
                assert [event.session_id for event in posted] == (["saved-a"] if kept else [])
                posted.clear()
                valid = next(node for node in tree.root.children if node.data == "saved-a")
                panel._selected(Tree.NodeSelected(valid))
                assert [event.session_id for event in posted] == ["saved-a"]
            if ending == "cancel":
                worker.cancel()
                with pytest.raises(WorkerCancelled):
                    await worker.wait()
            elif ending == "close":
                await panel.remove()
                assert not panel.is_attached
            released.set()
            async_release.set()
            if ending not in {"cancel", "close"}:
                await worker.wait()
                # A superseded read cannot replace the runtime row or show its error.
                assert panel._registry_error is None
                assert sorted(workspace_session_rows(panel)) == sorted(expected_rows)
                assert panel.can_restore_session("saved-a")
        finally:
            released.set()
            async_release.set()


def _lines(panel: WorkspacePanel) -> list[str]:
    return [str(node.label) for node in panel.query_one(Tree).root.children]


def _node_data(panel: WorkspacePanel) -> list[str]:
    return [str(node.data) for node in panel.query_one(Tree).root.children]


async def test_workspace_listing_exception_replaces_loading_and_recovers_on_refresh(workspace_registry, monkeypatch):
    registry, workspace = workspace_registry
    store = FakeSessionStore(1)
    saved = store.sessions[0]
    registry.set_session_workspace(saved.session_id, workspace.id)
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test() as pilot:
        panel.set_session(None, store, "", "", registry=registry)
        empty = panel.query_one(".workspace-empty")
        assert empty.display and str(empty.render()) == "Loading workspaces"
        with monkeypatch.context() as fault:
            fault.setattr(
                store,
                "open_session_listing",
                create_autospec(store.open_session_listing, side_effect=OSError("private listing details")),
            )
            panel.refresh_sessions()
            await wait_for(lambda: empty.display and str(empty.render()) == "Could not load workspaces.", pilot=pilot)
            assert pilot.app.is_running
            await settle_workspace_page_reads(panel)

        panel.refresh_sessions()
        await wait_for(lambda: panel.can_restore_session(saved.session_id), pilot=pilot)
        assert not empty.display and panel._registry_error is None
        assert all("Could not load workspaces" not in line for line in _lines(panel))


async def test_empty_and_unreadable_registries_explain_themselves(tmp_path, workspace_registry):
    """an unreadable list shows its reason and only the running session."""
    now = datetime.now(UTC)

    store = FakeSessionStore(0)
    store.sessions = [_chat_session("empty", now), _chat_session("recorded", now)]

    registry = WorkspaceRegistry(tmp_path / "empty" / "workspaces.json")
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test() as pilot:
        panel.set_session(None, None, "", "", registry=registry)
        empty = panel.query_one(".workspace-empty")
        assert empty.display is True
        assert str(empty.render()) == "Loading workspaces"
        panel.refresh_sessions()
        await settle_workspace_page_reads(panel)
        assert empty.display is True
        assert str(empty.render()) == "Workspaces you create with /workspace new will appear here"
        assert panel.query_one(Tree).display is False

        broken, workspace = workspace_registry
        context = broken.snapshot().workspace(workspace.id)
        broken.path.write_text("broken", encoding="utf-8")
        panel.set_session(None, store, "", "", registry=broken)
        panel.refresh_sessions()
        await settle_workspace_page_reads(panel)
        assert str(empty.render()).startswith("Cannot read the workspace list: ")
        assert _node_data(panel) == []
        # Running in a workspace that can no longer be read: the workspace stays, the reason follows it.
        panel.set_session(context, None, "unsaved", "Live", registry=broken)
        await pilot.pause()
        assert empty.display is False
        assert _lines(panel)[0].lstrip().startswith(workspace.name + " ▾")
        tree = panel.query_one(Tree)
        prefix = "Cannot read the workspace list: "
        for width in (60, 80):
            await resize_when_settled(pilot, width, 24)
            expected = prefix + middle_cut_path(
                str(broken.path), tree.scrollable_content_region.width - 1 - cell_len(prefix)
            )
            await wait_for(lambda expected=expected: _lines(panel)[-1] == expected, pilot=pilot)
            assert "…" in expected
            assert expected.endswith("workspaces.json")
            assert cell_len(expected) <= tree.scrollable_content_region.width - 1


_UNSPECIFIED_GROUP = "__unspecified__"


def _chat_session(session_id: str, updated_at: datetime, *, title: str = "") -> ChatSessionMeta:
    return ChatSessionMeta(
        session_id=session_id,
        created_at=updated_at,
        updated_at=updated_at,
        agent_profile="",
        agent_display_name="",
        message_count=1,
        title=title or session_id,
    )


def test_registry_membership_alone_groups_saved_chats_and_excludes_workflows(workspace_registry, tmp_path):
    """one readable snapshot groups chats; unreadable membership never guesses a group."""
    registry, known = workspace_registry
    empty_root, deleted_root = make_workdirs(tmp_path, "empty", "deleted")
    empty = registry.create("Empty", str(empty_root))
    deleted = registry.create("Deleted", str(deleted_root))
    registry.set_session_workspace("known", known.id)
    registry.set_session_workspace("deleted", deleted.id)
    registry.set_session_workspace("workflow", known.id)
    registry.delete(deleted.id)
    now = datetime.now(UTC)
    sessions: list[SessionMeta] = [
        _chat_session("unspecified", now),
        _chat_session("deleted", now),
        _chat_session("known", now),
        workflow_session_meta("workflow", now, title="workflow"),
    ]
    snapshot = registry.snapshot()
    listing = SessionListing("chat", tuple(map(session_listing_entry, sessions)))
    rows, _, _ = _entries_by_group(listing, snapshot, "")
    assert {key: [row.session_id for row in group] for key, group in rows.items()} == {
        known.id: ["known"],
        _UNSPECIFIED_GROUP: ["unspecified", "deleted"],
    }
    assert empty.id not in rows
    # F1 can open a CLI chat: as the current chat it keeps its saved time, though no group lists it.
    cli = replace(session_listing_entry(_chat_session("cli", now - timedelta(days=10))), surface=SessionSurface.CLI)
    with_cli = SessionListing("chat", (*listing.entries, cli))
    rows, current, listed = _entries_by_group(with_cli, snapshot, "cli")
    assert current is not None and current.updated_at == cli.listed_at and not listed
    assert "cli" not in {row.session_id for group in rows.values() for row in group}
    registry.path.write_text("broken", encoding="utf-8")
    unreadable = registry.snapshot()
    assert _entries_by_group(listing, unreadable, "") == ({}, None, False)


async def test_current_chat_sits_in_the_unspecified_group_until_it_enters_a_workspace(workspace_registry):
    """J0 empty places this chat in the last group with the dot, its name in the theme's yellow.

    Entering a workspace moves the chat and the yellow there; the group then shows only with a member.
    """
    registry, workspace = workspace_registry
    snapshot = registry.snapshot()
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test(size=(60, 24)) as pilot:
        panel.set_session(None, None, "current", "Live", registry=registry)
        cache_workspace_page(panel, snapshot.workspaces, {workspace.id: []})
        panel._render_rows()
        tree = panel.query_one(Tree)
        await wait_for(lambda: tree.display and tree.size.width > 0, pilot=pilot)
        yellow = ThemeColor.parse(pilot.app.theme_variables["warning"]).rich_color
        nodes = list(tree.root.children)
        current = next(index for index, node in enumerate(nodes) if node.data == "current")
        assert str(nodes[current].label).startswith("  Live")
        assert nodes[current - 1].data == _UNSPECIFIED_GROUP
        assert workspace_row_style(panel.query_one(Tree), _UNSPECIFIED_GROUP).color == yellow

        panel.set_session(snapshot.workspace(workspace.id), None, "current", "Live", registry=registry)
        await pilot.pause()
        nodes = list(tree.root.children)
        current = next(index for index, node in enumerate(nodes) if node.data == "current")
        assert nodes[current - 1].data == f"__workspace__:{workspace.id}"
        assert _UNSPECIFIED_GROUP not in _node_data(panel)

        # A saved chat keeps the group listed, without the yellow, which went with this chat to its workspace.
        panel._saved[_UNSPECIFIED_GROUP] = [SavedWorkspaceSession("saved", "Saved", datetime.now(UTC))]
        panel._render_rows()
        assert _UNSPECIFIED_GROUP in _node_data(panel)
        assert workspace_row_style(panel.query_one(Tree), _UNSPECIFIED_GROUP).color is None
        assert workspace_row_style(panel.query_one(Tree), f"__workspace__:{workspace.id}").color == yellow


async def test_workspace_page_stays_borderless_in_shell_mode():
    async with WidgetApp(SidebarPanel).run_test(size=(140, 40)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        box = sidebar.query_one("WorkspacePanel > #workspace-sessions-box")
        tasks = sidebar.query_one("#tasks-todos")
        normal = tasks.styles.border_top
        assert not box.styles.border_top[0]
        sidebar.add_class("-shell-active")
        await wait_for(lambda: tasks.styles.border_top != normal, pilot=pilot)
        assert not box.styles.border_top[0]
        assert box.border_title is None
