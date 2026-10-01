# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Event type definitions for frontend ↔ backend communication."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal
from uuid import uuid4

from chrys.foundation.i18n import MessageRef
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserQuestion
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.foundation.models.invocations import InvocationOrigin
from chrys.foundation.models.todos import TodoItem
from chrys.foundation.models.workflow_session import (
    WorkflowModelSelection,
    WorkflowPins,
    WorkflowSessionSelection,
    WorkflowTarget,
)

if TYPE_CHECKING:
    # Typing-only import: avoids a runtime dependency from foundation events
    # into service mutations while still letting
    # :class:`RollbackResult.restore_results` advertise
    # the real dataclass it carries.  ``from __future__ import annotations``
    # (top of file) keeps all annotations as strings, so this import never
    # executes at runtime.
    from chrys.service.mutations.types import RestoreResult

AGENT_LOAD_PHASE_MODEL = "model"
AGENT_LOAD_PHASE_RUNTIME = "runtime"
AGENT_LOAD_PHASE_SESSION = "session"
AGENT_LOAD_PHASE_TOOLS = "tools"
AGENT_LOAD_PHASE_SUB_AGENTS = "sub_agents"
AGENT_LOAD_PHASE_MCP = "mcp"
AGENT_LOAD_PHASE_SKILLS = "skills"
AGENT_LOAD_PHASE_AGENT = "agent"

AGENT_LOAD_STATUS_RUNNING = "running"
AGENT_LOAD_STATUS_DONE = "done"
AGENT_LOAD_STATUS_FAILED = "failed"
AGENT_LOAD_STATUS_SKIPPED = "skipped"

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------


@dataclass
class Event:
    """Base class for all events."""

    event_id: str = field(default_factory=lambda: uuid4().hex[:12])
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    session_id: str | None = None


@dataclass(kw_only=True)
class ExecutionChanged(Event):
    """The execution owner changed, including admission and final lease release."""

    snapshot: ExecutionSnapshot


@dataclass(kw_only=True)
class InvocationEvent(Event):
    """An execution or presentation fact with an explicit logical owner.

    The immutable origin is the routing authority. The event envelope retains
    the bus timestamp and session fields used by existing frontend consumers.
    """

    origin: InvocationOrigin
    _sealed: ClassVar[bool] = False

    def __post_init__(self) -> None:
        if not isinstance(self.origin, InvocationOrigin):
            raise ValueError("Cannot route an invocation event without a live origin")
        if self.session_id and self.session_id != self.origin.session_id:
            raise ValueError("Event session does not match its invocation origin")
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        # User action envelopes are mutable; execution facts seal only after
        # their dataclass initializer has populated every inherited field.
        if self._sealed:
            raise FrozenInstanceError(f"cannot assign to field {name!r}")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        raise FrozenInstanceError(f"cannot delete field {name!r}")


# ---------------------------------------------------------------------------
# Frontend → Backend (user actions)
# ---------------------------------------------------------------------------


@dataclass
class UserMessage(Event):
    """User sends a message to the agent."""

    text: str = ""
    # Filled by the backend after prompt hooks, image validation, and image
    # compression succeed. TUI rendering uses this to preview exactly the
    # multimodal content accepted for the model call.
    prepared_contents: list[Any] | None = field(default=None, repr=False, compare=False)
    # Set by frontends that submit while a run is active so the resulting
    # mid-run injection can be correlated and cancelled (``UserInjectCancel``)
    # before the model sees it. ``None`` for ordinary fresh-turn submits.
    injection_id: str | None = None


@dataclass
class UserInterrupt(Event):
    """User interrupts the current agent execution."""


@dataclass
class UserRetry(Event):
    """User retries the last failed/interrupted run from current state.

    When ``text`` is non-empty, it is used as a mid-turn continuation prompt
    and persisted as real user input inside the current turn. An empty retry
    continues from completed history or replays the prior user opener.
    """

    text: str = ""


@dataclass
class UserInject(Event):
    """User injects a prompt while agent is executing (inserted before next model call)."""

    text: str = ""
    injection_id: str | None = None
    """Frontend-assigned id correlating this injection with cancel/result events."""


@dataclass
class UserInjectCancel(Event):
    """User cancels a queued mid-run injection before the model sees it.

    Effective only while the injection identified by ``injection_id`` is
    still pending (queued or in admission); once consumed, the cancel is a
    no-op and a ``consumed=True`` :class:`UserInjectResult` follows.
    """

    injection_id: str = ""


@dataclass
class UserRollback(Event):
    """User requests Chat rollback to keep the first ``target_turn`` turns.

    This command restores conversation/Agent state. Workflow file rollback
    uses Run identities and must never be routed through this Chat command.

    ``target_turn = 0`` means "roll back to the empty pre-session
    state" — all conversation history is discarded and (optionally)
    every file mutation is reverted.  ``target_turn = N >= 1`` means
    "keep turns 1..N and discard everything after"; any file
    mutations introduced by the discarded turns are (optionally)
    reverted.
    """

    target_turn: int = 0
    """Number of turns to keep (0 = session start, N = keep turns 1..N)."""

    relative_turns: int | None = None
    """When set, discard this many latest turns under the backend transition fence.

    This is the authoritative selector for relative commands such as
    ``/rollback 1``; ``target_turn`` is ignored in that case.  Deferring the
    calculation prevents a frontend from publishing a stale absolute target
    when another turn finalizes while the request is being admitted.
    """

    expected_current_turn: int | None = None
    """Picker projection turn that must still be current when rollback is fenced.

    Direct commands leave this unset because relative targets are resolved under
    the backend fence and absolute targets are explicit.  Picker confirmations
    set it so file selections projected from an older conversation cannot be
    applied after another turn completes while the modal remains open.
    """

    expected_conversation_revision: int | None = None
    """Fresh/retry lifecycle revision that must still match the picker projection.

    Unlike ``expected_current_turn``, this advances for a retry or continuation
    that changes history and mutation planning without opening a fresh turn.
    """

    expected_build_generation: int | None = None
    """Runtime build generation that produced the picker preview.

    Workspace and settings rebuilds may replace mutation coordination without
    advancing the conversation. Picker confirmations set this so concrete file
    selections cannot be applied against a different runtime build.
    """

    expected_workspace_cwd: str | None = None
    """Normalized primary workspace cwd that produced the picker preview."""

    revert_changes: bool = False
    """When True, also restore file-system changes from the discarded turns."""

    selected_paths: list[str] | None = None
    """Paths to include when ``revert_changes`` is True.

    Either ``None`` ("revert every path in the plan") or a non-empty
    list restricting the revert to those paths.  The rollback modal
    sends a concrete list when the user leaves files checked, and
    ``None`` when ``revert_changes`` is False.  ``[]`` is not a supported
    input — the modal collapses the "no files checked" case into
    ``revert_changes=False, selected_paths=None``; any empty list arriving
    from a server-side caller is treated as ``None`` by the engine.
    Ignored when ``revert_changes`` is False.
    """


