# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted sub-agent transcript normalization tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.platform.files import atomic_write_owner_only_text
from chrys.foundation.tool_result_metadata import TOOL_INTERRUPTED_METADATA_KEY, TOOL_RESULT_METADATA_KEY
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.kernel import Content, Message
from chrys.service.session.sub_agent_logs import (
    ACP_ANSWER_WITHDRAWN_UPDATE,
    SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY,
    sessions_dir,
)
from chrys.service.session.sub_agent_transcript import load_persisted_sub_agent_transcript


async def test_load_kernel_sub_agent_transcript_uses_canonical_messages(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_a1b2c3d4e5f6.json"
    path.parent.mkdir(parents=True)
    messages = [
        {"role": "assistant", "contents": [{"type": "text", "text": "checking"}]},
        {
            "role": "assistant",
            "contents": [
                {"type": "function_call", "name": "read_file", "call_id": "call-1", "arguments": {"path": "a"}}
            ],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "call-1", "result": "ok"}]},
        {"role": "assistant", "contents": [{"type": "text", "text": "done"}]},
    ]
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "kernel",
                    "agent_display_name": "Explore Agent",
                    "status": "completed",
                },
                "state": {"messages": messages},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == messages
    assert transcript.profile_name == "Explore Agent"
    assert transcript.terminal_audit is True
    assert transcript.includes_final_message is True


