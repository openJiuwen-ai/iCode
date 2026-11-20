# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own background test tasks and observe their state without racing timeout callbacks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from typing import Any

from tests.support.waiting import wait_for


@asynccontextmanager
async def running_task[T](request: Coroutine[Any, Any, T]) -> AsyncIterator[asyncio.Task[T]]:
    """Cancel and drain a request even when readiness or a test assertion fails."""
    task = asyncio.create_task(request)
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def wait_for_started(started: asyncio.Event, task: asyncio.Task[Any], *, description: str) -> None:
    """Observe the signal itself, or surface a request that ended before signalling.

    After a busy event-loop turn, an Event.wait() wakeup can lose to an overdue
    asyncio.wait_for() timeout even though the event is already set. The shared
    wait checks the actual state first and supplies the suite's wait budget.
    """
    await wait_for(lambda: started.is_set() or task.done(), description=description)
    if task.done():
        await task
    assert started.is_set(), f"request ended before {description}"


async def wait_for_completion[T](request: Coroutine[Any, Any, T] | asyncio.Task[T]) -> T:
    """Wait for completion with the shared budget, preserving the request's error."""
    task = asyncio.ensure_future(request)
    try:
        await wait_for(task.done, description="background task completion")
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
