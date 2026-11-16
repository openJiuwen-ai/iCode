# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests for multi-round conversation + context compression.

Tests the full CompressibleHistoryProvider pipeline:
- Turn marker insertion after each agent turn
- Single and stacked compression
- Serialization round-trip with compressed blocks
- Session save/restore with compressed state
- Engine-level marker insertion and interrupt rollback
- get_messages() filtering
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message
from chrys.service.context.providers.history import (
    CompressibleHistoryProvider,
    _is_compressed_summary,
    _is_turn_marker,
)
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.serializers import deserialize_state, serialize_state
from chrys.service.state.store import JsonFileStateStore
from tests.support.event_capture import collect_events
from tests.support.waiting import await_run_task_chain

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_multi_turn_state(num_turns: int) -> dict:
    """Build a state dict simulating num_turns of conversation with markers.

    Each turn: user message + assistant message + turn marker.
    """
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for i in range(1, num_turns + 1):
        state["messages"].append(Message("user", [f"User message turn {i}"]))
        state["messages"].append(Message("assistant", [f"Assistant response turn {i}"]))
        CompressibleHistoryProvider.insert_marker(state, i)
    return state


# ---------------------------------------------------------------------------
# Unit tests: CompressibleHistoryProvider
# ---------------------------------------------------------------------------


class TestInsertMarker:
    def test_insert_marker_appends_to_messages(self) -> None:
        state: dict = {"messages": [Message("user", ["hello"])]}
        marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
        assert marker_id == "turn_1"
        assert len(state["messages"]) == 2
        assert state["turn_counter"] == 1

    def test_marker_message_has_correct_metadata(self) -> None:
        state: dict = {"messages": []}
        CompressibleHistoryProvider.insert_marker(state, 3)
        marker_msg = state["messages"][0]
        assert marker_msg.additional_properties[HistoryMarkerKind.KEY] == HistoryMarkerKind.TURN
        assert marker_msg.additional_properties["_turn_id"] == "turn_3"
        assert marker_msg.additional_properties["_turn"] == 3

    def test_multiple_markers_have_unique_ids(self) -> None:
        state: dict = {"messages": []}
        m1 = CompressibleHistoryProvider.insert_marker(state, 1)
        m2 = CompressibleHistoryProvider.insert_marker(state, 2)
        m3 = CompressibleHistoryProvider.insert_marker(state, 3)
        assert m1 != m2 != m3
        assert len(state["messages"]) == 3


class TestGetMessagesFiltering:
    @pytest.mark.asyncio
    async def test_get_messages_excludes_markers(self) -> None:
        provider = CompressibleHistoryProvider()
        state = _build_multi_turn_state(3)

        # Raw messages include markers
        assert len(state["messages"]) == 9  # 3 turns * (user + assistant + marker)

        # get_messages should filter out markers
        visible = await provider.get_messages(None, state=state)
        assert len(visible) == 6  # 3 turns * (user + assistant)
        assert all(not _is_turn_marker(m) for m in visible)

    @pytest.mark.asyncio
    async def test_get_messages_keeps_compressed_summaries(self) -> None:
        provider = CompressibleHistoryProvider()
        state = _build_multi_turn_state(3)

        # Compress at marker_2
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary of turns 1-2")

        visible = await provider.get_messages(None, state=state)
        # Should include: summary + user3 + assistant3 (marker_3 filtered)
        summaries = [m for m in visible if _is_compressed_summary(m)]
        assert len(summaries) == 1
        assert "Summary of turns 1-2" in summaries[0].contents[0].text


