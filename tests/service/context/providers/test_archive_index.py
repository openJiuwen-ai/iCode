# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The head of a long history stays bounded: old fold summaries move into one archive index."""

from __future__ import annotations

from typing import Any

from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import EXCLUDE_REASON_KEY, EXCLUDED_KEY, Message
from chrys.service.context.providers.archive_index import (
    REASON_ARCHIVED,
    archive_entries,
    is_archive_index,
    is_archived_summary,
    merge_fold_summaries,
    project_archive,
)
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.state.serializers import deserialize_state, serialize_state

_WINDOW = 10_000
"""Summaries may take 800 tokens of it together, the index 200."""


def _count(text: str) -> int:
    return len(text) // 4


def _state_with_folds(folds: int, *, summary_chars: int = 800, unfolded_turns: int = 1) -> dict[str, Any]:
    """Provider state whose first *folds* turns are folded, one block and one summary per turn."""
    state: dict[str, Any] = {"messages": [], "compressed_msgs": [], "turn_counter": 0}
    for turn in range(1, folds + unfolded_turns + 1):
        state["messages"].append(Message("user", [f"Request of turn {turn}"]))
        state["messages"].append(Message("assistant", [f"Answer of turn {turn}"]))
        CompressibleHistoryProvider.insert_marker(state, turn)
    for turn in range(1, folds + 1):
        CompressibleHistoryProvider.compress(state, f"turn_{turn}", f"Summary of turn {turn}. " + "s" * summary_chars)
    return state


def _merge(state: dict[str, Any], window: int = _WINDOW):
    return merge_fold_summaries(state, max_context_tokens=window, count_tokens=_count)


def _summaries(state: dict[str, Any]) -> list[Message]:
    return [
        message
        for message in state["messages"]
        if message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.SUMMARY
    ]


def _visible(messages: list[Message]) -> list[Message]:
    return [message for message in messages if not message.additional_properties.get(EXCLUDED_KEY, False)]


def _wire(state: dict[str, Any]) -> list[Message]:
    """The request list a run loads from *state*: no turn markers, nothing excluded."""
    return [
        message
        for message in _visible(state["messages"])
        if message.additional_properties.get(HistoryMarkerKind.KEY) != HistoryMarkerKind.TURN
    ]


def _block_ids(messages: list[Message]) -> list[str]:
    return [message.additional_properties["_block_id"] for message in messages if not is_archive_index(message)]


def test_summaries_within_their_share_of_the_window_are_left_alone() -> None:
    state = _state_with_folds(3)  # About 615 tokens of summaries; 800 allowed.
    before = list(state["messages"])

    assert _merge(state) is None
    assert state["messages"] == before


def test_an_unknown_window_merges_nothing() -> None:
    state = _state_with_folds(8)

    assert _merge(state, window=0) is None


def test_summaries_over_their_share_lose_their_oldest_half_to_the_index() -> None:
    state = _state_with_folds(6)
    block_ids = [block.compressed_context_id for block in state["compressed_msgs"]]
    blocks_before = list(state["compressed_msgs"])

    merge = _merge(state)

    assert merge is not None
    index = state["messages"][0]
    assert merge.index is index
    assert is_archive_index(index)
    assert merge.previous_index is None
    # Half by size is three of six equal summaries; the newer three stay as they were.
    assert _block_ids(list(merge.merged_summaries)) == block_ids[:3]
    assert _block_ids(_visible(_summaries(state))) == block_ids[3:]
    assert all(is_archived_summary(summary) for summary in merge.merged_summaries)
    assert [entry.first_id for entry in archive_entries(index)] == block_ids[:3]
    # Nothing is lost: every block is still held, with its messages and its summary text.
    assert state["compressed_msgs"] == blocks_before
    assert all(block.summary_text.startswith("Summary of turn") for block in state["compressed_msgs"])


def test_the_index_names_each_block_by_id_turns_and_what_the_user_asked() -> None:
    state = _state_with_folds(6)

    _merge(state)

    lines = state["messages"][0].text.splitlines()
    first_id = state["compressed_msgs"][0].compressed_context_id
    assert lines[0] == "[Archive index]"
    assert f"{first_id} · turns 1-1 · Request of turn 1" in lines
    assert "list_compressed_contexts" in lines[1]
    assert "recall_context" in lines[1]


def test_the_newest_summary_is_never_archived() -> None:
    state = _state_with_folds(1, summary_chars=8_000)  # One summary, alone over the share.

    assert _merge(state) is None

    state = _state_with_folds(2, summary_chars=8_000)
    merge = _merge(state)

    assert merge is not None
    assert len(merge.merged_summaries) == 1
    assert _block_ids(_visible(_summaries(state))) == [state["compressed_msgs"][1].compressed_context_id]


def test_a_later_merge_writes_a_new_index_that_continues_the_old_one() -> None:
    state = _state_with_folds(6, unfolded_turns=7)
    first = _merge(state)
    assert first is not None
    for turn in range(7, 13):
        CompressibleHistoryProvider.compress(state, f"turn_{turn}", f"Summary of turn {turn}. " + "s" * 800)

    second = _merge(state)

    assert second is not None
    assert second.previous_index is first.index
    assert second.index is not first.index
    assert first.index not in state["messages"]
    assert [m for m in state["messages"] if is_archive_index(m)] == [second.index]
    assert state["messages"][0] is second.index
    old_lines = first.index.text.splitlines()
    assert second.index.text.splitlines()[: len(old_lines)] == old_lines  # Appended to, not rewritten.
    assert read_analytics_item_id(second.index.additional_properties) != read_analytics_item_id(
        first.index.additional_properties
    )


