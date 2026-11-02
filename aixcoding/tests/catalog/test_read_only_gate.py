# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The UI gate for centralized model management (migrated from 0.28).

The model configuration screen opens read-only no matter what the runtime is
doing: the model list is owned by the server catalog. The rendered notice
text is pinned upstream (tests/app/tui/screens/test_model_config.py); what
these anchor here is the gate itself and the removed F4 entry
(aixcoding/docs/batches/catalog-sync/design.md, R2 rows 5/6/10).
"""

from __future__ import annotations

import contextlib

import pytest

from chrys.app.tui.screens.models import screen as model_config_screen_module
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile


def _profile(profile_id: str) -> ModelProfile:
    return ModelProfile(id=profile_id, name=profile_id, provider="openai", model_id=f"{profile_id}-m")


class _NoopCallbacks:
    def __getattr__(self, name: str) -> object:
        return lambda *args, **kwargs: None


@pytest.mark.asyncio
async def test_open_model_config_screen_is_always_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even idle (no agent, nothing executing) the screen must open read-only."""
    from chrys.app.tui.screens.main.config_actions import RuntimeConfigController

    registry = ModelProfileRegistry()
    registry.register(_profile("a"))
    pushed: list[ModelConfigScreen] = []

    class _View:
        def push_screen(self, screen: ModelConfigScreen, _callback: object = None) -> None:
            pushed.append(screen)

        def run_worker(self, worker: object, **_kwargs: object) -> None:
            with contextlib.suppress(StopIteration):
                worker.send(None)

        def notify(self, *_args: object, **_kwargs: object) -> None: ...

        def focus_input(self) -> None: ...

    class _Run:
        agent_running = False
        agent_loading = False

    class _State:
        run = _Run()

    class _Services:
        model_registry = registry

        def execution_busy(self) -> bool:
            return False

    controller = RuntimeConfigController.__new__(RuntimeConfigController)
    controller._view = _View()
    controller._state = _State()
    controller._services = _Services()
    controller._callbacks = _NoopCallbacks()
    controller._model_config_opening = False

    monkeypatch.setattr("chrys.service.profiles.models.env_bridge.get_global_default_profile_id", lambda: None)
    await controller._open_model_config()

    assert len(pushed) == 1
    assert pushed[0]._read_only is True, "idle must still be read-only: management is centralized"


def test_read_only_notice_uses_central_management_wording() -> None:
    """The notice explains policy, not a transient agent-session condition."""
    fallback = model_config_screen_module._READ_ONLY_NOTICE.fallback
    assert "centrally managed" in fallback
    assert "Agent is running" not in fallback


def test_f4_binding_absent_from_main_screen() -> None:
    """F4 no longer opens the model config: the footer key is gone entirely."""
    from chrys.app.tui.screens.main.screen import MainScreen

    for binding in MainScreen.BINDINGS:
        key = getattr(binding, "key", None)
        action = getattr(binding, "action", None)
        assert not (key == "f4" or action == "models_config"), "the F4 models_config binding must stay removed"
