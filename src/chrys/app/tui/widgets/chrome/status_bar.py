# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""StatusBar — profile/model selectors and agent activity above the InputBar.

Layout: Agent [profile] Model [model] [LoadingIndicator] [status text] [run trail] ... [tool info]
Flash:  Agent [profile] Model [model] [message text] ... [tool counts]
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

from rich.cells import cell_len
from rich.text import Text
from textual.containers import Horizontal
from textual.css.scalar import Scalar
from textual.events import Click, Resize
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.visibility import (
    resync_compositor_regions,
    set_widget_visibility_without_layout,
    set_widgets_visibility_without_layout,
)
from chrys.app.tui.widgets import ChrysLoadingIndicator
from chrys.app.tui.widgets.selection import NonSelectableStatic
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message
from chrys.foundation.text.tokenizer import TOKEN_UNITS_PER_TOKEN, MixedLanguageTokenizer

if TYPE_CHECKING:
    from textual.app import ComposeResult, RenderResult

    from chrys.app.tui.i18n import LocaleController
    from chrys.app.tui.screens.main.model_indicator import ModelIndicatorState


type StatusMessage = MessageRef | str
type StatusTrail = StatusMessage | tuple[StatusMessage | TrailCount, ...]


@dataclass(frozen=True, slots=True)
class TrailCount:
    """A count in the trail: when the trail does not fit, it shows in its *short* form, at the end."""

    message: StatusMessage
    short: StatusMessage


_TRAIL_SEPARATOR = " \u00b7 "


_WIDE_TAG_LAYOUT_MIN_COLUMNS = 72
_WIDE_PROFILE_TAG_CELLS = 18
_WIDE_MODEL_TAG_CELLS = 26
_NARROW_PROFILE_TAG_CELLS = 10
_NARROW_MODEL_TAG_CELLS = 12
_COMPACT_LAYOUT_MIN_COLUMNS = 64
_COMPACT_PROFILE_TAG_CELLS = 7
_COMPACT_MODEL_TAG_CELLS = 10

STATUS_TOOL_CALLS = msg(
    "tui.status.tool_calls",
    fallback="{count} tool call",
    plural_fallback="{count} tool calls",
)
STATUS_ELAPSED_SECONDS = msg("tui.status.elapsed_seconds", fallback="{seconds}s")
STATUS_ELAPSED_MINUTES_SECONDS = msg(
    "tui.status.elapsed_minutes_seconds",
    fallback="{minutes}m {seconds}s",
)
STATUS_ELAPSED_HOURS_MINUTES_SECONDS = msg(
    "tui.status.elapsed_hours_minutes_seconds",
    fallback="{hours}h {minutes}m {seconds}s",
)
STATUS_ELAPSED_DAYS_HOURS_MINUTES_SECONDS = msg(
    "tui.status.elapsed_days_hours_minutes_seconds",
    fallback="{days}d {hours}h {minutes}m {seconds}s",
)
STATUS_OUTPUT_SPEED = msg("tui.status.output_speed", fallback="{speed} tok/s")
STATUS_DETAILS_TOOLTIP = msg("tui.status.details_tooltip", fallback="Click for details")
STATUS_AGENT_SELECTOR_LABEL = msg("tui.status.agent_selector_label", fallback="Agent")
STATUS_MODEL_SELECTOR_LABEL = msg("tui.status.model_selector_label", fallback="Model")

STATUS_INTERRUPTED = msg("tui.status.interrupted", fallback="Interrupted by user")
STATUS_COMPLETED = msg("tui.status.completed", fallback="Completed in {elapsed}")
STATUS_RUNNING_TOOL = msg("tui.status.running_tool", fallback="Running: {tool_name}")
STATUS_RETRYING = msg(
    "tui.status.retrying",
    fallback="Retrying ({attempt}/{max_attempts})...",
)
STATUS_SHELL_MODE = msg(
    "tui.status.shell_mode",
    fallback="Shell mode — double Esc or enter exit to quit",
)
STATUS_INTERACTIVE_MODE = msg(
    "tui.status.interactive_mode",
    fallback="Interactive mode — all input forwarded to the running process",
)
STATUS_AGENT_LOAD_FAILED = msg(
    "tui.status.agent_load_failed",
    fallback="Agent load failed: {message}",
)
STATUS_THINKING = msg("tui.status.thinking", fallback="Thinking")
STATUS_STREAMING = msg("tui.status.streaming", fallback="streaming...")
STATUS_COMPACTING = msg("tui.status.compacting", fallback="Compacting conversation...")
STATUS_ERROR = msg("tui.status.error", fallback="Error: {message}")
STATUS_OPENED_FORK = msg("tui.status.opened_fork", fallback="Opened fork: {fork_id}")
STATUS_FORK_CREATED = msg("tui.status.fork_created", fallback="Fork created: {fork_id}")
STATUS_SESSION_TITLE_UPDATED = msg(
    "tui.status.session_title_updated",
    fallback="Session title updated",
)
STATUS_CUSTOM_TITLE_CLEARED = msg(
    "tui.status.custom_title_cleared",
    fallback="Custom title cleared",
)
STATUS_FORK_NOTICE = msg("tui.status.fork_notice", fallback="Fork: {message}")
STATUS_SESSION_RESTORED = msg(
    "tui.status.session_restored",
    fallback="Session restored: {session_id}",
)
STATUS_RESUMING = msg("tui.status.resuming", fallback="Resuming")
STATUS_STILL_SENDING = msg("tui.status.still_sending", fallback="Still sending the previous message")
STATUS_AGENT_STARTUP_FAILED = msg(
    "tui.status.agent_startup_failed",
    fallback="Agent startup failed: {message}",
)

