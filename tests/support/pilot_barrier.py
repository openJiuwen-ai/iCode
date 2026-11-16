# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A Pilot wait that also drains what the messages it waited for posted in turn.

Textual 8.2.7's ``Pilot._wait_for_screen`` queues one callback behind whatever the App and
each widget of the current screen hold when it starts, and returns once every callback has
run. A handler that runs inside that window and posts on — a Button's ``Pressed`` bubbling to
the App, a dismissal replacing the screen, a widget handing its layout request to the screen
when it goes idle — leaves work the wait never covered. Pilot then relies on ``wait_for_idle``,
which calls the process idle once a 20 ms sleep burns no CPU time. A starved runner burns none
while it is descheduled, and Windows counts process time in 15.6 ms ticks, so the probe returns
with that work still queued and the test reads state from before it ran.

This wait queues the callback again on every pump of the current screen that still holds a
message or a pending callback, or is still inside a handler — a pump takes a message off its
queue before it runs the handler, so an empty queue alone says nothing — re-reading the screen
each round because a dismissal changes it, until a round finds nothing left. Work that runs
outside a pump — a worker, a task, a timer — is not a message, so a test still waits for the
state that work produces.
"""

from __future__ import annotations

import asyncio
import functools
import sys
import time
from typing import TYPE_CHECKING

from tests.support.stall_diagnostics import awaited_chain, describe_pending_pumps, frame_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import CodeType

    import pytest
    from textual.app import App
    from textual.message_pump import MessagePump
    from textual.pilot import Pilot
    from textual.screen import Screen

_SETTLED_TEXTUAL_VERSION = "8.2.7"
_monotonic = time.monotonic
"""Bound at import: tests patch ``time.monotonic`` to a constant, and the deadline must not follow them."""


@functools.cache
def _park() -> tuple[CodeType, CodeType]:
    """The one await a pump's loop parks in between messages: ``MessagePump._get_message`` on its
    queue's ``get``. Resolved on first use, so an unpinned Textual without these privates still
    imports this module; the pinned install resolves it up front."""
    from textual._queue import Queue
    from textual.message_pump import MessagePump

    return MessagePump._get_message.__code__, Queue.get.__code__


def is_parked(pump: MessagePump) -> bool:
    """*pump*'s loop is waiting for its next message.

    False while it is inside a message handler, an idle handler or a callback, and for a loop
    that has not started or has finished. A message posted now is the next thing a parked pump
    runs, so a callback queued behind it covers nothing the pump is still in the middle of.

    Read from another task: from the pump's own task its coroutine is running, not suspended,
    and reads as inside a handler.
    """
    task = pump._task
    if task is None or task.done():
        return False
    get_message, queue_get = _park()
    previous: CodeType | None = None
    for awaited in awaited_chain(task):
        frame = frame_of(awaited)
        if frame is None:
            return False
        if frame.f_code is queue_get:
            return previous is get_message
        previous = frame.f_code
    return False


def holds_pending_work(pump: MessagePump) -> bool:
    """*pump* has a message it has not dispatched yet or is still inside, or a callback it runs
    after the current one.

    A pump checks whether the message it took supersedes the next one by taking that one off
    the queue too, and keeps it aside as ``_pending_message`` until its turn: the queue then
    reads empty with a message still to go. It also takes a message off the queue before it
    runs the handler, so an empty queue with the loop off its wait is the pump still busy: in a
    handler, an idle handler or a callback, or a loop not yet at its first wait.
    """
    if pump._pending_message is not None or not pump._message_queue.empty() or bool(pump._next_callbacks):
        return True
    task = pump._task
    return task is not None and not task.done() and not is_parked(pump)


def screen_is_settled(app: App[object], screen: Screen[object]) -> bool:
    """Nothing is left that would still reach *screen*'s layout: no queued message or callback
    in the App or any widget of *screen*, no handler any of them is still inside, no layout or
    scroll request a widget has yet to hand on when it goes idle, and no callback waiting for
    *screen*'s next refresh."""
    widgets = list(screen.walk_children(with_self=True))
    return (
        not any(holds_pending_work(pump) for pump in (app, *widgets))
        and not any(widget._layout_required or widget._scroll_required for widget in widgets)
        and not screen._callbacks
    )


