# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP history replay."""

from __future__ import annotations

from typing import Any

import pytest
from acp import schema as acp_schema

from chrys.app.acp.history import replay_session_history
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.tool_kinds import KIND_FILESYSTEM_READ, KIND_FILESYSTEM_WRITE, KIND_MCP, KIND_SHELL
from chrys.foundation.tool_result_metadata import (
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.kernel import (
    GROUP_ANNOTATION_KEY,
    SUMMARY_OF_GROUP_IDS_KEY,
    SUMMARY_OF_MESSAGE_IDS_KEY,
    Content,
    Message,
    set_excluded,
)
from chrys.service.session.message_metadata import TOOL_CALL_KIND_METADATA_KEY, TOOL_RESULT_METADATA_KEY
from chrys.service.state.store import JsonFileStateStore
from tests.support.acp_wire import acp_outgoing_json


class _FakeClient:
    def __init__(self) -> None:
        self.updates: list[acp_schema.SessionNotification] = []

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        notification = acp_schema.SessionNotification(sessionId=session_id, update=update, **kwargs)
        acp_outgoing_json(notification)
        self.updates.append(notification)


def _history_id(message_index: int, content_index: int, occurrence_index: int) -> str:
    return f"history:{message_index}:{content_index}:{occurrence_index}"


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize(
    ("kind", "expected"),
    [(KIND_FILESYSTEM_READ, "read"), (KIND_FILESYSTEM_WRITE, "edit"), (KIND_SHELL, "execute"), ("", "other")],
)
async def test_replay_tool_kind_uses_persisted_metadata_or_resolver(tmp_path, persisted, kind, expected) -> None:
    call = Content.from_function_call("call", "custom_tool", arguments={})
    if persisted:
        call.additional_properties[TOOL_CALL_KIND_METADATA_KEY] = kind
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1", {"messages": [Message("assistant", [call])]}, agent_profile="Code", primary_cwd=str(tmp_path)
    )
    client = _FakeClient()
    await replay_session_history(client, store, "s1", tool_kind_resolver=lambda _name: "" if persisted else kind)
    start = next(n.update for n in client.updates if n.update.session_update == "tool_call")
    assert start.kind == expected


