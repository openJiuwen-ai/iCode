# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Five rows a level, Show more/Show less on each, and a highlight that follows its own node."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from rich.cells import cell_len
from textual.pilot import Pilot
from textual.widgets import Tree

from chrys.app.tui.util.workspace_order import sorted_workspaces
from chrys.app.tui.widgets.sidebar.workspaces import SavedWorkspaceSession, WorkspacePanel
from chrys.service.state.store import ChatSessionMeta
from chrys.service.workspaces.registry import RegistrySnapshot, Workspace, WorkspaceRegistry
from tests.support.sessions_browser import FakeSessionStore
from tests.support.tui_helpers import (
    WidgetApp,
    cache_workspace_page,
    click_when_settled,
    settle_workspace_page_reads,
    wait_for_workspace_tree,
    workspace_tree_cell,
    workspace_tree_line,
)
from tests.support.waiting import wait_for

_PAGE = 5
_WORKSPACE = "__workspace__"
_PAGE_ROW = "__page__"
_SPACER = "__workspace_spacer__"
_UNSPECIFIED = "__unspecified__"


def _sessions(name: str, now: datetime, count: int = 7) -> list[SavedWorkspaceSession]:
    return [
        SavedWorkspaceSession(f"{name}-{index}", f"{name} s{index}", now - timedelta(minutes=index))
        for index in range(count)
    ]


class _Fixture:
    """Seven workspaces of seven chats each, plus seven chats belonging to no workspace."""

    def __init__(self, tmp_path, *, workspaces: int = 7, sessions: int = 7) -> None:
        self.registry = WorkspaceRegistry(tmp_path / "workspaces.json")
        self.now = datetime.now(UTC)
        self.workspaces: list[Workspace] = []
        for index in range(workspaces):
            folder = tmp_path / f"p{index}"
            folder.mkdir()
            self.workspaces.append(self.registry.create(f"P{index}", str(folder)))
        self.snapshot: RegistrySnapshot = self.registry.snapshot()
        self.ordered = [
            workspace.name for workspace in sorted_workspaces(self.snapshot.workspaces, "", self.snapshot.last_used_at)
        ]
        self.by_name = {workspace.name: workspace for workspace in self.workspaces}
        self.saved = {workspace.id: _sessions(workspace.name, self.now, sessions) for workspace in self.workspaces}
        self.unspecified = _sessions("u", self.now, sessions)

    def group(self, name: str) -> str:
        return f"{_WORKSPACE}:{self.by_name[name].id}"

    def page(self, name: str) -> str:
        return f"{_PAGE_ROW}:{self.by_name[name].id}"

    def rows(self, name: str) -> list[str]:
        return [row.session_id for row in self.saved[self.by_name[name].id]]

    def show(self, panel: WorkspacePanel) -> None:
        """What a finished read would leave behind for this panel, without running one."""
        cache_workspace_page(
            panel, self.snapshot.workspaces, {**self.saved, _UNSPECIFIED: self.unspecified}, render=True
        )

    def expected(self, *, workspaces: int = _PAGE, name: str | None = None, rows: int = _PAGE) -> list[str]:
        """Every node data the page draws: *workspaces* groups, and *rows* in the one named *name*."""
        data: list[str] = []
        for workspace_name in self.ordered[:workspaces]:
            workspace = self.by_name[workspace_name]
            limit = rows if name == workspace_name else _PAGE
            data.append(self.group(workspace_name))
            data += [row.session_id for row in self.saved[workspace.id][:limit]]
            data.append(self.page(workspace_name))
        data.append(f"{_PAGE_ROW}:")
        data.append(_UNSPECIFIED)
        data += [row.session_id for row in self.unspecified[:_PAGE]]
        data.append(f"{_PAGE_ROW}:{_UNSPECIFIED}")
        return data


def _store(fixture: _Fixture) -> FakeSessionStore:
    store = FakeSessionStore(0)
    for workspace in fixture.workspaces:
        for row in fixture.saved[workspace.id]:
            fixture.registry.set_session_workspace(row.session_id, workspace.id)
            store.sessions.append(_meta(row))
    store.sessions.extend(map(_meta, fixture.unspecified))
    return store


def _meta(row: SavedWorkspaceSession) -> ChatSessionMeta:
    return ChatSessionMeta(
        session_id=row.session_id,
        title=row.title,
        created_at=row.updated_at,
        updated_at=row.updated_at,
        agent_profile="Code",
        agent_display_name="Code",
        message_count=1,
    )


def _data(tree: Tree[str]) -> list[str]:
    return [str(node.data) for node in tree.root.children if node.data != _SPACER]


