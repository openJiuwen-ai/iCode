# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Directory clicks and commands respect the workspace bound by a workflow session."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Static

from chrys.app.tui.screens.dialogs.confirm import NoticeDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.widgets.welcome import WelcomeWidget
from chrys.foundation.config.settings import Settings
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.workflow_session import WorkspaceSnapshot
from chrys.foundation.models.workspace import WorkingDir, Workspace
from tests.app.tui.screens.main._workflow_support import workflow_selection
from tests.orchestration.workflows._hosting import make_project, write_workflow
from tests.support.event_capture import capture_event_sequence
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for
from tests.support.workflow_workers import python_workflow

from ._workflow_support import WorkflowEngine, open_workflow, switch_mode


@pytest.mark.parametrize("changed", [False, True])
async def test_workspace_update_with_same_cwd_preserves_preparation_and_captures_all_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: bool
) -> None:
    project = make_project(tmp_path)
    monkeypatch.chdir(project)
    engine, bus = WorkflowEngine(), EventBus()
    engine.workspace = Workspace.from_cwd(str(project))
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        preview = main._workflow_panel.preview
        controller = main._workflow
        catalog = controller.browser.catalog
        if changed:
            shared = tmp_path / "shared"
            shared.mkdir()
            engine.workspace.working_dirs.append(WorkingDir(str(shared), "Shared context"))
            engine.workspace.reference_files.append(str(project / "README.md"))
        await bus.publish(
            events.WorkspaceUpdated(
                primary_cwd=str(project),
                working_dirs=[item.path for item in engine.workspace.working_dirs],
                reference_files=engine.workspace.reference_files,
            ),
            raise_handler_errors=True,
        )
        assert controller.session_view.selection is None and controller.browser.loaded is not None
        assert controller.workspace == WorkspaceSnapshot.capture(engine.workspace)
        assert controller.browser.loaded.preview.source.workflow_id == main._workflow_panel.definition.workflow_id
        assert controller.browser.catalog is catalog and main._workflow_panel.preview is preview
        assert controller.run_control._run_input_context().can_change_directory
        async with capture_event_sequence(bus, events.WorkflowRunRequest) as requests:
            assert controller.run_control.start("review")
            await wait_for(lambda: bool(requests), pilot=pilot)
        assert requests[0].target.workspace == WorkspaceSnapshot.capture(engine.workspace)


