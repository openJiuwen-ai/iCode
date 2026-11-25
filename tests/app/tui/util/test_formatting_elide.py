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
from rich.text import Text

from chrys.app.tui.util.formatting import _cell_width, elide_middle

ZWJ = "\u200d"
ELLIPSIS = "…"
COMBINING_ACUTE = "\u0301"

FAMILY = "👨\u200d👩\u200d👧\u200d👦"
FLAG_CHINA = "🇨🇳"
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

    assert _cell_width(result) <= WIDTH, f"{name}: {result!r} is {_cell_width(result)} cells against a cap of {WIDTH}"
    fragments = [cluster for cluster in _clusters(result) if cluster != ELLIPSIS and cluster not in whole]
    assert not fragments, f"{name}: {result!r} contains cluster fragments {fragments!r}"


def test_elide_middle_does_not_break_a_zwj_sequence() -> None:
    """The ellipsis may not sit inside a ZWJ sequence, where it joins the neighbours into garbage."""
    result = elide_middle(CASES["zwj-family"], WIDTH)

    assert _cell_width(result) <= WIDTH, f"{result!r} is {_cell_width(result)} cells against a cap of {WIDTH}"
    assert f"{ZWJ}{ELLIPSIS}" not in result and f"{ELLIPSIS}{ZWJ}" not in result, (
        f"ellipsis inserted inside a ZWJ sequence: {result!r}"
    )


def test_elide_middle_keeps_regional_indicators_paired() -> None:
    """A lone regional indicator renders as a lettered box: the count must stay even."""
    result = elide_middle(CASES["flag-pairs"], WIDTH)

    assert _cell_width(result) <= WIDTH, f"{result!r} is {_cell_width(result)} cells against a cap of {WIDTH}"
    count = _regional_indicator_count(result)
    assert count % 2 == 0, f" {result!r} holds {count} regional indicators, so one flag lost its pair"


def test_elide_middle_keeps_combining_marks_with_their_base() -> None:
    """An ellipsis carries no base glyph, so a combining mark after it has nothing to modify."""
    result = elide_middle(CASES["combining-marks"], WIDTH)

    assert _cell_width(result) <= WIDTH, f"{result!r} is {_cell_width(result)} cells against a cap of {WIDTH}"
    following = _char_after_ellipsis(result)
    assert not unicodedata.category(following).startswith("M"), (
        f"combining mark {following!r} follows the ellipsis in {result!r}"
    )
    detached = [cluster for cluster in _clusters(result) if unicodedata.category(cluster[0]).startswith("M")]
    assert not detached, f"{result!r} starts a cluster with a combining mark: {detached!r}"


def test_elide_middle_spends_its_budget_on_emoji() -> None:
    """Eliding to two of twelve columns is not 'keeping both ends' -- it drops the middle of nothing."""
    result = elide_middle(CASES["zwj-family"], WIDTH)

    assert _cell_width(result) <= WIDTH, f"{result!r} is {_cell_width(result)} cells against a cap of {WIDTH}"
    assert _cell_width(result) >= WIDTH - 4, (
        f"{result!r} is only {_cell_width(result)} cells wide against a budget of {WIDTH}; whole clusters were passed over"
    )


@pytest.mark.parametrize("name", list(ALL_CASES))
@pytest.mark.parametrize("max_width", [1, 2, 3, 4, 5, 7, 12, 24])
def test_a_result_never_exceeds_its_budget(name: str, max_width: int) -> None:
    """The cap is what the caller puts in a fixed column, so it holds for every input.

    ``rich.cells.cell_len`` under-counts a ZWJ glued to a non-emoji (it skips the
    next code point unconditionally), so both its measure and :func:`_cell_width`
    are checked: the fit holds for rich's number and for the cells the terminal
    really spends.
    """
    result = elide_middle(ALL_CASES[name], max_width)

    assert cell_len(result) <= max_width, f"{name} at {max_width}: {result!r} is {cell_len(result)} cells"
    assert _cell_width(result) <= max_width, f"{name} at {max_width}: {result!r} is really {_cell_width(result)} cells"


