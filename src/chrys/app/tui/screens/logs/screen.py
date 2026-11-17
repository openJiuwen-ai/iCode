# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""LogViewerScreen — real-time logger output viewer grouped by module."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, ClassVar

from rich.text import Text
from textual import on
from textual.containers import VerticalGroup
from textual.css.query import NoMatches
from textual.timer import Timer
from textual.widgets import RichLog, TabbedContent, TabPane

from chrys.app.tui.binding_display import CLOSE_BINDING, COPY_BINDING, localized_binding
from chrys.app.tui.clipboard import copy_text_to_clipboards, notify_after_copy
from chrys.app.tui.copy_messages import COPIED_TITLE
from chrys.app.tui.i18n import render_str, widget_localizer
from chrys.app.tui.screens.dialogs.base import BaseDialog
from chrys.app.tui.util.bottom_follow import BottomFollow
from chrys.app.tui.widgets import HatchedEmptyState
from chrys.foundation.i18n import msg

if TYPE_CHECKING:
    from typing import Self

    from rich.console import RenderableType
    from textual.app import ComposeResult
    from textual.events import Resize


_NO_ACTIVE_TAB = msg("tui.logs.copy.no_active_tab", fallback="No active tab to copy from")
_LEVEL_BINDING = msg("tui.binding.cycle_level", fallback="Level")
_COPY_LOGS_TITLE = msg("tui.logs.copy.title", fallback="Copy logs")
_NO_LOGS = msg("tui.logs.copy.no_logs", fallback="No {level}+ logs in '{module}'")
_COPIED_LINES = msg(
    "tui.logs.copy.copied_lines",
    fallback="Copied {count} line from '{module}'",
    plural_fallback="Copied {count} lines from '{module}'",
)
_LOG_VIEWER_TITLE = msg("tui.logs.title", fallback="Log Viewer")
_LEVEL_HINT = msg(
    "tui.logs.level_hint",
    fallback="Level: {level_name} (Press space to cycle, c to copy)",
)
_NO_MODULE_LOGS = msg("tui.logs.empty.module", fallback="No logs for {module}")
_NO_FILTERED_MODULE_LOGS = msg(
    "tui.logs.empty.filtered_module",
    fallback="No {level_name}+ logs for {module}",
)

MAX_DISPLAY_LINES = 1500
MAX_DISPLAY_LINE_CHARS = 2048
_TRUNCATED_LINE_SUFFIX = "..."
MAX_BUFFER_LINES = MAX_DISPLAY_LINES * 2
MAX_BUFFER_LINE_CHARS = 16 * 1024
"""Longest record the handler keeps whole for copying; a longer one keeps its head
and tail. SDK DEBUG records can carry a whole request body (the conversation so far)."""
_LOAD_BATCH_LINES = 250

# Interval in seconds between log refresh ticks
_REFRESH_INTERVAL = 1

# Top-level logger names the viewer will display, in tab order. Other
# loggers are still emitted to any global handlers (e.g. the debug log
# file), but are skipped by this in-memory handler to keep the UI focused
# and memory bounded.
MODULE_ORDER: tuple[str, ...] = (
    "chrys",
    "openai",
    "anthropic",
    "httpcore",
    "httpx",
)
MODULE_WHITELIST: frozenset[str] = frozenset(MODULE_ORDER)

# Level filter cycle — cycled by pressing Space in the viewer.
_LEVEL_CYCLE: tuple[int, ...] = (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR)


