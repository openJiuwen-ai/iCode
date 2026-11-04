# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Resolve one physical turn attempt into a ``TurnAnalysis``.

This module pairs lifecycles, checks coverage and runtime bounds, resolves the
turn tail, continuation polls and suspensions, computes token usage, and
assembles the attempt's metrics and their precision; counter-axis checks only
add diagnostics. The causal graph and time attribution live in
``_turn_graph``; context membership evidence lives in ``_context_evidence``.
Folding retry attempts into one logical turn is ``aggregation``'s job.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise
from threading import Event
from typing import Final, cast

from chrys.foundation.trajectory.event_types import TurnEndReason
from chrys.foundation.trajectory.sequences import SequenceRange
from chrys.service.analytics._context_evidence import _compaction_consumption_resolution, _RevisionResolution
from chrys.service.analytics._facts import (
    _START_FAMILIES,
    _UNREPORTED_FAILURE_USAGE_REASON,
    _active,
    _Endpoint,
    _ExchangeUsage,
    _Intermediate,
    _lifecycle_cut,
    _LifecycleCut,
    _Node,
    _payload_bool,
    _payload_int,
    _payload_str,
    _payload_value,
    _Segment,
    _Turn,
)
from chrys.service.analytics._timeline import (
    _endpoint_in_coverage_runtime,
    _hook_drain_scope,
    _infrastructure_branch_reaches,
    _materialize_timeline,
    _resolved_timeline_operation,
    _ResolvedNode,
    _timeline_hook_id,
    _timeline_identity,
    _timeline_parent_operation,
    _TimelineProjection,
)
from chrys.service.analytics._turn_graph import (
    _critical_paths,
    _failed_tool_cp_contributions,
    _project_turn,
    _required_causes_present,
    _server_tool_cp_contributions,
    _turn_flow,
    _wall_partition,
)
from chrys.service.analytics.math import Interval, interval_length
from chrys.service.analytics.model import (
    ContextSample,
    Metric,
    Precision,
    TimelineDiagnosticCode,
    TimelineOperation,
    TimeSlice,
    TokenUsage,
    TokenUsageSample,
    TurnAnalysis,
    TurnAttemptRef,
    UsageBucket,
    WallBucket,
)
from chrys.service.analytics.reader import raise_if_cancelled as _check_cancelled

_WEIGHTED_FAMILIES: Final = frozenset(_START_FAMILIES.values()) | {"approval", "compaction.phase"}
_OPTIONAL_USAGE_BUCKETS: Final = (UsageBucket.REASONING, UsageBucket.CACHE_READ, UsageBucket.CACHE_CREATION)


@dataclass(slots=True)
class _ResolutionCache:
    """Resolve-derived state retained for exactly one live fact index."""

    counter_axis_diagnostics_by_turn: dict[str, tuple[str, ...]] = field(default_factory=dict)


def _turn_token_usage(
    intermediate: _Intermediate,
    turn_id: str,
    inactive_ranges: tuple[SequenceRange, ...],
    verdict: Metric,
) -> TokenUsage:
    exchanges = [
        item
        for item in intermediate.usage_by_turn.get(turn_id, ())
        if _active(item.sequence, inactive_ranges) and not item.unreported_failure
    ]
    return _interned_token_usage(tuple(_turn_bucket_usage(exchanges, bucket, verdict) for bucket in UsageBucket))


def _turn_uses_session_carrier_fallback(
    intermediate: _Intermediate,
    turn_id: str,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    cancel_event: Event | None,
) -> bool:
    for node in intermediate.nodes_by_turn.get(turn_id, ()):
        _check_cancelled(cancel_event)
        if node.family != "tool.operation":
            continue
        for finish in node.finishes:
            if not _active(finish.sequence, inactive_ranges):
                continue
            if (
                _payload_str(finish.payload, "result_item_id") is not None
                and _payload_str(finish.payload, "result_carrier_item_id") is None
            ):
                return True
    return False


