# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline public-engine driver for independent rollback specification tests."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserMessage
from chrys.foundation.models.workspace import Workspace
from chrys.kernel import ChatResponseUpdate, Content
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    SkillsConfig,
    ToolsConfig,
)
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.scripted_clients import FrameworkBoom
from tests.support.waiting import await_run_task_chain

_CREATE_CLIENT = builder_module.create_client

PROFILE = AgentProfile(
    name="Spec",
    instructions="Follow the request.",
    tools=ToolsConfig(builtins=[]),
    skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False),
    approval=ApprovalConfig(default="auto"),
    compaction=CompactionConfig(enabled=False),
)


class SpecClient(MockChatClient):
    """Script a terminal failure or park a stream until the user interrupts it."""

    def __init__(self, responses):
        super().__init__(responses=responses)
        self.parked = asyncio.Event()

    async def _stream_updates(self, resp, model_id, finish_reason):
        if resp.text == "FAIL":
            raise FrameworkBoom("spec provider refusal")
        if resp.text.startswith("PARK:"):
            mode = resp.text.removeprefix("PARK:")
            if mode == "answer":
                yield ChatResponseUpdate(role="assistant", contents=[Content.from_text("partial answer A")])
            elif mode == "thought":
                yield ChatResponseUpdate(
                    role="assistant", contents=[Content.from_text_reasoning(text="private thought")]
                )
            self.parked.set()
            await asyncio.Event().wait()
        else:
            async for update in super()._stream_updates(resp, model_id, finish_reason):
                yield update


async def start_engine(agent_engine, monkeypatch, root: Path, store, client, *, session_id=None, keep=None):
    settings, models = make_mock_settings_and_registry(stream=True)
    settings = replace(settings, workspace_change_notice=False, max_transient_retries=0)
    if keep is not None:
        settings = replace(settings, rollback_snapshots_keep=keep)
    registry = AgentProfileRegistry()
    registry.register(PROFILE)
    monkeypatch.setattr(builder_module, "create_client", create_autospec(_CREATE_CLIENT, return_value=client))
    engine = agent_engine(
        EventBus(),
        settings=settings,
        state_store=store,
        agent_registry=registry,
        model_registry=models,
        initial_workspace=Workspace.from_cwd(str(root)),
    )
    if session_id is not None:
        engine.session.session_id = session_id
    await engine.start(PROFILE)
    return engine


async def drain(engine):
    await await_run_task_chain(engine, turn_state=engine.turns.turn_state)


async def ask(engine, text):
    await engine.event_bus.publish(UserMessage(text=text), raise_handler_errors=True)
    await drain(engine)


def answer(text):
    return MockResponse(text=text)
