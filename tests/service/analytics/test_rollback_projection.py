# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback ranges reverse superseded usage and cut approval and MCP-connect waits by owner activity."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.foundation.trajectory.envelope import measurement
from chrys.service.analytics import (
    Metric,
    Precision,
    TrajectoryAnalyzer,
    analyze_trajectory,
)
from chrys.service.analytics import _facts as facts_module
from chrys.service.analytics import _insights as insights_module
from tests.service.analytics._events import BRANCH_ID, NS, EventLog


@pytest.mark.parametrize("reason_code", ["user_rollback", "fresh_turn_retired"])
def test_rollback_reverses_superseded_usage_contributions_before_overview(tmp_path, reason_code: str) -> None:
    """A later rollback removes earlier contributions instead of requiring inverse events."""
    old_turn_id = "4" * 32
    live_turn_id = "5" * 32
    old_branch_id = "3" * 32
    new_branch_id = "6" * 32
    log = EventLog()
    log.add("turn.started", 0, turn_id=old_turn_id, payload={"turn_number": 1})
    log.add("model.exchange.started", 0, turn_id=old_turn_id, operation_id="a" * 32)
    log.add(
        "model.exchange.finished",
        NS,
        turn_id=old_turn_id,
        operation_id="a" * 32,
        payload={
            "outcome": "success",
            "duration_ms": 1000,
            "usage": {"normalized": {"input_total": 100, "output_total": 20}},
        },
        measurements={
            "/payload/duration_ms": {"source": "monotonic_clock", "method_version": 1},
            "/payload/usage/normalized/input_total": {"source": "provider", "adapter_version": 1},
            "/payload/usage/normalized/output_total": {"source": "provider", "adapter_version": 1},
        },
    )
    log.add("turn.finished", NS, turn_id=old_turn_id, payload={"end_reason": "cancelled", "duration_ms": 0})
    # A cold recorder resumes while rollback still owns the recovered branch.
    # These physical infrastructure lines fall inside the logical range that
    # rollback supersedes, but they remain the coverage/runtime of the live
    # turn opened on the successor branch.
    log.add(
        "trajectory.coverage.started",
        2 * NS,
        turn_id=None,
        payload={"coverage_reason": "runtime_resumed"},
    )
    log.add("trajectory.runtime.started", 2 * NS, turn_id=None)
    log.add(
        "trajectory.runtime.recovered",
        2 * NS,
        turn_id=None,
        payload={"truncated_bytes": 0, "resumed_from_sequence": 4},
    )
    log.add(
        "session.rollback",
        2 * NS,
        turn_id=None,
        branch_id=new_branch_id,
        payload={
            "old_branch_id": old_branch_id,
            "new_branch_id": new_branch_id,
            "superseded_from_sequence": 1,
            "superseded_to_sequence": 7,
            "reason_code": reason_code,
        },
    )
    log.add(
        "branch.superseded",
        2 * NS,
        turn_id=None,
        branch_id=new_branch_id,
        payload={"branch_id": old_branch_id, "superseded_by": new_branch_id},
    )
    log.add(
        "turn.started",
        3 * NS,
        turn_id=live_turn_id,
        branch_id=new_branch_id,
        payload={"turn_number": 1},
    )
    log.add(
        "model.exchange.started",
        3 * NS,
        turn_id=live_turn_id,
        operation_id="b" * 32,
        branch_id=new_branch_id,
    )
    log.add(
        "model.exchange.finished",
        4 * NS,
        turn_id=live_turn_id,
        operation_id="b" * 32,
        branch_id=new_branch_id,
        payload={
            "outcome": "success",
            "duration_ms": 1000,
            "usage": {"normalized": {"input_total": 25, "output_total": 5}},
        },
        measurements={
            "/payload/duration_ms": {"source": "monotonic_clock", "method_version": 1},
            "/payload/usage/normalized/input_total": {"source": "provider", "adapter_version": 1},
            "/payload/usage/normalized/output_total": {"source": "provider", "adapter_version": 1},
        },
    )
    log.add(
        "turn.finished",
        4 * NS,
        turn_id=live_turn_id,
        branch_id=new_branch_id,
        payload={"end_reason": "cancelled", "duration_ms": 0},
    )
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert [turn.turn_id for turn in analysis.turns] == [live_turn_id]
    assert analysis.overview is not None
    assert analysis.overview.usage_tokens.value == 30
    assert analysis.diagnostics.rollback_projection_unresolved is False
    assert all(metric.precision is Precision.EXACT for metric in analysis.turns[0].wall_time_ns.values())
    assert analysis.turns[0].usage_tokens.precision is Precision.EXACT


