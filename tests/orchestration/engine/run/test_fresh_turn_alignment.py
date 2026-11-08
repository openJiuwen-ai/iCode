# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real failed/interrupted turns stay aligned through fresh prompts and every rollback API."""

from __future__ import annotations

import asyncio
import copy
import json
import shutil
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.engine.run.runner as runner_module
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import ManagedSession
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    RollbackResult,
    SessionRestore,
    TurnStarted,
    UnansweredInputDropped,
    UserInterrupt,
    UserMessage,
    UserRetry,
    UserRollback,
    Warning,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.event_types import EventType
from chrys.foundation.trajectory.metadata import read_analytics_item_id
from chrys.kernel import ChatResponseUpdate, FinishReason, FinishReasonLiteral, Message
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.execution import ExecutionLease, PromptPromotionResult
from chrys.orchestration.engine.state.machine import EngineState
from chrys.orchestration.engine.state.session_writer import SessionWriter
from chrys.orchestration.engine.trajectory import TrajectoryRecorder
from chrys.service.approval.policy import ApprovalMode
from chrys.service.context.providers.history import CompressibleHistoryProvider
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import ToolsConfig
from chrys.service.state.store import JsonFileStateStore
from chrys.service.tools.background import crash as background_crash
from chrys.service.trajectory.session import trajectory_events_path
from chrys.service.trajectory.state import TRAJECTORY_STATE_KEY, TurnRecord, append_turn
from tests.app.acp._server_fakes import _FakeClient
from tests.app.acp._session_manager_fakes import _manager
from tests.orchestration.engine.run._engine_run_helpers import _PROFILE, _make_registry
from tests.support.background_tasks import HeldProcess
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.trajectory_invariants import assert_trajectory_file_accounted
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

if TYPE_CHECKING:
    from collections.abc import AsyncIterable, Callable
    from pathlib import Path

    from chrys.foundation.config.settings_store import SettingsHandle
    from chrys.orchestration.engine.state.active_session import ActiveSession


class _ProviderRefusal(RuntimeError):
    """A terminal provider error, independent of transient retry budgets."""


class _EndingClient(MockChatClient):
    def __init__(self, ending: str, path: Path) -> None:
        turn = MockResponse(text="partial answer", chunk_delay=0)
        if ending == "thought":
            turn = MockResponse(reasoning_text="thinking", chunk_delay=0)
        elif ending == "file":
            turn = MockResponse(
                tool_calls=[("write_file", "write", {"path": str(path), "content": "changed", "overwrite": True})]
            )
        super().__init__([MockResponse(text="baseline"), turn, MockResponse(text="next"), MockResponse(text="next")])
        self.ending = ending
        self.held = asyncio.Event()
        self.release = asyncio.Event()

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterable[ChatResponseUpdate]:
        call = self.call_count
        if (self.ending == "failed" and call == 2) or (self.ending == "guidance_failed" and call == 3):
            raise _ProviderRefusal("scripted terminal failure")
        hold_before = (self.ending == "empty" and call == 2) or (self.ending == "file" and call == 3)
        if hold_before:
            self.held.set()
            await self.release.wait()
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update
            if call == 2 and self.ending == "failed_answer":
                raise _ProviderRefusal("failure after visible output")
            if call == 2 and self.ending in {"answer", "thought", "guidance_failed"}:
                self.held.set()
                await self.release.wait()


class _RepeatedNoteClient(MockChatClient):
    """The first turn answers a call, then shows a partial answer and fails; the next *refused* calls fail before output.

    The answered call is the turn's work: the first note drops the partial answer, as a Continue does.
    """

    def __init__(self, refused: int) -> None:
        super().__init__(
            [
                MockResponse(tool_calls=[("missing_tool", "work", {})]),
                MockResponse(text="partial answer", chunk_delay=0),
            ]
            + [MockResponse(text="unused") for _ in range(refused)]
            + [MockResponse(text="next", chunk_delay=0)]
        )
        self.refused = refused

    async def _stream_updates(
        self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
    ) -> AsyncIterable[ChatResponseUpdate]:
        call = self.call_count
        if 2 < call <= 2 + self.refused:
            raise _ProviderRefusal("the call carrying the note fails before output")
        async for update in super()._stream_updates(resp, model_id, finish_reason):
            yield update
        if call == 2:
            raise _ProviderRefusal("the first turn fails after its answer")


