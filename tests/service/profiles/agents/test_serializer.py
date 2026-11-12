# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for agent profile serialization."""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    ApprovalConfig,
    CompactionConfig,
    MCPServerConfig,
    ToolsConfig,
)
from chrys.service.profiles.agents.serializer import delete_profile, profile_to_dict, save_profile


def test_profile_to_dict_serializes_mcp_timeout_and_headers() -> None:
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(
                    name="remote-api",
                    transport="http",
                    url="http://localhost:8080/mcp",
                    headers={"Authorization": "Bearer token"},
                    resolve_header_templates=False,
                    allowed_tools=["echo"],
                    tool_name_prefix="remote",
                    request_timeout=30,
                    max_tool_result_tokens=1234,
                    bypass_proxy=True,
                    env={"SSL_CERT_FILE": "/tmp/ca.pem"},
                    enabled=False,
                )
            ]
        ),
    )

    data = profile_to_dict(profile)
    mcp = data["tools"]["mcp"][0]
    assert mcp["headers"] == {"Authorization": "Bearer token"}
    assert mcp["resolve_header_templates"] is False
    assert mcp["allowed_tools"] == ["echo"]
    assert mcp["tool_name_prefix"] == "remote"
    assert mcp["timeout"] == 30
    assert mcp["max_tool_result_tokens"] == 1234
    assert mcp["bypass_proxy"] is True
    assert mcp["enabled"] is False
    assert "env" not in mcp
    assert "request_timeout" not in mcp


def test_profile_to_dict_serializes_stdio_mcp_env() -> None:
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(
                    name="local",
                    transport="stdio",
                    command="python",
                    env={"API_KEY": "secret"},
                )
            ]
        ),
    )

    data = profile_to_dict(profile)
    assert data["tools"]["mcp"][0]["env"] == {"API_KEY": "secret"}


def test_profile_to_dict_omits_unspecified_mcp_allowed_tools() -> None:
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="local", transport="stdio", command="python")]),
    )

    data = profile_to_dict(profile)
    mcp = data["tools"]["mcp"][0]
    assert "allowed_tools" not in mcp


def test_profile_to_dict_serializes_empty_mcp_allowed_tools() -> None:
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="local", transport="stdio", command="python", allowed_tools=[])]),
    )

    data = profile_to_dict(profile)
    mcp = data["tools"]["mcp"][0]
    assert mcp["allowed_tools"] == []


def test_profile_to_dict_serializes_progressive_mcp_options() -> None:
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(
                    name="local",
                    transport="stdio",
                    command="python",
                    use_progressive_disclosure=True,
                    always_load=["search", "read"],
                )
            ]
        ),
    )

    mcp = profile_to_dict(profile)["tools"]["mcp"][0]

    assert mcp["use_progressive_disclosure"] is True
    assert mcp["always_load"] == ["search", "read"]


def test_profile_to_dict_omits_default_progressive_mcp_options() -> None:
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="local", transport="stdio", command="python")]),
    )

    mcp = profile_to_dict(profile)["tools"]["mcp"][0]

    assert "use_progressive_disclosure" not in mcp
    assert "always_load" not in mcp


def test_progressive_mcp_options_round_trip_through_yaml(tmp_path: Path) -> None:
    profile = AgentProfile(
        name="progressive-round-trip",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(
                    name="local",
                    transport="stdio",
                    command="python",
                    use_progressive_disclosure=True,
                    always_load=["search", "read"],
                )
            ]
        ),
    )

    loaded = load_profile_from_yaml(save_profile(profile, target_dir=tmp_path))
    config = loaded.tools.mcp[0]

    assert config.use_progressive_disclosure is True
    assert config.always_load == ["search", "read"]


def test_last_words_supplement_round_trips(tmp_path: Path) -> None:
    profile = AgentProfile(
        name="last-words-supplement",
        compaction=CompactionConfig(last_words_template="Custom supplementary guidance"),
    )

    path = save_profile(profile, target_dir=tmp_path)
    loaded = load_profile_from_yaml(path)
    serialized = profile_to_dict(profile)

    assert serialized["compaction"]["last_words_template"] == "Custom supplementary guidance"
    assert set(serialized["compaction"]) == {"last_words_template"}
    assert loaded.compaction.last_words_template == "Custom supplementary guidance"


def test_compaction_schema_contains_only_current_configuration_fields() -> None:
    assert [field.name for field in fields(CompactionConfig)] == [
        "enabled",
        "last_words_template",
        "last_words_max_output_tokens",
        "phase4_side_call_token_budget",
    ]


def test_internal_compaction_disable_is_never_serialized_and_reenables_on_reload(tmp_path: Path) -> None:
    profile = AgentProfile(
        name="programmatically-disabled",
        compaction=CompactionConfig(enabled=False, last_words_template="Keep this"),
    )

    serialized = profile_to_dict(profile)
    loaded = load_profile_from_yaml(save_profile(profile, target_dir=tmp_path))

    assert serialized["compaction"] == {"last_words_template": "Keep this"}
    assert loaded.compaction.enabled


def test_profile_to_dict_omits_default_expose_instructions() -> None:
    """Default expose_instructions=True is omitted from serialized output."""
    profile = AgentProfile(
        name="test-profile",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="local", transport="stdio", command="python")]),
    )

    mcp = profile_to_dict(profile)["tools"]["mcp"][0]
    assert "expose_instructions" not in mcp


