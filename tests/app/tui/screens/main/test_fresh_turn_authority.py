# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The backend's cleanup and turn index own live transcript boundaries."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.service.agent_middleware.events.tool_events as tool_events_module
from chrys.app.tui.screens.main.screen import MainScreen
from chrys.app.tui.widgets.chat.messages import MessageRollbackButton, ThinkingMessage, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, CompactionConfig, ToolsConfig
from chrys.service.state.store import JsonFileStateStore
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.tui_helpers import ChatPanelApp, click_when_settled
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for


@pytest.mark.parametrize("retired", [False, True])
async def test_live_opener_waits_for_backend_cleanup_and_authoritative_index(retired: bool) -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        panel.set_rollback_targets({6, 7})
        await panel.add_user_message("Previous")
        panel.apply_turn_started(7)
        await panel.add_thinking_delta("thinking", "reasoning", "Only thinking")
        panel.finish_thinking_segment("thinking")
        await panel.add_interrupted()
        await panel.add_user_message("New question")
        previous, newest = panel.query(UserMessage)
        assert previous.turn_number == 7
        assert newest.turn_number is None
        assert not newest.query_one(MessageRollbackButton).display
        assert [item.turn_index for item in panel.toc_items] == [7, None]
        assert list(panel.query(ThinkingMessage))

        await panel.apply_fresh_turn_cleanup(retired_previous_turn=retired, removed_followups=0)
        panel.apply_turn_started(7 if retired else 8)

        assert [message.text for message in panel.query(UserMessage)] == (
            ["New question"] if retired else ["Previous", "New question"]
        )
        assert [item.turn_index for item in panel.toc_items] == ([7] if retired else [7, 8])
        assert bool(list(panel.query(ThinkingMessage))) is not retired
        assert newest.query_one(MessageRollbackButton).display


async def test_cleanup_removes_exact_backend_followup_count_without_judging_visible_work() -> None:
    async with ChatPanelApp(thinking_expanded=lambda: True).run_test() as pilot:
        panel = pilot.app.query_one(ChatPanel)
        await panel.add_user_message("Original")
        panel.apply_turn_started(4)
        await panel.add_user_message("Kept note", is_injection=True)
        await panel.add_thinking_delta("kept", "reasoning", "Kept thinking")
        panel.finish_thinking_segment("kept")
        await panel.add_user_message("Removed note", is_injection=True)
        await panel.add_thinking_delta("removed", "reasoning", "Removed thinking")
        panel.finish_thinking_segment("removed")
        await panel.add_interrupted()
        await panel.add_user_message("New")

        await panel.apply_fresh_turn_cleanup(retired_previous_turn=False, removed_followups=1)
        panel.apply_turn_started(5)

        assert [message.text for message in panel.query(UserMessage)] == ["Original", "Kept note", "New"]
        assert [message.text for message in panel.query(ThinkingMessage)] == ["Kept thinking"]
        assert [item.turn_index for item in panel.toc_items] == [4, 5]
        assert len(panel.toc_items[0].children) == 1


async def test_hook_interruption_keeps_invisible_tool_turn_through_message_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine
) -> None:
    settings, models = make_mock_settings_and_registry(stream=False)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    profile = AgentProfile(
        name="Editor",
        instructions="Edit files",
        tools=ToolsConfig(builtins=["filesystem.write"]),
        compaction=CompactionConfig(enabled=False),
    )
    registry = AgentProfileRegistry()
    registry.register(profile)
    target = tmp_path / "untouched.txt"
    target.write_text("before", encoding="utf-8")
    client = MockChatClient(
        [
            MockResponse(
                tool_calls=[("write_file", "edit", {"path": str(target), "content": "after", "overwrite": True})]
            ),
            MockResponse(text="New answer"),
        ]
    )
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    held, release = asyncio.Event(), asyncio.Event()

    async def hold_hook(
        *,
        manager,
        context,
        session_id,
        profile_name,
        tool_name,
        kind,
        call_id,
        args,
        workspace_cwd,
        target_operation_id=None,
        origin=None,
        extension_hooks=None,
    ) -> bool:
        held.set()
        await release.wait()
        return False

    monkeypatch.setattr(
        tool_events_module,
        "apply_before_tool_hooks",
        create_autospec(tool_events_module.apply_before_tool_hooks, side_effect=hold_hook),
    )
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )
    bus = EventBus()
    store = JsonFileStateStore(tmp_path / "sessions")
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=registry,
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
        surface=SessionSurface.TUI,
        initial_approval_mode=ApprovalMode.BYPASS,
    )
    engine.pin_model_profile()
    app = make_chrys_app(
        tmp_path / "sessions",
        settings=engine.settings_handle.settings,
        settings_handle=engine.settings_handle,
        engine=engine,
        event_bus=bus,
        agent_registry=registry,
        model_registry=models,
        profile_name="Editor",
    )
    async with app.run_test(size=(100, 32)) as pilot:
        await wait_for(
            lambda: (
                app._main_screen is not None
                and bool(app._main_screen.chat_session_id)
                and engine.current.loaded is not None
                and app.screen is app._main_screen
                and not app._main_screen.query_one(InputBar).agent_loading
            ),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="engine and composer ready",
        )
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        main._submit_user_text("Interrupted tool input")
        try:
            await wait_for(held.is_set, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="hook before tool card")
            assert not list(panel.query(ToolGroup))
            await main._input_flow.publish_interrupt()
            await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        finally:
            release.set()
        assert engine.current.require_loaded().bindings.backend.history_state["turn_counter"] == 1
        main._submit_user_text("New question")
        await wait_for(
            lambda: client.call_count == 2, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="new model call"
        )
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await wait_for(lambda: len(list(panel.query(UserMessage))) == 2, pilot=pilot, description="both openers stay")
        previous, newest = panel.query(UserMessage)
        assert [previous.turn_number, newest.turn_number] == [1, 2]
        assert [item.turn_index for item in panel.toc_items] == [1, 2]
        await wait_for(
            lambda: newest.query_one(MessageRollbackButton).display, pilot=pilot, description="new rollback link"
        )
        await click_when_settled(pilot, newest.query_one(MessageRollbackButton))
        await wait_for(
            lambda: (
                [message.text for message in panel.query(UserMessage)] == ["Interrupted tool input"]
                and main.query_one(InputBar).value == "New question"
            ),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="rollback removes only the new turn",
        )
        assert engine.current_turn_number == 1
        assert target.read_text(encoding="utf-8") == "before"
        assert main.query_one(InputBar).value == "New question"


