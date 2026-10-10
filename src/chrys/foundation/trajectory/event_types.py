# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Catalog of trajectory event types and closed enumerations.

Readers skip (and count) event types outside :data:`KNOWN_EVENT_TYPES`. A value
outside one of the enumerations below is never a reason to drop an event: it is
decoded and handed on as written, and a consumer that groups by it shows it as
``unknown`` rather than losing the row. Reason / error / failure fields carry
these codes only — never free text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class EventType:
    """Every event type the writer may emit."""

    # Coverage / runtime bookkeeping.
    COVERAGE_STARTED = "trajectory.coverage.started"
    COVERAGE_ENDED = "trajectory.coverage.ended"
    GAP = "trajectory.gap"
    CHECKPOINT = "trajectory.checkpoint"
    RUNTIME_STARTED = "trajectory.runtime.started"
    RUNTIME_RECOVERED = "trajectory.runtime.recovered"
    RUNTIME_FINISHED = "trajectory.runtime.finished"
    SEGMENT = "event.segment"

    # Session lifecycle.
    SESSION_STARTED = "session.started"
    SESSION_FORKED = "session.forked"
    SESSION_ROLLBACK = "session.rollback"
    BRANCH_SUPERSEDED = "branch.superseded"
    PROFILE_SWITCHED = "profile.switched"

    # Turn lifecycle.
    TURN_STARTED = "turn.started"
    TURN_SUSPENDED = "turn.suspended"
    TURN_RESUMED = "turn.resumed"
    TURN_FINISHED = "turn.finished"
    TURN_RESPONSE_SETTLED = "turn.response_settled"

    WORKFLOW_RUN_STARTED = "workflow.run.started"
    WORKFLOW_RUN_FINISHED = "workflow.run.finished"
    WORKFLOW_NODE_STARTED = "workflow.node.started"
    WORKFLOW_NODE_FINISHED = "workflow.node.finished"

    # Admission / dispatch preparation. ``preparation.state`` is a marker,
    # not a lifecycle terminal.
    PREPARATION_STARTED = "preparation.started"
    PREPARATION_FINISHED = "preparation.finished"
    PREPARATION_STATE = "preparation.state"

    # Model execution.
    MODEL_RUN_STARTED = "model.run.started"
    MODEL_RUN_FINISHED = "model.run.finished"
    MODEL_CYCLE_STARTED = "model.cycle.started"
    MODEL_CYCLE_FINISHED = "model.cycle.finished"
    MODEL_EXCHANGE_STARTED = "model.exchange.started"
    MODEL_EXCHANGE_FINISHED = "model.exchange.finished"
    MODEL_REQUEST_PREPARED = "model.request.prepared"
    MODEL_REQUEST_HEADERS_RECEIVED = "model.request.headers_received"
    MODEL_VALIDATION_FINISHED = "model.validation.finished"

    # Links, retries.
    OPERATION_LINK_OBSERVED = "operation.link.observed"
    RETRY_SCHEDULED = "retry.scheduled"
    RETRY_STARTED = "retry.started"

    # Tools, approvals, interrupts.
    TOOL_OPERATION_STARTED = "tool.operation.started"
    TOOL_OPERATION_FINISHED = "tool.operation.finished"
    TOOL_PAYLOAD_OBSERVED = "tool.payload.observed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    INTERRUPT_REQUESTED = "interrupt.requested"
    INTERRUPT_OBSERVED = "interrupt.observed"

    # Context revisions, hosted calls, compaction.
    CONTEXT_REVISION_RECORDED = "context.revision.recorded"
    HOSTED_CALL_OBSERVED = "hosted.call.observed"
    COMPACTION_STARTED = "compaction.started"
    COMPACTION_PHASE_FINISHED = "compaction.phase.finished"
    COMPACTION_FINISHED = "compaction.finished"
    COMPACTION_SKIPPED = "compaction.skipped"

    # Sub-agents, waits, hooks, mutation summary.
    SUB_AGENT_STARTED = "sub_agent.started"
    SUB_AGENT_FINISHED = "sub_agent.finished"
    WAIT_STARTED = "wait.started"
    WAIT_FINISHED = "wait.finished"
    HOOK_OPERATION_STARTED = "hook.operation.started"
    HOOK_OPERATION_FINISHED = "hook.operation.finished"
    TOOL_MUTATION_BATCH_SUMMARY = "tool.mutation_batch.summary"


KNOWN_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    value for name, value in vars(EventType).items() if not name.startswith("_") and isinstance(value, str)
)

UNKNOWN: Final = "unknown"


class WorkflowNodeOutcome(StrEnum):
    """Terminal outcome of a workflow node attempt; retry waits are outside the attempt."""

    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"


class CoverageReason:
    """``trajectory.coverage.started.coverage_reason``."""

    SESSION_STARTED = "session_started"
    RUNTIME_RESUMED = "runtime_resumed"
    FEATURE_INTRODUCED = "feature_introduced"


