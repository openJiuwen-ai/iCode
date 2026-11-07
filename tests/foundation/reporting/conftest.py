# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from chrys.foundation.reporting.config import ReportConfig
from chrys.foundation.reporting.diagnostics import ReportDiagnostics
from chrys.foundation.reporting.wal import DurableWAL


@pytest.fixture
def report_config(tmp_path: Path) -> ReportConfig:
    """A deliverable configuration pointing at a port nothing listens on."""
    return ReportConfig(
        enabled=True,
        endpoint="http://127.0.0.1:9",
        token="test-token",
        upload_timeout_seconds=1.0,
        batch_max_bytes=8 << 20,
        poll_interval_seconds=1.0,
        queue_bytes_max=4 << 20,
        workspace_capture=False,
        agent_config=False,
        diagnostics=False,
        directory=tmp_path / "reporting",
    )


@pytest.fixture
def report_diagnostics() -> ReportDiagnostics:
    return ReportDiagnostics()


@pytest.fixture
def wal(tmp_path: Path, report_diagnostics: ReportDiagnostics) -> Iterator[DurableWAL]:
    queue = DurableWAL(tmp_path / "reporting", queue_bytes_max=4 << 20, diagnostics=report_diagnostics)
    queue.open()
    try:
        yield queue
    finally:
        queue.close()
