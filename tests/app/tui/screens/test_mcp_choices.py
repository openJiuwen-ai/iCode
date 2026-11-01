# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline reuse grouping and conflict rules for MCP configuration choices."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from chrys.app.tui.screens.agents.mcp_choices import (
    MCPConflictKind,
    collect_mcp_choices,
    comparable_mcp_config,
    mcp_choice_conflict,
)
from chrys.app.tui.screens.agents.mcp_picker import mcp_target_summary
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile, MCPServerConfig, ToolsConfig


def test_choices_group_equivalent_configs_and_sort_without_mutating_sources() -> None:
    server = MCPServerConfig(name="Foo", transport="stdio", command="uvx", args=["mcp"], env={"KEY": "{{!secret}}"})
    profiles = [
        AgentProfile(name="Code", tools=ToolsConfig(mcp=[server])),
        AgentProfile(
            name="Explore",
            sub_agent_only=True,
            tools=ToolsConfig(mcp=[replace(server, name="foo", url="ignored", headers={"ignored": "value"})]),
        ),
        AgentProfile(name="Other", tools=ToolsConfig(mcp=[replace(server, name="Foo", env={"KEY": "different"})])),
        AgentProfile(
            name="Versions", tools=ToolsConfig(mcp=[replace(server, name="mcp10"), replace(server, name="mcp2")])
        ),
    ]
    before = deepcopy(profiles)

    choices = collect_mcp_choices(profiles)

    assert len(choices.choices) == 4
    assert [choice.config.name for choice in choices.choices] == ["Foo", "Foo", "mcp2", "mcp10"]
    assert choices.choices[0].sources == ("Code", "Explore")
    assert choices.choices[1].sources == ("Other",)
    assert profiles == before
    choices.choices[0].config.env["KEY"] = "changed"
    choices.choices[0].config.args.append("changed")
    assert profiles == before


def test_choices_filter_individual_invalid_servers_and_acp_profiles() -> None:
    valid = MCPServerConfig(
        name="good", transport="http", url="https://example.com/mcp", headers={"TOKEN": "{{!secret}}"}
    )
    profile = AgentProfile(
        name="Code",
        display_name="Coding",
        tools=ToolsConfig(
            mcp=[valid, replace(valid, name="bad", url=""), replace(valid, name=" "), replace(valid, name="GOOD")]
        ),
    )
    acp = AgentProfile(name="External", acp=AcpAgentConfig(command="external"), tools=ToolsConfig(mcp=[valid]))

    result = collect_mcp_choices([profile, acp])

    assert result.hidden_count == 3
    assert len(result.choices) == 1
    assert result.choices[0].sources == ("Coding (Code)",)


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_comparison_ignores_inactive_transport_fields_and_name_case(transport: str) -> None:
    if transport == "stdio":
        clean = MCPServerConfig(name="Straße", transport="stdio", command="server", args=["run"])
        extra = replace(
            clean,
            name=" STRASSE ",
            url="https://ignored",
            headers={"key": "secret"},
            verify_ssl=False,
            bypass_proxy=True,
            resolve_header_templates=False,
            terminate_on_close=True,
        )
    else:
        clean = MCPServerConfig(name="Straße", transport="http", url="https://example.com/mcp")
        extra = replace(
            clean, name=" STRASSE ", command="ignored", args=["ignored"], env={"key": "secret"}, encoding="utf-16"
        )

    assert comparable_mcp_config(clean) == comparable_mcp_config(extra)
    conflict = mcp_choice_conflict(extra, [clean])
    assert conflict is not None and conflict.kind is MCPConflictKind.ADDED


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", False),
        ("description", "different"),
        ("load_prompts", False),
        ("expose_instructions", False),
        ("max_tool_result_tokens", 123),
        ("allowed_tools", []),
        ("headers", {"TOKEN": "different"}),
        ("verify_ssl", False),
        ("resolve_header_templates", False),
    ],
)
def test_comparison_preserves_effective_settings(field: str, value: object) -> None:
    original = MCPServerConfig(name="server", transport="http", url="https://example.com/mcp")
    changed = replace(original, **{field: value})
    assert comparable_mcp_config(original) != comparable_mcp_config(changed)
    conflict = mcp_choice_conflict(changed, [original])
    assert conflict is not None and conflict.kind is MCPConflictKind.NAME


