# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Candidate preparation failures, resource cleanup, and atomic installation."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import PropertyMock

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import AgentLoadFailed, AgentLoadFinished, SessionReady
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import Message
from chrys.orchestration.engine import assembly as assembly_module
from chrys.orchestration.engine.build import construction
from chrys.orchestration.engine.build.loaded import AgentManifest
from chrys.orchestration.engine.state.machine import Trigger
from chrys.service.context.compaction import UnifiedContextStrategy
from chrys.service.session.runtime_metadata import CONTEXT_CALIBRATION_VERSION
from tests.orchestration.engine.test_agent_loader import _candidate_result, _profile


class _Coordinator:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _live_snapshot(engine, *, engine_services):
    tracker = engine_services(engine).workspace_change_tracker
    return (
        engine.settings_handle._base,
        engine.loaded_settings,
        vars(engine.session).copy(),
        engine.current.loaded,
        engine.current.manifest,
        engine_services(engine).history.state,
        tracker._scope,
        tracker.baseline,
        tracker._generation,
        tracker._pending_safety,
        engine.build_generation,
    )


def _assert_same_live(engine, before, *, engine_services):
    after = _live_snapshot(engine, engine_services=engine_services)
    for index, (old, new) in enumerate(zip(before, after, strict=True)):
        if index in {2, 8, 10}:
            assert new == old
        else:
            assert new is old


@pytest.mark.parametrize(
    "preparation",
    [
        "settings",
        "wiring",
        "last_words_restore",
        "pointer_restore",
        "history_ids",
        "manifest",
        "calibration",
        "workspace",
    ],
)
async def test_preparation_failure_closes_candidates_and_preserves_all_live_values(
    preparation, monkeypatch, agent_engine, tmp_path, *, engine_services
) -> None:
    bus = EventBus()
    results = []

    async def build_candidate(**kwargs):
        result = _candidate_result(bus)
        result.compaction_strategy = UnifiedContextStrategy()
        results.append(result)
        return result

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings(workspace_change_notice=False))
    profile = _profile()
    await engine.start(profile)
    engine.current.loaded.bindings.backend.history_state["messages"] = [Message("user", ["preserved"])]
    engine_services(engine).workspace_change_tracker.retarget_roots(Workspace(primary_cwd=str(tmp_path)))
    engine_services(engine).workspace_change_tracker.capture_baseline(3)
    engine_services(engine).workspace_change_tracker.queue_safety_notice("keep safety", cwd=str(tmp_path))
    staged_loaded = LoadedSettings(settings=Settings(locale="zh-Hans", workspace_change_notice=False), provenance={})
    candidate = _candidate_result(bus)
    candidate.compaction_strategy = UnifiedContextStrategy()
    coordinator = _Coordinator()
    stage = engine.loader.stage

    def candidate_stage(**kwargs):
        return replace(stage(**kwargs), mutation_coordinator=coordinator)

    async def next_candidate(**kwargs):
        return candidate

    def fail(*args, **kwargs):
        raise RuntimeError("preparation failed")

    monkeypatch.setattr(engine.loader, "stage", candidate_stage)
    monkeypatch.setattr(engine.loader, "_build_agent_fn", next_candidate)
    if preparation == "settings":
        engine.settings_handle.override(theme="chrys-legacy")
        monkeypatch.setattr(LoadedSettings, "overlay", fail)
    elif preparation == "wiring":
        monkeypatch.setattr(
            type(candidate.loop_recorder),
            "on_pre_wire_barrier",
            PropertyMock(side_effect=RuntimeError("preparation failed")),
            raising=False,
        )
    elif preparation == "last_words_restore":
        monkeypatch.setattr(candidate.last_words, "restore", fail)
    elif preparation == "pointer_restore":
        monkeypatch.setattr(candidate.reminder_middleware.sources.archive_pointer, "restore_record_count", fail)
    elif preparation == "history_ids":
        monkeypatch.setattr(construction, "stamp_history_item_ids", fail)
    elif preparation == "manifest":
        monkeypatch.setattr(AgentManifest, "from_build", fail)
    elif preparation == "calibration":
        engine.session.runtime_meta.context_calibration = {
            "v": CONTEXT_CALIBRATION_VERSION,
            "system_overhead_tokens": 0,
            "calibration_ratio": 10**400,
            "agent_profile_fingerprint": "agent-fp",
            "model_profile_fingerprint": "model-fp",
        }
    else:
        monkeypatch.setattr(engine_services(engine).workspace_change_tracker, "resolve_retarget", fail)
    events = []

    async def collect(event):
        events.append(event)

    for event in (AgentLoadFailed, AgentLoadFinished, SessionReady):
        await bus.subscribe(event, collect)
    before = _live_snapshot(engine, engine_services=engine_services)
    error = OverflowError if preparation == "calibration" else RuntimeError
    with pytest.raises(error):
        await engine.loader.reload(profile, staged_loaded=staged_loaded)
    _assert_same_live(engine, before, engine_services=engine_services)
    if preparation == "settings":
        # Settings are prepared when the build is staged, so their failure comes before any candidate exists.
        assert candidate.bindings.closed is False
        assert coordinator.closed is False
    else:
        assert candidate.bindings.closed is True
        assert coordinator.closed is True
    assert results[0].bindings.closed is False
    assert [type(event) for event in events] == [AgentLoadFailed]
    assert engine.permits.agent_loading is False


