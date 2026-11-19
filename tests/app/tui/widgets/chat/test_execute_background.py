# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A main-agent shell card says how to move its command to the background, and shows that it moved."""

from __future__ import annotations

import pytest

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.execute import ExecuteToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.foundation.tool_result_metadata import SHELL_BACKGROUND_TASK_METADATA_KEY, SHELL_EXIT_CODE_METADATA_KEY
from tests.support.tui_helpers import ChatPanelApp, open_process_for

_BACKGROUND = {SHELL_BACKGROUND_TASK_METADATA_KEY: {"id": "bg-1", "pid": 4242, "trigger": "user", "log": "/l"}}


@pytest.mark.asyncio
async def test_running_shell_card_hints_ctrl_t_and_ends_in_the_background() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "bash", "shell", args={"command": "npm run dev", "timeout": 120})
        open_process_for(panel.query(ToolGroup).last())
        panel.query(ToolGroup).last().collapsed = False
        card = pilot.app.query_one(ExecuteToolCall)
        assert card.offers_background
        assert "ctrl+t moves it to the background" in card._running_label_text().plain

        await panel.add_tool_result("c1", "bash", "Moved to the background", 25, metadata=_BACKGROUND)
        await pilot.pause()
        assert card.has_class("-backgrounded")
        assert not card.has_class("-success")
        assert str(card.query_one("#exec-panel").border_subtitle) == "→ in the background as bg-1, pid 4242"


@pytest.mark.asyncio
async def test_a_failed_shell_card_still_says_what_moved_to_the_background() -> None:
    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_tool_start("c1", "bash", "shell", args={"command": "npm run dev & npm test", "timeout": 120})
        open_process_for(panel.query(ToolGroup).last())
        panel.query(ToolGroup).last().collapsed = False
        card = pilot.app.query_one(ExecuteToolCall)

        metadata = {**_BACKGROUND, SHELL_EXIT_CODE_METADATA_KEY: 3}
        await panel.add_tool_result("c1", "bash", "1 test failed\n[exit_code: 3]", 25, metadata=metadata)
        await pilot.pause()
        assert card.has_class("-error")
        assert str(card.query_one("#exec-panel").border_subtitle) == "Errored [3] → in the background as bg-1, pid 4242"


def test_a_shell_card_outside_the_main_transcript_offers_no_background() -> None:
    card = ExecuteToolCall("c1", "bash", args={"command": "npm run dev", "timeout": 120})
    assert "background" not in card._running_label_text().plain
