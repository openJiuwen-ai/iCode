# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A retained snapshot window controls live message links and the real picker."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from textual.widgets import Select

import chrys.orchestration.engine.build.builder as builder_module
from chrys.app.tui.screens.diff.rollback_modal import RollbackModal
from chrys.app.tui.widgets.chat.messages import MessageRollbackButton, UserMessage
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    ToolsConfig,
)
from chrys.service.state.store import JsonFileStateStore
from tests.support.engines import AgentEngineFactory
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import (
    ENGINE_TEST_WAIT_TIMEOUT,
    ENGINE_TURN_TIMEOUT,
    await_run_task_chain,
    wait_for,
    with_wait_deadline,
)


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_seventh_turn_prunes_first_two_message_links_but_keeps_session_start_in_picker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: AgentEngineFactory,
) -> None:
    monkeypatch.setattr(
        "chrys.app.tui.util.git_branch.GitBranchMonitor._start_observer", lambda _monitor, _target: None
    )
    settings, models = make_mock_settings_and_registry(stream=False)
    settings = replace(settings, rollback_snapshots_keep=5, workspace_change_notice=False)
    profile = AgentProfile(
        name="Window",
        instructions="Answer the question.",
        tools=ToolsConfig(builtins=[]),
        skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    registry = AgentProfileRegistry()
    registry.register(profile)
    client = MockChatClient(responses=[MockResponse(text=f"Answer {turn}") for turn in range(1, 8)])
    held, release = asyncio.Event(), asyncio.Event()
    original_response = client._mock_response

    async def response(resp, model_id, finish_reason, on_text):
        if resp.text == "Answer 7":
            held.set()
            await release.wait()
        return await original_response(resp, model_id, finish_reason, on_text)

    monkeypatch.setattr(client, "_mock_response", response)
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    bus = EventBus()
    store = JsonFileStateStore(tmp_path / "state")
    engine = agent_engine(
        bus,
        settings=settings,
        state_store=store,
        agent_registry=registry,
        model_registry=models,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
        surface=SessionSurface.TUI,
        initial_approval_mode=ApprovalMode.BYPASS,
    )
    engine.pin_model_profile()
    app = make_chrys_app(
        tmp_path / "state",
        engine=engine,
        settings=engine.settings_handle.settings,
        settings_handle=engine.settings_handle,
        event_bus=bus,
        agent_registry=registry,
        model_registry=models,
        profile_name="Window",
    )
    async with app.run_test(size=(110, 38)) as pilot:
        await wait_for(
            lambda: (
                app._main_screen is not None
                and bool(app._main_screen.chat_session_id)
                and engine.current.loaded is not None
                and not app._main_screen.query_one(InputBar).agent_loading
            ),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="window test engine ready",
        )
        main = app._main_screen
        assert main is not None
        panel = main.query_one(ChatPanel)
        await app.workers.wait_for_complete()
        for turn in range(1, 7):
            main._submit_user_text(f"Question {turn}")
            await wait_for(
                lambda turn=turn: engine.current_turn_number == turn,
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
                description=f"turn {turn} admitted",
            )
            await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
            await wait_for(
                lambda: not main._state.run.agent_running and not engine.is_turn_lifecycle_active,
                pilot=pilot,
                timeout=ENGINE_TURN_TIMEOUT,
                description=f"turn {turn} finished in the frontend",
            )
            if turn == 2:
                await wait_for(
                    lambda: all(
                        message.query_one(MessageRollbackButton).display for message in panel.query(UserMessage)
                    ),
                    pilot=pilot,
                    description="initial rollback links visible before pruning",
                )

        main._submit_user_text("Question 7")
        await wait_for(
            lambda: engine.current_turn_number == 7,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="seventh turn admitted",
        )
        task = engine.turn_lifecycle_task
        assert task is not None
        await wait_for(
            lambda: held.is_set() or task.done(),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="seventh response held after its snapshot capture",
        )
        if task.done():
            await task
        try:
            assert held.is_set()
            await wait_for(
                lambda: (
                    len(panel.query(UserMessage)) == 7
                    and [message.turn_number for message in panel.query(UserMessage)] == list(range(1, 8))
                    and [message.query_one(MessageRollbackButton).display for message in panel.query(UserMessage)]
                    == [False, False, True, True, True, True, True]
                ),
                pilot=pilot,
                description="pruning hides only the first two message links",
            )
            assert engine.available_rollback_turns() == [0, 2, 3, 4, 5, 6]
        finally:
            release.set()
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await wait_for(
            lambda: not main._state.run.agent_running and not engine.is_turn_lifecycle_active,
            pilot=pilot,
            description="seventh turn settled before opening the picker",
        )

        main.action_show_rollback()
        await wait_for(
            lambda: isinstance(app.screen, RollbackModal) and app.screen._content_ready,
            pilot=pilot,
            description="real pruned rollback picker ready",
        )
        modal = app.screen
        assert isinstance(modal, RollbackModal)
        assert modal.query_one("#rollback-turn-select", Select).value == 6
        # The session start needs no snapshot: `/rollback to 0` and the picker keep it after pruning.
        assert [value for _label, value in modal._build_turn_options()] == [6, 5, 4, 3, 2, 0]
        await pilot.press("escape")
