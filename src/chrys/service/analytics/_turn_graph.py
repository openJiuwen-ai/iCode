# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Causal dependency graph and time attribution for one physical turn attempt.

Hook ownership, typed dependency edges, time slices, the wall-time partition,
critical paths and tool/MCP critical-path contributions all rely on one
definition of which operation displaces, waits on, or merely overlaps
another, so they live together. ``_critical_path`` holds the interval-path
search itself.
"""

from __future__ import annotations

from array import array
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import pairwise
from threading import Event
from typing import Final

from chrys.foundation.trajectory.envelope import LinkRelation
from chrys.foundation.trajectory.event_types import RetryMode, ToolOutcome, WaitCategory
from chrys.service.analytics._context_evidence import _CompactionConsumptionResolution, _RevisionResolution
from chrys.service.analytics._critical_path import _longest_interval_path, _PathResolution
from chrys.service.analytics._facts import (
    _HOOK_EXECUTION_MODES,
    _Endpoint,
    _Intermediate,
    _Link,
    _payload_bool,
    _payload_str,
)
from chrys.service.analytics._timeline import _hook_drain_scope, _ResolvedNode, _tool_context_for_start
from chrys.service.analytics.classification import tool_failed
from chrys.service.analytics.math import Interval, clip_interval, interval_union, subtract_intervals
from chrys.service.analytics.model import (
    FLOW_TERMINAL_INDEX,
    HookOwnership,
    TimelineOperation,
    TimeSlice,
    TurnFlow,
    WallBucket,
)
from chrys.service.analytics.reader import raise_if_cancelled as _check_cancelled
from chrys.service.hooks.events import HookEvent

_WALL_PRIORITY: Final = {
    WallBucket.IDLE: 0,
    WallBucket.WAIT: 1,
    WallBucket.TOOLS: 2,
    WallBucket.MODEL: 3,
}
# Calls closed before dispatch have no tool preamble and no caused-by edge.
_NEVER_DISPATCHED_TOOL_OUTCOMES: Final = frozenset(
    {
        ToolOutcome.INVALID_ARGUMENTS,
        ToolOutcome.UNKNOWN_TOOL,
        ToolOutcome.FILTERED,
    }
)
_MODEL_OPERATION_FAMILIES: Final = frozenset({"model.run", "model.cycle", "model.exchange"})
# An in-process sub-agent records no model.run: its sub_agent operation is
# the run, so a compaction it runs between cycles names that operation as
# its parent.
_COMPACTION_PARENT_FAMILIES: Final = _MODEL_OPERATION_FAMILIES | {"sub_agent"}
_RETRY_EXCHANGE_MODES: Final = frozenset(
    {RetryMode.WIRE, RetryMode.STALL_FALLBACK, RetryMode.VALIDATION, RetryMode.CONTEXT_OVERFLOW}
)


@dataclass(frozen=True, slots=True)
class _DependencyProof:
    edges: dict[str, set[str]]
    turn_root_id: str | None
    response_terminal_id: str | None
    # Displacing child -> parent pairs; an edge absent here is a causal pointer.
    parents: dict[str, str]
    # Edges that fork a concurrent hook off its target; the hook only depends
    # on the target's triggering event, never on the target completing.
    fork_edges: frozenset[tuple[str, str]] = frozenset()


@dataclass(frozen=True, slots=True)
class _TurnProjection:
    slices: tuple[TimeSlice, ...]
    dependency: _DependencyProof
    timeline_exact: bool
    dag_exact: bool
    response_dependency_exact: bool


@dataclass(frozen=True, slots=True)
class _CriticalPaths:
    compute_ns: int | None
    response_ns: int | None
    acyclic: bool
    response_reachable: bool
    compute_bounded: bool
    response_bounded: bool


def _project_turn(
    nodes: list[_ResolvedNode],
    turn_bounds: Interval,
    *,
    start: _Endpoint,
    finish: _Endpoint | None,
    tail_end: int | None,
    waited_hook_ids: frozenset[str] | None,
    suspension_deductions: dict[str, list[Interval]],
    revisions: _RevisionResolution,
    compaction_consumption: _CompactionConsumptionResolution,
    mapped_carrier: Callable[[str], str | None],
    cancel_event: Event | None,
    diagnostics: list[str],
) -> _TurnProjection:
    by_operation: dict[str, list[_ResolvedNode]] = defaultdict(list)
    for node in nodes:
        by_operation[node.operation_id].append(node)
    preparation_for_tool = {
        target: node
        for node in nodes
        if node.family == "preparation"
        and _payload_str(node.start.payload, "scope") == "tool_preamble"
        and (target := _payload_str(node.start.payload, "target_operation_id")) is not None
    }
    parents: dict[str, str] = {}
    graph_edges: dict[str, set[str]] = defaultdict(set)
    unsafe_work_nodes: set[str] = set()
    timeline_exact = True
    dag_exact = True
    for node in nodes:
        _check_cancelled(cancel_event)
        parent = _displacing_parent(node, by_operation, preparation_for_tool, diagnostics)
        if parent is False:
            timeline_exact = False
            dag_exact = False
            if node.family == "hook.operation":
                unsafe_work_nodes.add(node.node_id)
        elif isinstance(parent, str):
            parents[node.node_id] = parent
            graph_edges[parent].add(node.node_id)
        for relation, target_operation_id in node.start.links:
            if relation != LinkRelation.CAUSED_BY:
                continue
            # A retry lifecycle deliberately shares the next operation's id.
            # caused_by names the work operation, never that retry marker.
            targets = [
                target
                for target in by_operation.get(target_operation_id, [])
                if target.family != "retry" and target.node_id != node.node_id
            ]
            if len(targets) == 1:
                graph_edges[targets[0].node_id].add(node.node_id)
            else:
                diagnostics.append("caused_by target cannot be resolved uniquely")
                dag_exact = False
    response_dependency_exact = _validate_async_hook_fences(nodes, waited_hook_ids, diagnostics)
    children: dict[str, set[str]] = defaultdict(set)
    for child, parent in parents.items():
        children[parent].add(child)
    node_by_id = {node.node_id: node for node in nodes}
    if _has_cycle(children):
        diagnostics.append("displacing-edge graph contains a cycle")
        return _TurnProjection(
            slices=(),
            dependency=_DependencyProof(graph_edges, None, None, parents),
            timeline_exact=False,
            dag_exact=False,
            response_dependency_exact=response_dependency_exact,
        )
    slices: list[TimeSlice] = []
    for node in nodes:
        _check_cancelled(cancel_event)
        if _skip_node(node):
            continue
        descendants = _descendants(node.node_id, children)
        removed = [node_by_id[descendant].interval for descendant in descendants]
        removed.extend(suspension_deductions.get(node.node_id, ()))
        residuals = subtract_intervals(node.interval, removed)
        attributes = _slice_attributes(node)
        if attributes is None:
            continue
        if node.family == "hook.operation":
            mode = _payload_str(node.start.payload, "execution_mode")
            hook_event = _payload_str(node.start.payload, "hook_event")
            if hook_event == "user_interrupt" or mode == "fire_and_forget":
                attributes = replace(attributes, response_weight=False)
            elif mode == "async":
                attributes = replace(
                    attributes, response_weight=waited_hook_ids is not None and node.operation_id in waited_hook_ids
                )
        if node.node_id in unsafe_work_nodes:
            attributes = replace(attributes, counts_as_work=False, compute_weight=False, response_weight=False)
        for index, residual in enumerate(residuals):
            clipped = clip_interval(residual, turn_bounds)
            if clipped is None:
                continue
            slices.append(
                TimeSlice(
                    family=node.family,
                    slice_index=index,
                    turn_id=start.turn_id or "",
                    runtime_id=node.start.runtime_id,
                    operation_id=node.operation_id,
                    owner=attributes.owner,
                    start_ns=clipped[0],
                    end_ns=clipped[1],
                    wall_bucket=attributes.bucket,
                    counts_as_work=attributes.counts_as_work,
                    compute_weight=attributes.compute_weight,
                    response_weight=attributes.response_weight,
                    outcome=_payload_str(node.finish.payload, "outcome"),
                    tool_name=_payload_str(node.start.payload, "tool_name"),
                    tool_kind=_payload_str(node.start.payload, "tool_kind"),
                )
            )
    typed_exact, response_source_id = _connect_typed_dependencies(
        nodes,
        graph_edges,
        main_actor_id=start.actor_id,
        revisions=revisions,
        compaction_consumption=compaction_consumption,
        mapped_carrier=mapped_carrier,
        cancel_event=cancel_event,
        diagnostics=diagnostics,
    )
    dag_exact = dag_exact and typed_exact
    roots = [
        node.node_id
        for node in nodes
        if node.family == "preparation" and _payload_str(node.start.payload, "scope") == "turn_preamble"
    ]
    turn_root_id = roots[0] if len(roots) == 1 else None
    response_terminal_id = (
        f"turn.response:{start.turn_id}"
        if finish is not None and tail_end is not None and response_source_id is not None
        else None
    )
    if response_terminal_id is not None and response_source_id is not None:
        graph_edges[response_source_id].add(response_terminal_id)
    fork_edges: set[tuple[str, str]] = set()
    hook_exactness = _connect_hook_dependencies(
        nodes,
        graph_edges,
        waited_hook_ids=waited_hook_ids,
        turn_root_id=turn_root_id,
        response_source_id=response_source_id,
        response_terminal_id=response_terminal_id,
        fork_edges=fork_edges,
        diagnostics=diagnostics,
    )
    dag_exact = dag_exact and hook_exactness.dag
    response_dependency_exact = response_dependency_exact and hook_exactness.response
    if response_source_id is not None and response_terminal_id is not None:
        response_descendants = _descendants(response_source_id, children)
        for node_id in response_descendants:
            if not children.get(node_id, set()) & response_descendants:
                graph_edges[node_id].add(response_terminal_id)
    return _TurnProjection(
        slices=tuple(slices),
        dependency=_DependencyProof(graph_edges, turn_root_id, response_terminal_id, parents, frozenset(fork_edges)),
        timeline_exact=timeline_exact,
        dag_exact=dag_exact,
        response_dependency_exact=response_dependency_exact,
    )


def _turn_flow(
    turn_id: str,
    dependency: _DependencyProof,
    operations: tuple[TimelineOperation, ...],
    *,
    acyclic: bool,
) -> TurnFlow:
    """Index-encode the typed per-turn dependency graph against the operations tuple."""
    index_by_node: dict[str, int] = {}
    for index, operation in enumerate(operations):
        index_by_node.setdefault(f"{operation.family}:{operation.operation_id}", index)
    terminal_id = dependency.response_terminal_id
    parent_pairs = array("I")
    causal_pairs = array("I")
    for source, targets in sorted(dependency.edges.items()):
        source_index = index_by_node.get(source)
        if source_index is None:
            continue
        for target in sorted(targets):
            if target == terminal_id:
                target_index = FLOW_TERMINAL_INDEX
            else:
                found = index_by_node.get(target)
                if found is None:
                    continue
                target_index = found
            pairs = parent_pairs if dependency.parents.get(target) == source else causal_pairs
            pairs.append(source_index)
            pairs.append(target_index)
    root_id = dependency.turn_root_id
    return TurnFlow(
        turn_id=turn_id,
        root_index=index_by_node.get(root_id) if root_id is not None else None,
        has_terminal=terminal_id is not None,
        parent_pairs=parent_pairs.tobytes(),
        causal_pairs=causal_pairs.tobytes(),
        acyclic=acyclic,
    )


def _displacing_parent(
    node: _ResolvedNode,
    by_operation: dict[str, list[_ResolvedNode]],
    preparation_for_tool: dict[str, _ResolvedNode],
    diagnostics: list[str],
) -> str | bool | None:
    if node.family == "preparation":
        scope = _payload_str(node.start.payload, "scope")
        if scope == "turn_preamble":
            return None
        if scope == "pre_turn":
            return None
    if node.family == "hook.operation":
        hook_event = _payload_str(node.start.payload, "hook_event")
        mode = _payload_str(node.start.payload, "execution_mode")
        ownership = classify_hook_ownership(hook_event, mode)
        if ownership is HookOwnership.CONCURRENT:
            return None
        target = _payload_str(node.start.payload, "target_operation_id")
        if ownership is HookOwnership.TOOL_PREAMBLE and target is not None:
            preamble = preparation_for_tool.get(target)
            if preamble is not None:
                return preamble.node_id
        elif ownership in {
            HookOwnership.TOOL_TAIL,
            HookOwnership.APPROVAL,
            HookOwnership.TURN_PREAMBLE,
            HookOwnership.SUB_AGENT,
            HookOwnership.COMPACTION,
        }:
            families = _hook_target_families(hook_event)
            resolved = [candidate for candidate in by_operation.get(target or "", []) if candidate.family in families]
            if len(resolved) == 1:
                return resolved[0].node_id
        elif ownership in {HookOwnership.TURN_TAIL, HookOwnership.SESSION_ROOT, HookOwnership.WORKFLOW_ROOT}:
            return None
        diagnostics.append(f"blocking hook {hook_event or 'unknown'} has no placeable displacing edge")
        return False
    if node.family == "tool.operation":
        parent = _payload_str(node.start.payload, "parent_model_operation_id")
        if parent is None:
            diagnostics.append("tool operation lacks parent_model_operation_id")
            return False
        candidates = [
            candidate for candidate in by_operation.get(parent, []) if candidate.family in _MODEL_OPERATION_FAMILIES
        ]
        if len(candidates) == 1:
            return candidates[0].node_id
        diagnostics.append("tool parent model operation cannot be resolved uniquely")
        return False
    if node.family == "turn.suspension":
        return None
    target = None
    families: frozenset[str] | None = None
    if node.family == "approval":
        generic_target = _payload_str(node.start.payload, "target_operation_id")
        target = generic_target or _payload_str(node.start.payload, "target_tool_operation_id")
        if generic_target is None:
            families = frozenset({"tool.operation"})
    elif node.family in {"preparation", "compaction"}:
        target = node.start.parent_operation_id
        families = _COMPACTION_PARENT_FAMILIES if node.family == "compaction" else _MODEL_OPERATION_FAMILIES
    elif node.family == "compaction.phase":
        target = node.start.parent_operation_id
        families = frozenset({"compaction"})
    elif node.family == "model.cycle":
        target = node.start.parent_operation_id
        families = frozenset({"model.run", "sub_agent"})
    elif node.family == "model.exchange":
        target = node.start.parent_operation_id
        families = frozenset({"model.run", "model.cycle"})
    elif node.family == "sub_agent":
        target = node.start.parent_operation_id
        families = frozenset({"tool.operation", *_MODEL_OPERATION_FAMILIES})
    elif node.family == "wait":
        target = node.start.parent_operation_id
        category = _payload_str(node.start.payload, "category")
        families = _wait_parent_families(category)
    elif node.family == "retry":
        target = node.start.parent_operation_id
        families = _retry_parent_families(_payload_str(node.start.payload, "retry_mode"))
    else:
        target = node.start.parent_operation_id
    if target is None:
        return None
    candidates = [
        candidate
        for candidate in by_operation.get(target, [])
        if candidate.node_id != node.node_id
        and candidate.family != "retry"
        and (families is None or candidate.family in families)
    ]
    if len(candidates) == 1:
        return candidates[0].node_id
    if candidates:
        diagnostics.append(f"{node.family} parent operation is ambiguous")
        return False
    diagnostics.append(f"{node.family} parent operation is missing")
    return False


def _wait_parent_families(category: str | None) -> frozenset[str] | None:
    if category == WaitCategory.USER_INPUT:
        return frozenset({"tool.operation", "sub_agent"})
    if category == WaitCategory.MCP_CONNECT:
        return frozenset({"tool.operation"})
    if category in {WaitCategory.INPUT_ADMISSION, WaitCategory.TOOL_ADMISSION, WaitCategory.PROMPT_COMMANDS}:
        return frozenset({"preparation"})
    if category == WaitCategory.RATE_LIMIT:
        return _MODEL_OPERATION_FAMILIES
    if category == WaitCategory.SUB_AGENT_CONCURRENCY:
        return frozenset({"sub_agent"})
    return None


def _retry_parent_families(retry_mode: str | None) -> frozenset[str] | None:
    if retry_mode in _RETRY_EXCHANGE_MODES:
        return frozenset({"model.run", "model.cycle"})
    if retry_mode == RetryMode.RUN:
        return frozenset({"sub_agent"})
    if retry_mode == RetryMode.COMPACTION:
        return frozenset({"compaction"})
    if retry_mode == RetryMode.CONNECTION:
        return frozenset({"sub_agent"})
    return None


def _hook_target_families(hook_event: str | None) -> frozenset[str]:
    if hook_event in {HookEvent.APPROVAL_REQUESTED, HookEvent.APPROVAL_RESOLVED}:
        return frozenset({"approval"})
    if hook_event in {HookEvent.BEFORE_TOOL_CALL, HookEvent.AFTER_TOOL_CALL, HookEvent.TOOL_ERROR}:
        return frozenset({"tool.operation"})
    if hook_event == HookEvent.BEFORE_TURN:
        return frozenset({"preparation"})
    if hook_event in {HookEvent.SUB_AGENT_START, HookEvent.SUB_AGENT_END}:
        return frozenset({"sub_agent"})
    if hook_event == HookEvent.PRE_COMPACT:
        return frozenset({"compaction"})
    return frozenset()


def classify_hook_ownership(hook_event: str | None, execution_mode: str | None) -> HookOwnership:
    """Apply the ordered, exhaustive hook ownership matrix without timing guesses."""
    if execution_mode not in _HOOK_EXECUTION_MODES:
        return HookOwnership.UNSAFE
    if hook_event == HookEvent.USER_INTERRUPT or execution_mode != "blocking":
        return HookOwnership.CONCURRENT
    if hook_event == HookEvent.BEFORE_TOOL_CALL:
        return HookOwnership.TOOL_PREAMBLE
    if hook_event in {HookEvent.AFTER_TOOL_CALL, HookEvent.TOOL_ERROR}:
        return HookOwnership.TOOL_TAIL
    if hook_event in {HookEvent.APPROVAL_REQUESTED, HookEvent.APPROVAL_RESOLVED}:
        return HookOwnership.APPROVAL
    if hook_event == HookEvent.BEFORE_TURN:
        return HookOwnership.TURN_PREAMBLE
    if hook_event == HookEvent.AFTER_TURN:
        return HookOwnership.TURN_TAIL
    if hook_event == HookEvent.USER_PROMPT_SUBMIT:
        return HookOwnership.PRE_TURN
    if hook_event in {HookEvent.SUB_AGENT_START, HookEvent.SUB_AGENT_END}:
        return HookOwnership.SUB_AGENT
    if hook_event == HookEvent.PRE_COMPACT:
        return HookOwnership.COMPACTION
    if hook_event in {HookEvent.WORKFLOW_RUN_START, HookEvent.WORKFLOW_RUN_END}:
        return HookOwnership.WORKFLOW_ROOT
    if hook_event in {HookEvent.SESSION_START, HookEvent.SESSION_RESTORED, HookEvent.SESSION_END}:
        return HookOwnership.SESSION_ROOT
    return HookOwnership.UNSAFE


@dataclass(frozen=True, slots=True)
class _SliceAttributes:
    """How the time of a node's slices counts."""

    bucket: WallBucket
    counts_as_work: bool
    compute_weight: bool
    """Whether the time can count toward the compute critical path."""
    response_weight: bool
    """Whether the time can count toward the response critical path."""
    owner: str
    """The label slices carry (``TimeSlice.owner``)."""

    @classmethod
    def work(cls, bucket: WallBucket, owner: str, *, response_weight: bool = True) -> _SliceAttributes:
        return cls(bucket, counts_as_work=True, compute_weight=True, response_weight=response_weight, owner=owner)

    @classmethod
    def waiting(cls, owner: str) -> _SliceAttributes:
        return cls(WallBucket.WAIT, counts_as_work=False, compute_weight=False, response_weight=True, owner=owner)


