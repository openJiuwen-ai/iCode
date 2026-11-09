# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The process-wide reporting handle, and the save-path entry point.

``service/session/persistence.py`` calls :func:`publish_session_meta` once per
successful save. With reporting disabled (the default) that call is one global
read and a return: no import, no allocation, no ``await``. With it enabled the
call still only stores an immutable row and wakes the collector, because the
save is a user-visible path and reporting may never delay it.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from chrys.service.reporting.meta import ReportMeta
    from chrys.service.reporting.service import ReportingService

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_ACTIVE: ReportingService | None = None


def install_reporting(service: ReportingService) -> None:
    """Make *service* the process's reporting instance."""
    global _ACTIVE
    with _LOCK:
        _ACTIVE = service


def active_reporting() -> ReportingService | None:
    """The installed instance, or ``None`` when reporting is off."""
    return _ACTIVE


def clear_reporting() -> ReportingService | None:
    """Detach and return the installed instance."""
    global _ACTIVE
    with _LOCK:
        service = _ACTIVE
        _ACTIVE = None
    return service


def publish_session_meta(meta: ReportMeta) -> None:
    """Hand the save hook's immutable row to the collector.

    Never raises and never blocks: the save path is not reporting's to break.
    """
    service = _ACTIVE
    if service is None:
        return
    try:
        service.offer_meta(meta)
    except Exception:
        logger.warning("Reporting rejected a session meta row", exc_info=True)
