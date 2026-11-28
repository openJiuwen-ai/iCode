# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The terminal widget: a window onto a `TerminalEmulator`, and the user's hands on its program.

The widget owns no process. Whoever does feeds the program's output to `write`, and receives
whatever the terminal has for the program (keys, pastes, pointer and focus reports, the protocol's
own replies) through the callback given to `set_write_to_stdin`.

The emulator keeps history and screen as one list of rows, and the view scrolls over that list.
While it follows the output the view shows the screen whatever the scroll position says, because
the scroll position catches up with a growing history only at the next layout.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final, NamedTuple

from textual import events
from textual.cache import LRUCache
from textual.dom import NoScreen
from textual.geometry import Offset, Region, Size
from textual.message import Message
from textual.scroll_view import ScrollView
from textual.selection import Selection
from textual.strip import Strip
from textual.timer import Timer

from chrys.app.tui.behaviors.right_click_copy import copy_screen_selection
from chrys.app.tui.clipboard import paste_text_from_clipboards
from chrys.app.tui.support.gc_freeze import DetachedLruCache, detach_lru_cache, renew_lru_cache
from chrys.app.tui.terminal import emulator as vt
from chrys.app.tui.terminal.emulator import PointerAction, TerminalEmulator, encode_key, encode_paste, encode_pointer
from chrys.app.tui.terminal.leaked_reports import ReportMatch, match_outer_report
from chrys.app.tui.terminal.rendering import character_span_to_cells, draw_cursor, render_row, restyle
from chrys.app.tui.widgets.selection import normalize_selection_rich_style

ESCAPE_TAP_DURATION: Final = 0.4
"""Two Escapes within this many seconds leave the terminal; one alone reaches the program after it."""

_STRIP_CACHE_SIZE: Final = 1024
_MOTION_REPORT_INTERVAL: Final = 1 / 60
# A host that repaints after a resize sends the whole screen again, in bursts. Drawing waits until
# the burst has had this long to arrive.
_REPAINT_SETTLE_DELAY: Final = 0.033

_WHEEL_KEYS: Final = {PointerAction.WHEEL_UP: "up", PointerAction.WHEEL_DOWN: "down"}

type _MouseProtocol = tuple[vt.MouseTracking, vt.MouseEncoding]


class _QueuedMotion(NamedTuple):
    """A motion report waiting for its turn, and the protocol the program spoke when it was written."""

    report: str
    protocol: _MouseProtocol