async def _click_cell(pilot: Pilot, tree: Tree[str], data: str, column: int) -> None:
    await click_when_settled(pilot, tree, offset=workspace_tree_cell(tree, column, workspace_tree_line(tree, data)))


async def _click_link(pilot: Pilot, panel: WorkspacePanel, data: str, which: str) -> None:
    """Click the ``more`` or ``less`` link of a page row, at its own cells."""
    tree = panel.query_one(Tree)
    label = str(tree.get_node_at_line(workspace_tree_line(tree, data)).label)
    link = "Show more" if which == "more" else "Show less"
    start = cell_len(label[: label.index(link)])
    await _click_cell(pilot, tree, data, start + cell_len(link) // 2)


def _page(tmp_path, *, sessions: int = 7):
    fixture = _Fixture(tmp_path, sessions=sessions)
    panel = WorkspacePanel()
    return fixture, panel, WidgetApp(lambda: panel)


async def test_the_page_opens_with_five_workspaces_and_five_sessions_each(tmp_path):
    """five workspaces and five chats a level, each level's own page row, Unspecified last."""
    fixture, panel, app = _page(tmp_path)
    async with app.run_test(size=(60, 60)) as pilot:
        panel.set_session(None, None, "", "", registry=fixture.registry)
        fixture.show(panel)
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)

        assert _data(tree) == fixture.expected()
        # The workspace list's page row sits after the last workspace and before Unspecified.
        assert _data(tree)[-8] == f"{_PAGE_ROW}:"
        page_line = workspace_tree_line(tree, f"{_PAGE_ROW}:")
        assert [tree.get_node_at_line(line).data for line in (page_line - 1, page_line + 1)] == [_SPACER, _SPACER]
        # Pagination follows the indent of the rows it expands.
        group_page = tree.get_node_at_line(workspace_tree_line(tree, fixture.page(fixture.ordered[0]))).label
        assert str(group_page) == "  Show more"
        assert str(tree.get_node_at_line(workspace_tree_line(tree, f"{_PAGE_ROW}:")).label) == " Show more"
        # The link itself is underlined text.
        assert [str(span.style) for span in group_page.spans] == ["underline"]


async def test_a_level_with_nothing_more_has_no_page_row(tmp_path):
    """the row appears only with something to show or something to fold back."""
    fixture = _Fixture(tmp_path, workspaces=2, sessions=5)
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test(size=(60, 60)) as pilot:
        panel.set_session(None, None, "", "", registry=fixture.registry)
        fixture.show(panel)
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)

        expected: list[str] = []
        for name in fixture.ordered:
            expected += [fixture.group(name), *fixture.rows(name)]
        expected += [_UNSPECIFIED, *[row.session_id for row in fixture.unspecified]]
        assert _data(tree) == expected
        assert not [data for data in expected if data.startswith(_PAGE_ROW)]


@pytest.mark.parametrize(
    ("level", "count"),
    [("workspaces", 7), ("workspaces", 12), ("workspace", 12), ("unspecified", 12)],
    ids=["all-workspaces-shown", "more-workspaces-hidden", "workspace-sessions", "unspecified-sessions"],
)
async def test_show_more_adds_five_and_show_less_returns_each_level_to_five(tmp_path, level, count):
    """the same 5+5 and collapse rule governs every level, with links only for available actions."""
    fixture = _Fixture(tmp_path, workspaces=count if level == "workspaces" else 7, sessions=12)
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test(size=(60, 90)) as pilot:
        panel.set_session(None, None, "", "", registry=fixture.registry)
        fixture.show(panel)
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)
        if level == "workspaces":
            page = f"{_PAGE_ROW}:"
            rows = [fixture.group(name) for name in fixture.ordered]
        elif level == "workspace":
            name = fixture.ordered[0]
            page, rows = fixture.page(name), fixture.rows(name)
        else:
            page = f"{_PAGE_ROW}:{_UNSPECIFIED}"
            rows = [row.session_id for row in fixture.unspecified]
        hidden = rows[_PAGE + 1]
        assert hidden not in _data(tree)
        if level != "workspaces":
            assert panel.can_restore_session(rows[_PAGE - 1])
            assert not panel.can_restore_session(hidden)
        kept_nodes = [(node, node.label) for node in tree.root.children if node.data != page]
        kept = [(node.data, label) for node, label in kept_nodes]
        built: list[str] = []
        row_label = panel._row_label

        def counted(row):
            built.append(row.session_id)
            return row_label(row)

        panel._row_label = counted

        await _click_link(pilot, panel, page, "more")
        await wait_for(lambda: [data for data in _data(tree) if data in rows] == rows[: _PAGE * 2], pilot=pilot)
        assert str(tree.get_node_at_line(workspace_tree_line(tree, page)).label).strip() == (
            "Show more  Show less" if count > _PAGE * 2 else "Show less"
        )
        # Only the rows the page adds are built; every row already drawn keeps its text.
        assert len(built) == (_PAGE * (min(count, _PAGE * 2) - _PAGE) if level == "workspaces" else _PAGE)
        labels_by_data = {node.data: node.label for node in tree.root.children}
        assert all(labels_by_data[data] == label for data, label in kept if panel._row_nodes.get(data))
        if level == "workspaces":
            for name in fixture.ordered[: _PAGE * 2]:
                assert [row for row in _data(tree) if row.startswith(name + "-")] == fixture.rows(name)[:_PAGE]
        else:
            assert panel.can_restore_session(hidden)
            # Show more only adds rows: every row already drawn keeps its node and its text.
            drawn = tree.root.children
            assert all(any(node is other for other in drawn) and node.label is label for node, label in kept_nodes)
        if level == "unspecified":
            assert _data(tree)[-12:] == [_UNSPECIFIED, *rows[: _PAGE * 2], page]

        await _click_link(pilot, panel, page, "less")
        await wait_for(lambda: _data(tree) == fixture.expected(), pilot=pilot)
        assert str(tree.get_node_at_line(workspace_tree_line(tree, page)).label).strip() == "Show more"
        assert hidden not in _data(tree)
        if level != "workspaces":
            assert not panel.can_restore_session(hidden)