def _slice_attributes(node: _ResolvedNode) -> _SliceAttributes | None:
    family = node.family
    if family in {"model.run", "model.cycle", "model.exchange"}:
        return _SliceAttributes.work(WallBucket.MODEL, "model")
    if family in {"compaction", "compaction.phase"}:
        return _SliceAttributes.work(WallBucket.MODEL, "compaction")
    if family == "tool.operation":
        if _payload_str(node.start.payload, "tool_kind") == "sleep":
            return _SliceAttributes.waiting("tool sleep")
        return _SliceAttributes.work(WallBucket.TOOLS, "tool")
    if family == "sub_agent":
        return _SliceAttributes.work(WallBucket.TOOLS, "sub-agent")
    if family == "continuation.poll":
        return _SliceAttributes.waiting("continuation poll")
    if family == "turn.suspension":
        return _SliceAttributes.waiting("sub-agent suspension")
    if family in {"wait", "approval", "retry"}:
        if family == "wait" and _payload_str(node.start.payload, "category") in {
            WaitCategory.INPUT_ADMISSION,
            WaitCategory.PROMPT_COMMANDS,
        }:
            # Part of the admission's own preparation time: the gates it passes and
            # the commands the user's prompt runs, not time spent waiting.
            return None
        owner = (
            "approval" if family == "approval" or _payload_str(node.start.payload, "category") == "approval" else family
        )
        return _SliceAttributes.waiting(owner)
    if family == "preparation":
        scope = _payload_str(node.start.payload, "scope")
        if scope == "pre_turn":
            return None
        return _SliceAttributes.work(WallBucket.TOOLS, "preparation")
    if family == "hook.operation":
        outcome = _payload_str(node.finish.payload, "outcome")
        if outcome == "detached":
            return None
        mode = _payload_str(node.start.payload, "execution_mode")
        hook_event = _payload_str(node.start.payload, "hook_event")
        if mode == "fire_and_forget" or hook_event == "user_interrupt":
            return _SliceAttributes.work(WallBucket.TOOLS, "hook", response_weight=False)
        return _SliceAttributes.work(WallBucket.TOOLS, "hook")
    return None