@dataclass
class ApprovalResponse(Event):
    """User responds to an approval request."""

    request_id: str = ""
    approved: bool = False
    reason: str = ""
    modified_args: dict[str, Any] | None = None
    daa_choice: str = ""


@dataclass
class ApprovalCancelled(Event):
    """A pending approval request was abandoned by its backend owner."""

    request_id: str = ""


@dataclass
class ApprovalAutoFulfillBlocked(Event):
    """Frontend tells the backend not to auto-fulfil an approved judge verdict."""

    request_id: str = ""


@dataclass
class AskUserResponse(Event):
    """User responds to an ask_user question."""

    request_id: str = ""
    answers: tuple[AskUserAnswer, ...] | None = None
    cancelled: bool = False


@dataclass
class SleepSkip(Event):
    """User skips a running sleep tool call."""

    call_id: str = ""


@dataclass
class AgentProfileSwitch(Event):
    """User switches to a different agent profile."""

    profile_name: str = ""


@dataclass
class WorkspaceChange(Event):
    """User requests a workspace/cwd change."""

    primary_cwd: str = ""


@dataclass
class ConfigUpdate(Event):
    """User updates a configuration value."""

    key: str = ""
    value: Any = None


@dataclass
class SettingsReload(Event):
    """User updated .env settings — engine should reload Settings and rebuild agent."""


@dataclass
class SetApprovalMode(Event):
    """User changes the approval mode (manual/auto/bypass).

    The engine updates ``ApprovalMiddleware`` and echoes ``ApprovalModeUpdated``
    so the TUI can refresh the badge from the authoritative backend state.
    ``persist`` controls whether the mode is also written as the global default;
    ACP standard session-mode changes are session-scoped and set this false.
    """

    mode: str = ""  # "manual" | "auto" | "bypass"
    persist: bool = True


@dataclass
class SetModelProfile(Event):
    """User switches the active model profile for this session only.

    The engine swaps ``settings.model_profile`` in-memory and soft-restarts the
    agent — no global ``.env`` write — so each session can run a different model
    without colliding on process-wide environment state.  The engine echoes
    ``ModelProfileSwitched`` once the rebuild completes.
    """

    profile_id: str = ""


# ---------------------------------------------------------------------------
# Backend → Frontend (system events)
# ---------------------------------------------------------------------------


@dataclass
class AgentThinking(Event):
    """Agent is thinking (optional thinking content)."""

    text: str | None = None


@dataclass(frozen=True)
class ProvisionalPresentation:
    """Identity of one retractable text segment from a response attempt."""

    attempt_id: str
    segment_id: str


@dataclass
class InvocationStarted(InvocationEvent):
    """A sub-agent invocation has started — binds invocation_id to parent call_id.

    Emitted by ``SubAgentTools._invoke`` immediately after generating the
    ``invocation_id``, before the first LLM call.  Carries both the parent
    tool call's ``call_id`` (generated by :class:`ToolEventMiddleware`) and
    the new ``invocation_id`` so the TUI can deterministically link the
    sub-agent widget that was mounted for the parent call to the sub-agent
    runtime, without relying on first-inner-tool-call arrival order.

    This matters when multiple sub-agents run in parallel: if one stalls
    in a retry backoff while its sibling's inner tool calls start arriving,
    a FIFO linker would assign the wrong widget and route retry banners /
    progress updates to the wrong card.

    ``sub_agent_log_file`` is the writer's immutable safe basename, not a
    claim that the audit is complete. Frontends may retain it from this first
    event and accept the file only after its envelope reaches a terminal
    status; this keeps audit discovery independent of controller creation.

    ``opening_prompt`` carries the actual user text for standalone invocations
    whose input is not already visible in a parent tool card.
    """

    agent_name: str = ""
    tool_name: str = ""
    parent_call_id: str = ""
    sub_agent_log_file: str = ""
    opening_prompt: str = ""


@dataclass
class InvocationMessage(InvocationEvent):
    """Assistant output owned by an invocation; consumers choose its projection."""

    agent_name: str = ""
    text: str = ""
    is_final: bool = True
    is_intermediate: bool = False
    structured_output_completed: bool = False
    presentation: ProvisionalPresentation | None = None


@dataclass
class InvocationPresentationAttemptAccepted(InvocationEvent):
    """Commit provisional sub-agent transcript segments from one attempt."""

    agent_name: str = ""
    attempt_id: str = ""
    segment_ids: tuple[str, ...] = ()


@dataclass
class InvocationPresentationAttemptRejected(InvocationEvent):
    """Retract provisional sub-agent transcript segments from one attempt."""

    agent_name: str = ""
    attempt_id: str = ""


@dataclass
class InvocationToolCallStart(InvocationEvent):
    """A tool call started inside a sub-agent."""

    agent_name: str = ""
    tool_name: str = ""
    tool_kind: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_call_id: str = ""
    provider_status: str = ""