@dataclass
class _HostView:
    """The real engine and bus exposed at the ACP host boundary."""

    engine: AgentEngine

    @property
    def event_bus(self) -> EventBus:
        return self.engine.event_bus

    @property
    def session_id(self) -> str:
        return self.engine.session_id or ""


async def _ask(engine: AgentEngine, text: str) -> None:
    await engine.event_bus.publish(UserMessage(text=text))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)


@pytest.mark.parametrize("ending", ["answer", "empty", "thought", "guidance_failed", "file", "failed", "failed_answer"])
@pytest.mark.parametrize("restore", [False, True], ids=["live", "restored"])
@pytest.mark.parametrize("entry", ["relative", "absolute", "acp"])
async def test_fresh_prompt_and_rollback_share_one_turn_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, ending: str, restore: bool, entry: str
) -> None:
    """Required endings plus visible failure, live/restore, and three rollback paths."""
    path = tmp_path / "edited.txt"
    path.write_text("before", encoding="utf-8")
    client = _EndingClient(ending, path)
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    profile = replace(_PROFILE, tools=ToolsConfig(builtins=["filesystem.write"]))
    registry = _make_registry()
    registry.register(profile)
    store = JsonFileStateStore(tmp_path)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=registry,
        model_registry=models,
        state_store=store,
        initial_approval_mode=ApprovalMode.BYPASS,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(profile)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="failed input"))
    if ending not in {"failed", "failed_answer"}:
        await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="failed turn is in flight")
        await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    if ending == "guidance_failed":
        await engine.event_bus.publish(UserRetry(text="unanswered guidance"))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    failed_turn = engine.turn_records()[-1]

    sid = engine.session_id
    assert sid is not None
    snapshot = store.session_dir(sid) / "snapshots" / "turn_2.json"
    if restore:
        await engine.on_session_restore(SessionRestore(session_id=sid))
    await _ask(engine, "fresh input")

    # The guidance drops the answer the stopped turn showed before its call fails: no work is left.
    retained = ending in {"answer", "file", "failed_answer"}
    expected_count = 3 if retained else 2
    state = engine.current.require_loaded().bindings.backend.history_state
    assert state["turn_counter"] == engine.session.turn_number == expected_count
    assert [record.turn_number for record in engine.turn_records()] == list(range(1, expected_count + 1))
    tracker = engine.mutation_tracker
    assert tracker is not None
    assert [turn.turn_id for turn in tracker.get_all_turns()] == list(range(1, expected_count + 1))
    assert [message.text for message in state["messages"] if message.role == "user"] == (
        ["baseline input", "failed input", "fresh input"] if retained else ["baseline input", "fresh input"]
    )
    if not retained:
        saved = await asyncio.to_thread(snapshot.read_bytes)
        assert b"failed input" not in saved
        assert b"fresh input" not in saved
        log = assert_trajectory_file_accounted(trajectory_events_path(store.session_dir(sid)))
        retirement = [event for event in log.events if event.event_type == EventType.SESSION_ROLLBACK][-1]
        assert retirement.payload["reason_code"] == "fresh_turn_retired"
        assert retirement.payload["superseded_from_sequence"] == failed_turn.started_sequence
        assert retirement.payload["target_turn_id"] == engine.turn_records()[0].turn_id
    assert path.read_text(encoding="utf-8") == ("changed" if ending == "file" else "before")

    results: list[RollbackResult] = []
    await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))
    if entry == "acp":
        manager = _manager(str(tmp_path), store)
        manager._sessions[sid] = ManagedSession(sid, str(tmp_path), profile.name, _HostView(engine))  # type: ignore[arg-type]
        server = ChrysAcpServer(manager, initial_vision=False)
        server.on_connect(_FakeClient())
        await server.ext_method(
            "session/rollback", {"sessionId": sid, "targetTurn": expected_count - 1, "revertChanges": True}
        )
    else:
        event = UserRollback(relative_turns=1) if entry == "relative" else UserRollback(target_turn=expected_count - 1)
        await engine.event_bus.publish(event)

    assert len(results) == 1
    assert results[0].rolled_back_user_text == "fresh input"
    assert results[0].discarded_turn_count == 1
    restored_state = engine.current.require_loaded().bindings.backend.history_state
    assert restored_state["turn_counter"] == engine.session.turn_number == expected_count - 1
    assert path.read_text(encoding="utf-8") == ("changed" if ending == "file" else "before")
    assert not any(message.text == "fresh input" for message in restored_state["messages"])
    if retained:
        assert not any(
            message.additional_properties.get(HistoryMarkerKind.KEY) in HistoryMarkerKind.STATUS_MARKERS
            for message in restored_state["messages"]
        ), "the previous interrupt/error card must not return from the snapshot"


