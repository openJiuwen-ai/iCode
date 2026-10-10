# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The unified diff of a file-tool card in the chat."""

from __future__ import annotations

import asyncio
from contextlib import suppress

from textual.geometry import Region

from chrys.app.tui.widgets.diff_view.compute import compute_highlighted_lines, compute_hunks, shown_code
from chrys.app.tui.widgets.diff_view.rows import DiffRow, code_width, number_width, unified_rows
from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines


class InlineUnifiedDiffLines(UnifiedDiffLines):
    """A `UnifiedDiffLines` that works its rows out of two texts itself.

    `DiffView` is the viewer with everything. A card in the chat shows a few lines of a unified
    diff and nothing else, so it mounts this one widget instead of a view around it, and
    highlights no further into a file than the diff goes.
    """

    def __init__(
        self,
        path_before: str,
        path_after: str,
        code_before: str,
        code_after: str,
        *,
        max_display_lines: int | None = None,
        auto_height: bool = False,
        annotations: bool = True,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = "diff-group",
    ) -> None:
        super().__init__((), number_digits=1, code_width=1, annotations=annotations, name=name, id=id, classes=classes)
        self.path_before = path_before
        self.path_after = path_after
        self.code_before = code_before
        self.code_after = code_after
        self.max_display_lines = max_display_lines
        self.auto_height = auto_height
        self._prepared = False

    async def prepare(self) -> None:
        """Work the rows out off the event loop. Await this before mounting."""
        if self._prepared:
            return
        rows, number_digits, widest_code = await asyncio.to_thread(self._compute_rows)
        self.set_rows(rows, number_digits=number_digits, code_width=widest_code)
        if self.max_display_lines is not None:
            self.styles.height = min(len(rows), self.max_display_lines)
        elif self.auto_height:
            self.styles.height = len(rows)
        self._prepared = True

    def _compute_rows(self) -> tuple[list[DiffRow], int, int]:
        hunks = compute_hunks(self.code_before, self.code_after)
        before, after = compute_highlighted_lines(
            shown_code(self.code_before),
            shown_code(self.code_after),
            self.path_before,
            self.path_after,
            hunks,
            hunks_only=True,
        )
        rows = unified_rows(hunks, before, after)
        return rows, number_width(rows), code_width(rows)

    def prewarm_visible_rows(self) -> None:
        """Render the rows on display ahead of the first paint, so that it finds them cached."""
        with suppress(Exception):
            if not self._prepared or not self.is_attached:
                return
            width = self.content_region.width or self.size.width
            height = self.size.height
            if width <= 0 or height <= 0:
                return
            self.render_lines(Region(0, 0, width, min(height, len(self.rows))))
