# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Engine-driven tests — system-reminder injection into model-visible user messages.

``SystemReminderMiddleware`` enriches a shallow copy of the message list per
LLM call, so session-state user messages stay clean. These tests run a real
``AgentEngine`` against a scripted ``MockChatClient`` to pin the wiring around
the middleware:

- session-state cleanliness across turns, mid-turn injections, profile
  switches (``_switch_to`` tagging and merging), save/restore, and the
  empty-input resume after completed tool work;
- the model-visible view: skill-reference, profile-switch, usage, and
  injected-message reminders, on every call of a tool loop;
- the reminder record: earlier user messages render as they were sent on
  later turns and after restore, a retry re-sends a failed request's opener
  unchanged, and a ``compress_context`` fold moves the skill catalog to the
  current opener;
- every persisted message of every role is clean once a run ends.

Direct (engine-free) middleware tests live in ``test_system_reminder.py``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from tests.support.tool_packs import install_tools
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

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.settings import Settings
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentProfileSwitch,
    Error,
    Event,
    InvocationMessage,
    ProfileSwitched,
    SessionReady,
    SessionRestore,
    SessionRestored,
    SessionSaved,
    UsageUpdate,
    UserInject,
    UserInjectResult,
    UserMessage,
    UserRetry,
)
from chrys.foundation.models.history_markers import HistoryMarkerKind
from chrys.kernel import FunctionTool, Message
from chrys.orchestration.engine.engine import AgentEngine
from chrys.service.agent_middleware.reminders.turn_line import TurnLineSource
from chrys.service.agent_middleware.system_reminder import wrap_system_reminder as _wrap
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillConfig,
    SkillsConfig,
    ToolsConfig,
)
from chrys.service.state.store import JsonFileStateStore

# ---------------------------------------------------------------------------
# Helpers
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
    instructions="You are a code exploration assistant.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)

_DOCS = AgentProfile(
    name="docs",
    display_name="Docs Agent",
    instructions="You are a documentation writer.",
    tools=ToolsConfig(builtins=[]),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)


def _make_registry() -> AgentProfileRegistry:
    registry = AgentProfileRegistry()
    registry.register(_CODE)
    registry.register(_EXPLORE)
    registry.register(_DOCS)
    return registry


async def _collect(events: list, event: object) -> None:
    events.append(event)


def _filter(events: list, cls: type) -> list:
    return [e for e in events if isinstance(e, cls)]


def _final_agent_messages(events: list) -> list:
    return [e for e in _filter(events, InvocationMessage) if e.is_final]


def _user_messages(engine: AgentEngine) -> list:
    """Extract user messages from engine history state."""
    messages = engine.current.loaded.bindings.backend.history_state.get("messages", [])
    return [m for m in messages if m.role == "user"]


def _llm_saw_user_contents(client: MockChatClient, call_index: int = 0) -> list[str]:
    """Extract all text content from the user message the LLM saw on a given call.

    Returns a list of text strings from the user message's Content items.
    The LLM sees enriched messages (with <system-reminder> trailing Content
    items) during the call, even though session state is clean afterward.
    """
    if call_index >= len(client.call_history):
        return []
    messages, _opts = client.call_history[call_index]
    # Find the last user message in the conversation
    for msg in reversed(messages):
        if msg.role == "user":
            return [c.text for c in msg.contents if c.type == "text" and c.text]
    return []


def _all_user_msg_texts_in_call(client: MockChatClient, call_index: int = 0) -> list[list[str]]:
    """Return text contents of ALL user messages the LLM saw on a given call.

    Returns a list-of-lists: one inner list per user message, each containing
    the text of every Content item in that message.
    """
    if call_index >= len(client.call_history):
        return []
    messages, _opts = client.call_history[call_index]
    result = []
    for msg in messages:
        if msg.role == "user":
            texts = [c.text for c in msg.contents if c.type == "text" and c.text]
            result.append(texts)
    return result


def _assert_all_state_messages_clean(engine: AgentEngine) -> None:
    """Assert that NO message in session state contains <system-reminder> tags.

    Scans every message of every role — user, assistant, tool, system — to
    guarantee that the middleware's restoration left state fully clean.
    """
    messages = engine.current.loaded.bindings.backend.history_state.get("messages", [])
    for i, msg in enumerate(messages):
        for j, c in enumerate(msg.contents):
            text = c.text or ""
            assert "<system-reminder>" not in text, (
                f"State message[{i}] (role={msg.role}) content[{j}] contains <system-reminder>: {text[:80]!r}"
            )


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    *responses: MockResponse,
    client_type: type[MockChatClient] = MockChatClient,
) -> list[MockChatClient]:
    """Patch ``create_client`` to hand out fresh mock clients scripted with *responses*.

    The engine builds a new client per start and per profile switch; every one
    is appended to the returned list so tests can inspect its call history.
    """
    captured: list[MockChatClient] = []

    async def _mock_create_client(s=None, **kw):
        client = client_type(responses=list(responses))
        captured.append(client)
        return client

    monkeypatch.setattr(builder_module, "create_client", _mock_create_client)
    return captured


def _install_echo_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``echo`` the profile's only tool."""

    async def _echo(message: str) -> str:
        return f"echo: {message}"

    _install_tool(monkeypatch, FunctionTool(func=_echo, name="echo", description="Echo"))


@dataclass(frozen=True)
class _HeldTool:
    """A tool call that runs until the test releases it."""

    running: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