async def test_preparation_cleanup_cancellation_still_closes_both_candidate_owners(monkeypatch, agent_engine) -> None:
    bus = EventBus()
    candidate = _candidate_result(bus)

    async def build_candidate(**kwargs):
        return candidate

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    profile = _profile()
    coordinator = _Coordinator()
    staged = replace(
        engine.loader.stage(loaded=engine.loaded_settings, agent_profile=profile, workspace=None, hook_manager=None),
        mutation_coordinator=coordinator,
    )

    def fail(*args):
        raise RuntimeError("preparation failed")

    async def close_then_cancel(owner, *, reason):
        owner.close()
        raise asyncio.CancelledError("coordinator cleanup cancelled")

    monkeypatch.setattr(AgentManifest, "from_build", fail)
    monkeypatch.setattr(construction, "_close_coordinator", close_then_cancel)
    manifest = engine.current.manifest
    with pytest.raises(asyncio.CancelledError, match="coordinator cleanup cancelled"):
        await engine.loader.build(profile, staged)
    assert coordinator.closed is True
    assert candidate.bindings.closed is True
    assert engine.current.loaded is None
    assert engine.current.manifest is manifest
    assert engine.session.mutation_coordinator is None
    assert engine.build_generation == 0


async def test_start_failure_after_install_keeps_new_owner_and_releases_old(
    monkeypatch, agent_engine, *, engine_services
) -> None:
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
    generation = engine.build_generation
    transition = engine_services(engine).fsm.try_transition

    def fail_start(trigger):
        if trigger is Trigger.START:
            raise RuntimeError("post-install failure")
        return transition(trigger)

    monkeypatch.setattr(engine_services(engine).fsm, "try_transition", fail_start)
    with pytest.raises(RuntimeError, match="post-install failure"):
        await engine.lifecycle.start(_profile())
    assert engine.current.loaded is not old
    assert engine.current.loaded.bindings is results[1].bindings
    assert engine.build_generation == generation + 1
    assert results[0].bindings.closed is True
    assert results[1].bindings.closed is False


@pytest.mark.parametrize("ratio", [float("nan"), float("inf"), 1000.0])
async def test_rejected_calibration_keeps_candidate_defaults_and_build_succeeds(
    ratio, monkeypatch, agent_engine
) -> None:
    bus = EventBus()
    candidate = _candidate_result(bus)
    candidate.compaction_strategy = UnifiedContextStrategy()

    async def build_candidate(**kwargs):
        return candidate

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    engine.session.runtime_meta.context_calibration = {
        "v": CONTEXT_CALIBRATION_VERSION,
        "system_overhead_tokens": 0,
        "calibration_ratio": ratio,
        "agent_profile_fingerprint": "agent-fp",
        "model_profile_fingerprint": "model-fp",
    }
    await engine.start(_profile())
    assert engine.current.loaded.bindings is candidate.bindings
    assert candidate.compaction_strategy.calibration_initialized is False
    assert candidate.compaction_strategy.calibration_ratio == 1.0


