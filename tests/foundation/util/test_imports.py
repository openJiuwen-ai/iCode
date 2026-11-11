# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for importing modules off the event loop."""

from __future__ import annotations

import importlib
import sys
import threading
import types
from importlib.machinery import ModuleSpec

import pytest

from chrys.foundation.util import imports
from chrys.foundation.util.imports import import_off_loop

_PROBE = "_chrys_import_off_loop_probe"


@pytest.fixture
def probe_module(tmp_path, monkeypatch) -> str:
    """A module on ``sys.path`` that records the thread importing it, removed again at teardown."""
    (tmp_path / f"{_PROBE}.py").write_text(
        "import threading\nIMPORTED_ON = threading.get_ident()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    # Recorded present, then removed: teardown deletes whatever the import leaves.
    monkeypatch.setitem(sys.modules, _PROBE, None)
    monkeypatch.delitem(sys.modules, _PROBE)
    return _PROBE


@pytest.mark.asyncio
async def test_a_cold_module_is_imported_on_a_worker_thread(probe_module) -> None:
    await import_off_loop("json", probe_module)

    assert threading.get_ident() != sys.modules[probe_module].IMPORTED_ON


@pytest.mark.asyncio
async def test_a_failed_import_raises(probe_module) -> None:
    with pytest.raises(ModuleNotFoundError):
        await import_off_loop(probe_module, "_chrys_no_such_module")


def test_imported_modules_return_without_suspending() -> None:
    coroutine = import_off_loop("json", "asyncio")

    # Finishing at the first send means no await ran: no thread hop for loaded modules.
    with pytest.raises(StopIteration):
        coroutine.send(None)


@pytest.mark.asyncio
async def test_a_module_another_thread_is_still_importing_is_awaited_off_the_loop(monkeypatch) -> None:
    # It is in sys.modules already, and importing it on the loop would wait there for that thread.
    module = types.ModuleType(_PROBE)
    module.__spec__ = ModuleSpec(_PROBE, None)
    module.__spec__._initializing = True
    monkeypatch.setitem(sys.modules, _PROBE, module)
    importing_threads: list[int] = []
    monkeypatch.setattr(imports, "_import_all", lambda names: importing_threads.append(threading.get_ident()))

    await import_off_loop("json", _PROBE)

    assert len(importing_threads) == 1
    assert importing_threads[0] != threading.get_ident()
