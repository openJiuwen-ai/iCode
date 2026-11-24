# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The terminal tab while a workflow runs, and when the app mode switches."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from textual.widgets import Button

from chrys.app.tui.screens.dialogs.ask_user import AskUserDialog
from chrys.app.tui.screens.main import session_title
from chrys.app.tui.util.spinner import SPINNER_FRAMES, STATIC_SPINNER
from chrys.app.tui.widgets.text_area import EnhancedTextArea
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.ask_user import AskUserQuestion
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.app.tui.screens.main._workflow_support import (
    WorkflowEngine,
    open_workflow,
    start_workflow,
    switch_mode,
    workflow_selection,
)
from tests.orchestration.workflows._hosting import make_workdir
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


class _Tab:
    """Records each terminal title the main screen writes."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.titles: list[str] = []
        monkeypatch.setattr(session_title, "set_app_terminal_title", self._write)

    def _write(self, _app: object, title: str) -> bool:
        self.titles.append(title)
        return True

    @property
    def shown(self) -> str:
        return self.titles[-1] if self.titles else ""

    def spinning(self, text: str) -> bool:
        mark, _sep, rest = self.shown.partition(" ")
        return rest == text and mark in (*SPINNER_FRAMES, STATIC_SPINNER)


async def _requests(bus: EventBus) -> list[events.WorkflowRunRequest]:
    requests: list[events.WorkflowRunRequest] = []

    async def requested(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    await bus.subscribe(events.WorkflowRunRequest, requested)
    return requests


async def test_workflow_tab_follows_its_run_through_waits_to_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    tab = _Tab(monkeypatch)
    engine, bus = WorkflowEngine(), EventBus()
    requests = await _requests(bus)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        title = preview.title
        assert title
        await wait_for(lambda: tab.shown == title, pilot=pilot, description="workflow title without a mark")

        await start_workflow(pilot)
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        await wait_for(lambda: tab.spinning(title), pilot=pilot, description="spinner from the submit on")
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=requests[0].request_id, run_id="run", selection=workflow_selection(main)
            )
        )
        await bus.publish(events.WorkflowRunStarted(run_id="run", title=title, manifest=preview.manifest))
        node = preview.manifest["nodes"][0]["id"]
        failed = events.WorkflowNodeStateChanged(
            run_id="run", node_id=node, activation_id=node, attempt=1, state="awaiting_retry"
        )
        await bus.publish(failed)
        await wait_for(lambda: tab.shown == f"! {title}", pilot=pilot, description="node awaits a retry")
        assert main._workflow.run_control.retry_node(failed)
        await wait_for(lambda: tab.spinning(title), pilot=pilot, description="retry requested")
        await bus.publish(
            events.WorkflowNodeStateChanged(run_id="run", node_id=node, activation_id=node, attempt=2, state="running")
        )

        await bus.publish(
            events.WorkflowNodeAskUser(
                run_id="run",
                node_id=node,
                activation_id=node,
                attempt=2,
                request_id="question",
                questions=(AskUserQuestion("Which one?"),),
            )
        )
        await wait_for(
            lambda: isinstance(app.screen, AskUserDialog) and app.screen.is_mounted,
            pilot=pilot,
            description="workflow question dialog mounted",
        )
        await wait_for(lambda: tab.shown == f"? {title}", pilot=pilot, description="question waits for an answer")
        dialog = app.screen
        dialog.query_one("#askuser-input", EnhancedTextArea).load_text("this one")
        await click_when_settled(pilot, dialog.query_one("#askuser-submit", Button))
        await wait_for(lambda: app.screen is main and tab.spinning(title), pilot=pilot, description="answer sent")

        await bus.publish(events.WorkflowRunFinished(run_id="run", outcome="completed"))
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await wait_for(lambda: tab.shown == f"✓ {title}", pilot=pilot, description="completed mark")
        await switch_mode(main, pilot)
        await switch_mode(main, pilot)
        await wait_for(lambda: tab.shown == title, pilot=pilot, description="leaving acknowledged the result")

        await start_workflow(pilot)
        await wait_for(lambda: len(requests) == 2 and tab.spinning(title), pilot=pilot, description="next run")
        await bus.publish(events.WorkflowRunRejected(request_id=requests[1].request_id, error="cancelled"))
        await wait_for(lambda: tab.shown == title, pilot=pilot, description="a refused start leaves no mark")


@pytest.mark.parametrize(
    ("outcome", "reason", "started", "mark"),
    [
        # The run store failed before the run could record its start.
        ("storage_failed", "", False, "✗ "),
        ("cancelled", "deadline_exceeded", True, "✗ "),
        ("cancelled", "", True, ""),
        # The lease went away without a terminal: nothing is left running.
        (None, "", True, ""),
    ],
)
async def test_workflow_tab_settles_on_the_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str | None,
    reason: str,
    started: bool,
    mark: str,
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    tab = _Tab(monkeypatch)
    engine, bus = WorkflowEngine(), EventBus()
    requests = await _requests(bus)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        title = preview.title
        await start_workflow(pilot)
        await wait_for(lambda: len(requests) == 1, pilot=pilot)
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        selection = workflow_selection(main)
        await bus.publish(
            events.WorkflowRunAccepted(request_id=requests[0].request_id, run_id="run", selection=selection)
        )
        if started:
            await bus.publish(events.WorkflowRunStarted(run_id="run", title=title, manifest=preview.manifest))
        await wait_for(lambda: tab.spinning(title), pilot=pilot, description="run in flight")

        if outcome is not None:
            await bus.publish(
                events.WorkflowRunFinished(
                    run_id="run", outcome=outcome, reason=reason, session_id=selection.session_id
                )
            )
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        await wait_for(lambda: tab.shown == f"{mark}{title}", pilot=pilot, description="tab settles")


async def test_switching_app_mode_swaps_the_tab_and_drops_the_chat_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_workdir(tmp_path))
    tab = _Tab(monkeypatch)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=EventBus())
    async with app.run_test(size=(140, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        main._session_title.set_session_title_state(custom="Chat task")
        main._session_title.mark_terminal_title_completed()
        await wait_for(lambda: tab.shown == "✓ Chat task", pilot=pilot, description="chat result mark")

        preview = await open_workflow(main, pilot, "demo-workflow")
        await wait_for(lambda: tab.shown == preview.title, pilot=pilot, description="workflow tab")
        await switch_mode(main, pilot)
        await wait_for(lambda: tab.shown == "Chat task", pilot=pilot, description="chat tab without its mark")
