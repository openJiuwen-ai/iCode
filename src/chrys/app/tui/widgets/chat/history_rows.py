# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Map virtual transcript rows to history pages with variable measured heights."""

from __future__ import annotations

from collections.abc import Sequence


class HistoryRows:
    """A prefix-sum index of nonnegative page heights.

    Both updating a measured height and locating a page by its row take
    logarithmic time. Zero-height pages, such as fully folded content, have
    no rows and are skipped by row-to-page lookups.
    """

    def __init__(self, heights: Sequence[int]) -> None:
        self._heights = list(heights)
        self._sums = [0, *heights]
        for node in range(1, len(self._sums)):
            parent = node + (node & -node)
            if parent < len(self._sums):
                self._sums[parent] += self._sums[node]

    def __len__(self) -> int:
        return len(self._heights)

    def copy(self) -> HistoryRows:
        return HistoryRows(self._heights)

    def start(self, page: int) -> int:
        """Return the starting row of a page, or the extent for `len(self)`."""
        offset = 0
        while page:
            offset += self._sums[page]
            page &= page - 1
        return offset

    def measure(self, page: int, height: int) -> bool:
        """Replace a page's estimate with its laid-out height."""
        delta = height - self._heights[page]
        if not delta:
            return False
        self._heights[page] = height
        node = page + 1
        while node < len(self._sums):
            self._sums[node] += delta
            node += node & -node
        return True

    def page_at(self, row: int) -> int:
        """Return the page containing the row, or the end sentinel."""
        page = 0
        step = 1 << len(self).bit_length()
        while step:
            candidate = page + step
            if candidate <= len(self) and self._sums[candidate] <= row:
                row -= self._sums[candidate]
                page = candidate
            step >>= 1
        return page
