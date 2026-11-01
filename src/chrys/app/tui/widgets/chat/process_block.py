# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A persistent process header whose first-stage content remains adjacent siblings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from textual.events import Click
from textual.message import Message
from textual.widgets import Static

from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.widgets.chat.fold_gap import FoldGap
from chrys.app.tui.widgets.chat.fold_rows import (
    FOLDED_ARROW,
    OPEN_ARROW,
    FoldRow,
    ToolTally,
    fit_fold_row,
    fold_row_styles,
)
from chrys.app.tui.widgets.chat.messages import ThinkingMessage, folds_with_turn
from chrys.app.tui.widgets.chat.process_visibility import ProcessMember
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chat.transcript_panel import note_fold
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from rich.text import Text

_CALLS = msg("tui.chat.process.tool_calls", fallback="{count} tool call: ", plural_fallback="{count} tool calls: ")
_ACTIVITY = msg("tui.chat.process.activity", fallback="Activity")


class ProcessBlock(FoldGap, Static, can_focus=False):
    """A turn's process header: the calls its tool groups made, by kind, and how many failed or were interrupted.

    Collapse intent survives blocking members; their terminal notifications retry it.
    """

    @dataclass
    class ToggleRequested(Message):
        block: ProcessBlock

    COMPONENT_CLASSES: ClassVar[set[str]] = {
        "processblock--detail",
        "processblock--failed",
        "processblock--interrupted",
    }

    DEFAULT_CSS = """
    ProcessBlock {
        height: 1;
        padding: 0 1 0 0;
        color: $text-muted;
        overflow: hidden;
    }
    ProcessBlock > .processblock--detail {
        color: $text-muted;
    }
    ProcessBlock > .processblock--failed {
        color: $text-error;
    }
    ProcessBlock > .processblock--interrupted {
        color: $text-warning;
    }
    """

    def __init__(
        self, members: list[ProcessMember], *, segment_index: int, tally: ToolTally, is_compressed: bool = False
    ) -> None:
        super().__init__()
        self.members = members
        self.segment_index = segment_index
        self.tally = tally
        """What its tool groups' calls were, kept current as they change."""
        self.is_compressed = is_compressed
        self.collapsed = False
        self.requested_collapsed = True

    def focus_on_click(self) -> bool:
        """Keep transcript clicks in the composer, as tool and thinking headers do."""
        return False

    def ends_folded(self) -> bool:
        # Its one row heads the rows it opens to: a gap parts the first of them from it.
        return True

    @property
    def tool_count(self) -> int:
        return self.tally.total

    @property
    def blocked(self) -> bool:
        return any(
            (isinstance(member, ToolGroup) and (not member.all_complete or member.collapse_locked))
            or (isinstance(member, ThinkingMessage) and not member.foldable)
            for member in self.members
        )

    def render(self) -> Text:
        """``▸ 12 tool calls: 8 reads · 4 commands · 1 failed``, cut to the width with the outcomes kept in view.

        A turn in compressed history, or one that made no calls, reads ``▸ Activity``.
        """
        localizer = widget_localizer(self)
        arrow = FOLDED_ARROW if self.collapsed else OPEN_ARROW
        tally = self.tally
        if tally.total and not self.is_compressed:
            calls = render_str(localizer, _CALLS.bind(count=tally.total))
            failed, interrupted = tally.outcomes(localizer)
            row = FoldRow(f"{arrow} {calls}", tally.count_words(localizer), failed, interrupted)
        else:
            row = FoldRow(f"{arrow} {render_str(localizer, _ACTIVITY.bind())}")
        return fit_fold_row(row, self.content_size.width, fold_row_styles(self, "processblock"))

    def try_finalize_fold(self) -> None:
        """Apply the latest intent, including after the last interactive member finishes."""
        collapsed = self.requested_collapsed and not self.blocked
        if self.is_mounted and collapsed:
            focused = self.app.focused
            if focused is not None and any(focused in member.walk_children(with_self=True) for member in self.members):
                self.screen.set_focus(None, scroll_visible=False)
        for member in self.members:
            if collapsed and folds_with_turn(member):
                member.collapsed = True
            member.set_process_hidden(collapsed)
        if self.collapsed != collapsed:
            if collapsed:
                # Its members hide, folded as they ended or not.
                note_fold(self)
            self.collapsed = collapsed
            self.refresh(layout=False)

    def action_toggle_process(self) -> None:
        self.post_message(self.ToggleRequested(self))

    def on_click(self, event: Click) -> None:
        event.prevent_default()
        event.stop()
        self.action_toggle_process()
