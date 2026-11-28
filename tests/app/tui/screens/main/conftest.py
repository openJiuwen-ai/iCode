# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared fixtures for main-screen tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.screens.main.workspace_branch import WorkspaceBranchController
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    SessionReady,
    WorkspaceChange,
    WorkspaceOperation,
)
from chrys.orchestration.workflows import catalog as catalog_module
from chrys.service.state.store import JsonFileStateStore
from chrys.service.workspaces.registry import Workspace
from tests.support.rollback_spec import PROFILE
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import (
    ENGINE_TURN_TIMEOUT,
    MAIN_SCREEN_BUILD_TIMEOUT,
    wait_for,
)
from tests.support.workflow_previews import reused_preview_workflow
from tests.support.workflow_workers import share_worker_bytecode_cache
from tests.support.workspace_engine import mock_workspace_runtime


@pytest.fixture(autouse=True)
def reuse_workflow_previews(monkeypatch: pytest.MonkeyPatch) -> None:
    """Workflow screens get real previews, but each distinct preview runs its interpreter and worker only once."""
    monkeypatch.setattr(catalog_module, "preview_workflow", reused_preview_workflow)


@pytest.fixture(scope="session")
def session_bytecode_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("worker-pycache")


@pytest.fixture(autouse=True)
def _shared_bytecode_cache(monkeypatch: pytest.MonkeyPatch, session_bytecode_cache: Path) -> None:
    share_worker_bytecode_cache(monkeypatch, session_bytecode_cache)


@pytest.fixture
async def workspace_main(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(WorkspaceBranchController, "start", lambda _self, _cwd: None)
    app = make_chrys_app(tmp_path)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        main = app.query_one(MainScreen) if app._main_screen is None else app._main_screen
        registry = main._services.workspaces
        workspace = registry.create("Workspace", str(tmp_path))
        await main._services.bus.publish(
            SessionReady(
                session_id="current",
                primary_cwd=str(tmp_path),
                workspace=registry.snapshot().workspace(workspace.id),
            ),
            raise_handler_errors=True,
        )
        yield app, pilot, main
        main._events.cancel_agent_load()


@pytest.fixture
async def live_workspace_chat(agent_engine, monkeypatch, tmp_path):
    current_dir = tmp_path / "current"
    target_dir = tmp_path / "target"
    current_dir.mkdir()
    target_dir.mkdir()
    current_reference = tmp_path / "current-reference"
    current_reference.mkdir()
    monkeypatch.chdir(current_dir)
    monkeypatch.setattr(WorkspaceBranchController, "start", lambda _self, _cwd: None)
    session_root = tmp_path / "sessions"
    store = JsonFileStateStore(session_root)
    registry = store.workspace_registry
    current = registry.create("Current", str(current_dir), [str(current_reference)])
    target = registry.create("Target", str(target_dir))
    bus = EventBus()
    loaded, profiles, models = mock_workspace_runtime(monkeypatch)
    engine = agent_engine(
        bus,
        loaded_settings=loaded,
        agent_registry=profiles,
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(current_dir)),
    )
    app = make_chrys_app(session_root, engine=engine, event_bus=engine.event_bus)
    async with app.run_test(size=(120, 36), notifications=True) as pilot:
        await wait_for(
            lambda: isinstance(app.screen, MainScreen) and app.screen.is_mounted,
            pilot=pilot,
            timeout=MAIN_SCREEN_BUILD_TIMEOUT,
        )
        main = app.screen
        await engine.start(PROFILE)
        await engine.event_bus.publish(
            WorkspaceChange(workspace_operation=WorkspaceOperation.SELECT, workspace_id=current.id),
            raise_handler_errors=True,
        )
        await wait_for(lambda: app.screen is main, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT)
        yield pilot, main, engine, registry, current, target
