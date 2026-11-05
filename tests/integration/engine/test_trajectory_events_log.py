# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end: what a real engine turn writes into ``<session>/trajectory/events.jsonl``."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.env_layers import freeze_process_env
from chrys.foundation.config.settings import SESSION_ROOT_DIR_ENV_VAR, Settings, resolve_sessions_dir
from chrys.foundation.config.settings_store import persist
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ApprovalRequest,
    SessionDelete,
    SessionDeleted,
    SessionReady,
    SessionRestore,
    SetApprovalMode,
    SetModelProfile,
    SettingsReload,
    UserMessage,
    UserRetry,
    UserRollback,
    Warning,
)
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.envelope import TrajectoryEvent
from chrys.foundation.trajectory.event_types import EventType, ProfileKind, TurnEndReason
from chrys.foundation.trajectory.ids import is_valid_analytics_id
from chrys.foundation.trajectory.keys import ensure_owner_only_directory
from chrys.foundation.trajectory.lease import WriterLease
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY, OPERATION_ID_KEY
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.foundation.trajectory.segments import reassemble_array_slice
from chrys.foundation.trajectory.writer import DEFAULT_WRITE_ACK_TIMEOUT_SECONDS, FdWriteBackend, WriterState
from chrys.kernel import ChatResponse, Message
from chrys.service.analytics import Precision
from chrys.service.analytics.aggregation import analyze_trajectory
from chrys.service.approval import judge as judge_module
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.manager import HookManager
from chrys.service.hooks.runner import HookResult, HookRunner
from chrys.service.hooks.schema import HookConfig, HookExecution, HookRun, HooksFile
from chrys.service.llm.mock import MockResponse
from chrys.service.profiles.agents.schema import CompactionConfig
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.trajectory.session import (
    SessionTrajectory,
    TrajectoryDisabledReason,
    trajectory_dir,
    trajectory_events_path,
    trajectory_lease_path,
)
from tests.support.formal_approval import predicate_values
from tests.support.pipeline_helpers import (
    PipelineTestContext,
    create_test_engine,
    echo_tool,
    wait_for_event,
    wait_for_idle,
)
from tests.support.symlinks import symlink_or_skip
from tests.support.trajectory_invariants import assert_trajectory_accounted, assert_trajectory_operation_settlement
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    wait_for,
    wait_until,
    with_wait_deadline,
)

# Strings that must never reach the analytics log: the user's words, a tool
# argument, and the tool's output.
SECRET_PROMPT = "SENTINEL-user-prompt-Zx9"
SECRET_ARGUMENT = "SENTINEL-tool-argument-Qw7"
SECRET_RESULT_FRAGMENT = "echo: " + SECRET_ARGUMENT


def _events(session_dir: Path) -> list[TrajectoryEvent]:
    result = read_trajectory(trajectory_events_path(session_dir))
    assert result.corrupt_lines == []
    assert result.torn_tail_bytes == 0
    assert result.unsupported_event_count == 0
    assert_trajectory_accounted(result)
    # Every lifecycle this stack opens is closed on it: the full middleware is
    # present here, so the pairing oracle holds in its strongest form.
    assert_trajectory_operation_settlement(result.events)
    # A gap on a healthy run means the writer refused something a producer
    # built (bad id, over budget, unencodable) — the event is gone and only
    # this assertion would ever say so.
    assert [event for event in result.events if event.event_type == EventType.GAP] == []
    return result.events


def _of_type(events: list[TrajectoryEvent], event_type: str) -> list[TrajectoryEvent]:
    return [event for event in events if event.event_type == event_type]


def _one(events: list[TrajectoryEvent], event_type: str) -> TrajectoryEvent:
    matches = _of_type(events, event_type)
    assert len(matches) == 1, f"expected exactly one {event_type}, got {len(matches)}"
    return matches[0]


def _session_dir(ctx: PipelineTestContext) -> Path:
    session_dir = ctx.engine.session_dir
    assert session_dir is not None
    return session_dir


async def _finish(ctx: PipelineTestContext) -> Path:
    """Shut the engine down (closing the log) and return its session directory."""
    session_dir = _session_dir(ctx)
    await ctx.cleanup()
    return session_dir


# --------------------------------------------------------------- text turn


@pytest.mark.asyncio
async def test_a_text_turn_records_runtime_turn_and_model_spans_in_order(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    order = [event.event_type for event in events]
    # The prelude opens the file before anything else can be recorded:
    # coverage first, then the runtime it covers.
    assert order[:2] == [EventType.COVERAGE_STARTED, EventType.RUNTIME_STARTED]
    assert order[-2:] == [EventType.COVERAGE_ENDED, EventType.RUNTIME_FINISHED]
    for opener, closer in (
        (EventType.TURN_STARTED, EventType.TURN_FINISHED),
        (EventType.MODEL_RUN_STARTED, EventType.MODEL_RUN_FINISHED),
        (EventType.MODEL_CYCLE_STARTED, EventType.MODEL_CYCLE_FINISHED),
        (EventType.MODEL_EXCHANGE_STARTED, EventType.MODEL_EXCHANGE_FINISHED),
    ):
        assert order.index(opener) < order.index(closer), f"{opener} must precede {closer}"
    assert order.index(EventType.TURN_FINISHED) < order.index(EventType.TURN_RESPONSE_SETTLED)
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)
    assert len({event.event_id for event in events}) == len(events)
    assert {event.session_id for event in events} == {ctx.session_id}
    assert len({event.runtime_id for event in events}) == 1


