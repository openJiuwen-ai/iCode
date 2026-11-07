# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chrys.foundation.reporting.frames import canonical_json_bytes, sha256_hex
from chrys.service.reporting.agent_config import (
    MAX_PAYLOAD_BYTES,
    SCHEMA_NAME,
    SessionFacts,
    agent_environment_payload,
    collect_agent_environment,
    hooks_settings_digest,
    is_sensitive_key,
    load_profile,
    mcp_server_facts,
    model_profile_document,
    platform_string,
    redact_value,
)

PROFILE = """\
# The reviewer profile, one way of writing it.
name: reviewer
display_name: Reviewer
api_key: sk-live-should-never-ship
model:
  provider: anthropic
  maxTokens: 4096
  temperature: 0.2
tools:
  mcp:
    - name: filesystem
      transport: stdio
      command: npx
      args: [-y, "@modelcontextprotocol/server-filesystem", /workspace]
      env:
        MCP_TOKEN: hunter2
    - name: docs
      transport: http
      url: https://docs.internal.example/mcp
      headers:
        authorization: Bearer live-token
skills:
  paths: [skills/shared]
"""


def _write_profile(config_dir: Path, text: str, name: str = "Reviewer.yaml") -> None:
    agents = config_dir / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / name).write_text(text, encoding="utf-8")


def _facts(
    *,
    skills: tuple[tuple[str, str], ...] = (),
    hooks_count: int = 0,
    hook_events: tuple[str, ...] = (),
    hook_sources: tuple[tuple[str, str], ...] = (),
) -> SessionFacts:
    return SessionFacts(
        model_profile_id="11111111-1111-1111-1111-111111111111",
        model_provider="anthropic",
        model_id="claude",
        skills=skills,
        hooks_count=hooks_count,
        hook_events=hook_events,
        hook_sources=hook_sources,
    )


def test_credential_and_connection_keys_are_matched_by_segment() -> None:
    for key in ("api_key", "apiKey", "APIKey", "api-key", "access_token", "private_key", "refreshToken"):
        assert is_sensitive_key(key), key
    for key in ("command", "args", "env", "headers", "url", "base_url", "baseUrl", "endpoint", "proxy"):
        assert is_sensitive_key(key), key
    # Modelling parameters are configuration, not secrets: the plural segment
    # ``tokens`` never matches the singular ``token``.
    for key in ("maxTokens", "temperature", "topP", "max_tokens", "reasoning", "vision"):
        assert not is_sensitive_key(key), key


def test_redaction_replaces_values_with_their_subtrees_and_names_the_keys() -> None:
    document = {
        "api_key": "sk-live",
        "nested": {"headers": {"authorization": "Bearer x"}, "kept": 1},
        "flow": {"apiKey": "sk-live", "maxTokens": 4096},
        "list": [{"url": "https://x"}, {"name": "kept"}],
    }

    redacted, keys = redact_value(document)

    assert redacted == {
        "api_key": "<redacted>",
        "nested": {"headers": "<redacted>", "kept": 1},
        "flow": {"apiKey": "<redacted>", "maxTokens": 4096},
        "list": [{"url": "<redacted>"}, {"name": "kept"}],
    }
    assert sorted(keys) == ["apiKey", "api_key", "headers", "url"]
    assert document["api_key"] == "sk-live"  # the source is never mutated


def test_the_same_profile_written_two_ways_yields_the_same_payload(tmp_path: Path) -> None:
    reordered = """\
display_name: Reviewer
name: reviewer
skills:
  paths:
    - skills/shared
tools:
  mcp:
    - name: filesystem
      transport: stdio
      env: {MCP_TOKEN: hunter2}
      args: [-y, '@modelcontextprotocol/server-filesystem', /workspace]
      command: npx
    - headers: {authorization: Bearer live-token}
      transport: http
      url: https://docs.internal.example/mcp
      name: docs
model:
  temperature: 0.2
  maxTokens: 4096
  provider: anthropic
api_key: sk-live-should-never-ship
"""
    _write_profile(tmp_path, PROFILE)
    first = collect_agent_environment(_facts(), profile_name="reviewer", config_dir=tmp_path)
    (tmp_path / "agents" / "Reviewer.yaml").unlink()
    _write_profile(tmp_path, reordered)

    second = collect_agent_environment(_facts(), profile_name="reviewer", config_dir=tmp_path)

    first_payload, _ = agent_environment_payload(first, app_version="9.9.9")
    second_payload, _ = agent_environment_payload(second, app_version="9.9.9")
    assert first_payload == second_payload
    assert sha256_hex(first_payload) == sha256_hex(second_payload)