def _resolve_turn(
    intermediate: _Intermediate,
    turn: _Turn,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    resolution_cache: _ResolutionCache,
    revisions: _RevisionResolution,
    mapped_carrier: Callable[[str], str | None],
    rollback_projection_unresolved: bool,
    refresh_counter_axis: bool,
    cancel_event: Event | None,
) -> TurnAnalysis:
    _check_cancelled(cancel_event)
    diagnostics: list[str] = []
    starts = [event for event in turn.starts if _active(event.sequence, inactive_ranges)]
    finishes = [event for event in turn.finishes if _active(event.sequence, inactive_ranges)]
    if len(starts) != 1:
        return _empty_turn(
            intermediate,
            turn.turn_id,
            starts,
            "turn lifecycle is not uniquely opened",
            inactive_ranges=inactive_ranges,
            resolution_cache=resolution_cache,
            revisions=revisions,
            refresh=refresh_counter_axis,
            cancel_event=cancel_event,
        )
    start = starts[0]
    finish = finishes[0] if len(finishes) == 1 else None
    turn_lifecycle_exact = True
    if finish is None:
        diagnostics.append("turn lifecycle has no unique terminal")
        turn_lifecycle_exact = False
    elif finish.scope != start.scope or finish.sequence <= start.sequence or finish.monotonic_ns < start.monotonic_ns:
        diagnostics.append("turn interval has invalid monotonic endpoints")
        turn_lifecycle_exact = False
    tail = _turn_tail(intermediate, turn, start, finish, inactive_ranges, diagnostics)
    visible_end = tail.end_ns or (finish.monotonic_ns if finish is not None else start.monotonic_ns)
    turn_bounds = (start.monotonic_ns, max(start.monotonic_ns, visible_end))
    counter_axis_diagnostics = _counter_axis_diagnostics(
        intermediate,
        turn.turn_id,
        inactive_ranges,
        resolution_cache=resolution_cache,
        axis_start_ns=turn_bounds[0],
        axis_end_ns=turn_bounds[1],
        revisions=revisions,
        refresh=refresh_counter_axis,
        cancel_event=cancel_event,
    )
    integrity_diagnostics: list[str] = []
    coverage_exact = _turn_coverage_exact(
        intermediate,
        start,
        tail.end_sequence,
        inactive_ranges,
        integrity_diagnostics,
        rollback_projection_unresolved=rollback_projection_unresolved,
    )
    diagnostics.extend(integrity_diagnostics)
    resolved_nodes, lifecycle_exact = _resolved_turn_nodes(intermediate, start, inactive_ranges, diagnostics)
    closed_exchange_terminal_sequences = frozenset(
        node.finish.sequence for node in resolved_nodes if node.family == "model.exchange"
    )
    resolved_nodes = [node for node in resolved_nodes if not node.start.side_call]
    compaction_consumption = _compaction_consumption_resolution(
        intermediate,
        resolved_nodes,
        inactive_ranges,
        cancel_event=cancel_event,
        diagnostics=diagnostics,
    )
    _check_cancelled(cancel_event)
    resolved_nodes.extend(_continuation_poll_nodes(resolved_nodes))
    suspensions = _suspension_nodes(turn, resolved_nodes, start, finish, inactive_ranges, diagnostics)
    resolved_nodes.extend(suspensions.nodes)
    projection = _project_turn(
        resolved_nodes,
        turn_bounds,
        start=start,
        finish=finish,
        tail_end=tail.end_ns,
        waited_hook_ids=tail.waited_hook_ids,
        suspension_deductions=suspensions.deductions,
        revisions=revisions,
        compaction_consumption=compaction_consumption,
        mapped_carrier=mapped_carrier,
        cancel_event=cancel_event,
        diagnostics=diagnostics,
    )
    wall = _wall_partition(turn_bounds, projection.slices, cancel_event=cancel_event)
    idle_slices = tuple(
        TimeSlice(
            family="idle",
            slice_index=index,
            turn_id=turn.turn_id,
            runtime_id=start.runtime_id,
            operation_id=None,
            owner="idle(unattributed)",
            start_ns=idle_start,
            end_ns=idle_end,
            wall_bucket=WallBucket.IDLE,
            counts_as_work=False,
            compute_weight=False,
            response_weight=True,
        )
        for index, (idle_start, idle_end) in enumerate(wall.idle)
    )
    all_slices = tuple(
        sorted((*projection.slices, *idle_slices), key=lambda item: (item.start_ns, item.end_ns, item.slice_id))
    )
    work_slices = [item for item in all_slices if item.counts_as_work]
    exclusive_work = sum(item.duration_ns for item in work_slices)
    work_union = interval_length((item.start_ns, item.end_ns) for item in work_slices)
    overlap_gain = exclusive_work - work_union
    elapsed = turn_bounds[1] - turn_bounds[0]
    precision = (
        Precision.EXACT
        if coverage_exact
        and turn_lifecycle_exact
        and lifecycle_exact
        and tail.exact
        and projection.timeline_exact
        and suspensions.exact
        else Precision.UNRESOLVED
    )
    causes_exact = _required_causes_present(resolved_nodes, diagnostics)
    compute_cp_exact = (
        coverage_exact
        and turn_lifecycle_exact
        and lifecycle_exact
        and projection.timeline_exact
        and projection.dag_exact
        and causes_exact
    )
    response_cp_exact = compute_cp_exact and tail.exact and suspensions.exact and projection.response_dependency_exact
    critical_paths = _critical_paths(
        resolved_nodes,
        all_slices,
        projection.dependency,
        cancel_event=cancel_event,
    )
    if not critical_paths.compute_bounded:
        diagnostics.append("compute critical-path candidate cap exceeded")
        compute_cp_exact = False
    if not critical_paths.response_bounded:
        diagnostics.append("response critical-path candidate cap exceeded")
        response_cp_exact = False
    if not critical_paths.acyclic:
        diagnostics.append("operation dependency graph contains a cycle")
        compute_cp_exact = False
        response_cp_exact = False
    if not critical_paths.response_reachable:
        diagnostics.append("terminal response is not reachable from the turn root through typed edges")
        response_cp_exact = False
    critical_tool_contributions = (
        _failed_tool_cp_contributions(
            resolved_nodes,
            all_slices,
            projection.dependency,
            response_cp=critical_paths.response_ns,
            cancel_event=cancel_event,
        )
        if response_cp_exact and critical_paths.response_ns is not None
        else {}
    )
    server_critical_contributions = (
        _server_tool_cp_contributions(
            intermediate,
            resolved_nodes,
            all_slices,
            projection.dependency,
            response_cp=critical_paths.response_ns,
            cancel_event=cancel_event,
        )
        if response_cp_exact and critical_paths.response_ns is not None
        else {}
    )
    metric_reason = None if precision is Precision.EXACT else "; ".join(dict.fromkeys(diagnostics))
    unresolved_reason = "; ".join(dict.fromkeys(diagnostics))
    compute_cp_reason = None if compute_cp_exact else unresolved_reason
    response_cp_reason = None if response_cp_exact else unresolved_reason
    wall_metrics = {
        bucket: Metric(value=value, precision=precision, reason=metric_reason)
        for bucket, value in wall.durations.items()
    }
    utilization = {
        bucket: Metric(
            value=(
                sum(item.duration_ns for item in work_slices if item.wall_bucket is bucket) / elapsed
                if elapsed
                else 0.0
            ),
            precision=precision,
            reason=metric_reason,
        )
        for bucket in (WallBucket.MODEL, WallBucket.TOOLS)
    }
    usage = _turn_usage(
        intermediate,
        turn.turn_id,
        inactive_ranges,
        integrity_exact=coverage_exact,
        integrity_reason="; ".join(dict.fromkeys(integrity_diagnostics)),
        turn_lifecycle_exact=turn_lifecycle_exact,
        closed_exchange_terminal_sequences=closed_exchange_terminal_sequences,
        cancel_event=cancel_event,
    )
    token_usage = _turn_token_usage(intermediate, turn.turn_id, inactive_ranges, usage)
    turn_number = _payload_int(start.payload, "turn_number")
    action_projection_diagnostics: list[str] = []
    action_projection_exact = (
        coverage_exact
        and turn_lifecycle_exact
        and _tool_action_projection_exact(
            intermediate,
            start,
            finish.sequence if finish is not None else None,
            inactive_ranges,
            action_projection_diagnostics,
        )
    )
    action_projection_reason = "; ".join(dict.fromkeys((*integrity_diagnostics, *action_projection_diagnostics)))
    operations = _timeline_operations(
        intermediate,
        turn_id=turn.turn_id,
        inactive_ranges=inactive_ranges,
        resolved_nodes=resolved_nodes,
        cancel_event=cancel_event,
    )
    flow = _turn_flow(turn.turn_id, projection.dependency, operations, acyclic=critical_paths.acyclic)
    is_retry = _payload_bool(start.payload, "is_retry")
    return TurnAnalysis(
        turn_id=turn.turn_id,
        turn_number=turn_number,
        runtime_id=start.runtime_id,
        start_sequence=start.sequence,
        end_sequence=tail.end_sequence,
        elapsed_ns=Metric(elapsed, precision, metric_reason),
        compute_cp_ns=Metric(
            critical_paths.compute_ns if compute_cp_exact else None,
            Precision.EXACT if compute_cp_exact else Precision.UNRESOLVED,
            compute_cp_reason,
        ),
        response_cp_ns=Metric(
            critical_paths.response_ns if response_cp_exact else None,
            Precision.EXACT if response_cp_exact else Precision.UNRESOLVED,
            response_cp_reason,
        ),
        exclusive_work_ns=Metric(exclusive_work, precision, metric_reason),
        parallelism=Metric(exclusive_work / elapsed if elapsed else 0.0, precision, metric_reason),
        overlap_gain_ns=Metric(overlap_gain, precision, metric_reason),
        wall_time_ns=wall_metrics,
        utilization=utilization,
        usage_tokens=usage,
        attempts=(
            TurnAttemptRef(
                turn_id=turn.turn_id,
                runtime_id=start.runtime_id,
                is_retry=is_retry,
                physical_axis_start_ns=turn_bounds[0],
                physical_axis_end_ns=turn_bounds[1],
                logical_axis_start_ns=turn_bounds[0],
                operation_start_index=0,
                operation_end_index=len(operations),
                slice_start_index=0,
                slice_end_index=len(all_slices),
            ),
        ),
        axis_start_ns=turn_bounds[0],
        axis_end_ns=turn_bounds[1],
        operations=operations,
        slices=all_slices,
        diagnostics=tuple(dict.fromkeys((*diagnostics, *counter_axis_diagnostics))),
        critical_tool_contributions_ns=critical_tool_contributions,
        server_critical_contributions_ns=server_critical_contributions,
        action_projection_precision=Precision.EXACT if action_projection_exact else Precision.UNRESOLVED,
        action_projection_reason=None if action_projection_exact else action_projection_reason,
        token_usage=token_usage,
        flow=flow,
    )


