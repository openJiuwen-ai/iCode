# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Coordinate cold history requests with the viewport that is still painted."""

from __future__ import annotations

from asyncio import get_running_loop
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from asyncio import TimerHandle
    from collections.abc import Callable

    from textual.geometry import Size
    from textual.layout import DockArrangeResult
    from textual.widget import Widget

    from chrys.app.tui.widgets.chat.history_viewport import HistoryViewport
    from chrys.app.tui.widgets.chat.replay_mount import ReplayMountController

_DRAG_MOUNT_INTERVAL = 0.035


class HistoryScrollRequest(NamedTuple):
    window: tuple[int, int]
    row: float
    follow_tail: bool
    size: Size


class HistoryRestoreScroll(NamedTuple):
    from_bottom: bool
    steps: list[float]


class HistoryRestoreTurn(NamedTuple):
    turn_id: str


@dataclass(slots=True)
class RelativeHistoryScroll:
    origin: float
    steps: list[float] = field(default_factory=list)
    from_bottom: bool = False
    native_origin_fraction: float | None = None
    painted_origin: tuple[DockArrangeResult, float] | None = None

    def target_and_span(self, maximum: int, height: int) -> tuple[float, int, int, bool]:
        """Resolve each step and cover the rows it crosses at this measurement."""
        origin = maximum if self.from_bottom else self.origin
        row = low = high = min(maximum, max(0.0, origin))
        clamped = row != origin
        for distance in self.steps:
            requested = row + distance
            row = min(maximum, max(0.0, requested))
            clamped |= row != requested
            low, high = min(low, row), max(high, row)
        first, last = round(low), round(high)
        return row, first, height + last - first, clamped


class HistoryScrollController:
    """Keep the latest destination, in-flight mount and last painted viewport together."""

    def __init__(
        self,
        replay: ReplayMountController,
        stop_animation: Callable[[], None],
        ready: Callable[[HistoryScrollRequest], None],
        failed: Callable[[], None],
    ) -> None:
        self._replay = replay
        self._stop_animation = stop_animation
        self._ready = ready
        self._failed = failed
        self.pending: HistoryScrollRequest | None = None
        self.relative: RelativeHistoryScroll | None = None
        self.restoring_tail = False
        self.startup: HistoryRestoreScroll | HistoryRestoreTurn | None = None
        self.mount_request: HistoryScrollRequest | None = None
        self.mount_timer: TimerHandle | None = None
        self.mount_failed = False
        self.holding_failed_layout = False
        self.visible_row = 0.0
        self.visible_layout: DockArrangeResult | None = None
        self.visible_nodes_version = -1
        self.check_bottom_after_layout = False

    @property
    def has_request(self) -> bool:
        return self.pending is not None or self.mount_request is not None or self.mount_timer is not None

    def publish(self, layout: DockArrangeResult, row: float, nodes_version: int) -> None:
        self.visible_layout = layout
        self.visible_row = row
        self.visible_nodes_version = nodes_version

    def visible_anchor(self) -> tuple[DockArrangeResult, float] | None:
        if self.visible_layout is None:
            return None
        return self.visible_layout, self.visible_row

    def queue_startup_step(self, distance: float) -> bool:
        """Keep early input relative to the tail once it is laid out."""
        startup = self.startup
        if isinstance(startup, HistoryRestoreTurn):
            startup = self.startup = None
        if startup is None:
            if not self.restoring_tail:
                return False
            startup = self.startup = HistoryRestoreScroll(from_bottom=True, steps=[])
        startup.steps.append(distance)
        return True

    def queue_startup_home(self) -> bool:
        if self.restoring_tail or isinstance(self.startup, HistoryRestoreScroll):
            self.startup = HistoryRestoreScroll(from_bottom=False, steps=[])
            return True
        self.startup = None
        return False

    def queue_startup_turn(self, turn_id: str) -> bool:
        """Delay a TOC jump until the indexed tail has been laid out."""
        if self.restoring_tail:
            self.startup = HistoryRestoreTurn(turn_id)
            return True
        return False

    def defer(
        self,
        viewport: HistoryViewport,
        request: HistoryScrollRequest,
        *,
        dragging: bool,
    ) -> bool:
        mounted = self.mount_request
        covered = (
            mounted is not None and mounted.window[0] <= request.window[0] <= request.window[1] <= mounted.window[1]
        )
        if (mounted is None or mounted.window != request.window) and not viewport.needs_mount(
            request.window, self._replay.needs_mount
        ):
            return False
        if self.mount_failed:
            # A reflow may still need the same cold page (for example after
            # a resize). Keep the painted layout until navigation retries it.
            self.holding_failed_layout = True
            return True
        if self.pending != request:
            self.pending = request
            self._stop_animation()
        if not covered:
            # Replace queued work, leaving the bounded active batch to finish.
            self._cancel_mount()
            if dragging:
                if self.mount_timer is None:
                    self.mount_timer = get_running_loop().call_later(
                        _DRAG_MOUNT_INTERVAL, self.start_pending_mount, viewport
                    )
            else:
                self.start_pending_mount(viewport)
        elif mounted is not None and mounted.window != request.window and mounted.size != request.size:
            # Resize narrows the work still needed. At the same size, a smaller
            # range can be the first pass of remeasuring the expanded window.
            self.start_pending_mount(viewport)
        return True

    def start_pending_mount(self, viewport: HistoryViewport) -> None:
        self.stop_mount_timer()
        if (request := self.pending) is not None:
            self.mount_request = request
            self.request_widgets(request, viewport.widgets_in(request.window))

    def stop_mount_timer(self) -> None:
        if self.mount_timer is not None:
            self.mount_timer.cancel()
            self.mount_timer = None

    def request_widgets(self, request: HistoryScrollRequest, widgets: list[Widget]) -> None:
        self._replay.request_window(
            widgets, partial(self._window_ready, request), failed=partial(self._window_failed, request)
        )

    def _window_failed(self, request: HistoryScrollRequest) -> None:
        if self.mount_request is request:
            self.mount_failed = True
            self._failed()

    def allow_mount_retry(self) -> bool:
        failed = self.mount_failed
        self.mount_failed = self.holding_failed_layout = False
        return failed

    def _window_ready(self, request: HistoryScrollRequest) -> None:
        if self.mount_request is request:
            self.mount_request = None
            pending, self.pending = self.pending, None
            if pending is not None:
                self._ready(pending)

    def _cancel_mount(self) -> None:
        if self.mount_request is not None:
            self.mount_request = None
            self._replay.cancel_window_request()

    def cancel(self, *, keep_relative: bool = False) -> None:
        self.stop_mount_timer()
        self._cancel_mount()
        self.pending = None
        if not keep_relative:
            self.relative = None