@pytest.mark.parametrize("stop", ["refused", "interrupted"])
async def test_guidance_sent_twice_stays_two_items_the_next_prompt_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, stop: str
) -> None:
    """Each guided retry announces its own note item, so history keeps both notes and the cleanup names both.

    Refused at the provider or stopped before the executor, the notes come back through the runner's fallback,
    and a bare retry re-sends the second.
    """
    refused = stop == "refused"
    client = _RepeatedNoteClient(refused=3 if refused else 0)
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path),
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "original")
    announced: list[str] = []
    cleanups: list[UnansweredInputDropped] = []

    async def on_started(event: TurnStarted) -> None:
        if not event.is_retry:
            return
        if event.opening_item_id is not None:
            announced.append(event.opening_item_id)
        if not refused:
            await engine.event_bus.publish(UserInterrupt())

    await engine.event_bus.subscribe(TurnStarted, on_started)
    await engine.event_bus.subscribe(UnansweredInputDropped, lambda event: collect_events(cleanups, event))
    for text in ("same note", "same note", ""):
        await engine.event_bus.publish(UserRetry(text=text))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert len(set(announced)) == 2
    history = engine.current.require_loaded().bindings.backend.history_state["messages"]
    assert [read_analytics_item_id(m.additional_properties) for m in history if m.text == "same note"] == announced

    await _ask(engine, "fresh input")

    (cleanup,) = cleanups
    assert cleanup.retired_previous_turn is False
    assert cleanup.removed_item_ids == tuple(announced)
    state = engine.current.require_loaded().bindings.backend.history_state
    assert [m.text for m in state["messages"] if m.role == "user"] == ["original", "fresh input"]
    assert client.call_count == (6 if refused else 3)


