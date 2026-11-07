# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Arming, starting and stopping the reporting runtime.

``bootstrap_runtime`` calls :func:`arm_reporting`, which only decides whether
reporting is on and remembers the configuration — no queue, no thread, no
``httpx``, no WAL. The real start happens once an entry point has both an event
loop and the process's ``EventBus`` (the engine-start boundary), which is what
the design's "after the first frame" means in this codebase: a background task,
never a startup step.
"""

from __future__ import annotations

import atexit
import logging
from typing import TYPE_CHECKING

from chrys.service.reporting import hooks

if TYPE_CHECKING:
    from chrys.foundation.config.settings import Settings
    from chrys.foundation.events.bus import EventBus
    from chrys.foundation.reporting.config import ReportConfig
    from chrys.service.reporting.service import ReportingService

logger = logging.getLogger(__name__)

_ARMED: ReportConfig | None = None
_ATEXIT_REGISTERED = False


def arm_reporting(settings: Settings) -> bool:
    """Register the loader when ``report.enabled`` is set; return whether it is armed.

    Reads only the settings: with reporting off nothing in this subsystem is
    imported at all.
    """
    global _ARMED
    if _ARMED is not None:
        return True
    if not settings.report_enabled:
        return False
    from chrys.foundation.reporting.config import ReportConfig

    _ARMED = ReportConfig.from_settings(settings)
    return True


def armed_config() -> ReportConfig | None:
    """The configuration registered by :func:`arm_reporting`, if any."""
    return _ARMED


def clear_armed() -> None:
    """Forget the registration (tests and process shutdown)."""
    global _ARMED
    _ARMED = None


async def start_armed_reporting(bus: EventBus, *, settings: Settings | None = None) -> ReportingService | None:
    """Start the armed runtime once; a no-op when nothing is armed.

    Called from the engine-start boundary of each frontend, so the cost when
    reporting is off is one global read.
    """
    config = _ARMED
    if config is None:
        return None
    existing = hooks.active_reporting()
    if existing is not None:
        return existing
    from chrys.service.reporting.service import ReportingService

    service = ReportingService(bus, config, settings=settings)
    try:
        await service.start()
    except Exception:
        logger.warning("Could not start fact reporting", exc_info=True)
        return None
    hooks.install_reporting(service)
    _register_atexit()
    return service


async def stop_reporting() -> None:
    """Stop the installed runtime, if any."""
    service = hooks.clear_reporting()
    if service is None:
        return
    await service.close()


def _register_atexit() -> None:
    global _ATEXIT_REGISTERED
    if _ATEXIT_REGISTERED:
        return
    _ATEXIT_REGISTERED = True
    atexit.register(_shutdown_at_exit)


def _shutdown_at_exit() -> None:
    """Last chance at shutdown: bounded pump drain, unsent frames stay queued."""
    service = hooks.clear_reporting()
    if service is None:
        return
    try:
        service.shutdown_sync()
    except Exception:
        logger.warning("Fact reporting did not shut down cleanly", exc_info=True)
