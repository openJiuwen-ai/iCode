# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool, approval, hook, wait and sub-agent trace helpers recording under an in-memory sink."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from typing import Any

import pytest

from chrys.foundation.tool_result_metadata import (
    PROCESS_EXIT_CODE_METADATA_KEY,
    PROCESS_TIMED_OUT_METADATA_KEY,
    SHELL_EXIT_CODE_METADATA_KEY,
    SHELL_TIMED_OUT_METADATA_KEY,
    TOOL_ERROR_KIND_METADATA_KEY,
    TOOL_FAILED_METADATA_KEY,
)
from chrys.foundation.trajectory.context import (
    bind_tool_operation,
    current_trajectory,
    reset_tool_operation,
    side_call_scope,
    sub_agent_actor,
    trajectory_scope,
)
from chrys.foundation.trajectory.envelope import (
    ActorKind,
    ActorRole,
    EventDraft,
    LinkRelation,
    MeasurementSource,
    malformed_id_pointers,
)
from chrys.foundation.trajectory.event_types import EventType, RetryMode, ToolOutcome, TraceCoverage, WaitCategory
from chrys.foundation.trajectory.fingerprint import (
    DOMAIN_TOOL_ARGUMENTS,
    DOMAIN_TOOL_CONTENT,
    fingerprint_json,
    fingerprint_text,
)
from chrys.foundation.trajectory.ids import is_valid_analytics_id, new_analytics_id
from chrys.foundation.trajectory.metadata import (
    ANALYTICS_ITEM_ID_KEY,
    OPERATION_ID_KEY,
    TOOL_RESULT_ITEM_ID_METADATA_KEY,
)
from chrys.foundation.trajectory.writer import EmitResult
from chrys.service.trajectory.approvals import ApprovalDecider, ApprovalDecision, ApprovalTrace
from chrys.service.trajectory.hooks import HookOperationTrace, HookOutcome
from chrys.service.trajectory.preparation import PreparationOutcome, PreparationScope, PreparationTrace
from chrys.service.trajectory.retries import RetryBackoffTrace
from chrys.service.trajectory.sub_agents import SubAgentStatus, SubAgentTrace
from chrys.service.trajectory.tools import (
    TOKENIZER_FINGERPRINT,
    ToolOperationTrace,
    tool_operation_id,
    tool_outcome,
)
from chrys.service.trajectory.waits import WaitOutcome, WaitTrace
from tests.service.trajectory._fakes import (
    FINGERPRINT_KEY,
    SESSION_ID,
    CancelAckSink,
    FakeSink,
    make_context,
)

MONOTONIC = {"source": MeasurementSource.MONOTONIC_CLOCK, "method_version": 1}


# ----------------------------------------------------------------- ToolOperationTrace


def test_tool_operation_open_returns_none_without_ambient_context() -> None:
    assert current_trajectory() is None
    assert ToolOperationTrace.open({OPERATION_ID_KEY: new_analytics_id()}) is None


def test_tool_operation_open_binds_stamped_ids_and_mints_when_missing() -> None:
    context = make_context()
    operation_id = new_analytics_id()
    result_item_id = new_analytics_id()
    with trajectory_scope(context):
        stamped = ToolOperationTrace.open(
            {OPERATION_ID_KEY: operation_id, TOOL_RESULT_ITEM_ID_METADATA_KEY: result_item_id}
        )
        minted = ToolOperationTrace.open({})
    assert stamped is not None and minted is not None
    assert stamped.operation_id == operation_id
    assert stamped.context is context
    assert is_valid_analytics_id(minted.operation_id)
    assert minted.operation_id != operation_id


def test_tool_operation_id_reads_only_well_formed_metadata() -> None:
    operation_id = new_analytics_id()
    assert tool_operation_id({OPERATION_ID_KEY: operation_id}) == operation_id
    assert tool_operation_id({OPERATION_ID_KEY: ""}) is None
    assert tool_operation_id({}) is None
    assert tool_operation_id(None) is None
    assert tool_operation_id("not a mapping") is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_tool_operation_started_payload_fingerprints_arguments() -> None:
    sink = FakeSink()
    cycle_id = new_analytics_id()
    exchange_id = new_analytics_id()
    context = make_context(sink).with_cycle(cycle_id).with_exchange(exchange_id)
    operation_id = new_analytics_id()
    call_item_id = new_analytics_id()
    arguments = {"path": "/tmp/secret.txt", "count": 3}
    with trajectory_scope(context):
        trace = ToolOperationTrace.open({OPERATION_ID_KEY: operation_id})
    assert trace is not None
    await trace.started(
        tool_name="read_file",
        tool_kind="builtin",
        batch_index=2,
        invocation_order=1,
        arguments=arguments,
        invocation_metadata={ANALYTICS_ITEM_ID_KEY: call_item_id},
    )
    event = sink.only(EventType.TOOL_OPERATION_STARTED)
    assert event.operation_id == operation_id
    assert event.parent_operation_id == exchange_id
    assert event.turn_id == context.turn_id
    assert event.actor == context.actor
    payload = event.payload
    assert payload["tool_name"] == "read_file"
    assert payload["tool_kind"] == "builtin"
    assert payload["batch_index"] == 2
    assert payload["invocation_order"] == 1
    assert payload["parent_model_operation_id"] == exchange_id
    assert payload["call_item_id"] == call_item_id
    assert payload["argument_fingerprint"] == fingerprint_json(FINGERPRINT_KEY, DOMAIN_TOOL_ARGUMENTS, arguments)
    assert "/tmp/secret.txt" not in repr(payload)
    assert "arguments" not in payload
    assert "tool_context" not in payload


