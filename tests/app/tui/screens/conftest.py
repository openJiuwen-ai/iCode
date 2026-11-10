# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for screen tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.service.llm import model_list as model_list_module
from tests.app.tui.screens._model_list_support import FakeDiscover, RealDiscover
from tests.service.llm._model_list_support import clear_provider_env as _clear_provider_env
from tests.service.llm._model_list_support import preload_sdks
from tests.support.platform_fakes import platform_with_config_dir
from tests.support.waiting import reset_wait_deadline


@pytest.fixture
def isolated_chrys_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep agent-profile serializer paths inside pytest temp dirs.

    Opt-in via ``pytest.mark.usefixtures`` rather than autouse: the agent-config
    modules share it, while the other screen modules keep their own environment.
    Resetting the shared wait deadline first gives every test a fresh polling
    budget for the ``tests.support.waiting`` helpers.
    """
    reset_wait_deadline()
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)


@pytest.fixture
def isolated_model_config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep model-profile serializer paths inside pytest temp dirs, and nothing else.

    Deliberately narrower than :func:`isolated_chrys_config_dir`: the stand-in
    carries ``config_dir`` and no other field, so the day the model-config screen
    starts consulting another part of the platform record these tests raise
    ``AttributeError`` instead of quietly reading the developer's real machine.
    The absence is the assertion, so do not widen this to a full ``PlatformInfo``.
    """
    reset_wait_deadline()
    fake_platform = type("P", (), {"config_dir": tmp_path})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)


@pytest.fixture
def clear_model_profile_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start each test with no ``CHRYS_MODEL_PROFILE`` process pointer.

    ``setenv`` before ``delenv`` is deliberate rather than redundant: setting
    the key first registers it with monkeypatch even when the developer's shell
    never exported it, so a test that installs its own pointer mid-run is still
    unwound at teardown instead of leaking into the next test.
    """
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    monkeypatch.delenv("CHRYS_MODEL_PROFILE")


@pytest.fixture
def fake_discover(monkeypatch: pytest.MonkeyPatch) -> FakeDiscover:
    fake = FakeDiscover()
    monkeypatch.setattr(ModelConfigScreen, "discover_models", staticmethod(fake))
    return fake


@pytest.fixture
def real_discover(monkeypatch: pytest.MonkeyPatch) -> RealDiscover:
    # A cold SDK import (compiling and writing .pyc on a fresh CI runner) can outlast a 5 s UI wait;
    # load it before the test so the wait measures the lookup, not the first import.
    preload_sdks()
    real = RealDiscover()
    monkeypatch.setattr(ModelConfigScreen, "discover_models", staticmethod(real))
    # Unanswered addresses get up to five rounds within the deadline: keep that within a UI wait.
    monkeypatch.setattr(model_list_module, "_TOTAL_SECONDS", 1.0)
    monkeypatch.setattr(model_list_module, "_RETRY_BASE_SECONDS", 0.001)
    monkeypatch.setattr(model_list_module, "_RETRY_MAX_PAUSE_SECONDS", 0.001)
    return real


@pytest.fixture
def clear_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_provider_env(monkeypatch)
