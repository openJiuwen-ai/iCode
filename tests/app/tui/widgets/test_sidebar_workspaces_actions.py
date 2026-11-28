# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace groups fold in place; their mouse actions preserve the keyboard cursor."""

from __future__ import annotations

import asyncio

import pytest
from rich.cells import cell_len
from textual import on
from textual.widgets import Tree

from chrys.app.tui.widgets.sidebar.workspaces import WorkspacePanel
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.registry import Workspace, WorkspaceRegistry
from tests.support.tui_helpers import (
    WidgetApp,
    click_when_settled,
    workspace_tree_cell,
    workspace_tree_line,
)
from tests.support.waiting import wait_for


class _WorkspaceApp(WidgetApp):
    def __init__(self, panel: WorkspacePanel) -> None:
        super().__init__(lambda: panel)
        self.new_sessions: list[str | None] = []
        self.edits: list[Workspace] = []

    @on(WorkspacePanel.NewSessionRequested)
    def new_session(self, event: WorkspacePanel.NewSessionRequested) -> None:
        self.new_sessions.append(event.workspace_id)

    @on(WorkspacePanel.EditRequested)
    def edit_workspace(self, event: WorkspacePanel.EditRequested) -> None:
        self.edits.append(event.workspace)


async def _page(tmp_path, *, name: str = "Workspace"):
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    folder = tmp_path / "primary"
    folder.mkdir()
    workspace = registry.create(name, str(folder))
    store = JsonFileStateStore(tmp_path / "sessions")
    for session_id, workspace_id in (("saved", workspace.id), ("loose", None)):
        await store.save_session(
            session_id,
            {"messages": []},
            title=session_id,
            primary_cwd=workspace.primary_cwd,
        )
        if workspace_id is not None:
            registry.set_session_workspace(session_id, workspace_id)
    return registry, workspace, store


async def _read(
    pilot, panel: WorkspacePanel, registry: WorkspaceRegistry, store: JsonFileStateStore, *, ready: str = "saved"
) -> Tree[str]:
    panel.set_session(None, store, "", "", registry=registry)
    panel.refresh_sessions()
    tree = panel.query_one(Tree)
    await wait_for(
        lambda: (
            panel.can_restore_session(ready)
            and tree.size.width > 0
            and all(
                node.label.cell_len == tree.scrollable_content_region.width
                for node in tree.root.children
                if "+" in str(node.label)
            )
        ),
        pilot=pilot,
        description="the workspace tree has fitted its actions to the visible width",
    )
    return tree


def _node(tree: Tree[str], data: str):
    return next(node for node in tree.root.children if node.data == data)


def _group(tree: Tree[str], name: str):
    return next(node for node in tree.root.children if str(node.label).lstrip().startswith(name + " ▾"))


def _offset(tree: Tree[str], node, column: int) -> tuple[int, int]:
    return workspace_tree_cell(tree, column, workspace_tree_line(tree, node))


async def _click(pilot, tree: Tree[str], node, column: int) -> None:
    await click_when_settled(pilot, tree, offset=_offset(tree, node, column))


@pytest.mark.parametrize("group", ["workspace", "unspecified"])
async def test_mouse_and_enter_fold_groups_without_moving_an_unrelated_cursor(tmp_path, group):
    """both groups begin expanded, fold on a body click, and toggle on Enter in keyboard mode."""
    registry, workspace, store = await _page(tmp_path)
    panel = WorkspacePanel()
    app = _WorkspaceApp(panel)
    async with app.run_test(size=(60, 18)) as pilot:
        tree = await _read(pilot, panel, registry, store)
        name, hidden, held = (
            (workspace.name, "saved", "loose") if group == "workspace" else ("Unspecified", "loose", "saved")
        )
        group_node = _group(tree, name)
        group_id = group_node.data
        assert panel.can_restore_session(hidden)
        tree.cursor_line = workspace_tree_line(tree, _node(tree, held))

        await _click(pilot, tree, group_node, 0)
        await wait_for(lambda: not panel.can_restore_session(hidden), pilot=pilot)
        assert tree.cursor_node.data == held
        assert str(_node(tree, group_id).label).lstrip().startswith(name + " ▸")
        assert not tree.has_focus

        tree.focus()
        tree.cursor_line = workspace_tree_line(tree, _node(tree, group_id))
        await pilot.press("enter")
        await wait_for(lambda: panel.can_restore_session(hidden), pilot=pilot)
        assert tree.cursor_node.data == group_id
        assert str(_node(tree, group_id).label).lstrip().startswith(name + " ▾")
        assert app.new_sessions == []
        assert app.edits == []


