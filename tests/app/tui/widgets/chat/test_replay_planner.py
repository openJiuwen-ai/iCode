# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for pure chat history replay planning."""

from __future__ import annotations

from typing import Any

from chrys.app.tui.widgets.chat.replay import (
    HistoryReplayPlanner,
    ReplayAgentMessage,
    ReplayContextFold,
    ReplayInterrupted,
    ReplayStructuredCompletion,
    ReplayToolCall,
    ReplayTurnMarker,
    ReplayUserMessage,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_invocation_order import TOOL_INVOCATION_ORDER_KEY
from chrys.foundation.tool_kinds import KIND_MCP
from chrys.foundation.tool_result_metadata import (
    TOOL_FAILED_METADATA_KEY,
    TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY,
    TOOL_INTERRUPTED_METADATA_KEY,
)
from chrys.foundation.trajectory_timing import TRAJECTORY_TIMING_KEY
from chrys.kernel import ChatResponse, Content, Message
from chrys.service.context.providers.history import TURN_STARTED_AT_KEY
from chrys.service.session.message_metadata import (
    MESSAGE_CREATED_AT_KEY,
    TOOL_CALL_KIND_METADATA_KEY,
    TOOL_RESULT_METADATA_KEY,
)
from tests.support.transcript_invariants import assert_transcript_invariants


def _fc(call_id: str, name: str = "zsh") -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _legacy(call_id: str, name: str = "legacy_tool") -> dict[str, Any]:
    return {"type": "legacy", "call_id": call_id, "name": name, "arguments": {"legacy": True}}


def _fr(
    call_id: str,
    result: str = "ok",
    *,
    metadata: dict[str, Any] | None = None,
    image: bool = False,
) -> dict[str, Any]:
    content: dict[str, Any] = {"type": "function_result", "call_id": call_id, "result": result}
    if metadata is not None:
        content["additional_properties"] = {TOOL_RESULT_METADATA_KEY: metadata}
    if image:
        content["items"] = [{"type": "image", "media_type": "image/png", "data": "abc"}]
    return content


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _timing(
    duration_ms: int,
    *,
    started_at: str = "2026-08-19T01:02:03+00:00",
    finished_at: str = "2026-08-19T01:02:04+00:00",
) -> dict[str, Any]:
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
    }


def _assistant(contents: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "contents": contents}
    if extra:
        message["additional_properties"] = extra
    return message


def _tool(contents: list[dict[str, Any]]) -> dict[str, Any]:
    return {"role": "tool", "contents": contents}


def _planned_tools(
    messages: list[dict[str, Any]],
    *,
    file_snapshots: dict[str, list[Any]] | None = None,
) -> list[ReplayToolCall]:
    plan = HistoryReplayPlanner().build_plan(messages, file_snapshots=file_snapshots)
    return [
        content
        for entry in plan.entries
        if isinstance(entry, ReplayAgentMessage)
        for content in entry.contents
        if isinstance(content, ReplayToolCall)
    ]


def _planned_agent_messages(messages: list[dict[str, Any]]) -> list[ReplayAgentMessage]:
    plan = HistoryReplayPlanner().build_plan(messages)
    return [entry for entry in plan.entries if isinstance(entry, ReplayAgentMessage)]


def _planned_interruptions(messages: list[dict[str, Any]]) -> list[ReplayInterrupted]:
    plan = HistoryReplayPlanner().build_plan(messages)
    return [entry for entry in plan.entries if isinstance(entry, ReplayInterrupted)]


def _hosted_call(call_id: str, *, status: str, phase: str) -> dict[str, Any]:
    return Content.from_hosted_tool_call(
        call_id,
        tool_name="web_search",
        status=status,
        hosted_family="search",
        hosted_provider="openai",
        provider_phase=phase,
    ).to_dict()


def _hosted_result(call_id: str, text: str = "found") -> dict[str, Any]:
    return Content.from_hosted_tool_result(
        call_id,
        tool_name="web_search",
        items=[Content.from_text(text)],
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    ).to_dict()


def test_replay_history_preserves_legacy_input_annotations() -> None:
    snapshot = ("before", "after")
    metadata = {"status": "terminal"}
    approval = {"tool_name": "edit_file", "status": "user_approved", "request_id": "req-1"}
    function_call = _fc("edit-1", "edit_file")
    messages = [
        _assistant([function_call], _approval=approval),
        _tool([_fr("edit-1", "edited", metadata=metadata, image=True)]),
    ]

    plan = HistoryReplayPlanner().build_plan(messages, file_snapshots={"edit-1": [snapshot]})
    plan.apply_compatibility_annotations()

    assert function_call["_replay_result"] == "edited"
    assert function_call["_replay_metadata"] == metadata
    assert function_call["_replay_image_contents"] == [{"type": "image", "media_type": "image/png", "data": "abc"}]
    assert function_call["_file_snapshot"] == snapshot
    assert function_call["_replay_approval"] == approval


