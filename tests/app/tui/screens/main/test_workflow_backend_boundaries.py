# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow UI actions retain their own session, workspace and mutation scope."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec
from uuid import uuid4

import pytest
from textual.widgets import OptionList, Tab

from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen
from chrys.app.tui.screens.diff.screen import DiffScreen
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.workflow_session import WorkflowDraft
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.service.approval.policy import ApprovalMode
from chrys.service.mutations.store import SnapshotStore
from chrys.service.mutations.tracker import MutationTracker
from chrys.service.mutations.types import MutationOp, MutationSource
from tests.app.tui.screens.main._workflow_support import workflow_notice_text
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for
from tests.support.workflow_history import record_workflow_run
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, save_workflow_session, switch_mode, workflow_selection


async def test_preview_and_start_share_workspace_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    engine, bus = WorkflowEngine(), EventBus()
    engine.workspace = Workspace(str(project), [WorkingDir(str(tmp_path / "shared"))])
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        target = main._workflow.browser.draft
        assert main._workflow.session_view.selection is None
        engine.workspace.primary_cwd = str(tmp_path / "other-chat")
        engine.workspace.working_dirs.clear()
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert main._workflow.run_control.start("review")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert isinstance(requests[0].target, WorkflowDraft)
            assert requests[0].target.workspace == target.workspace
            assert target.workspace.primary_cwd == str(project)
            assert target.workspace.working_dirs[0].path == str(tmp_path / "shared")


async def test_workflow_shares_the_launch_approval_badge_and_its_errors_stay_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        main.chat_session_id = "chat"
        await bus.publish(events.ApprovalModeUpdated(mode="auto", session_id="chat"))
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, "workflow")
        # Only the engine launch policy updates the badge, in either app mode.
        await bus.publish(events.ApprovalModeUpdated(mode="manual", session_id="workflow"))
        assert main.header_approval_mode is ApprovalMode.AUTO
        async with capture_event_sequence(bus, events.SetApprovalMode) as changes:
            await main._config_actions.start_approval_mode_change("bypass").wait()
            assert len(changes) == 1
            assert (changes[0].mode, changes[0].session_id, changes[0].persist) == ("bypass", None, True)
            # The echo updates the shared badge without sending another mode request.
            await bus.publish(events.ApprovalModeUpdated(mode="bypass", session_id="chat"))
            assert len(changes) == 1
        assert main.header_approval_mode is ApprovalMode.BYPASS
        await bus.publish(events.ApprovalModeUpdated(mode="bypass", session_id="workflow"))
        await bus.publish(events.ApprovalModeUpdated(mode="manual", session_id="previous-workflow"))
        assert main.header_approval_mode is ApprovalMode.BYPASS
        chat = main.query_one(ChatPanel)
        before = len(chat.query("ErrorMessage"))
        await bus.publish(events.Error(session_id="workflow", code="workflow_storage_failed", message="disk full"))
        assert len(chat.query("ErrorMessage")) == before
        main._workflow.feedback.clear()
        # Switching app modes shows the same badge: there is one chosen mode per launch.
        await switch_mode(main, pilot)
        assert main.header_approval_mode is ApprovalMode.BYPASS
        await switch_mode(main, pilot)
        assert main.header_approval_mode is ApprovalMode.BYPASS


# The chosen mode is only a value carried through either path. Manual is the saved default, so only
# Bypass can tell a reset to it apart, and only the welcome path opens the workflow after the choice:
# that pairing is kept; with the preview open beforehand, the Auto check already catches a reset.
@pytest.mark.parametrize(
    ("selected", "mode"),
    [(False, ApprovalMode.BYPASS), (True, ApprovalMode.MANUAL)],
    ids=["welcome-bypass", "preview-manual"],
)
async def test_the_badge_keeps_the_launch_mode_while_workflow_drafts_carry_only_the_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: bool, mode: ApprovalMode
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus = EventBus()
    app = make_chrys_app(
        tmp_path / "sessions",
        engine=WorkflowEngine(),
        event_bus=bus,
        settings=Settings(default_approval_mode="manual"),
    )
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        main.chat_session_id = "chat"
        await bus.publish(events.ApprovalModeUpdated(mode="auto", session_id="chat"))
        await switch_mode(main, pilot)
        if selected:
            await open_workflow(main, pilot, "demo-workflow")
        badge = main.query_one("#approval-badge")
        assert badge.display and badge.region.width > 0
        # The mode chosen in chat, not the saved default, is the mode in workflow mode too.
        assert main.header_approval_mode is ApprovalMode.AUTO
        async with capture_event_sequence(bus, events.SetApprovalMode) as changes:
            await click_when_settled(pilot, badge)
            # The menu fills its options in on_mount, after the empty list is already queryable.
            await wait_for(
                lambda: isinstance(app.screen, ApprovalModeScreen) and app.screen.is_mounted,
                pilot=pilot,
                description="approval mode menu is mounted with its options",
            )
            options = app.screen.query_one(OptionList)
            options.highlighted = options.get_option_index(mode.value)
            await pilot.press("enter")
            await wait_for(lambda: app.screen is main and len(changes) == 1, pilot=pilot)
            # The choice goes to the chat session, which carries it for the launch, saved default included.
            assert (changes[0].mode, changes[0].session_id, changes[0].persist) == (mode.value, None, True)
        await bus.publish(events.ApprovalModeUpdated(mode=mode.value, session_id="chat"))
        assert main.header_approval_mode is mode
        if not selected:
            await open_workflow(main, pilot, "demo-workflow")
        assert main._workflow.session_view.selection is None
        await switch_mode(main, pilot)
        assert badge.display and main.header_approval_mode is mode
        await switch_mode(main, pilot)
        assert badge.display and main.header_approval_mode is mode
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert main._workflow.run_control.start("review")
            await wait_for(lambda: bool(requests), pilot=pilot)
            assert isinstance(requests[0].target, WorkflowDraft)
            assert requests[0].target.workspace == main._workflow.browser.draft.workspace


