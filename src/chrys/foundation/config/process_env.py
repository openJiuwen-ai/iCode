# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The frozen process environment used by settings and web credentials."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


def canonical_env_name(name: str) -> str:
    """Fold an environment name to its platform identity."""
    from chrys.foundation.platform import get_platform

    return name.upper() if get_platform().is_windows else name


@dataclass(frozen=True, slots=True)
class ProcessEnvSnapshot:
    """``os.environ`` as it stood at bootstrap — the one frozen layer."""

    values: Mapping[str, str]


_snapshot: ProcessEnvSnapshot | None = None


def freeze_process_env() -> ProcessEnvSnapshot:
    """Capture the environment on the first call and reuse that snapshot for the life of the process."""
    global _snapshot
    if _snapshot is None:
        _snapshot = ProcessEnvSnapshot(values=MappingProxyType(dict(os.environ)))
    return _snapshot


def process_env_snapshot() -> ProcessEnvSnapshot | None:
    """Return the frozen snapshot, or ``None`` before bootstrap.

    ``None`` is a real state, not an error: tools and unit tests read
    settings without bootstrapping. Callers fall back to the live
    environment in that state and must not cache the fallback.
    """
    return _snapshot


def _reset_process_env_snapshot_for_tests() -> None:
    global _snapshot
    _snapshot = None
