# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the log viewer handler's snapshot / trim / filter semantics."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import pytest
from textual import events
from textual.app import App, ComposeResult
from textual.widgets import RichLog, Static, TabbedContent

from chrys.app.tui.i18n import LocaleController
from chrys.app.tui.screens.logs.screen import (
    MAX_BUFFER_LINE_CHARS,
    MAX_DISPLAY_LINE_CHARS,
    MAX_DISPLAY_LINES,
    MODULE_ORDER,
    LogViewerScreen,
    _truncate_display_line,
    _TuiLogHandler,
    install_log_handler,
)
from chrys.app.tui.widgets.hatch import HatchedEmptyState
from chrys.foundation.config.settings import Settings
from tests.support.tui_helpers import make_wheel, resize_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from textual.pilot import Pilot


def _record(name: str, msg: str, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=0,
        msg=msg,
        args=None,
        exc_info=None,
    )


class _LogViewerApp(App):
    locale_controller = LocaleController(Settings(locale="en"))

    def compose(self) -> ComposeResult:
        yield Static("placeholder")


def test_truncate_display_line_leaves_bounded_lines_unchanged() -> None:
    line = "x" * MAX_DISPLAY_LINE_CHARS

    assert _truncate_display_line(line) == line


def test_truncate_display_line_bounds_long_lines_with_suffix() -> None:
    line = "x" * (MAX_DISPLAY_LINE_CHARS + 1)
    truncated = _truncate_display_line(line)

    assert len(truncated) == MAX_DISPLAY_LINE_CHARS
    assert truncated.endswith("...")


def test_install_log_handler_respects_explicitly_pinned_logger_levels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """App-wiring level pins must survive the install-time DEBUG escalation of whitelisted loggers."""
    from chrys.app.tui.screens.logs import screen as screen_mod

    root_logger = logging.getLogger()
    saved_levels = {name: logging.getLogger(name).level for name in screen_mod.MODULE_WHITELIST}
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.NOTSET)
    monkeypatch.setattr(screen_mod, "_handler", None)

    handler = screen_mod.install_log_handler()
    try:
        assert logging.getLogger("openai").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.DEBUG
    finally:
        root_logger.removeHandler(handler)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


def test_install_log_handler_reattaches_existing_singleton() -> None:
    root_logger = logging.getLogger()
    handler = install_log_handler()
    was_attached = handler in root_logger.handlers

    root_logger.removeHandler(handler)
    try:
        assert handler not in root_logger.handlers
        assert install_log_handler() is handler
        assert handler in root_logger.handlers
    finally:
        if handler in root_logger.handlers:
            root_logger.removeHandler(handler)
        if was_attached:
            root_logger.addHandler(handler)


