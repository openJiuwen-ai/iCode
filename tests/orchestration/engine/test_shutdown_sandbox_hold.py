# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""An engine returns its claim on the bundled sandbox server however its shutdown ends."""

from __future__ import annotations

import asyncio

import pytest

from chrys.service.llm.mock import MockResponse
from chrys.service.sandbox import runtime_hold, supervisor
from chrys.service.sandbox.runtime_hold import RuntimeHold
from tests.orchestration.invoker._build_fixtures import build_recipe_engine
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, wait_for, with_wait_deadline


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_a_shutdown_cancelled_in_the_workflow_drain_still_returns_the_sandbox_claim(
    tmp_path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    # Engines other tests in this worker prepared and never shut down still count.
    monkeypatch.setattr(runtime_hold, "_runtimes", 0)
    stops: list[str] = []

    async def shutdown() -> None:
        stops.append("shutdown")

    monkeypatch.setattr(supervisor, "shutdown", shutdown)
    first, _main, _child = await build_recipe_engine(
        agent_engine, monkeypatch, tmp_path, main=[MockResponse(text="unused")], child=[]
    )
    other = RuntimeHold()
    other.acquire()
    assert runtime_hold._runtimes == 2
    entered = asyncio.Event()
    workflows_shutdown = first._workflows.shutdown

    async def held_workflows_shutdown() -> None:
        # Held only the first time: the fixture's own shutdown at teardown goes through.
        monkeypatch.setattr(first._workflows, "shutdown", workflows_shutdown)
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(first._workflows, "shutdown", held_workflows_shutdown)
    stopping = asyncio.create_task(first.shutdown())
    await wait_for(lambda: entered.is_set() or stopping.done(), description="the workflow drain under way")
    assert not stopping.done()
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert first._release_task is None
    assert runtime_hold._runtimes == 1
    await other.release()
    assert runtime_hold._runtimes == 0
    assert stops == ["shutdown"]
