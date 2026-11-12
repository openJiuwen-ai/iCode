# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Segmented emission of unbounded fields, and the compaction run trace built on it."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace

import pytest

from chrys.foundation.trajectory.context import trajectory_scope
from chrys.foundation.trajectory.envelope import (
    LINE_BUDGET_BYTES,
    SEGMENT_EVENT_TYPE,
    MeasurementSource,
    build_event,
    encode_event_line,
)
from chrys.foundation.trajectory.event_types import CompactionSkipReason, EventType
from chrys.foundation.trajectory.ids import is_valid_analytics_id, new_analytics_id
from chrys.foundation.trajectory.segments import ENCODING_ARRAY_SLICE, reassemble_array_slice
from chrys.kernel import Content, Message
from chrys.service.context.compaction.events import CompactionInfo
from chrys.service.context.compaction.strategy import UnifiedContextStrategy
from chrys.service.context.manager import ContextManager
from chrys.service.profiles.agents.schema import CompactionConfig
from chrys.service.profiles.models.resolver import default_profile
from chrys.service.trajectory.compaction import (
    TOKEN_MEASUREMENT_SOURCE,
    CompactionRunTrace,
    record_compaction_skipped,
)
from chrys.service.trajectory.segmented import emit_segmented, emit_segmented_soon, measure_line, plan_segmented
from tests.service.trajectory._fakes import SESSION_ID, CancelAckSink, FakeSink, make_context


def _encoded_length(draft, *, sequence: int = 1) -> int:
    event = build_event(
        draft,
        sequence=sequence,
        runtime_id=new_analytics_id(),
        coverage_id=new_analytics_id(),
        session_id=SESSION_ID,
        branch_id=new_analytics_id(),
    )
    return len(encode_event_line(event))


# ------------------------------------------------------------ plan_segmented


def test_base_declares_each_field_and_never_carries_it_inline() -> None:
    context = make_context()

    base, segments = plan_segmented(
        context,
        event_type=EventType.COMPACTION_PHASE_FINISHED,
        payload={"phase": "phase1"},
        array_fields={"/payload/turn_numbers": [1, 2, 3], "/payload/tool_names": ["read", "write"]},
    )

    assert base.event_type == EventType.COMPACTION_PHASE_FINISHED
    assert base.payload == {"phase": "phase1"}
    assert [field.field_pointer for field in base.segmented_fields] == [
        "/payload/turn_numbers",
        "/payload/tool_names",
    ]
    assert all(field.segment_count == 1 for field in base.segmented_fields)
    assert [segment.event_type for segment in segments] == [SEGMENT_EVENT_TYPE] * 2
    assert reassemble_array_slice([segment.payload for segment in segments[:1]]) == [1, 2, 3]


def test_segments_point_back_at_the_base_event_and_share_its_moment() -> None:
    context = make_context()

    base, segments = plan_segmented(
        context,
        event_type=EventType.CONTEXT_REVISION_RECORDED,
        payload={},
        array_fields={"/payload/refs": [{"item_id": new_analytics_id()}]},
    )

    segment = segments[0]
    assert segment.payload["parent_event_id"] == base.event_id
    assert segment.payload["segment_group_id"] == base.segmented_fields[0].segment_group_id
    assert segment.payload["encoding"] == ENCODING_ARRAY_SLICE
    assert is_valid_analytics_id(segment.event_id)
    assert segment.event_id != base.event_id
    # A segment is part of its base event's moment, not a later one.
    assert segment.occurred_at == base.occurred_at
    assert segment.monotonic_ns == base.monotonic_ns
    assert segment.actor == base.actor
    assert segment.turn_id == base.turn_id


def test_an_empty_field_still_declares_one_empty_segment() -> None:
    context = make_context()

    base, segments = plan_segmented(
        context,
        event_type=EventType.COMPACTION_PHASE_FINISHED,
        payload={},
        array_fields={"/payload/turn_numbers": []},
    )

    # The reader must be able to tell "empty" from "not recorded".
    assert base.segmented_fields[0].segment_count == 1
    assert segments[0].payload["entries"] == []