@pytest.mark.asyncio
async def test_replay_session_history_includes_tool_calls_and_results(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("user", ["inspect"]),
                Message(
                    "assistant",
                    [
                        "I'll read it.",
                        Content.from_function_call("call_1", "read_file", arguments={"path": "a.py"}),
                    ],
                ),
                Message("tool", [Content.from_function_result("call_1", result="contents")]),
                Message("assistant", ["done"]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(
        client, store, "s1", tool_kind_resolver=lambda name: KIND_SHELL if name == "zsh" else ""
    )

    updates = [notification.update for notification in client.updates]
    tool_start = next(update for update in updates if update.session_update == "tool_call")
    tool_result = next(update for update in updates if update.session_update == "tool_call_update")
    assert tool_start.tool_call_id == _history_id(1, 1, 1)
    assert tool_start.title == "read file"
    assert tool_start.status == "in_progress"
    assert tool_start.raw_input == {"path": "a.py"}
    assert tool_result.tool_call_id == _history_id(1, 1, 1)
    assert tool_result.status == "completed"
    assert tool_result.raw_output == "contents"


@pytest.mark.asyncio
async def test_replay_session_history_disambiguates_duplicate_call_ids(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("shared", "zsh", arguments={"command": "ls"})]),
                Message("tool", [Content.from_function_result("shared", result="ok")]),
                Message("assistant", [Content.from_function_call("shared", "zsh", arguments={"command": "rm file"})]),
                Message("tool", [Content.from_function_result("shared", result="Error: rejected")]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(
        client, store, "s1", tool_kind_resolver=lambda name: KIND_SHELL if name == "zsh" else ""
    )

    starts = [
        notification.update for notification in client.updates if notification.update.session_update == "tool_call"
    ]
    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [update.tool_call_id for update in starts] == [_history_id(0, 0, 1), _history_id(2, 0, 2)]
    assert [update.tool_call_id for update in results] == [_history_id(0, 0, 1), _history_id(2, 0, 2)]
    assert [update.status for update in results] == ["completed", "failed"]


@pytest.mark.asyncio
async def test_replay_session_history_uses_persisted_tool_kind_before_resolver(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    call = Content.from_function_call("call_1", "remote_tool", arguments={"query": "status"})
    call.additional_properties[TOOL_CALL_KIND_METADATA_KEY] = KIND_MCP
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [call]),
                Message("tool", [Content.from_function_result("call_1", result="Error: remote text")]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1", tool_kind_resolver=lambda _name: KIND_FILESYSTEM_READ)

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [(_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_uses_structured_metadata_for_unkinded_skill_tools(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    result = Content.from_function_result("call_1", result="Error: script failed")
    result.additional_properties[TOOL_RESULT_METADATA_KEY] = {TOOL_FAILED_METADATA_KEY: True}
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("call_1", "run_skill_script", arguments={})]),
                Message("tool", [result]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [(_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_legacy_unkinded_skill_error_text_falls_back(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("call_1", "run_skill_script", arguments={})]),
                Message("tool", [Content.from_function_result("call_1", result="Error: script failed")]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [(_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_shared_result_answers_earliest_same_id_call(tmp_path) -> None:
    """A text-only assistant sibling stays inside the exchange, so the shared
    result block answers the earliest open same-id call, and the later
    duplicate surfaces as the one without a persisted result."""
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("shared", "zsh", arguments={"command": "old"})]),
                Message("assistant", ["No result arrived for the first call."]),
                Message("assistant", [Content.from_function_call("shared", "zsh", arguments={"command": "new"})]),
                Message("tool", [Content.from_function_result("shared", result="ok")]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [
        (_history_id(0, 0, 1), "completed"),
        (_history_id(2, 0, 2), "failed"),
    ]


@pytest.mark.asyncio
async def test_replay_session_history_marks_structured_failures_failed(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("c1", "zsh", arguments={"command": "echo"})]),
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            "c1",
                            result="normal-looking output",
                            additional_properties={"errored": True},
                        )
                    ],
                ),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [(_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_uses_shell_result_metadata_for_status(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("c1", "zsh", arguments={"command": "false"})]),
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            "c1",
                            result="boom\n[exit_code: 1]",
                            additional_properties={
                                TOOL_RESULT_METADATA_KEY: {SHELL_EXIT_CODE_METADATA_KEY: 1},
                            },
                        )
                    ],
                ),
                Message("assistant", [Content.from_function_call("c2", "zsh", arguments={"command": "printf"})]),
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            "c2",
                            result="Error: expected stdout",
                            additional_properties={
                                TOOL_RESULT_METADATA_KEY: {SHELL_EXIT_CODE_METADATA_KEY: 0},
                            },
                        )
                    ],
                ),
                Message("assistant", [Content.from_function_call("c3", "zsh", arguments={"command": "sleep"})]),
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            "c3",
                            result="partial output",
                            additional_properties={
                                TOOL_RESULT_METADATA_KEY: {SHELL_TIMED_OUT_METADATA_KEY: True},
                            },
                        )
                    ],
                ),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [
        (_history_id(0, 0, 1), "failed"),
        (_history_id(2, 0, 2), "completed"),
        (_history_id(4, 0, 3), "failed"),
    ]


@pytest.mark.asyncio
async def test_replay_session_history_uses_legacy_shell_exit_suffix_for_status(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [Content.from_function_call("c1", "zsh", arguments={"command": "printf"})]),
                Message(
                    "tool",
                    [
                        Content.from_function_result(
                            "c1",
                            result="Error: expected stdout\n[exit_code: 0]",
                        )
                    ],
                ),
                Message("assistant", [Content.from_function_call("c2", "zsh", arguments={"command": "false"})]),
                Message("tool", [Content.from_function_result("c2", result="boom\n[exit_code: 1]")]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    results = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    ]
    assert [(update.tool_call_id, update.status) for update in results] == [
        (_history_id(0, 0, 1), "completed"),
        (_history_id(2, 0, 2), "failed"),
    ]


