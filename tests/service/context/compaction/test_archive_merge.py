# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Folds keep the summaries at the head of a long session within their share of the window."""

from __future__ import annotations

from typing import Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.retry import restore_message_properties, snapshot_message_properties
from chrys.kernel import EXCLUDED_KEY, Message, project_included_messages
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.context.providers.archive_index import archive_entries, is_archive_index, is_archived_summary
from chrys.service.context.providers.history import PRE_OUTPUT_HISTORY_LEN_STATE_KEY, CompressibleHistoryProvider
from tests.service.context.compaction._compaction_helpers import (
    _assistant_text,
    _make_strategy,
    _user,
    _wire_view,
)

_WINDOW = 30_000
"""Summaries may take 2,400 tokens of it together; a turn's summary takes about 150."""


def _is_summary(message: Message) -> bool:
    return message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY


def _tokens(strategy: UnifiedContextStrategy, messages: list[Message]) -> int:
    return sum(strategy.tokenizer.count_tokens(message.text or "") for message in messages)


def _indexes(messages: list[Message]) -> list[Message]:
    return [message for message in messages if is_archive_index(message)]


async def _run_turn(
    strategy: UnifiedContextStrategy, state: dict[str, Any], turn: int
) -> tuple[list[Message], Message | None]:
    """One text-only turn as a run drives it; returns what its request carried and the index state held then."""
    strategy.bind_state(state)
    user = _user(f"Turn {turn} request: " + "question " * 60)
    wire = [*_wire_view(state), user]
    await strategy(wire)
    request = project_included_messages(wire)
    state_index = next(iter(_indexes(state["messages"])), None)
    answer = _assistant_text(f"Turn {turn} answer: " + "finding " * 440)
    state["messages"] = [*state["messages"], user, answer]
    strategy.persist_exclusions_to_state(state["messages"])
    CompressibleHistoryProvider.insert_marker(state, turn)
    if strategy._usage_pct(strategy.last_included_tokens) > strategy.force_pct:
        strategy.request_turn_boundary_pass()
    return request, state_index


def _new_state() -> dict[str, Any]:
    return {"messages": [], "compressed_msgs": [], "turn_counter": 0}


async def test_a_long_session_keeps_one_index_and_a_bounded_run_of_summaries() -> None:
    strategy = _make_strategy(max_context_tokens=_WINDOW)
    state = _new_state()
    requests = [await _run_turn(strategy, state, turn) for turn in range(1, 121)]

    blocks = state["compressed_msgs"]
    assert len(blocks) > 60  # Enough folds for several merges.
    index = state["messages"][0]
    assert is_archive_index(index)
    assert _indexes(state["messages"]) == [index]
    assert strategy.tokenizer.count_tokens(index.text) <= _WINDOW * 0.02

    largest_head = 0
    merging_requests = 0
    previous_index: Message | None = None
    for request, state_index in requests:
        indexes = _indexes(request)
        # What the request carries is the head the state holds at that moment,
        # the request of the very pass that merged included.
        assert indexes == ([state_index] if state_index is not None else [])
        if indexes:
            assert request[0] is indexes[0]
        merging_requests += state_index is not previous_index
        previous_index = state_index
        summaries = [message for message in request if _is_summary(message) and not is_archive_index(message)]
        # The summaries follow the index directly and nothing else comes between them.
        head = request[len(indexes) : len(indexes) + len(summaries)]
        assert head == summaries
        largest_head = max(largest_head, _tokens(strategy, summaries))
    assert merging_requests >= 3
    # A merge runs after the fold that crossed the share, so a request sees at most the share and one fold more.
    assert largest_head <= _WINDOW * 0.08 + 200
    assert strategy._usage_pct(strategy.last_included_tokens) < strategy.trigger_pct

    # Every block is reachable from exactly one place: an index line or its own summary.
    visible_ids = [
        message.additional_properties["_block_id"]
        for message in state["messages"]
        if _is_summary(message) and not is_archive_index(message) and not is_archived_summary(message)
    ]
    archived_ids = [
        message.additional_properties["_block_id"] for message in state["messages"] if is_archived_summary(message)
    ]
    assert sorted([*visible_ids, *archived_ids]) == sorted(block.compressed_context_id for block in blocks)
    assert sum(entry.blocks for entry in archive_entries(index)) == len(archived_ids)
    assert archive_entries(index)[0].turn_from == 1


async def test_without_a_merge_the_same_session_carries_every_summary() -> None:
    """The control for the test above: what the index is there to prevent."""
    strategy = _make_strategy(max_context_tokens=_WINDOW)
    strategy._compression._merge_archive = lambda _messages: None  # type: ignore[method-assign]
    state = _new_state()

    request: list[Message] = []
    for turn in range(1, 121):
        request, _ = await _run_turn(strategy, state, turn)

    assert not _indexes(state["messages"])
    assert _tokens(strategy, [message for message in request if _is_summary(message)]) > _WINDOW * 0.16