@dataclass
class InvocationToolCallArgsUpdated(InvocationEvent):
    """A sub-agent tool call's arguments changed after its start event."""

    agent_name: str = ""
    tool_name: str = ""
    tool_kind: str = ""
    call_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_call_id: str = ""
    provider_status: str = ""


@dataclass
class InvocationToolCallStatusUpdated(InvocationEvent):
    """A sub-agent tool call's structured lifecycle status changed."""

    agent_name: str = ""
    tool_name: str = ""
    call_id: str = ""
    status: str = ""
    provider_status: str = ""
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvocationToolCallProgress(InvocationEvent):
    """Incremental text or structured progress from a sub-agent tool call."""

    agent_name: str = ""
    tool_name: str = ""
    call_id: str = ""
    lines: list[str] = field(default_factory=list)
    image_contents: list[Any] = field(default_factory=list, repr=False, compare=False)
    snapshot_metadata: dict[str, Any] = field(default_factory=dict)
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_call_id: str = ""
    provider_status: str = ""


@dataclass
class InvocationToolCallResult(InvocationEvent):
    """A tool call completed inside a sub-agent."""

    agent_name: str = ""
    tool_name: str = ""
    call_id: str = ""
    result: str = ""
    image_contents: list[Any] = field(default_factory=list, repr=False, compare=False)
    duration_ms: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    provider_hosted: bool = False
    hosted_family: str = ""
    provider: str = ""
    provider_item_type: str = ""
    provider_call_id: str = ""
    provider_status: str = ""


@dataclass
class InvocationProgress(InvocationEvent):
    """Cumulative progress update from a sub-agent invocation."""

    agent_name: str = ""
    tool_call_count: int = 0
    """Completed inner tool calls for this invocation."""

    total_tokens: int = 0
    """Current context-window tokens reported by the most recent sub-agent LLM call."""

    total_usage_tokens: int = 0
    """Cumulative tokens consumed by this sub-agent invocation across LLM calls."""

    usage_unreported_attempts: int = 0
    """ACP attempts whose terminal response did not report spend."""


@dataclass
class InvocationCompactionStarted(InvocationEvent):
    """Phase-4 LAST_WORDS compaction began inside a sub-agent invocation.

    Mirrors :class:`CompactionStarted` with ``invocation_id`` as the
    routing key so the TUI can show a live compaction line on the
    owning sub-agent card.
    """

    agent_name: str = ""
    compaction_id: str = ""
    phase: str = "phase4"


@dataclass
class InvocationCompactionFinished(InvocationEvent):
    """Phase-4 LAST_WORDS compaction finished inside a sub-agent invocation.

    ``outcome`` matches :class:`CompactionFinished`.  The note text is
    deliberately omitted — sub-agent cards only flip the status line — but
    ``format_violation`` preserves an accepted structured-note violation for
    observers, and ``failure_reason`` carries the safety-limit cause of a
    failed outcome (see :class:`CompactionFinished`).
    """

    agent_name: str = ""
    compaction_id: str = ""
    phase: str = "phase4"
    outcome: str = ""
    duration_ms: int = 0
    format_violation: str = ""
    failure_reason: str = ""


@dataclass
class InvocationCompactionCommitted(InvocationEvent):
    """A Phase-4 compaction round was durably applied inside a sub-agent.

    Follows the finished(``outcome="ok"``) signal for the same
    ``compaction_id`` once the strategy has committed the round — spill
    written (or checkpointed), note set, messages excluded.  A finished-ok
    round whose spill write failed never produces this event, so observers
    that count real compactions (the sub-agent card's "Compactions: N"
    subtitle) count this instead of finished-ok.
    """

    agent_name: str = ""
    compaction_id: str = ""
    phase: str = "phase4"


# --- Sub-agent lifecycle (pause / retry / abort) ---------------------------
#
# These events scope retry and abort to a single sub-agent invocation so
# that, when multiple sub-agents run concurrently, a retry click on one
# paused card does not disturb healthy siblings.  Every event carries
# ``invocation_id`` as its routing key.


@dataclass
class InvocationRetryAttempt(InvocationEvent):
    """Executor is retrying a transient error (published before each retry sleep)."""

    agent_name: str = ""
    message: str = ""
    """Pre-rendered English retained byte-for-byte for compatibility consumers."""
    attempt: int = 0
    """1-based attempt number (the retry about to happen)."""
    max_attempts: int = 0
    delay_seconds: int = 0
    scope: Literal["wire", "run", "compaction", "connection"] = "run"
    """The owning retry lane: wire, whole run, connection, or compaction.  Frontends render compaction retries
    inside the live compaction card instead of a transcript-level error banner."""
    display_message: MessageRef | None = None
    """Locale-neutral display reference when this retry has migrated prose."""
    display_hint: MessageRef | None = None
    """Shown after ``display_message`` (e.g. "seems offline"); never without it."""
    detail: str = ""
    """Raw untranslated diagnostic component separated from a fixed wrapper."""


@dataclass
class InvocationPaused(InvocationEvent):
    """A sub-agent exhausted auto-retry (or hit a non-retryable error) and is
    waiting for the user to decide via Retry or Abort.

    The parent's ``_invoke()`` is still awaiting — the parent tool call
    will only resolve once the user picks a decision (or a global
    interrupt fires).
    """

    agent_name: str = ""
    tool_name: str = ""
    reason: str = ""
    """One of ``stream_stall``, ``last_words``, ``framework_exc``, ``acp_transport``."""
    last_error: str = ""
    last_error_display: MessageRef | None = None
    """What ``last_error`` means to the user; the raw text still follows it."""
    last_error_hint: MessageRef | None = None
    retry_attempts: int = 0
    diagnostic_path: str | None = None
    """UI-only diagnostic file path; never interpolate into model-visible errors."""


@dataclass
class InvocationRetryRequested(Event):
    """User clicked Retry on a paused sub-agent card (frontend → backend)."""

    invocation_id: str = ""