async def test_load_kernel_sub_agent_transcript_restores_compacted_messages_in_order(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_compacted.json"
    path.parent.mkdir(parents=True)
    first_block = [
        {"role": "assistant", "contents": [{"type": "text", "text": "I will inspect."}]},
        {
            "role": "assistant",
            "contents": [
                {"type": "function_call", "name": "read_file", "call_id": "call-1", "arguments": {"path": "a"}}
            ],
        },
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "call-1", "result": "contents"}]},
    ]
    second_block = [{"role": "assistant", "contents": [{"type": "text", "text": "One more check."}]}]
    first_summary = {
        "role": "assistant",
        "contents": [{"type": "text", "text": "[Compressed context: ctx-1]\nSummary: inspected"}],
        "additional_properties": {
            HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
            "_block_id": "ctx-1",
        },
    }
    second_summary = {
        "role": "assistant",
        "contents": [{"type": "text", "text": "[Compressed context: ctx-2]\nSummary: checked"}],
        "additional_properties": {
            HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
            "_block_id": "ctx-2",
        },
    }
    final = {"role": "assistant", "contents": [{"type": "text", "text": "Done."}]}
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {
                    "compressed_msgs": [
                        {"compressed_context_id": "ctx-1", "messages": first_block},
                        {"compressed_context_id": "ctx-2", "messages": second_block},
                    ],
                    "messages": [first_summary, second_summary, final],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [*first_block, *second_block, final]
    assert transcript.includes_final_message is True


@pytest.mark.parametrize("identified", [False, True], ids=["legacy", "identified"])
async def test_load_kernel_sub_agent_transcript_leaves_the_archive_index_out(tmp_path: Path, identified: bool) -> None:
    """The index lists blocks whose messages the transcript restores anyway: it is no message of the run."""
    path = sessions_dir(tmp_path) / "Explore_archived.json"
    path.parent.mkdir(parents=True)

    def message(role: str, text: str, item: int, **properties: object) -> dict:
        extra = {ANALYTICS_ITEM_ID_KEY: f"{item:032x}", **properties} if identified else dict(properties)
        return {"role": role, "contents": [{"type": "text", "text": text}], "additional_properties": extra}

    first_block = [message("user", "first question", 1), message("assistant", "first answer", 2)]
    second_block = [message("user", "second question", 3), message("assistant", "second answer", 4)]
    index = message(
        "assistant",
        "[Archive index]\nctx-1 · turns 1-1 · first question",
        5,
        **{
            HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
            HistoryMarkerKind.ARCHIVE_INDEX_KEY: [
                {"first_id": "ctx-1", "last_id": "ctx-1", "turn_from": 1, "turn_to": 1, "blocks": 1, "label": ""}
            ],
        },
    )
    archived_summary = message(
        "assistant",
        "[Compressed context: ctx-1]\nSummary: first",
        6,
        **{
            HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
            "_block_id": "ctx-1",
            "_excluded": True,
            "_exclude_reason": "archive_index",
        },
    )
    summary = message(
        "assistant",
        "[Compressed context: ctx-2]\nSummary: second",
        7,
        **{HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-2"},
    )
    final = message("assistant", "Done.", 8)
    meta: dict[str, object] = {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"}
    if identified:
        meta["transcript_occurrence_identity"] = SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": meta,
                "state": {
                    "compressed_msgs": [
                        {"compressed_context_id": "ctx-1", "messages": first_block},
                        {"compressed_context_id": "ctx-2", "messages": second_block},
                    ],
                    "messages": [index, archived_summary, summary, final],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [*first_block, *second_block, final]


async def test_load_kernel_sub_agent_transcript_deduplicates_only_twins_replaced_by_summary(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_twins.json"
    path.parent.mkdir(parents=True)
    twin = {"role": "assistant", "contents": [{"type": "text", "text": "Same content."}]}
    repeated = {"role": "assistant", "contents": [{"type": "text", "text": "Legitimate repeat."}]}
    archived_twin = {**twin, "message_id": "archived-id"}
    live_twin = {**twin, "message_id": "reminted-live-id"}
    summary = {
        "role": "assistant",
        "contents": [{"type": "text", "text": "[Compressed context: ctx-1]\nSummary: repeated"}],
        "additional_properties": {
            HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
            "_block_id": "ctx-1",
        },
    }
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {
                    "compressed_msgs": [
                        {
                            "compressed_context_id": "ctx-1",
                            "messages": [archived_twin, repeated, repeated],
                        }
                    ],
                    "messages": [live_twin, repeated, repeated, summary, twin, repeated],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [archived_twin, repeated, repeated, twin, repeated]


async def test_load_kernel_sub_agent_transcript_preserves_equal_message_after_summary(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_later_repeat.json"
    path.parent.mkdir(parents=True)
    archived = {
        "role": "assistant",
        "message_id": "archived-id",
        "contents": [{"type": "text", "text": "Done."}],
    }
    summary = {
        "role": "assistant",
        "contents": [{"type": "text", "text": "[Compressed context: ctx-1]\nSummary: done"}],
        "additional_properties": {
            HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY,
            "_block_id": "ctx-1",
        },
    }
    later = {
        "role": "assistant",
        "message_id": "reminted-live-id",
        "contents": [{"type": "text", "text": "Done."}],
    }
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {
                    "compressed_msgs": [{"compressed_context_id": "ctx-1", "messages": [archived]}],
                    "messages": [summary, later],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [archived, later]


async def test_load_kernel_sub_agent_transcript_deduplicates_orphan_archive_live_overlap(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_orphan_overlap.json"
    path.parent.mkdir(parents=True)
    archived_call = {
        "role": "assistant",
        "message_id": "archived-call",
        "contents": [{"type": "function_call", "name": "read_file", "call_id": "call-1", "arguments": {"path": "a"}}],
    }
    archived_result = {
        "role": "tool",
        "message_id": "archived-result",
        "contents": [{"type": "function_result", "call_id": "call-1", "result": "contents"}],
    }
    live_call = {**archived_call, "message_id": "reminted-live-call"}
    live_result = {**archived_result, "message_id": "reminted-live-result"}
    later_boundary = {"role": "user", "contents": [{"type": "text", "text": "Run it again."}]}
    later_call = {**archived_call, "message_id": "later-call"}
    later_result = {**archived_result, "message_id": "later-result"}
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {
                    "compressed_msgs": [
                        {
                            "compressed_context_id": "ctx-orphan",
                            "messages": [archived_call, archived_result],
                        }
                    ],
                    "messages": [live_call, live_result, later_boundary, later_call, later_result],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [archived_call, archived_result, later_boundary, later_call, later_result]


async def test_load_identified_kernel_transcript_orders_stacked_blocks_and_deduplicates_orphan(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_identified_stacked.json"
    path.parent.mkdir(parents=True)

    def identified(message: dict, item_id: str) -> dict:
        return {**message, "additional_properties": {ANALYTICS_ITEM_ID_KEY: item_id}}

    old_call = identified(
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "read_file", "call_id": "old", "arguments": {}}],
        },
        "1" * 32,
    )
    old_result = identified(
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "old", "result": "old"}]},
        "2" * 32,
    )
    orphan_call = identified(
        {
            "role": "assistant",
            "contents": [{"type": "function_call", "name": "read_file", "call_id": "new", "arguments": {}}],
        },
        "3" * 32,
    )
    orphan_result = identified(
        {"role": "tool", "contents": [{"type": "function_result", "call_id": "new", "result": "new"}]},
        "4" * 32,
    )
    summary = identified(
        {
            "role": "assistant",
            "contents": [{"type": "text", "text": "[Compressed context: ctx-old]\nSummary: old"}],
        },
        "5" * 32,
    )
    summary["additional_properties"].update({HistoryMarkerKind.KEY: HistoryMarkerKind.SUMMARY, "_block_id": "ctx-old"})
    later_boundary = identified(
        {"role": "user", "contents": [{"type": "text", "text": "Run the same operation again."}]},
        "6" * 32,
    )
    later_call = identified(
        {key: value for key, value in orphan_call.items() if key != "additional_properties"}, "7" * 32
    )
    later_result = identified(
        {key: value for key, value in orphan_result.items() if key != "additional_properties"},
        "8" * 32,
    )
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "kernel",
                    "transcript_occurrence_identity": SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY,
                },
                "state": {
                    "compressed_msgs": [
                        {"compressed_context_id": "ctx-old", "messages": [old_call, old_result]},
                        {"compressed_context_id": "ctx-orphan", "messages": [orphan_call, orphan_result]},
                    ],
                    "messages": [summary, orphan_call, orphan_result, later_boundary, later_call, later_result],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [
        old_call,
        old_result,
        orphan_call,
        orphan_result,
        later_boundary,
        later_call,
        later_result,
    ]


async def test_load_identified_kernel_transcript_fails_closed_when_an_occurrence_id_is_missing(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_incomplete_identity.json"
    path.parent.mkdir(parents=True)
    archived = {
        "role": "assistant",
        "contents": [{"type": "text", "text": "Archived once."}],
        "additional_properties": {ANALYTICS_ITEM_ID_KEY: "1" * 32},
    }
    final_without_id = {"role": "assistant", "contents": [{"type": "text", "text": "Final answer."}]}
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "kernel",
                    "transcript_occurrence_identity": SUB_AGENT_TRANSCRIPT_OCCURRENCE_IDENTITY,
                },
                "state": {
                    "compressed_msgs": [{"compressed_context_id": "ctx-orphan", "messages": [archived]}],
                    "messages": [final_without_id],
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [archived, final_without_id]


async def test_load_kernel_sub_agent_transcript_recognizes_final_text_after_hosted_result(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_hosted.json"
    path.parent.mkdir(parents=True)
    call = Content.from_hosted_tool_call(
        "search-1",
        tool_name="web_search",
        arguments={"query": "Chrys"},
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="start",
        provider_status="running",
    )
    result = Content.from_hosted_tool_result(
        "search-1",
        tool_name="web_search",
        result="found",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
        provider_status="completed",
    )
    message = Message("assistant", [call, result, Content.from_text("Final answer.")]).to_dict()
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {"messages": [message], "compressed_msgs": []},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [message]
    assert transcript.includes_final_message is True


async def test_load_kernel_sub_agent_transcript_recognizes_legacy_server_executed_final(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_legacy_hosted.json"
    path.parent.mkdir(parents=True)
    message = {
        "role": "assistant",
        "contents": [
            {
                "type": "function_call",
                "call_id": "legacy-search",
                "name": "tool_search",
                "arguments": {"query": "Chrys"},
                "informational_only": True,
                "additional_properties": {
                    "item_type": "tool_search_call",
                    "item_id": "server-item-1",
                    "execution": "server",
                    "status": "completed",
                },
            },
            {"type": "text", "text": "Legacy final answer."},
        ],
    }
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {"messages": [message], "compressed_msgs": []},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [message]
    assert transcript.includes_final_message is True


async def test_load_kernel_sub_agent_transcript_does_not_treat_local_tool_followup_as_final(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Explore_local_tool.json"
    path.parent.mkdir(parents=True)
    message = Message(
        "assistant",
        [
            Content.from_function_call("read-1", "read_file", arguments={"path": "README.md"}),
            Content.from_text("Still checking."),
        ],
    ).to_dict()
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": "completed"},
                "state": {"messages": [message], "compressed_msgs": []},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.includes_final_message is False


@pytest.mark.parametrize(
    ("status", "terminal_audit"),
    [
        ("failed", True),
        ("cancelled", True),
        ("aborted", True),
        ("cascade_aborted", True),
        ("setup_failed", True),
        ("orphaned", True),
        ("paused", False),
        ("running", False),
    ],
)
async def test_load_sub_agent_transcript_requires_successful_terminal_status_for_final_message(
    tmp_path: Path,
    status: str,
    terminal_audit: bool,
) -> None:
    path = sessions_dir(tmp_path) / f"Explore_{status}.json"
    path.parent.mkdir(parents=True)
    message = {"role": "assistant", "contents": [{"type": "text", "text": "Partial assistant prose."}]}
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {"record_type": "sub_agent_session", "runner": "kernel", "status": status},
                "state": {"messages": [message], "compressed_msgs": []},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.messages == [message]
    assert transcript.terminal_audit is terminal_audit
    assert transcript.includes_final_message is False


async def test_load_acp_sub_agent_transcript_preserves_message_tool_order(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Remote_a1b2c3d4e5f6.json"
    path.parent.mkdir(parents=True)
    updates = [
        {
            "seq": 1,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m1",
                "content": {"type": "text", "text": "I will inspect."},
            },
        },
        {
            "seq": 2,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "remote-1",
                "title": "Read",
                "kind": "filesystem.read",
                "rawInput": {"path": "README.md"},
                "status": "in_progress",
            },
        },
        {
            "seq": 3,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "remote-1",
                "rawOutput": "contents",
                "status": "completed",
            },
        },
        {
            "seq": 4,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "m2",
                "content": {"type": "text", "text": "Finished."},
            },
        },
    ]
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "acp",
                    "agent_display_name": "Remote",
                    "status": "completed",
                },
                "acp_state": {"translated_updates": updates},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.includes_final_message is True
    assert [message["role"] for message in transcript.messages] == ["assistant", "tool", "assistant"]
    first_contents = transcript.messages[0]["contents"]
    assert [content["type"] for content in first_contents] == ["text", "function_call"]
    assert first_contents[1]["arguments"] == {"path": "README.md"}
    assert transcript.messages[1]["contents"][0]["result"] == "contents"
    assert transcript.messages[2]["contents"][0]["text"] == "Finished."
    assert transcript.represents_result_text("Finished.") is False
    assert transcript.covers_result_text("Finished.") is False


async def test_load_acp_sub_agent_transcript_fingerprints_only_successful_attempt(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Remote_retried.json"
    path.parent.mkdir(parents=True)
    updates = [
        {
            "attempt": 1,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "old",
                "content": {"type": "text", "text": "Old failed attempt."},
            },
        },
        {
            "attempt": 2,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "new-1",
                "content": {"type": "text", "text": "New first."},
            },
        },
        {
            "attempt": 2,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "new-2",
                "content": {"type": "text", "text": "New second."},
            },
        },
        {
            "attempt": 2,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "terminal",
                "title": "Read",
                "kind": "read",
                "status": "completed",
            },
        },
    ]
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "acp",
                    "status": "completed",
                },
                "acp_state": {
                    "successful_attempt": 2,
                    "translated_updates": updates,
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert transcript.represents_result_text("New second.") is True
    assert transcript.represents_result_text("New first.\n\nNew second.") is True
    assert transcript.represents_result_text("Old failed attempt.\n\nNew first.\n\nNew second.") is False
    assert transcript.covers_result_text("New first.\n\nNew second.") is True


@pytest.mark.parametrize(
    ("trail", "expected"),
    [
        # The attempt's work before its last card stays; the retried answer shows once, from the attempt that gave it.
        ("retried", ["Before.", "acp:read:1", "acp:read:1", "Answer."]),
        # A retry that failed before its first update still withdrew the answer.
        ("retry_showed_nothing", ["Before.", "acp:read:1", "acp:read:1"]),
        # An audit that records no withdrawal keeps every attempt's text.
        ("no_withdrawal", ["Before.", "acp:read:1", "acp:read:1", "Answer.", "More.", "Answer."]),
    ],
)
async def test_load_acp_transcript_drops_the_answer_a_retry_withdrew(
    tmp_path: Path, trail: str, expected: list[str]
) -> None:
    path = sessions_dir(tmp_path) / "Remote_retry_answer.json"
    path.parent.mkdir(parents=True)

    def text(attempt: int, message_id: str, value: str) -> dict[str, object]:
        return {
            "attempt": attempt,
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "messageId": message_id,
                "content": {"type": "text", "text": value},
            },
        }

    updates: list[dict[str, object]] = [
        text(1, "a1-before", "Before."),
        {
            "attempt": 1,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "read",
                "title": "Read",
                "kind": "read",
                "status": "completed",
            },
        },
        text(1, "a1-answer", "Answer."),
        text(1, "a1-more", "More."),
    ]
    if trail != "no_withdrawal":
        updates.append({"attempt": 1, "seq": None, "update": {"sessionUpdate": ACP_ANSWER_WITHDRAWN_UPDATE}})
    meta = {"record_type": "sub_agent_session", "runner": "acp", "status": "failed"}
    acp_state: dict[str, object] = {"translated_updates": updates}
    if trail != "retry_showed_nothing":
        updates.append(text(2, "a2-answer", "Answer."))
        meta["status"] = "completed"
        acp_state["successful_attempt"] = 2
    atomic_write_owner_only_text(path, json.dumps({"meta": meta, "acp_state": acp_state}))

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    shown = [
        content.get("text") or content.get("call_id")
        for message in transcript.messages
        for content in message["contents"]
    ]
    assert shown == expected


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_load_terminal_acp_transcript_interrupts_unfinished_tools(tmp_path: Path, status: str) -> None:
    path = sessions_dir(tmp_path) / f"Remote_{status}_running_tool.json"
    path.parent.mkdir(parents=True)
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "acp",
                    "status": status,
                },
                "acp_state": {
                    "translated_updates": [
                        {
                            "attempt": 1,
                            "update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "unfinished",
                                "title": "Read",
                                "kind": "read",
                                "status": "in_progress",
                            },
                        }
                    ]
                },
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    assert [message["role"] for message in transcript.messages] == ["assistant", "tool"]
    result = transcript.messages[1]["contents"][0]
    assert result["result"] == "(interrupted)"
    assert result["additional_properties"][TOOL_RESULT_METADATA_KEY] == {
        TOOL_INTERRUPTED_METADATA_KEY: True,
    }