@pytest.mark.asyncio
async def test_replay_session_history_skips_flagged_nudge_streams_injection(tmp_path) -> None:
    """A flagged synthetic ``continue`` nudge is never streamed to the client;
    flagged injections still stream as plain user messages."""
    store = JsonFileStateStore(tmp_path / "sessions")
    nudge = Message("user", ["continue"])
    nudge.additional_properties[HistoryMarkerKind.CONTINUATION_KEY] = True
    injected = Message("user", ["mid-turn note"])
    injected.additional_properties[HistoryMarkerKind.INJECTED_KEY] = True
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("user", ["start task"]),
                Message("assistant", ["working"]),
                nudge,
                injected,
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    user_chunks = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "user_message_chunk"
    ]
    assert [chunk.content.text for chunk in user_chunks] == ["start task", "mid-turn note"]


@pytest.mark.asyncio
async def test_replay_session_history_streams_legacy_unflagged_continue(tmp_path) -> None:
    """Legacy pre-flag histories keep streaming their unflagged ``continue``."""
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("user", ["start task"]),
                Message("assistant", ["working"]),
                Message("user", ["continue"]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    user_chunks = [
        notification.update
        for notification in client.updates
        if notification.update.session_update == "user_message_chunk"
    ]
    assert [chunk.content.text for chunk in user_chunks] == ["start task", "continue"]


# --- exchange-shape pins ------------------------------------------------


async def _replayed_tool_updates(tmp_path, messages: list[Message]) -> list[tuple[str, str]]:
    """Replay *messages* and return (tool_call_id, event) tuples in order."""
    store = JsonFileStateStore(tmp_path / "sessions")
    await store.save_session("s1", {"messages": messages}, agent_profile="Code", primary_cwd=str(tmp_path))
    client = _FakeClient()
    await replay_session_history(client, store, "s1")
    out: list[tuple[str, str]] = []
    for notification in client.updates:
        update = notification.update
        if update.session_update == "tool_call":
            out.append((update.tool_call_id, "start"))
        elif update.session_update == "tool_call_update":
            out.append((update.tool_call_id, update.status))
    return out


def _shape_call(call_id: object, name: str = "zsh") -> Content:
    return Content.from_function_call(call_id, name, arguments={"command": "x"})


def _shape_result(call_id: object) -> Content:
    return Content.from_function_result(call_id, result=f"r_{call_id!r}")


@pytest.mark.asyncio
async def test_replay_session_history_falsy_id_calls_share_one_minting_space(tmp_path) -> None:
    """None-id and empty-id calls mint suffixed ids from one numbering space,
    so neither open failure collapses into the other."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call(None), _shape_call("")])],
    )
    assert updates == [
        (_history_id(0, 0, 1), "start"),
        (_history_id(0, 1, 2), "start"),
        (_history_id(0, 0, 1), "failed"),
        (_history_id(0, 1, 2), "failed"),
    ]


@pytest.mark.asyncio
async def test_replay_session_history_numeric_call_pairs_none_id_result(tmp_path) -> None:
    """A non-string call id and a None-id result pair positionally."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call(7)]), Message("tool", [_shape_result(None)])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_int_call_pairs_bool_result(tmp_path) -> None:
    """Non-string ids of different Python types still pair positionally."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call(1)]), Message("tool", [_shape_result(True)])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_unhashable_id_call_pairs_unhashable_result(tmp_path) -> None:
    """Unhashable ids never crash replay and pair positionally."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call(["x"])]), Message("tool", [_shape_result(["x"])])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_falsy_unhashable_pair_completes(tmp_path) -> None:
    """Falsy unhashable ids never crash replay and pair positionally."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call([])]), Message("tool", [_shape_result({})])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_marker_boundary_orphans_pending_call(tmp_path) -> None:
    """A history marker closes the exchange; results beyond it answer nothing."""
    marker = Message("assistant", ["Execution interrupted"])
    marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call("c1")]), marker, Message("tool", [_shape_result("c1")])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_marker_carried_call_does_not_suffix_real_call(tmp_path) -> None:
    """Payloads on skipped marker messages stay out of the replay-ID numbering space."""
    marker = Message("assistant", [_shape_call("c1")])
    marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    updates = await _replayed_tool_updates(
        tmp_path,
        [marker, Message("assistant", [_shape_call("c1")]), Message("tool", [_shape_result("c1")])],
    )
    assert updates == [(_history_id(1, 0, 1), "start"), (_history_id(1, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_falsy_kind_marker_is_chrome(tmp_path) -> None:
    """KEY presence marks engine chrome; a falsy kind must not stream tool activity."""
    stamped = Message("assistant", [_shape_call("m1")])
    stamped.additional_properties[HistoryMarkerKind.KEY] = ""
    updates = await _replayed_tool_updates(tmp_path, [stamped])
    assert updates == []


@pytest.mark.asyncio
async def test_replay_session_history_text_only_sibling_keeps_pending_call(tmp_path) -> None:
    """A text-only assistant sibling does not end the exchange's pending calls."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [
            Message("assistant", [_shape_call("c1")]),
            Message("assistant", ["thinking"]),
            Message("tool", [_shape_result("c1")]),
        ],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_result_only_assistant_completes_call(tmp_path) -> None:
    """An assistant-role message carrying only results answers the pending call."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call("c1")]), Message("assistant", [_shape_result("c1")])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_embedded_result_completes_call(tmp_path) -> None:
    """A result embedded in the call's own message answers that call."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call("c1"), _shape_result("c1")])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "completed")]


