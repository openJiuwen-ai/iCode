# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow inputs belong to a Run draft, independent of Chat and commands."""

from pathlib import Path

import pytest
from textual.widgets import TabbedContent

from chrys.app.tui.screens.dialogs.workflow_input import WorkflowInputDialog
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.editor import MessageEditor
from chrys.app.tui.widgets.markdown import VirtualizedMarkdown
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from tests.app.tui.screens.main._workflow_support import (
    WorkflowEngine,
    open_workflow,
    select_workflow_view,
    switch_mode,
    workflow_selection,
)
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_cancel_preserves_draft_submit_preserves_text_and_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    bus = EventBus()
    requests = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(150, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        composer = main.query_one(InputBar)
        composer.replace_draft("chat only")
        preview = await open_workflow(main, pilot, "demo-workflow")
        assert not composer.display and composer.value == "chat only"
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        draft = "  preserve [literal] text\n\nand whitespace  "
        app.screen.query_one(MessageEditor).load_text(draft)
        await click_when_settled(pilot, "#workflow-input-cancel")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert not main.query_one("#workflow-start").has_focus
        assert not requests and not main._workflow.awaiting_engine and not main._workflow_panel.run_ids
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        assert app.screen.query_one(MessageEditor).text == draft
        await click_when_settled(pilot, "#workflow-input-start")
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        request = requests[0]
        assert request.input_text == draft
        await bus.publish(
            events.WorkflowRunAccepted(request_id=request.request_id, run_id="run1", selection=workflow_selection(main))
        )
        await bus.publish(events.WorkflowRunStarted(run_id="run1", manifest=preview.manifest, input_text=draft))
        await bus.publish(events.WorkflowRunFinished(run_id="run1", outcome="completed"))
        tabs = main._workflow_panel.query_one("#workflow-run", TabbedContent)
        tabs.active = "workflow-input-tab"
        await wait_for(lambda: main.query_one("#workflow-run-input", VirtualizedMarkdown).source == draft, pilot=pilot)
        # The pane renders the input as markdown; its copy action returns it exactly as submitted.
        copied: list[str] = []
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)
        await click_when_settled(pilot, "#workflow-input-actions > MessageCopyButton")
        await wait_for(lambda: copied == [draft], pilot=pilot)
        assert app.clipboard == draft
        assert not main.query("#workflow-reuse-input")
        await select_workflow_view(main, pilot, "graph")
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)
        assert app.screen.query_one(MessageEditor).text == ""
        await click_when_settled(pilot, "#workflow-input-cancel")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        await switch_mode(main, pilot)
        assert composer.display and composer.value == "chat only"


async def test_workflow_ignores_hidden_chat_input_and_has_no_command_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        target = main._workflow.session_view.selection
        composer = main.query_one(InputBar)
        composer.replace_draft("chat draft")
        assert not main.query("#workflow-commands")
        await pilot.press("f3")
        assert app.screen is main
        # Events queued by the hidden Chat composer must not start a Workflow,
        # execute a command, or reopen the Start dialog after switching modes.
        composer.post_message(InputBar.UserSubmitted("/new"))
        composer.post_message(InputBar.UserSubmitted("queued chat text"))
        composer.post_message(InputBar.RetryRequested(text="queued retry"))
        await pilot.press("f3")
        assert app.screen is main
        assert main._workflow.session_view.selection is target
        assert not main._workflow.awaiting_engine
        assert composer.value == "chat draft"
        await click_when_settled(pilot, "#workflow-start")
        await wait_for(lambda: isinstance(app.screen, WorkflowInputDialog) and app.screen.is_mounted, pilot=pilot)


async def test_workflow_sidebar_keeps_focus_and_paste_does_not_edit_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PIL import Image
    from textual.events import Paste
    from textual.widgets import Tabs

    from chrys.app.tui.widgets.sidebar.panel import SidebarPanel

    monkeypatch.chdir(make_workdir(tmp_path))
    image = tmp_path / "image.png"
    Image.new("RGB", (2, 2)).save(image)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(150, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        composer = main.query_one(InputBar)
        composer.replace_draft("chat only")
        await open_workflow(main, pilot, "demo-workflow")
        sidebar_tabs = main.query_one(SidebarPanel).query_one(Tabs)
        sidebar_tabs.focus()
        await wait_for(lambda: app.focused is sidebar_tabs, pilot=pilot)
        main.on_paste(Paste(str(image)))
        await switch_mode(main, pilot)
        assert composer.value == "chat only"
