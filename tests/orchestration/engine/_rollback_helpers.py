# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine builders, session seeding, and restore/reset stand-ins shared by the rollback test modules."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import Message
from chrys.orchestration.engine.assembly import assemble_agent_engine
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.state.machine import EngineState, Trigger
from chrys.service.mutations.types import RollbackPlan
from chrys.service.profiles.agents.schema import AgentProfile
from chrys.service.state.store import JsonFileStateStore, atomic_copy_file
from tests.support.event_capture import collect_events
from tests.support.loaded_agents import install_loaded_agent


def _make_engine(
    tmp_path: Path,
    *,
    session_id: str = "rb_test",
    keep_last: int = 10,
    fsm_state: EngineState = EngineState.IDLE,
    engine_services,
) -> AgentEngine:
    """Build a minimally-initialized engine with a temp state store.

    FSM is transitioned to ``fsm_state`` (IDLE by default) so rollback
    validation reaches the branches beyond the initial safety gate.
    """
    store = JsonFileStateStore(tmp_path)
    settings = replace(Settings(), rollback_snapshots_keep=keep_last)
    engine = assemble_agent_engine(EventBus(), settings=settings, state_store=store)
    engine.session.session_id = session_id
    engine_services(engine).history.bind({"messages": []})
    if fsm_state is EngineState.IDLE:
        engine_services(engine).fsm.try_transition(Trigger.START)
    elif fsm_state is EngineState.RUNNING:
        engine_services(engine).fsm.try_transition(Trigger.START)
        engine_services(engine).fsm.try_transition(Trigger.USER_MESSAGE)
    elif fsm_state is EngineState.PENDING_RETRY:
        engine_services(engine).fsm.try_transition(Trigger.START)
        engine_services(engine).fsm.try_transition(Trigger.USER_MESSAGE)
        engine_services(engine).fsm.try_transition(Trigger.RETRY_REQUESTED)
    elif fsm_state is EngineState.AWAITING_SUB_AGENTS:
        engine_services(engine).fsm.try_transition(Trigger.START)
        engine_services(engine).fsm.try_transition(Trigger.USER_MESSAGE)
        engine_services(engine).fsm.try_transition(Trigger.SUB_AGENT_PAUSED)
    return engine


def _write_session_json(path: Path, messages: list[dict[str, Any]] | None = None) -> None:
    """Write a minimal session.json so ``_write_rollback_snapshot`` has something to copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "meta": {"session_id": "rb_test", "agent_profile": "p"},
        "state": {"messages": messages or [], "compressed_msgs": []},
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _turn_marker(idx: int) -> Message:
    marker = Message("assistant", [""])
    marker.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    marker.additional_properties["_turn_id"] = f"turn_{idx}"
    marker.additional_properties["_turn"] = idx
    return marker


def _state_after_turns(count: int) -> dict[str, Any]:
    messages: list[Message] = []
    for idx in range(1, count + 1):
        messages.extend(
            [
                Message("user", [f"user {idx}"]),
                Message("assistant", [f"assistant {idx}"]),
                _turn_marker(idx),
            ]
        )
    return {"messages": messages, "compressed_msgs": [], "turn_counter": count}


async def _collect_events(bus: EventBus, event_type: type, out: list[Any]) -> None:
    await bus.subscribe(event_type, lambda event: collect_events(out, event))


class _StubExec:
    """TurnBindings stand-in exposing only the ``history_state`` the engine reads back."""

    def __init__(self, history_state: dict[str, Any] | None = None) -> None:
        self.backend = self
        self.history_state: dict[str, Any] = {} if history_state is None else history_state


async def seed_turns(engine: AgentEngine, count: int, *, engine_services) -> JsonFileStateStore:
    """Persist ``count`` completed turns for ``rb_test`` and bind the live state to ``engine``.

    Each turn is saved as ``session.json`` and copied to ``snapshots/turn_{k + 1}.json`` for
    every turn but the last (snapshots are written at the *start* of the following turn), then
    the saved state is reloaded into the engine's history with ``_turn_number == count``.
    Returns the store so callers can pair it with :func:`fake_restore_factory`.
    """
    store = engine_services(engine).persistence.state_store
    assert isinstance(store, JsonFileStateStore)
    session_dir = store.session_dir("rb_test")
    session_file = session_dir / "session.json"
    snap_dir = session_dir / "snapshots"
    snap_dir.mkdir(parents=True)
    for turn in range(1, count + 1):
        await store.save_session("rb_test", _state_after_turns(turn))
        if turn < count:
            atomic_copy_file(session_file, snap_dir / f"turn_{turn + 1}.json")
    live = await store.load_session("rb_test")
    assert live is not None
    engine_services(engine).history.bind(live)
    engine.session.turn_number = count
    return store


def fake_restore_factory(
    engine: AgentEngine, store: JsonFileStateStore, *, engine_services
) -> Callable[[Any], Awaitable[None]]:
    """``on_session_restore`` stand-in that reloads the swapped session from ``store``."""

    async def _fake_restore(event: Any) -> None:
        loaded = await store.load_session(event.session_id)
        assert loaded is not None
        engine_services(engine).history.bind(loaded)
        engine.session.turn_number = loaded.get("turn_counter", 0)

    return _fake_restore


def fake_reset(engine: AgentEngine, *, engine_services) -> Callable[..., Awaitable[bool]]:
    """``reset_session_to_welcome`` stand-in that resets the change tracker and runs ``after_delete``."""

    async def _fake_reset(
        _session_id: str,
        *,
        write_lock_held: bool = False,
        after_delete: Any = None,
        before_restart: Any = None,
    ) -> bool:
        _ = write_lock_held, before_restart
        engine_services(engine).workspace_change_tracker.reset_for_restart()
        if after_delete is not None:
            await after_delete()
        return True

    return _fake_reset


def fake_start_factory(engine: AgentEngine) -> Callable[..., Awaitable[None]]:
    """``engine.start`` stand-in that installs the profile, a stub executor, and staged settings."""

    async def _fake_start(
        profile: AgentProfile,
        *,
        operation: str = "startup",
        staged_loaded: LoadedSettings | None = None,
        session: Mapping[str, Any] | None = None,
    ) -> None:
        engine.session.agent_profile = profile
        install_loaded_agent(engine, bindings=_StubExec())  # type: ignore[assignment]
        if staged_loaded is not None:
            engine.settings_handle.install_prepared(engine.settings_handle.prepare(staged_loaded, session=session))

    return _fake_start


@dataclass
class _FakeTurn:
    turn_id: int
    detection_truncated: bool = False


@dataclass
class _FakeRestoreResult:
    changed: bool


class _FakeMutationTracker:
    def __init__(self, turn_ids: list[int]) -> None:
        self._turns = [_FakeTurn(turn_id) for turn_id in turn_ids]
        self.rollback_calls: list[tuple[set[int], set[str] | None]] = []

    def get_all_turns(self) -> list[_FakeTurn]:
        return self._turns

    def serialize(self) -> dict[str, Any]:
        return {
            "turns": [{"turn_id": turn.turn_id, "mutations": []} for turn in self._turns],
            "snapshots": {},
        }

    def get_rollback_plan_for_turns(self, turn_ids: set[int]) -> RollbackPlan:
        return RollbackPlan(entries=[])

    def rollback_turns(
        self,
        turn_ids: set[int],
        *,
        only_paths: set[str] | None = None,
        plan: RollbackPlan | None = None,
    ) -> list[_FakeRestoreResult]:
        self.rollback_calls.append((turn_ids, only_paths))
        return [_FakeRestoreResult(changed=True), _FakeRestoreResult(changed=False)]
