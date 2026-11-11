# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Black-box acceptance of turn alignment after failed turns and of snapshot lifecycle rules."""

from __future__ import annotations

import json

import pytest

from chrys.foundation.events.types import (
    RollbackResult,
    SessionClear,
    SessionRestore,
    UserInterrupt,
    UserMessage,
    UserRetry,
    UserRollback,
    Warning,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.reader import read_trajectory
from chrys.kernel import FunctionTool
from chrys.service.llm.mock import MockResponse
from chrys.service.mutations.types import MutationOp, MutationSource
from chrys.service.state.store import JsonFileStateStore
from chrys.service.trajectory.session import trajectory_events_path
from tests.support.event_capture import capture_events
from tests.support.rollback_spec import SpecClient, answer, ask, drain, start_engine
from tests.support.tool_packs import install_tools
from tests.support.waiting import wait_for


def texts(engine, role):
    return [message.text for message in engine.history_messages if message.role == role and message.text]


def assert_aligned(engine, expected):
    markers = [
        m
        for m in engine.history_messages
        if m.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN
    ]
    assert len(markers) == expected
    assert engine.current_turn_number == engine.session.turn_number == expected
    assert [r.turn_number for r in engine.turn_records()] == list(range(1, expected + 1))
    assert [t.turn_id for t in engine.mutation_tracker.get_all_turns()] == list(range(1, expected + 1))


@pytest.mark.parametrize(
    "mode", ["answer", "empty", "thought", "retry-empty-failure", "retry-all-empty", "failure", "file"]
)
@pytest.mark.parametrize("reopen", [False, True], ids=["live", "restored"])
@pytest.mark.parametrize("selector", ["relative", "absolute"])
async def test_fresh_turn_and_rollback_follow_one_numbering(
    tmp_path,
    monkeypatch,
    agent_engine,
    mode,
    reopen,
    selector,
):
    """A's fate must agree across history, registry, snapshots and file rollback."""
    store = JsonFileStateStore(tmp_path / "state")
    target = tmp_path / "work.txt"
    target.write_text("original", encoding="utf-8")
    holder = {}

    async def edit_work(value: str) -> str:
        tracker = holder["engine"].mutation_tracker
        mutation = tracker.record(str(target), MutationOp.MODIFY, MutationSource.EDIT_FILE, "spec-edit")
        assert mutation is not None
        target.write_text(value, encoding="utf-8")
        tracker.record_after(mutation)
        return "edited"

    install_tools(
        monkeypatch, [FunctionTool(func=edit_work, name="edit_work", description="Edit the test workspace file")]
    )
    responses = [answer("base answer")]
    if mode == "file":
        responses += [MockResponse(tool_calls=[("edit_work", "edit-A", {"value": "A"})]), answer("PARK:empty")]
    elif mode == "failure":
        responses += [answer("FAIL")]
    else:
        responses += [
            answer(
                "PARK:"
                + ("answer" if mode == "retry-empty-failure" else "empty" if mode == "retry-all-empty" else mode)
            )
        ]
    if mode in {"retry-empty-failure", "retry-all-empty"}:
        responses += [answer("FAIL")]
    responses += [MockResponse(tool_calls=[("edit_work", "edit-B", {"value": "B"})]), answer("answer B")]
    client = SpecClient(responses)
    engine = await start_engine(agent_engine, monkeypatch, tmp_path, store, client)
    holder["engine"] = engine
    await ask(engine, "base prompt")
    await engine.event_bus.publish(UserMessage(text="prompt A"), raise_handler_errors=True)
    if mode != "failure":
        await wait_for(client.parked.is_set)
        await engine.event_bus.publish(UserInterrupt(), raise_handler_errors=True)
    await drain(engine)
    if mode in {"retry-empty-failure", "retry-all-empty"}:
        await engine.event_bus.publish(UserRetry(text="unanswered continuation"), raise_handler_errors=True)
        await drain(engine)
    old_record = engine.turn_records()[-1]
    snapshot = engine.session_dir / "snapshots" / "turn_2.json"
    if reopen:
        session_id = engine.session_id
        await engine.shutdown()
        engine = await start_engine(agent_engine, monkeypatch, tmp_path, store, client)
        holder["engine"] = engine
        await engine.event_bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
    await ask(engine, "prompt B")
    # A note drops the answer A showed before its call goes out, as a Continue does: when that call
    # fails, A holds no work, like a turn that showed nothing.
    kept_a = mode in {"answer", "file"}
    expected = 3 if kept_a else 2
    assert_aligned(engine, expected)
    saved = await store.load_session(engine.session_id)
    assert saved["turn_counter"] == expected
    users = texts(engine, "user")
    assert ("prompt A" in users) is kept_a
    assert "unanswered continuation" not in users
    assert target.read_text(encoding="utf-8") == "B"
    if not kept_a:
        assert old_record.turn_id not in [r.turn_id for r in engine.turn_records()]
        events = read_trajectory(trajectory_events_path(engine.session_dir)).events
        assert old_record.started_sequence is not None
        assert any(
            e.event_type == EventType.SESSION_ROLLBACK
            and e.payload.get("superseded_from_sequence") == old_record.started_sequence
            for e in events
        ), "the discarded empty turn must be superseded in trajectory"
        # The cleanup is saved first, so the new turn's
        # snapshot is rewritten from disk and holds the state before the retired turn.
        rebuilt = json.loads(snapshot.read_bytes())
        rebuilt_messages = json.dumps(rebuilt.get("state", rebuilt).get("messages", []), ensure_ascii=False)
        assert "prompt A" not in rebuilt_messages
        assert "unanswered continuation" not in rebuilt_messages
    results = await capture_events(engine.event_bus, RollbackResult)
    request = (
        UserRollback(relative_turns=1, revert_changes=True)
        if selector == "relative"
        else UserRollback(target_turn=expected - 1, revert_changes=True)
    )
    await engine.event_bus.publish(request, raise_handler_errors=True)
    assert len(results) == 1
    assert results[0].rolled_back_user_text == "prompt B"
    assert_aligned(engine, expected - 1)
    assert "prompt B" not in texts(engine, "user")
    assert ("prompt A" in texts(engine, "user")) is kept_a
    assert target.read_text(encoding="utf-8") == ("A" if mode == "file" else "original")
    if kept_a:
        await engine.event_bus.publish(UserRollback(target_turn=1, revert_changes=True), raise_handler_errors=True)
        assert results[-1].rolled_back_user_text == "prompt A"
        assert_aligned(engine, 1)
        assert target.read_text(encoding="utf-8") == "original"


async def test_restore_unclosed_tail_seals_before_setting_number(tmp_path, monkeypatch, agent_engine):
    """Emulate SIGKILL by removing final status and turn markers on disk."""
    store = JsonFileStateStore(tmp_path / "state")
    engine = await start_engine(agent_engine, monkeypatch, tmp_path, store, SpecClient([answer("one"), answer("two")]))
    await ask(engine, "first")
    await ask(engine, "killed prompt")
    sid, directory = engine.session_id, engine.session_dir
    await engine.shutdown()
    state = await store.load_session(sid)
    state["messages"] = [
        m
        for m in state["messages"]
        if not (
            m.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN
            and m.additional_properties.get("_turn") == 2
        )
    ]
    state["turn_counter"] = 1
    await store.save_session(sid, state, agent_profile="Spec", primary_cwd=str(tmp_path))
    restored = await start_engine(agent_engine, monkeypatch, tmp_path, store, SpecClient([answer("three")]))
    await restored.event_bus.publish(SessionRestore(session_id=sid), raise_handler_errors=True)
    assert_aligned(restored, 2)
    kinds = [m.additional_properties.get(HistoryMarkerKind.KEY) for m in restored.history_messages]
    assert HistoryMarkerKind.INTERRUPTED in kinds
    await ask(restored, "after crash")
    assert_aligned(restored, 3)
    assert directory.exists()


async def test_stale_revision_cannot_retract_a_new_turn(tmp_path, monkeypatch, agent_engine):
    """A request captured before another turn must be refused, without any rollback."""
    store = JsonFileStateStore(tmp_path / "state")
    engine = await start_engine(agent_engine, monkeypatch, tmp_path, store, SpecClient([answer("one"), answer("two")]))
    await ask(engine, "first")
    revision = engine.conversation_revision
    await ask(engine, "second")
    results = await capture_events(engine.event_bus, RollbackResult)
    warnings = await capture_events(engine.event_bus, Warning)
    await engine.event_bus.publish(
        UserRollback(target_turn=0, expected_conversation_revision=revision), raise_handler_errors=True
    )
    assert not results
    assert warnings
    assert_aligned(engine, 2)
    assert "second" in texts(engine, "user")


@pytest.mark.parametrize("operation", ["welcome", "clear", "delete"])
async def test_session_cleanup_removes_snapshots(tmp_path, monkeypatch, agent_engine, operation):
    """Every whole-session deletion route removes the rollback snapshots."""
    store = JsonFileStateStore(tmp_path / "state")
    engine = await start_engine(agent_engine, monkeypatch, tmp_path, store, SpecClient([answer("one"), answer("two")]))
    await ask(engine, "first")
    await ask(engine, "second")
    sid, directory = engine.session_id, engine.session_dir
    assert list((directory / "snapshots").rglob("*"))
    if operation == "welcome":
        await engine.event_bus.publish(UserRollback(target_turn=0), raise_handler_errors=True)
    elif operation == "clear":
        await engine.event_bus.publish(SessionClear(session_id=sid), raise_handler_errors=True)
    else:
        await engine.shutdown()
        await store.delete_session(sid)
    assert not list((directory / "snapshots").rglob("*"))
    assert not (directory / "session.json").exists()


async def test_rollback_to_early_turn_restores_history(tmp_path, monkeypatch, agent_engine):
    """The first retained target still works after 30+ turns and newer snapshots are removed."""
    store = JsonFileStateStore(tmp_path / "state")
    # A retention window larger than the run keeps turn 1 targetable (the default is 20).
    engine = await start_engine(
        agent_engine, monkeypatch, tmp_path, store, SpecClient([answer(f"reply {n}") for n in range(33)]), keep=50
    )
    await ask(engine, "question 1")
    for n in range(2, 34):
        await ask(engine, f"question {n}")
    assert 1 in engine.available_rollback_turns()
    results = await capture_events(engine.event_bus, RollbackResult)
    await engine.event_bus.publish(UserRollback(target_turn=1), raise_handler_errors=True)
    assert len(results) == 1
    assert results[0].rolled_back_user_text == "question 2"
    assert_aligned(engine, 1)
    assert texts(engine, "user") == ["question 1"]
    remaining = list((engine.session_dir / "snapshots").glob("turn_*.json"))
    assert not any(int(p.stem.removeprefix("turn_")) > 2 for p in remaining)


async def test_empty_turn_missing_original_snapshot_is_recreated(tmp_path, monkeypatch, agent_engine):
    """The turn replacing an empty one writes its own snapshot."""
    store = JsonFileStateStore(tmp_path / "state")
    engine = await start_engine(
        agent_engine, monkeypatch, tmp_path, store, SpecClient([answer("base"), answer("FAIL"), answer("new answer")])
    )
    await ask(engine, "baseline")
    await ask(engine, "empty failure")
    (engine.session_dir / "snapshots" / "turn_2.json").unlink()
    await ask(engine, "replacement")
    assert_aligned(engine, 2)
    assert 1 in engine.available_rollback_turns()
    results = await capture_events(engine.event_bus, RollbackResult)
    await engine.event_bus.publish(UserRollback(target_turn=1), raise_handler_errors=True)
    assert len(results) == 1
    assert results[0].rolled_back_user_text == "replacement"
    assert "replacement" not in texts(engine, "user")
