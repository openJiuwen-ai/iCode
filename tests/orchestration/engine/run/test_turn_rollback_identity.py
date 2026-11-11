# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rollback compatibility for sessions whose counters drifted before turn alignment.

The fixtures explicitly seed the former missing-boundary layout. New turns no
longer create it, but rollback must keep handling those stored sessions.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import RollbackResult, TurnStarted, UserMessage, UserRollback
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.service.llm.mock import MockChatClient
from chrys.service.state.serializers import serialize_state
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY, turn_records
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import await_run_task_chain

if TYPE_CHECKING:
    from pathlib import Path

_SESSION_ID = "e0a1b2c3-d4e5-4f60-8a91-b2c3d4e5f601"


async def _start(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store,
    client: MockChatClient,
    *,
    session_id: str | None = None,
):
    turns: list[TurnStarted] = []
    bus = EventBus()
    await bus.subscribe(TurnStarted, lambda event, _turns=turns: collect_events(_turns, event))
    settings, model_registry = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=model_registry,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    if session_id is not None:
        # Before start: the recorder takes its session directory from the id
        # the session has when it binds, not from later assignments.
        engine.session.session_id = session_id
    await engine.start(_PROFILE)
    return engine, turns


async def _ask(engine, text: str) -> None:
    await engine.event_bus.publish(UserMessage(text=text))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)


def _seed_legacy_drift(engine) -> None:
    """Represent the missing boundary produced by older fresh-prompt cleanup."""
    state = engine.current.require_loaded().bindings.backend.history_state
    assert state["messages"][-1].additional_properties.get("_chrys_kind") == "turn"
    state["messages"].pop()
    state["turn_counter"] -= 1


def _rollback_payload(engine) -> dict:
    """The payload of the last rollback the session's log recorded."""
    rollback = [
        event
        for event in read_trajectory(trajectory_events_path(engine.session_dir)).events
        if event.event_type == EventType.SESSION_ROLLBACK
    ][-1]
    return rollback.payload


def _reopen(engine, saved: dict, services) -> None:
    """Re-base the runtime on a save, as a restore to that session would.

    The backend and the history manager must end up sharing one ``chrys_history``
    dict: the runtime writes a turn's registry entry into the backend's state and
    the rollback reads it back through the manager's.
    """
    backend = engine.current.loaded.bindings.backend
    backend.history_state = saved
    services.history.bind(backend.history_state)
    engine.session.turn_number = saved.get("turn_counter", 0)


