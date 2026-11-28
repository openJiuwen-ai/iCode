# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The last live runtime's release stops the bundled sandbox server, and only that one."""

from __future__ import annotations

import pytest

from chrys.service.sandbox import runtime_hold, supervisor
from chrys.service.sandbox.runtime_hold import RuntimeHold


@pytest.fixture
def stops(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    # Engines other tests in this worker prepared and never shut down still count.
    monkeypatch.setattr(runtime_hold, "_runtimes", 0)
    recorded: list[str] = []

    async def shutdown() -> None:
        recorded.append("shutdown")

    monkeypatch.setattr(supervisor, "shutdown", shutdown)
    return recorded


async def test_only_the_last_release_stops_the_server(stops: list[str]) -> None:
    first, second = RuntimeHold(), RuntimeHold()
    first.acquire()
    second.acquire()
    await first.release()
    assert stops == []
    await second.release()
    assert stops == ["shutdown"]


async def test_acquire_and_release_are_idempotent(stops: list[str]) -> None:
    held, other = RuntimeHold(), RuntimeHold()
    held.acquire()
    held.acquire()
    other.acquire()
    await held.release()
    await held.release()
    assert stops == []
    await other.release()
    assert stops == ["shutdown"]


async def test_a_hold_never_acquired_releases_nothing(stops: list[str]) -> None:
    live, idle = RuntimeHold(), RuntimeHold()
    live.acquire()
    await idle.release()
    assert stops == []
    await live.release()
    assert stops == ["shutdown"]
