# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Side questions use an inert text snapshot and cannot acquire tools or main-session handles."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest

from chrys.kernel import EXCLUDED_KEY, Content, Message
from chrys.service.llm import side_question
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.serializers import serialize_message

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


def test_snapshot_includes_full_tool_text_without_mutating_history() -> None:
    result = "source code " * 200
    messages = [
        Message(role="user", contents=["Explain this code"]),
        Message(
            role="assistant",
            contents=[Content.from_function_call("call", "read_file", arguments={"path": "sample.py"})],
        ),
        Message(role="tool", contents=[Content.from_function_result("call", result=result)]),
        Message(role="assistant", contents=["excluded"], additional_properties={EXCLUDED_KEY: True}),
    ]
    before = [serialize_message(message) for message in messages]
    snapshot = side_question.conversation_snapshot(messages)
    assert result in snapshot
    assert "read_file" in snapshot
    assert "excluded" not in snapshot
    assert [serialize_message(message) for message in messages] == before
    assert "sample.py" in snapshot
    messages[-2].contents[0].result = "new result"
    assert result in snapshot


@pytest.mark.parametrize("stream", [False, True])
async def test_side_call_has_no_tools_or_service_conversation_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    client = MockChatClient(responses=[MockResponse(text="The answer.")])
    closed = False
    captured: list[tuple[str | None, str | None]] = []

    @asynccontextmanager
    async def scoped(
        profile: ModelProfile, *, session_id: str | None, parent_session_id: str | None, session_dir: Path | None
    ) -> AsyncIterator[MockChatClient]:
        nonlocal closed
        captured.append((session_id, parent_session_id))
        try:
            yield client
        finally:
            closed = True
            await client.aclose()

    monkeypatch.setattr(side_question, "scoped_client", scoped)
    profile = ModelProfile(
        id="side-model",
        name="Side model",
        provider="openai",
        api_style="responses",
        model_id="test-model",
        stream=stream,
        chat_options=json.dumps(
            {"tools": [{"type": "web_search"}], "store": True, "extra_body": {"enable_search": True, "store": True}}
        ),
    )
    answer = await side_question.answer_side_question(
        profile,
        session_id="session",
        session_dir=None,
        question="Why?",
        context="Existing result.",
        reminders=("Only explain the current code.",),
    )
    assert answer == "The answer."
    assert closed
    assert captured[0][0] != "session"
    assert captured[0][1] == "session"
    messages, options = client._call_history[-1]
    assert not options.get("tools")
    assert options["store"] is False
    assert options["extra_body"]["store"] is False
    assert "enable_search" not in options["extra_body"]
    assert any("Existing result." in message.text for message in messages)
    assert "<system-reminder>\nOnly explain the current code.\n</system-reminder>" in messages[0].text
    assert messages[0].role == "system"


@pytest.mark.parametrize("key", ["response_format", "text", "text_format", "output_format", "output_config"])
def test_side_options_drop_output_schemas_at_both_levels(key: str) -> None:
    payload = {
        key: {"type": "json_schema"},
        "temperature": 0.25,
        "extra_body": {key: {"type": "json_schema"}, "enable_search": True},
    }
    profile = ModelProfile(id="side", name="Side", provider="openai", model_id="test", chat_options=json.dumps(payload))
    options = side_question._options(profile)
    assert key not in options
    assert key not in options["extra_body"]
    assert options["temperature"] == 0.25
    assert json.loads(profile.chat_options) == payload


def test_snapshot_omits_status_markers_and_names_tool_results() -> None:
    from chrys.foundation.models.history_markers import HistoryMarkerKind

    messages = [
        Message(
            "assistant",
            ["internal interruption marker"],
            additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED},
        ),
        Message(
            "assistant",
            ["internal waiting marker"],
            additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.AWAITING_SUB_AGENTS},
        ),
        Message("assistant", [Content.from_function_call("call", "read_file", arguments={"path": "main.py"})]),
        Message("tool", [Content.from_function_result("call", result="file contents")]),
    ]
    snapshot = side_question.conversation_snapshot(messages)
    assert "internal" not in snapshot
    assert "[Tool result: read_file]\nfile contents" in snapshot


def test_snapshot_pairs_embedded_results_and_reused_ids_in_order() -> None:
    messages = [
        Message(
            "assistant",
            [
                Content.from_function_result("shared", result="file contents"),
                Content.from_function_call("shared", "read_file", arguments={}),
                Content.from_function_call("shared", "shell", arguments={}),
            ],
        ),
        Message("tool", [Content.from_function_result("shared", result="shell output")]),
    ]
    snapshot = side_question.conversation_snapshot(messages)
    assert "[Tool result: read_file]\nfile contents" in snapshot
    assert "[Tool result: shell]\nshell output" in snapshot


def test_snapshot_does_not_pair_results_across_history_markers() -> None:
    from chrys.foundation.models.history_markers import HistoryMarkerKind

    messages = [
        Message("assistant", [Content.from_function_call("shared", "read_file", arguments={})]),
        Message("assistant", [], additional_properties={HistoryMarkerKind.KEY: HistoryMarkerKind.TURN}),
        Message("tool", [Content.from_function_result("shared", result="orphaned output")]),
    ]
    snapshot = side_question.conversation_snapshot(messages)
    assert "[Tool result: tool]\norphaned output" in snapshot