@dataclass(frozen=True, slots=True)
class _TurnTail:
    """Where a turn's visible span ends: its response fence, or its terminal event without one."""

    end_ns: int | None
    exact: bool
    """Whether the end is proven: a cancelled or process-exit turn's terminal event, or a valid
    fence with every hook it waited on accounted for."""
    end_sequence: int | None
    waited_hook_ids: frozenset[str] | None
    """The hooks the response waited on; None when they cannot be known."""


def _turn_tail(
    intermediate: _Intermediate,
    turn: _Turn,
    start: _Endpoint,
    finish: _Endpoint | None,
    inactive_ranges: tuple[SequenceRange, ...],
    diagnostics: list[str],
) -> _TurnTail:
    if finish is None:
        return _TurnTail(None, False, None, None)
    if finish.scope != start.scope or finish.sequence <= start.sequence or finish.monotonic_ns < start.monotonic_ns:
        return _TurnTail(finish.monotonic_ns, False, finish.sequence, None)
    end_reason = _payload_str(finish.payload, "end_reason")
    if end_reason in {TurnEndReason.PROCESS_EXIT, TurnEndReason.CANCELLED}:
        return _TurnTail(finish.monotonic_ns, True, finish.sequence, frozenset())
    markers = [marker for marker in turn.response_markers if _active(marker.sequence, inactive_ranges)]
    if len(markers) != 1:
        diagnostics.append("turn response fence is missing or duplicated")
        return _TurnTail(finish.monotonic_ns, False, finish.sequence, None)
    marker = markers[0]
    segment_end_sequence = max(
        (
            segment.sequence
            for segment in intermediate.segments.get(marker.event_id or "", ())
            if _active(segment.sequence, inactive_ranges)
        ),
        default=marker.sequence,
    )
    if marker.scope != start.scope or marker.sequence <= finish.sequence or marker.monotonic_ns < finish.monotonic_ns:
        diagnostics.append("turn response fence is outside the turn runtime or precedes turn.finished")
        return _TurnTail(finish.monotonic_ns, False, segment_end_sequence, None)
    waited_ids = _reassemble_waited_hook_ids(intermediate, marker, inactive_ranges)
    expected = _payload_int(marker.payload, "waited_hook_operation_count")
    if waited_ids is None or expected is None or len(waited_ids) != expected or len(set(waited_ids)) != expected:
        diagnostics.append("turn response fence hook membership is incomplete")
        return _TurnTail(marker.monotonic_ns, False, segment_end_sequence, None)
    drained_scopes = _payload_value(marker.payload, "drained_scopes")
    outcome = _payload_str(marker.payload, "outcome")
    if (
        not isinstance(drained_scopes, tuple)
        or not all(isinstance(scope, str) for scope in drained_scopes)
        or len(drained_scopes) != len(set(drained_scopes))
        or any(scope != "turn" for scope in drained_scopes)
        or outcome not in {"settled", "cancelled", "partial"}
        or (outcome == "settled" and drained_scopes not in {(), ("turn",)})
        or (waited_ids and "turn" not in drained_scopes)
    ):
        diagnostics.append("turn response fence drained_scopes are inconsistent with its outcome or membership")
        return _TurnTail(marker.monotonic_ns, False, segment_end_sequence, None)
    hook_nodes = {
        node.operation_id: node
        for node in intermediate.nodes_by_turn.get(turn.turn_id, ())
        if node.family == "hook.operation"
    }
    for operation_id in waited_ids:
        node = hook_nodes.get(operation_id)
        starts = [] if node is None else [event for event in node.starts if _active(event.sequence, inactive_ranges)]
        terminals = (
            [] if node is None else [event for event in node.finishes if _active(event.sequence, inactive_ranges)]
        )
        if (
            len(starts) != 1
            or len(terminals) != 1
            or _payload_str(starts[0].payload, "execution_mode") != "async"
            or _hook_drain_scope(starts[0]) != "turn"
            or starts[0].scope != start.scope
            or terminals[0].scope != start.scope
            or starts[0].sequence <= start.sequence
            or terminals[0].sequence <= starts[0].sequence
            or terminals[0].sequence >= marker.sequence
            or terminals[0].monotonic_ns < starts[0].monotonic_ns
            or not _endpoint_in_coverage_runtime(intermediate, starts[0], inactive_ranges)
            or not _endpoint_in_coverage_runtime(intermediate, terminals[0], inactive_ranges)
        ):
            diagnostics.append("a waited hook is not a uniquely closed async turn-scope hook before the response fence")
            return _TurnTail(marker.monotonic_ns, False, segment_end_sequence, None)
    return _TurnTail(marker.monotonic_ns, True, segment_end_sequence, frozenset(waited_ids))


def _reassemble_waited_hook_ids(
    intermediate: _Intermediate,
    marker: _Endpoint,
    inactive_ranges: tuple[SequenceRange, ...],
) -> list[str] | None:
    declarations = [
        declaration
        for declaration in marker.segmented_fields
        if declaration.field_pointer == "/payload/waited_hook_operation_ids"
    ]
    if len(declarations) != 1:
        return None
    declaration = declarations[0]
    segments = [
        segment
        for segment in intermediate.segments.get(marker.event_id, [])
        if _active(segment.sequence, inactive_ranges) and segment.field_pointer == declaration.field_pointer
    ]
    if len(segments) != declaration.segment_count:
        return None
    indexed_segments: list[tuple[int, _Segment]] = []
    for segment in segments:
        segment_index = segment.segment_index
        if segment_index is None:
            return None
        indexed_segments.append((segment_index, segment))
    indices = [segment_index for segment_index, _ in indexed_segments]
    if sorted(indices) != list(range(declaration.segment_count)):
        return None
    values: list[str] = []
    for _, segment in sorted(indexed_segments):
        if (
            segment.segment_count != declaration.segment_count
            or segment.segment_group_id != declaration.segment_group_id
            or segment.encoding != "array_slice"
            or segment.entry_oversized
            or segment.runtime_id != marker.runtime_id
            or segment.branch_id != marker.branch_id
            or segment.coverage_id != marker.coverage_id
            or segment.turn_id != marker.turn_id
            or segment.sequence <= marker.sequence
        ):
            return None
        entries = segment.entries
        if entries is None or not all(isinstance(item, str) for item in entries):
            return None
        values.extend(item for item in entries if isinstance(item, str))
    return values


