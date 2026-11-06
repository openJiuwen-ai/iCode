# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool, MCP and Skill insight panels and context carrying load.

The panels group already resolved turns and actions with shared counting and
precision rules. Approval durations, MCP connection waits and context carrying
load also read the fact index directly: the first two take rollback ownership
from ``_timeline``; context carrying load matches model exchanges to the
revision memberships ``_context_evidence`` verified and weights each carried
item by its session-projection token count.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from statistics import median
from threading import Event
from typing import NamedTuple, cast

from chrys.foundation.tool_kinds import TOOL_KINDS, is_extension_kind
from chrys.foundation.trajectory.sequences import SequenceRange
from chrys.service.analytics._context_evidence import _RevisionResolution
from chrys.service.analytics._facts import (
    _active,
    _Endpoint,
    _Intermediate,
    _lifecycle_cut,
    _LifecycleCut,
    _payload_str,
)
from chrys.service.analytics._metric_ops import _cap_session_grade, _cap_session_metric, _percentile_metric
from chrys.service.analytics._session_projection import _SessionProjection
from chrys.service.analytics._timeline import _exclusively_inactive, _node_owner_membership
from chrys.service.analytics.model import (
    ActionOperation,
    ContextCarryingLoad,
    Grade,
    InsightsAnalysis,
    McpInsights,
    McpRemoteRow,
    McpServerRow,
    Metric,
    NamedCountRow,
    Precision,
    SkillActivityRow,
    SkillInsightRow,
    SkillInsights,
    ToolInsightRow,
    ToolInsights,
    ToolUsagePanel,
    TurnAnalysis,
)
from chrys.service.analytics.reader import raise_if_cancelled as _check_cancelled


def _insights_analysis(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    turns: list[TurnAnalysis],
    logical_turns: list[TurnAnalysis],
    actions: tuple[ActionOperation, ...],
    *,
    context_carrying_load: tuple[ContextCarryingLoad, ...],
    cancel_event: Event | None,
) -> InsightsAnalysis:
    return InsightsAnalysis(
        tools=_tool_insights(actions, turns),
        mcp=_mcp_insights(intermediate, inactive_ranges, actions, turns, cancel_event=cancel_event),
        skills=_skill_insights(actions, turns, logical_turns),
        context_carrying_load=context_carrying_load,
    )


def _tool_insights(actions: tuple[ActionOperation, ...], turns: list[TurnAnalysis]) -> ToolInsights:
    grouped: dict[tuple[str, str | None], list[ActionOperation]] = defaultdict(list)
    unclassified = 0
    for action in actions:
        kind = action.tool_kind
        tool_kind = kind if kind is not None and (kind in TOOL_KINDS or is_extension_kind(kind)) else "unclassified"
        if tool_kind == "unclassified":
            unclassified += 1
        grouped[(tool_kind, action.tool_name)].append(action)
    total_duration = _complete_duration_total(actions)
    rows = tuple(
        sorted(
            (
                ToolInsightRow(
                    tool_kind=tool_kind,
                    tool_name=tool_name,
                    calls=len(group),
                    duration_share=_duration_share(group, total_duration),
                    p50_ns=_duration_percentile(group, 0.50),
                    p95_ns=_duration_percentile(group, 0.95),
                    outcomes=_outcome_rows(group),
                )
                for (tool_kind, tool_name), group in grouped.items()
            ),
            key=lambda row: (
                -(float(row.duration_share.value) if row.duration_share.value is not None else -1.0),
                -row.calls,
                row.tool_kind,
                row.tool_name or "",
            ),
        )
    )
    grade = _action_panel_grade(turns)
    return ToolInsights(
        total=len(actions),
        rows=rows,
        unclassified=unclassified,
        precision=grade.precision,
        reason=grade.reason,
    )