@dataclass
class InvocationAbortRequested(Event):
    """User clicked Abort on a paused sub-agent card (frontend → backend).

    The controller resolves the pending decision to ``"abort"``; the tool
    call returns an ``Error:`` string so the parent agent sees a normal
    tool-failure result and can decide what to do next.
    """

    invocation_id: str = ""


@dataclass
class InvocationResumed(InvocationEvent):
    """The controller restarted the sub-agent after a user Retry (backend → frontend)."""

    agent_name: str = ""


@dataclass
class InvocationCascadeAborted(InvocationEvent):
    """A global :class:`UserInterrupt` tore down this paused/running sub-agent.

    Distinct from :class:`InvocationPaused` with an abort action: here the
    user stopped the whole engine, not just this one sub-agent.
    """

    agent_name: str = ""


@dataclass
class InvocationAborted(InvocationEvent):
    """A failed sub-agent ended without a retry (backend → frontend).

    Emitted by the controller after resolving its ``pending_decision``
    with the user's Abort — or at once, where the caller's surface shows
    no card that could pause — right before the tool call returns an
    ``Error:`` string to the parent.  Distinct from
    :class:`InvocationCascadeAborted` — this one is scoped to a single
    invocation, the parent run keeps going.  The engine listens for this
    event to decrement its paused-invocation set and drive the FSM out
    of ``AWAITING_SUB_AGENTS``.
    """

    agent_name: str = ""
    last_error: str = ""


@dataclass
class ApprovalRequest(Event):
    """Engine requests user approval for a tool call."""

    request_id: str = ""
    call_id: str = ""
    caller_name: str = ""
    tool_name: str = ""
    tool_kind: str = ""
    presentation_kind: str = ""
    """Display-only kind hint for bridged remote (ACP) requests.

    Drives the approval dialog's human header ("Run command", …). Never
    consulted by approval policy or the judge — ``tool_kind`` stays ``""``
    for bridged requests precisely so kind-scoped rules cannot match them.
    """
    args: dict[str, Any] = field(default_factory=dict)
    intent_summary: str = ""
    user_message: str = ""
    workspace_roots: list[str] = field(default_factory=list)
    workspace_cwd: str = ""
    daa_exact: str = ""
    daa_prefix: tuple[str, ...] = ()
    judging: bool = False
    """True when an LLM reviewer is concurrently evaluating this request.

    The TUI shows the "Evaluating" spinner on arrival and waits for an
    ``ApprovalReviewed`` event to update the dialog.
    """


@dataclass
class ApprovalReviewed(Event):
    """Automated reviewer has finished evaluating a pending ``ApprovalRequest``.

    Published only for AUTO-mode requests.  An approved outcome normally
    causes the backend to auto-fulfil the approval after the TUI receives this
    event, unless the frontend publishes ``ApprovalAutoFulfillBlocked`` because
    a user decision is already in flight.  A flagged outcome carries the
    concern so the user can decide.
    """

    request_id: str = ""
    approved: bool = False
    reason: str = ""


@dataclass
class ApprovalModeUpdated(Event):
    """Backend confirms the current approval mode (after a ``SetApprovalMode``
    or after session start).  The TUI uses this as the authoritative source
    for the header badge.
    """

    mode: str = ""  # "manual" | "auto" | "bypass"


@dataclass
class QuestionToUser(Event):
    """Agent asks the user a question."""

    questions: tuple[AskUserQuestion, ...] = ()
    request_id: str = ""
    call_id: str = ""
    caller_name: str = ""


@dataclass
class AskUserTimedOut(Event):
    """An ask_user question expired before the user responded."""

    request_id: str = ""


@dataclass
class ToolCompacted(Event):
    """Intra-turn tool compaction compressed old tool-call groups."""

    compacted_groups: int = 0
    """Number of tool-call groups that were compacted."""

    phase: str = ""
    """Which phase: ``"phase1"`` to ``"phase4"`` (see compaction module docstring)."""

    turn_numbers: list[int] = field(default_factory=list)
    """1-based turn numbers affected (phase 1 only)."""

    compacted_tool_names: list[str] = field(default_factory=list)
    """Tool names from compacted groups (for debugging)."""

    tokens_before: int = 0
    """Estimated token count before compaction."""

    tokens_after: int = 0
    """Estimated token count after compaction."""

    last_words_generated: bool = False
    """Phase 4 only: True when a LAST_WORDS progress note was produced/updated."""


@dataclass
class CompactionStarted(Event):
    """Phase-4 LAST_WORDS compaction began on the main agent.

    Published immediately before the summarization LLM call so frontends
    can surface a live "Compacting conversation…" indicator during the
    otherwise-silent wait (seconds to minutes with retry backoff).
    ``compaction_id`` correlates with :class:`CompactionFinished`.
    """

    compaction_id: str = ""
    phase: str = "phase4"


@dataclass
class CompactionFinished(Event):
    """Phase-4 LAST_WORDS compaction finished on the main agent.

    ``outcome`` is ``"ok"`` (note generated), ``"failed"`` (generation
    exhausted its retry budget), or ``"canceled"`` (the run was
    interrupted mid-generation).  ``last_words`` carries the generated
    note on success so frontends can render it without re-reading backend
    state. ``format_violation`` records a structured-note violation accepted
    when the bounded corrective retry process stops.  ``failure_reason`` is
    a short human-readable cause set on failed outcomes caused by a known
    safety limit (per-turn round limit, side-call spend budget); frontends
    show it on the failure card in place of the duration.
    """

    compaction_id: str = ""
    phase: str = "phase4"
    outcome: str = ""
    duration_ms: int = 0
    last_words: str = ""
    format_violation: str = ""
    failure_reason: str = ""


@dataclass
class InvocationContextPressure(InvocationEvent):
    """Phase 4 disabled after its attempt/progress/spend breaker tripped."""

    reason: str = ""
    attempts: int = 0
    side_call_tokens: int = 0
    side_call_token_budget: int = 0
    source: str = "main"


