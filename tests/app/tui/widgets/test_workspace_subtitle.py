# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace border paths, cell budgets and idempotent chrome updates."""

from __future__ import annotations

from unittest.mock import create_autospec

import pytest
from rich.cells import cell_len
from rich.text import Text
from textual.app import App, ComposeResult

from chrys.app.tui.theme import TuiVariableDefaultsMixin
from chrys.app.tui.util.git_branch import format_workspace_subtitle
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.workflow import panel as workflow_panel
from chrys.app.tui.widgets.workflow import text as workflow_text
from chrys.app.tui.widgets.workflow.panel import WorkflowPanel
from tests.support.tui_helpers import ChatPanelApp, click_when_settled, resize_when_settled
from tests.support.waiting import wait_for


def _subtitle_plain(panel: ChatPanel) -> str:
    return Text.from_markup(panel.border_subtitle or "").plain


@pytest.mark.parametrize("branch", ["", "main", "feat/abc"])
def test_subtitle_full_path_and_optional_branch(branch: str) -> None:
    subtitle = format_workspace_subtitle("/tmp/project", branch, width=80)
    assert subtitle.plain == "/tmp/project" + (f" ({branch})" if branch else "")
    assert not subtitle.spans
    assert format_workspace_subtitle("/tmp/project", branch, width=None) == subtitle


@pytest.mark.parametrize(
    ("cwd", "width", "expected"),
    [
        ("/Users/developer/code/project", 25, "/…/code/project (main)"),
        ("/用户/开发/代码/项目", 26, "/用户/…/代码/项目 (main)"),
        ("C:\\Users\\developer\\project", 26, "C:\\Users\\…\\project (main)"),
        ("/Users/developer/very-long-project-name", 12, "…roject-name"),
    ],
)
def test_subtitle_uses_cell_budget(cwd: str, width: int, expected: str) -> None:
    branch = "" if width == 12 else "main"
    subtitle = format_workspace_subtitle(cwd, branch, width=width)
    assert subtitle.plain == expected
    assert subtitle.cell_len <= width


@pytest.mark.parametrize(
    "cwd",
    ["/Users/developer/project/", "/用户/开发/项目/", "C:\\Users\\developer\\project\\", "C:/Users/developer/project/"],
)
def test_non_root_trailing_separator_does_not_lose_the_directory(cwd: str) -> None:
    for width in range(101):
        assert format_workspace_subtitle(cwd, "main", width=width) == format_workspace_subtitle(
            cwd.rstrip("/\\"), "main", width=width
        )


@pytest.mark.parametrize("root", ["/", "//", "C:\\", "C:/", "\\\\server\\share\\", "//server/share/"])
def test_root_separator_is_preserved(root: str) -> None:
    expected = f"{root} (main)"
    assert format_workspace_subtitle(root, "main", width=cell_len(expected)).plain == expected


def test_long_branch_leaves_last_directory() -> None:
    subtitle = format_workspace_subtitle("/Users/developer/project", "feature/" + "x" * 100, width=25)
    assert subtitle.plain == "…/project (feature/" + "x" * 100 + ")"


def test_subtitle_treats_markup_as_literal_and_sanitizes_controls() -> None:
    subtitle = format_workspace_subtitle("/tmp/[project]\x1b", "[main]\x1b", width=100)
    assert subtitle.plain == "/tmp/[project]� ([main]�)"
    assert not subtitle.spans
    assert format_workspace_subtitle("", "main", width=80) == Text()


