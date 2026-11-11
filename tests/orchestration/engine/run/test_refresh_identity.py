# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill refresh admission and publication retain their owning build and session."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentRuntimeUpdated, RuntimeSkillDetails, UserMessage, Warning
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Message
from chrys.orchestration.engine.run.active_injection import ActiveTurnInjector
from chrys.orchestration.engine.run.prompt_content import PromptContentPreparer
from chrys.orchestration.engine.run.retry import RetryCoordinator
from chrys.orchestration.engine.run.runtime_skills import RuntimeSkillRefresher
from chrys.orchestration.engine.run.turn_hooks import PromptSubmitGate
from chrys.orchestration.engine.state.lifecycle_permits import RebuildPermit
from chrys.orchestration.engine.state.machine import EngineStateMachine, Trigger
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import SkillConfig, SkillsConfig
from chrys.service.session.history import SessionHistoryManager
from chrys.service.session.persistence import SessionPersistence
from chrys.service.skills.model import SkillProviderWarning
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE
from tests.orchestration.engine.run.test_lifecycle_hooks import _StagedSkillsProvider
from tests.support.components import make_current, make_permits, make_session, make_turn_state
from tests.support.loaded_agents import SkillRefreshLoader, install_loaded_agent, make_loaded_agent, make_manifest
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import ENGINE_TURN_TIMEOUT, wait_for


class _ParkedProvider(_StagedSkillsProvider):
    def __init__(self) -> None:
        super().__init__([RuntimeSkillDetails(name="captured", description="A skill", source="inline")])
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stage_context_refresh(self):
        staged = await super().stage_context_refresh()
        self.entered.set()
        await self.release.wait()
        return staged


@pytest.fixture
async def refresh_components(tmp_path):
    bus = EventBus()
    current = make_current()
    state = make_turn_state()
    session = make_session(persistence=SessionPersistence(None, bus), workspace=None, approval_mode=None)
    session.reset(session_id="original", workspace=Workspace.from_cwd(str(tmp_path)))
    permits = make_permits(turn_state=state, session=session)
    fsm = EngineStateMachine()
    fsm.transition(Trigger.START)
    fsm.transition(Trigger.USER_MESSAGE)
    history = SessionHistoryManager()
    history.bind({"messages": [Message("user", ["original request"])]})
    provider = _ParkedProvider()
    install_loaded_agent(SimpleNamespace(current=current), loaded=make_loaded_agent(skills_provider=provider))
    loaded = current.loaded
    loaded.bindings.trajectory_context = None
    skills = RuntimeSkillRefresher(current=current, loader=SkillRefreshLoader(current), session=session, bus=bus)
    gate = PromptSubmitGate(session=session, current=current, bus=bus, fsm=fsm)
    content = PromptContentPreparer(session=session, current=current, bus=bus, history=history, fsm=fsm)
    injector = ActiveTurnInjector(
        turn_state=state,
        current=current,
        permits=permits,
        session=session,
        fsm=fsm,
        bus=bus,
        gate=gate,
        content=content,
        skills=skills,
    )
    retry = RetryCoordinator(
        turn_state=state,
        current=current,
        permits=permits,
        session=session,
        fsm=fsm,
        history=history,
        bus=bus,
        trajectory_recorder=TrajectoryRecorder(),
        gate=gate,
        injector=injector,
        content=content,
        skills=skills,
        retry_and_save=None,
    )
    updates = []

    async def record(event):
        updates.append(event)

    await bus.subscribe(AgentRuntimeUpdated, record)
    yield current, state, session, permits, skills, injector, retry, provider, bus, updates
    provider.release.set()
    await loaded.aclose()
    if current.loaded is not loaded:
        await current.loaded.aclose()


def _open_scope(current, state, permits):
    scope = state.lease.begin_current_run_scope(
        owner_admission_id=1,
        session_generation=permits.session_generation,
        build_generation=permits.build_generation,
        reminder_scope=current.loaded.reminder_middleware.create_current_run_scope(),
    )
    state.lease.open_injection_admission(scope)
    return scope