@pytest.mark.asyncio
async def test_a_started_tool_operation_names_the_server_or_skill_behind_the_tool() -> None:
    """The tool name alone does not say which MCP server, or which skill, served the call."""
    sink = FakeSink()
    with trajectory_scope(make_context(sink)):
        trace = ToolOperationTrace.open({})
    assert trace is not None
    await trace.started(
        tool_name="search_issues",
        tool_kind="mcp",
        batch_index=None,
        invocation_order=0,
        arguments={},
        invocation_metadata={},
        tool_context={"server_name": "github", "remote_name": "search-issues"},
    )

    payload = sink.only(EventType.TOOL_OPERATION_STARTED).payload
    assert payload["tool_context"] == {"server_name": "github", "remote_name": "search-issues"}


@pytest.mark.asyncio
async def test_tool_operation_started_omits_optional_keys_and_fingerprint_without_key() -> None:
    sink = FakeSink(fingerprint_key=None)
    context = make_context(sink)
    trace = ToolOperationTrace(
        context, operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    await trace.started(
        tool_name="shell",
        tool_kind="builtin",
        batch_index=None,
        invocation_order=None,
        arguments="ls -la",
        invocation_metadata={},
    )
    payload = sink.only(EventType.TOOL_OPERATION_STARTED).payload
    assert set(payload) == {"tool_name", "tool_kind", "parent_model_operation_id"}
    assert payload["parent_model_operation_id"] == context.run_operation_id


@pytest.mark.asyncio
async def test_tool_operation_started_fingerprints_raw_text_arguments() -> None:
    sink = FakeSink()
    context = make_context(sink)
    trace = ToolOperationTrace(
        context, operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    await trace.started(
        tool_name="shell",
        tool_kind="builtin",
        batch_index=None,
        invocation_order=None,
        arguments="echo héllo",
        invocation_metadata={},
    )
    payload = sink.only(EventType.TOOL_OPERATION_STARTED).payload
    assert payload["argument_fingerprint"] == fingerprint_text(FINGERPRINT_KEY, DOMAIN_TOOL_ARGUMENTS, "echo héllo")


@pytest.mark.asyncio
async def test_tool_payload_observed_records_shape_and_keyed_content_fingerprint() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id())
    result_item_id = new_analytics_id()
    trace = ToolOperationTrace(
        context,
        operation_id=new_analytics_id(),
        result_item_id=result_item_id,
        result_carrier_item_id=None,
    )
    text = "line one\nline two 中文"
    await trace.payload_observed(
        result_text=text,
        image_count=2,
        observation={"original_bytes": 5000, "truncated": True, "artifact_id": "shell_1a2b3c4d.txt", "ignored": 1},
    )
    event = sink.only(EventType.TOOL_PAYLOAD_OBSERVED)
    assert event.operation_id == trace.operation_id
    assert event.parent_operation_id == context.cycle_operation_id
    payload = event.payload
    assert payload["model_visible_bytes"] == len(text.encode("utf-8"))
    assert isinstance(payload["local_token_estimate"], int) and payload["local_token_estimate"] > 0
    assert payload["tokenizer_fingerprint"] == TOKENIZER_FINGERPRINT
    assert payload["truncated"] is True
    assert payload["original_bytes"] == 5000
    # A spill artifact is named by its file name, not by an analytics id, and
    # the envelope must accept that shape rather than gap the whole event.
    assert payload["artifact_id"] == "shell_1a2b3c4d.txt"
    assert malformed_id_pointers({"payload": payload}) == []
    assert "ignored" not in payload
    assert payload["content_type"] == "text+image"
    assert payload["image_count"] == 2
    assert payload["result_item_id"] == result_item_id
    assert payload["content_fingerprint"] == fingerprint_text(FINGERPRINT_KEY, DOMAIN_TOOL_CONTENT, text)
    assert text not in repr(payload)
    assert event.measurements == {
        "/payload/local_token_estimate": {"source": MeasurementSource.LOCAL_TOKENIZER, "method_version": 1}
    }


@pytest.mark.asyncio
async def test_tool_payload_observed_text_only_without_fingerprint_key() -> None:
    sink = FakeSink(fingerprint_key=None)
    trace = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    await trace.payload_observed(result_text="ok", image_count=0, observation=None)
    payload = sink.only(EventType.TOOL_PAYLOAD_OBSERVED).payload
    assert payload["content_type"] == "text"
    assert payload["truncated"] is False
    assert "image_count" not in payload
    assert "result_item_id" not in payload
    assert "content_fingerprint" not in payload


@pytest.mark.asyncio
async def test_tool_operation_finished_payload_carries_exit_code_timeout_and_error_kind() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange(new_analytics_id())
    result_item_id = new_analytics_id()
    result_carrier_item_id = new_analytics_id()
    trace = ToolOperationTrace(
        context,
        operation_id=new_analytics_id(),
        result_item_id=result_item_id,
        result_carrier_item_id=result_carrier_item_id,
    )
    await trace.finished(
        outcome=ToolOutcome.TIMED_OUT,
        duration_ms=-5,
        result_metadata={
            SHELL_EXIT_CODE_METADATA_KEY: 124,
            SHELL_TIMED_OUT_METADATA_KEY: True,
            TOOL_ERROR_KIND_METADATA_KEY: "timeout",
        },
    )
    event = sink.only(EventType.TOOL_OPERATION_FINISHED)
    assert event.operation_id == trace.operation_id
    assert event.parent_operation_id == context.exchange_operation_id
    assert event.payload == {
        "outcome": ToolOutcome.TIMED_OUT,
        "duration_ms": 0,
        "result_item_id": result_item_id,
        "result_carrier_item_id": result_carrier_item_id,
        "exit_code": 124,
        "timed_out": True,
        "error_kind": "timeout",
    }
    assert event.measurements == {"/payload/duration_ms": MONOTONIC}


@pytest.mark.asyncio
async def test_tool_operation_finished_explicit_error_kind_wins_and_optionals_are_omitted() -> None:
    sink = FakeSink()
    trace = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    await trace.finished(
        outcome=ToolOutcome.ERRORED,
        duration_ms=12,
        result_metadata={TOOL_ERROR_KIND_METADATA_KEY: "structured"},
        error_kind="raised",
    )
    payload = sink.only(EventType.TOOL_OPERATION_FINISHED).payload
    assert payload == {"outcome": ToolOutcome.ERRORED, "duration_ms": 12, "error_kind": "raised"}


@pytest.mark.asyncio
async def test_tool_operation_finished_is_idempotent_across_both_closers() -> None:
    sink = FakeSink()
    trace = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    await trace.finished(outcome=ToolOutcome.SUCCESS, duration_ms=1, result_metadata={})
    await trace.finished(outcome=ToolOutcome.FAILED, duration_ms=2, result_metadata={})
    trace.finished_soon(outcome=ToolOutcome.INTERRUPTED, duration_ms=3)
    finished = sink.of_type(EventType.TOOL_OPERATION_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["outcome"] == ToolOutcome.SUCCESS


@pytest.mark.asyncio
async def test_tool_operation_finished_soon_closes_once_and_blocks_later_finished() -> None:
    sink = FakeSink()
    trace = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    trace.finished_soon(outcome=ToolOutcome.INTERRUPTED, duration_ms=7)
    trace.finished_soon(outcome=ToolOutcome.SUCCESS, duration_ms=8)
    await trace.finished(outcome=ToolOutcome.SUCCESS, duration_ms=9, result_metadata={})
    finished = sink.of_type(EventType.TOOL_OPERATION_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload == {"outcome": ToolOutcome.INTERRUPTED, "duration_ms": 7}


@pytest.mark.asyncio
async def test_tool_operation_emit_failures_are_swallowed() -> None:
    sink = FakeSink()
    trace = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    sink.fail_next = True
    await trace.started(
        tool_name="x",
        tool_kind="builtin",
        batch_index=None,
        invocation_order=None,
        arguments=None,
        invocation_metadata={},
    )
    sink.fail_next = True
    await trace.payload_observed(result_text="", image_count=0, observation=None)
    sink.fail_next = True
    await trace.finished(outcome=ToolOutcome.SUCCESS, duration_ms=0, result_metadata={})
    assert sink.drafts == []
    other = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    sink.fail_next = True
    other.finished_soon(outcome=ToolOutcome.SUCCESS, duration_ms=0)
    assert sink.drafts == []


@pytest.mark.parametrize(
    ("cancelled", "rejected", "errored", "metadata", "expected"),
    [
        (True, True, True, {}, ToolOutcome.INTERRUPTED),
        (False, True, True, {}, ToolOutcome.REJECTED),
        (False, False, True, {}, ToolOutcome.ERRORED),
        (False, False, False, {PROCESS_TIMED_OUT_METADATA_KEY: True}, ToolOutcome.TIMED_OUT),
        (False, False, False, {SHELL_TIMED_OUT_METADATA_KEY: True}, ToolOutcome.TIMED_OUT),
        (False, False, False, {TOOL_FAILED_METADATA_KEY: True}, ToolOutcome.FAILED),
        (False, False, False, {SHELL_EXIT_CODE_METADATA_KEY: 2}, ToolOutcome.FAILED),
        (False, False, False, {PROCESS_EXIT_CODE_METADATA_KEY: 0}, ToolOutcome.SUCCESS),
        (False, False, False, {TOOL_FAILED_METADATA_KEY: False}, ToolOutcome.SUCCESS),
        (False, False, False, {}, ToolOutcome.SUCCESS),
    ],
)
def test_tool_outcome_mapping(
    cancelled: bool, rejected: bool, errored: bool, metadata: dict[str, object], expected: str
) -> None:
    assert tool_outcome(cancelled=cancelled, rejected=rejected, errored=errored, result_metadata=metadata) == expected


# --------------------------------------------------------------- side calls


def test_a_side_call_records_under_its_own_actor_and_drops_the_callers_facts() -> None:
    context = make_context().with_exchange_facts({"agent_profile_id": "main", "request_model": "big-model"})
    with trajectory_scope(context), side_call_scope(ActorRole.APPROVAL_JUDGE):
        side = current_trajectory()

    assert side is not None
    assert side.actor.actor_id != context.actor.actor_id
    assert side.actor.role == ActorRole.APPROVAL_JUDGE
    # The judge answers on its own profile and model; inheriting the turn's
    # facts would record the main agent's as this exchange's own.
    assert side.exchange_facts == {}
    # Leaving the scope restores the caller untouched.
    assert current_trajectory() is None


def test_a_side_call_without_an_ambient_context_is_inert() -> None:
    with side_call_scope(ActorRole.COMPACTION):
        assert current_trajectory() is None


# ---------------------------------------------------------------------- ApprovalTrace


def test_approval_open_returns_none_without_context() -> None:
    assert ApprovalTrace.open({OPERATION_ID_KEY: new_analytics_id()}) is None


@pytest.mark.asyncio
async def test_approval_requested_and_resolved_hang_under_target_tool_operation() -> None:
    sink = FakeSink()
    exchange_id = new_analytics_id()
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange(exchange_id)
    target = new_analytics_id()
    with trajectory_scope(context):
        trace = ApprovalTrace.open({OPERATION_ID_KEY: target})
    assert trace is not None
    await trace.requested(tool_name="shell", approval_mode="always_ask", approval_level="write")
    await trace.resolved(
        approved=False, decider=ApprovalDecider.JUDGE, reason_code="policy_denied", arguments_modified=True
    )
    assert sink.event_types == [EventType.APPROVAL_REQUESTED, EventType.APPROVAL_RESOLVED]
    requested, resolved = sink.drafts
    for event in (requested, resolved):
        assert event.operation_id == target
        assert event.parent_operation_id == exchange_id
        assert event.links == ()
        # Minted by the log itself, and the same on both events: the pair is
        # only joinable through it when the request has no target operation.
        assert is_valid_analytics_id(event.payload["approval_request_id"])
        assert event.payload["target_tool_operation_id"] == target
    assert requested.payload["approval_request_id"] == resolved.payload["approval_request_id"]
    assert requested.payload["tool_name"] == "shell"
    assert requested.payload["approval_mode"] == "always_ask"
    assert requested.payload["approval_level"] == "write"
    assert requested.payload["requested_at"].endswith("Z")
    assert resolved.payload["decision"] == ApprovalDecision.REJECTED
    assert resolved.payload["decider"] == ApprovalDecider.JUDGE
    assert resolved.payload["reason_code"] == "policy_denied"
    assert resolved.payload["arguments_modified"] is True
    assert resolved.payload["resolved_at"].endswith("Z")
    assert resolved.payload["wait_ms"] >= 0
    assert resolved.measurements == {"/payload/wait_ms": MONOTONIC}


@pytest.mark.asyncio
async def test_generic_approval_targets_a_sub_agent_boundary() -> None:
    sink = FakeSink()
    context = make_context(sink)
    boundary = new_analytics_id()
    trace = ApprovalTrace.open_for_operation(context=context, target_operation_id=boundary)
    assert trace is not None

    await trace.requested(tool_name="acp:shell", approval_mode="manual", approval_level="require")
    await trace.resolved(
        approved=True,
        decider=ApprovalDecider.USER,
        reason_code="approved",
        arguments_modified=False,
    )

    for event in sink.drafts:
        assert event.operation_id == event.payload["approval_request_id"]
        assert event.operation_id != event.parent_operation_id
        assert event.parent_operation_id == boundary
        assert event.payload["target_operation_id"] == boundary
        assert "target_tool_operation_id" not in event.payload


@pytest.mark.asyncio
async def test_approval_without_target_operation_omits_the_target_and_records_approval() -> None:
    sink = FakeSink()
    context = make_context(sink)
    with trajectory_scope(context):
        trace = ApprovalTrace.open({OPERATION_ID_KEY: 42})
    assert trace is not None
    await trace.requested(tool_name="shell", approval_mode="manual", approval_level="required")
    await trace.resolved(
        approved=True, decider=ApprovalDecider.USER, reason_code="user_approved", arguments_modified=False
    )
    event = sink.only(EventType.APPROVAL_RESOLVED)
    assert event.operation_id is None
    assert event.parent_operation_id == context.run_operation_id
    assert "target_tool_operation_id" not in event.payload
    assert event.payload["decision"] == ApprovalDecision.APPROVED


@pytest.mark.asyncio
async def test_approval_emit_failures_are_swallowed() -> None:
    sink = FakeSink()
    trace = ApprovalTrace(make_context(sink), target_operation_id=None)
    sink.fail_next = True
    await trace.requested(tool_name="x", approval_mode="m", approval_level="l")
    sink.fail_next = True
    await trace.resolved(approved=True, decider=ApprovalDecider.USER, reason_code="r", arguments_modified=False)
    assert sink.drafts == []


@pytest.mark.asyncio
async def test_an_abandoned_approval_resolves_as_interrupted_with_nobody_deciding() -> None:
    sink = FakeSink()
    trace = ApprovalTrace(make_context(sink), target_operation_id=None)
    await trace.requested(tool_name="write_file", approval_mode="manual", approval_level="required")

    trace.interrupted_soon()

    event = sink.only(EventType.APPROVAL_RESOLVED)
    assert event.payload["decision"] == ApprovalDecision.INTERRUPTED
    assert event.payload["decider"] == ApprovalDecider.NONE
    assert event.payload["arguments_modified"] is False
    assert event.payload["wait_ms"] >= 0
    assert event.measurements == {"/payload/wait_ms": MONOTONIC}


@pytest.mark.asyncio
async def test_an_approval_resolves_exactly_once_whichever_close_runs_first() -> None:
    sink = FakeSink()
    trace = ApprovalTrace(make_context(sink), target_operation_id=None)
    await trace.requested(tool_name="shell", approval_mode="manual", approval_level="required")
    await trace.resolved(approved=True, decider=ApprovalDecider.USER, reason_code="", arguments_modified=False)
    # The interrupt that unwinds the turn arrives after the decision landed.
    trace.interrupted_soon()
    assert sink.only(EventType.APPROVAL_RESOLVED).payload["decision"] == ApprovalDecision.APPROVED

    other = ApprovalTrace(make_context(sink), target_operation_id=None)
    await other.requested(tool_name="shell", approval_mode="manual", approval_level="required")
    other.interrupted_soon()
    await other.resolved(approved=False, decider=ApprovalDecider.JUDGE, reason_code="", arguments_modified=False)
    decisions = [event.payload["decision"] for event in sink.of_type(EventType.APPROVAL_RESOLVED)]
    assert decisions == [ApprovalDecision.APPROVED, ApprovalDecision.INTERRUPTED]


# ------------------------------------------------------------------ HookOperationTrace


def test_hook_open_falls_back_to_provider_only_without_ambient_context() -> None:
    explicit = make_context()
    ambient = make_context()
    provided = make_context()
    assert HookOperationTrace.open() is None
    assert HookOperationTrace.open(provider=lambda: None) is None
    via_provider = HookOperationTrace.open(provider=lambda: provided)
    assert via_provider is not None and via_provider._context is provided
    with trajectory_scope(ambient):
        via_ambient = HookOperationTrace.open(provider=lambda: provided)
        via_explicit = HookOperationTrace.open(context=explicit, provider=lambda: provided)
    assert via_ambient is not None and via_ambient._context is ambient
    assert via_explicit is not None and via_explicit._context is explicit
    assert is_valid_analytics_id(via_ambient.operation_id)
    assert via_ambient.operation_id != via_provider.operation_id


def test_hook_outcome_values_are_strings() -> None:
    values = [value for name, value in vars(HookOutcome).items() if not name.startswith("_")]
    assert values
    assert all(isinstance(value, str) and value for value in values)
    assert len(set(values)) == len(values)


@pytest.mark.asyncio
async def test_hook_started_and_finished_under_target_operation() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id())
    target = new_analytics_id()
    trace = HookOperationTrace(context, target_operation_id=target)
    await trace.started(
        hook_id="guard",
        hook_event="pre_tool_use",
        execution_mode="blocking",
        detach=False,
        delivery="best_effort",
    )
    await trace.finished(outcome=HookOutcome.TIMED_OUT, arguments_modified=True, exit_code=-9, timed_out=True)
    started = sink.only(EventType.HOOK_OPERATION_STARTED)
    finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
    for event in (started, finished):
        assert event.operation_id == trace.operation_id
        assert event.parent_operation_id == target
    assert started.payload == {
        "hook_key": "guard",
        "hook_event": "pre_tool_use",
        "execution_mode": "blocking",
        "detach": False,
        "delivery": "best_effort",
        "scope": "turn",
        "drain_scope": "turn",
        "target_operation_id": target,
    }
    assert finished.payload["outcome"] == HookOutcome.TIMED_OUT
    assert finished.payload["arguments_modified"] is True
    assert finished.payload["exit_code"] == -9
    assert finished.payload["timed_out"] is True
    assert finished.payload["duration_ms"] >= 0
    assert finished.measurements == {"/payload/duration_ms": MONOTONIC}


@pytest.mark.asyncio
async def test_hook_without_target_hangs_under_innermost_model_operation_and_omits_optionals() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id())
    trace = HookOperationTrace(context, target_operation_id=None)
    await trace.started(
        hook_id="session-bootstrap",
        hook_event="session_start",
        execution_mode="async",
        detach=False,
        delivery="durable",
    )
    await trace.finished(outcome=HookOutcome.SUCCESS)
    started = sink.only(EventType.HOOK_OPERATION_STARTED)
    finished = sink.only(EventType.HOOK_OPERATION_FINISHED)
    assert started.parent_operation_id == context.cycle_operation_id
    assert finished.parent_operation_id == context.cycle_operation_id
    assert started.payload == {
        "hook_key": "session-bootstrap",
        "hook_event": "session_start",
        "execution_mode": "async",
        "detach": False,
        "delivery": "durable",
        "scope": "turn",
        "drain_scope": "turn",
    }
    assert set(finished.payload) == {"outcome", "arguments_modified", "duration_ms"}
    assert finished.payload["arguments_modified"] is False


@pytest.mark.asyncio
async def test_hook_finished_soon_is_idempotent() -> None:
    sink = FakeSink()
    trace = HookOperationTrace(make_context(sink), target_operation_id=None)
    await trace.started(
        hook_id="test",
        hook_event="e",
        execution_mode="blocking",
        detach=False,
        delivery="best_effort",
    )
    trace.finished_soon(outcome=HookOutcome.CANCELLED)
    trace.finished_soon(outcome=HookOutcome.SUCCESS)
    await trace.finished(outcome=HookOutcome.FAILED, exit_code=1)
    finished = sink.of_type(EventType.HOOK_OPERATION_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["outcome"] == HookOutcome.CANCELLED
    assert "exit_code" not in finished[0].payload


@pytest.mark.asyncio
async def test_a_hook_terminal_is_dropped_when_its_start_never_took_a_sequence() -> None:
    """An interrupt in the start marker's ack wait must not close what was never opened."""

    class _CancelsBeforeTheSequence(FakeSink):
        async def emit(
            self,
            draft: EventDraft,
            *,
            payload_factory: Callable[[int], Mapping[str, Any]] | None = None,
        ) -> EmitResult:
            # Where the interrupt lands: the writer thread has the draft and
            # has not reached the locked step that hands out a sequence.
            raise asyncio.CancelledError

    sink = _CancelsBeforeTheSequence()
    trace = HookOperationTrace(make_context(sink), target_operation_id=None)
    with pytest.raises(asyncio.CancelledError):
        await trace.started(
            hook_id="test",
            hook_event="session_start",
            execution_mode="blocking",
            detach=False,
            delivery="best_effort",
        )

    trace.finished_soon(outcome=HookOutcome.CANCELLED)
    await trace.finished(outcome=HookOutcome.CANCELLED)

    assert sink.drafts == []


@pytest.mark.asyncio
async def test_hook_emit_failures_are_swallowed() -> None:
    sink = FakeSink()
    trace = HookOperationTrace(make_context(sink), target_operation_id=None)
    sink.fail_next = True
    await trace.started(
        hook_id="test",
        hook_event="e",
        execution_mode="blocking",
        detach=False,
        delivery="best_effort",
    )
    await trace.finished(outcome=HookOutcome.SUCCESS)
    assert sink.drafts == []

    awaited = HookOperationTrace(make_context(sink), target_operation_id=None)
    await awaited.started(
        hook_id="test",
        hook_event="e",
        execution_mode="blocking",
        detach=False,
        delivery="best_effort",
    )
    sink.fail_next = True
    await awaited.finished(outcome=HookOutcome.SUCCESS)

    queued = HookOperationTrace(make_context(sink), target_operation_id=None)
    await queued.started(
        hook_id="test",
        hook_event="e",
        execution_mode="blocking",
        detach=False,
        delivery="best_effort",
    )
    sink.fail_next = True
    queued.finished_soon(outcome=HookOutcome.SUCCESS)

    assert sink.event_types == [EventType.HOOK_OPERATION_STARTED] * 2


# -------------------------------------------------------------------------- WaitTrace


def test_pre_turn_outcomes_are_the_complete_contract() -> None:
    assert {
        PreparationOutcome.FRESH_TURN,
        PreparationOutcome.RETRY_TURN,
        PreparationOutcome.INJECTED,
        PreparationOutcome.ABANDONED_NO_TARGET,
        PreparationOutcome.CANCELLED,
        PreparationOutcome.TARGET_STALE,
        PreparationOutcome.REJECTED,
        PreparationOutcome.IMAGE_REJECTED,
        PreparationOutcome.COMMAND_FAILED,
        PreparationOutcome.NOT_READY,
        PreparationOutcome.PREPARATION_FAILED,
        PreparationOutcome.CONFLICT,
        PreparationOutcome.OWNER_CHANGED,
        PreparationOutcome.SUPERSEDED,
        PreparationOutcome.DROPPED,
    } == {
        "fresh_turn",
        "retry_turn",
        "injected",
        "abandoned_no_target",
        "cancelled",
        "target_stale",
        "rejected",
        "image_rejected",
        "command_failed",
        "not_ready",
        "preparation_failed",
        "conflict",
        "owner_changed",
        "superseded",
        "dropped",
    }


@pytest.mark.asyncio
async def test_preparation_scope_records_state_and_one_terminal() -> None:
    sink = FakeSink()
    context = make_context(sink).with_turn(None).with_run(None)
    target = new_analytics_id()
    trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="input_admission",
        target_operation_id=target,
        context=context,
    )
    assert trace is not None

    await trace.started()
    await trace.state("queued")
    await trace.finished(outcome=PreparationOutcome.INJECTED, target_turn_id=new_analytics_id())
    await trace.finished(outcome=PreparationOutcome.DROPPED)

    assert sink.event_types == [
        EventType.PREPARATION_STARTED,
        EventType.PREPARATION_STATE,
        EventType.PREPARATION_FINISHED,
    ]
    started, state, finished = sink.drafts
    assert started.turn_id is None
    assert started.parent_operation_id is None
    assert started.payload == {
        "scope": PreparationScope.PRE_TURN,
        "phase": "input_admission",
        "target_operation_id": target,
    }
    assert state.payload["state"] == "queued"
    assert state.payload["scope_operation_id"] == trace.operation_id
    assert finished.payload["outcome"] == PreparationOutcome.INJECTED
    assert finished.payload["duration_ms"] >= 0
    assert finished.measurements == {"/payload/duration_ms": MONOTONIC}
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_preparation_cancel_before_late_start_commit_still_records_terminal() -> None:
    class _LateCommitSink(FakeSink):
        def __init__(self) -> None:
            super().__init__()
            self.start_received = asyncio.Event()
            self.allow_commit = asyncio.Event()

        async def emit(
            self,
            draft: EventDraft,
            *,
            payload_factory: Callable[[int], Mapping[str, Any]] | None = None,
        ) -> EmitResult:
            if draft.event_type == EventType.PREPARATION_STARTED:
                self.start_received.set()
                await self.allow_commit.wait()
            return self._record(draft, payload_factory)

    sink = _LateCommitSink()
    trace = PreparationTrace.open(
        scope=PreparationScope.PRE_TURN,
        phase="input_admission",
        context=make_context(sink).with_turn(None).with_run(None),
    )
    assert trace is not None

    caller = asyncio.create_task(trace.started())
    await sink.start_received.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    trace.finished_soon(outcome=PreparationOutcome.CANCELLED)
    terminal_settlement = trace._terminal_settlement
    assert terminal_settlement is not None
    assert sink.drafts == []

    sink.allow_commit.set()
    await terminal_settlement

    assert sink.event_types == [EventType.PREPARATION_STARTED, EventType.PREPARATION_FINISHED]
    assert sink.only(EventType.PREPARATION_FINISHED).payload["outcome"] == PreparationOutcome.CANCELLED
    sink.assert_operations_settled()


@pytest.mark.asyncio
async def test_retry_backoff_records_a_closed_pair_under_its_container() -> None:
    sink = FakeSink()
    parent = new_analytics_id()
    trace = RetryBackoffTrace.open(
        context=make_context(sink),
        parent_operation_id=parent,
        retry_mode=RetryMode.COMPACTION,
    )
    assert trace is not None

    await trace.scheduled(reason_code="transient_error", delay_seconds=1.25)
    await trace.started()

    assert sink.event_types == [EventType.RETRY_SCHEDULED, EventType.RETRY_STARTED]
    scheduled, started = sink.drafts
    assert scheduled.operation_id == started.operation_id
    assert scheduled.parent_operation_id == started.parent_operation_id == parent
    assert scheduled.payload["delay_ms"] == 1250
    assert scheduled.payload["reason_code"] == "transient_error"
    assert scheduled.payload["retry_mode"] == started.payload["retry_mode"] == RetryMode.COMPACTION


@pytest.mark.asyncio
async def test_retry_schedule_ack_cancellation_does_not_claim_retry_started() -> None:
    sink = CancelAckSink(at=1)
    trace = RetryBackoffTrace.open(
        context=make_context(sink),
        parent_operation_id=new_analytics_id(),
        retry_mode=RetryMode.RUN,
    )
    assert trace is not None

    with pytest.raises(asyncio.CancelledError):
        await trace.scheduled(reason_code="transient_error", delay_seconds=3)

    assert sink.event_types == [EventType.RETRY_SCHEDULED]


def test_wait_open_resolves_ambient_then_provider() -> None:
    ambient = make_context()
    provided = make_context()
    assert WaitTrace.open(WaitCategory.USER_INPUT) is None
    assert WaitTrace.open(WaitCategory.USER_INPUT, provider=lambda: None) is None
    via_provider = WaitTrace.open(WaitCategory.USER_INPUT, provider=lambda: provided)
    assert via_provider is not None and via_provider._context is provided
    with trajectory_scope(ambient):
        via_ambient = WaitTrace.open(WaitCategory.MCP_CONNECT, provider=lambda: provided)
    assert via_ambient is not None and via_ambient._context is ambient
    assert is_valid_analytics_id(via_ambient.operation_id)


@pytest.mark.asyncio
async def test_wait_started_and_finished_payloads() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id())
    target = new_analytics_id()
    trace = WaitTrace(context, category=WaitCategory.SUB_AGENT_CONCURRENCY, target_operation_id=target)
    await trace.started()
    await trace.finished(outcome=WaitOutcome.TIMED_OUT)
    started = sink.only(EventType.WAIT_STARTED)
    finished = sink.only(EventType.WAIT_FINISHED)
    for event in (started, finished):
        assert event.operation_id == trace.operation_id
        assert event.parent_operation_id == target
        assert event.payload["category"] == WaitCategory.SUB_AGENT_CONCURRENCY
        assert event.payload["target_operation_id"] == target
    assert set(started.payload) == {"category", "target_operation_id"}
    assert finished.payload["outcome"] == WaitOutcome.TIMED_OUT
    assert finished.payload["duration_ms"] >= 0
    assert finished.measurements == {"/payload/duration_ms": MONOTONIC}