def _install_held_tool(monkeypatch: pytest.MonkeyPatch, name: str) -> _HeldTool:
    """Make *name* the profile's only tool; each call waits for ``release``."""
    held = _HeldTool()

    async def _hold(message: str) -> str:
        held.running.set()
        await held.release.wait()
        return f"{name}: {message}"

    _install_tool(monkeypatch, FunctionTool(func=_hold, name=name, description=name.title()))
    return held


async def _await_held_tool(engine: AgentEngine, held: _HeldTool) -> None:
    """Wait until the turn runs the held tool; a turn that ends first surfaces its own error."""
    run_task = engine.turns.turn_state.lease.run_task
    assert run_task is not None
    await wait_for(
        lambda: held.running.is_set() or run_task.done(),
        timeout=ENGINE_TURN_TIMEOUT,
        description="held tool running or turn over",
    )
    if not held.running.is_set():
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert held.running.is_set(), "the turn ended before the held tool ran"


def _install_tool(monkeypatch: pytest.MonkeyPatch, tool: FunctionTool) -> None:
    """Make *tool* the profile's only tool (signature-checked)."""
    install_tools(monkeypatch, [tool])


async def _subscribe_all(bus: EventBus, events: list[object], *classes: type[Event]) -> None:
    """Collect every published instance of *classes* into *events*."""
    for cls in classes:
        await bus.subscribe(cls, lambda e, _events=events: _collect(_events, e))


# ---------------------------------------------------------------------------
# Session-state cleanliness: switch tags, restore, multi-turn, empty-input resume
# ---------------------------------------------------------------------------


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_profile_switch_tagged_on_user_message(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """After switching profiles, the next user message should carry ``_switch_to``."""
    _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate)

    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
    )
    await engine.start(_CODE)

    # Chat as Code
    await bus.publish(UserMessage(text="msg1"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="pre-switch persisted user message",
    )
    # Drain the turn before switching, so msg1 is a completed turn rather
    # than a still-RUNNING one when the switch (and later msg2) land.
    await engine.wait_for_run_task()

    # Switch to Explore
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: bool(_filter(events, ProfileSwitched)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="profile-switch event",
    )

    # Chat as Explore — this message should carry the switch tag
    await bus.publish(UserMessage(text="msg2"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="post-switch persisted user message",
    )
    # The switch tag is applied after the executor run completes, while the
    # user message lands at run start — drain the turn before asserting.
    await engine.wait_for_run_task()

    user_msgs = _user_messages(engine)
    assert len(user_msgs) == 2

    # First message: no switch tag, clean text
    assert user_msgs[0].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) is None
    assert (user_msgs[0].text or "") == "msg1"

    # Second message: has switch tag, clean text
    assert user_msgs[1].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) == "Explore Agent"
    assert (user_msgs[1].text or "") == "msg2"


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_consecutive_switches_merge(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Multiple switches without a user message should merge into one."""
    _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate)

    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
    )
    await engine.start(_CODE)

    # Chat as Code
    await bus.publish(UserMessage(text="q1"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="persisted user message before consecutive switches",
    )
    # Drain q1's turn before the switches, so q1 is a completed turn (not a
    # still-RUNNING one absorbing the switch) on slow runners.
    await engine.wait_for_run_task()

    # Consecutive switches: Code -> Explore -> Docs (no chat in between)
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: len(_filter(events, ProfileSwitched)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="first consecutive profile-switch event",
    )
    await bus.publish(AgentProfileSwitch(profile_name="docs"))
    await wait_for(
        lambda: len(_filter(events, ProfileSwitched)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="second consecutive profile-switch event",
    )

    # Chat — should show merged switch: Code Agent -> Docs Agent
    await bus.publish(UserMessage(text="q2"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="persisted user message after consecutive switches",
    )
    # The switch tag is applied after the executor run completes, while the
    # user message lands at run start — drain the turn before asserting.
    await engine.wait_for_run_task()

    user_msgs = _user_messages(engine)
    assert len(user_msgs) == 2

    # Second message: tagged with final switch target
    assert user_msgs[1].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) == "Docs Agent"
    assert (user_msgs[1].text or "") == "q2"


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_switch_tag_consumed_once(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Switch tag should only appear on the first message after switching."""
    _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate)

    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
    )
    await engine.start(_CODE)

    # Switch
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: len(_filter(events, ProfileSwitched)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="profile-switch event before first message",
    )

    # First message after switch — has tag
    await bus.publish(UserMessage(text="msg1"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="first persisted user message after switch",
    )
    # Drain msg1's turn before msg2 so msg2 is a fresh turn, not an injection.
    await engine.wait_for_run_task()

    # Second message — no switch tag
    await bus.publish(UserMessage(text="msg2"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="second persisted user message after switch",
    )
    # The switch tag is applied after the executor run completes, while the
    # user message lands at run start — drain the turn before asserting.
    await engine.wait_for_run_task()

    user_msgs = _user_messages(engine)
    assert len(user_msgs) == 2

    assert user_msgs[0].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) == "Explore Agent"
    assert user_msgs[1].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) is None


