# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Drive the real ACP idle watchdog without timing subprocess scheduling."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import ModuleType
from typing import Any

import pytest

from chrys.service.acp_client import AcpAgentClient
from chrys.service.acp_client import client as client_mod
from tests.support.waiting import wait_for


class IdleWatchdogClock:
    """Freeze only the client's clock and advance its real watchdog timers on demand.

    Install after connect/open_session so handshake deadlines stay on real time.
    Other tasks' timeout contexts, the SDK, and the event loop remain untouched.
    """

    def __init__(self, client: AcpAgentClient, monkeypatch: pytest.MonkeyPatch) -> None:
        self._loop = asyncio.get_running_loop()
        self._now = self._loop.time()
        self._deadlines: dict[asyncio.Timeout, float] = {}
        self._waits = 0
        self.watchdog: asyncio.Task[None] | None = None
        activity = client._activity_event
        assert activity is not None
        self._activity = activity
        original_wait = activity.wait
        original_start = client._start_idle_watchdog

        async def wait() -> bool:
            self._waits += 1
            return await original_wait()

        def start() -> asyncio.Task[None] | None:
            self.watchdog = original_start()
            return self.watchdog

        shadow = ModuleType("asyncio")
        shadow.__dict__.update(vars(asyncio), get_running_loop=lambda: self, timeout=self._timeout)
        monkeypatch.setattr(client_mod, "asyncio", shadow)
        monkeypatch.setattr(activity, "wait", wait)
        monkeypatch.setattr(client, "_start_idle_watchdog", start)

    def time(self) -> float:
        return self._now

    def create_future(self) -> asyncio.Future[Any]:
        return self._loop.create_future()

    @asynccontextmanager
    async def _timeout(self, delay: float | None) -> AsyncIterator[None]:
        if asyncio.current_task() is not self.watchdog or delay is None:
            async with asyncio.timeout(delay):
                yield
            return
        async with asyncio.timeout(None) as timer:
            self._deadlines[timer] = self._now + delay
            try:
                yield
            finally:
                del self._deadlines[timer]

    @property
    def armed(self) -> bool:
        """Whether the watchdog is waiting on an idle timer."""
        return self.watchdog is not None and bool(self._deadlines)

    async def _settle_activity(self, watchdog: asyncio.Task[None]) -> None:
        # A callback can enter before the watchdog handles its activity wake.
        # Let it take that wake before moving the clock, so its old timer cannot
        # race a human wait that the watchdog has not inspected yet. Only real
        # activity wakes it: the watchdog restarts its idle clock on a wake.
        if self._activity.is_set():
            waits = self._waits
            await wait_for(
                lambda: self._waits > waits or watchdog.done(),
                description="idle watchdog handled pending activity",
            )

    def stall_in_step(self, seconds: float) -> None:
        """Move the clock with no wake, as a stall inside the watchdog's own synchronous step does."""
        self._now += seconds

    async def stall(self, seconds: float) -> None:
        """Jump the clock as a loop stalled for *seconds* resumes: every due timer fires once, late."""
        watchdog = self.watchdog
        assert watchdog is not None
        await self._settle_activity(watchdog)
        waits = self._waits
        self._now += seconds
        for timer, deadline in list(self._deadlines.items()):
            if deadline <= self._now:
                timer.reschedule(self._loop.time())
        await wait_for(
            lambda: self._waits > waits or watchdog.done(),
            description="idle watchdog woke after the stall",
        )
        if watchdog.done():
            await watchdog

    async def advance(self, seconds: float) -> None:
        """Let pending activity settle, then cross deadlines and observe the next watchdog wait or exit."""
        watchdog = self.watchdog
        assert watchdog is not None
        await self._settle_activity(watchdog)
        target = self._now + seconds
        # Cross each watchdog deadline on time, as a responsive loop would: one
        # jump past a slice deadline is what a stalled loop looks like, and the
        # watchdog gives that lateness back to the agent.
        while not watchdog.done():
            waits = self._waits
            due = [deadline for deadline in self._deadlines.values() if deadline <= target]
            if due:
                self._now = min(due)
                for timer, deadline in list(self._deadlines.items()):
                    if deadline <= self._now:
                        timer.reschedule(self._loop.time())
            elif self._deadlines:
                # The next deadline lies beyond this advance.
                self._now = max(self._now, target)
                break
            else:
                # A human wait has no timer. Wake it so the real watchdog
                # must recheck its suspension even after the idle budget.
                self._now = max(self._now, target)
                self._activity.set()
            await wait_for(
                lambda waits=waits: self._waits > waits or watchdog.done(),
                description="idle watchdog observed advanced time",
            )
            if not due:
                break
        self._now = max(self._now, target)
        if watchdog.done():
            await watchdog
