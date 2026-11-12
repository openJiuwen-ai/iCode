# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""End-to-end retry through a real ``AgentEngine`` — after interrupt, after error, after restore.

Each scenario drives a started engine over the event bus (interrupt, retry,
retry-with-note, restore-then-retry) and asserts what lands in session history
and on disk.  The ``TurnResumePolicy.retry_request`` unit surface lives in ``test_resume.py``
and the run-package lifecycle in ``test_retry_lifecycle.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    ContextCompressed,
    Error,
    InvocationMessage,
    InvocationRetryAttempt,
    SessionReady,
    SessionRestore,
    SessionRestored,
    SessionSaved,
    UsageUpdate,
    UserInterrupt,
    UserMessage,
    UserRetry,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import Content, Message
from chrys.orchestration.engine.engine import AgentEngine
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.context.compaction.last_words import LastWordsGenerator
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from tests.orchestration.engine.run._engine_run_helpers import (
    _PROFILE,
    _filter,
    _final_agent_messages_after_run,
    _history_messages,
    _make_registry,
    _wait_for_call_count,
)
from tests.support.event_capture import collect_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.scripted_clients import HostedMockChatClient, HostedMockResponse
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for, wait_until

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


# ---------------------------------------------------------------------------
# Engine bootstrap
# ---------------------------------------------------------------------------

_RETRY_EVENT_TYPES: tuple[type, ...] = (SessionReady, InvocationMessage, Error, UsageUpdate, SessionSaved)
_RESTORE_EVENT_TYPES: tuple[type, ...] = (
    SessionReady,
    SessionRestored,
    InvocationMessage,
    Error,
    UsageUpdate,
    SessionSaved,
)
_COMPRESSION_EVENT_TYPES: tuple[type, ...] = (
    SessionReady,
    InvocationMessage,
    ContextCompressed,
    Error,
    InvocationRetryAttempt,
    SessionSaved,
)


@dataclass(slots=True)
class StartedRetryEngine:
    """A started engine plus the handles the retry scenarios drive it through."""

    engine: AgentEngine
    bus: EventBus
    events: list[object]
    clients: list[MockChatClient]