class _TuiLogHandler(logging.Handler):
    """In-memory log handler that buffers records per top-level module name.

    Callers read via :meth:`snapshot`, which returns only the lines emitted
    since the caller's last observed ``total`` count.  Totals are monotonic
    even when the in-memory buffer is trimmed, so buffer trimming never
    causes readers to skip new lines.  Each buffered entry carries the
    record's numeric level so the UI can re-filter without re-parsing.
    A record longer than ``MAX_BUFFER_LINE_CHARS`` is kept cut short.
    """

    def __init__(self) -> None:
        super().__init__()
        self._buffers: dict[str, list[tuple[int, str]]] = {}
        # Monotonic count of records emitted per module (never decremented,
        # even when the buffer is trimmed).
        self._totals: dict[str, int] = {}

    # ------------------------------------------------------------------
    # logging.Handler interface
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        module = record.name.split(".")[0]
        if module not in MODULE_WHITELIST:
            return
        line = _truncate_buffered_line(self.format(record))
        buf = self._buffers.setdefault(module, [])
        buf.append((record.levelno, line))
        self._totals[module] = self._totals.get(module, 0) + 1
        # Trim oldest entries to avoid unbounded growth. Totals stay
        # monotonic; readers reconcile via ``snapshot``.
        if len(buf) > MAX_BUFFER_LINES:
            del buf[:MAX_DISPLAY_LINES]

    # ------------------------------------------------------------------
    # Public helpers (thread-safe via handler lock)
    # ------------------------------------------------------------------

    def modules_with_logs(self) -> list[str]:
        """Return sorted list of modules that have buffered log lines."""
        self.acquire()
        try:
            return sorted(self._buffers)
        finally:
            self.release()

    def snapshot(
        self,
        module: str,
        since_total: int,
        min_level: int = logging.NOTSET,
        limit: int | None = None,
    ) -> tuple[list[str], int]:
        """Return ``(new_lines, new_total)`` for *module*.

        ``new_lines`` contains every record emitted since ``since_total``
        that is still present in the in-memory buffer AND whose level
        is at least ``min_level``. If the buffer has been trimmed past
        ``since_total``, older dropped lines are silently skipped — the
        returned ``new_total`` still reflects the real count, so
        subsequent calls remain consistent.

        When ``limit`` is provided, only the newest matching records are
        returned. The total still advances to the latest record count so
        UI callers can tail large buffers without writing lines their
        widget will immediately discard.
        """
        self.acquire()
        try:
            total = self._totals.get(module, 0)
            buf = self._buffers.get(module, [])
            if since_total >= total:
                return [], total
            missing = total - since_total
            available = min(missing, len(buf))
            start = len(buf) - available
            if limit is not None and limit <= 0:
                return [], total
            recent = buf[start:]
            lines = [line for (lvl, line) in recent if lvl >= min_level]
            if limit is not None and len(lines) > limit:
                lines = lines[-limit:]
            return lines, total
        finally:
            self.release()


# Singleton handler — installed once, shared across all LogViewerScreen instances.
_handler: _TuiLogHandler | None = None


def install_log_handler() -> _TuiLogHandler:
    """Install the in-memory log handler on the root logger.

    Call this once at process startup (after debug-file logging is configured)
    so that records from whitelisted modules are captured for the entire
    lifetime of the app — not only from the moment the Log Viewer modal
    is first opened. Subsequent calls are idempotent.
    """
    global _handler
    root_logger = logging.getLogger()
    if _handler is None:
        _handler = _TuiLogHandler()
        _handler.setLevel(logging.DEBUG)
        _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        # Lower each whitelisted logger to DEBUG so DEBUG records are
        # created and can reach this handler. The queued root file handler
        # (attached in app.py) will also observe these records, which is
        # consistent with the shared debug file logging. Loggers app wiring
        # already pinned to an explicit level are left alone.
        for module in MODULE_WHITELIST:
            module_logger = logging.getLogger(module)
            if module_logger.level == logging.NOTSET:
                module_logger.setLevel(logging.DEBUG)
    if _handler not in root_logger.handlers:
        root_logger.addHandler(_handler)
    return _handler


