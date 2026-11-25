# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end coverage for ``SessionRuntimeMetadata`` across the full engine.

Exercises the live ``_runtime_meta`` ↔ ``executor.backend.history_state`` ↔ disk
round-trip across every lifecycle path that touches usage metadata:

* save → shutdown → restore in a fresh engine
* soft-restart via ``AgentProfileSwitch`` / ``SettingsReload``
* sub-agent usage accumulation
* rollback to a prior snapshot and to the welcome state
* defensive recovery from a corrupt persisted ``last_usage``
* session deletion via the event bus

Each test patches ``chrys.orchestration.engine.build.builder.create_client`` so the real
``TurnBindings`` / build path runs without a live model — the metadata
contract is what we validate, not the LLM.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentProfileSwitch,
    Error,
    SessionClear,
    SessionDelete,
    SessionDeleted,
    SessionNew,
    SessionRestore,
    SessionRestored,
    SettingsReload,
    UsageUpdate,
    UserMessage,
    UserRollback,
)
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.hooks.events import HookEvent
from chrys.service.hooks.schema import HookDecision
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.session.runtime_metadata import SessionRuntimeMetadata
from chrys.service.state.store import JsonFileStateStore
from tests.support.async_tasks import wait_for_completion, wait_for_started
from tests.support.event_capture import collect_events
from tests.support.waiting import wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import Iterator


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


