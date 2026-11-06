# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the headless Chrys session host."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.session_host as session_host_module
import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import (
    AgentLoadFailed,
    AgentLoadFinished,
    AgentLoadStarted,
    AgentRuntimeUpdated,
    ApprovalRequest,
    AskUserResponse,
    Error,
    Event,
    InvocationAborted,
    InvocationMessage,
    InvocationPaused,
    InvocationToolCallResult,
    InvocationToolCallStart,
    QuestionToUser,
    TodoListUpdated,
    TurnStarted,
    UserMessage,
)
from chrys.foundation.i18n import DisplayBlock, DisplaySequence, Localizer, MessageRef
from chrys.foundation.models.ask_user import AskUserAnswer, AskUserQuestion
from chrys.foundation.models.todos import TodoItem
from chrys.orchestration.session_host import (
    AgentProfileNotFoundError,
    ChrysSessionHost,
    Errored,
)
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.state.store import JsonFileStateStore
from chrys.service.tools.builtins.ask_user import ask_user
from tests.orchestration._session_host_helpers import (
    _make_agent_registry,
    _make_agent_registry_many,
    _make_profile,
    _make_settings_and_models,
    _make_tool,
    _patch_runtime,
)
from tests.support.scripted_clients import ErrorMockChatClient, FrameworkBoom

# Restore-path tests can run two serial authority-bound phases, each with a
# 45s async timeout. The repository's 60s default could kill the worker during
# phase two before its own TimeoutError surfaces; 120s leaves 30s beyond the
# declared 90s path budget for diagnostics and teardown.
pytestmark = pytest.mark.timeout(120)


def _hand_out_sub_agent_client(monkeypatch: pytest.MonkeyPatch, client: MockChatClient) -> None:
    """The parent's delegation creates the child's client through the sub-agent module."""

    async def create_client(
        model_profile: ModelProfile,
        *,
        session_id: str,
        parent_session_id: str,
        use_route_session_context: bool,
        session_dir: Path,
        tool_result_ceiling_tokens: int | None,
    ) -> MockChatClient:
        return client

    monkeypatch.setattr(sub_agent_module, "create_client", create_client)


def _pending_event_stream_tasks() -> list[asyncio.Task[Any]]:
    current = asyncio.current_task()
    pending: list[asyncio.Task[Any]] = []
    for task in asyncio.all_tasks():
        if task is current or task.done():
            continue
        coro = task.get_coro()
        # This intentionally targets chrys.foundation.events.bus stream iterator tasks without depending on the class name.
        if "EventStream" in coro.__qualname__ and coro.__name__ == "__anext__":
            pending.append(task)
    return pending


