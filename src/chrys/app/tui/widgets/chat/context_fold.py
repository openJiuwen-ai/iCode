# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ContextFoldWidget — visual representation of a context compression event."""

from __future__ import annotations

from collections.abc import Callable

from rich.text import Text
from textual.widgets import Static

from chrys.app.tui.widgets.sidebar.context import CONTEXT_BLOCK_MESSAGES, CONTEXT_TURN_RANGE, CONTEXT_TURN_SINGLE
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_block

_CONTEXT_FOLD_COMPRESSED = msg("tui.transcript.fold.compressed", fallback="Compressed")


class ContextFoldWidget(Static):
    """Displays a context compression with summary and freed message count.

    Shown inline in the chat when the compression mechanism folds messages
    to free up context window space.
    """

    DEFAULT_CSS = """
    ContextFoldWidget {
        margin: 1 0;
        padding: 0 1;
        text-align: center;
        color: $text-muted;
        height: auto;
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
        self._summary = summary
        self._freed_messages = freed_messages
        self._turn_range = turn_range
        self._compressed_label = resolve_message(_CONTEXT_FOLD_COMPRESSED.bind())
        first_turn, last_turn = turn_range
        self._turn_label = ""
        if first_turn > 0 and last_turn >= first_turn:
            if first_turn == last_turn:
                self._turn_label = resolve_message(CONTEXT_TURN_SINGLE.bind(turn=first_turn))
            else:
                self._turn_label = resolve_message(CONTEXT_TURN_RANGE.bind(first=first_turn, last=last_turn))
        self._freed_label = ""
        if freed_messages:
            self._freed_label = resolve_message(
                CONTEXT_BLOCK_MESSAGES.bind(count=freed_messages, formatted=f"{freed_messages:,}")
            )
        super().__init__()

    def render(self) -> Text:
        t = Text()
        # Top rule
        t.append("  ═══", style="magenta dim")
        t.append(f" {self._compressed_label}", style="bold magenta")
        if self._turn_label:
            t.append(f" ({self._turn_label})", style="magenta")
        elif self._freed_label:
            t.append(f" ({self._freed_label})", style="magenta")
        t.append(" ═══", style="magenta dim")
        # Summary
        if self._summary:
            t.append(f"\n  {sanitize_legacy_block(self._summary)}", style="dim italic")
        return t
