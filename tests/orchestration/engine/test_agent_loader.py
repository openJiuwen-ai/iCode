# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Loader handoff, resource lifetime, and terminal-event admission contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadStarted,
    SessionReady,
    UserMessage,
)
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine import assembly as assembly_module
from chrys.orchestration.engine import loader as loader_module
from chrys.orchestration.engine.build.construction import StagedBuild
from chrys.orchestration.engine.build.loaded import CompletedBuild
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mcp.cache import MCPConnectionCache
from chrys.service.mutations.coordination import MutationCoordinator
from chrys.service.profiles.agents.schema import AgentProfile, CompactionConfig, ToolsConfig
from tests.orchestration.engine.build.test_lifecycle_close import _fresh_approval, _make_build_result
from tests.support.loaded_agents import make_loaded_agent, make_manifest
from tests.support.reminder_stack import reminder_pair
from tests.support.waiting import await_run_task_chain


def _candidate_result(bus):
    result = _make_build_result(_fresh_approval(bus))
    result.bindings.backend.service_session_id = ""
    result.bindings.backend.service_session_storage_enabled = False
    result.reminder_middleware, result.last_words = reminder_pair()
    return result


def _profile(name: str = "Code") -> AgentProfile:
    return AgentProfile(
        name=name,
        instructions="Answer briefly.",
        tools=ToolsConfig(builtins=[]),
        compaction=CompactionConfig(enabled=False),
    )


async def test_loader_build_install_release_handoff_preserves_live_until_install(monkeypatch, agent_engine) -> None:
    bus = EventBus()
    calls = []

    async def build_candidate(**kwargs):
        result = _candidate_result(bus)
        calls.append(result)
        return result

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    loader = engine.loader
    profile = _profile()
    first = await loader.build(
        profile, loader.stage(loaded=engine.loaded_settings, agent_profile=profile, workspace=None, hook_manager=None)
    )
    assert engine.current.loaded is None
    replaced = loader.install(first)
    assert replaced.loaded is None
    assert engine.current.loaded is first.loaded
    await loader.release(replaced)
    generation = engine.build_generation
    second = await loader.build(
        profile, loader.stage(loaded=engine.loaded_settings, agent_profile=profile, workspace=None, hook_manager=None)
    )
    assert engine.current.loaded is first.loaded
    assert calls[0].bindings.closed is False
    replaced = loader.install(second)
    assert replaced.loaded is first.loaded
    assert engine.current.loaded is second.loaded
    assert engine.build_generation == generation + 1
    assert calls[0].bindings.closed is False
    await loader.release(replaced)
    assert calls[0].bindings.closed is True
    assert calls[1].bindings.closed is False


@pytest.mark.parametrize("reuse_coordinator", [True, False])
async def test_loader_release_closes_only_coordinator_displaced_at_install(
    agent_engine, reuse_coordinator: bool, *, engine_services
) -> None:
    engine = agent_engine(EventBus(), settings=Settings())
    old_coordinator = create_autospec(MutationCoordinator, instance=True)
    new_coordinator = old_coordinator if reuse_coordinator else create_autospec(MutationCoordinator, instance=True)
    engine.session.mutation_coordinator = old_coordinator
    staged = StagedBuild(
        prepared=engine.settings_handle.prepare(engine.settings_handle.base),
        agent_profile=_profile(),
        workspace=None,
        hook_manager=None,
        mutation_coordinator=new_coordinator,
    )
    completed = CompletedBuild(
        staged=staged,
        settings=engine.settings_handle.prepare(staged.loaded),
        workspace_retarget=engine_services(engine).workspace_change_tracker.resolve_retarget(None, resolve_scope=False),
        loaded=make_loaded_agent(),
        manifest=make_manifest(),
        compaction_strategy=None,
        mutation_tracker=None,
        todo_tracker=None,
    )

    replaced = engine.loader.install(completed)
    assert replaced.coordinator is (None if reuse_coordinator else old_coordinator)
    assert engine.session.mutation_coordinator is new_coordinator
    old_coordinator.close.assert_not_called()
    await engine.loader.release(replaced)
    if not reuse_coordinator:
        old_coordinator.close.assert_called_once_with()
    new_coordinator.close.assert_not_called()


async def test_reload_installs_new_owner_and_releases_previous_build(monkeypatch, agent_engine) -> None:
    bus = EventBus()
    results = []

    async def build_candidate(**kwargs):
        result = _candidate_result(bus)
        results.append(result)
        return result

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    await engine.start(_profile())
    old = engine.current.loaded
    await engine.loader.reload(_profile("Explore"))
    assert engine.current.loaded is not old
    assert results[0].bindings.closed is True
    assert results[1].bindings.closed is False
    assert engine.session.agent_profile.name == "Explore"