def _mcp_insights(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    actions: tuple[ActionOperation, ...],
    turns: list[TurnAnalysis],
    *,
    cancel_event: Event | None,
) -> McpInsights:
    selected = [action for action in actions if action.tool_kind == "mcp"]
    grouped: dict[str, list[ActionOperation]] = defaultdict(list)
    unattributed = 0
    for action in selected:
        if action.server_name is None:
            unattributed += 1
        else:
            grouped[action.server_name].append(action)
    total_duration = _complete_duration_total(actions)
    approval_by_tool = _approval_durations_by_tool(intermediate, inactive_ranges, cancel_event=cancel_event)
    connection_waits = _mcp_connection_waits(
        intermediate,
        inactive_ranges,
        cancel_event=cancel_event,
    )
    turns_by_id = {turn.turn_id: turn for turn in turns}
    rows = tuple(
        sorted(
            (
                _mcp_server_row(
                    server_name,
                    group,
                    total_duration=total_duration,
                    approval_by_tool=approval_by_tool,
                    connection_waits=connection_waits.by_server.get(server_name, ()),
                    turns_by_id=turns_by_id,
                )
                for server_name, group in grouped.items()
            ),
            key=lambda row: (
                -(float(row.duration_share.value) if row.duration_share.value is not None else -1.0),
                -row.calls,
                row.server_name,
            ),
        )
    )
    grade = _action_panel_grade(turns)
    return McpInsights(
        total=len(selected),
        rows=rows,
        unattributed=unattributed,
        unattributed_connection_waits=connection_waits.unattributed,
        precision=grade.precision,
        reason=grade.reason,
    )


def _mcp_server_row(
    server_name: str,
    actions: list[ActionOperation],
    *,
    total_duration: int | None,
    approval_by_tool: dict[str, tuple[int | None, ...]],
    connection_waits: tuple[int | None, ...],
    turns_by_id: dict[str, TurnAnalysis],
) -> McpServerRow:
    remote_groups: dict[str | None, list[ActionOperation]] = defaultdict(list)
    for action in actions:
        remote_groups[action.remote_name].append(action)
    remotes = tuple(
        McpRemoteRow(
            remote_name=remote_name,
            calls=len(group),
            p50_ns=_duration_percentile(group, 0.50),
            p95_ns=_duration_percentile(group, 0.95),
            outcomes=_outcome_rows(group),
        )
        for remote_name, group in sorted(remote_groups.items(), key=lambda item: (-len(item[1]), item[0] or ""))
    )
    approval_samples = tuple(
        duration for action in actions for duration in approval_by_tool.get(action.operation_id, ())
    )
    action_duration = _complete_duration_total(actions)
    approval_total = sum(duration for duration in approval_samples if duration is not None)
    if any(duration is None for duration in approval_samples) or action_duration is None:
        approval_share = Metric(None, Precision.UNRESOLVED, "one or more approval or tool intervals are unresolved")
    else:
        # A tool operation stays open for its whole approval wait, so the
        # operation durations already contain the approval time; adding the
        # approval total to the denominator would count that wait twice.
        approval_share = Metric(approval_total / action_duration if action_duration else 0.0, Precision.EXACT)
    return McpServerRow(
        server_name=server_name,
        calls=len(actions),
        duration_share=_duration_share(actions, total_duration),
        p50_ns=_duration_percentile(actions, 0.50),
        p95_ns=_duration_percentile(actions, 0.95),
        outcomes=_outcome_rows(actions),
        approval_blocking_share=approval_share,
        result_bytes=_payload_sum_metric(actions, "bytes"),
        result_tokens=_payload_sum_metric(actions, "tokens", inherently_estimated=True),
        truncated_count=_payload_sum_metric(actions, "truncated"),
        spill_count=_payload_sum_metric(actions, "spill"),
        critical_path_exclusive_ns=_server_cp_metric(server_name, actions, turns_by_id),
        connection_wait_count=Metric(len(connection_waits), Precision.EXACT),
        connection_wait_ns=_connection_wait_duration(connection_waits),
        remotes=remotes,
    )


