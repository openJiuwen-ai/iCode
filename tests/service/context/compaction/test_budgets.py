# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for model-derived compaction token budgets."""

from __future__ import annotations

import logging

import pytest

from chrys.service.context.compaction.budgets import (
    MIN_DERIVABLE_CONTEXT_TOKENS,
    CompactionBudgets,
    derive_compaction_budgets,
)
from chrys.service.profiles.models.schema import DEFAULT_MAX_OUTPUT_TOKENS, ModelProfile


def _profile(context: int, output: int, *, name: str = "Budget Test") -> ModelProfile:
    return ModelProfile(
        id="budget-test",
        name=name,
        max_context_tokens=context,
        max_output_tokens=output,
    )


@pytest.mark.parametrize(
    ("context", "output", "expected"),
    [
        (200_000, 20_000, CompactionBudgets(200_000, 20_000, 10_000, 170_000, 106_250, 127_500, 148_750, False)),
        (200_000, 30_000, CompactionBudgets(200_000, 30_000, 10_000, 160_000, 100_000, 120_000, 140_000, False)),
        (100_000, 32_000, CompactionBudgets(100_000, 32_000, 5_000, 63_000, 39_375, 47_250, 55_125, False)),
        (100_000, 64_000, CompactionBudgets(100_000, 64_000, 5_000, 31_000, 19_375, 23_250, 27_125, False)),
        (100_000, 70_000, CompactionBudgets(100_000, 70_000, 5_000, 30_000, 18_750, 22_500, 26_250, True)),
    ],
)
def test_worked_budget_rows(context: int, output: int, expected: CompactionBudgets) -> None:
    assert derive_compaction_budgets(_profile(context, output)) == expected


@pytest.mark.parametrize(
    ("context", "expected_margin"),
    [(99_999, 5_000), (100_000, 5_000), (100_001, 5_001), (200_000, 10_000)],
)
def test_safety_margin_uses_five_thousand_floor_then_five_percent_ceiling(
    context: int,
    expected_margin: int,
) -> None:
    assert derive_compaction_budgets(_profile(context, 1_000)).safety_margin_tokens == expected_margin


def test_floor_clamp_boundary_and_warning(caplog: pytest.LogCaptureFixture) -> None:
    exact = derive_compaction_budgets(_profile(100_000, 65_000))
    assert exact.trigger_tokens == 30_000
    assert not exact.floor_clamped

    with caplog.at_level(logging.WARNING):
        clamped = derive_compaction_budgets(_profile(100_000, 65_001, name="Clamped Profile"))

    assert clamped.trigger_tokens == 30_000
    assert clamped.floor_clamped
    assert "Clamped Profile" in caplog.text
    assert "max_context_tokens=100000" in caplog.text
    assert "max_output_tokens=65001" in caplog.text


def test_target_and_force_use_half_up_integer_rounding() -> None:
    budgets = derive_compaction_budgets(_profile(100_000, 31_996))

    assert budgets.trigger_tokens == 63_004
    assert budgets.target_tokens == 39_378
    assert budgets.satisfied_tokens == 47_253
    assert budgets.force_compress_tokens == 55_129


@pytest.mark.parametrize("context", [1, 2, 99])
def test_context_below_derivable_minimum_raises(context: int) -> None:
    with pytest.raises(ValueError, match=str(MIN_DERIVABLE_CONTEXT_TOKENS)):
        derive_compaction_budgets(_profile(context, 1))


@pytest.mark.parametrize("output", [0, -1, -20_000])
def test_non_positive_output_uses_default_for_direct_profiles(output: int) -> None:
    budgets = derive_compaction_budgets(_profile(200_000, output))

    assert budgets.output_reserve_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert budgets.trigger_tokens == 158_000


@pytest.mark.parametrize("context", [100, 101, 128, 1_000, 8_000, 100_000, 200_000, 1_000_000])
@pytest.mark.parametrize("output_factor", [0, 1, 3, 7, 12])
def test_integer_ordering_invariant_over_grid(context: int, output_factor: int) -> None:
    output = max(1, context * output_factor // 10)
    budgets = derive_compaction_budgets(_profile(context, output))

    assert 1 <= budgets.target_tokens < budgets.force_compress_tokens < budgets.trigger_tokens <= context
    assert budgets.target_tokens <= budgets.satisfied_tokens <= budgets.force_compress_tokens
    assert budgets.trigger_pct <= 1.0
