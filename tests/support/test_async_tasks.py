# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fault injection for background task readiness, completion, and test-owned cleanup."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.async_tasks import running_task, wait_for_completion, wait_for_started
from tests.support.waiting import DEFAULT_WAIT_TIMEOUT, with_wait_deadline


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_a_request_ending_before_readiness_reports_its_cause(fails: bool) -> None:
    async def ends_early() -> None:
        if fails:
            raise RuntimeError("request failed before reaching the client")

    async with running_task(ends_early()) as task:
        error = RuntimeError if fails else AssertionError
        message = "request failed before reaching the client" if fails else "request ended before client ready"
        with pytest.raises(error, match=message):
            await wait_for_started(asyncio.Event(), task, description="client ready")


@pytest.mark.asyncio
async def test_a_failed_assertion_cancels_and_drains_the_pending_request() -> None:
    started = asyncio.Event()
    cleaned_up = False

    async def pending_request() -> None:
        nonlocal cleaned_up
        started.set()
        try:
            await asyncio.Future()
        finally:
            cleaned_up = True

    with pytest.raises(AssertionError, match="failed assertion"):
        async with running_task(pending_request()) as task:
            await wait_for_started(started, task, description="client ready")
            raise AssertionError("failed assertion")

    assert task.cancelled()
    assert cleaned_up


@pytest.mark.asyncio
@with_wait_deadline(0)
async def test_readiness_timeout_cleans_up_the_request() -> None:
    with pytest.raises(AssertionError, match="client ready not met"):
        async with running_task(asyncio.Event().wait()) as task:
            await wait_for_started(asyncio.Event(), task, description="client ready")

    assert task.cancelled()


@pytest.mark.asyncio
async def test_completion_observes_a_finished_request_after_a_scheduler_stall(monkeypatch: pytest.MonkeyPatch) -> None:
    loop = asyncio.get_running_loop()
    real_time = loop.time

    async def stalled_request() -> str:
        monkeypatch.setattr(loop, "time", lambda: real_time() + 2 * DEFAULT_WAIT_TIMEOUT)
        return "completed"

    assert await wait_for_completion(stalled_request()) == "completed"