async def test_a_page_row_acts_only_through_its_links_and_enter_shows_more_or_less(tmp_path):
    """each link is hit by its own cells; a click beside them only moves the highlight there.

    That click never eats the next Enter: with the tree focused, Enter shows more while there is more, else less.
    """
    fixture, panel, app = _page(tmp_path, sessions=12)
    async with app.run_test(size=(60, 60)) as pilot:
        panel.set_session(None, None, "", "", registry=fixture.registry)
        fixture.show(panel)
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)
        name = fixture.ordered[0]
        page = fixture.page(name)

        await _click_link(pilot, panel, page, "more")
        await wait_for(
            lambda: [data for data in _data(tree) if data.startswith(name + "-")] == fixture.rows(name)[: _PAGE * 2],
            pilot=pilot,
        )
        page_label = tree.get_node_at_line(workspace_tree_line(tree, page)).label
        more, less = [(span.start, span.end) for span in page_label.spans]
        assert (more, less) == ((2, 11), (13, 22))
        # The two cells between the links, the indent before them, and the row after them.
        for column in (more[1], less[0] - 1, 0, less[1] + 1):
            await _click_cell(pilot, tree, page, column)
        assert [row for row in _data(tree) if row.startswith(name + "-")] == fixture.rows(name)[: _PAGE * 2]
        assert tree.cursor_node is not None and tree.cursor_node.data == page

        # Each link still does its own thing from its own cells; the highlight stays on the page row.
        await _click_link(pilot, panel, page, "less")
        await wait_for(
            lambda: [data for data in _data(tree) if data.startswith(name + "-")] == fixture.rows(name)[:_PAGE],
            pilot=pilot,
        )
        assert tree.cursor_node is not None and tree.cursor_node.data == page

        tree.focus()
        await wait_for(lambda: tree.has_focus, pilot=pilot)
        await pilot.press("enter")
        await wait_for(
            lambda: [data for data in _data(tree) if data.startswith(name + "-")] == fixture.rows(name)[: _PAGE * 2],
            pilot=pilot,
        )
        await pilot.press("enter")
        await wait_for(
            lambda: [data for data in _data(tree) if data.startswith(name + "-")] == fixture.rows(name), pilot=pilot
        )
        assert [row for row in _data(tree) if row.startswith(name + "-")] == fixture.rows(name)
        # Nothing more to show: Enter shows less.
        await pilot.press("enter")
        await wait_for(
            lambda: [data for data in _data(tree) if data.startswith(name + "-")] == fixture.rows(name)[:_PAGE],
            pilot=pilot,
        )
        assert _data(tree) == fixture.expected()