@dataclass
class ContextCompressed(Event):
    """Context was compressed (folded) at a turn marker."""

    compressed_context_id: str = ""
    summary: str = ""
    freed_messages: int = 0
    turn_range: tuple[int, int] = (0, 0)
    source: str = "agent"
    """What initiated: ``"agent"`` (LLM) or ``"auto"`` (platform)."""


@dataclass
class UsageUpdate(Event):
    """Token usage update."""

    agent_profile: str = ""
    """Agent profile that produced this usage update."""

    usage_source_id: str = ""
    """Stable source id for the agent run that produced this update.

    Main-agent usage uses the session id; sub-agent usage uses the sub-agent
    invocation id.  Profile names are display metadata and are not unique
    enough to distinguish main-agent and sub-agent windows.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    pct: float = 0.0
    max_context_tokens: int = 0
    total_session_tokens: int = 0
    """Cumulative tokens consumed across all LLM calls in this session."""

    total_session_input_tokens: int = 0
    """Cumulative input tokens consumed across all LLM calls in this session."""

    total_session_output_tokens: int = 0
    """Cumulative output tokens consumed across all LLM calls in this session."""

    cache_hit_tokens: int | None = None
    """Cache-read tokens reported by the provider for the last LLM call, or
    ``None`` if no provider in this session has reported a cache field."""

    total_session_cache_hit_tokens: int | None = None
    """Cumulative cache-read tokens across the session, or ``None`` until the
    first cache-aware response."""

    local_tokens: int = 0
    """Local tokenizer estimate of included conversation tokens (excludes system prompt/tools)."""

    calibration_ratio: float = 1.0
    """Ratio between API token count and local estimate (> 1.0 = API sees more due to overhead)."""

    system_overhead_tokens: int = 0
    """Estimated fixed overhead (system prompt + tool definitions), not counted in local_tokens."""


@dataclass
class StateChanged(Event):
    """A session state value changed."""

    key: str = ""
    value: Any = None


@dataclass
class TodoListUpdated(Event):
    """The session todo list was replaced (full-list payload, not a diff)."""

    items: list[TodoItem] = field(default_factory=list)
    source: str = "main"
    """Origin of the update; future: sub-agent name / ``"user"``."""


@dataclass
class UserInjectResult(Event):
    """Reports whether a mid-run user message was delivered to the model.

    consumed=True:  The message was injected before the next model call.
                    The TUI should display it in the chat at this point.
    consumed=False: agent.run() ended before the message could be injected.
                    The TUI should keep the text in the input bar for re-use.

    ``created_at`` is the original user-message timestamp, not this result
    event's publish timestamp.
    """

    text: str = ""
    consumed: bool = False
    created_at: datetime | str | None = None
    injection_id: str | None = None
    """Id of the originating injection when it carried one; frontends use it
    to ignore results for injections they already cancelled or replaced."""


@dataclass
class Error(Event):
    """An error occurred."""

    code: str = ""
    message: str = ""
    """Pre-rendered English retained byte-for-byte for compatibility consumers."""
    recoverable: bool = True
    display_message: MessageRef | None = None
    display_hint: MessageRef | None = None
    """Shown after ``display_message``; never without it."""


@dataclass
class Warning(Event):
    """A non-fatal warning that should be surfaced to the user without disrupting workflow."""

    request_id: str = ""
    """Workflow admission diagnostics route before a run has been accepted."""
    code: str = ""
    message: str = ""
    """Pre-rendered English retained byte-for-byte for compatibility consumers."""
    display_message: MessageRef | None = None


@dataclass
class RuntimeModelDetails:
    """Non-sensitive details about the active model profile."""

    profile_id: str = ""
    name: str = ""
    provider: str = ""
    api_style: str = ""
    model_id: str = ""
    max_context_tokens: int = 0
    base_url: str = ""
    stream: bool = False
    vision: bool = False
    selection_source: Literal["override", "agent", "inherited", "active", "default"] = "active"


@dataclass
class RuntimeSkillDetails:
    """Non-sensitive details about one loaded runtime skill."""

    name: str = ""
    description: str = ""
    source: str = ""


@dataclass
class RuntimeHookDetails:
    """Non-sensitive details about one configured runtime hook."""

    id: str = ""
    event: str = ""
    execution_mode: str = ""
    enabled: bool = True
    description: str = ""


@dataclass
class RuntimeHookSourceDetails:
    """One project or global source contributing runtime hooks."""

    scope: Literal["project", "global"] = "global"
    source_path: str = ""
    hooks: list[RuntimeHookDetails] = field(default_factory=list)


@dataclass
class AgentRuntimeDetails:
    """Grouped runtime metadata for the TUI details dialog."""

    model: RuntimeModelDetails = field(default_factory=RuntimeModelDetails)
    builtin_tools: dict[str, list[str]] = field(default_factory=dict)
    web_search_providers: dict[str, str] = field(default_factory=dict)
    sub_agent_tools: list[str] = field(default_factory=list)
    mcp_tools: dict[str, list[str]] = field(default_factory=dict)
    mcp_failures: dict[str, str] = field(default_factory=dict)
    skill_sources: dict[str, list[str]] = field(default_factory=dict)
    skill_details: list[RuntimeSkillDetails] = field(default_factory=list)
    hook_sources: list[RuntimeHookSourceDetails] = field(default_factory=list)
    memory_sources: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class AgentLoadStarted(Event):
    """Agent infrastructure loading has started."""

    operation: str = ""
    from_profile: str = ""
    to_profile: str = ""
    from_display_name: str = ""
    to_display_name: str = ""


@dataclass
class AgentLoadProgress(Event):
    """Incremental progress while building agent tools/context."""

    phase: str = ""
    message: str = ""
    """Pre-rendered English retained byte-for-byte for compatibility consumers."""
    server_name: str = ""
    """MCP server identifier retained for compatibility."""
    current: int = 0
    total: int = 0
    failed: int = 0
    status: str = ""
    """Closed progress status from the ``AGENT_LOAD_STATUS_*`` vocabulary."""
    subject: str = ""
    """Generic per-item identifier, such as an MCP server or sub-agent profile."""
    detail: str = ""
    """Raw untranslated dynamic reason associated with this progress update."""


@dataclass
class AgentLoadFinished(Event):
    """Agent infrastructure loading has finished successfully."""

    operation: str = ""
    agent_profile: str = ""
    display_name: str = ""


@dataclass
class AgentLoadFailed(Event):
    """Agent infrastructure loading failed before the session became usable."""

    operation: str = ""
    agent_profile: str = ""
    display_name: str = ""
    message: str = ""
    """Pre-rendered English retained byte-for-byte for compatibility consumers."""
    display_message: MessageRef | None = None
    """Locale-neutral display reference for future migrated failure producers."""
    display_hint: MessageRef | None = None
    """Shown after ``display_message``; never without it."""


@dataclass
class ImageAttachmentCompressionStarted(Event):
    """Image attachment compression has started before a user turn is sent."""

    image_count: int = 0


@dataclass
class ImageAttachmentCompressionFinished(Event):
    """Image attachment compression has finished before a user turn is sent."""

    image_count: int = 0


@dataclass
class SessionReady(Event):
    """Session is initialized and ready."""

    agent_profile: str = ""
    display_name: str = ""
    model_profile_id: str = ""
    max_context_tokens: int = 0
    tool_names: list[str] = field(default_factory=list)
    tool_kinds: dict[str, str] = field(default_factory=dict)
    skill_names: list[str] = field(default_factory=list)
    sub_agent_tool_names: list[str] = field(default_factory=list)
    memory_files: list[str] = field(default_factory=list)
    runtime_details: AgentRuntimeDetails = field(default_factory=AgentRuntimeDetails)
    primary_cwd: str = ""
    # All workspace roots (may include the primary cwd) so consumers such as
    # the workspace MRU can record secondary roots without an engine query.
    working_dirs: list[str] = field(default_factory=list)


@dataclass
class AgentRuntimeUpdated(Event):
    """Agent runtime metadata changed without a full agent rebuild.

    ``model_profile_id`` and ``max_context_tokens`` are intentionally
    denormalized from ``runtime_details.model`` for active model and context
    window tracking. ``runtime_details`` carries the complete confirmed
    metadata consumed by runtime confirmation paths.
    """

    model_profile_id: str = ""
    max_context_tokens: int = 0
    tool_names: list[str] = field(default_factory=list)
    skill_names: list[str] = field(default_factory=list)
    memory_files: list[str] = field(default_factory=list)
    runtime_details: AgentRuntimeDetails = field(default_factory=AgentRuntimeDetails)


# ---------------------------------------------------------------------------
# Workspace & history events
# ---------------------------------------------------------------------------


@dataclass
class SessionNew(Event):
    """User requests to start a new session (preserving the current one)."""


@dataclass
class SessionRestore(Event):
    """User requests to restore a saved session."""

    session_id: str = ""
    primary_cwd: str = ""
    profile_name: str = ""
    # Rollback restores have already swapped session.json and must ignore/delete
    # any live crash-recovery sidecar from the pre-rollback state.
    ignore_recovery: bool = False
    apply_saved_model: bool = False
    # Request-time additional working directories (paths), excluding the primary cwd.
    # ``None`` means the caller did not specify roots — keep the saved workspace. A
    # list (possibly empty) is authoritative and replaces the saved additional roots,
    # so ACP clients can narrow, swap, or clear scope on load.
    working_dirs: list[str] | None = None


@dataclass
class SessionDelete(Event):
    """User requests to delete a saved session."""

    session_id: str = ""


@dataclass
class SessionClear(Event):
    """User requests to delete the ACTIVE session and start a fresh one.

    One fenced backend transition: prompt admission is closed for the whole
    operation, the deletion is acknowledged by ``SessionDeleted`` before the
    fresh session starts, and a failed deletion keeps the current session
    intact and reports ``Error(code="session_clear_failed")`` — the fresh
    session is never started in that case.  ``session_id`` must be the
    engine's active session.
    """

    session_id: str = ""


@dataclass
class SessionRestored(Event):
    """A session has been successfully restored (backend → frontend)."""

    session_id: str = ""
    agent_profile: str = ""
    display_name: str = ""
    # Profile shown by the oldest restored messages. This avoids a frontend
    # scan of every saved session merely to recover one history field.
    initial_agent_profile: str = ""
    message_count: int = 0
    cwd_warning: str = ""
    primary_cwd: str = ""
    recovered_from_sidecar: bool = False
    # Restored workspace roots (may include the primary cwd) so consumers
    # such as the workspace MRU can record secondary roots without an
    # engine query.
    working_dirs: list[str] = field(default_factory=list)


@dataclass
class SessionSaved(Event):
    """A session has been auto-saved (backend → frontend, triggers history refresh)."""

    session_id: str = ""


@dataclass
class SessionTitleUpdated(Event):
    """A session's title overlay changed (backend → frontend).

    Published after a title patch lands in ``session.json``: either the
    user saved/cleared a custom title (``custom=True``, ``title`` may be
    empty to fall back to automatic titles) or the post-turn summarizer
    persisted a fresh auto-generated title (``custom=False``).

    ``display_title`` carries the post-update resolved title (custom >
    generated > first-message fallback) so protocol consumers that show a
    single title string (e.g. the ACP bridge) don't clear it while a
    fallback still exists; ``title`` stays the raw patched value for
    consumers that track the overlay fields themselves.
    """

    title: str = ""
    custom: bool = False
    display_title: str = ""


@dataclass
class SessionFork(Event):
    """User requests to fork the current session."""

    session_id: str = ""


@dataclass
class SessionForked(Event):
    """A session has been forked (backend → frontend)."""

    session_id: str = ""
    parent_session_id: str = ""
    new_session_id: str = ""


@dataclass
class RollbackResult(Event):
    """Rollback completed (backend → frontend).

    The TUI should clear its chat view and either replay the restored
    history (for ``target_turn >= 1``) or show the welcome state
    (for ``target_turn == 0``). Frontends may seed their input composer
    from ``rolled_back_user_text`` after the rollback UI refresh.
    """

    session_id: str = ""
    target_turn: int = 0
    """Number of turns kept after the rollback (0 = session start)."""

    rolled_back_user_text: str = ""
    """First user prompt from the discarded turn range, if available."""

    files_reverted: int = 0
    """Count of files that were actually changed on disk by the revert
    (sum of ``r.changed`` across :attr:`restore_results`).  Zero means
    either no revert was requested or every target was already at the
    expected content.  Use ``bool(restore_results)`` to distinguish
    "revert was attempted" from "revert was skipped"."""

    restore_results: list[RestoreResult] = field(default_factory=list)
    """Per-file :class:`chrys.service.mutations.types.RestoreResult`
    entries — one per path the rollback plan targeted.  The import is
    ``TYPE_CHECKING``-only so the events module stays runtime-free of
    a core dependency (see header); consumers import ``RestoreResult``
    directly and use ``r.changed``/``r.ok`` for per-path counts and
    ``r.reason`` for failure text.  The list is never heterogeneous —
    ``MutationTracker.rollback`` is the sole producer."""

    exclusions: list[tuple[str, str]] = field(default_factory=list)
    """``(path, reason)`` pairs for files the rollback plan dropped —
    primitive shapes only (``reason`` is a
    ``RollbackExclusionReason.value`` string like ``"unrestorable"`` /
    ``"move_poisoned"``); the enum lives in the service layer, which
    foundation must not import.  Populated from the pre-built
    ``RollbackPlan`` — the engine builds the plan before executing
    because the rolled-back turns (and with them the exclusions) are
    unreconstructable afterwards.  Empty when no file revert was
    requested."""

    warnings: list[str] = field(default_factory=list)
    """Advisory, repo-level notice strings attached to the rollback
    plan (e.g. another chrys session has an active command in this
    tree).  Per-path hazards are exclusions, never warnings."""


@dataclass
class SessionDeleted(Event):
    """A session has been deleted (backend → frontend)."""

    session_id: str = ""


@dataclass
class ProfileSwitched(Event):
    """A profile switch has completed with history preserved (backend → frontend)."""

    from_profile: str = ""
    to_profile: str = ""
    from_display_name: str = ""
    to_display_name: str = ""
    message_count: int = 0
    model_profile_id: str = ""
    max_context_tokens: int = 0
    tool_names: list[str] = field(default_factory=list)
    skill_names: list[str] = field(default_factory=list)
    sub_agent_tool_names: list[str] = field(default_factory=list)
    memory_files: list[str] = field(default_factory=list)
    runtime_details: AgentRuntimeDetails = field(default_factory=AgentRuntimeDetails)


@dataclass
class ModelProfileSwitched(Event):
    """A model profile switch has completed for this session (backend → frontend)."""

    model_profile_id: str = ""
    max_context_tokens: int = 0
    runtime_details: AgentRuntimeDetails = field(default_factory=AgentRuntimeDetails)


@dataclass
class WorkspaceUpdated(Event):
    """Session workspace has been updated mid-session (backend → frontend)."""

    primary_cwd: str = ""
    working_dirs: list[str] = field(default_factory=list)
    reference_files: list[str] = field(default_factory=list)


@dataclass
class SettingsReloaded(Event):
    """A settings reload (env + registries) has completed (backend → frontend).

    Echoed once the agent has been rebuilt so a caller awaiting the reload can
    distinguish success from an ``AgentLoadFailed`` rebuild failure.
    """

    runtime_details: AgentRuntimeDetails = field(default_factory=AgentRuntimeDetails)


# ---------------------------------------------------------------------------
# Workflow mode
# ---------------------------------------------------------------------------
#
# Lifecycle events (``WorkflowRunStarted`` … ``WorkflowRunFinished``) carry
# ``seq``, the run store's record sequence: they are published only after the
# record is written, in sequence order, so a live consumer applies exactly what
# a replay of ``events.jsonl`` would. The one exception is the
# ``outcome="storage_failed"`` terminal, which is out of band (``seq=None``) and
# reports how far the written prefix reaches (``last_written_seq``). Node events
# carry the activation identity ``run_id + node_id + activation_id + attempt``;
# ``attempt=0`` is the sentinel for pending/skipped states and run notices.
# ``WorkflowRunAccepted`` / ``WorkflowRunRejected`` are control-plane replies
# to a ``WorkflowRunRequest``: never stored, no ``seq``, exactly one per
# ``request_id``.

WORKFLOW_NOTICE_DATA_DROPPED = "data_dropped_at_agent_boundary"
WORKFLOW_OUTPUT_EMIT = "emit"
"""``WorkflowNodeOutput.kind`` of a process fragment published by ``ctx.emit``."""
WORKFLOW_OUTPUT_FINAL = "final"
"""``WorkflowNodeOutput.kind`` of an activation's final value."""


