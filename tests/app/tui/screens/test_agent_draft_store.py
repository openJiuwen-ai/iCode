# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for agent draft-store validation."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from chrys.app.tui.screens.agents.agent_draft_store import (
    AgentDraftStore,
    validate_acp_draft,
    validate_agent_profile_draft,
    validate_mcp_draft,
)
from chrys.service.profiles.agents.schema import (
    AcpAgentConfig,
    AgentProfile,
    CompactionConfig,
    MCPServerConfig,
    ModelConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
    ToolsConfig,
)


@dataclass
class _Draft:
    key: str
    original_name: str | None
    profile: AgentProfile
    dirty: bool


def _profile(name: str, *, sub_agent_only: bool = False) -> AgentProfile:
    return AgentProfile(
        name=name,
        display_name=f"{name} Display",
        description=f"{name} description",
        instructions=f"{name} instructions",
        sub_agent_only=sub_agent_only,
    )


def test_agent_draft_store_rejects_empty_graph() -> None:
    store = AgentDraftStore([], [])

    assert store.validate() == ["At least one agent profile is required."]


def test_agent_draft_store_rejects_dirty_duplicate_name_against_registry() -> None:
    draft = _Draft(
        key="draft:1",
        original_name=None,
        profile=_profile("Existing"),
        dirty=True,
    )
    store = AgentDraftStore([draft], ["Existing"])

    assert store.validate() == ["A profile named 'Existing' already exists (names are case-insensitive)."]


def test_agent_draft_store_allows_sub_agent_ref_to_clean_visible_draft() -> None:
    parent = _profile("Parent")
    parent.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Child")])
    child = _profile("Child", sub_agent_only=True)
    store = AgentDraftStore(
        [
            _Draft("profile:Parent", "Parent", parent, dirty=True),
            _Draft("profile:Child", "Child", child, dirty=False),
        ],
        ["Parent", "Child"],
    )

    assert store.validate() == []


def test_agent_draft_store_validates_dirty_profile_sections() -> None:
    profile = _profile("Parent")
    profile.display_name = ""
    profile.description = ""
    profile.instructions = ""
    profile.model = ModelConfig(profile_id="missing-model")
    profile.sub_agents = SubAgentsConfig(
        max_total_concurrency=0,
        agents=[
            SubAgentRef(profile="Missing", tool_name="bad-name!", max_concurrency=0),
            SubAgentRef(profile="Missing", tool_name="bad-name!", max_concurrency=2),
        ],
    )
    profile.compaction = CompactionConfig(last_words_max_output_tokens=42)
    profile.skills = SkillsConfig(paths=["skills", "skills/"], script_timeout=0)
    profile.tools = ToolsConfig(
        mcp=[
            MCPServerConfig(
                name="remote",
                transport="http",
                url="ftp://example.test/mcp",
                request_timeout=0,
                tool_name_prefix="bad prefix",
            )
        ]
    )
    store = AgentDraftStore(
        [_Draft("profile:Parent", "Parent", profile, dirty=True)],
        ["Parent"],
        model_profile_exists=lambda _profile_id: False,
        workspace_cwd="/repo",
    )

    errors = store.validate()

    assert "Parent: display name is required." in errors
    assert "Parent: description is required." in errors
    assert "Parent: instructions are required." in errors
    assert "Parent: selected model profile no longer exists." in errors
    assert "Parent: max total concurrency must be a positive integer." in errors
    assert "Parent: Sub-Agent 1: profile 'Missing' does not exist." in errors
    assert any("Parent: Sub-Agent 1: tool name must be a valid identifier" in error for error in errors)
    assert "Parent: duplicate sub-agent profile: 'Missing'." in errors
    assert "Parent: duplicate sub-agent tool name: 'bad-name!'." in errors
    assert "Parent: Sub-Agent 1: max concurrency must be a positive integer." in errors
    assert "Parent: last words max output tokens must be between 1000 and 64000." in errors
    assert any("Parent: Skill Directory 2: 'skills/' duplicates Skill Directory 1" in error for error in errors)
    assert "Parent: skill script timeout must be a positive integer." in errors
    assert "Parent: Server 'remote': URL must start with http:// or https://." in errors
    assert "Parent: Server 'remote': timeout must be a positive integer." in errors
    assert any("Parent: Server 'remote': tool name prefix" in error for error in errors)