_MIN_WIDE_STATUS_BODY_CELLS = 40
_SELECTOR_GROUP_HORIZONTAL_PADDING_CELLS = 2
_SELECTOR_LABEL_HORIZONTAL_PADDING_CELLS = 2
_SELECTOR_TAG_HORIZONTAL_PADDING_CELLS = 2
_MODEL_SELECTOR_LEFT_MARGIN_CELLS = 1
_STATUS_MIN_INTERVAL_SECONDS = 0.15
"""Shortest time between two changes of the run status.

A change that comes sooner waits for the interval and gives way to a newer
one, so a burst of changes repaints the row once; repeating the status shown
(once per streamed delta) repaints nothing.
"""
_OUTPUT_SPEED_WINDOW_SECONDS = 5.0
"""How far back the shown speed looks, so it follows the stream rather than the run."""
_OUTPUT_SPEED_IDLE_SECONDS = 3.0
"""The speed leaves the trail once no text came for this long: a running tool has no speed to show."""
_OUTPUT_SPEED_MIN_SPAN_SECONDS = 1.0
"""A burst just begun is measured over at least this long, or its first delta would read as a huge speed."""
_OUTPUT_TOKENIZER = MixedLanguageTokenizer()


@dataclass
class _FlashState:
    text: StatusMessage
    trail: StatusTrail
    warn: bool
    error: bool
    caution: bool
    elapsed_seconds: int | None = None


class _StatusBarChromeLabel(NonSelectableStatic):
    """Status chrome excluded from terminal drag selection and copy."""


class _TrailInfo(Static):
    """The right-aligned trail; one too wide for it keeps its counts, in their short form, and cuts the rest."""

    def __init__(self, *, classes: str, id: str) -> None:
        super().__init__("", classes=classes, id=id)
        self._narrow = ("", "")

    def show_trail(self, full: str, narrow: tuple[str, str]) -> None:
        """Show *full*, or *narrow* — the other parts and the counts' short forms, each joined — when it does not fit."""
        self._narrow = narrow
        self.update(Text(full), layout=False)

    def render(self) -> RenderResult:
        others, counts = self._narrow
        width = self.size.width
        if not counts or not width or cell_len(str(self.content)) <= width:
            return super().render()
        room = width - cell_len(_TRAIL_SEPARATOR + counts)
        if not others or room < 1:
            return Text(counts)
        head = Text(others)
        head.truncate(room, overflow="ellipsis")
        return Text(head.plain + _TRAIL_SEPARATOR + counts)


