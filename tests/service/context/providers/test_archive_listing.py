# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A listing of compressed blocks comes in pages that each pass a tool result whole."""

from __future__ import annotations

from typing import Any

import pytest

from chrys.foundation.text.tokenizer import MixedLanguageTokenizer
from chrys.service.context.providers.archive_listing import (
    ListingError,
    block_for_turn,
    marker_hint,
    page_budget_tokens,
    render_listing,
)

_counter = MixedLanguageTokenizer()


def _info(blocks: int, markers: int = 0, *, summary: str = "Summary text.") -> dict[str, Any]:
    """``list_compressed`` output: *blocks* blocks of two turns each, then *markers* unfolded turns."""
    return {
        "blocks": [
            {
                "compressed_context_id": f"ctx_{index:04d}",
                "summary": f"{summary} (block {index})",
                "marker_id": f"turn_{2 * index + 2}",
                "turn_range": (2 * index + 1, 2 * index + 2),
                "num_messages": 12,
                "created_at": "2026-01-01T00:00:00Z",
                "first_user_text": f"What the user asked in block {index}",
            }
            for index in range(blocks)
        ],
        "markers": [
            {"marker_id": f"turn_{turn}", "turn_index": turn, "position": turn}
            for turn in range(2 * blocks + 1, 2 * blocks + markers + 1)
        ],
    }


def _all_pages(info: dict[str, Any], *, budget_tokens: int, **request: Any) -> list[str]:
    pages: list[str] = []
    cursor: str | None = None
    while True:
        page = render_listing(info, budget_tokens=budget_tokens, cursor=cursor, **request)
        pages.append(page)
        header = page.split("\n")[1]
        if header.endswith("End of list."):
            return pages
        cursor = header.split('cursor="', 1)[1].split('"', 1)[0]
        assert len(pages) < 500


def _items(pages: list[str]) -> list[str]:
    return [line for page in pages for line in page.split("\n")[2:] if line.startswith("  - ")]


@pytest.mark.parametrize(
    ("ceiling", "expected"),
    [(None, 7_200), (0, 7_200), (64_000, 7_200), (8_000, 7_200), (2_000, 1_800)],
)
def test_a_page_aims_for_eight_thousand_tokens_or_less_under_a_lower_ceiling(
    ceiling: int | None, expected: int
) -> None:
    """The margin is for the kernel, which counts the finished result once more."""
    assert page_budget_tokens(ceiling) == expected


def test_a_short_list_is_one_page_of_one_line_per_block_and_marker() -> None:
    page = render_listing(_info(2, 2), budget_tokens=7_200)

    assert page.split("\n") == [
        '2 compressed block(s), turns 1-4; 2 fold marker(s), turns 5-6 (marker_id="turn_<n>").',
        "Items 1-4 of 4. End of list.",
        "Compressed blocks:",
        "  - ctx_0000 · turns 1-2 · 12 messages · What the user asked in block 0",
        "  - ctx_0001 · turns 3-4 · 12 messages · What the user asked in block 1",
        "Available fold markers (use the marker_id value exactly as shown):",
        '  - marker_id="turn_5" (turn 5)',
        '  - marker_id="turn_6" (turn 6)',
    ]


def test_a_long_list_is_paged_without_losing_or_repeating_an_item() -> None:
    info = _info(300, 150)

    pages = _all_pages(info, budget_tokens=1_800)

    assert len(pages) > 3
    assert all(_counter.count_tokens(page) <= 1_800 for page in pages)
    items = _items(pages)
    assert [line.split(" · ")[0] for line in items[:300]] == [f"  - ctx_{index:04d}" for index in range(300)]
    assert items[300:] == [f'  - marker_id="turn_{turn}" (turn {turn})' for turn in range(601, 751)]
    # Every page opens with the totals and says where it is and how to go on.
    assert all(page.startswith("300 compressed block(s), turns 1-600; 150 fold marker(s)") for page in pages)
    first = int(pages[1].split("\n")[1].split("Items ")[1].split("-")[0])
    assert pages[0].split("\n")[1] == f'Items 1-{first - 1} of 450. More: call again with cursor="{first - 1}".'
    # A section's heading is repeated on the page that continues it.
    assert pages[1].split("\n")[2] == "Compressed blocks:"


def test_a_page_always_holds_an_item_even_under_a_budget_too_small_for_one() -> None:
    pages = _all_pages(_info(3), budget_tokens=1)

    assert len(pages) == 3
    assert len(_items(pages)) == 3