def _turn_coverage_exact(
    intermediate: _Intermediate,
    start: _Endpoint,
    end_sequence: int | None,
    inactive_ranges: tuple[SequenceRange, ...],
    diagnostics: list[str],
    *,
    rollback_projection_unresolved: bool,
) -> bool:
    if end_sequence is None:
        return False
    exact = True
    coverage_starts = [
        event
        for event in intermediate.coverage_starts.get(start.coverage_id, [])
        if event.sequence <= start.sequence
        and event.runtime_id == start.runtime_id
        and _infrastructure_branch_reaches(intermediate, event, start, inactive_ranges)
    ]
    if len(coverage_starts) != 1:
        diagnostics.append("turn is not covered by trajectory.coverage.started")
        exact = False
    coverage_ends = [
        event
        for event in intermediate.coverage_ends.get(start.coverage_id, ())
        if event.runtime_id == start.runtime_id
        and _infrastructure_branch_reaches(intermediate, start, event, inactive_ranges)
    ]
    if len(coverage_ends) > 1:
        diagnostics.append("trajectory coverage has duplicate terminal markers")
        exact = False
    elif coverage_ends:
        coverage_end = coverage_ends[0]
        last_sequence = _payload_int(coverage_end.payload, "last_sequence")
        if last_sequence != coverage_end.sequence - 1:
            diagnostics.append("trajectory coverage terminal has an invalid last_sequence")
            exact = False
        elif start.sequence > last_sequence or end_sequence > last_sequence:
            diagnostics.append("turn falls outside its trajectory coverage window")
            exact = False
    runtime_starts = [
        event
        for event in intermediate.runtime_starts.get(start.runtime_id, ())
        if _infrastructure_branch_reaches(intermediate, event, start, inactive_ranges)
    ]
    if len(runtime_starts) > 1:
        diagnostics.append("trajectory runtime has duplicate start endpoints")
        exact = False
    elif runtime_starts and runtime_starts[0].sequence >= start.sequence:
        diagnostics.append("turn begins before its trajectory runtime endpoint")
        exact = False
    runtime_recoveries = [
        event
        for event in intermediate.runtime_recoveries.get(start.runtime_id, ())
        if _infrastructure_branch_reaches(intermediate, event, start, inactive_ranges)
    ]
    if len(runtime_recoveries) > 1:
        diagnostics.append("trajectory runtime has duplicate recovery endpoints")
        exact = False
    elif runtime_recoveries and runtime_recoveries[0].sequence >= start.sequence:
        diagnostics.append("turn begins before its trajectory runtime recovery endpoint")
        exact = False
    runtime_finishes = [
        event
        for event in intermediate.runtime_finishes.get(start.runtime_id, ())
        if _infrastructure_branch_reaches(intermediate, start, event, inactive_ranges)
    ]
    if len(runtime_finishes) > 1:
        diagnostics.append("trajectory runtime has duplicate terminal markers")
        exact = False
    elif runtime_finishes:
        runtime_finish = runtime_finishes[0]
        if start.sequence >= runtime_finish.sequence or end_sequence >= runtime_finish.sequence:
            diagnostics.append("turn occurs after trajectory.runtime.finished")
            exact = False
        matching_coverage_end = coverage_ends[0] if len(coverage_ends) == 1 else None
        if matching_coverage_end is None or matching_coverage_end.sequence >= runtime_finish.sequence:
            diagnostics.append("trajectory runtime closure lacks an ordered coverage terminal")
            exact = False
    for first, last in intermediate.explicit_gaps:
        if first <= end_sequence and last >= start.sequence:
            diagnostics.append("trajectory gap intersects the turn")
            exact = False
            break
    if any(start.sequence <= sequence <= end_sequence for sequence in intermediate.unsupported_sequences):
        diagnostics.append("unsupported trajectory event intersects the turn")
        exact = False
    if any(start.sequence <= sequence + 1 <= end_sequence for sequence in intermediate.corrupt_after_sequences):
        diagnostics.append("corrupt trajectory line intersects the turn")
        exact = False
    if any(violation.first_sequence <= end_sequence for violation in intermediate.prefix_violations):
        diagnostics.append("trajectory accounted-prefix invariant failed")
        exact = False
    if rollback_projection_unresolved:
        diagnostics.append("rollback live-history range is unresolved")
        exact = False
    return exact


def _resolved_turn_nodes(
    intermediate: _Intermediate,
    turn_start: _Endpoint,
    inactive_ranges: tuple[SequenceRange, ...],
    diagnostics: list[str],
) -> tuple[list[_ResolvedNode], bool]:
    resolved: list[_ResolvedNode] = []
    exact = True
    turn_id = turn_start.turn_id or ""
    for node in intermediate.nodes_by_turn.get(turn_id, ()):
        starts = [
            event for event in node.starts if event.turn_id == turn_id and _active(event.sequence, inactive_ranges)
        ]
        finishes = [
            event for event in node.finishes if event.turn_id == turn_id and _active(event.sequence, inactive_ranges)
        ]
        if not starts and not finishes:
            continue
        endpoints = (*starts, *finishes)
        if endpoints and all(event.side_call for event in endpoints):
            # Side-call lifecycles stay out of the turn's arithmetic; the
            # closed ones are kept only so their exchange terminals can vouch
            # for the usage they contributed, and are dropped right after.
            side_call_node = _resolved_side_call_node(intermediate, node, turn_start, starts, finishes, inactive_ranges)
            if side_call_node is not None:
                resolved.append(side_call_node)
            continue
        if any(event.side_call for event in endpoints):
            diagnostics.append(f"{node.family} lifecycle mixes main and non-main actors")
            exact = False
            continue
        cut = _lifecycle_cut(node, inactive_ranges)
        if cut is not _LifecycleCut.NONE:
            diagnostics.append(_lifecycle_cut_reason(node.family, cut))
            exact = False
            continue
        if node.family == "retry" and len(starts) == 1 and not finishes:
            # A lone scheduled marker is a cancelled backoff, not an open span.
            continue
        if len(starts) != 1 or len(finishes) != 1:
            diagnostics.append(f"{node.family} lifecycle is not uniquely closed")
            exact = False
            continue
        start, finish = starts[0], finishes[0]
        if (
            start.runtime_id != turn_start.runtime_id
            or finish.runtime_id != turn_start.runtime_id
            or start.branch_id != turn_start.branch_id
            or finish.branch_id != turn_start.branch_id
            or start.coverage_id != turn_start.coverage_id
            or finish.coverage_id != turn_start.coverage_id
            or start.sequence <= turn_start.sequence
            or finish.sequence <= turn_start.sequence
        ):
            diagnostics.append(f"{node.family} lifecycle does not belong to the owning turn runtime and branch")
            exact = False
            continue
        if (
            start.scope != finish.scope
            or (node.family != "compaction.phase" and finish.sequence <= start.sequence)
            or finish.monotonic_ns < start.monotonic_ns
        ):
            diagnostics.append(f"{node.family} interval has invalid monotonic endpoints")
            exact = False
            continue
        if not _endpoint_in_coverage_runtime(intermediate, start, inactive_ranges) or not _endpoint_in_coverage_runtime(
            intermediate, finish, inactive_ranges
        ):
            diagnostics.append(f"{node.family} lifecycle falls outside coverage or after runtime closure")
            exact = False
            continue
        if node.family in _WEIGHTED_FAMILIES and not finish.monotonic_measurement:
            diagnostics.append(f"{node.family} duration lacks monotonic provenance")
            exact = False
        resolved.append(
            _ResolvedNode(
                node_id=f"{node.family}:{node.operation_id}",
                family=node.family,
                operation_id=node.operation_id,
                start=start,
                finish=finish,
            )
        )
    return resolved, exact


