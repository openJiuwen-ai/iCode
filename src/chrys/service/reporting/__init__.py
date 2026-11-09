# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Content collectors for the ``report/1.0`` fact channel.

The transport lives in ``chrys.foundation.reporting``; this package knows what a
trajectory, a snapshot, a workspace and an agent configuration are. Importing it
costs nothing heavy: the queue, the sender and ``httpx`` are reached through the
runtime module when reporting is actually enabled.
"""

from __future__ import annotations

from chrys.service.reporting.hooks import active_reporting, publish_session_meta
from chrys.service.reporting.meta import ReportMeta
from chrys.service.reporting.startup import arm_reporting, start_armed_reporting, stop_reporting

__all__ = [
    "ReportMeta",
    "active_reporting",
    "arm_reporting",
    "publish_session_meta",
    "start_armed_reporting",
    "stop_reporting",
]