def _skip_node(node: _ResolvedNode) -> bool:
    return node.family == "tool.operation" and _payload_bool(node.finish.payload, "abandoned")


def _never_dispatched(node: _ResolvedNode) -> bool:
    return (
        node.family == "tool.operation"
        and _payload_str(node.finish.payload, "outcome") in _NEVER_DISPATCHED_TOOL_OUTCOMES
    )


def _required_causes_present(nodes: list[_ResolvedNode], diagnostics: list[str]) -> bool:
    preambles = [
        node
        for node in nodes
        if node.family == "preparation" and _payload_str(node.start.payload, "scope") == "turn_preamble"
    ]
    runs = sorted((node for node in nodes if node.family == "model.run"), key=lambda node: node.start.sequence)
    exact = True
    if len(preambles) != 1 or not runs:
        diagnostics.append("turn_preamble or first model.run is missing")
        exact = False
    elif _Link(LinkRelation.CAUSED_BY, preambles[0].operation_id) not in runs[0].start.links:
        diagnostics.append("first model.run lacks caused_by link to turn_preamble")
        exact = False
    tool_preambles = {
        target: node
        for node in nodes
        if node.family == "preparation"
        and _payload_str(node.start.payload, "scope") == "tool_preamble"
        and (target := _payload_str(node.start.payload, "target_operation_id")) is not None
    }
    tools = [node for node in nodes if node.family == "tool.operation" and not _never_dispatched(node)]
    if len(tool_preambles) != len(tools):
        diagnostics.append("tool_preamble to tool pairing is not one-to-one")
        exact = False
    for tool in tools:
        preamble = tool_preambles.get(tool.operation_id)
        if preamble is None or _Link(LinkRelation.CAUSED_BY, preamble.operation_id) not in tool.start.links:
            diagnostics.append("tool operation lacks caused_by link to its tool_preamble")
            exact = False
    return exact