def _resolved_side_call_node(
    intermediate: _Intermediate,
    node: _Node,
    turn_start: _Endpoint,
    starts: list[_Endpoint],
    finishes: list[_Endpoint],
    inactive_ranges: tuple[SequenceRange, ...],
) -> _ResolvedNode | None:
    """Validate a side-call lifecycle in its own actor domain without entering main-turn metrics."""
    if len(starts) != 1 or len(finishes) != 1:
        return None
    start, finish = starts[0], finishes[0]
    if (
        start.scope != finish.scope
        or start.runtime_id != turn_start.runtime_id
        or start.branch_id != turn_start.branch_id
        or start.coverage_id != turn_start.coverage_id
        or start.sequence <= turn_start.sequence
        or finish.sequence <= start.sequence
        or finish.monotonic_ns < start.monotonic_ns
        or not _endpoint_in_coverage_runtime(intermediate, start, inactive_ranges)
        or not _endpoint_in_coverage_runtime(intermediate, finish, inactive_ranges)
    ):
        return None
    return _ResolvedNode(
        node_id=f"{node.family}:{node.operation_id}",
        family=node.family,
        operation_id=node.operation_id,
        start=start,
        finish=finish,
    )


def _tool_action_projection_exact(
    intermediate: _Intermediate,
    turn_start: _Endpoint,
    terminal_sequence: int | None,
    inactive_ranges: tuple[SequenceRange, ...],
    diagnostics: list[str],
) -> bool:
    exact = True
    turn_id = turn_start.turn_id or ""
    for node in intermediate.nodes_by_turn.get(turn_id, ()):
        if node.family != "tool.operation":
            continue
        starts = [event for event in node.starts if _active(event.sequence, inactive_ranges)]
        finishes = [event for event in node.finishes if _active(event.sequence, inactive_ranges)]
        if not starts and not finishes:
            continue
        endpoints = (*starts, *finishes)
        if endpoints and all(event.side_call for event in endpoints):
            continue
        if any(event.side_call for event in endpoints):
            diagnostics.append("tool action projection mixes main and non-main actors")
            exact = False
            continue
        cut = _lifecycle_cut(node, inactive_ranges)
        if cut is not _LifecycleCut.NONE:
            diagnostics.append(_lifecycle_cut_reason("tool action", cut))
            exact = False
            continue
        if len(starts) != 1:
            diagnostics.append("tool action projection lacks a unique start event")
            exact = False
            continue
        start = starts[0]
        if (
            start.runtime_id != turn_start.runtime_id
            or start.branch_id != turn_start.branch_id
            or start.coverage_id != turn_start.coverage_id
            or start.turn_id != turn_start.turn_id
            or start.sequence <= turn_start.sequence
        ):
            diagnostics.append("tool action start does not belong to the owning turn scope")
            exact = False
            continue
        # The scope check above bounds the start from below; tool activity
        # past the turn terminal is just as unprovable as activity before the
        # turn opened. The bound is ``turn.finished`` itself, not the tail's
        # response fence — the fence settles after the turn closes and no
        # tool may run in between. Terminals count too: an outcome read from
        # a finish beyond the turn is no better founded than a stray start.
        # An open turn has no terminal yet, and is already inexact through
        # its lifecycle.
        if terminal_sequence is not None and any(event.sequence > terminal_sequence for event in (start, *finishes)):
            diagnostics.append("tool action endpoint lies beyond the turn terminal")
            exact = False
    return exact


def _timeline_operations(
    intermediate: _Intermediate,
    *,
    turn_id: str,
    inactive_ranges: tuple[SequenceRange, ...],
    resolved_nodes: list[_ResolvedNode],
    cancel_event: Event | None,
) -> tuple[TimelineOperation, ...]:
    """Project lifecycle nodes for display without deriving metric values from them."""
    rows = [_resolved_timeline_operation(node) for node in resolved_nodes]
    resolved_ids = {node.node_id for node in resolved_nodes}
    for node in intermediate.nodes_by_turn.get(turn_id, ()):
        _check_cancelled(cancel_event)
        node_id = f"{node.family}:{node.operation_id}"
        if node_id in resolved_ids:
            continue
        starts = [
            event for event in node.starts if event.turn_id == turn_id and _active(event.sequence, inactive_ranges)
        ]
        finishes = [
            event for event in node.finishes if event.turn_id == turn_id and _active(event.sequence, inactive_ranges)
        ]
        endpoints = sorted((*starts, *finishes), key=lambda endpoint: endpoint.sequence)
        if not endpoints:
            continue
        if all(endpoint.side_call for endpoint in endpoints):
            continue
        cut = _lifecycle_cut(node, inactive_ranges)
        identity_endpoint = starts[0] if starts else endpoints[0]
        identity_source = (
            node.starts[0] if cut is _LifecycleCut.FINISH_SURVIVES and len(node.starts) == 1 else identity_endpoint
        )
        if cut is not _LifecycleCut.NONE:
            reason = _lifecycle_cut_reason(node.family, cut)
            diagnostic_code = _lifecycle_cut_diagnostic_code(cut)
        else:
            reason, diagnostic_code = _unresolved_operation_reason(node.family, starts, finishes)
        rows.append(
            _TimelineProjection(
                operation_id=node.operation_id,
                parent_operation_id=_timeline_parent_operation(node.family, identity_endpoint),
                start_sequence=endpoints[0].sequence,
                family=node.family,
                start_ns=None,
                end_ns=None,
                precision=Precision.UNRESOLVED,
                reason=reason,
                diagnostic_code=diagnostic_code,
                identity=_timeline_identity(node.family, identity_source),
                hook_id=_timeline_hook_id(node.family, identity_source),
            )
        )
    return _materialize_timeline(rows)


