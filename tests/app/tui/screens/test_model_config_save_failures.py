# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Every model-profile save path reports an unusable ``models/`` directory instead of crashing.

A ``models/`` symbolic link (the dotfiles pattern) is refused by the
owner-only directory helper, and the auto-create, New and Clone paths write
without going through the form's save wrapper.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.pilot import Pilot

from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.platform import get_platform
from chrys.service.profiles.models.registry import ModelProfileRegistry
from tests.app.tui.screens._model_config_support import open_model_config, single_profile_registry
from tests.support.symlinks import symlink_or_skip
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env")


def _record_notifications(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Shadow ``notify`` on the class, capturing calls made before the screen mounts."""
    captured: list[tuple[str, str, str]] = []

    def _notify(
        _self: ModelConfigScreen,
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        captured.append((severity, message, title))

    monkeypatch.setattr(ModelConfigScreen, "notify", _notify)
    return captured


def _symlink_models_dir(config_dir: Path) -> None:
    if get_platform().is_windows:
        pytest.skip("the Windows branch neither opens nor tightens the directory")
    elsewhere = config_dir / "elsewhere"
    elsewhere.mkdir()
    symlink_or_skip(config_dir / "models", elsewhere, target_is_directory=True)


async def _assert_save_error_toast(captured: list[tuple[str, str, str]], pilot: Pilot, count: int) -> None:
    await wait_for(lambda: len(captured) == count, pilot=pilot, description=f"{count} save failure toast(s)")
    severity, message, title = captured[count - 1]
    assert severity == "error"
    assert title == "Save Error"
    assert "symbolic link" in message


async def test_model_config_auto_create_toasts_when_the_models_dir_is_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening the screen on a linked models/ shows a toast and leaves the app running."""
    _symlink_models_dir(tmp_path)
    captured = _record_notifications(monkeypatch)
    registry = ModelProfileRegistry()

    async with open_model_config(registry) as (screen, pilot):
        await _assert_save_error_toast(captured, pilot, 1)
        assert screen.is_attached
        assert registry.list_profiles() == []

        # The New button reaches the same helper after mount.
        await click_when_settled(pilot, "#mc-new")
        await _assert_save_error_toast(captured, pilot, 2)
        assert screen.is_attached
        assert registry.list_profiles() == []


async def test_model_config_clone_toasts_when_the_models_dir_is_a_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cloning writes a fresh profile; a refused directory must not take the screen down."""
    _symlink_models_dir(tmp_path)
    captured = _record_notifications(monkeypatch)
    registry, profile = single_profile_registry()

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        await click_when_settled(pilot, "#mc-clone")
        await _assert_save_error_toast(captured, pilot, 1)

        assert screen.is_attached
        assert screen._selected_profile_id == profile.id
        assert [p.id for p in registry.list_profiles()] == [profile.id]
