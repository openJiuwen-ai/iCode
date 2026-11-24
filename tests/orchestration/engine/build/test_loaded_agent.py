# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Installed manifest snapshots remain stable across refresh and shutdown."""

from __future__ import annotations

import asyncio
import copy
import gc
import weakref
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeUpdated, SessionReady, Warning
from chrys.foundation.models.workspace import Workspace
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.build.loaded import AgentManifest, CompletedBuild
from chrys.orchestration.engine.loader import AgentLoader
from chrys.orchestration.engine.run.runtime_skills import CommittedSkillRefresh, RuntimeSkillRefresher
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.profiles.models.schema import ModelProfile
from tests.orchestration.engine.build.test_lifecycle_close import _fresh_approval, _make_build_result
from tests.support.components import plan_workspace
from tests.support.loaded_agents import install_loaded_agent, make_loaded_agent, make_manifest


def test_from_build_copies_mutable_builder_values() -> None:
    result = _make_build_result(_fresh_approval(EventBus()))
    manifest = AgentManifest.from_build(result)
    before = copy.deepcopy(manifest.runtime_details)
    result.tool_names.append("later")
    result.tool_kinds["later"] = "shell"
    result.skill_names.append("later")
    result.memory_files.append("later")
    result.runtime_details.skill_sources["later"] = ["later"]
    result.active_profile.model_id = "later"
    assert "later" not in manifest.tool_names
    assert "later" not in manifest.tool_kinds
    assert "later" not in manifest.skill_names
    assert "later" not in manifest.memory_files
    assert manifest.runtime_details == before
    assert manifest.active_profile.model_id != "later"
    with pytest.raises(FrozenInstanceError):
        manifest.skill_names = ()
    with pytest.raises(TypeError):
        manifest.tool_kinds["later"] = "shell"


def test_skill_refresh_replaces_manifest_without_changing_previous_details() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    previous = engine.current.manifest
    before = copy.deepcopy(previous.runtime_details)
    updated = engine.loader.apply_skill_refresh(
        skill_names=["new"], skill_sources={"project": ["new"]}, skill_details=[]
    )
    assert updated is engine.current.manifest
    assert updated is not previous
    assert updated.runtime_details is not previous.runtime_details
    assert previous.runtime_details == before
    assert previous.skill_names == ()
    assert updated.skill_names == ("new",)
    assert updated.runtime_details.skill_sources == {"project": ["new"]}


def test_facades_return_independent_copies_of_manifest_values() -> None:
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    profile = ModelProfile(id="model", name="Model", provider="mock", model_id="original")
    install_loaded_agent(engine, active_profile=profile)
    details = engine.runtime_details
    active = engine.active_model_profile
    details.skill_sources["external"] = ["changed"]
    active.model_id = "external"
    assert engine.current.manifest.runtime_details.skill_sources == {}
    assert engine.current.manifest.active_profile.model_id == "original"
    other = assemble_agent_engine(EventBus(), settings=Settings())
    assert engine.current.manifest.runtime_details is not other.current.manifest.runtime_details


async def test_runtime_update_contains_manifest_values() -> None:
    bus = EventBus()
    events: list[AgentRuntimeUpdated] = []

    async def record(event: AgentRuntimeUpdated) -> None:
        events.append(event)

    await bus.subscribe(AgentRuntimeUpdated, record)
    engine = assemble_agent_engine(bus, settings=Settings())
    engine.loader.apply_skill_refresh(skill_names=["new"], skill_sources={"project": ["new"]}, skill_details=[])
    await RuntimeSkillRefresher(
        current=engine.current, loader=engine.loader, session=engine.session, bus=engine.event_bus
    )._publish_runtime_update(engine.current.manifest, session_id="session")
    event = events[0]
    assert event.skill_names == list(engine.current.manifest.skill_names)
    assert event.runtime_details == engine.current.manifest.runtime_details
    assert event.runtime_details is not engine.current.manifest.runtime_details
    engine.loader.apply_skill_refresh(skill_names=["later"], skill_sources={}, skill_details=[])
    assert event.skill_names == ["new"]
    assert event.runtime_details.skill_sources == {"project": ["new"]}


