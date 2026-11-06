# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The autouse guard that keeps forgotten redirects out of the real config dir."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.foundation.config.settings import persist_theme
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.platform import get_platform


@pytest.fixture(scope="session")
def _config_dir_a_session_fixture_sees() -> Path:
    """Read at session-setup time, which is before any test has a ``tmp_path``."""
    return get_platform().config_dir


def test_writers_that_forgot_to_redirect_land_in_the_temp_dir(tmp_path: Path) -> None:
    """The writer does not redirect anything — that is the whole point.

    The path comes from the platform singleton, and ``persist_theme``
    swallows its own failures, so without the guard this test would quietly
    rewrite the developer's ``~/.chrys`` and still pass.

    Which is why the destination is checked before anything is written: the
    one test whose job is to fail when the guard breaks must not do the damage
    it is meant to catch on its way to failing.
    """
    settings_path = user_settings_path()
    assert settings_path.is_relative_to(tmp_path)

    persist_theme("chrys-legacy")

    assert "chrys-legacy" in settings_path.read_text(encoding="utf-8")


def test_the_redirect_reaches_modules_that_imported_get_platform_by_value(tmp_path: Path) -> None:
    """Rebinding the name in the platform module would miss these importers.

    ``prompt_history``, ``workspace_mru`` and the notification drivers all do
    ``from chrys.foundation.platform import get_platform`` at module scope, so
    they hold the function object rather than the module attribute; the guard
    has to work through the cache behind it to reach them. This module's own
    import is the same shape.
    """
    assert get_platform().config_dir.is_relative_to(tmp_path)
    assert get_platform().data_dir == get_platform().config_dir


def test_clearing_the_platform_cache_cannot_escape_the_redirect(tmp_path: Path) -> None:
    """A refill has to come back redirected, not re-detected.

    ``get_platform`` is ``functools.cache``d, so anything that clears it —
    product code, another fixture, a test tearing down its own fake — would
    otherwise hand the real directory back for the rest of the test.
    """
    get_platform.cache_clear()

    assert get_platform().config_dir.is_relative_to(tmp_path)


def test_a_session_fixture_already_sees_an_isolated_config_dir(
    _config_dir_a_session_fixture_sees: Path,
    real_platform_config_dir: Path,
) -> None:
    """Collection and session/module setup run before any per-test redirect.

    The per-test guard cannot cover them — ``tmp_path`` does not exist yet — so
    the redirect is installed when this conftest is imported and merely
    *narrowed* per test.
    """
    assert _config_dir_a_session_fixture_sees != real_platform_config_dir


@pytest.mark.real_config_dir
def test_the_marker_gives_back_the_real_platform(real_platform_config_dir: Path) -> None:
    """The opt-out exists for tests that read the developer's own directory."""
    assert get_platform().config_dir == real_platform_config_dir


def test_the_session_teardown_hands_the_real_platform_back(
    real_platform_config_dir: Path,
    tmp_path: Path,
) -> None:
    """What ``pytest_unconfigure`` runs, exercised here rather than after the run.

    Under ``pytest.main()`` in a long-lived process the run returns into code
    that keeps using whatever the last redirect left behind, so the teardown
    has to hand back genuine detection — not merely stop narrowing it.
    """
    import chrys.foundation.platform as platform_mod
    from tests import conftest as harness

    try:
        harness.restore_real_platform()

        # Undone, not merely neutralized: a wrapper left installed would keep
        # answering from whatever the next redirect writes to the pin.
        assert platform_mod.detect_platform is harness._original_detect_platform
        assert get_platform().config_dir == real_platform_config_dir
    finally:
        harness._pin_config_dir(tmp_path / "platform-config")

    assert get_platform().config_dir.is_relative_to(tmp_path)


def test_a_second_run_in_the_same_process_is_armed_again(
    real_platform_config_dir: Path,
    tmp_path: Path,
) -> None:
    """``pytest.main()`` twice over: the module-level arming happens only once.

    The teardown above is what makes this necessary — it deliberately gives the
    real platform back — and the conftest is already imported by then, so
    nothing reinstalls the redirect before the next run starts collecting.
    """
    import shutil

    from tests import conftest as harness

    # The hook that has to do the arming, checked as wiring rather than by
    # calling it: a second ``pytest_configure`` on this run's config would strip
    # the environment again over an already-stripped one and stack a second
    # egress guard on the first, which is a worse test than reading the call.
    assert "_arm_config_dir_redirect" in harness.pytest_configure.__code__.co_names

    try:
        # Where the end of the first run leaves the process.
        harness.restore_real_platform()
        shutil.rmtree(harness._SESSION_CONFIG_DIR, ignore_errors=True)
        assert get_platform().config_dir == real_platform_config_dir

        harness._arm_config_dir_redirect()

        assert get_platform().config_dir == harness._SESSION_CONFIG_DIR
        assert harness._SESSION_CONFIG_DIR.is_dir()
    finally:
        # Armed again before narrowing: the session directory was deleted above,
        # and a failure between there and here would otherwise leave every later
        # test pointed at a path that no longer exists.
        harness._arm_config_dir_redirect()
        harness._pin_config_dir(tmp_path / "platform-config")

    assert get_platform().config_dir.is_relative_to(tmp_path)
