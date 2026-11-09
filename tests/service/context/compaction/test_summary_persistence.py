# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Phase-1 replacements survive wire views, next turns and persisted sessions."""

from __future__ import annotations

import asyncio
from copy import copy
from unittest.mock import patch

import pytest

from chrys.foundation.retry import restore_message_properties, snapshot_message_properties
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import EXCLUDED_KEY, Agent, AgentSession, Content, Message
from chrys.service.context.compaction import _group_messages_by_id
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.state.store import JsonFileStateStore
from tests.service.context.compaction._compaction_helpers import (
    _build_multi_turn,
    _estimate_tokens,
    _make_strategy,
    _turn_marker,
)


def _summaries(messages: list[Message]) -> list[Message]:
    return [message for message in messages if message.text.startswith("[Tool call:")]


@pytest.mark.parametrize("stream", [False, True])
async def test_agent_compaction_summary_survives_next_turn_and_disk_restore(tmp_path, stream) -> None:
    originals = _build_multi_turn(1, groups_per_turn=2, result_size=8000)
    strategy = _make_strategy(max_context_tokens=_estimate_tokens(originals) + 100, trigger_pct=0.8, target_pct=0.3)
    provider = CompressibleHistoryProvider(compaction_strategy=strategy)
    state = {"messages": originals}
    session = AgentSession()
    session.state[provider.source_id] = state
    client = MockChatClient(responses=[MockResponse(text="done"), MockResponse(text="next done")])
    agent = Agent(client=client, context_providers=[provider])
    async with agent:
        if stream:
            response_stream = agent.run("next request", session=session, stream=True, compaction_strategy=strategy)
            async for _ in response_stream:
                pass
            await response_stream.get_final_response()
        else:
            await agent.run("next request", session=session, compaction_strategy=strategy)
        summaries = _summaries(state["messages"])
        assert summaries
        expected = [message.text for message in summaries]
        expected_ids = [read_analytics_item_id(message.additional_properties) for message in summaries]
        assert [message.text for message in _summaries(client.call_history[0][0])] == expected

        await agent.run("another request", session=session, compaction_strategy=strategy)
        assert [message.text for message in _summaries(client.call_history[-1][0])] == expected
        assert [read_analytics_item_id(m.additional_properties) for m in _summaries(state["messages"])] == expected_ids

    store = JsonFileStateStore(tmp_path)
    await store.save_session("summary", state, agent_profile="Code")
    restored = await store.load_session("summary")
    assert restored is not None
    fresh_provider = CompressibleHistoryProvider(compaction_strategy=_make_strategy())
    visible = await fresh_provider.get_messages(None, state=restored)
    assert [message.text for message in _summaries(visible)] == expected
    assert [read_analytics_item_id(m.additional_properties) for m in _summaries(visible)] == expected_ids
    assert all(not message.additional_properties.get(EXCLUDED_KEY) for message in visible)
    replay = await store.load_session_raw("summary")
    assert replay is not None
    assert [
        content["text"]
        for message in replay
        for content in message["contents"]
        if content.get("type") == "text" and content.get("text", "").startswith("[Tool call:")
    ] == expected


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
async def test_summary_commits_before_provider_failure_or_cancellation(tmp_path, error_type) -> None:
    originals = _build_multi_turn(1, groups_per_turn=2, result_size=8000)
    strategy = _make_strategy(max_context_tokens=_estimate_tokens(originals) + 100, trigger_pct=0.8, target_pct=0.3)
    provider = CompressibleHistoryProvider(compaction_strategy=strategy)
    state = {"messages": originals}
    session = AgentSession()
    session.state[provider.source_id] = state
    client = MockChatClient(responses=[MockResponse(text="recovered")])
    agent = Agent(client=client, context_providers=[provider])
    async with agent:
        with (
            patch.object(client, "_next_response", autospec=True, side_effect=error_type("provider stopped")),
            pytest.raises(error_type),
        ):
            await agent.run("next request", session=session, compaction_strategy=strategy)
        summaries = _summaries(state["messages"])
        assert summaries
        expected = [message.text for message in summaries]
        store = JsonFileStateStore(tmp_path)
        await store.save_session("failed", state, agent_profile="Code")
        restored = await store.load_session("failed")
        assert restored is not None
        fresh_provider = CompressibleHistoryProvider(compaction_strategy=_make_strategy())
        visible = await fresh_provider.get_messages(None, state=restored)
        assert [message.text for message in _summaries(visible)] == expected
        await agent.run("retry request", session=session, compaction_strategy=strategy)
        assert [message.text for message in _summaries(client.call_history[-1][0])] == expected


