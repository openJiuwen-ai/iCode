# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP agent-profile parsing, serialization, and effective fingerprint tests."""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

import pytest

from chrys.service.profiles.agents.loader import AgentProfileLoadError, _parse_acp, load_profile_from_yaml
from chrys.service.profiles.agents.schema import AcpAgentConfig, AgentProfile
from chrys.service.profiles.agents.serializer import profile_to_dict, save_profile
from chrys.service.session.persistence import agent_profile_context_fingerprint


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("command", 1),
        ("cwd", False),
        ("session_mode", []),
        ("model_id", {}),
        ("args", "x"),
        ("args", [1]),
        ("env", []),
        ("env", {"OK": 1}),
        ("config_options", []),
        ("config_options", {"x": 1}),
        ("result_mode", "other"),
        ("allow_external_cwd", 1),
        ("best_effort_options", "false"),
    ],
)
def test_parse_acp_rejects_each_wrong_field_type(field: str, value: object) -> None:
    with pytest.raises(AgentProfileLoadError):
        _parse_acp({field: value})


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("handshake_timeout_seconds", True, 30.0),
        ("handshake_timeout_seconds", 0, 30.0),
        ("handshake_timeout_seconds", float("inf"), 30.0),
        # YAML ints are arbitrary-precision; float() overflow must degrade,
        # not abort registry loading.
        ("handshake_timeout_seconds", 10**400, 30.0),
        ("idle_timeout_seconds", False, 600.0),
        ("idle_timeout_seconds", -1, 600.0),
        ("idle_timeout_seconds", float("nan"), 600.0),
        ("idle_timeout_seconds", 10**400, 600.0),
        ("idle_timeout_seconds", 0, 0.0),
    ],
)
def test_parse_acp_timeout_validation_rejects_bool_before_numeric(
    field: str,
    value: object,
    expected: float,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        parsed = _parse_acp({field: value})
    assert getattr(parsed, field) == expected
    if value != 0 or type(value) is bool:
        assert field in caplog.text


@pytest.mark.parametrize(
    "env",
    [
        {"1BAD": "x"},
        {"Path": "a", "PATH": "b"},
        {"chrys_acp_subagent_depth": "4"},
        {"OK": "nul\0value"},
    ],
)
def test_parse_acp_rejects_unsafe_environment(env: dict[str, str]) -> None:
    with pytest.raises(AgentProfileLoadError):
        _parse_acp({"env": env})


def test_acp_profile_forces_sub_agent_only_and_resets_conflicting_sections(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "external.yaml"
    path.write_text(
        """\
name: external
sub_agent_only: false
instructions: ignore me
tools:
  builtins: [filesystem.read]
extensions: [acme_release]
skills:
  paths: [skills]
sub_agents:
  agents:
    - profile: nested
memory:
  files: [MEMORY.md]
compaction:
  last_words_max_output_tokens: 10
approval:
  default: skip
model:
  profile_id: model-id
acp:
  command: external-agent
""",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        profile = load_profile_from_yaml(path)
    assert profile.sub_agent_only is True
    assert profile.instructions == ""
    assert profile.tools.builtins == []
    assert profile.extensions == []
    assert profile.skills.paths == []
    assert profile.sub_agents.agents == []
    assert profile.memory.files == []
    assert profile.model.profile_id == ""
    for section in (
        "tools",
        "extensions",
        "skills",
        "sub_agents.agents",
        "memory",
        "compaction",
        "instructions",
        "approval",
        "model.profile_id",
    ):
        assert section in caplog.text


def test_empty_acp_discriminator_and_empty_command_round_trip(tmp_path: Path) -> None:
    profile = AgentProfile(name="external", acp=AcpAgentConfig(), sub_agent_only=True)
    assert profile_to_dict(profile)["acp"] == {}
    path = save_profile(profile, target_dir=tmp_path)
    restored = load_profile_from_yaml(path)
    assert restored.acp is not None
    assert restored.acp.command == ""


def test_effective_sub_profile_change_invalidates_parent_fingerprint() -> None:
    parent = AgentProfile(name="parent")
    external = AgentProfile(
        name="external",
        sub_agent_only=True,
        acp=AcpAgentConfig(command="agent", args=["--mode", "one"]),
    )
    first = [{"resolved_profile": asdict(external), "state": "registered"}]
    fingerprint = agent_profile_context_fingerprint(parent, effective_sub_agents=first)
    external.acp.args[-1] = "two"
    second = [{"resolved_profile": asdict(external), "state": "registered"}]
    assert agent_profile_context_fingerprint(parent, effective_sub_agents=second) != fingerprint
    second[0]["state"] = "depth_limit"
    depth_skipped = agent_profile_context_fingerprint(parent, effective_sub_agents=second)
    assert depth_skipped != fingerprint