async def test_a_refused_retry_keeps_guidance_worded_like_its_opener_as_a_note_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    """The runner keeps guidance the provider refused: flagged, under the item its pass announced, opener untouched."""
    client = _RepeatedNoteClient(refused=1)
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path),
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "original")
    announced: list[str | None] = []

    async def on_started(event: TurnStarted) -> None:
        if event.is_retry:
            announced.append(event.opening_item_id)

    await engine.event_bus.subscribe(TurnStarted, on_started)
    await engine.event_bus.publish(UserRetry(text="original"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert client.call_count == 3
    history = engine.current.require_loaded().bindings.backend.history_state["messages"]
    opener, note = [m for m in history if m.role == "user"]
    assert (opener.text, note.text) == ("original", "original")
    assert HistoryMarkerKind.INJECTED_KEY not in opener.additional_properties
    assert note.additional_properties[HistoryMarkerKind.INJECTED_KEY] is True
    assert [read_analytics_item_id(note.additional_properties)] == announced


@pytest.mark.parametrize("awaiting", [False, True], ids=["killed", "paused-child"])
async def test_restore_closes_the_last_unfinished_turn_before_numbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, engine_services, awaiting: bool
) -> None:
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = MockChatClient([MockResponse(text="baseline"), MockResponse(text="next")])
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    history = engine_services(engine).history
    history.messages.extend([Message("user", ["unfinished input"]), Message("assistant", ["partial work"])])
    append_turn(history.state, TurnRecord(turn_number=2, turn_id="unfinished"))
    if awaiting:
        history.upsert_awaiting_sub_agents_marker(["paused-child"])
    tracker = engine.mutation_tracker
    assert tracker is not None
    tracker.start_turn(2)
    history.state["chrys_mutations"] = tracker.serialize()
    engine.session.turn_number = 2
    # Restore loads its candidate before shutdown: persist the deliberately
    # unfinished state that a killed process or paused parent left on disk.
    sid = engine.session_id
    assert sid is not None
    await store.save_session(sid, history.state, agent_profile=_PROFILE.name, primary_cwd=str(tmp_path))
    await engine.on_session_restore(SessionRestore(session_id=sid))

    state = engine.current.require_loaded().bindings.backend.history_state
    assert state["turn_counter"] == engine.session.turn_number == 2
    assert state["messages"][-1].additional_properties[HistoryMarkerKind.KEY] == HistoryMarkerKind.TURN
    assert engine_services(engine).history.trailing_status_marker() is not None
    await _ask(engine, "fresh input")
    state = engine.current.require_loaded().bindings.backend.history_state
    assert state["turn_counter"] == engine.session.turn_number == 3
    assert [record.turn_number for record in engine.turn_records()] == [1, 2, 3]


@pytest.mark.parametrize("restore", [False, True], ids=["live", "restored"])
async def test_incomplete_file_detection_retains_a_turn_without_model_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, caplog: pytest.LogCaptureFixture, restore: bool
) -> None:
    """Incomplete shell detection is retained before any history or ordinal is retired."""
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient("empty", tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="no output"))
    await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="empty stream held")
    tracker = engine.mutation_tracker
    assert tracker is not None
    tracker.mark_detection_truncated()
    await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    if restore:
        sid = engine.session_id
        assert sid is not None
        await engine.on_session_restore(SessionRestore(session_id=sid))

    await _ask(engine, "fresh input")

    state = engine.current.require_loaded().bindings.backend.history_state
    assert engine.state is EngineState.IDLE
    assert client.call_count == 3
    assert state["turn_counter"] == engine.session.turn_number == 3
    assert [record.turn_number for record in engine.turn_records()] == [1, 2, 3]
    tracker = engine.mutation_tracker
    assert tracker is not None
    assert [turn.turn_id for turn in tracker.get_all_turns()] == [1, 2, 3]
    assert tracker.get_all_turns()[1].detection_truncated is True
    assert [message.text for message in state["messages"] if message.role == "user"] == [
        "baseline input",
        "no output",
        "fresh input",
    ]
    assert sum("mutation tracker contains work" in record.message for record in caplog.records) == 1