@pytest.mark.asyncio
async def test_replay_session_history_none_result_does_not_answer_empty_id_call(tmp_path) -> None:
    """None-id and empty-id occurrences pair within their own streams."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call("")]), Message("tool", [_shape_result(None)])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_empty_string_result_does_not_answer_falsy_numeric_call(tmp_path) -> None:
    """A falsy non-string call id joins the None stream, not the empty-id stream."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [Message("assistant", [_shape_call(0)]), Message("tool", [_shape_result("")])],
    )
    assert updates == [(_history_id(0, 0, 1), "start"), (_history_id(0, 0, 1), "failed")]


@pytest.mark.asyncio
async def test_replay_session_history_duplicate_id_result_answers_first_occurrence(tmp_path) -> None:
    """A shared result block answers duplicate same-id calls in occurrence order."""
    updates = await _replayed_tool_updates(
        tmp_path,
        [
            Message("assistant", [_shape_call("shared")]),
            Message("assistant", ["gap"]),
            Message("assistant", [_shape_call("shared")]),
            Message("tool", [_shape_result("shared")]),
        ],
    )
    assert updates == [
        (_history_id(0, 0, 1), "start"),
        (_history_id(2, 0, 2), "start"),
        (_history_id(0, 0, 1), "completed"),
        (_history_id(2, 0, 2), "failed"),
    ]