@dataclass
class WorkflowPreviewProgress(Event):
    """Progress of an explicit preview load, correlated to its requesting view."""

    request_id: str = ""
    workflow_id: str = ""
    stage: Literal["definition", "environment", "graph", "ready"] = "definition"
    title: str = ""
    node_count: int = 0


@dataclass(kw_only=True)
class WorkflowRunRequest(Event):
    """User asks to run a discovered workflow (frontend → backend).

    ``target`` fixes the workflow and workspace before admission. A draft creates
    a session; a selection restores its durable session and verifies the binding.
    ``timeout`` above zero is the run-level deadline in seconds: when it passes the run
    is cancelled with ``reason="deadline_exceeded"``.
    """

    target: WorkflowTarget
    session_id: str | None = field(default=None, init=False)
    pins: WorkflowPins | None = None
    input_text: str = ""
    request_id: str = ""
    timeout: float = 0.0

    def __post_init__(self) -> None:
        self.session_id = self.target.session_id if isinstance(self.target, WorkflowSessionSelection) else None


@dataclass(kw_only=True)
class WorkflowModelChangeRequest(Event):
    """Change one idle Workflow session's default model without changing global or Chat settings."""

    session_id: str
    profile_id: str
    request_id: str


@dataclass(kw_only=True)
class WorkflowModelChangeResult(Event):
    """Acknowledge a model change only after the session checkpoint has been saved."""

    request_id: str
    selection: WorkflowSessionSelection | None = None
    error: str = ""