def _unresolved_operation_reason(
    family: str,
    starts: list[_Endpoint],
    finishes: list[_Endpoint],
) -> tuple[str, TimelineDiagnosticCode]:
    if not starts:
        return f"{family} lifecycle has no start endpoint", TimelineDiagnosticCode.MISSING_START
    if not finishes:
        return f"{family} lifecycle has no terminal endpoint", TimelineDiagnosticCode.MISSING_TERMINAL
    if len(starts) != 1 or len(finishes) != 1:
        return f"{family} lifecycle is not uniquely closed", TimelineDiagnosticCode.NONUNIQUE_LIFECYCLE
    if finishes[0].sequence <= starts[0].sequence or finishes[0].monotonic_ns < starts[0].monotonic_ns:
        return f"{family} interval has invalid monotonic endpoints", TimelineDiagnosticCode.INVALID_ENDPOINTS
    return f"{family} lifecycle falls outside the owning turn coverage", TimelineDiagnosticCode.OUTSIDE_TURN_COVERAGE


def _lifecycle_cut_reason(family: str, cut: _LifecycleCut) -> str:
    """Describe which endpoint of a raw lifecycle remains active."""
    surviving = "start" if cut is _LifecycleCut.START_SURVIVES else "terminal"
    return f"{family} lifecycle crosses rollback projection; only its {surviving} endpoint remains active"


def _lifecycle_cut_diagnostic_code(cut: _LifecycleCut) -> TimelineDiagnosticCode:
    return (
        TimelineDiagnosticCode.ROLLBACK_START_SURVIVES
        if cut is _LifecycleCut.START_SURVIVES
        else TimelineDiagnosticCode.ROLLBACK_TERMINAL_SURVIVES
    )


def _continuation_poll_nodes(nodes: list[_ResolvedNode]) -> list[_ResolvedNode]:
    """Derive the uninstrumented fixed continuation-poll pauses."""
    exchanges = [node for node in nodes if node.family == "model.exchange"]
    retries = [node for node in nodes if node.family == "retry"]
    by_cycle: dict[str, list[_ResolvedNode]] = defaultdict(list)
    for exchange in exchanges:
        parent = exchange.start.parent_operation_id
        if parent is not None:
            by_cycle[parent].append(exchange)
    derived: list[_ResolvedNode] = []
    for cycle_operation_id, cycle_exchanges in by_cycle.items():
        ordered = sorted(cycle_exchanges, key=lambda node: (node.start.monotonic_ns, node.start.sequence))
        for previous, current in pairwise(ordered):
            if (
                _payload_str(previous.finish.payload, "outcome") != "success"
                or _payload_str(current.start.payload, "continuation_mode") != "poll"
                or previous.finish.monotonic_ns >= current.start.monotonic_ns
            ):
                continue
            if any(
                retry.start.monotonic_ns < current.start.monotonic_ns
                and retry.finish.monotonic_ns > previous.finish.monotonic_ns
                for retry in retries
            ):
                continue
            operation_id = f"continuation_poll:{previous.operation_id}:{current.operation_id}"
            start = _synthetic_endpoint(
                previous.finish,
                monotonic_ns=previous.finish.monotonic_ns,
                parent_operation_id=cycle_operation_id,
                payload={
                    "category": "continuation_poll",
                    "previous_exchange_operation_id": previous.operation_id,
                    "next_exchange_operation_id": current.operation_id,
                },
            )
            finish = _synthetic_endpoint(
                current.start,
                monotonic_ns=current.start.monotonic_ns,
                parent_operation_id=cycle_operation_id,
                payload={
                    "category": "continuation_poll",
                    "outcome": "completed",
                    "previous_exchange_operation_id": previous.operation_id,
                    "next_exchange_operation_id": current.operation_id,
                },
            )
            derived.append(
                _ResolvedNode(
                    node_id=f"continuation.poll:{operation_id}",
                    family="continuation.poll",
                    operation_id=operation_id,
                    start=start,
                    finish=finish,
                )
            )
    return derived


@dataclass(frozen=True, slots=True)
class _Suspensions:
    """A turn's paired suspensions and the time each deducts from the sub-agent call it paused."""

    nodes: list[_ResolvedNode]
    deductions: dict[str, list[Interval]]
    """Suspended intervals by the node id of the sub-agent call they fall in."""
    exact: bool


def _suspension_nodes(
    turn: _Turn,
    nodes: list[_ResolvedNode],
    turn_start: _Endpoint,
    finish: _Endpoint | None,
    inactive_ranges: tuple[SequenceRange, ...],
    diagnostics: list[str],
) -> _Suspensions:
    """Pair turn suspension markers and identify the sole deductible boundary."""
    suspended = sorted(
        (event for event in turn.suspended if _active(event.sequence, inactive_ranges)),
        key=lambda event: event.sequence,
    )
    resumed = sorted(
        (event for event in turn.resumed if _active(event.sequence, inactive_ranges)),
        key=lambda event: event.sequence,
    )
    resume_index = 0
    exact = True
    derived: list[_ResolvedNode] = []
    deductions: dict[str, list[Interval]] = defaultdict(list)
    boundaries = [node for node in nodes if node.family == "sub_agent"]
    for index, start in enumerate(suspended):
        while resume_index < len(resumed) and resumed[resume_index].sequence <= start.sequence:
            diagnostics.append("turn.resumed has no preceding suspension")
            exact = False
            resume_index += 1
        if resume_index < len(resumed):
            terminal = resumed[resume_index]
            resume_index += 1
        elif finish is not None:
            terminal = finish
            diagnostics.append("turn suspension reaches the turn terminal without resume")
            exact = False
        else:
            diagnostics.append("turn suspension has no observable terminal")
            exact = False
            continue
        if (
            start.runtime_id != turn_start.runtime_id
            or terminal.runtime_id != turn_start.runtime_id
            or start.branch_id != turn_start.branch_id
            or terminal.branch_id != turn_start.branch_id
            or terminal.sequence <= start.sequence
            or terminal.monotonic_ns < start.monotonic_ns
        ):
            diagnostics.append("turn suspension has invalid monotonic endpoints")
            exact = False
            continue
        operation_id = f"turn_suspension:{turn.turn_id}:{index}"
        derived.append(
            _ResolvedNode(
                node_id=f"turn.suspension:{operation_id}",
                family="turn.suspension",
                operation_id=operation_id,
                start=_synthetic_endpoint(
                    start,
                    monotonic_ns=start.monotonic_ns,
                    parent_operation_id=None,
                    payload={"category": "sub_agent_suspension"},
                ),
                finish=_synthetic_endpoint(
                    terminal,
                    monotonic_ns=terminal.monotonic_ns,
                    parent_operation_id=None,
                    payload={"category": "sub_agent_suspension", "outcome": "completed"},
                ),
            )
        )
        candidates = [
            boundary
            for boundary in boundaries
            if boundary.start.monotonic_ns <= start.monotonic_ns
            and boundary.finish.monotonic_ns >= terminal.monotonic_ns
        ]
        if len(candidates) == 1:
            deductions[candidates[0].node_id].append((start.monotonic_ns, terminal.monotonic_ns))
        elif len(candidates) > 1:
            diagnostics.append("turn suspension overlaps multiple open sub-agent boundaries")
            exact = False
    if resume_index < len(resumed):
        diagnostics.append("turn.resumed has no preceding suspension")
        exact = False
    return _Suspensions(derived, deductions, exact)