def test_a_block_asked_for_by_id_comes_with_its_full_summary_and_nothing_else() -> None:
    page = render_listing(_info(3, 2), budget_tokens=7_200, compressed_context_id="ctx_0001")

    assert page.split("\n")[1:] == [
        "Items 1-1 of 1. End of list.",
        "Compressed blocks:",
        "  - ctx_0001 · turns 3-4 · 12 messages",
        "    Summary: Summary text. (block 1)",
    ]


def test_a_full_summary_longer_than_a_page_goes_on_where_the_cursor_says() -> None:
    summary = " ".join(f"fact-{number}" for number in range(4_000))
    info = _info(1, summary=summary)

    pages = _all_pages(info, budget_tokens=1_800, compressed_context_id="ctx_0000")

    assert len(pages) > 2
    assert all(_counter.count_tokens(page) <= 1_800 for page in pages)
    assert "+" in pages[0].split("\n")[1].split('cursor="', 1)[1]
    bodies = [pages[0].split("\n", 3)[3]]
    for page in pages[1:]:
        continued = page.split("\n", 3)[3]
        assert continued.startswith("    … ")
        bodies.append(continued.removeprefix("    … "))
    assert "".join(bodies) == f"  - ctx_0000 · turns 1-2 · 12 messages\n    Summary: {summary} (block 0)"


def test_full_summaries_of_every_block_are_paged_block_by_block() -> None:
    info = _info(40, summary="A summary of some length. " * 20)

    pages = _all_pages(info, budget_tokens=1_800, full_summaries=True)

    assert len(pages) > 2
    assert all(_counter.count_tokens(page) <= 1_800 for page in pages)
    assert len(_items(pages)) == 40
    assert sum(page.count("    Summary: ") for page in pages) == 40


def test_a_turn_range_keeps_the_blocks_and_markers_that_touch_it() -> None:
    page = render_listing(_info(5, 5), budget_tokens=7_200, turn_from=4, turn_to=11)

    lines = page.split("\n")
    assert lines[0].endswith("Matching this request: 4 block(s), 1 marker(s).")
    assert [line.split(" · ")[0] for line in lines if line.startswith("  - ctx_")] == [
        "  - ctx_0001",
        "  - ctx_0002",
        "  - ctx_0003",
        "  - ctx_0004",
    ]
    assert [line for line in lines if "marker_id=" in line and line.startswith("  - ")] == [
        '  - marker_id="turn_11" (turn 11)'
    ]


def test_a_range_that_matches_nothing_still_gives_the_totals() -> None:
    page = render_listing(_info(2, 1), budget_tokens=7_200, turn_from=90)

    assert page == (
        '2 compressed block(s), turns 1-4; 1 fold marker(s), turns 5-5 (marker_id="turn_<n>"). '
        "Matching this request: 0 block(s), 0 marker(s)."
    )


@pytest.mark.parametrize(
    "request_arguments",
    [
        {"compressed_context_id": "ctx_9999"},
        {"cursor": "abc"},
        {"cursor": "3+x"},
        {"cursor": "500"},
        {"cursor": "0+5"},  # A character offset into an item that is never split.
        {"turn_from": 9, "turn_to": 3},
    ],
)
def test_a_request_that_cannot_be_served_says_how_to_correct_it(request_arguments: dict[str, Any]) -> None:
    with pytest.raises(ListingError):
        render_listing(_info(3, 3), budget_tokens=7_200, **request_arguments)


def test_the_block_holding_a_turn_is_found_by_its_turn_range() -> None:
    blocks = _info(3)["blocks"]

    assert block_for_turn(blocks, 4) is blocks[1]
    assert block_for_turn(blocks, 5) is blocks[2]
    assert block_for_turn(blocks, 7) is None


def test_a_marker_hint_names_every_marker_of_a_short_list() -> None:
    hint = marker_hint(_info(0, 3)["markers"])

    assert hint.split("\n")[1:] == [f'  - marker_id="turn_{turn}" (turn {turn})' for turn in (1, 2, 3)]
    assert marker_hint([]).startswith("No fold markers available")


def test_a_marker_hint_names_both_ends_of_a_long_list() -> None:
    hint = marker_hint(_info(0, 500)["markers"])

    lines = hint.split("\n")
    assert len(lines) == 22
    assert lines[1] == '  - marker_id="turn_1" (turn 1)'
    assert lines[11] == "  … 480 more (list_compressed_contexts shows them all)"
    assert lines[-1] == '  - marker_id="turn_500" (turn 500)'
