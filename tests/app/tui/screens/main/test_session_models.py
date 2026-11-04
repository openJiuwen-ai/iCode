# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Spec tests for the one place the main screen reads which model the session runs."""

from __future__ import annotations

import pytest

from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState, running_model
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle
from chrys.foundation.config.spec import Source
from chrys.foundation.events.bus import EventBus


def _running(*, confirmed: str | None, active: str, pin: str = "") -> str:
    state = MainScreenState()
    if confirmed is not None:
        state.runtime.details_confirmed = True
        state.runtime.details.model.profile_id = confirmed
    loaded = LoadedSettings(settings=Settings(), provenance={}).overlay(Source.PROCESS_RUNTIME, model_profile=active)
    if pin:
        loaded = loaded.overlay(Source.SESSION, model_profile=pin)
    services = MainScreenServices(
        bus=EventBus(), settings_handle=SettingsHandle(loaded), active_model_profile_id=active
    )
    return running_model(state, services)


@pytest.mark.parametrize(
    ("confirmed", "pin", "expected"),
    [
        # Before the runtime names a model, the launch setting.
        (None, "", "model-p"),
        # A prompt shortcut's session-only switch the runtime took up.
        ("model-s", "model-s", "model-s"),
        # An agent's own model runs while the session's own choice waits underneath.
        ("model-a", "model-s", "model-a"),
        ("model-a", "", "model-a"),
        # A runtime answer that names no model says nothing: the launch setting stands.
        ("", "", "model-p"),
    ],
    ids=["before-runtime", "shortcut-switch", "agent-own-over-switch", "agent-own", "blank-answer"],
)
def test_running_model_is_what_the_runtime_runs_else_the_launch_setting(
    confirmed: str | None, pin: str, expected: str
) -> None:
    assert _running(confirmed=confirmed, active="model-p", pin=pin) == expected
