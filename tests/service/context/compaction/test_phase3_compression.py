# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for Phase 3 text-only history compression and the compressed-context cache."""

import gc
import weakref
from collections.abc import Callable

import pytest

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.retry import restore_message_properties, snapshot_message_properties
from chrys.kernel import (
    EXCLUDE_REASON_KEY,
    EXCLUDED_KEY,
    AgentSession,
    ChatResponse,
    Message,
    SessionContext,
    included_token_count,
    project_included_messages,
)
from chrys.kernel.loop import _wire_message_view
from chrys.service.context.compaction import (
    _REASON_COMPRESSION,
    _REASON_CURRENT_TURN_DROP,
    CompressInfo,
    PreCompactInfo,
)
from chrys.service.context.providers.history import (
    PRE_OUTPUT_HISTORY_LEN_STATE_KEY,
    CompressibleHistoryProvider,
)
from chrys.service.session.message_metadata import LAST_ASSISTANT_CREATED_AT_STATE_KEY, MESSAGE_CREATED_AT_KEY
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _async_appender,
    _build_tool_group,
    _estimate_tokens,
    _folded_text_state,
    _make_strategy,
    _markerless_wire,
    _status_marker,
    _user,
    _wire_view,
)


@pytest.mark.parametrize("wrap", [lambda msg: msg, _wire_message_view], ids=["state_originals", "wire_views"])
async def test_text_only_history_emergency_compresses_completed_turns_before_request(
    wrap: Callable[[Message], Message],
) -> None:
    """Text-heavy history with markers must be folded before the next model call.

    The legacy behaviour is still covered by ``test_no_tool_groups_no_change``:
    without bound history state/markers there is nothing safe to fold.  This
    test exercises the real session shape, where completed turns are present
    in provider state but filtered out of the wire list — once with the state
    originals on the wire and once through the kernel's real per-call VIEWS.
    ``_wire_message_view`` mints a fresh wrapper + fresh contents list for
    every outgoing message, so wrapper identity and ``id(msg.contents)``
    both miss state; only the content OBJECTS are shared.  Phase 3 must
    still fold completed turns, and same-run reinjection must recognise
    brand-new views of the folded originals on the next call.
    """
    state = _folded_text_state(5, fill=2000)

    wire_source = _markerless_wire(state)
    wire_source.append(_user("Current turn"))
    messages = [wrap(msg) for msg in wire_source]
    tokens_before = _estimate_tokens(messages)
    compressed_events: list[CompressInfo] = []
    precompact_events: list[PreCompactInfo] = []

    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
        on_compress=_async_appender(compressed_events),
        on_pre_compact=_async_appender(precompact_events),
    )
    strategy.bind_state(state)

    changed = await strategy(messages)

    assert changed
    assert precompact_events
    triggers = [info.trigger for info in precompact_events]
    assert "phase3" in triggers
    assert "force" not in triggers
    assert compressed_events
    assert all(info.source == "auto" for info in compressed_events)
    assert state["compressed_msgs"]
    projected = project_included_messages(messages)
    assert any(msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY for msg in projected)
    assert included_token_count(messages) < tokens_before
    assert strategy._usage_pct(included_token_count(messages)) <= strategy.target_pct

    # The next call gets the same originals again (or FRESH views of them,
    # per-call wire semantics): the folded ones carry their exclusion through
    # the shared props dict, and the cached summary must reinject against
    # copies that share nothing but content objects with call one.
    second_call_messages = [wrap(msg) for msg in wire_source]
    assert not any(
        msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
        for msg in second_call_messages
    )

    await strategy(second_call_messages)

    assert any(
        msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
        for msg in project_included_messages(second_call_messages)
    )
    assert strategy._compressed_context_cache
    assert all(len(entry.folded_contents) > 0 for entry in strategy._compressed_context_cache.values())

    # The next bind is a run boundary and clears the cache outright.
    strategy.bind_state(state)
    assert not strategy._compressed_context_cache