@pytest.mark.asyncio
async def test_hosted_replay_uses_stable_ids_and_terminalizes_every_card(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    completed_call = Content.from_hosted_tool_call(
        "provider-complete",
        tool_name="web_search",
        arguments={"query": "Chrys"},
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="start",
    )
    completed_result = Content.from_hosted_tool_result(
        "provider-complete",
        tool_name="web_search",
        result="found",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    running_call = Content.from_hosted_tool_call(
        "provider-running",
        tool_name="web_search",
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="snapshot",
    )
    failed_call = Content.from_hosted_tool_call(
        "provider-failed",
        tool_name="web_search",
        status="failed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    await store.save_session(
        "s1",
        {"messages": [Message("assistant", [completed_call, completed_result, running_call, failed_call])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )

    async def replay_updates() -> list[acp_schema.SessionUpdate]:
        client = _FakeClient()
        await replay_session_history(client, store, "s1")
        return [notification.update for notification in client.updates]

    first = await replay_updates()
    second = await replay_updates()
    assert [update.tool_call_id for update in first if hasattr(update, "tool_call_id")] == [
        update.tool_call_id for update in second if hasattr(update, "tool_call_id")
    ]
    starts = [update for update in first if update.session_update == "tool_call"]
    terminals = [update for update in first if update.session_update == "tool_call_update"]
    assert [update.kind for update in starts] == ["search", "search", "search"]
    assert [update.tool_call_id for update in starts] == [
        _history_id(0, 0, 1),
        _history_id(0, 2, 2),
        _history_id(0, 3, 3),
    ]
    assert {update.tool_call_id: update.status for update in terminals} == {
        _history_id(0, 0, 1): "completed",
        _history_id(0, 2, 2): "failed",
        _history_id(0, 3, 3): "failed",
    }
    assert all(update.status != "in_progress" for update in terminals)
    assert all(update.raw_output != "No persisted tool result." for update in terminals)


@pytest.mark.asyncio
async def test_hosted_replay_emits_image_and_resource_content(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    call = Content.from_hosted_tool_call(
        "provider-artifacts",
        tool_name="code_interpreter",
        status="running",
        hosted_family="code",
        hosted_provider="openai",
        provider_phase="start",
    )
    result = Content.from_hosted_tool_result(
        "provider-artifacts",
        tool_name="code_interpreter",
        items=[
            Content.from_uri("data:image/png;base64,QUJD", media_type="image/png"),
            Content.from_uri("file:///tmp/report.csv", media_type="text/csv"),
            Content.from_hosted_file("provider-file", media_type="application/pdf", name="report.pdf"),
        ],
        status="completed",
        hosted_family="code",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    await store.save_session(
        "s1",
        {"messages": [Message("assistant", [call, result])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    terminal = next(
        notification.update
        for notification in client.updates
        if notification.update.session_update == "tool_call_update"
    )
    start = next(
        notification.update for notification in client.updates if notification.update.session_update == "tool_call"
    )
    assert terminal.status == "completed"
    assert start.field_meta == {
        "chrys": {
            "provider_hosted": True,
            "hosted_family": "code",
            "provider": "openai",
            "provider_call_id": "provider-artifacts",
            "provider_status": "running",
        }
    }
    assert terminal.field_meta == {
        "chrys": {
            "provider_hosted": True,
            "hosted_family": "code",
            "provider": "openai",
            "provider_call_id": "provider-artifacts",
            "provider_status": "completed",
        }
    }
    assert [item.content.type for item in terminal.content] == ["image", "resource_link", "text"]
    assert terminal.content[0].content.data == "QUJD"
    assert terminal.content[1].content.uri == "file:///tmp/report.csv"
    assert terminal.content[2].content.text == "Hosted artifact: report.pdf (application/pdf)"


@pytest.mark.asyncio
async def test_hosted_replay_emits_standalone_results_with_synthetic_starts(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    assistant_result = Content.from_hosted_tool_result(
        "assistant-only",
        tool_name="web_search",
        result="assistant result",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    tool_result = Content.from_hosted_tool_result(
        "tool-only",
        tool_name="web_search",
        result="tool result",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    await store.save_session(
        "s1",
        {
            "messages": [
                Message("assistant", [assistant_result]),
                Message("tool", [tool_result]),
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    updates = [notification.update for notification in client.updates]
    assert [update.session_update for update in updates] == [
        "tool_call",
        "tool_call_update",
        "tool_call",
        "tool_call_update",
    ]
    assert [update.tool_call_id for update in updates] == [
        _history_id(0, 0, 1),
        _history_id(0, 0, 1),
        _history_id(1, 0, 2),
        _history_id(1, 0, 2),
    ]
    assert [update.raw_output for update in updates if update.session_update == "tool_call_update"] == [
        "assistant result",
        "tool result",
    ]
    assert all(update.field_meta["chrys"]["provider_hosted"] is True for update in updates)


@pytest.mark.asyncio
async def test_hosted_replay_preserves_call_result_final_text_order(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    call = Content.from_hosted_tool_call(
        "provider-search",
        tool_name="web_search",
        arguments={"query": "Chrys"},
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="start",
    )
    result = Content.from_hosted_tool_result(
        "provider-search",
        tool_name="web_search",
        result="found",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    await store.save_session(
        "s1",
        {"messages": [Message("assistant", [call, result, Content.from_text("Final answer.")])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    updates = [notification.update for notification in client.updates]
    assert [update.session_update for update in updates] == [
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert updates[-1].content.text == "Final answer."


@pytest.mark.asyncio
async def test_hosted_replay_terminalizes_call_only_terminal_before_later_text(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    failed_call = Content.from_hosted_tool_call(
        "provider-failed",
        tool_name="web_search",
        status="failed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    await store.save_session(
        "s1",
        {"messages": [Message("assistant", [failed_call, Content.from_text("Final answer.")])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    updates = [notification.update for notification in client.updates]
    assert [update.session_update for update in updates] == [
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    assert updates[1].status == "failed"
    assert updates[-1].content.text == "Final answer."


@pytest.mark.asyncio
async def test_hosted_replay_defers_unfinished_call_terminalization_until_eof(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    running_call = Content.from_hosted_tool_call(
        "provider-running",
        tool_name="web_search",
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="snapshot",
    )
    await store.save_session(
        "s1",
        {"messages": [Message("assistant", [running_call, Content.from_text("Observed so far.")])]},
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    updates = [notification.update for notification in client.updates]
    assert [update.session_update for update in updates] == [
        "tool_call",
        "agent_message_chunk",
        "tool_call_update",
    ]
    assert updates[-1].status == "failed"
    assert updates[-1].raw_output == "Provider-hosted tool was interrupted before the session was persisted."


@pytest.mark.asyncio
async def test_replay_preserves_mixed_text_local_and_hosted_content_order(tmp_path) -> None:
    store = JsonFileStateStore(tmp_path / "sessions")
    hosted_call = Content.from_hosted_tool_call(
        "provider-search",
        tool_name="web_search",
        status="running",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="start",
    )
    hosted_result = Content.from_hosted_tool_result(
        "provider-search",
        tool_name="web_search",
        result="found",
        status="completed",
        hosted_family="search",
        hosted_provider="openai",
        provider_phase="terminal",
    )
    local_call = Content.from_function_call("local-read", "read_file", arguments={"path": "README.md"})
    local_result = Content.from_function_result("local-read", result="contents")
    await store.save_session(
        "s1",
        {
            "messages": [
                Message(
                    "assistant",
                    [
                        Content.from_text("Before."),
                        hosted_call,
                        hosted_result,
                        Content.from_text("Between."),
                        local_call,
                        local_result,
                        Content.from_text("After."),
                    ],
                )
            ]
        },
        agent_profile="Code",
        primary_cwd=str(tmp_path),
    )
    client = _FakeClient()

    await replay_session_history(client, store, "s1")

    updates = [notification.update for notification in client.updates]
    assert [update.session_update for update in updates] == [
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
    ]
    text_updates = [update for update in updates if update.session_update == "agent_message_chunk"]
    assert [update.content.text for update in text_updates] == ["Before.", "\n\nBetween.", "\n\nAfter."]


_SUMMARY_TEXT = '[Tool call: read_file(path="a.py") → contents]'


def _summary_replay_transcript(*, compacted: bool) -> list[Message]:
    """A turn whose read_file group compaction replaced, as saved: the summary, then the excluded originals.

    Without ``compacted`` the group is absent altogether, which is how the
    reopened transcript should read.
    """
    read_group: list[Message] = []
    if compacted:
        annotation = {
            "id": "group_1",
            "kind": "assistant_text",
            SUMMARY_OF_MESSAGE_IDS_KEY: ["msg_1", "msg_2"],
            SUMMARY_OF_GROUP_IDS_KEY: ["group_1"],
        }
        read_group = [
            Message("assistant", [_SUMMARY_TEXT], additional_properties={GROUP_ANNOTATION_KEY: annotation}),
            Message("assistant", [Content.from_function_call("c1", "read_file", arguments={"path": "a.py"})]),
            Message("tool", [Content.from_function_result("c1", result="contents")]),
        ]
        for original in read_group[1:]:
            set_excluded(original, excluded=True, reason="budget_tool_compaction")
    return [
        Message("user", ["inspect"]),
        *read_group,
        Message("assistant", [Content.from_function_call("c2", "zsh", arguments={"command": "ls"})]),
        Message("tool", [Content.from_function_result("c2", result="ok")]),
        # A real answer that happens to read like a summary.
        Message("assistant", [_SUMMARY_TEXT]),
    ]


@pytest.mark.asyncio
async def test_replay_session_history_skips_compacted_groups_but_keeps_same_text_answers(tmp_path) -> None:
    """A compacted group replays as nothing: its originals are excluded on disk and its summary is the model's."""
    store = JsonFileStateStore(tmp_path / "sessions")
    updates: dict[bool, list[Any]] = {}
    for compacted in (False, True):
        session_id = f"s-{compacted}"
        await store.save_session(
            session_id,
            {"messages": _summary_replay_transcript(compacted=compacted)},
            agent_profile="Code",
            primary_cwd=str(tmp_path),
        )
        client = _FakeClient()
        await replay_session_history(client, store, session_id)
        updates[compacted] = [notification.update for notification in client.updates]

    assert updates[True] == updates[False]
    assert [update.content.text for update in updates[True] if update.session_update == "agent_message_chunk"] == [
        _SUMMARY_TEXT
    ]
    assert [update.title for update in updates[True] if update.session_update == "tool_call"] == ["zsh"]