@pytest.mark.parametrize("fault", ["save-error", "cancel-waiter"])
@pytest.mark.parametrize("ending", ["answer", "empty"])
async def test_cleanup_fault_still_finalizes_the_admitted_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
    caplog: pytest.LogCaptureFixture,
    fault: str,
    ending: str,
) -> None:
    """A committed prompt owns cleanup until its ordinary final save and release."""
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient(ending, tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="failed input"))
    await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="interrupted stream held")
    await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    sid = engine.session_id
    assert sid is not None
    old_snapshot = store.session_dir(sid) / "snapshots" / "turn_2.json"
    old_payload = old_snapshot.read_bytes()
    snapshot_capture = create_autospec(
        runner_module.capture_snapshot_writer, side_effect=runner_module.capture_snapshot_writer
    )
    monkeypatch.setattr(runner_module, "capture_snapshot_writer", snapshot_capture)
    entered = asyncio.Event()
    release = asyncio.Event()
    original_save = SessionWriter.save_current_session

    async def save(self: SessionWriter, *, raise_on_error: bool = False) -> bool:
        if raise_on_error:
            entered.set()
            await release.wait()
            if fault == "save-error":
                raise OSError("injected cleanup save failure")
        return await original_save(self, raise_on_error=raise_on_error)

    monkeypatch.setattr(SessionWriter, "save_current_session", save)
    await engine.event_bus.publish(UserMessage(text="fresh input"))
    await wait_for(entered.is_set, timeout=ENGINE_TURN_TIMEOUT, description="cleanup save entered")
    task = engine.turn_lifecycle_task
    assert task is not None
    assert not task.done()
    try:
        if fault == "cancel-waiter":
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "the shielded cleanup still belongs to this lifecycle"
    finally:
        release.set()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert not engine.is_turn_lifecycle_active
    assert client.call_count == (3 if fault == "save-error" else 2)
    assert engine.state is (EngineState.IDLE if fault == "save-error" else EngineState.INTERRUPTED)
    state = engine.current.require_loaded().bindings.backend.history_state
    count = 3 if ending == "answer" else 2
    assert state["turn_counter"] == engine.session.turn_number == count
    assert any(message.role == "user" and message.text == "fresh input" for message in state["messages"])
    if fault == "save-error":
        assert sum("fresh-turn cleanup could not be saved" in record.message for record in caplog.records) == 1
        sid = engine.session_id
        assert sid is not None
        snapshot_capture.assert_not_called()
        if ending == "empty":
            assert old_snapshot.read_bytes() == old_payload
            return
        assert not (store.session_dir(sid) / "snapshots" / f"turn_{count}.json").exists()
        assert count - 1 not in engine.available_rollback_turns()
        assert any(message.role == "assistant" and message.text == "next" for message in state["messages"])
        results: list[RollbackResult] = []
        warnings: list[Warning] = []
        await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))
        await engine.event_bus.subscribe(Warning, lambda event: collect_events(warnings, event))
        await engine.event_bus.publish(UserRollback(relative_turns=1))
        assert results == []
        assert [warning.code for warning in warnings] == ["rollback_unavailable"]
        assert engine.session.turn_number == count


async def test_retained_interrupted_turn_keeps_its_background_command_in_the_rollback_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    """The background registry uses the same ordinal as the retained message."""
    # HeldProcess owns no OS process; its fake PID must not trigger a process
    # table scan while the crash recorder saves the registry's membership.
    monkeypatch.setattr(background_crash, "_members", lambda _session: ())
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    client = _EndingClient("answer", tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path),
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="start a background command"))
    await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="turn with background command held")
    commands = engine.session.background_tasks
    assert commands is not None
    process = HeldProcess()
    command = commands.adopt(process, command="held command", trigger="timeout")
    assert command is not None
    assert command.turn == 2
    await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert not command.ended.is_set()
    await _ask(engine, "fresh input")
    assert engine.session.turn_number == 3
    assert command.turn == 2
    assert not command.ended.is_set()

    await engine.event_bus.publish(UserRollback(target_turn=1))

    assert command.ended.is_set()
    assert command.stopped_by == "rollback"
    assert process.closed
    assert engine.session.turn_number == 1


