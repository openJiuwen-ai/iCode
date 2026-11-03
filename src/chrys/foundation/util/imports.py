# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Importing a large package without stalling the event loop."""

from __future__ import annotations

import asyncio
import importlib
import sys


async def import_off_loop(*names: str) -> None:
    """Import the modules *names* on a worker thread unless every one is already imported.

    An import runs to its end on the thread that starts it. A cold import of a
    large package (a provider SDK is over a thousand modules, seconds on a
    slow Windows disk) would hold the event loop all that time, and the TUI
    would stop painting, its loading animation with it. On a worker thread the
    loop keeps running between the import's turns at the interpreter lock, and
    the caller's own import statement then finds the module loaded. A failed
    import raises here, as it would at that statement.
    """
    if all(_imported(name) for name in names):
        return
    await asyncio.to_thread(_import_all, names)


def _imported(name: str) -> bool:
    """Whether *name* is in ``sys.modules`` and no thread is still running its import."""
    module = sys.modules.get(name)
    # importlib's own test: a module whose import another thread is still
    # running is already in sys.modules, and importing it would wait for that.
    return module is not None and not getattr(getattr(module, "__spec__", None), "_initializing", False)


def _import_all(names: tuple[str, ...]) -> None:
    for name in names:
        importlib.import_module(name)