async def test_saved_group_definition_drives_label_and_editor_after_a_deferred_edit(tmp_path):
    registry, current, store = await _page(tmp_path)
    panel = WorkspacePanel()
    app = _WorkspaceApp(panel)
    async with app.run_test(size=(60, 18)) as pilot:
        panel.set_session(current, store, "saved", "Saved", registry=registry)
        updated = await asyncio.to_thread(registry.update, current.id, name="Saved name", primary=current.primary_cwd)
        panel.refresh_sessions()
        await wait_for(
            lambda: any("Saved name" in str(node.label) for node in panel.query_one(Tree).root.children), pilot=pilot
        )
        tree = panel.query_one(Tree)
        node = _group(tree, "Saved name")
        await wait_for(lambda: node.label.cell_len == tree.scrollable_content_region.width, pilot=pilot)
        label = str(node.label)
        await _click(pilot, tree, node, cell_len(label[: label.index("✎")]))
        await wait_for(lambda: bool(app.edits), pilot=pilot)
        assert app.edits == [updated]
        assert panel._workspace == current
        assert panel._group_nodes[node.data].id == current.id


@pytest.mark.parametrize("name", ["Workspace", "工作区"])
@pytest.mark.parametrize("width", [9, 11, 14])
async def test_a_narrow_group_row_acts_only_where_its_symbols_are_drawn(tmp_path, width, name):
    """a symbol the row is too narrow to show takes no click, and every drawn one still works."""
    registry, workspace, store = await _page(tmp_path, name=name)
    panel = WorkspacePanel()
    app = _WorkspaceApp(panel)
    async with app.run_test(size=(width, 18)) as pilot:
        panel.set_session(None, store, "", "", registry=registry)
        panel.refresh_sessions()
        tree = panel.query_one(Tree)
        await wait_for(
            lambda: (
                panel.can_restore_session("saved")
                and tree.scrollable_content_region.width > 0
                and panel._rows_width == tree.scrollable_content_region.width
            ),
            pilot=pilot,
        )

        def group():
            return next(node for node in tree.root.children if panel._group_nodes.get(node.data) == workspace)

        line = workspace_tree_line(tree, group())
        drawn = tree.render_line(line - tree.scroll_offset.y).text
        # The scrollbar's gutter column too: nothing is drawn or clicked there.
        for column in range(tree.size.width):
            await _click(pilot, tree, group(), column)
            await pilot.pause()
            # Each symbol takes the blank on either side of it.
            near = drawn[max(0, column - 1) : column + 2]
            assert (app.new_sessions, app.edits) == (
                [workspace.id] if "+" in near else [],
                [workspace] if "✎" in near else [],
            ), (column, drawn)
            app.new_sessions.clear()
            app.edits.clear()
            if not panel.can_restore_session("saved"):
                # A click beside the symbols folds the group; open it again for the next column.
                await _click(pilot, tree, group(), 0)
                await wait_for(lambda: panel.can_restore_session("saved"), pilot=pilot)


@pytest.mark.parametrize("width", [7, 9, 14, 22, 41])
async def test_no_row_draws_or_takes_clicks_past_the_rows_width(tmp_path, width):
    """every row fits the width it is drawn in, links included, and a session row keeps some of its title."""
    registry, workspace, store = await _page(tmp_path, name="一个名字很长的工作区")
    for index in range(6):
        session_id = f"long-{index}"
        await store.save_session(
            session_id, {"messages": []}, title="很长的会话标题" * 4, primary_cwd=workspace.primary_cwd
        )
        registry.set_session_workspace(session_id, workspace.id)
    panel = WorkspacePanel()
    app = _WorkspaceApp(panel)
    async with app.run_test(size=(width, 30)) as pilot:
        panel.set_session(None, store, "", "", registry=registry)
        panel.refresh_sessions()
        tree = panel.query_one(Tree)
        await wait_for(
            lambda: (
                panel.can_restore_session("long-5")
                and tree.scrollable_content_region.width > 0
                and panel._rows_width == tree.scrollable_content_region.width
            ),
            pilot=pilot,
        )
        room = panel._rows_width
        assert panel._page_nodes, "the group has no page row"
        for node in tree.root.children:
            assert node.label.cell_len <= room, (str(node.label), room)
            for _, (start, end) in panel._link_spans.get(str(node.data), ()):
                assert end <= room, (str(node.label), start, end, room)
            if str(node.data).startswith("long-"):
                assert str(node.label).strip(), "a session row shows no title"
            if panel._group_nodes.get(node.data) == workspace and panel._link_spans.get(node.data):
                # A symbol is drawn only beside enough of the name to tell whose it is.
                assert "一个名字" in str(node.label), str(node.label)
        for line, node in enumerate(tree.root.children):
            if node.data in panel._page_nodes and not panel._link_spans.get(node.data):
                # A page row too narrow for any link takes neither the cursor nor Enter.
                assert panel.inert(node.data)
                tree.cursor_line = 0
                tree.cursor_line = line
                assert tree.cursor_line != line
