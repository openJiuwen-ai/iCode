# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill and MCP usage panels, insight rows, tool payload observations, and context-carrying load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chrys.foundation.trajectory.envelope import Actor, SegmentedField, measurement
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.metadata import ANALYTICS_ITEM_ID_KEY
from chrys.service.analytics import (
    Metric,
    Precision,
    analyze_trajectory,
)
from chrys.service.analytics import _facts as facts_module
from chrys.service.analytics.classification import evidence_key
from tests.service.analytics._events import NS, EventLog, installed_events_path


def test_tool_usage_panels_group_skill_and_mcp_actions(tmp_path) -> None:
    skill_call = "7" * 32
    path = installed_events_path(tmp_path / ".chrys")
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    for operation, second, tool_name, tool_kind, call_item_id in (
        ("a", 1, "load_skill", "skill", skill_call),
        ("b", 2, "run_skill_script", "skill", "8" * 32),
        ("c", 3, "figma.render", "mcp", "9" * 32),
        ("d", 4, "figma.render", "mcp", "0" * 32),
        ("e", 5, "jira.search", "mcp", "1" * 32),
    ):
        log.span(
            "tool.operation",
            operation * 32,
            second * NS,
            (second + 1) * NS,
            start_payload={
                "tool_name": tool_name,
                "tool_kind": tool_kind,
                "call_item_id": call_item_id,
                "argument_fingerprint": operation,
            },
            finish_payload={"outcome": "success"},
        )
    log.add("turn.finished", 7 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "messages": [
                        {
                            "contents": [
                                {
                                    "type": "function_call",
                                    "arguments": json.dumps({"skill_name": "review-deck"}),
                                    "additional_properties": {ANALYTICS_ITEM_ID_KEY: skill_call},
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    analysis = analyze_trajectory(path)

    assert analysis.skill_usage is not None
    assert analysis.skill_usage.total == 2
    assert [(row.name, row.count) for row in analysis.skill_usage.rows] == [("review-deck", 1)]
    assert analysis.skill_usage.unattributed == 1
    assert analysis.skill_usage.precision is Precision.EXACT
    assert analysis.mcp_usage is not None
    assert analysis.mcp_usage.total == 3
    assert [(row.name, row.count) for row in analysis.mcp_usage.rows] == [("figma.render", 2), ("jira.search", 1)]
    assert analysis.mcp_usage.unattributed == 0


def test_insights_retain_structured_mcp_payload_wait_and_skill_metrics(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    mcp_id = "a" * 32
    log.add(
        "approval.requested",
        0,
        operation_id="b" * 32,
        payload={"approval_request_id": "b" * 32, "target_tool_operation_id": mcp_id},
    )
    log.add(
        "approval.resolved",
        NS,
        operation_id="b" * 32,
        payload={"approval_request_id": "b" * 32, "target_tool_operation_id": mcp_id, "wait_ms": 1000},
        measurements={"/payload/wait_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add(
        "wait.started",
        0,
        operation_id="c" * 32,
        payload={"category": "mcp_connect", "server_name": "figma", "target_operation_id": mcp_id},
    )
    log.add(
        "wait.finished",
        NS,
        operation_id="c" * 32,
        payload={
            "category": "mcp_connect",
            "server_name": "figma",
            "target_operation_id": mcp_id,
            "duration_ms": 1000,
        },
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add(
        "tool.operation.started",
        NS,
        operation_id=mcp_id,
        payload={
            "tool_name": "figma_render",
            "tool_kind": "mcp",
            "tool_context": {"server_name": "figma", "remote_name": "render"},
        },
    )
    log.add(
        "tool.payload.observed",
        2 * NS,
        operation_id=mcp_id,
        payload={
            "model_visible_bytes": 4096,
            "local_token_estimate": 100,
            "original_bytes": 8000,
            "truncated": True,
            "artifact_id": "artifact-1",
        },
    )
    log.add(
        "tool.operation.finished",
        3 * NS,
        operation_id=mcp_id,
        payload={"outcome": "success", "duration_ms": 2000},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    load_id = "d" * 32
    log.add(
        "tool.operation.started",
        4 * NS,
        operation_id=load_id,
        payload={
            "tool_name": "load_skill",
            "tool_kind": "skill",
            "tool_context": {"skill_name": "slides", "skill_revision": "rev-a"},
        },
    )
    log.add(
        "tool.payload.observed",
        5 * NS,
        operation_id=load_id,
        payload={"model_visible_bytes": 1000, "local_token_estimate": 250, "truncated": False},
    )
    log.add(
        "tool.operation.finished",
        5 * NS,
        operation_id=load_id,
        payload={"outcome": "success", "duration_ms": 1000},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    script_id = "e" * 32
    log.add(
        "tool.operation.started",
        6 * NS,
        operation_id=script_id,
        payload={
            "tool_name": "run_skill_script",
            "tool_kind": "skill",
            "tool_context": {
                "skill_name": "slides",
                "skill_revision": "rev-b",
                "script_name": "scripts/render.py",
            },
        },
    )
    log.add(
        "tool.operation.finished",
        7 * NS,
        operation_id=script_id,
        payload={"outcome": "failed", "duration_ms": 1000, "exit_code": 7},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add("turn.finished", 8 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)
    assert analysis.insights is not None
    mcp = analysis.insights.mcp.rows[0]
    assert (mcp.server_name, mcp.calls) == ("figma", 1)
    assert mcp.remotes[0].remote_name == "render"
    assert (mcp.result_bytes.value, mcp.result_bytes.precision) == (4096, Precision.EXACT)
    assert (mcp.result_tokens.value, mcp.result_tokens.precision) == (100, Precision.ESTIMATED)
    assert (mcp.truncated_count.value, mcp.spill_count.value) == (1, 1)
    assert (mcp.connection_wait_count.value, mcp.connection_wait_ns.value) == (1, NS)
    assert mcp.approval_blocking_share.value == pytest.approx(1 / 2)
    skill = analysis.insights.skills.rows[0]
    assert (skill.skill_name, skill.load_count, skill.script_count, skill.turn_count) == ("slides", 1, 1, 1)
    assert (skill.first_action_median_ns.value, skill.first_action_median_ns.precision) == (NS, Precision.EXACT)
    assert (skill.injected_tokens.value, skill.injected_tokens.precision) == (250, Precision.ESTIMATED)
    assert skill.revisions == ("rev-a", "rev-b")
    assert [(row.name, row.count) for row in skill.script_exit_codes] == [("7", 1)]


def test_skill_insights_count_retry_attempts_as_one_logical_turn(tmp_path: Path) -> None:
    first_turn_id = "4" * 32
    retry_turn_id = "5" * 32
    log = EventLog()
    log.coverage()
    for index, (turn_id, is_retry) in enumerate(((first_turn_id, False), (retry_turn_id, True))):
        start_ns = index * 10 * NS
        log.add(
            EventType.TURN_STARTED,
            start_ns,
            turn_id=turn_id,
            payload={"turn_number": 1, "is_retry": is_retry},
        )
        log.span(
            "tool.operation",
            str(index + 6) * 32,
            start_ns,
            start_ns + NS,
            turn_id=turn_id,
            start_payload={
                "tool_name": "load_skill",
                "tool_kind": "skill",
                "tool_context": {"skill_name": "slides", "skill_revision": "rev-a"},
            },
        )
        log.add(
            EventType.TURN_FINISHED,
            start_ns + 2 * NS,
            turn_id=turn_id,
            payload={"end_reason": "cancelled", "duration_ms": 2_000},
        )
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert len(analysis.turns) == 1
    assert len(analysis.turns[0].attempts) == 2
    assert analysis.insights is not None
    skill = analysis.insights.skills.rows[0]
    assert (skill.load_count, skill.turn_count) == (2, 1)


def test_negative_tool_payload_counts_are_not_reported_as_exact(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    operation_id = "a" * 32
    log.add(
        "tool.operation.started",
        0,
        operation_id=operation_id,
        payload={
            "tool_name": "figma_render",
            "tool_kind": "mcp",
            "tool_context": {"server_name": "figma", "remote_name": "render"},
        },
    )
    log.add(
        "tool.payload.observed",
        NS,
        operation_id=operation_id,
        payload={"model_visible_bytes": -1, "local_token_estimate": -2, "original_bytes": -3},
    )
    log.add(
        "tool.operation.finished",
        2 * NS,
        operation_id=operation_id,
        payload={"outcome": "success", "duration_ms": 2000},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add("turn.finished", 2 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert analysis.insights is not None
    mcp = analysis.insights.mcp.rows[0]
    assert (mcp.result_bytes.value, mcp.result_bytes.precision) == (None, Precision.MISSING)
    assert (mcp.result_tokens.value, mcp.result_tokens.precision) == (None, Precision.MISSING)


def test_skill_action_starting_before_the_load_terminal_is_not_an_exact_latency(tmp_path) -> None:
    """A related action that began before the load's terminal landed cannot
    prove it used the loaded skill, even when the boundary timestamps meet."""
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    load_id = "d" * 32
    script_id = "e" * 32
    log.add(
        "tool.operation.started",
        NS,
        operation_id=load_id,
        payload={
            "tool_name": "load_skill",
            "tool_kind": "skill",
            "tool_context": {"skill_name": "slides", "skill_revision": "rev-a"},
        },
    )
    log.add(
        "tool.operation.started",
        2 * NS,
        operation_id=script_id,
        payload={
            "tool_name": "run_skill_script",
            "tool_kind": "skill",
            "tool_context": {"skill_name": "slides", "skill_revision": "rev-a", "script_name": "scripts/render.py"},
        },
    )
    log.add(
        "tool.operation.finished",
        2 * NS,
        operation_id=load_id,
        payload={"outcome": "success", "duration_ms": 1000},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add(
        "tool.operation.finished",
        3 * NS,
        operation_id=script_id,
        payload={"outcome": "success", "duration_ms": 1000},
        measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
    )
    log.add("turn.finished", 4 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert analysis.insights is not None
    skill = analysis.insights.skills.rows[0]
    assert skill.first_action_median_ns == Metric(
        None, Precision.UNRESOLVED, "load-to-action latency endpoints are unresolved"
    )


def test_malformed_tool_payload_truncated_flag_is_not_reported_as_exact(tmp_path) -> None:
    """A malformed boolean remains unknown on an otherwise valid log line."""
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    for index, payload in enumerate(
        (
            {"model_visible_bytes": 10, "truncated": True, "artifact_id": "artifact-1"},
            {"model_visible_bytes": 10, "truncated": "true", "artifact_id": "artifact-2"},
        )
    ):
        operation_id = f"{index + 10:032x}"
        log.add(
            "tool.operation.started",
            index * 2 * NS,
            operation_id=operation_id,
            payload={
                "tool_name": "figma_render",
                "tool_kind": "mcp",
                "tool_context": {"server_name": "figma", "remote_name": "render"},
            },
        )
        log.add("tool.payload.observed", (index * 2 + 1) * NS, operation_id=operation_id, payload=payload)
        log.add(
            "tool.operation.finished",
            (index * 2 + 2) * NS,
            operation_id=operation_id,
            payload={"outcome": "success", "duration_ms": 2000},
            measurements={"/payload/duration_ms": measurement("monotonic_clock", method_version=1)},
        )
    log.add("turn.finished", 4 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)

    assert analysis.diagnostics is not None
    assert analysis.diagnostics.corrupt_line_count == 0
    assert analysis.diagnostics.accounted_prefix_violations == ()
    assert analysis.insights is not None
    mcp = analysis.insights.mcp.rows[0]
    assert mcp.truncated_count == Metric(
        1,
        Precision.ESTIMATED,
        "one or more tool payload observations are missing",
    )
    assert mcp.spill_count == Metric(2, Precision.EXACT)


def test_spilled_unknown_flag_remains_defensive_unknown_evidence() -> None:
    """Envelope validation makes this state unreachable from persisted logs."""
    payload = facts_module._ToolPayloadExtras(
        sequence=1,
        scope=facts_module._EventScope(
            runtime_id="runtime",
            branch_id="branch",
            coverage_id="coverage",
            actor_id=None,
            turn_id=None,
        ),
        model_visible_bytes=None,
        local_token_estimate=None,
        original_bytes=None,
        flags=facts_module._TOOL_PAYLOAD_SPILLED_UNKNOWN_BIT,
    )

    assert payload.spilled is None


def test_missing_or_unknown_tool_identity_uses_only_frozen_fallback_buckets(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "a" * 32,
        0,
        NS,
        start_payload={"tool_name": "figma_render", "tool_kind": "mcp"},
    )
    log.span(
        "tool.operation",
        "b" * 32,
        NS,
        2 * NS,
        start_payload={"tool_name": "load_skill", "tool_kind": "future.kind"},
    )
    log.add("turn.finished", 2 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)
    assert analysis.insights is not None
    assert analysis.insights.mcp.total == 1
    assert analysis.insights.mcp.unattributed == 1
    assert analysis.insights.mcp.rows == ()
    assert analysis.insights.mcp.precision is Precision.EXACT
    assert analysis.insights.skills.total == 0
    assert analysis.insights.tools.unclassified == 1
    assert any(
        row.tool_kind == "unclassified" and row.tool_name == "load_skill" for row in analysis.insights.tools.rows
    )
    assert analysis.turns[0].action_projection_precision is Precision.EXACT


def test_an_extension_s_calls_are_grouped_under_its_kind(tmp_path) -> None:
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    log.span(
        "tool.operation",
        "a" * 32,
        0,
        NS,
        start_payload={"tool_name": "acme_project_name", "tool_kind": "extension.acme_release"},
    )
    log.add("turn.finished", NS, payload={"end_reason": "completed", "duration_ms": 0})
    path = tmp_path / "events.jsonl"
    log.write(path)

    analysis = analyze_trajectory(path)
    assert analysis.insights is not None
    assert analysis.insights.tools.unclassified == 0
    assert [(row.tool_kind, row.tool_name) for row in analysis.insights.tools.rows] == [
        ("extension.acme_release", "acme_project_name")
    ]


@pytest.mark.parametrize("workflow_run", [False, True], ids=["chat", "workflow-session"])
def test_context_carrying_load_is_surfaced_without_changing_finding_identity(tmp_path, workflow_run: bool) -> None:
    item_id = "7" * 32
    revision_id = "8" * 32
    segment_id = "9" * 32
    exchange_id = "a" * 32
    side_revision_id = "b" * 32
    side_segment_id = "c" * 32
    side_exchange_id = "d" * 32
    side_actor = Actor(kind="side_call", role="title_gen", actor_id="0" * 32)
    path = installed_events_path(tmp_path)
    log = EventLog()
    log.coverage()
    log.add("turn.started", 0, payload={"turn_number": 1})
    revision = log.add(
        "context.revision.recorded",
        NS,
        operation_id=revision_id,
        parent_operation_id=exchange_id,
        payload={
            "revision_id": revision_id,
            "is_checkpoint": True,
            "item_count": 1,
            "untokenized_item_count": 0,
            "unidentified_item_count": 0,
        },
        segmented_fields=(SegmentedField(field_pointer="/payload/refs", segment_group_id=segment_id, segment_count=1),),
    )
    log.add(
        "event.segment",
        NS,
        operation_id=None,
        payload={
            "parent_event_id": revision.event_id,
            "field_pointer": "/payload/refs",
            "segment_group_id": segment_id,
            "segment_index": 0,
            "segment_count": 1,
            "encoding": "array_slice",
            "entries": [{"item_id": item_id, "occurrence": 0, "position": 0, "action": "add"}],
        },
    )
    log.span(
        "model.exchange",
        exchange_id,
        2 * NS,
        3 * NS,
        start_payload={"context_revision_id": revision_id},
    )
    side_revision = log.add(
        "context.revision.recorded",
        4 * NS,
        operation_id=side_revision_id,
        parent_operation_id=side_exchange_id,
        actor=side_actor,
        payload={
            "revision_id": side_revision_id,
            "is_checkpoint": True,
            "item_count": 1,
            "untokenized_item_count": 0,
            "unidentified_item_count": 0,
        },
        segmented_fields=(
            SegmentedField(field_pointer="/payload/refs", segment_group_id=side_segment_id, segment_count=1),
        ),
    )
    log.add(
        "event.segment",
        4 * NS,
        operation_id=None,
        actor=side_actor,
        payload={
            "parent_event_id": side_revision.event_id,
            "field_pointer": "/payload/refs",
            "segment_group_id": side_segment_id,
            "segment_index": 0,
            "segment_count": 1,
            "encoding": "array_slice",
            "entries": [{"item_id": item_id, "occurrence": 0, "position": 0, "action": "add"}],
        },
    )
    log.span(
        "model.exchange",
        side_exchange_id,
        5 * NS,
        6 * NS,
        actor=side_actor,
        start_payload={"context_revision_id": side_revision_id},
    )
    log.add("turn.finished", 6 * NS, payload={"end_reason": "cancelled", "duration_ms": 0})
    if workflow_run:
        log.span(
            "workflow.run",
            "e" * 32,
            7 * NS,
            8 * NS,
            turn_id=None,
            start_payload={"workflow": "demo-workflow", "run_id": "e" * 32},
            finish_payload={"outcome": "completed"},
        )
    log.write(path)
    path.parents[1].joinpath("session.json").write_text(
        json.dumps(
            {
                "state": {
                    "messages": [
                        {
                            "role": "assistant",
                            "additional_properties": {
                                ANALYTICS_ITEM_ID_KEY: item_id,
                                "_group": {"token_count": 100_000},
                            },
                            "contents": [
                                {"type": "text_reasoning", "text": "…"},
                                {"type": "function_call", "call_id": "call-1", "name": "zsh", "arguments": "{}"},
                                {"type": "function_call", "call_id": "call-2", "name": "zsh", "arguments": "{}"},
                            ],
                        },
                        {
                            "role": "tool",
                            "additional_properties": {
                                ANALYTICS_ITEM_ID_KEY: "6" * 32,
                                "_group": {"token_count": 4},
                            },
                            "contents": [{"type": "function_result", "call_id": "call-1", "result": "ok"}],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    analysis = analyze_trajectory(path)
    if workflow_run:
        # A workflow session shows no Insights to name the item by, so no
        # finding points at it.
        assert analysis.workflow_runs
        assert analysis.insights is None
        assert all(row.rule_id != "context-carrying-load" for row in analysis.findings)
        return
    assert analysis.insights is not None
    carrying = analysis.insights.context_carrying_load
    assert len(carrying) == 1
    assert (carrying[0].item_id, carrying[0].load, carrying[0].turn_number) == (item_id, 100_000, 1)
    assert (carrying[0].token_count, carrying[0].carry_count, carrying[0].origin_turn_number) == (100_000, 1, 1)
    assert (carrying[0].role, carrying[0].tool_names) == ("assistant", ("zsh", "zsh"))
    finding = next(row for row in analysis.findings if row.rule_id == "context-carrying-load")
    assert finding.evidence_key == evidence_key("context-carrying-load", 1, (f"item:{item_id}",))
