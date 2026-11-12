# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared Phase 4 test stubs: fake ``LastWordsGenerator`` + reminder middleware.

The real generator reaches into the LLM client and the real middleware into
Chrys middleware machinery; tests that exercise compaction / Phase 4 wiring
need lightweight stand-ins that just record what they were asked to do.  The
LAST_WORDS state itself is pure, so the stub middleware carries a real one.
"""

from __future__ import annotations

from chrys.service.agent_middleware.system_reminder import ReminderTurns, TurnReminderState
from chrys.service.context.compaction.last_words_state import LastWordsState


class StubLastWordsGenerator:
    """Test stub for ``LastWordsGenerator`` — records calls, returns fixed text."""

    def __init__(self, text: str = "[STUB LAST_WORDS progress note]") -> None:
        self.text = text
        self.calls: list[dict] = []
        self.breaker_trips: list[str] = []
        self.committed_publishes = 0

    async def publish_breaker_trip(self, failure_reason: str) -> None:
        self.breaker_trips.append(failure_reason)

    async def publish_committed(self) -> None:
        self.committed_publishes += 1

    async def generate(
        self,
        scoped_groups: list,
        previous_last_words: str | None,
        *,
        degraded_opener: bool,
        has_continuation_nudges: bool,
        completer: object | None = None,
        tokenizer: object | None = None,
        system_overhead_tokens: int = 0,
        tool_definition_tokens: int = 0,
        request_overhead_tokens: int = 0,
        calibration_ratio: float = 1.0,
        spend_side_call_tokens: object | None = None,
        affords_side_call_tokens: object | None = None,
        entry_messages: object | None = None,
    ) -> str:
        self.calls.append(
            {
                "entry_messages": entry_messages,
                "scoped_groups": list(scoped_groups),
                "previous_last_words": previous_last_words,
                "has_continuation_nudges": has_continuation_nudges,
                "degraded_opener": degraded_opener,
                "completer": completer,
                "tokenizer": tokenizer,
                "system_overhead_tokens": system_overhead_tokens,
                "tool_definition_tokens": tool_definition_tokens,
                "request_overhead_tokens": request_overhead_tokens,
                "calibration_ratio": calibration_ratio,
                "spend_side_call_tokens": spend_side_call_tokens,
            }
        )
        return self.text


class StubReminderMiddleware:
    """Test stub for the strategy-facing ``SystemReminderMiddleware`` calls.

    It renders the real ``LastWordsState`` it carries (``last_words``), so
    ``bind_reminder`` accepts the pair.  The default state starts on an
    installed turn, as a prepared one does, so every task sees the same note.
    """

    def __init__(self, last_words: LastWordsState | None = None) -> None:
        if last_words is None:
            turns = ReminderTurns()
            turns.install(TurnReminderState())
            last_words = LastWordsState(turns)
        self.last_words = last_words
        self.refresh_calls: list[list] = []
        self.restore_folded_calls: list[list] = []

    def renders_last_words(self, state: object) -> bool:
        return state is self.last_words

    def refresh_last_words_reminder(self, messages: list) -> int | None:
        """Record the refresh request; never rewrites the stub's message list."""
        self.refresh_calls.append(messages)
        return None

    def restore_folded_reminders(self, messages: list) -> int | None:
        """Record the request; the stub carries no catalogs, so nothing is re-sent."""
        self.restore_folded_calls.append(messages)
        return None
