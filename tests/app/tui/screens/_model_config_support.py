# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared host app, mount context manager, waits, and factories for the model configuration screen tests."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from textual.pilot import Pilot
from textual.widgets import Input

from chrys.app.tui.screens.main.config_actions import RuntimeConfigController
from chrys.app.tui.screens.main.state import MainScreenServices, MainScreenState
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SettingsReload
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.support.event_capture import capture_events
from tests.support.tui_helpers import PlaceholderHostApp
from tests.support.waiting import wait_for


def single_profile_registry(**overrides: Any) -> tuple[ModelProfileRegistry, ModelProfile]:
    """Return a registry holding exactly one profile, plus that profile.

    Defaults to the ``model-a``/``Model A``/``gpt-test`` triple the screen tests
    use whenever the profile's contents are not the thing under test;
    *overrides* replace individual ``ModelProfile`` fields.
    """
    fields: dict[str, Any] = {"id": "model-a", "name": "Model A", "model_id": "gpt-test"}
    fields.update(overrides)
    profile = ModelProfile(**fields)
    registry = ModelProfileRegistry()
    registry.register(profile)
    return registry, profile


@contextlib.asynccontextmanager
async def open_model_config(
    registry: ModelProfileRegistry,
    *,
    size: tuple[int, int] = (120, 40),
    **screen_kwargs: Any,
) -> AsyncIterator[tuple[ModelConfigScreen, Pilot]]:
    """Mount ``ModelConfigScreen`` on a bare host app and yield it with the pilot.

    Tests that need the host app reach it through ``pilot.app``; assertions on
    values captured inside the block stay valid after it exits.
    """
    app = PlaceholderHostApp()
    async with app.run_test(size=size) as pilot:
        screen = ModelConfigScreen(registry, **screen_kwargs)
        await app.push_screen(screen)
        await pilot.pause()
        yield screen, pilot


async def _wait_for_kv_rows(container: Any, pilot: Pilot, count: int) -> None:
    """Poll until async key-value row mounts (and their inputs) are ready.

    Wait for both the row container AND its key/value ``Input`` children: on
    slow runners (e.g. Windows CI) the row mounts a tick before its children,
    so querying inputs immediately after the row count is satisfied races.
    """

    def rows_ready() -> bool:
        rows = list(container.query(".mc-kv-item-row"))
        return len(rows) >= count and all(
            row.query(".mc-kv-key-input") and row.query(".mc-kv-value-input") for row in rows
        )

    await wait_for(
        rows_ready,
        pilot=pilot,
        description=f"at least {count} key-value rows with inputs mounted",
    )


def fill_kv_row(container: Any, key: str, value: str, *, index: int = 0) -> None:
    """Type *key*/*value* into the *index*-th key-value row of a ``#mc-*-list`` container."""
    row = list(container.query(".mc-kv-item-row"))[index]
    row.query_one(".mc-kv-key-input", Input).value = key
    row.query_one(".mc-kv-value-input", Input).value = value


def _capture_notifications(screen: ModelConfigScreen) -> list[tuple[str, str]]:
    """Shadow ``screen.notify`` and record ``(severity, message)`` pairs."""
    captured: list[tuple[str, str]] = []

    def _notify(
        message: str,
        *,
        title: str = "",
        severity: str = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        captured.append((severity, message))

    screen.notify = _notify  # type: ignore[method-assign]
    return captured


async def _model_config_result_events(result: str, registry: ModelProfileRegistry) -> list[SettingsReload]:
    """Run the modal's close result through ``RuntimeConfigController`` and return the reloads it published."""
    bus = EventBus()
    published = await capture_events(bus, SettingsReload)
    controller = RuntimeConfigController(
        state=MainScreenState(),
        services=MainScreenServices(bus=bus, model_registry=registry),
        view=cast(Any, SimpleNamespace(notify=lambda *_args, **_kwargs: None)),
        callbacks=cast(Any, SimpleNamespace(debug=lambda _key, _message="": None, user_picked=lambda: None)),
    )
    await controller.on_model_config_result(result)
    return published