@pytest.mark.parametrize("cwd", ["/Users/developer/code/project", "/用户/开发/源代码/项目"])
async def test_chat_path_resizes_without_counts_and_bottom_border_opens_directory(cwd, monkeypatch) -> None:
    clicked = []

    class App(ChatPanelApp):
        def on_chat_panel_working_dir_clicked(self, message: ChatPanel.WorkingDirClicked) -> None:
            clicked.append("directory")

    async with App().run_test(size=(100, 30)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_workspace_cwd(cwd)
        panel.set_workspace_branch("main")
        await pilot.pause()
        assert _subtitle_plain(panel) == cwd + " (main)"
        setter = create_autospec(panel.set_border_subtitle, side_effect=panel.set_border_subtitle)
        monkeypatch.setattr(panel, "set_border_subtitle", setter)
        panel.set_workspace_cwd(cwd)
        panel.set_workspace_branch("main")
        panel.on_resize()
        setter.assert_not_called()
        await resize_when_settled(pilot, 35, 30)
        expected = "/Users/…/code/project (main)" if cwd.startswith("/Users") else "/用户/开发/源代码/项目 (main)"
        await wait_for(lambda: _subtitle_plain(panel) == expected, pilot=pilot)
        border = pilot.app.screen._compositor.render_strips()[panel.region.bottom - 1].text
        assert expected in border
        assert "+" not in border and "-" not in border
        for column in [1, panel.region.width // 2, panel.region.width - 5]:
            await click_when_settled(pilot, panel, offset=(column, panel.region.height - 1))
        assert clicked == ["directory"] * 3
        await resize_when_settled(pilot, 100, 30)
        await wait_for(lambda: _subtitle_plain(panel) == cwd + " (main)", pilot=pilot)
        panel.set_workspace_branch("feature")
        assert _subtitle_plain(panel) == cwd + " (feature)"


class _WorkflowPanelApp(TuiVariableDefaultsMixin, App[None]):
    def compose(self) -> ComposeResult:
        panel = WorkflowPanel()
        panel.display = True
        yield panel


@pytest.mark.parametrize(
    ("cwd", "expected"),
    [
        ("/Users/developer/source/project", "/Users/…/source/project"),
        ("/用户/开发/很长的工作目录/源代码/项目", "/用户/开发/…/源代码/项目"),
    ],
)
async def test_workflow_subtitle_shortens_the_middle_and_recomputes_after_resize(
    cwd: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with _WorkflowPanelApp().run_test(size=(36, 24)) as pilot:
        panel = pilot.app.query_one(WorkflowPanel)
        panel.set_header("Workflow", session_id="", cwd=cwd)
        await pilot.pause()
        subtitle = Text.from_markup(panel.border_subtitle or "")
        assert subtitle.plain == expected
        assert subtitle.cell_len <= panel.region.width - 6
        assert not subtitle.spans
        border = pilot.app.screen._compositor.render_strips()[panel.region.bottom - 1].text
        assert subtitle.plain in border

        formatter = create_autospec(format_workspace_subtitle, side_effect=format_workspace_subtitle)
        monkeypatch.setattr(workflow_panel, "format_workspace_subtitle", formatter)
        panel.set_header("Another workflow", session_id="session", cwd=cwd)
        panel.on_resize()
        formatter.assert_not_called()

        await resize_when_settled(pilot, 100, 24)
        await wait_for(
            lambda: Text.from_markup(panel.border_subtitle or "").plain == cwd,
            pilot=pilot,
            description="workflow subtitle restores full path after widening",
        )
        assert formatter.call_count == 1
        await resize_when_settled(pilot, 100, 30)
        assert formatter.call_count == 1

        panel.set_header("Workflow", session_id="", cwd="/tmp/other")
        assert Text.from_markup(panel.border_subtitle or "").plain == "/tmp/other"
        assert formatter.call_count == 2

        panel.set_header("Workflow", session_id="", cwd=cwd)
        await resize_when_settled(pilot, 36, 30)
        await wait_for(
            lambda: "…" in Text.from_markup(panel.border_subtitle or "").plain,
            pilot=pilot,
            description="workflow subtitle shortens again after narrowing",
        )
        assert Text.from_markup(panel.border_subtitle or "").plain == subtitle.plain


@pytest.mark.parametrize("cwd", ["/tmp/[name]\x1b\n\t\r\x7f\x85", "/tmp/bad-\udcff", "/tmp/bad-\ud800"])
async def test_workflow_subtitle_preserves_existing_display_sanitization(cwd: str) -> None:
    async with _WorkflowPanelApp().run_test(size=(100, 24)) as pilot:
        panel = pilot.app.query_one(WorkflowPanel)
        panel.set_header("Workflow", session_id="", cwd=cwd)

        subtitle = Text.from_markup(panel.border_subtitle or "").plain
        assert subtitle == workflow_text.shown(cwd)
        subtitle.encode("utf-8")
        assert panel.workspace_cwd == cwd


async def test_workflow_shortened_subtitle_keeps_working_directory_click() -> None:
    clicked: list[WorkflowPanel.WorkingDirClicked] = []

    class App(_WorkflowPanelApp):
        def on_workflow_panel_working_dir_clicked(self, message: WorkflowPanel.WorkingDirClicked) -> None:
            clicked.append(message)

    async with App().run_test(size=(36, 24)) as pilot:
        panel = pilot.app.query_one(WorkflowPanel)
        panel.set_header("Workflow", session_id="", cwd="/Users/developer/source/project")
        await click_when_settled(pilot, panel, offset=(panel.region.width - 5, panel.region.height - 1))

        assert Text.from_markup(panel.border_subtitle or "").plain == "/Users/…/source/project"
        assert len(clicked) == 1