def _validate_async_hook_fences(
    nodes: list[_ResolvedNode],
    waited_hook_ids: frozenset[str] | None,
    diagnostics: list[str],
) -> bool:
    async_turn_hooks = {
        node.operation_id
        for node in nodes
        if node.family == "hook.operation"
        and _payload_str(node.start.payload, "execution_mode") == "async"
        and _hook_drain_scope(node.start) == "turn"
    }
    if not async_turn_hooks:
        return True
    if waited_hook_ids is None:
        diagnostics.append("async hook response dependencies lack a complete turn fence")
        return False
    known_hooks = {node.operation_id: node for node in nodes if node.family == "hook.operation"}
    invalid_members = [
        operation_id
        for operation_id in waited_hook_ids
        if operation_id not in known_hooks
        or _payload_str(known_hooks[operation_id].start.payload, "execution_mode") != "async"
        or _hook_drain_scope(known_hooks[operation_id].start) != "turn"
    ]
    if invalid_members:
        diagnostics.append("turn fence contains a hook that is not an async turn-scope dependency")
        return False
    return True


def _connect_typed_dependencies(
    nodes: list[_ResolvedNode],
    edges: dict[str, set[str]],
    *,
    main_actor_id: str | None,
    revisions: _RevisionResolution,
    compaction_consumption: _CompactionConsumptionResolution,
    mapped_carrier: Callable[[str], str | None],
    cancel_event: Event | None,
    diagnostics: list[str],
) -> tuple[bool, str | None]:
    """Connect only producer-declared causal pointers; never infer adjacency."""
    by_operation: dict[str, list[_ResolvedNode]] = defaultdict(list)
    for node in nodes:
        by_operation[node.operation_id].append(node)
    exact = compaction_consumption.exact

    compaction_bridges: dict[str, str] = {}
    for run_id, consumption in compaction_consumption.by_run.items():
        _check_cancelled(cancel_event)
        compactions = [candidate for candidate in by_operation.get(run_id, ()) if candidate.family == "compaction"]
        if len(compactions) != 1:
            diagnostics.append("Phase-4 consumed items cannot resolve their compaction run uniquely")
            exact = False
            continue
        compaction = compactions[0]
        candidates = [
            candidate
            for candidate in by_operation.get(consumption.next_exchange_operation_id, ())
            if candidate.family == "model.exchange"
            and candidate.start.scope == compaction.start.scope
            and candidate.start.sequence > compaction.finish.sequence
            and candidate.start.monotonic_ns >= compaction.finish.monotonic_ns
        ]
        if len(candidates) != 1:
            diagnostics.append("Phase-4 compaction successor exchange cannot be resolved uniquely")
            exact = False
            continue
        edges[compaction.node_id].add(candidates[0].node_id)
        compaction_bridges[compaction.operation_id] = compaction.node_id

    for run in (node for node in nodes if node.family == "model.run"):
        _check_cancelled(cancel_event)
        previous = _payload_str(run.start.payload, "previous_run_operation_id")
        if previous is not None:
            exact = (
                _connect_unique(
                    by_operation,
                    previous,
                    run.node_id,
                    edges,
                    diagnostics,
                    "previous model run",
                    families=frozenset({"model.run"}),
                )
                and exact
            )

    for retry in (node for node in nodes if node.family == "retry"):
        retry_mode = _payload_str(retry.start.payload, "retry_mode")
        neighbor_families = _retry_neighbor_families(retry_mode)
        previous = _payload_str(retry.start.payload, "previous_operation_id")
        following = _payload_str(retry.start.payload, "next_operation_id") or _payload_str(
            retry.finish.payload, "next_operation_id"
        )
        if previous is not None:
            exact = (
                _connect_unique(
                    by_operation,
                    previous,
                    retry.node_id,
                    edges,
                    diagnostics,
                    "retry predecessor",
                    families=neighbor_families,
                )
                and exact
            )
        if following is not None:
            exact = (
                _connect_unique(
                    by_operation,
                    following,
                    None,
                    edges,
                    diagnostics,
                    "retry successor",
                    source=retry.node_id,
                    families=neighbor_families,
                )
                and exact
            )

    for poll in (node for node in nodes if node.family == "continuation.poll"):
        _check_cancelled(cancel_event)
        previous = _payload_str(poll.start.payload, "previous_exchange_operation_id")
        following = _payload_str(poll.start.payload, "next_exchange_operation_id")
        if previous is None or following is None:
            diagnostics.append("continuation poll lacks typed neighboring exchanges")
            exact = False
            continue
        exact = (
            _connect_unique(
                by_operation,
                previous,
                poll.node_id,
                edges,
                diagnostics,
                "continuation poll predecessor",
                families=frozenset({"model.exchange"}),
            )
            and exact
        )
        exact = (
            _connect_unique(
                by_operation,
                following,
                None,
                edges,
                diagnostics,
                "continuation poll successor",
                source=poll.node_id,
                families=frozenset({"model.exchange"}),
            )
            and exact
        )

    response_source_id: str | None = None
    response_source_operation_id: str | None = None
    # Sub-agent cycles are inlined in the turn; only the turn's own actor
    # answers the user, so the response source is its last final exchange.
    cycles_with_final = [
        (node, final_exchange)
        for node in nodes
        if node.family == "model.cycle"
        and node.start.actor_id == main_actor_id
        and (final_exchange := _payload_str(node.finish.payload, "final_exchange_operation_id")) is not None
    ]
    if not cycles_with_final:
        diagnostics.append("response linkage lacks final_exchange_operation_id")
    else:
        _, final_exchange_id = max(cycles_with_final, key=lambda item: item[0].finish.sequence)
        candidates = [node for node in by_operation.get(final_exchange_id, ()) if node.family == "model.exchange"]
        if len(candidates) != 1:
            diagnostics.append("final exchange operation cannot be resolved uniquely")
            exact = False
        else:
            response_source_id = candidates[0].node_id
            response_source_operation_id = candidates[0].operation_id

    exchanges_by_revision: dict[str, list[_ResolvedNode]] = defaultdict(list)
    for exchange in (node for node in nodes if node.family == "model.exchange"):
        _check_cancelled(cancel_event)
        revision_id = _payload_str(exchange.start.payload, "context_revision_id")
        if revision_id is not None:
            exchanges_by_revision[revision_id].append(exchange)

    valid_exchange_memberships: dict[str, tuple[str, ...]] = {}
    invalid_exchange_membership_diagnostics: list[str] = []
    for revision_id, exchanges in exchanges_by_revision.items():
        _check_cancelled(cancel_event)
        revision_errors = revisions.errors.get(revision_id, ())
        if revision_errors:
            invalid_exchange_membership_diagnostics.extend(revision_errors)
            continue
        membership = revisions.memberships.get(revision_id)
        revision = revisions.endpoints.get(revision_id)
        if membership is None or revision is None:
            invalid_exchange_membership_diagnostics.append(f"context revision {revision_id} cannot be resolved")
            continue
        if len(exchanges) != 1:
            invalid_exchange_membership_diagnostics.append(
                f"context revision {revision_id} is claimed by multiple exchanges"
            )
            continue
        exchange = exchanges[0]
        if (
            revision.parent_operation_id != exchange.operation_id
            or revision.runtime_id != exchange.start.runtime_id
            or revision.branch_id != exchange.start.branch_id
            or revision.coverage_id != exchange.start.coverage_id
            or revision.actor_id != exchange.start.actor_id
            or revision.sequence >= exchange.start.sequence
        ):
            invalid_exchange_membership_diagnostics.append(
                f"context revision {revision_id} does not belong to its claiming exchange"
            )
            continue
        valid_exchange_memberships[revision_id] = membership
    diagnostics.extend(invalid_exchange_membership_diagnostics)

    for tool in (node for node in nodes if node.family == "tool.operation" and not _skip_node(node)):
        _check_cancelled(cancel_event)
        parent = _payload_str(tool.start.payload, "parent_model_operation_id")
        if parent is None:
            diagnostics.append("tool call producer lacks parent_model_operation_id")
            exact = False
        else:
            exact = (
                _connect_unique(
                    by_operation,
                    parent,
                    tool.node_id,
                    edges,
                    diagnostics,
                    "tool call producer",
                    families=frozenset({"model.run", "model.cycle", "model.exchange"}),
                )
                and exact
            )
        call_item_id = _payload_str(tool.start.payload, "call_item_id")
        result_item_id = _payload_str(tool.finish.payload, "result_item_id")
        if result_item_id is None and _never_dispatched(tool):
            # A call the kernel closed without dispatching may own no result
            # item at all (a filtered call never gets one), so there is no
            # fan-in edge to demand. One that does carry a result (invalid
            # arguments, unknown tool) is consumed like any other and falls
            # through to the ordinary requirement.
            continue
        if call_item_id is None or result_item_id is None:
            diagnostics.append("tool result fan-in lacks call_item_id or result_item_id")
            exact = False
            continue
        result_carrier_item_id = _payload_str(tool.finish.payload, "result_carrier_item_id")
        fan_in_item_ids = {result_item_id}
        if result_carrier_item_id is not None:
            fan_in_item_ids.add(result_carrier_item_id)
        consumers = {
            exchange.node_id
            for revision_id, memberships in valid_exchange_memberships.items()
            if fan_in_item_ids.intersection(memberships)
            for exchange in exchanges_by_revision.get(revision_id, ())
        }
        if not consumers:
            needs_consumer = response_source_operation_id is None or parent != response_source_operation_id
            carrier_mapping_missing = False
            if needs_consumer and result_carrier_item_id is None:
                result_carrier_item_id = mapped_carrier(result_item_id)
                if result_carrier_item_id is None:
                    carrier_mapping_missing = True
                else:
                    fan_in_item_ids.add(result_carrier_item_id)
                    consumers = {
                        exchange.node_id
                        for revision_id, memberships in valid_exchange_memberships.items()
                        if result_carrier_item_id in memberships
                        for exchange in exchanges_by_revision.get(revision_id, ())
                    }
            if needs_consumer and not consumers:
                compaction_runs = [
                    run_id
                    for run_id, consumption in compaction_consumption.by_run.items()
                    if fan_in_item_ids.intersection(consumption.item_ids)
                ]
                if len(compaction_runs) == 1:
                    compaction_node_id = compaction_bridges.get(compaction_runs[0])
                    if compaction_node_id is not None:
                        consumers = {compaction_node_id}
                elif len(compaction_runs) > 1:
                    diagnostics.append("tool result fan-in matches multiple Phase-4 compaction runs")
                    exact = False
            if needs_consumer and not consumers:
                if carrier_mapping_missing:
                    diagnostics.append("carrier mapping unavailable")
                diagnostics.append("tool result fan-in cannot resolve a consuming context revision")
                exact = False
        if not consumers:
            continue
        edges[tool.node_id].update(consumers)
    return exact, response_source_id