class TestTuiLogHandler:
    def test_snapshot_returns_new_lines_incrementally(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))

        for i in range(3):
            h.emit(_record("chrys", f"m{i}"))

        lines, total = h.snapshot("chrys", 0)
        assert lines == ["m0", "m1", "m2"]
        assert total == 3

        # Emitting more only yields the new ones on the next snapshot
        h.emit(_record("chrys", "m3"))
        lines, total = h.snapshot("chrys", total)
        assert lines == ["m3"]
        assert total == 4

    def test_emit_keeps_long_records_but_cuts_request_body_sized_ones(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        traceback_sized = "t" * MAX_BUFFER_LINE_CHARS
        request_body = "Request options: " + "x" * (MAX_BUFFER_LINE_CHARS * 10) + " ValueError: last"
        half = MAX_BUFFER_LINE_CHARS // 2

        h.emit(_record("chrys", traceback_sized))
        h.emit(_record("anthropic", request_body))

        assert h.snapshot("chrys", 0)[0] == [traceback_sized]
        [kept] = h.snapshot("anthropic", 0)[0]
        cut = len(request_body) - MAX_BUFFER_LINE_CHARS
        assert kept == (
            f"{request_body[:half]} ... [{cut} of {len(request_body)} chars cut] ... {request_body[-half:]}"
        )
        assert kept.startswith("Request options: ")
        assert kept.endswith("ValueError: last")

    def test_snapshot_survives_buffer_trim(self) -> None:
        """After buffer trim, subsequent emissions must still be delivered."""
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))

        # Fill past the trim threshold (2 * MAX_DISPLAY_LINES).
        # Before the fix, the flushed index became absolute and the
        # buffer-relative slice silently returned [] for new records.
        for i in range(MAX_DISPLAY_LINES * 2 + 10):
            h.emit(_record("chrys", f"m{i}"))

        # Drain everything observed so far.
        _, total = h.snapshot("chrys", 0)
        assert total == MAX_DISPLAY_LINES * 2 + 10

        # New record after trim MUST be observable.
        h.emit(_record("chrys", "fresh"))
        lines, new_total = h.snapshot("chrys", total)
        assert lines == ["fresh"]
        assert new_total == total + 1

    def test_snapshot_unknown_module_returns_empty(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        lines, total = h.snapshot("does-not-exist", 0)
        assert lines == []
        assert total == 0

    def test_modules_with_logs_groups_by_top_level(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.emit(_record("chrys.orchestration.engine.engine", "a"))
        h.emit(_record("chrys.app.tui.app", "b"))
        h.emit(_record("httpx._client", "c"))
        assert h.modules_with_logs() == ["chrys", "httpx"]

    def test_non_whitelisted_modules_are_dropped(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.emit(_record("markdown_it.parser", "noise"))
        h.emit(_record("some.third.party", "noise"))
        h.emit(_record("chrys.core", "real"))
        assert h.modules_with_logs() == ["chrys"]
        lines, total = h.snapshot("markdown_it", 0)
        assert lines == []
        assert total == 0

    def test_snapshot_filters_by_min_level(self) -> None:
        """min_level drops lower-severity lines but keeps the monotonic total."""
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.emit(_record("chrys", "dbg", logging.DEBUG))
        h.emit(_record("chrys", "info", logging.INFO))
        h.emit(_record("chrys", "warn", logging.WARNING))

        debug_lines, total = h.snapshot("chrys", 0, min_level=logging.DEBUG)
        assert debug_lines == ["dbg", "info", "warn"]
        assert total == 3

        info_lines, total = h.snapshot("chrys", 0, min_level=logging.INFO)
        assert info_lines == ["info", "warn"]
        assert total == 3

        warn_lines, total = h.snapshot("chrys", 0, min_level=logging.WARNING)
        assert warn_lines == ["warn"]
        assert total == 3

    def test_snapshot_limit_returns_tail_without_changing_total(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))

        for i in range(5):
            h.emit(_record("chrys", f"m{i}"))

        lines, total = h.snapshot("chrys", 0, limit=2)
        assert lines == ["m3", "m4"]
        assert total == 5

    def test_snapshot_limit_is_applied_after_level_filter(self) -> None:
        h = _TuiLogHandler()
        h.setFormatter(logging.Formatter("%(message)s"))
        h.emit(_record("chrys", "warn-0", logging.WARNING))
        h.emit(_record("chrys", "debug-0", logging.DEBUG))
        h.emit(_record("chrys", "warn-1", logging.WARNING))
        h.emit(_record("chrys", "debug-1", logging.DEBUG))

        lines, total = h.snapshot("chrys", 0, min_level=logging.WARNING, limit=2)
        assert lines == ["warn-0", "warn-1"]
        assert total == 4


@pytest.mark.asyncio
async def test_every_log_tab_scrollbar_is_one_cell_and_flush_right() -> None:
    handler = _TuiLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    for module in MODULE_ORDER:
        for index in range(100):
            handler.emit(_record(module, f"{module}-{index}"))

    screen = LogViewerScreen()
    screen._handler = handler

    async with _LogViewerApp().run_test(size=(120, 20)) as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()

        tabs = screen.query_one("#lv-tabs", TabbedContent)
        container = screen.query_one("#lv-container")
        for module in MODULE_ORDER:
            tabs.active = f"lv-tab-{module}"
            await pilot.pause()

            log_widget = screen.query_one(f"#lv-log-{module}", RichLog)
            assert log_widget.show_vertical_scrollbar is True
            assert log_widget.styles.scrollbar_size_vertical == 1
            assert log_widget.vertical_scrollbar.region.right == container.content_region.right


# A scroll so slow that what its first frames show holds for the whole test: Textual's
# animator runs on wall-clock time, so a faster scroll can pass a state the test waits
# for while a loaded runner stalls.
_CREEPING_SCROLL_SECONDS = 40 * ENGINE_TURN_TIMEOUT


def _at_bottom(log_widget: RichLog) -> bool:
    return log_widget.max_scroll_y > 0 and log_widget.scroll_y == log_widget.max_scroll_y


class _OpenLog:
    """The log viewer opened on its ``chrys`` log, which new lines can land in."""

    def __init__(self, pilot: Pilot[None], handler: _TuiLogHandler, screen: LogViewerScreen) -> None:
        self.pilot = pilot
        self.widget = screen.query_one("#lv-log-chrys", RichLog)
        self._handler = handler
        self._screen = screen
        self._landed = 0

    def animating(self) -> bool:
        return self.pilot.app.animator.is_being_animated(self.widget, "scroll_y")

    def land(self, count: int = 20) -> None:
        """Log *count* new lines and show them, as the viewer's refresh does."""
        for _ in range(count):
            self._handler.emit(_record("chrys", f"new-{self._landed}"))
            self._landed += 1
        self._screen._refresh_logs()

    async def assert_followed(self) -> None:
        """Land new lines: the log goes to its new bottom."""
        bottom = self.widget.max_scroll_y
        self.land()
        await wait_for(
            lambda: self.widget.max_scroll_y > bottom and _at_bottom(self.widget),
            pilot=self.pilot,
            description="new lines are followed",
        )

    async def assert_not_followed(self) -> None:
        """Land new lines: the log stays on the row it shows."""
        reading, bottom = self.widget.scroll_y, self.widget.max_scroll_y
        self.land()
        assert self.widget.max_scroll_y > bottom
        assert not await wait_until(lambda: self.widget.scroll_y != reading, pilot=self.pilot, timeout=0.5)


@asynccontextmanager
async def _open_log(*, lines: int = 100, size: tuple[int, int] = (120, 20)) -> AsyncIterator[_OpenLog]:
    """Open the log viewer on a ``chrys`` log of *lines* lines, at its newest."""
    handler = _TuiLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    for index in range(lines):
        handler.emit(_record("chrys", f"line-{index}"))
    screen = LogViewerScreen()
    screen._handler = handler
    async with _LogViewerApp().run_test(size=size) as pilot:
        await pilot.app.push_screen(screen)
        log = _OpenLog(pilot, handler, screen)
        await wait_for(lambda: _at_bottom(log.widget), pilot=pilot, description="the log opens at its newest line")
        yield log


def _drag_selection(log: _OpenLog, *, row: int, delta_y: int) -> None:
    """Move the mouse that holds a selection to *row* of the log, as the terminal reports a drag."""
    region = log.widget.content_region
    x, y = region.x + 3, region.y + row
    log.pilot.app.post_message(events.MouseMove(None, x, y, 0, delta_y, 1, False, False, False, screen_x=x, screen_y=y))


@pytest.mark.asyncio
async def test_new_lines_follow_a_log_only_while_it_is_scrolled_to_the_bottom() -> None:
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        await log.assert_followed()

        log_widget.post_message(make_wheel(log_widget, up=True))
        await wait_for(
            lambda: 0 < log_widget.scroll_y < log_widget.max_scroll_y, pilot=pilot, description="scrolled up"
        )
        await log.assert_not_followed()

        # A scroll heading to the bottom follows again from the first frame of its move.
        log_widget.scroll_to(y=log_widget.max_scroll_y, duration=1.0)
        await wait_for(log.animating, pilot=pilot, description="a scroll heading to the bottom")
        await log.assert_followed()


@pytest.mark.asyncio
@pytest.mark.parametrize("thumb_pressed", [False, True], ids=["home", "home-then-thumb-press"])
async def test_a_scroll_up_cut_short_by_a_taller_view_does_not_follow_new_lines(thumb_pressed: bool) -> None:
    """A taller view clamps a scroll heading up to the new bottom: that is no return to it, the scroll goes on up.

    Its frames stay at that bottom until the scroll passes it: lines that land meanwhile are not followed. A
    press on the scrollbar as it goes, which sets Textual's scroll target to where the scroll has got to, changes
    nothing.
    """
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        bottom = log_widget.max_scroll_y

        log_widget.scroll_home(duration=_CREEPING_SCROLL_SECONDS, easing="linear", immediate=True)
        # Before its first frame: a line that lands now must not take the view back down.
        assert not log_widget._bottom_follow.following
        await wait_for(log.animating, pilot=pilot, description="Home animating")
        if thumb_pressed:
            # As ``ScrollBar`` does when the mouse captures its thumb.
            log_widget.release_anchor()
        await resize_when_settled(pilot, 120, 35)
        await wait_for(
            lambda: log_widget.max_scroll_y < bottom and log_widget.scroll_y == log_widget.max_scroll_y,
            pilot=pilot,
            description="the taller view clamped the scroll to its bottom",
        )
        assert log.animating()

        log.land()
        assert not await wait_until(lambda: _at_bottom(log_widget), pilot=pilot, timeout=0.5)
        assert log.animating()
        pilot.app.animator.force_stop_animation(log_widget, "scroll_y")
        assert log_widget.scroll_y == 0
        await log.assert_not_followed()


@pytest.mark.asyncio
async def test_page_down_from_the_bottom_a_taller_view_holds_home_at_follows_new_lines() -> None:
    """Page Down stops Home, which lands at the top, and heads back to the bottom the taller view held Home at.

    Its target repeats the one Textual's ``release_anchor()`` set as Page Down started.
    """
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        bottom = log_widget.max_scroll_y
        log_widget.scroll_home(duration=_CREEPING_SCROLL_SECONDS, easing="linear", immediate=True)
        await wait_for(log.animating, pilot=pilot, description="Home animating")
        await resize_when_settled(pilot, 120, 35)
        await wait_for(
            lambda: log_widget.max_scroll_y < bottom and log_widget.scroll_y == log_widget.max_scroll_y,
            pilot=pilot,
            description="the taller view clamped Home to its bottom",
        )

        log_widget.scroll_page_down(duration=ENGINE_TURN_TIMEOUT, easing="linear")
        assert log.animating()
        assert log_widget.scroll_y == 0
        assert log_widget.scroll_target_y == log_widget.max_scroll_y
        await log.assert_followed()


@pytest.mark.asyncio
async def test_a_scroll_to_the_bottom_cut_short_by_another_scroll_lands_there_and_follows() -> None:
    """A sideways scroll (Right) stops End where it was headed, but leaves Textual's scroll target where End had got to."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_home(animate=False, immediate=True)

        log_widget.scroll_end(duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="End animating")
        log_widget.scroll_to(x=0, animate=False, immediate=True)
        assert _at_bottom(log_widget)
        assert log_widget.scroll_target_y < log_widget.max_scroll_y
        await log.assert_followed()


@pytest.mark.asyncio
async def test_a_wheel_up_as_end_scrolls_stops_the_following() -> None:
    """The wheel stops End at the bottom, then scrolls up from there: a scroll up, though from a target End had not reached."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_home(animate=False, immediate=True)

        log_widget.scroll_end(duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="End animating")
        log_widget.post_message(make_wheel(log_widget, up=True))
        await wait_for(
            lambda: not log.animating() and 0 < log_widget.scroll_y < log_widget.max_scroll_y,
            pilot=pilot,
            description="the wheel scrolled up from the bottom",
        )
        await log.assert_not_followed()


@pytest.mark.asyncio
async def test_a_scroll_up_from_where_end_is_headed_stops_the_following_before_its_first_frame() -> None:
    """The wheel asks for rows above End's target, below where End has got to: End lands first, so it scrolls up.

    Lines that land before its first frame must not take the view back down.
    """
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_home(animate=False, immediate=True)

        log_widget.scroll_end(duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="End animating")
        wheel_target = log_widget.scroll_target_y - pilot.app.scroll_sensitivity_y
        assert log_widget.scroll_y < wheel_target
        # As the wheel's notch up does, from Textual's scroll target.
        log_widget.scroll_to(y=wheel_target, duration=ENGINE_TURN_TIMEOUT, easing="linear")
        assert _at_bottom(log_widget)
        assert not log_widget._bottom_follow.following

        log.land()
        assert not await wait_until(lambda: _at_bottom(log_widget), pilot=pilot, timeout=0.5)


@pytest.mark.asyncio
async def test_a_scroll_to_the_row_end_has_got_to_stops_the_following() -> None:
    """End lands at the bottom first, so a scroll to where it had got to is a scroll up, as the wheel late in End asks."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_home(animate=False, immediate=True)

        log_widget.scroll_end(duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="End animating")
        reading = round(log_widget.scroll_y)
        log_widget.scroll_to(y=reading, animate=False, immediate=True)
        assert log_widget.scroll_y == reading < log_widget.max_scroll_y
        await log.assert_not_followed()


@pytest.mark.asyncio
async def test_a_scroll_down_a_taller_view_cuts_short_at_its_bottom_follows() -> None:
    """The scroll was headed past the new bottom: it ends there, though it was not aimed at the bottom when it started."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_to(y=50, animate=False, immediate=True)

        log_widget.scroll_to(y=60, duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="the scroll down animating")
        await resize_when_settled(pilot, 120, 70)
        await wait_for(
            lambda: log_widget.max_scroll_y < 50 and _at_bottom(log_widget),
            pilot=pilot,
            description="the taller view clamped the scroll to its bottom",
        )
        pilot.app.animator.force_stop_animation(log_widget, "scroll_y")
        assert _at_bottom(log_widget)
        await log.assert_followed()


@pytest.mark.asyncio
async def test_a_press_on_the_scrollbar_as_end_scrolls_keeps_it_following() -> None:
    """The press marks where End has got to as Textual's scroll target, and End goes on: lines that land are followed."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_home(animate=False, immediate=True)

        log_widget.scroll_end(duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="End animating")
        # As ``ScrollBar`` does when the mouse captures its thumb.
        log_widget.release_anchor()
        assert log.animating()
        await log.assert_followed()


@pytest.mark.asyncio
async def test_a_selection_dragged_past_the_edge_of_a_log_follows_only_back_at_its_bottom() -> None:
    """Textual scrolls the log a row at a time while a selection is held past its edge, asking for no scroll."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        height = log_widget.content_region.height
        bottom = log_widget.max_scroll_y

        await pilot.mouse_down(log_widget, offset=(3, height - 2))
        _drag_selection(log, row=0, delta_y=2 - height)
        await wait_for(lambda: log_widget.scroll_y < bottom - 3, pilot=pilot, description="the selection scrolls up")
        await pilot.mouse_up(log_widget, offset=(3, 0))
        await log.assert_not_followed()

        await pilot.mouse_down(log_widget, offset=(3, 1))
        _drag_selection(log, row=height - 1, delta_y=height - 2)
        await wait_for(lambda: _at_bottom(log_widget), pilot=pilot, description="the selection scrolls to the bottom")
        await pilot.mouse_up(log_widget, offset=(3, height - 1))
        await log.assert_followed()


@pytest.mark.asyncio
async def test_lines_that_land_before_a_taller_view_settles_a_following_log_are_followed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A taller view clamps a following log to its new bottom, which is no scroll up of the user's.

    The viewer's refresh may write lines before the follow settles that size change, after the refresh.
    """
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        bottom = log_widget.max_scroll_y
        follow = log_widget._bottom_follow
        settle = follow._settle
        landed: list[bool] = []

        def land_then_settle() -> None:
            if not landed and log_widget.max_scroll_y < bottom:
                landed.append(True)
                log.land(5)
            settle()

        monkeypatch.setattr(follow, "_settle", land_then_settle)
        await resize_when_settled(pilot, 120, 35)
        await wait_for(
            lambda: bool(landed) and _at_bottom(log_widget),
            pilot=pilot,
            description="the lines that landed meanwhile are followed",
        )
        assert log_widget.max_scroll_y < bottom


@pytest.mark.asyncio
async def test_a_log_follows_while_it_shows_its_bottom_row_through_moves_of_part_of_a_row() -> None:
    """A selection's auto-scroll moves part of a row a frame, and Textual shows the rounded row."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot

        def auto_scroll(rows: float) -> None:
            # As Screen's auto-scroll does each frame while a selection is held past the log's edge.
            log_widget.scroll_y += rows
            log_widget.scroll_target_y = log_widget.scroll_y

        auto_scroll(-1 / 3)
        assert log_widget.scroll_offset.y == log_widget.max_scroll_y
        await log.assert_followed()

        auto_scroll(-4 / 3)
        assert log_widget.scroll_offset.y == log_widget.max_scroll_y - 1
        assert not log_widget._bottom_follow.following
        auto_scroll(1)
        assert log_widget.scroll_offset.y == log_widget.max_scroll_y
        await log.assert_followed()

        # Home's first frames still show the bottom row: they are no move back to it.
        bottom = log_widget.max_scroll_y
        log_widget.scroll_home(duration=_CREEPING_SCROLL_SECONDS, easing="linear", immediate=True)
        await wait_for(lambda: log_widget.scroll_y < bottom, pilot=pilot, description="Home's first frame")
        assert log_widget.scroll_offset.y == bottom
        assert not log_widget._bottom_follow.following
        log.land()
        assert not await wait_until(lambda: _at_bottom(log_widget), pilot=pilot, timeout=0.5)


@pytest.mark.asyncio
async def test_a_taller_view_that_ends_exactly_at_a_scrolled_up_log_bottom_follows_new_lines() -> None:
    """The view grows by just the rows it was scrolled up: no scroll moves, and the newest line shows at the bottom."""
    async with _open_log(size=(120, 23)) as log:
        log_widget, pilot = log.widget, log.pilot
        taller_bottom = log_widget.max_scroll_y
        await resize_when_settled(pilot, 120, 20)
        await wait_for(
            lambda: log_widget.max_scroll_y > taller_bottom and _at_bottom(log_widget),
            pilot=pilot,
            description="the shorter view keeps to the bottom",
        )
        log_widget.scroll_to(y=taller_bottom, animate=False, immediate=True)
        assert not log_widget._bottom_follow.following

        await resize_when_settled(pilot, 120, 23)
        await wait_for(
            lambda: log_widget.max_scroll_y == taller_bottom and log_widget._bottom_follow.following,
            pilot=pilot,
            description="the taller view ends at the line being read, and follows",
        )
        assert log_widget.scroll_y == taller_bottom
        await log.assert_followed()


@pytest.mark.asyncio
async def test_a_log_a_taller_view_pushes_to_its_bottom_follows_though_the_scroll_target_was_left_above() -> None:
    """A sideways scroll that stops Page Down leaves Textual's scroll target where Page Down had got to."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        log_widget.scroll_home(animate=False, immediate=True)

        log_widget.scroll_to(y=60, duration=ENGINE_TURN_TIMEOUT, easing="linear")
        await wait_for(log.animating, pilot=pilot, description="the scroll down animating")
        log_widget.scroll_to(x=0, animate=False, immediate=True)
        assert log_widget.scroll_y == 60
        assert log_widget.scroll_target_y < 60
        assert not log_widget._bottom_follow.following

        await resize_when_settled(pilot, 120, 60)
        await wait_for(
            lambda: log_widget.max_scroll_y < 60 and _at_bottom(log_widget),
            pilot=pilot,
            description="the taller view pushed the log to its bottom",
        )
        assert log_widget.scroll_target_y < log_widget.max_scroll_y
        await log.assert_followed()


@pytest.mark.asyncio
async def test_a_following_log_scrolled_down_before_it_reaches_new_lines_keeps_following() -> None:
    """A scroll down while the follow's move to the new bottom waits for a refresh heads on toward it."""
    async with _open_log() as log:
        log_widget, pilot = log.widget, log.pilot
        bottom = log_widget.max_scroll_y
        log.land()
        assert log_widget.max_scroll_y > bottom
        log_widget.scroll_to(y=log_widget.scroll_y + 1, animate=False, immediate=True)
        assert log_widget.scroll_y < log_widget.max_scroll_y
        await wait_for(lambda: _at_bottom(log_widget), pilot=pilot, description="the log reached its new bottom")


@pytest.mark.asyncio
async def test_a_scrolled_up_full_log_keeps_its_lines_in_place_as_the_oldest_go() -> None:
    """Past ``MAX_DISPLAY_LINES`` each new line drops the oldest; the lines being read must not move up."""
    async with _open_log(lines=MAX_DISPLAY_LINES + 50) as log:
        log_widget, pilot = log.widget, log.pilot
        await wait_for(
            lambda: len(log_widget.lines) == MAX_DISPLAY_LINES and _at_bottom(log_widget),
            pilot=pilot,
            description="the full log opens at its newest line",
        )

        log_widget.post_message(make_wheel(log_widget, up=True))
        await wait_for(
            lambda: 0 < log_widget.scroll_y < log_widget.max_scroll_y, pilot=pilot, description="scrolled up"
        )
        reading = log_widget.lines[int(log_widget.scroll_y)].text
        log.land()
        assert log_widget.lines[-1].text.startswith("new-19")
        assert log_widget.lines[int(log_widget.scroll_y)].text == reading

        # A scroll still animating lands where it was headed, as the lines it was headed to move up.
        log_widget.scroll_page_up(duration=1.0)
        await wait_for(log.animating, pilot=pilot, description="Page Up animating")
        headed = log_widget.lines[int(log_widget.scroll_target_y)].text
        log.land(1)
        assert not log.animating()
        assert log_widget.lines[int(log_widget.scroll_y)].text == headed


@pytest.mark.asyncio
async def test_empty_state_stays_visible_after_filtered_handler_buffer_trim() -> None:
    h = _TuiLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("chrys", "warn", logging.WARNING))

    screen = LogViewerScreen()
    screen._handler = h
    screen._filter_level = logging.WARNING

    async with _LogViewerApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()

        log_widget = screen.query_one("#lv-log-chrys", RichLog)
        empty = screen.query_one("#lv-empty-chrys", HatchedEmptyState)
        assert bool(log_widget.display)
        assert not bool(empty.display)

        for index in range(MAX_DISPLAY_LINES * 2 + 10):
            h.emit(_record("chrys", f"debug-{index}", logging.DEBUG))

        screen._refresh_logs()
        await pilot.pause()

        assert bool(log_widget.display)
        assert not bool(empty.display)


@pytest.mark.asyncio
async def test_openai_records_render_from_initial_buffer() -> None:
    h = _TuiLogHandler()
    h.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    h.emit(_record("openai", "request started", logging.INFO))

    screen = LogViewerScreen()
    screen._handler = h

    async with _LogViewerApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()

        log_widget = screen.query_one("#lv-log-openai", RichLog)
        empty = screen.query_one("#lv-empty-openai", HatchedEmptyState)
        assert screen._rendered_any_lines["openai"] is True
        assert bool(log_widget.display)
        assert not bool(empty.display)


@pytest.mark.asyncio
async def test_inactive_tabs_render_lazily_on_activation() -> None:
    h = _TuiLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    h.emit(_record("chrys", "initial", logging.INFO))
    h.emit(_record("httpx", "lazy", logging.INFO))

    screen = LogViewerScreen()
    screen._handler = h

    async with _LogViewerApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()

        assert screen._rendered_any_lines["chrys"] is True
        assert "httpx" not in screen._rendered_any_lines

        tabs = screen.query_one("#lv-tabs", TabbedContent)
        tabs.active = "lv-tab-httpx"
        await pilot.pause()

        assert screen._rendered_any_lines["httpx"] is True


@pytest.mark.asyncio
async def test_flush_module_queues_truncated_display_lines_for_inactive_tab() -> None:
    h = _TuiLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))

    screen = LogViewerScreen()
    screen._handler = h

    async with _LogViewerApp().run_test(size=(120, 40)) as pilot:
        await pilot.app.push_screen(screen)
        await pilot.pause()

        full_line = "x" * (MAX_DISPLAY_LINE_CHARS + 200)
        h.emit(_record("chrys", full_line, logging.INFO))

        screen._active_module = "httpx"
        screen._flush_module("chrys")

        assert screen._pending_lines["chrys"] == [_truncate_display_line(full_line)]
        assert len(screen._pending_lines["chrys"][0]) == MAX_DISPLAY_LINE_CHARS

        lines, _ = h.snapshot("chrys", 0)
        assert lines == [full_line]


@pytest.mark.asyncio
async def test_copy_logs_uses_full_lines_when_display_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _TuiLogHandler()
    h.setFormatter(logging.Formatter("%(message)s"))
    full_line = "request-body=" + ("x" * (MAX_DISPLAY_LINE_CHARS + 200))
    h.emit(_record("chrys", full_line, logging.INFO))

    screen = LogViewerScreen()
    screen._handler = h

    async with _LogViewerApp().run_test(size=(120, 40)) as pilot:
        copied: list[str] = []
        # Patch the shared TUI clipboard helper before invoking the action.
        monkeypatch.setattr("chrys.app.tui.clipboard.clipboard_copy", copied.append)

        await pilot.app.push_screen(screen)
        await pilot.pause()

        screen.action_copy_logs()

        assert pilot.app.clipboard == full_line
        assert copied == [full_line]