def test_an_opening_zwj_run_is_measured_by_the_width_it_really_takes() -> None:
    """``\\u200d\\u200d中`` is reported as zero cells by ``split_graphemes`` and takes two."""
    text = REVIEW_CASES["leading-zwj"]
    assert cell_len(text[:3]) == 2, "the fixture must be a zero-width lead plus a wide character"

    result = elide_middle(text, 24)

    assert _cell_width(result) <= 24, f"{result!r} is {_cell_width(result)} cells against a cap of 24"
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
    assert _cell_width(result) <= max_width


@pytest.mark.parametrize("lead", [ZWJ, "\ufeff", "\u200b", "\u200c"])
def test_a_zero_width_lead_never_displaces_the_ellipsis(lead: str) -> None:
    """A single-cell cap must yield ``…`` even when the text opens with a zero-width lead."""
    text = f"{lead}\u4e2d" + "a" * 30

    result = elide_middle(text, 1)

    assert result == ELLIPSIS, f"{result!r} should be just the ellipsis"
    assert cell_len(result) == 1


def test_a_dangling_joiner_is_not_left_against_the_ellipsis() -> None:
    """A joiner that ends the head sits on the ellipsis and joins nothing, so it is stripped."""
    result = elide_middle(f"a{ZWJ}{ZWJ}" + "b" * 30, 2)

    assert result == "a…", f"{result!r} leaves the joiners dangling on the ellipsis"
    assert cell_len(result) == 2


@pytest.mark.parametrize(
    ("text", "max_width"),
    [
        ("\u2060\u200d中" + "a" * 5, 3),
        ("\ufeff\u200d中" + "a" * 5, 3),
        ("\u200c\u200d中" + "a" * 5, 3),
        ("x\u200d中" + "a" * 5, 3),
    ],
)
def test_a_zwj_glued_to_a_wide_letter_never_overruns(text: str, max_width: int) -> None:
    """``ZWJ + 中`` spends the letter's cells; ``cell_len`` alone under-counts it as zero."""
    result = elide_middle(text, max_width)

    assert cell_len(result) <= max_width, f"{result!r} is {cell_len(result)} cells on rich's measure"
    assert _cell_width(result) <= max_width, f"{result!r} is really {_cell_width(result)} cells"
    assert "\u200d中" not in result, f"{result!r} keeps a ZWJ glued to a wide letter"


@pytest.mark.parametrize("separator", [ZWJ, "\u200c", "\u200b"])
def test_a_variation_selector_only_widens_the_character_it_follows(separator: str) -> None:
    """A terminal applies a VS16 to the character right before it, so a separator blocks it.

    Reading the selector through a zero-width joiner widened a heart that stays
    one cell, which is the opposite error of :func:`_cell_width`'s own: ``rich``
    counts ``\\u2764\\u200c\\ufe0f`` as two cells.
    """
    assert _cell_width(f"\u2764{separator}\ufe0f") == 1, "the selector reached past the separator"
    assert _cell_width("\u2764\ufe0f") == 2, "a selector directly after its base still gives it width"


EMOJI_SEQUENCES = {
    "kiss-couple": "\U0001f469\u200d\u2764\ufe0f\u200d\U0001f48b\u200d\U0001f468",
    "pirate-flag": "\U0001f3f4\u200d\u2620\ufe0f",
    "eye-in-speech-bubble": "\U0001f441\ufe0f\u200d\U0001f5e8\ufe0f",
}


@pytest.mark.parametrize("name", list(EMOJI_SEQUENCES))
def test_a_sequence_joined_through_a_narrow_emoji_stays_two_cells(name: str) -> None:
    """``❤`` and ``☠`` are one cell until their own VS16 arrives, and still join a ZWJ.

    Reading the joiner as "swallow only a wide glyph" made a two-cell sequence
    measure four, so a budget spent on it dropped the emoji it was meant to keep.
    """
    sequence = EMOJI_SEQUENCES[name]

    assert _cell_width(sequence) == 2, f"{name} measures {_cell_width(sequence)} cells, not two"
    result = elide_middle(sequence * 6, 12)

    assert result == sequence * 6, f"{name}: {result!r} does not keep six whole sequences in twelve cells"


def test_a_narrow_emoji_without_its_selector_does_not_join() -> None:
    """``👩\\u200d❤`` is no sequence at all: the terminal draws a woman and a heart, three cells."""
    assert _cell_width("\U0001f469\u200d\u2764") == 3