@pytest.mark.parametrize(
    ("tool_name_prefix", "progressive", "message"),
    [
        ("bad prefix", False, "tool name prefix"),
        ("github.v1", False, "underscores, and hyphens"),
        ("a" * 50, True, "invalid generated control"),
    ],
)
def test_validate_mcp_draft_rejects_invalid_tool_name_prefix(
    tool_name_prefix: str,
    progressive: bool,
    message: str,
) -> None:
    profile = _profile("Prefix")
    profile.tools = ToolsConfig(
        mcp=[
            MCPServerConfig(
                name="remote",
                transport="http",
                url="https://api.example.com/mcp",
                tool_name_prefix=tool_name_prefix,
                use_progressive_disclosure=progressive,
            )
        ]
    )

    errors = validate_mcp_draft("Prefix Test", profile)

    assert any(message in error for error in errors)


def test_validate_mcp_draft_rejects_invalid_always_load_entries() -> None:
    profile = _profile("Progressive")
    profile.tools = ToolsConfig(
        mcp=[
            MCPServerConfig(
                name="remote",
                transport="http",
                url="https://api.example.com/mcp",
                use_progressive_disclosure=True,
                always_load=["search", " ", "search"],
            )
        ]
    )

    errors = validate_mcp_draft("Progressive Test", profile)

    assert any("Initially Visible Tools must not contain an empty name" in error for error in errors)
    assert any("Initially Visible Tools contains duplicate tool name 'search'" in error for error in errors)


def test_validate_mcp_draft_enforces_loading_policy_hierarchy() -> None:
    profile = _profile("Progressive")
    profile.tools = ToolsConfig(
        mcp=[
            MCPServerConfig(
                name="remote",
                transport="http",
                url="https://api.example.com/mcp",
                allowed_tools=["search"],
                use_progressive_disclosure=True,
                always_load=["write"],
            )
        ]
    )

    errors = validate_mcp_draft("Progressive Test", profile)

    assert any("always_load tools must also appear in allowed_tools: write" in error for error in errors)


