# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A restored session's sub-agent cards show the counters their live cards showed."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.sub_agents.tools as sub_agent_module
from chrys.app.tui.widgets.chat.panel import ChatPanel
from chrys.app.tui.widgets.chat.renderers.sub_agent import SubAgentToolCall
from chrys.app.tui.widgets.chat.tool_call import ToolGroup
from chrys.app.tui.widgets.chrome.input_bar import InputBar
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import SessionRestore
from chrys.foundation.models.session_surface import SessionSurface
from chrys.foundation.models.workspace import Workspace
from chrys.kernel.types import UsageDetails
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
from chrys.service.state.store import JsonFileStateStore
from tests.support.engines import AgentEngineFactory
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.tui_app_harness import make_chrys_app
from tests.support.waiting import ENGINE_TURN_TIMEOUT, await_run_task_chain, wait_for

pytestmark = pytest.mark.usefixtures("frozen_earlier_heap")


def _counters(main: object) -> dict[str, str]:
    """Each sub-agent card's counters line, by the prompt it was given."""
    return {card._task_prompt: card._render_subtitle() for card in main.query(SubAgentToolCall)}  # type: ignore[attr-defined]


@pytest.mark.parametrize("children", [1, 3], ids=["one", "parallel"])
async def test_restored_sub_agent_cards_show_their_context_and_spend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_engine: AgentEngineFactory,
    children: int,
) -> None:
    settings, models = make_mock_settings_and_registry(stream=True)
    lead = AgentProfile(
        name="Lead",
        instructions="Delegate.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Writer")]),
    )
    writer = AgentProfile(
        name="Writer",
        sub_agent_only=True,
        instructions="Write.",
        tools=ToolsConfig(builtins=[]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    # Every child spends the same, so the cards match whichever child takes which answer.
    child_client = MockChatClient(
        responses=[
            MockResponse(text="A story.", usage_details=UsageDetails(input_token_count=9_000, output_token_count=100))
            for _ in range(children)
        ]
    )
    monkeypatch.setattr(
        sub_agent_module, "create_client", create_autospec(sub_agent_module.create_client, return_value=child_client)
    )
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("Writer", f"story{i}", {"prompt": f"Write story {i}"}) for i in range(children)]),
            MockResponse(text="Done."),
        ]
    )
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    bus = EventBus()
    registry = AgentProfileRegistry()
    registry.register(lead)
    registry.register(writer)
    engine = agent_engine(
        bus,
        settings=settings,
        agent_registry=registry,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
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
        profile_name="Lead",
    )
    async with app.run_test(size=(160, 60)) as pilot:
        await wait_for(
            lambda: (
                app._main_screen is not None
                and bool(app._main_screen.chat_session_id)
                and engine.current.loaded is not None
                and not app._main_screen.query_one(InputBar).agent_loading
            ),
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="engine ready",
        )
        main = app._main_screen
        await app.workers.wait_for_complete()
        main._submit_user_text("Delegate the stories")
        await wait_for(
            lambda: client.call_count > 0, pilot=pilot, timeout=ENGINE_TURN_TIMEOUT, description="turn admitted"
        )
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
        await wait_for(
            lambda: (
                len(cards := list(main.query(SubAgentToolCall))) == children
                and all(card.status == "complete" for card in cards)
            ),
            pilot=pilot,
            description="every live card complete",
        )
        live = _counters(main)
        assert all("Ctx: 9.1k tokens · Spend: 9.1k tokens" in counters for counters in live.values()), live

        session_id = engine.session.session_id
        await main._sessions.create_new_session()
        await wait_for(
            lambda: main.chat_session_id != session_id,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="new session",
        )
        await bus.publish(SessionRestore(session_id=session_id), raise_handler_errors=True)
        await wait_for(
            lambda: main.chat_session_id == session_id,
            pilot=pilot,
            timeout=ENGINE_TURN_TIMEOUT,
            description="session restored",
        )
        await main.query_one(ChatPanel).wait_replay_complete()
        for group in main.query(ToolGroup):
            group.collapsed = False
        await wait_for(
            lambda: (
                len(cards := list(main.query(SubAgentToolCall))) == children
                and all(card.status == "complete" for card in cards)
            ),
            pilot=pilot,
            description="every replayed card complete",
        )

        assert _counters(main) == live