class StatusBar(Widget):
    """Single-line profile/model controls followed by status and trail info."""

    class ProfileTagClicked(Message):
        """Posted when the profile tag is clicked."""

    class ModelTagClicked(Message):
        """Posted when the model tag is clicked, a locked tag included."""

        def __init__(self, mode: Literal["configure", "select", "locked"]) -> None:
            self.mode = mode
            super().__init__()

    class SelectorBusy(Message):
        """Posted when a selector is clicked while a run keeps it read-only."""

        def __init__(self, selector: Literal["profile", "model"]) -> None:
            self.selector = selector
            super().__init__()

    class DetailsClicked(Message):
        """Posted when the persistent runtime details area is clicked."""

    DEFAULT_CSS = """
    StatusBar {
        width: 100%;
        height: 1;
        layout: horizontal;
        display: block;
        visibility: hidden;
    }
    StatusBar > .status-selectors {
        width: auto;
        height: 1;
        padding: 0 1 0 1;
    }
    StatusBar > .status-selectors > .selector-label {
        width: auto;
        height: 1;
        background: $foreground 15%;
        color: $foreground 90%;
        padding: 0 1;
        text-wrap: nowrap;
    }
    StatusBar > .status-selectors > .model-selector-label {
        margin: 0 0 0 1;
    }
    StatusBar > .status-selectors > .profile-tag {
        width: auto;
        height: 1;
        margin: 0;
        padding: 0 1;
        background: $accent 20%;
        color: $accent;
        text-style: bold;
    }
    StatusBar > .status-selectors > .profile-tag:hover,
    StatusBar > .status-selectors > .profile-tag:focus,
    StatusBar > .status-selectors > .profile-tag.-active {
        background: $foreground;
        color: $background;
    }
    StatusBar > .status-selectors > .model-tag {
        width: auto;
        height: 1;
        margin: 0;
        padding: 0 1;
        background: $accent 20%;
        color: $accent;
        text-style: bold;
    }
    StatusBar > .status-selectors > .model-tag:hover,
    StatusBar > .status-selectors > .model-tag:focus,
    StatusBar > .status-selectors > .model-tag.-active {
        background: $foreground;
        color: $background;
    }
    StatusBar > .status-selectors > .model-tag.-locked {
        color: $accent 50%;
    }
    StatusBar > .status-selectors > .model-tag.-locked:hover,
    StatusBar > .status-selectors > .model-tag.-locked:focus,
    StatusBar > .status-selectors > .model-tag.-locked.-active {
        background: $accent 20%;
        color: $accent 50%;
    }
    StatusBar.-compact > .status-selectors > .model-tag {
        margin-left: 1;
    }
    StatusBar > .status-body {
        width: 1fr;
        height: 1;
        padding: 0;
    }
    StatusBar > .status-body > .status-run {
        position: absolute;
        height: 1;
        width: 100%;
    }
    StatusBar > .status-body > .status-run > ChrysLoadingIndicator {
        width: 6;
        height: 1;
        margin: 0 1;
        color: $accent;
    }
    StatusBar > .status-body > .status-run > .status-text {
        width: 0;
        height: 1;
        color: $accent;
        text-wrap: nowrap;
    }
    StatusBar > .status-body > .status-run > .status-trail {
        width: 0;
        height: 1;
        color: $text-muted;
        text-wrap: nowrap;
    }
    StatusBar > .status-body > .status-run > .status-tool-info {
        width: 1fr;
        height: 1;
        color: $text-muted;
        content-align: right middle;
        padding: 0 1;
    }
    StatusBar > .status-body > .status-flash-bar {
        position: absolute;
        width: 100%;
        height: 1;
        visibility: hidden;
    }
    StatusBar > .status-body > .status-flash-bar > .status-flash {
        width: 0;
        max-width: 100%;
        height: 1;
        color: $text-muted;
        padding: 0 1;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }
    StatusBar > .status-body > .status-flash-bar > .status-flash-trail {
        width: 1fr;
        height: 1;
        color: $text-muted;
        content-align: right middle;
        padding: 0 1;
        text-overflow: ellipsis;
        text-wrap: nowrap;
    }
    StatusBar.-compact > .status-body > .status-run > .status-tool-info,
    StatusBar.-compact > .status-body > .status-flash-bar > .status-flash-trail {
        display: none;
    }
    StatusBar.-warn > .status-body > .status-flash-bar > .status-flash {
        width: 1fr;
        color: $warning;
        background: $warning 15%;
    }
    StatusBar.-warn > .status-body > .status-flash-bar > .status-flash-trail {
        display: none;
    }
    StatusBar.-error > .status-body > .status-flash-bar > .status-flash {
        color: $error;
    }
    StatusBar.-caution > .status-body > .status-flash-bar > .status-flash {
        color: $warning;
    }
    """

    status: reactive[StatusMessage] = reactive("", always_update=True)
    agent_running: reactive[bool] = reactive(False, repaint=False)
    agent_loading: reactive[bool] = reactive(False, repaint=False)
    input_locked: reactive[bool] = reactive(False, repaint=False)
    shell_mode: reactive[bool] = reactive(False, repaint=False)

    def __init__(self, *, locale_controller: LocaleController | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._locale_controller = locale_controller
        self._start_time: float = 0.0
        self._tool_count: int = 0
        self._output_samples: deque[tuple[float, int]] = deque()
        self._flash: _FlashState | None = None
        self._timer: Timer | None = None
        self._status_shown_at = 0.0
        self._pending_status: StatusMessage | None = None
        self._pending_status_timer: Timer | None = None
        self._tool_trail: StatusTrail = ""
        self._last_formatted_elapsed_seconds: int | None = None
        self._current_profile: str = ""
        self._current_profile_description: str = ""
        self._current_model: str = ""
        self._current_model_tooltip: str = ""
        self._current_model_mode: Literal["configure", "select", "locked"] = "locked"
        self._current_model_visible = False
        self._content_shown = False
        self._execution_busy = False
        self._idle_shown = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="status-selectors"):
            yield _StatusBarChromeLabel("", id="agent-label", classes="selector-label agent-selector-label")
            yield _StatusBarChromeLabel("", id="profile-tag", classes="profile-tag")
            yield _StatusBarChromeLabel("", id="model-label", classes="selector-label model-selector-label")
            yield _StatusBarChromeLabel("", id="model-tag", classes="model-tag")
        with Horizontal(classes="status-body"):
            with Horizontal(classes="status-run"):
                yield ChrysLoadingIndicator()
                yield Static("", classes="status-text", id="status-text")
                yield Static("", classes="status-trail", id="status-trail")
                yield _TrailInfo(classes="status-tool-info", id="status-tool-info")
            with Horizontal(classes="status-flash-bar"):
                yield Static("", classes="status-flash", id="status-flash")
                yield _TrailInfo(classes="status-flash-trail", id="status-flash-trail")

    def on_mount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.register_surface(self)
        self._timer = self.set_interval(1.0, self._tick)
        self._sync_selector_visibility()
        self._refresh_tags()
        self._sync_visibility(self.visible)
        self.refresh_localization()

    def on_unmount(self) -> None:
        if self._locale_controller is not None:
            self._locale_controller.unregister_surface(self)
        if self._timer is not None:
            self._timer.stop()
        self._cancel_pending_status()

    def _tick(self) -> None:
        if self._content_shown and self._flash is None:
            self._refresh_trail()

    def watch_status(self, status: StatusMessage) -> None:
        if not self.is_mounted:
            return
        self._refresh_status_text()
        if self._content_shown and self._flash is None:
            self._refresh_trail()

    def set_profile(self, name: str, description: str = "") -> None:
        """Update the profile selector label and tooltip."""
        self._current_profile = name
        self._current_profile_description = description
        if self.is_mounted:
            self._refresh_tags()
            self._sync_selector_visibility()
            self._show_selectors_only_if_hidden()

    def set_model(self, state: ModelIndicatorState) -> None:
        """Update the model selector label, tooltip and interaction state."""
        fields = (state.label, state.tooltip, state.mode, state.visible)
        if (
            self._current_model,
            self._current_model_tooltip,
            self._current_model_mode,
            self._current_model_visible,
        ) == fields:
            return
        self._current_model = state.label
        self._current_model_tooltip = state.tooltip
        self._current_model_mode = state.mode
        self._current_model_visible = state.visible
        if self.is_mounted:
            self._refresh_tags()
            self._sync_selector_visibility()
            self._show_selectors_only_if_hidden()

    def set_input_locked(self, locked: bool) -> None:
        """Mirror the input bar's queued-injection lock for selector guards."""
        self.input_locked = locked

    @staticmethod
    def _truncate_tag(text: str, max_cells: int) -> Text:
        rendered = Text(text)
        rendered.truncate(max_cells, overflow="ellipsis")
        return rendered

    @staticmethod
    def _full_tag_tooltip(label: str, description: str) -> Text | None:
        if not label:
            return None
        if not description:
            return Text(label)
        if label in description:
            return Text(description)
        return Text(f"{label}\n{description}")

    def _tag_cell_limits(self, available_columns: int) -> tuple[int, int]:
        if available_columns < _COMPACT_LAYOUT_MIN_COLUMNS:
            return _COMPACT_PROFILE_TAG_CELLS, _COMPACT_MODEL_TAG_CELLS
        wide_selector_cells = self._selector_cells_for_limits(
            _WIDE_PROFILE_TAG_CELLS,
            _WIDE_MODEL_TAG_CELLS,
        )
        wide = (
            available_columns >= _WIDE_TAG_LAYOUT_MIN_COLUMNS
            and available_columns - wide_selector_cells >= _MIN_WIDE_STATUS_BODY_CELLS
        )
        if wide:
            return _WIDE_PROFILE_TAG_CELLS, _WIDE_MODEL_TAG_CELLS
        return _NARROW_PROFILE_TAG_CELLS, _NARROW_MODEL_TAG_CELLS

    def _selector_cells_for_limits(self, profile_limit: int, model_limit: int) -> int:
        """Project selector width while reserving room for status content."""
        cells = _SELECTOR_GROUP_HORIZONTAL_PADDING_CELLS
        if self._current_profile:
            cells += cell_len(self._render_message(STATUS_AGENT_SELECTOR_LABEL.bind()))
            cells += _SELECTOR_LABEL_HORIZONTAL_PADDING_CELLS
            cells += min(cell_len(self._current_profile), profile_limit)
            cells += _SELECTOR_TAG_HORIZONTAL_PADDING_CELLS
        if self._current_model_visible:
            cells += _MODEL_SELECTOR_LEFT_MARGIN_CELLS
            cells += cell_len(self._render_message(STATUS_MODEL_SELECTOR_LABEL.bind()))
            cells += _SELECTOR_LABEL_HORIZONTAL_PADDING_CELLS
            cells += min(cell_len(self._current_model), model_limit)
            cells += _SELECTOR_TAG_HORIZONTAL_PADDING_CELLS
        return cells

    def _refresh_tags(self) -> None:
        """Render both selectors from their original text at the current width."""
        available_columns = self.size.width or self.app.size.width
        compact = available_columns < _COMPACT_LAYOUT_MIN_COLUMNS
        self.set_class(compact, "-compact")
        profile_limit, model_limit = self._tag_cell_limits(available_columns)
        profile_visible = bool(self._current_profile)
        agent_label = self.query_one("#agent-label", Static)
        agent_label.display = profile_visible and not compact
        agent_label.update(Text(self._render_message(STATUS_AGENT_SELECTOR_LABEL.bind())))

        profile_tag = self.query_one("#profile-tag", Static)
        profile_tag.display = profile_visible
        profile_tag.update(self._truncate_tag(self._current_profile, profile_limit))
        profile_tag.tooltip = self._full_tag_tooltip(self._current_profile, self._current_profile_description)

        model_label = self.query_one("#model-label", Static)
        model_label.display = self._current_model_visible and not compact
        model_label.update(Text(self._render_message(STATUS_MODEL_SELECTOR_LABEL.bind())))

        model_tag = self.query_one("#model-tag", Static)
        model_tag.display = self._current_model_visible
        model_tag.update(self._truncate_tag(self._current_model, model_limit))
        model_tag.tooltip = self._full_tag_tooltip(self._current_model, self._current_model_tooltip)
        self._refresh_tag_interaction_state()

    def _show_selectors_only_if_hidden(self) -> None:
        """Expose configured selectors without inventing an idle status message."""
        if self.visible or self.shell_mode or not (self._current_profile or self._current_model_visible):
            return
        set_widgets_visibility_without_layout(
            [
                (self, True),
                (self.query_one(".status-run"), False),
                (self.query_one(".status-flash-bar"), False),
            ]
        )
        self._refresh_tag_interaction_state()

    def _tags_busy(self) -> bool:
        """Return whether a run, from loading through its queued input, holds the selectors."""
        return self._execution_busy or self.agent_running or self.agent_loading or self.input_locked

    def _tags_interactive(self) -> bool:
        """Return whether transient screen state permits selector interaction."""
        return not self._tags_busy() and not self.shell_mode

    def _refresh_tag_interaction_state(self) -> None:
        """Keep both selector pointers and locked styling in sync with guards."""
        if not self.is_mounted or not self.visible:
            return
        interactive = self._tags_interactive()
        profile_tag = self.query_one("#profile-tag", Static)
        profile_clickable = bool(self._current_profile) and profile_tag.display and interactive
        profile_tag.styles.pointer = "pointer" if profile_clickable else "default"

        model_tag = self.query_one("#model-tag", Static)
        model_clickable = (
            self._current_model_visible
            and bool(self._current_model)
            and self._current_model_mode != "locked"
            and interactive
        )
        model_tag.styles.pointer = "pointer" if model_clickable else "default"
        model_tag.set_class(self._current_model_mode == "locked", "-locked")

    def watch_agent_running(self, _running: bool) -> None:
        self._refresh_tag_interaction_state()

    def watch_agent_loading(self, _loading: bool) -> None:
        self._refresh_tag_interaction_state()

    def watch_input_locked(self, _locked: bool) -> None:
        self._refresh_tag_interaction_state()

    def _sync_selector_visibility(self) -> None:
        """Collapse absent selectors and hide configured ones during shell mode."""
        if not self.is_attached:
            return
        selectors = self.query_one(".status-selectors")
        display = not self.shell_mode and bool(self._current_profile or self._current_model_visible)
        if selectors.display != display:
            selectors.display = display

    def watch_shell_mode(self, _shell_mode: bool) -> None:
        self._sync_selector_visibility()
        self._refresh_tag_interaction_state()

    def on_resize(self, _event: Resize) -> None:
        """Re-truncate selector labels when the terminal crosses a width tier."""
        self._refresh_tags()

    def set_tool_info(self, trail: StatusTrail) -> None:
        """Set persistent tool/skill info shown across all status bar modes."""
        previous_tool_trail = self._tool_trail
        self._tool_trail = trail
        if self.is_mounted:
            if self._idle_shown:
                idle_shown = not self.shell_mode and bool(
                    self._current_profile or self._current_model_visible or self._tool_trail
                )
                set_widgets_visibility_without_layout(
                    [
                        (self, idle_shown),
                        (self.query_one(".status-run"), idle_shown),
                        (self.query_one("#status-tool-info"), idle_shown),
                    ]
                )
            if self._flash is None:
                self._refresh_trail()
            else:
                if not self._flash.trail or self._flash.trail == previous_tool_trail:
                    self._flash.trail = trail
                self._set_flash_trail(self._flash_display_trail())
                self._refresh_details_pointer()

    def start_run(self) -> None:
        """Start timing a new agent.run cycle."""
        self._start_time = time.monotonic()
        self._tool_count = 0
        self._output_samples.clear()
        self._refresh_running_trail()

    def note_output_text(self, text: str) -> None:
        """Count streamed model text toward the speed; only the trail's own refresh shows it, so a delta paints nothing."""
        now = time.monotonic()
        self._output_samples.append((now, _OUTPUT_TOKENIZER.count_token_units(text)))
        self._drop_old_output_samples(now)

    def _drop_old_output_samples(self, now: float) -> None:
        samples = self._output_samples
        while samples and samples[0][0] < now - _OUTPUT_SPEED_WINDOW_SECONDS:
            samples.popleft()

    def _output_speed(self) -> int | None:
        """Estimated tokens per second over the recent stream.

        ``None`` while no text is arriving, and for text that arrived all at
        once, as a response that was not streamed does: it has no rate to show.
        The span starts when the first sample landed, so that sample's text,
        written before then, does not count.
        """
        now = time.monotonic()
        self._drop_old_output_samples(now)
        samples = self._output_samples
        if not samples or now - samples[-1][0] > _OUTPUT_SPEED_IDLE_SECONDS:
            return None
        seconds = samples[-1][0] - samples[0][0]
        if seconds < _OUTPUT_SPEED_MIN_SPAN_SECONDS:
            return None
        units = sum(count for _at, count in samples) - samples[0][1]
        return max(1, round(units / TOKEN_UNITS_PER_TOKEN / seconds))

    def show(self, status: StatusMessage) -> None:
        """Show the status bar with given status text.

        While a run status is up, the status changes at most every
        ``_STATUS_MIN_INTERVAL_SECONDS``, and showing it again repaints nothing.
        """
        if self._content_shown and self._flash is None:
            if status == self.status:
                self._cancel_pending_status()
                return
            wait = self._status_shown_at + _STATUS_MIN_INTERVAL_SECONDS - time.monotonic()
            if wait > 0:
                self._pending_status = status
                if self._pending_status_timer is None:
                    self._pending_status_timer = self.set_timer(wait, self._show_pending_status)
                return
        self._show_now(status)

    def _show_pending_status(self) -> None:
        status = self._pending_status
        self._pending_status_timer = None
        if status is not None:
            self._show_now(status)

    def _cancel_pending_status(self) -> None:
        self._pending_status = None
        if self._pending_status_timer is not None:
            self._pending_status_timer.stop()
            self._pending_status_timer = None

    def _show_now(self, status: StatusMessage) -> None:
        self._cancel_pending_status()
        self._status_shown_at = time.monotonic()
        self._flash = None
        self.status = status
        self.update_classes({"-warn": False, "-error": False, "-caution": False})
        self._sync_visibility(True)
        self._refresh_trail()

    def add_tool_call(self) -> None:
        """Increment tool call counter."""
        self._tool_count += 1
        self._refresh_running_trail()

    def _refresh_running_trail(self) -> None:
        """Show a new count or start time now: the status shown again with it repaints nothing."""
        if self.is_mounted and self._content_shown and self._flash is None:
            self._refresh_trail()

    def flash(
        self,
        text: StatusMessage,
        *,
        warn: bool = False,
        error: bool = False,
        caution: bool = False,
        trail: StatusTrail = "",
    ) -> None:
        """Show a static message (no spinner). Stays until replaced by show() or hide().

        Args:
            text: Left-aligned message text.
            warn: Apply warning styling (yellow with background).
            error: Apply error styling (red).
            caution: Apply caution styling (yellow, no background).
            trail: Right-aligned trailing text. If empty, uses persistent tool info.
        """
        self._cancel_pending_status()
        elapsed_seconds = None
        if isinstance(text, MessageRef) and text.definition is STATUS_COMPLETED:
            elapsed_seconds = self._last_formatted_elapsed_seconds
        self._last_formatted_elapsed_seconds = None
        self._flash = _FlashState(
            text=text,
            trail=trail,
            warn=warn,
            error=error,
            caution=caution,
            elapsed_seconds=elapsed_seconds,
        )
        self.status = ""
        self.update_classes({"-warn": warn, "-error": error, "-caution": caution})
        self._sync_visibility(True)
        self._refresh_flash_text()
        self._set_flash_trail(self._flash_display_trail())
        self._refresh_details_pointer()

    def flash_completed(self) -> None:
        """Flash how long the run took; the elapsed time follows later locale switches."""
        self.flash(STATUS_COMPLETED.bind(elapsed=self._format_elapsed()))

    def hide(self) -> None:
        """Hide the status bar."""
        self._cancel_pending_status()
        self._flash = None
        self.status = ""
        self.update_classes({"-warn": False, "-error": False, "-caution": False})
        self._sync_visibility(False)

    def clear_status(self) -> None:
        """Clear status content while keeping selectors and runtime info visible."""
        self._cancel_pending_status()
        self._flash = None
        self.status = ""
        self.update_classes({"-warn": False, "-error": False, "-caution": False})
        self._content_shown = False
        self._idle_shown = True
        self._refresh_trail()
        idle_shown = not self.shell_mode and bool(
            self._current_profile or self._current_model_visible or self._tool_trail
        )
        set_widgets_visibility_without_layout(
            [
                (self, idle_shown),
                (self.query_one(".status-run"), idle_shown),
                (self.query_one(".status-flash-bar"), False),
                (self.query_one("ChrysLoadingIndicator"), False),
                (self.query_one("#status-text"), False),
                (self.query_one("#status-trail"), False),
                (self.query_one("#status-tool-info"), idle_shown),
            ]
        )
        if idle_shown:
            self._refresh_tag_interaction_state()

    @property
    def shown(self) -> bool:
        """Whether the reserved status row is currently painted."""
        return self.visible

    def _flash_display_trail(self) -> StatusTrail:
        """Trail text for the active flash; warn flashes get the row to themselves."""
        if self._flash is None or self._flash.warn:
            return ""
        return self._flash.trail or self._tool_trail

    def _set_flash_trail(self, trail: StatusTrail) -> None:
        """Render runtime details into the space left by the primary flash."""
        display_trail = self._render_trail(trail)
        trail_widget = self.query_one("#status-flash-trail", _TrailInfo)
        trail_widget.show_trail(display_trail, self._narrow_trail(trail))
        trail_widget.tooltip = self._build_tooltip_text(display_trail)

    def _sync_visibility(self, shown: bool) -> None:
        """Switch status modes without changing MainScreen geometry."""
        was_idle = self._idle_shown
        self._content_shown = shown
        self._idle_shown = False
        if not self.is_mounted:
            set_widget_visibility_without_layout(self, shown)
            return
        visibility: list[tuple[Widget, bool | None]] = [
            (self, shown),
            (self.query_one(".status-run"), shown and self._flash is None),
            (self.query_one(".status-flash-bar"), shown and self._flash is not None),
        ]
        if was_idle:
            visibility.extend(
                [
                    (self.query_one("ChrysLoadingIndicator"), None),
                    (self.query_one("#status-text"), None),
                    (self.query_one("#status-trail"), None),
                    (self.query_one("#status-tool-info"), None),
                ]
            )
        set_widgets_visibility_without_layout(visibility)
        if shown:
            self._refresh_tag_interaction_state()

    def snapshot(self) -> dict:
        """Capture current display state for later restore (e.g. during shell mode)."""
        return {
            "visible": self.visible,
            "content_shown": self._content_shown,
            "idle_shown": self._idle_shown,
            "flash": self._flash,
            "status": self.status if self._pending_status is None else self._pending_status,
            "start_time": self._start_time,
            "tool_count": self._tool_count,
            "tool_trail": self._tool_trail,
        }

    def restore(self, state: dict, *, tool_trail: StatusTrail | None = None) -> None:
        """Restore a previously snapshotted display state.

        *tool_trail* replaces the snapshot's trail, which moved on meanwhile; a
        flash that showed the old one shows it too.
        """
        self._cancel_pending_status()
        self._start_time = state.get("start_time", 0.0)
        self._tool_count = state.get("tool_count", 0)
        snapshot_trail = state.get("tool_trail", self._tool_trail)
        self._tool_trail = snapshot_trail if tool_trail is None else tool_trail
        flash: _FlashState | None = state.get("flash")
        if flash is not None and flash.trail == snapshot_trail:
            flash = replace(flash, trail=self._tool_trail)
        if not state.get("visible"):
            self.clear_status()
        elif not state.get("content_shown", True):
            self._flash = flash
            self.status = state.get("status", "")
            self.update_classes(
                {
                    "-warn": flash.warn if flash is not None else False,
                    "-error": flash.error if flash is not None else False,
                    "-caution": flash.caution if flash is not None else False,
                }
            )
            self._content_shown = False
            self._idle_shown = state.get("idle_shown", False)
            self._refresh_trail()
            visibility: list[tuple[Widget, bool | None]] = [
                (self, True),
                (self.query_one(".status-run"), self._idle_shown),
                (self.query_one(".status-flash-bar"), False),
            ]
            if self._idle_shown:
                visibility.extend(
                    [
                        (self.query_one("ChrysLoadingIndicator"), False),
                        (self.query_one("#status-text"), False),
                        (self.query_one("#status-trail"), False),
                        (self.query_one("#status-tool-info"), True),
                    ]
                )
            set_widgets_visibility_without_layout(visibility)
            self._refresh_tag_interaction_state()
        elif flash is not None:
            self.flash(flash.text, warn=flash.warn, error=flash.error, caution=flash.caution, trail=flash.trail)
            if self._flash is not None:
                self._flash.elapsed_seconds = flash.elapsed_seconds
                self._refresh_flash_text()
        else:
            self._show_now(state["status"])

    def _format_elapsed(self) -> str:
        elapsed = int(time.monotonic() - self._start_time)
        self._last_formatted_elapsed_seconds = elapsed
        return self._format_elapsed_seconds(elapsed)

    def _format_elapsed_seconds(self, elapsed: int) -> str:
        if elapsed < 60:
            return self._render_message(STATUS_ELAPSED_SECONDS.bind(seconds=elapsed))
        minutes, seconds = divmod(elapsed, 60)
        if minutes >= 60:
            hours, minutes = divmod(minutes, 60)
            if hours >= 24:
                days, hours = divmod(hours, 24)
                return self._render_message(
                    STATUS_ELAPSED_DAYS_HOURS_MINUTES_SECONDS.bind(
                        days=days, hours=hours, minutes=minutes, seconds=seconds
                    )
                )
            return self._render_message(
                STATUS_ELAPSED_HOURS_MINUTES_SECONDS.bind(hours=hours, minutes=minutes, seconds=seconds)
            )
        return self._render_message(STATUS_ELAPSED_MINUTES_SECONDS.bind(minutes=minutes, seconds=seconds))

    def refresh_localization(self) -> None:
        """Retranslate the bounded status-bar chrome from stored semantic state."""
        if not self.is_mounted:
            return
        self._refresh_tags()
        self._refresh_status_text()
        if self._flash is None:
            self._refresh_trail()
        else:
            self._refresh_flash_text()
            self._set_flash_trail(self._flash_display_trail())
            self._refresh_details_pointer()

    def _render_message(self, message: StatusMessage) -> str:
        if isinstance(message, str):
            return message
        controller = self._locale_controller
        if controller is None:
            return format_message(message)
        return render_str(controller.localizer, message)

    def _render_trail(self, trail: StatusTrail) -> str:
        if isinstance(trail, tuple):
            return _TRAIL_SEPARATOR.join(
                self._render_message(part.message if isinstance(part, TrailCount) else part) for part in trail
            )
        return self._render_message(trail)

    def _narrow_trail(self, trail: StatusTrail) -> tuple[str, str]:
        """*trail*'s parts other than its counts, and its counts' short forms, each joined."""
        if not isinstance(trail, tuple):
            return self._render_message(trail), ""
        others = [self._render_message(part) for part in trail if not isinstance(part, TrailCount)]
        counts = [self._render_message(part.short) for part in trail if isinstance(part, TrailCount)]
        return _TRAIL_SEPARATOR.join(others), _TRAIL_SEPARATOR.join(counts)

    def _refresh_status_text(self) -> None:
        rendered = self._render_message(self.status)
        self.query_one("#status-text", Static).update(Text(rendered), layout=False)

    def _refresh_flash_text(self) -> None:
        if self._flash is None:
            return
        if self._flash.elapsed_seconds is not None:
            self._flash.text = STATUS_COMPLETED.bind(elapsed=self._format_elapsed_seconds(self._flash.elapsed_seconds))
        rendered = self._render_message(self._flash.text)
        flash_widget = self.query_one("#status-flash", Static)
        flash_widget.update(Text(rendered), layout=False)
        width_changed = (
            flash_widget.styles.clear_rule("width")
            if self._flash.warn
            else flash_widget.styles.set_rule("width", Scalar.from_number(cell_len(rendered) + 2))
        )
        if width_changed:
            flash_bar = self.query_one(".status-flash-bar")
            flash_bar._clear_arrangement_cache()
            resync_compositor_regions(flash_bar)

    def _refresh_trail(self) -> None:
        """Update the run-mode trailing info (elapsed, tool count) and tool info."""
        stats: list[str] = []
        if self._start_time:
            stats.append(self._format_elapsed())
        if self._tool_count:
            stats.append(self._render_message(STATUS_TOOL_CALLS.bind(count=self._tool_count)))
        speed = self._output_speed()
        if speed is not None:
            stats.append(self._render_message(STATUS_OUTPUT_SPEED.bind(speed=speed)))
        text = f"  ({' \u00b7 '.join(stats)})" if stats else ""
        self.query_one("#status-trail", Static).update(Text(text), layout=False)
        # Tool info on the right
        tool_info = self.query_one("#status-tool-info", _TrailInfo)
        rendered_tool_trail = self._render_trail(self._tool_trail)
        tool_info.show_trail(rendered_tool_trail, self._narrow_trail(self._tool_trail))
        tool_info.tooltip = self._build_tooltip_text(rendered_tool_trail)
        self._pin_run_widths(trail_text=text)
        self._refresh_details_pointer()

    def _pin_run_widths(self, trail_text: str) -> None:
        """Content-size the run label and trail without layout escalation.

        Same pattern as ``_set_flash_trail``: ``width: auto`` would escalate
        to a real layout pass on every update, while a flexible width strands
        the trail mid-row, away from the label it belongs to. Measure the two
        dynamic texts, pin their width rules, and remap the run face with the
        cache-hot resync; the right-aligned tool info keeps the remainder.
        """
        rendered_status = self._render_message(self.status)
        changed = self.query_one("#status-text", Static).styles.set_rule(
            "width", Scalar.from_number(cell_len(rendered_status))
        )
        changed = (
            self.query_one("#status-trail", Static).styles.set_rule("width", Scalar.from_number(cell_len(trail_text)))
            or changed
        )
        if changed:
            run_bar = self.query_one(".status-run")
            run_bar._clear_arrangement_cache()
            resync_compositor_regions(run_bar)

    def _refresh_details_pointer(self) -> None:
        """Use the hand pointer only when runtime details can be opened."""
        pointer = "pointer" if self._tool_trail else "default"
        for selector in ("#status-tool-info", "#status-flash-trail"):
            widget = self.query_one(selector, Static)
            widget.styles.pointer = pointer
            widget.tooltip = self._build_tooltip_text(str(widget.content))

    def _build_tooltip_text(self, display_text: str) -> Text | None:
        """Return the runtime-details click affordance for non-empty info."""
        if not display_text:
            return None
        return Text(self._render_message(STATUS_DETAILS_TOOLTIP.bind()))

    def on_click(self, event: Click) -> None:
        """Handle profile/model selectors and the right-side runtime details."""
        profile_tag = self.query_one("#profile-tag", Static)
        if profile_tag.display and profile_tag.region.contains(event.screen_x, event.screen_y):
            event.prevent_default()
            event.stop()
            if self._tags_interactive():
                self.post_message(self.ProfileTagClicked())
            elif self._tags_busy():
                self.post_message(self.SelectorBusy("profile"))
            return

        model_tag = self.query_one("#model-tag", Static)
        if model_tag.display and model_tag.region.contains(event.screen_x, event.screen_y):
            event.prevent_default()
            event.stop()
            # A lock outlasts the run, so its reason wins over the busy notice.
            if self._current_model_mode == "locked" and not self.shell_mode:
                self.post_message(self.ModelTagClicked("locked"))
            elif self._tags_interactive():
                self.post_message(self.ModelTagClicked(self._current_model_mode))
            elif self._tags_busy():
                self.post_message(self.SelectorBusy("model"))
            return

        if not self._tool_trail:
            return
        widgets = [
            self.query_one("#status-tool-info", Static),
            self.query_one("#status-flash-trail", Static),
        ]
        if any(widget.region.contains(event.screen_x, event.screen_y) for widget in widgets):
            event.prevent_default()
            event.stop()
            self.post_message(self.DetailsClicked())

    def set_execution_busy(self, busy: bool) -> None:
        """Keep selectors locked until the current execution has fully drained."""
        if busy == self._execution_busy:
            return
        self._execution_busy = busy
        self._refresh_tag_interaction_state()
