# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A temporary one-line view of the current activity in a running process."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.text import Text
from textual.events import Click
from textual.message import Message
from textual.widgets import Static

from chrys.app.tui.widgets.chat.fold_gap import FOLD_GAP_ROWS, FoldGap
from chrys.app.tui.widgets.chat.fold_rows import FOLDED_ARROW, ToolTally
from chrys.app.tui.widgets.chat.guide_fold import ProcessGuideMixin
from chrys.app.tui.widgets.chat.messages import AgentMessage, ThinkingMessage
from chrys.app.tui.widgets.chat.process_block import render_process_title
from chrys.app.tui.widgets.chat.process_visibility import LiveStatus
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.loading import RunningTitle

if TYPE_CHECKING:
    from textual.geometry import Size

type LiveProcessMember = AgentMessage | ThinkingMessage | ToolGroup


class LiveProcessRow(ProcessGuideMixin, RunningTitle, FoldGap, Static, can_focus=False):
    """Show the latest member while closed and a tally above the members while open."""

    @dataclass
    class ToggleRequested(Message):
        row: LiveProcessRow

    SHIMMER_COMPONENT: ClassVar[str] = "toolgrouptitle--shimmer"
    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "processblock--detail",
        "processblock--failed",
        "processblock--interrupted",
        "liveprocessrow--thought",
        "liveprocessrow--text",
        "toolgrouptitle--detail",
        "toolgrouptitle--running",
        "toolgrouptitle--failed",
        SHIMMER_COMPONENT,
    }
    DEFAULT_CSS = """
    LiveProcessRow {
        height: auto;
        padding: 0 1 0 0;
        color: $text-muted;
        overflow: hidden;
    }
    LiveProcessRow > .processblock--detail { color: $text-muted; }
    LiveProcessRow > .processblock--failed { color: $text-error; }
    LiveProcessRow > .processblock--interrupted { color: $text-warning; }
    LiveProcessRow > .liveprocessrow--thought { color: $text-primary; }
    LiveProcessRow > .liveprocessrow--text { color: $text; }
    LiveProcessRow > .toolgrouptitle--detail { color: $text-warning; }
    LiveProcessRow > .toolgrouptitle--running { color: $text-muted; }
    LiveProcessRow > .toolgrouptitle--failed { color: $text-error; }
    LiveProcessRow > .toolgrouptitle--shimmer { color: $text; }
    """

    def __init__(self) -> None:
        super().__init__()
        self.collapsed = True
        self.members: list[LiveProcessMember] = []
        self.tally = ToolTally()
        self.settled = False
        self.summarized = False
        self.answering: AgentMessage | None = None
        """The answer below this segment, which summarizes once its work is idle."""
        self.running_members: set[LiveProcessMember] = set()
        self._status: LiveStatus | None = None

    @property
    def latest(self) -> LiveProcessMember | None:
        return self.members[-1] if self.members else None

    def focus_on_click(self) -> bool:
        return False

    def guide_members(self) -> list[LiveProcessMember]:
        return self.members

    def ends_folded(self) -> bool:
        return True

    def takes_fold_gap(self) -> bool:
        return True

    def _content_rows(self, container: Size, viewport: Size, width: int) -> int:
        return 1 + (FOLD_GAP_ROWS if self.gap_above else 0)

    def _size_updated(self, size: Size, virtual_size: Size, container_size: Size, layout: bool = True) -> bool:
        # The layout that measured the gap already placed this row.
        return super()._size_updated(size, virtual_size, container_size, layout=False)

    def update_status(self, *, resample: bool = False) -> None:
        latest = self.latest
        if not self.settled or resample:
            self._status = latest.live_status() if latest is not None else None
        self.set_running(self.collapsed and not self.settled and not self.summarized and latest in self.running_members)
        self.refresh(layout=False)

    def settle(self) -> None:
        """Freeze the preview when the turn goes on below a segment boundary."""
        self.answering = None
        self.summarized = False
        self.running_members.clear()
        self.settled = True
        self.update_status(resample=True)

    def render(self) -> Text:
        line = self._render_title()
        return Text.assemble(self.gap_text(), "\n", line) if self.gap_above else line

    def _render_title(self) -> Text:
        if not self.collapsed or self.summarized or self._status is None:
            return render_process_title(self, self.tally, collapsed=self.collapsed)
        width = self.content_size.width
        text = self._status(self, max(width - 2, 0), self._work_running)
        line = Text.assemble(f"{FOLDED_ARROW} ", text)
        if width:
            line.truncate(width, overflow="ellipsis")
        return line

    def on_click(self, event: Click) -> None:
        if self._click_guide(event):
            return
        event.prevent_default()
        event.stop()
        if self.clicked_same_cell(event) and self._process_title_at(event.screen_offset):
            self.post_message(self.ToggleRequested(self))