@pytest.mark.asyncio
async def test_mcp_connect_wait_carries_server_and_tool_target() -> None:
    sink = FakeSink()
    target = new_analytics_id()
    trace = WaitTrace(
        make_context(sink),
        category=WaitCategory.MCP_CONNECT,
        target_operation_id=target,
        server_name="filesystem",
    )
    await trace.started()
    await trace.finished()

    for event in sink.drafts:
        assert event.parent_operation_id == target
        assert event.payload["target_operation_id"] == target
        assert event.payload["server_name"] == "filesystem"


@pytest.mark.asyncio
async def test_wait_without_target_defaults_to_completed_under_model_operation() -> None:
    sink = FakeSink()
    context = make_context(sink)
    trace = WaitTrace(context, category=WaitCategory.RATE_LIMIT, target_operation_id=None)
    await trace.started()
    await trace.finished()
    started = sink.only(EventType.WAIT_STARTED)
    finished = sink.only(EventType.WAIT_FINISHED)
    assert started.parent_operation_id == context.run_operation_id
    assert started.payload == {"category": WaitCategory.RATE_LIMIT}
    assert finished.payload == {
        "category": WaitCategory.RATE_LIMIT,
        "outcome": WaitOutcome.COMPLETED,
        "duration_ms": finished.payload["duration_ms"],
    }