def test_a_field_too_large_for_one_line_continues_across_contiguous_segments() -> None:
    context = make_context()
    turn_numbers = list(range(4000))

    base, segments = plan_segmented(
        context,
        event_type=EventType.COMPACTION_PHASE_FINISHED,
        payload={"phase": "phase2"},
        array_fields={"/payload/turn_numbers": turn_numbers},
    )

    declaration = base.segmented_fields[0]
    assert declaration.segment_count > 1
    assert len(segments) == declaration.segment_count
    assert [segment.payload["segment_index"] for segment in segments] == list(range(declaration.segment_count))
    assert all(segment.payload["segment_count"] == declaration.segment_count for segment in segments)
    assert all(segment.payload["segment_group_id"] == declaration.segment_group_id for segment in segments)
    # Every line the writer will encode fits the budget, base included.
    assert _encoded_length(base) <= LINE_BUDGET_BYTES
    assert all(_encoded_length(segment) <= LINE_BUDGET_BYTES for segment in segments)
    assert reassemble_array_slice([segment.payload for segment in segments]) == turn_numbers


@pytest.mark.parametrize("entry_width", [1, 2, 3, 6])
def test_segments_stay_within_the_budget_at_the_widest_sequence(entry_width: int) -> None:
    context = make_context()
    entries = ["x" * entry_width] * 1200

    base, segments = plan_segmented(
        context,
        event_type=EventType.COMPACTION_PHASE_FINISHED,
        payload={"phase": "phase2"},
        array_fields={"/payload/tool_names": entries},
    )

    # Each segment is filled to what the planner measured, so a probe that
    # leaves out a field the emitted segment carries spends the difference on
    # entries — and the writer refuses the line it gets. Measured at the
    # widest sequence, which is the slot the planner reserves for it.
    assert all(segment.turn_id == base.turn_id for segment in segments)
    assert all(_encoded_length(segment, sequence=10**9) <= LINE_BUDGET_BYTES for segment in segments)
    assert reassemble_array_slice([segment.payload for segment in segments]) == entries


def test_measure_line_accounts_for_the_writer_owned_fields() -> None:
    context = make_context()
    draft = context.draft(EventType.COMPACTION_STARTED, payload={"trigger": "usage_threshold"})

    # The measurement is what the writer will really encode, not the bare draft.
    assert measure_line(draft, session_id=SESSION_ID) == _encoded_length(draft, sequence=10**9)


def test_without_a_fingerprint_key_planning_still_succeeds() -> None:
    context = make_context(FakeSink(fingerprint_key=None))

    base, segments = plan_segmented(
        context,
        event_type=EventType.CONTEXT_REVISION_RECORDED,
        payload={},
        array_fields={"/payload/refs": [1, 2]},
    )

    assert base.segmented_fields[0].segment_count == 1
    assert segments[0].payload["entries"] == [1, 2]


# ------------------------------------------------------------ emit ordering


@pytest.mark.asyncio
async def test_emit_segmented_queues_the_base_before_its_segments() -> None:
    sink = FakeSink()
    context = make_context(sink)

    assert (
        await emit_segmented(
            context,
            event_type=EventType.COMPACTION_PHASE_FINISHED,
            payload={"phase": "phase1"},
            array_fields={"/payload/turn_numbers": list(range(4000))},
        )
        is True
    )

    # A segment may never precede the event it continues.
    assert sink.event_types[0] == EventType.COMPACTION_PHASE_FINISHED
    assert set(sink.event_types[1:]) == {SEGMENT_EVENT_TYPE}
    base = sink.drafts[0]
    assert reassemble_array_slice([draft.payload for draft in sink.drafts[1:]]) == list(range(4000))
    assert base.segmented_fields[0].segment_count == len(sink.drafts) - 1


def test_emit_segmented_soon_queues_the_same_order_without_awaiting() -> None:
    sink = FakeSink()
    context = make_context(sink)

    assert (
        emit_segmented_soon(
            context,
            event_type=EventType.CONTEXT_REVISION_RECORDED,
            payload={"revision_id": new_analytics_id()},
            array_fields={"/payload/refs": [{"item_id": new_analytics_id()}, {"item_id": new_analytics_id()}]},
        )
        is True
    )

    assert sink.event_types == [EventType.CONTEXT_REVISION_RECORDED, SEGMENT_EVENT_TYPE]