class _ModuleLog(RichLog):
    """One module's log: new lines keep it at the bottom while the user leaves it there."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._bottom_follow = BottomFollow(self)

    def _scroll_to(self, x: float | None = None, y: float | None = None, **kwargs: Any) -> bool:
        with self._bottom_follow.scroll(y):
            return super()._scroll_to(x, y, **kwargs)

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        super().watch_scroll_y(old_value, new_value)
        self._bottom_follow.scrolled(old_value, new_value)

    def watch_virtual_size(self) -> None:
        self._bottom_follow.resized()

    def on_resize(self, event: Resize) -> None:
        # No super(): Textual runs RichLog's own handler next in the same MRO walk.
        self._bottom_follow.resized()

    def write(
        self,
        content: RenderableType | object,
        width: int | None = None,
        expand: bool = False,
        shrink: bool = True,
        scroll_end: bool | None = None,
        animate: bool = False,
    ) -> Self:
        start = self._start_line
        super().write(content, width, expand, shrink, scroll_end, animate)
        dropped = self._start_line - start
        if dropped and not self._bottom_follow.following:
            # Past ``max_lines`` the oldest lines go and the rest move up: keep the
            # lines the user is reading where they were. A scroll still animating
            # (Page Up, Home) lands where it was headed first, or the move would
            # cut it short.
            self.app.animator.force_stop_animation(self, "scroll_y")
            self.scroll_to(y=max(0.0, self.scroll_y - dropped), animate=False, immediate=True)
        return self


def _make_log_widget(module: str) -> RichLog:
    widget = _ModuleLog(
        id=f"lv-log-{module}",
        max_lines=MAX_DISPLAY_LINES,
        wrap=False,
        highlight=True,
        markup=False,
        auto_scroll=False,
    )
    widget.display = False
    return widget


def _truncate_buffered_line(line: str) -> str:
    if len(line) <= MAX_BUFFER_LINE_CHARS:
        return line
    # The head names the record; a traceback ends with the exception raised.
    half = MAX_BUFFER_LINE_CHARS // 2
    return f"{line[:half]} ... [{len(line) - 2 * half} of {len(line)} chars cut] ... {line[-half:]}"


def _truncate_display_line(line: str) -> str:
    """Return a display-bounded log line while the handler keeps longer lines for copying."""
    if len(line) <= MAX_DISPLAY_LINE_CHARS:
        return line
    # Formatted exception records can contain embedded newlines; bounding the
    # formatted record here keeps pathological records cheap for RichLog.
    return line[: MAX_DISPLAY_LINE_CHARS - len(_TRUNCATED_LINE_SUFFIX)] + _TRUNCATED_LINE_SUFFIX


class LogViewerScreen(BaseDialog[None]):
    """Modal that shows runtime logger output grouped by top-level module.

    Every whitelisted module gets a tab up-front. Press F6 (or Escape) to
    close; press Space to cycle the minimum displayed level between
    DEBUG, INFO and WARNING; press C to copy the active tab's full log
    lines under the current level filter to the clipboard.
    """

    CSS_PATH = "screen.tcss"

    BINDINGS: ClassVar[list] = [
        localized_binding("escape", "dismiss", CLOSE_BINDING, show=True, priority=True),
        localized_binding("f6", "dismiss", CLOSE_BINDING, show=False, priority=True),
        localized_binding("space", "cycle_level", _LEVEL_BINDING, show=True, priority=True),
        localized_binding("c", "copy_logs", COPY_BINDING, show=True, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        # In normal app flow the handler is already installed at startup by
        # app.py's ``main``; this is a safety net for test/embedded use.
        self._handler = install_log_handler()
        # module -> monotonic total count last flushed into the RichLog widget
        self._flushed: dict[str, int] = {}
        # module -> lines queued for batched writes into the active RichLog
        self._pending_lines: dict[str, list[str]] = {}
        # module -> whether any line has been flushed into the RichLog under the active filter
        self._rendered_any_lines: dict[str, bool] = {}
        # active tab module name
        self._active_module: str = ""
        # minimum level currently rendered
        self._filter_level: int = logging.DEBUG
        self._drain_timer: Timer | None = None

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        # Always show every whitelisted module as a tab regardless of
        # whether it has captured any records yet.
        with VerticalGroup(id="lv-container") as container:
            container.border_title = Text(render_str(widget_localizer(self), _LOG_VIEWER_TITLE.bind()))
            container.border_subtitle = Text(self._level_hint())
            with TabbedContent(id="lv-tabs"):
                for module in MODULE_ORDER:
                    with TabPane(module, id=f"lv-tab-{module}"), VerticalGroup(classes="lv-log-stack"):
                        yield _make_log_widget(module)
                        yield HatchedEmptyState(
                            self._empty_label(module),
                            id=f"lv-empty-{module}",
                            classes="lv-empty",
                        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        active_modules = set(self._handler.modules_with_logs())

        self._active_module = self._initial_active_module(active_modules)
        self._activate_tab(self._active_module)
        self._flush_module(self._active_module)
        for module in MODULE_ORDER:
            self._sync_empty_state(module)

        self.set_interval(_REFRESH_INTERVAL, self._refresh_logs)

    def _initial_active_module(self, active_modules: set[str]) -> str:
        """Choose the initial tab without forcing every tab to render."""
        for module in MODULE_ORDER:
            if module in active_modules:
                return module
        return MODULE_ORDER[0]

    def _activate_tab(self, module: str) -> None:
        try:
            tabs = self.query_one("#lv-tabs", TabbedContent)
        except NoMatches:
            return
        tabs.active = f"lv-tab-{module}"

    # ------------------------------------------------------------------
    # Tab switching
    # ------------------------------------------------------------------

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane is None:
            return
        pane_id = event.pane.id or ""
        prefix = "lv-tab-"
        if pane_id.startswith(prefix):
            module = pane_id[len(prefix) :]
            self._active_module = module
            # Switching tabs is a user intent to see the latest content.
            self._flush_module(module)
            self._sync_empty_state(module)
            self._scroll_to_bottom(module)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh_logs(self) -> None:
        """Called on interval — flush new lines into the active RichLog."""
        if self._active_module:
            self._flush_module(self._active_module)

    def _flush_module(self, module: str) -> None:
        """Write any new lines for *module* into its RichLog widget.

        New lines keep a log scrolled to its bottom there; one the user
        scrolled up stays where they put it.
        """
        try:
            # Guard before advancing the flushed marker; the actual write path
            # fetches the widget again when it drains the pending batch.
            self.query_one(f"#lv-log-{module}", RichLog)
        except NoMatches:
            return

        already = self._flushed.get(module, 0)
        new_lines, new_total = self._handler.snapshot(
            module,
            already,
            self._filter_level,
            limit=MAX_DISPLAY_LINES,
        )
        # Advance the flushed marker even if the filter dropped everything,
        # so the next tick only inspects genuinely newer records.
        self._flushed[module] = new_total
        if not new_lines:
            if module == self._active_module and self._pending_lines.get(module):
                self._drain_pending_logs()
                return
            self._sync_empty_state(module)
            return

        pending = self._pending_lines.setdefault(module, [])
        pending.extend(_truncate_display_line(line) for line in new_lines)
        if len(pending) > MAX_DISPLAY_LINES:
            del pending[: len(pending) - MAX_DISPLAY_LINES]
        self._drain_pending_logs()

    def _drain_pending_logs(self, *, from_timer: bool = False) -> None:
        """Write a bounded chunk so opening the viewer never monopolizes the UI loop."""
        if from_timer:
            self._drain_timer = None
        else:
            self._stop_drain_timer()
        module = self._active_module
        if not module:
            return
        pending = self._pending_lines.get(module)
        if not pending:
            self._sync_empty_state(module)
            return

        try:
            log_widget = self.query_one(f"#lv-log-{module}", RichLog)
        except NoMatches:
            return

        chunk = pending[:_LOAD_BATCH_LINES]
        del pending[:_LOAD_BATCH_LINES]
        for line in chunk:
            log_widget.write(line, scroll_end=False)
        self._rendered_any_lines[module] = True
        self._sync_empty_state(module)
        if pending:
            self._schedule_drain()

    def _schedule_drain(self) -> None:
        self._stop_drain_timer()
        self._drain_timer = self.set_timer(0.01, lambda: self._drain_pending_logs(from_timer=True))

    def _stop_drain_timer(self) -> None:
        if self._drain_timer is not None:
            self._drain_timer.stop()
            self._drain_timer = None

    def _sync_empty_state(self, module: str) -> None:
        try:
            log_widget = self.query_one(f"#lv-log-{module}", RichLog)
            empty = self.query_one(f"#lv-empty-{module}", HatchedEmptyState)
        except NoMatches:
            return

        has_visible_logs = self._rendered_any_lines.get(module, False)
        log_widget.display = has_visible_logs
        empty.update_label(self._empty_label(module))
        empty.display = not has_visible_logs

    def _scroll_to_bottom(self, module: str) -> None:
        try:
            log_widget = self.query_one(f"#lv-log-{module}", RichLog)
        except NoMatches:
            return
        log_widget.scroll_end(animate=False)

    # ------------------------------------------------------------------
    # Level cycle (space)
    # ------------------------------------------------------------------

    def action_cycle_level(self) -> None:
        idx = _LEVEL_CYCLE.index(self._filter_level)
        self._filter_level = _LEVEL_CYCLE[(idx + 1) % len(_LEVEL_CYCLE)]
        self._re_render_with_filter()
        self.query_one("#lv-container", VerticalGroup).border_subtitle = Text(self._level_hint())

    def _re_render_with_filter(self) -> None:
        """Clear every RichLog and re-flush from scratch under the current filter."""
        self._stop_drain_timer()
        self._flushed.clear()
        self._pending_lines.clear()
        self._rendered_any_lines.clear()
        for module in MODULE_ORDER:
            try:
                log_widget = self.query_one(f"#lv-log-{module}", RichLog)
            except NoMatches:
                continue
            log_widget.clear()
            self._sync_empty_state(module)
        if self._active_module:
            self._flush_module(self._active_module)
            self._scroll_to_bottom(self._active_module)

    def _level_hint(self) -> str:
        level_name = logging.getLevelName(self._filter_level)
        return render_str(widget_localizer(self), _LEVEL_HINT.bind(level_name=level_name))

    def _empty_label(self, module: str) -> str:
        level_name = logging.getLevelName(self._filter_level)
        if self._filter_level == logging.DEBUG:
            return render_str(widget_localizer(self), _NO_MODULE_LOGS.bind(module=module))
        return render_str(
            widget_localizer(self),
            _NO_FILTERED_MODULE_LOGS.bind(level_name=level_name, module=module),
        )

    # ------------------------------------------------------------------
    # Copy (c)
    # ------------------------------------------------------------------

    def action_copy_logs(self) -> None:
        """Copy full log lines under the current level filter to the clipboard."""
        module = self._active_module
        if not module:
            localizer = widget_localizer(self)
            self.notify(
                render_str(localizer, _NO_ACTIVE_TAB.bind()),
                title=render_str(localizer, _COPY_LOGS_TITLE.bind()),
                severity="warning",
                timeout=3,
                markup=False,
            )
            return

        # Snapshot from 0 under the current filter so the clipboard matches
        # the visible line set. Lines are copied as the handler keeps them,
        # longer than display rendering shows them.
        lines, _ = self._handler.snapshot(module, 0, self._filter_level, limit=MAX_DISPLAY_LINES)
        if not lines:
            level_name = logging.getLevelName(self._filter_level)
            localizer = widget_localizer(self)
            self.notify(
                render_str(localizer, _NO_LOGS.bind(level=level_name, module=module)),
                title=render_str(localizer, _COPY_LOGS_TITLE.bind()),
                severity="warning",
                timeout=3,
                markup=False,
            )
            return

        payload = "\n".join(lines)
        dispatched = copy_text_to_clipboards(self.app, payload)
        localizer = widget_localizer(self)
        notify_after_copy(
            self,
            render_str(localizer, _COPIED_LINES.bind(count=len(lines), module=module)),
            dispatched=dispatched,
            title=render_str(localizer, COPIED_TITLE.bind()),
            timeout=2,
            markup=False,
        )
