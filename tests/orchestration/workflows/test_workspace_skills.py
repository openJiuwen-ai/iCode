# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow nodes keep cwd skills while excluding their chat session's workspace sources."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings
from chrys.foundation.events.types import WorkspaceChange, WorkspaceOperation
from chrys.orchestration.engine import session_lifecycle
from chrys.orchestration.engine.state import controls
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import SkillsConfig
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_workdir,
    patch_runtime,
    run,
    write_workflow,
)
from tests.support.workspace_registry import write_workspace_skill


async def test_workflow_node_keeps_cwd_skills_without_loading_chat_workspace_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = make_workdir(tmp_path)
    related = tmp_path / "related"
    await asyncio.to_thread(write_workspace_skill, cwd, "cwd-skill", "Scope fixture")
    await asyncio.to_thread(write_workspace_skill, related, "related-skill", "Scope fixture")
    node = MockChatClient([MockResponse(text="node done")])
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), node])
    profile = replace(
        make_profile(),
        skills=SkillsConfig(auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=True),
    )
    settings = Settings(model_profile="mock-profile", workdir_skills_enabled=True, workspace_change_notice=False)
    loaded = LoadedSettings(settings=settings, provenance={})
    for module in (controls, session_lifecycle):
        monkeypatch.setattr(module, "load_settings", create_autospec(module.load_settings, return_value=loaded))
    write_workflow(
        cwd,
        "scope",
        b"from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('scope')\n"
        b"node = wf.agent('node', profile='Headless')\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n",
    )
    host = make_host(tmp_path, project=cwd, profiles=[profile], loaded_settings=loaded)
    try:
        await host.start()
        selected = host.engine.lifecycle.workspaces.registry.create("Chat workspace", str(cwd), (str(related),))
        request = WorkspaceChange(workspace_operation=WorkspaceOperation.SELECT, workspace_id=selected.id)
        await host.event_bus.publish(request, raise_handler_errors=True)
        assert host.engine.session.workspace_id == selected.id
        assert {skill.name for skill in host.engine.runtime_details.skill_details} == {"cwd-skill", "related-skill"}

        await confirm(host, "scope")
        result, _events = await run(host, "scope")

        assert result.outcome.value == "completed"
        messages, _options = node.call_history[0]
        prompt = "\n".join(message.text for message in messages)
        assert "<available_skills>" in prompt
        assert "<name>cwd-skill</name>" in prompt
        assert "<name>related-skill</name>" not in prompt
        assert {skill.name for skill in host.engine.runtime_details.skill_details} == {"cwd-skill", "related-skill"}
    finally:
        await host.shutdown()
