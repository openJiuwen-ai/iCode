# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turn-aware four-phase compaction strategy.

A pass runs in one of three modes, fixed when it starts (strongest first):
overflow recovery (the provider rejected a request as too large), a hard pass
(the estimate reached the trigger) and a turn-boundary pass (the previous turn
ended above the force line).  The mode decides which phases may run, when a
pass escalates to the next one and where it stops; see
:class:`CompactionMode`.

Progressive compaction with escalating aggressiveness:

**Phase 1** — Old turn tool results are summarised oldest-turn-first (keeping
call arguments, replacing results with one-line summaries).

**Phase 2** — Old turn tool-call groups are removed entirely (args + results),
oldest-turn-first.  Only the user/assistant text messages survive per turn.

**Phase 3** — Completed turns are folded into summaries oldest-turn-first
when Phase 1/2 leave usage above the line the pass's mode sets.

**Phase 4** — The current turn's tool-call and inline assistant-text messages,
except the newest groups that fit the recent-work allowance, are dropped in
one round and replaced by a ``<system-reminder>[LAST_WORDS]
…</system-reminder>`` block appended to the user message's content array by
``SystemReminderMiddleware``.  The note is produced by
:class:`chrys.service.context.compaction.last_words.LastWordsGenerator` (always LLM-generated —
no programmatic fallback; transient generator failures retry the same
compaction input with backoff).  On subsequent Phase 4 triggers
within the same turn the previous note + newly-dropped work are fed back
to the generator so the note accumulates insight without re-summarising
from scratch.

Turn boundaries come from ``_resolve_turns`` (built on
``chrys.foundation.models.turns``): only real openers — user messages
without a mid-turn flag — split turns, so injections and synthetic
``continue`` nudges stay inside the turn they arrived in, and the marker
region of the provider state decides degraded (opener-less) shapes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from copy import copy
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from chrys.foundation.errors import context_overflow_limit, iter_explicit_graph
from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.foundation.trajectory.context import ExchangeTrace, current_trajectory
from chrys.foundation.trajectory.event_types import CompactionSkipReason, PromptCacheMissCause
from chrys.kernel import (
    Message,
    annotate_message_groups,
    annotate_token_counts,
    included_token_count,
    project_included_messages,
    recount_user_messages,
)
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS
from chrys.service.trajectory.compaction import (
    CompactionRunTrace,
    bind_compaction_operation,
    record_compaction_skipped,
    record_compaction_thrash,
    record_prompt_cache_miss,
    reset_compaction_operation,
)
from chrys.service.trajectory.state import turn_records

from .breaker import DropBreakerController
from .budgets import BOUNDARY_SUSPENDED_STATE_KEY, TURN_BOUNDARY_PENDING_STATE_KEY
from .cache_telemetry import THRASH_WINDOW_TURNS, PromptCacheTelemetry
from .calibration import TokenCalibration
from .compression import CompressionEngine
from .current_turn_drop import CurrentTurnDropRound
from .events import (
    CachedCompressedContextSummary,
    CachedSummary,
    CompactionInfo,
    CompressInfo,
    PendingCompression,
    PreCompactInfo,
)
from .exclusions import ExclusionLedger, _ExclusionAnchor
from .groups import (
    _any_tool_groups_in_range,
    _dedup_message_ids,
    _group_kind_map,
    _group_messages_by_id,
    _group_start_indices,
    _ordered_group_ids,
    _tool_groups_in_range,
)
from .last_words import LastWordsGenerationError
from .last_words_state import DropRoundBreakerState, LastWordsState, Phase4RetrySnapshot
from .scoped import ScopedGroup
from .spill import SpillQuota
from .summaries import _compact_group, _remove_group
from .turns import _resolve_turns

_log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from chrys.kernel import CompactionCallContext, LastWordsCompleter, TokenizerProtocol


# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------


class CompactionMode(StrEnum):
    """Why a pass runs; the value is the trajectory run's ``trigger``.

    ``OVERFLOW`` — the provider found the window full, so the local estimate
    is too low: no escalation gate, and the stop point is scaled down by how
    far below the trigger the estimate was.

    ``HARD`` — the estimate reached the trigger.  Phase 1 always runs; Phases
    2 and 3 start only above the satisfied line, Phase 4 only above the force
    line; every phase that starts runs down to the target.

    ``BOUNDARY`` — booked by a turn that ended above the force line, run
    before the next request while still below the trigger.  As ``HARD``, but
    never Phase 4 and never the newest completed turn, which the user is
    likely following up on.
    """

    OVERFLOW = "context_overflow"
    HARD = "usage_threshold"
    BOUNDARY = "turn_boundary"


@dataclass(frozen=True, slots=True)
class _PassPlan:
    """The thresholds one pass runs under (usage fractions of the window)."""

    mode: CompactionMode
    stop_pct: float
    """Where every phase that started stops."""
    escalate_pct: float
    """Phases 2 and 3 start only above this."""
    phase4_pct: float | None
    """Phase 4 starts only above this; ``None`` keeps it out of the pass."""
    protect_newest_turn: bool
    """Leave the newest completed turn untouched in Phases 1 to 3."""
    spend_recent_tail: bool = False
    """Phase 4 gives up the recent work it would keep, oldest first, until usage is at the stop point."""


class LastWordsGeneratorLike(Protocol):
    """Strategy-facing contract of ``LastWordsGenerator`` (mirrored by test stubs)."""

    async def generate(
        self,
        scoped_groups: list[ScopedGroup],
        previous_last_words: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        completer: LastWordsCompleter | None = None,
        tokenizer: TokenizerProtocol | None = None,
        system_overhead_tokens: int = 0,
        tool_definition_tokens: int = 0,
        request_overhead_tokens: int = 0,
        calibration_ratio: float = 1.0,
        spend_side_call_tokens: Callable[[int], bool] | None = None,
        affords_side_call_tokens: Callable[[int], bool] | None = None,
        entry_messages: Sequence[Message] | None = None,
    ) -> str: ...

    async def publish_breaker_trip(self, failure_reason: str) -> None: ...

    async def publish_committed(self) -> None: ...


class ReminderMiddlewareLike(Protocol):
    """Strategy-facing contract of ``SystemReminderMiddleware`` (mirrored by test stubs)."""

    def renders_last_words(self, state: object) -> bool: ...

    def refresh_last_words_reminder(self, messages: list[Any]) -> int | None: ...

    def restore_folded_reminders(self, messages: list[Message]) -> int | None: ...


@dataclass(frozen=True, slots=True)
class CompactionRetrySnapshot:
    """Attempt rollback state split by compaction lifetime.

    Cross-turn folds published after ``published_fold_count`` are monotonic
    commits and get silently replayed onto restored history.  Phase-4 note
    content is attempt-local and rolls back.  Breaker/pressure accounting is
    intentionally owned by the LAST_WORDS state's logical-turn slot and is
    not represented here.
    """

    anchors: tuple[_ExclusionAnchor, ...]
    published_fold_count: int
    phase4: Phase4RetrySnapshot | None
    # The boundary schedule goes back with the history it was decided on: a
    # pass that consumed its booking inside the attempt is rolled back with
    # the attempt, and the retry has to run it again.
    turn_boundary_pending: bool = False
    boundary_suspended: bool = False


