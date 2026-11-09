# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow drafts, independent history and run selection through the real TUI."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, ContentSwitcher, Static, Tab, TabbedContent, Tabs
from textual.widgets._tabs import Underline

from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.workflow_confirm import WorkflowConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.screens.main import workflow_session_view
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from tests.app.tui.screens.main._workflow_support import workflow_selecting
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import BusyWidget, assert_app_handles_messages, click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until
from tests.support.workflow_history import record_workflow_run
from tests.support.workflow_workers import python_workflow

from ._workflow_support import (
    WorkflowEngine,
    dismiss_workflow_notice,
    open_workflow,
    run_store,
    save_workflow_session,
    switch_mode,
    workflow_selection,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.pilot import Pilot

    from chrys.app.tui.screens.main.screen import MainScreen
    from chrys.app.tui.widgets.workflow.projector import ObservedRun
    from chrys.service.workflows.artifacts import WorkflowRunRecord


class _Cover(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static("cover")


async def test_overlapping_run_tabs_keep_latest_request_and_append_existing_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)):
        main = app._main_screen
        assert main is not None
        panel = main._workflow_panel
        tabs = panel.query_one("#workflow-run-tabs", Tabs)
        panel.run_id = "r2"
        await asyncio.gather(panel.show_runs(["r1"]), panel.show_runs(["r1", "r2"]))
        assert [tab.id for tab in tabs.query(Tab)] == ["run-r1", "run-r2"]
        assert panel.run_ids == ["r1", "r2"] and tabs.active == "run-r2"
        first = tabs.query_one("#run-r1", Tab)
        await panel.show_runs(["r1", "r2", "r3"])
        assert tabs.query_one("#run-r1", Tab) is first
        # A session reset may overtake an in-flight append; only the latest
        # session's requested list should survive, with no duplicate widgets.
        panel.run_id = "new"
        await asyncio.gather(
            panel.show_runs(["r1", "r2", "r3", "r4"]),
            panel.show_runs([]),
            panel.show_runs(["new"]),
        )
        assert panel.run_ids == ["new"] and tabs.active == "run-new"
        assert [tab.id for tab in tabs.query(Tab)] == ["run-new"]


async def test_a_cancelled_run_tab_update_finishes_clearing_before_the_next_one_reads_the_tabs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)):
        main = app._main_screen
        assert main is not None
        panel = main._workflow_panel
        tabs = panel.query_one("#workflow-run-tabs", Tabs)
        panel.run_id = "r1"
        await panel.show_runs(["r1", "r2"])
        old = tabs.query_one("#run-r1", Tab)
        busy = BusyWidget()
        await old.mount(busy)
        busy.hold()
        try:
            await wait_for(lambda: busy.holding, description="the busy widget holds its message loop")
            first = asyncio.create_task(panel.show_runs(["r3"]))
            await wait_for(lambda: busy.exit_requested, description="the update clears the run tabs")
            # A newer restore or run switch cancels the flow task that is updating the tabs.
            first.cancel()
            second = asyncio.create_task(panel.show_runs(["r1", "r2"]))
            assert not await wait_until(second.done, timeout=0.3)
        finally:
            busy.release.set()
        await wait_for(lambda: first.done() and second.done(), description="both updates end")
        assert first.cancelled() and second.exception() is None
        assert [tab.id for tab in tabs.query(Tab)] == ["run-r1", "run-r2"]
        assert not old.is_attached and tabs.active == "run-r1"
        await assert_app_handles_messages(app)


