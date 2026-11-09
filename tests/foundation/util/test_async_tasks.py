# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for shared asyncio task-boundary helpers."""

from __future__ import annotations

import asyncio

import pytest

from chrys.foundation.util.async_tasks import await_task_quiescence
from chrys.foundation.util.once_close import finish_close


@pytest.mark.asyncio
async def test_await_task_quiescence_tolerates_captured_task_cancellation() -> None:
    task = asyncio.create_task(asyncio.Event().wait())
    task.cancel()

    await await_task_quiescence(task)


@pytest.mark.asyncio
async def test_await_task_quiescence_propagates_waiter_cancellation() -> None:
    captured = asyncio.create_task(asyncio.Event().wait())
    waiter = asyncio.create_task(await_task_quiescence(captured))
    await asyncio.sleep(0)

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    assert captured.done() is False
    captured.cancel()
    await asyncio.gather(captured, return_exceptions=True)


@pytest.mark.parametrize("saved", [False, True])
async def test_finish_close_returns_the_owned_tasks_result(saved: bool) -> None:
    async def save() -> bool:
        return saved

    task = asyncio.create_task(save())
    assert await finish_close(task) is saved
    assert task.done()