class TestRollbackOfLegacyDrift:
    @pytest.mark.asyncio
    async def test_only_the_turns_after_the_snapshot_are_retired(
        self, tmp_path: Path, agent_engine, engine_services, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A turn admitted after a failed one is retired by id, not by ordinal."""
        store = JsonFileStateStore(tmp_path)
        engine, _ = await _start(agent_engine, tmp_path, monkeypatch, store, MockChatClient([]), session_id=_SESSION_ID)

        # A fails, then B and C complete. B and C take ordinals 2 and 3 while the
        # counter the save carries reaches only 2: the drift this is about.
        await _ask(engine, "A")
        _seed_legacy_drift(engine)
        await _ask(engine, "B")
        await _ask(engine, "C")
        turns = engine.turn_records()
        assert [record.turn_number for record in turns] == [1, 2, 3]
        kept, superseded = turns[1], turns[2]
        assert superseded.started_sequence is not None

        saved = await store.load_session(_SESSION_ID)
        assert saved is not None
        assert saved.get("turn_counter", 0) == 2
        assert [record.turn_id for record in turn_records(saved)] == [record.turn_id for record in turns]
        _reopen(engine, saved, engine_services(engine))

        # The snapshot a rollback to turn 1 restores is the state as it read
        # before C: B's ordinal there is 2, the same one the save carries.
        snapshot_state = serialize_state(
            {
                "messages": saved["messages"],
                "compressed_msgs": [],
                "turn_counter": 1,
                TRAJECTORY_STATE_KEY: {
                    "turn_log": [
                        {"turn_number": 1, "turn_id": turns[0].turn_id},
                        {"turn_number": 2, "turn_id": kept.turn_id},
                    ]
                },
            }
        )
        snap_dir = store.session_dir(_SESSION_ID) / "snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / "turn_1.json").write_text(
            json.dumps({"meta": {"session_id": _SESSION_ID}, "state": snapshot_state}), encoding="utf-8"
        )

        results: list[RollbackResult] = []
        await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert len(results) == 1
        # Only C. Subtracting ordinals would have retired B as well, since the
        # turn the snapshot names as its own is the one the counter reached.
        assert results[0].superseded_turn_ids == [superseded.turn_id]
        assert results[0].target_turn == 1

        # The restored session continues from the kept turn's registry — on disk
        # and in the runtime the rollback left behind, which is the one the next
        # turn reads.
        restored = await store.load_session(_SESSION_ID)
        assert restored is not None
        assert [record.turn_id for record in turn_records(restored)] == [turns[0].turn_id, kept.turn_id]
        assert [record.turn_id for record in engine.turn_records()] == [turns[0].turn_id, kept.turn_id]

        # The trajectory range starts where the retired turn's own pass did and
        # names the turn the new branch continues from.
        rollback = _rollback_payload(engine)
        assert rollback["superseded_from_sequence"] == superseded.started_sequence
        assert rollback["target_turn_id"] == kept.turn_id

    @pytest.mark.asyncio
    async def test_a_snapshot_that_names_nothing_does_not_decide_what_was_retired(
        self, tmp_path: Path, agent_engine, engine_services, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """What is reported retired is what the restored session lost, not what the snapshot named.

        The snapshot a rollback reads can be unreadable, and the swap copies it
        over the session file all the same; the store then falls back to an older
        snapshot that does parse. Nothing the unreadable one could name is
        evidence of a turn being retired, so the report has to come from the
        session the restore came back with.
        """
        store = JsonFileStateStore(tmp_path)
        engine, _ = await _start(agent_engine, tmp_path, monkeypatch, store, MockChatClient([]), session_id=_SESSION_ID)

        await _ask(engine, "A")
        _seed_legacy_drift(engine)
        await _ask(engine, "B")
        await _ask(engine, "C")
        turns = engine.turn_records()
        assert len(turns) == 3

        snap_dir = store.session_dir(_SESSION_ID) / "snapshots"
        # The fallback the store reads once the swap leaves it nothing usable: a
        # snapshot naming A and B that neither matches target 1 (its counter is 0)
        # nor gets pruned as stale.
        fallback = serialize_state(
            {
                "messages": [],
                "compressed_msgs": [],
                "turn_counter": 0,
                TRAJECTORY_STATE_KEY: {
                    "turn_log": [
                        {"turn_number": 1, "turn_id": turns[0].turn_id},
                        {"turn_number": 2, "turn_id": turns[1].turn_id},
                    ]
                },
            }
        )
        (snap_dir / "turn_1.json").write_text(
            json.dumps({"meta": {"session_id": _SESSION_ID}, "state": fallback}), encoding="utf-8"
        )
        # The rollback to turn 1 reads turn_2 — make it the only match, and make it
        # unreadable, so it names no turn at all.
        (snap_dir / "turn_3.json").unlink()
        (snap_dir / "turn_2.json").write_text("{ not json", encoding="utf-8")

        results: list[RollbackResult] = []
        await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))

        await engine._on_user_rollback(UserRollback(target_turn=1, revert_changes=False))

        assert len(results) == 1
        # The restore fell back to the snapshot naming A and B, so those two are
        # the session that is live now and only C is gone.
        assert [record.turn_id for record in engine.turn_records()] == [turns[0].turn_id, turns[1].turn_id]
        assert results[0].superseded_turn_ids == [turns[2].turn_id]
        # The audit record has to agree: the snapshot named no turn, so nothing
        # can be shown to be superseded by it, and marking A and B retired would
        # contradict the session the result just reported.
        assert "superseded_from_sequence" not in _rollback_payload(engine)

    @pytest.mark.asyncio
    async def test_a_rollback_to_welcome_retires_every_registered_turn(
        self, tmp_path: Path, agent_engine, engine_services, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The welcome branch reports the turns it drops, the failed one included."""
        store = JsonFileStateStore(tmp_path)
        engine, _ = await _start(agent_engine, tmp_path, monkeypatch, store, MockChatClient([]), session_id=_SESSION_ID)

        await _ask(engine, "A")
        _seed_legacy_drift(engine)
        await _ask(engine, "B")
        registered = engine.turn_records()
        assert len(registered) == 2
        assert [record.turn_id for record in engine.turn_records()] == [record.turn_id for record in registered]

        results: list[RollbackResult] = []
        await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))

        await engine._on_user_rollback(UserRollback(target_turn=0, revert_changes=False))

        assert len(results) == 1
        assert results[0].superseded_turn_ids == [record.turn_id for record in registered]
        assert engine.turn_records() == []
        # The range the branch supersedes covers the turns the reset dropped:
        # their own passes are what the log can no longer continue from.
        payload = _rollback_payload(engine)
        assert payload["superseded_from_sequence"] == registered[0].started_sequence