def test_validate_acp_draft_accepts_secondary_workspace_root_and_empty_instructions(tmp_path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    profile = _profile("External", sub_agent_only=True)
    profile.instructions = ""
    profile.acp = AcpAgentConfig(command="acp-agent", cwd=str(secondary))

    errors = validate_agent_profile_draft(
        "External",
        profile,
        {"External"},
        workspace_cwd=str(primary),
        workspace_roots=[str(primary), str(secondary)],
    )

    assert errors == []


def test_validate_acp_draft_enforces_launch_types_bounds_and_workspace_containment(tmp_path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    profile = _profile("External", sub_agent_only=True)
    profile.acp = AcpAgentConfig(
        command="agent\0bad",
        args=["ok", "bad\0arg"],
        env={
            "1BAD": "value",
            "Path": "one",
            "PATH": "two",
            "chrys_acp_subagent_depth": "3",
            "NUL": "bad\0value",
        },
        cwd=str(outside),
        config_options={"good": True, "bad": 3},  # type: ignore[dict-item]
        result_mode="unknown",  # type: ignore[arg-type]
        handshake_timeout_seconds=False,
        idle_timeout_seconds=float("inf"),
    )

    errors = validate_acp_draft(
        "External",
        profile,
        workspace_cwd=str(root),
        workspace_roots=[str(root)],
    )

    assert any("executable must not contain" in error for error in errors)
    assert any("arguments must not contain" in error for error in errors)
    assert any("name '1BAD' is invalid" in error for error in errors)
    assert any("case-insensitive duplicate" in error for error in errors)
    assert any("CHRYS_ACP_SUBAGENT_DEPTH" in error and "reserved" in error for error in errors)
    assert any("environment value for 'NUL'" in error for error in errors)
    assert any("outside all workspace roots" in error for error in errors)
    assert any("config option 'bad'" in error for error in errors)
    assert any("result mode" in error for error in errors)
    assert any("handshake timeout must be a finite number" in error for error in errors)
    assert any("idle timeout must be finite" in error for error in errors)


def test_validate_acp_draft_takes_commands_in_arguments_and_environment(tmp_path) -> None:
    profile = _profile("External", sub_agent_only=True)
    profile.acp = AcpAgentConfig(command="agent", args=["--key={{!tool get}}"], env={"TOKEN": "{{!tool get}}"})

    assert validate_acp_draft("External", profile, workspace_cwd=str(tmp_path), workspace_roots=[str(tmp_path)]) == []


@pytest.mark.parametrize("field", ["command", "cwd"])
def test_validate_acp_draft_refuses_commands_for_what_runs_and_where(tmp_path, field: str) -> None:
    profile = _profile("External", sub_agent_only=True)
    profile.acp = AcpAgentConfig(**{"command": "agent", field: "{{!tool --password hunter2}}"})

    errors = validate_acp_draft("External", profile, workspace_cwd=str(tmp_path), workspace_roots=[str(tmp_path)])

    assert any("not supported" in error for error in errors)
    assert not any("hunter2" in error for error in errors)


def test_validate_acp_draft_expands_cwd_templates_before_containment(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    profile = _profile("External", sub_agent_only=True)
    profile.acp = AcpAgentConfig(command="agent", cwd="{{CHRYS_ACP_TEST_CWD}}")

    monkeypatch.setenv("CHRYS_ACP_TEST_CWD", str(outside))
    errors = validate_acp_draft("External", profile, workspace_cwd=str(root), workspace_roots=[str(root)])
    assert any("outside all workspace roots" in error for error in errors)

    monkeypatch.delenv("CHRYS_ACP_TEST_CWD")
    errors = validate_acp_draft("External", profile, workspace_cwd=str(root), workspace_roots=[str(root)])
    assert any("CHRYS_ACP_TEST_CWD" in error for error in errors)

    monkeypatch.setenv("CHRYS_ACP_TEST_CWD", str(root / "inner"))
    errors = validate_acp_draft("External", profile, workspace_cwd=str(root), workspace_roots=[str(root)])
    assert not [error for error in errors if "working directory" in error]


def test_validate_acp_draft_reports_unresolvable_templates_in_command_args_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHRYS_ACP_TEST_MISSING", raising=False)
    profile = _profile("External", sub_agent_only=True)
    profile.acp = AcpAgentConfig(
        command="{{CHRYS_ACP_TEST_MISSING}}/agent",
        args=["--flag", "{{CHRYS_ACP_TEST_MISSING}}"],
        env={"TOKEN": "{{CHRYS_ACP_TEST_MISSING}}"},
    )

    errors = validate_acp_draft("External", profile)

    assert sum("CHRYS_ACP_TEST_MISSING" in error for error in errors) == 3
    assert any("ACP command" in error for error in errors)
    assert any("ACP argument" in error for error in errors)
    assert any("ACP environment 'TOKEN'" in error for error in errors)

    monkeypatch.setenv("CHRYS_ACP_TEST_MISSING", "resolved")
    assert validate_acp_draft("External", profile) == []


def test_validate_acp_draft_rejects_model_driven_sections() -> None:
    profile = _profile("External", sub_agent_only=True)
    profile.acp = AcpAgentConfig(command="agent")
    profile.tools = ToolsConfig(
        builtins=["read_file"],
        custom=["custom"],
        mcp=[MCPServerConfig(name="remote", transport="http", url="https://example.test")],
    )
    profile.sub_agents = SubAgentsConfig(agents=[SubAgentRef(profile="Other")])

    errors = validate_acp_draft("External", profile)

    assert "External: ACP profiles cannot configure nested sub-agents." in errors
    assert "External: ACP profiles cannot configure MCP servers." in errors
    assert "External: ACP profiles cannot configure custom tools." in errors
    assert "External: ACP profiles cannot configure built-in tools." in errors