@pytest.mark.asyncio
async def test_wait_finished_soon_is_idempotent_and_failures_are_swallowed() -> None:
    sink = FakeSink()
    trace = WaitTrace(make_context(sink), category=WaitCategory.USER_INPUT, target_operation_id=None)
    await trace.started()
    trace.finished_soon(outcome=WaitOutcome.CANCELLED)
    trace.finished_soon(outcome=WaitOutcome.FAILED)
    await trace.finished(outcome=WaitOutcome.COMPLETED)
    finished = sink.of_type(EventType.WAIT_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["outcome"] == WaitOutcome.CANCELLED

    failing = WaitTrace(make_context(sink), category=WaitCategory.USER_INPUT, target_operation_id=None)
    sink.fail_next = True
    await failing.started()
    sink.fail_next = True
    await failing.finished()
    assert len(sink.drafts) == 2


# ---------------------------------------------------------------------- SubAgentTrace

INVOCATION_ID = "abcdef012345"


def test_sub_agent_open_returns_none_without_context() -> None:
    assert SubAgentTrace.open(invocation_id=INVOCATION_ID, trace_coverage=TraceCoverage.FULL) is None


def test_sub_agent_open_binds_the_executing_tool_operation_as_parent() -> None:
    context = make_context().with_cycle(new_analytics_id())
    tool_operation_id = new_analytics_id()
    token = bind_tool_operation(tool_operation_id)
    try:
        with trajectory_scope(context):
            trace = SubAgentTrace.open(invocation_id=INVOCATION_ID, trace_coverage=TraceCoverage.FULL)
    finally:
        reset_tool_operation(token)
    assert trace is not None
    assert trace._parent_tool_operation_id == tool_operation_id
    assert is_valid_analytics_id(trace.operation_id)
    child = trace.child_context
    assert child.actor == sub_agent_actor(SESSION_ID, INVOCATION_ID)
    assert child.actor.kind == ActorKind.AGENT
    assert child.actor.role == ActorRole.SUB_AGENT
    assert child.actor.invocation_id == INVOCATION_ID
    assert child.run_operation_id == trace.operation_id
    assert child.cycle_operation_id is None
    assert child.exchange_operation_id is None
    assert child.exchange_facts == {}
    assert child.turn_id == context.turn_id
    assert child.sink is context.sink
    assert child.revisions is context.revisions


@pytest.mark.asyncio
async def test_sub_agent_started_and_finished_link_the_boundary_to_the_tool_operation() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id()).with_exchange_facts({"model_profile_id": "m"})
    tool_operation_id = new_analytics_id()
    trace = SubAgentTrace(
        context,
        invocation_id=INVOCATION_ID,
        trace_coverage=TraceCoverage.BOUNDARY_ONLY,
        parent_tool_operation_id=tool_operation_id,
    )
    assert trace.child_context.exchange_facts == {}
    await trace.started(tool_name="delegate", agent_profile="researcher")
    assert trace.child_context.exchange_facts == {"agent_profile_id": "researcher"}
    await trace.finished(status=SubAgentStatus.FAILED, failure_reason_code="tool_error")
    started = sink.only(EventType.SUB_AGENT_STARTED)
    finished = sink.only(EventType.SUB_AGENT_FINISHED)
    for event in (started, finished):
        assert event.operation_id == trace.operation_id
        assert event.parent_operation_id == tool_operation_id
        assert event.actor == context.actor
        assert len(event.links) == 1
        assert event.links[0].relation == LinkRelation.BOUNDARY_OF
        assert event.links[0].target_operation_id == tool_operation_id
        assert event.payload["invocation_id"] == INVOCATION_ID
    assert started.payload == {
        "invocation_id": INVOCATION_ID,
        "actor_id": trace.child_context.actor.actor_id,
        "tool_name": "delegate",
        "agent_profile": "researcher",
        "trace_coverage": TraceCoverage.BOUNDARY_ONLY,
        "parent_tool_operation_id": tool_operation_id,
    }
    assert is_valid_analytics_id(started.payload["actor_id"])
    assert finished.payload["status"] == SubAgentStatus.FAILED
    assert finished.payload["failure_reason_code"] == "tool_error"
    assert finished.payload["duration_ms"] >= 0
    assert finished.measurements == {"/payload/duration_ms": MONOTONIC}


