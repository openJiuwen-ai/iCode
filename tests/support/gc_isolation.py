# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep a timed test window from paying for the heap earlier tests left behind.

An xdist worker keeps whatever every earlier test left alive. A full
collection that starts inside a test walks all of it: tens of milliseconds
locally, but up to ten seconds on a loaded macOS runner. That is enough to fail
a 5 s budget or an "it returned promptly" bound, however fast the code under
test was. Objects frozen with :func:`gc.freeze` are skipped by every
collection, so a collection inside the block walks only what the block itself
allocated.
"""

from __future__ import annotations

import gc
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def earlier_tests_heap_frozen(*, collect_first: bool = False) -> Iterator[None]:
    """Freeze every object alive at entry until the block exits."""
    if collect_first:
        # Reclaim discarded widget trees before freezing survivors across a TUI session.
        gc.collect()
    gc.freeze()
    try:
        yield
    finally:
        gc.unfreeze()