async def test_warning_delayed_runtime_update_can_publish_after_shutdown() -> None:
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings())
    install_loaded_agent(engine, loaded=make_loaded_agent(), manifest=make_manifest(skill_names=["captured"]))
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[AgentRuntimeUpdated] = []

    async def hold_warning(_event: Warning) -> None:
        entered.set()
        await release.wait()

    await bus.subscribe(Warning, hold_warning)

    async def record(event: AgentRuntimeUpdated) -> None:
        events.append(event)

    await bus.subscribe(AgentRuntimeUpdated, record)
    committed = CommittedSkillRefresh(
        warnings=[SimpleNamespace(code="notice", message="waiting")], manifest=engine.current.manifest
    )
    task = asyncio.create_task(
        RuntimeSkillRefresher(
            current=engine.current, loader=engine.loader, session=engine.session, bus=engine.event_bus
        ).publish_committed_refresh(committed, session_id="original")
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        await engine.shutdown()
        assert engine.current.loaded is None
        release.set()
        await task
        assert events[0].session_id == "original"
        assert events[0].skill_names == ["captured"]
    finally:
        release.set()
        await task


async def test_session_ready_payload_survives_refresh_and_shutdown(
    monkeypatch: pytest.MonkeyPatch, *, engine_services
) -> None:
    bus = EventBus()
    engine = assemble_agent_engine(bus, settings=Settings())
    entered = asyncio.Event()
    release = asyncio.Event()
    ready: list[SessionReady] = []

    async def hold_ready(event: SessionReady) -> None:
        ready.append(event)
        entered.set()
        await release.wait()

    async def install_candidate(_profile, staged) -> CompletedBuild:
        return CompletedBuild(
            staged=staged,
            settings=engine.settings_handle.prepare(staged.loaded),
            workspace_retarget=engine_services(engine).workspace_change_tracker.resolve_retarget(
                staged.workspace_plan.workspace,
                resolve_scope=engine.settings_handle.prepare(staged.loaded).effective.settings.workspace_change_notice,
            ),
            loaded=make_loaded_agent(),
            manifest=make_manifest(skill_names=["ready"]),
            mutation_tracker=None,
            todo_tracker=None,
            compaction_strategy=None,
        )

    monkeypatch.setattr(engine.loader, "build", install_candidate)
    await bus.subscribe(SessionReady, hold_ready)
    task = asyncio.create_task(engine.lifecycle.start(AgentProfile(name="Code"), operation="startup"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        original_details = copy.deepcopy(ready[0].runtime_details)
        engine.loader.apply_skill_refresh(skill_names=["refreshed"], skill_sources={}, skill_details=[])
        await engine.shutdown()
        release.set()
        await task
        assert ready[0].skill_names == ["ready"]
        assert ready[0].runtime_details == original_details
        assert engine.current.manifest.skill_names == ("refreshed",)
    finally:
        release.set()
        await task
        engine.session.guard.release()


def test_build_commit_contains_no_await() -> None:
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(AgentLoader.install)))
    assert not any(isinstance(node, ast.Await) for node in ast.walk(tree))


async def test_successful_rebuild_keeps_pending_injection_notification(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.orchestration.engine import assembly as assembly_module

    async def build_candidate(**_kwargs):
        return _make_build_result(_fresh_approval(engine.event_bus))

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    notification_future = asyncio.get_running_loop().create_future()
    future_ref = weakref.ref(notification_future)

    async def notify(future) -> None:
        await future

    notification = asyncio.create_task(notify(notification_future))
    notification_ref = weakref.ref(notification)
    engine.loader.hold_injection_notification(notification)
    del notification, notification_future
    profile = AgentProfile(name="Code")

    try:
        for _ in range(2):
            staged = engine.loader.stage(
                loaded=engine.loaded_settings,
                agent_profile=profile,
                workspace_plan=plan_workspace(Workspace.from_cwd()),
                hook_manager=None,
            )
            completed = await engine.loader.build(profile, staged)
            replaced = engine.loader.install(completed)
            await engine.loader.release(replaced)
            gc.collect()
            assert notification_ref() is not None
            assert not notification_ref().done()
        await engine.shutdown()
        assert not notification_ref().done()
    finally:
        notification = notification_ref()
        assert notification is not None
        future_ref().set_result(None)
        await notification


async def test_failed_rebuild_preserves_live_resource_and_manifest(monkeypatch: pytest.MonkeyPatch) -> None:
    from chrys.orchestration.engine import assembly as assembly_module

    async def fail_candidate(**_kwargs):
        raise RuntimeError("candidate rejected")

    monkeypatch.setattr(assembly_module, "build_agent", fail_candidate)
    engine = assemble_agent_engine(EventBus(), settings=Settings())
    install_loaded_agent(engine, loaded=make_loaded_agent(), manifest=make_manifest(skill_names=["kept"]))
    loaded = engine.current.loaded
    manifest = engine.current.manifest
    profile = AgentProfile(name="Code")

    staged = engine.loader.stage(
        loaded=engine.loaded_settings,
        agent_profile=profile,
        workspace_plan=plan_workspace(Workspace.from_cwd()),
        hook_manager=None,
    )
    with pytest.raises(RuntimeError, match="candidate rejected"):
        await engine.loader.build(profile, staged)
    assert engine.current.loaded is loaded
    assert engine.current.manifest is manifest
    assert manifest.skill_names == ("kept",)
    await engine.shutdown()


async def test_staged_refresh_can_finish_after_build_closes_without_changing_manifest() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    context = SimpleNamespace(
        warnings=[],
        skill_names=lambda: ["staged"],
        skill_sources=lambda: {"project": ["staged"]},
        skill_details=list,
        render_catalog_reminder=lambda: "staged catalog",
    )

    class Provider:
        async def stage_context_refresh(self):
            entered.set()
            await release.wait()
            return context

    engine = assemble_agent_engine(EventBus(), settings=Settings())
    provider = Provider()
    install_loaded_agent(engine, loaded=make_loaded_agent(skills_provider=provider))
    manifest = engine.current.manifest
    refresher = RuntimeSkillRefresher(
        current=engine.current, loader=engine.loader, session=engine.session, bus=engine.event_bus
    )
    task = asyncio.create_task(refresher.stage_refresh())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        await engine.shutdown()
        release.set()
        staged = await task
        assert staged.provider is provider
        assert staged.skill_names == ["staged"]
        assert engine.current.loaded is None
        assert engine.current.manifest is manifest
        assert await refresher.stage_refresh() is None
        await refresher.publish_committed_refresh(None, session_id="closed")
    finally:
        release.set()
        await task


async def test_multiple_notifications_survive_rebuild_and_close_until_each_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from chrys.orchestration.engine import assembly as assembly_module

    async def build_candidate(**_kwargs):
        return _make_build_result(_fresh_approval(engine.event_bus))

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = assemble_agent_engine(EventBus(), settings=Settings())

    async def notify(future) -> None:
        await future

    task_refs = []
    future_refs = []
    for _ in range(2):
        future = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(notify(future))
        engine.loader.hold_injection_notification(task)
        task_refs.append(weakref.ref(task))
        future_refs.append(weakref.ref(future))
    del task, future
    await asyncio.sleep(0)
    profile = AgentProfile(name="Code")

    try:
        for _ in range(2):
            staged = engine.loader.stage(
                loaded=engine.loaded_settings,
                agent_profile=profile,
                workspace_plan=plan_workspace(Workspace.from_cwd()),
                hook_manager=None,
            )
            completed = await engine.loader.build(profile, staged)
            replaced = engine.loader.install(completed)
            await engine.loader.release(replaced)
            gc.collect()
            assert all(ref() is not None and not ref().done() for ref in task_refs)
        await engine.shutdown()
        assert all(ref() is not None and not ref().done() for ref in task_refs)
    finally:
        for task_ref, future_ref in zip(task_refs, future_refs, strict=True):
            task = task_ref()
            assert task is not None
            future_ref().set_result(None)
            await task
        del task
        await asyncio.sleep(0)
        gc.collect()
        assert all(ref() is None for ref in task_refs)