@pytest.mark.parametrize(
    "left,right,conflicts",
    [
        (None, None, True),
        (None, ["read"], True),
        (["read"], None, True),
        (["read", "write"], ["write"], True),
        (["read"], ["write"], False),
        (["read"], ["gh_read"], False),
        ([], None, False),
        (None, [], False),
        ([], [], False),
    ],
)
@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_same_connection_intersections_ignore_enabled_state(
    left: list[str] | None,
    right: list[str] | None,
    conflicts: bool,
    enabled: bool,
    transport: str,
) -> None:
    original = (
        MCPServerConfig(name="gh", transport="stdio", command="npx", args=["github"])
        if transport == "stdio"
        else MCPServerConfig(name="gh", transport="http", url="https://example.com/mcp")
    )
    original = replace(original, allowed_tools=left, enabled=enabled)
    candidate = replace(original, name="github", allowed_tools=right, enabled=not enabled)
    conflict = mcp_choice_conflict(candidate, [original])
    if conflicts:
        assert conflict is not None
        assert conflict.kind is MCPConflictKind.CONNECTION
        assert conflict.existing_name == "gh"
    else:
        assert conflict is None
    assert mcp_choice_conflict(replace(candidate, tool_name_prefix="other"), [original]) is None


def test_different_connections_and_transports_remain_selectable() -> None:
    stdio = MCPServerConfig(name="local", transport="stdio", command="npx", args=["server-a"])
    assert mcp_choice_conflict(replace(stdio, name="other", args=["server-b"]), [stdio]) is None
    assert mcp_choice_conflict(replace(stdio, name="other", command="uvx"), [stdio]) is None
    http = MCPServerConfig(name="remote", transport="http", url="https://example.com/a")
    assert mcp_choice_conflict(http, [stdio]) is None
    assert mcp_choice_conflict(replace(http, name="other", url="https://example.com/b"), [http]) is None


def test_same_name_takes_precedence_over_connection_conflicts() -> None:
    existing = MCPServerConfig(name="gh", transport="stdio", command="npx", args=["server"])
    candidate = replace(existing, name="github")
    conflict = mcp_choice_conflict(candidate, [existing, replace(candidate, args=["different"])])
    assert conflict is not None and conflict.kind is MCPConflictKind.NAME


@pytest.mark.parametrize(
    "transport,field,value",
    [
        ("stdio", "timeout", "30"),
        ("stdio", "command", 123),
        ("stdio", "args", "server"),
        ("stdio", "args", [123]),
        ("http", "url", 123),
    ],
)
def test_malformed_yaml_servers_do_not_hide_valid_siblings(
    tmp_path: Path,
    transport: str,
    field: str,
    value: object,
) -> None:
    bad = {
        "name": "broken",
        "transport": transport,
        "command": "server",
        "url": "https://example.com/mcp",
        field: value,
    }
    good = {"name": "good", "transport": "stdio", "command": "valid-server"}
    path = tmp_path / "source.yaml"
    path.write_text(yaml.safe_dump({"name": "Code", "tools": {"mcp": [bad, good]}}), encoding="utf-8")
    source = load_profile_from_yaml(path)

    choices = collect_mcp_choices([source])

    assert choices.hidden_count == 1
    assert [choice.config.name for choice in choices.choices] == ["good"]
    assert len(source.tools.mcp) == 2


def test_summaries_hide_credentials_arguments_and_url_query() -> None:
    assert (
        mcp_target_summary(
            MCPServerConfig(name="x", transport="stdio", command=r"C:\tools\server.exe", args=["--token=secret"])
        )
        == "server.exe"
    )
    assert (
        mcp_target_summary(
            MCPServerConfig(name="x", transport="http", url="https://user:secret@example.com/path?token=secret#secret")
        )
        == "example.com"
    )
    assert mcp_target_summary(MCPServerConfig(name="x", transport="http", url="https://[broken")) == ""


@pytest.mark.parametrize(
    "command,expected",
    [
        ("npx -y @scope/server --token=abc", "npx"),
        ("/bin/server --token=secret/path", "server"),
        ("server\t--token=secret", "server"),
        ("   ", ""),
    ],
)
def test_summary_does_not_show_arguments_embedded_in_yaml_command(command: str, expected: str) -> None:
    assert mcp_target_summary(MCPServerConfig(name="x", transport="stdio", command=command)) == expected