@dataclass(kw_only=True)
class WorkflowRollbackRequest(Event):
    """Preview, or apply a confirmed file-only rollback before a stable Run ID.

    An empty token previews. Commit repeats the returned token; the owner checks
    the ledger and the current safe file plan again under the session lock.
    Run records are never deleted or converted into retryable executions.
    """

    session_id: str
    run_id: str
    request_id: str
    token: str = ""


@dataclass(kw_only=True)
class WorkflowRollbackResult(Event):
    request_id: str
    run_id: str
    token: str = ""
    paths: tuple[str, ...] = ()
    exclusions: tuple[tuple[str, str], ...] = ()
    warnings: tuple[str, ...] = ()
    applied: bool = False
    changed: int = 0
    error: str = ""


@dataclass
class WorkflowCancelRequest(Event):
    """User cancels the running workflow (frontend → backend)."""

    run_id: str = ""


@dataclass
class WorkflowNodeAnswer(Event):
    """User answers a node's ``ctx.ask`` (frontend → backend): one answer per question, in order."""

    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    request_id: str = ""
    answers: tuple[AskUserAnswer, ...] = ()


@dataclass
class WorkflowNodeRetryRequest(Event):
    """User retries an activation that is awaiting retry (frontend → backend).

    ``request_id`` and ``expected_failed_attempt`` make the request idempotent:
    a second delivery, or a replay after a later failure, starts no attempt.
    """

    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    request_id: str = ""
    expected_failed_attempt: int = 0


