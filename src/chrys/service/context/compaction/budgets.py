# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model-derived token budgets for context compaction."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from chrys.service.profiles.models.schema import DEFAULT_MAX_OUTPUT_TOKENS

if TYPE_CHECKING:
    from chrys.service.profiles.models.schema import ModelProfile

_log = logging.getLogger(__name__)

TRIGGER_SAFETY_MARGIN_MIN_TOKENS = 5_000
TRIGGER_SAFETY_MARGIN_PCT_NUM = 5
TRIGGER_SAFETY_MARGIN_PCT_DEN = 100
TRIGGER_FLOOR_PCT_NUM = 30
TRIGGER_FLOOR_PCT_DEN = 100
MIN_DERIVABLE_CONTEXT_TOKENS = 100


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


@dataclass(frozen=True)
class CompactionBudgets:
    """Absolute compaction thresholds derived from one model profile."""

    max_context_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    trigger_tokens: int
    target_tokens: int
    satisfied_tokens: int
    force_compress_tokens: int
    floor_clamped: bool

    @property
    def trigger_pct(self) -> float:
        """Compaction trigger as a fraction of the context window."""
        return self.trigger_tokens / self.max_context_tokens

    @property
    def target_pct(self) -> float:
        """Post-compaction target as a fraction of the context window."""
        return self.target_tokens / self.max_context_tokens

    @property
    def satisfied_pct(self) -> float:
        """Usage at or below which a pass stops escalating, as a context fraction."""
        return self.satisfied_tokens / self.max_context_tokens

    @property
    def force_compress_pct(self) -> float:
        """Turn-boundary high-water mark and Phase 4 entry gate as a context fraction."""
        return self.force_compress_tokens / self.max_context_tokens


def derive_compaction_budgets(profile: ModelProfile) -> CompactionBudgets:
    """Derive safe, ordered compaction thresholds from ``profile``."""
    max_context = profile.max_context_tokens
    if max_context < MIN_DERIVABLE_CONTEXT_TOKENS:
        raise ValueError(f"max_context_tokens must be at least {MIN_DERIVABLE_CONTEXT_TOKENS}; got {max_context}")

    margin = max(
        TRIGGER_SAFETY_MARGIN_MIN_TOKENS,
        _ceil_div(max_context * TRIGGER_SAFETY_MARGIN_PCT_NUM, TRIGGER_SAFETY_MARGIN_PCT_DEN),
    )
    output_reserve = profile.max_output_tokens if profile.max_output_tokens > 0 else DEFAULT_MAX_OUTPUT_TOKENS
    trigger = max_context - output_reserve - margin
    floor = _ceil_div(max_context * TRIGGER_FLOOR_PCT_NUM, TRIGGER_FLOOR_PCT_DEN)
    floor_clamped = trigger < floor
    if floor_clamped:
        trigger = floor
        _log.warning(
            "ModelProfile %r compaction trigger floor-clamped: max_context_tokens=%d, max_output_tokens=%d",
            profile.name,
            max_context,
            profile.max_output_tokens,
        )

    # Four ordered lines, an eighth of the trigger apart: a pass stops at the
    # target, stops escalating once at or below the satisfied line, and a turn
    # that ends above the force line books a turn-boundary pass.
    target = (5 * trigger + 4) // 8
    satisfied = (6 * trigger + 4) // 8
    force = (7 * trigger + 4) // 8
    force = max(min(force, trigger - 1), 2)
    target = max(min(target, force - 1), 1)
    satisfied = max(min(satisfied, force), target)
    return CompactionBudgets(
        max_context_tokens=max_context,
        output_reserve_tokens=output_reserve,
        safety_margin_tokens=margin,
        trigger_tokens=trigger,
        target_tokens=target,
        satisfied_tokens=satisfied,
        force_compress_tokens=force,
        floor_clamped=floor_clamped,
    )
