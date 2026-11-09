# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HookRunList — the sidebar's Hooks tab: every hook run since iCode started, across sessions."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, ClassVar

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.geometry import Region
from textual.message import Message
from textual.widgets import Static

from chrys.app.tui.i18n import render_str
from chrys.app.tui.util.visibility import is_widget_shown_on_active_screen, set_widget_visibility_without_layout
from chrys.app.tui.widgets.chat.tool_call import fmt_duration
from chrys.app.tui.widgets.selection import NonSelectableStatic
from chrys.app.tui.widgets.sidebar.empty_state import SidebarEmptyStateLabel
from chrys.foundation.branding import APP_DISPLAY_NAME
from chrys.foundation.i18n import MessageRef, msg
from chrys.foundation.i18n.formatting import format_message, sanitize_legacy_scalar
from chrys.foundation.models.hook_runs import HookRunRecord
from chrys.service.hooks.run_log import HOOK_RUN_LOG_MAX_ENDED

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Click, Leave, MouseEvent, MouseMove
    from textual.timer import Timer

    from chrys.app.tui.i18n import LocaleController

_TITLE = msg("tui.sidebar.hooks.title", fallback="Hooks")
_TITLE_ACTIVE = msg(
    "tui.sidebar.hooks.title_active",
    fallback="Hooks ({count} active)",
    plural_fallback="Hooks ({count} active)",
)
_CLEAR_ENDED = msg("tui.sidebar.hooks.clear_ended", fallback="Clear ended")
_EMPTY_UNCONFIGURED = msg("tui.sidebar.hooks.empty_unconfigured", fallback="No hooks configured")
_EMPTY = msg("tui.sidebar.hooks.empty", fallback="No hook has run since {app} started")
_ONLY_LATEST_KEPT = msg("tui.sidebar.hooks.only_latest_kept", fallback="Only the latest {limit} runs are kept")
_CLEARED = msg("tui.sidebar.hooks.cleared", fallback="Ended runs cleared")
_RUNNING = msg("tui.sidebar.hooks.outcome.running", fallback="running")
_QUEUED = msg("tui.sidebar.hooks.outcome.queued", fallback="queued")
_BLOCKED = msg("tui.sidebar.hooks.outcome.blocked", fallback="blocked")
_MODIFIED_ARGS = msg("tui.sidebar.hooks.outcome.modified_args", fallback="modified args")
_ADDED_REMINDER = msg("tui.sidebar.hooks.outcome.added_reminder", fallback="added reminder")
_ADDED_CONTEXT = msg("tui.sidebar.hooks.outcome.added_context", fallback="added context")
_EXIT = msg("tui.sidebar.hooks.outcome.exit", fallback="exit {code}")
_TIMED_OUT = msg("tui.sidebar.hooks.outcome.timed_out", fallback="timed out")
_COULDNT_START = msg("tui.sidebar.hooks.outcome.couldnt_start", fallback="couldn't start")
_CANCELLED = msg("tui.sidebar.hooks.outcome.cancelled", fallback="cancelled")
_DETACHED = msg("tui.sidebar.hooks.outcome.detached", fallback="detached")
_CALL_REJECTED = msg("tui.sidebar.hooks.outcome.call_rejected", fallback="call rejected")
_ASKED_PENDING = msg("tui.sidebar.hooks.outcome.asked.pending", fallback="waiting for user")
_ASKED_APPROVED = msg("tui.sidebar.hooks.outcome.asked.approved", fallback="user continued")
_ASKED_DECLINED = msg("tui.sidebar.hooks.outcome.asked.declined", fallback="user cancelled")
_ASKED_TIMED_OUT = msg("tui.sidebar.hooks.outcome.asked.timed_out", fallback="no answer in time")
_ASKED_UNAVAILABLE = msg("tui.sidebar.hooks.outcome.asked.unavailable", fallback="couldn't ask user")
_ASKED_INVALIDATED = msg("tui.sidebar.hooks.outcome.asked.invalidated", fallback="dropped")
_ASKED_INTERRUPTED = msg("tui.sidebar.hooks.outcome.asked.interrupted", fallback="interrupted")
_ASKED = {
    "pending": _ASKED_PENDING,
    "approved": _ASKED_APPROVED,
    "declined": _ASKED_DECLINED,
    "timed_out": _ASKED_TIMED_OUT,
    "unavailable": _ASKED_UNAVAILABLE,
    "invalidated": _ASKED_INVALIDATED,
    "interrupted": _ASKED_INTERRUPTED,
}
# What an asked run's state is, by what came of its question.
_ASKED_STATES = {
    "pending": "asking",
    "approved": "success",
    "declined": "blocked",
    "timed_out": "blocked",
    "unavailable": "blocked",
    "invalidated": "cancelled",
    "interrupted": "cancelled",
}