def test_an_index_over_its_share_merges_its_oldest_lines_into_a_range() -> None:
    state = _state_with_folds(40, summary_chars=400)
    block_ids = [block.compressed_context_id for block in state["compressed_msgs"]]

    while _merge(state) is not None:
        pass

    index = state["messages"][0]
    entries = archive_entries(index)
    assert _count(index.text) <= _WINDOW * 0.02
    assert entries[0].blocks > 1
    assert entries[0].first_id == block_ids[0]
    assert f"{block_ids[0]} … {entries[0].last_id} · turns 1-{entries[0].turn_to} · {entries[0].blocks} blocks" in (
        index.text.splitlines()
    )
    # Every archived block is still counted, in order, exactly once.
    archived = [summary for summary in _summaries(state) if is_archived_summary(summary)]
    assert sum(entry.blocks for entry in entries) == len(archived)
    assert [entry.turn_from for entry in entries] == sorted(entry.turn_from for entry in entries)


def test_a_tiny_index_budget_leaves_one_range_line() -> None:
    state = _state_with_folds(12)

    while _merge(state, window=4_000) is not None:  # 80 tokens for the index: less than its header.
        pass

    entries = archive_entries(state["messages"][0])
    assert len(entries) == 1
    assert entries[0].turn_from == 1


def test_the_next_fold_starts_where_it_did_before_the_merge() -> None:
    state = _state_with_folds(6, unfolded_turns=2)
    _merge(state)

    block_id = CompressibleHistoryProvider.compress(state, "turn_7", "Summary of turn 7")

    block = state["compressed_msgs"][-1]
    assert block.compressed_context_id == block_id
    assert block.turn_range == (7, 7)
    assert [message.text for message in block.messages[:2]] == ["Request of turn 7", "Answer of turn 7"]
    summaries = _summaries(state)
    assert summaries[-1].additional_properties["_block_id"] == block_id
    assert state["messages"].index(summaries[-1]) == len(summaries) - 1  # Still the last of the head.
    assert [m["marker_id"] for m in CompressibleHistoryProvider.list_compressed(state)["markers"]] == ["turn_8"]


async def test_a_request_carries_the_index_and_the_summaries_it_does_not_list() -> None:
    state = _state_with_folds(6)
    _merge(state)

    wire = await CompressibleHistoryProvider().get_messages(None, state=state)

    assert is_archive_index(wire[0])
    assert _block_ids(wire[1:4]) == [block.compressed_context_id for block in state["compressed_msgs"][3:]]
    assert [message.text for message in wire[4:]] == ["Request of turn 7", "Answer of turn 7"]


def test_a_saved_session_reloads_with_the_same_head() -> None:
    state = _state_with_folds(6)
    _merge(state)

    reloaded = deserialize_state(serialize_state(state))

    assert [message.text for message in reloaded["messages"]] == [message.text for message in state["messages"]]
    index = reloaded["messages"][0]
    assert archive_entries(index) == archive_entries(state["messages"][0])
    assert [is_archived_summary(message) for message in _summaries(reloaded)] == [
        is_archived_summary(message) for message in _summaries(state)
    ]
    # And goes on merging from it.
    CompressibleHistoryProvider.compress(reloaded, "turn_7", "Summary of turn 7. " + "s" * 3_000)
    assert _merge(reloaded) is not None


def test_a_request_list_built_before_the_merge_is_brought_in_line() -> None:
    state = _state_with_folds(6)
    wire = _wire(state)
    merge = _merge(state)
    assert merge is not None

    assert project_archive(state, wire)

    assert wire[0] is merge.index
    included = _visible(wire)
    assert included[0] is merge.index
    assert _block_ids(included[1:4]) == [block.compressed_context_id for block in state["compressed_msgs"][3:]]
    # Already in line: nothing more to do.
    assert not project_archive(state, wire)
    assert sum(is_archive_index(message) for message in wire) == 1


def test_a_request_list_drops_the_index_a_later_merge_replaced() -> None:
    state = _state_with_folds(6, unfolded_turns=7)
    first = _merge(state)
    assert first is not None
    wire = _wire(state)
    for turn in range(7, 13):
        CompressibleHistoryProvider.compress(state, f"turn_{turn}", f"Summary of turn {turn}. " + "s" * 800)
    second = _merge(state)
    assert second is not None

    project_archive(state, wire)

    included = _visible(wire)
    assert included[0] is second.index
    assert first.index in wire
    assert first.index.additional_properties[EXCLUDED_KEY] is True
    assert first.index.additional_properties[EXCLUDE_REASON_KEY] == REASON_ARCHIVED
    assert sum(is_archive_index(message) for message in included) == 1


def test_a_request_list_that_replays_no_history_gets_no_index() -> None:
    """A provider that holds the conversation is sent the new messages only, every request."""
    state = _state_with_folds(6)
    assert _merge(state) is not None
    wire = [Message("system", ["You are an agent."]), Message("user", ["Request of turn 7"])]

    assert not project_archive(state, wire)

    assert [message.role for message in wire] == ["system", "user"]


def test_a_copy_of_the_current_index_counts_as_the_index() -> None:
    state = _state_with_folds(6)
    merge = _merge(state)
    assert merge is not None
    copy = Message("assistant", list(merge.index.contents))
    copy.additional_properties.update(merge.index.additional_properties)
    wire = [copy, Message("user", ["Request of turn 7"])]

    assert not project_archive(state, wire)
    assert wire[0] is copy
