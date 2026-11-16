# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Mode-specific session browsing and reopening archived workflow artifacts in ChrysApp."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from textual.widgets import Button, DataTable, Input, Static

from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_node import WorkflowNodeDialog
from chrys.app.tui.screens.sessions.screen import SessionsScreen
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.workflow.graph import WorkflowGraph
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.kernel import Message
from tests.orchestration.workflows._hosting import make_project
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_history import record_workflow_run

from ._workflow_support import WorkflowEngine, save_workflow_session, select_workflow_view, switch_mode


@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_session_modes_and_archived_workflow_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, locale: str
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    bus = EventBus()
    app = make_chrys_app(
        tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus, settings=Settings(locale=locale)
    )
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        store = main._services.state_store
        assert store is not None
        chat_id, workflow_id, mixed_id = (str(uuid4()) for _ in range(3))
        runs = [
            (workflow_id, "Older review", "completed"),
            (workflow_id, "Cancelled [review]", "cancelled"),
            (mixed_id, "Failed review", "node_failed"),
            (mixed_id, "Interrupted review", ""),
        ]
        run_ids = []
        for session_id, title, outcome in runs:
            run_id = uuid4().hex
            run_ids.append(run_id)
            await record_workflow_run(
                store.session_dir(session_id) / "workflows" / run_id,
                session_id=session_id,
                title=title,
                outcome=outcome,
            )
        for session_id in (chat_id, workflow_id, mixed_id):
            if session_id == chat_id:
                await store.save_session(
                    session_id,
                    {"messages": [Message("user", ["chat-only searchable prompt"])]},
                    title="Chat title",
                    primary_cwd=str(project),
                )
            else:
                await save_workflow_session(store, session_id, project)
        await pilot.press("f1")
        await wait_for(
            lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted and not app.screen._loading,
            pilot=pilot,
        )
        browser = app.screen
        assert isinstance(browser, SessionsScreen)
        assert {row.meta.session_id for row in browser._rows} == {chat_id}
        assert str(browser.query_one("#container").border_title) == ("Chat Sessions" if locale == "en" else "聊天会话")
        assert {column.key for column in browser._columns} == {
            "id",
            "title",
            "directory",
            "last_active",
            "turns",
            "size",
        }
        app.save_screenshot(f"chat-sessions-{locale}.svg", path=str(tmp_path))
        await pilot.press("escape")
        await switch_mode(main, pilot)
        await pilot.press("f1")
        await wait_for(
            lambda: isinstance(app.screen, SessionsScreen) and app.screen.is_mounted and not app.screen._loading,
            pilot=pilot,
        )
        browser = app.screen
        assert isinstance(browser, SessionsScreen)
        assert str(browser.query_one("#container").border_title) == (
            "Workflow Sessions" if locale == "en" else "工作流会话"
        )
        assert len(browser._rows) == 2
        assert {row.meta.session_id for row in browser._rows} == {workflow_id, mixed_id}
        assert {row.workflow.run_id for row in browser._rows if row.workflow} == {run_ids[1], run_ids[3]}
        assert {column.key for column in browser._columns} == {
            "id",
            "workflow",
            "directory",
            "last_active",
            "status",
            "runs",
            "size",
        }
        expected = {"Cancelled", "Interrupted"} if locale == "en" else {"已取消", "已中断"}
        assert {row.cells["status"] for row in browser._rows} == expected
        app.save_screenshot(f"workflow-sessions-{locale}.svg", path=str(tmp_path))
        search = browser.query_one("#search", Input)
        search.value = "chat-only"
        await wait_for(lambda: not browser._rows, pilot=pilot)
        search.value = "Interrupted review"
        await wait_for(lambda: len(browser._rows) == 1, pilot=pilot)
        await click_when_settled(pilot, browser.query_one("#delete", Button))
        await wait_for(
            lambda: (
                isinstance(app.screen, ConfirmDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: not store.session_dir(mixed_id).exists() and not browser._loading, pilot=pilot)
        search.value = ""
        # The filtered list held one row as well, the session that is gone now.
        await wait_for(lambda: {row.meta.session_id for row in browser._rows} == {workflow_id}, pilot=pilot)
        search.value = "Cancelled [review]"
        await wait_for(lambda: len(browser._rows) == 1, pilot=pilot)
        assert browser._rows[0].workflow is not None and browser._rows[0].workflow.run_id == run_ids[1]

        chat_id_before = main.query_one(ChatPanel).session_id
        browser.query_one(DataTable).focus()
        await pilot.press("enter")
        await wait_for(lambda: main._workflow_panel.run_id == run_ids[1], pilot=pilot)
        assert main._workflow.session_id == workflow_id
        assert main.query_one(ChatPanel).session_id == chat_id_before
        assert not main._workflow_panel.query_one("#workflow-start", Button).disabled
        await select_workflow_view(main, pilot, "code")
        await select_workflow_view(main, pilot, "output")
        await wait_for(
            lambda: (
                "Archived workflow output" in str(main._workflow_panel.query_one("#workflow-outputs", Static).content)
            ),
            pilot=pilot,
        )
        await select_workflow_view(main, pilot, "graph")
        graph = main._workflow_panel.query_one(WorkflowGraph)
        graph.focus()
        assert not graph.selected_node
        await pilot.press("j", "enter")
        await wait_for(lambda: isinstance(app.screen, WorkflowNodeDialog) and app.screen.is_mounted, pilot=pilot)
        await pilot.press("escape")
        await switch_mode(main, pilot)
        assert main.query_one(ChatPanel).session_id == chat_id_before
