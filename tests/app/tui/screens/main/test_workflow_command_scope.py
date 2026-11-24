# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The Chat command list exposes Workflow mode and enforces execution guards."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.app.tui.screens.dialogs.workflow_picker import WorkflowPickerDialog
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import wait_for

from ._workflow_support import WorkflowEngine

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def test_workflow_command_is_discoverable_and_disabled_during_a_chat_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = make_chrys_app(tmp_path / "sessions", engine=WorkflowEngine())
    async with app.run_test(size=(120, 40)) as pilot:
        main = app._main_screen
        assert main is not None
        commands = {command.name: command for command in main._suggestions._visible_slash_commands()}
        assert "workflow" in commands
        assert not commands["workflow"].allow_while_running
        notify = create_autospec(main._suggestions._callbacks.notify_warning)
        monkeypatch.setattr(
            main._suggestions, "_callbacks", replace(main._suggestions._callbacks, notify_warning=notify)
        )
        main._set_agent_running(True)
        assert main._suggestions.dispatch_slash_command("/workflow")
        notify.assert_called_once()
        assert not main._workflow.workflow_mode and app.screen is main
        main._set_agent_running(False)
        assert main._suggestions.dispatch_slash_command("/workflow")
        await wait_for(lambda: isinstance(app.screen, WorkflowPickerDialog) and app.screen.is_mounted, pilot=pilot)
        assert main._workflow.workflow_mode
