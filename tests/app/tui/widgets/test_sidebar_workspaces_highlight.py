# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace groups leave blank rows that neither mouse nor keyboard selects."""

from __future__ import annotations

from textual.widgets import Tree

from chrys.app.tui.widgets.sidebar.workspaces import WorkspacePanel
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.tui_helpers import WidgetApp, wait_for_workspace_tree


def _folder(tmp_path, name: str) -> str:
    folder = tmp_path / name
    folder.mkdir()
    return str(folder)


async def test_workspace_group_spacing_is_blank_and_skips_mouse_and_keyboard(tmp_path):
    from tests.support.tui_helpers import click_when_settled

    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    first = registry.create("First", _folder(tmp_path, "first"))
    second = registry.create("Second", _folder(tmp_path, "second"))
    panel = WorkspacePanel()
    async with WidgetApp(lambda: panel).run_test(size=(60, 30)) as pilot:
        panel.set_session(None, None, "chat", "Chat", registry=registry)
        panel._workspaces = registry.snapshot().workspaces
        panel._collapsed.update((first.id, second.id, "__unspecified__"))
        panel._render_rows()
        tree = panel.query_one(Tree)
        await wait_for_workspace_tree(pilot, tree)

        def visible_text():
            return [tree.render_line(index).text.strip() for index in range(len(tree.root.children))]

        assert [bool(line) for line in visible_text()] == [True, False, True, False, True]
        assert {segment.style for segment in tree.render_line(1)} == {tree.rich_style}
        tree.focus()
        tree.cursor_line = 0
        await pilot.press("down")
        assert tree.cursor_line == 2
        await pilot.press("down")
        assert tree.cursor_line == 4
        await pilot.press("up")
        assert tree.cursor_line == 2
        await pilot.hover(tree, offset=(2, 1))
        assert tree.hover_line == -1
        await click_when_settled(pilot, tree, offset=(2, 1))
        assert tree.cursor_line == 2
        await pilot.press("enter")
        assert [bool(line) for line in visible_text()] == [True, False, True, True, False, True]