_EFFECTS = {
    "blocked": _BLOCKED,
    "modified_args": _MODIFIED_ARGS,
    "added_reminder": _ADDED_REMINDER,
    "added_context": _ADDED_CONTEXT,
}

_ICONS = {
    # As a running background task: a still dot in the running color.
    "running": "●",
    "queued": "◌",
    "asking": "?",
    "success": "✓",
    "blocked": "⊘",
    "failure": "✗",
    "cancelled": "■",
    "detached": "→",
}
_ICON_STYLES = {
    "queued": "dim",
    "asking": "yellow",
    "success": "green",
    "blocked": "yellow",
    "failure": "red",
    "cancelled": "dim",
    "detached": "dim",
}


def run_state(record: HookRunRecord) -> str:
    """One of "running", "queued", "asking", "success", "blocked", "failure", "cancelled" or "detached".

    "asking": it asked the user, who has not answered yet; an asked run then takes the state of the
    answer. "detached": handed to its own process.
    """
    if record.state != "ended":
        return record.state
    outcome = record.outcome
    if outcome == "cancelled":
        return "cancelled"
    if outcome == "detached":
        return "detached"
    if outcome == "success":
        if record.effect == "asked":
            return _ASKED_STATES[record.confirmation or "pending"]
        return "blocked" if record.effect == "blocked" or record.call_rejected else "success"
    return "failure"


def run_icon(record: HookRunRecord, *, running_style: Style) -> Text:
    """The state glyph of *record*, colored; a running run's in *running_style*."""
    state = run_state(record)
    return Text(_ICONS[state], style=running_style if state == "running" else _ICON_STYLES[state])


def run_outcome(record: HookRunRecord) -> tuple[MessageRef, ...]:
    """How the run ended, then what iCode took from it; empty for a success that changed nothing.

    A failure that ``on_error: block`` turned into a block says both.
    """
    if record.state == "running":
        return (_RUNNING.bind(),)
    if record.state == "queued":
        return (_QUEUED.bind(),)
    outcome = record.outcome
    if outcome == "success":
        if record.effect == "asked":
            return (_ASKED[record.confirmation or "pending"].bind(),)
        if record.call_rejected:
            # Its change came after the user continued once too often: what matters is that the
            # call was rejected; the change itself is in the run's details.
            return (_CALL_REJECTED.bind(),)
        effect = _EFFECTS.get(record.effect or "")
        return (effect.bind(),) if effect is not None else ()
    if outcome == "cancelled":
        return (_CANCELLED.bind(),)
    if outcome == "detached":
        return (_DETACHED.bind(),)
    if outcome == "failed":
        failure = _EXIT.bind(code=str(record.exit_code))
    elif outcome == "timed_out":
        failure = _TIMED_OUT.bind()
    else:
        failure = _COULDNT_START.bind()
    return (failure, _BLOCKED.bind()) if record.effect == "blocked" else (failure,)


def run_duration(record: HookRunRecord, now: float) -> str:
    """How long it queued (still queued) or ran; empty when it never took a slot or was detached."""
    if record.state == "queued":
        return fmt_duration(max(0, int((now - record.registered_at) * 1000)))
    # Handed off, it runs on its own: iCode never sees how long.
    if record.started_at is None or record.outcome == "detached":
        return ""
    end = record.ended_at if record.ended_at is not None else now
    return fmt_duration(max(0, int((end - record.started_at) * 1000)))


def run_label(record: HookRunRecord) -> str:
    """The hook's id, safe to show.

    Only the id: a hook is configured for one event, so its event says nothing new in
    a narrow row, and the tool and agent that fired it are in the run's details.
    """
    return sanitize_legacy_scalar(" ".join(record.hook_id.split()))


