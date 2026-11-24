# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow-only background work and compact menus follow actual screen visibility."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.widgets import OptionList

from chrys.app.tui.screens.dialogs.app_mode import AppModeDialog
from chrys.app.tui.screens.dialogs.approval.mode import ApprovalModeScreen
from chrys.app.tui.screens.dialogs.confirm import ConfirmDialog
from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolCall
from chrys.app.tui.widgets.chrome.status_bar import StatusBar
from chrys.app.tui.widgets.workflow.selection import WorkflowRow, WorkflowSelection
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.approval.policy import ApprovalMode
from chrys.service.workflows.discovery import SkippedSource
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_chat_modal_resume_skips_source_checks_and_lease_updates_need_no_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = WorkflowEngine()
    app = make_chrys_app(tmp_path / "sessions", engine=engine)
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        chat = main.query_one(ChatPanel)
        cards = [ToolCall(f"call-{index}", "read_file", args={"path": f"file-{index}"}) for index in range(20)]
        await chat.mount(*cards)
        for card in cards:
            card.set_complete("contents\n" * 15)
        await wait_for(lambda: chat.virtual_size.height > chat.size.height, pilot=pilot)
        check = create_autospec(main._workflow.browser.check_preview, side_effect=main._workflow.browser.check_preview)
        monkeypatch.setattr(main._workflow.browser, "check_preview", check)
        assert main._workflow_timer is None
        await app.push_screen(ConfirmDialog())
        await pilot.press("escape")
        await wait_for(lambda: app.screen is main, pilot=pilot)
        check.assert_not_called()
        bar = main.query_one(StatusBar)
        await engine.set_execution(ExecutionSnapshot("turn", cancellable=True), main._services.bus)
        assert main._execution_binding_busy and not bar._tags_interactive()
        assert main._workflow_timer is None
        await engine.set_execution(ExecutionSnapshot("idle"), main._services.bus)
        assert not main._execution_binding_busy and bar._tags_interactive()

        main._set_workflow_mode(True)
        await wait_for(lambda: main._workflow_timer is not None, pilot=pilot)
        await app.push_screen(ConfirmDialog())
        await wait_for(lambda: main._workflow_timer is None, pilot=pilot)
        await pilot.press("escape")
        await wait_for(lambda: main._workflow_timer is not None, pilot=pilot)
        main._set_workflow_mode(False)
        assert main._workflow_timer is None


@pytest.mark.parametrize("menu", ["mode", "approval", "workflow"])
async def test_menu_descriptions_rewrap_after_layout_and_resize(tmp_path: Path, menu: str) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(100, 36)) as pilot:
        if menu == "mode":
            dialog = AppModeDialog(False)
        elif menu == "approval":
            dialog = ApprovalModeScreen(ApprovalMode.MANUAL)
        else:
            selection = WorkflowSelection()
            selection.show_rows(
                [
                    WorkflowRow(
                        SkippedSource(
                            path="/tmp/example.py",
                            reason="A long warning about a workflow that could not be read from this directory.",
                            source_kind="global",
                        ),
                        "",
                    )
                ],
                "",
            )
            dialog = WorkflowPickerDialog(selection, delete=lambda: None)
        await app.push_screen(dialog)
        await wait_for(lambda: dialog.is_mounted and dialog.query_one(OptionList).size.width > 40, pilot=pilot)
        options = dialog.query_one(OptionList)
        wide = str(options.get_option_at_index(0).prompt)
        highlighted = options.highlighted
        await pilot.resize_terminal(22, 36)
        await wait_for(
            lambda: options.size.width < 22 and str(options.get_option_at_index(0).prompt) != wide, pilot=pilot
        )
        narrow = str(options.get_option_at_index(0).prompt)
        assert narrow.count("\n") > wide.count("\n")
        assert options.highlighted == highlighted
        await pilot.resize_terminal(100, 36)
        await wait_for(lambda: str(options.get_option_at_index(0).prompt) == wide, pilot=pilot)
