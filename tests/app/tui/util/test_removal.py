# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A shielded removal runs to its end whatever happens to the task that awaits it."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from chrys.app.tui.util.removal import finish_shielded, remove_all_shielded
from tests.support.waiting import wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import Awaitable


class _Operation:
    """An operation that starts, then waits for the test to let it finish."""

    def __init__(self, error: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False
        self._error = error

    async def run(self) -> None:
        self.started.set()
        await self.release.wait()
        self.finished = True
        if self._error is not None:
            raise self._error


class _Removable:
    """A detached widget whose removal is *operation*."""

    parent = None

    def __init__(self, operation: _Operation) -> None:
        self._operation = operation

    def remove(self) -> Awaitable[None]:
        return self._operation.run()


async def _caller_inside(operation: _Operation) -> asyncio.Task[None]:
    caller = asyncio.create_task(finish_shielded(operation.run()))
    await wait_for(operation.started.is_set, description="the operation starts")
    return caller


@pytest.mark.asyncio
async def test_a_cancelled_caller_waits_for_the_operation_and_stays_cancelled() -> None:
    operation = _Operation()
    caller = await _caller_inside(operation)

    caller.cancel()
    assert not await wait_until(caller.done, timeout=0.2)
    assert not operation.finished

    operation.release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert operation.finished


@pytest.mark.asyncio
async def test_cancelling_the_caller_again_ends_its_wait_but_not_the_operation() -> None:
    operation = _Operation()
    caller = await _caller_inside(operation)

    caller.cancel()
    # One scheduler turn delivers the first cancellation; the caller is then waiting again.
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert not operation.finished

    operation.release.set()
    await wait_for(lambda: operation.finished, description="the operation finishes on its own")


@pytest.mark.asyncio
async def test_the_cancellation_outranks_an_error_from_the_operation() -> None:
    operation = _Operation(ValueError("removal failed"))
    caller = await _caller_inside(operation)

    caller.cancel()
    operation.release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert operation.finished


@pytest.mark.asyncio
async def test_a_caller_that_is_not_cancelled_sees_the_operation_error() -> None:
    operation = _Operation(ValueError("removal failed"))
    operation.release.set()

    with pytest.raises(ValueError, match="removal failed"):
        await finish_shielded(operation.run())


@pytest.mark.asyncio
async def test_a_caller_cancelled_while_the_first_widget_goes_still_removes_every_widget() -> None:
    first, second = _Operation(), _Operation()
    second.release.set()
    removables: list[Any] = [_Removable(first), _Removable(second)]
    caller = asyncio.create_task(remove_all_shielded(removables))
    await wait_for(first.started.is_set, description="the first removal starts")

    caller.cancel()
    first.release.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert (first.finished, second.finished) == (True, True)


class _SlowToLeave(Static):
    """A widget whose unmount waits for the test, as a busy machine makes every removal wait."""

    def __init__(self, release: asyncio.Event) -> None:
        super().__init__("row")
        self.leaving = False
        self._release = release

    async def on_unmount(self) -> None:
        self.leaving = True
        await self._release.wait()


@pytest.mark.asyncio
async def test_siblings_leave_together_so_their_waits_do_not_add_up() -> None:
    release = asyncio.Event()
    rows = [_SlowToLeave(release) for _ in range(3)]

    class _Rows(App[None]):
        def compose(self) -> ComposeResult:
            yield from rows

    async with _Rows().run_test():
        removal = asyncio.create_task(remove_all_shielded(rows))
        try:
            await wait_for(lambda: all(row.leaving for row in rows), description="every row leaves before any finishes")
        finally:
            release.set()
            await removal
        assert not any(row.is_attached for row in rows)