def _replace_build(current):
    install_loaded_agent(
        SimpleNamespace(current=current),
        loaded=make_loaded_agent(),
        manifest=make_manifest(skill_names=["replacement"]),
    )
    return current.manifest


async def test_main_refresh_holds_real_rebuild_permit_until_run_finishes(
    tmp_path,
    monkeypatch,
    agent_engine,
) -> None:
    bus = EventBus()
    settings, registry = make_mock_settings_and_registry()
    settings = replace(settings, workspace_change_notice=False)
    client = MockChatClient(responses=[MockResponse(text="done")])
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    profile = replace(
        _PROFILE,
        skills=SkillsConfig(
            inline=[SkillConfig(name="captured", description="A skill", instructions="A skill")],
            auto_load_user_agents_skills=False,
        ),
    )
    engine = agent_engine(bus, settings=settings, model_registry=registry, state_store=JsonFileStateStore(tmp_path))
    await engine.start(profile)
    loaded = engine.current.loaded
    assert loaded is not None
    entered = asyncio.Event()
    release = asyncio.Event()
    original_stage = RuntimeSkillRefresher.stage_refresh
    updates = []

    async def hold_stage(self):
        staged = await original_stage(self)
        assert staged is not None
        entered.set()
        await release.wait()
        return staged

    async def record(event):
        assert engine.current.loaded is loaded
        updates.append(event)

    monkeypatch.setattr(RuntimeSkillRefresher, "stage_refresh", hold_stage)
    await bus.subscribe(AgentRuntimeUpdated, record)
    await bus.publish(UserMessage(text="hello"))
    await wait_for(entered.is_set, description="main refresh staging", timeout=ENGINE_TURN_TIMEOUT)
    run_task = engine.turn_lifecycle_task
    assert run_task is not None
    token = engine.permits.capture_control_token()

    async def acquire():
        permit = await engine.permits.acquire_rebuild_permit(token)
        assert isinstance(permit, RebuildPermit)
        assert run_task.done()
        engine.permits.release_rebuild_permit(permit)
        return permit

    permit_task = asyncio.create_task(acquire())
    try:
        await wait_for(
            lambda: not engine.turns.turn_state.lease.prompt_admission_open.is_set(),
            description="rebuild admission gate closes",
            timeout=ENGINE_TURN_TIMEOUT,
        )
        assert not permit_task.done()
        assert engine.current.loaded is loaded
        assert updates == []
        release.set()
        await engine.wait_for_run_task()
        permit = await permit_task
        assert isinstance(permit, RebuildPermit)
        assert engine.current.loaded is loaded
        assert len(updates) == 1
        assert updates[0].skill_names == ["captured"]
        assert updates[0].session_id == engine.session_id
        assert engine.turns.turn_state.lease.prompt_admission_open.is_set()
    finally:
        release.set()
        await engine.wait_for_run_task()
        await permit_task


@pytest.mark.parametrize("route", ["injection", "retry_immediate", "retry_deferred"])
async def test_staged_refresh_is_abandoned_when_its_build_changes(refresh_components, route) -> None:
    current, state, _session, permits, _skills, injector, retry, provider, _bus, updates = refresh_components
    if route != "retry_deferred":
        _open_scope(current, state, permits)
    if route == "injection":
        state.lease.run_task = asyncio.current_task()
        task = asyncio.create_task(
            injector.inject(
                "use captured",
                created_at=None,
                route="fsm_active",
                reject_images_without_target=True,
            )
        )
    else:
        owner = retry._capture_retry_note_owner()
        task = asyncio.create_task(retry._commit_retry_note_side_effects("use captured", None, owner, None))
    try:
        await wait_for(provider.entered.is_set, description="staged refresh pauses")
        manifest = _replace_build(current)
        provider.release.set()
        result = await task
        assert result is False if route == "injection" else result == (False, None)
        assert current.manifest is manifest
        assert provider.commit_calls == 0
        assert updates == []
    finally:
        provider.release.set()
        await task
        state.lease.release_run_task()