@dataclass(kw_only=True)
class WorkflowRunAccepted(Event):
    """The run request was admitted and bound to ``run_id`` (backend → frontend)."""

    request_id: str = ""
    run_id: str = ""
    selection: WorkflowSessionSelection
    session_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.session_id = self.selection.session_id


@dataclass
class WorkflowRunRejected(Event):
    """The run request was refused deterministically (backend → frontend).

    ``error`` is the structured reason code; ``message`` is for display.
    """

    request_id: str = ""
    error: str = ""
    message: str = ""


@dataclass
class WorkflowRunStarted(Event):
    """A run began; carries the manifest and the per-agent-node resolution snapshot."""

    seq: int = 0
    input_text: str = ""
    run_id: str = ""
    workflow_id: str = ""
    source_kind: str = ""
    canonical_path: str = ""
    title: str = ""
    spec_digest: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    resolved_nodes: list[dict[str, Any]] = field(default_factory=list)
    model: WorkflowModelSelection | None = None


@dataclass
class WorkflowNodeStateChanged(Event):
    """An activation changed state.

    ``invocation_id`` binds an agent activation to its ``Invocation*`` events
    ahead of time: the runner allocates the origin before publishing ``running``.
    """

    seq: int = 0
    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    attempt: int = 0
    state: str = ""
    iteration: int = 0
    failure_phase: str = ""
    invocation_id: str = ""
    error: str = ""
    error_class: str = ""


@dataclass
class WorkflowNodeOutput(Event):
    """A process fragment (``kind="emit"``) or the activation's final value (``kind="final"``).

    ``ordinal`` is scoped to ``(activation_id, attempt)`` and restarts at 1 per
    attempt. ``summary_text`` is a bounded display summary; the full value is
    in the run store.
    """

    seq: int = 0
    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    attempt: int = 0
    kind: str = ""
    ordinal: int = 0
    summary_text: str = ""


@dataclass
class WorkflowNodeAskUser(Event):
    """A python node is waiting on ``ctx.ask``; answer with ``WorkflowNodeAnswer``.

    Live events carry the full questions; a replayed one carries the stored
    summary as a single open question.
    """

    seq: int = 0
    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    attempt: int = 0
    request_id: str = ""
    questions: tuple[AskUserQuestion, ...] = ()


@dataclass
class WorkflowNodeAnswered(Event):
    """An accepted answer, journaled before the waiting attempt resumes; ``answer`` is its summary."""

    seq: int = 0
    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    attempt: int = 0
    request_id: str = ""
    answer: str = ""


@dataclass
class WorkflowLoopIteration(Event):
    """A loop's ``until`` verdict for one iteration (``continue`` / ``exit`` / ``exhausted``)."""

    seq: int = 0
    run_id: str = ""
    loop_id: str = ""
    activation_id: str = ""
    attempt: int = 0
    iteration: int = 0
    verdict: str = ""


@dataclass
class WorkflowRunNotice(Event):
    """A once-per-run advisory; the node identity is the first activation that hit it."""

    seq: int = 0
    run_id: str = ""
    node_id: str = ""
    activation_id: str = ""
    attempt: int = 0
    code: str = ""
    message: str = ""


@dataclass
class WorkflowOutputSummary:
    """One declared output of a finished run; the full value is in the run store."""

    node_id: str = ""
    activation_id: str = ""
    summary_text: str = ""
    attempt: int = 0


@dataclass
class WorkflowRunFinished(Event):
    """The run reached a terminal outcome.

    ``outputs`` follows the workflow's ``output()`` declaration order and omits
    skipped outputs. ``node_id``/``error`` name the failing activation for
    ``node_failed``/``loop_exhausted``. ``reason`` refines the outcome
    (``deadline_exceeded`` for a run-level timeout cancellation).
    """

    seq: int | None = None
    run_id: str = ""
    outcome: str = ""
    outputs: list[WorkflowOutputSummary] = field(default_factory=list)
    duration: float = 0.0  # seconds
    node_id: str = ""
    error: str = ""
    reason: str = ""
    last_written_seq: int = 0
    degraded: bool = False