def _retry_neighbor_families(retry_mode: str | None) -> frozenset[str] | None:
    if retry_mode in _RETRY_EXCHANGE_MODES:
        return frozenset({"model.exchange"})
    if retry_mode == RetryMode.RUN:
        return frozenset({"model.run"})
    return None


def _connect_unique(
    by_operation: dict[str, list[_ResolvedNode]],
    operation_id: str,
    target_node_id: str | None,
    edges: dict[str, set[str]],
    diagnostics: list[str],
    label: str,
    *,
    source: str | None = None,
    families: frozenset[str] | None = None,
) -> bool:
    candidates = [
        candidate
        for candidate in by_operation.get(operation_id, ())
        if candidate.node_id != source
        and candidate.node_id != target_node_id
        and (families is None or candidate.family in families)
    ]
    if len(candidates) != 1:
        diagnostics.append(f"{label} cannot be resolved uniquely")
        return False
    if source is None and target_node_id is not None:
        edges[candidates[0].node_id].add(target_node_id)
    elif source is not None:
        edges[source].add(candidates[0].node_id)
    return True


@dataclass(frozen=True, slots=True)
class _HookDependencyExactness:
    """Whether the hook edges added to the graph can be trusted."""

    dag: bool
    """Every blocking hook dispatch carried ordering evidence and a consistent serial chain."""
    response: bool
    """Every hook the response fence waited on is known and reachable from the turn root."""


