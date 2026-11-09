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
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from chrys.kernel.aixcoding.auth import AccountInfo


@pytest.fixture(autouse=True)
def _reset_catalog_sync_singleton() -> None:
    """Stop the poll singleton and clear the shared config around every test."""
    from chrys.service.aixcoding.config import load_config
    from chrys.service.aixcoding.models import catalog

    load_config.cache_clear()
    yield
    catalog.stop_sync_runtime()
    with catalog._runtime_lock:
        catalog._runtime = None
        catalog._last_wiring = None
    load_config.cache_clear()


@pytest.fixture(autouse=True)
def _verified_login_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pre-verification defaults to a verified login (catalog-auth-gating).

    Keeps the pre-existing suite focused on the clock/publish semantics; the
    dedicated gating cases override the verify outcome (or the whole session)
    themselves. The env credential is cleared so the store path is exercised.
    """
    session = SimpleNamespace(
        stored_token="t",
        stored_user_id="u",  # F3: without it _catalog_user_id() degrades to a WARNING + empty userId
        verify_live_credential=AsyncMock(return_value=AccountInfo()),
    )
    monkeypatch.setattr("chrys.kernel.aixcoding.auth.get_login_session", lambda: session)
    monkeypatch.delenv("AIXCODING_MODEL_CATALOG_TOKEN", raising=False)
    monkeypatch.delenv("AIXCODING_ENVIRONMENT", raising=False)


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
