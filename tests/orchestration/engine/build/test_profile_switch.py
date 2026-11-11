# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Integration tests — mid-session agent profile switching + session restore.

Verifies:
1. Profile switch preserves conversation history (messages survive across switches)
2. Correct events are published in the right order
3. Profile switch info is persisted on user-message metadata and surfaced in reminders
4. Session save captures profile_history and agent_profile_switches
5. Session restore replays messages with correct profile attribution
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import create_autospec

import pytest

from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    await_run_task_chain,
    wait_for,
    wait_until,
    with_wait_deadline,
)

if TYPE_CHECKING:
    from pathlib import Path

    from chrys.orchestration.engine.engine import AgentEngine

from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadStarted,
    AgentProfileSwitch,
    Error,
    InvocationMessage,
    ProfileSwitched,
    SessionReady,
    SessionRestore,
    SessionRestored,
    SessionSaved,
    SettingsReload,
    UsageUpdate,
    UserInterrupt,
    UserMessage,
    Warning,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.orchestration.engine.state.machine import EngineState
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.state.store import JsonFileStateStore
from tests.support.event_capture import collect_events

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CODE = AgentProfile(
    name="Code",
    id="agent-code-id",
    display_name="Code Agent",
    instructions="You are a coding assistant.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)

_EXPLORE = AgentProfile(
    name="Explore",
    id="agent-explore-id",
    display_name="Explore Agent",
    instructions="You are a code exploration assistant.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)

_DOCS = AgentProfile(
    name="docs",
    id="agent-docs-id",
    display_name="Docs Agent",
    instructions="You are a documentation writer.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)

_PROFILE_SWITCH_REBUILD_TIMEOUT_SECONDS = 5.0


@pytest.fixture(autouse=True)
def _isolated_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep real workspace scans independent of the checkout and session files."""
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    monkeypatch.chdir(workdir)


async def _complete_turn(engine: AgentEngine) -> None:
    """Drain preparation, response, history tagging, and persistence before assertions.

    A final frontend message precedes the turn's history updates and save.
    Real workspace I/O also belongs to this boundary and can exceed the generic
    five-second event wait on loaded Windows workers.
    """
    await await_run_task_chain(
        engine,
        turn_state=engine.turns.turn_state,
        timeout=ENGINE_TURN_TIMEOUT,
        propagate_inner_cancel=True,
        expect_installed=True,
    )


def _make_registry() -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    registry.register(_CODE)
    registry.register(_EXPLORE)
    registry.register(_DOCS)
    return registry


def _assert_one_reminder_pair(engine: AgentEngine) -> None:
    """The installed build renders, compacts into and restores one LAST_WORDS state."""
    loaded = engine.current.require_loaded()
    strategy = loaded.bindings._compaction_strategy
    assert loaded.reminder_middleware.renders_last_words(loaded.last_words)
    assert strategy._reminder_middleware is loaded.reminder_middleware
    assert strategy._last_words_state is loaded.last_words


def _filter(events: list, cls: type) -> list:
    return [e for e in events if isinstance(e, cls)]


# ---------------------------------------------------------------------------
# Helper: read switch tag from user message additional_properties
# ---------------------------------------------------------------------------


def _switched_to(msg) -> str | None:
    """Read the ``_switch_to`` tag from a Message."""
    return (getattr(msg, "additional_properties", None) or {}).get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY)


# ---------------------------------------------------------------------------
# Test: profile switch preserves history and publishes correct events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_profile_switch_preserves_history_and_events(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Start as Code Agent, chat, switch to Explore Agent, chat, switch to Docs Agent, chat.

    Verify:
    - All ProfileSwitched events have correct from/to names and display names
    - Messages from all profiles are preserved in session state
    - Profile switch info is stored on user-message metadata
    - Session save contains profile_history with display names
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, SessionSaved, Error, UsageUpdate]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    state_store = JsonFileStateStore(tmp_path)
    registry = _make_registry()

    # Each _build_agent call creates a new client.
    # We use a list of clients so each rebuild gets a fresh one with responses.
    mock_clients: list[MockChatClient] = []

    async def _mock_create_client(s=None, **kw):
        client = MockChatClient(
            responses=[
                MockResponse(text=f"Response #{len(mock_clients)}"),
            ]
        )
        mock_clients.append(client)
        return client

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=registry,
        state_store=state_store,
    )

    # ---- Phase 1: Start with Code Agent ----
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)
    spill_quota = engine.session.spill_quota
    assert engine.current.loaded is not None
    assert engine.current.loaded.bindings._compaction_strategy._spill_quota is spill_quota
    assert engine.current.loaded.bindings._compaction_strategy._persist_recovery_now == engine.writer.persist_now
    _assert_one_reminder_pair(engine)

    ready_events = _filter(events, SessionReady)
    assert len(ready_events) == 1
    assert ready_events[0].agent_profile == "Code"
    assert ready_events[0].display_name == "Code Agent"

    # Chat as Code Agent
    await bus.publish(UserMessage(text="Write a function"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    code_msgs = [e for e in _filter(events, InvocationMessage) if e.is_final]
    assert len(code_msgs) == 1

    # ---- Phase 2: Switch to Explore Agent ----
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    assert engine.session.spill_quota is spill_quota
    assert engine.current.loaded is not None
    assert engine.current.loaded.bindings._compaction_strategy._spill_quota is spill_quota
    _assert_one_reminder_pair(engine)

    switched_events = _filter(events, ProfileSwitched)
    assert len(switched_events) == 1
    assert switched_events[0].from_profile == "Code"
    assert switched_events[0].to_profile == "Explore"
    assert switched_events[0].from_display_name == "Code Agent"
    assert switched_events[0].to_display_name == "Explore Agent"
    assert switched_events[0].message_count > 0  # preserved messages

    # Chat as Explore Agent
    await bus.publish(UserMessage(text="Write a test"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2

    all_final = [e for e in _filter(events, InvocationMessage) if e.is_final]
    assert len(all_final) == 2

    # ---- Phase 3: Switch to Docs Agent ----
    await bus.publish(AgentProfileSwitch(profile_name="docs"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)
    assert engine.session.spill_quota is spill_quota

    switched_events = _filter(events, ProfileSwitched)
    assert len(switched_events) == 2
    assert switched_events[1].from_profile == "Explore"
    assert switched_events[1].to_profile == "docs"
    assert switched_events[1].from_display_name == "Explore Agent"
    assert switched_events[1].to_display_name == "Docs Agent"

    # Chat as Docs Agent
    await bus.publish(UserMessage(text="Write docs"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 3

    all_final = [e for e in _filter(events, InvocationMessage) if e.is_final]
    assert len(all_final) == 3

    # ---- Verify session state ----
    session_state = engine.current.loaded.bindings.backend.history_state
    messages = session_state.get("messages", [])

    # Should have: user + assistant (code) + turn_marker,
    #              user + assistant (explore) + turn_marker,
    #              user + assistant (docs)
    # = at least 6 messages (no assistant marker messages)
    assert len(messages) >= 6

    # Profile switch info tagged on user messages via additional_properties
    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 3
    # First user message: no switch
    assert _switched_to(user_msgs[0]) is None
    # Second user message: switch from Code to Explore
    assert _switched_to(user_msgs[1]) == "Explore Agent"
    assert (user_msgs[1].text or "") == "Write a test"
    # Third user message: switch from Explore to Docs
    assert _switched_to(user_msgs[2]) == "Docs Agent"
    assert (user_msgs[2].text or "") == "Write docs"

    # Verify agent_profile_switches in state
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 2
    assert switches[0]["from"] == "Code"
    assert switches[0]["to"] == "Explore"
    assert switches[0]["from_display"] == "Code Agent"
    assert switches[0]["to_display"] == "Explore Agent"
    assert switches[1]["from"] == "Explore"
    assert switches[1]["to"] == "docs"
    assert switches[1]["from_display"] == "Explore Agent"
    assert switches[1]["to_display"] == "Docs Agent"

    # ---- Save and verify persisted session ----
    await engine.shutdown()
    assert await wait_until(lambda: len(_filter(events, SessionSaved)) >= 1), "session was not persisted in time"

    sessions = await state_store.list_sessions()
    assert len(sessions) == 1
    meta = sessions[0]
    assert meta.agent_profile == "docs"
    assert meta.agent_display_name == "Docs Agent"
    assert meta.agent_profile_history == ["Code Agent", "Explore Agent", "Docs Agent"]
    assert meta.message_count >= 3  # at least 3 visible user+assistant pairs


# ---------------------------------------------------------------------------
# Test: profile switch preserves the session todo list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_profile_switch_preserves_todo_list(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """The todo tracker is session-scoped and profile-INDEPENDENT: it exists
    even for profiles without the ``todo`` category, survives a switch to
    such a profile (same object, no rebuild reset), and the post-switch save
    still persists ``chrys_todos``."""
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, SessionSaved, Error, UsageUpdate]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    state_store = JsonFileStateStore(tmp_path)

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
        state_store=state_store,
    )
    # _CODE has builtins=[] — no ``todo`` category — yet construction
    # still owns a tracker (profile-independence).
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)
    tracker = engine.todo_tracker
    assert tracker is not None

    todos = [{"content": "survive the switch", "status": "in_progress", "active_form": "Surviving"}]
    await tracker.restore(todos)

    # One turn so the session has real messages to persist.
    await bus.publish(UserMessage(text="hello"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)

    # Same tracker object — the rebuild must not reset session todo state.
    assert engine.todo_tracker is tracker
    assert engine.todo_tracker.serialize() == todos

    await engine.shutdown()
    assert await wait_until(lambda: len(_filter(events, SessionSaved)) >= 1)

    sessions = await state_store.list_sessions()
    assert len(sessions) == 1
    loaded = await state_store.load_session(sessions[0].session_id)
    assert loaded is not None
    assert loaded["chrys_todos"] == todos


# ---------------------------------------------------------------------------
# Test: session restore after profile switches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_session_restore_after_profile_switches(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Save a session with profile switches, then restore and verify everything loads correctly.

    Verify:
    - Restored session has correct profile (last used)
    - Message history is preserved
    - SessionRestored event has correct display_name
    - New messages after restore use the correct profile
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, SessionRestored, Error, UsageUpdate, SessionSaved]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    state_store = JsonFileStateStore(tmp_path)
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="Mock response")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    # ---- Phase 1: Build a session with switches ----
    engine = agent_engine(bus, settings=settings, agent_registry=registry, state_store=state_store)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    # Chat as Code Agent
    await bus.publish(UserMessage(text="Hello code"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    # Switch to Explore Agent and chat
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    await bus.publish(UserMessage(text="Hello test"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2

    # Save session
    saved_session_id = engine.session.session_id
    await engine.shutdown()
    assert await wait_until(lambda: len(_filter(events, SessionSaved)) >= 1), "session was not persisted in time"

    # Verify session was saved
    sessions = await state_store.list_sessions()
    assert len(sessions) == 1
    assert sessions[0].session_id == saved_session_id

    # ---- Phase 2: Create a fresh engine on a new bus and restore ----
    events2: list[object] = []
    bus2 = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, SessionRestored, Error, UsageUpdate, SessionSaved]:
        await bus2.subscribe(cls, lambda e, _events=events2: collect_events(_events, e))

    engine2 = agent_engine(bus2, settings=settings, agent_registry=registry, state_store=state_store)
    await engine2.start(_CODE)
    assert await wait_until(lambda: len(_filter(events2, SessionReady)) >= 1)

    # Restore the saved session
    await bus2.publish(SessionRestore(session_id=saved_session_id))
    assert await wait_until(lambda: len(_filter(events2, SessionRestored)) >= 1), (
        "session restore did not complete in time"
    )

    # Verify SessionRestored event
    restored_events = _filter(events2, SessionRestored)
    assert len(restored_events) == 1
    assert restored_events[0].session_id == saved_session_id
    assert restored_events[0].agent_profile == "Explore"  # last used profile
    assert restored_events[0].display_name == "Explore Agent"

    # Verify the engine is now using the Explore Agent profile
    assert engine2.session.agent_profile is not None
    assert engine2.session.agent_profile.name == "Explore"

    # Verify message history was restored (no assistant marker messages)
    session_state = engine2.current.loaded.bindings.backend.history_state
    messages = session_state.get("messages", [])
    assert len(messages) >= 4  # user, assistant, user, assistant (+ turn markers)

    # Verify switch info is stored on user-message metadata.

    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) >= 2
    assert _switched_to(user_msgs[1]) == "Explore Agent"

    # Verify we can still chat after restore
    await bus2.publish(UserMessage(text="Hello after restore"))
    await _complete_turn(engine2)
    assert len([e for e in _filter(events2, InvocationMessage) if e.is_final and "Mock response" in e.text]) >= 1

    post_restore_msgs = [e for e in _filter(events2, InvocationMessage) if e.is_final and "Mock response" in e.text]
    assert len(post_restore_msgs) >= 1

    # Verify agent_profile_switches is preserved in session state
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 1
    assert switches[0]["from_display"] == "Code Agent"
    assert switches[0]["to_display"] == "Explore Agent"


# ---------------------------------------------------------------------------
# Test: switching to same profile is a no-op
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_to_same_profile_is_noop(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Switching to the currently active profile should be a no-op."""
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    await bus.subscribe(ProfileSwitched, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert await wait_until(lambda: engine.current.loaded is not None)

    # Switch to same profile. ``bus.publish`` awaits the engine's switch
    # handler inline, so any ``ProfileSwitched`` it would emit has already
    # been processed by the time the publish returns — no barrier needed.
    await bus.publish(AgentProfileSwitch(profile_name="Code"))

    switched = _filter(events, ProfileSwitched)
    assert len(switched) == 1
    assert switched[0].from_profile == "Code"
    assert switched[0].to_profile == "Code"


@pytest.mark.asyncio
async def test_profile_switch_failure_publishes_agent_load_failed(
    monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    """A failed rebuild after AgentLoadStarted must clear the TUI loading state."""
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [AgentLoadStarted, AgentLoadFinished, AgentLoadFailed]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)

    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    events.clear()

    async def _fail_build(_profile: AgentProfile, _staged: object, **_kwargs: object) -> None:
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr(engine.loader, "build", _fail_build)

    # The TUI publishes ``AgentProfileSwitch`` fire-and-forget; the bus logs
    # handler failures and surfaces them via ``AgentLoadFailed`` so the loading
    # spinner clears.  The publish call itself must not raise.
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert engine.agent_profile is _CODE
    assert engine.current.loaded.agent is not None
    assert engine.current.loaded is not None

    assert [type(event) for event in events] == [AgentLoadStarted, AgentLoadFailed]
    failed = _filter(events, AgentLoadFailed)[0]
    assert failed.operation == "switch"
    assert failed.agent_profile == "Explore"
    assert failed.message == "rebuild failed"


@pytest.mark.asyncio
async def test_publish_load_failed_formats_empty_transport_exception() -> None:
    """Agent-load failures should not surface blank transport exception messages."""
    from chrys.orchestration.engine.loader import AgentLoader

    class ReadTimeout(Exception):
        pass

    events: list[AgentLoadFailed] = []
    bus = EventBus()

    async def _collect_load_failed(event: AgentLoadFailed) -> None:
        events.append(event)

    await bus.subscribe(AgentLoadFailed, _collect_load_failed)

    class _LoadFailurePublisher:
        _bus = bus
        _session = SimpleNamespace(session_id="sid")
        publish_load_failed = AgentLoader.publish_load_failed

    await _LoadFailurePublisher().publish_load_failed(
        operation="start",
        profile=_CODE,
        exc=ReadTimeout(TimeoutError()),
    )

    assert len(events) == 1
    assert events[0].message == "Read timed out (ReadTimeout)"


@pytest.mark.asyncio
async def test_user_message_waits_for_profile_switch_rebuild(monkeypatch: pytest.MonkeyPatch, agent_engine) -> None:
    """A message published during rebuild should run on the replacement executor."""
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[InvocationMessage] = []
    bus = EventBus()
    await bus.subscribe(InvocationMessage, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()
    response_texts = ["old executor response", "new executor response"]
    clients: list[MockChatClient] = []

    async def _mock_create_client(s=None, **kw):
        text = response_texts[len(clients)] if len(clients) < len(response_texts) else "extra response"
        client = MockChatClient(responses=[MockResponse(text=text)])
        clients.append(client)
        return client

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)

    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)

    original_build_agent = engine.loader.build
    rebuild_entered = asyncio.Event()
    release_rebuild = asyncio.Event()
    switch_task: asyncio.Task[None] | None = None
    message_task: asyncio.Task[None] | None = None

    async def _slow_rebuild(profile: AgentProfile, staged: object, **kwargs: object) -> None:
        if profile.name == "Explore":
            rebuild_entered.set()
            await release_rebuild.wait()
        return await original_build_agent(profile, staged, **kwargs)

    monkeypatch.setattr(engine.loader, "build", _slow_rebuild)

    try:
        switch_task = asyncio.create_task(bus.publish(AgentProfileSwitch(profile_name="Explore")))
        await asyncio.wait_for(rebuild_entered.wait(), timeout=5.0)

        message_task = asyncio.create_task(bus.publish(UserMessage(text="sent during rebuild")))
        await asyncio.sleep(0.05)

        assert [event for event in events if event.is_final] == []

        release_rebuild.set()
        await asyncio.wait_for(switch_task, timeout=_PROFILE_SWITCH_REBUILD_TIMEOUT_SECONDS)
        await asyncio.wait_for(message_task, timeout=_PROFILE_SWITCH_REBUILD_TIMEOUT_SECONDS)
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state, expect_installed=True)

        final_messages = [event for event in events if event.is_final]
        assert [event.text for event in final_messages] == ["new executor response"]
        assert clients[0].call_count == 0
        assert clients[1].call_count == 1
    finally:
        release_rebuild.set()
        for task in (switch_task, message_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


@pytest.mark.asyncio
async def test_user_interrupt_does_not_wait_for_profile_switch_rebuild(
    monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    """Interrupt during rebuild should return immediately and not hit the old executor."""
    from unittest.mock import AsyncMock

    import chrys.orchestration.engine.build.builder as builder_module

    warnings: list[Warning] = []
    bus = EventBus()
    await bus.subscribe(Warning, lambda e, _events=warnings: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)

    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert engine.current.loaded is not None
    interrupt_mock = AsyncMock()
    monkeypatch.setattr(engine.current.loaded.bindings, "interrupt", interrupt_mock)

    original_build_agent = engine.loader.build
    rebuild_entered = asyncio.Event()
    release_rebuild = asyncio.Event()
    switch_task: asyncio.Task[None] | None = None

    async def _slow_rebuild(profile: AgentProfile, staged: object, **kwargs: object) -> None:
        if profile.name == "Explore":
            rebuild_entered.set()
            await release_rebuild.wait()
        return await original_build_agent(profile, staged, **kwargs)

    monkeypatch.setattr(engine.loader, "build", _slow_rebuild)

    try:
        switch_task = asyncio.create_task(bus.publish(AgentProfileSwitch(profile_name="Explore")))
        await asyncio.wait_for(rebuild_entered.wait(), timeout=5.0)

        await asyncio.wait_for(bus.publish(UserInterrupt()), timeout=5.0)

        interrupt_mock.assert_not_awaited()
        assert [warning.code for warning in warnings] == ["agent_loading_interrupt_ignored"]

        release_rebuild.set()
        await asyncio.wait_for(switch_task, timeout=_PROFILE_SWITCH_REBUILD_TIMEOUT_SECONDS)
    finally:
        release_rebuild.set()
        if switch_task is not None and not switch_task.done():
            switch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await switch_task


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_settings_reload_recovers_after_startup_failure(
    monkeypatch: pytest.MonkeyPatch, agent_engine, manual: bool
) -> None:
    """Startup failure should leave backend handlers available for in-app recovery."""
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [AgentLoadFailed, AgentLoadFinished, SessionReady]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()
    calls = 0

    async def _mock_create_client(s=None, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("startup model unavailable")
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)

    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    with pytest.raises(RuntimeError, match="startup model unavailable"):
        await engine.start(_CODE)

    assert engine.current.loaded is None
    assert engine._subscribed is True

    await bus.publish(SettingsReload(session_id=engine.session_id, manual=manual))

    assert engine.current.loaded is not None
    assert engine.state is EngineState.IDLE
    assert any(isinstance(event, AgentLoadFailed) for event in events)
    assert any(isinstance(event, AgentLoadFinished) for event in events)
    assert any(isinstance(event, SessionReady) for event in events)


@pytest.mark.asyncio
async def test_reload_and_profile_switch_rebuilds_thread_updated_transient_budget(
    monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    """Rebuilds hand the executor the budget effective at rebuild time.

    A settings reload re-reads ``CHRYS_MAX_TRANSIENT_RETRIES`` from the
    environment; a subsequent profile switch rebuilds from the already-reloaded
    settings without consulting the environment again.
    """
    import chrys.orchestration.engine.build.builder as builder_module

    bus = EventBus()
    monkeypatch.delenv("CHRYS_MAX_TRANSIENT_RETRIES", raising=False)
    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok") for _ in range(4)])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert engine.current.loaded.bindings._max_retries_override == 10

    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", "9")
    pre_reload = engine.current.loaded.bindings
    await bus.publish(SettingsReload())
    assert await wait_until(
        lambda: engine.current.loaded is not None and engine.current.loaded.bindings is not pre_reload
    )
    assert engine.current.loaded.bindings._max_retries_override == 9

    # A changed environment value must NOT leak into the switch rebuild —
    # only a settings reload re-reads the environment.
    monkeypatch.setenv("CHRYS_MAX_TRANSIENT_RETRIES", "3")
    pre_switch = engine.current.loaded.bindings
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(
        lambda: engine.current.loaded is not None and engine.current.loaded.bindings is not pre_switch
    )
    assert engine.current.loaded.bindings._max_retries_override == 9


# ---------------------------------------------------------------------------
# Test: SessionReady / SessionRestored carry all workspace roots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_session_ready_and_restored_payloads_include_workspace_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
) -> None:
    """Both events must carry primary AND secondary workspace roots.

    Workspace-MRU consumers record the roots straight from these payloads;
    a publish site that forgets ``working_dirs`` silently drops secondary
    roots from the recent-directories list.
    """
    import chrys.orchestration.engine.build.builder as builder_module
    from chrys.foundation.models.workspace import WorkingDir, Workspace

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, SessionSaved, UsageUpdate]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    monkeypatch.setattr(
        builder_module,
        "create_client",
        create_autospec(
            builder_module.create_client,
            side_effect=lambda s=None, **kw: MockChatClient(responses=[MockResponse(text="ok")]),
        ),
    )

    primary = tmp_path / "primary"
    extra = tmp_path / "extra"
    primary.mkdir()
    extra.mkdir()
    settings = Settings()
    state_store = JsonFileStateStore(tmp_path / "sessions")
    registry = _make_registry()
    workspace = Workspace(
        primary_cwd=str(primary),
        working_dirs=[WorkingDir(path=str(primary), is_primary=True), WorkingDir(path=str(extra))],
    )

    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=registry,
        state_store=state_store,
        initial_workspace=workspace,
    )
    try:
        await engine.start(_CODE)
        assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

        ready = _filter(events, SessionReady)[0]
        assert ready.primary_cwd == str(primary)
        assert ready.working_dirs == [str(primary), str(extra)]

        await bus.publish(UserMessage(text="hello"))
        await _complete_turn(engine)
        assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1
        saved_session_id = engine.session.session_id
    finally:
        await engine.shutdown()
    assert await wait_until(lambda: len(_filter(events, SessionSaved)) >= 1), "session was not persisted in time"

    events2: list[object] = []
    bus2 = EventBus()
    for cls in [SessionReady, SessionRestored, Error]:
        await bus2.subscribe(cls, lambda e, _events=events2: collect_events(_events, e))
    engine2 = agent_engine(bus2, settings=settings, agent_registry=registry, state_store=state_store)
    await engine2.start(_CODE)
    assert await wait_until(lambda: len(_filter(events2, SessionReady)) >= 1)

    await bus2.publish(SessionRestore(session_id=saved_session_id))
    assert await wait_until(lambda: len(_filter(events2, SessionRestored)) >= 1), (
        "session restore did not complete in time"
    )

    restored = _filter(events2, SessionRestored)[0]
    assert restored.primary_cwd == str(primary)
    assert restored.working_dirs == [str(primary), str(extra)]


# ---------------------------------------------------------------------------
# Test: raw session file has correct structure after switches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_session_file_structure_after_switches(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Verify the JSON session file on disk has the expected meta fields."""
    import json

    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, SessionSaved, UsageUpdate]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    state_store = JsonFileStateStore(tmp_path)
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="reply")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry, state_store=state_store)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    await bus.publish(UserMessage(text="msg1"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)

    await bus.publish(UserMessage(text="msg2"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2

    await bus.publish(AgentProfileSwitch(profile_name="docs"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)

    session_id = engine.session.session_id
    await engine.shutdown()
    assert await wait_until(lambda: len(_filter(events, SessionSaved)) >= 1), "session was not persisted in time"

    # Read raw JSON from disk — folder-based, named by the short
    # projection of the session id (see SESSION_SHORT_ID_LEN).
    session_file = state_store.session_dir(session_id) / "session.json"
    assert session_file.exists()
    raw = json.loads(session_file.read_text(encoding="utf-8"))

    meta = raw["meta"]
    assert meta["agent_profile"] == "docs"
    assert meta["agent_profile_id"] == "agent-docs-id"
    assert meta["agent_display_name"] == "Docs Agent"
    assert meta["agent_profile_history"] == ["Code Agent", "Explore Agent", "Docs Agent"]

    state = raw["state"]
    assert "agent_profile_switches" in state
    switches = state["agent_profile_switches"]
    assert len(switches) == 2
    assert switches[0]["from"] == "Code"
    assert switches[0]["to"] == "Explore"
    assert switches[0]["from_display"] == "Code Agent"
    assert switches[0]["to_display"] == "Explore Agent"
    assert switches[1]["from"] == "Explore"
    assert switches[1]["to"] == "docs"


# ---------------------------------------------------------------------------
# Test: consecutive switches without conversation merge into one marker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_consecutive_switches_merge_markers(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Chat, then switch A->B->C without chatting -> merged into one switch.

    Verifies:
    - Only one entry in agent_profile_switches (not 2)
    - Pending profile switch merges original_from -> final_to
    - Chatting after the merged switch works correctly
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate, Error]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    state_store = JsonFileStateStore(tmp_path)
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="response")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry, state_store=state_store)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    # Chat as Code Agent
    await bus.publish(UserMessage(text="Write code"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1
    assert len(engine.current.loaded.bindings.backend.history_state.get("messages", [])) >= 3

    # Consecutive switches: code -> test -> docs (no chat in between)
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    await bus.publish(AgentProfileSwitch(profile_name="docs"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)

    session_state = engine.current.loaded.bindings.backend.history_state

    # Verify only 1 switch record (merged)
    assert await wait_until(
        lambda: (
            len(session_state.get("agent_profile_switches", [])) == 1
            and session_state.get("agent_profile_switches", [{}])[0].get("to") == "docs"
        )
    ), "merged profile switch record did not settle in time"
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 1, f"Expected 1 merged switch record, got {len(switches)}"
    assert switches[0]["from"] == "Code"
    assert switches[0]["to"] == "docs"
    assert switches[0]["from_display"] == "Code Agent"
    assert switches[0]["to_display"] == "Docs Agent"

    # Chat as Docs Agent — should work fine
    await bus.publish(UserMessage(text="Write docs"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2
    assert len(session_state.get("messages", [])) >= 6

    errors = _filter(events, Error)
    assert len(errors) == 0, f"Unexpected errors: {errors}"

    all_final = [e for e in _filter(events, InvocationMessage) if e.is_final]
    assert len(all_final) == 2, f"Expected 2 final messages, got {len(all_final)}"

    # Total messages: user + assistant (code) + turn_marker + user + assistant (docs) + turn_marker = 6
    # (no assistant marker message)
    messages = session_state.get("messages", [])
    assert len(messages) == 6

    # Verify the second user message has merged switch reminder (Code Agent -> Docs Agent)

    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 2
    assert _switched_to(user_msgs[1]) == "Docs Agent"


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_switch_chat_switch_switch_chat_produces_correct_markers(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Chat, switch, chat, switch-switch (consecutive), chat -> 2 markers total.

    Scenario: code -> (chat) -> test -> (chat) -> docs -> (no chat) -> code -> (chat)
    Expected: 2 markers: code->test (with chat after), test->code (merged test->docs->code)
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    # Chat as Code Agent
    await bus.publish(UserMessage(text="q1"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    # Switch to Explore Agent, then chat (this creates a real marker)
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    await bus.publish(UserMessage(text="q2"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2

    # Consecutive switches: test -> docs -> code (no chat in between)
    await bus.publish(AgentProfileSwitch(profile_name="docs"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)
    await bus.publish(AgentProfileSwitch(profile_name="Code"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 3)

    # Chat as Code Agent (back to original)
    await bus.publish(UserMessage(text="q3"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 3

    session_state = engine.current.loaded.bindings.backend.history_state
    messages = session_state.get("messages", [])

    # No assistant marker messages — switch info in user message reminders

    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 3  # q1, q2, q3
    # q1: no switch (started as Code)
    assert _switched_to(user_msgs[0]) is None
    # q2: switch Code -> Explore
    assert _switched_to(user_msgs[1]) == "Explore Agent"
    # q3: consecutive switch Explore -> docs -> Code (merged to Explore -> Code)
    assert _switched_to(user_msgs[2]) == "Code Agent"

    # Switches should also be 2
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 2
    assert switches[0]["from"] == "Code"
    assert switches[0]["to"] == "Explore"
    assert switches[1]["from"] == "Explore"
    assert switches[1]["to"] == "Code"

    all_final = [e for e in _filter(events, InvocationMessage) if e.is_final]
    assert len(all_final) == 3  # q1, q2, q3


# ---------------------------------------------------------------------------
# Test: consecutive A→B→A cancels out — no switch record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_consecutive_switch_back_to_origin_cancels_out(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Chat, then switch A→B→A without chatting -> cancels out entirely.

    Verifies:
    - No entry in agent_profile_switches (from == to, removed)
    - No system reminder is set (no net change)
    - Chatting after the cancelled switch works correctly
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate, Error]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="response")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    # Chat as Code Agent
    await bus.publish(UserMessage(text="hello"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    # Consecutive switches: Code → Explore → Code (back to origin)
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    await bus.publish(AgentProfileSwitch(profile_name="Code"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)

    session_state = engine.current.loaded.bindings.backend.history_state

    # Switch record should be empty — the round-trip cancels out
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 0, f"Expected 0 switch records (cancelled out), got {len(switches)}: {switches}"

    # Chat — no system reminder for switch should appear
    await bus.publish(UserMessage(text="still here"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2

    errors = _filter(events, Error)
    assert len(errors) == 0, f"Unexpected errors: {errors}"

    all_final = [e for e in _filter(events, InvocationMessage) if e.is_final]
    assert len(all_final) == 2

    # Neither user message should have a switch tag
    messages = session_state.get("messages", [])
    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 2
    assert _switched_to(user_msgs[0]) is None
    assert _switched_to(user_msgs[1]) is None


# ---------------------------------------------------------------------------
# Test: consecutive A→B→C→A cancels out — no switch record
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_consecutive_triple_switch_back_to_origin_cancels_out(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Chat, then switch A→B→C→A without chatting -> cancels out entirely.

    Verifies:
    - No entry in agent_profile_switches
    - No system reminder
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate, Error]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="response")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    await bus.publish(UserMessage(text="hello"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    # Consecutive: Code → Explore → Docs → Code
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    await bus.publish(AgentProfileSwitch(profile_name="docs"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)
    await bus.publish(AgentProfileSwitch(profile_name="Code"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 3)

    session_state = engine.current.loaded.bindings.backend.history_state
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 0, f"Expected 0 records (cancelled out), got {switches}"

    # Chat — no switch reminder
    await bus.publish(UserMessage(text="still here"))
    await _complete_turn(engine)
    assert sum(1 for m in session_state.get("messages", []) if m.role == "user") >= 2
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) == 2
    assert _filter(events, Error) == []

    messages = session_state.get("messages", [])
    user_msgs = [m for m in messages if m.role == "user"]
    assert _switched_to(user_msgs[1]) is None


# ---------------------------------------------------------------------------
# Test: A→B (messages) B→A produces two separate switch records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_switch_chat_switch_back_creates_separate_records(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Switch, chat, switch back -> two separate switch records (not merged).

    Scenario: Code → Explore → (chat) → Code
    Expected: 2 switch records: Code→Explore and Explore→Code
    """
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate, Error]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    engine = agent_engine(bus, settings=settings, agent_registry=registry)
    await engine.start(_CODE)
    assert await wait_until(lambda: len(_filter(events, SessionReady)) >= 1)

    # Chat as Code.
    await bus.publish(UserMessage(text="q1"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1

    # Switch to Explore, chat (consumes the switch)
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 1)
    await bus.publish(UserMessage(text="q2"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 2

    # Switch back to Code, chat
    await bus.publish(AgentProfileSwitch(profile_name="Code"))
    assert await wait_until(lambda: len(_filter(events, ProfileSwitched)) >= 2)
    await bus.publish(UserMessage(text="q3"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 3

    session_state = engine.current.loaded.bindings.backend.history_state
    assert await wait_until(lambda: len(session_state.get("agent_profile_switches", [])) == 2), (
        f"Expected 2 switch records, got {session_state.get('agent_profile_switches', [])}"
    )
    switches = session_state.get("agent_profile_switches", [])
    assert len(switches) == 2, f"Expected 2 switch records, got {switches}"
    assert switches[0]["from"] == "Code"
    assert switches[0]["to"] == "Explore"
    assert switches[1]["from"] == "Explore"
    assert switches[1]["to"] == "Code"

    # Verify both switch tags on user messages
    messages = session_state.get("messages", [])
    user_msgs = [m for m in messages if m.role == "user"]
    assert len(user_msgs) == 3
    assert _switched_to(user_msgs[0]) is None  # q1: no switch
    assert _switched_to(user_msgs[1]) == "Explore Agent"  # q2: Code → Explore
    assert _switched_to(user_msgs[2]) == "Code Agent"  # q3: Explore → Code


# ---------------------------------------------------------------------------
# Test: settings reload uses refreshed profile from registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_reload_uses_registry_refreshed_profile(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """SettingsReload should restart with the latest profile object from registry.

    This ensures saved profile edits (e.g. MCP enabled/disabled) are applied
    without requiring a manual profile switch.
    """
    import chrys.orchestration.engine.build.builder as builder_module

    bus = EventBus()
    settings = Settings()
    state_store = JsonFileStateStore(tmp_path)

    # Build two profile objects with the same name but different instructions.
    old_code = AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="OLD instructions",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    new_code = AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="NEW instructions",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )

    registry = AgentProfileRegistry()
    registry.register(old_code)

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="ok")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)

    engine = agent_engine(bus, settings=settings, agent_registry=registry, state_store=state_store)
    await engine.start(old_code)
    assert await wait_until(lambda: engine.agent_profile is not None)

    assert engine.agent_profile is not None
    assert engine.agent_profile.instructions == "OLD instructions"

    # Simulate profile file save by replacing the registry entry.
    registry.register(new_code)

    await bus.publish(SettingsReload())
    assert await wait_until(
        lambda: engine.agent_profile is not None and engine.agent_profile.instructions == "NEW instructions"
    ), "settings reload did not apply the refreshed profile in time"

    assert engine.agent_profile is not None
    assert engine.agent_profile.instructions == "NEW instructions"


# ---------------------------------------------------------------------------
# Test: restoring a session whose profile no longer exists keeps current
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_session_restore_missing_profile_keeps_current(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """When a saved session's profile is gone, the engine keeps its current profile."""
    import chrys.orchestration.engine.build.builder as builder_module

    events: list[object] = []
    bus = EventBus()
    for cls in [SessionReady, InvocationMessage, SessionRestored, Error, UsageUpdate, SessionSaved]:
        await bus.subscribe(cls, lambda e, _events=events: collect_events(_events, e))

    settings = Settings()
    state_store = JsonFileStateStore(tmp_path)
    registry = _make_registry()

    async def _mock_create_client(s=None, **kw):
        return MockChatClient(responses=[MockResponse(text="Mock response")])

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    # ---- Phase 1: Create a session with the "docs" profile ----
    engine = agent_engine(bus, settings=settings, agent_registry=registry, state_store=state_store)
    await engine.start(_DOCS)
    await wait_for(
        lambda: len(_filter(events, SessionReady)) >= 1,
        description="phase 1 session-ready event",
    )

    # Complete the turn and its durable save before restoring the session.
    await bus.publish(UserMessage(text="Write documentation"))
    await _complete_turn(engine)
    assert len([e for e in _filter(events, InvocationMessage) if e.is_final]) >= 1
    assert len(_filter(events, SessionSaved)) >= 1

    saved_session_id = engine.session.session_id
    await engine.shutdown()

    # ---- Phase 2: Restore with a registry that does NOT have "docs" ----
    registry2 = AgentProfileRegistry()
    registry2.register(_EXPLORE)
    registry2.register(_CODE)
    # Deliberately NOT registering _DOCS

    events2: list[object] = []
    bus2 = EventBus()
    for cls in [SessionReady, InvocationMessage, SessionRestored, Error, UsageUpdate, SessionSaved]:
        await bus2.subscribe(cls, lambda e, _events=events2: collect_events(_events, e))

    engine2 = agent_engine(bus2, settings=settings, agent_registry=registry2, state_store=state_store)
    await engine2.start(_CODE)
    await wait_for(
        lambda: len(_filter(events2, SessionReady)) >= 1,
        description="phase 2 session-ready event",
    )

    # Restore the session saved with "docs" profile
    await bus2.publish(SessionRestore(session_id=saved_session_id))
    await wait_for(
        lambda: len(_filter(events2, SessionRestored)) >= 1,
        description="session restore completion",
    )

    # The current Code profile wins even though Explore is first in the registry.
    restored_events = _filter(events2, SessionRestored)
    assert len(restored_events) == 1
    # Should be "Code" (current in engine2), NOT "docs" or first-available "Explore".
    assert restored_events[0].agent_profile == "Code"
    assert restored_events[0].display_name == "Code Agent"

    # Verify the engine is using the retained current profile.
    assert engine2.session.agent_profile is not None
    assert engine2.session.agent_profile.name == "Code"

    # Verify no errors
    errors = _filter(events2, Error)
    assert len(errors) == 0, f"Unexpected errors: {errors}"

    # Verify we can still chat after restore
    await bus2.publish(UserMessage(text="Hello after fallback"))
    await _complete_turn(engine2)
    assert len([e for e in _filter(events2, InvocationMessage) if e.is_final]) >= 1

    post_restore = [e for e in _filter(events2, InvocationMessage) if e.is_final]
    assert len(post_restore) >= 1