async def settled_wait_for_screen(self: Pilot, timeout: float = 30.0) -> bool:
    """Textual 8.2.7's ``Pilot._wait_for_screen``, repeated until the screen holds nothing more.

    As upstream, returns False when the App reports an exception and raises
    ``WaitForScreenTimeout`` when *timeout* runs out; the message then names the widgets still
    holding a callback and the handler each one's pump is suspended in.
    """
    deadline = _monotonic() + timeout
    first_round = True
    while True:
        try:
            screen = self.app.screen
        except Exception:
            return False
        pumps = [self.app, *screen.walk_children(with_self=True)]
        if not first_round:
            pumps = [pump for pump in pumps if holds_pending_work(pump)]
        first_round = False
        processed = await _barrier(self.app, pumps, deadline)
        if processed is not None:
            return processed


async def _barrier(app: App[object], pumps: list[MessagePump], deadline: float) -> bool | None:
    """Queue a callback behind everything each of *pumps* holds and wait for all of them.

    None when every callback ran, so another round may follow; True when no pump took one,
    so there is nothing left; False when the App reported an exception first.
    """
    from textual.pilot import WaitForScreenTimeout

    pending: dict[int, MessagePump] = {}
    all_processed = asyncio.Event()

    def callback_for(pump: MessagePump) -> Callable[[], None]:
        def processed() -> None:
            pending.pop(id(pump), None)
            if not pending:
                all_processed.set()

        return processed

    for pump in pumps:
        pending[id(pump)] = pump
        if not pump.call_later(callback_for(pump)):
            pending.pop(id(pump), None)
    if not pending:
        return True
    waits = [asyncio.create_task(all_processed.wait()), asyncio.create_task(app._exception_event.wait())]
    _, unfinished = await asyncio.wait(
        waits, timeout=max(0.0, deadline - _monotonic()), return_when=asyncio.FIRST_COMPLETED
    )
    for wait in unfinished:
        wait.cancel()
    if len(waits) == len(unfinished):
        raise WaitForScreenTimeout(
            "Timed out while waiting for widgets to process pending messages. Still pending:\n"
            + describe_pending_pumps(pending.values())
        )
    return False if pending else None


async def press_keys(self: App[object], keys: Iterable[str]) -> None:
    """Textual 8.2.7's ``App._press_keys``, waiting for animations only while one is pending.

    Upstream probes for CPU idle, waits for every current and scheduled animation, and probes
    again, for each key. The second probe covers only what an animation's end set off, so it
    runs only when the key left one pending; a key that animates nothing costs one probe.
    """
    import unicodedata

    from textual import events
    from textual._wait import wait_for_idle
    from textual.keys import REPLACED_KEYS, _character_to_key, _get_unicode_name_from_key

    driver = self._driver
    if driver is None:
        raise RuntimeError("keys pressed before the app has a driver")
    animator = self._animator
    for key in keys:
        if key.startswith("wait:"):
            _, wait_ms = key.split(":")
            await asyncio.sleep(float(wait_ms) / 1000)
            await animator.wait_until_complete()
            continue
        if len(key) == 1 and not key.isalnum():
            key = _character_to_key(key)
        original_key = REPLACED_KEYS.get(key, key)
        char: str | None
        try:
            char = unicodedata.lookup(_get_unicode_name_from_key(original_key))
        except KeyError:
            char = key if len(key) == 1 else None
        key_event = events.Key(key, char)
        key_event.set_sender(self)
        driver.send_message(key_event)
        await wait_for_idle(0)
        if not animator._complete_event.is_set():
            await animator.wait_until_complete()
            await wait_for_idle(0)


_IDLE_PROBE_SECONDS = 0.005
"""One round of Textual's CPU-idle probe, which calls the process idle once a round burns
under 1 ms of CPU time; upstream sleeps 20 ms a round, and every key press and bare
``pause()`` takes at least one. Windows keeps upstream's: it counts process time in 15.6 ms
ticks, and its loop sleeps at that resolution anyway."""


def install_settled_wait_for_screen(monkeypatch: pytest.MonkeyPatch) -> bool:
    """Swap in the settled wait and key presses on the pinned Textual; False leaves an unpinned
    install untouched."""
    import textual
    import textual._wait
    from textual.app import App
    from textual.pilot import Pilot

    if textual.__version__ != _SETTLED_TEXTUAL_VERSION:
        return False
    _park()
    monkeypatch.setattr(Pilot, "_wait_for_screen", settled_wait_for_screen)
    monkeypatch.setattr(App, "_press_keys", press_keys)
    if sys.platform != "win32":
        monkeypatch.setattr(textual._wait, "SLEEP_GRANULARITY", _IDLE_PROBE_SECONDS)
    return True
