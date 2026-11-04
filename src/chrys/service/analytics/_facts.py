# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact ingest-owned facts for trajectory analytics."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Final, NamedTuple, cast

from chrys.foundation.trajectory.envelope import ActorKind, SegmentedField, TrajectoryEvent
from chrys.foundation.trajectory.event_types import EventType, ExchangeOutcome
from chrys.foundation.trajectory.sequences import SequenceRange
from chrys.service.analytics.model import CorruptLineDiagnostic, UnsupportedLineDiagnostic, UsageBucket
from chrys.service.analytics.reader import PrefixViolation, ScanBatch
from chrys.service.analytics.reader import plain_int_value as _plain_int_value

WORKFLOW_RUN_FAMILY: Final = "workflow.run"
WORKFLOW_NODE_FAMILY: Final = "workflow.node"

_START_FAMILIES: Final = {
    EventType.WORKFLOW_RUN_STARTED: WORKFLOW_RUN_FAMILY,
    EventType.WORKFLOW_NODE_STARTED: WORKFLOW_NODE_FAMILY,
    EventType.MODEL_RUN_STARTED: "model.run",
    EventType.MODEL_CYCLE_STARTED: "model.cycle",
    EventType.MODEL_EXCHANGE_STARTED: "model.exchange",
    EventType.TOOL_OPERATION_STARTED: "tool.operation",
    EventType.COMPACTION_STARTED: "compaction",
    EventType.SUB_AGENT_STARTED: "sub_agent",
    EventType.WAIT_STARTED: "wait",
    EventType.HOOK_OPERATION_STARTED: "hook.operation",
    EventType.PREPARATION_STARTED: "preparation",
}
_FINISH_FAMILIES: Final = {
    EventType.WORKFLOW_RUN_FINISHED: WORKFLOW_RUN_FAMILY,
    EventType.WORKFLOW_NODE_FINISHED: WORKFLOW_NODE_FAMILY,
    EventType.MODEL_RUN_FINISHED: "model.run",
    EventType.MODEL_CYCLE_FINISHED: "model.cycle",
    EventType.MODEL_EXCHANGE_FINISHED: "model.exchange",
    EventType.TOOL_OPERATION_FINISHED: "tool.operation",
    EventType.COMPACTION_FINISHED: "compaction",
    EventType.SUB_AGENT_FINISHED: "sub_agent",
    EventType.WAIT_FINISHED: "wait",
    EventType.HOOK_OPERATION_FINISHED: "hook.operation",
    EventType.PREPARATION_FINISHED: "preparation",
}
_PAYLOAD_KEYS: Final = frozenset(
    {
        "abandoned",
        "workflow",
        "node",
        "activation",
        "attempt",
        "kind",
        "run_id",
        "approval_request_id",
        "argument_fingerprint",
        "agent_profile",
        "attempt_index",
        "batch_index",
        "branch_id",
        "call_item_id",
        "category",
        "continuation_mode",
        "create",
        "context_revision_id",
        "delay_ms",
        "delivery",
        "detach",
        "drain_scope",
        "drained_scopes",
        "duration_ms",
        "end_reason",
        "exit_code",
        "execution_mode",
        "files_touched",
        "final_exchange_operation_id",
        "hook_event",
        "hook_id",
        "hook_key",
        "is_checkpoint",
        "is_retry",
        "item_count",
        "last_sequence",
        "membership_hash",
        "next_operation_id",
        "next_exchange_operation_id",
        "new_branch_id",
        "net_zero_count",
        "outcome",
        "old_branch_id",
        "parent_model_operation_id",
        "parent_revision_id",
        "phase",
        "previous_operation_id",
        "previous_run_operation_id",
        "preparation_scope_operation_id",
        "reason",
        "reason_code",
        "retry_mode",
        "result_item_id",
        "result_carrier_item_id",
        "revision_id",
        "scope",
        "server_name",
        "source_ref",
        "superseded_by",
        "superseded_from_sequence",
        "superseded_to_sequence",
        "target_operation_id",
        "target_turn_id",
        "target_tool_operation_id",
        "tool_kind",
        "tool_name",
        "trace_coverage",
        "turn_number",
        "modify",
        "delete",
        "unidentified_item_count",
        "untokenized_item_count",
        "waited_hook_operation_count",
        "wait_ms",
    }
)
_PAYLOAD_IDENTITY_KEYS: Final = _PAYLOAD_KEYS - {"membership_hash"}
type _Payload = tuple[object, ...]

_EMPTY_PAYLOAD: Final[_Payload] = ()
_HOOK_EXECUTION_MODES: Final = frozenset({"blocking", "async", "fire_and_forget"})


@dataclass(frozen=True, slots=True)
class _EventScope:
    runtime_id: str
    branch_id: str
    coverage_id: str
    actor_id: str | None
    turn_id: str | None