async def test_clean_messages_survive_session_restore(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Clean user messages should survive save and restore round-trip."""
    _install_client(monkeypatch, MockResponse(text="reply"))
    # Phase 1: Create and save a session
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, SessionSaved)

    state_store = JsonFileStateStore(tmp_path)
    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
        state_store=state_store,
    )
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="hello world"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1 and bool(_final_agent_messages(events)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="persisted user message and final response before restore",
    )

    session_id = engine.session.session_id
    await engine.shutdown()

    # Phase 2: Restore on a new engine
    bus2 = EventBus()
    events2: list[object] = []
    await _subscribe_all(bus2, events2, SessionReady, SessionRestored, UsageUpdate)

    engine2 = agent_engine(
        bus2,
        settings=Settings(),
        agent_registry=_make_registry(),
        state_store=state_store,
    )
    await engine2.start(_CODE)

    await bus2.publish(SessionRestore(session_id=session_id))
    await wait_for(
        lambda: bool(_filter(events2, SessionRestored)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="restored clean-message session",
    )

    # Verify clean messages survived
    user_msgs = _user_messages(engine2)
    assert len(user_msgs) >= 1

    raw_text = user_msgs[0].text or ""
    assert "<system-reminder>" not in raw_text
    assert raw_text == "hello world"


async def test_multiple_turns_all_clean(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """All user messages across multiple turns should be clean in state."""
    _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="t1"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="first persisted message in multi-turn run",
    )
    # Drain t1 before t2 so t2 starts a fresh turn (not a mid-turn injection).
    await engine.wait_for_run_task()
    await bus.publish(UserMessage(text="t2"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="second persisted message in multi-turn run",
    )

    user_msgs = _user_messages(engine)
    assert len(user_msgs) == 2

    # Both should be clean
    assert (user_msgs[0].text or "") == "t1"
    assert (user_msgs[1].text or "") == "t2"
    assert "<system-reminder>" not in (user_msgs[0].text or "")
    assert "<system-reminder>" not in (user_msgs[1].text or "")


async def test_empty_input_resume_delivers_reminders_without_synthetic_user(
    tmp_path: Path,
    agent_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completed work resumes with ``[]`` while reminders attach to the real user."""
    from chrys.orchestration.invoker.contracts import RunRequest
    from chrys.orchestration.invoker.kernel import KernelConversation

    class _FailAfterToolClient(MockChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            if len(self.call_history) == 1:
                self._call_history.append((list(messages), dict(options)))
                raise RuntimeError("failed after completed tool work")
            return super()._inner_get_response(
                messages=messages,
                stream=stream,
                options=options,
                **kwargs,
            )

    captured_client = _install_client(
        monkeypatch,
        MockResponse(tool_calls=[("echo", "c1", {"message": "working"})]),
        MockResponse(text="resumed from history"),
        client_type=_FailAfterToolClient,
    )
    _install_echo_tool(monkeypatch)

    execute_inputs: list[list[Message]] = []
    original_execute = KernelConversation.run

    async def _capture_execute(self: KernelConversation, request: RunRequest):
        execute_inputs.append(list(request.messages))
        return await original_execute(self, request)

    monkeypatch.setattr(KernelConversation, "run", _capture_execute)

    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, Error)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="do work"))
    assert await wait_until(lambda: bool(_filter(events, Error)))
    await engine.wait_for_run_task()

    events.clear()
    await bus.publish(UserRetry())
    assert await wait_until(lambda: bool(_final_agent_messages(events)))
    await engine.wait_for_run_task()

    assert execute_inputs[-1] == []
    client = captured_client[-1]
    retry_wire_messages = client.call_history[2][0]
    retry_users = [message for message in retry_wire_messages if message.role == "user"]
    assert len(retry_users) == 1
    retry_user_texts = [content.text for content in retry_users[0].contents if content.type == "text"]
    assert retry_user_texts[0] == "do work"
    assert any(text and text.startswith("<system-reminder>") for text in retry_user_texts[1:])

    history_messages = engine.current.loaded.bindings.backend.history_state["messages"]
    all_messages = [*history_messages, *(message for call, _options in client.call_history for message in call)]
    assert all(not message.additional_properties.get(HistoryMarkerKind.CONTINUATION_KEY) for message in all_messages)
    assert all(message.role != "user" or (message.text or "").strip() != "continue" for message in all_messages)


# ===========================================================================
# LLM view: the reminders the model actually receives
# ===========================================================================


async def test_llm_receives_skill_reference_reminder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
):
    """A leading /skill token should queue a skill-reference system reminder."""
    fake_platform = type("P", (), {"config_dir": tmp_path / "chrys-config"})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: tmp_path / "agents-root")

    captured_client = _install_client(monkeypatch, MockResponse(text="hi"))

    profile = AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="You are a coding assistant.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(
            inline=[
                SkillConfig(
                    name="review",
                    description="Review code and identify issues",
                    instructions="Review carefully.",
                )
            ],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )

    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(profile)

    await bus.publish(UserMessage(text="/review focus on auth"))
    await wait_for(
        lambda: bool(captured_client) and captured_client[-1].call_count >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="model call with live runtime reminder",
    )

    client = captured_client[-1]
    user_contents = _llm_saw_user_contents(client, 0)
    instructions = str(client.call_history[0][1].get("instructions", ""))
    assert "<available_skills>" not in instructions
    reminders = [text for text in user_contents if text.startswith("<system-reminder>")]

    assert any("<available_skills>" in text for text in reminders)
    assert any("<name>review</name>" in text for text in reminders)
    assert any("[Skill Reference] User explicitly invoked a skill: review" in text for text in reminders)
    assert any('skill_name="review"' in text for text in reminders)
    assert user_contents[0] == "/review focus on auth"

    user_msgs = _user_messages(engine)
    assert len(user_msgs) == 1
    assert (user_msgs[0].text or "") == "/review focus on auth"
    assert "<system-reminder>" not in (user_msgs[0].text or "")