class GapReason:
    """``trajectory.gap.reason``."""

    WRITE_TIMEOUT = "write_timeout"
    WRITE_FAILURE = "write_failure"
    LINE_BUDGET_EXCEEDED = "line_budget_exceeded"
    VALUE_OUT_OF_RANGE = "value_out_of_range"
    ENCODE_FAILURE = "encode_failure"
    DEGRADED_CLOSE = "degraded_close"
    RECOVERED_UNREADABLE = "recovered_unreadable"
    """Slots an existing file spent on lines no reader can show (found at activation)."""


class RuntimeFinishReason:
    """``trajectory.runtime.finished.reason``."""

    GRACEFUL_SHUTDOWN = "graceful_shutdown"
    SESSION_SWITCH = "session_switch"


class TurnEndReason:
    """``turn.finished.end_reason`` — terminal states only."""

    COMPLETED = "completed"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    PROCESS_EXIT = "process_exit"


class CompactionSkipReason:
    """``compaction.skipped.reason_code`` — why usage past the trigger started no pass."""

    DISABLED = "disabled"
    """The agent's profile turns automatic compaction off."""
    TURNS_UNRESOLVED = "turns_unresolved"
    """The request's messages resolved to no turn, so there is nothing to compact against."""


class TurnSuspendReason:
    """``turn.suspended.reason``."""

    AWAITING_SUB_AGENTS = "awaiting_sub_agents"


class ExchangeOutcome:
    """``model.exchange.finished.outcome``."""

    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    STALLED = "stalled"
    INTERRUPTED = "interrupted"


class ValidationOutcome:
    """``model.validation.finished.outcome``."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ValidationReason:
    """``model.validation.finished.reason_code`` — why a response was rejected."""

    NO_ASSISTANT_MESSAGE = "no_assistant_message"
    EMPTY_CONTENTS = "empty_contents"
    OUTPUT_TRUNCATED = "output_truncated"
    REASONING_ONLY = "reasoning_only"
    HOSTED_EVIDENCE_ONLY = "hosted_evidence_only"
    CONTENT_FILTERED = "content_filtered"
    WHITESPACE_ONLY = "whitespace_only"
    LEAKED_TOOL_CALL = "leaked_tool_call"
    UNNAMED_TOOL_CALL = "unnamed_tool_call"
    RULE_VIOLATION = "rule_violation"
    UNKNOWN = "unknown"


class ToolOutcome:
    """``tool.operation.finished.outcome``."""

    SUCCESS = "success"
    FAILED = "failed"
    ERRORED = "errored"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    REJECTED = "rejected"
    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    FILTERED = "filtered"
    UNKNOWN = "unknown"


class TraceCoverage:
    """``sub_agent.started.trace_coverage``."""

    FULL = "full"
    BOUNDARY_ONLY = "boundary_only"
    UNAVAILABLE = "unavailable"


class WaitCategory:
    """``wait.started.category``."""

    SUB_AGENT_CONCURRENCY = "sub_agent_concurrency"
    USER_INPUT = "user_input"
    MCP_CONNECT = "mcp_connect"
    RATE_LIMIT = "rate_limit"
    INPUT_ADMISSION = "input_admission"
    TOOL_ADMISSION = "tool_admission"


class ProfileKind:
    """``profile.switched.kind``."""

    AGENT = "agent"
    MODEL = "model"


class SourceRefKind:
    """``source_ref.kind`` for derived summaries."""

    SESSION_CHECKPOINT = "session_checkpoint"
    SUB_AGENT_LOG = "sub_agent_log"
    COMPRESSED_BLOCK = "compressed_block"


class RetryMode:
    """``retry.scheduled.retry_mode`` / ``retry.started.retry_mode``."""

    WIRE = "wire"
    """A wire attempt inside one model cycle is replayed (new exchange operation)."""
    STALL_FALLBACK = "stall_fallback"
    """A stalled stream is abandoned and re-acquired as a blocking exchange."""
    RUN = "run"
    """The whole model run is retried by the service-side retry loop."""
    CONNECTION = "connection"
    """An external agent connection/setup attempt is retried."""
    COMPACTION = "compaction"
    """A compaction side call is retried without replaying the model run."""
    VALIDATION = "validation"
    """The response was rejected by validation and the request is re-issued."""
    CONTEXT_OVERFLOW = "context_overflow"
    """The provider found the context window full; the request is re-issued once after compacting."""


class RetryReason:
    """``retry.scheduled.reason_code``."""

    TRANSIENT_ERROR = "transient_error"
    STREAM_STALL = "stream_stall"
    VALIDATION_REJECTED = "validation_rejected"
    RATE_LIMITED = "rate_limited"
    CONNECTION = "connection"
    UNKNOWN = "unknown"


class ContinuationMode:
    """``model.exchange.*.continuation_mode``."""

    NONE = "none"
    """A fresh create request."""
    POLL = "poll"
    """A retrieval of an already-created background response."""


class ModelRunEndReason:
    """``model.run.finished.outcome``."""

    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