async def started_retry_engine(
    agent_engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    client_factory: Callable[[], MockChatClient],
    stream: bool = True,
    settings: Settings | None = None,
    model_registry: ModelProfileRegistry | None = None,
    agent_registry: AgentProfileRegistry | None = None,
    state_store: JsonFileStateStore | None = None,
    profile: AgentProfile = _PROFILE,
    event_types: Sequence[type] = _RETRY_EVENT_TYPES,
) -> StartedRetryEngine:
    """Build, wire, and start the engine every retry scenario needs.

    Collects *event_types* into one list, points ``create_client`` at
    *client_factory* while recording every client it hands out, and starts the
    engine on *profile* through the ``agent_engine`` fixture so shutdown stays
    the fixture's job.  Passing *settings* explicitly leaves the model registry
    alone; otherwise both come from the mock profile at *stream*, with advisory
    workspace scans disabled. Every engine uses the test's temporary workspace.
    """
    events: list[object] = []
    bus = EventBus()
    for cls in event_types:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    if settings is None:
        settings, mock_model_registry = make_mock_settings_and_registry(stream=stream)
        if model_registry is None:
            model_registry = mock_model_registry

    clients: list[MockChatClient] = []

    async def _create_client(s=None, **kw):
        client = client_factory()
        clients.append(client)
        return client

    monkeypatch.setattr(builder_module, "create_client", _create_client)
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=agent_registry if agent_registry is not None else _make_registry(),
        model_registry=model_registry,
        state_store=state_store if state_store is not None else JsonFileStateStore(tmp_path),
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    await engine.start(profile)
    return StartedRetryEngine(engine=engine, bus=bus, events=events, clients=clients)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_retry_after_interrupt_preserves_tools(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Interrupt mid-run, then retry → completed tools preserved, no duplicate user message.

    Flow:
    1. User sends message, agent streams slowly
    2. User interrupts
    3. User retries → agent continues from completed work
    4. Verify: no duplicate user messages, completed work preserved
    """
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(
            responses=[
                # Run 1: slow stream, will be interrupted
                MockResponse(text="A" * 20, chunk_size=2, chunk_delay=0.02),
                # Run 2 (retry): fast response
                MockResponse(text="Continued successfully."),
            ]
        ),
    )
    bus, events, engine, mock_clients = started.bus, started.events, started.engine, started.clients

    # Send message and interrupt mid-stream (poll until the run reaches the
    # LLM so the interrupt lands during the stream, not before it starts).
    await bus.publish(UserMessage(text="Do something"))
    await _wait_for_call_count(mock_clients[0], 1)
    await bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # Verify interrupted state
    msgs = _history_messages(engine)
    user_msgs = [m for m in msgs if getattr(m, "role", "") == "user"]
    assert len(user_msgs) >= 1, "User message should be preserved after interrupt"

    # Now retry
    events.clear()
    await bus.publish(UserRetry())

    # After retry, should have a final response
    finals = await _final_agent_messages_after_run(engine, events)
    assert len(finals) == 1, f"Expected 1 final message after retry, got {len(finals)}"

    # Verify no "continue" user message in final state
    msgs = _history_messages(engine)
    continue_msgs = [m for m in msgs if getattr(m, "role", "") == "user" and (m.text or "").strip() == "continue"]
    assert len(continue_msgs) == 0, "Continuation 'continue' message should be cleaned up"

    # Only one user message (the original)
    user_msgs = [m for m in msgs if getattr(m, "role", "") == "user"]
    original_msgs = [m for m in user_msgs if (m.text or "").strip() == "Do something"]
    assert len(original_msgs) == 1, f"Expected exactly 1 original user message, got {len(original_msgs)}"

    # No stale interrupted markers after successful retry
    interrupted = [
        m
        for m in msgs
        if (getattr(m, "additional_properties", None) or {}).get(HistoryMarkerKind.KEY) == HistoryMarkerKind.INTERRUPTED
    ]
    assert len(interrupted) == 0, "Interrupted markers should be cleaned up after successful retry"


async def test_retry_with_note_after_interrupt_is_mid_turn(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """User types a note and clicks Continue after an interrupt.

    The note becomes a real user message in history (not stripped like
    ``"continue"``), no ``"continue"`` placeholder appears in history,
    and the turn_counter is unchanged — the note is mid-turn.
    """
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(
            responses=[
                # Run 1: slow stream, will be interrupted
                MockResponse(text="A" * 20, chunk_size=2, chunk_delay=0.02),
                # Run 2 (retry with note): fast response
                MockResponse(text="Picked up your note."),
            ]
        ),
    )
    bus, events, engine, mock_clients = started.bus, started.events, started.engine, started.clients

    await bus.publish(UserMessage(text="Do something"))
    await _wait_for_call_count(mock_clients[0], 1)
    await bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    turn_counter_before = engine.current.loaded.bindings.backend.history_state.get("turn_counter", 0)

    events.clear()
    await bus.publish(UserRetry(text="also check env vars"))

    finals = await _final_agent_messages_after_run(engine, events)
    assert len(finals) == 1, f"Expected 1 final after retry-with-note, got {len(finals)}"

    msgs = _history_messages(engine)

    # No bare "continue" placeholder was persisted.
    continue_msgs = [m for m in msgs if getattr(m, "role", "") == "user" and (m.text or "").strip() == "continue"]
    assert len(continue_msgs) == 0, "Mid-turn note must not leave a 'continue' placeholder behind"

    # The user's note is a real user message.
    note_msgs = [
        m for m in msgs if getattr(m, "role", "") == "user" and (m.text or "").strip() == "also check env vars"
    ]
    assert len(note_msgs) == 1, f"User note missing from history: {[m.text for m in msgs if m.role == 'user']}"

    # Both the original prompt and the note survive — the note does
    # not replace the original.
    original_msgs = [m for m in msgs if getattr(m, "role", "") == "user" and (m.text or "").strip() == "Do something"]
    assert len(original_msgs) == 1

    # Mid-turn semantics: turn_counter unchanged across the retry.
    turn_counter_after = engine.current.loaded.bindings.backend.history_state.get("turn_counter", 0)
    assert turn_counter_after == turn_counter_before, (
        f"Retry-with-note must be mid-turn: turn_counter {turn_counter_before} → {turn_counter_after}"
    )

    # No stale interrupted markers.
    interrupted = [
        m
        for m in msgs
        if (getattr(m, "additional_properties", None) or {}).get(HistoryMarkerKind.KEY) == HistoryMarkerKind.INTERRUPTED
    ]
    assert len(interrupted) == 0


@pytest.mark.parametrize(
    ("marker_source", "expected_state"),
    [("user", EngineState.INTERRUPTED), ("error", EngineState.FAILED)],
)
async def test_retry_after_restored_interrupted_session_starts_model_call(
    tmp_path: Path,
    marker_source: str,
    expected_state: EngineState,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restored terminal history should rebuild FSM state so Continue starts a retry."""
    state_store = JsonFileStateStore(tmp_path)
    session_id = f"restore-interrupted-session-{marker_source}"
    user = Message("user", ["What tools do you have?"])
    interrupted = Message("assistant", ["Execution interrupted"])
    interrupted.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    interrupted.additional_properties["_interrupted_by"] = marker_source
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn.additional_properties["_turn"] = 1
    await state_store.save_session(
        session_id,
        {"messages": [user, interrupted, turn], "turn_counter": 1},
        agent_profile="Code",
        agent_display_name="Code Agent",
        primary_cwd=str(tmp_path),
        agent_profile_history=["Code"],
    )

    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(responses=[MockResponse(text="Restored retry completed.")]),
        state_store=state_store,
        event_types=_RESTORE_EVENT_TYPES,
    )
    bus, events, engine, mock_clients = started.bus, started.events, started.engine, started.clients

    await bus.publish(SessionRestore(session_id=session_id))
    assert await wait_until(lambda: len(_filter(events, SessionRestored)) >= 1), (
        "session restore did not complete in time"
    )
    assert engine.state is expected_state

    events.clear()
    await bus.publish(UserRetry())

    finals = await _final_agent_messages_after_run(engine, events)
    assert len(finals) == 1
    assert mock_clients[-1].call_count == 1

    msgs = _history_messages(engine)
    interrupted_markers = [
        m for m in msgs if m.additional_properties.get(HistoryMarkerKind.KEY) == HistoryMarkerKind.INTERRUPTED
    ]
    assert interrupted_markers == []
    user_msgs = [m for m in msgs if m.role == "user"]
    assert [(m.text or "").strip() for m in user_msgs] == ["What tools do you have?"]
    assert engine.current.loaded.bindings.backend.history_state.get("turn_counter", 0) == 1