async def test_load_acp_sub_agent_transcript_preserves_reused_tool_call_id_occurrences(tmp_path: Path) -> None:
    path = sessions_dir(tmp_path) / "Remote_reused.json"
    path.parent.mkdir(parents=True)
    updates = [
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "reused",
                "title": "Read first",
                "kind": "filesystem.read",
                "rawInput": {"path": "first.txt"},
                "status": "in_progress",
            }
        },
        {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "reused",
                "rawOutput": "first result",
                "status": "completed",
            }
        },
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "reused",
                "title": "Read second",
                "kind": "filesystem.read",
                "rawInput": {"path": "second.txt"},
                "status": "in_progress",
            }
        },
        {
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "reused",
                "rawOutput": "second result",
                "status": "completed",
            }
        },
    ]
    atomic_write_owner_only_text(
        path,
        json.dumps(
            {
                "meta": {
                    "record_type": "sub_agent_session",
                    "runner": "acp",
                    "agent_display_name": "Remote",
                },
                "acp_state": {"translated_updates": updates},
            }
        ),
    )

    transcript = await load_persisted_sub_agent_transcript(tmp_path, path.name)

    assert transcript is not None
    calls = [
        message["contents"][0]
        for message in transcript.messages
        if message["role"] == "assistant" and message["contents"][0]["type"] == "function_call"
    ]
    results = [message["contents"][0] for message in transcript.messages if message["role"] == "tool"]
    assert [call["name"] for call in calls] == ["Read first", "Read second"]
    assert [call["arguments"] for call in calls] == [{"path": "first.txt"}, {"path": "second.txt"}]
    assert calls[0]["call_id"] != calls[1]["call_id"]
    assert [result["call_id"] for result in results] == [call["call_id"] for call in calls]
    assert [result["result"] for result in results] == ["first result", "second result"]


@pytest.mark.parametrize("log_file", ["../escape.json", "nested/file.json", ".hidden.json"])
async def test_load_sub_agent_transcript_rejects_unsafe_basename(tmp_path: Path, log_file: str) -> None:
    assert await load_persisted_sub_agent_transcript(tmp_path, log_file) is None