@pytest.mark.parametrize("enabled", [False, True])
async def test_workspace_preparation_reads_runtime_overlay(
    enabled, monkeypatch, agent_engine, *, engine_services
) -> None:
    bus = EventBus()

    async def build_candidate(**kwargs):
        return _candidate_result(bus)

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings(workspace_change_notice=not enabled))
    engine.settings_handle.override(workspace_change_notice=enabled)
    resolve = engine_services(engine).workspace_change_tracker.resolve_retarget
    seen = []

    def observe(workspace, *, resolve_scope):
        seen.append(resolve_scope)
        return resolve(workspace, resolve_scope=resolve_scope)

    monkeypatch.setattr(engine_services(engine).workspace_change_tracker, "resolve_retarget", observe)
    await engine.start(_profile())
    assert seen == [enabled]
    assert engine.settings.workspace_change_notice is enabled


async def test_candidate_history_stamps_occurrences_before_install(
    monkeypatch, agent_engine, *, engine_services
) -> None:
    bus = EventBus()
    candidate = _candidate_result(bus)
    message = Message("user", ["legacy"], message_id="msg_7")
    candidate.bindings.backend.history_state["messages"] = [message]

    async def build_candidate(**kwargs):
        return candidate

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    profile = _profile()
    completed = await engine.loader.build(
        profile,
        engine.loader.stage(loaded=engine.loaded_settings, agent_profile=profile, workspace=None, hook_manager=None),
    )
    assert engine.current.loaded is None
    item_id = read_analytics_item_id(message.additional_properties)
    assert item_id
    replaced = engine.loader.install(completed)
    await engine.loader.release(replaced)
    assert engine_services(engine).history.state["messages"][0] is message
    assert read_analytics_item_id(message.additional_properties) == item_id
    assert message.message_id == "msg_7"


async def test_release_coordinator_cancellation_still_closes_old_build(monkeypatch, agent_engine) -> None:
    from chrys.orchestration.engine.build.loaded import ReplacedBuild
    from tests.support.loaded_agents import make_loaded_agent

    bus = EventBus()
    engine = agent_engine(bus, settings=Settings())
    coordinator = _Coordinator()
    old = _candidate_result(bus)

    async def close_then_cancel(owner, *, reason):
        owner.close()
        raise asyncio.CancelledError("release cancelled")

    monkeypatch.setattr(construction, "_close_coordinator", close_then_cancel)
    with pytest.raises(asyncio.CancelledError, match="release cancelled"):
        await engine.loader.release(
            ReplacedBuild(loaded=make_loaded_agent(prepared=old.prepared), coordinator=coordinator)
        )
    assert coordinator.closed is True
    assert old.bindings.closed is True
    assert engine.current.loaded is None


async def test_injection_notification_captures_session_before_checkpoint_yields(monkeypatch, agent_engine) -> None:
    from types import SimpleNamespace

    from chrys.foundation.events.types import UserInjectResult

    bus = EventBus()
    delivered = asyncio.Event()
    events = []

    async def collect(event):
        events.append(event)
        delivered.set()

    await bus.subscribe(UserInjectResult, collect)

    async def build_candidate(**kwargs):
        return _candidate_result(bus)

    monkeypatch.setattr(assembly_module, "build_agent", build_candidate)
    engine = agent_engine(bus, settings=Settings())
    await engine.start(_profile())
    original_id = engine.session.session_id

    async def checkpoint():
        engine.session.session_id = "next-session"
        await asyncio.sleep(0)

    async def call_next():
        return None

    monkeypatch.setattr(engine.writer, "save_checkpoint", checkpoint)
    injection = engine.current.loaded.injection
    injection.queue("delayed delivery", injection_id="queued")
    try:
        await injection.process(SimpleNamespace(messages=[Message("user", ["anchor"])], options=None), call_next)
        await asyncio.wait_for(delivered.wait(), timeout=5)
        assert len(events) == 1
        assert events[0].session_id == original_id
        assert events[0].injection_id == "queued"
        assert events[0].text == "delayed delivery"
    finally:
        engine.session.session_id = original_id