@pytest.mark.parametrize("has_trailing_turn_marker", [False, True])
async def test_retry_after_restored_awaiting_sub_agents_marker_starts_model_call(
    tmp_path: Path,
    has_trailing_turn_marker: bool,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restored awaiting-sub-agents markers should become retryable terminal history."""
    state_store = JsonFileStateStore(tmp_path)
    session_id = f"restore-awaiting-sub-agents-session-{has_trailing_turn_marker}"
    user = Message("user", ["Run the child agent"])
    awaiting = Message("assistant", ["Awaiting 1 sub-agent(s)"])
    awaiting.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.AWAITING_SUB_AGENTS
    awaiting.additional_properties["_invocation_ids"] = ["inv-1"]
    messages = [user, awaiting]
    turn_counter = 0
    if has_trailing_turn_marker:
        turn = Message("assistant", [""])
        turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
        turn.additional_properties["_turn"] = 1
        messages.append(turn)
        turn_counter = 1
    await state_store.save_session(
        session_id,
        {"messages": messages, "turn_counter": turn_counter},
        agent_profile="Code",
        agent_display_name="Code Agent",
        primary_cwd=str(tmp_path),
        agent_profile_history=["Code"],
    )

    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(responses=[MockResponse(text="Restored awaiting retry completed.")]),
        state_store=state_store,
        event_types=_RESTORE_EVENT_TYPES,
    )
    bus, events, engine, mock_clients = started.bus, started.events, started.engine, started.clients

    await bus.publish(SessionRestore(session_id=session_id))
    assert await wait_until(lambda: len(_filter(events, SessionRestored)) >= 1), (
        "session restore did not complete in time"
    )
    assert engine.state is EngineState.FAILED
    repaired_raw = await state_store.load_session_raw(session_id)
    assert repaired_raw is not None
    repaired_kinds = [message.get("additional_properties", {}).get(HistoryMarkerKind.KEY) for message in repaired_raw]
    assert HistoryMarkerKind.AWAITING_SUB_AGENTS not in repaired_kinds
    assert HistoryMarkerKind.INTERRUPTED in repaired_kinds

    events.clear()
    await bus.publish(UserRetry())

    finals = await _final_agent_messages_after_run(engine, events)
    assert len(finals) == 1
    assert mock_clients[-1].call_count == 1

    msgs = _history_messages(engine)
    status_markers = [
        m for m in msgs if m.additional_properties.get(HistoryMarkerKind.KEY) in HistoryMarkerKind.STATUS_MARKERS
    ]
    assert status_markers == []
    user_msgs = [m for m in msgs if m.role == "user"]
    assert [(m.text or "").strip() for m in user_msgs] == ["Run the child agent"]
    assert engine.current.loaded.bindings.backend.history_state.get("turn_counter", 0) == 1


async def test_retry_after_restored_first_turn_compacts_its_tool_work(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Continue on a restored first turn with completed tool work sends no new
    input: the stored opener is the last user message, rebuilt by the reminder
    middleware.  With nothing earlier in state to anchor the history segment,
    compaction must still resolve that turn and drop its work: all but the
    newest of it, which the request keeps beside the note."""
    profile = AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="You are a coding assistant.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=True),
    )
    registry = AgentProfileRegistry()
    registry.register(profile)
    state_store = JsonFileStateStore(tmp_path)
    session_id = "restore-first-turn-tool-work"
    user = Message("user", ["Inspect the repository"])
    call = Message("assistant", [Content.from_function_call("read-1", "read_file", arguments={"path": "a.py"})])
    # Larger than the recent work a drop leaves in place; the second read is well within it.
    result = Message("tool", [Content.from_function_result("read-1", result="source " * 40_000)])
    recent_call = Message("assistant", [Content.from_function_call("read-2", "read_file", arguments={"path": "b.py"})])
    recent_result = Message("tool", [Content.from_function_result("read-2", result="source " * 200)])
    interrupted = Message("assistant", ["Execution interrupted"])
    interrupted.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.INTERRUPTED
    interrupted.additional_properties["_interrupted_by"] = "error"
    turn = Message("assistant", [""])
    turn.additional_properties[HistoryMarkerKind.KEY] = HistoryMarkerKind.TURN
    turn.additional_properties["_turn"] = 1
    await state_store.save_session(
        session_id,
        {"messages": [user, call, result, recent_call, recent_result, interrupted, turn], "turn_counter": 1},
        agent_profile="Code",
        agent_display_name="Code Agent",
        primary_cwd=str(tmp_path),
        agent_profile_history=["Code"],
    )

    async def generate(self: LastWordsGenerator, *_args: object, **_kwargs: object) -> str:
        return "Read a.py. Next, report what it does."

    generate_call = create_autospec(LastWordsGenerator.generate, side_effect=generate)
    monkeypatch.setattr(LastWordsGenerator, "generate", generate_call)
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(responses=[MockResponse(text="Reported from the note.")]),
        agent_registry=registry,
        state_store=state_store,
        profile=profile,
        event_types=_RESTORE_EVENT_TYPES,
    )
    bus, events, engine, mock_clients = started.bus, started.events, started.engine, started.clients

    await bus.publish(SessionRestore(session_id=session_id))
    await wait_for(lambda: len(_filter(events, SessionRestored)) >= 1, description="session restore")
    assert engine.current.loaded is not None
    strategy = engine.current.loaded.bindings.backend.compaction_strategy
    assert strategy is not None
    # Only a current turn exists, so an unreachable target leaves Phase 4 as
    # the one phase that can act.
    strategy.trigger_pct = 0.00001
    strategy.target_pct = strategy.satisfied_pct = strategy.force_pct = 0.000005

    events.clear()
    await bus.publish(UserRetry())
    finals = await _final_agent_messages_after_run(engine, events)

    assert len(finals) == 1
    assert generate_call.call_count == 1
    (sent, _options) = mock_clients[-1].call_history[-1]
    assert [
        (content.type, content.call_id)
        for message in sent
        for content in message.contents
        if content.type in ("function_call", "function_result")
    ] == [("function_call", "read-2"), ("function_result", "read-2")]
    (opener,) = [m for m in sent if m.role == "user"]
    assert opener.text.startswith("Inspect the repository")
    assert "Read a.py. Next, report what it does." in opener.text


