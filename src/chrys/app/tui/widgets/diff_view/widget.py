# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""DiffView: two texts compared, side by side or as one column."""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

from textual import containers
from textual.reactive import reactive, var

from chrys.app.tui.widgets.diff_view.cells import ANNOTATION_WIDTH, annotation_cell, number_cell, number_cell_width
from chrys.app.tui.widgets.diff_view.code import CodeColumn
from chrys.app.tui.widgets.diff_view.compute import compute_highlighted_lines, compute_hunks, shown_code
from chrys.app.tui.widgets.diff_view.gutter import GutterColumn
from chrys.app.tui.widgets.diff_view.rows import (
    DiffRow,
    Hunk,
    Side,
    change_counts,
    code_width,
    number_width,
    split_rows,
    unified_rows,
)
from chrys.app.tui.widgets.diff_view.unified import UnifiedDiffLines

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.content import Content
    from textual.widget import Widget


class _Layout(NamedTuple):
    """The rows of one way to show the diff, with the widths their columns need."""

    sides: tuple[list[DiffRow], ...]
    """One list of rows for a unified diff, two of the same length for a split one."""
    number_digits: int
    code_width: int

    @classmethod
    def of(cls, *sides: list[DiffRow]) -> _Layout:
        return cls(sides, number_width(*sides), code_width(*sides))


class DiffView(containers.VerticalGroup):
    """The difference between two texts, with syntax highlighting and the edits within lines marked.

    ``split`` chooses between two columns side by side and a single one, and ``annotations`` turns
    the ``+`` and ``-`` beside changed lines on and off; both can change while the view is mounted.

    Diffing and highlighting take long for a large file. ``await view.prepare()`` does them off the
    event loop ahead of mounting; a view mounted unprepared does them when it composes.
    """

    split: reactive[bool] = reactive(True, recompose=True)
    annotations: var[bool] = var(True, toggle_class="-with-annotations")

    DEFAULT_CSS = """
    DiffView {
        width: 1fr;
        height: auto;

        .diff-group {
            background: $foreground 4%;
        }

        .annotations { width: 1; }
        &.-with-annotations {
            .annotations { width: auto; }
        }
        .code-left {
            scrollbar-size-vertical: 0;
        }
    }
    """

    def __init__(
        self,
        path_before: str,
        path_after: str,
        code_before: str,
        code_after: str,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.path_before = path_before
        self.path_after = path_after
        self.code_before = code_before
        self.code_after = code_after
        self.max_display_lines: int | None = None
        """Show at most this many rows, and scroll within them."""
        self.auto_height = False
        """Be as tall as the diff. ``max_display_lines`` takes precedence."""
        self.show_scrollbars = True
        """Without scrollbars, a unified diff is drawn by a single `UnifiedDiffLines`."""
        self._shown_code: tuple[str, str] | None = None
        self._hunks: list[Hunk] | None = None
        self._highlighted_lines: tuple[list[Content], list[Content]] | None = None
        self._layouts: dict[bool, _Layout] = {}

    # -- the diff ----------------------------------------------------------------------------------

    async def prepare(self) -> None:
        """Work the diff out off the event loop, for the layout that ``split`` selects."""
        await asyncio.to_thread(self._layout, self.split)

    @property
    def shown_code(self) -> tuple[str, str]:
        """The old and the new text as the diff shows them."""
        if self._shown_code is None:
            self._shown_code = (shown_code(self.code_before), shown_code(self.code_after))
        return self._shown_code

    @property
    def hunks(self) -> list[Hunk]:
        if self._hunks is None:
            self._hunks = compute_hunks(self.code_before, self.code_after)
        return self._hunks

    @property
    def counts(self) -> tuple[int, int]:
        """How many lines were added and how many removed."""
        return change_counts(self.hunks)

    @property
    def highlighted_lines(self) -> tuple[list[Content], list[Content]]:
        """Every line of the old and of the new text."""
        if self._highlighted_lines is None:
            self._highlighted_lines = compute_highlighted_lines(
                *self.shown_code, self.path_before, self.path_after, self.hunks
            )
        return self._highlighted_lines

    def _layout(self, split: bool) -> _Layout:
        layout = self._layouts.get(split)
        if layout is None:
            rows = split_rows if split else unified_rows
            sides = rows(self.hunks, *self.highlighted_lines)
            layout = self._layouts[split] = _Layout.of(*(sides if isinstance(sides, tuple) else (sides,)))
        return layout

    # -- composing ---------------------------------------------------------------------------------

    def watch_annotations(self, annotations: bool) -> None:
        """The gutter columns collapse through CSS. The single-widget diff draws its own and is told."""
        for lines in self.query(UnifiedDiffLines):
            lines.set_annotations(annotations)

    def compose(self) -> ComposeResult:
        layout = self._layout(self.split)
        row_count = len(layout.sides[0])
        if not self.split and not self.show_scrollbars:
            lines = UnifiedDiffLines(
                layout.sides[0],
                number_digits=layout.number_digits,
                code_width=layout.code_width,
                annotations=self.annotations,
                classes="diff-group",
            )
            self._set_height(lines, row_count)
            yield lines
            return

        group = containers.HorizontalGroup(classes="diff-group")
        # A row more for the scrollbar under the code.
        self._set_height(group, row_count + (1 if self.show_scrollbars else 0))
        with group:
            if self.split:
                left, right = layout.sides
                code_left = yield from self._compose_side(left, layout, Side.BEFORE, classes="code-left")
                code_right = yield from self._compose_side(right, layout, Side.AFTER, classes="code-right")
                code_left.scroll_sync = code_right
                code_right.scroll_sync = code_left
            else:
                yield from self._compose_side(layout.sides[0], layout, None, classes="code-right")

    def _compose_side(
        self, rows: list[DiffRow], layout: _Layout, side: Side | None, *, classes: str
    ) -> Generator[Widget, None, CodeColumn]:
        """The gutters and the code of one side, or with ``side`` left out, of a unified diff.

        A side numbers its rows in its own text. A unified diff numbers them in both: the old
        text first and under the edge bar, then the new.
        """
        code = CodeColumn(rows, layout.code_width, classes=classes)
        if not self.show_scrollbars:
            code.styles.overflow_x = "hidden"
            code.styles.overflow_y = "hidden"
        digits = layout.number_digits
        if side is None:
            numbers = [
                partial(number_cell, side=Side.BEFORE, digits=digits, edge=True),
                partial(number_cell, side=Side.AFTER, digits=digits, edge=False),
            ]
        else:
            numbers = [partial(number_cell, side=side, digits=digits, edge=True)]
        code.gutters = [
            *(GutterColumn(rows, cell, number_cell_width(digits), code) for cell in numbers),
            GutterColumn(rows, annotation_cell, ANNOTATION_WIDTH, code, classes="annotations"),
        ]
        yield from code.gutters
        yield code
        return code

    def _set_height(self, widget: UnifiedDiffLines | containers.HorizontalGroup, natural_height: int) -> None:
        if self.max_display_lines is not None:
            widget.styles.height = min(natural_height, self.max_display_lines)
        elif self.auto_height:
            widget.styles.height = natural_height
