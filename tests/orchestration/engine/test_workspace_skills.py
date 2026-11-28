# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workspace skill sources follow the committed runtime through real engine turns."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import create_autospec

from chrys.foundation.events.types import WorkspaceChange, WorkspaceOperation
from chrys.orchestration.sub_agents import tools as children
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import SkillsConfig, SubAgentRef, SubAgentsConfig
from chrys.service.state.store import JsonFileStateStore
from tests.support import workspace_engine
from tests.support.pipeline_helpers import make_mock_settings_and_registry
from tests.support.rollback_spec import PROFILE, ask
from tests.support.waiting import ENGINE_TEST_WAIT_TIMEOUT, with_wait_deadline
from tests.support.workspace_registry import write_workspace_skill


def _configure(monkeypatch, *, enabled=True, profile_enabled=True):
    settings, models = make_mock_settings_and_registry()
    settings = replace(settings, workdir_skills_enabled=enabled)
    monkeypatch.setattr(
        workspace_engine,
        "make_mock_settings_and_registry",
        create_autospec(workspace_engine.make_mock_settings_and_registry, return_value=(settings, models)),
    )
    profile = replace(
        PROFILE,
        skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=profile_enabled),
    )
    monkeypatch.setattr(workspace_engine, "PROFILE", profile)
    return profile


def _catalog(client: MockChatClient) -> str:
    messages, _options = client.call_history[-1]
    text = next(message.text for message in reversed(messages) if message.role == "user")
    return text.split("<available_skills>", 1)[1].split("</available_skills>", 1)[0]


async def test_workspace_skills_reach_model_and_runtime_with_both_gates_enabled(agent_engine, monkeypatch, tmp_path):
    primary, related, another = (tmp_path / name for name in ("primary", "related", "another"))
    cwd = primary / "working"
    cwd.mkdir(parents=True)
    related.mkdir()
    another.mkdir()
    for root in (cwd, primary, related, another):
        await asyncio.to_thread(write_workspace_skill, root, "shared", root.name)
        await asyncio.to_thread(write_workspace_skill, root, root.name)
    for root in (primary, related, another):
        await asyncio.to_thread(write_workspace_skill, root, "workspace-shared", root.name)
    for root in (related, another):
        await asyncio.to_thread(write_workspace_skill, root, "related-shared", root.name)
    selected = JsonFileStateStore(tmp_path / "sessions").workspace_registry.create(
        "App", str(primary), (str(related), str(another))
    )
    _configure(monkeypatch)
    client = MockChatClient([MockResponse(text="done")])
    engine = await workspace_engine.start_workspace_engine(
        agent_engine,
        monkeypatch,
        primary,
        workspace_id=selected.id,
        client=client,
        store=JsonFileStateStore(tmp_path / "sessions"),
    )

    await ask(engine, "Use the available skills")

    details = engine.runtime_details
    expected = {
        "shared": str(primary / ".agents" / "skills" / "shared"),
        "primary": str(primary / ".agents" / "skills" / "primary"),
        "workspace-shared": str(primary / ".agents" / "skills" / "workspace-shared"),
        "related": str(related / ".agents" / "skills" / "related"),
        "related-shared": str(related / ".agents" / "skills" / "related-shared"),
        "another": str(another / ".agents" / "skills" / "another"),
    }
    assert {skill.name: skill.source for skill in details.skill_details} == expected
    assert details.skill_sources == {source: [name] for name, source in expected.items()}
    catalog = _catalog(client)
    assert catalog.count("<name>shared</name>") == 1
    for name, source in expected.items():
        assert f"<name>{name}</name>" in catalog
        assert f"<skill_dir>{source}</skill_dir>" in catalog


@with_wait_deadline(ENGINE_TEST_WAIT_TIMEOUT)
async def test_workspace_skill_folders_refresh_on_rebuild_and_leave_with_the_workspace(
    agent_engine, monkeypatch, tmp_path
):
    primary, old, new = (tmp_path / name for name in ("primary", "old", "new"))
    for root in (primary, old, new):
        await asyncio.to_thread(write_workspace_skill, root, root.name)
    registry = JsonFileStateStore(tmp_path / "sessions").workspace_registry
    selected = registry.create("App", str(primary), (str(old),))
    _configure(monkeypatch)
    client = MockChatClient([MockResponse(text="done") for _ in range(4)])
    engine = await workspace_engine.start_workspace_engine(
        agent_engine,
        monkeypatch,
        primary,
        workspace_id=selected.id,
        client=client,
        store=JsonFileStateStore(tmp_path / "sessions"),
    )

    await ask(engine, "first")
    assert {skill.name for skill in engine.runtime_details.skill_details} == {"primary", "old"}
    assert "<name>old</name>" in _catalog(client)
    # Editing membership replaces the build's directory snapshot when it is rebuilt.
    registry.update(selected.id, name="App", primary=str(primary), folders=(str(new),))
    await workspace_engine.select_workspace(engine.event_bus, selected.id)
    await ask(engine, "after editing the workspace")
    assert {skill.name for skill in engine.runtime_details.skill_details} == {"primary", "new"}
    assert "<name>new</name>" in _catalog(client)
    assert "<name>old</name>" not in _catalog(client)
    # A later file in the same committed directory is found by the per-turn rescan.
    await asyncio.to_thread(write_workspace_skill, new, "added-later")
    await ask(engine, "after adding a skill file")
    assert {skill.name for skill in engine.runtime_details.skill_details} == {"primary", "new", "added-later"}
    assert "<name>added-later</name>" in _catalog(client)

    request = WorkspaceChange(workspace_operation=WorkspaceOperation.UNSPECIFY)
    await engine.event_bus.publish(request, raise_handler_errors=True)
    assert engine.session.workspace_id is None
    await ask(engine, "after leaving the workspace")
    assert engine.workspace.primary_cwd == str(primary)
    assert {skill.name for skill in engine.runtime_details.skill_details} == {"primary"}
    assert "<name>new</name>" not in _catalog(client)
    assert "<name>added-later</name>" not in _catalog(client)


async def test_kernel_child_loads_workspace_skills_from_parent_runtime(agent_engine, monkeypatch, tmp_path):
    primary, related = tmp_path / "primary", tmp_path / "related"
    primary.mkdir()
    expected_source = await asyncio.to_thread(write_workspace_skill, related, "related-skill")
    selected = JsonFileStateStore(tmp_path / "sessions").workspace_registry.create("App", str(primary), (str(related),))
    parent = _configure(monkeypatch)
    parent.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Worker", tool_name="Worker")])
    registry = AgentProfileRegistry()
    registry.register(replace(parent, name="Worker", sub_agents=SubAgentsConfig()))
    monkeypatch.setattr(
        workspace_engine,
        "AgentProfileRegistry",
        create_autospec(AgentProfileRegistry, return_value=registry),
    )
    child = MockChatClient([MockResponse(text="child done")])
    monkeypatch.setattr(children, "create_client", create_autospec(children.create_client, return_value=child))
    client = MockChatClient(
        [
            MockResponse(tool_calls=[("Worker", "delegate", {"prompt": "Use related-skill"})]),
            MockResponse(text="parent done"),
        ]
    )
    engine = await workspace_engine.start_workspace_engine(
        agent_engine,
        monkeypatch,
        primary,
        workspace_id=selected.id,
        client=client,
        store=JsonFileStateStore(tmp_path / "sessions"),
    )

    await ask(engine, "Delegate this work")

    catalog = _catalog(child)
    assert "<name>related-skill</name>" in catalog
    assert f"<skill_dir>{expected_source}</skill_dir>" in catalog