async def test_a_fold_the_model_asked_for_is_followed_by_the_merge_too() -> None:
    strategy = _make_strategy(max_context_tokens=_WINDOW)
    state = _new_state()
    for turn in range(1, 5):
        state["messages"].extend([_user(f"Turn {turn} request"), _assistant_text(f"Turn {turn} answer")])
        CompressibleHistoryProvider.insert_marker(state, turn)
    strategy.bind_state(state)
    long_summary = "The model's own account of the work. " + "detail " * 1_200  # About 5% of the window each.
    wire = [*_wire_view(state), _user("Current request")]

    strategy.queue_compression("turn_1", long_summary)
    await strategy(wire)
    assert not _indexes(state["messages"])  # One summary within the share.
    strategy.queue_compression("turn_2", long_summary)
    await strategy(wire)

    index = state["messages"][0]
    assert is_archive_index(index)
    first_block, second_block = state["compressed_msgs"]
    assert [entry.first_id for entry in archive_entries(index)] == [first_block.compressed_context_id]
    request = project_included_messages(wire)
    assert request[0] is index
    assert [message.additional_properties.get("_block_id") for message in request[1:2]] == [
        second_block.compressed_context_id
    ]
    assert long_summary not in "".join(message.text for message in request[2:])
    assert state[PRE_OUTPUT_HISTORY_LEN_STATE_KEY] == len(state["messages"])

    # A later call of the same run gets the same head again, not the archived summary back.
    later = [*_wire_view(state), _user("Current request")]
    await strategy(later)
    assert [message.text for message in project_included_messages(later)[:2]] == [
        message.text for message in request[:2]
    ]


async def test_a_fold_flushed_after_the_run_merges_in_state() -> None:
    strategy = _make_strategy(max_context_tokens=_WINDOW)
    state = _new_state()
    for turn in range(1, 4):
        state["messages"].extend([_user(f"Turn {turn} request"), _assistant_text(f"Turn {turn} answer")])
        CompressibleHistoryProvider.insert_marker(state, turn)
    strategy.bind_state(state)
    long_summary = "The model's own account of the work. " + "detail " * 1_200

    strategy.queue_compression("turn_1", long_summary)
    assert await strategy.flush_pending_compressions()
    strategy.queue_compression("turn_2", long_summary)
    assert await strategy.flush_pending_compressions()

    assert is_archive_index(state["messages"][0])
    assert [is_archived_summary(message) for message in state["messages"][1:3]] == [True, False]


async def test_a_rolled_back_attempt_leaves_a_head_the_next_pass_merges_again() -> None:
    """A retry restores the history the attempt started from: the merge is redone by the next fold, not replayed."""
    strategy = _make_strategy(max_context_tokens=_WINDOW)
    state = _new_state()
    for turn in range(1, 17):
        await _run_turn(strategy, state, turn)
    assert not _indexes(state["messages"])  # Folds so far, none of them over the share.
    folded_before = len(state["compressed_msgs"])

    # The attempt: more turns land as one long history, and its pass folds past the share.
    for turn in range(17, 40):
        state["messages"].extend(
            [_user(f"Turn {turn} request: " + "question " * 60), _assistant_text("finding " * 440)]
        )
        CompressibleHistoryProvider.insert_marker(state, turn)
    baseline = list(state["messages"])
    baseline_properties = snapshot_message_properties(baseline)
    strategy.bind_state(state)
    retry_snapshot = strategy.snapshot_retry_state()
    wire = [*_wire_view(state), _user("Current request")]
    await strategy(wire)
    assert _indexes(state["messages"])
    published = [block.compressed_context_id for block in state["compressed_msgs"]]

    # Mirror HistoryRollback.restore, then the strategy's replay of the folds it had published.
    state["messages"] = list(baseline)
    del state["compressed_msgs"][folded_before:]
    state.pop(PRE_OUTPUT_HISTORY_LEN_STATE_KEY, None)
    restore_message_properties(state["messages"], baseline_properties)
    strategy.restore_retry_state(retry_snapshot)

    assert [block.compressed_context_id for block in state["compressed_msgs"]] == published
    assert not _indexes(state["messages"])
    assert not any(is_archived_summary(message) for message in state["messages"])
    summaries = [message for message in state["messages"] if _is_summary(message)]
    assert len(summaries) == len(published)
    assert not any(message.additional_properties.get(EXCLUDED_KEY, False) for message in summaries)

    # The retry's request carries every summary once more; the fold of a later pass merges them again.
    for turn in range(40, 70):
        await _run_turn(strategy, state, turn)
    assert is_archive_index(state["messages"][0])
    assert len(_indexes(state["messages"])) == 1