class _Link(NamedTuple):
    """A pointer an event declares to another operation, and how they relate."""

    relation: str
    target_operation_id: str


@dataclass(frozen=True, slots=True)
class _Endpoint:
    event_id: str | None
    sequence: int
    scope: _EventScope
    monotonic_ns: int
    parent_operation_id: str | None
    side_call: bool
    payload: _Payload
    links: tuple[_Link, ...]
    segmented_fields: tuple[SegmentedField, ...]
    monotonic_measurement: bool

    @property
    def runtime_id(self) -> str:
        return self.scope.runtime_id

    @property
    def branch_id(self) -> str:
        return self.scope.branch_id

    @property
    def coverage_id(self) -> str:
        return self.scope.coverage_id

    @property
    def actor_id(self) -> str | None:
        return self.scope.actor_id

    @property
    def turn_id(self) -> str | None:
        return self.scope.turn_id


@dataclass(slots=True)
class _Node:
    family: str
    operation_id: str
    starts: tuple[_Endpoint, ...] = ()
    finishes: tuple[_Endpoint, ...] = ()
    extras: _ToolOperationExtras | None = None


class _LifecycleCut(Enum):
    """How a uniquely paired raw lifecycle intersects the active projection."""

    NONE = "none"
    START_SURVIVES = "start_survives"
    FINISH_SURVIVES = "finish_survives"


_TOOL_PAYLOAD_TRUNCATED_BIT: Final = 1
_TOOL_PAYLOAD_SPILLED_BIT: Final = 2
_TOOL_PAYLOAD_TRUNCATED_UNKNOWN_BIT: Final = 4
_TOOL_PAYLOAD_SPILLED_UNKNOWN_BIT: Final = 8


@dataclass(frozen=True, slots=True)
class _ToolContextExtras:
    sequence: int
    server_name: str | None
    remote_name: str | None
    skill_name: str | None
    skill_revision: str | None
    script_name: str | None
    resource_name: str | None


@dataclass(frozen=True, slots=True)
class _ToolPayloadExtras:
    sequence: int
    scope: _EventScope
    model_visible_bytes: int | None
    local_token_estimate: int | None
    original_bytes: int | None
    flags: int

    @property
    def truncated(self) -> bool | None:
        if self.flags & _TOOL_PAYLOAD_TRUNCATED_UNKNOWN_BIT:
            return None
        return bool(self.flags & _TOOL_PAYLOAD_TRUNCATED_BIT)

    @property
    def spilled(self) -> bool | None:
        if self.flags & _TOOL_PAYLOAD_SPILLED_UNKNOWN_BIT:
            return None
        return bool(self.flags & _TOOL_PAYLOAD_SPILLED_BIT)


@dataclass(slots=True)
class _ToolOperationExtras:
    contexts: tuple[_ToolContextExtras, ...] = ()
    payloads: tuple[_ToolPayloadExtras, ...] = ()


# Bit-packed per-exchange facts; the scan retains one record per exchange for
# the whole session, so this stays within the resident-memory acceptance gate.
_USAGE_PROVENANCE_BITS: Final[dict[UsageBucket, int]] = {
    UsageBucket.INPUT: 1,
    UsageBucket.OUTPUT: 2,
    UsageBucket.REASONING: 4,
    UsageBucket.CACHE_READ: 8,
    UsageBucket.CACHE_CREATION: 16,
}
_USAGE_NORMALIZATION_UNAVAILABLE_BIT: Final = 32
# A request that failed, stalled or was interrupted may have reported nothing,
# or only what the provider said before the end (a cut-off stream's input and a
# placeholder output). Totals count what it reported in full and say they are a
# lower bound instead of going missing or reading as exact.
_USAGE_UNREPORTED_FAILURE_BIT: Final = 64
_USAGE_FAILED_BIT: Final = 128
_UNREPORTED_FAILURE_USAGE_REASON: Final = "one or more model requests failed or were interrupted before reporting full usage, so totals count only what was reported"

_USAGE_BUCKET_KEYS: Final[dict[UsageBucket, str]] = {
    UsageBucket.INPUT: "input_total",
    UsageBucket.OUTPUT: "output_total",
    UsageBucket.REASONING: "reasoning",
    UsageBucket.CACHE_READ: "cache_read",
    UsageBucket.CACHE_CREATION: "cache_creation",
}


@dataclass(frozen=True, slots=True)
class _ExchangeUsageExtras:
    reasoning: int | None
    cache_read: int | None
    cache_creation: int | None


