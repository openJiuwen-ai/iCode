# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cancel-safe, run-once close.

A close runs as ONE task: the first caller creates it and every later caller
awaits that same task. A waiter's cancellation never reaches the task, so the
resource is released even when every caller gives up waiting.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable


async def finish_close[T](work: asyncio.Future[T]) -> T:
    """Drain owned cleanup work even after repeated waiter cancellation."""
    cancelled: asyncio.CancelledError | None = None
    while not work.done():
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError as exc:
            cancelled = exc
    result = work.result()
    if cancelled is not None:
        raise cancelled
    return result


class OnceClose:
    """Run *close* at most once; concurrent and later callers share its task."""

    def __init__(self, close: Callable[[], Awaitable[None]]) -> None:
        self._close = close
        self._task: asyncio.Task[None] | None = None

    @property
    def started(self) -> bool:
        return self._task is not None

    async def _run(self) -> None:
        await self._close()

    async def __call__(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
        await finish_close(self._task)