class UnifiedContextStrategy:
    """Unified strategy for intra-turn compaction AND cross-turn compression.

    Owns **all** context reduction during a tool loop.  Compression requests
    from the ``compress_context`` tool are queued via :meth:`queue_compression`
    and processed inside :meth:`__call__` on FunctionInvocationLayer's message list —
    guaranteeing that ``_excluded`` flags are set on shared objects and
    ``project_included_messages`` filters them out on the next API call.

    **Compaction** (4-phase, budget-triggered):

    1. Estimate context usage as ``(included_tokens * ratio) / max_context``.
    2. If usage < ``trigger_pct``, no-op, unless the provider has reported the
       context window full since the last completed pass
       (:meth:`note_context_overflow`). Past the trigger, a call that cannot
       start a pass (compaction disabled, no resolvable turn) logs and records
       ``compaction.skipped`` once per stretch above the trigger.
    3. **Phase 1-4**: trim old tools, compress old text turns, then drop
       current-turn tool work only as the final fallback.  The pass's
       :class:`CompactionMode` gates each escalation.
    4. A turn that ends above the force line books a turn-boundary pass
       (:meth:`request_turn_boundary_pass`) instead of rewriting history
       after the run: the next request compacts down to the target in one
       pass, and no further one is booked until usage has been at or below
       the satisfied line.

    **Compression** (agent-requested via ``compress_context`` tool):

    Queued by :meth:`queue_compression`, processed before the compaction
    phases.  Sets ``_excluded`` on fold-range messages and replaces
    ``state["messages"]`` for persistence.

    Args:
        max_context_tokens: Maximum context window size (e.g. 200_000).
        trigger_pct: Usage fraction that triggers compaction (e.g. 0.85).
        target_pct: Usage fraction to compact down to (e.g. 0.50).
        satisfied_pct: Usage fraction at or below which a pass stops
            escalating and turn-boundary passes may be booked again.  Defaults
            to a third of the way from the target to the trigger.
        force_pct: Usage fraction above which Phase 4 may start in a hard
            pass.  Defaults to two thirds of the way from the target to the
            trigger.
        tokenizer: Token estimator.  Defaults to ``MixedLanguageTokenizer``.
        on_compaction: Async callback fired after compaction phases.
        on_compress: Async callback fired after compression.
        compaction_enabled: When ``False``, skip budget-triggered compaction
            but still process queued compressions.
    """

    def __init__(
        self,
        *,
        max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS,
        trigger_pct: float = 0.85,
        target_pct: float = 0.50,
        satisfied_pct: float | None = None,
        force_pct: float | None = None,
        tokenizer: TokenizerProtocol | None = None,
        on_compaction: Callable[[CompactionInfo], Awaitable[None]] | None = None,
        on_pre_compact: Callable[[PreCompactInfo], Awaitable[None]] | None = None,
        on_compress: Callable[[CompressInfo], Awaitable[None]] | None = None,
        on_context_pressure: Callable[[str, DropRoundBreakerState, int], Awaitable[None] | None] | None = None,
        spill_quota: SpillQuota | None = None,
        spill_root: Path | None = None,
        spill_record_dir: Path | None = None,
        spill_session_id: str = "",
        debug_log_dir: Path | None = None,
        phase4_side_call_token_budget: int | None = -1,
        compaction_enabled: bool = True,
    ) -> None:
        if max_context_tokens <= 0:
            raise ValueError("max_context_tokens must be positive")
        if compaction_enabled:
            if not 0.0 < trigger_pct <= 1.0:
                raise ValueError("trigger_pct must be in (0.0, 1.0]")
            if not 0.0 < target_pct < trigger_pct:
                raise ValueError("target_pct must be in (0.0, trigger_pct)")
        span = trigger_pct - target_pct
        satisfied_pct = target_pct + span / 3 if satisfied_pct is None else satisfied_pct
        force_pct = target_pct + 2 * span / 3 if force_pct is None else force_pct
        if compaction_enabled and not target_pct <= satisfied_pct <= force_pct <= trigger_pct:
            raise ValueError("satisfied_pct and force_pct must lie in order between target_pct and trigger_pct")
        self.max_context_tokens = max_context_tokens
        self.trigger_pct = trigger_pct
        self.target_pct = target_pct
        self.satisfied_pct = satisfied_pct
        self.force_pct = force_pct
        self._compaction_enabled = compaction_enabled
        self._tokenizer: TokenizerProtocol = tokenizer or MixedLanguageTokenizer()
        self._on_compaction = on_compaction
        self._on_pre_compact = on_pre_compact
        self._on_compress = on_compress
        # The trajectory run of the compaction pass in flight (one per
        # triggered ``__call__``); phases report into it.
        self._trajectory_run: CompactionRunTrace | None = None
        # The skip (reason, session) already reported for the current
        # stretch above the trigger; cleared when usage drops below it or a
        # pass starts.
        self._reported_skip: tuple[str, str | None] | None = None
        # Set when the provider rejected a request for overflowing the context
        # window; forces the next pass whatever the estimate says and clears
        # only when one completes. Per instance on purpose: retry rollback
        # keeps it, and it is never persisted.
        self._context_overflow_pending = False
        # Booked by a turn that ended above the force line; the next request
        # runs a turn-boundary pass even below the trigger.  Its own field,
        # never the overflow note: a pass that is cancelled or fails keeps
        # both as they were, so the retry runs in the same mode.  Unlike the
        # overflow note it is kept in the bound state too: the request it is
        # booked for is the next turn's, which another process may send.
        self._turn_boundary_pending = False
        # Set when a pass ended above the satisfied line: another boundary
        # pass would free too little to be worth a rewritten prefix, so none
        # is booked until usage is at or below that line again.  Kept in the
        # bound state with the booking.
        self._boundary_suspended = False
        # The plan of the pass in flight; phases read it through ``_plan``.
        self._active_plan: _PassPlan | None = None
        # The request as the pass in flight found it, for a note asked for as
        # an extension of it; None when that pass may not extend.
        self._entry_request: tuple[Message, ...] | None = None
        self._on_context_pressure = on_context_pressure
        self._spill_quota = spill_quota
        self._spill_root = spill_root
        self._spill_record_dir = spill_record_dir or Path("compactions") / "dropped"
        self._spill_session_id = spill_session_id
        self._debug_log_dir = debug_log_dir
        # Negative (or None, kept for call-site compatibility) = unlimited:
        # the per-turn round limit is the guard, not a spend cap.
        self._phase4_side_call_token_budget = (
            -1 if phase4_side_call_token_budget is None else phase4_side_call_token_budget
        )
        self._last_included_tokens: int = 0
        # Phase 4 LAST_WORDS collaborators (wired post-construction by
        # AgentBuilder so ContextManager can keep a simple signature).
        self._reminder_middleware: ReminderMiddlewareLike | None = None
        self._last_words_state: LastWordsState | None = None
        self._last_words_generator: LastWordsGeneratorLike | None = None
        self._persist_recovery_now: Callable[[], Awaitable[bool]] | None = None
        # Detached on_compaction deliveries scheduled when cancellation
        # interrupts the committed publish — anchored here because asyncio
        # holds only weak references to tasks.
        self._detached_deliveries: set[asyncio.Future] = set()
        self._calibration = TokenCalibration(self)
        self._cache_telemetry = PromptCacheTelemetry()
        self._exclusions = ExclusionLedger()
        self._compression = CompressionEngine(self, self._exclusions)
        self._breaker = DropBreakerController(self)
        self._current_turn_drop = CurrentTurnDropRound(self, self._breaker, self._exclusions)

    @property
    def _calibration_ratio(self) -> float:
        return self._calibration._calibration_ratio

    @_calibration_ratio.setter
    def _calibration_ratio(self, value: float) -> None:
        self._calibration._calibration_ratio = value

    @property
    def _overhead_initialized(self) -> bool:
        return self._calibration._overhead_initialized

    @_overhead_initialized.setter
    def _overhead_initialized(self, value: bool) -> None:
        self._calibration._overhead_initialized = value

    @property
    def _system_overhead(self) -> int:
        return self._calibration._system_overhead

    @_system_overhead.setter
    def _system_overhead(self, value: int) -> None:
        self._calibration._system_overhead = value

    @property
    def _request_overhead_floor(self) -> int:
        return self._calibration._request_overhead_floor

    @_request_overhead_floor.setter
    def _request_overhead_floor(self, value: int) -> None:
        self._calibration._request_overhead_floor = value

    @property
    def _summary_cache(self) -> dict[str, CachedSummary]:
        return self._exclusions._summary_cache

    @_summary_cache.setter
    def _summary_cache(self, value: dict[str, CachedSummary]) -> None:
        self._exclusions._summary_cache = value

    @property
    def _removed_group_ids(self) -> set[str]:
        return self._exclusions._removed_group_ids

    @_removed_group_ids.setter
    def _removed_group_ids(self, value: set[str]) -> None:
        self._exclusions._removed_group_ids = value

    @property
    def _excluded_anchors(self) -> list[_ExclusionAnchor]:
        return self._exclusions._excluded_anchors

    @_excluded_anchors.setter
    def _excluded_anchors(self, value: list[_ExclusionAnchor]) -> None:
        self._exclusions._excluded_anchors = value

    @property
    def _state(self) -> dict[str, Any] | None:
        return self._compression._state

    @_state.setter
    def _state(self, value: dict[str, Any] | None) -> None:
        self._compression._state = value

    @property
    def _pending_compressions(self) -> list[PendingCompression]:
        return self._compression._pending_compressions

    @_pending_compressions.setter
    def _pending_compressions(self, value: list[PendingCompression]) -> None:
        self._compression._pending_compressions = value

    @property
    def _compressed_context_cache(self) -> dict[str, CachedCompressedContextSummary]:
        return self._compression._compressed_context_cache

    @_compressed_context_cache.setter
    def _compressed_context_cache(self, value: dict[str, CachedCompressedContextSummary]) -> None:
        self._compression._compressed_context_cache = value

    @property
    def _context_pressure_tasks(self) -> set[asyncio.Future[None]]:
        return self._breaker._context_pressure_tasks

    @_context_pressure_tasks.setter
    def _context_pressure_tasks(self, value: set[asyncio.Future[None]]) -> None:
        self._breaker._context_pressure_tasks = value

    # ------------------------------------------------------------------
    # Phase 4 wiring (called by AgentBuilder after middleware + generator exist)
    # ------------------------------------------------------------------

    def bind_reminder(self, middleware: ReminderMiddlewareLike, last_words: LastWordsState) -> None:
        """Bind the ``SystemReminderMiddleware`` and the LAST_WORDS state it renders, together.

        Phase 4 writes its note to *last_words* and drops history only when
        both are bound, so the next request carries the note in place of the
        dropped history.  A state the middleware does not render is refused.
        """
        if not middleware.renders_last_words(last_words):
            raise ValueError("The reminder middleware does not render this LAST_WORDS state.")
        self._reminder_middleware = middleware
        self._last_words_state = last_words

    def set_last_words_generator(self, generator: LastWordsGeneratorLike) -> None:
        """Bind the ``LastWordsGenerator`` used by Phase 4."""
        self._last_words_generator = generator

    def set_recovery_persistence_callback(self, callback: Callable[[], Awaitable[bool]]) -> None:
        """Bind the main engine's strict recovery-sidecar durability callback."""
        self._persist_recovery_now = callback

    def _write_reminder_debug(self) -> None:
        """Dump the LAST_WORDS reminder exactly as the next model call injects it.

        Owner-only (0600) like the spill records: the rendered reminder
        carries the progress note, which summarises session content.
        """
        if self._debug_log_dir is None or self._last_words_state is None:
            return
        try:
            text = self._last_words_state.render_last_words_reminder_text()
            if not text:
                return
            import os
            from datetime import UTC, datetime

            self._debug_log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(UTC).strftime("%H%M%S_%f")[:-3]
            path = self._debug_log_dir / f"reminder_{ts}_{uuid.uuid4().hex[:6]}.log"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(text)
        except Exception:
            _log.debug("Failed to write LAST_WORDS reminder debug dump", exc_info=True)

    def _spend_side_call_tokens(self, estimated_tokens: int) -> bool:
        """Delegate the test-visible side-call charging seam."""
        return self._breaker.spend_side_call_tokens(estimated_tokens)

    async def _notify_compaction(self, info: CompactionInfo) -> None:
        """Record a finished phase on the trajectory, then deliver the UI callback."""
        await self._notify_compaction_for_run(info, self._trajectory_run)

    async def _notify_compaction_for_run(self, info: CompactionInfo, run: CompactionRunTrace | None) -> None:
        """As :meth:`_notify_compaction`, against the run *info* was measured under.

        A detached delivery outlives the pass that scheduled it, so reading
        the live handle at delivery time would hang the phase off whatever
        run is current by then — or drop it once the pass has cleared it.
        """
        if run is not None:
            await run.phase_finished(
                phase=info.phase,
                groups_compacted=info.compacted_groups,
                turn_numbers=info.turn_numbers,
                tool_names=info.tool_names,
                tokens_before=info.tokens_before,
                tokens_after=info.tokens_after,
                last_words_generated=info.last_words_generated,
                consumed_item_ids=info.consumed_item_ids if info.phase == "phase4" else None,
            )
        if self._on_compaction is not None:
            await self._on_compaction(info)

    async def _notify_compress(
        self,
        info: CompressInfo,
        *,
        trigger: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        """Record a cross-turn fold on the trajectory, then deliver the UI callback.

        Inside a triggered pass the fold is a phase of that run; a fold the
        agent asked for, made at its next request or after the run, is a run
        of its own.
        """
        run = self._trajectory_run
        turn_numbers = list(range(info.turn_range[0], info.turn_range[1] + 1)) if info.turn_range != (0, 0) else []
        if run is not None:
            await run.phase_finished(
                phase="phase3",
                groups_compacted=1,
                turn_numbers=turn_numbers,
                tool_names=[],
                tokens_before=tokens_before,
                tokens_after=tokens_after,
                messages_freed=info.freed_messages,
                compressed_context_id=info.compressed_context_id,
            )
        else:
            await CompactionRunTrace.record_compression(
                trigger=trigger,
                compressed_context_id=info.compressed_context_id,
                messages_freed=info.freed_messages,
                tokens_before=tokens_before,
                tokens_after=tokens_after,
            )
        if self._on_compress is not None:
            await self._on_compress(info)

    async def _fire_pre_compact(self, trigger: str, *, usage_pct: float, tokens_before: int) -> None:
        if self._on_pre_compact is None:
            return
        await self._on_pre_compact(
            PreCompactInfo(
                trigger=trigger,
                usage_pct=usage_pct,
                tokens_before=tokens_before,
                trajectory_operation_id=(self._trajectory_run.run_id if self._trajectory_run is not None else None),
            )
        )

    # ------------------------------------------------------------------
    # State binding (called by CompressibleHistoryProvider.before_run)
    # ------------------------------------------------------------------

    def bind_state(self, state: dict[str, Any]) -> None:
        """Bind to the history provider's state dict for this agent.run() cycle.

        The boundary schedule is read from the state on every bind: the state
        is what a restart, a rewind or a fork hands over, and within one
        process it holds what this strategy last wrote.  Only a schedule made
        before the first bind has nowhere else to be, and is carried in.
        """
        first_bind = self._state is None
        self._compression.bind_state(state)
        pending = bool(state.get(TURN_BOUNDARY_PENDING_STATE_KEY))
        suspended = bool(state.get(BOUNDARY_SUSPENDED_STATE_KEY))
        if first_bind:
            pending = pending or self._turn_boundary_pending
            suspended = suspended or self._boundary_suspended
        self._set_boundary_schedule(pending=pending, suspended=suspended)

    def _set_boundary_schedule(self, *, pending: bool, suspended: bool) -> None:
        """Set the boundary booking and suspension, here and in the bound state.

        Absent means false in the state, as the session serializer keeps
        only truthy values of these keys.
        """
        self._turn_boundary_pending = pending
        self._boundary_suspended = suspended
        state = self._state
        if state is None:
            return
        for key, value in (
            (TURN_BOUNDARY_PENDING_STATE_KEY, pending),
            (BOUNDARY_SUSPENDED_STATE_KEY, suspended),
        ):
            if value:
                state[key] = True
            else:
                state.pop(key, None)

    # ------------------------------------------------------------------
    # Cross-turn compression API
    # ------------------------------------------------------------------

    def queue_compression(self, marker_id: str, summary_text: str) -> tuple[str, int]:
        """Queue a compression request.  Returns ``(compressed_context_id, freed_count)``.

        Validates the marker exists in state (raises ``ValueError`` if not).
        The actual compression is executed inside :meth:`__call__`.
        """
        return self._compression.queue_compression(marker_id, summary_text)

    def list_compressed(self) -> dict[str, Any]:
        """Return compressed blocks and available markers, accounting for pending compressions."""
        return self._compression.list_compressed()

    # ------------------------------------------------------------------
    # After-run persistence (called by CompressibleHistoryProvider.after_run)
    # ------------------------------------------------------------------

    def persist_exclusions_to_state(self, state_messages: list[Message]) -> None:
        """Propagate tool-loop exclusion flags to state messages.

        History messages reach the wire as per-call views sharing the
        state original's ``additional_properties`` dict and content
        objects, so a flag set on the wire copy is already on the stored
        message and the anchor's shared-content-ids tier re-sets it
        idempotently.  The load-bearing case is streaming: finalizers
        rebuild current-turn response messages from updates before they
        are stored, so their flags only reach state through the
        structural anchors.

        Anchors are single-use: one snapshot pass, one persist pass.  The
        list is cleared after consumption because under-trigger runs return
        before ``_snapshot_excluded_anchors`` — without the clear, a stale
        list replays against ever-longer state on every subsequent
        after_run, and the newest-first structural fallback would bind a
        later structural twin (a repeated identical tool call or
        byte-equal assistant text) and silently exclude a live message.
        The anchors deliberately survive a run whose after_run never fired
        (interrupt before storage): the next after_run is their one chance
        to reach the stored twins.
        """
        self._exclusions.persist_to_state(state_messages)

    def snapshot_retry_state(self) -> CompactionRetrySnapshot:
        """Capture compaction state at retry-attempt entry.

        A Phase-4 commit inside the attempt records anchors describing the
        current-turn messages it dropped.  When a transient error rolls the
        attempt back, history is restored to the pre-attempt snapshot and
        those messages never reach state — but the anchors would survive,
        and the successful retry can emit identical calls/text, so the
        after_run persist's newest-first structural fallback would bind the
        retry's fresh live messages and silently exclude them.  The retry
        loop snapshots this alongside history and restores both together.
        Published cross-turn folds are different: Phase 3 only folds completed
        turns, so a transient provider failure must preserve them across the
        restored pre-input history baseline.  The journal boundary lets
        restore replay only folds committed by this outer attempt.

        LAST_WORDS note/todo/manifest content describes current-turn messages
        dropped by Phase 4 and therefore rolls back with those messages.
        Breaker and pressure accounting remain monotonic per logical turn.
        """
        phase4 = self._last_words_state.snapshot_phase4_retry_state() if self._last_words_state is not None else None
        return CompactionRetrySnapshot(
            anchors=self._exclusions.snapshot_retry_state(),
            published_fold_count=self._compression.published_fold_count(),
            phase4=phase4,
            turn_boundary_pending=self._turn_boundary_pending,
            boundary_suspended=self._boundary_suspended,
        )

    def restore_retry_state(self, snapshot: CompactionRetrySnapshot) -> None:
        """Restore the anchors captured by :meth:`snapshot_retry_state`.

        Discards anchors created by the rolled-back attempt while keeping
        ones that predate it (anchors surviving an interrupted prior run
        still get their one after_run persist).  Phase-4 content is restored
        before published completed-turn folds are silently replayed onto the
        executor-restored provider state.
        """
        self._exclusions.restore_retry_state(snapshot.anchors)
        if self._last_words_state is not None and snapshot.phase4 is not None:
            self._last_words_state.restore_phase4_retry_state(snapshot.phase4)
        self._compression.replay_published_folds(snapshot.published_fold_count)
        if self._state is not None:
            self._exclusions.discard_unreferenced_summaries(self._state.get("messages", []))
        self._set_boundary_schedule(
            pending=snapshot.turn_boundary_pending,
            suspended=snapshot.boundary_suspended,
        )

    async def flush_pending_compressions(self) -> bool:
        """Process pending cross-turn compressions on bound state.

        Called by ``after_run`` when the LLM called ``compress_context``
        right before a successful run ended, and by the next executor cycle
        before its retry snapshot when a failed run left that request
        queued.  Unlike ``__call__``'s compression path, this does not set
        ``_excluded`` on a framework message list; subsequent provider input
        is built from the already-folded state.
        """
        return await self._compression.flush_pending_compressions()

    @property
    def last_included_tokens(self) -> int:
        """Estimated included token count from the last ``__call__``.

        Uses the same ``MixedLanguageTokenizer`` as the threshold check,
        so callers can use it instead of the API's ``input_token_count``
        to avoid calibration mismatch.
        """
        return self._last_included_tokens

    def calibrate(self, api_input_tokens: int, *, local_included: int = 0) -> None:
        """Update system overhead and calibration ratio from an API response.

        Uses an **additive + multiplicative** model::

            estimated_api_tokens = (local + system_overhead) * ratio

        *  ``system_overhead`` (fixed) captures the system prompt + tool
           definitions — computed once on the first API response and kept
           constant for the session.
        *  ``ratio`` (updated each call) captures only tokenizer drift —
           should stay close to 1.0.

        On the first call the overhead is learned.  When the heuristic
        overestimates enough that ``local > api``, overhead is clamped to
        zero and the ratio is calibrated immediately to absorb the full gap.

        Args:
            api_input_tokens: The API's ``input_token_count``.
            local_included: Optional override for the local baseline.
        """
        local = local_included if local_included > 0 else self._last_included_tokens
        self._calibration.calibrate(api_input_tokens, local_included=local)

    def observe_response_usage(
        self, *, input_tokens: int, cache_read_tokens: int, exchange: ExchangeTrace | None = None
    ) -> None:
        """Judge a response's cache read against the request before it and report a miss.

        Called with the provider's own counts for every response of the
        conversation this strategy compacts, and with the *exchange* that
        produced them, which the miss is recorded under.  A miss right after
        a compaction is that compaction's price; one after a long pause is
        the cache expiring; any other is unexplained, and is logged as a
        warning.
        """
        miss = self._cache_telemetry.observe_response(input_tokens=input_tokens, cache_read_tokens=cache_read_tokens)
        if miss is None:
            return
        _log.log(
            logging.WARNING if miss.cause == PromptCacheMissCause.UNKNOWN else logging.INFO,
            "Prompt cache miss: cause=%s, missed_tokens=%d, input_tokens=%d, previous_input_tokens=%d, "
            "cache_read_tokens=%d",
            miss.cause,
            miss.missed_tokens,
            miss.input_tokens,
            miss.previous_input_tokens,
            miss.cache_read_tokens,
        )
        record_prompt_cache_miss(
            cause=miss.cause,
            missed_tokens=miss.missed_tokens,
            input_tokens=miss.input_tokens,
            previous_input_tokens=miss.previous_input_tokens,
            cache_read_tokens=miss.cache_read_tokens,
            exchange=exchange,
        )

    def _note_prefix_rewrite(self, *, head: bool, before_next_request: bool = False) -> None:
        """Tell the cache telemetry the next request's prefix changed, and report thrash.

        Called where the change lands, before anything the caller awaits: the
        notifications that follow a change can be cancelled, and the change
        stays.  *head* is a rewrite of what precedes the current turn (phases
        1 to 3 and folds).  A current-turn drop rewrites only that turn's own
        tail, as a long task has to now and then, and is not counted as
        thrash.  *before_next_request* is a rewrite made outside a request's
        preparation (a fold after the run).
        """
        turns = self._cache_telemetry.note_rewrite(
            head=head,
            turn=self._running_turn_number(),
            before_next_request=before_next_request,
        )
        if turns is None:
            return
        # Not a warning: the thresholds are a judgement no workload has
        # calibrated, and a session that is doing well can cross them.  The
        # trajectory event is what the count is read from.
        _log.debug(
            "Compaction thrash: the head of the request was rewritten %d times within %d turns (in turns %s); "
            "after each rewrite the provider reads the request again, uncached, from the point that changed",
            len(turns),
            THRASH_WINDOW_TURNS,
            ", ".join(str(turn) for turn in turns),
        )
        record_compaction_thrash(rewrites=len(turns), window_turns=THRASH_WINDOW_TURNS, turn_numbers=turns)

    def _running_turn_number(self) -> int | None:
        """The number of the turn in progress, as its ``turn.started`` gave it.

        The turn registry's last entry is that turn, also for a retry that
        resumed it and for a fold made after its run.  A history without a
        registry (a child agent's) only counts the turns it completed, so the
        one in progress is the next; with neither there is no number.
        """
        state = self._state
        if state is None:
            return None
        records = turn_records(state)
        if records:
            return records[-1].turn_number
        completed = state.get("turn_counter")
        return completed + 1 if isinstance(completed, int) else None

    @property
    def calibration_ratio(self) -> float:
        """Tokenizer accuracy ratio (should be close to 1.0).

        > 1.0 = local tokenizer underestimates.
        < 1.0 = local tokenizer overestimates.
        Updated by :meth:`calibrate` after each API response.
        """
        return self._calibration._calibration_ratio

    @property
    def calibration_initialized(self) -> bool:
        """Whether a provider response initialized calibration state."""
        return self._calibration._overhead_initialized

    def restore_calibration(self, system_overhead_tokens: object, calibration_ratio: object) -> bool:
        """Restore validated calibration values from provenance-gated state."""
        return self._calibration.restore(system_overhead_tokens, calibration_ratio)

    @property
    def compaction_enabled(self) -> bool:
        """Whether passes run on their own; off, only folds the model asks for happen."""
        return self._compaction_enabled

    @property
    def tokenizer(self) -> TokenizerProtocol:
        """Tokenizer shared by compaction and per-call budget estimation."""
        return self._tokenizer

    @property
    def system_overhead_tokens(self) -> int:
        """Estimated fixed token overhead (system prompt + tool definitions).

        Computed as ``api_input_tokens - local_conversation_tokens`` after
        each API response.  The true overhead is constant within a session;
        small fluctuations reflect tokenizer inaccuracy on conversation content.
        """
        return self._calibration._system_overhead

    @property
    def estimated_context_input_tokens(self) -> int:
        """Estimated input occupancy for the most recently prepared request.

        This uses the same request-overhead floor and tokenizer calibration as
        admission and compaction checks, so it remains usable when a provider's
        reported input usage includes hidden hosted-tool execution.
        """
        return self._calibration.estimated_tokens(self._last_included_tokens)

    def _usage_pct(self, included: int) -> float:
        """Compute effective context usage as a fraction of max_context_tokens."""
        return self._calibration.usage_pct(included)

    def note_context_overflow(self, exc: BaseException | None = None) -> bool:
        """Make the next pass run even below the trigger: the provider found the window full.

        Not for a failure of compaction's own last-words call (owner-terminal;
        its fallback text names the context window), nor while compaction is
        off, which would only record a misleading skip. Returns whether to
        compact and resend the same request: not when the provider names a
        smaller window than this one, since requests sized for this window
        keep overflowing until the user sets the right one; the failure then
        tells the user which value to set.
        """
        if exc is not None and any(isinstance(node, LastWordsGenerationError) for node in iter_explicit_graph(exc)):
            return False
        if not self._compaction_enabled:
            return False
        self._context_overflow_pending = True
        limit = context_overflow_limit(exc) if exc is not None else None
        return limit is None or limit >= self.max_context_tokens

    def request_turn_boundary_pass(self) -> bool:
        """Book a turn-boundary pass for the next request; the turn just ended above the force line.

        Refused while compaction is off, and while suspended: the last pass
        ended above the satisfied line, so the trigger is the backstop until
        usage is at or below that line again.
        """
        if not self._compaction_enabled or self._boundary_suspended:
            return False
        self._set_boundary_schedule(pending=True, suspended=False)
        return True

    @property
    def _plan(self) -> _PassPlan:
        """The plan of the pass in flight, or a hard pass's for a phase run on its own."""
        return self._active_plan or self._plan_for(CompactionMode.HARD, self.trigger_pct)

    def _plan_for(self, mode: CompactionMode, entry_usage: float) -> _PassPlan:
        if mode is CompactionMode.OVERFLOW:
            # The provider counted more than the estimate: assume real usage
            # was at least at the trigger and scale the stop point to match.
            stop = self.target_pct * min(1.0, entry_usage / self.trigger_pct)
            return _PassPlan(
                mode,
                stop_pct=stop,
                escalate_pct=stop,
                phase4_pct=stop,
                protect_newest_turn=False,
                spend_recent_tail=True,
            )
        boundary = mode is CompactionMode.BOUNDARY
        return _PassPlan(
            mode,
            stop_pct=self.target_pct,
            escalate_pct=self.satisfied_pct,
            phase4_pct=None if boundary else self.force_pct,
            protect_newest_turn=boundary,
        )

    async def __call__(self, messages: list[Message], context: CompactionCallContext | None = None) -> bool:
        self._cache_telemetry.begin_request()
        try:
            return await self._compact(messages, context)
        finally:
            # Also when the pass was interrupted: what it changed stays, and
            # the next request is another one.
            self._cache_telemetry.request_prepared()

    async def _compact(self, messages: list[Message], context: CompactionCallContext | None) -> bool:
        # max() folds the retained legacy ``tool_definition_tokens`` spelling:
        # external constructors of CompactionCallContext may populate only it.
        self._request_overhead_floor = (
            max(context.request_overhead_tokens, context.tool_definition_tokens) if context is not None else 0
        )
        compressions_processed = await self._run_queued_compressions(messages)
        self._reset_stale_exclusions(messages)
        self._reinject_compressed_context_summaries(messages)
        current = self._annotate_and_count(messages)
        usage = self._usage_pct(current)
        below_trigger = usage < self.trigger_pct
        overflow_forced = self._context_overflow_pending
        if usage <= self.satisfied_pct:
            # Room to grow again, whatever made it (a pass, a fold the model
            # asked for, a shorter history): boundary passes may be booked,
            # and one already booked has nothing left to do.
            self._set_boundary_schedule(pending=False, suspended=False)
        boundary_booked = self._turn_boundary_pending

        if below_trigger and not overflow_forced and not boundary_booked:
            self._reported_skip = None
            return compressions_processed

        if not self._compaction_enabled:
            # A pass booked while compaction was on asks for nothing now,
            # and a booking alone is not a request that needed room.
            self._set_boundary_schedule(pending=False, suspended=self._boundary_suspended)
            if below_trigger and not overflow_forced:
                self._reported_skip = None
            else:
                self._report_skip(CompactionSkipReason.DISABLED, current)
            return compressions_processed

        tokens_before = current
        resolved = _resolve_turns(messages, self._state)

        if not resolved.spans:
            # ``compaction.skipped`` reports usage past the trigger; a pass the
            # overflow forced below it just waits for the next call.
            if below_trigger:
                self._reported_skip = None
            else:
                self._report_skip(CompactionSkipReason.TURNS_UNRESOLVED, current)
            return compressions_processed

        # Only REAL previous turns are eligible for P1/P2 mechanical
        # truncation — mid-turn user messages (injections, nudges) no longer
        # split the current task into fake "previous" turns.  The span COUNT
        # is insertion-stable (summaries are assistant messages and never
        # add or remove turns), so iterating by index over re-resolved
        # snapshots is safe.
        previous_count = len(resolved.spans) - 1

        # Strongest mode wins, and is fixed for the pass: an overflow or a
        # usage at the trigger lifts the boundary pass's limits.
        if overflow_forced:
            mode = CompactionMode.OVERFLOW
        elif not below_trigger:
            mode = CompactionMode.HARD
        else:
            mode = CompactionMode.BOUNDARY
        self._active_plan = self._plan_for(mode, usage)
        self._entry_request = (
            self._snapshot_entry_request(messages, context, usage) if not compressions_processed else None
        )

        run = CompactionRunTrace.open(
            next_exchange_operation_id=context.next_exchange_operation_id if context is not None else None
        )
        self._trajectory_run = run
        compaction_token = bind_compaction_operation(run.run_id if run is not None else None)
        changed = False
        try:
            if run is not None:
                await run.started(
                    trigger=mode.value,
                    tokens_before=tokens_before,
                    estimated_input_tokens=self._calibration.estimated_tokens(tokens_before),
                    trigger_tokens=self._trigger_tokens(),
                    max_context_tokens=self.max_context_tokens,
                )
            changed = await self._run_phase1(
                messages,
                previous_count=previous_count,
                usage=usage,
                tokens_before=tokens_before,
            )
            changed |= await self._run_phase2(messages)
            changed |= await self._run_phase3(messages)
            changed |= await self._run_phase4(messages, context)
        except BaseException:
            # Interrupted (or failed) mid-pass: only this path can close the
            # run, and a started run with no terminal marker reads as one that
            # is still going.  The overflow note and the boundary booking stay
            # as they were, so the retry runs this pass's mode again.
            if run is not None:
                run.finished_soon()
            raise
        finally:
            reset_compaction_operation(compaction_token)
            self._trajectory_run = None
            self._active_plan = None
            self._entry_request = None
        if not changed and mode is not CompactionMode.BOUNDARY:
            # The request still needs the room the pass was run for.
            self._report_skip(CompactionSkipReason.NOTHING_FREED, current)
        else:
            self._reported_skip = None
        self._finalize_pass(messages, changed, entry_tokens=current)
        # A completed pass answers every reason at or below its own mode.
        self._context_overflow_pending = False
        self._set_boundary_schedule(
            pending=False,
            suspended=self._usage_pct(self._last_included_tokens) > self.satisfied_pct,
        )
        if run is not None:
            await run.finished(tokens_before=tokens_before, tokens_after=self._last_included_tokens)
        return changed or compressions_processed

    def _snapshot_entry_request(
        self,
        messages: list[Message],
        context: CompactionCallContext | None,
        usage: float,
    ) -> tuple[Message, ...] | None:
        """The request as this pass found it, when its note may be asked for as an extension of it.

        That list is what the provider has cached: the previous request plus
        the newest tool results.  The pass rewrites it from Phase 1 on, so it
        is copied here, before any phase runs; a queued fold has already
        rewritten its head, and then nothing is taken.  Only for a pass that
        can reach Phase 4 with room for one more message: an overflow pass
        starts from a request the window refused.
        """
        plan = self._plan
        if (
            context is None
            or context.last_words_completer is None
            or not context.last_words_extends_request
            or plan.phase4_pct is None
            or plan.spend_recent_tail
            or usage <= plan.phase4_pct
        ):
            return None
        views: list[Message] = []
        for message in project_included_messages(messages):
            view = copy(message)
            view.contents = list(message.contents)
            view.additional_properties = dict(message.additional_properties)
            views.append(view)
        return tuple(views)

    def _trigger_tokens(self) -> int:
        """The trigger line in tokens, as the events report it."""
        # round(): the derived fraction is trigger_tokens / window, and ceil()
        # would turn its float error into one token too many.
        return round(self.trigger_pct * self.max_context_tokens)

    def _report_skip(self, reason_code: str, included: int) -> None:
        """Say why a request that needed room got none, once per stretch of such requests."""
        # Per session too: a restore that keeps this strategy starts a
        # session whose trajectory has not been told yet.
        trajectory = current_trajectory()
        skip = (reason_code, trajectory.session_id if trajectory is not None else None)
        if self._reported_skip == skip:
            return
        self._reported_skip = skip
        estimated = self._calibration.estimated_tokens(included)
        trigger_tokens = self._trigger_tokens()
        _log.log(
            logging.INFO if reason_code == CompactionSkipReason.DISABLED else logging.WARNING,
            "Compaction freed nothing: reason=%s, estimated_input_tokens=%d, trigger_tokens=%d, max_context_tokens=%d"
            if reason_code == CompactionSkipReason.NOTHING_FREED
            else "Compaction skipped past its trigger: reason=%s, estimated_input_tokens=%d, trigger_tokens=%d, "
            "max_context_tokens=%d",
            reason_code,
            estimated,
            trigger_tokens,
            self.max_context_tokens,
        )
        record_compaction_skipped(
            reason_code=reason_code,
            estimated_input_tokens=estimated,
            trigger_tokens=trigger_tokens,
            max_context_tokens=self.max_context_tokens,
        )

    async def _run_queued_compressions(self, messages: list[Message]) -> bool:
        return await self._compression.run_queued(messages)

    def _settle_after_fold(self, messages: list[Message]) -> None:
        """Make *messages* the request a fold leaves: reminders re-attached, ids, groups and token counts current.

        Run before the fold is reported, so the count it reports includes the
        summary that replaced the turn and the reminders that moved off it.
        """
        self._restore_folded_reminders(messages)
        _dedup_message_ids(messages)
        annotate_message_groups(messages, force_reannotate=True)
        annotate_token_counts(messages, tokenizer=self._tokenizer, force_retokenize=True)

    def _restore_folded_reminders(self, messages: list[Message]) -> int | None:
        """Re-attach catalog reminders whose carrier a fold just excluded.

        Returns the rebuilt index; token counts from there on are stale.
        """
        if self._reminder_middleware is None:
            return None
        return self._reminder_middleware.restore_folded_reminders(messages)

    def _reset_stale_exclusions(self, messages: list[Message]) -> None:
        # ---- Step 1: Handle stale _excluded flags ----
        #
        # ``project_included_messages`` filters messages with _excluded=True,
        # and those flags can outlive a single compaction pass on shared
        # Message objects or restored state.
        #
        # If this strategy has previously compacted groups, re-inject any
        # cached summaries missing from the current list so excluded originals
        # still have a visible replacement. Flags on compacted messages are
        # kept, preventing redundant re-compaction and duplicate events.
        #
        # If this strategy has never compacted (fresh instance or different
        # instance), reset stale foreign _excluded flags while preserving
        # persisted Chrys compaction reasons.
        self._exclusions.reset_stale_exclusions(messages)

    def _annotate_and_count(self, messages: list[Message]) -> int:
        # De-duplicate message IDs to prevent cross-run group merging
        _dedup_message_ids(messages)

        # Re-annotate groups with force so group IDs reflect the deduped
        # message IDs.
        annotate_message_groups(messages, force_reannotate=True)
        annotate_token_counts(messages, tokenizer=self._tokenizer)
        # Reminders on a user message change between calls (a later drop
        # rewrites its last-words note, a turn adds notices), so its stored
        # count can be another call's. There are about as many as turns.
        recount_user_messages(messages, tokenizer=self._tokenizer)

        current = included_token_count(messages)
        self._last_included_tokens = current

        return current

    def _trim_groups_to_target(
        self,
        messages: list[Message],
        group_ids: list[str],
        grouped: dict[str, list[Message]],
        operation: Callable[[list[Message], str, list[Message]], list[str] | None],
    ) -> tuple[bool, int, list[str]]:
        changed = False
        compacted_count = 0
        tool_names: list[str] = []
        for group_id in group_ids:
            group_msgs = grouped.get(group_id, [])
            result = operation(messages, group_id, group_msgs)
            if result:
                changed = True
                compacted_count += 1
                tool_names.extend(result)

                if self._usage_pct(included_token_count(messages)) <= self._plan.stop_pct:
                    break

        return changed, compacted_count, tool_names

    async def _run_phase1(
        self,
        messages: list[Message],
        *,
        previous_count: int,
        usage: float,
        tokens_before: int,
    ) -> bool:
        changed = False
        plan = self._plan
        if plan.protect_newest_turn:
            previous_count = max(0, previous_count - 1)

        # ---- Phase 1: Summarise previous turns' tool results ----
        if previous_count:
            await self._fire_pre_compact("phase1", usage_pct=usage, tokens_before=tokens_before)
        p1_count = 0
        p1_tool_names: list[str] = []
        p1_turn_numbers: list[int] = []

        for turn_idx in range(previous_count):
            # ONE COHERENT SNAPSHOT per turn iteration: spans and all four
            # group projections recomputed together.  _compact_group both
            # inserts a summary message and re-annotates the suffix, so
            # projections from different mutation generations mix stale
            # group ids with fresh indices — stale spans silently skip a
            # later turn's shifted tail groups (under-compaction) and
            # mis-bucket them into the wrong turn's event numbers.
            resolved = _resolve_turns(messages, self._state)
            span = resolved.previous[turn_idx]
            ordered_ids = _ordered_group_ids(messages)
            kinds = _group_kind_map(messages)
            grouped = _group_messages_by_id(messages)
            starts = _group_start_indices(messages)
            turn_groups = _tool_groups_in_range(span.start, span.end, ordered_ids, kinds, starts, grouped)

            turn_compacted, turn_count, turn_tool_names = self._trim_groups_to_target(
                messages,
                turn_groups,
                grouped,
                self._compact_group,
            )
            if turn_compacted:
                changed = True
                self._note_prefix_rewrite(head=True)
                p1_count += turn_count
                p1_tool_names.extend(turn_tool_names)

            if turn_compacted:
                p1_turn_numbers.append(span.absolute_number)

            if self._usage_pct(included_token_count(messages)) <= plan.stop_pct:
                break

        if p1_count > 0:
            await self._notify_compaction(
                CompactionInfo(
                    compacted_groups=p1_count,
                    phase="phase1",
                    turn_numbers=p1_turn_numbers,
                    tool_names=p1_tool_names,
                    tokens_before=tokens_before,
                    tokens_after=included_token_count(messages),
                )
            )

        return changed

    async def _run_phase2(self, messages: list[Message]) -> bool:
        changed = False

        # ---- Phase 2: Remove previous turns' tool-call groups entirely ----
        p2_count = 0
        p2_tool_names: list[str] = []
        p2_turn_numbers: list[int] = []
        plan = self._plan

        # Escalate only above the plan's gate: once an earlier phase left
        # enough room, removing the calls themselves buys little.
        if self._usage_pct(included_token_count(messages)) > plan.escalate_pct:
            p2_tokens_before = included_token_count(messages)
            # Re-resolve at phase entry — Phase 1 insertions may have shifted indices
            resolved = _resolve_turns(messages, self._state)
            previous_spans = resolved.previous[:-1] if plan.protect_newest_turn else resolved.previous
            if previous_spans:
                await self._fire_pre_compact(
                    "phase2",
                    usage_pct=self._usage_pct(p2_tokens_before),
                    tokens_before=p2_tokens_before,
                )
                ordered_ids = _ordered_group_ids(messages)
                kinds = _group_kind_map(messages)

                # _remove_group does not insert messages, so the phase-entry
                # spans stay valid across iterations.
                for span in previous_spans:
                    grouped = _group_messages_by_id(messages)
                    starts = _group_start_indices(messages)
                    # Use _any_ variant: Phase 1 already excluded originals,
                    # but summaries are still visible and need removal.
                    turn_groups = _any_tool_groups_in_range(span.start, span.end, ordered_ids, kinds, starts)

                    turn_removed, turn_count, turn_tool_names = self._trim_groups_to_target(
                        messages,
                        turn_groups,
                        grouped,
                        self._remove_group,
                    )
                    if turn_removed:
                        changed = True
                        self._note_prefix_rewrite(head=True)
                        p2_count += turn_count
                        p2_tool_names.extend(turn_tool_names)

                    if turn_removed:
                        p2_turn_numbers.append(span.absolute_number)

                    if self._usage_pct(included_token_count(messages)) <= plan.stop_pct:
                        break

            if p2_count > 0:
                await self._notify_compaction(
                    CompactionInfo(
                        compacted_groups=p2_count,
                        phase="phase2",
                        turn_numbers=p2_turn_numbers,
                        tool_names=p2_tool_names,
                        tokens_before=p2_tokens_before,
                        tokens_after=included_token_count(messages),
                    )
                )

        return changed

    async def _run_phase3(self, messages: list[Message]) -> bool:
        changed = False

        # ---- Phase 3: fold old text turns before touching current work ----
        #
        # Phase 1/2 only reduce tool-call groups.  Once old tool results have
        # been summarised/removed, long-running text-heavy sessions can still
        # exceed the request window with no remaining tool group to compact.
        # Fold completed turns by marker here, before Phase 4 considers
        # dropping current-turn tool work.
        p3_count = 0
        plan = self._plan
        if self._usage_pct(included_token_count(messages)) > plan.escalate_pct:
            while self._usage_pct(included_token_count(messages)) > plan.stop_pct:
                p3_tokens_before = included_token_count(messages)
                progressed, compressed = await self._emergency_compress_oldest_turn(
                    messages,
                    usage_pct=self._usage_pct(p3_tokens_before),
                    tokens_before=p3_tokens_before,
                )
                if not progressed:
                    break
                if not compressed:
                    continue
                p3_count += 1
                changed = True

        return changed

    async def _run_phase4(
        self,
        messages: list[Message],
        context: CompactionCallContext | None,
    ) -> bool:
        return await self._current_turn_drop.run(messages, context)

    def _finalize_pass(self, messages: list[Message], changed: bool, *, entry_tokens: int) -> None:
        post_compaction = included_token_count(messages)
        self._last_included_tokens = post_compaction if changed else entry_tokens

        if changed:
            self._write_reminder_debug()

        # Final snapshot for after_run propagation — Phase-4 rounds already
        # snapshotted inside their synchronous commit blocks; this pass adds
        # exclusions from the non-Phase-4 paths above.
        self._snapshot_excluded_anchors(messages)

    def _snapshot_excluded_anchors(self, messages: list[Message]) -> None:
        """Delegate the test-visible exclusion-anchor snapshot seam."""
        self._exclusions.snapshot_excluded_anchors(messages)

    async def _emergency_compress_oldest_turn(
        self,
        messages: list[Message],
        *,
        usage_pct: float,
        tokens_before: int,
    ) -> tuple[bool, bool]:
        return await self._compression.emergency_compress_oldest_turn(
            messages,
            usage_pct=usage_pct,
            tokens_before=tokens_before,
            protect_newest_turn=self._plan.protect_newest_turn,
        )

    def _reinject_compressed_context_summaries(self, messages: list[Message]) -> None:
        """Delegate the test-visible compressed-summary reinjection seam."""
        self._compression.reinject_summaries(messages)

    def _compact_group(
        self,
        messages: list[Message],
        group_id: str,
        group_msgs: list[Message],
    ) -> list[str] | None:
        """Compact one Phase-1 group and commit its replacement to owned history."""
        result = _compact_group(
            messages,
            group_id,
            group_msgs,
            ledger=self._exclusions,
            tokenizer=self._tokenizer,
        )
        if result is not None and self._state is not None:
            # Phase 1 only replaces completed-turn history. Commit its visible
            # replacement synchronously with exclusion: a failed/cancelled
            # provider call skips after_run but still saves the excluded state.
            self._exclusions.reinject_cached_summaries(self._state.get("messages", []))
            self._compression._record_pre_output_history_len()
        return result

    def _remove_group(
        self,
        messages: list[Message],
        group_id: str,
        group_msgs: list[Message],
    ) -> list[str] | None:
        """Delegate the test-visible Phase-2 group operation."""
        return _remove_group(messages, group_id, group_msgs, ledger=self._exclusions)