@pytest.mark.asyncio
async def test_persistent_activation_failure_retries_then_warns_the_user_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    warnings: list[Warning] = []

    async def _collect(event: Warning) -> None:
        warnings.append(event)

    await ctx.bus.subscribe(Warning, _collect)
    attempts = 0

    def _fail_activation(_trajectory: SessionTrajectory) -> None:
        nonlocal attempts
        attempts += 1
        raise RuntimeError("activation unavailable")

    monkeypatch.setattr(SessionTrajectory, "_activate_locked", _fail_activation)

    await ctx.send_message("hello")
    assert await wait_until(lambda: bool(warnings))
    await ctx.cleanup()

    assert attempts == 2
    assert [(warning.code, warning.reason) for warning in warnings] == [
        ("trajectory_activation_failed", "activation_failed")
    ]
    assert warnings[0].display_message is not None


@pytest.mark.asyncio
async def test_a_session_another_writer_holds_warns_the_user_with_the_reason(tmp_path: Path) -> None:
    """A refused activation, not only a failed one, tells the user recording is off, and why."""
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    warnings: list[Warning] = []

    async def _collect(event: Warning) -> None:
        warnings.append(event)

    await ctx.bus.subscribe(Warning, _collect)
    session_dir = _session_dir(ctx)
    ensure_owner_only_directory(trajectory_dir(session_dir))
    holder = WriterLease.try_acquire(trajectory_lease_path(session_dir))
    assert holder is not None
    try:
        await ctx.send_message("hello")
        assert await wait_until(lambda: bool(warnings))
    finally:
        await ctx.cleanup()
        holder.release()

    assert [(warning.code, warning.reason) for warning in warnings] == [("trajectory_activation_failed", "lease_held")]
    assert warnings[0].session_id == ctx.session_id


