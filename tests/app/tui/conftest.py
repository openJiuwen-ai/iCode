# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for TUI tests."""

from __future__ import annotations

import gc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal

import pytest
from textual.app import App

from chrys.app.tui import app as chrys_app
from chrys.app.tui.support import gc_freeze
from chrys.app.tui.theme import TUI_VARIABLE_DEFAULTS, with_tui_css_variables


@pytest.fixture(autouse=True)
def prevent_theme_preference_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevent TUI tests from writing the developer's real theme preference."""
    monkeypatch.setattr("chrys.app.tui.app.persist_theme", lambda _theme: None)


@pytest.fixture(autouse=True)
def provide_tui_variable_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make lightweight Textual test apps resolve Chrys-specific CSS slots.

    Production always hosts these widgets in ``ChrysApp``, which provides the
    same defaults. Most focused widget tests intentionally use a bare ``App``
    to avoid constructing the full application shell.
    """
    original = App.get_theme_variable_defaults
    original_get_css_variables = App.get_css_variables

    def _get_theme_variable_defaults(app: App) -> dict[str, str]:
        return {**original(app), **TUI_VARIABLE_DEFAULTS}

    def _get_css_variables(app: App) -> dict[str, str]:
        variables = with_tui_css_variables(app.current_theme, original_get_css_variables(app))
        app.theme_variables = variables
        return variables

    monkeypatch.setattr(App, "get_theme_variable_defaults", _get_theme_variable_defaults)
    monkeypatch.setattr(App, "get_css_variables", _get_css_variables)


@pytest.fixture(autouse=True)
def isolate_gc_freeze_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep process-global permanent-generation state isolated per TUI test.

    The unfreeze+collect repair pair only runs when frozen state actually
    exists: ``gc.collect()`` walks the whole tracked heap (tens of ms on a
    loaded test worker), so paying it unconditionally taxes every TUI test
    while ~99% of them never freeze.  ``gc.get_freeze_count()`` is O(frozen
    objects) — free when nothing is frozen — which keeps the clean path cheap
    while still asserting the zero-frozen baseline.
    """
    if gc.get_freeze_count():
        gc.unfreeze()
        gc.collect()
    assert gc.get_freeze_count() == 0
    assert gc_freeze._enabled_owner is None
    # Production defaults on. Flipped here so only the tests that pass
    # ``gc_freeze_enabled=True`` mutate process-global permanent-generation
    # state; patched where ``app.py`` reads it, since it imports the name.
    monkeypatch.setattr(chrys_app, "GC_FREEZE_ENABLED", False)

    yield

    leaked_owner = gc_freeze._enabled_owner
    if leaked_owner is not None:
        leaked_owner.close()
    if leaked_owner is not None or gc.get_freeze_count():
        gc.unfreeze()
        gc.collect()
    assert leaked_owner is None
    assert gc_freeze._enabled_owner is None
    assert gc.get_freeze_count() == 0


@pytest.fixture
def frozen_earlier_heap() -> Iterator[None]:
    """Keep what earlier tests in this worker left alive out of this test's full collections.

    A transcript replay ends its GC pause with one full ``gc.collect()``. That pass walks every
    tracked object in the process, so its cost grows with the worker's whole history, not with
    the replay: a quarter second in a long-lived worker, seconds on a loaded runner. Frozen,
    that heap is skipped and the pass walks only what this test allocated.

    Opt in with ``pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")`` in modules that
    replay transcripts; never in one that exercises the GC freeze or the scroll GC pause, which
    count frozen objects and collections themselves.
    """
    gc.freeze()
    yield
    gc.unfreeze()


@pytest.fixture
def buddy_reply_gate_left_open() -> Iterator[None]:
    """Fail the test that leaves the process-wide buddy reply gate held, instead of the ones that run after it."""
    from tests.support.buddies import assert_reply_gate_open

    yield
    assert_reply_gate_open()


@pytest.fixture
def fake_platform(tmp_path: Path) -> Callable[..., object]:
    """Build a ``PlatformInfo``-shaped fake covering attributes read by mounted TUI panels.

    Mirrors the public surface of :class:`chrys.foundation.platform.PlatformInfo`
    that any agent-config tab (Skills, Memory, …) may touch during compose —
    notably the ``is_macos`` / ``is_windows`` / ``is_linux`` booleans used for
    platform-specific placeholders.  Returns a factory so callers can override
    ``config_dir`` (defaults to ``tmp_path``) and ``os_name`` per test.
    """

    def _make(
        *,
        config_dir: Path | None = None,
        os_name: Literal["macos", "windows", "linux"] = "macos",
    ) -> object:
        cfg = config_dir if config_dir is not None else tmp_path
        return type(
            "P",
            (),
            {
                "config_dir": cfg,
                "data_dir": cfg,
                "os_name": os_name,
                "is_macos": os_name == "macos",
                "is_windows": os_name == "windows",
                "is_linux": os_name == "linux",
            },
        )()

    return _make
