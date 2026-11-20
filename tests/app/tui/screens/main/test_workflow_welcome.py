# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mode-specific welcome content shares layout and follows live workspace changes."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from rich.cells import cell_len

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.util.logo import CHAT_LOGO, WORKFLOW_LOGO
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from tests.app.tui.screens.main._workflow_support import WorkflowEngine, open_workflow, switch_mode
from tests.orchestration.workflows._hosting import make_workdir, write_workflow
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import rich_plain
from tests.support.waiting import wait_for
from tests.support.workflow_workers import python_workflow

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("width", [140, 90])
async def test_welcome_follows_mode_workspace_and_locale(
    width: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), settings=Settings(locale="en"))
    async with app.run_test(size=(width, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        bus = main._services.bus
        await bus.publish(
            events.SessionReady(agent_profile="Code", display_name="Code Agent", primary_cwd=str(project)),
            raise_handler_errors=True,
        )
        chat = main.query_one(ChatPanel)
        chat_welcome = chat.query_one(WelcomeWidget)
        chat_text = rich_plain(chat_welcome.render())
        chat_rows = [line.strip() for line in chat_text.splitlines() if line.strip()]
        assert chat_rows[:6] == [line.strip() for line in CHAT_LOGO.splitlines() if line.strip()]
        assert "Code Agent" in chat_text
        app.save_screenshot(f"chat-welcome-{width}.svg", path=str(tmp_path))

        await switch_mode(main, pilot)
        panel = main._workflow_panel
        welcome = panel.query_one(WelcomeWidget)
        await wait_for(lambda: welcome.size.width > 0, pilot=pilot)
        workflow_text = rich_plain(welcome.render())
        workflow_rows = [line.strip() for line in workflow_text.splitlines() if line.strip()]
        if width == 140:
            assert workflow_rows[:8] == [line.strip() for line in WORKFLOW_LOGO.splitlines() if line.strip()]
        else:
            assert workflow_rows[0] == "Workflow"
        assert welcome.render().title == ""
        assert "Code Agent" not in workflow_text
        assert not chat.display

        # A hidden chat welcome still follows profile changes, without leaking the
        # agent's name into the workflow's welcome screen.
        await bus.publish(
            events.ProfileSwitched(from_profile="Code", to_profile="QA", to_display_name="QA [Agent]"),
            raise_handler_errors=True,
        )
        # The workflow side shows the directory as the workspace resolves it, drive included.
        workspace = os.path.abspath("/workspace/[项目]")
        await bus.publish(events.WorkspaceUpdated(primary_cwd=workspace), raise_handler_errors=True)
        app.locale_controller.switch_locale("zh-Hans")
        await wait_for(lambda: workspace in rich_plain(welcome.render()), pilot=pilot)
        rendered = rich_plain(welcome.render())
        cwd = next(line for line in rendered.splitlines() if workspace in line)
        left = len(cwd) - len(cwd.lstrip())
        assert abs(2 * left + cell_len(cwd.strip()) - welcome.size.width) <= 1
        assert welcome.render().title == ""
        assert "QA [Agent]" not in rendered
        app.save_screenshot(f"workflow-welcome-{width}.svg", path=str(tmp_path))

        await switch_mode(main, pilot)
        assert chat.query_one(WelcomeWidget) is chat_welcome
        await wait_for(
            lambda: chat_welcome.size.width > 0 and chat_welcome.size.height > 0,
            pilot=pilot,
            description="restored chat welcome is laid out",
        )
        restored = rich_plain(chat_welcome.render())
        assert "QA [Agent]" in restored
        assert workspace in restored

        # Selecting a workflow replaces the welcome with the existing graph view.
        await bus.publish(events.WorkspaceUpdated(primary_cwd=str(project)), raise_handler_errors=True)
        write_workflow(project, "welcome", python_workflow("def fn(value):\n    return value\n", "fn"))
        await open_workflow(main, pilot, "welcome")
        assert not welcome.display
        assert panel.query_one("#workflow-run").display
        await switch_mode(main, pilot)
        chat_welcome = chat.query_one(WelcomeWidget)
        await wait_for(
            lambda: chat_welcome.size.width > 0 and chat_welcome.size.height > 0,
            pilot=pilot,
            description="chat welcome is laid out after leaving the workflow",
        )
        restored = rich_plain(chat_welcome.render())
        assert "QA [Agent]" in restored
        assert chat.query_one(WelcomeWidget).render().cwd == str(project)


async def test_hidden_chat_rebuild_retains_welcome_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_workdir(tmp_path)
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 36)) as pilot:
        main = app._main_screen
        assert main is not None
        await main._services.bus.publish(
            events.SessionReady(display_name="Code [Agent]", primary_cwd=str(project)), raise_handler_errors=True
        )
        chat = main.query_one(ChatPanel)
        previous = chat.query_one(WelcomeWidget)
        await open_workflow(main, pilot, "demo-workflow")
        # Empty-session replay can rebuild the hidden transcript while browsing workflows.
        await chat.clear()
        assert chat.query_one(WelcomeWidget) is not previous
        await switch_mode(main, pilot)
        welcome = chat.query_one(WelcomeWidget)
        await wait_for(
            lambda: welcome.size.width > 0 and welcome.size.height > 0,
            pilot=pilot,
            description="rebuilt chat welcome is laid out",
        )
        assert "Code [Agent]" in rich_plain(welcome.render())
        assert welcome.render().cwd == str(project)
