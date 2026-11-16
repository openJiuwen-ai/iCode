# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the sleep tool renderer."""

from __future__ import annotations

import json

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Button, Static

from chrys.app.tui.widgets.chat.agent_transcript_surface import AgentTranscriptSurface
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sleep import SleepSkipClicked, SleepToolCall
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import BaseToolCard, ToolGroup
from chrys.foundation.tool_kinds import KIND_SLEEP, KIND_SUB_AGENT
from chrys.foundation.tool_result_metadata import TOOL_FAILED_METADATA_KEY, TOOL_INTERRUPTED_METADATA_KEY
from chrys.service.session.message_metadata import TOOL_RESULT_METADATA_KEY
from tests.support.tui_helpers import LocalizedWidgetApp
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


async def _wait_for_nested_sleep(card: SubAgentToolCall, call_id: str, pilot: object) -> SleepToolCall:
    surface = card._tool_view_output_widgets()[0]
    assert isinstance(surface, AgentTranscriptSurface)
    await card.app.mount(surface)
    await wait_for(
        lambda: surface.is_mounted and len(surface.query(ToolGroup)) == 1,
        pilot=pilot,
        description="sub-agent sleep detail mounted",
    )
    group = surface.query_one(ToolGroup)
    matched: list[SleepToolCall] = []

    def find_sleep() -> bool:
        group.collapsed = False
        nested = group.get_tool(call_id)
        if isinstance(nested, SleepToolCall):
            matched[:] = [nested]
            return True
        return False

    await wait_for(find_sleep, pilot=pilot, description=f"nested sleep {call_id}")
    return matched[0]


def test_sleep_renderer_uses_base_tool_card_contract() -> None:
    tool = SleepToolCall(
        "c1",
        "sleep",
        args_summary=json.dumps({"seconds": 3, "reason": "wait"}),
    )

    assert isinstance(tool, BaseToolCard)
    assert tool.call_id == "c1"
    assert tool.tool_name == "sleep"
    assert tool.status == "running"
    assert tool.result_text == ""
    assert tool.duration_ms == 0
    assert tool.args == {"seconds": 3, "reason": "wait"}


def test_sleep_renderer_records_completion_approval() -> None:
    tool = SleepToolCall("c1", "sleep", args={"seconds": 3})

    tool.set_complete("Rejected by user", approval="user_rejected")

    assert tool.approval == "user_rejected"


def test_sleep_renderer_rejected_status_survives_unmounted_query_failure() -> None:
    tool = SleepToolCall("c1", "sleep", args={"seconds": 3})

    tool.set_complete("Rejected by user", approval="user_rejected")

    assert tool.status == "rejected"
    assert tool.has_class("-rejected")


def test_sleep_renderer_error_status_survives_unmounted_query_failure() -> None:
    tool = SleepToolCall("c1", "sleep", args={"seconds": 3})

    tool.set_complete("Error: invalid duration", metadata={TOOL_FAILED_METADATA_KEY: True})

    assert tool.status == "error"
    assert tool.has_class("-error")


def test_sleep_renderer_reads_args_summary_json() -> None:
    tool = SleepToolCall(
        "c1",
        "sleep",
        args_summary=json.dumps({"seconds": 12, "reason": "wait for server boot"}),
    )

    assert tool._seconds == 12
    assert tool._reason == "wait for server boot"


@pytest.mark.parametrize("replay", [False, True], ids=["live", "replay"])
async def test_sleep_reason_renders_controls_as_single_line_literal(*, replay: bool) -> None:
    reason = "Waiting 中文 \x1b[24D\x9b24D\r\n\tend-marker"
    args = {"seconds": 30, "reason": reason}
    tool = (
        SleepToolCall("c1", "sleep", args_summary=json.dumps(args))
        if replay
        else SleepToolCall("c1", "sleep", args=args)
    )
    async with LocalizedWidgetApp(lambda: tool).run_test(size=(120, 20)) as pilot:
        panel = tool.query_one("#sleep-reason", Static)
        await wait_for(
            lambda: "Waiting" in panel.render_line(0).text,
            pilot=pilot,
            description="sleep reason has rendered",
        )
        expected = "Waiting 中文 �[24D�24D���end-marker"
        assert isinstance(panel.content, Text)
        assert panel.content.plain == expected
        assert panel.render_line(0).text.rstrip() == expected
        assert panel.size.height == 1
        assert tool.args["reason"] == reason
        assert json.loads(tool._tool_copy_input()[1])["reason"] == reason


@pytest.mark.parametrize(
    ("metadata", "subtitle"),
    [
        ({"sleep_skipped": True}, "Skipped"),
        ({"sleep_interrupted": True}, "Interrupted"),
    ],
)
@pytest.mark.asyncio
async def test_sleep_renderer_uses_metadata_for_terminal_status(metadata: dict[str, bool], subtitle: str) -> None:
    class ToolApp(App):
        def compose(self) -> ComposeResult:
            yield SleepToolCall("c1", "sleep", args={"seconds": 5})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SleepToolCall)

        tool.set_complete("done by external signal", duration_ms=10, metadata=metadata)
        await pilot.pause()

        assert tool.query_one("#sleep-panel").border_subtitle == subtitle