async def test_retry_restore_silently_replays_published_phase3_folds_in_order() -> None:
    """Published completed-turn folds survive attempt rollback with stable blocks."""
    state = _folded_text_state(5, fill=2_000)

    baseline_messages = list(state["messages"])
    baseline_properties = snapshot_message_properties(baseline_messages)
    current_user = _user("Current turn")
    wire = [
        _wire_message_view(message)
        for message in [
            *(
                message
                for message in baseline_messages
                if message.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.TURN
            ),
            current_user,
        ]
    ]
    tokens_before = _estimate_tokens(wire)
    compressed_events: list[CompressInfo] = []
    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.20,
        on_compress=_async_appender(compressed_events),
    )
    strategy.bind_state(state)
    retry_snapshot = strategy.snapshot_retry_state()

    assert await strategy(wire)
    published_blocks = list(state["compressed_msgs"])
    published_ids = [block.compressed_context_id for block in published_blocks]
    assert len(published_ids) >= 2  # Exercise ordered replay, not only one fold.
    assert [event.compressed_context_id for event in compressed_events] == published_ids

    # Mirror HistoryRollback.restore: restore the pre-input baseline first,
    # then let the strategy silently preserve committed completed-turn folds.
    state["messages"] = list(baseline_messages)
    state["compressed_msgs"].clear()
    state.pop(PRE_OUTPUT_HISTORY_LEN_STATE_KEY, None)
    restore_message_properties(state["messages"], baseline_properties)
    strategy.restore_retry_state(retry_snapshot)

    restored_blocks = state["compressed_msgs"]
    assert restored_blocks == published_blocks
    assert all(restored is published for restored, published in zip(restored_blocks, published_blocks, strict=True))
    assert [block.compressed_context_id for block in restored_blocks] == published_ids
    assert len(compressed_events) == len(published_ids)  # Replay emits no second ContextCompressed event.
    assert state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == len(state["messages"])


async def test_queued_compression_inserts_and_caches_summary_once_for_same_run_retry():
    first_user = _user("Turn one request")
    first_answer = _assistant_text("Turn one answer")
    state: dict = {
        "messages": [first_user, first_answer],
        "compressed_msgs": [],
        "turn_counter": 0,
    }
    marker_id = CompressibleHistoryProvider.insert_marker(state, 1)
    current_user = _user("Current turn")
    wire_source = [first_user, first_answer, current_user]
    first_wire = [_wire_message_view(message) for message in wire_source]
    strategy = _make_strategy(compaction_enabled=False)
    strategy.bind_state(state)
    compressed_context_id, _ = strategy.queue_compression(marker_id, "Turn one summary")

    assert await strategy(first_wire)

    first_projected = project_included_messages(first_wire)
    first_summaries = [
        message
        for message in first_projected
        if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
    ]
    assert len(first_summaries) == 1
    assert len(state["compressed_msgs"]) == 1
    assert compressed_context_id in strategy._compressed_context_cache
    assert all(
        message.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_COMPRESSION
        for message in (first_user, first_answer)
    )

    retry_wire = [_wire_message_view(message) for message in wire_source]
    assert not any(
        message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY for message in retry_wire
    )

    await strategy(retry_wire)

    retry_summaries = [
        message
        for message in project_included_messages(retry_wire)
        if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
    ]
    assert retry_summaries == first_summaries
    assert len(state["compressed_msgs"]) == 1