async def test_retry_after_error_no_progress(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Error before any tools execute → retry re-sends original message.

    Flow:
    1. User sends message, LLM call fails immediately
    2. User retries → same message re-sent
    3. Second attempt succeeds
    """
    call_count = 0

    class FailThenSucceedClient(MockChatClient):
        def _inner_get_response(self, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Simulated non-retryable error")
            return super()._inner_get_response(**kwargs)

    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: FailThenSucceedClient(
            responses=[
                MockResponse(text="Success after retry"),
                MockResponse(text="Success after retry"),  # extra for safety
            ]
        ),
        settings=Settings(workspace_change_notice=False),
    )
    bus, events, engine = started.bus, started.events, started.engine

    # Send message — will fail
    await bus.publish(UserMessage(text="Do work"))
    await wait_for(
        lambda: len(_filter(events, Error)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="resume failure error event",
    )

    errors = _filter(events, Error)
    assert len(errors) >= 1, "Should have received an error"

    # Retry
    events.clear()
    await bus.publish(UserRetry())

    # Should succeed this time
    finals = await _final_agent_messages_after_run(engine, events)
    assert len(finals) == 1

    # Only one user message in state
    msgs = _history_messages(engine)
    user_msgs = [m for m in msgs if getattr(m, "role", "") == "user"]
    assert len(user_msgs) == 1
    assert (user_msgs[0].text or "").strip() == "Do work"


async def test_retry_persists_carried_compression_before_transient_attempt_rollback(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fold queued by the failed pass must be durable across retry rollback.

    This models the live failure sequence precisely: a terminal error leaves
    a validated ``compress_context`` request pending; the user retries; the
    first stored-mode provider attempt applies the fold and then fails
    transiently; the automatic retry succeeds.  The live fold event and the
    saved session must describe the same compression.
    """
    state_store = JsonFileStateStore(tmp_path)
    raw_call_count = 0

    class RetryRollbackClient(MockChatClient):
        def _inner_get_response(self, **kwargs):
            nonlocal raw_call_count
            raw_call_count += 1
            if raw_call_count == 3:
                raise RuntimeError("terminal failure before retry")
            if raw_call_count == 4:
                raise ConnectionError("transient retry failure")
            return super()._inner_get_response(**kwargs)

    client = RetryRollbackClient(
        responses=[
            MockResponse(text="turn one"),
            MockResponse(text="turn two"),
            MockResponse(text="retry recovered"),
        ]
    )
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: client,
        stream=False,
        state_store=state_store,
        event_types=_COMPRESSION_EVENT_TYPES,
    )
    bus, events, engine = started.bus, started.events, started.engine
    assert engine.current.loaded is not None
    engine.current.loaded.bindings._chat_options = {"store": True}
    engine.current.loaded.bindings._BACKOFF_SCHEDULE = (0,)
    engine.current.loaded.bindings._max_retries_override = 1

    await bus.publish(UserMessage(text="first"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    await bus.publish(UserMessage(text="second"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    await bus.publish(UserMessage(text="will fail"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert _filter(events, Error)

    strategy = engine.current.loaded.bindings.backend.compaction_strategy
    assert strategy is not None
    context_id, _ = strategy.queue_compression("turn_1", "durable retry summary")

    events.clear()
    await bus.publish(UserRetry())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    compressed_events = _filter(events, ContextCompressed)
    assert [event.compressed_context_id for event in compressed_events] == [context_id]
    assert len(_filter(events, InvocationRetryAttempt)) == 1

    session_id = engine.session.session_id
    assert session_id is not None
    loaded = await state_store.load_session(session_id)
    assert loaded is not None
    assert [block.compressed_context_id for block in loaded["compressed_msgs"]] == [context_id]
    assert any(message.additional_properties.get("_block_id") == context_id for message in loaded["messages"])


async def test_stored_retry_preserves_published_auto_phase3_fold(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request-time auto fold is replayed silently after whole-run rollback."""
    profile = AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="You are a coding assistant.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=True),
    )
    registry = AgentProfileRegistry()
    registry.register(profile)
    state_store = JsonFileStateStore(tmp_path)
    raw_call_count = 0

    class AutoFoldRetryClient(MockChatClient):
        def _inner_get_response(self, **kwargs):
            nonlocal raw_call_count
            raw_call_count += 1
            if raw_call_count == 2:
                raise ConnectionError("transient failure after auto fold")
            return super()._inner_get_response(**kwargs)

    client = AutoFoldRetryClient(
        responses=[
            MockResponse(text="completed first turn"),
            MockResponse(text="recovered second turn"),
        ]
    )
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: client,
        stream=False,
        agent_registry=registry,
        state_store=state_store,
        profile=profile,
        event_types=_COMPRESSION_EVENT_TYPES,
    )
    bus, events, engine = started.bus, started.events, started.engine
    assert engine.current.loaded is not None
    engine.current.loaded.bindings._chat_options = {"store": True}
    engine.current.loaded.bindings._BACKOFF_SCHEDULE = (0,)
    engine.current.loaded.bindings._max_retries_override = 1

    await bus.publish(UserMessage(text="first turn"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    strategy = engine.current.loaded.bindings.backend.compaction_strategy
    assert strategy is not None
    # The first turn completed under normal thresholds. Force Phase 3 only
    # for the next request's first wire build, where the transient lands.
    strategy.trigger_pct = 0.00001
    strategy.target_pct = strategy.satisfied_pct = strategy.force_pct = 0.000005
    events.clear()

    second_input = "second turn"
    await bus.publish(UserMessage(text=second_input))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    compressed_events = _filter(events, ContextCompressed)
    assert len(compressed_events) == 1
    assert compressed_events[0].source == "auto"
    assert len(_filter(events, InvocationRetryAttempt)) == 1
    context_id = compressed_events[0].compressed_context_id

    session_id = engine.session.session_id
    assert session_id is not None
    loaded = await state_store.load_session(session_id)
    assert loaded is not None
    assert [block.compressed_context_id for block in loaded["compressed_msgs"]] == [context_id]
    assert any(message.additional_properties.get("_block_id") == context_id for message in loaded["messages"])
    assert [message.text for message in loaded["messages"] if message.role == "user"].count(second_input) == 1


async def test_multiple_interrupt_retry_notes_stay_in_single_turn(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """User interrupts twice within the same turn, sending a note each time.

    End state must contain the original prompt + both notes as distinct
    user messages inside a single turn (``turn_counter == 1``), with no
    ``"continue"`` placeholder ever persisted.
    """
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: MockChatClient(
            responses=[
                # Run 1 (original): slow stream, gets interrupted.
                MockResponse(text="A" * 40, chunk_size=2, chunk_delay=0.03),
                # Run 2 (retry with note 1): also slow, also interrupted.
                MockResponse(text="B" * 40, chunk_size=2, chunk_delay=0.03),
                # Run 3 (retry with note 2): fast, completes.
                MockResponse(text="Finished after both notes."),
            ]
        ),
    )
    bus, events, engine, mock_clients = started.bus, started.events, started.engine, started.clients

    # Run 1: original prompt, interrupt mid-stream.
    await bus.publish(UserMessage(text="original prompt"))
    await _wait_for_call_count(mock_clients[0], 1)
    await bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # Run 2: retry with note 1, interrupt mid-stream again.
    await bus.publish(UserRetry(text="note one"))
    await _wait_for_call_count(mock_clients[0], 2)
    await bus.publish(UserInterrupt())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    # Run 3: retry with note 2, let it complete.
    events.clear()
    await bus.publish(UserRetry(text="note two"))

    finals = await _final_agent_messages_after_run(engine, events)
    assert len(finals) == 1, f"Expected 1 final after the second note-retry, got {len(finals)}"

    msgs = _history_messages(engine)
    user_texts = [(m.text or "").strip() for m in msgs if getattr(m, "role", "") == "user"]

    # All three distinct user messages survived.
    assert "original prompt" in user_texts
    assert "note one" in user_texts
    assert "note two" in user_texts
    # Order: original → note one → note two.
    order = [t for t in user_texts if t in {"original prompt", "note one", "note two"}]
    assert order == ["original prompt", "note one", "note two"], f"Unexpected ordering: {order}"

    # No "continue" placeholder ever landed in history.
    assert "continue" not in user_texts, "Placeholder 'continue' must not leak into persisted history"

    # Still a single turn — two retries didn't bump turn_counter.
    assert engine.current.loaded.bindings.backend.history_state.get("turn_counter", 0) == 1, (
        "Two mid-turn retries must stay inside turn 1"
    )

    # No stale interrupted markers after the final successful retry.
    interrupted = [
        m
        for m in msgs
        if (getattr(m, "additional_properties", None) or {}).get(HistoryMarkerKind.KEY) == HistoryMarkerKind.INTERRUPTED
    ]
    assert len(interrupted) == 0


async def test_retry_that_polls_an_empty_background_response_never_creates_its_hosted_work_again(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry polls the response the failed turn left running; when the poll comes back empty, a new request would
    run the response's hosted call again, so the turn fails instead."""
    model_registry = ModelProfileRegistry()
    model_registry.register(
        ModelProfile(
            id="mock-profile",
            name="mock",
            provider="mock",
            model_id="mock",
            stream=True,
            # Stored responses can keep running in the background after the connection drops.
            chat_options='{"store": true}',
        )
    )
    started = await started_retry_engine(
        agent_engine,
        tmp_path,
        monkeypatch,
        client_factory=lambda: HostedMockChatClient(
            responses=[
                HostedMockResponse(
                    hosted=[Content.from_mcp_server_tool_call("mc1", "create_issue")],
                    continuation_token={"response_id": "resp_1"},
                    error_after_hosted=ConnectionResetError("connection reset by peer"),
                ),
                MockResponse(text=""),
                MockResponse(text="created again"),
            ]
        ),
        settings=Settings(model_profile="mock-profile", workspace_change_notice=False, max_transient_retries=0),
        model_registry=model_registry,
    )
    bus, engine, (client,) = started.bus, started.engine, started.clients

    await bus.publish(UserMessage(text="File the issue"))
    await _wait_for_call_count(client, 1)
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert engine.state is EngineState.FAILED

    await bus.publish(UserRetry())
    await _wait_for_call_count(client, 2)
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert engine.state is EngineState.FAILED
    assert client.call_count == 2
    _messages, poll_options = client.call_history[1]
    assert poll_options["continuation_token"] == {"response_id": "resp_1"}