@dataclass(frozen=True, slots=True)
class _ExchangeUsage:
    sequence: int
    input_total: int | None
    output_total: int | None
    extras: _ExchangeUsageExtras | None
    flags: int

    @property
    def normalization_unavailable(self) -> bool:
        return bool(self.flags & _USAGE_NORMALIZATION_UNAVAILABLE_BIT)

    @property
    def unreported_failure(self) -> bool:
        return bool(self.flags & _USAGE_UNREPORTED_FAILURE_BIT)

    @property
    def failed(self) -> bool:
        return bool(self.flags & _USAGE_FAILED_BIT)

    def bucket_value(self, bucket: UsageBucket) -> int | None:
        if bucket is UsageBucket.INPUT:
            return self.input_total
        if bucket is UsageBucket.OUTPUT:
            return self.output_total
        if self.extras is None:
            return None
        if bucket is UsageBucket.REASONING:
            return self.extras.reasoning
        if bucket is UsageBucket.CACHE_READ:
            return self.extras.cache_read
        return self.extras.cache_creation

    def bucket_has_provider_provenance(self, bucket: UsageBucket) -> bool:
        return bool(self.flags & _USAGE_PROVENANCE_BITS[bucket])


@dataclass(slots=True)
class _Turn:
    turn_id: str
    starts: list[_Endpoint] = field(default_factory=list)
    finishes: list[_Endpoint] = field(default_factory=list)
    response_markers: list[_Endpoint] = field(default_factory=list)
    suspended: list[_Endpoint] = field(default_factory=list)
    resumed: list[_Endpoint] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _RevisionEntry:
    item_id: str
    occurrence: int
    position: int
    action: str


@dataclass(frozen=True, slots=True)
class _Segment:
    sequence: int
    runtime_id: str
    branch_id: str
    coverage_id: str
    turn_id: str | None
    field_pointer: str | None
    segment_group_id: str | None
    segment_index: int | None
    segment_count: int | None
    encoding: str | None
    entry_oversized: bool
    entries: tuple[str | _RevisionEntry, ...] | None


@dataclass(slots=True)
class DirtySet:
    """Generation-local invalidation set for live-tail resolution.

    Every event that adds to or changes a turn's projection — its nodes or
    its lifecycle endpoints — must mark that turn dirty; a miss keeps serving
    the cached analysis, which hides in-flight work until some terminal event
    lands on the same turn.
    """

    turn_ids: set[str] = field(default_factory=set)
    full: bool = False


class _BranchSwitch(NamedTuple):
    """The event at *sequence* says *old_branch* was superseded by *new_branch*."""

    sequence: int
    old_branch: str | None
    new_branch: str | None


class _Rollback(NamedTuple):
    """The rollback event at *sequence* moved history from *old_branch* to *new_branch*."""

    sequence: int
    old_branch: str | None
    new_branch: str


class _BackgroundTask(NamedTuple):
    """The sequences of a background task's start and finish events, when recorded."""

    started: int | None = None
    finished: int | None = None


class _Membership(NamedTuple):
    """Whether some evidence lies in the active projection, and whether some was rolled back."""

    active: bool
    inactive: bool