async def test_run_tab_updates_activate_no_tab_of_their_own(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)):
        main = app._main_screen
        assert main is not None
        panel = main._workflow_panel
        tabs = panel.query_one("#workflow-run-tabs", Tabs)
        handled: list[Message] = []
        tabs.message_signal.subscribe(tabs, handled.append, immediate=True)
        try:
            panel.run_id = "r2"
            # From no tabs, a rebuild, and an append.
            await panel.show_runs(["r1", "r2"])
            await panel.show_runs(["r0", "r2"])
            await panel.show_runs(["r0", "r2", "r3"])
            # The bar handles its messages in order, so any activation the updates posted comes first.
            marker = Tabs.TabActivated(tabs, tabs.query_one("#run-r2", Tab))
            assert tabs.post_message(marker)
            await wait_for(lambda: marker in handled, description="the bar handles the marker activation")
        finally:
            tabs.message_signal.unsubscribe(tabs)
        assert [message for message in handled if isinstance(message, Tabs.TabActivated)] == [marker]
        assert tabs.active == "run-r2"


async def test_a_run_tab_update_leaves_other_tab_bars_working_while_it_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 36)):
        main = app._main_screen
        assert main is not None
        panel = main._workflow_panel
        tabs = panel.query_one("#workflow-run-tabs", Tabs)
        views = panel.query_one("#workflow-run", TabbedContent)
        panel.run_id = "r1"
        await panel.show_runs(["r1", "r2"])
        busy = BusyWidget()
        await tabs.query_one("#run-r1", Tab).mount(busy)
        busy.hold()
        try:
            await wait_for(lambda: busy.holding, description="the busy widget holds its message loop")
            update = asyncio.create_task(panel.show_runs(["r3"]))
            await wait_for(lambda: busy.exit_requested, description="the update clears the run tabs")
            # The reader picks another view while the update waits.
            code_tab = views.get_tab("workflow-code-tab")
            code_tab.post_message(Tab.Clicked(code_tab))
            await wait_for(
                lambda: views.query_one(ContentSwitcher).current == "workflow-code-tab",
                description="the view follows the tab the reader picked",
            )
            assert not update.done()
        finally:
            busy.release.set()
        await wait_for(update.done, description="the run tab update ends")
        assert update.exception() is None
        assert [tab.id for tab in tabs.query(Tab)] == ["run-r3"] and tabs.active == ""
        await assert_app_handles_messages(app)


async def _restore_archived_runs(main: MainScreen, pilot: Pilot, cwd: Path) -> tuple[str, str]:
    """Restore a session of two archived runs, showing the later one; returns (earlier, later)."""
    store = main._services.state_store
    assert store is not None
    session_id = str(uuid4())
    earlier, later = uuid4().hex, uuid4().hex
    for run_id in (earlier, later):
        await record_workflow_run(
            store.session_dir(session_id) / "workflows" / run_id,
            session_id=session_id,
            title="Archived review",
            outcome="completed",
        )
    await save_workflow_session(store, session_id, cwd)
    await switch_mode(main, pilot)
    await main._workflow.session_view.restore_session(session_id, later)
    await wait_for(lambda: main._workflow_panel.run_id == later and main.app.screen is main, pilot=pilot)
    return earlier, later


def _underlines(tabs: Tabs, tab: Tab) -> bool:
    underline = tabs.query_one(Underline)
    span = tab.virtual_region.shrink(tab.styles.gutter).column_span
    return tab.has_class("-active") and (underline.highlight_start, underline.highlight_end) == span


