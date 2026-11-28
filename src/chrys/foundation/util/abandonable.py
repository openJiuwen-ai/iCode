# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run read-only work off the event loop without making exit wait for it."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


async def run_abandonable[**P, T](name: str, fn: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Let cancellation or exit abandon a read without joining its thread.

    Neither asyncio's default executor nor ThreadPoolExecutor(wait=False)
    suffices: both join running work at shutdown. Use this only for work that
    reads; the event loop alone owns any state the result updates.
    """
    loop = asyncio.get_running_loop()
    answer: asyncio.Future[T] = loop.create_future()

    def deliver(outcome: Callable[[], None]) -> None:
        if not answer.done():
            outcome()

    def work() -> None:
        outcome: Callable[[], None]
        try:
            value = fn(*args, **kwargs)
        except Exception as exc:
            outcome = functools.partial(answer.set_exception, exc)
        else:
            outcome = functools.partial(answer.set_result, value)
        with contextlib.suppress(RuntimeError):  # The app may already have closed its loop.
            loop.call_soon_threadsafe(deliver, outcome)

    threading.Thread(target=work, name=name, daemon=True).start()
    return await answer
