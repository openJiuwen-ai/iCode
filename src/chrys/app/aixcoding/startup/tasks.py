# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Individually fail-safe startup tasks for AIxCoding customizations."""


def _configure_environment() -> None:
    """Inject the shared deployment environment before auth sessions exist."""
    from chrys.kernel.aixcoding.auth.environments import configure_environment
    from chrys.service.aixcoding.config import load_config

    configure_environment(load_config().environment)


def _ensure_file_sink() -> None:
    """Attach the ``aixcoding`` rotating file sink (kernel diagnostics)."""
    from chrys.kernel.aixcoding.diagnostics.logging_support import ensure_file_sink
    from chrys.service.aixcoding.config import load_config

    ensure_file_sink(load_config().diagnostics)
