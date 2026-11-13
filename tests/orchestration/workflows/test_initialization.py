# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Workflow nodes and their children share initialization only within one build."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.sub_agents.tools as sub_agent_module
import chrys.service.mcp._connection as mcp_connection_module
import chrys.service.skills.discovery as discovery_module
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.schema import MCPServerConfig, SkillsConfig, SubAgentRef, SubAgentsConfig
from tests.orchestration.workflows._hosting import (
    confirm,
    make_host,
    make_profile,
    make_project,
    patch_runtime,
    run,
    write_workflow,
)
from tests.service.mcp.test_cache import _FakeMCPTool

_WORKFLOW = (
    b"from chrys.workflows import WorkflowBuilder\nwf = WorkflowBuilder('initialization')\n"
    b"node = wf.agent('node', profile='Worker')\nwf.start(node)\nwf.output(node)\nworkflow = wf.build()\n"
)


@pytest.fixture
def skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    folder = root / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Shared catalog\n---\nInitial instructions.\n", encoding="utf-8"
    )
    return root


def _profiles(skill_root: Path):
    server = MCPServerConfig(name="shared", transport="stdio", command="unused")
    children = [make_profile(f"Child{index}") for index in range(3)]
    worker = make_profile("Worker")
    worker.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile=child.name) for child in children])
    for profile in [worker, *children]:
        profile.tools.mcp = [server]
        profile.skills = SkillsConfig(
            paths=[str(skill_root)], auto_load_user_agents_skills=False, auto_load_cwd_agents_skills=False
        )
    return [make_profile(), worker, *children]


def _tool_result_text(client: MockChatClient) -> str:
    return "\n".join(
        str(content.result)
        for messages, _options in client.call_history
        for message in messages
        for content in message.contents
        if content.type == "function_result"
    )


def _tools_sent(client: MockChatClient) -> set[str]:
    _messages, options = client.call_history[0]
    return {tool.name for tool in options.get("tools") or []}


def _child_clients(monkeypatch: pytest.MonkeyPatch) -> list[MockChatClient]:
    clients: list[MockChatClient] = []

    async def create_client(
        model_profile,
        *,
        session_id,
        parent_session_id,
        use_route_session_context,
        session_dir,
        tool_result_ceiling_tokens,
    ):
        client = MockChatClient(
            responses=[
                MockResponse(tool_calls=[("remote", "child-remote", {"value": "child connection works"})]),
                MockResponse(text="child done"),
            ]
        )
        clients.append(client)
        return client

    monkeypatch.setattr(sub_agent_module, "create_client", create_client)
    return clients


@pytest.mark.parametrize("first_connection_fails", [False, True], ids=["success-reuse", "failure-recovery"])
async def test_node_and_children_share_one_initialization_and_next_activation_refreshes(
    tmp_path: Path, skill_root: Path, monkeypatch: pytest.MonkeyPatch, first_connection_fails: bool
) -> None:
    first = MockChatClient(
        responses=[
            MockResponse(tool_calls=[("load_skill", "initial-skill", {"skill_name": "demo"})]),
            MockResponse(text="initial run done"),
        ]
    )
    second = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[
                    ("remote", "node-remote", {"value": "node connection works"}),
                    ("load_skill", "changed-skill", {"skill_name": "demo"}),
                    ("Child0", "delegate", {"prompt": "use the remote tool"}),
                ]
            ),
            MockResponse(text="second run done"),
        ]
    )
    patch_runtime(monkeypatch, [MockChatClient(responses=[]), first, second])
    children = _child_clients(monkeypatch)
    good = _FakeMCPTool(name="shared")
    bad = _FakeMCPTool(name="shared", enter_error=RuntimeError("synthetic connection failure"))
    factory = create_autospec(
        mcp_connection_module._create_mcp_tool, return_value=bad if first_connection_fails else good
    )
    monkeypatch.setattr(mcp_connection_module, "_create_mcp_tool", factory)
    scan = create_autospec(discovery_module.discover_file_skills, side_effect=discovery_module.discover_file_skills)
    monkeypatch.setattr(discovery_module, "discover_file_skills", scan)
    project = make_project(tmp_path)
    write_workflow(project, "initialization", _WORKFLOW)
    host = make_host(tmp_path, project=project, profiles=_profiles(skill_root))

    def shared_scans() -> int:
        return sum(str(skill_root) in call.args[0] for call in scan.call_args_list)

    try:
        await confirm(host, "initialization")
        result, _events = await run(host, "initialization")
        assert result.outcome.value == "completed"
        assert factory.call_count == 1
        assert shared_scans() == 1
        assert len(children) == 3
        assert {"Child0", "Child1", "Child2", "load_skill"} <= _tools_sent(first)
        assert ("remote" in _tools_sent(first)) is not first_connection_fails
        assert "Initial instructions." in _tool_result_text(first)

        factory.return_value = good
        await asyncio.to_thread(
            (skill_root / "demo" / "SKILL.md").write_text,
            "---\nname: demo\ndescription: Updated catalog\n---\nChanged instructions.\n",
            encoding="utf-8",
        )
        await confirm(host, "initialization")
        result, _events = await run(host, "initialization")
        assert result.outcome.value == "completed"
        assert factory.call_count == (2 if first_connection_fails else 1)
        assert shared_scans() == 2
        assert len(children) == 6
        assert {"Child0", "Child1", "Child2", "load_skill", "remote"} <= _tools_sent(second)
        assert "Changed instructions." in _tool_result_text(second)
        assert "node connection works" in _tool_result_text(second)
        assert "child connection works" in _tool_result_text(children[3])
        assert good.exit_count == 0
    finally:
        await host.shutdown()
    assert good.exit_count == 1