@pytest.mark.parametrize("ending", ["empty", "failed"])
@pytest.mark.parametrize("initial_snapshot", [False, True])
async def test_no_work_replacement_rewrites_its_start_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, ending: str, initial_snapshot: bool
) -> None:
    """A replacement recreates its boundary snapshot from the cleaned session on disk."""
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient(ending, tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    original_capture = runner_module.capture_snapshot_writer
    captures: list[int] = []
    replacement_payloads: list[tuple[bytes, bytes]] = []

    def capture(session: ActiveSession, handle: SettingsHandle) -> Callable[[], None]:
        captures.append(session.turn_number)
        if captures == [1, 2] and not initial_snapshot:
            # Capture failures are best-effort in production: no file remains.
            return lambda: None
        write = original_capture(session, handle)
        replacement = len(captures) == 3
        session_dir = session.session_dir_for(session.session_id or "")

        def checked_write() -> None:
            write()
            if replacement:
                assert session_dir is not None
                replacement_payloads.append(
                    (
                        (session_dir / "session.json").read_bytes(),
                        (session_dir / "snapshots" / "turn_2.json").read_bytes(),
                    )
                )

        return checked_write

    monkeypatch.setattr(runner_module, "capture_snapshot_writer", capture)
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="failed input"))
    if ending == "empty":
        await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="empty stream held")
        await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    sid = engine.session_id
    assert sid is not None
    snapshot = store.session_dir(sid) / "snapshots" / "turn_2.json"
    assert snapshot.exists() is initial_snapshot
    before = snapshot.read_bytes() if initial_snapshot else None
    await _ask(engine, "fresh input")

    assert captures == [1, 2, 2]
    assert len(replacement_payloads) == 1
    disk, restored = replacement_payloads[0]
    assert restored == disk
    assert b"failed input" not in restored
    assert b"fresh input" not in restored
    assert snapshot.read_bytes() != before
    assert snapshot.exists()
    assert 1 in engine.available_rollback_turns()
    results: list[RollbackResult] = []
    warnings: list[Warning] = []
    await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))
    await engine.event_bus.subscribe(Warning, lambda event: collect_events(warnings, event))
    await engine.event_bus.publish(UserRollback(target_turn=1))

    assert len(results) == 1
    assert results[0].rolled_back_user_text == "fresh input"
    assert results[0].discarded_turn_count == 1
    assert warnings == []
    assert engine.session.turn_number == 1


async def test_compacted_completed_turn_is_not_retired_with_the_empty_interrupted_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient("empty", tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "completed input")
    state = engine.current.require_loaded().bindings.backend.history_state
    marker = state["messages"][-1]
    CompressibleHistoryProvider.compress(state, marker.additional_properties["_turn_id"], "completed summary")
    summary = state["messages"][0]
    assert summary.additional_properties[HistoryMarkerKind.KEY] == HistoryMarkerKind.SUMMARY
    assert not any(
        message.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.TURN
        for message in state["messages"]
    )
    await engine.writer.save_current_session()
    cleaned: list[UnansweredInputDropped] = []
    starts: list[TurnStarted] = []
    await engine.event_bus.subscribe(UnansweredInputDropped, lambda event: collect_events(cleaned, event))
    await engine.event_bus.subscribe(TurnStarted, lambda event: collect_events(starts, event))

    await engine.event_bus.publish(UserMessage(text="empty input"))
    await wait_for(
        client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="empty turn after compacted history in flight"
    )
    await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    empty_turn_id = engine.turn_records()[-1].turn_id
    sid = engine.session_id
    assert sid is not None
    snapshot = store.session_dir(sid) / "snapshots" / "turn_2.json"
    captured = await asyncio.to_thread(snapshot.read_bytes)

    await _ask(engine, "fresh input")

    state = engine.current.require_loaded().bindings.backend.history_state
    assert state["messages"][0] == summary
    assert state["turn_counter"] == engine.session.turn_number == 2
    assert [message.text for message in state["messages"] if message.role == "user"] == ["fresh input"]
    assert [record.turn_number for record in engine.turn_records()] == [1, 2]
    assert empty_turn_id not in {record.turn_id for record in engine.turn_records()}
    tracker = engine.mutation_tracker
    assert tracker is not None
    assert [turn.turn_id for turn in tracker.get_all_turns()] == [1, 2]
    rewritten = await asyncio.to_thread(snapshot.read_bytes)
    assert b"completed summary" in rewritten
    boundary = json.loads(rewritten)["state"]["messages"]
    assert not any(message["role"] == "user" for message in boundary)
    assert rewritten != captured
    assert [event.turn_number for event in starts] == [2, 2]
    # History names the retired opener by the item its TurnStarted announced.
    assert starts[0].opening_item_id is not None
    assert [(event.retired_previous_turn, event.removed_item_ids, event.retired_turn_id) for event in cleaned] == [
        (True, (starts[0].opening_item_id,), empty_turn_id)
    ]