@pytest.mark.parametrize("sessions", [7, 6], ids=["more-hidden", "one-displaced"])
async def test_the_current_chat_past_the_limit_takes_the_last_slot(tmp_path, sessions):
    """An older current chat takes the last of the five slots, so the group keeps its five rows."""
    fixture, panel, app = _page(tmp_path, sessions=sessions)
    store = _store(fixture)
    async with app.run_test(size=(60, 80)) as pilot:
        panel.set_session(None, store, "", "", registry=fixture.registry)
        panel.refresh_sessions()
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)
        name = fixture.ordered[0]
        workspace = fixture.by_name[name]
        current = fixture.rows(name)[-1]
        panel.set_session(fixture.snapshot.workspace(workspace.id), store, current, "", registry=fixture.registry)

        shown = [data for data in _data(tree) if data.startswith(name + "-")]
        assert shown == [*fixture.rows(name)[: _PAGE - 1], current]
        assert panel.can_restore_session(fixture.rows(name)[0])
        # The chat it displaced is behind Show more.
        assert fixture.page(name) in _data(tree)

        # Saved, it moves to the top: the rows trade places in the same nodes, with no new layout.
        nodes = list(tree.root.children)
        panel.current_session_saved(current, datetime.now(UTC), None)
        shown = [data for data in _data(tree) if data.startswith(name + "-")]
        assert shown == [current, *fixture.rows(name)[: _PAGE - 1]]
        assert len(tree.root.children) == len(nodes)
        assert all(node is before for node, before in zip(tree.root.children, nodes, strict=True))


async def test_only_the_open_pages_and_a_lookahead_are_materialized(tmp_path, monkeypatch):
    import weakref

    from chrys.app.tui.widgets.sidebar import workspaces as workspace_module

    fixture = _Fixture(tmp_path, workspaces=7, sessions=12)
    panel = WorkspacePanel()
    app = WidgetApp(lambda: panel)
    store = _store(fixture)
    materialized = []
    original = workspace_module.WorkspaceSessionRow
    ephemeral_metas = []
    open_listing = store.open_session_listing

    def record_row(session_id, title, updated_at, current=False):
        materialized.append(session_id)
        return original(session_id, title, updated_at, current)

    async def record_listing(*, kind):
        listing = await open_listing(kind=kind)
        ephemeral_metas.extend(weakref.ref(entry.meta) for entry in listing.entries)
        return listing

    monkeypatch.setattr(workspace_module, "WorkspaceSessionRow", record_row)
    monkeypatch.setattr(store, "open_session_listing", record_listing)
    async with app.run_test(size=(60, 70)) as pilot:
        panel.set_session(None, store, "", "", registry=fixture.registry)
        panel.refresh_sessions()
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)
        await settle_workspace_page_reads(panel)
        assert len(ephemeral_metas) == len(store.sessions)
        assert all(meta() is None for meta in ephemeral_metas)
        cached = {row.session_id: row for rows in panel._saved.values() for row in rows}
        assert len(cached) == len(store.sessions)
        assert all(cached[meta.session_id].title is meta.title for meta in store.sessions)
        expected = [session_id for name in fixture.ordered[:_PAGE] for session_id in fixture.rows(name)[: _PAGE + 1]]
        expected.extend(entry.session_id for entry in fixture.unspecified[: _PAGE + 1])
        assert materialized == expected
        assert sum(map(len, panel._group_rows.values())) == len(expected)


@pytest.mark.parametrize("level", ["workspaces", "workspace", "unspecified"])
async def test_narrow_page_links_each_have_a_visible_clickable_row(tmp_path, level):
    fixture = _Fixture(tmp_path, workspaces=12, sessions=12)
    panel = WorkspacePanel()
    panel.styles.width = 17
    async with WidgetApp(lambda: panel).run_test(size=(52, 160)) as pilot:
        panel.set_session(None, None, "", "", registry=fixture.registry)
        fixture.show(panel)
        tree = panel.query_one(Tree)
        await wait_for(lambda: tree.display and tree.size.width > 0 and tree.last_line >= 0, pilot=pilot)
        key = (
            ""
            if level == "workspaces"
            else (fixture.by_name[fixture.ordered[0]].id if level == "workspace" else _UNSPECIFIED)
        )
        page = f"{_PAGE_ROW}:{key}"
        for action, expected in (("more", 10), ("less", 5), ("more", 10), ("more", 15)):
            data = f"{page}:less" if action == "less" else page
            if action == "less":
                panel.styles.width = 30
                await wait_for(lambda: f"{page}:less" not in panel._page_nodes, pilot=pilot)
                assert (
                    str(tree.get_node_at_line(workspace_tree_line(tree, page)).label).strip() == "Show more  Show less"
                )
                panel.styles.width = 17
                await wait_for(lambda: f"{page}:less" in panel._page_nodes, pilot=pilot)
                assert panel._limit_for(key) == 10
            if panel._limit_for(key) == 10:
                assert workspace_tree_line(tree, f"{page}:less") == workspace_tree_line(tree, page) + 1
                for target in (page, f"{page}:less"):
                    label = tree.get_node_at_line(workspace_tree_line(tree, target)).label
                    assert label.cell_len <= tree.scrollable_content_region.width - 1
            await _click_link(pilot, panel, data, action)
            await wait_for(lambda expected=expected: panel._limit_for(key) == expected, pilot=pilot)