def test_expose_instructions_false_round_trips_through_yaml(tmp_path: Path) -> None:
    profile = AgentProfile(
        name="instructions-round-trip",
        tools=ToolsConfig(
            mcp=[MCPServerConfig(name="local", transport="stdio", command="python", expose_instructions=False)]
        ),
    )

    loaded = load_profile_from_yaml(save_profile(profile, target_dir=tmp_path))

    assert loaded.tools.mcp[0].expose_instructions is False


def test_profile_to_dict_omits_enabled_when_true() -> None:
    """Default enabled=True is omitted from serialized output."""
    profile = AgentProfile(
        name="default-enabled",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="s", transport="http", url="http://localhost:8080/mcp")]),
    )

    data = profile_to_dict(profile)
    mcp = data["tools"]["mcp"][0]
    assert "enabled" not in mcp


def test_profile_to_dict_emits_id_after_name() -> None:
    """``id`` is always emitted and positioned right after ``name``."""
    profile = AgentProfile(name="agent-x", id="abc123def456")
    data = profile_to_dict(profile)
    keys = list(data.keys())
    assert keys[0] == "name"
    assert keys[1] == "id"
    assert data["id"] == "abc123def456"


def test_profile_to_dict_emits_id_even_when_empty() -> None:
    """Empty id is still emitted (defensive — registry should always set one)."""
    profile = AgentProfile(name="no-id")
    data = profile_to_dict(profile)
    assert data.get("id") == ""


def test_profile_to_dict_passes_approval_override_keys_through() -> None:
    """Approval-override keys serialize verbatim — bare kinds are both the
    runtime and the on-disk form (no prefix conversion)."""
    profile = AgentProfile(
        name="approval-test",
        approval=ApprovalConfig(
            default="auto",
            overrides={
                "shell": "require",
                "filesystem.write": "require",
                "filesystem.write.write_file": "auto",
                "read_file": "skip",  # bare tool name — pass through unchanged
            },
        ),
    )
    data = profile_to_dict(profile)
    assert data["approval"]["overrides"] == {
        "shell": "require",
        "filesystem.write": "require",
        "filesystem.write.write_file": "auto",
        "read_file": "skip",
    }


def test_profile_to_dict_default_approval_overrides_use_bare_form() -> None:
    """Default ApprovalConfig serializes with the bare kind keys."""
    profile = AgentProfile(name="default-approval")
    data = profile_to_dict(profile)
    overrides = data.get("approval", {}).get("overrides")
    assert overrides == {"shell": "require", "filesystem.write": "require", "todo": "skip"}


@pytest.mark.parametrize("name", ["../escape", "/abs", "a/b", "..", " padded"])
def test_save_and_delete_refuse_names_that_are_not_filename_safe(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="not filename-safe"):
        save_profile(AgentProfile(name=name), target_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []
    assert not (tmp_path.parent / "escape.yaml").exists()
    with pytest.raises(ValueError, match="not filename-safe"):
        delete_profile(name)


def test_a_search_provider_preset_is_saved_as_its_shorthand(tmp_path: Path) -> None:
    """Saving keeps ``preset:`` and only the overrides, so a later preset fix still reaches the profile."""
    from chrys.service.tools.builtins.web.config import parse_search

    search = parse_search(
        {
            "mode": "auto",
            "fallback_chain": ["sx", "sp"],
            "providers": {
                "sx": {"type": "custom_http", "preset": "searxng", "endpoint": "https://searx.example/search"},
                "sp": {"type": "custom_http", "preset": "serpapi"},
            },
        }
    )
    profile = AgentProfile(name="presets", tools=ToolsConfig(builtins=["web_search"], web_search=search))

    providers = profile_to_dict(profile)["tools"]["web_search"]["providers"]
    assert providers == {
        "sx": {"type": "custom_http", "preset": "searxng", "endpoint": "https://searx.example/search"},
        "sp": {"type": "custom_http", "preset": "serpapi"},
    }
    loaded = load_profile_from_yaml(save_profile(profile, target_dir=tmp_path))
    assert loaded.tools.web_search == search


def test_mcp_cwd_round_trips_and_is_omitted_when_empty(tmp_path: Path) -> None:
    profile = AgentProfile(
        name="cwd-round-trip",
        tools=ToolsConfig(
            mcp=[
                MCPServerConfig(name="set", transport="stdio", command="python", cwd="~/srv"),
                MCPServerConfig(name="unset", transport="stdio", command="python"),
            ]
        ),
    )

    serialized = profile_to_dict(profile)["tools"]["mcp"]
    first = save_profile(profile, target_dir=tmp_path / "one")
    loaded = load_profile_from_yaml(first)
    second = save_profile(loaded, target_dir=tmp_path / "two")

    assert serialized[0]["cwd"] == "~/srv"
    assert "cwd" not in serialized[1]
    assert [config.cwd for config in loaded.tools.mcp] == ["~/srv", ""]
    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_http_mcp_never_writes_cwd() -> None:
    profile = AgentProfile(
        name="http-cwd",
        tools=ToolsConfig(mcp=[MCPServerConfig(name="remote", transport="http", url="http://x/mcp", cwd="/srv")]),
    )

    assert "cwd" not in profile_to_dict(profile)["tools"]["mcp"][0]