async def test_failed_promotion_leaves_every_previous_turn_ledger_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient("empty", tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="empty input"))
    await wait_for(
        client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="empty stream held before promotion fault"
    )
    await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    state = engine.current.require_loaded().bindings.backend.history_state
    before_messages = [message.to_dict() for message in state["messages"]]
    before_state = copy.deepcopy({key: value for key, value in state.items() if key != "messages"})
    before_number = engine.session.turn_number
    before_records = engine.turn_records()
    tracker = engine.mutation_tracker
    assert tracker is not None
    before_tracker = copy.deepcopy(tracker.get_all_turns())
    sid = engine.session_id
    assert sid is not None
    path = trajectory_events_path(store.session_dir(sid))
    relevant = {EventType.SESSION_ROLLBACK, EventType.BRANCH_SUPERSEDED, EventType.TURN_STARTED}
    before_events = [event for event in assert_trajectory_file_accounted(path).events if event.event_type in relevant]
    rejected = create_autospec(
        ExecutionLease.promote_fresh_admission_to_run, return_value=PromptPromotionResult(promoted=False, conflict=True)
    )
    monkeypatch.setattr(ExecutionLease, "promote_fresh_admission_to_run", rejected)

    event = UserMessage(text="refused input")
    await engine.event_bus.publish(event)

    rejected.assert_called_once()
    assert event.started_turn is False
    assert [message.to_dict() for message in state["messages"]] == before_messages
    assert {key: value for key, value in state.items() if key != "messages"} == before_state
    assert engine.session.turn_number == before_number
    assert engine.turn_records() == before_records
    assert tracker.get_all_turns() == before_tracker
    assert [
        event for event in assert_trajectory_file_accounted(path).events if event.event_type in relevant
    ] == before_events
    assert client.call_count == 2