@pytest.mark.asyncio
async def test_a_failing_sink_reports_false_and_never_raises() -> None:
    sink = FakeSink()
    context = make_context(sink)
    sink.fail_next = True

    assert (
        await emit_segmented(
            context,
            event_type=EventType.COMPACTION_PHASE_FINISHED,
            payload={},
            array_fields={"/payload/turn_numbers": [1]},
        )
        is False
    )

    sink.fail_next = True
    assert (
        emit_segmented_soon(
            context,
            event_type=EventType.CONTEXT_REVISION_RECORDED,
            payload={},
            array_fields={"/payload/refs": [1]},
        )
        is False
    )


# --------------------------------------------------------- compaction trace


def test_open_without_an_ambient_scope_records_nothing() -> None:
    assert CompactionRunTrace.open() is None


@pytest.mark.asyncio
async def test_a_run_ties_started_phases_and_finished_to_one_id() -> None:
    sink = FakeSink()
    context = make_context(sink)
    run = CompactionRunTrace(context)

    await run.started(trigger="usage_threshold", tokens_before=900)
    await run.phase_finished(
        phase="phase1",
        groups_compacted=2,
        turn_numbers=[3, 4],
        tool_names=["read"],
        tokens_before=900,
        tokens_after=700,
    )
    await run.finished(tokens_before=900, tokens_after=500)

    started = sink.only(EventType.COMPACTION_STARTED)
    phase = sink.only(EventType.COMPACTION_PHASE_FINISHED)
    finished = sink.only(EventType.COMPACTION_FINISHED)
    assert started.operation_id == run.run_id
    assert finished.operation_id == run.run_id
    assert started.parent_operation_id == context.run_operation_id
    assert phase.parent_operation_id == run.run_id
    assert phase.operation_id != run.run_id
    assert started.payload["trigger"] == "usage_threshold"
    assert phase.payload["phase"] == "phase1"
    assert phase.payload["groups_compacted"] == 2
    assert finished.payload["tokens_after"] == 500
    # The phase's own lists ride on segments, never inline.
    assert reassemble_array_slice(
        [
            draft.payload
            for draft in sink.of_type(SEGMENT_EVENT_TYPE)
            if "turn_numbers" in draft.payload["field_pointer"]
        ]
    ) == [3, 4]
    assert "turn_numbers" not in phase.payload


@pytest.mark.asyncio
async def test_phase4_declares_consumed_items_and_the_reserved_successor_exchange() -> None:
    sink = FakeSink()
    next_exchange_operation_id = new_analytics_id()
    consumed_item_ids = [new_analytics_id(), new_analytics_id()]
    run = CompactionRunTrace(
        make_context(sink),
        next_exchange_operation_id=next_exchange_operation_id,
    )

    await run.started(trigger="usage_threshold", tokens_before=900)
    await run.phase_finished(
        phase="phase4",
        groups_compacted=1,
        turn_numbers=[4],
        tool_names=["read"],
        tokens_before=900,
        tokens_after=100,
        last_words_generated=True,
        consumed_item_ids=consumed_item_ids,
    )

    assert "next_exchange_operation_id" not in sink.only(EventType.COMPACTION_STARTED).payload
    assert (
        sink.only(EventType.COMPACTION_PHASE_FINISHED).payload["next_exchange_operation_id"]
        == next_exchange_operation_id
    )
    assert (
        reassemble_array_slice(
            [
                draft.payload
                for draft in sink.of_type(SEGMENT_EVENT_TYPE)
                if draft.payload["field_pointer"] == "/payload/consumed_item_ids"
            ]
        )
        == consumed_item_ids
    )


@pytest.mark.asyncio
async def test_every_token_figure_is_labelled_with_its_measurement_source() -> None:
    sink = FakeSink()
    run = CompactionRunTrace(make_context(sink))

    await run.started(trigger="force", tokens_before=10)
    await run.phase_finished(
        phase="phase3", groups_compacted=1, turn_numbers=[], tool_names=[], tokens_before=10, tokens_after=4
    )

    started = sink.only(EventType.COMPACTION_STARTED)
    assert started.payload["token_measurement_source"] == TOKEN_MEASUREMENT_SOURCE
    assert started.measurements["/payload/tokens_before"]["source"] == TOKEN_MEASUREMENT_SOURCE
    phase = sink.only(EventType.COMPACTION_PHASE_FINISHED)
    assert phase.measurements["/payload/tokens_after"]["source"] == TOKEN_MEASUREMENT_SOURCE
    # Durations are clock readings, not token estimates.
    assert phase.measurements["/payload/duration_ms"]["source"] == MeasurementSource.MONOTONIC_CLOCK


