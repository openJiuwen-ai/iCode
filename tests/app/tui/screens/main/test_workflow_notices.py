# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow feedback stays in owned modals without changing the underlying view."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.widgets import Button, Static, TabbedContent

from chrys.app.tui import i18n as tui_i18n
from chrys.app.tui.screens.dialogs.agent_load import AgentLoadDialog
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog, NoticeDialog
from chrys.app.tui.screens.main import workflow_content
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.footer import ChrysFooter
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.app.tui.widgets.loading import ChrysLoadingIndicator
from chrys.foundation.events import types as events
from chrys.foundation.events.bus import EventBus
from chrys.orchestration.workflows.catalog import WorkflowCatalog
from chrys.orchestration.workflows.preview import WorkflowPreview
from tests.orchestration.workflows._hosting import make_project
from tests.support.pilot_barrier import screen_is_settled
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine, dismiss_workflow_notice, open_workflow, switch_mode


async def test_missing_selection_notice_preserves_draft_and_underlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    monkeypatch.setattr(tui_i18n, "persist_locale", lambda _locale: None)
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(100, 32)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        cards = [ToolCall(f"chat-{index}", "read_file", args={"path": f"file-{index}.py"}) for index in range(30)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("File contents\n" * 20)
        await wait_for(lambda: chat.virtual_size.height > chat.size.height, pilot=pilot)
        app.locale_controller.switch_locale("zh-Hans")
        await switch_mode(main, pilot)
        composer = main.query_one(InputBar)
        composer.replace_draft("draft [literal]")
        composer.focus_input()
        footer = main.query_one(ChrysFooter)
        # The spies count only the notice's own work: the draft's edit and focus, and the
        # mode switch's deferred refresh, have to have reached the screen's layout first.
        await wait_for(
            lambda: (
                composer.snapshot_draft().text == "draft [literal]"
                and screen_is_settled(app, main)
                and not main._workflow._refresh_pending
                and not footer._binding_recompose_in_progress
                and not footer._binding_recompose_dirty
                and footer._visible_binding_signature == footer._binding_signature(main)
            ),
            pilot=pilot,
        )
        layout = create_autospec(main._refresh_layout, side_effect=main._refresh_layout)
        styles = create_autospec(main.update_node_styles, side_effect=main.update_node_styles)
        recompose = create_autospec(footer.recompose, side_effect=footer.recompose)
        monkeypatch.setattr(main, "_refresh_layout", layout)
        monkeypatch.setattr(main, "update_node_styles", styles)
        monkeypatch.setattr(footer, "recompose", recompose)
        main._on_workflow_start()
        await wait_for(
            lambda: (
                isinstance(app.screen, NoticeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        dialog = app.screen
        button = dialog.query_one("#confirm-yes", Button)
        await wait_for(
            lambda: dialog.is_mounted and button.has_focus and screen_is_settled(app, dialog),
            pilot=pilot,
        )
        container = dialog.query_one("#confirm-container")
        assert abs(container.region.x * 2 + container.region.width - app.size.width) <= 1
        assert abs(container.region.y * 2 + container.region.height - app.size.height) <= 1
        assert container.styles.border.top[0] == "round"
        assert "请先选择工作流" in str(dialog.query_one("#confirm-message", Static).content)
        assert len(dialog.query(Button)) == 1
        assert composer.snapshot_draft().text == "draft [literal]"
        assert not main._workflow_panel.query("#workflow-error, #workflow-banner")
        app.save_screenshot("workflow-notice.svg", path=str(tmp_path))
        main._workflow.refresh()
        assert app.screen is dialog
        await pilot.press("enter")
        await wait_for(lambda: app.screen is main and main._workflow.feedback.notice is None, pilot=pilot)
        main._workflow.refresh()
        assert not main._workflow.feedback._pending_notices
        assert composer.snapshot_draft().text == "draft [literal]"
        assert layout.call_count == styles.call_count == recompose.call_count == 0


async def test_rejection_waits_for_other_modal_and_long_notice_remains_dismissible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hold_refresh_callbacks = True
    # Hold post-refresh callbacks to prove the initial layout already keeps
    # long notices scrollable and their actions on screen.
    invoke_callbacks = NoticeDialog._invoke_and_clear_callbacks

    async def invoke_when_released(dialog: NoticeDialog) -> None:
        if not hold_refresh_callbacks:
            await invoke_callbacks(dialog)

    monkeypatch.setattr(NoticeDialog, "_invoke_and_clear_callbacks", invoke_when_released)
    monkeypatch.chdir(make_project(tmp_path))
    bus = EventBus()
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine(), event_bus=bus)
    async with app.run_test(size=(100, 28)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        controller, panel = main._workflow, main._workflow_panel
        assert controller.run_control.start("draft")
        request_id = main._workflow.run_control._pending_run.request_id
        covering = ConfirmDialog()
        await app.push_screen(covering)
        message = "\n".join(f"Traceback [literal] {line}" for line in range(70))
        await bus.publish(events.WorkflowRunRejected(request_id=request_id, error="invalid", message=message))
        controller.refresh()
        assert app.screen is covering and controller.feedback.notice is None
        assert len(controller.feedback._pending_notices) == 1
        await pilot.press("escape")
        await wait_for(
            lambda: (
                isinstance(app.screen, NoticeDialog)
                and app.screen.is_mounted
                and bool(app.screen.query("#confirm-yes"))
            ),
            pilot=pilot,
        )
        dialog = app.screen
        button = dialog.query_one("#confirm-yes", Button)
        try:
            await wait_for(lambda: dialog.is_mounted and button.region.width > 0, pilot=pilot)
            assert button.region.bottom <= app.size.height
            initial_button = button.region
            assert not screen_is_settled(app, dialog)
        finally:
            hold_refresh_callbacks = False
            dialog.check_idle()
        await wait_for(lambda: screen_is_settled(app, dialog), pilot=pilot)
        assert button.region == initial_button
        app.save_screenshot("workflow-long-notice.svg", path=str(tmp_path))
        assert button.region.bottom <= app.size.height, [
            (widget.id, widget.region, widget.styles.height, widget.styles.max_height)
            for widget in dialog.query("#confirm-container, #confirm-inner, #confirm-message, #confirm-buttons")
        ]
        assert str(dialog.query_one("#confirm-message", Static).content) == f"invalid: {message}"
        body = dialog.query_one("#notice-scroll")
        assert body.max_scroll_y > 0
        body.scroll_end(animate=False)
        await wait_for(lambda: body.scroll_y == body.max_scroll_y, pilot=pilot)
        assert app.get_widget_at(button.region.x, button.region.y)[0] is button
        await click_when_settled(pilot, button)
        await wait_for(lambda: app.screen is main and controller.feedback.notice is None, pilot=pilot)
        await wait_for(lambda: not panel.query_one("#workflow-start", Button).disabled, pilot=pilot)
        warnings = panel.query_one("#workflow-manifest-warnings", Static)
        assert list(panel.query_one("#workflow-graph-tab").query(Static)) == [
            panel.query_one("#workflow-header", Static),
            warnings,
        ]
        assert not warnings.display


async def test_source_read_error_does_not_reopen_notice_on_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        await open_workflow(main, pilot, "demo-workflow")
        read = create_autospec(workflow_content.read_entry_bytes, side_effect=OSError("source [unavailable]"))
        monkeypatch.setattr(workflow_content, "read_entry_bytes", read)
        tabs = main._workflow_panel.query_one(TabbedContent)
        await click_when_settled(pilot, tabs.get_tab("workflow-code-tab"))
        await dismiss_workflow_notice(main, pilot, "source [unavailable]")
        await wait_for(lambda: not main._workflow._tasks and not main._workflow._refresh_pending, pilot=pilot)
        assert app.screen is main and not main._workflow.feedback._pending_notices
        assert tabs.active == "workflow-code-tab"
        assert read.call_count >= 1


async def test_preview_completion_does_not_pop_covering_modal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(make_project(tmp_path))
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        preview = await main._workflow.browser.catalog.preview("demo-workflow")
        ready, release = asyncio.Event(), asyncio.Event()

        async def load(
            catalog: WorkflowCatalog,
            workflow_id: str,
            *,
            timeout: float | None = None,
            request_id: str = "",
            expected_identity=None,
            authorize=None,
        ) -> WorkflowPreview:
            ready.set()
            await release.wait()
            return preview

        monkeypatch.setattr(WorkflowCatalog, "preview", create_autospec(WorkflowCatalog.preview, side_effect=load))
        await switch_mode(main, pilot)
        main._workflow.browser.open("demo-workflow")
        try:
            await wait_for(
                lambda: ready.is_set() and isinstance(app.screen, AgentLoadDialog) and app.screen.is_mounted,
                pilot=pilot,
            )
            loading = app.screen
            assert loading.query_one(ChrysLoadingIndicator).display
            assert not loading.query_one("#agent-load-buttons").display
            await pilot.press("escape")
            assert app.screen is loading and main._workflow.feedback.loading is loading
            await main._services.bus.publish(
                events.WorkflowPreviewProgress(
                    request_id=main._workflow.browser._preview_request_id, workflow_id="demo-workflow", stage="graph"
                )
            )
            body = str(loading.query_one("#agent-load-message", Static).content)
            assert "Workflow definition" in body and "Execution environment" in body and "Workflow graph" in body
            assert "Preparing agent" not in body
            covering = ConfirmDialog()
            await app.push_screen(covering)
            release.set()
            await wait_for(lambda: main._workflow.feedback.loading is None, pilot=pilot)
            assert loading.dismiss_requested and app.screen is covering
            assert main._workflow_panel.preview is None
            await pilot.press("escape")
            await wait_for(lambda: main._workflow_panel.preview is preview and app.screen is main, pilot=pilot)
            assert loading not in app.screen_stack
        finally:
            release.set()