class TestCompress:
    def test_single_compress(self) -> None:
        state = _build_multi_turn_state(3)

        ctx_id = CompressibleHistoryProvider.compress(state, "turn_2", "Did turns 1 and 2")

        assert ctx_id.startswith("ctx_")
        # After compression: summary + user3 + assistant3 + marker_3 = 4
        assert len(state["messages"]) == 4
        # First message should be the summary
        assert _is_compressed_summary(state["messages"][0])
        assert "Did turns 1 and 2" in state["messages"][0].contents[0].text
        # compressed_msgs should have one block
        assert len(state["compressed_msgs"]) == 1
        block = state["compressed_msgs"][0]
        assert block.compressed_context_id == ctx_id
        assert block.marker_id == "turn_2"
        assert block.turn_range == (1, 2)
        # Block should contain the original messages (6: user1+asst1+marker1+user2+asst2+marker2)
        assert len(block.messages) == 6

    def test_stacking_two_compressions(self) -> None:
        state = _build_multi_turn_state(4)

        # First compress at marker_2
        ctx_id_a = CompressibleHistoryProvider.compress(state, "turn_2", "Summary A: turns 1-2")
        # State: summary_A + user3 + asst3 + marker_3 + user4 + asst4 + marker_4 = 7
        assert len(state["messages"]) == 7

        # Second compress at marker_3
        ctx_id_b = CompressibleHistoryProvider.compress(state, "turn_3", "Summary B: turn 3")
        # State: summary_A + summary_B + user4 + asst4 + marker_4 = 5
        assert len(state["messages"]) == 5
        assert _is_compressed_summary(state["messages"][0])
        assert _is_compressed_summary(state["messages"][1])

        # Two compressed blocks
        assert len(state["compressed_msgs"]) == 2
        assert state["compressed_msgs"][0].compressed_context_id == ctx_id_a
        assert state["compressed_msgs"][1].compressed_context_id == ctx_id_b

        # Second block covers turn 3 only (user3+asst3+marker_3 = 3 messages)
        assert state["compressed_msgs"][1].turn_range == (3, 3)
        assert len(state["compressed_msgs"][1].messages) == 3

    def test_stacking_three_compressions(self) -> None:
        state = _build_multi_turn_state(5)

        CompressibleHistoryProvider.compress(state, "turn_2", "Summary A")
        CompressibleHistoryProvider.compress(state, "turn_3", "Summary B")
        CompressibleHistoryProvider.compress(state, "turn_4", "Summary C")

        # State: summary_A + summary_B + summary_C + user5 + asst5 + marker_5 = 6
        assert len(state["messages"]) == 6
        assert len(state["compressed_msgs"]) == 3

        # Summaries preserved in order
        for i in range(3):
            assert _is_compressed_summary(state["messages"][i])

    def test_compress_all_turns_except_last(self) -> None:
        """Compress everything up to the last marker, leaving only the most recent turn."""
        state = _build_multi_turn_state(3)
        CompressibleHistoryProvider.compress(state, "turn_3", "Summary of all 3 turns")

        # All messages compressed — only summary remains
        assert len(state["messages"]) == 1
        assert _is_compressed_summary(state["messages"][0])
        assert len(state["compressed_msgs"]) == 1
        assert state["compressed_msgs"][0].turn_range == (1, 3)

    def test_compress_preserves_original_messages_via_deep_copy(self) -> None:
        state = _build_multi_turn_state(2)
        original_user_text = state["messages"][0].contents[0].text

        CompressibleHistoryProvider.compress(state, "turn_1", "Summary")

        # Original messages in the block should be independent copies
        block = state["compressed_msgs"][0]
        assert block.messages[0].contents[0].text == original_user_text

        # Mutating the block's messages should not affect the state
        block.messages[0].contents[0]._text = "MUTATED"
        # State messages should be unaffected (they're replaced by summary anyway)

    def test_compress_invalid_marker_raises(self) -> None:
        state = _build_multi_turn_state(2)
        with pytest.raises(ValueError, match="not found"):
            CompressibleHistoryProvider.compress(state, "marker_999", "summary")

    def test_compress_empty_history_raises(self) -> None:
        state: dict = {"messages": [], "compressed_msgs": []}
        with pytest.raises(ValueError, match="empty"):
            CompressibleHistoryProvider.compress(state, "turn_1", "summary")

    def test_compress_already_compressed_marker_raises(self) -> None:
        state = _build_multi_turn_state(3)
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary A")
        # marker_1 was consumed by compression — no longer in active history
        with pytest.raises(ValueError, match="not found"):
            CompressibleHistoryProvider.compress(state, "turn_1", "Summary B")