@pytest.mark.asyncio
async def test_a_fold_names_the_block_it_produced_and_what_it_freed() -> None:
    sink = FakeSink()
    run = CompactionRunTrace(make_context(sink))

    await run.phase_finished(
        phase="phase3",
        groups_compacted=1,
        turn_numbers=[1, 2],
        tool_names=[],
        tokens_before=800,
        tokens_after=120,
        messages_freed=7,
        compressed_context_id="ctx_0a1b2c3d",
    )

    payload = sink.only(EventType.COMPACTION_PHASE_FINISHED).payload
    assert payload["messages_freed"] == 7
    assert payload["compressed_context_id"] == "ctx_0a1b2c3d"


@pytest.mark.asyncio
async def test_a_phase_that_folded_nothing_omits_the_fold_only_fields() -> None:
    sink = FakeSink()
    run = CompactionRunTrace(make_context(sink))

    await run.phase_finished(
        phase="phase1", groups_compacted=0, turn_numbers=[], tool_names=[], tokens_before=5, tokens_after=5
    )

    payload = sink.only(EventType.COMPACTION_PHASE_FINISHED).payload
    assert "messages_freed" not in payload
    assert "compressed_context_id" not in payload


@pytest.mark.asyncio
async def test_last_words_generation_sticks_to_the_run_once_any_phase_reports_it() -> None:
    sink = FakeSink()
    run = CompactionRunTrace(make_context(sink))

    await run.phase_finished(
        phase="phase2",
        groups_compacted=1,
        turn_numbers=[],
        tool_names=[],
        tokens_before=5,
        tokens_after=4,
        last_words_generated=True,
    )
    await run.phase_finished(
        phase="phase4", groups_compacted=1, turn_numbers=[], tool_names=[], tokens_before=4, tokens_after=3
    )
    await run.finished(tokens_before=5, tokens_after=3)

    phases = sink.of_type(EventType.COMPACTION_PHASE_FINISHED)
    assert [phase.payload["last_words_generated"] for phase in phases] == [True, False]
    assert sink.only(EventType.COMPACTION_FINISHED).payload["last_words_generated"] is True


@pytest.mark.asyncio
async def test_finishing_twice_records_one_terminal_marker() -> None:
    sink = FakeSink()
    run = CompactionRunTrace(make_context(sink))

    await run.finished(tokens_before=9, tokens_after=8)
    await run.finished(tokens_before=9, tokens_after=1)

    assert len(sink.of_type(EventType.COMPACTION_FINISHED)) == 1
    assert sink.only(EventType.COMPACTION_FINISHED).payload["tokens_after"] == 8


@pytest.mark.asyncio
async def test_a_standalone_fold_is_its_own_run_with_both_markers() -> None:
    sink = FakeSink()
    context = make_context(sink)

    with trajectory_scope(context):
        await CompactionRunTrace.record_compression(
            trigger="agent_request",
            compressed_context_id="ctx_deadbeef",
            messages_freed=12,
            tokens_before=4000,
            tokens_after=300,
        )

    started = sink.only(EventType.COMPACTION_STARTED)
    finished = sink.only(EventType.COMPACTION_FINISHED)
    assert started.payload["trigger"] == "agent_request"
    assert finished.operation_id == started.operation_id
    assert finished.payload["compressed_context_id"] == "ctx_deadbeef"
    assert finished.payload["messages_freed"] == 12
    # A standalone fold has no phases of its own.
    assert sink.of_type(EventType.COMPACTION_PHASE_FINISHED) == []


@pytest.mark.asyncio
async def test_recording_a_compression_without_a_scope_is_a_no_op() -> None:
    await CompactionRunTrace.record_compression(
        trigger="force", compressed_context_id="ctx_deadbeef", messages_freed=1, tokens_before=2, tokens_after=1
    )


@pytest.mark.asyncio
async def test_a_failing_sink_never_breaks_a_compaction_pass() -> None:
    sink = FakeSink()
    run = CompactionRunTrace(make_context(sink))

    sink.fail_next = True
    await run.started(trigger="usage_threshold", tokens_before=1)
    sink.fail_next = True
    await run.phase_finished(
        phase="phase1", groups_compacted=1, turn_numbers=[], tool_names=[], tokens_before=1, tokens_after=1
    )
    sink.fail_next = True
    await run.finished(tokens_before=1, tokens_after=1)

    assert sink.drafts == []


