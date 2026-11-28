# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Virtual transcript extent and row lookup agree after height corrections."""

from chrys.app.tui.widgets.chat.history_rows import HistoryRows


def test_empty_index_has_only_the_end_sentinel() -> None:
    rows = HistoryRows([])

    assert len(rows) == 0
    assert rows.start(0) == 0
    assert rows.page_at(0) == 0


def test_lookup_skips_folded_pages_and_tracks_width_remeasurements() -> None:
    heights = [0, 3, 2, 0, 4, 1, 0, 2, 0]
    rows = HistoryRows(heights)

    for page, new_height in [(0, 2), (5, 0), (4, 15), (1, 0), (8, 4), (8, 4)]:
        heights[page] = new_height
        rows.measure(page, new_height)

        expected_pages = [page for page, height in enumerate(heights) for _ in range(height)]
        assert [rows.page_at(row) for row in range(len(expected_pages))] == expected_pages
        assert rows.page_at(len(expected_pages)) == len(heights)
        assert rows.page_at(len(expected_pages) + 5) == len(heights)
        assert [rows.start(page) for page in range(len(heights) + 1)] == [
            sum(heights[:page]) for page in range(len(heights) + 1)
        ]