def test_the_real_layout_reaches_the_payload_without_plaintext(tmp_path: Path) -> None:
    """Acceptance: a real profile and hooks document produce a full, redacted capture."""
    _write_profile(tmp_path, PROFILE)
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "11111111-1111-1111-1111-111111111111.yaml").write_text(
        "id: 11111111-1111-1111-1111-111111111111\n"
        "name: Claude\n"
        "provider: anthropic\n"
        "model_id: claude\n"
        "api_key: sk-model-secret\n"
        "base_url: https://proxy.internal.example\n"
        "max_output_tokens: 8192\n"
        "thinking: auto\n",
        encoding="utf-8",
    )
    hooks = tmp_path / "hooks" / "hooks.yaml"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(
        "settings:\n"
        "  max_parallel_hooks: 2\n"
        "hooks:\n"
        "  - id: lint\n"
        "    event: after_tool_call\n"
        "    run: {type: shell, shell: 'ruff check'}\n"
        "  - id: notify\n"
        "    event: after_turn\n"
        "    run: {type: shell, shell: 'notify-send done'}\n",
        encoding="utf-8",
    )
    facts = _facts(
        skills=(("triage", str(tmp_path / "skills" / "triage")),),
        hooks_count=2,
        hook_events=("after_tool_call", "after_turn"),
        hook_sources=(("global", str(hooks)),),
    )
    (tmp_path / "skills" / "triage").mkdir(parents=True)
    (tmp_path / "skills" / "triage" / "SKILL.md").write_bytes(b"---\nname: triage\n---\nTriage things.\n")

    environment = collect_agent_environment(facts, profile_name="reviewer", config_dir=tmp_path)
    payload, redacted = agent_environment_payload(environment, app_version="9.9.9")
    document = json.loads(payload)

    # Acceptance 1: the real layout is non-empty and every section is captured.
    assert [server["name"] for server in document["mcp"]["servers"]] == ["filesystem", "docs"]
    assert document["hooks"] == {"count": 2, "events": ["after_tool_call", "after_turn"]}
    assert document["sections"] == {
        "profile": "captured",
        "model": "captured",
        "skills": "captured",
        "mcp": "captured",
        "hooks": "captured",
    }

    # Acceptance 2: no connection or credential plaintext leaves the machine.
    for plaintext in (
        b"sk-live-should-never-ship",
        b"sk-model-secret",
        b"hunter2",
        b"live-token",
        b"https://docs.internal.example/mcp",
        b"https://proxy.internal.example",
        b"@modelcontextprotocol/server-filesystem",
        b"ruff check",
        b"notify-send",
    ):
        assert plaintext not in payload, plaintext
    assert "api_key" in redacted
    assert "base_url" in redacted
    assert "headers" in redacted
    assert "command" in redacted
    assert "env" in redacted
    assert "url" in redacted

    # The MCP connection digest is of the unredacted connection facts.
    stdio_digest = sha256_hex(
        canonical_json_bytes(["npx", "-y", "@modelcontextprotocol/server-filesystem", "/workspace"])
    )
    http_digest = sha256_hex(canonical_json_bytes(["https://docs.internal.example/mcp"]))
    assert document["mcp"]["servers"][0]["connectionDigest"] == stdio_digest
    assert document["mcp"]["servers"][1]["connectionDigest"] == http_digest

    # The model section carries the identity and the non-identity parameters.
    assert document["model"]["profileId"] == "11111111-1111-1111-1111-111111111111"
    assert document["model"]["provider"] == "anthropic"
    assert document["model"]["modelId"] == "claude"
    assert document["model"]["params"] == {
        "api_key": "<redacted>",
        "base_url": "<redacted>",
        "max_output_tokens": 8192,
        "thinking": "auto",
    }

    # Skills carry a content digest of the SKILL.md bytes, never a name digest.
    assert document["skills"] == [
        {
            "name": "triage",
            "source": "config",
            "digest": hashlib.sha256(b"---\nname: triage\n---\nTriage things.\n").hexdigest(),
        }
    ]
    assert document["skills"][0]["digest"] != sha256_hex(b"triage")
    assert document["settingsDigest"] == hooks_settings_digest((("global", str(hooks)),))
    assert document["schema"] == SCHEMA_NAME
    assert document["truncated"] is False


