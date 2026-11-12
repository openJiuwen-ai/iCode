# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Context usage: the ``[Context Usage]`` line and the high-usage warning.

The warning is an event reminder armed while usage stays below the threshold:
it goes out on the turn usage reaches the threshold, and again only after
usage fell below it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from chrys.kernel import Message


CONTEXT_USAGE_WARNING = (
    "[Context Usage] WARNING: Context usage is high. The platform compacts older content "
    "on its own, so no action is needed. If a large piece of finished work no longer needs "
    "its details, you may fold it with one `compress_context` call up to the newest marker "
    "that fits (`list_compressed_contexts` lists them); `recall_context` queries a "
    "compressed block later."
)
"""Sent once when context usage reaches the warning threshold.

It reports the state and leaves the decision with the model: telling it to fold
now makes an obedient model rewrite the prompt head while nothing is short.
"""

CONTEXT_USAGE_WARNING_MANUAL = (
    "[Context Usage] WARNING: Context usage is high, and automatic compaction is turned off "
    "for this agent. Use `list_compressed_contexts` to see available fold markers, then "
    "`compress_context` with a marker_id to compress older turns into a summary. Use "
    "`recall_context` to query compressed blocks if you need specific details later."
)
"""The warning where nothing compacts on its own: only a fold by the model frees context."""


def context_usage_percent(usage: Mapping[str, Any], *, max_context_tokens: int) -> float:
    """The share of the context window *usage* fills, in percent (one decimal)."""
    return round(_total_tokens(usage) / max_context_tokens * 100, 1) if max_context_tokens else 0.0


def format_usage_line(usage: Mapping[str, Any], *, max_context_tokens: int) -> str:
    """The ``[Context Usage]`` line for *usage*."""
    total_pct = context_usage_percent(usage, max_context_tokens=max_context_tokens)
    return f"[Context Usage] current: {total_pct}% ({_total_tokens(usage):,}/{max_context_tokens:,})"


def _total_tokens(usage: Mapping[str, Any]) -> int:
    input_tokens = usage.get("input_token_count") or 0
    output_tokens = usage.get("output_token_count") or 0
    return usage.get("total_token_count") or (input_tokens + output_tokens)


class ContextWarningSource:
    """The context-usage warning one middleware offers, and whether it is armed."""

    def __init__(
        self, *, max_context_tokens: int, warn_threshold_pct: float, automatic_compaction: bool = True
    ) -> None:
        self._max_context_tokens = max_context_tokens
        self._warn_threshold_pct = warn_threshold_pct
        self._warning = CONTEXT_USAGE_WARNING if automatic_compaction else CONTEXT_USAGE_WARNING_MANUAL
        # Whether the next turn at or above the warning threshold warns: a
        # request carrying the warning disarms it, a turn below re-arms it.
        # A new middleware (rebuild, restart) warns again at most once.
        self._armed = True

    @property
    def armed(self) -> bool:
        """Whether the next turn at or above the threshold warns."""
        return self._armed

    def snapshot(self, usage: Mapping[str, Any]) -> str | None:
        """The warning for a turn starting after *usage*, while armed; usage below the threshold re-arms it."""
        if not usage:
            return None
        usage_pct = context_usage_percent(usage, max_context_tokens=self._max_context_tokens)
        if usage_pct < self._warn_threshold_pct * 100:
            self._armed = True
            return None
        return self._warning if self._armed else None

    def delivered(self, _messages: Sequence[Message]) -> None:
        """Request observer: a request carried the warning, which disarms it."""
        self._armed = False
