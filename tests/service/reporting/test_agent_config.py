# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

from chrys.service.reporting.agent_config import (
    MAX_PAYLOAD_BYTES,
    SCHEMA_NAME,
    AgentEnvironment,
    agent_environment_payload,
    command_digest,
    platform_string,
    profile_yaml_text,
    redact_profile_text,
)


def test_credential_values_are_replaced_and_named() -> None:
    profile = "\n".join(
        [
            "name: reviewer",
            "api_key: sk-live-should-never-ship",
            "model:",
            "  provider: anthropic",
            "  max_tokens: 4096",
            "headers:",
            "  x-secret: nope",
            "  x-other: nope",
            "temperature: 0.2",
        ]
    )

    text, redacted = redact_profile_text(profile)

    assert "sk-live-should-never-ship" not in text
    assert "x-secret: nope" not in text
    assert "provider: anthropic" in text
    assert "temperature: 0.2" in text
    assert redacted == ["api_key", "headers"]


def test_the_payload_is_the_documented_whitelist_without_credentials() -> None:
    environment = AgentEnvironment(
        profile_name="reviewer",
        profile_yaml="name: reviewer\npassword: hunter2\n",
        model_profile_id="model-1",
        model_provider="anthropic",
        model_id="claude",
        model_params={"temperature": 0.2},
        tool_names=("read_file",),
        tool_kinds={"read_file": "fs"},
        skill_names=("triage",),
        settings_digest="digest",
    )

    payload, redacted = agent_environment_payload(environment, app_version="9.9.9")
    document = json.loads(payload)

    assert document["schema"] == SCHEMA_NAME
    assert document["appVersion"] == "9.9.9"
    assert document["platform"] == platform_string()
    assert document["profile"]["name"] == "reviewer"
    assert document["model"] == {
        "profileId": "model-1",
        "provider": "anthropic",
        "modelId": "claude",
        "params": {"temperature": 0.2},
    }
    assert document["tools"] == {"enabled": ["read_file"], "kinds": {"read_file": "fs"}}
    assert [skill["name"] for skill in document["skills"]] == ["triage"]
    assert document["settingsDigest"] == "digest"
    assert document["redacted"] == ["password"]
    assert document["truncated"] is False
    assert b"hunter2" not in payload
    assert redacted == ["password"]


def test_an_oversized_environment_degrades_instead_of_shipping() -> None:
    environment = AgentEnvironment(
        profile_name="reviewer",
        profile_yaml="x" * 64_000,
        tool_names=tuple(f"tool-{index:05d}-" + "y" * 40 for index in range(8_000)),
    )

    payload, _redacted = agent_environment_payload(environment, app_version="1.0.0")
    document = json.loads(payload)

    assert len(payload) <= MAX_PAYLOAD_BYTES
    assert document["truncated"] is True
    assert document["profile"] == {"name": "reviewer", "yaml": ""}
    assert document["tools"] == {"enabled": [], "kinds": {}}


def test_a_profile_body_is_read_from_the_config_directory(tmp_path: Path) -> None:
    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "Reviewer.yaml").write_text("name: reviewer\n", encoding="utf-8")

    assert profile_yaml_text("reviewer", tmp_path) == "name: reviewer\n"
    assert profile_yaml_text("absent", tmp_path) == ""
    assert profile_yaml_text("", tmp_path) == ""


def test_a_command_is_only_ever_reduced_to_its_digest() -> None:
    assert command_digest("") == ""
    assert command_digest("npx server --token secret") == command_digest("npx server --token secret")
    assert len(command_digest("npx server")) == 64


def test_the_platform_string_names_the_os_and_the_architecture() -> None:
    os_name, _, arch = platform_string().partition("-")

    assert os_name in {"macos", "windows", "linux"}
    assert arch
