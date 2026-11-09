# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from chrys.foundation.config.settings import Settings
from chrys.foundation.platform import get_platform
from chrys.foundation.reporting.config import REPORTING_DIR_NAME, ReportConfig


def test_the_projection_reads_the_report_settings_group() -> None:
    settings = replace(
        Settings(),
        report_enabled=True,
        report_endpoint="http://report.example.test/",
        report_token=" secret ",
        report_upload_timeout_ms=2_500,
        report_batch_max_bytes=1 << 20,
        report_poll_interval_ms=1_500,
        report_queue_bytes_max=1 << 21,
        report_workspace_capture=False,
        report_agent_config=False,
        report_diagnostics=False,
    )

    config = ReportConfig.from_settings(settings)

    assert config.enabled is True
    assert config.deliverable is True
    assert config.endpoint == "http://report.example.test/"
    assert config.token == "secret"
    assert config.upload_timeout_seconds == 2.5
    assert config.poll_interval_seconds == 1.5
    assert config.batch_max_bytes == 1 << 20
    assert config.queue_bytes_max == 1 << 21
    assert config.workspace_capture is False
    assert config.agent_config is False
    assert config.diagnostics is False
    assert config.directory == Path(get_platform().config_dir) / REPORTING_DIR_NAME


def test_the_endpoint_is_joined_without_doubling_the_separator() -> None:
    config = replace(
        ReportConfig.from_settings(Settings()),
        enabled=True,
        endpoint="http://report.example.test/base/",
    )

    assert config.url("/v1/batch") == "http://report.example.test/base/v1/batch"


def test_an_enabled_process_without_an_endpoint_is_not_deliverable() -> None:
    config = replace(ReportConfig.from_settings(Settings()), enabled=True, endpoint="   ")

    assert config.deliverable is False


def test_reporting_is_off_by_default() -> None:
    config = ReportConfig.from_settings(Settings())

    assert config.enabled is False
    assert config.deliverable is False