class HookRunList(Vertical, can_focus=False):
    """The hook runs, oldest first and numbered, one row each; a click on a row opens its details.

    The runs are read from the engine's run log through *reader* only while
    the list is shown: a change while it is hidden only marks it stale. Durations
    tick once a second, only while a run is in flight and the list is composited.
    Same-sized repaints skip layout; a row added or removed costs one.
    """

    class Opened(Message):
        """The user clicked the row of a hook run."""

        def __init__(self, run_id: str) -> None:
            super().__init__()
            self.run_id = run_id

    COMPONENT_CLASSES: ClassVar[set[str]] = {"hook-runs--running", "hook-runs--hover", "hook-runs--selected"}

    DEFAULT_CSS = """
    HookRunList {
        height: 100%;
        width: 100%;
        /* Padding lies outside the scrollbar, so the right side has none: the one-cell bar sits
           on the panel's edge, and its always-reserved column mirrors the left padding. */
        padding: 0 0 0 1;
    }
    HookRunList > .hook-runs--running {
        color: $warning;
    }
    HookRunList > .hook-runs--hover {
        background: $primary 30%;
    }
    HookRunList > .hook-runs--selected {
        background: $primary 15%;
    }
    HookRunList > Horizontal {
        height: 1;
        padding-right: 1;
    }
    HookRunList #hook-runs-title {
        width: 1fr;
        text-style: bold;
        color: $text-muted;
    }
    HookRunList #hook-runs-clear {
        width: auto;
        color: $text-muted;
        text-style: underline;
        pointer: pointer;
    }
    /* No surrounding border: the rows take the whole width. */
    HookRunList > #hook-runs-box {
        height: 1fr;
        scrollbar-size: 1 1;
        scrollbar-gutter: stable;
        overflow-x: hidden;
        display: none;
    }
    HookRunList #hook-runs-unlisted {
        color: $text-muted;
        display: none;
    }
    HookRunList #hook-runs-rows {
        width: 100%;
        height: auto;
        pointer: pointer;
    }
    HookRunList > #hook-runs-empty {
        height: 1fr;
        padding-right: 1;
        color: $text-muted;
        content-align: center middle;
    }
    """

    def __init__(self, *, locale_controller: LocaleController | None = None) -> None:
        super().__init__()
        self._locale_controller = locale_controller
        self._reader: Callable[[], tuple[HookRunRecord, ...]] | None = None
        self._configured: Callable[[], int] | None = None
        self._records: tuple[HookRunRecord, ...] = ()
        """What the rows show: the log as last read, without the cleared runs."""
        self._cleared: set[str] = set()
        """Ended runs the user cleared from the list: nothing shows them again."""
        self._dropped: Callable[[], int] | None = None
        self._kept_all = True
        """No ended run was dropped past the log's cap."""
        self._logged = 0
        """Runs the log keeps, the cleared ones included."""
        self._stale = True
        self._read_pending = False
        self._composited = False
        self._timer: Timer | None = None
        self._painted_rows = -1
        self._hovered: int | None = None
        """The row under the mouse."""
        self._selected = ""
        """The run whose details were opened last: its row stays marked."""
        self._reveal = False
        """Scroll the selected run's row into view at the next paint that lists it."""
        self._empty_text = ""
        """What the empty state says, as last painted."""
        self._row_styles: tuple[Style, Style, Style] | None = None
        """The running, hover and selected styles the rows were last painted in."""

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(id="hook-runs-title")
            clear = Static(id="hook-runs-clear")
            # Hidden, not undisplayed: it comes and goes as runs end, and must not lay out the screen.
            clear.styles.visibility = "hidden"
            yield clear
        # Not focusable: a click on a row must leave the keyboard where it was.
        with VerticalScroll(id="hook-runs-box", can_focus=False):
            # Above the oldest row: the list starts later than #1, or has gaps.
            yield NonSelectableStatic(id="hook-runs-unlisted")
            yield Static(id="hook-runs-rows")
        yield SidebarEmptyStateLabel(id="hook-runs-empty")

    def on_mount(self) -> None:
        self._timer = self.set_interval(1, self._tick, pause=True)
        # Scrolled, another row is under the mouse: the next move finds it.
        self.watch(self.query_one("#hook-runs-box", VerticalScroll), "scroll_y", self.unhover, init=False)
        # The row colors are resolved into the painted table, so a theme change repaints it.
        self.app.theme_changed_signal.subscribe(self, self._theme_changed)
        self._localize()
        self._paint()

    def on_unmount(self) -> None:
        self.app.theme_changed_signal.unsubscribe(self)

    def _theme_changed(self, _theme: object) -> None:
        # Hidden or covered, it is painted in the new colors when it shows or its screen
        # resumes; a repaint before then is work nobody sees.
        if not self._composited or not is_widget_shown_on_active_screen(self):
            return
        if self._row_styles is not None and self._resolve_row_styles() != self._row_styles:
            self.query_one("#hook-runs-rows", Static).update(self._rows(self._records), layout=False)

    def on_show(self) -> None:
        self._composited = True
        if self._stale:
            # Shown by the user (the tab, or the indicator opening a run's
            # details over it): read now, even behind that dialog.
            self._read(covered_too=True)
        else:
            self._paint()

    def on_hide(self) -> None:
        self._composited = False
        self._sync_timer()

    def on_resize(self) -> None:
        # The rows moved under the mouse: its next move finds the row there.
        self._hover(None)

    def bind_source(
        self,
        reader: Callable[[], tuple[HookRunRecord, ...]],
        configured: Callable[[], int],
        dropped: Callable[[], int],
    ) -> None:
        """Read the runs from *reader*, the number of enabled hooks from *configured*,
        and how many ended runs the log dropped past its cap from *dropped*."""
        self._reader = reader
        self._configured = configured
        self._dropped = dropped
        self.runs_changed()

    def runs_changed(self) -> None:
        """The run log changed: read it once, soon, if the list shows; else when it shows next.

        Covered by an overlay, it waits for the screen's resume (``screen_resumed``).
        """
        self._stale = True
        if not self.is_mounted or not self._composited or self._read_pending:
            return
        if not is_widget_shown_on_active_screen(self):
            return
        # Coalesced: every change in one pump of messages costs one read and one paint.
        self._read_pending = self.call_later(self._read)

    def _read(self, *, covered_too: bool = False) -> None:
        self._read_pending = False
        if not self.is_mounted:
            return
        # Covered since a change queued this read: still stale, read at the screen's resume.
        if not covered_too and not is_widget_shown_on_active_screen(self):
            return
        self._stale = False
        records = self._reader() if self._reader is not None else ()
        self._kept_all = (self._dropped() if self._dropped is not None else 0) == 0
        self._logged = len(records)
        self._cleared.intersection_update(record.run_id for record in records)
        self._records = tuple(record for record in records if record.run_id not in self._cleared)
        self._paint()

    def refresh_localization(self) -> None:
        """Retranslate the list."""
        if self.is_mounted:
            self._localize()
            self._paint()

    def _localize(self) -> None:
        # Sized by its text, so it lays out again: only here, never on a tick.
        self.query_one("#hook-runs-clear", Static).update(Text(self._text(_CLEAR_ENDED.bind())))
        self.query_one("#hook-runs-unlisted", Static).update(
            Text(self._text(_ONLY_LATEST_KEPT.bind(limit=HOOK_RUN_LOG_MAX_ENDED)))
        )

    def screen_resumed(self) -> None:
        """Catch up once an overlay over the screen closes.

        The overlay parked the timer, and closing it resumes the screen without
        showing this widget again.
        """
        if not self.is_mounted or not self._composited:
            return
        if self._stale:
            self._read()
        else:
            self._paint()

    def _tick(self) -> None:
        # An overlay opened since the timer started: nothing sends this widget
        # a Hide then, so the first tick stops the timer until the screen resumes.
        if not is_widget_shown_on_active_screen(self):
            if self._timer is not None:
                self._timer.pause()
            return
        self._paint()

    def _sync_timer(self) -> None:
        if self._timer is None:
            return
        live = any(record.state != "ended" for record in self._records)
        if self._composited and live and is_widget_shown_on_active_screen(self):
            self._timer.resume()
        else:
            self._timer.pause()

    def _paint(self) -> None:
        records = self._records
        active = sum(record.state != "ended" for record in records)
        title = _TITLE_ACTIVE.bind(count=active) if active else _TITLE.bind()
        self.query_one("#hook-runs-title", Static).update(Text(self._text(title)), layout=False)
        set_widget_visibility_without_layout(self.query_one("#hook-runs-clear", Static), active < len(records))
        rows = self.query_one("#hook-runs-rows", Static)
        box = self.query_one("#hook-runs-box", VerticalScroll)
        grown = len(records) > self._painted_rows
        reveal = self._reveal and any(record.run_id == self._selected for record in records)
        # Only runs dropped past the log's cap: clearing is the user's own doing.
        unlisted = not self._kept_all
        notice = self.query_one("#hook-runs-unlisted", Static)
        # Newest at the bottom: a list read to its end follows the runs that join it, and the
        # notice that shows above them, as the chat does; one scrolled up to an older run stays there.
        follow = (
            (grown or (unlisted and not notice.display))
            and not reveal
            and (self._painted_rows <= 0 or box.scroll_y >= box.max_scroll_y)
        )
        rows.update(self._rows(records), layout=len(records) != self._painted_rows)
        self._painted_rows = len(records)
        if unlisted != notice.display:
            notice.display = unlisted
        if follow:
            box.scroll_end(animate=False)
        empty = self.query_one("#hook-runs-empty", Static)
        if not records:
            configured = self._configured() if self._configured is not None else 0
            if self._logged:
                reference = _CLEARED.bind()
            elif configured:
                reference = _EMPTY.bind(app=APP_DISPLAY_NAME)
            else:
                reference = _EMPTY_UNCONFIGURED.bind()
            text = self._text(reference)
            # Painted again on every resume of the screen: an update that changes nothing would lay it out.
            if text != self._empty_text:
                self._empty_text = text
                empty.update(Text(text))
        if box.display != bool(records):
            box.display = bool(records)
            empty.display = not records
        if reveal:
            self._reveal = False
            # Once laid out: the rows may have just grown, or the tab just shown.
            self.call_after_refresh(self._scroll_to_selected)
        self._sync_timer()

    def select(self, run_id: str) -> None:
        """Mark the row of *run_id*, whose details are open, and scroll it into view."""
        self._selected = run_id
        self._reveal = True
        if self.is_mounted and self._composited:
            # Read again first: the run may be newer than the rows. Read now
            # even under the details dialog this opens with: the user asked
            # for this row, and the list shows dimmed behind the dialog.
            self._read(covered_too=True)

    def _scroll_to_selected(self) -> None:
        if not self.is_mounted:
            return
        row = next((row for row, record in enumerate(self._records) if record.run_id == self._selected), None)
        if row is None:
            return
        box = self.query_one("#hook-runs-box", VerticalScroll)
        # Below the notice, however many lines it wraps to.
        top = self.query_one("#hook-runs-rows", Static).virtual_region.y
        box.scroll_to_region(Region(0, top + row, box.size.width, 1), center=True, animate=False, immediate=True)

    def _resolve_row_styles(self) -> tuple[Style, Style, Style]:
        """The running, hover and selected styles in the current theme."""
        resolved = self.get_component_rich_style("hook-runs--running", default=Style(color="yellow"))
        hover = self.get_component_rich_style("hook-runs--hover")
        selected = self.get_component_rich_style("hook-runs--selected")
        return Style(color=resolved.color), Style(bgcolor=hover.bgcolor), Style(bgcolor=selected.bgcolor)

    def _rows(self, records: tuple[HookRunRecord, ...]) -> Table:
        self._row_styles = running_style, hover, selected = self._resolve_row_styles()
        now = time.monotonic()
        grid = Table.grid(padding=(0, 1), expand=True)
        # Numbered as the Chat tab numbers turns: the run's place since iCode started.
        grid.add_column(justify="right", no_wrap=True, style="dim")
        grid.add_column(width=1, no_wrap=True)
        # No result column: the icon says how it went, and the run's details say the rest.
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        for row, record in enumerate(records):
            icon = run_icon(record, running_style=running_style)
            detail = running_style if record.state == "running" else "dim"
            grid.add_row(
                str(record.number),
                icon,
                Text(run_label(record), no_wrap=True, overflow="ellipsis"),
                Text(run_duration(record, now), style=detail),
                style=hover if row == self._hovered else selected if record.run_id == self._selected else None,
            )
        return grid

    def _text(self, reference: MessageRef) -> str:
        controller = self._locale_controller
        return format_message(reference) if controller is None else render_str(controller.localizer, reference)

    def on_click(self, event: Click) -> None:
        clear = self.query_one("#hook-runs-clear", Static)
        if clear.visible and event.get_content_offset(clear) is not None:
            event.stop()
            self._cleared.update(record.run_id for record in self._records if record.state == "ended")
            self._records = tuple(record for record in self._records if record.run_id not in self._cleared)
            self._paint()
            return
        row = self._row_at(event)
        if row is not None:
            event.stop()
            run_id = self._records[row].run_id
            self._selected = run_id
            self._paint()
            self.post_message(self.Opened(run_id))

    def on_mouse_move(self, event: MouseMove) -> None:
        self._hover(self._row_at(event))

    def _row_at(self, event: MouseEvent) -> int | None:
        rows = self.query_one("#hook-runs-rows", Static)
        # Scrolled, the rows reach up under the title: only the mouse on them counts.
        offset = event.get_content_offset(rows) if event.widget is rows else None
        return offset.y if offset is not None and 0 <= offset.y < len(self._records) else None

    def on_leave(self, event: Leave) -> None:
        if event.node is self.query_one("#hook-runs-rows", Static):
            self._hover(None)

    def unhover(self) -> None:
        """Drop the highlight, as the rows moved under the mouse: its next move finds the row there."""
        self._hover(None)

    def _hover(self, row: int | None) -> None:
        if row != self._hovered:
            self._hovered = row
            self.query_one("#hook-runs-rows", Static).update(self._rows(self._records), layout=False)