_CODE = AgentProfile(
    name="Code",
    display_name="Code Agent",
    instructions="You are a coding assistant.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)

_EXPLORE = AgentProfile(
    name="Explore",
    display_name="Explore Agent",
    instructions="You are an exploration assistant.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)


def _registry(*profiles: AgentProfile) -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    for profile in profiles:
        registry.register(profile)
    return registry


@pytest.fixture
def patch_create_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[MockChatClient]]:
    """Replace the build-time client factory with a fresh MockChatClient per build."""
    clients: list[MockChatClient] = []

    async def _factory(_settings=None, **_kwargs):
        client = MockChatClient(responses=[MockResponse(text=f"reply-{len(clients)}") for _ in range(20)])
        clients.append(client)
        return client

    monkeypatch.setattr(builder_module, "create_client", _factory)
    yield clients


def _filter(events: list, cls: type) -> list:
    return [e for e in events if isinstance(e, cls)]


@dataclass
class _TurnUsage:
    """Per-turn usage payload to inject after each ``UserMessage``."""

    input_tokens: int
    output_tokens: int
    total_tokens: int = 0
    local_tokens: int = 0
    calibration_ratio: float = 1.0
    system_overhead_tokens: int = 0


async def _drive_turn(
    engine: AgentEngine,
    bus: EventBus,
    text: str,
    usage: _TurnUsage | None = None,
) -> None:
    """Publish a user message, wait for completion, optionally inject usage and re-save.

    Real middleware would call ``engine.usage_publisher.publish_usage(...)`` while the run is
    in flight; with ``MockChatClient`` no usage_details flow through the
    middleware, so we simulate the effect after the run completes and re-save
    so the next snapshot captures the new metadata.
    """
    await bus.publish(UserMessage(text=text))
    # ``on_user_message`` schedules ``run_and_save`` as a background task and
    # returns; we must explicitly await that task before injecting synthetic
    # usage, otherwise the next ``_save_current_session`` could race with
    # ``_post_run`` and metadata assertions would observe an in-flight turn.
    run_task = engine.turns.turn_state.lease.run_task
    if run_task is not None and not run_task.done():
        try:
            # Generous ceiling: a loaded CI runner (parallel xdist on Windows)
            # can stall a mock turn for many seconds; the happy path never
            # waits this long because the wait ends at task completion.
            await asyncio.wait_for(asyncio.shield(run_task), timeout=45.0)
        except TimeoutError:
            pytest.fail(
                f"Run task for {text!r} did not complete within 45s — "
                "metadata assertions would race against an in-flight turn."
            )
    if usage is not None:
        engine.usage_publisher.publish_usage(
            total_tokens=usage.total_tokens,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            local_tokens=usage.local_tokens,
            calibration_ratio=usage.calibration_ratio,
            system_overhead_tokens=usage.system_overhead_tokens,
        )
        await engine.writer.save_current_session()


class _SessionEndProbe:
    """Hook-manager stand-in modelling an ``async``-mode ``session_end`` hook.

    Like the real manager, ``fire()`` only spawns the hook; it runs to
    completion when a drain awaits it.  Each completion records whether the
    session file still existed, so a delete that happens between fire and
    drain shows up as ``False``.
    """

    def __init__(self, session_file: Path) -> None:
        self._session_file = session_file
        self.session_end_payloads: list[dict[str, Any]] = []
        self.file_existed_at_completion: list[bool] = []
        self.drain_close_flags: list[bool] = []
        self._pending: list[asyncio.Task[None]] = []
        self._release = asyncio.Event()

    async def fire(self, event: object, payload: dict[str, Any], **_kwargs: object) -> HookDecision:
        if event is HookEvent.SESSION_END:
            self.session_end_payloads.append(payload)
            self._pending.append(asyncio.create_task(self._run_hook()))
        return HookDecision()

    async def _run_hook(self) -> None:
        await self._release.wait()
        self.file_existed_at_completion.append(self._session_file.exists())

    async def drain_session(self, *, close: bool = True) -> None:
        self.drain_close_flags.append(close)
        self._release.set()
        pending, self._pending = self._pending, []
        await asyncio.gather(*pending)
        self._release.clear()


def _probe_session_end(engine: AgentEngine, store: JsonFileStateStore, session_id: str) -> _SessionEndProbe:
    probe = _SessionEndProbe(store.session_dir(session_id) / "session.json")
    engine.session.hook_manager = probe  # type: ignore[assignment]
    return probe


def _build_engine(
    tmp_path: Path,
    bus: EventBus,
    *profiles: AgentProfile,
    agent_engine,
) -> tuple[AgentEngine, JsonFileStateStore]:
    store = JsonFileStateStore(tmp_path)
    registry = _registry(*(profiles or (_CODE,)))
    engine = agent_engine(bus, settings=Settings(), agent_registry=registry, state_store=store)
    return engine, store


# ---------------------------------------------------------------------------
# Foundational round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_then_restore_round_trips_metadata_in_fresh_engine(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Persisted token totals + ``last_usage`` survive shutdown and a fresh engine restore."""
    bus_a = EventBus()
    engine_a, store = _build_engine(tmp_path, bus_a, agent_engine=agent_engine)
    await engine_a.start(_CODE)

    await _drive_turn(
        engine_a,
        bus_a,
        "first turn",
        _TurnUsage(input_tokens=120, output_tokens=80, total_tokens=200, calibration_ratio=1.05),
    )

    saved_session_id = engine_a.session.session_id
    assert engine_a.session.runtime_meta.total_session_tokens == 200
    await engine_a.shutdown()

    bus_b = EventBus()
    restored: list[SessionRestored] = []
    await bus_b.subscribe(SessionRestored, lambda e: collect_events(restored, e))

    engine_b = agent_engine(bus_b, settings=Settings(), agent_registry=_registry(_CODE), state_store=store)
    await engine_b.start(_CODE)
    await bus_b.publish(SessionRestore(session_id=saved_session_id))
    assert await wait_until(lambda: bool(restored)), "SessionRestored never arrived"

    assert restored and restored[0].session_id == saved_session_id
    assert engine_b.session.runtime_meta.total_session_tokens == 200
    assert engine_b.session.runtime_meta.total_session_input_tokens == 120
    assert engine_b.session.runtime_meta.total_session_output_tokens == 80
    assert engine_b.session.runtime_meta.last_usage_details["calibration_ratio"] == 1.05
    assert engine_b.session.runtime_meta.last_usage_details["total_token_count"] == 200


@pytest.mark.asyncio
async def test_publish_usage_emits_event_with_cumulative_totals(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """``UsageUpdate`` payload reflects the live ``_runtime_meta`` totals."""
    bus = EventBus()
    seen: list[UsageUpdate] = []
    await bus.subscribe(UsageUpdate, lambda e: collect_events(seen, e))
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(total_tokens=50, input_tokens=30, output_tokens=20, calibration_ratio=1.1)
    engine.usage_publisher.publish_usage(total_tokens=70, input_tokens=40, output_tokens=30, calibration_ratio=1.2)
    assert await wait_until(lambda: len(seen) >= 2), "both UsageUpdate events never arrived"

    # Both calls update the cumulative session totals (30+40=70 in, 20+30=50 out).
    last = seen[-1]
    assert last.total_session_tokens == 120
    assert last.total_session_input_tokens == 70
    assert last.total_session_output_tokens == 50
    assert last.calibration_ratio == 1.2
    assert last.agent_profile == "Code"
    assert last.usage_source_id == engine.session.session_id
    # The most-recent per-call usage replaces last_usage_details.
    assert last.input_tokens == 40
    assert last.output_tokens == 30
    assert last.total_tokens == 70


@pytest.mark.asyncio
async def test_publish_aggregate_usage_emits_window_context_and_raw_session_totals(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    bus = EventBus()
    seen: list[UsageUpdate] = []
    await bus.subscribe(UsageUpdate, lambda event: collect_events(seen, event))
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(
        total_tokens=275_586,
        input_tokens=273_399,
        output_tokens=2_187,
        local_tokens=20_240,
        cache_hit_tokens=259_200,
        use_local_context_estimate=True,
    )
    assert await wait_until(lambda: bool(seen)), "aggregate UsageUpdate never arrived"

    event = seen[-1]
    assert (event.input_tokens, event.output_tokens, event.total_tokens) == (20_240, 2_187, 22_427)
    assert event.pct == 11.2
    assert event.max_context_tokens == 200_000
    assert event.total_session_tokens == 275_586
    assert event.total_session_input_tokens == 273_399
    assert event.total_session_output_tokens == 2_187
    assert event.total_session_cache_hit_tokens == 259_200


@pytest.mark.asyncio
async def test_publish_usage_writes_calibration_only_when_initialized_with_cached_fingerprints(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(
        total_tokens=10,
        input_tokens=8,
        output_tokens=2,
        calibration_ratio=1.0,
        system_overhead_tokens=0,
    )
    assert engine.session.runtime_meta.context_calibration is None

    engine.usage_publisher.publish_usage(
        total_tokens=12,
        input_tokens=10,
        output_tokens=2,
        calibration_ratio=1.2,
        system_overhead_tokens=7,
        calibration_initialized=True,
    )

    assert engine.session.runtime_meta.context_calibration == {
        "v": 2,
        "system_overhead_tokens": 7,
        "calibration_ratio": 1.2,
        "model_profile_fingerprint": engine.current.manifest.model_profile_fingerprint,
        "agent_profile_fingerprint": engine.current.manifest.agent_profile_fingerprint,
    }


@pytest.mark.asyncio
async def test_restore_into_running_engine_does_not_leak_previous_sessions_calibration(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Switching sessions on a live engine must not carry calibration across.

    ``_hydrate_restored_session`` rebuilds the agent via ``engine.start(...,
    operation="restore")``, and construction hydrates the fresh strategy from
    ``engine.session.runtime_meta``.  The target session's metadata therefore has to
    be installed *before* that build: with the old ordering, restoring a
    session without a calibration record while the engine still held one
    (same profile → matching fingerprints) hydrated the new strategy from the
    previous session's record.
    """
    bus_a = EventBus()
    engine_a, store = _build_engine(tmp_path, bus_a, agent_engine=agent_engine)
    await engine_a.start(_CODE)
    await _drive_turn(engine_a, bus_a, "calibrated turn", _TurnUsage(input_tokens=10, output_tokens=5, total_tokens=15))
    engine_a.usage_publisher.publish_usage(
        total_tokens=20,
        input_tokens=15,
        output_tokens=5,
        calibration_ratio=1.2,
        system_overhead_tokens=7,
        calibration_initialized=True,
    )
    await engine_a.writer.save_current_session()
    sid_with_record = engine_a.session.session_id
    await engine_a.shutdown()

    bus_a2 = EventBus()
    engine_a2, _ = _build_engine(tmp_path, bus_a2, agent_engine=agent_engine)
    await engine_a2.start(_CODE)
    await _drive_turn(engine_a2, bus_a2, "plain turn", _TurnUsage(input_tokens=4, output_tokens=2, total_tokens=6))
    sid_without_record = engine_a2.session.session_id
    assert engine_a2.session.runtime_meta.context_calibration is None
    await engine_a2.shutdown()
    assert sid_with_record != sid_without_record

    bus_b = EventBus()
    restored: list[SessionRestored] = []
    await bus_b.subscribe(SessionRestored, lambda e: collect_events(restored, e))
    engine_b = agent_engine(bus_b, settings=Settings(), agent_registry=_registry(_CODE), state_store=store)
    await engine_b.start(_CODE)

    # Sanity: restoring the calibrated session hydrates the fresh strategy.
    await bus_b.publish(SessionRestore(session_id=sid_with_record))
    assert await wait_until(lambda: len(restored) >= 1), "first SessionRestored never arrived"
    strategy = engine_b.current.loaded.bindings.backend.compaction_strategy
    assert strategy is not None and strategy.calibration_initialized
    assert strategy.system_overhead_tokens == 7
    assert strategy.calibration_ratio == 1.2

    # Now switch — same engine, same profile/fingerprints — to the session
    # WITHOUT a record: nothing may survive from the previous session.
    await bus_b.publish(SessionRestore(session_id=sid_without_record))
    assert await wait_until(lambda: len(restored) >= 2), "second SessionRestored never arrived"
    assert engine_b.session.session_id == sid_without_record
    assert engine_b.session.runtime_meta.context_calibration is None
    strategy = engine_b.current.loaded.bindings.backend.compaction_strategy
    assert strategy is not None and not strategy.calibration_initialized


# ---------------------------------------------------------------------------
# Soft-restart paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_restart_via_profile_switch_keeps_runtime_meta_instance(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Profile switch rebuilds the agent but the engine's ``_runtime_meta`` instance survives.

    ``last_usage_details`` is overwritten from preserved state (matching the
    original contract); the totals stay live on the in-memory dataclass.
    """
    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, _CODE, _EXPLORE, agent_engine=agent_engine)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "before switch", _TurnUsage(input_tokens=10, output_tokens=15, total_tokens=25))
    pre_switch_meta = engine.session.runtime_meta
    pre_total = pre_switch_meta.total_session_tokens
    pre_last_usage = dict(pre_switch_meta.last_usage_details)

    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: engine.agent_profile is not None and engine.agent_profile.name == "Explore"), (
        "profile switch to Explore never completed"
    )

    # The dataclass instance is the same object (engine field, not replaced).
    assert engine.session.runtime_meta is pre_switch_meta
    # Cumulative totals carry across the soft restart unchanged.
    assert engine.session.runtime_meta.total_session_tokens == pre_total
    # Soft-restart restores last_usage from preserved state.
    assert engine.session.runtime_meta.last_usage_details == pre_last_usage

    # A subsequent usage write keeps accumulating on the same dataclass.
    engine.usage_publisher.publish_usage(total_tokens=11, input_tokens=4, output_tokens=7)
    assert await wait_until(lambda: engine.session.runtime_meta.total_session_tokens == pre_total + 11), (
        "subsequent usage write never accumulated"
    )


@pytest.mark.asyncio
async def test_settings_reload_preserves_runtime_metadata(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """``SettingsReload`` rebuilds the agent and must not drop runtime metadata."""
    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "before reload", _TurnUsage(input_tokens=22, output_tokens=33, total_tokens=55))
    pre = SessionRuntimeMetadata(
        total_session_tokens=engine.session.runtime_meta.total_session_tokens,
        total_session_input_tokens=engine.session.runtime_meta.total_session_input_tokens,
        total_session_output_tokens=engine.session.runtime_meta.total_session_output_tokens,
        last_usage_details=dict(engine.session.runtime_meta.last_usage_details),
    )

    pre_executor = engine.current.loaded.bindings
    await bus.publish(SettingsReload())
    assert await wait_until(lambda: engine.current.loaded.bindings is not pre_executor), (
        "settings reload never rebuilt the executor"
    )

    assert engine.session.runtime_meta.total_session_tokens == pre.total_session_tokens
    assert engine.session.runtime_meta.total_session_input_tokens == pre.total_session_input_tokens
    assert engine.session.runtime_meta.total_session_output_tokens == pre.total_session_output_tokens
    assert engine.session.runtime_meta.last_usage_details == pre.last_usage_details


@pytest.mark.asyncio
async def test_consecutive_profile_switches_preserve_cumulative_totals(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Cumulative session totals survive two soft-restarts in a row."""
    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, _CODE, _EXPLORE, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(total_tokens=100, input_tokens=60, output_tokens=40)
    await engine.writer.save_current_session()

    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: engine.agent_profile is not None and engine.agent_profile.name == "Explore"), (
        "profile switch to Explore never completed"
    )

    engine.usage_publisher.publish_usage(total_tokens=50, input_tokens=20, output_tokens=30)
    await engine.writer.save_current_session()

    await bus.publish(AgentProfileSwitch(profile_name="Code"))
    assert await wait_until(lambda: engine.agent_profile is not None and engine.agent_profile.name == "Code"), (
        "profile switch back to Code never completed"
    )

    assert engine.session.runtime_meta.total_session_tokens == 150
    assert engine.session.runtime_meta.total_session_input_tokens == 80
    assert engine.session.runtime_meta.total_session_output_tokens == 70


# ---------------------------------------------------------------------------
# Sub-agent usage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sub_agent_usage_accumulates_without_replacing_last_usage(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Sub-agent usage updates session totals and emits live events without replacing parent last_usage."""
    bus = EventBus()
    seen: list[UsageUpdate] = []
    await bus.subscribe(UsageUpdate, lambda e: collect_events(seen, e))
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(total_tokens=40, input_tokens=20, output_tokens=20, calibration_ratio=1.3)
    parent_last_usage = dict(engine.session.runtime_meta.last_usage_details)

    engine.usage_publisher.accumulate_invocation_usage(
        total_tokens=30,
        input_tokens=12,
        output_tokens=18,
        max_context_tokens=60,
        usage_source_id="sub-usage-1",
    )
    engine.usage_publisher.accumulate_invocation_usage(
        total_tokens=10,
        input_tokens=4,
        output_tokens=6,
        local_tokens=9,
        calibration_ratio=1.1,
        system_overhead_tokens=2,
        max_context_tokens=50,
        usage_source_id="sub-usage-1",
    )
    assert await wait_until(lambda: any(e.total_tokens == 10 and e.max_context_tokens == 50 for e in seen)), (
        "second sub-agent UsageUpdate never arrived"
    )

    assert engine.session.runtime_meta.total_session_tokens == 40 + 30 + 10
    assert engine.session.runtime_meta.total_session_input_tokens == 20 + 12 + 4
    assert engine.session.runtime_meta.total_session_output_tokens == 20 + 18 + 6
    live_sub_usage = [event for event in seen if event.total_tokens == 10 and event.max_context_tokens == 50]
    assert live_sub_usage
    assert live_sub_usage[-1].pct == 20.0
    assert live_sub_usage[-1].total_session_tokens == 40 + 30 + 10
    assert live_sub_usage[-1].local_tokens == 9
    assert live_sub_usage[-1].calibration_ratio == 1.1
    assert live_sub_usage[-1].system_overhead_tokens == 2
    assert live_sub_usage[-1].agent_profile == ""
    assert live_sub_usage[-1].usage_source_id == "sub-usage-1"
    # Sub-agent usage must not overwrite the parent-agent's last per-call snapshot.
    assert engine.session.runtime_meta.last_usage_details == parent_last_usage


@pytest.mark.asyncio
async def test_sub_agent_aggregate_usage_emits_window_context_and_accumulates_raw_billing(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    bus = EventBus()
    seen: list[UsageUpdate] = []
    await bus.subscribe(UsageUpdate, lambda event: collect_events(seen, event))
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.accumulate_invocation_usage(
        total_tokens=80_500,
        input_tokens=80_000,
        output_tokens=500,
        local_tokens=10_000,
        max_context_tokens=100_000,
        usage_source_id="sub-deepseek",
        use_local_context_estimate=True,
    )
    assert await wait_until(lambda: bool(seen)), "sub-agent aggregate UsageUpdate never arrived"

    event = seen[-1]
    assert (event.input_tokens, event.output_tokens, event.total_tokens) == (10_000, 500, 10_500)
    assert event.pct == 10.5
    assert event.total_session_tokens == 80_500
    assert event.total_session_input_tokens == 80_000
    assert event.total_session_output_tokens == 500


@pytest.mark.asyncio
async def test_side_call_usage_accumulates_without_replacing_last_usage(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Phase-4 side-call usage folds into session totals and refreshes the
    panel via one UsageUpdate whose window numbers still describe the
    parent's last call (the context meter must not see side-call spend)."""
    bus = EventBus()
    seen: list[UsageUpdate] = []
    await bus.subscribe(UsageUpdate, lambda e: collect_events(seen, e))
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(total_tokens=40, input_tokens=20, output_tokens=20, calibration_ratio=1.3)
    parent_last_usage = dict(engine.session.runtime_meta.last_usage_details)

    # Empty/zero usage (mock providers) is dropped entirely — no event.
    engine.usage_publisher.accumulate_side_call_usage({})
    engine.usage_publisher.accumulate_side_call_usage({"input_token_count": 0, "output_token_count": 0})
    # A real side-call report — raw provider usage_details shape.
    engine.usage_publisher.accumulate_side_call_usage(
        {
            "input_token_count": 12,
            "output_token_count": 8,
            "total_token_count": 20,
            "cache_read_input_token_count": 5,
        }
    )
    assert await wait_until(lambda: any(e.total_session_tokens == 60 for e in seen)), (
        "side-call UsageUpdate never arrived"
    )

    assert engine.session.runtime_meta.total_session_tokens == 40 + 20
    assert engine.session.runtime_meta.total_session_input_tokens == 20 + 12
    assert engine.session.runtime_meta.total_session_output_tokens == 20 + 8
    assert engine.session.runtime_meta.total_session_cache_hit_tokens == 5
    # The parent's per-call snapshot (context meter source) is untouched.
    assert engine.session.runtime_meta.last_usage_details == parent_last_usage

    side_event = [e for e in seen if e.total_session_tokens == 60][-1]
    # Session totals refreshed; window numbers still the parent's last call.
    assert side_event.total_session_input_tokens == 32
    assert side_event.total_session_output_tokens == 28
    assert side_event.total_session_cache_hit_tokens == 5
    assert side_event.input_tokens == 20
    assert side_event.output_tokens == 20
    assert side_event.total_tokens == 40
    assert side_event.calibration_ratio == 1.3
    # Ordered publish chain: had the empty reports published, their events
    # would already have arrived before the side-call one.
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_usage_updates_publish_in_creation_order_with_slow_handlers(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Concurrent publish tasks must not let later usage totals overtake earlier ones."""
    bus = EventBus()
    seen: list[tuple[int, int, str]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def _slow_collect(event: UsageUpdate) -> None:
        if event.usage_source_id == "sub-1":
            first_started.set()
            await release_first.wait()
        seen.append((event.total_tokens, event.total_session_tokens, event.usage_source_id))

    await bus.subscribe(UsageUpdate, _slow_collect)
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    try:
        engine.usage_publisher.accumulate_invocation_usage(total_tokens=10, usage_source_id="sub-1")
        first = engine.usage_publisher.tail
        assert first is not None
        await wait_for_started(first_started, first, description="first usage handler started")
        engine.usage_publisher.accumulate_invocation_usage(total_tokens=20, usage_source_id="sub-2")
        second = engine.usage_publisher.tail
        assert second is not None and second is not first

        def second_waits_on_first() -> bool:
            graph = asyncio.capture_call_graph(first, limit=0)
            return graph is not None and any(parent.future is second for parent in graph.awaited_by)

        # Observe the real await dependency before releasing the slow handler:
        # merely queueing the second task does not prove that ordering holds.
        await wait_for(
            lambda: second.done() or second_waits_on_first(),
            description="second usage publication waiting on the first",
        )
        if second.done():
            await second
        assert not second.done(), "later usage publication overtook the held first handler"
        assert not first.done()
        assert seen == []
    finally:
        release_first.set()
        await wait_for_completion(engine.usage_publisher.settle())

    assert seen == [(10, 10, "sub-1"), (20, 30, "sub-2")]


# ---------------------------------------------------------------------------
# Rollback paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollback_to_prior_turn_restores_metadata_from_snapshot(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Rolling back to turn N restores the metadata captured at the start of turn N+1."""
    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "turn 1", _TurnUsage(input_tokens=10, output_tokens=10, total_tokens=20))
    await _drive_turn(engine, bus, "turn 2", _TurnUsage(input_tokens=30, output_tokens=40, total_tokens=70))
    await _drive_turn(engine, bus, "turn 3", _TurnUsage(input_tokens=100, output_tokens=200, total_tokens=300))

    assert engine.session.runtime_meta.total_session_tokens == 20 + 70 + 300

    # Rolling back to turn 1 promotes snapshot turn_2.json — captured at the
    # start of turn 2, i.e. AFTER turn 1's _save_current_session ran.
    pre_rollback_meta = engine.session.runtime_meta
    await bus.publish(UserRollback(target_turn=1, revert_changes=False))
    assert await wait_until(lambda: engine.session.runtime_meta is not pre_rollback_meta), (
        "rollback never replaced runtime metadata"
    )

    assert engine.session.runtime_meta is not pre_rollback_meta  # full restore happened
    assert engine.session.runtime_meta.total_session_tokens == 20
    assert engine.session.runtime_meta.total_session_input_tokens == 10
    assert engine.session.runtime_meta.total_session_output_tokens == 10
    assert engine.session.runtime_meta.last_usage_details["total_token_count"] == 20


@pytest.mark.asyncio
async def test_rollback_to_welcome_zeros_runtime_metadata(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """``UserRollback(target_turn=0)`` resets the engine and zeroes runtime metadata."""
    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "burned turn", _TurnUsage(input_tokens=50, output_tokens=50, total_tokens=100))
    assert engine.session.runtime_meta.total_session_tokens == 100

    await bus.publish(UserRollback(target_turn=0, revert_changes=False))
    assert await wait_until(lambda: engine.session.runtime_meta.total_session_tokens == 0), (
        "rollback to welcome never zeroed runtime metadata"
    )

    assert engine.session.runtime_meta.total_session_tokens == 0
    assert engine.session.runtime_meta.total_session_input_tokens == 0
    assert engine.session.runtime_meta.total_session_output_tokens == 0
    assert engine.session.runtime_meta.last_usage_details == {}


# ---------------------------------------------------------------------------
# Defensive recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unexpected_last_usage_schema_does_not_crash_full_restore(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """A persisted ``last_usage`` with an unexpected schema must not crash restore.

    A future provider could write extra keys (or omit expected keys) into
    ``last_usage``.  As long as the value is a dict-coercible mapping the
    serializer accepts it, ``from_state_dict`` accepts it, and downstream
    consumers tolerate missing fields via ``.get(..., default)``.
    """
    bus_a = EventBus()
    engine_a, store = _build_engine(tmp_path, bus_a, agent_engine=agent_engine)
    await engine_a.start(_CODE)

    await _drive_turn(engine_a, bus_a, "valid turn", _TurnUsage(input_tokens=5, output_tokens=5, total_tokens=10))
    sid = engine_a.session.session_id
    await engine_a.shutdown()

    # Persist an off-schema last_usage (no token keys, extra unknown ones).
    session_file = store.session_dir(sid) / "session.json"
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    payload["state"]["last_usage"] = {"unexpected_field": "garbage", "extra": 42}
    session_file.write_text(json.dumps(payload), encoding="utf-8")

    bus_b = EventBus()
    engine_b = agent_engine(bus_b, settings=Settings(), agent_registry=_registry(_CODE), state_store=store)
    await engine_b.start(_CODE)
    await bus_b.publish(SessionRestore(session_id=sid))
    assert await wait_until(lambda: engine_b.session.session_id == sid), "session restore never completed"

    # Off-schema dict survives end-to-end; missing token keys degrade to defaults.
    assert engine_b.session.runtime_meta.last_usage_details == {"unexpected_field": "garbage", "extra": 42}
    assert engine_b.session.runtime_meta.last_usage_details.get("total_token_count", 0) == 0
    assert engine_b.session.runtime_meta.total_session_tokens == 10


@pytest.mark.asyncio
async def test_non_dict_last_usage_degrades_to_absent_on_full_restore(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """A non-dict ``last_usage`` on disk is dropped by the serializer, not fatal.

    The serializer's ``_copy_optional_state_keys`` Mapping-gates dict-valued
    keys (P2 fix), so a truly corrupt value degrades to "record absent" and
    the restore completes with default metadata instead of crashing inside
    ``dict()`` before the engine-level guard could run.
    """
    bus_a = EventBus()
    engine_a, store = _build_engine(tmp_path, bus_a, agent_engine=agent_engine)
    await engine_a.start(_CODE)
    await _drive_turn(engine_a, bus_a, "valid turn", _TurnUsage(input_tokens=5, output_tokens=5, total_tokens=10))
    sid = engine_a.session.session_id
    await engine_a.shutdown()

    session_file = store.session_dir(sid) / "session.json"
    payload = json.loads(session_file.read_text(encoding="utf-8"))
    payload["state"]["last_usage"] = "not-a-dict"
    session_file.write_text(json.dumps(payload), encoding="utf-8")

    bus_b = EventBus()
    engine_b = agent_engine(bus_b, settings=Settings(), agent_registry=_registry(_CODE), state_store=store)
    await engine_b.start(_CODE)
    await bus_b.publish(SessionRestore(session_id=sid))
    assert await wait_until(lambda: engine_b.session.session_id == sid), "session restore never completed"

    assert engine_b.session.runtime_meta.last_usage_details == {}
    assert engine_b.session.runtime_meta.total_session_tokens == 10


@pytest.mark.asyncio
async def test_falsy_metadata_does_not_pollute_persisted_state(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Zero totals + empty last_usage are filtered by the serializer table."""
    bus = EventBus()
    engine, store = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    # No usage events fire; drive a turn and let _save_current_session run.
    await _drive_turn(engine, bus, "no-usage turn")
    sid = engine.session.session_id

    payload = json.loads((store.session_dir(sid) / "session.json").read_text(encoding="utf-8"))
    state = payload["state"]
    for key in (
        "last_usage",
        "total_session_tokens",
        "total_session_input_tokens",
        "total_session_output_tokens",
    ):
        assert key not in state, f"falsy metadata leaked into persisted state: {key}={state[key]!r}"


# ---------------------------------------------------------------------------
# Session deletion via the bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_delete_event_clears_disk_and_releases_lock(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """``SessionDelete`` removes session.json and frees the active-session lock."""
    bus = EventBus()
    deleted: list[SessionDeleted] = []
    await bus.subscribe(SessionDeleted, lambda e: collect_events(deleted, e))
    engine, store = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "doomed turn", _TurnUsage(input_tokens=1, output_tokens=1, total_tokens=2))
    sid = engine.session.session_id
    assert (store.session_dir(sid) / "session.json").exists()

    await bus.publish(SessionDelete(session_id=sid))
    assert await wait_until(lambda: bool(deleted)), "SessionDeleted never arrived"

    assert deleted and deleted[0].session_id == sid
    assert not (store.session_dir(sid) / "session.json").exists()
    # Engine clears its own session_id when deleting the active session.
    assert engine.session.session_id is None


@pytest.mark.asyncio
async def test_session_clear_deletes_active_session_under_fence_then_starts_fresh(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """``SessionClear`` = one fenced transition: delete (acknowledged) → fresh session.

    Prompt admission must already be closed when ``SessionDeleted`` fires (no
    prompt can be admitted against the detached session), the deleted session
    must not be resurrected by the new-session shutdown save, and the engine
    ends on a different, empty session with admission reopened.
    """
    bus = EventBus()
    deleted: list[SessionDeleted] = []
    admission_closed_at_delete: list[bool] = []
    engine, store = _build_engine(tmp_path, bus, agent_engine=agent_engine)

    async def _on_deleted(event: SessionDeleted) -> None:
        deleted.append(event)
        admission_closed_at_delete.append(engine.turns.turn_state.lease.prompt_admission_closed)

    await bus.subscribe(SessionDeleted, _on_deleted)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "doomed turn", _TurnUsage(input_tokens=1, output_tokens=1, total_tokens=2))
    sid = engine.session.session_id
    assert (store.session_dir(sid) / "session.json").exists()
    probe = _probe_session_end(engine, store, sid)

    await bus.publish(SessionClear(session_id=sid))
    assert await wait_until(lambda: engine.session.session_id not in (None, sid)), "fresh session never started"

    assert [e.session_id for e in deleted] == [sid]
    assert admission_closed_at_delete == [True]
    assert engine.turns.turn_state.lease.prompt_admission_closed is False
    assert not store.session_dir(sid).exists()
    assert engine.session.runtime_meta.total_session_tokens == 0
    assert [meta.session_id for meta in await store.list_sessions() if meta.session_id == sid] == []
    # session_end fired exactly once for the deleted session and its async
    # hook was drained (manager left open) before the files went away; the
    # new-session shutdown then only ran the closing drain, no second fire.
    assert [payload["session_id"] for payload in probe.session_end_payloads] == [sid]
    assert probe.file_existed_at_completion == [True]
    assert probe.drain_close_flags == [False, True]
    assert engine.session.hook_manager is not probe
    assert engine.session.session_end_fired is False


@pytest.mark.asyncio
async def test_session_clear_failure_keeps_session_and_never_starts_new(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """A failed delete reports ``session_clear_failed`` and leaves everything as it was."""
    import chrys.service.state.store as store_module
    from chrys.foundation.util.lock import FileLock

    monkeypatch.setattr(store_module, "SESSION_WRITE_LOCK_TIMEOUT_SECONDS", 0.01)
    bus = EventBus()
    errors: list[Error] = []
    deleted: list[SessionDeleted] = []
    await bus.subscribe(Error, lambda e: collect_events(errors, e))
    await bus.subscribe(SessionDeleted, lambda e: collect_events(deleted, e))
    engine, store = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    await _drive_turn(engine, bus, "kept turn", _TurnUsage(input_tokens=1, output_tokens=1, total_tokens=2))
    sid = engine.session.session_id
    generation_before = engine.permits.session_generation
    tokens_before = engine.session.runtime_meta.total_session_tokens
    probe = _probe_session_end(engine, store, sid)

    held = FileLock(store_module.session_write_lock_path(tmp_path, sid), timeout=1.0)
    held.acquire()
    try:
        await bus.publish(SessionClear(session_id=sid))
    finally:
        held.release()

    assert [e.code for e in errors] == ["session_clear_failed"]
    assert deleted == []
    assert engine.session.session_id == sid
    assert engine.session.guard.owns(sid)
    assert engine.permits.session_generation == generation_before
    assert engine.session.runtime_meta.total_session_tokens == tokens_before
    assert engine.turns.turn_state.lease.prompt_admission_closed is False
    assert (store.session_dir(sid) / "session.json").exists()
    # The early session_end already went out and was drained without closing
    # the manager (accepted); the surviving session is re-armed so its real
    # shutdown still ends it for hooks, then closes.
    assert [payload["session_id"] for payload in probe.session_end_payloads] == [sid]
    assert probe.drain_close_flags == [False]
    assert engine.session.session_end_fired is False
    await engine.shutdown()
    assert [payload["session_id"] for payload in probe.session_end_payloads] == [sid, sid]
    assert probe.file_existed_at_completion == [True, True]
    assert probe.drain_close_flags == [False, False, True]


@pytest.mark.asyncio
async def test_delete_current_session_then_new_fires_session_end_once_with_files(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """The sessions-browser path (SessionDelete of the active session, then SessionNew) shares the fix."""
    bus = EventBus()
    engine, store = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)
    await _drive_turn(engine, bus, "browser-deleted turn", _TurnUsage(input_tokens=1, output_tokens=1, total_tokens=2))
    sid = engine.session.session_id
    probe = _probe_session_end(engine, store, sid)

    await bus.publish(SessionDelete(session_id=sid))
    assert engine.session.session_id is None
    assert [payload["session_id"] for payload in probe.session_end_payloads] == [sid]
    assert probe.file_existed_at_completion == [True]
    assert probe.drain_close_flags == [False]

    await bus.publish(SessionNew())
    assert await wait_until(lambda: engine.session.session_id not in (None, sid)), "fresh session never started"
    assert [payload["session_id"] for payload in probe.session_end_payloads] == [sid]
    assert probe.drain_close_flags == [False, True]
    assert engine.session.session_end_fired is False


@pytest.mark.asyncio
async def test_session_clear_refuses_non_active_session(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Only the active session can be cleared; other ids fail without touching disk."""
    bus = EventBus()
    errors: list[Error] = []
    deleted: list[SessionDeleted] = []
    await bus.subscribe(Error, lambda e: collect_events(errors, e))
    await bus.subscribe(SessionDeleted, lambda e: collect_events(deleted, e))
    engine, store = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await store.save_session("other", {"messages": [], "compressed_msgs": []})
    await engine.start(_CODE)
    sid = engine.session.session_id

    await bus.publish(SessionClear(session_id="other"))

    assert [e.code for e in errors] == ["session_clear_failed"]
    assert deleted == []
    assert engine.session.session_id == sid
    assert await store.load_session("other") is not None


# ---------------------------------------------------------------------------
# Big multi-phase scenario
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_complex_metadata_lifecycle_across_switch_restore_and_rollback(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """Walk a single session through switch + shutdown + restore + rollback.

    This is the user-visible flow the metadata refactor was designed for:
    multiple turns build up usage, a profile switch preserves it across a
    soft-rebuild, a fresh engine restores the totals after shutdown, more
    usage accumulates in the restored engine, and finally a rollback to
    a mid-session snapshot rewinds the totals to the snapshot's recorded
    state.
    """
    bus_a = EventBus()
    saved_events: list = []
    for cls in (UsageUpdate, SessionRestored, SessionDeleted):
        await bus_a.subscribe(cls, lambda e, _events=saved_events: collect_events(_events, e))

    engine_a, store = _build_engine(tmp_path, bus_a, _CODE, _EXPLORE, agent_engine=agent_engine)
    await engine_a.start(_CODE)

    # --- Phase 1: two turns under the original profile ---
    await _drive_turn(engine_a, bus_a, "phase1-a", _TurnUsage(input_tokens=10, output_tokens=20, total_tokens=30))
    await _drive_turn(engine_a, bus_a, "phase1-b", _TurnUsage(input_tokens=15, output_tokens=25, total_tokens=40))
    after_phase1 = engine_a.session.runtime_meta.total_session_tokens
    assert after_phase1 == 70

    # --- Phase 2: profile switch must keep totals + last_usage ---
    last_usage_phase1 = dict(engine_a.session.runtime_meta.last_usage_details)
    await bus_a.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(
        lambda: engine_a.session.agent_profile is not None and engine_a.session.agent_profile.name == "Explore"
    ), "profile switch to Explore never completed"
    assert engine_a.session.runtime_meta.total_session_tokens == after_phase1
    assert engine_a.session.runtime_meta.last_usage_details == last_usage_phase1

    await _drive_turn(engine_a, bus_a, "phase2", _TurnUsage(input_tokens=50, output_tokens=50, total_tokens=100))
    after_phase2 = engine_a.session.runtime_meta.total_session_tokens
    assert after_phase2 == 170

    sid = engine_a.session.session_id
    await engine_a.shutdown()

    # --- Phase 3: fresh engine restores ---
    bus_b = EventBus()
    restored: list[SessionRestored] = []
    await bus_b.subscribe(SessionRestored, lambda e: collect_events(restored, e))

    engine_b = agent_engine(bus_b, settings=Settings(), agent_registry=_registry(_CODE, _EXPLORE), state_store=store)
    await engine_b.start(_CODE)
    await bus_b.publish(SessionRestore(session_id=sid))
    assert await wait_until(lambda: bool(restored)), "SessionRestored never arrived"

    assert restored and restored[0].agent_profile == "Explore"
    assert engine_b.session.runtime_meta.total_session_tokens == after_phase2
    assert engine_b.session.runtime_meta.total_session_input_tokens == 10 + 15 + 50
    assert engine_b.session.runtime_meta.total_session_output_tokens == 20 + 25 + 50

    # --- Phase 4: more usage accumulates in the restored engine ---
    await _drive_turn(engine_b, bus_b, "phase4", _TurnUsage(input_tokens=8, output_tokens=12, total_tokens=20))
    assert engine_b.session.runtime_meta.total_session_tokens == 190

    # --- Phase 5: rollback to the end of phase 1 (turn 2) ---
    # Snapshots are written at the START of each turn, so snapshot turn_3.json
    # captures the post-turn-2 state — ``UserRollback(target_turn=2)``
    # promotes that snapshot via ``rollback_snapshot_path(target_turn + 1)``.
    pre_rollback_meta = engine_b.session.runtime_meta
    await bus_b.publish(UserRollback(target_turn=2, revert_changes=False))
    assert await wait_until(lambda: engine_b.session.runtime_meta is not pre_rollback_meta), (
        "rollback never replaced runtime metadata"
    )

    assert engine_b.session.runtime_meta is not pre_rollback_meta
    assert engine_b.session.runtime_meta.total_session_tokens == after_phase1
    assert engine_b.session.runtime_meta.last_usage_details == last_usage_phase1


# ---------------------------------------------------------------------------
# Reset-for-restart edge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_event_resets_runtime_meta_to_fresh_instance(
    tmp_path: Path,
    patch_create_client: list[MockChatClient],
    agent_engine,
) -> None:
    """``SessionNew`` (via ``reset_for_restart``) replaces ``_runtime_meta`` with defaults."""
    from chrys.foundation.events.types import SessionNew

    bus = EventBus()
    engine, _ = _build_engine(tmp_path, bus, agent_engine=agent_engine)
    await engine.start(_CODE)

    engine.usage_publisher.publish_usage(total_tokens=99, input_tokens=49, output_tokens=50)
    assert await wait_until(lambda: engine.session.runtime_meta.total_session_tokens == 99), (
        "usage write never accumulated"
    )
    pre_meta = engine.session.runtime_meta

    await bus.publish(SessionNew())
    assert await wait_until(lambda: engine.session.runtime_meta is not pre_meta), (
        "SessionNew never reset runtime metadata"
    )

    assert engine.session.runtime_meta is not pre_meta
    assert engine.session.runtime_meta.total_session_tokens == 0
    assert engine.session.runtime_meta.last_usage_details == {}