def _connect_hook_dependencies(
    nodes: list[_ResolvedNode],
    edges: dict[str, set[str]],
    *,
    waited_hook_ids: frozenset[str] | None,
    turn_root_id: str | None,
    response_source_id: str | None,
    response_terminal_id: str | None,
    fork_edges: set[tuple[str, str]],
    diagnostics: list[str],
) -> _HookDependencyExactness:
    by_operation: dict[str, list[_ResolvedNode]] = defaultdict(list)
    for node in nodes:
        by_operation[node.operation_id].append(node)
    runs = sorted((node for node in nodes if node.family == "model.run"), key=lambda node: node.start.sequence)
    dag_exact = True
    response_exact = True
    after_tool_hooks: list[tuple[_ResolvedNode, _ResolvedNode]] = []
    blocking_dispatches: dict[tuple[str, str | None, str], list[_ResolvedNode]] = defaultdict(list)
    for hook in (node for node in nodes if node.family == "hook.operation"):
        mode = _payload_str(hook.start.payload, "execution_mode")
        event = _payload_str(hook.start.payload, "hook_event")
        if mode not in _HOOK_EXECUTION_MODES:
            continue
        target = _payload_str(hook.start.payload, "target_operation_id")
        if mode == "blocking" and event != HookEvent.USER_INTERRUPT:
            scope = _hook_drain_scope(hook.start)
            if event is None or scope is None:
                diagnostics.append("blocking hook dispatch lacks field-based ordering evidence")
                dag_exact = False
            else:
                blocking_dispatches[(event, target, scope)].append(hook)
        target_families = _hook_target_families(event)
        targets = [
            candidate
            for candidate in by_operation.get(target or "", ())
            if candidate.family != "retry" and (not target_families or candidate.family in target_families)
        ]
        if mode != "blocking" or event == HookEvent.USER_INTERRUPT:
            if target is not None and len(targets) == 1:
                edges[targets[0].node_id].add(hook.node_id)
                fork_edges.add((targets[0].node_id, hook.node_id))
        elif event == HookEvent.BEFORE_TOOL_CALL and len(targets) == 1:
            edges[hook.node_id].add(targets[0].node_id)
        elif event == HookEvent.BEFORE_TURN and runs:
            edges[hook.node_id].add(runs[0].node_id)
        elif event in {HookEvent.AFTER_TOOL_CALL, HookEvent.TOOL_ERROR} and len(targets) == 1:
            after_tool_hooks.append((hook, targets[0]))
        elif event == HookEvent.AFTER_TURN and response_source_id is not None:
            edges[response_source_id].add(hook.node_id)
            if response_terminal_id is not None:
                edges[hook.node_id].add(response_terminal_id)
    after_tool_hook_node_ids = {hook.node_id for hook, _ in after_tool_hooks}
    for hook, target in after_tool_hooks:
        for successor in tuple(edges.get(target.node_id, ())):
            if successor not in after_tool_hook_node_ids:
                edges[hook.node_id].add(successor)
    for dispatch_hooks in blocking_dispatches.values():
        ordered = sorted(dispatch_hooks, key=lambda hook: hook.start.sequence)
        for previous, current in pairwise(ordered):
            edges[previous.node_id].add(current.node_id)
            if (
                previous.start.runtime_id != current.start.runtime_id
                or previous.start.branch_id != current.start.branch_id
                or previous.finish.sequence >= current.start.sequence
                or previous.finish.monotonic_ns > current.start.monotonic_ns
            ):
                diagnostics.append("blocking hook dispatch has inconsistent serial intervals")
                dag_exact = False
    if waited_hook_ids is not None and response_terminal_id is not None:
        hooks = {node.operation_id: node for node in nodes if node.family == "hook.operation"}
        for operation_id in waited_hook_ids:
            hook = hooks.get(operation_id)
            if hook is None:
                diagnostics.append("turn response fence names an unknown hook operation")
                response_exact = False
            else:
                edges[hook.node_id].add(response_terminal_id)
                if turn_root_id is None or not _reachable(edges, turn_root_id, hook.node_id):
                    diagnostics.append("waited async hook lacks a typed fork path from the turn root")
                    response_exact = False
    return _HookDependencyExactness(dag=dag_exact, response=response_exact)