@pytest.mark.asyncio
async def test_sleep_renderer_places_countdown_next_to_skip_button() -> None:
    class ToolApp(App):
        def compose(self) -> ComposeResult:
            yield SleepToolCall("c1", "sleep", args={"seconds": 5, "reason": "wait"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SleepToolCall)
        countdown = tool.query_one("#sleep-countdown", Static)

        assert countdown.parent is tool.query_one("#sleep-actions")


@pytest.mark.asyncio
async def test_sub_agent_sleep_skip_button_posts_sleep_skip_message() -> None:
    class ToolApp(App):
        def __init__(self) -> None:
            super().__init__()
            self.skipped: list[str] = []

        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("parent", "Explore", args={"prompt": "watch the service"})

        def on_sleep_skip_clicked(self, event: SleepSkipClicked) -> None:
            self.skipped.append(event.call_id)
            event.stop()

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)

        await tool.add_inner_tool_start("sleep1", "pause", {"seconds": 30}, tool_kind=KIND_SLEEP)
        await pilot.pause()
        assert tool.has_class("-sleeping")

        tool.query_one("#sa-skip-sleep-btn", Button).press()
        await pilot.pause()

        assert pilot.app.skipped == ["sleep1"]

        tool.complete_inner_tool(
            "sleep1", "Sleep skipped by user after 0 seconds.", 0, metadata={"sleep_skipped": True}
        )
        await pilot.pause()
        assert not tool.has_class("-sleeping")
        nested_sleep = await _wait_for_nested_sleep(tool, "sleep1", pilot)
        assert nested_sleep.query_one("#sleep-panel").border_subtitle == "Skipped"


@pytest.mark.asyncio
async def test_sub_agent_inner_sleep_interrupted_row_uses_metadata() -> None:
    class ToolApp(App):
        def compose(self) -> ComposeResult:
            yield SubAgentToolCall("parent", "Explore", args={"prompt": "watch the service"})

    async with ToolApp().run_test() as pilot:
        tool = pilot.app.query_one(SubAgentToolCall)

        await tool.add_inner_tool_start("sleep1", "sleep", {"seconds": 30}, tool_kind=KIND_SLEEP)
        tool.complete_inner_tool(
            "sleep1",
            "Sleep interrupted after 0 seconds.",
            0,
            metadata={"sleep_interrupted": True},
        )
        await pilot.pause()

        nested_sleep = await _wait_for_nested_sleep(tool, "sleep1", pilot)
        assert nested_sleep.query_one("#sleep-panel").border_subtitle == "Interrupted"


@pytest.mark.parametrize(
    ("metadata", "subtitle"),
    [
        ({"sleep_skipped": True}, "Skipped"),
        ({"sleep_interrupted": True}, "Interrupted"),
    ],
)
@pytest.mark.asyncio
async def test_sleep_replay_recovers_terminal_status_from_result_metadata(
    metadata: dict[str, bool],
    subtitle: str,
) -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "wait"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "sleep",
                    "call_id": "sleep1",
                    "arguments": {"seconds": 30},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "sleep1",
                    "result": "Sleep resolved with a reworded result.",
                    "additional_properties": {TOOL_RESULT_METADATA_KEY: metadata},
                }
            ],
        },
    ]

    async with PanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({"sleep": KIND_SLEEP})
        await panel.replay_history(messages)
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        assert group._content_mounted is False
        group.collapsed = False
        await pilot.pause()

        tool = panel.query_one(SleepToolCall)
        assert tool.query_one("#sleep-panel").border_subtitle == subtitle


@pytest.mark.asyncio
async def test_sub_agent_replay_renders_interrupted_parent_result_card() -> None:
    class PanelApp(App):
        def compose(self) -> ComposeResult:
            yield ChatPanel()

    messages = [
        {"role": "user", "contents": [{"type": "text", "text": "delegate"}]},
        {
            "role": "assistant",
            "contents": [
                {
                    "type": "function_call",
                    "name": "explore",
                    "call_id": "parent-call",
                    "arguments": {"prompt": "inspect"},
                }
            ],
        },
        {
            "role": "tool",
            "contents": [
                {
                    "type": "function_result",
                    "call_id": "parent-call",
                    "result": "Error: Tool execution was interrupted.",
                    "additional_properties": {
                        TOOL_RESULT_METADATA_KEY: {
                            TOOL_INTERRUPTED_METADATA_KEY: True,
                            "sub_agent_invocation_id": "child-invocation",
                            "sub_agent_log_file": "child.json",
                        }
                    },
                }
            ],
        },
    ]

    async with PanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_tool_kinds({"explore": KIND_SUB_AGENT})
        await panel.replay_history(messages)
        await pilot.pause()

        group = panel.query_one(ToolGroup)
        group.collapsed = False
        await pilot.pause()

        card = panel.query_one(SubAgentToolCall)
        assert card.status == "interrupted"
        assert card.query_one("#sa-panel").border_subtitle == "Interrupted"
