# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ContextPanel — token usage visualization and compressed block list."""

from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.cells import cell_len
from rich.text import Text
from textual.events import Resize
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import RichLog, Sparkline, Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.formatting import format_token_count
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.service.profiles.models.schema import DEFAULT_MAX_CONTEXT_TOKENS

_CONTEXT_USAGE = msg("tui.sidebar.context.usage", fallback="Context Usage")
_CONTEXT_TOKEN_USAGE = msg("tui.sidebar.context.token_usage", fallback="Token Usage")
_CONTEXT_COMPRESSED_MESSAGES = msg("tui.sidebar.context.compressed_messages", fallback="Compressed Messages")
_CONTEXT_CACHED = msg("tui.sidebar.context.cached", fallback="Cached")
_CONTEXT_INPUT = msg("tui.sidebar.context.input", fallback="Input")
_CONTEXT_OUTPUT = msg("tui.sidebar.context.output", fallback="Output")
_CONTEXT_TOTAL = msg("tui.sidebar.context.total", fallback="Total")
CONTEXT_TURN_SINGLE = msg("tui.sidebar.context.turn_single", fallback="Turn {turn}")
CONTEXT_TURN_RANGE = msg("tui.sidebar.context.turn_range", fallback="Turn {first}-{last}")
CONTEXT_BLOCK_MESSAGES = msg(
    "tui.sidebar.context.block_messages",
    fallback="{formatted} message",
    plural_fallback="{formatted} messages",
)
_CONTEXT_ROW_MESSAGES = (_CONTEXT_CACHED, _CONTEXT_INPUT, _CONTEXT_OUTPUT, _CONTEXT_TOTAL)
_LEGACY_CONTEXT_LABEL_WIDTH = cell_len(_CONTEXT_CACHED.fallback) + 1

if TYPE_CHECKING:
    from collections.abc import Callable

    from textual.app import ComposeResult

    from chrys.app.tui.i18n import LocaleController


def _format_breakdown(value: int, total: int) -> str:
    # Em-dash signals "provider didn't report this split" — distinguishes it
    # from the genuine pre-first-call zero state (where total is also 0).
    if value == 0 and total > 0:
        return "\u2014"
    return format_token_count(value)


def _format_optional(value: int | None) -> str:
    """Render an optional token count: ``-`` when the provider didn't report."""
    if value is None:
        return "-"
    return format_token_count(value)


@dataclass(frozen=True, slots=True)
class _CompressedBlock:
    """Semantic state of one compressed context block, rendered per locale."""

    context_id: str
    summary: str
    freed_messages: int
    turn_range: tuple[int, int]


class _CompressedBlocksLog(RichLog):
    """Retain blocks so hidden-tab writes and width changes can be reflowed."""

    _MAX_RENDERED_LINES = 200
    _MAX_RETAINED_BLOCKS = 200

    def __init__(self, renderer: Callable[[_CompressedBlock], Text]) -> None:
        super().__init__(id="ctx-snapshots", max_lines=self._MAX_RENDERED_LINES, min_width=1, wrap=True)
        self._render_block = renderer
        self._blocks: deque[_CompressedBlock] = deque(maxlen=self._MAX_RETAINED_BLOCKS)
        self._last_render_width = 0
        self._needs_reflow = False
        self._reflow_scheduled = False

    def write_block(self, block: _CompressedBlock) -> None:
        """Retain and render one block when a usable content width exists."""
        self._blocks.append(block)
        width = self.scrollable_content_region.width
        if width <= 0 or not self._size_known:
            self._needs_reflow = True
            return
        if self._needs_reflow or width != self._last_render_width:
            self._schedule_reflow()
            return
        super().write(self._render_block(block), width=width)

    def relocalize(self) -> None:
        """Re-render retained blocks so their prose follows the active locale."""
        if self._blocks:
            self._schedule_reflow()

    def clear(self) -> _CompressedBlocksLog:
        """Clear rendered lines and their retained reflow sources."""
        self._blocks.clear()
        self._last_render_width = 0
        self._needs_reflow = False
        super().clear()
        return self

    def on_resize(self, event: Resize) -> None:
        """Rebuild retained blocks when the log becomes visible or changes width."""
        super().on_resize(event)
        width = self.scrollable_content_region.width
        if width > 0 and (self._needs_reflow or width != self._last_render_width):
            self._schedule_reflow()

    def _schedule_reflow(self) -> None:
        self._needs_reflow = True
        if not self._reflow_scheduled:
            self._reflow_scheduled = self.call_after_refresh(self._flush_reflow)

    def _flush_reflow(self) -> None:
        self._reflow_scheduled = False
        width = self.scrollable_content_region.width
        if width <= 0:
            self._needs_reflow = True
            return
        if self._needs_reflow or width != self._last_render_width:
            self._reflow(width)

    def _reflow(self, width: int) -> None:
        super().clear()
        self._last_render_width = width
        self._needs_reflow = False
        for block in self._blocks:
            super().write(self._render_block(block), width=width)