# ------------------------------------------------------------ skipped passes

# A pass annotates and may exclude the messages it is handed, so each call
# gets fresh ones.


def _no_turn() -> list[Message]:
    """Past a 500-token trigger, with no user input for a turn to resolve against."""
    return [Message("assistant", ["world " * 2000])]


def _one_turn() -> list[Message]:
    return [Message("user", ["hello"]), Message("assistant", ["world " * 2000])]


def _below_trigger() -> list[Message]:
    return [Message("user", ["hi"])]


def _skip_strategy(*, compaction_enabled: bool = True) -> UnifiedContextStrategy:
    return UnifiedContextStrategy(
        max_context_tokens=1000,
        trigger_pct=0.5,
        target_pct=0.25,
        compaction_enabled=compaction_enabled,
    )


@pytest.mark.asyncio
async def test_usage_past_the_trigger_that_starts_no_pass_says_why_once_per_stretch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _skip_strategy()
    sink = FakeSink()

    with caplog.at_level(logging.WARNING), trajectory_scope(make_context(sink)):
        await strategy(_no_turn())
        await strategy(_no_turn())
        await strategy(_below_trigger())
        await strategy(_no_turn())

    skipped = sink.of_type(EventType.COMPACTION_SKIPPED)
    assert len(skipped) == 2  # once per stretch above the trigger
    assert sink.of_type(EventType.COMPACTION_STARTED) == []
    payload = skipped[0].payload
    assert payload["reason_code"] == CompactionSkipReason.TURNS_UNRESOLVED
    assert payload["trigger_tokens"] == 500
    assert payload["max_context_tokens"] == 1000
    assert payload["estimated_input_tokens"] >= payload["trigger_tokens"]
    assert skipped[0].measurements["/payload/estimated_input_tokens"]["source"] == TOKEN_MEASUREMENT_SOURCE
    assert [record.levelno for record in caplog.records if "Compaction skipped" in record.getMessage()] == [
        logging.WARNING
    ] * 2
    assert "reason=turns_unresolved" in caplog.text


@pytest.mark.asyncio
async def test_a_session_restored_onto_the_same_strategy_is_told_why_again() -> None:
    """A restore without a rebuild keeps the strategy; the stretch it was in
    belongs to the session it left."""
    strategy = _skip_strategy()
    first_sink, restored_sink = FakeSink(), FakeSink()

    with trajectory_scope(make_context(first_sink)):
        await strategy(_no_turn())
    with trajectory_scope(replace(make_context(restored_sink), session_id="restored-session")):
        await strategy(_no_turn())
        await strategy(_no_turn())

    assert len(first_sink.of_type(EventType.COMPACTION_SKIPPED)) == 1
    assert len(restored_sink.of_type(EventType.COMPACTION_SKIPPED)) == 1


@pytest.mark.asyncio
async def test_a_pass_that_frees_nothing_says_so_once_per_stretch(caplog: pytest.LogCaptureFixture) -> None:
    """One turn of plain text holds nothing a phase can take: the request stays as large as it came."""
    strategy = _skip_strategy()
    sink = FakeSink()

    with caplog.at_level(logging.WARNING), trajectory_scope(make_context(sink)):
        await strategy(_no_turn())
        assert not await strategy(_one_turn())
        assert not await strategy(_one_turn())
        await strategy(_no_turn())

    assert len(sink.of_type(EventType.COMPACTION_STARTED)) == 2
    assert [event.payload["reason_code"] for event in sink.of_type(EventType.COMPACTION_SKIPPED)] == [
        CompactionSkipReason.TURNS_UNRESOLVED,
        CompactionSkipReason.NOTHING_FREED,
        CompactionSkipReason.TURNS_UNRESOLVED,
    ]
    assert caplog.text.count("Compaction freed nothing: reason=nothing_freed") == 1