@pytest.mark.parametrize(
    ("text", "max_width"),
    [
        ("x" * 10 + "🇨🇳🇯🇵🇺", 11),
        ("🇺🇨🇳🇯🇵" + "x" * 10, 11),
        ("🇨\ufe0f" + "🇨🇳" * 10, 12),
    ],
)
def test_elision_never_splits_a_flag_in_an_odd_run(text: str, max_width: int) -> None:
    """A budget cut falls between flags, and a lone indicator already in the
    source stays with its end so head/tail remain a prefix and a suffix.

    The old "every run is even" assertion demanded that elision repair a
    malformed source, which is impossible without dropping a character from
    the middle of an end. For a well-formed source the check still holds;
    :func:`test_elide_middle_keeps_regional_indicators_paired` covers it.
    """
    result = elide_middle(text, max_width)

    runs = _regional_indicator_runs(result)
    source_runs = _regional_indicator_runs(text)
    if all(run % 2 == 0 for run in source_runs):
        assert all(run % 2 == 0 for run in runs), f"{result!r} holds a lone flag: runs {runs!r}"
    assert _cell_width(result) <= max_width
    head, _, tail = result.partition(ELLIPSIS)
    assert text.startswith(head), f"head {head!r} is not a prefix of {text!r}"
    assert text.endswith(tail), f"tail {tail!r} is not a suffix of {text!r}"


CORPUS = [
    FAMILY * 3,
    FLAG_CHINA * 4,
    f"e{COMBINING_ACUTE}" * 12,
    "C://Users//Administrator//Documents//a-very-long-project-name",
    "项目目录" * 6,
    f"a{FLAG_CHINA}{ZWJ}{FLAG_CHINA}bcdefghij",
    f"{FLAG_CHINA}\ufe0f{FAMILY}tail",
]


@pytest.mark.parametrize("max_width", list(range(1, 31)))
def test_the_invariants_hold_across_a_corpus(max_width: int) -> None:
    """Budget, flag parity, and "head is a prefix, tail is a suffix" -- for every width.

    The "fits" branch uses :func:`_cell_width` (the real width ``elide_middle``
    budgets against) rather than ``rich.cells.cell_len``: the latter under-counts
    a ZWJ glued between two flags, so it would claim ``a🇨🇳\\u200d🇨🇳bc…`` fits
    when the terminal really spends a cell more.
    """
    for text in CORPUS:
        result = elide_middle(text, max_width)

        assert cell_len(result) <= max_width, f"w={max_width} {text!r} -> {result!r} is {cell_len(result)} cells"
        assert _cell_width(result) <= max_width, (
            f"w={max_width} {text!r} -> {result!r} is really {_cell_width(result)} cells"
        )
        assert all(run % 2 == 0 for run in _regional_indicator_runs(result)), (
            f"w={max_width} {text!r} -> {result!r} holds half a flag"
        )
        if _cell_width(text) <= max_width:
            assert result == text
            continue
        head, _, tail = result.partition(ELLIPSIS)
        assert text.startswith(head), f"w={max_width} head {head!r} is not a prefix of {text!r}"
        assert text.endswith(tail), f"w={max_width} tail {tail!r} is not a suffix of {text!r}"


def test_a_lone_regional_indicator_inside_an_end_keeps_the_end_contiguous() -> None:
    """Skipping an unpaired indicator dropped a letter from the *middle* of the head."""
    text = "a\U0001f1fabcdefgh"

    result = elide_middle(text, 7)

    assert result == "a\U0001f1fab…fgh", f"{result!r} is not a contiguous head and tail"
    head, _, tail = result.partition(ELLIPSIS)
    assert text.startswith(head), f"head {head!r} is not a prefix of {text!r}"
    assert text.endswith(tail), f"tail {tail!r} is not a suffix of {text!r}"


def test_a_devanagari_virama_does_not_swallow_a_latin_letter() -> None:
    """A virama joins a consonant of its own script, not just any Unicode letter."""
    text = "\u0915\u094dA" + "x" * 10

    result = elide_middle(text, 3)

    assert result == "\u0915\u094d…x", f"{result!r} lost the virama cluster or kept the Latin letter"