def _reachable(edges: dict[str, set[str]], source: str, target: str) -> bool:
    pending = [source]
    seen: set[str] = set()
    while pending:
        node_id = pending.pop()
        if node_id == target:
            return True
        if node_id in seen:
            continue
        seen.add(node_id)
        pending.extend(edges.get(node_id, ()))
    return False


def _critical_paths(
    nodes: list[_ResolvedNode],
    slices: tuple[TimeSlice, ...],
    dependency: _DependencyProof,
    *,
    cancel_event: Event | None,
) -> _CriticalPaths:
    interval_maps = _critical_path_interval_maps(nodes, slices, cancel_event=cancel_event)
    compute_intervals, response_intervals = interval_maps.compute, interval_maps.response
    terminal_id = dependency.response_terminal_id
    if terminal_id is not None:
        compute_intervals[terminal_id] = []
        response_intervals[terminal_id] = [
            (item.start_ns, item.end_ns) for item in slices if item.operation_id is None and item.response_weight
        ]
    compute = _longest_interval_path(
        compute_intervals,
        dependency.edges,
        parents=dependency.parents,
        fork_edges=dependency.fork_edges,
        cancel_event=cancel_event,
    )
    if dependency.turn_root_id is None or terminal_id is None:
        response = _PathResolution(None, compute.acyclic, True)
        response_reachable = False
    else:
        response = _longest_interval_path(
            response_intervals,
            dependency.edges,
            parents=dependency.parents,
            fork_edges=dependency.fork_edges,
            root_id=dependency.turn_root_id,
            terminal_id=terminal_id,
            cancel_event=cancel_event,
        )
        response_reachable = _reachable(dependency.edges, dependency.turn_root_id, terminal_id)
    return _CriticalPaths(
        compute_ns=compute.value,
        response_ns=response.value,
        acyclic=compute.acyclic and response.acyclic,
        response_reachable=response_reachable,
        compute_bounded=compute.bounded,
        response_bounded=response.bounded,
    )


@dataclass(frozen=True, slots=True)
class _CriticalPathIntervals:
    """Each node's slice intervals that weigh on the compute and on the response critical path."""

    compute: dict[str, list[Interval]]
    response: dict[str, list[Interval]]


def _critical_path_interval_maps(
    nodes: list[_ResolvedNode],
    slices: tuple[TimeSlice, ...],
    *,
    cancel_event: Event | None,
) -> _CriticalPathIntervals:
    compute_intervals: dict[str, list[Interval]] = {node.node_id: [] for node in nodes}
    response_intervals: dict[str, list[Interval]] = {node.node_id: [] for node in nodes}
    for item in slices:
        _check_cancelled(cancel_event)
        if item.operation_id is None:
            continue
        candidates = [node for node in nodes if node.operation_id == item.operation_id]
        if len(candidates) != 1:
            candidates = [
                node
                for node in candidates
                if (attributes := _slice_attributes(node)) is not None and attributes.owner == item.owner
            ]
        if len(candidates) != 1:
            continue
        node_id = candidates[0].node_id
        if item.compute_weight:
            compute_intervals[node_id].append((item.start_ns, item.end_ns))
        if item.response_weight:
            response_intervals[node_id].append((item.start_ns, item.end_ns))
    return _CriticalPathIntervals(compute_intervals, response_intervals)


