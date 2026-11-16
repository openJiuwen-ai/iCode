# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP sessions share dependency initialization and recover through a full reload."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import create_autospec

import pytest
from acp.helpers import text_block

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.sub_agents.tools as child_module
import chrys.service.mcp._connection as connection_module
import chrys.service.skills.discovery as discovery_module
from chrys.app.acp.server import ChrysAcpServer
from chrys.app.acp.session_manager import AcpSessionManager
from chrys.foundation.config.spec import Source
from chrys.orchestration.startup import bootstrap_runtime
from chrys.service.approval.policy import ApprovalMode
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.models.registry import ModelProfileRegistry
from tests.app.acp._server_fakes import _FakeClient
from tests.app.cli.test_initialization import saved_initialization as saved_initialization
from tests.orchestration.workflows.test_initialization import _tool_result_text, _tools_sent
from tests.service.mcp.test_cache import _FakeMCPTool


async def test_session_initialization_failure_is_shared_and_full_reload_recovers(
    tmp_path: Path, saved_initialization: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = MockChatClient(responses=[MockResponse(text="initial done")])
    recovered = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[
                    ("remote", "remote-call", {"value": "ACP tool works"}),
                    ("load_skill", "skill-call", {"skill_name": "demo"}),
                ]
            ),
            MockResponse(text="recovered done"),
        ]
    )
    monkeypatch.setattr(
        builder_module, "create_client", create_autospec(builder_module.create_client, side_effect=[initial, recovered])
    )
    child_factory = create_autospec(
        child_module.create_client, side_effect=[MockChatClient(responses=[]) for _ in range(6)]
    )
    monkeypatch.setattr(child_module, "create_client", child_factory)
    bad = _FakeMCPTool(name="shared", enter_error=RuntimeError("synthetic failure"))
    good = _FakeMCPTool(name="shared")
    connect = create_autospec(connection_module._create_mcp_tool, return_value=bad)
    monkeypatch.setattr(connection_module, "_create_mcp_tool", connect)
    scan = create_autospec(discovery_module.discover_file_skills, side_effect=discovery_module.discover_file_skills)
    monkeypatch.setattr(discovery_module, "discover_file_skills", scan)
    loaded = bootstrap_runtime(apply_patches=False, setup_telemetry=False).loaded
    agents, models = AgentProfileRegistry(), ModelProfileRegistry()
    agents.load_all()
    models.load_all()
    manager = AcpSessionManager(
        loaded_settings=loaded.overlay(Source.CLI, ask_user_timeout_seconds=None),
        profile_name="Worker",
        approval_mode=ApprovalMode.BYPASS,
        process_cwd=str(tmp_path),
        agent_registry=agents,
        model_registry=models,
    )
    server = ChrysAcpServer(manager, initial_vision=False)
    server.on_connect(_FakeClient())
    try:
        session = await server.new_session(cwd=str(tmp_path), mcp_servers=[])
        assert connect.call_count == scan.call_count == 1
        assert child_factory.call_count == 3
        await server.prompt([text_block("inspect the fixture")], session_id=session.session_id)
        assert {"Child0", "Child1", "Child2", "load_skill"} <= _tools_sent(initial)
        assert "remote" not in _tools_sent(initial)
        connect.return_value = good
        await asyncio.to_thread(
            (saved_initialization / "demo" / "SKILL.md").write_text,
            "---\nname: demo\ndescription: Updated catalog\n---\nChanged instructions.\n",
            encoding="utf-8",
        )
        report = await server.ext_method("settings/reload", {"sessionId": session.session_id, "scope": "full"})
        assert report["installed"] is True and report["complete"] is True
        assert connect.call_count == scan.call_count == 2
        assert child_factory.call_count == 6
        await server.prompt([text_block("use the recovered tools")], session_id=session.session_id)
        assert "ACP tool works" in _tool_result_text(recovered)
        assert "Changed instructions." in _tool_result_text(recovered)
    finally:
        await manager.shutdown()
    assert good.exit_count == 1