def test_planner_restores_tool_durations_not_response_durations() -> None:
    call = _fc("c1")
    call["additional_properties"] = {TRAJECTORY_TIMING_KEY: _timing(125)}
    result = _fr("c1")
    result["additional_properties"] = {
        TRAJECTORY_TIMING_KEY: _timing(
            987,
            started_at="2026-08-19T01:02:05+00:00",
            finished_at="2026-08-19T01:02:06+00:00",
        )
    }
    messages = [
        {
            "role": "user",
            "contents": [_text("hello")],
            "additional_properties": {
                MESSAGE_CREATED_AT_KEY: "not-a-time",
                TRAJECTORY_TIMING_KEY: _timing(0),
            },
        },
        _assistant([call], **{TRAJECTORY_TIMING_KEY: _timing(321)}),
        _tool([result]),
        _assistant([_text("done")], **{TRAJECTORY_TIMING_KEY: _timing(654)}),
    ]

    tools = _planned_tools(messages)
    agent_messages = _planned_agent_messages(messages)
    user_message = next(
        entry for entry in HistoryReplayPlanner().build_plan(messages).entries if isinstance(entry, ReplayUserMessage)
    )

    assert user_message.duration_ms == 0
    assert user_message.created_at == "2026-08-19T01:02:04.000000+00:00"
    assert [tool.duration_ms for tool in tools] == [987]
    assert [tool.started_at for tool in tools] == ["2026-08-19T01:02:05.000000+00:00"]
    # An agent message tells no time of its own response: the answer tells its turn's, once a marker ends it.
    assert [message.duration_ms for message in agent_messages] == [None, None]
    assert {message.created_at for message in agent_messages} == {"2026-08-19T01:02:04.000000+00:00"}


def test_coalesced_structured_completion_finished_with_the_final_tool_cycle() -> None:
    """A turn-ending hosted completion belongs to the final tool cycle: it finished then, and tells its turn's time."""
    hosted_call = Content.from_hosted_tool_call(
        "hosted-1",
        tool_name="server_task",
        status="running",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="start",
    ).to_dict()
    hosted_result = Content.from_hosted_tool_result(
        "hosted-1",
        tool_name="server_task",
        result="done",
        status="completed",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="terminal",
    ).to_dict()
    first_timing = _timing(111)
    latest_timing = _timing(
        999,
        started_at="2026-08-19T01:02:05+00:00",
        finished_at="2026-08-19T01:02:06+00:00",
    )
    messages = [
        _assistant([_fc("local-1")], **{TRAJECTORY_TIMING_KEY: first_timing}),
        _tool([_fr("local-1", "done")]),
        _assistant([hosted_call, hosted_result], **{TRAJECTORY_TIMING_KEY: latest_timing}),
        _assistant(
            [_text("")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.TURN, TURN_STARTED_AT_KEY: "2026-08-19T01:01:00+00:00"},
        ),
    ]

    entries = HistoryReplayPlanner().build_plan(messages).entries

    assert [type(entry) for entry in entries] == [ReplayAgentMessage, ReplayStructuredCompletion, ReplayTurnMarker]
    assert entries[1] == ReplayStructuredCompletion("2026-08-19T01:02:06.000000+00:00", 66_000)


def test_planner_treats_missing_legacy_timing_as_unknown() -> None:
    messages = [
        {"role": "user", "contents": [_text("hello")]},
        _assistant([_fc("c1")]),
        _tool([_fr("c1")]),
        _assistant([_text("done")]),
    ]
    plan = HistoryReplayPlanner().build_plan(messages)

    assert next(entry for entry in plan.entries if isinstance(entry, ReplayUserMessage)).duration_ms is None
    assert (
        next(
            content
            for entry in plan.entries
            if isinstance(entry, ReplayAgentMessage)
            for content in entry.contents
            if isinstance(content, ReplayToolCall)
        ).duration_ms
        is None
    )
    assert all(tool.started_at is None for tool in _planned_tools(messages))
    assert all(entry.duration_ms is None for entry in plan.entries if isinstance(entry, ReplayAgentMessage))


def test_hosted_replay_plan_can_decode_compatibility_annotated_call_again() -> None:
    call = Content.from_hosted_tool_call(
        "provider_1",
        tool_name="server_task",
        status="running",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="start",
    ).to_dict()
    result = Content.from_hosted_tool_result(
        "provider_1",
        tool_name="server_task",
        result="done",
        status="completed",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="terminal",
    ).to_dict()
    messages = [_assistant([call, result])]
    first = HistoryReplayPlanner().build_plan(messages)
    first.apply_compatibility_annotations()

    tools = _planned_tools(messages)

    assert len(tools) == 1
    assert tools[0].result == "done"
    assert tools[0].canonical_status == "completed"


def test_hosted_replay_plan_keeps_provider_payload_for_lazy_card() -> None:
    artifact = Content.from_hosted_file(
        "file_1",
        media_type="text/plain",
        name="report.txt",
        additional_properties={"size": 42},
    )
    call = Content.from_hosted_tool_call(
        "provider_1",
        tool_name="server_task",
        arguments={"query": "Chrys"},
        status="running",
        hosted_family="generic",
        hosted_provider="openai",
        provider_item_type="server_task_call",
        provider_phase="start",
    )
    result = Content.from_hosted_tool_result(
        "provider_1",
        tool_name="server_task",
        items=[Content.from_text("done"), artifact],
        status="completed",
        hosted_family="generic",
        hosted_provider="openai",
        provider_item_type="server_task_result",
        provider_phase="terminal",
    )

    tools = _planned_tools([_assistant([call.to_dict(), result.to_dict()])])

    assert len(tools) == 1
    assert tools[0].provider_hosted is True
    assert tools[0].hosted_family == "generic"
    assert tools[0].provider == "openai"
    assert tools[0].provider_item_type == "server_task_call"
    assert tools[0].provider_call_id == "provider_1"
    assert tools[0].canonical_status == "completed"
    assert tools[0].arguments == {"query": "Chrys"}
    assert tools[0].result == "done"
    assert tools[0].artifacts == [{"id": "file_1", "path": "report.txt", "mime": "text/plain", "size": 42}]


