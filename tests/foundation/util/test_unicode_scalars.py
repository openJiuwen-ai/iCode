# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for unpaired-surrogate detection."""

from __future__ import annotations

import pytest

from chrys.foundation.util.unicode_scalars import find_unpaired_surrogate


@pytest.mark.parametrize(
    ("text", "index"),
    [
        ("", -1),
        ("plain ascii", -1),
        ("中文 é 😀", -1),
        # A pair held as two code points is what JSON re-joins.
        ("ok\ud83d\ude00ok", -1),
        ("bad\udce9", 3),
        ("tail\ud83d", 4),
        ("\ude00\ud83d", 0),
        ("😀\ud83d\ude00\udce9", 3),
    ],
    ids=["empty", "ascii", "non-ascii", "pair", "lone-low", "trailing-high", "reversed-pair", "after-pair"],
)
def test_find_unpaired_surrogate_reports_the_first_lone_one(text: str, index: int) -> None:
    assert find_unpaired_surrogate(text) == index