def test_session_host_normalizes_relative_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    base = tmp_path / "base"
    project = base / "project"
    project.mkdir(parents=True)
    monkeypatch.chdir(base)
    settings, model_registry = _make_settings_and_models()

    host = ChrysSessionHost(
        profile_name="Headless",
        cwd="project",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    assert host.engine.workspace is not None
    assert Path(host.engine.workspace.primary_cwd) == project


@pytest.mark.asyncio
async def test_session_host_defers_observing_unfinished_cancelled_run_task(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[BaseException | None] = []

    def _record_exception(task: asyncio.Task[object]) -> None:
        if task.done() and not task.cancelled():
            observed.append(task.exception())

    monkeypatch.setattr(ChrysSessionHost, "_observe_task_exception", staticmethod(_record_exception))

    async def _fail_later() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("late failure")

    task = asyncio.create_task(_fail_later())

    ChrysSessionHost._observe_or_defer_task_exception(task)
    assert observed == []

    with pytest.raises(RuntimeError, match="late failure"):
        await task
    await asyncio.sleep(0)

    assert len(observed) == 1
    assert isinstance(observed[0], RuntimeError)
    assert str(observed[0]) == "late failure"


@pytest.mark.asyncio
async def test_session_host_defaults_to_bypass_approval(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("guarded_echo", "c1", {"message": "hi"})]),
            MockResponse(text="done"),
        ]
    )
    _patch_runtime(monkeypatch, [client], [_make_tool()])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile()),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("run the tool", timeout=45, keep_events=True)
    finally:
        await host.shutdown()

    assert host.approval_mode is ApprovalMode.BYPASS
    assert result.text == "done"
    assert any(isinstance(event, AgentLoadStarted) for event in result.events)
    assert any(isinstance(event, AgentLoadFinished) for event in result.events)
    assert not any(isinstance(event, UserMessage) for event in result.events)
    assert not any(isinstance(event, ApprovalRequest) for event in result.events)
    assert any(
        (isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn")
        and event.tool_name == "guarded_echo"
        and "echo: hi" in event.result
        for event in result.events
    )


@pytest.mark.asyncio
async def test_run_until_final_hands_every_streamed_event_to_on_event(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("guarded_echo", "c1", {"message": "hi"})]),
            MockResponse(text="done"),
        ]
    )
    _patch_runtime(monkeypatch, [client], [_make_tool()])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile()),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    seen: list[Event] = []

    try:
        result = await host.run_until_final("run the tool", timeout=45, keep_events=True, on_event=seen.append)
    finally:
        await host.shutdown()

    assert result.text == "done"
    assert seen == result.events
    assert any(isinstance(event, InvocationToolCallResult) for event in seen)


