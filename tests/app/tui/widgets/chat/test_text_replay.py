# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Replay text reconstruction and legacy intermediate-sidecar compatibility."""

from __future__ import annotations

import pytest

from chrys.app.tui.widgets.chat.messages import AgentMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.kernel import OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY, AgentResponse, ChatResponse, Content, Message
from chrys.orchestration.engine.run.bindings import TurnBindings
from chrys.service.agent_middleware.events.hosted_tools import ResponsePresentationPlan
from chrys.service.llm.observer import intermediate_text_signal
from tests.support.tui_helpers import ChatPanelApp

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


@pytest.mark.parametrize("legacy_separator", ["", "\n"])
@pytest.mark.parametrize(
    "parts",
    [
        ["回", "退\n把代码核对完毕"],
        ["基线\n", "核对完毕"],
        ["第一段", "\n\n", "第二段"],
    ],
)
async def test_old_fragmented_history_matches_live_and_suppresses_sidecar(
    parts: list[str], legacy_separator: str
) -> None:
    contents: list[Content] = []
    for text in parts:
        contents.extend([Content.from_text_reasoning(text="thinking"), Content.from_text(text)])
    call = Content.from_function_call(call_id="call_1", name="read_file", arguments="{}")
    before_tool = Message(
        "assistant", [*contents, call], additional_properties={"_intermediate_text": legacy_separator.join(parts)}
    )
    saved = before_tool.to_dict()
    result = Message("tool", [Content.from_function_result(call_id="call_1", result="ok")])
    expected = "".join(parts)

    assert intermediate_text_signal(ChatResponse(messages=[before_tool])) == expected
    final_message = Message("assistant", contents)
    assert TurnBindings._extract_final_text(AgentResponse(messages=[final_message])) == expected
    assert ResponsePresentationPlan.from_messages([final_message]).final_text == expected

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history([saved, result.to_dict()])
        await pilot.pause()

        assert [message.text for message in panel.query(AgentMessage)] == [expected]
        assert len(panel.query(ToolGroup)) == 1


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        ([("回", "a"), ("退", "a"), ("Next.", "b")], "回退\nNext."),
        ([("One.", "a"), ("Two.", "b"), ("Three.", "a")], "One.\nTwo.\nThree."),
        ([("One.", None), ("Two.", "a"), ("Three.", None)], "One.\nTwo.\nThree."),
        ([("One.", "a"), ("", "b"), ("Three.", "a")], "One.\nThree."),
        ([("One.", "a"), ("", None), ("Three.", "a")], "One.\nThree."),
    ],
)
async def test_replay_preserves_distinct_output_item_boundaries(
    parts: list[tuple[str, str | None]], expected: str
) -> None:
    contents: list[Content] = []
    for index, (text, block_id) in enumerate(parts):
        properties = {}
        if block_id is not None:
            properties[OPENAI_OUTPUT_MESSAGE_ENVELOPE_KEY] = {
                "id": block_id,
                "status": "in_progress" if index == 0 else "completed",
                "phase": "commentary" if index == 0 else "final_answer",
            }
        contents.append(Content.from_text(text, additional_properties=properties))
    message = Message("assistant", contents)

    # Live retains its existing separator between items in this fix.
    assert TurnBindings._extract_final_text(AgentResponse(messages=[message])) == "".join(text for text, _ in parts)

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history([message.to_dict()])
        await pilot.pause()

        assert [widget.text for widget in panel.query(AgentMessage)] == [expected]


async def test_replay_keeps_tool_calls_between_text_segments() -> None:
    message = Message(
        "assistant",
        [
            Content.from_text("回"),
            Content.from_text_reasoning(text="thinking"),
            Content.from_text("退"),
            Content.from_function_call(call_id="call_1", name="read_file", arguments="{}"),
            Content.from_text("核对"),
            Content.from_text_reasoning(text="thinking"),
            Content.from_text("完毕"),
        ],
    )
    result = Message("tool", [Content.from_function_result(call_id="call_1", result="ok")])

    async with ChatPanelApp().run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.replay_history([message.to_dict(), result.to_dict()])
        await pilot.pause()

        widgets = list(panel.query("AgentMessage, ToolGroup"))
        assert [type(widget) for widget in widgets] == [AgentMessage, ToolGroup, AgentMessage]
        assert [widget.text for widget in widgets if isinstance(widget, AgentMessage)] == ["回退", "核对完毕"]
