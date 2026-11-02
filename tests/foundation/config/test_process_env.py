# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the frozen process snapshot and environment name identity."""

from __future__ import annotations

import dataclasses

import pytest

from chrys.foundation.config.process_env import (
    canonical_env_name,
    freeze_process_env,
    process_env_snapshot,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.spec import specs_by_field


def _fake_platform(monkeypatch: pytest.MonkeyPatch, os_name: str) -> None:
    import chrys.foundation.platform as platform_mod

    fake = dataclasses.replace(platform_mod.get_platform(), os_name=os_name)
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)


# --- canonical_env_name ---


def test_canonical_env_name_folds_case_only_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_platform(monkeypatch, "windows")
    assert canonical_env_name("chrys_model_profile") == "CHRYS_MODEL_PROFILE"

    _fake_platform(monkeypatch, "linux")
    assert canonical_env_name("chrys_model_profile") == "chrys_model_profile"


def test_setting_environment_aliases_are_uppercase() -> None:
    aliases = [entry.env for entry in specs_by_field(Settings).values() if entry.env is not None]

    assert aliases
    assert [name for name in aliases if name != name.upper()] == []


# --- ProcessEnvSnapshot ---


def test_snapshot_is_none_until_frozen_and_first_freeze_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    assert process_env_snapshot() is None

    monkeypatch.setenv("SNAPSHOT_PROBE", "before")
    first = freeze_process_env()
    assert process_env_snapshot() is first
    assert first.values["SNAPSHOT_PROBE"] == "before"

    monkeypatch.setenv("SNAPSHOT_PROBE", "after")
    assert freeze_process_env() is first
    assert first.values["SNAPSHOT_PROBE"] == "before"


def test_snapshot_mapping_rejects_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SNAPSHOT_PROBE", "value")
    snapshot = freeze_process_env()
    with pytest.raises(TypeError):
        snapshot.values["SNAPSHOT_PROBE"] = "mutated"  # type: ignore[index]