def _skill_insights(
    actions: tuple[ActionOperation, ...],
    turns: list[TurnAnalysis],
    logical_turns: list[TurnAnalysis],
) -> SkillInsights:
    selected = [action for action in actions if action.tool_kind == "skill"]
    grouped: dict[str, list[ActionOperation]] = defaultdict(list)
    not_found: Counter[str] = Counter()
    unattributed = 0
    for action in selected:
        if action.skill_name is None:
            unattributed += 1
        elif action.skill_revision is None:
            if action.tool_name == "load_skill":
                not_found[action.skill_name] += 1
            else:
                unattributed += 1
        else:
            grouped[action.skill_name].append(action)
    turns_by_id = {turn.turn_id: turn for turn in turns}
    canonical_turn_ids = {
        attempt.turn_id: logical_turn.turn_id for logical_turn in logical_turns for attempt in logical_turn.attempts
    }
    rows = tuple(
        sorted(
            (_skill_row(skill_name, group, turns_by_id, canonical_turn_ids) for skill_name, group in grouped.items()),
            key=lambda row: (-row.load_count, -row.script_count - row.resource_count, row.skill_name),
        )
    )
    grade = _action_panel_grade(turns)
    return SkillInsights(
        total=len(selected),
        rows=rows,
        not_found=tuple(
            NamedCountRow(name=name, count=count)
            for name, count in sorted(not_found.items(), key=lambda item: (-item[1], item[0]))
        ),
        unattributed=unattributed,
        precision=grade.precision,
        reason=grade.reason,
    )


def _skill_row(
    skill_name: str,
    actions: list[ActionOperation],
    turns_by_id: dict[str, TurnAnalysis],
    canonical_turn_ids: dict[str, str],
) -> SkillInsightRow:
    loads = [action for action in actions if action.tool_name == "load_skill"]
    scripts = [action for action in actions if action.tool_name == "run_skill_script"]
    resources = [action for action in actions if action.tool_name == "read_skill_resource"]
    script_groups: dict[str | None, list[ActionOperation]] = defaultdict(list)
    resource_groups: dict[str | None, list[ActionOperation]] = defaultdict(list)
    for action in scripts:
        script_groups[action.script_name].append(action)
    for action in resources:
        resource_groups[action.resource_name].append(action)
    return SkillInsightRow(
        skill_name=skill_name,
        load_count=len(loads),
        turn_count=len({canonical_turn_ids.get(action.turn_id, action.turn_id) for action in actions}),
        first_action_median_ns=_skill_first_action_latency(loads, (*scripts, *resources), turns_by_id),
        script_count=len(scripts),
        script_outcomes=_outcome_rows(scripts),
        script_exit_codes=_exit_code_rows(scripts),
        resource_count=len(resources),
        injected_tokens=_payload_sum_metric(loads, "tokens", inherently_estimated=True),
        revisions=tuple(sorted({action.skill_revision for action in actions if action.skill_revision is not None})),
        scripts=tuple(
            SkillActivityRow(
                name=name,
                count=len(group),
                outcomes=_outcome_rows(group),
                exit_codes=_exit_code_rows(group),
            )
            for name, group in sorted(script_groups.items(), key=lambda item: (-len(item[1]), item[0] or ""))
        ),
        resources=tuple(
            SkillActivityRow(name=name, count=len(group), outcomes=_outcome_rows(group))
            for name, group in sorted(resource_groups.items(), key=lambda item: (-len(item[1]), item[0] or ""))
        ),
    )


def _action_panel_grade(turns: list[TurnAnalysis]) -> Grade:
    degraded = next((turn for turn in turns if turn.action_projection_precision is not Precision.EXACT), None)
    if degraded is None:
        return Grade(Precision.EXACT)
    return Grade(Precision.UNRESOLVED, degraded.action_projection_reason or "tool action projection is incomplete")


def _complete_duration_total(actions: Iterable[ActionOperation]) -> int | None:
    durations = [action.duration_ns for action in actions]
    return (
        sum(cast("int", duration) for duration in durations)
        if all(duration is not None for duration in durations)
        else None
    )


def _duration_share(actions: Iterable[ActionOperation], total_duration: int | None) -> Metric:
    duration = _complete_duration_total(actions)
    if duration is None or total_duration is None:
        return Metric(None, Precision.UNRESOLVED, "one or more tool durations are unresolved")
    return Metric(duration / total_duration if total_duration else 0.0, Precision.EXACT)


