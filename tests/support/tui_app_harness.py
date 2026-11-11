# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Whole-app ``ChrysApp`` doubles and factory shared by TUI behaviour tests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from chrys.app.tui.app import ChrysApp
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.execution import ExecutionSnapshot
from chrys.service.approval.policy import ApprovalMode
from chrys.service.state.store import JsonFileStateStore

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path


class ShutdownOnlyEngine:
    """Engine double exposing shutdown and an idle execution snapshot.

    ``ChrysApp._build_main_screen`` always installs an engine provider.
    Permanent chrome reads the launch approval mode and lifecycle task;
    the Messages preview reads the session generation and mutation baseline.
    Other backend capabilities stay absent so unintended reads fail loudly.
    """

    approval_mode = ApprovalMode.MANUAL
    turn_lifecycle_task: asyncio.Task[None] | None = None
    session_generation = 0
    mutation_coordinator = None
    mutation_tracker = None

    def execution(self) -> ExecutionSnapshot:
        return ExecutionSnapshot("idle")

    def execution_busy(self) -> bool:
        return self.execution().kind != "idle"

    async def shutdown(self) -> None:
        return


class SessionGenerationEngine(ShutdownOnlyEngine):
    """Idle engine that also reports a fresh session generation."""

    session_generation = 0


class EmptyAgentRegistry:
    """Agent-profile registry double that publishes no profiles."""

    def list_profiles(self) -> list[object]:
        return []

    def load_all(self) -> None:
        return

    def get(self, _name: str) -> None:
        return None

    def resolve_selector(self, _selector: str) -> None:
        return None


def make_chrys_app(
    state_root: Path,
    *,
    settings: Settings | None = None,
    engine: object | None = None,
    agent_registry: object | None = None,
    gc_freeze_enabled: bool | None = False,
    event_bus: EventBus | None = None,
    **kwargs: object,
) -> ChrysApp:
    """Build a ``ChrysApp`` over inert doubles with its state store under *state_root*.

    GC freezing is off by default because almost every app test wants an inert
    coordinator; pass ``gc_freeze_enabled=None`` to defer to the module default
    exactly as ``ChrysApp`` does when the argument is omitted.
    """
    return ChrysApp(
        event_bus if event_bus is not None else EventBus(),
        engine if engine is not None else ShutdownOnlyEngine(),  # type: ignore[arg-type]
        settings=settings if settings is not None else Settings(),
        state_store=JsonFileStateStore(state_root),
        agent_registry=agent_registry if agent_registry is not None else EmptyAgentRegistry(),  # type: ignore[arg-type]
        gc_freeze_enabled=gc_freeze_enabled,
        **kwargs,  # type: ignore[misc]
    )
