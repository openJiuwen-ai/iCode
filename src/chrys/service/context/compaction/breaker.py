# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Current-turn drop breaker transitions and pressure emission."""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from .last_words import SPEND_BUDGET_FAILURE_REASON
from .last_words_state import DropRoundBreakerState

if TYPE_CHECKING:
    from .strategy import UnifiedContextStrategy

_log = logging.getLogger(__name__)

# Primary current-turn-drop safety guard: at most this many rounds per
# logical turn.  A round replaces most of a window of work with a note, so a
# turn that needs more than these is one the notes no longer carry.
MAX_DROP_ROUNDS_PER_TURN = 8
_DROP_MIN_PROGRESS_PCT = 0.05

# Human-readable card text for an entry-time breaker trip, keyed by the
# breaker reason.  "disabled" is deliberately absent: after the first trip
# every later trigger re-enters through it, and one failure card per turn
# is enough.
_BREAKER_TRIP_FAILURE_REASONS = {
    "round_limit": f"{MAX_DROP_ROUNDS_PER_TURN} attempts limit exceeded for current turn",
    "side_call_budget": SPEND_BUDGET_FAILURE_REASON,
}


class DropBreakerController:
    """Own persisted drop-breaker transitions and pressure tasks."""

    def __init__(self, strategy: UnifiedContextStrategy) -> None:
        self._strategy = strategy
        self._context_pressure_tasks: set[asyncio.Future[None]] = set()

    def enter_round(self) -> tuple[DropRoundBreakerState | None, str]:
        """Synchronously trip or increment the persisted breaker.

        Returns ``(entered_state, "")`` when the round may run, or
        ``(None, reason)`` when the breaker refused entry (the breaker is
        force-disabled and the pressure event fires as a side effect).
        """
        last_words = self._strategy._last_words_state
        if last_words is None:
            return None, ""
        breaker = last_words.get_drop_round_breaker()
        reason = ""
        if breaker.disabled:
            reason = "disabled"
        elif breaker.attempts >= MAX_DROP_ROUNDS_PER_TURN:
            reason = "round_limit"
        elif 0 <= self._strategy._phase4_side_call_token_budget <= breaker.side_call_tokens:
            reason = "side_call_budget"
        if reason:
            disabled = replace(breaker, disabled=True)
            last_words.set_drop_round_breaker(disabled)
            self.emit_context_pressure(reason, disabled)
            return None, reason
        entered = replace(breaker, attempts=breaker.attempts + 1)
        last_words.set_drop_round_breaker(entered)
        return entered, ""

    async def publish_trip(self, reason: str) -> None:
        """Publish the first entry-time trip card, when applicable.

        Only reasons with a mapped message publish — the first trip per turn.
        Later triggers re-enter through "disabled" and stay silent (the
        edge-triggered pressure event already covered them).
        """
        message = _BREAKER_TRIP_FAILURE_REASONS.get(reason)
        generator = self._strategy._last_words_generator
        if message is None or generator is None:
            return
        await generator.publish_breaker_trip(message)

    def spend_side_call_tokens(self, estimated_tokens: int) -> bool:
        """Charge one imminent provider attempt and report admission.

        Always accumulates the estimate for observability; an unlimited
        budget (negative) never refuses.
        """
        last_words = self._strategy._last_words_state
        if last_words is None:
            return False
        breaker = last_words.get_drop_round_breaker()
        charged = replace(breaker, side_call_tokens=breaker.side_call_tokens + max(0, estimated_tokens))
        last_words.set_drop_round_breaker(charged)
        if self._strategy._phase4_side_call_token_budget < 0:
            return True
        return charged.side_call_tokens < self._strategy._phase4_side_call_token_budget

    def record_no_progress(self, reason: str) -> DropRoundBreakerState | None:
        """Record one failed or low-yield round and apply escalation."""
        last_words = self._strategy._last_words_state
        if last_words is None:
            return None
        breaker = last_words.get_drop_round_breaker()
        consecutive = breaker.consecutive_no_progress + 1
        updated = replace(
            breaker,
            consecutive_no_progress=consecutive,
            tail_override=True,
            disabled=breaker.disabled or consecutive >= 2,
        )
        last_words.set_drop_round_breaker(updated)
        if updated.disabled:
            self.emit_context_pressure(reason, updated)
        return updated

    def record_progress(self, entry_usage_pct: float, post_usage_pct: float) -> DropRoundBreakerState | None:
        """Update progress state from the synchronous post-drop sample."""
        if entry_usage_pct - post_usage_pct < _DROP_MIN_PROGRESS_PCT:
            return self.record_no_progress("no_progress")
        last_words = self._strategy._last_words_state
        if last_words is None:
            return None
        breaker = last_words.get_drop_round_breaker()
        updated = replace(breaker, consecutive_no_progress=0)
        last_words.set_drop_round_breaker(updated)
        return updated

    def emit_context_pressure(self, reason: str, breaker: DropRoundBreakerState) -> None:
        """Schedule a pressure event without awaiting inside a state commit."""
        callback = self._strategy._on_context_pressure
        last_words = self._strategy._last_words_state
        if callback is None or last_words is None:
            return
        if not last_words.claim_context_pressure_notification():
            return
        try:
            pending = callback(reason, breaker, self._strategy._phase4_side_call_token_budget)
        except Exception:
            last_words.release_context_pressure_notification()
            _log.debug("Failed to create context-pressure event", exc_info=True)
            return
        if pending is None or not inspect.isawaitable(pending):
            return
        task = asyncio.ensure_future(pending)
        self._context_pressure_tasks.add(task)
        task.add_done_callback(self._context_pressure_event_done)

    def _context_pressure_event_done(self, task: asyncio.Future[None]) -> None:
        self._context_pressure_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            _log.debug("Failed to publish context-pressure event", exc_info=True)
