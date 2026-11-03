# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for the aixcoding catalog tests.

Carries a local ``wait_until`` equivalent (ADR-0001 keeps this tree from
sharing a failure domain with ``tests/support``) and resets the catalog
poll singleton after every test: the runtime is process-wide state, and one
leaked handle would keep fetching through the rest of this suite's clock
assertions.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_catalog_sync_singleton() -> None:
    """Stop and fully reset the poll singleton around every test."""
    from chrys.service.aixcoding import catalog

    yield
    catalog.stop_sync_runtime()
    with catalog._runtime_lock:
        catalog._runtime = None
        catalog._last_wiring = None
    catalog._warned_retired_env.clear()


async def wait_until(
    predicate: Callable[[], object],
    *,
    timeout: float = 10.0,
    interval: float = 0.02,
    pilot: Any | None = None,
) -> bool:
    """Return whether *predicate* became truthy before the deadline.

    With a ``pilot`` the loop pumps the Textual event loop (``pause()``);
    without one it sleeps, so plain-asyncio callers work too. Polling avoids
    scheduler races seen on loaded Windows CI workers, where a single
    ``asyncio.sleep(0)`` may return before a fire-and-forget task runs.
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if pilot is not None:
            await pilot.pause()
        else:
            await asyncio.sleep(interval)
        if time.monotonic() >= deadline:
            return False