class Terminal(ScrollView, can_focus=True):
    """Shows a terminal and types into it."""

    # A line never outgrows the terminal, so there is nothing to scroll sideways. The gutter stays
    # open whether or not there is history to scroll (the alternate screen has none): a scrollbar
    # that came and went would resize the program each time.
    DEFAULT_CSS = """
    Terminal {
        overflow-x: hidden;
        overflow-y: auto;
        scrollbar-gutter: stable;
    }
    """

    class AlternateScreenChanged(Message):
        """The program switched to or from the alternate screen."""

        def __init__(self, terminal: Terminal, enabled: bool) -> None:
            super().__init__()
            self.terminal = terminal
            self.enabled = enabled

        @property
        def control(self) -> Terminal:
            return self.terminal

    class SizeChanged(Message):
        """The terminal has a new size, which its program has yet to be told."""

        def __init__(self, terminal: Terminal, width: int, height: int) -> None:
            super().__init__()
            self.terminal = terminal
            self.width = width
            self.height = height

        @property
        def control(self) -> Terminal:
            return self.terminal

    class EscapeExited(Message):
        """The user tapped Escape twice to leave the terminal."""

        def __init__(self, terminal: Terminal) -> None:
            super().__init__()
            self.terminal = terminal

        @property
        def control(self) -> Terminal:
            return self.terminal

    class DirectoryChanged(Message):
        """The shell reported its working directory."""

        def __init__(self, terminal: Terminal, path: str) -> None:
            super().__init__()
            self.terminal = terminal
            self.path = path

        @property
        def control(self) -> Terminal:
            return self.terminal

    class CommandSubmitted(Message):
        """The shell reported the command line it is about to run."""

        def __init__(self, terminal: Terminal, command: str) -> None:
            super().__init__()
            self.terminal = terminal
            self.command = command

        @property
        def control(self) -> Terminal:
            return self.terminal

    def __init__(
        self,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        size: tuple[int, int] | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        # Hyperlinks come from the program (OSC 8), styled by the program.
        self.set_reactive(Terminal.auto_links, False)
        columns, lines = size or (80, 24)
        self.emulator = TerminalEmulator(columns, lines)
        self.host_repaints = False
        """Whoever feeds this terminal keeps a screen of its own and repaints ours after a resize
        (Windows ConPTY), so a resize must not rewrap what that host is about to send again."""
        self._write_to_stdin: Callable[[str], Awaitable[object]] | None = None
        self._strips: LRUCache[int, Strip] | DetachedLruCache = LRUCache(_STRIP_CACHE_SIZE)
        self._following = True
        self._window_start = 0
        self._rows_trimmed = 0
        self._held_escape: str | None = None
        self._held_text = ""
        self._hold_generation = 0
        self._hold_timer: Timer | None = None
        self._pending_motion: _QueuedMotion | None = None
        self._last_motion: str | None = None
        self._motion_timer: Timer | None = None
        self._repaint_timer: Timer | None = None

    # -- what the owner of the process uses ------------------------------------------------------

    @property
    def width(self) -> int:
        return self.emulator.columns

    @property
    def height(self) -> int:
        return self.emulator.lines

    @property
    def alternate_screen(self) -> bool:
        return self.emulator.alternate_screen

    def set_write_to_stdin(self, write_to_stdin: Callable[[str], Awaitable[object]] | None) -> None:
        """Name the coroutine function that delivers the terminal's input to the program."""
        self._write_to_stdin = write_to_stdin

    async def write(self, text: str) -> bool:
        """Show more of the program's output. Returns whether anything on display changed."""
        emulator = self.emulator
        was_alternate = emulator.alternate_screen
        update = emulator.feed(text)
        if emulator.alternate_screen != was_alternate:
            self.post_message(self.AlternateScreenChanged(self, emulator.alternate_screen))
        for event in update.events:
            match event:
                case vt.DirectoryChanged(path=path):
                    self.post_message(self.DirectoryChanged(self, path))
                case vt.CommandSubmitted(command=command):
                    self.post_message(self.CommandSubmitted(self, command))
        self._show(update)
        if update.replies:
            await self._send(update.replies)
        return update.damaged is None or bool(update.damaged)

    async def paste(self, text: str) -> None:
        """Deliver ``text`` the way a terminal pastes."""
        await self._type(encode_paste(text, bracketed=self.emulator.bracketed_paste))

    def reset(self) -> None:
        """Start over with a blank terminal of the same size, as for a new program."""
        was_alternate = self.emulator.alternate_screen
        self.emulator = TerminalEmulator(self.width, self.height)
        self._rows_trimmed = 0
        self._drop_selection()
        self._drop_held_keys()
        self._drop_motion()
        self._last_motion = None
        self._following = True
        if was_alternate:
            self.post_message(self.AlternateScreenChanged(self, False))
        self._sync_viewport()
        self.refresh()

    def update_size(self, width: int, height: int, *, immediate: bool = False, force_reflow: bool = False) -> None:
        """Give the terminal a new size.

        Args:
            width: Columns.
            height: Lines.
            immediate: Redraw now even when a repainting host is expected to follow up.
            force_reflow: Rewrap to the new width although the host repaints. For a size the host
                never saw the intermediate steps of, as when the widget was hidden meanwhile.
        """
        if width <= 0 or height <= 0:
            # A hidden widget is laid out empty. Keeping the last real size keeps the program's
            # idea of the screen in step with ours until the widget shows again.
            return
        emulator = self.emulator
        resized = (width, height) != (emulator.columns, emulator.lines)
        if not resized and not force_reflow:
            self._sync_viewport()
            return
        if width != emulator.columns:
            # The rows a selection names are about to be rewrapped from under it.
            self._drop_selection()
        emulator.resize(width, height, reflow=force_reflow or not self.host_repaints, host_repaints=self.host_repaints)
        if resized:
            self.post_message(self.SizeChanged(self, width, height))
        self._sync_viewport()
        if immediate or not self.host_repaints:
            self._stop_repaint_timer()
            self.refresh()
        else:
            self._stop_repaint_timer()
            self._repaint_timer = self.set_timer(_REPAINT_SETTLE_DELAY, self._repaint_settled)

    def detach_render_cache(self) -> None:
        """Let go of the strip cache, a cyclic structure, ahead of a GC freeze."""
        self._strips = detach_lru_cache(self._strips)

    def renew_render_cache(self) -> None:
        """Put a strip cache back after a GC freeze."""
        self._strips = renew_lru_cache(self._strips)

    # -- the view over the rows --------------------------------------------------------------------

    @property
    def allow_vertical_scroll(self) -> bool:
        return not self.emulator.alternate_screen and super().allow_vertical_scroll

    @property
    def allow_horizontal_scroll(self) -> bool:
        return False

    @property
    def allow_select(self) -> bool:
        # A program tracking the pointer gets the drags, and a full-screen program's rows are not text.
        emulator = self.emulator
        return not emulator.alternate_screen and emulator.mouse_tracking is vt.MouseTracking.OFF

    @property
    def cursor_screen_offset(self) -> Offset | None:
        """Where on the screen the cursor is drawn, for anchoring an input method's window."""
        buffer = self.emulator.buffer
        y = buffer.cursor_index - self._window_start
        region = self.scrollable_content_region
        if not 0 <= y < region.height:
            return None
        return Offset(region.x + min(buffer.cursor.x, region.width - 1), region.y + y)

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """The selected text. A line the terminal wrapped comes back as the one line it was."""
        buffer = self.emulator.buffer
        trimmed = self._rows_trimmed
        first = 0 if selection.start is None else max(selection.start.y - trimmed, 0)
        last = buffer.used_height - 1
        if selection.end is not None:
            last = min(selection.end.y - trimmed, last)
        pieces: list[str] = []
        for index in range(first, last + 1):
            if (span := selection.get_span(index + trimmed)) is None:
                continue
            row = buffer.rows[index]
            text = row.text
            start, end = span
            piece = text[start:] if end < 0 else text[start:end]
            if not row.wrapped and (end < 0 or end >= len(text)):
                # The blanks a row ends with were never typed; the line break after it was.
                piece = piece.rstrip() + ("\n" if index < last else "")
            pieces.append(piece)
        selected = "".join(pieces)
        return (selected, "\n") if selected else None

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._following = new_value >= self.max_scroll_y
        self._window_start = self._first_visible_row()

    def render_line(self, y: int) -> Strip:
        width = self.scrollable_content_region.width
        base_style = self.rich_style
        emulator = self.emulator
        buffer = emulator.buffer
        index = self._window_start + y
        if not 0 <= index < len(buffer.rows):
            return Strip.blank(width, base_style)
        row = buffer.rows[index]
        # Cached as the program styled it, by a key that changes when the row does: neither a
        # resize nor a new theme spoils the cache.
        strips = self._strip_cache
        if (strip := strips.get(row.stamp)) is None:
            strip = strips[row.stamp] = render_row(row)
        strip = strip.apply_style(base_style).adjust_cell_length(width, base_style)

        line_number = index + self._rows_trimmed
        if (selection := self._text_selection()) is not None and (span := selection.get_span(line_number)):
            start, end = character_span_to_cells(row.cells, *span, width)
            # Normalized, the style leaves the program's colors alone wherever the theme gives
            # selected text no color of its own.
            selected = normalize_selection_rich_style(self.screen.get_component_rich_style("screen--selection"))
            strip = restyle(strip, row, start, end, selected)
        if index == buffer.cursor_index and emulator.cursor_visible and self.has_focus:
            strip = draw_cursor(strip, row, buffer.cursor.x, emulator.cursor_shape)
            strip = strip.adjust_cell_length(width, base_style)
        return strip.apply_offsets(0, line_number)

    @property
    def _strip_cache(self) -> LRUCache[int, Strip]:
        if isinstance(self._strips, DetachedLruCache):
            self.renew_render_cache()
        if not isinstance(self._strips, LRUCache):
            raise TypeError("Renewing the terminal render cache did not install an LRU cache.")
        return self._strips

    def _text_selection(self) -> Selection | None:
        try:
            return self.text_selection
        except NoScreen:
            return None

    def _drop_selection(self) -> None:
        try:
            screen = self.screen
        except NoScreen:
            return
        if self in screen.selections:
            screen.selections = {widget: held for widget, held in screen.selections.items() if widget is not self}

    def _first_visible_row(self) -> int:
        """The index of the row drawn on the widget's first line."""
        buffer = self.emulator.buffer
        if self.emulator.alternate_screen:
            return buffer.top
        if buffer.used_height <= self.scrollable_content_region.height:
            return 0
        return buffer.top if self._following else min(round(self.scroll_y), buffer.top)

    def _sync_viewport(self) -> None:
        """Size the scrollable area to the rows there are, and keep the view where it belongs."""
        buffer = self.emulator.buffer
        visible_width, visible_height = self.scrollable_content_region.size
        if self.emulator.alternate_screen or buffer.used_height <= visible_height:
            height = visible_height if self.emulator.alternate_screen else buffer.used_height
        else:
            height = max(buffer.used_height, buffer.top + visible_height)
        if self.virtual_size != (virtual_size := Size(visible_width, height)):
            self.virtual_size = virtual_size
        if self._following:
            self._scroll_at_once(self.max_scroll_y)
        self._window_start = self._first_visible_row()

    def _scroll_at_once(self, y: float) -> None:
        """Put the view at ``y`` now. The scroll target goes along: the wheel scrolls on from there."""
        if self.scroll_target_y != y:
            self.scroll_target_y = y
        if self.scroll_y != y:
            self.scroll_y = y

    def _show(self, update: vt.Update) -> None:
        """Bring the display up to date with what a write changed."""
        first_before = self._window_start
        if update.trimmed:
            self._rows_trimmed += update.trimmed
            if not self._following:
                # The rows moved up; move the view with them so it stays on the same text.
                self._scroll_at_once(max(0.0, self.scroll_y - update.trimmed))
        self._sync_viewport()
        if self._repaint_timer is not None:
            return
        first = self._window_start
        if update.damaged is None or first != first_before:
            self.refresh()
            return
        region = self.scrollable_content_region
        lines = [
            Region(0, index - first, region.width, 1) for index in update.damaged if 0 <= index - first < region.height
        ]
        if lines:
            self.refresh(*lines)

    def _refresh_cursor(self) -> None:
        region = self.scrollable_content_region
        if 0 <= (y := self.emulator.buffer.cursor_index - self._window_start) < region.height:
            self.refresh(Region(0, y, region.width, 1))

    def _stop_repaint_timer(self) -> None:
        if self._repaint_timer is not None:
            self._repaint_timer.stop()
            self._repaint_timer = None

    def _repaint_settled(self) -> None:
        self._repaint_timer = None
        self.refresh()

    def on_mount(self) -> None:
        self.update_size(*self.scrollable_content_region.size, immediate=True)

    def on_resize(self) -> None:
        self.update_size(*self.scrollable_content_region.size)

    # -- input -------------------------------------------------------------------------------------

    async def _send(self, data: str) -> None:
        if self._write_to_stdin is not None:
            await self._write_to_stdin(data)

    async def _type(self, data: str) -> None:
        """Send something the user did, which also brings the view back to where it takes effect."""
        if not self._following:
            shown_from = self._window_start
            self._following = True
            self._sync_viewport()
            if self._window_start != shown_from:
                self.refresh()
        await self._send(data)

    async def on_key(self, event: events.Key) -> None:
        event.prevent_default()
        event.stop()
        key = event.key
        if key in ("ctrl+c", "ctrl+insert") and copy_screen_selection(self.screen):
            return
        if key == "ctrl+insert":
            pass
        elif key == "shift+insert":
            if text := paste_text_from_clipboards(self.app):
                await self.paste(text)
        else:
            await self._press(event)

    async def _press(self, event: events.Key) -> None:
        emulator = self.emulator
        encoded = encode_key(
            event.key,
            event.character,
            application_cursor_keys=emulator.application_cursor_keys,
            protocol=emulator.key_protocol,
        )
        if event.key == "escape" and not emulator.alternate_screen:
            # Held back: a second tap leaves the terminal, and the keys that follow may turn out to
            # be the rest of a report from the outer terminal. A full-screen program gets its
            # Escape at once instead; there it is an everyday key and a delay would be felt.
            if self._held_escape is not None and not self._held_text:
                self._drop_held_keys()
                self.blur()
                self.post_message(self.EscapeExited(self))
                return
            await self._release_held_keys()
            self._held_escape = encoded
            self._restart_hold_timer()
            return
        if self._held_escape is not None and event.character is not None:
            held_text = self._held_text + event.character
            match match_outer_report(f"\x1b{held_text}"):
                case ReportMatch.COMPLETE:
                    self._drop_held_keys()
                    return
                case ReportMatch.PARTIAL:
                    self._held_text = held_text
                    self._restart_hold_timer()
                    return
        await self._release_held_keys()
        if encoded is not None:
            await self._type(encoded)

    def _restart_hold_timer(self) -> None:
        if self._hold_timer is not None:
            self._hold_timer.stop()
        self._hold_generation += 1
        generation = self._hold_generation

        async def expire() -> None:
            # A stopped timer may already have its callback queued; it must not release a later hold.
            if generation == self._hold_generation:
                await self._release_held_keys()

        self._hold_timer = self.set_timer(ESCAPE_TAP_DURATION, expire)

    def _drop_held_keys(self) -> str:
        """Forget the held keys, returning what they would have sent."""
        held = f"{self._held_escape}{self._held_text}" if self._held_escape is not None else ""
        self._held_escape = None
        self._held_text = ""
        self._hold_generation += 1
        if self._hold_timer is not None:
            self._hold_timer.stop()
            self._hold_timer = None
        return held

    async def _release_held_keys(self) -> None:
        if held := self._drop_held_keys():
            await self._type(held)

    async def on_paste(self, event: events.Paste) -> None:
        event.prevent_default()
        event.stop()
        await self.paste(event.text)

    async def on_focus(self) -> None:
        self._refresh_cursor()
        if self.emulator.focus_reporting:
            await self._send(vt.FOCUS_IN)

    async def on_blur(self) -> None:
        self._refresh_cursor()
        if self.emulator.focus_reporting:
            await self._send(vt.FOCUS_OUT)

    def on_click(self, event: events.Click) -> None:
        self.focus()
        event.stop()

    # -- pointer -----------------------------------------------------------------------------------

    def _pointer_report(self, action: PointerAction, event: events.MouseEvent) -> str | None:
        """The report the program asked for about a pointer event, if it asked."""
        emulator = self.emulator
        if emulator.mouse_tracking is vt.MouseTracking.OFF:
            return None
        offset = event.get_content_offset_capture(self)
        x, y = offset.x, self._window_start + offset.y - emulator.buffer.top
        if self.app.mouse_captured is self:
            # A drag that left the terminal is still reported, from the edge it left by.
            x = min(max(x, 0), emulator.columns - 1)
            y = min(max(y, 0), emulator.lines - 1)
        elif not (0 <= x < emulator.columns and 0 <= y < emulator.lines):
            return None
        return encode_pointer(
            emulator.mouse_tracking,
            emulator.mouse_encoding,
            action,
            x=x,
            y=y,
            button=event.button - 1 if event.button else None,
            shift=event.shift,
            alt=event.meta,
            ctrl=event.ctrl,
        )

    async def _report_button(self, action: PointerAction, event: events.MouseEvent) -> bool:
        if (report := self._pointer_report(action, event)) is None:
            return False
        event.prevent_default()
        event.stop()
        await self._flush_motion()
        self._last_motion = None
        await self._send(report)
        return True

    async def on_mouse_down(self, event: events.MouseDown) -> None:
        if await self._report_button(PointerAction.PRESS, event):
            # The program is owed the rest of the gesture, wherever the pointer goes meanwhile.
            self.capture_mouse()

    async def on_mouse_up(self, event: events.MouseUp) -> None:
        await self._report_button(PointerAction.RELEASE, event)
        if self.app.mouse_captured is self:
            self.release_mouse()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if (report := self._pointer_report(PointerAction.MOVE, event)) is None:
            return
        event.prevent_default()
        event.stop()
        if report == self._last_motion:
            # Still in the same cell: a terminal reports motion between cells, not within one.
            return
        self._last_motion = report
        self._pending_motion = _QueuedMotion(report, self._mouse_protocol())
        if self._motion_timer is None:
            self._motion_timer = self.set_timer(_MOTION_REPORT_INTERVAL, self._flush_motion)

    def _mouse_protocol(self) -> _MouseProtocol:
        return self.emulator.mouse_tracking, self.emulator.mouse_encoding

    async def _flush_motion(self) -> None:
        """Send the latest motion. Only the latest: a program redrawing per report cannot keep up with more."""
        motion = self._pending_motion
        self._drop_motion()
        if motion is None:
            return
        if motion.protocol == self._mouse_protocol():
            await self._send(motion.report)
        else:
            # The program stopped tracking, or left, while this waited. Whatever reads its input now
            # would take the report for something typed.
            self._last_motion = None

    def _drop_motion(self) -> None:
        """Forget the motion waiting to be sent."""
        if self._motion_timer is not None:
            self._motion_timer.stop()
            self._motion_timer = None
        self._pending_motion = None

    async def _wheel(self, action: PointerAction, event: events.MouseEvent) -> None:
        emulator = self.emulator
        report = self._pointer_report(action, event)
        scrolls_program = emulator.alternate_screen and emulator.alternate_scroll
        if report is None and scrolls_program and (key := _WHEEL_KEYS.get(action)) is not None:
            # A full-screen program has no history of ours to scroll; the wheel moves its cursor.
            report = encode_key(
                key, None, application_cursor_keys=emulator.application_cursor_keys, protocol=emulator.key_protocol
            )
        if report is None:
            return
        event.prevent_default()
        event.stop()
        await self._send(report)

    async def on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        await self._wheel(PointerAction.WHEEL_UP, event)

    async def on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        await self._wheel(PointerAction.WHEEL_DOWN, event)

    async def on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        await self._wheel(PointerAction.WHEEL_LEFT, event)

    async def on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        await self._wheel(PointerAction.WHEEL_RIGHT, event)