@pytest.mark.parametrize("deferred", [False, True], ids=["immediate", "promotion"])
async def test_retry_revalidates_owner_changed_by_before_commit(refresh_components, monkeypatch, deferred) -> None:
    current, state, _session, permits, _skills, _injector, retry, provider, _bus, updates = refresh_components
    provider.release.set()
    changed = []

    def before_commit():
        changed.append(_replace_build(current))

    if deferred:
        owner = retry._capture_retry_note_owner()
        accepted, pending = await retry._commit_retry_note_side_effects("use captured", None, owner, None)
        assert accepted is True
        assert pending is not None
        assert provider.commit_calls == 0
        scope = _open_scope(current, state, permits)
        commit_to_target = retry._commit_retry_note_side_effects_to_target

        async def change_before_commit(side_effects, target, *, before_commit=None):
            return await commit_to_target(
                side_effects, target, before_commit=lambda: changed.append(_replace_build(current))
            )

        monkeypatch.setattr(retry, "_commit_retry_note_side_effects_to_target", change_before_commit)
        assert await retry._commit_deferred_retry_note_side_effects(pending, scope) is False
    else:
        _open_scope(current, state, permits)
        owner = retry._capture_retry_note_owner()
        assert await retry._commit_retry_note_side_effects(
            "use captured",
            None,
            owner,
            None,
            before_commit=before_commit,
        ) == (False, None)
    assert len(changed) == 1
    assert current.manifest is changed[0]
    assert provider.commit_calls == 0
    assert updates == []


async def test_committed_manifest_and_session_survive_warning_await(refresh_components) -> None:
    current, _state, session, _permits, skills, _injector, _retry, provider, bus, updates = refresh_components
    provider.release.set()
    provider._commit_warnings = [SkillProviderWarning(code="notice", message="wait")]
    staged = await skills.stage_refresh()
    committed = skills.commit_staged_refresh(staged)
    assert committed is not None
    captured_manifest = current.manifest
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_warning(_event):
        entered.set()
        await release.wait()

    await bus.subscribe(Warning, hold_warning)
    task = asyncio.create_task(skills.publish_committed_refresh(committed, session_id=session.session_id))
    try:
        await wait_for(entered.is_set, description="committed refresh warning pauses")
        replacement = _replace_build(current)
        session.reset(session_id="replacement", workspace=session.workspace)
        release.set()
        await task
        assert current.manifest is replacement
        assert len(updates) == 1
        assert updates[0].session_id == "original"
        assert updates[0].skill_names == ["captured"]
        assert updates[0].runtime_details == captured_manifest.runtime_details
        assert updates[0].runtime_details is not captured_manifest.runtime_details
    finally:
        release.set()
        await task


@pytest.mark.parametrize("main_refresh", [True, False], ids=["main", "admitted-side-effect"])
async def test_provider_commit_failure_keeps_the_callers_error_policy(
    refresh_components, monkeypatch, caplog, main_refresh
) -> None:
    _current, _state, _session, _permits, skills, _injector, _retry, provider, _bus, updates = refresh_components
    provider.release.set()
    calls = []

    def fail_commit(context):
        calls.append(context)
        raise RuntimeError("provider commit failed")

    monkeypatch.setattr(provider, "commit_context_refresh", fail_commit)
    if main_refresh:
        await skills.refresh()
        assert "Failed to refresh runtime skills" in caplog.text
    else:
        staged = await skills.stage_refresh()
        with pytest.raises(RuntimeError, match="provider commit failed"):
            skills.commit_staged_refresh(staged)
    assert len(calls) == 1
    assert updates == []


async def test_main_refresh_does_not_swallow_manifest_installation_failure(refresh_components, monkeypatch) -> None:
    _current, _state, _session, _permits, skills, _injector, _retry, provider, _bus, updates = refresh_components
    provider.release.set()
    calls = []

    def fail_install(self, **values):
        calls.append(values)
        raise RuntimeError("manifest installation failed")

    monkeypatch.setattr(SkillRefreshLoader, "apply_skill_refresh", fail_install)
    with pytest.raises(RuntimeError, match="manifest installation failed"):
        await skills.refresh()
    assert len(calls) == 1
    assert provider.commit_calls == 1
    assert updates == []
