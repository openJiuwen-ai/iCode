# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import Frame, decode_frames
from chrys.foundation.reporting.wal import DurableWAL


@pytest.fixture(autouse=True)
def clean_reporting_state() -> Iterator[None]:
    """Reset the process-wide reporting handles around every test.

    ``startup``'s armed configuration, ``hooks``' installed runtime and the
    atexit registration are process globals: without this, one test's runtime
    leaks into the next and a test's atexit hook outlives the worker.
    """
    from chrys.service.reporting import hooks, startup

    startup.clear_armed()
    hooks.clear_reporting()
    startup.reset_atexit_registration()
    yield
    service = hooks.clear_reporting()
    startup.clear_armed()
    if service is not None:
        # A test that armed a real runtime and never stopped it must not leak
        # its sender thread or hold the queue's files open into other tests.
        with contextlib.suppress(Exception):
            service.shutdown_sync(timeout=0.5)


@pytest.fixture
def queue(tmp_path: Path) -> Iterator[DurableWAL]:
    """An open queue in this test's own directory."""
    wal = DurableWAL(tmp_path / "reporting", queue_bytes_max=8 << 20, diagnostics=ReportDiagnostics())
    wal.open()
    try:
        yield wal
    finally:
        wal.close()


@pytest.fixture
def queued() -> Callable[[DurableWAL], list[Frame]]:
    """Read every frame this queue currently holds."""

    def _read(wal: DurableWAL) -> list[Frame]:
        batch = wal.next_batch(max_bytes=8 << 20)
        if batch is None:
            return []
        return decode_frames(wal.read(batch))

    return _read
