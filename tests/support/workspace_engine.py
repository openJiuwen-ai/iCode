# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real engine construction with only model access and unrelated settings loads fixed."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings_store import LoadedSettings, load_settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionRestore, WorkspaceChange, WorkspaceOperation
from chrys.foundation.models.workspace import WorkingDir, Workspace
from chrys.kernel import Message
from chrys.orchestration.engine import session_lifecycle
from chrys.orchestration.engine.build import builder
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.state import controls
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.state.store import JsonFileStateStore
from tests.support.components import plan_workspace
from tests.support.engines import AgentEngineFactory
from tests.support.initialization_build import _client
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.rollback_spec import PROFILE

_CREATE_CLIENT = builder.create_client
_CASE_STORE = object()


def workspace_selection(workspace_id: str, **kwargs) -> WorkspaceChange:
    return WorkspaceChange(workspace_operation=WorkspaceOperation.SELECT, workspace_id=workspace_id, **kwargs)


def workspace_restore(session_id: str, workspace_id: str, **kwargs) -> SessionRestore:
    return SessionRestore(
        session_id=session_id, workspace_operation=WorkspaceOperation.SELECT, workspace_id=workspace_id, **kwargs
    )


async def select_workspace(bus: EventBus, workspace_id: str, **kwargs) -> None:
    await bus.publish(workspace_selection(workspace_id, **kwargs), raise_handler_errors=True)


async def build_workspace_candidate(engine, profile):
    loader = engine.loader
    return await loader.build(
        profile,
        loader.stage(
            loaded=engine.loaded_settings,
            agent_profile=profile,
            workspace_plan=plan_workspace(Workspace.from_cwd()),
            hook_manager=None,
        ),
    )


def stub_workspace_start(monkeypatch, engine, on_start, *, close) -> None:
    """Exercise the session transition without building an agent."""

    async def start(
        profile, *, operation="startup", staged_loaded=None, session=None, workspace_plan=None, commit_workspace=None
    ):
        assert commit_workspace is None
        if workspace_plan is not None:
            engine.session.install_workspace(workspace_plan)
        on_start(profile=profile, operation=operation, staged_loaded=staged_loaded, session=session)

    monkeypatch.setattr(engine.lifecycle, "start", start)
    for method in ("shutdown", "close_session", "close_session_in_place"):
        monkeypatch.setattr(engine.lifecycle, method, close)


@asynccontextmanager
async def running_acp_manager(loaded, profiles, models, store, *, profile_name, approval_mode, process_cwd=None):
    from chrys.app.acp.session_manager import AcpSessionManager

    manager = AcpSessionManager(
        loaded_settings=loaded,
        profile_name=profile_name,
        approval_mode=approval_mode,
        process_cwd=process_cwd,
        state_store=store,
        agent_registry=profiles,
        model_registry=models,
    )
    try:
        yield manager
    finally:
        await manager.shutdown()


def runtime_reminder(engine: AgentEngine) -> str:
    """The runtime reminder sent from the main build's committed environment."""
    from chrys.service.agent_middleware.reminders.runtime_env import RuntimeEnvSource

    runtime = engine.current.require_loaded().runtime
    reminder = RuntimeEnvSource(runtime, shell_tool_enabled=False, workspace_section=True).snapshot()
    assert reminder is not None
    return reminder


async def side_answer_settled(engine: AgentEngine) -> None:
    """Await the side answer's own task, then its save, so a failure shows at once rather than as a timeout."""
    from tests.support.waiting import ENGINE_TURN_TIMEOUT

    questions = engine._side_questions
    await asyncio.wait_for(asyncio.gather(*tuple(questions._run_tasks)), ENGINE_TURN_TIMEOUT)
    await asyncio.wait_for(asyncio.gather(*tuple(questions._commit_tasks)), ENGINE_TURN_TIMEOUT)


class HeldFirstCallClient(MockChatClient):
    """Holds the first model call until the test releases it; later calls run at once."""

    def __init__(self, responses: list[MockResponse]) -> None:
        super().__init__(responses=responses)
        self.first_call_entered = asyncio.Event()
        self.release_first_call = asyncio.Event()
        self._held = False

    def _inner_get_response(
        self,
        *,
        messages: Sequence[Message],
        stream: bool,
        options: Mapping[str, Any],
        **kwargs: Any,
    ) -> Any:
        if self._held:
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)
        self._held = True
        inner = super()._inner_get_response

        async def held() -> Any:
            self.first_call_entered.set()
            await self.release_first_call.wait()
            response = inner(messages=messages, stream=stream, options=options, **kwargs)
            assert isinstance(response, Awaitable)
            return await response

        return held()


