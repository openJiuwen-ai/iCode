# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``elide_middle`` must cut on grapheme-cluster boundaries, not on code points.

Every case below is a string whose *user-perceived characters* are wider than
one code point: an emoji ZWJ sequence, a regional-indicator flag pair, and a
base letter with a combining mark. Eliding them at a code-point boundary
produces output that is either unreadable (a lone regional indicator), wrong
(an accent detached from its base) or both -- and, for the ZWJ family, spends
two of its twelve columns.
"""

from __future__ import annotations

import unicodedata

import pytest
from rich.cells import cell_len, split_graphemes

from chrys.app.tui.util.formatting import elide_middle

ZWJ = "\u200d"
ELLIPSIS = "\u2026"
COMBINING_ACUTE = "\u0301"

FAMILY = f"\U0001f468{ZWJ}\U0001f469{ZWJ}\U0001f467{ZWJ}\U0001f466"
FLAG_CHINA = "\U0001f1e8\U0001f1f3"
LOWER_REGIONAL_INDICATOR = 0x1F1E6
UPPER_REGIONAL_INDICATOR = 0x1F1FF

WIDTH = 12
"""The budget every case is elided to."""

CASES = {
    "zwj-family": FAMILY * 10,
    "flag-pairs": FLAG_CHINA * 8,
    "combining-marks": f"e{COMBINING_ACUTE}" * 20,
}


def _clusters(text: str) -> list[str]:
    """*text* split into user-perceived characters."""
    spans, _ = split_graphemes(text)
    return [text[start:end] for start, end, _width in spans]


def _regional_indicator_count(text: str) -> int:
    return sum(1 for char in text if LOWER_REGIONAL_INDICATOR <= ord(char) <= UPPER_REGIONAL_INDICATOR)


def _char_after_ellipsis(text: str) -> str:
    index = text.find(ELLIPSIS)
    assert index >= 0, f"no ellipsis in {text!r}"
    following = text[index + len(ELLIPSIS) :]
    return following[0] if following else ""


@pytest.mark.parametrize("name", list(CASES))
def test_elide_middle_never_splits_a_grapheme_cluster(name: str) -> None:
    """No cluster of the result is a fragment: each is whole, or the ellipsis itself.

    ``flag-pairs`` passes here by construction: ``split_graphemes`` segments a
    run of regional indicators one code point at a time, so a lone indicator is
    indistinguishable from a whole flag at this level. The pairing is asserted
    by :func:`test_elide_middle_keeps_regional_indicators_paired` instead.
    """
    text = CASES[name]
    whole = set(_clusters(text))

    result = elide_middle(text, WIDTH)

    fragments = [cluster for cluster in _clusters(result) if cluster != ELLIPSIS and cluster not in whole]
    assert not fragments, f"{name}: {result!r} contains cluster fragments {fragments!r}"


def test_elide_middle_does_not_break_a_zwj_sequence() -> None:
    """The ellipsis may not sit inside a ZWJ sequence, where it joins the neighbours into garbage."""
    result = elide_middle(CASES["zwj-family"], WIDTH)

    assert f"{ZWJ}{ELLIPSIS}" not in result and f"{ELLIPSIS}{ZWJ}" not in result, (
        f"ellipsis inserted inside a ZWJ sequence: {result!r}"
    )


def test_elide_middle_keeps_regional_indicators_paired() -> None:
    """A lone regional indicator renders as a lettered box: the count must stay even."""
    result = elide_middle(CASES["flag-pairs"], WIDTH)

    count = _regional_indicator_count(result)
    assert count % 2 == 0, f" {result!r} holds {count} regional indicators, so one flag lost its pair"


def test_elide_middle_keeps_combining_marks_with_their_base() -> None:
    """An ellipsis carries no base glyph, so a combining mark after it has nothing to modify."""
    result = elide_middle(CASES["combining-marks"], WIDTH)

    following = _char_after_ellipsis(result)
    assert not unicodedata.category(following).startswith("M"), (
        f"combining mark {following!r} follows the ellipsis in {result!r}"
    )
    detached = [cluster for cluster in _clusters(result) if unicodedata.category(cluster[0]).startswith("M")]
    assert not detached, f"{result!r} starts a cluster with a combining mark: {detached!r}"


def test_elide_middle_spends_its_budget_on_emoji() -> None:
    """Eliding to two of twelve columns is not 'keeping both ends' -- it drops the middle of nothing."""
    result = elide_middle(CASES["zwj-family"], WIDTH)

    assert cell_len(result) >= WIDTH - 4, (
        f"{result!r} is only {cell_len(result)} cells wide against a budget of {WIDTH}; "
        "whole clusters were passed over"
    )