def _duration_percentile(actions: Iterable[ActionOperation], quantile: float) -> Metric:
    group = list(actions)
    durations = [action.duration_ns for action in group]
    if not group:
        return Metric(None, Precision.MISSING, "no matching tool calls")
    if any(duration is None for duration in durations):
        return Metric(None, Precision.UNRESOLVED, "one or more tool durations are unresolved")
    return _percentile_metric([cast("int", duration) for duration in durations], quantile)


def _outcome_rows(actions: Iterable[ActionOperation]) -> tuple[NamedCountRow, ...]:
    counts = Counter(action.outcome or "unresolved" for action in actions)
    return tuple(
        NamedCountRow(name=name, count=count)
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


def _exit_code_rows(actions: Iterable[ActionOperation]) -> tuple[NamedCountRow, ...]:
    counts = Counter(str(action.exit_code) for action in actions if action.exit_code is not None)
    return tuple(
        NamedCountRow(name=name, count=count)
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )


def _payload_sum_metric(
    actions: Iterable[ActionOperation],
    field: str,
    *,
    inherently_estimated: bool = False,
) -> Metric:
    group = list(actions)
    observed = [action for action in group if action.payload_observed]
    if not observed:
        return Metric(None, Precision.MISSING, "no tool payload observation is available")
    values: list[int] = []
    for action in observed:
        value: int | None
        if field == "bytes":
            value = action.payload_bytes
        elif field == "tokens":
            value = action.payload_token_estimate
        elif field == "truncated":
            value = int(action.payload_truncated) if action.payload_truncated is not None else None
        else:
            value = int(action.payload_spilled) if action.payload_spilled is not None else None
        if value is not None:
            values.append(value)
    if not values:
        return Metric(None, Precision.MISSING, "the observed payload did not report this field")
    partial = len(observed) < len(group) or len(values) < len(observed)
    precision = Precision.ESTIMATED if partial or inherently_estimated else Precision.EXACT
    reason = (
        "local tokenizer estimate"
        if inherently_estimated and not partial
        else "one or more tool payload observations are missing"
        if partial
        else None
    )
    return Metric(sum(values), precision, reason)


def _approval_durations_by_tool(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    cancel_event: Event | None,
) -> dict[str, tuple[int | None, ...]]:
    durations: dict[str, tuple[int | None, ...]] = {}
    for node in intermediate.nodes.get("approval", {}).values():
        _check_cancelled(cancel_event)
        cut = _lifecycle_cut(node, inactive_ranges)
        if cut is not _LifecycleCut.NONE:
            raw_start = node.starts[0]
            if raw_start.side_call:
                continue
            target = _payload_str(raw_start.payload, "target_tool_operation_id")
            if target is None:
                continue
            ownership = _node_owner_membership(
                intermediate,
                node,
                inactive_ranges,
                target_operation_id=target,
            )
            if _exclusively_inactive(ownership):
                continue
            durations[target] = (*durations.get(target, ()), None)
            continue
        starts = [event for event in node.starts if _active(event.sequence, inactive_ranges) and not event.side_call]
        finishes = [
            event for event in node.finishes if _active(event.sequence, inactive_ranges) and not event.side_call
        ]
        if not starts:
            continue
        target = _payload_str(starts[0].payload, "target_tool_operation_id") if len(starts) == 1 else None
        if target is None:
            continue
        duration = None
        if (
            len(finishes) == 1
            and finishes[0].monotonic_measurement
            and finishes[0].scope == starts[0].scope
            and finishes[0].sequence > starts[0].sequence
            and finishes[0].monotonic_ns >= starts[0].monotonic_ns
        ):
            duration = finishes[0].monotonic_ns - starts[0].monotonic_ns
        durations[target] = (*durations.get(target, ()), duration)
    return durations


@dataclass(frozen=True, slots=True)
class _ConnectionWaits:
    """The MCP connection waits the log records."""

    by_server: dict[str, tuple[int | None, ...]]
    """Each server's wait durations; None for a wait that cannot be measured."""
    unattributed: int
    """Waits that name no single server."""


def _mcp_connection_waits(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    cancel_event: Event | None,
) -> _ConnectionWaits:
    grouped: dict[str, tuple[int | None, ...]] = {}
    unattributed = 0
    for node in intermediate.nodes.get("wait", {}).values():
        _check_cancelled(cancel_event)
        cut = _lifecycle_cut(node, inactive_ranges)
        if cut is not _LifecycleCut.NONE:
            raw_start = node.starts[0]
            if raw_start.side_call or _payload_str(raw_start.payload, "category") != "mcp_connect":
                continue
            target = _payload_str(raw_start.payload, "target_operation_id")
            ownership = _node_owner_membership(
                intermediate,
                node,
                inactive_ranges,
                target_operation_id=target,
            )
            if _exclusively_inactive(ownership):
                continue
            server_name = _payload_str(raw_start.payload, "server_name")
            if server_name is None:
                unattributed += 1
            else:
                grouped[server_name] = (*grouped.get(server_name, ()), None)
            continue
        starts = [event for event in node.starts if _active(event.sequence, inactive_ranges) and not event.side_call]
        finishes = [
            event for event in node.finishes if _active(event.sequence, inactive_ranges) and not event.side_call
        ]
        if not starts or not any(_payload_str(start.payload, "category") == "mcp_connect" for start in starts):
            continue
        server_name = _payload_str(starts[0].payload, "server_name") if len(starts) == 1 else None
        if server_name is None:
            unattributed += 1
            continue
        duration = None
        if (
            len(finishes) == 1
            and finishes[0].monotonic_measurement
            and finishes[0].scope == starts[0].scope
            and finishes[0].sequence > starts[0].sequence
            and finishes[0].monotonic_ns >= starts[0].monotonic_ns
        ):
            duration = finishes[0].monotonic_ns - starts[0].monotonic_ns
        grouped[server_name] = (*grouped.get(server_name, ()), duration)
    return _ConnectionWaits(grouped, unattributed)


def _connection_wait_duration(samples: tuple[int | None, ...]) -> Metric:
    if any(sample is None for sample in samples):
        return Metric(None, Precision.UNRESOLVED, "one or more MCP connection waits are unresolved")
    return Metric(sum(cast("int", sample) for sample in samples), Precision.EXACT)


def _server_cp_metric(
    server_name: str,
    actions: list[ActionOperation],
    turns_by_id: dict[str, TurnAnalysis],
) -> Metric:
    relevant = [turns_by_id[turn_id] for turn_id in {action.turn_id for action in actions} if turn_id in turns_by_id]
    exact = [turn for turn in relevant if turn.response_cp_ns.precision is Precision.EXACT]
    unresolved_count = len(relevant) - len(exact)
    if not exact:
        return Metric(None, Precision.UNRESOLVED, "no related turn has an exact response critical path")
    value = sum(turn.server_critical_contributions_ns.get(server_name, 0) for turn in exact)
    if unresolved_count:
        return Metric(
            value,
            Precision.ESTIMATED,
            f"{unresolved_count} related turn(s) have an unresolved response critical path",
        )
    return Metric(value, Precision.EXACT)


def _skill_first_action_latency(
    loads: list[ActionOperation],
    related: tuple[ActionOperation, ...],
    turns_by_id: dict[str, TurnAnalysis],
) -> Metric:
    samples: list[int] = []
    unresolved = 0
    ordered_related = sorted(related, key=lambda action: action.start_sequence)
    for load in loads:
        following = next((action for action in ordered_related if action.start_sequence > load.start_sequence), None)
        if following is None:
            continue
        load_turn = turns_by_id.get(load.turn_id)
        action_turn = turns_by_id.get(following.turn_id)
        # An action that began before the load's terminal landed cannot
        # prove it used the loaded skill, however its timestamps read.
        if (
            load.end_ns is None
            or load.end_sequence is None
            or following.start_sequence <= load.end_sequence
            or load_turn is None
            or action_turn is None
            or load_turn.runtime_id != action_turn.runtime_id
            or following.start_ns < load.end_ns
        ):
            unresolved += 1
            continue
        samples.append(following.start_ns - load.end_ns)
    if not samples:
        if unresolved:
            return Metric(None, Precision.UNRESOLVED, "load-to-action latency endpoints are unresolved")
        return Metric(None, Precision.MISSING, "no load was followed by a related skill action")
    value = int(median(samples))
    if unresolved:
        return Metric(value, Precision.ESTIMATED, "one or more load-to-action latency samples are unresolved")
    return Metric(value, Precision.EXACT)


class _RevisionClaim(NamedTuple):
    """The model exchange that sent a context revision, and the event that started it."""

    exchange_operation_id: str
    start: _Endpoint


class _CarriedItem(NamedTuple):
    """How many requests carried a context item, and the revisions of the first and the last."""

    carry_count: int
    first: _Endpoint
    last: _Endpoint


def _context_carrying_load(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
    turns: list[TurnAnalysis],
    revisions: _RevisionResolution,
    projection: _SessionProjection,
    *,
    cancel_event: Event | None,
) -> tuple[ContextCarryingLoad, ...]:
    if not projection.item_tokens:
        return ()
    turns_by_id = {turn.turn_id: turn for turn in turns}
    claims: dict[str, _RevisionClaim] = {}
    ambiguous_claims: set[str] = set()
    for node in intermediate.nodes.get("model.exchange", {}).values():
        _check_cancelled(cancel_event)
        for start in node.starts:
            if not _active(start.sequence, inactive_ranges) or start.side_call:
                continue
            revision_id = _payload_str(start.payload, "context_revision_id")
            if revision_id is None or revision_id in ambiguous_claims:
                continue
            if revision_id in claims:
                claims.pop(revision_id)
                ambiguous_claims.add(revision_id)
            else:
                claims[revision_id] = _RevisionClaim(node.operation_id, start)

    carried: dict[str, _CarriedItem] = {}
    for revision_id, claim in sorted(claims.items(), key=lambda item: item[1].start.sequence):
        _check_cancelled(cancel_event)
        start = claim.start
        revision = revisions.endpoints.get(revision_id)
        membership = revisions.memberships.get(revision_id)
        if (
            revision is None
            or membership is None
            or revision_id in revisions.errors
            or revision.side_call
            or revision.parent_operation_id != claim.exchange_operation_id
            or revision.runtime_id != start.runtime_id
            or revision.branch_id != start.branch_id
            or revision.coverage_id != start.coverage_id
            or revision.actor_id != start.actor_id
            or revision.sequence >= start.sequence
        ):
            continue
        counted: set[str] = set()
        for item_id in membership:
            _check_cancelled(cancel_event)
            if item_id in counted or item_id not in projection.item_tokens:
                continue
            counted.add(item_id)
            previous = carried.get(item_id)
            carried[item_id] = (
                _CarriedItem(previous.carry_count + 1, previous.first, revision)
                if previous is not None
                else _CarriedItem(1, revision, revision)
            )

    rows: list[ContextCarryingLoad] = []
    for item_id, item in carried.items():
        _check_cancelled(cancel_event)
        token_count = projection.item_tokens[item_id]
        if token_count <= 0:
            continue
        turn = turns_by_id.get(item.last.turn_id or "")
        origin_turn = turns_by_id.get(item.first.turn_id or "")
        rows.append(
            ContextCarryingLoad(
                load=token_count * item.carry_count,
                item_id=item_id,
                # One row per item: items still in context share their last revision.
                occurrence_id=f"context-load:{item_id}",
                turn_id=item.last.turn_id,
                turn_number=turn.turn_number if turn is not None else None,
                token_count=token_count,
                carry_count=item.carry_count,
                origin_turn_number=origin_turn.turn_number if origin_turn is not None else None,
                role=projection.item_roles.get(item_id),
                tool_names=projection.item_tool_names.get(item_id, ()),
            )
        )
    return tuple(rows)


def _tool_usage_panel(
    actions: tuple[ActionOperation, ...],
    turns: list[TurnAnalysis],
    *,
    tool_kind: str,
    display_name: Callable[[ActionOperation], str | None],
) -> ToolUsagePanel:
    selected = [action for action in actions if action.tool_kind == tool_kind]
    counts: Counter[str] = Counter()
    unattributed = 0
    for action in selected:
        name = display_name(action)
        if name is None:
            unattributed += 1
        else:
            counts[name] += 1
    rows = tuple(
        NamedCountRow(name=name, count=count)
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
    degraded = next((turn for turn in turns if turn.action_projection_precision is not Precision.EXACT), None)
    if degraded is not None:
        return ToolUsagePanel(
            total=len(selected),
            rows=rows,
            unattributed=unattributed,
            precision=Precision.UNRESOLVED,
            reason=degraded.action_projection_reason or "tool action projection is incomplete",
        )
    return ToolUsagePanel(total=len(selected), rows=rows, unattributed=unattributed)


def _degrade_insights(insights: InsightsAnalysis, reason: str) -> InsightsAnalysis:
    tools_grade = _cap_session_grade(Grade(insights.tools.precision, insights.tools.reason), reason)
    tools = replace(
        insights.tools,
        rows=tuple(
            replace(
                row,
                duration_share=_cap_session_metric(row.duration_share, reason),
                p50_ns=_cap_session_metric(row.p50_ns, reason),
                p95_ns=_cap_session_metric(row.p95_ns, reason),
            )
            for row in insights.tools.rows
        ),
        precision=tools_grade.precision,
        reason=tools_grade.reason,
    )
    mcp_grade = _cap_session_grade(Grade(insights.mcp.precision, insights.mcp.reason), reason)
    mcp = replace(
        insights.mcp,
        rows=tuple(_degrade_mcp_server_row(row, reason) for row in insights.mcp.rows),
        precision=mcp_grade.precision,
        reason=mcp_grade.reason,
    )
    skills_grade = _cap_session_grade(Grade(insights.skills.precision, insights.skills.reason), reason)
    skills = replace(
        insights.skills,
        rows=tuple(
            replace(
                row,
                first_action_median_ns=_cap_session_metric(row.first_action_median_ns, reason),
                injected_tokens=_cap_session_metric(row.injected_tokens, reason),
            )
            for row in insights.skills.rows
        ),
        precision=skills_grade.precision,
        reason=skills_grade.reason,
    )
    context_carrying_grade = _cap_session_grade(
        Grade(insights.context_carrying_precision, insights.context_carrying_reason), reason
    )
    return replace(
        insights,
        tools=tools,
        mcp=mcp,
        skills=skills,
        context_carrying_precision=context_carrying_grade.precision,
        context_carrying_reason=context_carrying_grade.reason,
    )


def _degrade_mcp_server_row(row: McpServerRow, reason: str) -> McpServerRow:
    return replace(
        row,
        duration_share=_cap_session_metric(row.duration_share, reason),
        p50_ns=_cap_session_metric(row.p50_ns, reason),
        p95_ns=_cap_session_metric(row.p95_ns, reason),
        approval_blocking_share=_cap_session_metric(row.approval_blocking_share, reason),
        result_bytes=_cap_session_metric(row.result_bytes, reason),
        result_tokens=_cap_session_metric(row.result_tokens, reason),
        truncated_count=_cap_session_metric(row.truncated_count, reason),
        spill_count=_cap_session_metric(row.spill_count, reason),
        critical_path_exclusive_ns=_cap_session_metric(row.critical_path_exclusive_ns, reason),
        connection_wait_count=_cap_session_metric(row.connection_wait_count, reason),
        connection_wait_ns=_cap_session_metric(row.connection_wait_ns, reason),
        remotes=tuple(
            replace(
                remote,
                p50_ns=_cap_session_metric(remote.p50_ns, reason),
                p95_ns=_cap_session_metric(remote.p95_ns, reason),
            )
            for remote in row.remotes
        ),
    )


def _degrade_tool_usage_panel(panel: ToolUsagePanel, reason: str) -> ToolUsagePanel:
    grade = _cap_session_grade(Grade(panel.precision, panel.reason), reason)
    return replace(panel, precision=grade.precision, reason=grade.reason)
