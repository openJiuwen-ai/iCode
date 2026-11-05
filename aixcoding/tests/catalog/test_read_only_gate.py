# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The UI gate for centralized model management (migrated from 0.28).

The model configuration screen opens read-only no matter what the runtime is
doing: the model list is owned by the server catalog. The rendered notice
text is pinned upstream (tests/app/tui/screens/test_model_config.py); what
these anchor here is the gate itself and the removed F4 entry
(aixcoding/docs/batches/catalog-sync/design.md, R2 rows 5/6/10), plus the
busy-state and indicator-entry anchors of catalog-readonly-coverage
(aixcoding/docs/batches/catalog-readonly-coverage/design.md).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from chrys.app.tui.screens.models import screen as model_config_screen_module
from chrys.app.tui.screens.models.screen import ModelConfigScreen
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from chrys.app.tui.screens.main.config_actions import RuntimeConfigController


def _profile(profile_id: str) -> ModelProfile:
    return ModelProfile(id=profile_id, name=profile_id, provider="openai", model_id=f"{profile_id}-m")


class _NoopCallbacks:
    def __getattr__(self, name: str) -> object:
        return lambda *args, **kwargs: None


def _make_controller(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_running: bool = False,
    execution_busy: bool = False,
    submit_active: bool = False,
) -> tuple[RuntimeConfigController, list[ModelConfigScreen], list[Coroutine[object, object, None]]]:
    """Build a controller around stubs, mirroring the idle harness of the gate test.

    ``pushed`` collects screens handed to ``push_screen``; ``captured`` collects
    the coroutines handed to ``run_worker`` (the caller drives them, so the
    real ``_open_model_config`` chain runs under the test's event loop).
    """
    from chrys.app.tui.screens.main.config_actions import RuntimeConfigController as _Controller

    registry = ModelProfileRegistry()
    registry.register(_profile("a"))
    pushed: list[ModelConfigScreen] = []
    captured: list[Coroutine[object, object, None]] = []

    class _View:
        def push_screen(self, screen: ModelConfigScreen, _callback: object = None) -> None:
            pushed.append(screen)

        def run_worker(self, worker: Coroutine[object, object, None], **_kwargs: object) -> None:
            captured.append(worker)

        def notify(self, *_args: object, **_kwargs: object) -> None: ...

        def focus_input(self) -> None: ...

    class _Run:
        agent_running = False
        agent_loading = False

    class _Submit:
        active = False

    _Run.agent_running = agent_running
    _Submit.active = submit_active

    class _State:
        run = _Run()
        submit = _Submit()

    class _Services:
        model_registry = registry

        def execution_busy(self) -> bool:
            return execution_busy

    controller = _Controller.__new__(_Controller)
    controller._view = _View()
    controller._state = _State()
    controller._services = _Services()
    controller._callbacks = _NoopCallbacks()
    controller._model_config_opening = False
    monkeypatch.setattr("chrys.service.profiles.models.env_bridge.get_global_default_profile_id", lambda: None)
    return controller, pushed, captured


@pytest.mark.asyncio
async def test_open_model_config_screen_is_always_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even idle (no agent, nothing executing) the screen must open read-only."""
    controller, pushed, _ = _make_controller(monkeypatch)

    await controller._open_model_config()

    assert len(pushed) == 1
    assert pushed[0]._read_only is True, "idle must still be read-only: management is centralized"


@pytest.mark.parametrize(
    ("agent_running", "execution_busy", "submit_active"),
    [
        pytest.param(True, False, False, id="agent-running"),
        pytest.param(False, True, False, id="execution-busy"),
        pytest.param(False, False, True, id="submit-active"),
        pytest.param(True, True, True, id="all-three"),
    ],
)
@pytest.mark.asyncio
async def test_open_model_config_screen_is_read_only_while_busy(
    monkeypatch: pytest.MonkeyPatch,
    agent_running: bool,
    execution_busy: bool,
    submit_active: bool,
) -> None:
    """Each signal of the upstream conditional gate, alone and combined, still opens read-only.

    Upstream decides editability from ``agent_running or submit.active or
    execution_busy()``; the centralized catalog makes the screen read-only in
    every one of those states (the dimension the v0.33.0 models send-in-flight
    test used to pin upstream — C135).
    """
    controller, pushed, _ = _make_controller(
        monkeypatch, agent_running=agent_running, execution_busy=execution_busy, submit_active=submit_active
    )

    await controller._open_model_config()

    assert len(pushed) == 1
    assert isinstance(pushed[0], ModelConfigScreen)
    assert pushed[0]._read_only is True, "busy must not weaken the always-read-only gate"


@pytest.mark.asyncio
async def test_model_indicator_configure_entry_opens_read_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The indicator's configure entry routes through the real open chain to a read-only screen."""
    controller, pushed, captured = _make_controller(monkeypatch)

    controller.on_model_tag_clicked("configure")
    assert captured, "open_model_config must hand the open coroutine to run_worker"
    await captured[0]

    assert len(pushed) == 1
    assert isinstance(pushed[0], ModelConfigScreen)
    assert pushed[0]._read_only is True, "the configure entry opens the same always-read-only screen"


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
