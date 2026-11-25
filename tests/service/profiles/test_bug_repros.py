# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Focused repro tests for profile/model/tool configuration bugs."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from chrys.service.approval.policy import ApprovalPolicy
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import ApprovalConfig


def test_approval_loader_promotes_mcp_override_for_dotted_tool_name(tmp_path: Path) -> None:
    """MCP tools may expose dotted names, and ``mcp.<tool>`` is still a kind-qualified key."""
    path = tmp_path / "agent.yaml"
    path.write_text(
        """\
name: dotted-mcp
approval:
  default: auto
  overrides:
    mcp.server.tool: require
""",
        encoding="utf-8",
    )

    profile = load_profile_from_yaml(path)

    assert profile.approval.overrides == {"mcp.server.tool": "require"}


def test_approval_policy_matches_kind_override_for_dotted_tool_name() -> None:
    """A runtime ``kind.tool_name`` override must match even when the tool name contains dots."""
    tools = [SimpleNamespace(name="server.tool", chrys_kind="mcp")]
    policy = ApprovalPolicy(
        ApprovalConfig(default="auto", overrides={"mcp.server.tool": "require"}),
        tools=tools,
    )

    assert policy.should_require_approval("server.tool")


def test_approval_policy_does_not_apply_absent_known_kind_override_to_other_kind() -> None:
    """A known kind-qualified override must not fall through to a same-named tool of another kind."""
    tools = [SimpleNamespace(name="server.tool", chrys_kind="filesystem.write")]
    policy = ApprovalPolicy(
        ApprovalConfig(default="require", overrides={"mcp.server.tool": "skip"}),
        tools=tools,
    )

    assert policy.should_require_approval("server.tool")


def test_text_false_user_can_override_does_not_allow_a_session_override(tmp_path: Path) -> None:
    """``"false"`` is a true value in Python; the reader must take the word, not the string."""
    path = tmp_path / "text-false.yaml"
    path.write_text(
        """\
name: text-false
approval:
  user_can_override: "false"
""",
        encoding="utf-8",
    )

    policy = ApprovalPolicy(load_profile_from_yaml(path).approval)

    assert policy.set_user_override("require") is False


def test_profile_with_non_text_display_name_is_not_registered(tmp_path: Path) -> None:
    """A refused profile never reaches the registry, so a selector miss stays a miss."""
    (tmp_path / "good.yaml").write_text("name: good\n", encoding="utf-8")
    (tmp_path / "bad.yaml").write_text("name: bad\ndisplay_name: 2024\n", encoding="utf-8")
    registry = AgentProfileRegistry()

    assert registry.load_user_profiles(tmp_path) == 1
    assert registry.resolve_selector("2024") is None
    assert registry.resolve_selector("good") is not None