async def test_compressed_summary_cache_does_not_retain_folded_originals():
    """The summary cache holds WEAK identities: it never keeps the folded
    originals alive, and once they die its identity tiers match nothing —
    stale folded evidence can never misplace a summary into new work."""
    state = _folded_text_state(3, fill=2000)

    messages = _markerless_wire(state)
    messages.append(_user("Current turn"))
    tokens_before = _estimate_tokens(messages)
    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
    )
    strategy.bind_state(state)

    changed = await strategy(messages)

    assert changed
    assert strategy._compressed_context_cache
    folded = [msg for msg in messages if msg.additional_properties.get(EXCLUDE_REASON_KEY) == "cross_turn_compression"]
    assert folded
    message_refs = [weakref.ref(msg) for msg in folded]
    contents_refs = [weakref.ref(msg.contents) for msg in folded]

    del folded
    messages.clear()
    gc.collect()

    assert all(ref() is None for ref in message_refs), "cache must not retain folded originals"
    assert all(ref() is None for ref in contents_refs)

    # Dead tiers match nothing: a fresh per-call list of brand-new work
    # yields no insertion anchor, so the summary is not misplaced into it.
    fresh = [_user("brand new work")]
    strategy._reinject_compressed_context_summaries(fresh)
    assert all(msg.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.SUMMARY for msg in fresh)


async def test_text_only_history_emergency_does_not_persist_folded_anchor_onto_repeated_current_turn():
    """Folded old text must not structurally match and exclude repeated current input."""
    repeated_text = "repeat me " * 2000
    old_user = _user(repeated_text)
    old_answer = _assistant_text("old answer " + ("y " * 2000))
    current_user = _user(repeated_text)
    state: dict = {"messages": [old_user, old_answer], "compressed_msgs": [], "turn_counter": 0}
    CompressibleHistoryProvider.insert_marker(state, 1)

    messages = [old_user, old_answer, current_user]
    tokens_before = _estimate_tokens(messages)
    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
    )
    strategy.bind_state(state)

    changed = await strategy(messages)

    assert changed
    assert old_user.additional_properties.get(EXCLUDE_REASON_KEY) == "cross_turn_compression"
    assert current_user.additional_properties.get(EXCLUDED_KEY) is not True
    assert all(anchor.reason != _REASON_COMPRESSION for anchor in strategy._excluded_anchors)

    final = _assistant_text("final")
    state["messages"].extend([current_user, final])
    strategy.persist_exclusions_to_state(state["messages"])

    assert current_user.additional_properties.get(EXCLUDED_KEY) is not True
    assert final.additional_properties.get(EXCLUDED_KEY) is not True


async def test_text_only_history_emergency_skips_empty_markers_before_visible_turns():
    """Phase 3 must keep scanning when an old marker has no visible wire messages."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    CompressibleHistoryProvider.insert_marker(state, 1)
    state["messages"].append(_user("Large completed request " + ("x " * 2000)))
    state["messages"].append(_assistant_text("Large completed answer " + ("y " * 2000)))
    CompressibleHistoryProvider.insert_marker(state, 2)

    messages = _markerless_wire(state)
    messages.append(_user("Current turn"))
    tokens_before = _estimate_tokens(messages)
    compressed_events = []
    precompact_events: list[PreCompactInfo] = []

    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
        on_compress=_async_appender(compressed_events),
        on_pre_compact=_async_appender(precompact_events),
    )
    strategy.bind_state(state)

    changed = await strategy(messages)

    assert changed
    assert [info.trigger for info in precompact_events].count("phase3") == 1
    assert len(compressed_events) == 1
    assert compressed_events[0].summary != "Earlier conversation"
    assert not any("Earlier conversation" in (msg.text or "") for msg in state["messages"])
    assert included_token_count(messages) < tokens_before
    assert strategy._usage_pct(included_token_count(messages)) <= strategy.target_pct


async def test_text_only_history_emergency_preserves_nonvisible_state_markers():
    """Phase 3 must not delete real history markers when local history is omitted."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("old real context"))
    state["messages"].append(_assistant_text("old real answer"))
    CompressibleHistoryProvider.insert_marker(state, 1)

    messages = [_user("Current service-side continuation " + ("x " * 2000))]
    tokens_before = _estimate_tokens(messages)
    compressed_events = []
    precompact_events: list[PreCompactInfo] = []

    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
        on_compress=_async_appender(compressed_events),
        on_pre_compact=_async_appender(precompact_events),
    )
    strategy.bind_state(state)

    changed = await strategy(messages)

    assert not changed
    assert not compressed_events
    assert not precompact_events
    assert not state["compressed_msgs"]
    assert any(
        msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN for msg in state["messages"]
    )