@pytest.mark.asyncio
async def test_a_session_root_reached_through_a_link_still_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The log is opened by helpers that refuse a link anywhere on the path, so
    session storage is handed out by its real path and recording starts."""
    real_root = tmp_path / "disk" / "root"
    real_root.mkdir(parents=True)
    symlink_or_skip(tmp_path / "root", real_root, target_is_directory=True)
    monkeypatch.setenv(SESSION_ROOT_DIR_ENV_VAR, str(tmp_path / "root"))
    ctx = await create_test_engine([MockResponse(text="hello back")], resolve_sessions_dir().parent)
    warnings: list[Warning] = []

    async def _collect(event: Warning) -> None:
        warnings.append(event)

    await ctx.bus.subscribe(Warning, _collect)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    assert warnings == []
    assert _one(_events(session_dir), EventType.TURN_FINISHED).session_id == ctx.session_id


@pytest.mark.asyncio
async def test_model_spans_nest_run_cycle_exchange(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    turn = _one(events, EventType.TURN_STARTED)
    run = _one(events, EventType.MODEL_RUN_STARTED)
    cycle = _one(events, EventType.MODEL_CYCLE_STARTED)
    exchange = _one(events, EventType.MODEL_EXCHANGE_STARTED)
    preparations = _of_type(events, EventType.PREPARATION_STARTED)
    pre_turn = next(event for event in preparations if event.payload["scope"] == "pre_turn")
    turn_preamble = next(event for event in preparations if event.payload["scope"] == "turn_preamble")

    assert run.parent_operation_id is None or run.parent_operation_id == turn.operation_id
    assert cycle.parent_operation_id == run.operation_id
    assert exchange.parent_operation_id == cycle.operation_id
    assert all(is_valid_analytics_id(event.operation_id or "") for event in (run, cycle, exchange))
    # Every span of the turn is stamped with the turn it belongs to.
    assert {event.turn_id for event in (run, cycle, exchange)} == {turn.turn_id}
    assert pre_turn.turn_id is None
    assert turn.payload["preparation_scope_operation_id"] == pre_turn.operation_id
    assert turn_preamble.turn_id == turn.turn_id
    assert turn_preamble.parent_operation_id is None
    assert [(link.relation, link.target_operation_id) for link in run.links] == [
        ("caused_by", turn_preamble.operation_id)
    ]


@pytest.mark.asyncio
async def test_usage_rides_on_the_exchange_that_finished(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    finished = _one(events, EventType.MODEL_EXCHANGE_FINISHED)
    # Usage belongs to the exchange layer and nowhere else.
    assert "usage" in finished.payload
    assert "usage" not in _one(events, EventType.MODEL_RUN_FINISHED).payload
    assert "usage" not in _one(events, EventType.MODEL_CYCLE_FINISHED).payload


@pytest.mark.asyncio
async def test_both_exchange_markers_name_the_profiles_provider(tmp_path: Path) -> None:
    """``provider`` is the model profile's on both markers.

    The wire client knows its own OTel dialect name, and every
    OpenAI-compatible client answers the same one — recording that under the
    key the finished marker fills from the profile would make one exchange
    look like two providers.
    """
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    started = _one(events, EventType.MODEL_EXCHANGE_STARTED)
    finished = _one(events, EventType.MODEL_EXCHANGE_FINISHED)
    assert started.payload["provider"] == "mock"
    assert finished.payload["provider"] == started.payload["provider"]


# ---------------------------------------------------------- context revision


@pytest.mark.asyncio
async def test_the_exchange_names_the_context_revision_it_sent(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    revision = _one(events, EventType.CONTEXT_REVISION_RECORDED)
    finished = _one(events, EventType.MODEL_EXCHANGE_FINISHED)

    assert finished.payload["context_revision_id"] == revision.payload["revision_id"]
    # Both markers name it: a process that dies before the terminal one still
    # leaves the exchange joined to the context it actually sent.
    started = _one(events, EventType.MODEL_EXCHANGE_STARTED)
    assert started.payload["context_revision_id"] == revision.payload["revision_id"]
    assert started.sequence > revision.sequence
    assert revision.payload["is_checkpoint"] is True
    assert "parent_revision_id" not in revision.payload
    # The membership rides on segments, so no revision line can outgrow the budget.
    refs = reassemble_array_slice(
        [
            event.payload
            for event in _of_type(events, EventType.SEGMENT)
            if event.payload["parent_event_id"] == revision.event_id
        ]
    )
    assert len(refs) == revision.payload["item_count"]
    assert all(is_valid_analytics_id(ref["item_id"]) for ref in refs)


@pytest.mark.asyncio
async def test_a_second_request_extends_the_actors_revision_chain(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
    )
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    revisions = _of_type(_events(session_dir), EventType.CONTEXT_REVISION_RECORDED)
    assert len(revisions) == 2
    assert revisions[1].payload["parent_revision_id"] == revisions[0].payload["revision_id"]
    # The tool round added items, so the second request is a delta, not a snapshot.
    assert revisions[1].payload["is_checkpoint"] is False
    assert revisions[1].payload["item_count"] > revisions[0].payload["item_count"]


@pytest.mark.asyncio
async def test_every_request_names_every_item_it_sends(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
    )
    await ctx.send_message("hello")
    raw_messages = await ctx.get_session_messages()
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    revisions = _of_type(events, EventType.CONTEXT_REVISION_RECORDED)
    assert len(revisions) == 2
    for revision in revisions:
        # Items minted mid-turn (the tool-result carrier above all) are stamped
        # where they are made, so no request sends an item the membership
        # cannot name.
        assert revision.payload["unidentified_item_count"] == 0

    carriers = {
        properties[ANALYTICS_ITEM_ID_KEY]
        for message in raw_messages
        if (properties := message.get("additional_properties") or {})
        and any(content["type"] == "function_result" for content in message.get("contents") or [])
    }
    assert len(carriers) == 1
    refs = reassemble_array_slice(
        [
            event.payload
            for event in _of_type(events, EventType.SEGMENT)
            if event.payload["parent_event_id"] == revisions[1].event_id
        ]
    )
    # The delta names the carrier the tool round appended.
    assert carriers <= {ref["item_id"] for ref in refs}


@pytest.mark.asyncio
async def test_an_injected_message_keeps_one_item_id_from_wire_to_session(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="done")], tmp_path)
    injection = ctx.engine.current.loaded.injection
    assert injection is not None
    injection.queue("mid-turn note", injection_id="ui-injection")

    await ctx.send_message("hello")
    raw_messages = await ctx.get_session_messages()
    session_dir = await _finish(ctx)

    injected = [
        message
        for message in raw_messages
        if message["role"] == "user" and (message.get("additional_properties") or {}).get("_injected") is True
    ]
    assert len(injected) == 1
    injected_item_id = injected[0]["additional_properties"][ANALYTICS_ITEM_ID_KEY]
    assert is_valid_analytics_id(injected_item_id)

    events = _events(session_dir)
    assert len(_of_type(events, EventType.TURN_STARTED)) == 1
    assert len(analyze_trajectory(trajectory_events_path(session_dir)).turns) == 1
    revision = _one(events, EventType.CONTEXT_REVISION_RECORDED)
    assert revision.payload["unidentified_item_count"] == 0
    refs = reassemble_array_slice(
        [
            event.payload
            for event in _of_type(events, EventType.SEGMENT)
            if event.payload["parent_event_id"] == revision.event_id
        ]
    )
    assert injected_item_id in {ref["item_id"] for ref in refs}


@pytest.mark.asyncio
async def test_tool_terminals_share_the_carrier_id_named_by_the_next_revision(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [
            MockResponse(
                tool_calls=[
                    ("echo", "call-1", {"message": "first"}),
                    ("echo", "call-2", {"message": "second"}),
                ]
            ),
            MockResponse(text="done"),
        ],
        tmp_path,
    )
    await ctx.send_message("hello")
    raw_messages = await ctx.get_session_messages()
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    finished = _of_type(events, EventType.TOOL_OPERATION_FINISHED)
    revisions = _of_type(events, EventType.CONTEXT_REVISION_RECORDED)
    carriers = [
        message
        for message in raw_messages
        if message["role"] == "tool"
        and any(content["type"] == "function_result" for content in message.get("contents") or [])
    ]
    assert len(finished) == 2
    assert len(carriers) == 1
    carrier_item_id = carriers[0]["additional_properties"][ANALYTICS_ITEM_ID_KEY]
    assert {event.payload["result_carrier_item_id"] for event in finished} == {carrier_item_id}
    refs = reassemble_array_slice(
        [
            event.payload
            for event in _of_type(events, EventType.SEGMENT)
            if event.payload["parent_event_id"] == revisions[1].event_id
        ]
    )
    assert carrier_item_id in {ref["item_id"] for ref in refs}


@pytest.mark.asyncio
async def test_real_phase4_emission_closes_analytics_fan_in(tmp_path: Path) -> None:
    """A real strategy/write/read pass agrees on Phase-4 segmented evidence."""
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
        compaction=CompactionConfig(),
    )
    executor = ctx.engine.current.loaded.bindings
    assert executor is not None
    strategy = executor.backend.compaction_strategy
    assert strategy is not None
    # The first request has no tool group, so it records no phase. The second
    # request necessarily reaches Phase 4 and drops the just-finished result
    # before the successor revision is recorded.
    strategy.trigger_pct = 0.0
    strategy.target_pct = 0.0

    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    phase4 = [
        event for event in _of_type(events, EventType.COMPACTION_PHASE_FINISHED) if event.payload["phase"] == "phase4"
    ]
    assert len(phase4) == 1
    phase = phase4[0]
    consumed_item_ids = set(
        reassemble_array_slice(
            [
                event.payload
                for event in _of_type(events, EventType.SEGMENT)
                if event.payload["parent_event_id"] == phase.event_id
                and event.payload["field_pointer"] == "/payload/consumed_item_ids"
            ]
        )
    )
    tool = _one(events, EventType.TOOL_OPERATION_FINISHED)
    result_item_ids = {tool.payload["result_item_id"], tool.payload["result_carrier_item_id"]}
    assert result_item_ids <= consumed_item_ids

    successor_id = phase.payload["next_exchange_operation_id"]
    successor = next(
        event for event in _of_type(events, EventType.MODEL_EXCHANGE_STARTED) if event.operation_id == successor_id
    )
    assert phase.sequence < successor.sequence
    revision_id = successor.payload["context_revision_id"]
    revision = next(
        event
        for event in _of_type(events, EventType.CONTEXT_REVISION_RECORDED)
        if event.payload["revision_id"] == revision_id
    )
    successor_refs = reassemble_array_slice(
        [
            event.payload
            for event in _of_type(events, EventType.SEGMENT)
            if event.payload["parent_event_id"] == revision.event_id
            and event.payload["field_pointer"] == "/payload/refs"
        ]
    )
    assert result_item_ids.isdisjoint(ref["item_id"] for ref in successor_refs)

    turn = analyze_trajectory(trajectory_events_path(session_dir)).turns[0]
    assert turn.compute_cp_ns.precision is Precision.EXACT
    assert turn.response_cp_ns.precision is Precision.EXACT
    assert not any("tool result fan-in" in diagnostic for diagnostic in turn.diagnostics)
    assert turn.flow is not None
    operation_index = {
        (operation.family, operation.operation_id): index for index, operation in enumerate(turn.operations)
    }
    causal_edges = set(turn.flow.causal_edges())
    compaction_id = phase.parent_operation_id
    assert compaction_id is not None
    assert (
        operation_index[("tool.operation", tool.operation_id)],
        operation_index[("compaction", compaction_id)],
    ) in causal_edges
    assert (
        operation_index[("compaction", compaction_id)],
        operation_index[("model.exchange", successor_id)],
    ) in causal_edges


# --------------------------------------------------------------- tool turn


@pytest.mark.asyncio
async def test_a_tool_round_records_one_operation_under_its_exchange(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": SECRET_ARGUMENT})]), MockResponse(text="done")],
        tmp_path,
    )
    await ctx.send_message(SECRET_PROMPT)
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    started = _one(events, EventType.TOOL_OPERATION_STARTED)
    finished = _one(events, EventType.TOOL_OPERATION_FINISHED)
    exchanges = _of_type(events, EventType.MODEL_EXCHANGE_STARTED)

    assert started.operation_id == finished.operation_id
    assert started.parent_operation_id == exchanges[0].operation_id
    assert started.payload["tool_name"] == "echo"
    assert finished.payload["outcome"] == "success"
    # The tool ran between the exchange that asked for it and the next one.
    assert events.index(started) > events.index(exchanges[0])
    assert events.index(finished) < events.index(exchanges[1])


@pytest.mark.asyncio
async def test_session_items_carry_the_operation_that_produced_them(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
    )
    await ctx.send_message("hello")
    raw_messages = await ctx.get_session_messages()
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    exchange_ids = {event.operation_id for event in _of_type(events, EventType.MODEL_EXCHANGE_STARTED)}
    tool_id = _one(events, EventType.TOOL_OPERATION_STARTED).operation_id

    item_ids: set[str] = set()
    assistant_operations: set[str] = set()
    call_operations: set[str] = set()
    result_operations: set[str] = set()
    for message in raw_messages:
        properties = message.get("additional_properties") or {}
        item_id = properties.get(ANALYTICS_ITEM_ID_KEY)
        if item_id is not None:
            assert is_valid_analytics_id(item_id)
            assert item_id not in item_ids, "an item id is minted once and never reused"
            item_ids.add(item_id)
        if message["role"] == "assistant" and properties.get(OPERATION_ID_KEY):
            assistant_operations.add(properties[OPERATION_ID_KEY])
        for content in message.get("contents") or []:
            content_properties = content.get("additional_properties") or {}
            operation_id = content_properties.get(OPERATION_ID_KEY)
            if content["type"] == "function_call" and operation_id:
                call_operations.add(operation_id)
            if content["type"] == "function_result" and operation_id:
                result_operations.add(operation_id)

    assert item_ids, "persisted messages must carry stable analytics item ids"
    assert assistant_operations <= exchange_ids
    # A call and its result share the tool operation they belong to.
    assert call_operations == {tool_id}
    assert result_operations == {tool_id}


@pytest.mark.asyncio
async def test_an_executed_call_names_the_item_that_requested_it(tmp_path: Path) -> None:
    """``call_item_id`` is about the request, not about whether the tool ran."""
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
    )
    await ctx.send_message("hello")
    raw_messages = await ctx.get_session_messages()
    session_dir = await _finish(ctx)

    call_item_ids = [
        (content.get("additional_properties") or {}).get(ANALYTICS_ITEM_ID_KEY)
        for message in raw_messages
        for content in message.get("contents") or []
        if content["type"] == "function_call"
    ]
    assert len(call_item_ids) == 1

    started = _one(_events(session_dir), EventType.TOOL_OPERATION_STARTED)
    assert started.payload["call_item_id"] == call_item_ids[0]


# ----------------------------------------------------------------- privacy


@pytest.mark.asyncio
async def test_no_conversation_content_or_path_reaches_the_log(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": SECRET_ARGUMENT})]), MockResponse(text="done")],
        tmp_path,
    )
    await ctx.send_message(SECRET_PROMPT)
    session_dir = await _finish(ctx)

    raw = trajectory_events_path(session_dir).read_bytes()
    for secret in (SECRET_PROMPT, SECRET_ARGUMENT, SECRET_RESULT_FRAGMENT, "hello back", "done"):
        assert secret.encode() not in raw, f"{secret!r} leaked into the trajectory log"
    # Filesystem paths are never recorded either.
    assert str(tmp_path).encode() not in raw
    assert str(session_dir.name).encode() not in raw


@pytest.mark.asyncio
async def test_recorded_reasons_are_enum_codes_not_free_text(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    assert _one(events, EventType.TURN_FINISHED).payload["end_reason"] == "completed"
    assert _one(events, EventType.RUNTIME_FINISHED).payload["reason"] == "graceful_shutdown"
    for event in events:
        for key, value in event.payload.items():
            if key.endswith(("_reason", "_kind", "outcome")) and isinstance(value, str):
                # A code is a short snake_case token; a sentence is content.
                assert " " not in value, f"{event.event_type}.{key} carries free text: {value!r}"


# -------------------------------------------------- sessions without a log


@pytest.mark.asyncio
async def test_an_engine_without_a_state_store_writes_no_trajectory(
    tmp_path: Path, agent_engine: Any, *, engine_services
) -> None:
    bus = EventBus()
    engine = agent_engine(bus, settings=Settings())
    assert engine.session_dir is None

    # What the build binds when there is no store: a session with nowhere to
    # write. A turn driven through it degrades instead of creating a log.
    trajectory = engine_services(engine).trajectory_recorder.bind_session(
        session_id="d0f1a2b3-0000-4000-8000-000000000000",
        session_dir=None,
        write_lock_path=None,
        session_start_info=lambda: None,
    )
    turn_id = await engine_services(engine).trajectory_recorder.turn_started(
        turn_id="cccccccc-dddd-4eee-8fff-000000000001",
        logical_turn_id="cccccccc-dddd-4eee-8fff-000000000001",
        turn_number=1,
        is_retry=False,
        agent_profile_fingerprint="a" * 64,
        model_profile_fingerprint="b" * 64,
        primary_cwd=str(tmp_path),
        history_state=None,
    )
    # The recorder answers with the id it was handed; only the writing is skipped.
    assert turn_id == "cccccccc-dddd-4eee-8fff-000000000001"
    await engine_services(engine).trajectory_recorder.turn_finished(end_reason=TurnEndReason.COMPLETED)

    assert not trajectory.is_active
    assert trajectory.disabled_reason == TrajectoryDisabledReason.NO_SESSION_DIR
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_a_session_recorded_before_the_feature_still_loads_and_appends(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="first")], tmp_path)
    await ctx.send_message("hello")
    session_id = ctx.session_id
    session_dir = await _finish(ctx)

    # An old session has no trajectory directory at all; remove it to simulate one.
    events_path = trajectory_events_path(session_dir)
    first_events = _events(session_dir)
    for child in trajectory_dir(session_dir).iterdir():
        child.unlink()
    trajectory_dir(session_dir).rmdir()
    assert not events_path.exists()

    resumed = await create_test_engine([MockResponse(text="second")], tmp_path)
    await resumed.engine.on_session_restore(SessionRestore(session_id=session_id))
    await resumed.send_message("again")
    resumed_dir = _session_dir(resumed)
    await resumed.cleanup()

    # The history survived and a fresh log was started for the new runtime.
    messages = await resumed.store.load_session_raw(session_id)
    assert messages is not None
    assert len([message for message in messages if message["role"] == "user"]) >= 2
    events = _events(resumed_dir)
    assert _of_type(events, EventType.RUNTIME_STARTED)
    assert first_events[0].runtime_id != events[0].runtime_id


@pytest.mark.asyncio
@pytest.mark.timeout(90)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_second_runtime_appends_to_the_same_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    # As in the delayed-write shape regression below, this checks lossless
    # persistence across runtimes, not the production 3s disk-stall deadline.
    def allow_recording_io(ctx: PipelineTestContext) -> None:
        trajectory = engine_services(ctx.engine).trajectory_recorder.trajectory
        assert trajectory is not None
        monkeypatch.setattr(trajectory, "_write_ack_timeout", ENGINE_TURN_TIMEOUT)
        if trajectory.writer is not None:
            monkeypatch.setattr(trajectory.writer, "_write_ack_timeout", ENGINE_TURN_TIMEOUT)

    ctx = await create_test_engine([MockResponse(text="first")], tmp_path)
    allow_recording_io(ctx)
    await ctx.send_message("hello")
    session_id = ctx.session_id
    session_dir = await _finish(ctx)
    first = _events(session_dir)

    resumed = await create_test_engine([MockResponse(text="second")], tmp_path)
    allow_recording_io(resumed)
    await resumed.engine.on_session_restore(SessionRestore(session_id=session_id))
    allow_recording_io(resumed)
    await resumed.send_message("again")
    await resumed.cleanup()

    events = _events(session_dir)
    assert len(events) > len(first)
    assert [event.to_dict() for event in events[: len(first)]] == [event.to_dict() for event in first]
    runtimes = [event.runtime_id for event in _of_type(events, EventType.RUNTIME_STARTED)]
    # Two runtimes, one continuous sequence: the accounted prefix spans both.
    assert len(set(runtimes)) == 2
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))


@pytest.mark.asyncio
async def test_the_log_directory_is_owner_only(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    from chrys.foundation.platform import get_platform

    if get_platform().is_windows:
        pytest.skip("POSIX permission bits")
    assert trajectory_dir(session_dir).stat().st_mode & 0o077 == 0
    assert trajectory_events_path(session_dir).stat().st_mode & 0o077 == 0


# ------------------------------------------------------------------ shape


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_writer", [False, True], ids=["normal-write", "delayed-write"])
# One shared 45s turn budget plus two 15s closure acknowledgements and a 2s
# worker join need teardown headroom beyond the suite's ordinary 60s timeout.
@pytest.mark.timeout(90)
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_every_line_fits_the_budget_and_names_its_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delay_writer: bool, *, engine_services
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
        workspace=Workspace.from_cwd(str(workspace)),
    )
    trajectory = engine_services(ctx.engine).trajectory_recorder.trajectory
    assert trajectory is not None
    assert trajectory.writer is None  # configuration precedes lazy activation
    # This test checks lossless event shape, not the production disk-stall
    # deadline. Windows -n4 exceeded the normal 3s ack budget on a healthy
    # write, degrading the writer and replacing all later events with a gap.
    # Keep the real thread, descriptor, acknowledgements and no-gap assertion;
    # the foundation writer tests still pin the short timeout/degrade policy.
    monkeypatch.setattr(trajectory, "_write_ack_timeout", ENGINE_TURN_TIMEOUT)

    write_entered = threading.Event()
    release_write = threading.Event()
    original_write = FdWriteBackend.write

    def _starts_a_turn(data: memoryview) -> bool:
        # The writer retries a short write from mid-buffer, so a slice can open
        # on a partial line. Decode defensively: raising in here would surface
        # as a degraded writer, i.e. as the very failure being reproduced.
        for line in data.tobytes().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event_type") == EventType.TURN_STARTED:
                return True
        return False

    def delayed_write(backend: FdWriteBackend, data: memoryview) -> int:
        # Prelude writes stay synchronous and healthy. Delay the first async
        # event at the actual file boundary, reproducing the contended Windows
        # worker that exceeded the production 3s ack budget and lost slots 4+.
        if _starts_a_turn(data):
            write_entered.set()
            if not release_write.wait(timeout=ENGINE_TEST_WAIT_TIMEOUT):
                raise TimeoutError("test did not release the trajectory write")
        return original_write(backend, data)

    if delay_writer:
        monkeypatch.setattr(FdWriteBackend, "write", delayed_write)
    run = asyncio.create_task(ctx.send_message("hello"))
    session_dir = _session_dir(ctx)
    try:
        if delay_writer:
            await wait_for(write_entered.is_set, timeout=ENGINE_TURN_TIMEOUT, description="trajectory write entered")
            assert not await wait_until(run.done, timeout=DEFAULT_WRITE_ACK_TIMEOUT_SECONDS + 0.25)
            assert trajectory.writer is not None
            assert trajectory.writer.state is WriterState.ACTIVE
            release_write.set()
        await wait_for(run.done, timeout=ENGINE_TEST_WAIT_TIMEOUT, description="trajectory shape turn finalized")
        await run
    finally:
        release_write.set()
        if not run.done():
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await run
        await ctx.cleanup()

    for line in trajectory_events_path(session_dir).read_bytes().splitlines():
        assert len(line) + 1 <= 4096
    for event in _events(session_dir):
        assert event.actor.kind
        assert event.schema_version >= 1


@pytest.mark.asyncio
async def test_an_approval_round_records_a_pair_the_writer_accepts(tmp_path: Path) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
        approval_default="require",
    )
    await ctx.send_message("please echo")
    requests = [event for event in ctx.events if isinstance(event, ApprovalRequest)]
    assert len(requests) == 1
    await ctx.approve(requests[0].request_id)
    await wait_for_idle(ctx)
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    requested = _one(events, EventType.APPROVAL_REQUESTED)
    resolved = _one(events, EventType.APPROVAL_RESOLVED)
    # Both events reached the file: an id the writer cannot validate would
    # have turned each of them into a gap instead.
    assert is_valid_analytics_id(requested.payload["approval_request_id"])
    assert resolved.payload["approval_request_id"] == requested.payload["approval_request_id"]
    assert resolved.payload["decision"] == "approved"
    assert resolved.payload["target_tool_operation_id"] == requested.payload["target_tool_operation_id"]


@pytest.mark.parametrize("reload_during_review", [False, True])
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_auto_formal_executes_the_tool_and_records_its_judgement(tmp_path, monkeypatch, reload_during_review):
    from chrys.service.tools.registry import ToolRegistry

    def load_builtins(
        registry,
        categories,
        runtime=None,
        settings=None,
        shell_filter_config=None,
        session_id=None,
        session_dir=None,
        web=None,
        background_tasks=None,
        session_wide_background=False,
    ):
        registry.register(echo_tool)
        return [echo_tool]

    monkeypatch.setattr(
        ToolRegistry, "load_builtins", create_autospec(ToolRegistry.load_builtins, side_effect=load_builtins)
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def response(client, messages, *, stream, options, timeout):
        if reload_during_review:
            entered.set()
            await release.wait()
        return ChatResponse(messages=[Message("assistant", [json.dumps(predicate_values())])])

    respond = create_autospec(judge_module.get_final_response, side_effect=response)
    monkeypatch.setattr(judge_module, "get_final_response", respond)
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
        approval_default="require",
    )
    session_dir = _session_dir(ctx)
    run_task = reload_task = None
    try:
        freeze_process_env()
        persist({"approval.auto_review": "formal", "model.profile.active": "mock-profile"})
        await ctx.bus.publish(SettingsReload(), raise_handler_errors=True)
        await ctx.bus.publish(SetApprovalMode(mode="auto", persist=False), raise_handler_errors=True)
        assert ctx.engine.current.loaded.approval_judge._auto_review == "formal"
        if reload_during_review:
            bound_judge = ctx.engine.current.loaded.approval_judge
            run_task = asyncio.create_task(ctx.send_message("please echo"))
            await wait_for(
                lambda: entered.is_set() or run_task.done(),
                timeout=ENGINE_TURN_TIMEOUT,
                description="Formal review in flight",
            )
            assert entered.is_set() and not run_task.done()
            assert persist({"approval.auto_review": "direct"}).ok
            reload_task = asyncio.create_task(ctx.bus.publish(SettingsReload(), raise_handler_errors=True))
            release.set()
            await wait_for(run_task.done, timeout=ENGINE_TURN_TIMEOUT, description="Formal turn finalized")
            await run_task
            await wait_for(reload_task.done, timeout=ENGINE_TURN_TIMEOUT, description="Review strategy reloaded")
            await reload_task
            assert ctx.engine.current.loaded.approval_judge is not bound_judge
            assert ctx.engine.current.loaded.approval_judge._auto_review == "direct"
        else:
            await ctx.send_message("please echo")
        raw = await ctx.get_session_messages()
        assert any(content.get("result") == "echo: hi" for message in raw for content in message.get("contents", []))
        assert respond.await_count == 1
    finally:
        release.set()
        for task in (run_task, reload_task):
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await ctx.cleanup()
    resolved = _one(_events(session_dir), EventType.APPROVAL_RESOLVED).payload
    assert resolved["decision"] == "approved"
    assert resolved["decider"] == "judge"
    assert resolved["formal_judge"]["stages"] == ["predicate"]
    assert type(resolved["formal_judge"]["decision_version"]) is int
    assert resolved["formal_judge"]["decision_version"] == 1
    assert resolved["formal_judge"]["predicate_results"] == [{"id": key, "value": False} for key in predicate_values()]


@pytest.mark.asyncio
async def test_an_interrupted_run_may_stay_open_but_its_turn_still_closes(tmp_path: Path) -> None:
    """The span policy: an interrupt may orphan a ``started``, never a turn.

    ``model.run.finished`` is written from a scope that is already unwinding
    under cancellation, where nothing can be awaited to completion, so it is
    best effort by design — and a process that dies first leaves the run open.
    An unclosed span is a shape readers already handle; what analysis counts on
    is the turn, which the recorder closes itself if the run never got there.
    Do not "fix" a dangling run by holding the interrupt open for its terminal.
    """
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="done")],
        tmp_path,
        approval_default="require",
    )
    await ctx.send_message("please echo")
    assert [event for event in ctx.events if isinstance(event, ApprovalRequest)]
    await ctx.send_interrupt()
    await wait_for_idle(ctx)
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    assert _of_type(events, EventType.MODEL_RUN_STARTED)
    assert len(_of_type(events, EventType.MODEL_RUN_FINISHED)) <= len(_of_type(events, EventType.MODEL_RUN_STARTED))
    finished = _one(events, EventType.TURN_FINISHED)
    assert finished.payload["end_reason"] in {TurnEndReason.INTERRUPTED, TurnEndReason.PROCESS_EXIT}
    assert finished.turn_id == _one(events, EventType.TURN_STARTED).turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_text", ["", "continue with this turn"], ids=["bare-resume", "guided-retry"])
async def test_interrupt_then_resume_analyzes_as_one_logical_turn(tmp_path: Path, retry_text: str) -> None:
    ctx = await create_test_engine(
        [MockResponse(tool_calls=[("echo", "call-1", {"message": "hi"})]), MockResponse(text="resumed")],
        tmp_path,
        approval_default="require",
    )
    await ctx.bus.publish(UserMessage(text="please echo"))
    assert await wait_for_event(ctx.events, ApprovalRequest, timeout=20.0)
    await ctx.send_interrupt()
    await wait_for_idle(ctx)
    await ctx.bus.publish(UserRetry(text=retry_text))
    await wait_for_idle(ctx)
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    starts = _of_type(events, EventType.TURN_STARTED)
    assert len(starts) == 2
    assert [event.payload["turn_number"] for event in starts] == [1, 1]
    assert [event.payload["is_retry"] for event in starts] == [False, True]
    analysis = analyze_trajectory(trajectory_events_path(session_dir))
    assert len(analysis.turns) == 1
    assert tuple(attempt.turn_id for attempt in analysis.turns[0].attempts) == tuple(event.turn_id for event in starts)


@pytest.mark.asyncio
async def test_a_rollback_to_welcome_opens_a_branch_like_any_other(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="one"), MockResponse(text="two")], tmp_path)
    session_dir = _session_dir(ctx)
    await ctx.send_message("first")
    # The reset deletes the session's messages and restarts on this very
    # directory. The log is not the conversation and is not deleted with it:
    # the same file carries both sides of the reset, which is the only way the
    # branch on the far side means anything.
    await ctx.bus.publish(UserRollback(target_turn=0, revert_changes=False))
    # The reset restarts the session; its SessionReady is the completion signal.
    await wait_for_event(ctx.events, SessionReady, min_count=2, timeout=20.0)
    await ctx.send_message("after the reset")
    await ctx.cleanup()

    events = _events(session_dir)
    rollback = _one(events, EventType.SESSION_ROLLBACK)
    new_branch = rollback.payload["new_branch_id"]
    assert new_branch != rollback.payload["old_branch_id"]
    # The turn numbering starts over after the reset, so the second "turn 1"
    # is only distinguishable from the first by the branch it was recorded on.
    turn_starts = _of_type(events, EventType.TURN_STARTED)
    assert [event.payload["turn_number"] for event in turn_starts] == [1, 1]
    assert turn_starts[0].branch_id != turn_starts[1].branch_id
    assert turn_starts[1].branch_id == new_branch


@pytest.mark.asyncio
@pytest.mark.parametrize("hook_mode", ["blocking", "fire_and_forget"])
async def test_a_rollback_that_opens_the_inherited_log_still_records_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_mode: str, *, engine_services
) -> None:
    ctx = await create_test_engine([MockResponse(text="one")], tmp_path)
    await ctx.send_message("first")
    session_id = ctx.session_id
    session_dir = await _finish(ctx)

    import chrys.orchestration.engine.loader as loader_module

    managers: list[HookManager] = []
    background_result_ready = asyncio.Event()
    release_background_result = asyncio.Event()
    original_run = HookRunner.run_and_wait

    async def held_result(
        self: HookRunner,
        hook: HookConfig,
        payload: dict[str, Any],
        *,
        payload_text: str | None = None,
        result: HookResult | None = None,
    ) -> HookResult:
        result = await original_run(self, hook, payload, payload_text=payload_text, result=result)
        background_result_ready.set()
        await release_background_result.wait()
        return result

    if hook_mode == "fire_and_forget":
        # Keep real subprocess execution, but delay result handling until the
        # log has closed. This makes a slow background completion deterministic.
        monkeypatch.setattr(HookRunner, "run_and_wait", held_result)

    async def _session_start_hook_manager(*_args: Any, **_kwargs: Any) -> HookManager:
        manager = HookManager(
            file=HooksFile(
                hooks=[
                    HookConfig(
                        id=f"rollback-{hook_mode}",
                        event=HookEvent.SESSION_START,
                        run=HookRun(type="command", argv=[sys.executable, "-c", "pass"]),
                        execution=HookExecution(mode=hook_mode, timeout_seconds=5),
                    )
                ],
                source=str(tmp_path / "hooks.yaml"),
            ),
            hooks_dir=tmp_path / "hooks",
        )
        managers.append(manager)
        return manager

    monkeypatch.setattr(loader_module.AgentLoader, "build_hook_manager", _session_start_hook_manager)

    resumed = await create_test_engine([MockResponse(text="two")], tmp_path)
    try:
        try:
            await resumed.engine.on_session_restore(SessionRestore(session_id=session_id))
            assert _session_dir(resumed) == session_dir
            ready_before = len([event for event in resumed.events if isinstance(event, SessionReady)])

            # No message first: the inherited log is still closed, so the reset is
            # what opens it — and opening it recovers the tail under the session
            # write lock the reset itself is holding.
            await asyncio.wait_for(
                resumed.bus.publish(UserRollback(target_turn=0, revert_changes=False)),
                timeout=5.0,
            )
            await wait_for_event(resumed.events, SessionReady, min_count=ready_before + 1, timeout=5.0)
            await resumed.send_message("after the reset")
            if hook_mode == "fire_and_forget":
                await wait_for(
                    background_result_ready.is_set,
                    timeout=ENGINE_TURN_TIMEOUT,
                    description="background hook result held before settlement",
                )
            trajectory = engine_services(resumed.engine).trajectory_recorder.trajectory
            assert trajectory is not None
            fingerprint_key = trajectory.fingerprint_key
            assert fingerprint_key is not None
        finally:
            await resumed.cleanup()

        events = _events(session_dir)
        rollback = _one(events, EventType.SESSION_ROLLBACK)
        assert rollback.payload["new_branch_id"] != rollback.payload["old_branch_id"]
        assert _of_type(events, EventType.BRANCH_SUPERSEDED)
        turn_starts = _of_type(events, EventType.TURN_STARTED)
        assert [event.payload["turn_number"] for event in turn_starts] == [1, 1]
        assert turn_starts[1].branch_id == rollback.payload["new_branch_id"]

        analysis = analyze_trajectory(trajectory_events_path(session_dir), fingerprint_key=fingerprint_key)
        assert analysis.diagnostics.integrity_unresolved is False
        assert analysis.diagnostics.rollback_projection_unresolved is False
        assert len(analysis.turns) == 1
        assert analysis.turns[0].diagnostics == ()
    finally:
        release_background_result.set()
        await asyncio.wait_for(
            asyncio.gather(*(tracked.task for manager in managers for tracked in manager._inflight)),
            timeout=ENGINE_TURN_TIMEOUT,
        )


@pytest.mark.asyncio
async def test_deleting_the_live_session_takes_its_log_with_it(tmp_path: Path) -> None:
    ctx = await create_test_engine([MockResponse(text="one")], tmp_path)
    session_dir = _session_dir(ctx)
    await ctx.send_message("first")
    assert trajectory_events_path(session_dir).is_file()

    deleted: list[SessionDeleted] = []

    async def _collect(event: SessionDeleted) -> None:
        deleted.append(event)

    await ctx.bus.subscribe(SessionDeleted, _collect)
    await ctx.bus.publish(SessionDelete(session_id=ctx.session_id))
    assert await wait_until(lambda: bool(deleted)), "SessionDeleted never arrived"
    await ctx.cleanup()

    assert not session_dir.exists()
    # A writer still holding the log would have made this a rename into
    # ``.tombstones/``, where the deleted conversation waits for a sweep that
    # only runs when a store is next constructed.
    graveyard = tmp_path / "sessions" / ".tombstones"
    assert not graveyard.is_dir() or list(graveyard.iterdir()) == []


@pytest.mark.asyncio
async def test_session_started_names_the_profiles_the_switch_says_it_left(tmp_path: Path) -> None:
    """The recorder opens the log at its first event — which a model switch
    before any prompt gets to be. What it reports the session started with is
    the build that started it, not the one running when the log opened."""
    ctx = await create_test_engine([MockResponse(text="hello back")], tmp_path)
    registry = ctx.engine.model_registry
    assert registry is not None
    registry.register(ModelProfile(id="mock-profile-2", name="mock2", provider="mock", model_id="mock-2"))
    await ctx.engine._on_set_model_profile(SetModelProfile(profile_id="mock-profile-2"))
    # The switch is what opened the log; the turn after it is what keeps it.
    await ctx.send_message("hello")
    session_dir = await _finish(ctx)

    events = _events(session_dir)
    started = _one(events, EventType.SESSION_STARTED)
    switched = _one(events, EventType.PROFILE_SWITCHED)
    assert switched.payload["kind"] == ProfileKind.MODEL
    # The chain connects: what the session opened with is what the switch left.
    assert started.payload["model_profile_fingerprint"] == switched.payload["from_fingerprint"]
    assert started.payload["model_profile_fingerprint"] != switched.payload["to_fingerprint"]