def _synthetic_endpoint(
    source: _Endpoint,
    *,
    monotonic_ns: int,
    parent_operation_id: str | None,
    payload: dict[str, object],
) -> _Endpoint:
    return _Endpoint(
        event_id=source.event_id,
        sequence=source.sequence,
        scope=source.scope,
        monotonic_ns=monotonic_ns,
        parent_operation_id=parent_operation_id,
        side_call=source.side_call,
        payload=tuple(item for pair in payload.items() for item in pair),
        links=(),
        segmented_fields=(),
        monotonic_measurement=True,
    )


def _usage_samples_by_turn(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
) -> dict[str, tuple[TokenUsageSample, ...]]:
    """Project timestamped per-exchange usage counters on demand for export."""
    finish_ns = {
        endpoint.sequence: endpoint.monotonic_ns
        for node in intermediate.nodes.get("model.exchange", {}).values()
        for endpoint in node.finishes
    }
    samples: dict[str, tuple[TokenUsageSample, ...]] = {}
    for turn_id, items in intermediate.usage_by_turn.items():
        rows = tuple(
            TokenUsageSample(
                sequence=item.sequence,
                end_ns=finish_ns.get(item.sequence),
                input_tokens=item.input_total,
                output_tokens=item.output_total,
                reasoning_tokens=item.extras.reasoning if item.extras is not None else None,
                cache_read_tokens=item.extras.cache_read if item.extras is not None else None,
                cache_creation_tokens=item.extras.cache_creation if item.extras is not None else None,
            )
            for item in items
            if _active(item.sequence, inactive_ranges)
            and (item.input_total is not None or item.output_total is not None or item.extras is not None)
        )
        if rows:
            samples[turn_id] = rows
    return samples


def _context_samples_by_turn(
    intermediate: _Intermediate,
    inactive_ranges: tuple[SequenceRange, ...],
) -> dict[str, tuple[ContextSample, ...]]:
    """Project timestamped context-size counters on demand for export.

    The declared item_count is read straight off each uniquely defined revision
    endpoint, so no membership replay is needed; declaration inconsistencies
    already surface through the diagnostics pipeline.
    """
    by_turn: dict[str, list[ContextSample]] = defaultdict(list)
    for candidates in intermediate.context_revisions.values():
        active = [endpoint for endpoint in candidates if _active(endpoint.sequence, inactive_ranges)]
        if len(active) != 1:
            continue
        endpoint = active[0]
        item_count = _payload_int(endpoint.payload, "item_count")
        if endpoint.turn_id is None or item_count is None or item_count < 0 or endpoint.side_call:
            continue
        by_turn[endpoint.turn_id].append(
            ContextSample(
                sequence=endpoint.sequence,
                ns=endpoint.monotonic_ns,
                item_count=item_count,
            )
        )
    return {
        turn_id: tuple(sorted(rows, key=lambda sample: sample.sequence)) for turn_id, rows in sorted(by_turn.items())
    }


def _turn_usage(
    intermediate: _Intermediate,
    turn_id: str,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    integrity_exact: bool,
    integrity_reason: str,
    turn_lifecycle_exact: bool,
    closed_exchange_terminal_sequences: frozenset[int],
    cancel_event: Event | None,
) -> Metric:
    unclosed_start = False
    exchange_projection_exact = True
    for node in intermediate.nodes_by_turn.get(turn_id, ()):
        _check_cancelled(cancel_event)
        if node.family != "model.exchange":
            continue
        starts = [
            event for event in node.starts if event.turn_id == turn_id and _active(event.sequence, inactive_ranges)
        ]
        finishes = [
            event for event in node.finishes if event.turn_id == turn_id and _active(event.sequence, inactive_ranges)
        ]
        endpoints = (*starts, *finishes)
        if not endpoints:
            continue
        if _lifecycle_cut(node, inactive_ranges) is not _LifecycleCut.NONE:
            exchange_projection_exact = False
            continue
        if len(starts) == 1 and not finishes:
            unclosed_start = True
            continue
        if len(starts) != 1 or len(finishes) != 1 or finishes[0].sequence not in closed_exchange_terminal_sequences:
            exchange_projection_exact = False
    if unclosed_start:
        return Metric(None, Precision.MISSING, "one or more model exchanges have no terminal usage")
    active = [item for item in intermediate.usage_by_turn.get(turn_id, ()) if _active(item.sequence, inactive_ranges)]
    exchanges = [item for item in active if not item.unreported_failure]
    if any(
        item.normalization_unavailable or item.input_total is None or item.output_total is None for item in exchanges
    ):
        return Metric(None, Precision.MISSING, "one or more normalized usage totals are missing")
    value = sum(cast("int", item.input_total) + cast("int", item.output_total) for item in exchanges)
    if not integrity_exact:
        return Metric(value, Precision.UNRESOLVED, integrity_reason or "turn sequence integrity is unresolved")
    usage_terminals = [item.sequence for item in active]
    if (
        not turn_lifecycle_exact
        or not exchange_projection_exact
        or any(sequence not in closed_exchange_terminal_sequences for sequence in usage_terminals)
    ):
        return Metric(value, Precision.UNRESOLVED, "usage requires a uniquely closed exchange and exact turn lifecycle")
    if not all(
        item.bucket_has_provider_provenance(UsageBucket.INPUT)
        and item.bucket_has_provider_provenance(UsageBucket.OUTPUT)
        for item in exchanges
    ):
        return Metric(value, Precision.UNRESOLVED, "normalized usage provenance is incomplete")
    if any(item.failed for item in active):
        return Metric(value, Precision.ESTIMATED, _UNREPORTED_FAILURE_USAGE_REASON)
    return Metric(value, Precision.EXACT)


def _turn_bucket_usage(exchanges: list[_ExchangeUsage], bucket: UsageBucket, verdict: Metric) -> Metric:
    """Split one turn's usage verdict into a per-bucket display metric.

    The turn-level verdict caps every bucket: a missing or unresolved turn can
    never yield an exact bucket, and an estimated one (failed requests, with
    those that reported no full usage already left out of *exchanges*) keeps
    every bucket a lower bound. Optional buckets additionally degrade on
    partial reporting because providers normalize them inconsistently.
    """
    if verdict.value is None:
        return Metric(None, verdict.precision, verdict.reason)
    reported = [value for item in exchanges if (value := item.bucket_value(bucket)) is not None]
    if bucket in _OPTIONAL_USAGE_BUCKETS and exchanges and not reported:
        return Metric(None, Precision.MISSING, "no exchange reported this bucket")
    value = sum(reported)
    if verdict.precision not in {Precision.EXACT, Precision.ESTIMATED}:
        return Metric(value, Precision.UNRESOLVED, verdict.reason)
    if bucket is UsageBucket.REASONING and any(
        item.extras is not None
        and item.extras.reasoning is not None
        and item.output_total is not None
        and item.extras.reasoning > item.output_total
        for item in exchanges
    ):
        return Metric(value, Precision.UNRESOLVED, "reasoning tokens exceed normalized output tokens")
    if len(reported) < len(exchanges):
        return Metric(value, Precision.ESTIMATED, "not every exchange reported this bucket")
    if not all(item.bucket_has_provider_provenance(bucket) for item in exchanges):
        return Metric(value, Precision.UNRESOLVED, "normalized usage provenance is incomplete")
    if verdict.precision is Precision.ESTIMATED:
        return Metric(value, Precision.ESTIMATED, verdict.reason)
    return Metric(value, Precision.EXACT)