def test_unmatched_branch_supersession_keeps_rollback_projection_unresolved(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add(
        "branch.superseded",
        1,
        turn_id=None,
        payload={"branch_id": "3" * 32, "superseded_by": "6" * 32},
    )
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    reason = "session trajectory integrity is unresolved: unresolved rollback projection"
    assert analysis.diagnostics.rollback_projection_unresolved is True
    assert analysis.overview is not None
    assert analysis.overview.elapsed_ns == Metric(0, Precision.UNRESOLVED, reason)
    assert analysis.validation is not None
    assert analysis.validation.tool_count == Metric(0, Precision.UNRESOLVED, reason)


def test_one_slot_rollback_range_removes_that_exact_sequence(tmp_path) -> None:
    new_branch_id = "6" * 32
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.add(
        "session.rollback",
        1,
        turn_id=None,
        branch_id=new_branch_id,
        payload={
            "old_branch_id": "3" * 32,
            "new_branch_id": new_branch_id,
            "superseded_from_sequence": 2,
            "superseded_to_sequence": 2,
        },
    )
    path = tmp_path / "events.jsonl"
    log.write(path)

    assert analyze_trajectory(path).turns == ()


def _cut_wait_log(path: Path, *, active_owner: bool) -> None:
    """Write a tool whose approval and MCP-connect wait straddle a rollback cut.

    With ``active_owner`` the owning turn is retained and only a later turn is rolled
    back; otherwise the owning turn itself is exclusively rolled back and its
    post-cut terminals land on the new branch.
    """
    log = EventLog()
    log.coverage()
    retained_turn_id = "4" * 32
    removed_turn_id = "5" * 32
    owner_turn_id = retained_turn_id if active_owner else removed_turn_id
    tool_id = "a" * 32
    approval_id = "b" * 32
    wait_id = "c" * 32
    superseded_from = log.next_sequence
    log.add("turn.started", 0, turn_id=owner_turn_id, payload={"turn_number": 1})
    log.add(
        "tool.operation.started",
        NS,
        turn_id=owner_turn_id,
        operation_id=tool_id,
        payload={"tool_name": "figma_render", "tool_kind": "mcp"},
    )
    log.add(
        "approval.requested",
        2 * NS,
        turn_id=owner_turn_id,
        operation_id=approval_id,
        payload={"approval_request_id": approval_id, "target_tool_operation_id": tool_id},
    )
    log.add(
        "wait.started",
        3 * NS,
        turn_id=owner_turn_id,
        operation_id=wait_id,
        payload={"category": "mcp_connect", "server_name": "figma", "target_operation_id": tool_id},
    )
    log.add(
        "turn.finished",
        4 * NS,
        turn_id=owner_turn_id,
        payload={"end_reason": "cancelled", "duration_ms": 4000},
    )
    if active_owner:
        superseded_from = log.next_sequence
        log.add("turn.started", 5 * NS, turn_id=removed_turn_id, payload={"turn_number": 2})
        terminal_branch = BRANCH_ID
    else:
        log.resolved_rollback(superseded_from, 5 * NS)
        terminal_branch = "6" * 32
    log.add(
        "approval.resolved",
        6 * NS,
        turn_id=owner_turn_id,
        operation_id=approval_id,
        branch_id=terminal_branch,
        payload={"approval_request_id": approval_id, "target_tool_operation_id": tool_id, "wait_ms": 4000},
        measurements={"/payload/wait_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add(
        "wait.finished",
        7 * NS,
        turn_id=owner_turn_id,
        operation_id=wait_id,
        branch_id=terminal_branch,
        payload={
            "category": "mcp_connect",
            "server_name": "figma",
            "target_operation_id": tool_id,
            "duration_ms": 4000,
        },
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    if active_owner:
        log.add(
            "turn.finished",
            8 * NS,
            turn_id=removed_turn_id,
            payload={"end_reason": "cancelled", "duration_ms": 3000},
        )
        log.resolved_rollback(superseded_from, 9 * NS, target_turn_id=retained_turn_id)
    log.write(path)


@pytest.mark.parametrize(
    ("active_owner", "expected_approvals", "expected_waits"),
    [
        pytest.param(True, {"a" * 32: (None,)}, {"figma": (None,)}, id="active_owner_remains_unresolved"),
        pytest.param(False, {}, {}, id="exclusively_inactive_owner_dropped"),
    ],
)
def test_cut_approval_and_mcp_wait_samples_follow_owner_activity(
    tmp_path: Path,
    active_owner: bool,
    expected_approvals: dict[str, tuple[None]],
    expected_waits: dict[str, tuple[None]],
) -> None:
    path = tmp_path / "events.jsonl"
    _cut_wait_log(path, active_owner=active_owner)

    analyzer = TrajectoryAnalyzer()
    analyzer.load(path)
    intermediate = analyzer._intermediate
    assert intermediate is not None
    inactive_ranges = facts_module._closed_sequence_union(intermediate.rollback_ranges)

    assert (
        insights_module._approval_durations_by_tool(
            intermediate,
            inactive_ranges,
            cancel_event=None,
        )
        == expected_approvals
    )
    waits = insights_module._mcp_connection_waits(
        intermediate,
        inactive_ranges,
        cancel_event=None,
    )
    assert waits.by_server == expected_waits
    assert waits.unattributed == 0