@pytest.mark.parametrize("entry", ["click", "chdir", "delayed_picker"])
@pytest.mark.parametrize("locale", ["en", "zh-Hans"])
async def test_bound_workspace_shows_the_same_modal_and_chat_still_changes_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: str, locale: str
) -> None:
    project = make_project(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(project)
    bus = EventBus()
    app = make_chrys_app(
        tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus, settings=Settings(locale=locale)
    )
    async with app.run_test(size=(140, 48)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        controller, panel = main._workflow, main._workflow_panel
        assert controller.run_control.start("review")
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=controller.run_control._pending_run.request_id,
                run_id="run",
                selection=workflow_selection(main, "bound"),
            )
        )
        await bus.publish(events.WorkflowRunFinished(run_id="run", session_id="bound", outcome="completed"))
        await wait_for(lambda: app.screen is main and not main._workflow.awaiting_engine, pilot=pilot)
        async with capture_event_sequence(bus, events.WorkspaceChange) as changes:
            if entry == "click":
                await wait_for(lambda: panel.region.height > 1 and bool(panel.border_subtitle), pilot=pilot)
                await click_when_settled(pilot, panel, offset=(panel.region.width // 2, panel.region.height - 1))
            elif entry == "chdir":
                assert main._suggestions.dispatch_slash_command(f"/chdir {other}")
            else:
                # A picker opened during the draft may finish after a Run was accepted.
                main._workspace_actions.on_chdir_dialog_result(str(other))
            await wait_for(
                lambda: (
                    isinstance(app.screen, NoticeDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query("#confirm-no"))
                ),
                pilot=pilot,
            )
            dialog = app.screen
            message = str(dialog.query_one("#confirm-message", Static).content)
            assert "/new" in message
            assert ("bound to its workspace" if locale == "en" else "已绑定工作区") in message
            main._workspace_actions.open_working_dir_picker()
            assert app.screen is dialog and changes == []
            close = dialog.query_one("#confirm-no")
            await wait_for(
                lambda: (
                    close.region.width > 0
                    and app.screen.region.contains(close.region.x, close.region.y)
                    and app.get_widget_at(close.region.x, close.region.y)[0] is close
                ),
                pilot=pilot,
            )
            await click_when_settled(pilot, close)
            await wait_for(lambda: app.screen is main, pilot=pilot)
            assert controller.session_id == "bound" and controller.project_cwd == str(project)
            main._set_workflow_mode(False)
            await main._workspace_actions.start_chdir(str(other)).wait()
            assert [event.primary_cwd for event in changes] == [str(other)]
            assert controller.project_cwd == str(project)


async def test_workspace_modal_new_session_opens_picker_and_preserves_history_on_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(140, 48)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        controller = main._workflow
        assert controller.run_control.start("review")
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=controller.run_control._pending_run.request_id,
                run_id="run",
                selection=workflow_selection(main, "bound"),
            )
        )
        await bus.publish(events.WorkflowRunFinished(run_id="run", session_id="bound", outcome="cancelled"))
        main._workspace_actions.open_working_dir_picker()
        await wait_for(
            lambda: (
                isinstance(app.screen, NoticeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        await click_when_settled(pilot, "#confirm-yes")
        await wait_for(lambda: isinstance(app.screen, WorkflowPickerDialog) and app.screen.is_mounted, pilot=pilot)
        assert controller.session_id == "bound"
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        assert controller.session_id == "bound"


async def test_draft_workspace_change_reloads_preview_for_the_new_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = make_project(tmp_path)
    other = make_project(tmp_path / "other")
    monkeypatch.chdir(project)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(140, 48)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        original = main._workflow_panel.preview
        async with capture_event_sequence(bus, events.WorkspaceChange) as changes:
            await main._workspace_actions.start_chdir(str(other)).wait()
            assert not changes
        await wait_for(
            lambda: (
                main._workflow_panel.preview is not original
                and main._workflow.browser.loaded.catalog is main._workflow.browser.catalog
                and not main._workflow_panel.stale
                and app.screen is main
            ),
            pilot=pilot,
        )
        assert main._workflow.browser.catalog.project_cwd == other
        assert main._workflow.session_id == ""


@pytest.mark.parametrize("command", ["chdir", "cd"])
async def test_running_workflow_directory_command_shows_workspace_modal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    bus, engine = EventBus(), WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine, event_bus=bus)
    async with app.run_test(size=(140, 48)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        controller = main._workflow
        assert controller.run_control.start("review")
        await engine.set_execution(ExecutionSnapshot("workflow", "run", True), main._services.bus)
        await bus.publish(
            events.WorkflowRunAccepted(
                request_id=controller.run_control._pending_run.request_id,
                run_id="run",
                selection=workflow_selection(main, "bound"),
            )
        )
        async with capture_event_sequence(bus, events.WorkspaceChange) as changes:
            text = f"/{command} {other}"
            assert main._suggestions.dispatch_slash_command(text)
            await wait_for(
                lambda: (
                    isinstance(app.screen, NoticeDialog)
                    and app.screen.is_mounted
                    and bool(app.screen.query("#confirm-yes"))
                ),
                pilot=pilot,
            )
            assert "/new" in str(app.screen.query_one("#confirm-message", Static).content)
            assert not app.screen.query("#confirm-no")
            assert changes == []
            await pilot.press("escape")
            await wait_for(lambda: app.screen is main, pilot=pilot)


async def test_empty_draft_workspace_change_updates_welcome_and_new_session_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, other = tmp_path / "original", tmp_path / "other"
    original.mkdir()
    other.mkdir()
    write_workflow(other, "only_here", python_workflow("def echo(text):\n    return text\n", "echo"))
    # Neither directory is a Git repository: branch-change events must not mask a missing refresh.
    monkeypatch.chdir(original)
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(140, 48)) as pilot:
        main = app._main_screen
        assert main is not None
        await switch_mode(main, pilot)
        panel = main._workflow_panel
        await wait_for(
            lambda: panel.workspace_cwd == str(original) and not main._workflow._refresh_pending,
            pilot=pilot,
        )
        assert panel.preview is None and not main._workflow.session_id
        async with capture_event_sequence(bus, events.WorkspaceChange) as changes:
            await main._workspace_actions.start_chdir(str(other)).wait()
            assert not changes
        assert main._workspace_cwd() == str(original)
        await wait_for(lambda: panel.workspace_cwd == str(other), pilot=pilot)
        assert panel.query_one(WelcomeWidget).render().cwd == str(other)
        assert panel.preview is None and not main._workflow.session_id
        await click_when_settled(pilot, "#workflow-new")
        await wait_for(
            lambda: (
                isinstance(app.screen, WorkflowPickerDialog)
                and app.screen.is_mounted
                and bool(main._workflow.browser._picker.selection.rows)
            ),
            pilot=pilot,
        )
        assert "only_here" in [row.workflow_id for row in main._workflow.browser._picker.selection.rows]
        picker = main._workflow.browser._picker.selection.list
        picker.highlighted = next(
            i
            for i, row in enumerate(main._workflow.browser._picker.selection.rows)
            if row.workflow_id == "demo-workflow"
        )
        picker.focus()
        await pilot.press("enter")
        await wait_for(
            lambda: app.screen is main and panel.preview is not None, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT
        )
        assert main._workflow.session_view.selection is None
        assert main._workflow.workspace.primary_cwd == str(other)
        assert main._workspace_cwd() == str(original)