@dataclass(frozen=True, slots=True)
class ContextUsageState:
    """View state for the Context panel's usage and session-total display."""

    used_tokens: int = 0
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    total_session_tokens: int = 0
    total_session_input_tokens: int = 0
    total_session_output_tokens: int = 0
    total_session_cache_hit_tokens: int | None = None
    update_window: bool = True

    @classmethod
    def with_window(
        cls,
        *,
        used_tokens: int,
        max_context_tokens: int,
        total_session_tokens: int = 0,
        total_session_input_tokens: int = 0,
        total_session_output_tokens: int = 0,
        total_session_cache_hit_tokens: int | None = None,
    ) -> ContextUsageState:
        """Update the main-session used/max gauge and the session totals."""
        return cls(
            used_tokens=used_tokens,
            max_context_tokens=max_context_tokens,
            total_session_tokens=total_session_tokens,
            total_session_input_tokens=total_session_input_tokens,
            total_session_output_tokens=total_session_output_tokens,
            total_session_cache_hit_tokens=total_session_cache_hit_tokens,
            update_window=True,
        )

    @classmethod
    def session_totals_only(
        cls,
        previous: ContextUsageState | None,
        *,
        fallback_used_tokens: int,
        total_session_tokens: int = 0,
        total_session_input_tokens: int = 0,
        total_session_output_tokens: int = 0,
        total_session_cache_hit_tokens: int | None = None,
    ) -> ContextUsageState:
        """Advance totals without changing the main-session used/max gauge."""
        used_tokens = previous.used_tokens if previous else fallback_used_tokens
        max_context_tokens = previous.max_context_tokens if previous else cls().max_context_tokens
        return cls(
            used_tokens=used_tokens,
            max_context_tokens=max_context_tokens,
            total_session_tokens=total_session_tokens,
            total_session_input_tokens=total_session_input_tokens,
            total_session_output_tokens=total_session_output_tokens,
            total_session_cache_hit_tokens=total_session_cache_hit_tokens,
            update_window=False,
        )


