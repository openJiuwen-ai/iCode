# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Whether each request found the prefix of the one before it in the provider's cache.

Between two compactions the history is only appended to, so a request should
read everything the previous one sent from the cache.  What it had to read
at full price instead is a miss, and a miss is put down to what this process
knows changed the prefix: a compaction, or two requests further apart than a
provider keeps a prefix cached.  A miss with neither cause is the one to
investigate.

Rewrites of the head of the request (previous turns summarised, removed or
folded) are also counted against the turns they happened in, once per
request however many steps rewrote it: several within a few turns mean
compaction is undoing the cache it exists to protect.

The tracker only decides; the strategy that owns it logs and records what it
returns.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.foundation.trajectory.event_types import PromptCacheMissCause

if TYPE_CHECKING:
    from collections.abc import Callable

CACHE_MISS_MIN_TOKENS = 1_024
"""A read may fall this far short of the previous request with nothing changed: providers cache whole blocks."""

CACHE_IDLE_SECONDS = 300.0
"""The shortest time a provider keeps an unread prefix cached, counted from the request that last read it."""

THRASH_REWRITES = 3
THRASH_WINDOW_TURNS = 10
"""This many head rewrites within this many turns are reported as thrash."""


@dataclass(frozen=True, slots=True)
class PromptCacheMiss:
    """One request that re-read at full price what the request before it had sent."""

    cause: str
    """A ``PromptCacheMissCause`` code."""
    missed_tokens: int
    input_tokens: int
    previous_input_tokens: int
    cache_read_tokens: int


class PromptCacheTelemetry:
    """Judge each response's cache read against the request before it, and count head rewrites per turn."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._previous_input: int | None = None
        self._previous_sent_at = 0.0
        self._rewritten = False
        # The request being prepared, and when its preparation ended.
        self._request = 0
        self._request_at: float | None = None
        self._head_counted_for: int | None = None
        # A provider that never reports a cache read has no cache to miss:
        # nothing is judged until one response has read from it.
        self._cache_seen = False
        self._head_rewrite_turns: deque[int] = deque()

    def begin_request(self) -> None:
        """A request is being prepared: rewrites noted from here on are part of it."""
        self._request += 1

    def request_prepared(self) -> None:
        """The request is about to be sent.

        Taken after the preparation, which can itself take minutes (a hook, a
        note written by a side call): the pause that lets a cache expire is
        the one between two requests reaching the provider.
        """
        self._request_at = self._clock()

    def note_rewrite(
        self, *, head: bool, turn: int | None, before_next_request: bool = False
    ) -> tuple[int, ...] | None:
        """Note that the next request's prefix differs from what the provider cached.

        A *head* rewrite changed what precedes the current turn and is counted
        against *turn*, the number of turns the session has completed; without
        a turn number it is only noted.  The provider is sent one changed
        prefix however many steps changed it, so a request counts once;
        *before_next_request* is a rewrite made between two requests, which
        the next one carries.  Returns the turns of the counted rewrites once
        they amount to thrash, and starts counting afresh.
        """
        self._rewritten = True
        if not head or turn is None:
            return None
        request = self._request + 1 if before_next_request else self._request
        if self._head_counted_for == request:
            return None
        self._head_counted_for = request
        turns = self._head_rewrite_turns
        if turns and turn < turns[-1]:
            # The session was rewound: turns counted after this one no
            # longer exist, and would never age out of the window.
            turns.clear()
        turns.append(turn)
        while turns and turn - turns[0] >= THRASH_WINDOW_TURNS:
            turns.popleft()
        if len(turns) < THRASH_REWRITES:
            return None
        thrash = tuple(turns)
        turns.clear()
        return thrash

    def observe_response(self, *, input_tokens: int, cache_read_tokens: int) -> PromptCacheMiss | None:
        """Judge one response's usage; returns the miss, if the request was one.

        *input_tokens* is everything the request sent, cache reads included.
        Usage that does not say what the request sent (a failed response
        that reports its output alone) is no observation: taken as a request
        of nothing, it would hide the miss of the request after it.
        """
        if input_tokens <= 0:
            return None
        # When the request was sent, not when its answer was complete: the
        # provider decided hit or miss before it generated anything.
        sent_at = self._request_at if self._request_at is not None else self._clock()
        self._request_at = None
        previous, previous_sent_at, rewritten = self._previous_input, self._previous_sent_at, self._rewritten
        self._previous_input, self._previous_sent_at, self._rewritten = input_tokens, sent_at, False
        if cache_read_tokens > 0:
            self._cache_seen = True
        if previous is None or not self._cache_seen:
            return None
        # A request shorter than the one before it (a retry after a rollback,
        # a compacted one) can only read as much as it sends.
        missed = min(previous, input_tokens) - cache_read_tokens
        if missed <= CACHE_MISS_MIN_TOKENS:
            return None
        if rewritten:
            cause = PromptCacheMissCause.COMPACTION
        elif sent_at - previous_sent_at > CACHE_IDLE_SECONDS:
            cause = PromptCacheMissCause.IDLE
        else:
            cause = PromptCacheMissCause.UNKNOWN
        return PromptCacheMiss(
            cause=cause,
            missed_tokens=missed,
            input_tokens=input_tokens,
            previous_input_tokens=previous,
            cache_read_tokens=cache_read_tokens,
        )
