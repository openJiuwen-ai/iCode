# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Local estimates of how much model text has arrived and how fast it is arriving."""

from __future__ import annotations

from collections import deque

from chrys.foundation.text.tokenizer import TOKEN_UNITS_PER_TOKEN, MixedLanguageTokenizer

_TOKENIZER = MixedLanguageTokenizer()
_WINDOW_SECONDS = 5.0
"""Look back only this far so the rate follows recent output."""
_IDLE_SECONDS = 3.0
"""Hide a stale rate while the model is silent, for example during a tool call."""
_MIN_SPAN_SECONDS = 1.0
"""A burst needs a measurable span before its rate stops being dominated by the first arrivals."""


class StreamTokens:
    """Count deltas without rounding each chunk, and measure speed over recent arrivals."""

    def __init__(self) -> None:
        self._units = 0
        self._samples: deque[tuple[float, int]] = deque()

    def note(self, text: str, now: float) -> None:
        if text:
            self._units += _TOKENIZER.count_token_units(text)
            # A timestamp has one cumulative count, even when a burst arrives in several chunks.
            if self._samples and self._samples[-1][0] == now:
                self._samples.pop()
            self._samples.append((now, self._units))
            self._drop_old_samples(now)

    @property
    def tokens(self) -> int:
        return self._units // TOKEN_UNITS_PER_TOKEN

    def _drop_old_samples(self, now: float) -> None:
        while self._samples and self._samples[0][0] < now - _WINDOW_SECONDS:
            self._samples.popleft()

    def speed(self, now: float) -> int | None:
        """Tokens per second between arrivals, once they span a second. A stalled stream has no rate."""
        self._drop_old_samples(now)
        if not self._samples:
            return None
        first_at, first_units = self._samples[0]
        last_at, last_units = self._samples[-1]
        span = last_at - first_at
        if span < _MIN_SPAN_SECONDS or now - last_at > _IDLE_SECONDS:
            return None
        # The first sample's text arrived before this span began.
        return max(1, round((last_units - first_units) / TOKEN_UNITS_PER_TOKEN / span))