def test_hosted_replay_rederives_synthesized_failure_signal_from_raw_contents() -> None:
    # Persisted sessions store only the raw provider contents; the marker must
    # come back from re-adaptation, not from persisted metadata.
    call = Content.from_hosted_tool_call(
        "provider_failure",
        tool_name="server_task",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="start",
    )
    result = Content.from_hosted_tool_result(
        "provider_failure",
        tool_name="server_task",
        result="",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="terminal",
        additional_properties={"is_error": True},
    )

    tools = _planned_tools([_assistant([call.to_dict(), result.to_dict()])])

    assert len(tools) == 1
    assert tools[0].result == "Error: Provider-hosted tool failed."
    assert tools[0].metadata is not None
    assert tools[0].metadata[TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY] is True


def test_hosted_replay_keeps_status_derived_failure_text_unmarked() -> None:
    # A paired call status supplies the failure text on replay, so the result
    # is provider-derived — no synthesized marker, literal rendering.
    call = Content.from_hosted_tool_call(
        "provider_failure",
        tool_name="server_task",
        status="running",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="start",
    )
    result = Content.from_hosted_tool_result(
        "provider_failure",
        tool_name="server_task",
        result="",
        hosted_family="generic",
        hosted_provider="openai",
        provider_phase="terminal",
        additional_properties={"is_error": True},
    )

    tools = _planned_tools([_assistant([call.to_dict(), result.to_dict()])])

    assert len(tools) == 1
    assert tools[0].result == "Error: running"
    assert tools[0].metadata is None or TOOL_FAILURE_TEXT_SYNTHESIZED_METADATA_KEY not in tools[0].metadata


def test_hosted_replay_plan_synthesizes_calls_for_standalone_results() -> None:
    assistant_result = _hosted_result("assistant-only", "assistant result")
    tool_result = _hosted_result("tool-only", "tool result")

    tools = _planned_tools([_assistant([assistant_result]), _tool([tool_result])])

    assert [
        (
            tool.call_id,
            tool.name,
            tool.result,
            tool.provider_hosted,
            tool.hosted_family,
            tool.canonical_status,
        )
        for tool in tools
    ] == [
        ("assistant-only", "web_search", "assistant result", True, "search", "completed"),
        ("tool-only", "web_search", "tool result", True, "search", "completed"),
    ]


