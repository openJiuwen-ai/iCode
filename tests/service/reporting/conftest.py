# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.frames import Frame, decode_frames
from chrys.foundation.reporting.wal import DurableWAL


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