class ContextPanel(Widget, can_focus=False):
    """Displays token usage trend (sparkline) and list of compressed blocks."""

    usage_state: reactive[ContextUsageState | None] = reactive(None, always_update=True)

    DEFAULT_CSS = """
    ContextPanel {
        height: 100%;
        width: 100%;
    }
    ContextPanel > Static {
        height: auto;
        padding: 0 1;
    }
    ContextPanel > .ctx-label {
        text-style: bold;
        color: $text-muted;
    }
    ContextPanel > Sparkline {
        height: 1;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    ContextPanel > #ctx-session-total {
        margin: 0 0 1 0;
    }
    ContextPanel > RichLog {
        height: 1fr;
        border: round $tui-border-primary-darken-3 $inner-border-opacity;
        scrollbar-size: 1 1;
        overflow-x: hidden;
    }
    SidebarPanel.-shell-active ContextPanel > RichLog {
        border: round $tui-border-warning-darken-1 $inner-border-opacity;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._usage_history: list[float] = [0.0]
        self._current_used = 0
        self._current_max = DEFAULT_MAX_CONTEXT_TOKENS
        self._total_session_tokens = 0
        self._total_session_input_tokens = 0
        self._total_session_output_tokens = 0
        self._total_session_cache_hit_tokens: int | None = None

    def watch_usage_state(self, state: ContextUsageState | None) -> None:
        """Apply routed usage state from the screen."""
        if state is None:
            return
        if state.update_window:
            self.update_usage(
                state.used_tokens,
                state.max_context_tokens,
                state.total_session_tokens,
                state.total_session_input_tokens,
                state.total_session_output_tokens,
                state.total_session_cache_hit_tokens,
            )
            return
        self._current_used = state.used_tokens
        self._current_max = state.max_context_tokens
        with contextlib.suppress(Exception):
            self._update_usage_text()
        self.update_session_totals(
            total_session_tokens=state.total_session_tokens,
            total_session_input_tokens=state.total_session_input_tokens,
            total_session_output_tokens=state.total_session_output_tokens,
            total_session_cache_hit_tokens=state.total_session_cache_hit_tokens,
        )

    def compose(self) -> ComposeResult:
        yield Static(Text(self._render_message(_CONTEXT_USAGE.bind())), classes="ctx-label", id="ctx-usage-label")
        yield Static(
            Text(f"{self._current_used:,} / {self._current_max:,} (0.0%)"),
            id="ctx-usage-text",
        )
        yield Sparkline(self._usage_history, id="ctx-sparkline")
        yield Static(
            Text(self._render_message(_CONTEXT_TOKEN_USAGE.bind())),
            classes="ctx-label",
            id="ctx-token-usage-label",
        )
        yield Static(Text(self._format_session_row("\u2713", _CONTEXT_CACHED.bind(), "-")), id="ctx-session-cache-hit")
        yield Static(Text(self._format_session_row("\u2191", _CONTEXT_INPUT.bind(), "0")), id="ctx-session-input")
        yield Static(Text(self._format_session_row("\u2193", _CONTEXT_OUTPUT.bind(), "0")), id="ctx-session-output")
        yield Static(Text(self._format_session_row("\u03a3", _CONTEXT_TOTAL.bind(), "0")), id="ctx-session-total")
        yield Static(
            Text(self._render_message(_CONTEXT_COMPRESSED_MESSAGES.bind())),
            classes="ctx-label",
            id="ctx-compressed-label",
        )
        yield _CompressedBlocksLog(self._render_compressed_block)

    def refresh_localization(self) -> None:
        """Retranslate headings and rebuild current usage rows in place."""
        if not self.is_mounted:
            return
        self.query_one("#ctx-usage-label", Static).update(Text(self._render_message(_CONTEXT_USAGE.bind())))
        self.query_one("#ctx-token-usage-label", Static).update(Text(self._render_message(_CONTEXT_TOKEN_USAGE.bind())))
        self.query_one("#ctx-compressed-label", Static).update(
            Text(self._render_message(_CONTEXT_COMPRESSED_MESSAGES.bind()))
        )
        self._refresh_session_total_rows()
        self.query_one("#ctx-snapshots", _CompressedBlocksLog).relocalize()

    def _render_message(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        if controller is None:
            return format_message(reference)
        return render_str(controller.localizer, reference)

    def _session_label_width(self) -> int:
        return max(
            _LEGACY_CONTEXT_LABEL_WIDTH,
            *(cell_len(self._render_message(definition.bind())) for definition in _CONTEXT_ROW_MESSAGES),
        )

    def _format_session_row(self, glyph: str, label_ref: MessageRef, value: str) -> str:
        label = self._render_message(label_ref)
        padding = " " * (self._session_label_width() - cell_len(label))
        return f"{glyph} {label}{padding}{value:>8}"

    def _refresh_session_total_rows(self) -> None:
        self.query_one("#ctx-session-input", Static).update(
            Text(
                self._format_session_row(
                    "\u2191",
                    _CONTEXT_INPUT.bind(),
                    _format_breakdown(self._total_session_input_tokens, self._total_session_tokens),
                )
            )
        )
        self.query_one("#ctx-session-output", Static).update(
            Text(
                self._format_session_row(
                    "\u2193",
                    _CONTEXT_OUTPUT.bind(),
                    _format_breakdown(self._total_session_output_tokens, self._total_session_tokens),
                )
            )
        )
        self.query_one("#ctx-session-total", Static).update(
            Text(
                self._format_session_row(
                    "\u03a3", _CONTEXT_TOTAL.bind(), format_token_count(self._total_session_tokens)
                )
            )
        )
        self.query_one("#ctx-session-cache-hit", Static).update(
            Text(
                self._format_session_row(
                    "\u2713",
                    _CONTEXT_CACHED.bind(),
                    _format_optional(self._total_session_cache_hit_tokens),
                )
            )
        )

    def reset(self, max_context_tokens: int = 0) -> None:
        """Reset usage state for a new session."""
        self._usage_history = [0.0]
        self._current_used = 0
        self._total_session_tokens = 0
        self._total_session_input_tokens = 0
        self._total_session_output_tokens = 0
        self._total_session_cache_hit_tokens = None
        if max_context_tokens > 0:
            self._current_max = max_context_tokens
        try:
            self.query_one("#ctx-usage-text", Static).update(f"0 / {self._current_max:,} (0.0%)")
            self.query_one("#ctx-sparkline", Sparkline).data = list(self._usage_history)
            self._refresh_session_total_rows()
            self.query_one("#ctx-snapshots", _CompressedBlocksLog).clear()
        except Exception:
            pass

    def update_usage(
        self,
        used: int,
        max_tokens: int,
        total_session_tokens: int = 0,
        total_session_input_tokens: int = 0,
        total_session_output_tokens: int = 0,
        total_session_cache_hit_tokens: int | None = None,
    ) -> None:
        """Record a new usage data point."""
        self._current_used = used
        self._current_max = max_tokens
        pct = used / max_tokens * 100 if max_tokens > 0 else 0.0
        self._usage_history.append(pct)
        # Keep last 60 data points
        if len(self._usage_history) > 60:
            self._usage_history = self._usage_history[-60:]
        self.update_session_totals(
            total_session_tokens=total_session_tokens,
            total_session_input_tokens=total_session_input_tokens,
            total_session_output_tokens=total_session_output_tokens,
            total_session_cache_hit_tokens=total_session_cache_hit_tokens,
        )
        try:
            self._update_usage_text()
            self.query_one("#ctx-sparkline", Sparkline).data = list(self._usage_history)
        except Exception:
            pass

    def _update_usage_text(self) -> None:
        pct = self._current_used / self._current_max * 100 if self._current_max > 0 else 0.0
        self.query_one("#ctx-usage-text", Static).update(f"{self._current_used:,} / {self._current_max:,} ({pct:.1f}%)")

    def update_session_totals(
        self,
        *,
        total_session_tokens: int = 0,
        total_session_input_tokens: int = 0,
        total_session_output_tokens: int = 0,
        total_session_cache_hit_tokens: int | None = None,
    ) -> None:
        """Update only the cross-source session totals row.

        Use this for sub-agent UsageUpdates \u2014 their used/max belong to the
        sub-agent's window and must not overwrite the main session's gauge,
        but their token costs still accumulate into the session totals shown
        below the gauge.
        """
        self._total_session_tokens = total_session_tokens
        self._total_session_input_tokens = total_session_input_tokens
        self._total_session_output_tokens = total_session_output_tokens
        self._total_session_cache_hit_tokens = total_session_cache_hit_tokens
        with contextlib.suppress(Exception):
            self._refresh_session_total_rows()

    def clear_blocks(self) -> None:
        """Clear the compressed blocks log."""
        self.query_one("#ctx-snapshots", _CompressedBlocksLog).clear()

    def add_compressed_block(
        self,
        compressed_context_id: str,
        summary: str,
        freed_messages: int = 0,
        turn_range: tuple[int, int] = (0, 0),
    ) -> None:
        """Record a compressed context block."""
        log = self.query_one("#ctx-snapshots", _CompressedBlocksLog)
        log.write_block(
            _CompressedBlock(
                context_id=compressed_context_id,
                summary=summary,
                freed_messages=freed_messages,
                turn_range=turn_range,
            )
        )

    def _render_compressed_block(self, block: _CompressedBlock) -> Text:
        """Render one retained block's prose in the active locale."""
        t = Text()
        t.append(f"\u2022 {self._compressed_block_title(block)}", style="magenta")
        if block.freed_messages:
            freed = self._render_message(
                CONTEXT_BLOCK_MESSAGES.bind(count=block.freed_messages, formatted=f"{block.freed_messages:,}")
            )
            t.append(f"  {freed}", style="dim")
        t.append(f"\n{block.summary}", style="dim italic")
        return t

    def _compressed_block_title(self, block: _CompressedBlock) -> str:
        """Render a compressed block's turn span, retaining its recall id."""
        turn_label = self._render_turn_range(block.turn_range)
        if turn_label:
            return f"{turn_label} ({block.context_id})"
        return block.context_id

    def _render_turn_range(self, turn_range: tuple[int, int]) -> str:
        first_turn, last_turn = turn_range
        if first_turn <= 0 or last_turn < first_turn:
            return ""
        if first_turn == last_turn:
            return self._render_message(CONTEXT_TURN_SINGLE.bind(turn=first_turn))
        return self._render_message(CONTEXT_TURN_RANGE.bind(first=first_turn, last=last_turn))