@pytest.mark.asyncio
async def test_a_pass_that_frees_something_ends_the_stretch_a_skip_was_reported_for() -> None:
    strategy = _skip_strategy()
    sink = FakeSink()
    reclaimable = [
        Message("user", ["first"]),
        Message("assistant", [Content.from_function_call("call-1", "read_file", arguments={"path": "a.py"})]),
        Message("tool", [Content.from_function_result("call-1", result="source " * 2000)]),
        Message("assistant", ["done"]),
        Message("user", ["second"]),
    ]

    with trajectory_scope(make_context(sink)):
        await strategy(_no_turn())
        assert await strategy(reclaimable)
        await strategy(_no_turn())

    assert [event.payload["reason_code"] for event in sink.of_type(EventType.COMPACTION_SKIPPED)] == [
        CompactionSkipReason.TURNS_UNRESOLVED,
        CompactionSkipReason.TURNS_UNRESOLVED,
    ]


@pytest.mark.asyncio
async def test_disabled_compaction_past_the_trigger_is_recorded_as_a_choice(
    caplog: pytest.LogCaptureFixture,
) -> None:
    strategy = _skip_strategy(compaction_enabled=False)
    sink = FakeSink()

    with caplog.at_level(logging.INFO), trajectory_scope(make_context(sink)):
        await strategy(_below_trigger())
        await strategy(_one_turn())

    skipped = sink.only(EventType.COMPACTION_SKIPPED)
    assert skipped.payload["reason_code"] == CompactionSkipReason.DISABLED
    assert sink.of_type(EventType.COMPACTION_STARTED) == []
    # The profile asked for this, so it is not a warning.
    assert [record.levelno for record in caplog.records if "Compaction skipped" in record.getMessage()] == [
        logging.INFO
    ]


@pytest.mark.parametrize(
    ("max_context_tokens", "trigger_tokens", "words"),
    [
        (200_000, 158_000, 72_900),
        # trigger_tokens / window as a float, times the window, lands just
        # above 54,727: rounding up would report 54,728.
        (91_727, 54_727, 24_900),
    ],
)
@pytest.mark.asyncio
async def test_disabled_compaction_is_reported_against_the_models_derived_trigger(
    max_context_tokens: int, trigger_tokens: int, words: int
) -> None:
    """With 32k of output a 200k window derives a 158k trigger. A fixed 85%
    stand-in for the disabled case would stay silent up to 170k and then
    report 170k as the threshold."""
    manager = ContextManager(
        replace(default_profile(), max_context_tokens=max_context_tokens, max_output_tokens=32_000),
        compaction_config=CompactionConfig(enabled=False),
    )
    sink = FakeSink()

    with trajectory_scope(make_context(sink)):
        await manager.compaction_strategy([Message("user", ["evidence " * words])])

    skipped = sink.only(EventType.COMPACTION_SKIPPED)
    assert skipped.payload["reason_code"] == CompactionSkipReason.DISABLED
    assert skipped.payload["trigger_tokens"] == manager.budgets.trigger_tokens == trigger_tokens
    # Between the derived trigger and where 85% of the window would put it.
    assert trigger_tokens <= skipped.payload["estimated_input_tokens"] < round(0.85 * max_context_tokens)


def test_recording_a_skip_without_a_scope_or_with_a_failing_sink_never_raises() -> None:
    figures = {"estimated_input_tokens": 9, "trigger_tokens": 5, "max_context_tokens": 10}
    record_compaction_skipped(reason_code=CompactionSkipReason.DISABLED, **figures)

    sink = FakeSink()
    sink.fail_next = True
    with trajectory_scope(make_context(sink)):
        record_compaction_skipped(reason_code=CompactionSkipReason.DISABLED, **figures)

    assert sink.drafts == []


# ------------------------------------------------- detached phase deliveries