async def test_text_only_history_emergency_preserves_after_run_timestamp_window():
    """Phase 3 state compression must not hide current assistant outputs from after_run stamping."""
    state = _folded_text_state(5, fill=2000)

    current_user = _user("Current turn")
    wire_messages = _markerless_wire(state)
    wire_messages.append(current_user)
    tokens_before = _estimate_tokens(wire_messages)

    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
    )
    provider = CompressibleHistoryProvider(compaction_strategy=strategy)
    session = AgentSession(session_id="timestamps")
    context = SessionContext(session_id="timestamps", input_messages=[current_user])

    await provider.before_run(agent=object(), session=session, context=context, state=state)
    changed = await strategy(wire_messages)
    assert changed

    final_assistant = _assistant_text("final answer")
    context._response = ChatResponse(messages=[final_assistant])
    await provider.after_run(agent=object(), session=session, context=context, state=state)

    assert final_assistant.additional_properties[MESSAGE_CREATED_AT_KEY]
    assert (
        session.state[LAST_ASSISTANT_CREATED_AT_STATE_KEY]
        == final_assistant.additional_properties[MESSAGE_CREATED_AT_KEY]
    )


async def test_text_only_history_emergency_records_floor_after_exhausting_markers():
    """Phase 3 must leave a metadata floor even when no completed marker survives."""
    state = _folded_text_state(2, fill=1000)

    messages = _markerless_wire(state)
    messages.append(_user("Current oversized turn " + ("z " * 8000)))
    tokens_before = _estimate_tokens(messages)

    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.001,
    )
    strategy.bind_state(state)

    changed = await strategy(messages)

    assert changed
    assert not any(
        msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN for msg in state["messages"]
    )
    assert state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == len(state["messages"])


async def test_text_only_history_emergency_prunes_cache_after_retry_rollback():
    """Rolled-back Phase 3 summaries must not be reinserted on retry."""
    state = _folded_text_state(5, fill=2000)

    snapshot_messages = list(state["messages"])
    snapshot_properties = snapshot_message_properties(snapshot_messages)
    snapshot_compressed_count = len(state["compressed_msgs"])

    first_messages = _markerless_wire(state)
    first_messages.append(_user("Current turn"))
    tokens_before = _estimate_tokens(first_messages)

    strategy = _make_strategy(
        max_context_tokens=tokens_before + 100,
        trigger_pct=0.85,
        target_pct=0.50,
    )
    strategy.bind_state(state)

    assert await strategy(first_messages)
    first_block_count = len(state["compressed_msgs"])
    assert first_block_count > 0
    assert strategy._compressed_context_cache

    state["messages"] = snapshot_messages
    del state["compressed_msgs"][snapshot_compressed_count:]
    restore_message_properties(snapshot_messages, snapshot_properties)

    retry_messages = _markerless_wire(state)
    retry_messages.append(_user("Current turn retry"))

    assert await strategy(retry_messages)
    projected_summaries = [
        msg
        for msg in project_included_messages(retry_messages)
        if msg.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
    ]
    assert len(projected_summaries) == len(state["compressed_msgs"]) == first_block_count


