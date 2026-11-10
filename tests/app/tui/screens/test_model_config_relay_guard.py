# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Model config screen: the relay checkbox and its approval-judge save constraint.

Real Textual pages, real clicks, real files under the isolated config dir —
these verify the boundary (toast shown, file and registry unchanged or
updated), not just that the guard function returned a code.
"""

from __future__ import annotations

import pytest
from textual.widgets import Checkbox

from chrys.service.profiles.models.loader import load_profile_from_yaml
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.tui.screens._model_config_support import (
    _capture_notifications,
    open_model_config,
    single_profile_registry,
)
from tests.support.tui_helpers import click_when_settled
from tests.support.waiting import wait_for

pytestmark = pytest.mark.usefixtures("isolated_model_config_dir", "clear_model_profile_env")


def _relay_and_judge_registry() -> tuple[ModelProfileRegistry, ModelProfile, ModelProfile]:
    relay = ModelProfile(
        id="relay-a",
        name="Relay A",
        model_id="gpt-x",
        base_url="https://relay.example.com/v1",
        api_key="sk-relay",
        is_relay=True,
    )
    judge = ModelProfile(
        id="judge-b",
        name="Judge B",
        model_id="gpt-y",
        base_url="https://direct.example.com/v1",
        api_key="sk-judge",
    )
    registry = ModelProfileRegistry()
    registry.register(relay)
    registry.register(judge)
    return registry, relay, judge


def _models_dir():
    from chrys.foundation.platform import get_platform

    return get_platform().config_dir / "models"


def _profile_path(profile_id: str):
    # Resolved at call time: the isolation fixture patches the module
    # attribute, which a module-level ``from`` import would sidestep.
    return _models_dir() / f"{profile_id}.yaml"


def _error_toasts(captured: list[tuple[str, str]]) -> list[str]:
    return [message for severity, message in captured if severity == "error"]


async def test_save_relay_without_judge_selector_is_rejected_and_writes_nothing() -> None:
    registry, profile = single_profile_registry(base_url="https://relay.example.com/v1", api_key="sk-relay")

    async with open_model_config(registry, global_default_profile_id=profile.id) as (screen, pilot):
        captured = _capture_notifications(screen)
        screen.query_one("#mc-relay", Checkbox).value = True
        await click_when_settled(pilot, "#mc-save")

        errors = _error_toasts(captured)
        assert errors and any("approval judge" in message for message in errors)
        # Rejected before any write: no file, registry unchanged, form kept.
        assert not _profile_path("model-a").exists()
        assert registry.get("model-a").is_relay is False
        assert screen.query_one("#mc-relay", Checkbox).value is True


async def test_save_relay_with_independent_judge_persists_the_flag() -> None:
    registry, relay, judge = _relay_and_judge_registry()

    async with open_model_config(registry, global_default_profile_id="", approval_judge_selector=judge.id) as (
        screen,
        pilot,
    ):
        captured = _capture_notifications(screen)
        screen._load_profile(relay.id)
        screen.query_one("#mc-relay", Checkbox).value = True
        await click_when_settled(pilot, "#mc-save")

        assert not _error_toasts(captured)
        assert registry.get("relay-a").is_relay is True
        assert load_profile_from_yaml(_profile_path("relay-a")).is_relay is True


async def test_clone_of_relay_without_judge_is_rejected() -> None:
    registry, _profile = single_profile_registry(is_relay=True)

    async with open_model_config(registry) as (screen, pilot):
        captured = _capture_notifications(screen)
        await click_when_settled(pilot, "#mc-clone")

        assert any("approval judge" in message for message in _error_toasts(captured))
        assert len(registry.list_profiles()) == 1
        assert not list(_models_dir().glob("*.yaml"))


async def test_clone_of_relay_with_judge_is_allowed() -> None:
    registry, relay, judge = _relay_and_judge_registry()

    async with open_model_config(registry, approval_judge_selector=judge.id) as (screen, pilot):
        captured = _capture_notifications(screen)
        screen._load_profile(relay.id)
        await click_when_settled(pilot, "#mc-clone")

        assert not _error_toasts(captured)
        assert len(registry.list_profiles()) == 3
        duplicate = next(p for p in registry.list_profiles() if p.id not in {relay.id, judge.id})
        assert duplicate.is_relay is True
        assert load_profile_from_yaml(_profile_path(duplicate.id)).is_relay is True


async def test_delete_of_judge_with_dependent_relay_is_rejected() -> None:
    registry, _relay, judge = _relay_and_judge_registry()

    async with open_model_config(registry, approval_judge_selector=judge.id) as (screen, pilot):
        captured = _capture_notifications(screen)
        screen._load_profile(judge.id)
        await click_when_settled(pilot, "#mc-delete")
        await click_when_settled(pilot, "#confirm-yes")

        await wait_for(
            lambda: bool(_error_toasts(captured)),
            pilot=pilot,
            description="relay-guard rejection toast after confirmed delete",
        )
        assert any("approval judge" in message for message in _error_toasts(captured))
        assert registry.get(judge.id) is not None


async def test_delete_of_relay_while_judge_is_independent_is_allowed() -> None:
    registry, relay, judge = _relay_and_judge_registry()

    async with open_model_config(registry, approval_judge_selector=judge.id) as (screen, pilot):
        captured = _capture_notifications(screen)
        screen._load_profile(relay.id)
        await click_when_settled(pilot, "#mc-delete")
        await click_when_settled(pilot, "#confirm-yes")

        await wait_for(
            lambda: registry.get(relay.id) is None,
            pilot=pilot,
            description="relay profile deleted after confirmation",
        )
        assert not any("approval judge" in message for message in _error_toasts(captured))
        assert registry.get(judge.id) is not None


async def test_relay_hint_follows_the_checkbox_and_loaded_profile() -> None:
    registry, _profile = single_profile_registry(is_relay=True)

    async with open_model_config(registry) as (screen, pilot):
        hint = screen.query_one("#mc-relay-hint")
        checkbox = screen.query_one("#mc-relay", Checkbox)
        assert checkbox.value is True
        assert hint.display is True

        checkbox.value = False
        await pilot.pause()
        assert hint.display is False
