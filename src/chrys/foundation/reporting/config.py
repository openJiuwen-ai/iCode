# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings projection for the reporting subsystem.

Everything here is a pure read of :class:`~chrys.foundation.config.settings.Settings`;
nothing touches the filesystem, opens the queue or starts a thread, which is
what lets bootstrap register the loader without paying for it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from chrys.foundation.platform import get_platform

if TYPE_CHECKING:
    from chrys.foundation.config.settings import Settings

REPORTING_DIR_NAME = "reporting"

BATCH_ENDPOINT_PATH = "/v1/batch"
"""Relative path of the batch endpoint under ``report.endpoint``."""

MANIFEST_ENDPOINT_TEMPLATE = "/v1/session/{session_id}/manifest"
"""Relative path of the missing-block negotiation endpoint."""


@dataclass(frozen=True, slots=True)
class ReportConfig:
    """The effective reporting configuration for this process."""

    enabled: bool
    endpoint: str
    token: str
    upload_timeout_seconds: float
    batch_max_bytes: int
    poll_interval_seconds: float
    queue_bytes_max: int
    workspace_capture: bool
    agent_config: bool
    diagnostics: bool
    directory: Path

    @property
    def deliverable(self) -> bool:
        """Whether there is anywhere to send to.

        ``report.enabled`` without an endpoint still collects into the queue
        (diagnostics keep counting), but a pump with no base URL can only
        back off, so the runtime starts without one.
        """
        return self.enabled and bool(self.endpoint.strip())

    def url(self, path: str) -> str:
        """Join the configured base URL with an endpoint *path*."""
        return f"{self.endpoint.rstrip('/')}{path}"

    @classmethod
    def from_settings(cls, settings: Settings) -> ReportConfig:
        """Project the ``report.*`` settings group."""
        return cls(
            enabled=settings.report_enabled,
            endpoint=settings.report_endpoint.strip(),
            token=settings.report_token.strip(),
            upload_timeout_seconds=max(1.0, settings.report_upload_timeout_ms / 1000.0),
            batch_max_bytes=max(64 * 1024, settings.report_batch_max_bytes),
            poll_interval_seconds=max(1.0, settings.report_poll_interval_ms / 1000.0),
            queue_bytes_max=max(1 << 20, settings.report_queue_bytes_max),
            workspace_capture=settings.report_workspace_capture,
            agent_config=settings.report_agent_config,
            diagnostics=settings.report_diagnostics,
            directory=Path(get_platform().config_dir) / REPORTING_DIR_NAME,
        )