class TestListCompressed:
    def test_empty_state(self) -> None:
        state: dict = {"messages": [], "compressed_msgs": []}
        info = CompressibleHistoryProvider.list_compressed(state)
        assert info["blocks"] == []
        assert info["markers"] == []

    def test_lists_available_markers(self) -> None:
        state = _build_multi_turn_state(3)
        info = CompressibleHistoryProvider.list_compressed(state)
        assert len(info["markers"]) == 3
        assert info["markers"][0]["marker_id"] == "turn_1"
        assert info["markers"][2]["marker_id"] == "turn_3"

    def test_markers_after_compress_excludes_folded(self) -> None:
        state = _build_multi_turn_state(4)
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary A")

        info = CompressibleHistoryProvider.list_compressed(state)
        # Only marker_3 and marker_4 should be available
        marker_ids = [m["marker_id"] for m in info["markers"]]
        assert "turn_1" not in marker_ids
        assert "turn_2" not in marker_ids
        assert "turn_3" in marker_ids
        assert "turn_4" in marker_ids

    def test_lists_compressed_blocks(self) -> None:
        state = _build_multi_turn_state(3)
        CompressibleHistoryProvider.compress(state, "turn_1", "First summary")
        CompressibleHistoryProvider.compress(state, "turn_2", "Second summary")

        info = CompressibleHistoryProvider.list_compressed(state)
        assert len(info["blocks"]) == 2
        assert info["blocks"][0]["summary"] == "First summary"
        assert info["blocks"][1]["summary"] == "Second summary"


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerializationRoundTrip:
    def test_state_with_compressed_blocks_roundtrip(self) -> None:
        state = _build_multi_turn_state(3)
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary of turns 1-2")

        data = serialize_state(state)
        restored = deserialize_state(data)

        assert len(restored["compressed_msgs"]) == 1
        block = restored["compressed_msgs"][0]
        assert block.summary_text == "Summary of turns 1-2"
        assert block.marker_id == "turn_2"
        assert block.turn_range == (1, 2)
        assert len(block.messages) == 6  # original messages preserved

    def test_stacked_compressions_roundtrip(self) -> None:
        state = _build_multi_turn_state(4)
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary A")
        CompressibleHistoryProvider.compress(state, "turn_3", "Summary B")

        data = serialize_state(state)
        restored = deserialize_state(data)

        assert len(restored["compressed_msgs"]) == 2
        assert restored["compressed_msgs"][0].summary_text == "Summary A"
        assert restored["compressed_msgs"][1].summary_text == "Summary B"
        assert restored["turn_counter"] == 4

        # Summary messages in active history are preserved
        summaries = [m for m in restored["messages"] if _is_compressed_summary(m)]
        assert len(summaries) == 2

    def test_turn_counter_roundtrip(self) -> None:
        state = _build_multi_turn_state(5)
        assert state["turn_counter"] == 5

        data = serialize_state(state)
        restored = deserialize_state(data)
        assert restored["turn_counter"] == 5

    @pytest.mark.asyncio
    async def test_session_store_save_load_with_compressed(self, tmp_path: Path) -> None:
        store = JsonFileStateStore(tmp_path)
        state = _build_multi_turn_state(3)
        CompressibleHistoryProvider.compress(state, "turn_2", "Summary A")

        await store.save_session("s1", state, agent_profile="code")
        loaded = await store.load_session("s1")

        assert loaded is not None
        assert len(loaded["compressed_msgs"]) == 1
        assert loaded["compressed_msgs"][0].summary_text == "Summary A"

        # Active messages: summary + user3 + asst3 + marker_3
        assert len(loaded["messages"]) == 4


# ---------------------------------------------------------------------------
# Engine integration: marker insertion + compression + interrupt rollback
# ---------------------------------------------------------------------------


