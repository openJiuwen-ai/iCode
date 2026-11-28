# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline trajectory health of a session: ``<session>/trajectory/health.json``.

The user sees why a session records no trajectory in a warning, but a caller
that launches iCode with a prepared session has no warning to read before the
session's first prompt, and no supported way to tell "another live writer has
this session" from "this session can never record". This file is that answer:
a small owner-only JSON document rewritten atomically at every activation
verdict, so a reader outside the process sees the current state and its
machine-readable cause.

``state`` is ``recovering`` once a run has the writer lease and is still
bringing its writer up, ``healthy`` once that writer runs, and ``degraded`` for
every reason recording was disabled (``reason`` then carries the same value the
warning reports, and ``detail`` what is known about the cause — the process
holding the session, say).

The newest verdict wins, including one from a run that was refused the lease:
such a run writes ``degraded``/``lease_held`` naming the holder, and the holder
does not write again, so during a takeover the file says "this session is held
elsewhere" rather than "it is recording". That is the answer a caller starting
a prepared session needs, and ``pid`` and ``updatedAt`` name the run that
wrote the verdict; the file says what that run decided, never whether its
process is still alive.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Final

from chrys.foundation.platform.files import SecureFileError, atomic_write_owner_only_bytes
from chrys.foundation.trajectory.envelope import utc_now_rfc3339

logger = logging.getLogger(__name__)

HEALTH_FILE_NAME: Final = "health.json"
HEALTH_SCHEMA_VERSION: Final = 1

_TRAJECTORY_DIR_NAME: Final = "trajectory"


class TrajectoryHealthState:
    """The values ``state`` takes; never persisted anywhere else."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"


def trajectory_health_path(session_dir: Path) -> Path:
    return session_dir / _TRAJECTORY_DIR_NAME / HEALTH_FILE_NAME


def write_health(session_dir: Path, *, session_id: str, state: str, reason: str = "", detail: str = "") -> None:
    """Record the session's current trajectory health; never raises.

    The trajectory folder must already exist: this is a record *about* the
    folder's activation, never a reason to create it, and a folder this user
    cannot write is exactly the ``permission`` case where no record can exist.
    """
    payload = (
        json.dumps(
            {
                "schemaVersion": HEALTH_SCHEMA_VERSION,
                "sessionId": session_id,
                "pid": os.getpid(),
                "state": state,
                "reason": reason,
                "detail": detail,
                "updatedAt": utc_now_rfc3339(),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        + "\n"
    )
    try:
        # ``create_parents=False``: the folder's absence is the ``permission``
        # case, and recreating it here would undo the very refusal being recorded.
        atomic_write_owner_only_bytes(
            trajectory_health_path(session_dir),
            payload.encode("utf-8", errors="backslashreplace"),
            create_parents=False,
        )
    except SecureFileError, OSError:
        # A health record nobody can write is not a reason to fail a session.
        logger.debug("Unable to record trajectory health for session %s", session_id, exc_info=True)
