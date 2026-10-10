# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persisted file edits join to tool occurrences even when provider IDs repeat."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
from chrys.foundation.events.bus import EventBus
from chrys.foundation.events.types import UserMessage
from chrys.foundation.models.workspace import Workspace
from chrys.foundation.trajectory.metadata import OPERATION_ID_KEY
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.mutations.types import FileMutation
from chrys.service.profiles.agents.schema import AgentProfile, ApprovalConfig, CompactionConfig, ToolsConfig
from chrys.service.state.store import JsonFileStateStore
from tests.support.engines import AgentEngineFactory
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.waiting import await_run_task_chain


async def test_repeated_provider_call_ids_keep_distinct_persisted_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_engine: AgentEngineFactory
) -> None:
    settings, models = make_mock_settings_and_registry(stream=False)
    path = str(tmp_path / "edited.txt")
    profile = AgentProfile(
        name="Editor",
        instructions="Edit files.",
        tools=ToolsConfig(builtins=["filesystem.write"]),
        approval=ApprovalConfig(default="auto", overrides={"write_file": "auto"}),
        compaction=CompactionConfig(enabled=False),
    )
    client = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("write_file", "call_repeated", {"path": path, "content": "first\n"})]),
            MockResponse(text="Done"),
            MockResponse(tool_calls=[("write_file", "call_repeated", {"path": path, "content": "second\n"})]),
            MockResponse(text="Done"),
        ]
    )
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, return_value=client)
    )
    bus = EventBus()
    store = JsonFileStateStore(tmp_path / "sessions")
    engine = agent_engine(
        bus,
        settings=settings,
        model_registry=models,
        state_store=store,
        initial_workspace=Workspace.from_cwd(str(tmp_path)),
    )
    engine.pin_model_profile()
    await engine.start(profile)
    for text in ("Write first", "Write second"):
        await bus.publish(UserMessage(text=text), raise_handler_errors=True)
        await await_run_task_chain(engine, turn_state=engine.turns.turn_state)
    state = await store.load_session(engine.session.session_id)
    assert state is not None
    messages = await store.load_session_raw(engine.session.session_id)
    assert messages is not None
    calls = [content for message in messages for content in message["contents"] if content["type"] == "function_call"]
    assert len(calls) == 2
    operations = [call["additional_properties"][OPERATION_ID_KEY] for call in calls]
    assert len(set(operations)) == 2
    mutations = [mutation for turn in state["chrys_mutations"]["turns"] for mutation in turn["mutations"]]
    assert [mutation["tool_operation_id"] for mutation in mutations] == operations
    assert all(mutation["tool_call_id"] != "call_repeated" for mutation in mutations)
    for mutation in mutations:
        assert FileMutation.from_dict(mutation).to_dict() == mutation
    legacy = dict(mutations[0])
    legacy.pop("tool_operation_id")
    assert FileMutation.from_dict(legacy).tool_operation_id is None
    assert FileMutation.from_dict(legacy).to_dict() == legacy
