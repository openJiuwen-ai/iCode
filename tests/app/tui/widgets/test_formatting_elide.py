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

REVIEW_CASES = {
    "leading-zwj": f"{ZWJ}{ZWJ}\u4e2d" + "a" * 30,
    "flag-with-variation-selector": f"{FLAG_CHINA}\ufe0f" * 3,
}

ALL_CASES = {**CASES, **REVIEW_CASES}


def _regional_indicator_runs(text: str) -> list[int]:
    """Lengths of the contiguous runs of regional indicators, VS16 not breaking a run."""
    runs: list[int] = []
    current = 0
    for char in text:
        if LOWER_REGIONAL_INDICATOR <= ord(char) <= UPPER_REGIONAL_INDICATOR:
            current += 1
        elif char not in "\ufe0e\ufe0f":
            if current:
                runs.append(current)
            current = 0
    if current:
        runs.append(current)
    return runs


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

    assert cell_len(result) <= WIDTH, f"{name}: {result!r} is {cell_len(result)} cells against a cap of {WIDTH}"
    fragments = [cluster for cluster in _clusters(result) if cluster != ELLIPSIS and cluster not in whole]
    assert not fragments, f"{name}: {result!r} contains cluster fragments {fragments!r}"


def test_elide_middle_does_not_break_a_zwj_sequence() -> None:
    """The ellipsis may not sit inside a ZWJ sequence, where it joins the neighbours into garbage."""
    result = elide_middle(CASES["zwj-family"], WIDTH)

    assert cell_len(result) <= WIDTH, f"{result!r} is {cell_len(result)} cells against a cap of {WIDTH}"
    assert f"{ZWJ}{ELLIPSIS}" not in result and f"{ELLIPSIS}{ZWJ}" not in result, (
        f"ellipsis inserted inside a ZWJ sequence: {result!r}"
    )


def test_elide_middle_keeps_regional_indicators_paired() -> None:
    """A lone regional indicator renders as a lettered box: the count must stay even."""
    result = elide_middle(CASES["flag-pairs"], WIDTH)

    assert cell_len(result) <= WIDTH, f"{result!r} is {cell_len(result)} cells against a cap of {WIDTH}"
    count = _regional_indicator_count(result)
    assert count % 2 == 0, f" {result!r} holds {count} regional indicators, so one flag lost its pair"


def test_elide_middle_keeps_combining_marks_with_their_base() -> None:
    """An ellipsis carries no base glyph, so a combining mark after it has nothing to modify."""
    result = elide_middle(CASES["combining-marks"], WIDTH)

    assert cell_len(result) <= WIDTH, f"{result!r} is {cell_len(result)} cells against a cap of {WIDTH}"
    following = _char_after_ellipsis(result)
    assert not unicodedata.category(following).startswith("M"), (
        f"combining mark {following!r} follows the ellipsis in {result!r}"
    )
    detached = [cluster for cluster in _clusters(result) if unicodedata.category(cluster[0]).startswith("M")]
    assert not detached, f"{result!r} starts a cluster with a combining mark: {detached!r}"


def test_elide_middle_spends_its_budget_on_emoji() -> None:
    """Eliding to two of twelve columns is not 'keeping both ends' -- it drops the middle of nothing."""
    result = elide_middle(CASES["zwj-family"], WIDTH)

    assert cell_len(result) <= WIDTH, f"{result!r} is {cell_len(result)} cells against a cap of {WIDTH}"
    assert cell_len(result) >= WIDTH - 4, (
        f"{result!r} is only {cell_len(result)} cells wide against a budget of {WIDTH}; whole clusters were passed over"
    )


@pytest.mark.parametrize("name", list(ALL_CASES))
@pytest.mark.parametrize("max_width", [1, 2, 3, 4, 5, 7, 12, 24])
def test_a_result_never_exceeds_its_budget(name: str, max_width: int) -> None:
    """The cap is what the caller puts in a fixed column, so it holds for every input.

    ``split_graphemes`` reports the width of a span it built by gluing a code point
    onto a zero-width one as zero, so a budget accumulated from those numbers can
    spend cells the terminal will really use.
    """
    result = elide_middle(ALL_CASES[name], max_width)

    assert cell_len(result) <= max_width, f"{name} at {max_width}: {result!r} is {cell_len(result)} cells"


def test_an_opening_zwj_run_is_measured_by_the_width_it_really_takes() -> None:
    """``\\u200d\\u200d中`` is reported as zero cells by ``split_graphemes`` and takes two."""
    text = REVIEW_CASES["leading-zwj"]
    assert cell_len(text[:3]) == 2, "the fixture must be a zero-width lead plus a wide character"

    result = elide_middle(text, 24)

    assert cell_len(result) <= 24, f"{result!r} is {cell_len(result)} cells against a cap of 24"
    assert "\u4e2d" in result, f"the wide character was dropped from {result!r}"


@pytest.mark.parametrize("max_width", [3, 4, 5, 6])
def test_a_flag_whose_second_half_carries_a_variation_selector_stays_whole(max_width: int) -> None:
    """``🇨🇳\\ufe0f`` splits as one indicator plus ``indicator + VS16``, which is still one flag.

    Reading the second half as "not a single code point, so not a partner" let the
    budget cut between the two halves and print a lettered box.
    """
    result = elide_middle(REVIEW_CASES["flag-with-variation-selector"], max_width)

    runs = _regional_indicator_runs(result)
    assert all(run % 2 == 0 for run in runs), f"{result!r} holds half a flag: runs {runs!r}"
    assert cell_len(result) <= max_width


CORPUS = [
    FAMILY * 3,
    FLAG_CHINA * 4,
    f"e{COMBINING_ACUTE}" * 12,
    "C://Users//Administrator//Documents//a-very-long-project-name",
    "\u9879\u76ee\u76ee\u5f55" * 6,
    f"a{FLAG_CHINA}{ZWJ}{FLAG_CHINA}bcdefghij",
    f"{FLAG_CHINA}\ufe0f{FAMILY}tail",
]


@pytest.mark.parametrize("max_width", list(range(1, 31)))
def test_the_invariants_hold_across_a_corpus(max_width: int) -> None:
    """Budget, flag parity, and "head is a prefix, tail is a suffix" -- for every width."""
    for text in CORPUS:
        result = elide_middle(text, max_width)

        assert cell_len(result) <= max_width, f"w={max_width} {text!r} -> {result!r} is {cell_len(result)} cells"
        assert all(run % 2 == 0 for run in _regional_indicator_runs(result)), (
            f"w={max_width} {text!r} -> {result!r} holds half a flag"
        )
        if cell_len(text) <= max_width:
            assert result == text
            continue
        head, _, tail = result.partition(ELLIPSIS)
        assert text.startswith(head), f"w={max_width} head {head!r} is not a prefix of {text!r}"
        assert text.endswith(tail), f"w={max_width} tail {tail!r} is not a suffix of {text!r}"
