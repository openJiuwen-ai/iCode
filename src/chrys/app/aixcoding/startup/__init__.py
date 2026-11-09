# ruff: noqa: T201
# (stderr notes are the observable-degradation channel, DEVELOPMENT.md §9)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Startup aggregation entry for AIxCoding customizations.

``app/aixcoding`` is the host-seam package: app is the only tier that can
aggregate startup tasks from every lower tier without a ceiling.
``chrys.app.cli.app:main`` is the sole caller; new tasks are appended to
``_TASKS`` instead of re-wiring upstream.

Each task is idempotent and fail-safe on its own. This entry adds per-task
containment so adding a task cannot break CLI startup.
"""

from __future__ import annotations

import sys

from .tasks import _configure_environment, _ensure_file_sink

# Startup tasks in order; each must stay idempotent + individually fail-safe.
_TASKS = (_configure_environment, _ensure_file_sink)


def aixcoding_startup() -> None:
    """Run every customization startup task; per-task fail-safe, idempotent."""
    for task in _TASKS:
        try:
            task()
        except Exception as exc:
            print(f"aixcoding.startup: task {task.__name__} failed ({exc}); skipped", file=sys.stderr)