def _failed_tool_cp_contributions(
    nodes: list[_ResolvedNode],
    slices: tuple[TimeSlice, ...],
    dependency: _DependencyProof,
    *,
    response_cp: int,
    cancel_event: Event | None,
) -> dict[str, int]:
    groups = {
        node.operation_id: frozenset((node.node_id,))
        for node in nodes
        if node.family == "tool.operation" and tool_failed(_payload_str(node.finish.payload, "outcome"))
    }
    # Waiting on the user inside a failed call (its approval, or a question
    # nobody answered) is not failed work; the approval rule reports approvals.
    waits_on_user = frozenset(
        node.node_id
        for node in nodes
        if (node.family == "wait" and _payload_str(node.start.payload, "category") == WaitCategory.USER_INPUT)
        or ((attributes := _slice_attributes(node)) is not None and attributes.owner == "approval")
    )
    return {
        operation_id: contribution
        for operation_id, contribution in _grouped_cp_contributions(
            nodes,
            slices,
            dependency,
            groups=groups,
            kept=waits_on_user,
            response_cp=response_cp,
            cancel_event=cancel_event,
        ).items()
        if contribution
    }


def _server_tool_cp_contributions(
    intermediate: _Intermediate,
    nodes: list[_ResolvedNode],
    slices: tuple[TimeSlice, ...],
    dependency: _DependencyProof,
    *,
    response_cp: int,
    cancel_event: Event | None,
) -> dict[str, int]:
    grouped: dict[str, set[str]] = defaultdict(set)
    tool_nodes = intermediate.nodes.get("tool.operation", {})
    for node in nodes:
        _check_cancelled(cancel_event)
        if node.family != "tool.operation":
            continue
        raw_node = tool_nodes.get(node.operation_id)
        context = _tool_context_for_start(raw_node, node.start) if raw_node is not None else None
        if context is not None and context.server_name is not None:
            grouped[context.server_name].add(node.node_id)
    return _grouped_cp_contributions(
        nodes,
        slices,
        dependency,
        groups={name: frozenset(node_ids) for name, node_ids in grouped.items()},
        response_cp=response_cp,
        cancel_event=cancel_event,
    )


def _grouped_cp_contributions(
    nodes: list[_ResolvedNode],
    slices: tuple[TimeSlice, ...],
    dependency: _DependencyProof,
    *,
    groups: dict[str, frozenset[str]],
    kept: frozenset[str] = frozenset(),
    response_cp: int,
    cancel_event: Event | None,
) -> dict[str, int]:
    """Resolve one leave-one-group-out response path per supplied group.

    Nodes in ``kept`` stay on the path when their ancestor leaves it.
    """
    terminal_id = dependency.response_terminal_id
    root_id = dependency.turn_root_id
    if not groups or root_id is None or terminal_id is None or response_cp <= 0:
        return {}
    response_intervals = _critical_path_interval_maps(nodes, slices, cancel_event=cancel_event).response
    response_intervals[terminal_id] = [
        (item.start_ns, item.end_ns) for item in slices if item.operation_id is None and item.response_weight
    ]
    children: dict[str, set[str]] = defaultdict(set)
    for child, parent in dependency.parents.items():
        children[parent].add(child)
    contributions: dict[str, int] = {}
    for group, node_ids in groups.items():
        _check_cancelled(cancel_event)
        without = dict(response_intervals)
        # A member's contribution spans everything it displaced: the approval
        # and sub-agent inside a tool left with it, unless the caller keeps them.
        for node_id in node_ids:
            without[node_id] = []
            for descendant in _descendants(node_id, children) - kept:
                without[descendant] = []
        resolved = _longest_interval_path(
            without,
            dependency.edges,
            parents=dependency.parents,
            fork_edges=dependency.fork_edges,
            root_id=root_id,
            terminal_id=terminal_id,
            cancel_event=cancel_event,
        )
        if resolved.value is not None and resolved.acyclic and resolved.bounded:
            contributions[group] = max(0, response_cp - resolved.value)
    return contributions


@dataclass(frozen=True, slots=True)
class _WallPartition:
    """A turn's wall-clock time split into buckets, and where it was idle."""

    durations: dict[WallBucket, int]
    idle: tuple[Interval, ...]


def _wall_partition(
    bounds: Interval,
    slices: tuple[TimeSlice, ...],
    *,
    cancel_event: Event | None,
) -> _WallPartition:
    points = {bounds[0], bounds[1]}
    changes: dict[int, Counter[WallBucket]] = defaultdict(Counter)
    for item in slices:
        _check_cancelled(cancel_event)
        if item.wall_bucket is None:
            continue
        start = max(bounds[0], item.start_ns)
        end = min(bounds[1], item.end_ns)
        if end <= start:
            continue
        points.add(start)
        points.add(end)
        changes[start][item.wall_bucket] += 1
        changes[end][item.wall_bucket] -= 1
    ordered = sorted(points)
    durations = dict.fromkeys(WallBucket, 0)
    idle: list[Interval] = []
    active_counts: Counter[WallBucket] = Counter()
    for left, right in pairwise(ordered):
        _check_cancelled(cancel_event)
        for bucket, delta in changes.get(left, {}).items():
            count = active_counts[bucket] + delta
            if count > 0:
                active_counts[bucket] = count
            else:
                active_counts.pop(bucket, None)
        if right <= left:
            continue
        bucket = max(active_counts, key=lambda value: _WALL_PRIORITY[value]) if active_counts else WallBucket.IDLE
        durations[bucket] += right - left
        if bucket is WallBucket.IDLE:
            idle.append((left, right))
    return _WallPartition(durations, interval_union(idle))


def _descendants(node_id: str, children: dict[str, set[str]]) -> set[str]:
    result: set[str] = set()
    pending = list(children.get(node_id, set()))
    while pending:
        child = pending.pop()
        if child in result:
            continue
        result.add(child)
        pending.extend(children.get(child, set()))
    return result


def _has_cycle(children: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        if any(visit(child) for child in children.get(node_id, set())):
            return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in children)