# ---------------------------------------------------------------------------
# Phase 3 clamp pins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_kind",
    [HistoryMarkerKind.INTERRUPTED, HistoryMarkerKind.AWAITING_SUB_AGENTS],
)
async def test_p3_clamp_skips_interior_marker_protects_current_work(status_kind: str):
    """§4.1 P3 clamp: a crash-leftover interior marker inside the current real
    turn is never folded — current-task work survives while the real previous
    turn still folds.  Health is STATUS_MARKERS-driven, so the paused
    awaiting-sub-agents cluster behaves identically."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("Turn 1 request " + "x " * 1500))
    state["messages"].append(_assistant_text("Turn 1 answer " + "y " * 1500))
    CompressibleHistoryProvider.insert_marker(state, 1)
    opener2 = _user("Turn 2 request")
    state["messages"].append(opener2)
    work2 = _build_tool_group("t2_c0", "search", "z" * 3000)
    state["messages"].extend(work2)
    state["messages"].append(_status_marker(status_kind))
    CompressibleHistoryProvider.insert_marker(state, 2)

    wire = _wire_view(state)
    total = _estimate_tokens(wire)

    compressed_events: list = []

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,  # unreachable: forces P3 to attempt every marker
        satisfied_pct=0.01,
        force_pct=0.01,
        on_compress=_async_appender(compressed_events),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    # Only turn 1 folded; the interior marker's candidate was clamped.
    assert [info.turn_range for info in compressed_events] == [(1, 1)]
    for msg in work2:
        assert msg.additional_properties.get(EXCLUDE_REASON_KEY) == _REASON_CURRENT_TURN_DROP, (
            "current-task work must reach Phase 4, never P3 folding"
        )
    # The interior marker survives unconsumed in provider state.
    markers = CompressibleHistoryProvider.list_compressed(state)["markers"]
    assert [m["marker_id"] for m in markers] == ["turn_2"]


async def test_p3_fresh_prompt_completed_turn_remains_foldable():
    """§4.1: a fresh prompt's opener is not in provider state, so the clamp
    protects nothing and the normally-completed turn stays foldable — no
    capacity regression."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("Turn 1 request " + "x " * 2000))
    state["messages"].append(_assistant_text("Turn 1 answer " + "y " * 2000))
    CompressibleHistoryProvider.insert_marker(state, 1)

    fresh_opener = _user("Turn 2 request")  # in-flight input, unsaved
    wire = [*_wire_view(state), fresh_opener]
    total = _estimate_tokens(wire)

    compressed_events: list = []

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.50,
        on_compress=_async_appender(compressed_events),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    assert [info.turn_range for info in compressed_events] == [(1, 1)]
    assert not fresh_opener.additional_properties.get(EXCLUDED_KEY, False)
    assert strategy._usage_pct(included_token_count(wire)) <= strategy.target_pct


async def test_p3_stale_index_trap_re_resolves_after_fold():
    """§4.1 STALE-INDEX trap: each fold shrinks state["messages"], so the
    clamp boundary must be re-resolved per candidate pass — a boundary cached
    before the folds would let the interior marker's candidate through."""
    state: dict = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    state["messages"].append(_user("Turn 1 request " + "x " * 1500))
    state["messages"].append(_assistant_text("Turn 1 answer " + "y " * 1500))
    CompressibleHistoryProvider.insert_marker(state, 1)
    state["messages"].append(_user("Turn 2 request " + "x " * 1500))
    state["messages"].append(_assistant_text("Turn 2 answer " + "y " * 1500))
    CompressibleHistoryProvider.insert_marker(state, 2)
    opener3 = _user("Turn 3 request")
    state["messages"].append(opener3)
    work3 = _assistant_text("Turn 3 findings " + "z " * 1500)  # text-only: P4 stays out
    state["messages"].append(work3)
    state["messages"].append(_status_marker(HistoryMarkerKind.INTERRUPTED))
    CompressibleHistoryProvider.insert_marker(state, 3)

    wire = _wire_view(state)
    total = _estimate_tokens(wire)

    compressed_events: list = []

    strategy = _make_strategy(
        max_context_tokens=total + 100,
        trigger_pct=0.85,
        target_pct=0.01,  # unreachable: P3 folds everything it may
        on_compress=_async_appender(compressed_events),
    )
    strategy.bind_state(state)
    changed = await strategy(wire)
    assert changed

    # Both completed turns folded — in order — and NOTHING covers turn 3.
    assert [info.turn_range for info in compressed_events] == [(1, 1), (2, 2)]
    assert not opener3.additional_properties.get(EXCLUDED_KEY, False)
    assert not work3.additional_properties.get(EXCLUDED_KEY, False)
    markers = CompressibleHistoryProvider.list_compressed(state)["markers"]
    assert [m["marker_id"] for m in markers] == ["turn_3"]
