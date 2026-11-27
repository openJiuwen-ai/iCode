# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace sidebar chrome retranslates cached semantic state."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from textual.widgets import Tree

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen
from chrys.app.tui.widgets.sidebar.panel import SidebarPanel
from chrys.app.tui.widgets.sidebar.workspaces import SavedWorkspaceSession, WorkspacePanel
from chrys.foundation.config.settings import Settings
from chrys.service.workspaces.registry import WorkspaceRegistry
from tests.support.tui_helpers import WidgetApp, cache_workspace_page
from tests.support.waiting import wait_for


@pytest.mark.parametrize("others", [0, 6], ids=["current-only", "more"])
async def test_workspace_page_retranslates_cached_rows_without_loading_again(tmp_path, monkeypatch, others):
    """relative times and the Unspecified group use the new locale without replacing cached rows."""
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    controller = LocaleController(Settings(locale="en"))
    registry = WorkspaceRegistry(tmp_path / "workspaces.json")
    workspace = registry.create("Workspace [P]", str(tmp_path))
    context = registry.snapshot().workspace(workspace.id)
    async with WidgetApp(lambda: SidebarPanel(locale_controller=controller)).run_test(size=(160, 30)) as pilot:
        sidebar = pilot.app.query_one(SidebarPanel)
        panel = sidebar.query_one(WorkspacePanel)
        sidebar.focus_tab("tab-workspace")
        await wait_for(lambda: is_widget_shown_on_active_screen(panel), pilot=pilot)
        panel.set_session(context, None, "current", "Current [x]", registry=registry)
        saved_at = datetime.now(UTC) - timedelta(minutes=2)
        cache_workspace_page(
            panel,
            registry.snapshot().workspaces,
            {
                workspace.id: [
                    SavedWorkspaceSession(f"saved-{index}", f"Saved [{index}]", saved_at) for index in range(others)
                ],
                "__unspecified__": [SavedWorkspaceSession("loose", "Loose", saved_at)],
            },
            render=True,
        )
        tree = panel.query_one(Tree)
        rows = tuple(node.data for node in tree.root.children)
        box = panel.query_one("#workspace-sessions-box")
        for locale, recent, age, unspecified in (
            ("en", "just now", "2m ago", "Unspecified"),
            ("zh-Hans", "刚刚", "2 分钟前", "未指定"),
            ("en", "just now", "2m ago", "Unspecified"),
        ):
            controller.switch_locale(locale)
            await pilot.pause()
            text = "\n".join(str(node.label) for node in tree.root.children)
            assert all(fragment in text for fragment in ("Workspace [P]", " Current [x]", recent))
            assert box.border_title is None
            assert all(not side[0] for side in box.styles.border)
            assert tree.scrollable_content_region.width == panel.content_region.width - tree.scrollbar_size_vertical - 1
            group = next(str(node.label) for node in tree.root.children if node.data == "__unspecified__")
            assert group.startswith(f" {unspecified} ▾")
            if others:
                assert "Saved [0]" in text and age in text
                assert "Saved [5]" not in text
            assert tuple(node.data for node in tree.root.children) == rows

        panel.set_session(None, None, "current", "Current [x]", registry=registry)
        panel._workspaces, panel._saved = (), {}
        panel._render_rows()
        empty = panel.query_one(".workspace-empty")
        for locale, hint in (
            ("zh-Hans", "用 /workspace new 创建的工作区会显示在这里"),
            ("en", "Workspaces you create with /workspace new will appear here"),
        ):
            controller.switch_locale(locale)
            await pilot.pause()
            assert str(empty.render()) == hint