async def test_llm_receives_skill_reference_reminder_for_injection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine,
):
    """A mid-turn /skill injection should update active-turn reminders."""
    fake_platform = type("P", (), {"config_dir": tmp_path / "chrys-config"})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: tmp_path / "agents-root")

    captured_client = _install_client(
        monkeypatch,
        MockResponse(tool_calls=[("echo", "c1", {"message": "working"})]),
        MockResponse(text="done"),
    )
    held = _install_held_tool(monkeypatch, "echo")

    profile = AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="You are a coding assistant.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(
            inline=[
                SkillConfig(
                    name="review",
                    description="Review code and identify issues",
                    instructions="Review carefully.",
                )
            ],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )

    bus = EventBus()
    engine = agent_engine(bus, settings=Settings())
    await engine.start(profile)

    await bus.publish(UserMessage(text="do something"))
    try:
        await _await_held_tool(engine, held)
        assert captured_client[-1].call_count == 1
        # Admission finishes inside publish(): the injection is queued before call 2.
        await bus.publish(UserInject(text="/review focus on auth"))
    finally:
        held.release.set()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    client = captured_client[-1]
    assert client.call_count == 2

    all_user_msgs = _all_user_msg_texts_in_call(client, 1)
    flattened = [text for user_texts in all_user_msgs for text in user_texts]
    assert any("[Skill Reference] User explicitly invoked a skill: review" in text for text in flattened)
    assert any('skill_name="review"' in text for text in flattened)

    assert "/review focus on auth" in flattened

    messages = engine.current.loaded.bindings.backend.history_state.get("messages", [])
    injected = [m for m in messages if m.role == "user" and m.additional_properties.get("_injected", False)]
    assert any((m.text or "") == "/review focus on auth" for m in injected)
    for message in injected:
        assert "<system-reminder>" not in (message.text or "")
    # The history copy keeps what its wire copy carried, so later calls render it again.
    skill_injection = next(m for m in injected if (m.text or "") == "/review focus on auth")
    record = skill_injection.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY]
    assert any(
        entry["kind"] == "event" and "[Skill Reference] User explicitly invoked a skill: review" in entry["text"]
        for entry in record
    )


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_llm_receives_profile_switch_reminder(agent_engine, monkeypatch: pytest.MonkeyPatch):
    """After a real AgentProfileSwitch, the LLM sees the switch reminder.

    Kept engine-driven on purpose: it pins the wiring that AgentEngine
    forwards profile switches AND the current tool names into
    SystemReminderMiddleware. Seeding the middleware directly would still
    pass with that wiring broken.
    """
    captured_clients = _install_client(monkeypatch, MockResponse(text="ok"))
    _install_echo_tool(monkeypatch)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate)

    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry())
    await engine.start(_CODE)

    # Chat as Code; drain msg1's turn before switching so it is complete.
    await bus.publish(UserMessage(text="msg1"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="pre-switch persisted user message for provider refresh",
    )
    await engine.wait_for_run_task()

    # Switch to Explore (creates a new client)
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: bool(_filter(events, ProfileSwitched)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="profile-switch event for provider refresh",
    )

    # Chat as Explore; wait for the post-switch client to be invoked.
    await bus.publish(UserMessage(text="msg2"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2 and bool(captured_clients) and captured_clients[-1].call_count >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="post-switch persisted message and model call",
    )
    # call_count increments when the model call STARTS; the profile-switch
    # marker is applied to session state only after executor.run() returns.
    # Drain the turn before inspecting state.
    await engine.wait_for_run_task()

    # The post-switch client is the last one
    post_switch_client = captured_clients[-1]
    assert post_switch_client.call_count >= 1

    user_contents = _llm_saw_user_contents(post_switch_client, 0)
    reminder_texts = [t for t in user_contents if t.startswith("<system-reminder>")]

    # Should contain a switch reminder mentioning both profiles
    switch_reminders = [r for r in reminder_texts if "switched" in r.lower() or "profile" in r.lower()]
    assert len(switch_reminders) >= 1, f"Expected switch reminder, got reminders: {reminder_texts}"

    switch_text = switch_reminders[0]
    assert "Code Agent" in switch_text, f"Expected 'Code Agent' in switch reminder: {switch_text}"
    assert "Explore Agent" in switch_text, f"Expected 'Explore Agent' in switch reminder: {switch_text}"

    # The engine forwarded the post-switch tool names into the middleware.
    assert "Your currently available tools are:" in switch_text, (
        f"Expected the current tool list in switch reminder: {switch_text}"
    )
    assert "echo" in switch_text

    # User text is still first and clean.
    assert user_contents[0] == "msg2"

    # State is clean
    user_msgs = _user_messages(engine)
    assert (user_msgs[1].text or "") == "msg2"
    assert user_msgs[1].additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) == "Explore Agent"


async def test_tool_loop_all_calls_see_enriched_message(agent_engine, monkeypatch: pytest.MonkeyPatch):
    """During a real tool loop, every LLM call should see the <system-reminder> tags.

    Kept as a full-engine test on purpose: it pins the kernel↔ChatMiddleware
    contract that the middleware is re-invoked before EVERY model call of the
    loop — including calls whose context ends with trailing assistant/tool
    messages. Direct middleware invocations cannot detect the kernel skipping
    enrichment after the first call.
    """
    captured_client = _install_client(
        monkeypatch,
        # LLM call 1: tool call
        MockResponse(tool_calls=[("echo", "c1", {"message": "step1"})]),
        # LLM call 2: another tool call
        MockResponse(tool_calls=[("echo", "c2", {"message": "step2"})]),
        # LLM call 3: final text response
        MockResponse(text="all done"),
    )
    _install_echo_tool(monkeypatch)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="do multi-step work"))
    await wait_for(
        lambda: bool(captured_client) and captured_client[-1].call_count >= 3,
        timeout=ENGINE_TURN_TIMEOUT,
        description="three model calls in tool loop",
    )

    client = captured_client[-1]
    # Tool call → tool call → final response = 3 model calls
    assert client.call_count == 3, f"Expected 3 LLM calls, got {client.call_count}"

    # ALL three calls must see the user message with <system-reminder> tags,
    # even though calls 2 and 3 carry trailing assistant/tool messages.
    for i in range(3):
        user_contents = _llm_saw_user_contents(client, i)
        reminder_texts = [t for t in user_contents if t.startswith("<system-reminder>")]
        assert len(reminder_texts) >= 1, f"LLM call {i}: expected at least 1 reminder, got {len(reminder_texts)}"
        assert any("do multi-step work" in t for t in user_contents), (
            f"LLM call {i}: user text missing from: {user_contents}"
        )

    # After all calls, session state should be clean
    user_msgs = _user_messages(engine)
    assert len(user_msgs) >= 1
    assert (user_msgs[0].text or "") == "do multi-step work"


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_session_restore_preserves_usage_details(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """Usage details should survive session save/restore and be used in next turn's reminders."""
    captured_clients = _install_client(monkeypatch, MockResponse(text="reply"))
    # Phase 1: Create session and send a message (generates usage)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, SessionSaved)

    state_store = JsonFileStateStore(tmp_path)
    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
        state_store=state_store,
    )
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="initial"))
    await wait_for(
        lambda: engine.session.runtime_meta.last_usage_details is not None,
        timeout=ENGINE_TURN_TIMEOUT,
        description="persisted runtime usage details",
    )

    session_id = engine.session.session_id
    # Check that usage_details are set
    assert engine.session.runtime_meta.last_usage_details is not None

    await engine.shutdown()

    # Phase 2: Restore session on new engine
    bus2 = EventBus()
    events2: list[object] = []
    await _subscribe_all(bus2, events2, SessionReady, SessionRestored, UsageUpdate)

    engine2 = agent_engine(
        bus2,
        settings=Settings(),
        agent_registry=_make_registry(),
        state_store=state_store,
    )
    await engine2.start(_CODE)

    await bus2.publish(SessionRestore(session_id=session_id))
    await wait_for(
        lambda: isinstance(engine2.session.runtime_meta.last_usage_details, dict),
        timeout=ENGINE_TURN_TIMEOUT,
        description="restored runtime usage details",
    )

    # Verify usage details were restored
    assert isinstance(engine2.session.runtime_meta.last_usage_details, dict)

    # Phase 3: Send a new message — should see runtime reminder
    await bus2.publish(UserMessage(text="after restore"))
    await wait_for(
        lambda: captured_clients[-1].call_count >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="post-restore model call with runtime reminder",
    )
    # The reminder is stripped from the stored user message only after the
    # run ends, so the state assertion below needs the turn to be over.
    await engine2.wait_for_run_task()

    # The post-restore client should have received reminders
    post_restore_client = captured_clients[-1]
    assert post_restore_client.call_count >= 1

    user_contents = _llm_saw_user_contents(post_restore_client, post_restore_client.call_count - 1)
    reminder_texts = [t for t in user_contents if t.startswith("<system-reminder>")]

    # Should have at least runtime reminder
    assert len(reminder_texts) >= 1, f"Expected reminders after restore, got: {user_contents}"

    # User message in state should be clean
    user_msgs = _user_messages(engine2)
    last_msg = user_msgs[-1]
    assert (last_msg.text or "") == "after restore"


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_consecutive_switch_back_to_origin_no_reminder(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Consecutive A→B→A should NOT inject a switch reminder — no net change."""
    captured_clients = _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate)

    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
    )
    await engine.start(_CODE)

    # Chat as Code first
    await bus.publish(UserMessage(text="q1"))
    await wait_for(
        lambda: len(_final_agent_messages(events)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="first final agent message before round-trip switch",
    )

    # Consecutive round-trip: Code → Explore → Code
    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: len(_filter(events, ProfileSwitched)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="outbound round-trip profile-switch event",
    )
    await bus.publish(AgentProfileSwitch(profile_name="Code"))
    await wait_for(
        lambda: len(_filter(events, ProfileSwitched)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="returning round-trip profile-switch event",
    )

    # Chat — LLM should NOT see any switch reminder (back to origin)
    await bus.publish(UserMessage(text="q2"))
    await wait_for(
        lambda: len(_final_agent_messages(events)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="second final agent message after round-trip switch",
    )

    final_client = captured_clients[-1]
    assert final_client.call_count >= 1

    user_contents = _llm_saw_user_contents(final_client, 0)
    reminder_texts = [t for t in user_contents if t.startswith("<system-reminder>")]
    switch_reminders = [r for r in reminder_texts if "switched" in r.lower()]
    assert len(switch_reminders) == 0, f"Should be no switch reminder for round-trip, got: {switch_reminders}"


async def test_injected_message_repeats_no_reminder_and_opener_stays_byte_stable(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """A mid-turn injection repeats no reminder the turn already shows.

    InjectionMiddleware (ChatMiddleware) appends clean ``Message("user", [text])``
    inside the tool loop.  SystemReminderMiddleware is the next ChatMiddleware:
    the opener keeps the reminders it carried on call 1 byte-identically, and
    the injected message — now the last user message — gets only reminders
    the turn has not shown yet, none here.
    """
    captured_client = _install_client(
        monkeypatch,
        # LLM call 1: tool call (creates time for injection)
        MockResponse(tool_calls=[("echo", "c1", {"message": "working"})]),
        # LLM call 2: final text (after seeing injection)
        MockResponse(text="done"),
    )
    held = _install_held_tool(monkeypatch, "echo")
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, UserInjectResult)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="do something"))
    try:
        await _await_held_tool(engine, held)
        assert captured_client[-1].call_count == 1
        # Admission finishes inside publish(): the injection is queued before call 2.
        await bus.publish(UserInject(text="also check this"))
    finally:
        held.release.set()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    client = captured_client[-1]

    # LLM call 2 (after tool result + injection): check all user messages.
    assert client.call_count == 2
    first_call_users = _all_user_msg_texts_in_call(client, 0)
    all_user_msgs = _all_user_msg_texts_in_call(client, 1)
    assert len(all_user_msgs) >= 2

    original_user_texts = all_user_msgs[0]
    assert original_user_texts[0] == "do something"
    assert any(text.startswith("<system-reminder>") for text in original_user_texts[1:])
    assert original_user_texts == first_call_users[0]
    assert all_user_msgs[-1] == ["also check this"]

    # After run: ALL messages in state are clean, and the injected message is
    # persisted verbatim — no trailing reminders leak into session state.
    assert _filter(events, UserInjectResult)
    assert _final_agent_messages(events)
    _assert_all_state_messages_clean(engine)
    injected = [m for m in _user_messages(engine) if m.additional_properties.get("_injected", False)]
    assert injected
    for inj in injected:
        assert (inj.text or "") == "also check this"


# ===========================================================================
# The reminder record: earlier user messages render as they were sent
# ===========================================================================


def _review_skill_profile() -> AgentProfile:
    return AgentProfile(
        name="Code",
        display_name="Code Agent",
        instructions="You are a coding assistant.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(
            inline=[
                SkillConfig(
                    name="review",
                    description="Review code and identify issues",
                    instructions="Review carefully.",
                )
            ],
            auto_load_user_agents_skills=False,
            auto_load_cwd_agents_skills=False,
        ),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )


def _user_texts_by_opener(
    client: MockChatClient, call_index: int, *, visible_only: bool = False
) -> dict[str, list[str]]:
    """Map each user message's first text to every text it carried on *call_index*."""
    messages, _opts = client.call_history[call_index]
    result: dict[str, list[str]] = {}
    for message in messages:
        if message.role != "user":
            continue
        if visible_only and message.additional_properties.get("_excluded", False):
            continue
        texts = [c.text for c in message.contents if c.type == "text" and c.text]
        result[texts[0]] = texts
    return result


def _catalog_carriers(texts_by_opener: dict[str, list[str]]) -> list[str]:
    return [opener for opener, texts in texts_by_opener.items() for text in texts[1:] if "<available_skills>" in text]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_earlier_openers_reach_later_turns_byte_identical_and_survive_restore(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Each opener keeps the reminders it was sent with, on every later turn and after restore."""
    captured_client = _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)
    state_store = JsonFileStateStore(tmp_path)
    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry(), state_store=state_store)
    await engine.start(_CODE)

    for text in ("first", "second"):
        await bus.publish(UserMessage(text=text))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    client = captured_client[-1]
    first_call = _user_texts_by_opener(client, 0)
    second_call = _user_texts_by_opener(client, 1)
    assert any(text.startswith("<system-reminder>") for text in first_call["first"][1:])
    assert second_call["first"] == first_call["first"]
    assert any(text.startswith("<system-reminder>") for text in second_call["second"][1:])
    # The runtime environment is still in view on the first opener; the
    # second carries only its own turn line.
    assert sum("[Runtime Environment]" in text for text in first_call["first"]) == 1
    assert not any("[Runtime Environment]" in text for text in second_call["second"])
    assert sum("[Turn Start]" in text for text in second_call["second"]) == 1

    history_openers = _user_messages(engine)
    assert [m.text for m in history_openers] == ["first", "second"]
    for opener in history_openers:
        record = opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY]
        assert record[0]["kind"] == "turn"
    _assert_all_state_messages_clean(engine)

    session_id = engine.session.session_id
    await engine.shutdown()

    bus2 = EventBus()
    events2: list[object] = []
    await _subscribe_all(bus2, events2, SessionReady, SessionRestored, UsageUpdate)
    engine2 = agent_engine(bus2, settings=Settings(), agent_registry=_make_registry(), state_store=state_store)
    await engine2.start(_CODE)
    await bus2.publish(SessionRestore(session_id=session_id))
    await wait_for(
        lambda: bool(_filter(events2, SessionRestored)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="restored session with reminder records",
    )

    await bus2.publish(UserMessage(text="third"))
    await await_run_task_chain(engine2, turn_state=engine2.turns.turn_state)

    restored_call = _user_texts_by_opener(captured_client[-1], 0)
    assert restored_call["first"] == first_call["first"]
    assert restored_call["second"] == second_call["second"]
    assert any(text.startswith("<system-reminder>") for text in restored_call["third"][1:])


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_retry_after_a_failed_request_resends_the_opener_byte_identical(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Retrying a turn whose only request failed re-sends its opener exactly as the failed request did."""

    class _FailFirstCallClient(MockChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            if not self.call_history:
                self._call_history.append((list(messages), dict(options)))
                raise RuntimeError("failed on the first request")
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

    captured_client = _install_client(monkeypatch, MockResponse(text="ok"), client_type=_FailFirstCallClient)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, Error)
    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="first"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert _filter(events, Error)

    await bus.publish(UserRetry())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    client = captured_client[-1]
    failed_call = _user_texts_by_opener(client, 0)
    retried_call = _user_texts_by_opener(client, 1)
    assert any(text.startswith("<system-reminder>") for text in failed_call["first"][1:])
    assert retried_call == failed_call
    [opener] = _user_messages(engine)
    record = opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY]
    assert [_wrap(item["text"]) for item in record] == failed_call["first"][1:]
    _assert_all_state_messages_clean(engine)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_opener_of_a_turn_that_failed_after_work_reaches_the_next_turn_byte_identical(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """The failure fallback rebuilds the opener with the reminders its request carried."""

    class _FailAfterToolClient(MockChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            if len(self.call_history) == 1:
                self._call_history.append((list(messages), dict(options)))
                raise RuntimeError("failed after completed tool work")
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

    captured_client = _install_client(
        monkeypatch,
        MockResponse(tool_calls=[("echo", "c1", {"message": "working"})]),
        MockResponse(text="second answer"),
        client_type=_FailAfterToolClient,
    )
    _install_echo_tool(monkeypatch)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, Error)
    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="first"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert _filter(events, Error)

    await bus.publish(UserMessage(text="second"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    client = captured_client[-1]
    first_call = _user_texts_by_opener(client, 0)
    next_turn_call = _user_texts_by_opener(client, 2)
    assert any(text.startswith("<system-reminder>") for text in first_call["first"][1:])
    assert next_turn_call["first"] == first_call["first"]
    _assert_all_state_messages_clean(engine)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_crash_recovery_snapshot_keeps_the_reminders_the_opener_was_sent_with(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """A mid-turn recovery snapshot holds the opener with the reminders its request carried."""
    captured_client = _install_client(
        monkeypatch,
        MockResponse(tool_calls=[("hold", "c1", {"message": "working"})]),
        MockResponse(text="done"),
    )
    held = _install_held_tool(monkeypatch, "hold")
    bus = EventBus()
    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="first"))
    try:
        await _await_held_tool(engine, held)
        snapshot = engine.writer.build_recovery_snapshot()
    finally:
        held.release.set()
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    assert snapshot is not None
    [opener] = [m for m in snapshot["messages"] if m.role == "user"]
    record = opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY]
    sent = _user_texts_by_opener(captured_client[-1], 0)["first"][1:]
    assert any(text.startswith("<system-reminder>") for text in sent)
    assert [_wrap(item["text"]) for item in record] == sent


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_profile_switch_before_a_retry_reaches_the_model(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """Switching profiles after a failed turn tells the retried request, although the opener's turn group is kept."""
    failures = [RuntimeError("failed on the first request")]

    class _FailOnceClient(MockChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            if failures:
                self._call_history.append((list(messages), dict(options)))
                raise failures.pop()
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

    captured_client = _install_client(monkeypatch, MockResponse(text="ok"), client_type=_FailOnceClient)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate, Error)
    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry())
    await engine.start(_CODE)
    await bus.publish(UserMessage(text="first"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert _filter(events, Error)
    failed_call = _user_texts_by_opener(captured_client[-1], 0)

    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: bool(_filter(events, ProfileSwitched)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="profile switch after the failed turn",
    )
    await bus.publish(UserRetry())
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    retried = _user_texts_by_opener(captured_client[-1], 0)["first"]
    assert retried[: len(failed_call["first"])] == failed_call["first"]
    notices = [text for text in retried if "[Agent profile switched from 'Code Agent' to 'Explore Agent']" in text]
    assert len(notices) == 1
    [opener] = _user_messages(engine)
    assert opener.additional_properties.get(HistoryMarkerKind.PROFILE_SWITCH_TO_KEY) == "Explore Agent"
    record = opener.additional_properties[HistoryMarkerKind.SYSTEM_REMINDERS_KEY]
    assert [_wrap(item["text"]) for item in record] == retried[1:]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_retry_after_restore_replays_the_failed_opener_as_it_was_sent(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """A restarted process retries the saved failed turn with the reminders the opener carried, not new ones."""
    hints = iter(range(1, 100))
    monkeypatch.setattr(TurnLineSource, "clock", staticmethod(lambda: f"clock {next(hints)}"))
    failures = [RuntimeError("failed on the first request")]

    class _FailOnceClient(MockChatClient):
        def _inner_get_response(self, *, messages, stream, options, **kwargs):
            if failures:
                self._call_history.append((list(messages), dict(options)))
                raise failures.pop()
            return super()._inner_get_response(messages=messages, stream=stream, options=options, **kwargs)

    captured_client = _install_client(monkeypatch, MockResponse(text="ok"), client_type=_FailOnceClient)
    state_store = JsonFileStateStore(tmp_path)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate, Error)
    engine = agent_engine(bus, settings=Settings(), agent_registry=_make_registry(), state_store=state_store)
    await engine.start(_CODE)
    await bus.publish(UserMessage(text="first"))
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    assert _filter(events, Error)
    failed_call = _user_texts_by_opener(captured_client[-1], 0)
    session_id = engine.session.session_id
    await engine.shutdown()

    bus2 = EventBus()
    events2: list[object] = []
    await _subscribe_all(bus2, events2, SessionReady, SessionRestored, InvocationMessage, UsageUpdate, Error)
    engine2 = agent_engine(bus2, settings=Settings(), agent_registry=_make_registry(), state_store=state_store)
    await engine2.start(_CODE)
    await bus2.publish(SessionRestore(session_id=session_id))
    await wait_for(
        lambda: bool(_filter(events2, SessionRestored)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="restored the failed session",
    )
    await bus2.publish(UserRetry())
    await await_run_task_chain(engine2, turn_state=engine2.turns.turn_state)

    assert not _filter(events2, Error)
    retried_call = _user_texts_by_opener(captured_client[-1], 0)
    assert _wrap("clock 1") in failed_call["first"]
    assert retried_call["first"] == failed_call["first"]


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_compress_context_fold_resends_the_skill_catalog(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """The catalog rides its first carrier until a fold takes it out of view, then moves once."""
    fake_platform = type("P", (), {"config_dir": tmp_path / "chrys-config"})()
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    monkeypatch.setattr("chrys.service.skills.adapter.user_agents_dir", lambda: tmp_path / "agents-root")
    captured_client = _install_client(
        monkeypatch,
        MockResponse(text="one"),
        MockResponse(text="two"),
        MockResponse(tool_calls=[("compress_context", "cc1", {"marker_id": "turn_2", "summary": "Turns one and two"})]),
        MockResponse(text="compressed"),
        MockResponse(text="four"),
    )
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)
    engine = agent_engine(bus, settings=Settings())
    await engine.start(_review_skill_profile())

    for text in ("t1", "t2", "t3", "t4"):
        await bus.publish(UserMessage(text=text))
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)

    client = captured_client[-1]
    assert client.call_count == 5
    # Before the fold, turns 2 and 3 found the catalog in view on t1.
    assert _catalog_carriers(_user_texts_by_opener(client, 1)) == ["t1"]
    assert _catalog_carriers(_user_texts_by_opener(client, 2)) == ["t1"]
    # The fold excluded t1 on the call that followed it; t3 carries the catalog from then on.
    after_fold = _user_texts_by_opener(client, 3, visible_only=True)
    assert "t1" not in after_fold
    assert _catalog_carriers(after_fold) == ["t3"]
    next_turn = _user_texts_by_opener(client, 4, visible_only=True)
    assert _catalog_carriers(next_turn) == ["t3"]
    assert next_turn["t3"] == after_fold["t3"]
    _assert_all_state_messages_clean(engine)


# ===========================================================================
# Every state message of every role is clean after a run
# ===========================================================================


async def test_all_state_messages_clean_after_simple_run(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """After a simple single-turn run, every message in state is clean and the user text is stored verbatim."""
    _install_client(monkeypatch, MockResponse(text="response"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="hello"))
    await wait_for(
        lambda: bool(_final_agent_messages(events)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="final agent message for simple clean-state run",
    )
    await engine.wait_for_run_task()

    _assert_all_state_messages_clean(engine)
    user_msgs = _user_messages(engine)
    assert len(user_msgs) == 1
    assert (user_msgs[0].text or "") == "hello"


async def test_all_state_messages_clean_after_tool_loop(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """After a multi-step tool loop, every message in state should be clean."""
    _install_client(
        monkeypatch,
        MockResponse(tool_calls=[("echo", "c1", {"message": "step1"})]),
        MockResponse(tool_calls=[("echo", "c2", {"message": "step2"})]),
        MockResponse(text="all done"),
    )
    _install_echo_tool(monkeypatch)
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="multi-step"))
    await wait_for(
        lambda: bool(_final_agent_messages(events)),
        timeout=ENGINE_TURN_TIMEOUT,
        description="final agent message for tool-loop clean-state run",
    )

    # Every message in state must be clean — user, assistant (tool calls),
    # tool (results), and final assistant response
    _assert_all_state_messages_clean(engine)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_all_state_messages_clean_after_multi_turn(tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch):
    """After multiple turns, every message in state should be clean."""
    _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, UsageUpdate)

    engine = agent_engine(bus, settings=Settings())
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="turn1"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="first clean-state turn user message",
    )
    # Drain each turn before the next publish so every UserMessage starts a
    # fresh turn rather than being injected into a still-RUNNING one.
    await engine.wait_for_run_task()
    await bus.publish(UserMessage(text="turn2"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="second clean-state turn user message",
    )
    await engine.wait_for_run_task()
    await bus.publish(UserMessage(text="turn3"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 3,
        timeout=ENGINE_TURN_TIMEOUT,
        description="third clean-state turn user message",
    )

    _assert_all_state_messages_clean(engine)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_all_state_messages_clean_after_profile_switch(
    tmp_path: Path, agent_engine, monkeypatch: pytest.MonkeyPatch
):
    """After a profile switch + message, every message in state should be clean."""
    _install_client(monkeypatch, MockResponse(text="ok"))
    bus = EventBus()
    events: list[object] = []
    await _subscribe_all(bus, events, SessionReady, InvocationMessage, ProfileSwitched, UsageUpdate)

    engine = agent_engine(
        bus,
        settings=Settings(),
        agent_registry=_make_registry(),
    )
    await engine.start(_CODE)

    await bus.publish(UserMessage(text="before switch"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="pre-switch clean-state user message",
    )
    # Drain the pre-switch turn so it completes before the switch lands.
    await engine.wait_for_run_task()

    await bus.publish(AgentProfileSwitch(profile_name="Explore"))
    await wait_for(
        lambda: len(_filter(events, ProfileSwitched)) >= 1,
        timeout=ENGINE_TURN_TIMEOUT,
        description="clean-state profile-switch event",
    )

    await bus.publish(UserMessage(text="after switch"))
    await wait_for(
        lambda: len(_user_messages(engine)) >= 2,
        timeout=ENGINE_TURN_TIMEOUT,
        description="post-switch clean-state user message",
    )

    # All messages clean — including the one that carried a switch reminder
    _assert_all_state_messages_clean(engine)
