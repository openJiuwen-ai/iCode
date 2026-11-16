# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Modal viewer for a compiled terminal Mermaid diagram."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from rich.cells import cell_len
from rich.text import Text
from textual.containers import VerticalGroup

from chrys.app.tui.binding_display import CLOSE_BINDING, COPY_BINDING, TOGGLE_VIEW_BINDING, localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.widgets.markdown.diagram.model import CompiledDiagram
from chrys.app.tui.widgets.markdown.diagram.viewer import DiagramViewport
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from textual.app import ComposeResult


_TITLE = msg("tui.diagram.title", fallback="Mermaid diagram")
_VIEW_HINT = msg(
    "tui.diagram.view_hint",
    fallback="Press Space to switch source/rendered view · Press c to copy",
)
_COPIED_SOURCE = msg("tui.diagram.copied_source", fallback="Copied Mermaid source")


def _source_canvas(diagram: CompiledDiagram) -> CompiledDiagram:
    """Build a control-safe, cell-measured canvas for the Mermaid source."""
    source_lines = diagram.source.expandtabs(4).splitlines() or [""]
    rows = tuple(
        " " + "".join("�" if ord(char) < 32 or 0x7F <= ord(char) < 0xA0 else char for char in line)
        for line in source_lines
    )
    return CompiledDiagram(
        source=diagram.source,
        kind=diagram.kind,
        width=max((cell_len(row) for row in rows), default=1) + 1,
        height=len(rows),
        rows=rows,
        diagnostics=diagram.diagnostics,
    )


class DiagramDialog(BaseDialog[None]):
    """Large diagram view with native draggable scrollbars on both axes."""

    CSS_PATH = "diagram.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "close", CLOSE_BINDING, show=False, priority=True),
        localized_binding("q", "close", CLOSE_BINDING, show=False),
        localized_binding("space", "toggle_view", TOGGLE_VIEW_BINDING, show=False, priority=True),
        localized_binding("c", "copy_source", COPY_BINDING, show=False),
    ]

    def __init__(self, diagram: CompiledDiagram) -> None:
        self.diagram = diagram
        self._source_canvas = _source_canvas(diagram)
        self._showing_source = False
        super().__init__()

    def compose(self) -> ComposeResult:
        localizer = widget_localizer(self)
        with VerticalGroup(id="diagram-container") as container:
            container.border_title = Text(render_str(localizer, _TITLE.bind()))
            container.border_subtitle = Text(render_str(localizer, _VIEW_HINT.bind()))
            yield DiagramViewport(self.diagram, id="diagram-viewport")

    def on_mount(self) -> None:
        """Put keyboard scrolling directly on the diagram surface."""
        if not self._composed_live:
            return
        self.query_one(DiagramViewport).focus()

    def action_copy_source(self) -> None:
        """Copy the original Mermaid source without terminal decoration."""
        copy_text_to_clipboards(self.app, self.diagram.source)
        localizer = widget_localizer(self)
        self.notify(
            render_str(localizer, _COPIED_SOURCE.bind()),
            title=render_str(localizer, COPIED_TITLE.bind()),
            timeout=2,
            markup=False,
        )

    def action_toggle_view(self) -> None:
        """Switch the viewport between rendered cells and literal Mermaid source."""
        self._showing_source = not self._showing_source
        diagram = self._source_canvas if self._showing_source else self.diagram
        self.query_one(DiagramViewport).set_diagram(diagram)

    def action_close(self) -> None:
        """Close the dialog."""
        self.dismiss(None)


__all__ = ["DiagramDialog"]