@pytest.mark.asyncio
async def test_an_interrupted_phase_is_recorded_before_the_run_it_belongs_to_ends() -> None:
    """The pass closes its run while unwinding and the delivery it scheduled
    has not started yet, so a phase left to that task lands after its run."""
    delivered: list[CompactionInfo] = []

    async def _record(info: CompactionInfo) -> None:
        delivered.append(info)

    strategy = UnifiedContextStrategy(max_context_tokens=1000, on_compaction=_record)
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        run = CompactionRunTrace.open()
    assert run is not None
    strategy._trajectory_run = run

    strategy._current_turn_drop._schedule_detached_on_compaction(
        CompactionInfo(
            compacted_groups=1,
            phase="phase4",
            turn_numbers=[3],
            tool_names=["echo"],
            tokens_before=100,
            tokens_after=40,
            last_words_generated=True,
        )
    )
    # The pass unwinds: it clears the handle and closes the run.
    strategy._trajectory_run = None
    run.finished_soon()
    for delivery in list(strategy._detached_deliveries):
        await delivery

    phase = sink.only(EventType.COMPACTION_PHASE_FINISHED)
    assert phase.payload["compaction_run_id"] == run.run_id
    assert phase.payload["phase"] == "phase4"
    assert sink.event_types.index(EventType.COMPACTION_PHASE_FINISHED) < sink.event_types.index(
        EventType.COMPACTION_FINISHED
    )
    # And the run's own terminal names what phase 4 reached, not the figure
    # it carried into the phase.
    assert sink.only(EventType.COMPACTION_FINISHED).payload["tokens_after"] == 40
    # The observer callback the task carries is still delivered.
    assert [info.phase for info in delivered] == ["phase4"]


@pytest.mark.asyncio
async def test_an_interrupted_pass_still_closes_its_run() -> None:
    """A pass cancelled mid-phase records its terminal marker: a run with only
    a start marker reads as one that is still going."""
    strategy = UnifiedContextStrategy(max_context_tokens=100, trigger_pct=0.01, target_pct=0.005)

    async def _cancelled(*args: object, **kwargs: object) -> bool:
        raise asyncio.CancelledError

    strategy._run_phase1 = _cancelled  # the user pressed Stop while phase 1 was awaiting
    messages = [Message("user", ["hello"]), Message("assistant", ["world " * 500])]
    sink = FakeSink()
    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await strategy(messages)

    started = sink.only(EventType.COMPACTION_STARTED)
    finished = sink.only(EventType.COMPACTION_FINISHED)
    assert finished.payload["compaction_run_id"] == started.payload["compaction_run_id"]
    # Nothing was measured as freed, so the run reports the figure it started from.
    assert finished.payload["tokens_after"] == started.payload["tokens_before"]


@pytest.mark.asyncio
async def test_an_interrupted_group_still_writes_the_segments_it_declared() -> None:
    """A group cut short would reassemble as a shorter field with nothing on
    the line to say so, so the tail follows the base even when the caller
    unwinds."""
    sink = CancelAckSink(at=2)
    context = make_context(sink)
    with pytest.raises(asyncio.CancelledError):
        await emit_segmented(
            context,
            event_type=EventType.COMPACTION_PHASE_FINISHED,
            operation_id=new_analytics_id(),
            payload={"compaction_run_id": new_analytics_id(), "phase": "phase1"},
            array_fields={"/payload/turn_numbers": list(range(3000))},
        )

    base = sink.only(EventType.COMPACTION_PHASE_FINISHED)
    declaration = base.segmented_fields[0]
    segments = sink.of_type(SEGMENT_EVENT_TYPE)
    assert declaration.segment_count > 1
    assert len(segments) == declaration.segment_count
    assert reassemble_array_slice([segment.payload for segment in segments]) == list(range(3000))


@pytest.mark.asyncio
async def test_a_pass_interrupted_in_its_start_marker_still_closes_the_run() -> None:
    strategy = UnifiedContextStrategy(max_context_tokens=100, trigger_pct=0.01, target_pct=0.005)
    messages = [Message("user", ["hello"]), Message("assistant", ["world " * 500])]
    sink = CancelAckSink(at=1)
    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await strategy(messages)

    started = sink.only(EventType.COMPACTION_STARTED)
    finished = sink.only(EventType.COMPACTION_FINISHED)
    assert finished.payload["compaction_run_id"] == started.payload["compaction_run_id"]


@pytest.mark.asyncio
async def test_a_standalone_fold_interrupted_between_markers_reports_what_it_folded() -> None:
    sink = CancelAckSink(at=1)
    with trajectory_scope(make_context(sink)), pytest.raises(asyncio.CancelledError):
        await CompactionRunTrace.record_compression(
            trigger="force",
            compressed_context_id="ctx_deadbeef",
            messages_freed=4,
            tokens_before=900,
            tokens_after=300,
        )

    finished = sink.only(EventType.COMPACTION_FINISHED)
    assert finished.payload["tokens_before"] == 900
    assert finished.payload["tokens_after"] == 300
    assert finished.payload["compressed_context_id"] == "ctx_deadbeef"
    assert finished.payload["messages_freed"] == 4