async def test_restoring_a_workflow_keeps_the_launch_badge_without_writing_approval_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        store = main._services.state_store
        assert store is not None
        session_id, run_id = str(uuid4()), uuid4().hex
        await record_workflow_run(
            store.session_dir(session_id) / "workflows" / run_id,
            session_id=session_id,
            title=run_id,
            outcome="completed",
        )
        await save_workflow_session(store, session_id, project)
        main.chat_session_id = "chat"
        await bus.publish(events.ApprovalModeUpdated(mode="bypass", session_id="chat"))
        await switch_mode(main, pilot)
        checkpoint = store.session_dir(session_id) / "session.json"
        before = checkpoint.read_bytes()
        async with capture_event_sequence(bus, events.SetApprovalMode) as changes:
            await main._workflow.session_view.restore_session(session_id)
        assert not changes
        assert checkpoint.read_bytes() == before
        assert main._workflow.session_id == session_id
        assert main.header_approval_mode is ApprovalMode.BYPASS
        async with capture_event_sequence(bus, events.SetApprovalMode) as quiet:
            await bus.publish(events.ApprovalModeUpdated(mode="bypass", session_id="chat"))
            assert not quiet
        assert main.header_approval_mode is ApprovalMode.BYPASS


async def test_workflow_diff_reads_active_run_snapshot_not_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        main._workflow.session_view.selection = workflow_selection(main, "workflow")
        store = main._services.state_store
        assert store is not None
        tracker = MutationTracker(SnapshotStore(store.session_dir("workflow")))
        file = project / "changed.txt"
        file.write_text("before")
        tracker.start_workflow_run("run-one")
        mutation = tracker.record(str(file), MutationOp.MODIFY, MutationSource.WRITE_FILE, "write")
        assert mutation is not None
        file.write_text("after")
        tracker.record_after(mutation)
        snapshot = create_autospec(engine.workflows.mutation_snapshot, return_value=tracker.serialize())
        monkeypatch.setattr(engine.workflows, "mutation_snapshot", snapshot)
        main.action_show_diff()
        await wait_for(lambda: isinstance(app.screen, DiffScreen) and app.screen._content_ready, pilot=pilot)
        snapshot.assert_called_once_with("workflow")
        assert app.screen.query_one("#--content-tab-turn-1", Tab).label.plain == "Run 1"


async def test_draft_reselection_keeps_its_workspace_after_hidden_chat_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    write_workflow(project, "alternate", python_workflow("def fn(value):\n    return value\n", "fn"))
    monkeypatch.chdir(project)
    engine = WorkflowEngine()
    engine.workspace = Workspace.from_cwd(project)
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        await main._workflow.browser.change_draft_workspace(str(project))
        original = main._workflow.browser.draft
        assert original is not None
        await switch_mode(main, pilot)
        engine.workspace = Workspace.from_cwd(tmp_path / "hidden-chat")
        await switch_mode(main, pilot)
        await open_workflow(main, pilot, "alternate")
        assert main._workflow.browser.draft.workspace == original.workspace


async def test_bound_start_checks_shadow_identity_before_preview_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(140, 42)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        controller = main._workflow
        controller.session_view.selection = workflow_selection(main, "bound")
        marker = project / "should-not-execute"
        source = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n".encode()
        write_workflow(
            project, "demo-workflow", source + python_workflow("def check(value):\n    return value\n", "check")
        )
        assert not controller.run_control.start("review")
        task = controller.browser.preview_flow.task
        assert task is not None
        await task
        assert not marker.exists()
        assert controller.session_id == "bound"
        await wait_for(lambda: "another workflow source" in workflow_notice_text(main), pilot=pilot)
