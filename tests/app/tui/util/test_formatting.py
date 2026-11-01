# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared TUI formatting helpers."""

from __future__ import annotations

import pytest
from rich.text import Text

from chrys.app.tui.util.formatting import (
    elapsed_parts,
    format_elapsed,
    format_token_count,
    format_typed_count,
    with_count_hint,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0"),
        (999, "999"),
        (1_000, "1.0k"),
        (12_200, "12.2k"),
        (999_949, "999.9k"),
        (999_950, "1.0m"),
        (1_852_300, "1.9m"),
        (999_949_999, "999.9m"),
        (999_950_000, "1.0b"),
        (1_000_000_000, "1.0b"),
    ],
)
def test_format_token_count_uses_compact_units_without_rounded_overflow(value: int, expected: str) -> None:
    assert format_token_count(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0"), (999, "999"), (1_000, "1K"), (1_001, "~1K"), (1_010, "1.01K"), (1_100, "1.1K")],
)
def test_format_typed_count_starts_compacting_at_one_thousand(value: int, expected: str) -> None:
    assert format_typed_count(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1_280_000, "1.28M"), (128_040, "128.04K"), (12_340_000_000, "12.34B"), (12_345, "~12.35K")],
)
def test_format_typed_count_keeps_up_to_two_decimals(value: int, expected: str) -> None:
    assert format_typed_count(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(128_000, "128K"), (1_000_000, "1M"), (5_000_000_000, "5B"), (1_200_000, "1.2M"), (12_200, "12.2K")],
)
def test_format_typed_count_drops_trailing_zeros(value: int, expected: str) -> None:
    assert format_typed_count(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(128_041, "~128.04K"), (1_200_001, "~1.2M"), (3_500_008, "~3.5M"), (128_001, "~128K")],
)
def test_format_typed_count_marks_a_rounded_value(value: int, expected: str) -> None:
    assert format_typed_count(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1_005, "~1.01K"), (1_284_999, "~1.28M"), (1_285_000, "~1.29M")],
)
def test_format_typed_count_rounds_half_up(value: int, expected: str) -> None:
    assert format_typed_count(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(999_994, "~999.99K"), (999_995, "~1M"), (999_994_999, "~999.99M"), (999_995_000, "~1B")],
)
def test_format_typed_count_rounding_to_a_thousand_moves_to_the_next_unit(value: int, expected: str) -> None:
    assert format_typed_count(value) == expected


@pytest.mark.parametrize(("raw", "hint"), [("350000", " · 350K"), (" 1280000 ", " · 1.28M"), ("3500008", " · ~3.5M")])
def test_with_count_hint_appends_a_dim_compact_value(raw: str, hint: str) -> None:
    label = Text("Max", style="red")

    hinted = with_count_hint(label, raw)

    assert hinted.plain == f"Max{hint}"
    assert [(span.start, span.end, span.style) for span in hinted.spans] == [(3, 3 + len(hint), "dim")]
    assert hinted.style == "red"
    assert label.plain == "Max"


@pytest.mark.parametrize("raw", ["999", "", "12k", "-5000", "1e6"])
def test_with_count_hint_returns_the_label_unchanged_below_one_thousand_or_for_non_integers(raw: str) -> None:
    label = Text("Max", style="red")

    assert with_count_hint(label, raw) is label
    assert label.plain == "Max"
    assert label.spans == []


@pytest.mark.parametrize(
    ("seconds", "parts", "compact"),
    [
        (-1.0, ((0, "ms"),), "0ms"),
        (0.0, ((0, "ms"),), "0ms"),
        (0.412, ((412, "ms"),), "412ms"),
        (0.9999, ((999, "ms"),), "999ms"),
        (1.0, ((1, "s"),), "1s"),
        (59.9, ((59, "s"),), "59s"),
        (65.0, ((1, "m"), (5, "s")), "1m 05s"),
    ],
)
def test_elapsed_parts_spans_milliseconds_through_days(
    seconds: float, parts: tuple[tuple[int, str], ...], compact: str
) -> None:
    assert elapsed_parts(seconds) == parts
    assert format_elapsed(seconds) == compact
