# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for tiered prompt-history search."""

from __future__ import annotations

import unicodedata

import pytest

from chrys.app.tui.support.prompt_search import normalize_for_search, rank_prompts


def test_empty_query_keeps_every_prompt_in_order() -> None:
    prompts = ["newest", "middle", "oldest"]

    assert rank_prompts(prompts, "") == prompts
    assert rank_prompts(prompts, "   ") == prompts


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ssbg", ["fix the session bug"]),
        ("SESSION", ["fix the session bug"]),
        ("bug session", ["fix the session bug"]),
        ("gub", []),
    ],
    ids=["fuzzy", "case-insensitive", "terms-any-order", "order-matters"],
)
def test_terms_match_in_any_order(query: str, expected: list[str]) -> None:
    assert rank_prompts(["fix the session bug", "write docs"], query) == expected


def test_every_term_must_match() -> None:
    assert rank_prompts(["fix the session bug", "session storage notes"], "session bug") == ["fix the session bug"]


def test_whole_word_outranks_word_prefix_regardless_of_recency() -> None:
    prompts = ["revert local dirty changes", "run tests locally", "LOC for current repo?"]

    assert rank_prompts(prompts, "loc") == [
        "LOC for current repo?",
        "revert local dirty changes",
        "run tests locally",
    ]


def test_tiers_rank_word_prefix_substring_acronym_fuzzy() -> None:
    prompts = [
        "s-x-r spread",  # fuzzy: s, r within a short window
        "start review",  # acronym: initials of consecutive words
        "classroom notes",  # substring: "sr" inside "classroom"
        "sr tool",  # whole word
        "srcmap tool",  # word prefix
    ]

    assert rank_prompts(prompts, "sr") == [
        "sr tool",
        "srcmap tool",
        "classroom notes",
        "start review",
        "s-x-r spread",
    ]


def test_exact_entry_outranks_newer_entries_with_the_same_word() -> None:
    assert rank_prompts(["fix the bug in session", "fix"], "fix") == ["fix", "fix the bug in session"]
    assert rank_prompts(["fix  bug now", "fix   bug"], "fix bug") == ["fix   bug", "fix  bug now"]


def test_weakest_term_decides_before_tier_sum() -> None:
    # Both terms whole words beats one whole word plus one fuzzy match.
    prompts = ["session restore and b-u-g", "restore session bug"]

    assert rank_prompts(prompts, "session bug") == ["restore session bug", "session restore and b-u-g"]


def test_equal_tiers_keep_input_order() -> None:
    prompts = ["session storage notes", "a much longer prompt that also mentions the session"]

    assert rank_prompts(prompts, "session") == prompts
    assert rank_prompts(list(reversed(prompts)), "session") == list(reversed(prompts))


def test_acronym_requires_consecutive_words() -> None:
    assert rank_prompts(["session restore"], "sr") == ["session restore"]
    assert rank_prompts(["please summarise the architecture document in detail"], "sad") == []


def test_fuzzy_match_must_stay_within_a_short_window() -> None:
    assert rank_prompts(["axbxc"], "abc") == ["axbxc"]
    assert rank_prompts(["axxxxxxxbxxxxxxxc"], "abc") == []


def test_tighter_fuzzy_match_outranks_newer_looser_one() -> None:
    assert rank_prompts(["axxbxxc", "axbxc"], "abc") == ["axbxc", "axxbxxc"]


def test_fuzzy_tightness_does_not_outrank_a_stronger_tier() -> None:
    assert rank_prompts(["axbxc", "start abc"], "abc") == ["start abc", "axbxc"]


def test_fuzzy_match_uses_the_tightest_window_anywhere() -> None:
    # The leftmost match spans the whole prompt; a later one is tight.
    prompt = "axxxxxxxxxxxxxxbxc axbxc"

    assert rank_prompts([prompt], "abc") == [prompt]


def test_matching_is_nfkc_normalized_on_both_sides() -> None:
    composed = "café notes"
    decomposed = unicodedata.normalize("NFD", composed)

    assert rank_prompts([decomposed], "café") == [decomposed]
    assert rank_prompts([composed], unicodedata.normalize("NFD", "café")) == [composed]
    assert normalize_for_search(decomposed) == normalize_for_search(composed)


def test_full_width_letters_and_digits_match_ascii_queries() -> None:
    assert rank_prompts(["ＬＯＣ统计", "第１步"], "loc") == ["ＬＯＣ统计"]
    assert rank_prompts(["ＬＯＣ统计", "第１步"], "1") == ["第１步"]


def test_cjk_characters_are_words_of_their_own() -> None:
    # A contiguous CJK match is a whole word wherever it falls, so recency decides.
    prompts = ["恢复会话失败", "会话列表"]

    assert rank_prompts(prompts, "会话") == prompts
    assert rank_prompts(["会话ID丢失"], "id") == ["会话ID丢失"]
    assert rank_prompts(["会话ID丢失", "valid id"], "id") == ["会话ID丢失", "valid id"]


def test_cjk_prompts_match_by_fuzzy_window() -> None:
    prompts = ["把数据落盘的调查结果写到文档", "检查工作目录"]

    assert rank_prompts(prompts, "落盘文档") == ["把数据落盘的调查结果写到文档"]
