# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The synthetic transcripts that the chat panel profiler and the GC freeze calibration build.

Only the scheduled GC freeze calibration runs those scripts; building every
workload here keeps them on the chat panel's current API.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from scripts.chat_panel_workloads import (
    _populate_file_diff,
    _populate_mixed_markdown,
    _populate_realistic_session,
    _populate_sub_agent_markdown,
    _populate_table_markdown,
)

from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard
from tests.support.tui_helpers import ChatPanelApp
from tests.support.waiting import wait_for

_TURN = ["intermediate", "answer"]


@pytest.mark.parametrize(
    ("populate", "messages", "tool_calls"),
    [
        (lambda panel: _populate_realistic_session(panel, turns=2, collapsed=False), _TURN * 2, 12),
        (lambda panel: _populate_file_diff(panel, turns=2, tool_sets_per_turn=1, collapsed=False), _TURN * 2, 6),
        (
            lambda panel: _populate_sub_agent_markdown(
                panel, turns=2, tool_sets_per_turn=1, collapsed=False, markdown_lines=20
            ),
            _TURN * 2,
            2,
        ),
        (
            lambda panel: _populate_table_markdown(
                panel, turns=2, messages_per_turn=2, markdown_lines=10, table_columns=2, table_rows=2, cell_words=0
            ),
            ["answer"] * 4,
            0,
        ),
        (
            lambda panel: _populate_mixed_markdown(
                panel,
                turns=2,
                messages_per_turn=2,
                markdown_lines=10,
                fence_lines=2,
                table_columns=2,
                table_rows=2,
                cell_words=0,
            ),
            ["answer"] * 4,
            0,
        ),
    ],
    ids=["realistic-session", "file-diff", "sub-agent-markdown", "table-markdown", "mixed-markdown"],
)
async def test_a_workload_builds_its_turns_through_the_live_chat_api(
    populate: Callable[[ChatPanel], Awaitable[None]],
    messages: list[str],
    tool_calls: int,
) -> None:
    """Text before a tool call settles as intermediate text, and each turn's answer completes."""
    async with ChatPanelApp().run_test(size=(120, 36)) as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await populate(panel)
        await pilot.pause()

        shown = panel.mounted_agent_messages()
        assert ["intermediate" if message.has_class("--intermediate") else "answer" for message in shown] == messages
        assert not any(message.has_class("--streaming") for message in shown)
        await wait_for(
            lambda: len(panel.query(BaseToolCard)) == tool_calls,
            pilot=pilot,
            description="expanded workloads restored their completed tool cards",
        )
        assert len(panel.query(BaseToolCard)) == tool_calls