def test_a_nukta_between_virama_and_consonant_does_not_split_the_conjunct() -> None:
    """GB9c lets Extends sit between the linker and the following consonant."""
    conjunct = "\u0915\u094d\u093c\u0937"
    text = conjunct + "x" * 10

    elided = elide_middle(text, 3)
    assert elided == "…x", f"{elided!r} cut the conjunct at the nukta"
    kept = elide_middle(text, 4)
    assert kept == f"{conjunct}…x", f"{kept!r} did not keep the conjunct whole"


def test_the_final_width_is_checked_with_richs_own_measure() -> None:
    """Rich counts a VS16 after a combining mark a cell wider than ``_cell_width``."""
    text = "x" * 13 + "\u2764\u0301\ufe0f" + "x" * 12

    result = elide_middle(text, 26)

    assert Text(result).cell_len <= 26, f"{result!r} renders at {Text(result).cell_len} cells on Rich's measure"


OVERWEIGHT_PREFIX = "\u2764\u0301\ufe0f" * 12 + "x" * 30
"""A heart sequence Rich renders at two cells but ``_cell_width`` measures at one."""


def test_an_overweight_prefix_does_not_consume_the_whole_tail() -> None:
    """An overrun Rich sees in the head is shrunk on the head, not billed to the tail."""
    result = elide_middle(OVERWEIGHT_PREFIX, 24)

    head, _, tail = result.partition(ELLIPSIS)
    assert head, f"{result!r} dropped the whole head"
    assert tail, f"{result!r} dropped the whole tail on the head's overrun"
    assert Text(result).cell_len == 24, f"{result!r} renders at {Text(result).cell_len} cells, not 24"
    assert head.count("\u2764") == 6, f"{head!r} does not keep six hearts"
    assert tail.count("x") == 11, f"{tail!r} does not keep eleven x's"


@pytest.mark.parametrize("max_width", [12, 20])
def test_an_overweight_prefix_keeps_both_ends_at_a_narrow_width(max_width: int) -> None:
    """A tight budget still keeps a prefix and a suffix instead of an empty side."""
    result = elide_middle(OVERWEIGHT_PREFIX, max_width)

    head, _, tail = result.partition(ELLIPSIS)
    assert head and tail, f"{result!r} at {max_width} lost an end"
    assert Text(result).cell_len <= max_width, f"{result!r} renders at {Text(result).cell_len} cells"
    assert _cell_width(result) <= max_width, f"{result!r} is really {_cell_width(result)} cells"


def test_a_trailing_zwj_that_shapes_the_last_glyph_is_kept() -> None:
    """A Malayalam Chillu N ends in a ZWJ that shapes the letter: only the cut edge is stripped."""
    text = "x" * 20 + "\u0d28\u0d4d\u200d"

    result = elide_middle(text, 10)

    assert result.endswith("\u0d28\u0d4d\u200d"), f"{result!r} dropped the trailing shaping ZWJ"
    assert Text(result).cell_len <= 10, f"{result!r} renders at {Text(result).cell_len} cells"


WIDTH_CONSISTENCY_CORPUS = [
    "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466" * 4,  # ZWJ family
    "\u2764\u0301\ufe0f" * 12 + "x" * 30,  # VS16 after a combining mark
    "\u0d28\u0d4d\u200d" * 3 + "x" * 20,  # Chillu N (trailing ZWJ)
    "\u200d" + "abcdefghij",  # leading ZWJ
    "a\u200d" * 6,  # interior ZWJ run
    "中文名字很长" * 3,  # CJK
    "e\u0301" * 9,  # combining acute
]


@pytest.mark.parametrize("text", WIDTH_CONSISTENCY_CORPUS)
@pytest.mark.parametrize("max_width", [1, 2, 3, 5, 8, 10, 12, 16, 20, 24, 32, 40])
def test_the_measured_text_is_the_rendered_text(text: str, max_width: int) -> None:
    """The width fitted is the width printed, and both ends stay a prefix and a suffix.

    Fitting and rendering share one stripped string, so a Rich width measured for
    a side is exactly the width the caller ends up printing.  If they diverged,
    a ``Text.cell_len`` above the cap would slip through.
    """
    result = elide_middle(text, max_width)

    assert Text(result).cell_len <= max_width, f"{result!r} renders at {Text(result).cell_len} cells"
    head, sep, tail = result.partition(ELLIPSIS)
    if sep:
        assert text.startswith(head), f"head {head!r} is not a prefix of {text!r}"
        assert text.endswith(tail), f"tail {tail!r} is not a suffix of {text!r}"