class _Intermediate:
    """Compact facts retained between the physical scan and logical resolves."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.identifiers: dict[str, str] = {}
        self.scopes: dict[tuple[str, str, str, str | None, str | None], _EventScope] = {}
        self.nodes: dict[str, dict[str, _Node]] = defaultdict(dict)
        self.nodes_by_turn: dict[str, list[_Node]] = defaultdict(list)
        self.turns: dict[str, _Turn] = {}
        self.usage_by_turn: dict[str, list[_ExchangeUsage]] = defaultdict(list)
        self.mutation_summaries: list[_Endpoint] = []
        # Background shell tasks by id. A task keeps writing (its log, often
        # files) between its start and finish, outside any tool call.
        self.background_tasks: dict[str, _BackgroundTask] = {}
        self.segments: dict[str, tuple[_Segment, ...]] = {}
        self.context_revisions: dict[str, tuple[_Endpoint, ...]] = {}
        self.context_revision_ids_by_turn: dict[str, list[str]] = defaultdict(list)
        self.rollback_ranges: list[SequenceRange] = []
        self.rollback_errors: list[int] = []
        self.rollback_branch_pairs: list[_Rollback] = []
        self.branch_supersessions: list[_BranchSwitch] = []
        self.explicit_gaps: list[SequenceRange] = []
        self.coverage_starts: dict[str, list[_Endpoint]] = defaultdict(list)
        self.coverage_ends: dict[str, list[_Endpoint]] = defaultdict(list)
        self.runtime_starts: dict[str, list[_Endpoint]] = defaultdict(list)
        self.runtime_recoveries: dict[str, list[_Endpoint]] = defaultdict(list)
        self.runtime_finishes: dict[str, list[_Endpoint]] = defaultdict(list)
        self.unsupported_sequences: list[int] = []
        self.corrupt_after_sequences: list[int] = []
        self.prefix_violations: list[PrefixViolation] = []
        self.corrupt_lines: list[CorruptLineDiagnostic] = []
        self.unsupported_lines: list[UnsupportedLineDiagnostic] = []
        self.torn_tail_bytes = 0
        self.line_count = 0
        self.byte_count = 0
        self.unsupported_event_count = 0
        self.malformed_hook_execution_mode_count = 0
        self.first_turn_started_at: str | None = None
        self.last_turn_finished_at: str | None = None
        self.dirty = DirtySet(full=True)

    def consume(self, event: TrajectoryEvent, _offset: int, _byte_length: int, _line_number: int) -> None:
        endpoint = _endpoint(event, self._identity, self._scope)
        family = _START_FAMILIES.get(event.event_type)
        operation_id = self._identity(event.operation_id)
        if family is not None and operation_id is not None:
            node = self._node(family, operation_id)
            self._index_node(endpoint.turn_id, node)
            node.starts += (endpoint,)
            if event.event_type == EventType.TOOL_OPERATION_STARTED:
                self._consume_tool_context(node, event)
            if (
                family == "hook.operation"
                and _payload_str(endpoint.payload, "execution_mode") not in _HOOK_EXECUTION_MODES
            ):
                self.malformed_hook_execution_mode_count += 1
            if endpoint.turn_id is not None:
                self.dirty.turn_ids.add(endpoint.turn_id)
        family = _FINISH_FAMILIES.get(event.event_type)
        if family is not None and operation_id is not None:
            node = self._node(family, operation_id)
            self._index_node(endpoint.turn_id, node)
            node.finishes += (endpoint,)
            if endpoint.turn_id is not None:
                self.dirty.turn_ids.add(endpoint.turn_id)
        if event.event_type == EventType.TOOL_PAYLOAD_OBSERVED and operation_id is not None:
            self._consume_tool_payload(self._node("tool.operation", operation_id), event, endpoint.scope)
            if endpoint.turn_id is not None:
                self.dirty.turn_ids.add(endpoint.turn_id)
        self._consume_standalone(event, endpoint)

    def _node(self, family: str, operation_id: str) -> _Node:
        family_nodes = self.nodes[family]
        return family_nodes.setdefault(operation_id, _Node(family=family, operation_id=operation_id))

    def _consume_tool_context(self, node: _Node, event: TrajectoryEvent) -> None:
        raw = event.payload.get("tool_context")
        if not isinstance(raw, dict):
            return

        def text_value(key: str) -> str | None:
            value = raw.get(key)
            return self._identity(value) if isinstance(value, str) else None

        context = _ToolContextExtras(
            sequence=event.sequence,
            server_name=text_value("server_name"),
            remote_name=text_value("remote_name"),
            skill_name=text_value("skill_name"),
            skill_revision=text_value("skill_revision"),
            script_name=text_value("script_name"),
            resource_name=text_value("resource_name"),
        )
        if not any(
            (
                context.server_name,
                context.remote_name,
                context.skill_name,
                context.skill_revision,
                context.script_name,
                context.resource_name,
            )
        ):
            return
        extras = node.extras
        if extras is None:
            extras = _ToolOperationExtras()
            node.extras = extras
        extras.contexts += (context,)

    def _consume_tool_payload(self, node: _Node, event: TrajectoryEvent, scope: _EventScope) -> None:
        payload = event.payload
        model_visible_bytes = _usage_int_value(payload.get("model_visible_bytes"))
        local_token_estimate = _usage_int_value(payload.get("local_token_estimate"))
        original_bytes = _usage_int_value(payload.get("original_bytes"))
        flags = 0
        truncated = payload.get("truncated")
        if truncated is True:
            flags |= _TOOL_PAYLOAD_TRUNCATED_BIT
        elif truncated is not False:
            # The writer always emits a boolean leaf; an absent or malformed
            # one must read as unknown, not as an exact "not truncated".
            flags |= _TOOL_PAYLOAD_TRUNCATED_UNKNOWN_BIT
        artifact_id = payload.get("artifact_id")
        if isinstance(artifact_id, str):
            flags |= _TOOL_PAYLOAD_SPILLED_BIT
        elif artifact_id is not None:
            # Persisted envelopes reject malformed ID fields before they
            # reach this scanner.  Keep the unknown state as a defensive
            # boundary for directly constructed trajectory events.
            flags |= _TOOL_PAYLOAD_SPILLED_UNKNOWN_BIT
        extras = node.extras
        if extras is None:
            extras = _ToolOperationExtras()
            node.extras = extras
        extras.payloads += (
            _ToolPayloadExtras(
                sequence=event.sequence,
                scope=scope,
                model_visible_bytes=model_visible_bytes,
                local_token_estimate=local_token_estimate,
                original_bytes=original_bytes,
                flags=flags,
            ),
        )

    def _identity(self, value: str | None) -> str | None:
        if value is None:
            return None
        existing = self.identifiers.get(value)
        if existing is not None:
            return existing
        self.identifiers[value] = value
        return value

    def _scope(
        self,
        runtime_id: str,
        branch_id: str,
        coverage_id: str,
        actor_id: str | None,
        turn_id: str | None,
    ) -> _EventScope:
        key = (runtime_id, branch_id, coverage_id, actor_id, turn_id)
        return self.scopes.setdefault(key, _EventScope(*key))

    def _index_node(self, turn_id: str | None, node: _Node) -> None:
        if turn_id is not None and not any(event.turn_id == turn_id for event in (*node.starts, *node.finishes)):
            self.nodes_by_turn[turn_id].append(node)

    def _consume_standalone(self, event: TrajectoryEvent, endpoint: _Endpoint) -> None:
        event_type = event.event_type
        operation_id = self._identity(event.operation_id)
        turn_id = endpoint.turn_id
        if event_type == EventType.TURN_STARTED and turn_id is not None:
            turn = self.turns.setdefault(turn_id, _Turn(turn_id=turn_id))
            turn.starts.append(endpoint)
            # A brand-new turn recomputes regardless, but a duplicate start on
            # an already-analyzed turn degrades its lifecycle and must evict
            # the cached analysis.
            self.dirty.turn_ids.add(turn_id)
            if self.first_turn_started_at is None:
                self.first_turn_started_at = event.occurred_at
        elif event_type == EventType.TURN_FINISHED and turn_id is not None:
            self.turns.setdefault(turn_id, _Turn(turn_id=turn_id)).finishes.append(endpoint)
            self.last_turn_finished_at = event.occurred_at
            self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.TURN_RESPONSE_SETTLED and turn_id is not None:
            self.turns.setdefault(turn_id, _Turn(turn_id=turn_id)).response_markers.append(endpoint)
            self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.TURN_SUSPENDED and turn_id is not None:
            self.turns.setdefault(turn_id, _Turn(turn_id=turn_id)).suspended.append(endpoint)
            self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.TURN_RESUMED and turn_id is not None:
            self.turns.setdefault(turn_id, _Turn(turn_id=turn_id)).resumed.append(endpoint)
            self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.APPROVAL_REQUESTED:
            request_id = self._identity(_payload_str(endpoint.payload, "approval_request_id"))
            if request_id is not None:
                node = self._node("approval", request_id)
                self._index_node(turn_id, node)
                node.starts += (endpoint,)
                if turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.APPROVAL_RESOLVED:
            request_id = self._identity(_payload_str(endpoint.payload, "approval_request_id"))
            if request_id is not None:
                node = self._node("approval", request_id)
                self._index_node(turn_id, node)
                node.finishes += (endpoint,)
                if turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.RETRY_SCHEDULED:
            retry_id = operation_id or self._identity(_payload_str(endpoint.payload, "next_operation_id"))
            if retry_id is not None:
                node = self._node("retry", retry_id)
                self._index_node(turn_id, node)
                node.starts += (endpoint,)
                if turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.RETRY_STARTED:
            retry_id = operation_id or self._identity(_payload_str(endpoint.payload, "next_operation_id"))
            if retry_id is not None:
                node = self._node("retry", retry_id)
                self._index_node(turn_id, node)
                node.finishes += (endpoint,)
                if turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.COMPACTION_PHASE_FINISHED and operation_id is not None:
            duration = _payload_int(endpoint.payload, "duration_ms")
            if duration is not None:
                start = _derived_start(endpoint, duration)
                node = self._node("compaction.phase", f"{operation_id}:{event.sequence}")
                self._index_node(turn_id, node)
                node.starts += (start,)
                node.finishes += (endpoint,)
                if turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.MODEL_EXCHANGE_FINISHED:
            normalized = event.payload.get("usage")
            usage = normalized.get("normalized") if isinstance(normalized, dict) else None
            if turn_id is not None:
                reasoning = _usage_int_value(usage.get("reasoning")) if isinstance(usage, dict) else None
                cache_read = _usage_int_value(usage.get("cache_read")) if isinstance(usage, dict) else None
                cache_creation = _usage_int_value(usage.get("cache_creation")) if isinstance(usage, dict) else None
                extras = (
                    _ExchangeUsageExtras(reasoning=reasoning, cache_read=cache_read, cache_creation=cache_creation)
                    if reasoning is not None or cache_read is not None or cache_creation is not None
                    else None
                )
                flags = 0
                for bucket, key in _USAGE_BUCKET_KEYS.items():
                    if _usage_measurement_is_provider(event, key):
                        flags |= _USAGE_PROVENANCE_BITS[bucket]
                if isinstance(usage, dict) and usage.get("normalization_unavailable") is True:
                    flags |= _USAGE_NORMALIZATION_UNAVAILABLE_BIT
                input_total = _usage_int_value(usage.get("input_total")) if isinstance(usage, dict) else None
                output_total = _usage_int_value(usage.get("output_total")) if isinstance(usage, dict) else None
                outcome = event.payload.get("outcome")
                if isinstance(outcome, str) and outcome != ExchangeOutcome.SUCCESS:
                    flags |= _USAGE_FAILED_BIT
                    if input_total is None or output_total is None:
                        flags |= _USAGE_UNREPORTED_FAILURE_BIT
                self.usage_by_turn[turn_id].append(
                    _ExchangeUsage(
                        sequence=event.sequence,
                        input_total=input_total,
                        output_total=output_total,
                        extras=extras,
                        flags=flags,
                    )
                )
        elif event_type == EventType.TOOL_MUTATION_BATCH_SUMMARY:
            self.mutation_summaries.append(endpoint)
            self.dirty.full = True
        elif event_type == EventType.CONTEXT_REVISION_RECORDED:
            revision_id = self._identity(_payload_str(endpoint.payload, "revision_id"))
            if revision_id is not None:
                existing = self.context_revisions.get(revision_id, ())
                self.context_revisions[revision_id] = (*existing, endpoint)
                if turn_id is not None:
                    self.context_revision_ids_by_turn[turn_id].append(revision_id)
                # Revision identity is session-global. A second definition can
                # invalidate memberships, dependency graphs, and counter-axis
                # diagnostics belonging to any earlier turn or descendant
                # revision, so turn-local invalidation is not sufficient.
                if existing:
                    self.dirty.full = True
                elif turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)
        elif event_type == EventType.SESSION_ROLLBACK:
            first = _plain_int_value(event.payload.get("superseded_from_sequence"))
            last = _plain_int_value(event.payload.get("superseded_to_sequence"))
            old_branch_value = event.payload.get("old_branch_id")
            new_branch_value = event.payload.get("new_branch_id")
            old_branch = self._identity(old_branch_value if isinstance(old_branch_value, str) else None)
            new_branch = self._identity(new_branch_value if isinstance(new_branch_value, str) else None)
            if (
                first is None
                or last is None
                or first < 1
                or last < first
                or last != event.sequence - 1
                or new_branch is None
                or new_branch != endpoint.branch_id
            ):
                self.rollback_errors.append(event.sequence)
            else:
                self.rollback_ranges.append(SequenceRange(first, last))
                self.rollback_branch_pairs.append(_Rollback(event.sequence, old_branch, new_branch))
            self.dirty.full = True
        elif event_type == EventType.BRANCH_SUPERSEDED:
            branch_value = event.payload.get("branch_id")
            superseded_by_value = event.payload.get("superseded_by")
            self.branch_supersessions.append(
                _BranchSwitch(
                    event.sequence,
                    self._identity(branch_value if isinstance(branch_value, str) else None),
                    self._identity(superseded_by_value if isinstance(superseded_by_value, str) else None),
                )
            )
            self.dirty.full = True
        elif event_type == EventType.GAP:
            first = _plain_int_value(event.payload.get("first_sequence"))
            last = _plain_int_value(event.payload.get("last_sequence"))
            if first is not None and last is not None:
                self.explicit_gaps.append(SequenceRange(first, last))
            self.dirty.full = True
        elif event_type in {EventType.BACKGROUND_TASK_STARTED, EventType.BACKGROUND_TASK_FINISHED}:
            task_id = event.payload.get("task")
            if isinstance(task_id, str):
                task = self.background_tasks.get(task_id, _BackgroundTask())
                if event_type != EventType.BACKGROUND_TASK_STARTED:
                    task = task._replace(finished=endpoint.sequence)
                elif task.started is None:
                    task = task._replace(started=endpoint.sequence)
                self.background_tasks[task_id] = task
        elif event_type == EventType.COVERAGE_STARTED:
            self.coverage_starts[endpoint.coverage_id].append(endpoint)
            self.dirty.full = True
        elif event_type == EventType.COVERAGE_ENDED:
            self.coverage_ends[endpoint.coverage_id].append(endpoint)
            self.dirty.full = True
        elif event_type == EventType.RUNTIME_STARTED:
            self.runtime_starts[endpoint.runtime_id].append(endpoint)
            self.dirty.full = True
        elif event_type == EventType.RUNTIME_RECOVERED:
            self.runtime_recoveries[endpoint.runtime_id].append(endpoint)
            self.dirty.full = True
        elif event_type == EventType.RUNTIME_FINISHED:
            self.runtime_finishes[endpoint.runtime_id].append(endpoint)
            self.dirty.full = True
        elif event_type == EventType.SEGMENT:
            parent_id = event.payload.get("parent_event_id")
            if isinstance(parent_id, str):
                parent_id = cast("str", self._identity(parent_id))
                self.segments[parent_id] = (
                    *self.segments.get(parent_id, ()),
                    _compact_segment(event, endpoint, self._identity),
                )
                if turn_id is not None:
                    self.dirty.turn_ids.add(turn_id)

    def absorb_batch(self, batch: ScanBatch) -> None:
        self.line_count = batch.cursor.line_number
        self.byte_count = batch.cursor.byte_offset
        self.torn_tail_bytes = batch.torn_tail_bytes
        self.unsupported_event_count += batch.unsupported_event_count
        self.unsupported_sequences.extend(batch.unsupported_event_sequences)
        self.corrupt_after_sequences.extend(line.previous_sequence for line in batch.corrupt_lines)
        self.prefix_violations.extend(batch.prefix_violations)
        self.corrupt_lines.extend(
            CorruptLineDiagnostic(
                line_number=line.line_number,
                byte_offset=line.byte_offset,
                byte_length=line.byte_length,
                reason=line.reason,
                after_sequence=line.previous_sequence,
            )
            for line in batch.corrupt_lines
        )
        self.unsupported_lines.extend(
            UnsupportedLineDiagnostic(
                line_number=line.line_number,
                byte_offset=line.byte_offset,
                sequence=line.sequence,
                schema_version=line.schema_version,
            )
            for line in batch.unsupported_lines
        )
        if batch.corrupt_lines or batch.unsupported_event_sequences or batch.prefix_violations:
            self.dirty.full = True

    def mark_resolved(self) -> None:
        """Clear ingest invalidations after one successful logical resolve."""
        self.dirty = DirtySet()


def _endpoint(
    event: TrajectoryEvent,
    identity: Callable[[str | None], str | None],
    scope_for: Callable[[str, str, str, str | None, str | None], _EventScope],
) -> _Endpoint:
    payload_values: list[object] = []
    for key in _PAYLOAD_KEYS:
        if key not in event.payload:
            continue
        value = event.payload[key]
        if isinstance(value, str) and key in _PAYLOAD_IDENTITY_KEYS:
            value = cast("str", identity(value))
        elif isinstance(value, list):
            value = tuple(cast("str", identity(item)) if isinstance(item, str) else item for item in value)
        payload_values.extend((key, value))
    payload = tuple(payload_values) if payload_values else _EMPTY_PAYLOAD
    duration_pointer = "/payload/wait_ms" if event.event_type == EventType.APPROVAL_RESOLVED else "/payload/duration_ms"
    measurement = event.measurements.get(duration_pointer)
    segmented_fields = tuple(
        SegmentedField(
            field_pointer=cast("str", identity(declaration.field_pointer)),
            segment_group_id=cast("str", identity(declaration.segment_group_id)),
            segment_count=declaration.segment_count,
        )
        for declaration in event.segmented_fields
    )
    runtime_id = cast("str", identity(event.runtime_id))
    branch_id = cast("str", identity(event.branch_id))
    coverage_id = cast("str", identity(event.coverage_id))
    actor_id = identity(event.actor.actor_id)
    turn_id = identity(event.turn_id)
    return _Endpoint(
        event_id=identity(event.event_id) if event.segmented_fields else None,
        sequence=event.sequence,
        scope=scope_for(runtime_id, branch_id, coverage_id, actor_id, turn_id),
        monotonic_ns=event.monotonic_ns,
        parent_operation_id=identity(event.parent_operation_id),
        side_call=event.actor.kind == ActorKind.SIDE_CALL,
        payload=payload,
        links=tuple(
            _Link(cast("str", identity(link.relation)), cast("str", identity(link.target_operation_id)))
            for link in event.links
        ),
        segmented_fields=segmented_fields,
        monotonic_measurement=isinstance(measurement, dict) and measurement.get("source") == "monotonic_clock",
    )


def _valid_revision_entry(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"item_id", "occurrence", "position", "action"}:
        return False
    occurrence = _plain_int_value(value.get("occurrence"))
    position = _plain_int_value(value.get("position"))
    return (
        isinstance(value.get("item_id"), str)
        and occurrence is not None
        and occurrence >= 0
        and position is not None
        and position >= 0
        and value.get("action") in {"add", "remove"}
    )


def _compact_segment(
    event: TrajectoryEvent,
    endpoint: _Endpoint,
    identity: Callable[[str | None], str | None],
) -> _Segment:
    entries = event.payload.get("entries")
    field_pointer = event.payload.get("field_pointer")
    segment_group_id = event.payload.get("segment_group_id")
    compact_entries: list[str | _RevisionEntry] = []
    if isinstance(entries, list):
        for value in entries:
            if isinstance(value, str):
                compact_entries.append(cast("str", identity(value)))
                continue
            if not _valid_revision_entry(value):
                compact_entries = []
                entries = None
                break
            compact_entries.append(
                _RevisionEntry(
                    item_id=cast("str", identity(cast("str", value["item_id"]))),
                    occurrence=cast("int", value["occurrence"]),
                    position=cast("int", value["position"]),
                    action=cast("str", identity(cast("str", value["action"]))),
                )
            )
    else:
        entries = None
    return _Segment(
        sequence=endpoint.sequence,
        runtime_id=endpoint.runtime_id,
        branch_id=endpoint.branch_id,
        coverage_id=endpoint.coverage_id,
        turn_id=endpoint.turn_id,
        field_pointer=identity(field_pointer) if isinstance(field_pointer, str) else None,
        segment_group_id=identity(segment_group_id) if isinstance(segment_group_id, str) else None,
        segment_index=_plain_int_value(event.payload.get("segment_index")),
        segment_count=_plain_int_value(event.payload.get("segment_count")),
        encoding=(identity(encoding) if isinstance((encoding := event.payload.get("encoding")), str) else None),
        entry_oversized=event.payload.get("entry_oversized") is True,
        entries=tuple(compact_entries) if entries is not None else None,
    )


def _derived_start(finish: _Endpoint, duration_ms: int) -> _Endpoint:
    return _Endpoint(
        event_id=finish.event_id,
        sequence=finish.sequence,
        scope=finish.scope,
        monotonic_ns=max(0, finish.monotonic_ns - duration_ms * 1_000_000),
        parent_operation_id=finish.parent_operation_id,
        side_call=finish.side_call,
        payload=finish.payload,
        links=(),
        segmented_fields=(),
        monotonic_measurement=finish.monotonic_measurement,
    )


def _active(sequence: int, inactive_ranges: tuple[SequenceRange, ...]) -> bool:
    return not any(first <= sequence <= last for first, last in inactive_ranges)


def _lifecycle_cut(node: _Node, inactive_ranges: tuple[SequenceRange, ...]) -> _LifecycleCut:
    """Classify one raw start/finish pair cut by rollback projection.

    Cardinality is deliberately part of the predicate: missing or duplicate
    endpoints are damaged evidence, not a projection cut, and must continue
    through each consumer's existing diagnostics.
    """
    if len(node.starts) != 1 or len(node.finishes) != 1:
        return _LifecycleCut.NONE
    start_active = _active(node.starts[0].sequence, inactive_ranges)
    finish_active = _active(node.finishes[0].sequence, inactive_ranges)
    if start_active == finish_active:
        return _LifecycleCut.NONE
    return _LifecycleCut.START_SURVIVES if start_active else _LifecycleCut.FINISH_SURVIVES


def _projection_membership(
    endpoints: tuple[_Endpoint, ...] | list[_Endpoint],
    inactive_ranges: tuple[SequenceRange, ...],
) -> _Membership:
    """Return whether *endpoints* contain active and inactive evidence."""
    active = False
    inactive = False
    for endpoint in endpoints:
        if _active(endpoint.sequence, inactive_ranges):
            active = True
        else:
            inactive = True
    return _Membership(active, inactive)


def _closed_sequence_union(ranges: list[SequenceRange]) -> tuple[SequenceRange, ...]:
    """Merge inclusive sequence ranges without dropping a one-slot rollback."""
    ordered = sorted(item for item in ranges if item.last >= item.first)
    merged: list[SequenceRange] = []
    for first, last in ordered:
        if merged and first <= merged[-1].last + 1:
            merged[-1] = SequenceRange(merged[-1].first, max(merged[-1].last, last))
        else:
            merged.append(SequenceRange(first, last))
    return tuple(merged)


def _payload_value(payload: _Payload, key: str) -> object | None:
    for index in range(0, len(payload), 2):
        if payload[index] == key:
            return payload[index + 1]
    return None


def _payload_str(payload: _Payload, key: str) -> str | None:
    value = _payload_value(payload, key)
    return value if isinstance(value, str) and value else None


def _payload_int(payload: _Payload, key: str) -> int | None:
    return _plain_int_value(_payload_value(payload, key))


def _payload_bool(payload: _Payload, key: str) -> bool:
    return _payload_value(payload, key) is True


def _usage_measurement_is_provider(event: TrajectoryEvent, bucket: str) -> bool:
    measurement_value = event.measurements.get(f"/payload/usage/normalized/{bucket}")
    return isinstance(measurement_value, dict) and measurement_value.get("source") == "provider"


def _usage_int_value(value: object) -> int | None:
    normalized = _plain_int_value(value)
    return normalized if normalized is not None and normalized >= 0 else None
