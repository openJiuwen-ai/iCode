# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted search settings reach rebuilt main-agent tools."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings_store import persist
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import InvocationToolCallResult, SettingsReload, UserMessage
from chrys.foundation.models.workspace import Workspace
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.state.store import JsonFileStateStore
from tests.support.engines import AgentEngineFactory
from tests.support.event_capture import capture_events
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import ENGINE_TURN_TIMEOUT


async def test_main_search_tools_follow_persisted_setting_after_reload(
    tmp_path: Path, git_repo_factory, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    root = git_repo_factory(tmp_path / "repo")
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "ignored.py").write_text("NEEDLE\n", encoding="utf-8")
    (root / "visible.py").write_text("NEEDLE\n", encoding="utf-8")
    freeze_process_env()
    settings, models = make_mock_settings_and_registry(stream=False)
    profile = AgentProfile(
        name="Search",
        instructions="Search files.",
        tools=ToolsConfig(builtins=["search"]),
        approval=ApprovalConfig(default="auto"),
        compaction=CompactionConfig(enabled=False),
    )
    client = MockChatClient(
        responses=[
            response
            for index in range(3)
            for response in (
                MockResponse(tool_calls=[("grep", f"search-{index}", {"pattern": "NEEDLE", "glob": "*.py"})]),
                MockResponse(text="Done"),
            )
        ]
    )
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    bus = EventBus()
    results = await capture_events(bus, InvocationToolCallResult)
    descriptions = []
    engine = agent_engine(
        bus,
        settings=settings,
        model_registry=models,
        state_store=JsonFileStateStore(tmp_path / "sessions"),
        initial_workspace=Workspace.from_cwd(str(root)),
    )
    engine.pin_model_profile()
    await engine.start(profile)
    for index, respect in enumerate((True, False, True)):
        if index:
            persist({"tools.search.respect_gitignore": respect})
            await bus.publish(SettingsReload(), raise_handler_errors=True)
        assert engine.settings.search_respect_gitignore is respect
        await bus.publish(UserMessage(text="Search"), raise_handler_errors=True)
        await asyncio.wait_for(engine.wait_for_run_task(), ENGINE_TURN_TIMEOUT)
        tools = {tool.name: tool for tool in client.call_history[-1][1]["tools"]}
        descriptions.append(
            {name: tools[name].to_json_schema_spec()["function"]["description"] for name in ("grep", "glob")}
        )
        matches = [event for event in results if event.tool_name == "grep"]
        assert len(matches) == index + 1
        assert "visible.py" in matches[-1].result
        assert ("ignored.py" in matches[-1].result) is not respect
    for tool_descriptions, state in zip(descriptions, ("enabled", "disabled", "enabled"), strict=True):
        for description in tool_descriptions.values():
            assert f"Respect .gitignore is {state}" in description
            assert description.count("Current setting:") == 1
            assert ".ignore/.rgignore rules still apply" in description