def _counter_axis_diagnostics(
    intermediate: _Intermediate,
    turn_id: str,
    inactive_ranges: tuple[SequenceRange, ...],
    *,
    resolution_cache: _ResolutionCache,
    axis_start_ns: int,
    axis_end_ns: int,
    revisions: _RevisionResolution,
    refresh: bool,
    cancel_event: Event | None,
) -> tuple[str, ...]:
    """Validate one dirty physical turn's counter timestamps without a session scan."""
    if not refresh:
        return resolution_cache.counter_axis_diagnostics_by_turn.get(turn_id, ())
    diagnostics: list[str] = []
    usage_sequences: set[int] = set()
    for item in intermediate.usage_by_turn.get(turn_id, ()):
        _check_cancelled(cancel_event)
        if _active(item.sequence, inactive_ranges) and (
            item.input_total is not None or item.output_total is not None or item.extras is not None
        ):
            usage_sequences.add(item.sequence)
    usage_outside = False
    if usage_sequences:
        for node in intermediate.nodes_by_turn.get(turn_id, ()):
            _check_cancelled(cancel_event)
            if node.family != "model.exchange":
                continue
            for endpoint in node.finishes:
                _check_cancelled(cancel_event)
                if (
                    endpoint.turn_id == turn_id
                    and endpoint.sequence in usage_sequences
                    and _active(endpoint.sequence, inactive_ranges)
                    and not axis_start_ns <= endpoint.monotonic_ns <= axis_end_ns
                ):
                    usage_outside = True
                    break
            if usage_outside:
                break
    if usage_outside:
        diagnostics.append("usage sample lies outside its owning attempt axis")

    for revision_id in intermediate.context_revision_ids_by_turn.get(turn_id, ()):
        _check_cancelled(cancel_event)
        endpoint = revisions.endpoints.get(revision_id)
        if endpoint is None:
            continue
        item_count = _payload_int(endpoint.payload, "item_count")
        if (
            item_count is not None
            and item_count >= 0
            and not endpoint.side_call
            and not axis_start_ns <= endpoint.monotonic_ns <= axis_end_ns
        ):
            diagnostics.append("context sample lies outside its owning attempt axis")
            break
    result = tuple(diagnostics)
    resolution_cache.counter_axis_diagnostics_by_turn[turn_id] = result
    return result


@lru_cache(maxsize=256)
def _interned_token_usage(metrics: tuple[Metric, ...]) -> TokenUsage:
    """Share value-identical per-turn usage; every retained turn carries one.

    Long sessions repeat the same bucket shapes turn after turn, and a private
    five-metric dict per turn is what the residency ceiling notices first.
    """
    return TokenUsage(buckets=dict(zip(UsageBucket, metrics, strict=True)))


def _empty_turn(
    intermediate: _Intermediate,
    turn_id: str,
    starts: list[_Endpoint],
    reason: str,
    *,
    inactive_ranges: tuple[SequenceRange, ...],
    resolution_cache: _ResolutionCache,
    revisions: _RevisionResolution,
    refresh: bool,
    cancel_event: Event | None,
) -> TurnAnalysis:
    """Build an unresolved physical attempt without discarding stable opener identity.

    Duplicate starts may still prove one ``turn_number``/``is_retry`` pair and
    therefore participate in logical retry folding. With no surviving start
    (for example, an opener hidden by an unaccounted gap), no such association
    is provable and the attempt deliberately remains an unnumbered turn.
    """
    start = starts[0] if starts else None
    turn_numbers = {_payload_int(candidate.payload, "turn_number") for candidate in starts}
    retry_flags = {_payload_bool(candidate.payload, "is_retry") for candidate in starts}
    runtime_ids = {candidate.runtime_id for candidate in starts}
    turn_number = next(iter(turn_numbers)) if len(turn_numbers) == 1 else None
    is_retry = next(iter(retry_flags)) if len(retry_flags) == 1 else False
    diagnostics = [reason]
    if starts and (len(turn_numbers) != 1 or len(retry_flags) != 1):
        diagnostics.append("turn starts disagree on logical retry identity")
    if len(runtime_ids) > 1:
        diagnostics.append("turn starts disagree on runtime ownership")
    unresolved = Metric(None, Precision.UNRESOLVED, reason)
    runtime_id = start.runtime_id if start is not None else ""
    sequence = start.sequence if start is not None else 0
    axis_ns = start.monotonic_ns if start is not None else 0
    diagnostics.extend(
        _counter_axis_diagnostics(
            intermediate,
            turn_id,
            inactive_ranges,
            resolution_cache=resolution_cache,
            axis_start_ns=axis_ns,
            axis_end_ns=axis_ns,
            revisions=revisions,
            refresh=refresh,
            cancel_event=cancel_event,
        )
    )
    wall = dict.fromkeys(WallBucket, unresolved)
    utilization = dict.fromkeys((WallBucket.MODEL, WallBucket.TOOLS), unresolved)
    return TurnAnalysis(
        turn_id=turn_id,
        turn_number=turn_number,
        runtime_id=runtime_id,
        start_sequence=sequence,
        end_sequence=None,
        elapsed_ns=unresolved,
        compute_cp_ns=unresolved,
        response_cp_ns=unresolved,
        exclusive_work_ns=unresolved,
        parallelism=unresolved,
        overlap_gain_ns=unresolved,
        wall_time_ns=wall,
        utilization=utilization,
        usage_tokens=Metric(None, Precision.MISSING, reason),
        attempts=(
            TurnAttemptRef(
                turn_id=turn_id,
                runtime_id=runtime_id,
                is_retry=is_retry,
                physical_axis_start_ns=axis_ns,
                physical_axis_end_ns=axis_ns,
                logical_axis_start_ns=axis_ns,
                operation_start_index=0,
                operation_end_index=0,
                slice_start_index=0,
                slice_end_index=0,
            ),
        ),
        axis_start_ns=axis_ns,
        axis_end_ns=axis_ns,
        diagnostics=tuple(diagnostics),
        action_projection_precision=Precision.UNRESOLVED,
        action_projection_reason=reason,
    )