@pytest.mark.asyncio
async def test_session_host_auto_start_streams_load_before_final_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client = MockChatClient(responses=[MockResponse(text="done")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("prompt", timeout=45, keep_events=True)
    finally:
        await host.shutdown()

    ordered = [
        type(event)
        for event in result.events
        if isinstance(event, AgentLoadStarted | AgentLoadFinished | InvocationMessage)
    ]
    assert ordered == [AgentLoadStarted, AgentLoadFinished, InvocationMessage]


@pytest.mark.asyncio
async def test_session_host_cancel_between_turns_does_not_cancel_next_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client = MockChatClient(responses=[MockResponse(text="first"), MockResponse(text="second")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        first = await host.run_until_final("first prompt", timeout=45)
        await host.cancel_current_turn()
        second = await host.run_until_final("second prompt", timeout=45)
    finally:
        await host.shutdown()

    assert first.text == "first"
    assert second.text == "second"


def test_session_host_coerce_user_message_does_not_mutate_input(tmp_path) -> None:
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    original = UserMessage(text="hello", session_id="client-session")

    coerced = host._coerce_user_message(original)

    assert coerced is not original
    assert coerced.text == "hello"
    assert coerced.session_id is None
    assert original.session_id == "client-session"


@pytest.mark.asyncio
async def test_session_host_prestarted_stream_excludes_startup_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client = MockChatClient(responses=[MockResponse(text="done")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    events: list[Any] = []

    async def _collect() -> None:
        async for event in host.iter_run_events("prompt"):
            events.append(event)

    try:
        await host.start()
        await asyncio.wait_for(_collect(), timeout=45)
    finally:
        await host.shutdown()

    assert any(
        (isinstance(event, InvocationMessage) and event.origin.kind == "turn") and event.text == "done"
        for event in events
    )
    assert not any(isinstance(event, AgentLoadStarted | AgentLoadFinished) for event in events)


@pytest.mark.asyncio
async def test_session_host_streams_turn_started_during_turn(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # TurnStarted is published before the model request, and the ACP server turns
    # it into chrys/turn_started. The bridge only sees it if the headless run
    # stream forwards it, so guard against it being dropped from
    # _HEADLESS_RUN_EVENT_TYPES.
    client = MockChatClient(responses=[MockResponse(text="done")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    events: list[Any] = []

    async def _collect() -> None:
        async for event in host.iter_run_events("prompt"):
            events.append(event)

    try:
        await host.start()
        await asyncio.wait_for(_collect(), timeout=45)
    finally:
        await host.shutdown()

    started = [event for event in events if isinstance(event, TurnStarted)]
    assert len(started) == 1
    assert started[0].session_id == host.session_id
    assert started[0].turn_number == 1
    assert started[0].is_retry is False


@pytest.mark.asyncio
async def test_session_host_streams_runtime_update_during_turn(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # AgentRuntimeUpdated is published mid-turn by runtime-skill refreshes. The
    # ACP bridge only sees it if the headless run stream forwards it, so guard
    # against it being dropped from _HEADLESS_RUN_EVENT_TYPES.
    client = MockChatClient(responses=[MockResponse(text="done")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    events: list[Any] = []

    async def _collect() -> None:
        published = False
        async for event in host.iter_run_events("prompt"):
            events.append(event)
            if not published:
                # Enqueue on the first streamed event so it precedes the final
                # message in the FIFO stream and is reliably observed.
                published = True
                await host.event_bus.publish(AgentRuntimeUpdated(session_id=host.session_id, model_profile_id="m1"))

    try:
        await host.start()
        await asyncio.wait_for(_collect(), timeout=45)
    finally:
        await host.shutdown()

    assert any(isinstance(event, AgentRuntimeUpdated) and event.model_profile_id == "m1" for event in events)


@pytest.mark.asyncio
async def test_session_host_streams_todo_list_updated_during_turn(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # Both public stream entry points delegate to _iter_turn_events; this happy path covers both by construction.
    # todo_write publishes TodoListUpdated mid-turn with session_id stamped.
    # The stream test pins BOTH halves of that contract: the type is in
    # _HEADLESS_RUN_EVENT_TYPES, and a sessionless publish is dropped by
    # _event_belongs_to_session (so only a stamped event reaches ACP clients).
    client = MockChatClient(responses=[MockResponse(text="done")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    events: list[Any] = []

    async def _collect() -> None:
        published = False
        async for event in host.iter_run_events("prompt"):
            events.append(event)
            if not published:
                # Enqueue on the first streamed event so both precede the
                # final message in the FIFO stream and are reliably observed.
                published = True
                await host.event_bus.publish(
                    TodoListUpdated(items=[TodoItem(content="stamped")], session_id=host.session_id)
                )
                await host.event_bus.publish(TodoListUpdated(items=[TodoItem(content="sessionless")]))

    try:
        await host.start()
        await asyncio.wait_for(_collect(), timeout=45)
    finally:
        await host.shutdown()

    todo_events = [event for event in events if isinstance(event, TodoListUpdated)]
    assert [item.content for event in todo_events for item in event.items] == ["stamped"]


def test_headless_run_event_types_include_compaction_events() -> None:
    """Phase-4 compaction UX events must reach headless/ACP consumers.

    The ACP server only sees events forwarded by ``iter_turn_events``,
    which filters on this allow-list — guard against the compaction
    events being dropped from it (their sibling ``RetryAttempt`` /
    ``InvocationRetryAttempt`` events are forwarded the same way).
    """
    from chrys.foundation.events.types import (
        CompactionFinished,
        CompactionStarted,
        InvocationCompactionFinished,
        InvocationCompactionStarted,
        InvocationContextPressure,
    )
    from chrys.orchestration.session_host import _HEADLESS_RUN_EVENT_TYPES

    for event_type in (
        CompactionStarted,
        CompactionFinished,
        InvocationContextPressure,
        InvocationCompactionStarted,
        InvocationCompactionFinished,
    ):
        assert event_type in _HEADLESS_RUN_EVENT_TYPES


def test_headless_run_event_types_include_hosted_lifecycle_events() -> None:
    from chrys.foundation.events.types import (
        InvocationToolCallArgsUpdated,
        InvocationToolCallProgress,
        InvocationToolCallStatusUpdated,
    )
    from chrys.orchestration.session_host import _HEADLESS_RUN_EVENT_TYPES

    for event_type in (
        InvocationToolCallArgsUpdated,
        InvocationToolCallStatusUpdated,
        InvocationToolCallArgsUpdated,
        InvocationToolCallStatusUpdated,
        InvocationToolCallProgress,
    ):
        assert event_type in _HEADLESS_RUN_EVENT_TYPES


@pytest.mark.asyncio
async def test_session_host_streams_startup_failure_before_raising(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    async def _raise_create_client(_settings: Any = None, **_kwargs: Any) -> MockChatClient:
        msg = "client boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(builder_module, "create_client", _raise_create_client)
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    events: list[Any] = []

    try:
        with pytest.raises(RuntimeError, match="client boom"):
            async for event in host.iter_run_events("prompt"):
                events.append(event)
    finally:
        await host.shutdown()

    assert any(isinstance(event, AgentLoadStarted) for event in events)
    assert any(isinstance(event, AgentLoadFailed) and "client boom" in event.message for event in events)


@pytest.mark.asyncio
async def test_session_host_requires_existing_profile(tmp_path) -> None:
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Missing",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        with pytest.raises(AgentProfileNotFoundError, match="Agent profile not found: Missing") as exc_info:
            await host.start()
    finally:
        await host.shutdown()

    error = exc_info.value
    assert str(error) == "'Agent profile not found: Missing. Available profiles: Headless'"
    assert isinstance(error.display_message, MessageRef)
    assert error.display_message.definition is session_host_module._AGENT_PROFILE_NOT_FOUND_WITH_AVAILABLE
    assert dict(error.display_message.args) == {
        "name": "Missing",
        "available": DisplaySequence(("Headless",)),
    }


@pytest.mark.asyncio
async def test_session_host_reports_missing_profile_without_available_profiles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(AgentProfileRegistry, "load_all", lambda _self: 0)
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Missing",
        settings=settings,
        agent_registry=_make_agent_registry_many(),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        with pytest.raises(AgentProfileNotFoundError, match="Agent profile not found: Missing") as exc_info:
            await host.start()
    finally:
        await host.shutdown()

    error = exc_info.value
    assert str(error) == "'Agent profile not found: Missing'"
    assert isinstance(error.display_message, MessageRef)
    assert error.display_message.definition is session_host_module._AGENT_PROFILE_NOT_FOUND
    assert dict(error.display_message.args) == {"name": "Missing"}


@pytest.mark.asyncio
async def test_session_host_blank_session_id_starts_new_session(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(responses=[MockResponse(text="new response")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        session_id="  ",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("new prompt", timeout=45)
    finally:
        await host.shutdown()

    assert result.text == "new response"
    assert result.session_id


@pytest.mark.asyncio
async def test_session_host_hides_ask_user_from_noninteractive_model(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(responses=[MockResponse(text="all done")])
    _patch_runtime(monkeypatch, [client], [ask_user, _make_tool()])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("do this in the background", timeout=45)
        agent = host.engine.current.loaded.agent
        assert agent is not None
        model_tools = agent.default_options["tools"]
    finally:
        await host.shutdown()

    assert result.text == "all done"
    assert [tool.name for tool in model_tools] == ["guarded_echo"]
    assert list(host.engine.current.manifest.tool_names) == ["guarded_echo"]


@pytest.mark.asyncio
async def test_session_host_hides_ask_user_from_delegated_agent(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    parent_client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("Explore", "sub-1", {"prompt": "inspect the workspace"})]),
            MockResponse(text="delegation complete"),
        ]
    )
    sub_agent_client = MockChatClient(responses=[MockResponse(text="inspection complete")])
    _patch_runtime(monkeypatch, [parent_client], [ask_user, _make_tool()])
    monkeypatch.setattr(
        sub_agent_module,
        "create_client",
        create_autospec(sub_agent_module.create_client, return_value=sub_agent_client),
    )
    settings, model_registry = _make_settings_and_models()
    parent_profile = _make_profile(approval_default="skip")
    parent_profile.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Explore")])
    sub_agent_profile = AgentProfile(
        name="Explore",
        sub_agent_only=True,
        instructions="Inspect the workspace without asking the user questions.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="skip", overrides={}),
        compaction=CompactionConfig(enabled=False),
    )
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry_many(parent_profile, sub_agent_profile),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("delegate this task", timeout=45)
        sub_agent_tools = host.engine.current.loaded.sub_agent_tools
        assert sub_agent_tools is not None
        delegated_agent = sub_agent_tools._agents["Explore"]
        delegated_model_tools = delegated_agent.default_options["tools"]
        delegated_request_tools = sub_agent_client.call_history[0][1]["tools"]
    finally:
        await host.shutdown()

    assert result.text == "delegation complete"
    assert [tool.name for tool in delegated_model_tools] == ["guarded_echo"]
    delegated_request_tool_names = [tool.name for tool in delegated_request_tools]
    assert "guarded_echo" in delegated_request_tool_names
    assert "ask_user" not in delegated_request_tool_names


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["run_until_final", "iter_turn_events"])
async def test_a_delegated_agents_tool_calls_reach_the_headless_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path, entry: str
) -> None:
    """Child activity is headless-visible (only its prose stays private), so its local tool calls carry the session.

    ``iter_turn_events`` is the ACP server's entry, which forwards them as ``chrys/sub_agent_tool_call_*``.
    """
    parent_client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("Explore", "sub-1", {"prompt": "inspect the workspace"})]),
            MockResponse(text="delegation complete"),
        ]
    )
    sub_agent_client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("guarded_echo", "child-1", {"message": "hi"})]),
            MockResponse(text="inspection complete"),
        ]
    )
    _patch_runtime(monkeypatch, [parent_client], [_make_tool()])
    monkeypatch.setattr(
        sub_agent_module,
        "create_client",
        create_autospec(sub_agent_module.create_client, return_value=sub_agent_client),
    )
    settings, model_registry = _make_settings_and_models()
    parent_profile = _make_profile(approval_default="skip")
    parent_profile.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Explore")])
    sub_agent_profile = AgentProfile(
        name="Explore",
        sub_agent_only=True,
        instructions="Inspect the workspace.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="skip", overrides={}),
        compaction=CompactionConfig(enabled=False),
    )
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry_many(parent_profile, sub_agent_profile),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        if entry == "run_until_final":
            result = await host.run_until_final("delegate this task", timeout=45, keep_events=True)
            assert result.text == "delegation complete"
            events, session_id = result.events, result.session_id
        else:
            events = [event async for event in host.iter_turn_events("delegate this task")]
            session_id = host.session_id
    finally:
        await host.shutdown()

    child_calls = [
        event
        for event in events
        if isinstance(event, InvocationToolCallStart | InvocationToolCallResult) and event.origin.kind == "sub_agent"
    ]
    assert [type(event) for event in child_calls] == [InvocationToolCallStart, InvocationToolCallResult]
    assert {event.call_id for event in child_calls} == {child_calls[0].call_id}
    assert session_id
    assert all(event.session_id == session_id for event in child_calls)


@pytest.mark.asyncio
async def test_session_host_ends_a_failed_delegation_instead_of_pausing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A headless run shows no card to pause on, so a child's failure becomes the parent's tool result."""
    parent_client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("Explore", "sub-1", {"prompt": "inspect the workspace"})]),
            MockResponse(text="reported the failure"),
        ]
    )
    sub_agent_client = ErrorMockChatClient(outcomes=[FrameworkBoom("boom")])
    _patch_runtime(monkeypatch, [parent_client], [_make_tool()])
    _hand_out_sub_agent_client(monkeypatch, sub_agent_client)
    settings, model_registry = _make_settings_and_models()
    parent_profile = _make_profile(approval_default="skip")
    parent_profile.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Explore")])
    sub_agent_profile = AgentProfile(
        name="Explore",
        sub_agent_only=True,
        instructions="Inspect the workspace.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="skip", overrides={}),
        compaction=CompactionConfig(enabled=False),
    )
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry_many(parent_profile, sub_agent_profile),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("delegate this task", timeout=45, keep_events=True)
    finally:
        await host.shutdown()

    assert result.text == "reported the failure"
    assert sub_agent_client.call_count == 1
    assert not any(isinstance(event, InvocationPaused) for event in result.events)
    (ended,) = [event for event in result.events if isinstance(event, InvocationAborted)]
    assert "boom" in ended.last_error
    (delegated,) = [
        event for event in result.events if isinstance(event, InvocationToolCallResult) and event.origin.kind == "turn"
    ]
    assert delegated.result.startswith("Error: sub-agent 'Explore' failed — ")
    assert "boom" in delegated.result


@pytest.mark.parametrize(
    ("question", "detail"),
    [
        ("Should I proceed?", "1. Should I proceed?"),
        ("   ", "The agent requested user input."),
        ("Line one?\nLine two?", "1. Line one?\nLine two?"),
    ],
    ids=["question-text", "empty-question-fallback", "multiline-question-preserved"],
)
def test_session_host_noninteractive_question_remains_an_error_fallback(
    monkeypatch: pytest.MonkeyPatch,
    question: str,
    detail: str,
) -> None:
    monkeypatch.setenv("CHRYS_LOCALE", "en")
    host = ChrysSessionHost(profile_name="Headless")

    outcome = host._resolve_run_outcome(
        final=None,
        error=None,
        question=QuestionToUser(session_id="session-1", questions=(AskUserQuestion(question=question),)),
    )

    assert isinstance(outcome, Errored)
    expected_message = f"Agent requested user input in headless mode: {detail}"
    assert (outcome.error.code, outcome.error.message, outcome.error.session_id) == (
        "headless_interaction_required",
        expected_message,
        "session-1",
    )
    reference = outcome.error.display_message
    assert reference is not None
    assert reference.definition.key == "session_host.headless_interaction_required"
    assert dict(reference.args) == {"detail": DisplayBlock(detail)}
    assert Localizer(host._settings.locale).render(reference) == expected_message


def test_session_host_noninteractive_error_lists_every_question() -> None:
    host = ChrysSessionHost(profile_name="Headless")
    questions = (AskUserQuestion("First?"), AskUserQuestion("Second?"), AskUserQuestion("Third?"))

    outcome = host._resolve_run_outcome(
        final=None,
        error=None,
        question=QuestionToUser(session_id="session-1", questions=questions),
    )

    assert isinstance(outcome, Errored)
    assert outcome.error.message == ("Agent requested user input in headless mode: 1. First? 2. Second? 3. Third?")


def test_session_host_missing_final_response_has_localizable_error() -> None:
    host = ChrysSessionHost(profile_name="Headless")
    host.engine.session.session_id = "session-1"

    outcome = host._resolve_run_outcome(final=None, error=None, question=None)

    assert isinstance(outcome, Errored)
    error = outcome.error
    assert (error.code, error.message, error.session_id) == (
        "no_final_response",
        "Agent run ended without a final response.",
        "session-1",
    )
    assert error.display_message is not None
    assert error.display_message.definition.key == "session_host.no_final_response"
    assert dict(error.display_message.args) == {}


@pytest.mark.asyncio
async def test_session_host_interactive_ask_user_completes_turn(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[
                    (
                        "ask_user",
                        "q1",
                        {
                            "questions": [
                                {"question": "Should I proceed?", "options": [{"label": "yes"}, {"label": "no"}]}
                            ]
                        },
                    )
                ]
            ),
            MockResponse(text="all done"),
        ]
    )
    _patch_runtime(monkeypatch, [client], [ask_user])
    settings, model_registry = _make_settings_and_models()
    bus = EventBus()

    async def _answer_question(event: QuestionToUser) -> None:
        await bus.publish(
            AskUserResponse(
                request_id=event.request_id,
                answers=(AskUserAnswer(values=("yes",)),),
                session_id=event.session_id,
            )
        )

    await bus.subscribe(QuestionToUser, _answer_question)
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        event_bus=bus,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        allow_user_interaction=True,
    )

    try:
        result = await host.run_until_final("ask me something", timeout=45, keep_events=True)
    finally:
        await host.shutdown()

    assert result.text == "all done"
    assert any(isinstance(event, QuestionToUser) for event in result.events)
    outcome = host.last_turn_outcome
    assert type(outcome).__name__ == "EndTurn"


@pytest.mark.asyncio
async def test_session_host_timeout_interrupts_run(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(responses=[MockResponse(text="too late", delay=1)])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        with pytest.raises(TimeoutError):
            await host.run_until_final("slow prompt", timeout=0.05)
        assert not host.engine.is_running
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_session_host_iter_events_timeout_interrupts_run(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(responses=[MockResponse(text="too late", delay=1)])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    async def _consume_events() -> None:
        async for _event in host.iter_run_events("slow prompt"):
            pass

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_consume_events(), timeout=0.05)
        await asyncio.sleep(0)
        assert not host.engine.is_running
        assert _pending_event_stream_tasks() == []
    finally:
        await host.shutdown()


@pytest.mark.asyncio
async def test_session_host_ignores_sessionless_run_events(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    client = MockChatClient(responses=[MockResponse(text="done")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    bus = EventBus()

    async def _publish_sessionless_error(_event: UserMessage) -> None:
        await bus.publish(Error(code="global", message="global error"))

    await bus.subscribe(UserMessage, _publish_sessionless_error)
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        event_bus=bus,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )

    try:
        result = await host.run_until_final("prompt", timeout=45, keep_events=True)
    finally:
        await host.shutdown()

    assert result.text == "done"
    assert not any(isinstance(event, Error) and event.code == "global" for event in result.events)


@pytest.mark.asyncio
async def test_session_host_rejects_concurrent_turns(tmp_path) -> None:
    settings, model_registry = _make_settings_and_models()
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
    )
    await host._run_lock.acquire()
    try:
        events = host.iter_run_events("overlap")
        with pytest.raises(RuntimeError, match="Concurrent turns are not supported"):
            await events.__anext__()
    finally:
        host._run_lock.release()
        await host.shutdown()


@pytest.mark.asyncio
async def test_session_host_retries_the_failed_turn_with_the_note_in_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``iter_retry_events`` resumes the failed turn and keeps the note inside it."""
    client = ErrorMockChatClient([FrameworkBoom("boom"), MockResponse(text="recovered")])
    _patch_runtime(monkeypatch, [client], [])
    settings, model_registry = _make_settings_and_models()
    store = JsonFileStateStore(tmp_path / "sessions")
    host = ChrysSessionHost(
        profile_name="Headless",
        settings=settings,
        agent_registry=_make_agent_registry(_make_profile(approval_default="skip")),
        model_registry=model_registry,
        state_store=store,
    )
    first: list[Event] = []
    retried: list[Event] = []

    async def _drain(target: list[Event], stream) -> None:
        async for event in stream:
            target.append(event)

    try:
        await host.start()
        await _drain(first, host.iter_turn_events("do it"))
        assert host.engine.can_retry

        await _drain(retried, host.iter_retry_events("also check env vars"))
    finally:
        await host.shutdown()

    (opening,) = [event for event in first if isinstance(event, TurnStarted)]
    (resumed,) = [event for event in retried if isinstance(event, TurnStarted)]
    assert resumed.turn_id == opening.turn_id
    assert resumed.is_retry is True
    assert resumed.turn_number == opening.turn_number

    transcript = await store.load_session_transcript(host.session_id)
    assert transcript is not None
    # The note is guidance inside the retried turn, not a question of its own:
    # both user messages sit in one turn, whose id the retry kept.
    user_texts = [
        content["text"]
        for message in transcript.messages
        if message["role"] == "user"
        for content in message["contents"]
        if content["type"] == "text"
    ]
    assert [text.splitlines()[0] for text in user_texts] == ["do it", "also check env vars"]
    assert [record.turn_id for record in transcript.turns] == [opening.turn_id]