@pytest.mark.parametrize("ending", ["interrupted", "failed"])
async def test_first_empty_turn_replacement_never_snapshots_the_unsaved_old_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, ending: str
) -> None:
    """No baseline exists: an empty cleanup save cannot authorize a disk snapshot."""
    held = asyncio.Event()
    release = asyncio.Event()

    class FirstTurnEmptyClient(MockChatClient):
        async def _stream_updates(
            self, resp: MockResponse, model_id: str, finish_reason: FinishReasonLiteral | FinishReason
        ) -> AsyncIterable[ChatResponseUpdate]:
            if self.call_count == 1:
                if ending == "failed":
                    raise _ProviderRefusal("empty first turn")
                held.set()
                await release.wait()
            async for update in super()._stream_updates(resp, model_id, finish_reason):
                yield update

    client = FirstTurnEmptyClient(
        [MockResponse(text="unused"), MockResponse(text="B answer"), MockResponse(text="C answer")]
    )
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    store = JsonFileStateStore(tmp_path / "original")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await engine.event_bus.publish(UserMessage(text="A empty prompt"))
    if ending == "interrupted":
        await wait_for(held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="first empty turn held")
        await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    sid = engine.session_id
    assert sid is not None
    session_dir = store.session_dir(sid)
    assert b"A empty prompt" in (session_dir / "session.json").read_bytes()

    await _ask(engine, "B replacement")

    assert engine.session.turn_number == 1
    assert not (session_dir / "snapshots" / "turn_1.json").exists()
    assert engine.available_rollback_turns() == [0]
    await engine.on_session_restore(SessionRestore(session_id=sid))
    state = engine.current.require_loaded().bindings.backend.history_state
    assert [message.text for message in state["messages"] if message.role == "user"] == ["B replacement"]
    assert engine.available_rollback_turns() == [0]

    def fail_snapshot(source: Path, destination: Path) -> None:
        raise OSError("next turn snapshot failed")

    monkeypatch.setattr("chrys.service.mutations.snapshot_files.atomic_copy_file", fail_snapshot)
    await _ask(engine, "C next turn")
    assert engine.session.turn_number == 2
    assert not (session_dir / "snapshots" / "turn_2.json").exists()
    # The stale turn_1 snapshot is never used: only the session start (no snapshot needed) stays a target.
    assert engine.available_rollback_turns() == [0]
    results: list[RollbackResult] = []
    warnings: list[Warning] = []
    await engine.event_bus.subscribe(RollbackResult, lambda event: collect_events(results, event))
    await engine.event_bus.subscribe(Warning, lambda event: collect_events(warnings, event))
    await engine.event_bus.publish(UserRollback(target_turn=1))
    assert not results
    assert warnings
    assert engine.session.turn_number == 2

    # A damaged copy must not recover A from an incorrectly targeted turn_1 snapshot.
    recovery = JsonFileStateStore(tmp_path / "recovery")
    recovery_dir = recovery.session_dir(sid)
    await asyncio.to_thread(shutil.copytree, session_dir, recovery_dir)
    for name in ("session.json", "session.json.bak"):
        (recovery_dir / name).write_bytes(b"broken")
    assert await recovery.load_session(sid) is None


@pytest.mark.parametrize("fault", ["retire-error", "missing-record"])
async def test_retirement_audit_failure_does_not_block_the_clean_save_or_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine, fault: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False)
    store = JsonFileStateStore(tmp_path)
    client = _EndingClient("empty", tmp_path / "unused")
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    engine = agent_engine(
        EventBus(),
        settings=settings,
        agent_registry=_make_registry(),
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(_PROFILE)
    await _ask(engine, "baseline input")
    await engine.event_bus.publish(UserMessage(text="empty input"))
    await wait_for(client.held.is_set, timeout=ENGINE_TURN_TIMEOUT, description="empty turn held")
    await engine.event_bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    sid = engine.session_id
    assert sid is not None
    directory = store.session_dir(sid)
    saved_before_audit: list[bytes] = []

    async def fail_retire(
        self: TrajectoryRecorder, *, retired_turn: TurnRecord, target_turns: list[TurnRecord]
    ) -> None:
        saved = (directory / "session.json").read_bytes()
        assert b"empty input" not in saved
        saved_before_audit.append(saved)
        raise OSError("trajectory unavailable")

    if fault == "retire-error":
        monkeypatch.setattr(TrajectoryRecorder, "retire_turn", fail_retire)
    else:
        state = engine.current.require_loaded().bindings.backend.history_state
        state[TRAJECTORY_STATE_KEY]["turn_log"].pop()
    await _ask(engine, "replacement input")
    assert engine.session.turn_number == 2
    assert engine.state is EngineState.IDLE
    boundary = (directory / "snapshots" / "turn_2.json").read_bytes()
    assert b"baseline input" in boundary
    assert b"empty input" not in boundary
    assert b"replacement input" not in boundary
    if fault == "retire-error":
        assert saved_before_audit == [boundary]
        assert "Failed to record fresh-turn retirement" in caplog.text
    else:
        assert "without a matching trajectory record" in caplog.text
    assert b"replacement input" in (directory / "session.json").read_bytes()