async def test_fast_cleanup_and_turn_started_wait_for_the_new_opener_to_mount(tmp_path: Path) -> None:
    from chrys.foundation.events.types import FreshTurnCleaned, TurnStarted

    bus = EventBus()
    app = make_chrys_app(tmp_path, event_bus=bus)
    async with app.run_test(size=(80, 28)):
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        await panel.add_user_message("Empty interrupted input")
        panel.apply_turn_started(6)
        await panel.add_interrupted()
        main._state.render_gate.begin()
        await bus.publish(FreshTurnCleaned(retired_previous_turn=True), raise_handler_errors=True)
        await bus.publish(TurnStarted(turn_number=6), raise_handler_errors=True)
        assert [message.text for message in panel.query(UserMessage)] == ["Empty interrupted input"]

        await panel.add_user_message("New input")
        main._state.render_gate.finish(rendered=True)
        await main._input_flow.flush_deferred_agent_messages()

        assert [message.text for message in panel.query(UserMessage)] == ["New input"]
        assert panel.query_one(UserMessage).turn_number == 6
        assert [item.turn_index for item in panel.toc_items] == [6]


@pytest.mark.parametrize("deferred", [False, True])
@pytest.mark.parametrize("scenario", ["matching", "foreign", "previous-run", "retry", "session-changed", "run-changed"])
async def test_turn_facts_share_filters_before_and_after_render_gate_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, deferred: bool, scenario: str
) -> None:
    from chrys.foundation.events.types import FreshTurnCleaned, TurnStarted

    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )
    bus = EventBus()
    app = make_chrys_app(tmp_path, event_bus=bus)
    async with app.run_test(size=(90, 30)):
        main = app.screen
        assert isinstance(main, MainScreen)
        panel = main.query_one(ChatPanel)
        panel.set_session_id("current")
        await panel.add_user_message("Previous")
        panel.apply_turn_started(6)
        await panel.add_interrupted()
        main._state.run.agent_running = True
        main._state.run.started_at = datetime.now(tz=UTC)
        timestamp = main._state.run.started_at + timedelta(seconds=1)
        if scenario == "previous-run":
            timestamp = main._state.run.started_at - timedelta(seconds=1)
        session_id = "other" if scenario == "foreign" else "current"
        if deferred:
            main._state.render_gate.begin()
        else:
            await panel.add_user_message("New")
            if scenario == "session-changed":
                panel.set_session_id("other")
            if scenario == "run-changed":
                main._state.run.started_at = timestamp + timedelta(seconds=1)
        if scenario != "retry":
            await bus.publish(
                FreshTurnCleaned(session_id=session_id, timestamp=timestamp, retired_previous_turn=True),
                raise_handler_errors=True,
            )
        await bus.publish(
            TurnStarted(session_id=session_id, timestamp=timestamp, turn_number=6, is_retry=scenario == "retry"),
            raise_handler_errors=True,
        )
        if deferred:
            assert [message.text for message in panel.query(UserMessage)] == ["Previous"]
            await panel.add_user_message("New")
            if scenario == "session-changed":
                panel.set_session_id("other")
            if scenario == "run-changed":
                main._state.run.started_at = timestamp + timedelta(seconds=1)
            main._state.render_gate.finish(rendered=True)
            await main._input_flow.flush_deferred_agent_messages()
        users = list(panel.query(UserMessage))
        assert [message.text for message in users] == (["New"] if scenario == "matching" else ["Previous", "New"])
        assert users[-1].turn_number == (6 if scenario == "matching" else None)
        assert [item.turn_index for item in panel.toc_items] == ([6] if scenario == "matching" else [6, None])