async def test_new_turn_summary_cannot_bind_an_old_reused_positional_group_id() -> None:
    originals = _build_multi_turn(1, groups_per_turn=10, result_size=8000)
    strategy = _make_strategy(max_context_tokens=_estimate_tokens(originals) + 100, trigger_pct=0.8, target_pct=0.3)
    second_turn = Message("user", ["second turn"])
    originals.extend([_turn_marker(1), second_turn])
    state = {"messages": originals}
    strategy.bind_state(state)
    provider = CompressibleHistoryProvider(compaction_strategy=strategy)

    async def compact_view() -> list[Message]:
        visible = await provider.get_messages(None, state=state)
        wire = [copy(message) for message in visible]
        for view in wire:
            view.contents = list(view.contents)
        assert await strategy(wire)
        strategy.persist_exclusions_to_state(state["messages"])
        return wire

    await compact_view()
    old_keys = set(strategy._summary_cache)
    second_messages = _build_multi_turn(1, groups_per_turn=3, result_size=50000)[1:]
    for message in second_messages:
        if message.role == "tool":
            content = message.contents[0]
            content.result = "NEW " + content.result
            content.items = [Content.from_text(content.result)]
    state["messages"].extend([*second_messages, Message("user", ["third turn"])])
    wire = await compact_view()

    new_summaries = [message for message in _summaries(state["messages"]) if "NEW " in message.text]
    assert new_summaries
    assert old_keys <= strategy._summary_cache.keys()
    second_start = state["messages"].index(second_turn)
    assert all(state["messages"].index(message) > second_start for message in new_summaries)

    assert await strategy._emergency_compress_oldest_turn(
        wire, usage_pct=0.9, tokens_before=_estimate_tokens(wire)
    ) == (
        True,
        True,
    )
    assert all(not message.additional_properties.get(EXCLUDED_KEY) for message in new_summaries)
    visible = await provider.get_messages(None, state=state)
    assert [message.text for message in _summaries(visible)] == [message.text for message in new_summaries]

    # A projection containing only the new turn must not acquire an unrelated
    # old fold just because its group labels were reused.
    new_projection = [copy(message) for message in new_summaries]
    strategy._compression.reinject_summaries(new_projection)
    assert [message.text for message in new_projection] == [message.text for message in new_summaries]


async def test_summary_insertion_uses_anchors_and_occurrence_identity_not_message_ids() -> None:
    owned = _build_multi_turn(2, groups_per_turn=2, result_size=4000)
    strategy = _make_strategy(max_context_tokens=_estimate_tokens(owned) + 100, trigger_pct=0.8, target_pct=0.6)
    # Fresh wrapper/list views retain only content identity across the wire boundary.
    wire = []
    for original in owned:
        view = copy(original)
        view.contents = list(original.contents)
        wire.append(view)
    assert await strategy(wire)
    summaries = _summaries(wire)
    assert summaries
    # A positional message_id collision must neither hide a missing summary
    # nor cause a duplicate once the real summary is already present.
    owned[0].message_id = summaries[0].message_id
    strategy.persist_exclusions_to_state(owned)
    assert [message.text for message in _summaries(owned)] == [message.text for message in summaries]
    first = _summaries(owned)[0]
    first.message_id = "msg_0"
    strategy.persist_exclusions_to_state(owned)
    strategy._exclusions.reinject_cached_summaries(owned)
    assert len(_summaries(owned)) == len(summaries)


async def test_phase2_removal_does_not_resurrect_phase1_summaries() -> None:
    owned = _build_multi_turn(2, groups_per_turn=2, result_size=4000)
    wire = list(owned)
    strategy = _make_strategy(max_context_tokens=_estimate_tokens(owned) + 100, trigger_pct=0.8, target_pct=0.01)
    assert await strategy(wire)
    assert not strategy._summary_cache
    strategy.persist_exclusions_to_state(owned)
    assert not _summaries(owned)


def test_phase2_exclusion_writes_through_to_summary_already_committed_to_state() -> None:
    owned = _build_multi_turn(1, groups_per_turn=1, result_size=4000)
    _estimate_tokens(owned)
    wire = [copy(message) for message in owned]
    strategy = _make_strategy()
    strategy.bind_state({"messages": owned})
    group_id = wire[1].additional_properties["_group"]["id"]
    group = _group_messages_by_id(wire)[group_id]
    assert strategy._compact_group(wire, group_id, group)
    stored = _summaries(owned)[0]
    stored.message_id = "reassigned_positional_id"
    assert strategy._remove_group(wire, group_id, group)
    assert stored.additional_properties[EXCLUDED_KEY]
    assert not strategy._summary_cache


def test_retry_rollback_discards_attempt_summary_and_its_cache_entry() -> None:
    owned = _build_multi_turn(1, groups_per_turn=1, result_size=4000)
    _estimate_tokens(owned)
    state = {"messages": owned}
    strategy = _make_strategy()
    strategy.bind_state(state)
    saved_messages = list(owned)
    saved_properties = snapshot_message_properties(saved_messages)
    snapshot = strategy.snapshot_retry_state()
    wire = [copy(message) for message in owned]
    group_id = wire[1].additional_properties["_group"]["id"]
    assert strategy._compact_group(wire, group_id, _group_messages_by_id(wire)[group_id])
    assert strategy._summary_cache
    assert _summaries(owned)

    # The HistoryRollback contract restores owned messages/properties before
    # its strategy participant. An aborted attempt must leave no orphan cache.
    state["messages"] = saved_messages
    restore_message_properties(saved_messages, saved_properties)
    strategy.restore_retry_state(snapshot)
    assert not strategy._summary_cache
    strategy.persist_exclusions_to_state(state["messages"])
    assert not _summaries(state["messages"])
    assert all(not message.additional_properties.get(EXCLUDED_KEY) for message in state["messages"])