@pytest.mark.asyncio
async def test_sub_agent_without_tool_operation_hangs_under_model_operation_without_links() -> None:
    sink = FakeSink()
    context = make_context(sink).with_cycle(new_analytics_id())
    trace = SubAgentTrace(
        context, invocation_id=INVOCATION_ID, trace_coverage=TraceCoverage.FULL, parent_tool_operation_id=None
    )
    await trace.started(tool_name="delegate", agent_profile="p")
    trace.finished_soon(status=SubAgentStatus.OK)
    started = sink.only(EventType.SUB_AGENT_STARTED)
    finished = sink.only(EventType.SUB_AGENT_FINISHED)
    for event in (started, finished):
        assert event.parent_operation_id == context.cycle_operation_id
        assert event.links == ()
    assert "parent_tool_operation_id" not in started.payload
    assert "failure_reason_code" not in finished.payload


@pytest.mark.asyncio
async def test_sub_agent_finished_is_idempotent_and_failures_are_swallowed() -> None:
    sink = FakeSink()
    trace = SubAgentTrace(
        make_context(sink),
        invocation_id=INVOCATION_ID,
        trace_coverage=TraceCoverage.FULL,
        parent_tool_operation_id=None,
    )
    await trace.finished(status=SubAgentStatus.CANCELLED)
    await trace.finished(status=SubAgentStatus.OK)
    trace.finished_soon(status=SubAgentStatus.OK)
    finished = sink.of_type(EventType.SUB_AGENT_FINISHED)
    assert len(finished) == 1
    assert finished[0].payload["status"] == SubAgentStatus.CANCELLED

    failing = SubAgentTrace(
        make_context(sink),
        invocation_id=INVOCATION_ID,
        trace_coverage=TraceCoverage.FULL,
        parent_tool_operation_id=None,
    )
    sink.fail_next = True
    await failing.started(tool_name="t", agent_profile="p")
    sink.fail_next = True
    failing.finished_soon(status=SubAgentStatus.SETUP_FAILED)
    assert len(sink.drafts) == 1


@pytest.mark.asyncio
async def test_tool_payload_observed_survives_a_lone_surrogate_in_the_result() -> None:
    # An MCP server can return "\ud800" in JSON, and json.loads hands it back
    # as a lone high surrogate. Sizing it must not raise: the tool already ran,
    # and an exception here would report the completed call as failed.
    sink = FakeSink(fingerprint_key=None)
    trace = ToolOperationTrace(
        make_context(sink), operation_id=new_analytics_id(), result_item_id=None, result_carrier_item_id=None
    )
    text = "ok \ud800 done"
    await trace.payload_observed(result_text=text, image_count=0, observation=None)
    payload = sink.only(EventType.TOOL_PAYLOAD_OBSERVED).payload
    assert payload["model_visible_bytes"] == len(text.encode("utf-8", errors="backslashreplace"))
