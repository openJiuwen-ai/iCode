# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The headless command builds shared dependencies and keeps stdout machine-readable."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import create_autospec

import pytest

import chrys.orchestration.engine.build.builder as builder_module
import chrys.orchestration.sub_agents.tools as child_module
import chrys.service.mcp._connection as connection_module
import chrys.service.skills.discovery as discovery_module
from chrys.app.cli import run as run_cli
from chrys.foundation.config.runtime_pointer import MODEL_POINTER_ENV, MODEL_POINTER_KEY
from chrys.foundation.config.settings_store import persist
from chrys.service.llm.mock import MockChatClient, MockResponse
from chrys.service.profiles.agents.serializer import save_profile as save_agent
from chrys.service.profiles.models.schema import ModelProfile
from chrys.service.profiles.models.serializer import save_profile as save_model
from tests.orchestration.workflows.test_initialization import _profiles, _tool_result_text, _tools_sent
from tests.service.mcp.test_cache import _FakeMCPTool
from tests.support.tool_packs import install_tools


@pytest.fixture
def saved_initialization(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Profiles and skills are read from isolated real files by the entrypoint."""
    monkeypatch.setenv(MODEL_POINTER_ENV, "")
    monkeypatch.delenv(MODEL_POINTER_ENV)
    # The real CLI bootstrap exports these for subprocesses; own their test lifetime.
    monkeypatch.setenv("PYTHONUTF8", "1")
    monkeypatch.setenv("PYTHONIOENCODING", "utf-8")
    root = tmp_path / "skills"
    folder = root / "demo"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Shared catalog\n---\nInitial instructions.\n", encoding="utf-8"
    )
    for profile in _profiles(root):
        save_agent(profile)
    save_model(ModelProfile(id="mock-init", name="mock", provider="mock", model_id="mock", stream=False))
    assert persist({MODEL_POINTER_KEY: "mock-init"}).ok
    install_tools(monkeypatch)
    return root


def test_run_command_shares_failed_initialization_and_next_launch_recovers(
    tmp_path: Path,
    saved_initialization: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    initial = MockChatClient(responses=[MockResponse(text="initial done")])
    recovered = MockChatClient(
        responses=[
            MockResponse(
                tool_calls=[
                    ("remote", "remote-call", {"value": "headless tool works"}),
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
    argv = ["inspect the fixture", "--agent", "Worker", "--workdir", str(tmp_path), "--json"]

    assert run_cli.main(argv) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["result"] == "initial done"
    assert connect.call_count == scan.call_count == 1
    assert child_factory.call_count == 3
    assert {"Child0", "Child1", "Child2", "load_skill"} <= _tools_sent(initial)
    assert "remote" not in _tools_sent(initial)

    connect.return_value = good
    (saved_initialization / "demo" / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Updated catalog\n---\nChanged instructions.\n", encoding="utf-8"
    )
    assert run_cli.main(argv) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["result"] == "recovered done"
    assert connect.call_count == scan.call_count == 2
    assert child_factory.call_count == 6
    assert "headless tool works" in _tool_result_text(recovered)
    assert "Changed instructions." in _tool_result_text(recovered)
    assert good.exit_count == 1
