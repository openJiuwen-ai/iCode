# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A frozen ``time.monotonic`` for the modules under test.

Text derived from elapsed time (a countdown, "after 0 seconds", a duration) drifts
when the runner stalls between the two clock readings, and a loaded CI machine
can stall for seconds (a long gen2 GC pause). Freezing the clock of every module
on the path makes such text exact. Each module's own ``time`` name is replaced by
a shadow module, never the stdlib ``time`` itself, which the event loop and
:mod:`tests.support.waiting` read.
"""

from __future__ import annotations

import time
from types import ModuleType

import pytest


class FrozenClock:
    """A monotonic clock that moves only when the test moves ``now``."""

    def __init__(self, now: float) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now


def freeze_monotonic(monkeypatch: pytest.MonkeyPatch, *modules: ModuleType, now: float = 1000.0) -> FrozenClock:
    """Give each of *modules* one shared :class:`FrozenClock` as its ``time.monotonic``, starting at *now*."""
    clock = FrozenClock(now)
    shadow = ModuleType("time")
    shadow.__dict__.update(vars(time))
    shadow.monotonic = clock.monotonic  # type: ignore[attr-defined]
    for module in modules:
        monkeypatch.setattr(module, "time", shadow)
    return clock