async def test_loader_close_only_closes_mcp_cache_and_next_build_uses_replacement(monkeypatch, agent_engine) -> None:
    bus = EventBus()
    caches = []
    used = []

    class Cache(MCPConnectionCache):
        def __init__(self):
            super().__init__()
            caches.append(self)

    async def build_candidate(**kwargs):
        used.append(kwargs["mcp_cache"])
        return _candidate_result(bus)

    monkeypatch.setattr(assembly_module, "MCPConnectionCache", Cache)
    monkeypatch.setattr(loader_module, "MCPConnectionCache", Cache)
    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    await engine.start(_profile())
    loaded = engine.current.loaded
    manifest = engine.current.manifest
    await engine.loader.close()
    assert caches[0].closed is True
    assert caches[1].closed is False
    assert engine.current.loaded is loaded
    assert engine.current.manifest is manifest
    assert loaded.bindings.closed is False
    await engine.loader.reload(_profile("Explore"))
    assert used == caches[:2]


@pytest.mark.parametrize("replace_hook", [False, True])
async def test_hook_replacement_follows_release_and_only_closes_displaced_manager(
    tmp_path: Path, monkeypatch, agent_engine, replace_hook: bool
) -> None:
    bus = EventBus()
    order = []
    results = []

    class HookManager:
        async def drain_session(self):
            order.append("hook_closed")

        async def recover_outbox(self):
            return None

    old_hook = HookManager()
    new_hook = HookManager()

    async def build_candidate(**kwargs):
        result = _candidate_result(bus)
        results.append(result)
        return result

    async def build_hook(**kwargs):
        return new_hook

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    await engine.start(_profile())
    engine.session.hook_manager = old_hook

    async def record_release():
        order.append("resource_closed")

    results[0].prepared.own(record_release)
    monkeypatch.setattr(engine.loader, "build_hook_manager", build_hook)
    try:
        await engine.loader.reload(
            _profile("Explore"), workspace=Workspace(primary_cwd=str(tmp_path)) if replace_hook else None
        )
        assert order == (["resource_closed", "hook_closed"] if replace_hook else ["resource_closed"])
        assert engine.session.hook_manager is (new_hook if replace_hook else old_hook)
        assert engine.current.loaded.prepared is results[1].prepared
    finally:
        await engine.loader.cancel_outbox_recovery()
        engine.session.hook_manager = None


async def test_terminal_ready_subscriber_can_admit_a_user_message_inline(
    monkeypatch, agent_engine, *, engine_services
) -> None:
    import chrys.orchestration.engine.build.builder as builder_module

    bus = EventBus()
    client = MockChatClient(responses=[MockResponse(text="admitted")])
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(bus, settings=Settings())
    observed = []

    async def started(event: AgentLoadStarted):
        observed.append(("started", engine.permits.agent_loading))

    async def finished(event: AgentLoadFinished):
        observed.append(("finished", engine.permits.agent_loading))

    async def ready(event: SessionReady):
        observed.append(("ready", engine.permits.agent_loading))
        await bus.publish(UserMessage(text="run inline"))
        observed.append(("admitted", engine.permits.agent_loading))

    await bus.subscribe(AgentLoadStarted, started)
    await bus.subscribe(AgentLoadFinished, finished)
    await bus.subscribe(SessionReady, ready)
    await engine.prepare()
    profile = _profile()
    engine.session.begin(agent_profile=profile, workspace=None)
    trajectory = engine_services(engine).trajectory_recorder.bind_session(
        session_id=engine.session.session_id,
        session_dir=None,
        write_lock_path=None,
        session_start_info=engine.loader.trajectory_session_start_info,
    )
    await asyncio.wait_for(
        engine.loader.load(
            profile,
            workspace=engine.session.workspace,
            hook_manager=None,
            old_hook_manager=None,
            trajectory=trajectory,
        ),
        5,
    )
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)
    assert observed == [("started", True), ("finished", False), ("ready", False), ("admitted", False)]
    assert client.call_count == 1


async def test_failed_load_emits_failure_once_without_ready_and_preserves_live(monkeypatch, agent_engine) -> None:
    bus = EventBus()
    fail = False

    async def build_candidate(**kwargs):
        if fail:
            raise RuntimeError("candidate failed")
        return _candidate_result(bus)

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    await engine.start(_profile())
    loaded = engine.current.loaded
    manifest = engine.current.manifest
    events = []

    async def receive(event):
        events.append(type(event))

    for event_type in [AgentLoadStarted, AgentLoadFailed, AgentLoadFinished, SessionReady]:
        await bus.subscribe(event_type, receive)
    fail = True
    with pytest.raises(RuntimeError, match="candidate failed"):
        await engine.loader.reload(_profile("Explore"))
    assert events == [AgentLoadStarted, AgentLoadFailed]
    assert engine.current.loaded is loaded
    assert engine.current.manifest is manifest
    assert loaded.bindings.closed is False
    assert engine.permits.agent_loading is False