def test_hosted_replay_terminalizes_unanswered_calls_from_canonical_status() -> None:
    running = Content.from_hosted_tool_call(
        "provider-running",
        tool_name="web_search",
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="snapshot",
    )
    completed = Content.from_hosted_tool_call(
        "provider-completed",
        tool_name="web_search",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    failed = Content.from_hosted_tool_call(
        "provider-failed",
        tool_name="web_search",
        status="failed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )

    tools = _planned_tools([_assistant([running.to_dict(), completed.to_dict(), failed.to_dict()])])

    assert [tool.canonical_status for tool in tools] == ["interrupted", "completed", "failed"]


def test_hosted_trailing_text_replays_as_final_response() -> None:
    messages = [
        _assistant(
            [
                _hosted_call("ws-1", status="running", phase="start"),
                _hosted_result("ws-1"),
                _text("searching more"),
                _hosted_call("ws-2", status="running", phase="start"),
                _hosted_result("ws-2"),
                _text("final answer"),
            ]
        )
    ]

    entries = _planned_agent_messages(messages)

    assert [entry.is_intermediate for entry in entries] == [False, True, False, False]
    assert _text("searching more") in entries[1].contents
    assert _text("final answer") in entries[3].contents


def test_hosted_trailing_text_after_non_terminal_call_stays_intermediate() -> None:
    messages = [_assistant([_hosted_call("ws-1", status="running", phase="snapshot"), _text("partial notes")])]

    entries = _planned_agent_messages(messages)

    assert [entry.is_intermediate for entry in entries] == [False, True]


def test_trailing_text_after_actionable_call_stays_intermediate_despite_hosted_work() -> None:
    messages = [
        _assistant(
            [
                _hosted_call("ws-1", status="completed", phase="terminal"),
                _fc("local-1"),
                _text("pre-execution notes"),
            ]
        ),
        _tool([_fr("local-1", "ran")]),
    ]

    entries = _planned_agent_messages(messages)

    assert [entry.is_intermediate for entry in entries] == [False, True]


def test_legacy_informational_call_requires_explicit_server_execution_evidence_for_hosted_view() -> None:
    local_compat = _fc("local-info", "tool_search")
    local_compat["informational_only"] = True
    hosted_compat = _fc("hosted-info", "tool_search")
    hosted_compat["informational_only"] = True
    hosted_compat["additional_properties"] = {
        "item_type": "tool_search_call",
        "execution": "server",
        "status": "completed",
    }

    tools = _planned_tools([_assistant([local_compat, hosted_compat])])

    assert [tool.provider_hosted for tool in tools] == [False, True]
    assert tools[1].hosted_family == "tool_discovery"
    assert tools[1].canonical_status == "completed"


def test_planner_pairs_duplicate_ids_inside_immediate_block() -> None:
    messages = [
        _assistant([_fc("dup"), _fc("dup")]),
        _tool([_fr("dup", "first"), _fr("dup", "second")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == ["first", "second"]


def test_planner_reused_call_id_does_not_borrow_from_previous_block() -> None:
    messages = [
        _assistant([_fc("reuse")]),
        _tool([_fr("reuse", "first"), _fr("reuse", "extra old")]),
        _assistant([_text("boundary")]),
        _assistant([_fc("reuse")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == ["first", ""]


def test_planner_pairs_out_of_order_results_inside_immediate_block() -> None:
    messages = [
        _assistant([_fc("a1"), _fc("a2"), _fc("a3")]),
        _tool([_fr("a3", "res_3"), _fr("a1", "res_1"), _fr("a2", "res_2")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == ["res_1", "res_2", "res_3"]


def test_planner_pairs_results_behind_consecutive_assistant_block() -> None:
    """A multi-message response shares ONE result block after the whole
    assistant run; the first message's calls pair against it too."""
    messages = [
        _assistant([_fc("c1")]),
        _assistant([_fc("c2")]),
        _tool([_fr("c1", "first result"), _fr("c2", "second result")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == ["first result", "second result"]


def test_planner_shares_duplicate_id_cursor_across_consecutive_assistant_block() -> None:
    messages = [
        _assistant([_fc("dup")]),
        _assistant([_fc("dup")]),
        _tool([_fr("dup", "only result")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == ["only result", ""]


def test_planner_result_only_assistant_ends_exchange_before_reused_call_id() -> None:
    hosted_call = Content.from_function_call("dup", "web_search", arguments={}, informational_only=True)
    hosted_result = Content.from_function_result("dup", result="hosted result")
    local_call = Content.from_function_call("dup", "local_mcp", arguments={})
    local_call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
    local_result = Content.from_function_result("dup", result="local result")
    transcript = ChatResponse(
        messages=[
            Message("assistant", [hosted_call]),
            Message("assistant", [hosted_result]),
            Message("assistant", [local_call]),
            Message("tool", [local_result]),
        ]
    )
    assert_transcript_invariants(transcript)

    tools = _planned_tools([message.to_dict() for message in transcript.messages])

    assert [(tool.name, tool.result) for tool in tools] == [
        ("web_search", "hosted result"),
        ("local_mcp", "local result"),
    ]


def test_planner_collects_mixed_assistant_and_tool_result_block() -> None:
    first_call = Content.from_function_call("c1", "first", arguments={})
    first_call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
    second_call = Content.from_function_call("c2", "second", arguments={})
    second_call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 1
    transcript = ChatResponse(
        messages=[
            Message("assistant", [first_call, second_call]),
            Message("assistant", [Content.from_function_result("c1", result="first result")]),
            Message("tool", [Content.from_function_result("c2", result="second result")]),
        ]
    )
    assert_transcript_invariants(transcript)

    tools = _planned_tools([message.to_dict() for message in transcript.messages])

    assert [(tool.name, tool.result) for tool in tools] == [
        ("first", "first result"),
        ("second", "second result"),
    ]


def test_planner_empty_assistant_remains_response_sibling() -> None:
    call = Content.from_function_call("c1", "first", arguments={})
    call.additional_properties[TOOL_INVOCATION_ORDER_KEY] = 0
    transcript = ChatResponse(
        messages=[
            Message("assistant", [call]),
            Message("assistant", []),
            Message("tool", [Content.from_function_result("c1", result="done")]),
        ]
    )
    assert_transcript_invariants(transcript)

    tools = _planned_tools([message.to_dict() for message in transcript.messages])

    assert [(tool.name, tool.result) for tool in tools] == [("first", "done")]


def test_planner_does_not_pair_result_across_history_marker() -> None:
    messages = [
        _assistant([_fc("c1")]),
        _assistant(
            [_text("Execution interrupted")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED},
        ),
        _tool([_fr("c1", "orphan", metadata={"wrong": True}, image=True)]),
    ]

    tools = _planned_tools(messages)

    assert [(tool.result, tool.metadata, tool.image_contents) for tool in tools] == [("", None, None)]


def test_planner_treats_falsy_kind_marker_as_chrome() -> None:
    # KEY presence is what makes a message engine chrome — the value is the
    # marker kind. A falsy kind must not surface the marker's contents as
    # tool activity while the grammar treats the message as a boundary.
    messages = [_assistant([_fc("m1")], **{HistoryMarkerKind.KEY: ""})]

    assert _planned_tools(messages) == []
    assert _planned_agent_messages(messages) == []


def test_planner_renders_marker_fall_through_text_only() -> None:
    # An unrecognized marker kind may still show its text, but the marker's
    # other contents never ride along as raw tool payloads.
    messages = [
        _assistant([_text("chrome note"), _fc("m1")], **{HistoryMarkerKind.KEY: ""}),
    ]

    agent_messages = _planned_agent_messages(messages)

    assert [[c.get("type") for c in entry.contents] for entry in agent_messages] == [["text"]]


def test_planner_preserves_awaiting_sub_agents_marker_text() -> None:
    # A session saved mid-await legitimately replays this marker; its status
    # text stays visible while nothing tool-shaped leaks from it.
    messages = [
        _assistant(
            [_text("Waiting for 2 sub-agents")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.AWAITING_SUB_AGENTS},
        ),
    ]

    agent_messages = _planned_agent_messages(messages)

    assert _planned_tools(messages) == []
    assert [[c.get("text") for c in entry.contents] for entry in agent_messages] == [["Waiting for 2 sub-agents"]]


def test_planner_carries_only_recognized_status_codes() -> None:
    messages = [
        _assistant(
            [_text("recognized literal")],
            **{
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                HistoryMarkerKind.STATUS_CODE_KEY: HistoryMarkerKind.STATUS_EXECUTION_INTERRUPTED,
            },
        ),
        _assistant(
            [_text("future literal")],
            **{
                HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED,
                HistoryMarkerKind.STATUS_CODE_KEY: "future_status",
            },
        ),
        _assistant(
            [_text("old literal")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED},
        ),
    ]

    interruptions = _planned_interruptions(messages)

    assert [entry.reason for entry in interruptions] == ["recognized literal", "future literal", "old literal"]
    assert [entry.status_code for entry in interruptions] == [
        HistoryMarkerKind.STATUS_EXECUTION_INTERRUPTED,
        None,
        None,
    ]


def test_planner_awaiting_status_count_comes_from_structured_ids() -> None:
    messages = [
        _assistant(
            [_text("Awaiting 99 sub-agent(s)")],
            **{
                HistoryMarkerKind.KEY: HistoryMarkerKind.AWAITING_SUB_AGENTS,
                HistoryMarkerKind.STATUS_CODE_KEY: HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS,
                "_invocation_ids": ["first", "second", "third"],
            },
        )
    ]

    agent_messages = _planned_agent_messages(messages)

    assert len(agent_messages) == 1
    assert agent_messages[0].status_code == HistoryMarkerKind.STATUS_AWAITING_SUB_AGENTS
    assert agent_messages[0].status_count == 3
    assert agent_messages[0].contents == [_text("Awaiting 99 sub-agent(s)")]


def test_marker_carried_file_call_does_not_consume_snapshot_cursor() -> None:
    # A marker-carried file call never renders as a tool, so the tracker
    # snapshot it would consume belongs to the next visible real call.
    snapshot = ("before", "after")
    marker_call = _fc("same", "edit_file")
    messages = [
        _assistant([marker_call], **{HistoryMarkerKind.KEY: HistoryMarkerKind.INTERRUPTED}),
        _assistant([_fc("same", "edit_file")]),
    ]

    plan = HistoryReplayPlanner().build_plan(messages, file_snapshots={"same": [snapshot]})
    plan.apply_compatibility_annotations()
    tools = [
        content
        for entry in plan.entries
        if isinstance(entry, ReplayAgentMessage)
        for content in entry.contents
        if isinstance(content, ReplayToolCall)
    ]

    assert [tool.file_snapshot for tool in tools] == [snapshot]
    assert "_file_snapshot" not in marker_call


def test_planner_missing_result_has_no_metadata_or_images() -> None:
    messages = [
        _assistant([_fc("c1"), _fc("c2")]),
        _tool([_fr("c1", "got c1", metadata={"status": "ok"}, image=True)]),
    ]

    tools = _planned_tools(messages)

    assert [(tool.result, tool.metadata, tool.image_contents) for tool in tools] == [
        ("got c1", {"status": "ok"}, [{"type": "image", "media_type": "image/png", "data": "abc"}]),
        ("", None, None),
    ]


def test_planner_preserves_informational_call_without_synthetic_local_result() -> None:
    informational = _fc("hosted-1", "web_search")
    informational["informational_only"] = True

    tools = _planned_tools([_assistant([informational])])

    assert len(tools) == 1
    assert tools[0].source["informational_only"] is True
    assert tools[0].result == ""


def test_planner_preserves_persisted_tool_kind() -> None:
    function_call = _fc("remote-1", "remote_tool")
    function_call["additional_properties"] = {TOOL_CALL_KIND_METADATA_KEY: KIND_MCP}
    messages = [
        _assistant([function_call]),
        _tool([_fr("remote-1", "ok")]),
    ]

    tools = _planned_tools(messages)

    assert tools[0].tool_kind == KIND_MCP


def test_planner_merges_nested_and_top_level_result_metadata() -> None:
    result = _fr("c1", "ok", metadata={TOOL_FAILED_METADATA_KEY: False, "nested": "kept"})
    result["additional_properties"][TOOL_FAILED_METADATA_KEY] = True
    result["additional_properties"]["private_runtime_key"] = "drop"
    messages = [
        _assistant([_fc("c1")]),
        _tool([result]),
    ]

    tools = _planned_tools(messages)

    assert tools[0].metadata == {TOOL_FAILED_METADATA_KEY: True, "nested": "kept"}


def test_planner_restores_interrupted_non_hosted_tool_status() -> None:
    messages = [
        _assistant([_fc("interrupted")]),
        _tool([_fr("interrupted", "(interrupted)", metadata={TOOL_INTERRUPTED_METADATA_KEY: True})]),
    ]

    tools = _planned_tools(messages)

    assert len(tools) == 1
    assert tools[0].result == "(interrupted)"
    assert tools[0].canonical_status == "interrupted"


def test_planner_preserves_user_rejection_metadata_without_replay_approval() -> None:
    result = _fr("c1", "Error: rejected", metadata={TOOL_FAILED_METADATA_KEY: True, "approval": "user_rejected"})
    messages = [
        _assistant([_fc("c1")]),
        _tool([result]),
    ]

    tools = _planned_tools(messages)

    assert tools[0].approval is None
    assert tools[0].metadata == {TOOL_FAILED_METADATA_KEY: True, "approval": "user_rejected"}


def test_planner_pairs_legacy_tool_call_result() -> None:
    messages = [
        _assistant([_legacy("legacy-1")]),
        _tool([_fr("legacy-1", "legacy result", metadata={"status": "legacy"})]),
    ]

    tools = _planned_tools(messages)

    assert [(tool.name, tool.call_id, tool.arguments, tool.result, tool.metadata) for tool in tools] == [
        ("legacy_tool", "legacy-1", {"legacy": True}, "legacy result", {"status": "legacy"})
    ]


def test_planner_preserves_text_after_tool_calls_in_assistant_message() -> None:
    messages = [
        _assistant([_text("before"), _fc("call-1"), _text("after"), _fc("call-2"), _text("done")]),
        _tool([_fr("call-1", "first"), _fr("call-2", "second")]),
    ]

    entries = _planned_agent_messages(messages)

    assert [entry.is_intermediate for entry in entries] == [True, False, True, False, True]
    contents = [content for entry in entries for content in entry.contents]
    assert contents[0] == _text("before")
    assert isinstance(contents[1], ReplayToolCall)
    assert (contents[1].call_id, contents[1].result) == ("call-1", "first")
    assert contents[2] == _text("after")
    assert isinstance(contents[3], ReplayToolCall)
    assert (contents[3].call_id, contents[3].result) == ("call-2", "second")
    assert contents[4] == _text("done")


def test_planner_keeps_embedded_intermediate_text_when_later_text_is_not_duplicate() -> None:
    messages = [
        _assistant([_fc("call-1"), _text("final text")], _intermediate_text="thinking first"),
        _tool([_fr("call-1", "result")]),
    ]

    entries = _planned_agent_messages(messages)

    assert len(entries) == 2
    assert isinstance(entries[0].contents[0], ReplayToolCall)
    assert entries[0].intermediate_texts == ["thinking first"]
    assert entries[1].contents == [_text("final text")]


def test_planner_suppresses_embedded_intermediate_text_when_visible_text_matches() -> None:
    messages = [
        _assistant([_text("thinking first"), _fc("call-1")], _intermediate_text="thinking first"),
        _tool([_fr("call-1", "result")]),
    ]

    entries = _planned_agent_messages(messages)

    assert len(entries) == 2
    assert entries[0].contents == [_text("thinking first")]
    assert isinstance(entries[1].contents[0], ReplayToolCall)
    assert entries[1].intermediate_texts == []


def test_planner_suppresses_embedded_intermediate_text_matching_concatenated_visible_text() -> None:
    messages = [
        _assistant([_text("A"), _text("B"), _fc("call-1")], _intermediate_text="AB"),
        _tool([_fr("call-1", "result")]),
    ]

    entries = _planned_agent_messages(messages)

    assert len(entries) == 2
    assert entries[0].contents == [_text("A"), _text("B")]
    assert isinstance(entries[1].contents[0], ReplayToolCall)
    assert entries[1].intermediate_texts == []


def test_planner_suppresses_legacy_sidecar_matching_previous_tool_batch_text() -> None:
    messages = [
        _assistant([_text("thinking first"), _fc("call-1")], _batch_id=11),
        _tool([_fr("call-1", "first")]),
        _assistant([_fc("call-2")], _batch_id=12, _intermediate_text="thinking first"),
        _tool([_fr("call-2", "second")]),
    ]

    entries = _planned_agent_messages(messages)

    visible_texts = [
        content["text"]
        for entry in entries
        for content in entry.contents
        if isinstance(content, dict) and content.get("type") == "text"
    ]
    assert visible_texts == ["thinking first"]
    assert [text for entry in entries for text in entry.intermediate_texts] == []


def test_planner_suppresses_legacy_sidecar_matching_next_tool_batch_text() -> None:
    messages = [
        _assistant([_fc("call-1")], _batch_id=3, _intermediate_text="thinking next"),
        _tool([_fr("call-1", "first")]),
        _assistant([_text("thinking next"), _fc("call-2")], _batch_id=4),
        _tool([_fr("call-2", "second")]),
    ]

    entries = _planned_agent_messages(messages)

    visible_texts = [
        content["text"]
        for entry in entries
        for content in entry.contents
        if isinstance(content, dict) and content.get("type") == "text"
    ]
    assert visible_texts == ["thinking next"]
    assert [text for entry in entries for text in entry.intermediate_texts] == []


def test_planner_keeps_matching_sidecar_across_user_boundary() -> None:
    messages = [
        _assistant([_text("repeat intentionally"), _fc("call-1")], _batch_id=1),
        _tool([_fr("call-1", "first")]),
        {"role": "user", "contents": [_text("continue")]},
        _assistant([_fc("call-2")], _batch_id=2, _intermediate_text="repeat intentionally"),
        _tool([_fr("call-2", "second")]),
    ]

    entries = _planned_agent_messages(messages)

    assert [text for entry in entries for text in entry.intermediate_texts] == ["repeat intentionally"]


def test_planner_ignores_result_after_non_assistant_boundary() -> None:
    """A result past a user boundary never pairs backwards.

    A text-only assistant between a call and its results is NOT such a
    boundary: the kernel appends a response's single result message after
    ALL of the response's assistant messages, text-only ones included —
    that layout is pinned by
    ``test_planner_pairs_results_behind_consecutive_assistant_block``.
    """
    messages = [
        _assistant([_fc("late")]),
        {"role": "user", "contents": [_text("boundary")]},
        _tool([_fr("late", "must not pair")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == [""]
    assert tools[0].tool_kind == ""


def test_planner_provider_limited_orphan_result_does_not_pair_forward() -> None:
    messages = [
        _tool([_fr("limited", "stale")]),
        _assistant([_fc("limited")]),
    ]

    tools = _planned_tools(messages)

    assert [tool.result for tool in tools] == [""]


def test_planner_file_snapshots_are_independent_of_result_pairing() -> None:
    snapshot = ("before", "after")
    messages = [
        _assistant([_fc("shared", "read_file"), _fc("shared", "edit_file")]),
        _tool([_fr("shared", "read result")]),
    ]

    tools = _planned_tools(messages, file_snapshots={"shared": [snapshot]})

    assert [(tool.name, tool.result, tool.file_snapshot) for tool in tools] == [
        ("read_file", "read result", None),
        ("edit_file", "", snapshot),
    ]


def test_planner_context_fold_extracts_summary_text() -> None:
    messages = [
        _assistant(
            [_text("[Compressed context: old]\nSummary: condensed")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-1"},
        )
    ]

    plan = HistoryReplayPlanner().build_plan(messages)

    assert plan.entries == [ReplayContextFold(context_id="ctx-1", summary="condensed")]


def test_planner_shows_a_card_per_fold_and_none_for_the_archive_index() -> None:
    """The index only lists folds; each fold, archived or not, keeps the card of its own summary."""
    messages = [
        _assistant(
            [_text("[Archive index]\nOlder compressed blocks, oldest first.\nctx-1 · turns 1-1 · first question")],
            **{
                HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
                HistoryMarkerKind.ARCHIVE_INDEX_KEY: [
                    {"first_id": "ctx-1", "last_id": "ctx-1", "turn_from": 1, "turn_to": 1, "blocks": 1, "label": ""}
                ],
            },
        ),
        _assistant(
            [_text("[Compressed context: ctx-1]\nSummary: archived fold")],
            **{
                HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
                "_block_id": "ctx-1",
                "_excluded": True,
                "_exclude_reason": "archive_index",
            },
        ),
        _assistant(
            [_text("[Compressed context: ctx-2]\nSummary: newest fold")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-2"},
        ),
    ]

    plan = HistoryReplayPlanner().build_plan(messages)

    assert plan.entries == [
        ReplayContextFold(context_id="ctx-1", summary="archived fold"),
        ReplayContextFold(context_id="ctx-2", summary="newest fold"),
    ]


def test_planner_context_fold_fallback_keeps_last_text() -> None:
    messages = [
        _assistant(
            [_text("first"), _text("second")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-2"},
        )
    ]

    plan = HistoryReplayPlanner().build_plan(messages)

    assert plan.entries == [ReplayContextFold(context_id="ctx-2", summary="second")]


def _user_msg(text: str, **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "contents": [_text(text)]}
    if extra:
        message["additional_properties"] = extra
    return message


def _planned_user_messages(messages: list[dict[str, Any]]) -> list[ReplayUserMessage]:
    plan = HistoryReplayPlanner().build_plan(messages)
    return [entry for entry in plan.entries if isinstance(entry, ReplayUserMessage)]


def test_planner_skips_flagged_continuation_nudge() -> None:
    """A flagged synthetic ``continue`` nudge produces no replay entry."""
    messages = [
        _user_msg("start task"),
        _assistant([_text("working")]),
        _user_msg("continue", **{HistoryMarkerKind.CONTINUATION_KEY: True}),
    ]

    users = _planned_user_messages(messages)

    assert [entry.text for entry in users] == ["start task"]


def test_planner_flagged_nudge_does_not_flip_seen_user_in_turn() -> None:
    """If the skipped nudge flipped the positional flag, the following real
    opener would render as an injection."""
    messages = [
        _user_msg("continue", **{HistoryMarkerKind.CONTINUATION_KEY: True}),
        _user_msg("hello"),
    ]

    users = _planned_user_messages(messages)

    assert [(entry.text, entry.is_injection) for entry in users] == [("hello", False)]


def test_planner_injection_after_skipped_nudge_renders_per_positional_rules() -> None:
    messages = [
        _user_msg("start task"),
        _user_msg("continue", **{HistoryMarkerKind.CONTINUATION_KEY: True}),
        _user_msg("mid-turn note", **{HistoryMarkerKind.INJECTED_KEY: True}),
    ]

    users = _planned_user_messages(messages)

    assert [(entry.text, entry.is_injection) for entry in users] == [
        ("start task", False),
        ("mid-turn note", True),
    ]


def test_planner_legacy_unflagged_continue_renders_as_today() -> None:
    """Legacy pre-flag histories keep the old rendering at both positions."""
    # Mid-turn legacy nudge: the positional fallback styles it as an injection.
    messages = [
        _user_msg("start task"),
        _assistant([_text("working")]),
        _user_msg("continue"),
    ]
    users = _planned_user_messages(messages)
    assert [(entry.text, entry.is_injection) for entry in users] == [
        ("start task", False),
        ("continue", True),
    ]

    # Turn-opening legacy nudge: renders as a plain user message.
    users = _planned_user_messages([_user_msg("continue")])
    assert [(entry.text, entry.is_injection) for entry in users] == [("continue", False)]


def test_planner_flagged_injection_renders_when_first_in_turn() -> None:
    messages = [_user_msg("guidance", **{HistoryMarkerKind.INJECTED_KEY: True})]

    users = _planned_user_messages(messages)

    assert [(entry.text, entry.is_injection) for entry in users] == [("guidance", True)]


# --- exchange-shape pins ------------------------------------------------


def _raw_fc(call_id: Any, name: str = "zsh") -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _raw_fr(call_id: Any, result: str = "ok") -> dict[str, Any]:
    return {"type": "function_result", "call_id": call_id, "result": result}


def test_planner_user_role_result_message_not_part_of_block() -> None:
    """A user-role message carrying only results never answers a call."""
    messages = [
        _assistant([_fc("c1")]),
        {"role": "user", "contents": [_raw_fr("c1", "from-user")]},
    ]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("c1", "")]


def test_planner_pairs_numeric_call_with_digit_string_result() -> None:
    """Malformed ids pair by string form: int 7 matches result id "7"."""
    messages = [_assistant([_raw_fc(7)]), _tool([_fr("7", "seven")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("7", "seven")]


def test_planner_pairs_separately_constructed_nan_ids() -> None:
    """Two distinct NaN id objects pair through their identical string forms."""
    messages = [_assistant([_raw_fc(float("nan"))]), _tool([_raw_fr(float("nan"), "nan-result")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("nan", "nan-result")]


def test_planner_pairs_falsy_numeric_call_with_empty_id_result() -> None:
    """Falsy non-string call ids coerce into the empty-id stream."""
    messages = [_assistant([_raw_fc(0)]), _tool([_fr("", "zero-result")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("", "zero-result")]


def test_planner_does_not_pair_int_call_with_bool_result() -> None:
    """Python-equal ids of different types stay split by string form."""
    messages = [_assistant([_raw_fc(1)]), _tool([_raw_fr(True, "bool-result")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("1", "")]


def test_planner_pairs_unhashable_ids_by_string_form() -> None:
    """Unhashable ids never crash replay; equal string forms pair."""
    messages = [_assistant([_raw_fc(["x"])]), _tool([_raw_fr(["x"], "unhashable-result")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("['x']", "unhashable-result")]


def test_planner_pairs_falsy_unhashable_ids_via_empty_stream() -> None:
    """Falsy unhashable ids coerce into the empty-id stream without crashing."""
    messages = [_assistant([_raw_fc([])]), _tool([_raw_fr({}, "falsy-unhashable-result")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("", "falsy-unhashable-result")]


def test_planner_marker_message_result_not_paired() -> None:
    """A history-marker message never carries answers for a preceding call."""
    messages = [
        _assistant([_fc("c1")]),
        _assistant(
            [_fr("c1", "via-marker")],
            **{HistoryMarkerKind.KEY: HistoryMarkerKind.TURN},
        ),
    ]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("c1", "")]


def test_planner_pairs_embedded_same_message_result() -> None:
    """A result embedded in the call's own message answers that call."""
    messages = [_assistant([_fc("c1"), _fr("c1", "embedded")])]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [("c1", "embedded")]


def test_planner_separates_none_and_empty_id_queues() -> None:
    """None-id and empty-id occurrences pair within their own streams, not by
    arrival order across one conflated key."""
    messages = [
        _assistant([_raw_fc(None)]),
        _assistant([_fc("")]),
        _tool([_fr("", "empty-result"), _raw_fr(None, "none-result")]),
    ]
    tools = _planned_tools(messages)
    assert [(tool.call_id, tool.result) for tool in tools] == [
        ("", "none-result"),
        ("", "empty-result"),
    ]


def _tool_summary(text: str, group_id: str) -> dict[str, Any]:
    return _assistant(
        [_text(text)],
        _group={
            "id": group_id,
            "kind": "assistant_text",
            "_summary_of_message_ids": [f"msg_{group_id}"],
            "_summary_of_group_ids": [group_id],
        },
    )


def test_planner_skips_compaction_tool_summaries_but_keeps_same_text_answers() -> None:
    """A summary never renders or splits the tool groups around it; an answer
    with the same text but no summary annotation still renders.

    Compressed blocks keep summaries beside their originals, so a summary
    must not break how calls pair with their results; that matters for
    hosted cards, the only tool cards a compressed block shows.
    """
    summary_text = '[Tool call: read_file(path="a.py") → contents]'
    transcript = [
        _user_msg("inspect"),
        _assistant([_fc("c1", "read_file")]),
        _tool([_fr("c1", "contents")]),
        _assistant([_fc("c2")]),
        _tool([_fr("c2")]),
        _assistant([_text(summary_text)]),
    ]
    with_summaries = [
        transcript[0],
        _tool_summary(summary_text, "group_1"),
        *transcript[1:3],
        _tool_summary(summary_text, "group_2"),
        *transcript[3:],
    ]

    plan = HistoryReplayPlanner().build_plan(with_summaries)

    assert plan.entries == HistoryReplayPlanner().build_plan(transcript).entries
    assert [
        content["text"]
        for entry in plan.entries
        if isinstance(entry, ReplayAgentMessage)
        for content in entry.contents
        if isinstance(content, dict) and content.get("type") == "text"
    ] == [summary_text]
    assert [tool.call_id for tool in _planned_tools(with_summaries)] == ["c1", "c2"]