class WorkspaceBoundary:
    """A one-shot gate for a specified lifecycle boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.used = False

    async def wait(self) -> None:
        if self.used:
            return
        self.used = True
        self.entered.set()
        await self.release.wait()

    def open(self) -> None:
        self.release.set()


def before_await(monkeypatch, owner, name, callback: Callable[[], Awaitable[None]]) -> None:
    """Run a test checkpoint before the original async method."""
    original = getattr(owner, name)

    async def wrapped(*args, **kwargs):
        await callback()
        return await original(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapped)


def capture_async_results(monkeypatch, owner, name, observe) -> None:
    original = getattr(owner, name)

    async def capture(*args, **kwargs):
        result = await original(*args, **kwargs)
        observe(result, *args, **kwargs)
        return result

    monkeypatch.setattr(owner, name, capture)


def freeze_workspace_settings(monkeypatch: pytest.MonkeyPatch, loaded: LoadedSettings) -> None:
    for module in (controls, session_lifecycle):
        monkeypatch.setattr(module, "load_settings", create_autospec(load_settings, return_value=loaded))


def mock_workspace_runtime(
    monkeypatch: pytest.MonkeyPatch, client: MockChatClient | None = None
) -> tuple[LoadedSettings, AgentProfileRegistry, ModelProfileRegistry]:
    settings, models = make_mock_settings_and_registry()
    loaded = LoadedSettings(settings=settings, provenance={})
    freeze_workspace_settings(monkeypatch, loaded)
    monkeypatch.setattr(
        builder,
        "create_client",
        create_autospec(_CREATE_CLIENT, side_effect=_client)
        if client is None
        else create_autospec(_CREATE_CLIENT, return_value=client),
    )
    profiles = AgentProfileRegistry()
    profiles.register(PROFILE)
    return loaded, profiles, models


async def start_workspace_engine(
    factory: AgentEngineFactory,
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    workspace_id: str | None = None,
    additional: list[Path] | None = None,
    store: JsonFileStateStore | None = None,
    client: MockChatClient | None = None,
) -> AgentEngine:
    loaded, profiles, models = mock_workspace_runtime(monkeypatch, client)
    engine = factory(
        EventBus(),
        loaded_settings=loaded,
        agent_registry=profiles,
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace(
            primary_cwd=str(root), working_dirs=[WorkingDir(str(path)) for path in additional or []]
        ),
    )
    await engine.start(PROFILE)
    if workspace_id is not None:
        await select_workspace(engine.event_bus, workspace_id)
        if engine.session.workspace_id != workspace_id:
            raise AssertionError(f"Workspace selection into {workspace_id} was not applied")
    return engine


def patch_headless_host(monkeypatch, profiles, models, store) -> None:
    from chrys.app.cli import run as run_cli
    from chrys.orchestration.session_host import ChrysSessionHost

    monkeypatch.setattr(
        run_cli,
        "ChrysSessionHost",
        partial(ChrysSessionHost, agent_registry=profiles, model_registry=models, state_store=store),
    )


def mock_headless_workspace_restore(monkeypatch: pytest.MonkeyPatch, store: JsonFileStateStore) -> MockChatClient:
    from chrys.app.cli import run as run_cli
    from tests.app.cli.test_run import _prepared_runtime
    from tests.orchestration import _session_host_helpers as host_helpers

    settings, models = host_helpers._make_settings_and_models()
    client = MockChatClient(responses=[MockResponse(text="done")])
    host_helpers._patch_runtime(monkeypatch, [client], [])
    monkeypatch.setattr(run_cli.headless, "prepare_runtime", lambda **_: _prepared_runtime(settings))
    patch_headless_host(monkeypatch, host_helpers._make_agent_registry(host_helpers._make_profile()), models, store)
    return client


class WorkspaceCase:
    """Shared probes over an engine owned by agent_engine or a real frontend harness."""

    def __init__(self, factory, monkeypatch, tmp_path):
        self.factory, self.monkeypatch = factory, monkeypatch
        self.store = JsonFileStateStore(tmp_path / "sessions")
        self.registry = self.store.workspace_registry

    async def __call__(self, root, *, workspace_id=None, additional=None, client=None, store=_CASE_STORE):
        engine = await start_workspace_engine(
            self.factory,
            self.monkeypatch,
            root,
            workspace_id=workspace_id,
            additional=additional,
            store=self.store if store is _CASE_STORE else store,
            client=client,
        )
        self.attach(engine)
        return engine

    async def start_selected(self, primary: Path, name: str = "Team"):
        definition = self.registry.create(name, str(primary))
        engine = await self(primary, workspace_id=definition.id)
        return engine, definition

    def attach(self, engine):
        registry = engine.lifecycle.workspaces.registry
        if registry is not None:
            self.registry = registry
        freeze_workspace_settings(self.monkeypatch, engine.settings_handle.base)
        return self

    def gate(self, owner, name):
        """Hold an existing await; a row supplies its state edit or the real write-error shape."""
        gate = WorkspaceBoundary()
        before_await(self.monkeypatch, owner, name, gate.wait)
        return gate


@pytest.fixture
async def workspace_case(agent_engine, monkeypatch, tmp_path):
    """One lifecycle owner, with shared fault and frontend-attachment probes."""
    yield WorkspaceCase(agent_engine, monkeypatch, tmp_path)