async def test_the_run_tab_underline_follows_clicks_that_open_archived_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each click opens a loading dialog over the bar before the underline moves."""
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        earlier, later = await _restore_archived_runs(main, pilot, tmp_path)
        panel = main._workflow_panel
        tabs = panel.query_one("#workflow-run-tabs", Tabs)
        for run_id in (earlier, later, earlier):
            await click_when_settled(pilot, f"#run-{run_id}")
            await wait_for(lambda run_id=run_id: panel.run_id == run_id and app.screen is main, pilot=pilot)
            tab = tabs.query_one(f"#run-{run_id}", Tab)
            await wait_for(lambda tab=tab: _underlines(tabs, tab), pilot=pilot, description="underline under the run")


async def test_a_run_shown_once_its_covered_loading_dialog_closes_keeps_its_tab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        earlier, later = await _restore_archived_runs(main, pilot, tmp_path)
        panel = main._workflow_panel
        tabs = panel.query_one("#workflow-run-tabs", Tabs)
        read_started, release = threading.Event(), threading.Event()
        original_read = workflow_session_view.read_observed_run

        def read(record: WorkflowRunRecord) -> ObservedRun:
            read_started.set()
            release.wait(ENGINE_TURN_TIMEOUT)
            return original_read(record)

        monkeypatch.setattr(workflow_session_view, "read_observed_run", read)
        cover = _Cover()
        try:
            # The screen handling the click awaits the read, so nothing here may settle Pilot until it ends.
            tab = tabs.query_one(f"#run-{earlier}", Tab)
            assert tab.post_message(Tab.Clicked(tab))
            await wait_for(
                lambda: read_started.is_set() and isinstance(app.screen, AgentLoadDialog) and app.screen.is_mounted,
                description="the archived run is read behind a mounted loading dialog",
            )
            await app.push_screen(cover)
        finally:
            release.set()
        await wait_for(lambda: not main._workflow.session_view.tab_flow.active, pilot=pilot)
        # The run is only shown once the covered dialog can close; until then the bar keeps the shown run.
        assert panel.run_id == later and tabs.active == f"run-{later}"
        cover.dismiss()
        await wait_for(lambda: app.screen is main and panel.run_id == earlier, pilot=pilot)
        assert tabs.active == f"run-{earlier}"
        await wait_for(lambda: _underlines(tabs, tab), pilot=pilot, description="underline under the shown run")


async def test_start_adds_runs_retry_does_not_and_history_controls_are_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    requests: list[events.WorkflowRunRequest] = []
    retries: list[events.WorkflowNodeRetryRequest] = []

    async def request(event: events.WorkflowRunRequest) -> None:
        requests.append(event)

    async def retry(event: events.WorkflowNodeRetryRequest) -> None:
        retries.append(event)

    await bus.subscribe(events.WorkflowRunRequest, request)
    await bus.subscribe(events.WorkflowNodeRetryRequest, retry)
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        controller, panel = main._workflow, main._workflow_panel
        chat_id = main.query_one(ChatPanel).session_id
        own_id = str(uuid4())
        assert not panel.run_ids and not controller.session_id
        for number in (1, 2):
            run_id = f"run{number}"
            assert controller.run_control.start("review")
            await wait_for(lambda number=number: len(requests) == number, pilot=pilot)
            assert requests[-1].session_id == (None if number == 1 else own_id)
            await engine.set_execution(ExecutionSnapshot("workflow", run_id, True), main._services.bus)
            await bus.publish(
                events.WorkflowRunAccepted(
                    request_id=requests[-1].request_id, run_id=run_id, selection=workflow_selection(main, own_id)
                )
            )
            await bus.publish(
                events.WorkflowRunStarted(
                    run_id=run_id,
                    session_id=own_id,
                    title=preview.title,
                    manifest=preview.manifest,
                    workflow_id="demo-workflow",
                    canonical_path=preview.source.canonical_path,
                    source_kind=preview.source.source_kind,
                    spec_digest=preview.spec_digest,
                )
            )
            attempt = events.WorkflowNodeStateChanged(
                run_id=run_id,
                session_id=own_id,
                node_id="architecture",
                activation_id="architecture@iter#1",
                attempt=1,
                state="awaiting_retry",
            )
            await bus.publish(attempt)
            assert controller.run_control.retry_node(attempt)
            await wait_for(lambda number=number: len(retries) == number, pilot=pilot)
            assert len(panel.run_ids) == number
            if number == 1:
                await bus.publish(events.WorkflowRunFinished(run_id=run_id, session_id=own_id, outcome="completed"))
                await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
            else:
                await controller.session_view.select_run("run1")
                await wait_for(lambda: not panel.query_one("#workflow-stop", Button).disabled, pilot=pilot)
                assert panel.run_id == "run1" and not controller.run_control.retry_node(attempt)
                controller.run_control.stop()
                assert controller.run_control._cancel_confirmation is not None
                await pilot.press("escape")
                assert not await controller.session_view.new_session()
                controller.browser.open("alternate")
                await dismiss_workflow_notice(main, pilot, "Create a new session")
                assert panel.preview == preview
                await controller.session_view.select_run("run2")
                await bus.publish(events.WorkflowRunFinished(run_id=run_id, session_id=own_id, outcome="cancelled"))
                await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        assert controller.session_id == own_id != chat_id
        assert [str(tab.label) for tab in panel.query_one("#workflow-run-tabs", Tabs).query(Tab)] == [
            "Run 1",
            "Run 2",
        ]
        await switch_mode(main, pilot)
        assert main.query_one(ChatPanel).session_id == chat_id
        await switch_mode(main, pilot)
        assert controller.session_id == own_id and panel.run_id == "run2"
        assert await controller.session_view.new_session()
        assert not controller.session_id and not panel.run_ids and panel.preview == preview
        assert main.query_one(ChatPanel).session_id == chat_id


@pytest.mark.parametrize("accepted", [True, False])
async def test_pending_run_admission_is_independent_of_browsing_an_older_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        controller, panel = main._workflow, main._workflow_panel
        selection = workflow_selection(main, str(uuid4()))
        for run_id, digest in (("run1", "old-digest"), ("run2", preview.spec_digest)):
            assert controller.run_control.start("review")
            await bus.publish(
                events.WorkflowRunAccepted(
                    request_id=controller.run_control._pending_run.request_id, run_id=run_id, selection=selection
                )
            )
            await bus.publish(
                events.WorkflowRunStarted(
                    run_id=run_id,
                    session_id=selection.session_id,
                    manifest=preview.manifest,
                    title=preview.title,
                    workflow_id=preview.source.workflow_id,
                    canonical_path=preview.source.canonical_path,
                    source_kind=preview.source.source_kind,
                    spec_digest=digest,
                )
            )
            await bus.publish(
                events.WorkflowRunFinished(run_id=run_id, session_id=selection.session_id, outcome="completed")
            )
        assert controller.run_control.start("next")
        request_id = controller.run_control._pending_run.request_id
        await controller.session_view.select_run("run1")
        assert panel.preview is None
        if not accepted:
            await bus.publish(
                events.WorkflowRunRejected(request_id=request_id, error="stale", message="Definition changed")
            )
            assert not controller.awaiting_engine
            assert panel.run_ids == ["run1", "run2"] and panel.run_id == "run1"
            await dismiss_workflow_notice(main, pilot, "Definition changed")
            return
        await engine.set_execution(ExecutionSnapshot("workflow", "run3", True), main._services.bus)
        await bus.publish(events.WorkflowRunAccepted(request_id=request_id, run_id="run3", selection=selection))
        await bus.publish(
            events.WorkflowRunStarted(run_id="run3", session_id=selection.session_id, manifest=preview.manifest)
        )
        assert not controller.awaiting_engine
        assert panel.run_ids == ["run1", "run2", "run3"] and panel.run_id == "run1"
        assert controller.browser.loaded is not None and controller.browser.loaded.preview == preview
        await controller.session_view.select_run("run3")
        await wait_for(lambda: not panel.query_one("#workflow-stop", Button).disabled, pilot=pilot)
        controller.run_control.stop()
        assert controller.run_control._cancel_confirmation is not None


# The outcome decides only the refusals before New (any run binds the session); New's picker opens
# for a new session whatever the run ended as, so each outcome runs once and each choice twice.
@pytest.mark.parametrize(
    ("outcome", "chosen"),
    [
        ("running", "demo-workflow"),
        ("completed", "alternate"),
        ("cancelled", "demo-workflow"),
        ("node_failed", "alternate"),
    ],
)
async def test_workflow_selection_requires_new_after_any_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str, chosen: str
) -> None:
    project = make_project(tmp_path)
    write_workflow(project, "alternate", python_workflow("def fn(value):\n    return value\n", "fn"))
    monkeypatch.chdir(project)
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await open_workflow(main, pilot, "demo-workflow")
        controller, panel = main._workflow, main._workflow_panel
        session_id = str(uuid4())
        assert controller.run_control.start("review")
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=controller.run_control._pending_run.request_id,
                run_id="run",
                selection=workflow_selection(main, session_id),
            )
        )
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunStarted(
                run_id="run",
                session_id=session_id,
                workflow_id="demo-workflow",
                title=preview.title,
                canonical_path=preview.source.canonical_path,
                source_kind=preview.source.source_kind,
                spec_digest=preview.spec_digest,
                manifest=preview.manifest,
            )
        )
        if outcome != "running":
            await bus.publish(events.WorkflowRunFinished(run_id="run", session_id=session_id, outcome=outcome))
            await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        controller.browser.catalog.confirm(await controller.browser.catalog.preview("alternate", trust=True))
        controller.browser.enter_selection()
        await dismiss_workflow_notice(main, pilot, "Create a new session")
        assert controller.browser._picker is None and not workflow_selecting(main)
        controller.browser.open("alternate")
        await dismiss_workflow_notice(main, pilot, "Create a new session")
        assert controller.session_id == session_id and panel.run_ids == ["run"]
        assert panel.preview == preview
        assert panel.run_id == "run"
        if outcome == "running":
            assert not await controller.session_view.new_session()
            await bus.publish(events.WorkflowRunFinished(run_id="run", session_id=session_id, outcome="completed"))
            await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        composer = main.query_one(InputBar)
        composer.replace_draft("Keep until a workflow is selected")
        new = panel.query_one("#workflow-new", Button)
        await wait_for(lambda: new.visible and not new.disabled, pilot=pilot)
        await click_when_settled(pilot, new)
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowPickerDialog)
                and main._workflow.browser._picker.selection.is_mounted
                and bool(main._workflow.browser._picker.selection.rows)
            ),
            pilot=pilot,
        )
        assert controller.session_id == session_id and panel.run_ids == ["run"]
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert controller.session_id == session_id and panel.run_ids == ["run"]
        assert composer.snapshot_draft().text == "Keep until a workflow is selected"
        await click_when_settled(pilot, new)
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowPickerDialog)
                and main._workflow.browser._picker.selection.is_mounted
                and bool(main._workflow.browser._picker.selection.rows)
            ),
            pilot=pilot,
        )
        picker = main._workflow.browser._picker.selection.list
        current = next(
            i
            for i, row in enumerate(main._workflow.browser._picker.selection.rows)
            if row.workflow_id == "demo-workflow"
        )
        assert not picker.get_option_at_index(current).disabled
        picker.highlighted = next(
            i for i, row in enumerate(main._workflow.browser._picker.selection.rows) if row.workflow_id == chosen
        )
        picker.focus()
        await pilot.press("enter")
        await wait_for(
            lambda: app.screen is main and not workflow_selecting(main) and not controller.session_id,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert panel.preview is not None and panel.preview.source.workflow_id == chosen
        assert not panel.run_ids and composer.snapshot_draft().text == "Keep until a workflow is selected"
        assert new.visible


async def test_workflow_restore_reads_archives_without_chat_restore_or_source_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    restores: list[events.SessionRestore] = []

    async def chat_restore(event: events.SessionRestore) -> None:
        restores.append(event)

    await bus.subscribe(events.SessionRestore, chat_restore)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        store = main._services.state_store
        assert store is not None
        session_id = str(uuid4())
        old, latest = uuid4().hex, uuid4().hex
        for run_id in (old, latest):
            await record_workflow_run(
                store.session_dir(session_id) / "workflows" / run_id,
                session_id=session_id,
                title="Archived review",
                outcome="completed",
            )
        await save_workflow_session(store, session_id, tmp_path)
        chat_id = main.query_one(ChatPanel).session_id
        await switch_mode(main, pilot)
        read_started, release = asyncio.Event(), asyncio.Event()
        original_load = store.load_session_meta

        async def load_meta(session_id: str, *, prefer_recovery: bool = False, strict: bool = False):
            read_started.set()
            await release.wait()
            return await original_load(session_id, prefer_recovery=prefer_recovery, strict=strict)

        monkeypatch.setattr(store, "load_session_meta", load_meta)
        restore = asyncio.create_task(main._workflow.session_view.restore_session(session_id, latest))
        try:
            await wait_for(
                lambda: read_started.is_set() and isinstance(app.screen, AgentLoadDialog) and app.screen.is_mounted,
                pilot=pilot,
                description="session restore is reading behind a mounted loading dialog",
            )
            loading = app.screen
            assert loading.query_one(ChrysLoadingIndicator).display
            assert not loading.query_one("#agent-load-buttons").display
            await pilot.press("escape")
            assert app.screen is loading and not restore.done()
        finally:
            release.set()
            await restore
        await wait_for(lambda: main._workflow_panel.run_id == latest and app.screen is main, pilot=pilot)
        assert not restores
        assert main.query_one(ChatPanel).session_id == chat_id
        assert main._workflow.session_id == session_id
        assert set(main._workflow_panel.run_ids) == {old, latest}
        await main._workflow.session_view.select_run(old)
        # An archived run is shown once the dialog that covered its read has closed.
        await wait_for(lambda: main._workflow_panel.run_id == old and app.screen is main, pilot=pilot)
        main._workflow.browser.enter_selection()
        await dismiss_workflow_notice(main, pilot, "Create a new session")
        assert main._workflow.browser._picker is None and main._workflow.session_id == session_id
        assert await main._workflow.session_view.new_session()
        await wait_for(lambda: not main._workflow_panel.query_one("#workflow-start", Button).disabled, pilot=pilot)
        assert main._workflow_panel.definition.workflow_id


@pytest.mark.parametrize("changed", [False, True], ids=["same-version", "changed-version"])
async def test_start_from_history_preserves_the_bound_workflow_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    source = python_workflow("def echo(value):\n    return value\n", "echo")
    write_workflow(project, "echo", source)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(145, 45)) as pilot:
        main = app._main_screen
        assert main is not None
        controller, panel = main._workflow, main._workflow_panel
        preview = await open_workflow(main, pilot, "echo")
        session_id, run_id = str(uuid4()), uuid4().hex
        state_store = main._services.state_store
        assert state_store is not None
        archive = run_store(
            state_store.session_dir(session_id) / "workflows" / run_id,
            preview,
            session_id=session_id,
            started_at="2026-09-16",
        )
        await archive.close()
        await save_workflow_session(state_store, session_id, project)
        if changed:
            write_workflow(project, "echo", source + b"\n# Updated workflow version\n")
        await controller.session_view.restore_session(session_id)
        await wait_for(lambda: app.screen is main and panel.run_id == run_id, pilot=pilot)
        assert panel.preview is None
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert not controller.run_control.start("run again")  # An archive needs a fresh preview first.
            if changed:
                await wait_for(
                    lambda: isinstance(app.screen, WorkflowConfirmDialog), pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
                )
                await wait_for(lambda: bool(app.screen.query("#workflow-confirm-yes")), pilot=pilot)
                await click_when_settled(pilot, "#workflow-confirm-yes")
            # The preview loads on a throwaway worker, as when the workflow was first opened.
            await wait_for(lambda: panel.preview is not None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert len(requests) == 1
            assert requests[0].session_id == session_id
            assert (requests[0].pins.spec_digest != preview.spec_digest) is changed
        assert controller.session_id == session_id
        assert panel.run_id == run_id and panel.run_ids == [run_id]
