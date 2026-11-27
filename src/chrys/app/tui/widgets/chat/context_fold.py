# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ContextFoldWidget — visual representation of a context compression event."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.text import Text
from textual.containers import Vertical
from textual.geometry import Region
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.widgets.chat.fold_rows import FOLDED_ARROW, OPEN_ARROW
from chrys.app.tui.widgets.chat.guide_fold import SameCellClick
from chrys.app.tui.widgets.chat.transcript_panel import ChatTranscriptPanelMarker, note_fold
from chrys.app.tui.widgets.selection import NonSelectableStatic
from chrys.app.tui.widgets.sidebar.context import CONTEXT_BLOCK_MESSAGES, CONTEXT_TURN_RANGE, CONTEXT_TURN_SINGLE
from chrys.foundation.i18n import msg
from chrys.foundation.i18n.formatting import format_message

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.events import Click
    from textual.geometry import Offset

    from chrys.foundation.i18n import MessageRef

_CONTEXT_FOLD_COMPRESSED = msg("tui.transcript.fold.compressed", fallback="Compressed")


class ContextFoldWidget(SameCellClick, Vertical):
    """A centered compression marker whose summary starts folded; the marker row or /fold toggles it."""

    DEFAULT_CSS = """
    ContextFoldWidget {
        margin: 1 0;
        color: $text-muted;
        height: auto;
    }
    ContextFoldWidget > NonSelectableStatic {
        width: 1fr;
        height: 1;
        text-align: center;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    ContextFoldWidget > #summary {
        padding: 0 1;
        text-align: center;
        text-style: dim italic;
    }
    """

    def __init__(
        self,
        compressed_context_id: str,
        summary: str,
        freed_messages: int = 0,
        turn_range: tuple[int, int] = (0, 0),
        *,
        resolve_message: Callable[[MessageRef], str] = format_message,
    ) -> None:
        self._compressed_context_id = compressed_context_id
        self._compressed_label = resolve_message(_CONTEXT_FOLD_COMPRESSED.bind())
        first_turn, last_turn = turn_range
        detail = ""
        if first_turn > 0 and last_turn >= first_turn:
            if first_turn == last_turn:
                detail = resolve_message(CONTEXT_TURN_SINGLE.bind(turn=first_turn))
            else:
                detail = resolve_message(CONTEXT_TURN_RANGE.bind(first=first_turn, last=last_turn))
        elif freed_messages:
            detail = resolve_message(CONTEXT_BLOCK_MESSAGES.bind(count=freed_messages, formatted=f"{freed_messages:,}"))
        self._detail = f" ({detail})" if detail else ""
        self._body = Static(summary, markup=False, id="summary")
        self._body.display = False
        self._title = NonSelectableStatic(self._title_row())
        if self.has_summary():
            self._title.styles.pointer = "pointer"
        super().__init__(self._title, self._body)

    @property
    def title_text(self) -> str:
        """The marker's label with its fold arrow, without the rules around it."""
        return self._title_label().plain

    def _title_label(self) -> Text:
        label = Text()
        if self.has_summary():
            arrow = OPEN_ARROW if self._body.display else FOLDED_ARROW
            label.append(f"{arrow} ", style="bold magenta")
        label.append(self._compressed_label, style="bold magenta")
        label.append(self._detail, style="magenta")
        return label

    def _title_row(self) -> Text:
        return Text.assemble(("═══ ", "magenta dim"), self._title_label(), (" ═══", "magenta dim"))

    def has_summary(self) -> bool:
        """Whether there is a summary to show."""
        return bool(str(self._body.content).strip())

    @property
    def collapsed(self) -> bool:
        """Whether the summary is hidden."""
        return not self._body.display

    @collapsed.setter
    def collapsed(self, collapsed: bool) -> None:
        """Set summary visibility and its arrow without moving the view or focus."""
        if not self.has_summary() or self.collapsed == collapsed:
            return
        if collapsed:
            note_fold(self)
        self._body.display = not collapsed
        self._title.update(self._title_row())

    def toggle_summary(self) -> None:
        """Show or hide the summary."""
        if not self.has_summary():
            return
        if self.collapsed:
            for ancestor in self.ancestors:
                if isinstance(ancestor, ChatTranscriptPanelMarker):
                    ancestor.pause_following()
                    break
            self.call_after_refresh(self._reveal_summary_start)
        self.collapsed = not self.collapsed

    def _reveal_summary_start(self) -> None:
        parent = self.parent
        if not self._body.display or not self._body.content_region.height:
            return
        region = self.virtual_region
        height = self._body.content_region.y - self._title.region.y + 1
        start = Region(region.x, region.y, region.width, height)
        if isinstance(parent, ChatTranscriptPanelMarker):
            parent.reveal_transcript_region(start)
        elif isinstance(parent, Widget):
            parent.scroll_to_region(start, animate=False, immediate=True, x_axis=False)

    def fold_click_at(self, origin: Widget, cell: Offset) -> bool:
        return origin is self._title and self.has_summary()

    def on_click(self, event: Click) -> None:
        if self.has_summary() and event.screen_offset in self._title.region:
            event.stop()
            if self.clicked_same_cell(event) and event.chain == 1:
                self.toggle_summary()