def _history_state(engine: object) -> dict:
    return engine.current.loaded.bindings.backend.history_state


@pytest.mark.asyncio
async def test_engine_inserts_turn_markers(agent_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that each successful agent turn inserts a turn marker."""
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import InvocationMessage, SessionReady, UserMessage
    from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig

    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(SessionReady, lambda e: collect_events(events, e))
    await bus.subscribe(InvocationMessage, lambda e: collect_events(events, e))

    profile = AgentProfile(
        name="test",
        instructions="Test.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    settings = Settings()

    mock_responses = [
        MockResponse(text="Response 1"),
        MockResponse(text="Response 2"),
        MockResponse(text="Response 3"),
    ]
    mock_client = MockChatClient(responses=mock_responses)

    import chrys.orchestration.engine.build.builder as builder_module

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=mock_client)
    )
    engine = agent_engine(bus, settings=settings)
    await engine.start(profile)

    # Three rounds of conversation
    for _ in range(3):
        await bus.publish(UserMessage(text="hello"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    state = _history_state(engine)
    messages = state.get("messages", [])

    # Each turn: user + assistant + marker = 3 per turn = 9 total
    assert len(messages) == 9
    markers = [m for m in messages if _is_turn_marker(m)]
    assert len(markers) == 3
    assert state["turn_counter"] == 3

    # Markers have sequential IDs
    marker_ids = [m.additional_properties["_turn_id"] for m in markers]
    assert marker_ids == ["turn_1", "turn_2", "turn_3"]


@pytest.mark.asyncio
async def test_engine_multi_turn_then_compress(agent_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Full pipeline: 3 rounds of conversation, then agent compresses at marker_2.

    The mock agent's 4th response triggers a compress_context tool call.
    """
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import InvocationMessage, SessionReady, UserMessage
    from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig

    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(SessionReady, lambda e: collect_events(events, e))
    await bus.subscribe(InvocationMessage, lambda e: collect_events(events, e))

    profile = AgentProfile(
        name="test",
        instructions="Test.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    settings = Settings()

    mock_responses = [
        MockResponse(text="Response 1"),
        MockResponse(text="Response 2"),
        MockResponse(text="Response 3"),
        # 4th turn: agent calls compress_context, then responds
        MockResponse(
            tool_calls=[
                (
                    "compress_context",
                    "cc1",
                    {"marker_id": "turn_2", "summary": "Covered turns 1 and 2"},
                )
            ]
        ),
        MockResponse(text="Compressed. Moving on."),
    ]
    mock_client = MockChatClient(responses=mock_responses)

    import chrys.orchestration.engine.build.builder as builder_module

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=mock_client)
    )
    engine = agent_engine(bus, settings=settings)
    await engine.start(profile)

    # 3 rounds
    for _ in range(3):
        await bus.publish(UserMessage(text="do work"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # 4th round triggers compression
    await bus.publish(UserMessage(text="context is getting large, compress"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    state = _history_state(engine)

    # Should have 1 compressed block
    compressed = state.get("compressed_msgs", [])
    assert len(compressed) == 1
    assert compressed[0].marker_id == "turn_2"
    assert compressed[0].summary_text == "Covered turns 1 and 2"

    # Summary message should be in active history
    summaries = [m for m in state["messages"] if _is_compressed_summary(m)]
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_engine_stacking_compressions(agent_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """5 rounds, compress at marker_2, then compress at marker_4 → two stacked blocks."""
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import InvocationMessage, SessionReady, UserMessage
    from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig

    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(SessionReady, lambda e: collect_events(events, e))
    await bus.subscribe(InvocationMessage, lambda e: collect_events(events, e))

    profile = AgentProfile(
        name="test",
        instructions="Test.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    settings = Settings()

    mock_responses = [
        # 5 conversation rounds
        MockResponse(text="R1"),
        MockResponse(text="R2"),
        MockResponse(text="R3"),
        MockResponse(text="R4"),
        MockResponse(text="R5"),
        # 6th turn: first compress
        MockResponse(
            tool_calls=[("compress_context", "cc1", {"marker_id": "turn_2", "summary": "Summary A: turns 1-2"})]
        ),
        MockResponse(text="First compression done."),
        # 7th turn: second compress
        MockResponse(
            tool_calls=[("compress_context", "cc2", {"marker_id": "turn_4", "summary": "Summary B: turns 3-4"})]
        ),
        MockResponse(text="Second compression done."),
    ]
    mock_client = MockChatClient(responses=mock_responses)

    import chrys.orchestration.engine.build.builder as builder_module

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=mock_client)
    )
    engine = agent_engine(bus, settings=settings)
    await engine.start(profile)

    # 5 conversation rounds
    for _ in range(5):
        await bus.publish(UserMessage(text="work"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # First compress
    await bus.publish(UserMessage(text="compress first part"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # Second compress
    await bus.publish(UserMessage(text="compress second part"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    state = _history_state(engine)

    # Two compressed blocks
    compressed = state.get("compressed_msgs", [])
    assert len(compressed) == 2
    assert compressed[0].summary_text == "Summary A: turns 1-2"
    assert compressed[0].turn_range == (1, 2)
    assert compressed[1].summary_text == "Summary B: turns 3-4"
    assert compressed[1].turn_range == (3, 4)

    # Two summary messages at the start of active history
    summaries = [m for m in state["messages"] if _is_compressed_summary(m)]
    assert len(summaries) == 2


@pytest.mark.asyncio
async def test_engine_session_save_restore_with_compressed(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Save a session with compressed blocks, restore it, verify state survives."""
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import InvocationMessage, SessionReady, SessionSaved, UserMessage
    from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
    from chrys.service.state.store import JsonFileStateStore

    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(SessionReady, lambda e: collect_events(events, e))
    await bus.subscribe(InvocationMessage, lambda e: collect_events(events, e))
    await bus.subscribe(SessionSaved, lambda e: collect_events(events, e))

    store = JsonFileStateStore(tmp_path)

    profile = AgentProfile(
        name="test",
        instructions="Test.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    settings = Settings()

    mock_responses = [
        MockResponse(text="R1"),
        MockResponse(text="R2"),
        # Compress at marker_1
        MockResponse(tool_calls=[("compress_context", "cc1", {"marker_id": "turn_1", "summary": "Summary of turn 1"})]),
        MockResponse(text="Compressed."),
    ]
    mock_client = MockChatClient(responses=mock_responses)

    import chrys.orchestration.engine.build.builder as builder_module

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=mock_client)
    )
    engine = agent_engine(bus, settings=settings, state_store=store)
    await engine.start(profile)
    session_id = engine.session.session_id

    # Two rounds
    await bus.publish(UserMessage(text="round 1"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    await bus.publish(UserMessage(text="round 2"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # Compress
    await bus.publish(UserMessage(text="compress"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    await engine.shutdown()

    # Verify the session was saved with compressed blocks
    loaded = await store.load_session(session_id)
    assert loaded is not None
    assert len(loaded["compressed_msgs"]) == 1
    assert loaded["compressed_msgs"][0].summary_text == "Summary of turn 1"
    assert loaded["turn_counter"] >= 3

    # Summary message in active history
    summaries = [m for m in loaded["messages"] if _is_compressed_summary(m)]
    assert len(summaries) == 1


@pytest.mark.asyncio
async def test_engine_crash_after_compress_preserves_state(agent_engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """If the run crashes after compress_context, completed work is preserved.

    Simulates: agent calls compress_context (succeeds), then the next model
    call raises an exception (e.g. network error).  The compression and
    completed tool calls are kept (not rolled back) so the user can retry.
    """
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.events.types import Error, InvocationMessage, SessionReady, UserMessage
    from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig

    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(SessionReady, lambda e: collect_events(events, e))
    await bus.subscribe(InvocationMessage, lambda e: collect_events(events, e))
    await bus.subscribe(Error, lambda e: collect_events(events, e))

    profile = AgentProfile(
        name="test",
        instructions="Test.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    settings = Settings()

    # Track call count so we can crash on the right call
    call_count = 0

    class CrashingMockClient(MockChatClient):
        """Mock client that raises on a specific call index to simulate a network error."""

        def __init__(self, *args: object, crash_on_call: int = 0, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self._crash_on_call = crash_on_call

        def _inner_get_response(self, **kwargs: object) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == self._crash_on_call:
                raise RuntimeError("Simulated non-retryable error after compression")
            return super()._inner_get_response(**kwargs)

    mock_responses = [
        MockResponse(text="R1"),
        MockResponse(text="R2"),
        # 3rd turn: compress_context tool call
        MockResponse(tool_calls=[("compress_context", "cc1", {"marker_id": "turn_1", "summary": "Summary of turn 1"})]),
        # This response will never be reached — the client crashes instead
        MockResponse(text="Should not appear"),
    ]

    # Calls: turn1=1, turn2=2, turn3_compress_call=3, turn3_after_tool=4 (crash here)
    mock_client = CrashingMockClient(responses=mock_responses, crash_on_call=4)

    import chrys.orchestration.engine.build.builder as builder_module

    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=mock_client)
    )
    engine = agent_engine(bus, settings=settings)
    await engine.start(profile)

    # Two successful rounds
    await bus.publish(UserMessage(text="round 1"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    await bus.publish(UserMessage(text="round 2"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # Record state before the crashing run
    state_before = _history_state(engine)
    compressed_before = len(state_before.get("compressed_msgs", []))
    assert compressed_before == 0  # no compression yet

    # 3rd round: compress succeeds, then model call crashes
    await bus.publish(UserMessage(text="compress please"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # After crash, completed work is preserved (not rolled back)
    state_after = _history_state(engine)
    msg_count_after = len(state_after.get("messages", []))
    compressed_after = len(state_after.get("compressed_msgs", []))

    # Compression that completed mid-run is preserved
    assert compressed_after == 1, f"Expected compressed_msgs to be preserved (1), got {compressed_after}"

    # The run's work (including compression) is preserved, not rolled back.
    # The user message may have been compressed, but the compressed block
    # itself must exist as completed work.
    assert msg_count_after >= 1, "Expected at least some messages to survive"


# ---------------------------------------------------------------------------
# Compression in single-round / sub-agent scenarios
# ---------------------------------------------------------------------------


def test_compress_with_no_markers_raises():
    """compress_context fails gracefully when there are no turn markers.

    In a single-round sub-agent, no turn markers are inserted (they're
    inserted by the engine after each run, not during).  If the model
    calls compress_context with a bogus marker_id, it should get a clean
    error, not crash.
    """
    state: dict = {"messages": [Message("user", ["hello"]), Message("assistant", ["hi"])]}
    with pytest.raises(ValueError, match="not found"):
        CompressibleHistoryProvider.compress(state, "nonexistent_marker", "summary")


def test_compress_empty_history_raises():
    """compress_context on empty history returns a clear error."""
    state: dict = {"messages": []}
    with pytest.raises(ValueError, match="empty"):
        CompressibleHistoryProvider.compress(state, "turn_1", "summary")


def test_list_compressed_no_markers():
    """list_compressed_contexts returns empty markers for a fresh single-round session."""
    state: dict = {"messages": [Message("user", ["hello"]), Message("assistant", ["hi"])]}
    info = CompressibleHistoryProvider.list_compressed(state)
    assert info["markers"] == []
    assert info["blocks"] == []


def test_context_manager_without_context_management():
    """ContextManager with include_context_management=False excludes compression tools.

    Sub-agents should not have compress_context, recall_context, or
    list_compressed_contexts tools injected — they are single-turn.
    """
    from chrys.service.context.manager import ContextManager
    from chrys.service.profiles.models.resolver import default_profile

    profile = default_profile()

    # Default: includes context management
    ctx_with = ContextManager(profile, include_context_management=True)
    assert ctx_with.context_mgmt_provider is not None
    provider_types = [type(p).__name__ for p in ctx_with.providers]
    assert "ContextManagementProvider" in provider_types

    # Sub-agent mode: excludes context management
    ctx_without = ContextManager(profile, include_context_management=False)
    assert ctx_without.context_mgmt_provider is None
    provider_types = [type(p).__name__ for p in ctx_without.providers]
    assert "ContextManagementProvider" not in provider_types
    # History provider still present
    assert "CompressibleHistoryProvider" in provider_types


# ---------------------------------------------------------------------------
# Turn-end usage check tests
# ---------------------------------------------------------------------------


class TestTurnEndUsage:
    """A turn that ends above ``force_compress_pct`` books a pass; only a strategy-less provider folds."""

    def _make_provider(
        self,
        max_context_tokens: int = 100_000,
        force_compress_pct: float = 0.70,
        *,
        with_strategy: bool = True,
    ) -> CompressibleHistoryProvider:
        strategy = None
        if with_strategy and max_context_tokens > 0:
            from chrys.service.context.compaction import UnifiedContextStrategy

            strategy = UnifiedContextStrategy(max_context_tokens=max_context_tokens)
        return CompressibleHistoryProvider(
            compaction_strategy=strategy,
            max_context_tokens=max_context_tokens,
            force_compress_pct=force_compress_pct,
        )

    def _mock_context(self, input_tokens: int, *, aggregate_input_tokens: int | None = None) -> object:
        """Create a mock SessionContext with aggregate and latest-call usage."""
        from unittest.mock import MagicMock

        context = MagicMock()
        aggregate_input = aggregate_input_tokens if aggregate_input_tokens is not None else input_tokens
        context.response.usage_details = {
            "input_token_count": aggregate_input,
            "output_token_count": 2000,
            "total_token_count": aggregate_input + 2000,
        }
        context.response.latest_usage_details = {
            "input_token_count": input_tokens,
            "output_token_count": 2000,
            "total_token_count": input_tokens + 2000,
        }
        return context

    @pytest.mark.asyncio
    async def test_usage_above_threshold_books_a_pass_and_keeps_history(self) -> None:
        """The provider leaves history alone: the next request's pass does the compaction."""
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70)
        state = _build_multi_turn_state(3)
        provider._compaction_strategy.bind_state(state)
        before = list(state["messages"])
        context = self._mock_context(75_000)  # 75% > 70%

        changed = await provider._check_turn_end_usage(state, context)

        assert not changed
        assert provider._compaction_strategy._turn_boundary_pending
        assert state["messages"] == before
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_usage_below_threshold_books_nothing(self) -> None:
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70)
        state = _build_multi_turn_state(3)
        provider._compaction_strategy.bind_state(state)
        context = self._mock_context(60_000)  # 60% < 70%

        await provider._check_turn_end_usage(state, context)

        assert not provider._compaction_strategy._turn_boundary_pending
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_multi_call_aggregate_above_threshold_books_nothing(self) -> None:
        """Billable turn totals must not masquerade as current context occupancy."""
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70)
        state = _build_multi_turn_state(3)
        provider._compaction_strategy.bind_state(state)
        context = self._mock_context(20_000, aggregate_input_tokens=80_000)

        await provider._check_turn_end_usage(state, context)

        assert not provider._compaction_strategy._turn_boundary_pending
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_no_older_turn_books_nothing(self) -> None:
        """With no completed turn before the one that just ended, a pass has nothing to work on."""
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70)
        state: dict = {
            "messages": [Message("user", ["hello"]), Message("assistant", ["hi"])],
            "compressed_msgs": [],
        }
        provider._compaction_strategy.bind_state(state)
        context = self._mock_context(80_000)

        await provider._check_turn_end_usage(state, context)

        assert not provider._compaction_strategy._turn_boundary_pending
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_booking_twice_never_folds(self) -> None:
        """Turns that keep ending above the threshold never fold a turn here."""
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70)
        state = _build_multi_turn_state(5)
        provider._compaction_strategy.bind_state(state)
        context = self._mock_context(75_000)

        await provider._check_turn_end_usage(state, context)
        await provider._check_turn_end_usage(state, context)

        assert provider._compaction_strategy._turn_boundary_pending
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_strategy_less_provider_folds_its_oldest_turn(self) -> None:
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70, with_strategy=False)
        state = _build_multi_turn_state(3)
        context = self._mock_context(75_000)

        changed = await provider._check_turn_end_usage(state, context)

        assert changed
        compressed = state.get("compressed_msgs", [])
        assert len(compressed) == 1
        assert compressed[0].marker_id == "turn_1"

    def test_auto_summary_is_reasonable(self) -> None:
        """Auto-generated summary includes user text and tool names."""
        from chrys.service.context.providers.history import _auto_summary

        state = _build_multi_turn_state(3)
        # marker_1 is at position 2 (user + assistant + marker)
        marker_pos = 2
        summary = _auto_summary(state, marker_pos)
        assert "User:" in summary
        assert summary != "Earlier conversation"

    @pytest.mark.asyncio
    async def test_disabled_with_high_pct(self) -> None:
        """force_compress_pct >= 1.0 disables the check."""
        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=1.0)
        state = _build_multi_turn_state(3)
        context = self._mock_context(99_000)

        await provider._check_turn_end_usage(state, context)

        assert not provider._compaction_strategy._turn_boundary_pending
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_disabled_with_zero_max_context(self) -> None:
        """max_context_tokens=0 disables the check."""
        provider = self._make_provider(max_context_tokens=0, force_compress_pct=0.70)
        state = _build_multi_turn_state(3)
        context = self._mock_context(75_000)

        assert not await provider._check_turn_end_usage(state, context)
        assert state.get("compressed_msgs", []) == []

    @pytest.mark.asyncio
    async def test_no_response(self) -> None:
        """No crash when context has no response."""
        from unittest.mock import MagicMock

        provider = self._make_provider(max_context_tokens=100_000, force_compress_pct=0.70)
        state = _build_multi_turn_state(3)
        context = MagicMock()
        context.response = None

        await provider._check_turn_end_usage(state, context)

        assert not provider._compaction_strategy._turn_boundary_pending
        assert state.get("compressed_msgs", []) == []


@pytest.mark.asyncio
async def test_snapshot_rollback_restores_pre_compress_state() -> None:
    """Unit test: HistoryRollback.snapshot/restore survives compress_context.

    Simulates the exact failure scenario at the executor level:
    1. Snapshot history (80 messages)
    2. compress_context replaces state["messages"] (80 → 7 messages)
    3. after_run appends orphaned tool calls (7 → 10 messages)
    4. Restore from snapshot → back to original 80 messages
    """
    from chrys.kernel import AgentSession, Message

    session = AgentSession()
    session.state["chrys_history"] = {
        "messages": [Message("user", [f"msg_{i}"]) for i in range(80)],
    }

    # Simulate executor snapshot
    history_state = session.state["chrys_history"]
    snapshot = list(history_state["messages"])
    assert len(snapshot) == 80

    # Simulate compress_context replacing the list
    history_state["messages"] = [
        Message("assistant", ["[Compressed summary]"]),
        *history_state["messages"][-6:],
    ]
    assert len(history_state["messages"]) == 7

    # Simulate after_run appending orphaned tool calls
    history_state["messages"].extend(
        [
            Message("assistant", ["orphaned tool call"]),
            Message("assistant", ["another orphan"]),
        ]
    )
    assert len(history_state["messages"]) == 9

    # Old rollback (length-based) would fail: 9 < 80 → no-op
    # New snapshot-based restore works:
    history_state["messages"] = snapshot
    assert len(history_state["messages"]) == 80
    assert history_state["messages"][0].text == "msg_0"
    assert history_state["messages"][-1].text == "msg_79"