def test_sources_that_cannot_be_located_report_absent_instead_of_empty(tmp_path: Path) -> None:
    environment = collect_agent_environment(_facts(), profile_name="absent", config_dir=tmp_path)

    assert environment.profile_found is False
    assert environment.model_found is False
    payload, _redacted = agent_environment_payload(environment, app_version="1.0.0")
    document = json.loads(payload)

    assert document["profile"] == {"name": "absent", "yaml": ""}
    # The session's own roster is known (it arrived, empty): captured. The
    # profile and its model document were never located: absent.
    assert document["sections"] == {
        "profile": "absent",
        "model": "absent",
        "skills": "captured",
        "mcp": "absent",
        "hooks": "captured",
    }


def test_a_profile_without_mcp_is_captured_with_no_servers(tmp_path: Path) -> None:
    _write_profile(tmp_path, "name: reviewer\ntemperature: 0.2\n")

    environment = collect_agent_environment(_facts(), profile_name="reviewer", config_dir=tmp_path)

    assert environment.profile_found is True
    assert environment.mcp_servers == ()


def test_a_session_without_runtime_facts_reports_its_sections_absent(tmp_path: Path) -> None:
    _write_profile(tmp_path, "name: reviewer\n")

    environment = collect_agent_environment(None, profile_name="reviewer", config_dir=tmp_path)
    payload, _redacted = agent_environment_payload(environment, app_version="1.0.0")
    document = json.loads(payload)

    assert document["model"] == {"profileId": "", "provider": "", "modelId": "", "params": {}}
    assert document["sections"]["skills"] == "absent"
    assert document["sections"]["hooks"] == "absent"
    assert document["sections"]["model"] == "absent"


def test_a_profile_is_matched_by_stem_then_by_its_document_name(tmp_path: Path) -> None:
    _write_profile(tmp_path, "name: reviewer\n", name="Other.yaml")
    _write_profile(tmp_path, "name: qa\n", name="QA.yml")

    assert load_profile("other", tmp_path) is not None  # stem match, case-insensitive
    assert load_profile("reviewer", tmp_path) is not None  # document name fallback
    assert load_profile("absent", tmp_path) is None
    assert load_profile("", tmp_path) is None


def test_an_unparseable_profile_is_absent_not_raw(tmp_path: Path) -> None:
    _write_profile(tmp_path, "name: [broken\n")

    assert load_profile("reviewer", tmp_path) is None


def test_the_model_document_is_matched_by_id_then_by_stem(tmp_path: Path) -> None:
    models = tmp_path / "models"
    models.mkdir(parents=True)
    (models / "aaa.yaml").write_text("id: 22222222-2222-2222-2222-222222222222\nname: Other\n", encoding="utf-8")
    (models / "display-name.yaml").write_text("name: Display\n", encoding="utf-8")

    assert model_profile_document("22222222-2222-2222-2222-222222222222", tmp_path) is not None
    assert model_profile_document("display-name", tmp_path) is not None
    assert model_profile_document("33333333-3333-3333-3333-333333333333", tmp_path) is None
    assert model_profile_document("", tmp_path) is None


def test_mcp_digests_cover_stdio_commands_and_http_urls() -> None:
    document = {
        "tools": {
            "mcp": [
                {"name": "stdio", "transport": "stdio", "command": "npx", "args": ["-y", "server"]},
                {"name": "http", "transport": "http", "url": "https://example.test/mcp"},
                {"name": "no-transport", "command": "x"},
                "not-a-mapping",
            ]
        }
    }

    servers = mcp_server_facts(document)

    assert [server["name"] for server in servers] == ["stdio", "http"]
    assert servers[0]["connectionDigest"] == sha256_hex(canonical_json_bytes(["npx", "-y", "server"]))
    assert servers[1]["connectionDigest"] == sha256_hex(canonical_json_bytes(["https://example.test/mcp"]))
    assert mcp_server_facts(None) == ()
    assert mcp_server_facts({}) == ()


def test_skill_sources_are_classified_and_digested_from_their_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_skill = tmp_path / "skills" / "config-skill"
    config_skill.mkdir(parents=True)
    (config_skill / "SKILL.md").write_bytes(b"config skill body")
    linked_home = tmp_path / "agents-home"
    linked_skill = linked_home / "skills" / "linked-skill"
    linked_skill.mkdir(parents=True)
    (linked_skill / "SKILL.md").write_bytes(b"linked skill body")
    import chrys.service.skills.adapter as skills_adapter

    monkeypatch.setattr(skills_adapter, "user_agents_dir", lambda: linked_home)

    from chrys.service.reporting.agent_config import skill_facts

    facts = skill_facts(
        (
            ("config-skill", str(config_skill)),
            ("linked-skill", str(linked_skill)),
            ("inline-skill", ""),
            ("project-skill", str(tmp_path / "project" / ".agents" / "skills" / "project-skill")),
        ),
        tmp_path,
    )

    assert [(fact.name, fact.source) for fact in facts] == [
        ("config-skill", "config"),
        ("linked-skill", "linked"),
        ("inline-skill", ""),
        ("project-skill", ""),
    ]
    assert facts[0].digest == sha256_hex(b"config skill body")
    assert facts[1].digest == sha256_hex(b"linked skill body")
    assert facts[2].digest == ""


def test_the_hooks_settings_digest_covers_the_explicit_section(tmp_path: Path) -> None:
    hooks = tmp_path / "hooks.yaml"
    hooks.write_text("settings:\n  max_parallel_hooks: 2\nhooks: []\n", encoding="utf-8")
    other = tmp_path / "other.yaml"
    other.write_text("settings:\n  max_parallel_hooks: 9\nhooks: []\n", encoding="utf-8")

    digest = hooks_settings_digest((("global", str(hooks)),))
    project_wins = hooks_settings_digest((("global", str(hooks)), ("project", str(other))))
    empty = hooks_settings_digest(())

    assert digest != empty
    assert project_wins == hooks_settings_digest((("project", str(other)),))
    assert project_wins != digest


def test_the_platform_string_normalizes_the_architecture() -> None:
    os_name, _, arch = platform_string().partition("-")

    assert os_name in {"macos", "windows", "linux"}
    assert arch in {"amd64", "arm64", "ia32"}


def test_an_oversized_environment_degrades_instead_of_shipping(tmp_path: Path) -> None:
    _write_profile(
        tmp_path, "name: reviewer\n" + "".join(f"note-{index:05d}: value {index}\n" for index in range(9_000))
    )
    facts = _facts(skills=tuple((f"skill-{index:06d}", "") for index in range(8_000)))

    environment = collect_agent_environment(facts, profile_name="reviewer", config_dir=tmp_path)
    payload, _redacted = agent_environment_payload(environment, app_version="1.0.0")
    document = json.loads(payload)

    assert len(payload) <= MAX_PAYLOAD_BYTES
    assert document["truncated"] is True
    assert document["profile"] == {"name": "reviewer", "yaml": ""}
    assert document["skills"] == []
    assert document["model"]["params"] == {}
