# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for ACP agent/model profile reads, masked-secret writes, resets, and deletes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.app.acp import session_manager as session_manager_module
from chrys.app.acp.session_manager import AcpSessionError, AcpSessionManager
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    MCPServerConfig,
    SubAgentRef,
    SubAgentsConfig,
)
from chrys.service.profiles.agents.serializer import save_profile as save_agent_profile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.models.schema import ModelProfile
from tests.app.acp._session_manager_fakes import (
    _acp_manager,
    _acp_profile,
    _manager,
    _mcp_profile,
    _profile_manager,
    _StaticListStore,
)
from tests.support.platform_fakes import platform_with_config_dir


@pytest.mark.parametrize("name", ["Code", "QA", "Explore", "General"])
def test_acp_builtin_prompt_writes_keep_package_prompts_and_other_settings(tmp_path, monkeypatch, name):
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    registry = AgentProfileRegistry()
    registry.load_all(tmp_path / "agents")
    manager = _acp_manager(registry)
    data = manager.read_agent_profile(name)
    data["instructions"] = "attempted system prompt override"
    data["compaction"]["last_words_template"] = "attempted memory prompt override"
    data["compaction"]["last_words_max_output_tokens"] = 7000
    result = manager.write_agent_profile(data)
    assert result["ignored_fields"] == ["instructions", "compaction.last_words_template"]
    fresh = AgentProfileRegistry()
    fresh.load_all(tmp_path / "agents")
    effective = fresh.get(name)
    template = fresh.get_builtin_template(name)
    assert effective.instructions == template.instructions
    assert effective.compaction.last_words_template == template.compaction.last_words_template
    assert effective.compaction.last_words_max_output_tokens == 7000
    assert not list((tmp_path / "agents").glob("*-Modified*.yaml"))


@pytest.mark.parametrize("payload", ["unchanged", "omitted", "one_changed", "custom"])
def test_acp_ignored_prompts_only_reports_explicit_builtin_changes(tmp_path, monkeypatch, payload):
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    registry = AgentProfileRegistry()
    registry.load_all(tmp_path / "agents")
    manager = _acp_manager(registry)
    data = manager.read_agent_profile("Code")
    if payload == "omitted":
        data.pop("instructions")
        data["compaction"].pop("last_words_template")
    elif payload == "one_changed":
        data["instructions"] = "new prompt"
    elif payload == "custom":
        data.update(name="Code-copy", id="custom-id", instructions="custom prompt")
    result = manager.write_agent_profile(data)
    assert result.get("ignored_fields", []) == (["instructions"] if payload == "one_changed" else [])
    if payload == "custom":
        assert load_profile_from_yaml(tmp_path / "agents" / "Code-copy.yaml").instructions == "custom prompt"


def test_acp_write_backs_up_legacy_prompts_before_replacing_user_settings(tmp_path, monkeypatch):
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    registry = AgentProfileRegistry()
    registry.load_all(tmp_path / "agents")
    old = registry.get_builtin_template("Code")
    old.instructions = "legacy user system prompt"
    old.compaction.last_words_template = "legacy user memory prompt"
    save_agent_profile(old)
    manager = _acp_manager(registry)
    data = manager.read_agent_profile("Code")
    data["description"] = "updated description"
    manager.write_agent_profile(data)
    backup = load_profile_from_yaml(tmp_path / "agents" / "Code-Modified.yaml")
    assert backup.instructions == old.instructions
    assert backup.compaction.last_words_template == old.compaction.last_words_template
    assert registry.get("Code").description == "updated description"


def test_read_agent_profile_masks_mcp_secrets() -> None:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(_mcp_profile())
    manager = _profile_manager("WithMcp", agent_registry)

    data = manager.read_agent_profile("WithMcp")

    servers = {server["name"]: server for server in data["tools"]["mcp"]}
    # Non-empty secret values are masked; empty stays empty; keys preserved.
    assert servers["remote"]["headers"] == {"Authorization": "***", "X-Empty": ""}
    assert servers["local"]["env"] == {"API_KEY": "***"}


def test_read_agent_profile_masks_acp_args_env_and_string_config_options() -> None:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(_acp_profile())
    manager = _acp_manager(agent_registry)

    data = manager.read_agent_profile("WithAcp")

    assert data["acp"]["args"] == ["***", "***", "***"]
    assert data["acp"]["env"] == {"API_KEY": "***", "EMPTY": "***"}
    assert data["acp"]["config_options"] == {"channel": "***", "telemetry": False}


def test_reset_agent_profile_preserves_integrations_without_cascade(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    agent_registry = AgentProfileRegistry()
    agent_registry.load_builtins()
    customized = agent_registry.get_builtin_template("Code")
    assert customized is not None
    customized.instructions = "custom shadow"
    customized.model.profile_id = "resettable-model"
    customized.skills.paths = ["private-skills"]
    customized.tools.mcp = [
        MCPServerConfig(
            name="private",
            transport="http",
            url="https://example.test",
            headers={"Authorization": "Bearer secret"},
        )
    ]
    customized.memory.files = ["private.md"]
    save_agent_profile(customized, target_dir=user_dir)

    agent_registry = AgentProfileRegistry()
    agent_registry.load_all(user_dir=user_dir)
    parent = AgentProfile(
        name="Parent",
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Code")]),
    )
    agent_registry.register(parent)
    manager = _profile_manager("Code", agent_registry)

    result = manager.reset_agent_profile("Code")

    restored = agent_registry.get("Code")
    assert result["changed"] is True
    assert result["profile"]["builtin"] is True
    assert restored is not None
    assert restored.instructions != "custom shadow"
    assert restored.skills.paths == ["private-skills"]
    assert restored.tools.mcp[0].headers == {"Authorization": "Bearer secret"}
    assert restored.memory.files == ["private.md"]
    assert (user_dir / "Code.yaml").exists()
    assert [ref.profile for ref in parent.sub_agents.agents] == ["Code"]

    assert manager.reset_agent_profile("Code")["changed"] is False


def test_reset_agent_profile_without_shadow_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    agent_registry = AgentProfileRegistry()
    agent_registry.load_all(user_dir=tmp_path / "agents")
    manager = _acp_manager(agent_registry)

    result = manager.reset_agent_profile("Code")

    assert result["changed"] is False
    assert result["profile"]["builtin"] is True
    profiles = {profile["name"]: profile for profile in manager.list_agent_profiles()}
    assert profiles["Code"]["builtin"] is True


def test_reset_agent_profile_exact_template_deletes_canonicalized_shadow(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    source_registry = AgentProfileRegistry()
    source_registry.load_builtins()
    customized = source_registry.get_builtin_template("Code")
    assert customized is not None
    customized.instructions = "custom shadow"
    customized.skills.script_extensions = sorted(customized.skills.script_extensions)
    save_agent_profile(customized, target_dir=user_dir)
    agent_registry = AgentProfileRegistry()
    agent_registry.load_all(user_dir=user_dir)
    manager = _acp_manager(agent_registry)

    result = manager.reset_agent_profile("Code")

    assert result["changed"] is True
    assert not (user_dir / "Code.yaml").exists()


def test_reset_agent_profile_removes_noncanonical_shadow_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A shadow stored as ``my-code.yaml`` must not resurrect the customization after restart."""
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    source_registry = AgentProfileRegistry()
    source_registry.load_builtins()
    customized = source_registry.get_builtin_template("Code")
    assert customized is not None
    customized.instructions = "custom shadow"
    save_agent_profile(customized, target_dir=user_dir)
    (user_dir / "Code.yaml").rename(user_dir / "my-code.yaml")
    agent_registry = AgentProfileRegistry()
    agent_registry.load_all(user_dir=user_dir)
    manager = _acp_manager(agent_registry)
    assert agent_registry.get("Code-Modified").instructions == "custom shadow"
    assert agent_registry.get("Code").instructions == agent_registry.get_builtin_template("Code").instructions

    result = manager.reset_agent_profile("Code")

    assert result["changed"] is True
    assert not (user_dir / "my-code.yaml").exists()
    assert sorted(p.name for p in user_dir.glob("*.y*ml")) == ["Code-Modified.yaml"]
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    assert fresh.get("Code").instructions != "custom shadow"


def test_delete_agent_profile_rejects_builtin(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    agent_registry = AgentProfileRegistry()
    agent_registry.load_all(user_dir=tmp_path / "agents")
    manager = _acp_manager(agent_registry)

    with pytest.raises(AcpSessionError, match="cannot be deleted"):
        manager.delete_agent_profile("Code")


def test_restore_masked_mcp_secrets_recovers_originals() -> None:
    existing = _mcp_profile()
    incoming = {
        "name": "WithMcp",
        "tools": {
            "mcp": [
                {"name": "remote", "headers": {"Authorization": "***", "X-New": "added"}},
                {"name": "local", "env": {"API_KEY": "***"}},
                {"name": "unknown", "headers": {"Authorization": "***"}},
            ]
        },
    }

    session_manager_module._restore_masked_mcp_secrets(incoming, existing)

    servers = {server["name"]: server for server in incoming["tools"]["mcp"]}
    assert servers["remote"]["headers"] == {"Authorization": "Bearer real-token", "X-New": "added"}
    assert servers["local"]["env"] == {"API_KEY": "real-secret"}
    # No matching prior server -> masked sentinel left untouched (not inventable).
    assert servers["unknown"]["headers"] == {"Authorization": "***"}


def test_restore_masked_acp_secrets_requires_unchanged_all_masked_argument_shape() -> None:
    existing = _acp_profile()
    round_trip = {
        "acp": {
            "args": ["***", "***", "***"],
            "env": {"API_KEY": "***"},
            "config_options": {"channel": "***", "telemetry": False},
        }
    }

    restoration = session_manager_module._restore_masked_acp_secrets(round_trip, existing)

    assert restoration.args_restored is True
    assert round_trip["acp"]["args"] == ["--token", "real-token", "***"]
    assert round_trip["acp"]["env"] == {"API_KEY": "real-secret"}
    assert round_trip["acp"]["config_options"] == {"channel": "private", "telemetry": False}

    edited = {"acp": {"args": ["--token", "***"], "env": {}, "config_options": {}}}
    assert session_manager_module._restore_masked_acp_secrets(edited, existing).args_restored is False
    assert edited["acp"]["args"] == ["--token", "***"]

    literal = _acp_profile()
    assert literal.acp is not None
    literal.acp.args = ["***"]
    literal_round_trip = {"acp": {"args": ["***"], "env": {}, "config_options": {}}}
    literal_restored = session_manager_module._restore_masked_acp_secrets(literal_round_trip, literal)
    session_manager_module._reject_unresolved_masked_acp_secrets(
        literal_round_trip,
        restoration=literal_restored,
    )


def test_restore_masked_acp_secrets_round_trips_literal_mask_values_in_mappings() -> None:
    existing = _acp_profile()
    assert existing.acp is not None
    existing.acp.env = {"API_KEY": "***"}
    existing.acp.config_options = {"channel": "***", "telemetry": False}
    round_trip = {
        "acp": {
            "args": [],
            "env": {"API_KEY": "***"},
            "config_options": {"channel": "***", "telemetry": False},
        }
    }

    restoration = session_manager_module._restore_masked_acp_secrets(round_trip, existing)

    assert restoration.env_keys == {"API_KEY"}
    assert restoration.option_keys == {"channel"}
    # A stored secret that IS the literal mask must survive read → write.
    session_manager_module._reject_unresolved_masked_acp_secrets(round_trip, restoration=restoration)
    assert round_trip["acp"]["env"] == {"API_KEY": "***"}
    assert round_trip["acp"]["config_options"] == {"channel": "***", "telemetry": False}

    added = {
        "acp": {
            "args": [],
            "env": {"API_KEY": "***", "NEW_SECRET": "***"},
            "config_options": {},
        }
    }
    added_restoration = session_manager_module._restore_masked_acp_secrets(added, existing)
    with pytest.raises(AcpSessionError, match="still masked"):
        session_manager_module._reject_unresolved_masked_acp_secrets(added, restoration=added_restoration)


def test_reject_unresolved_acp_arg_masks_but_accept_literal_star_in_full_list() -> None:
    with pytest.raises(AcpSessionError, match="complete unmasked argument list"):
        session_manager_module._reject_unresolved_masked_acp_secrets(
            {"acp": {"args": ["***", "***"], "env": {}, "config_options": {}}}
        )

    session_manager_module._reject_unresolved_masked_acp_secrets(
        {"acp": {"args": ["--literal", "***"], "env": {}, "config_options": {}}}
    )


def test_reject_partially_masked_acp_args_after_masked_read_edit() -> None:
    existing = _acp_profile()
    # After a masked read of a non-empty stored list, positional identity is
    # lost the moment the list is edited: ANY leftover mask is a placeholder
    # whose acceptance would silently persist the literal "***" over the
    # stored secrets.
    for edited_args in (
        ["***", "***", "--verbose"],  # same length, partially replaced
        ["***", "--verbose"],  # shortened, mask left behind
        ["***", "***", "***", "--verbose"],  # extended, masks left behind
    ):
        edited = {"acp": {"args": list(edited_args), "env": {}, "config_options": {}}}
        restoration = session_manager_module._restore_masked_acp_secrets(edited, existing)
        assert restoration.args_restored is False
        assert restoration.args_were_masked is True
        with pytest.raises(AcpSessionError, match="complete unmasked argument list"):
            session_manager_module._reject_unresolved_masked_acp_secrets(edited, restoration=restoration)

    # A fully re-entered unmasked list is the documented way out.
    clean = {"acp": {"args": ["--token", "new-token", "--verbose"], "env": {}, "config_options": {}}}
    clean_restoration = session_manager_module._restore_masked_acp_secrets(clean, existing)
    session_manager_module._reject_unresolved_masked_acp_secrets(clean, restoration=clean_restoration)
    assert clean["acp"]["args"] == ["--token", "new-token", "--verbose"]

    # Literal "***" for an existing profile: clear the stored list in one
    # write, then re-add it — with an empty stored list the mask can only be
    # literal input, so the mixed shape is accepted (two-step escape hatch).
    cleared = _acp_profile()
    assert cleared.acp is not None
    cleared.acp.args = []
    literal = {"acp": {"args": ["--literal", "***"], "env": {}, "config_options": {}}}
    literal_restoration = session_manager_module._restore_masked_acp_secrets(literal, cleared)
    assert literal_restoration.args_were_masked is False
    session_manager_module._reject_unresolved_masked_acp_secrets(literal, restoration=literal_restoration)


@pytest.mark.parametrize(
    "acp",
    [
        {"args": [], "env": {"NEW_SECRET": "***"}, "config_options": {}},
        {"args": [], "env": {}, "config_options": {"new-option": "***"}},
    ],
)
def test_reject_unresolved_acp_mapping_masks(acp: dict[str, object]) -> None:
    with pytest.raises(AcpSessionError, match="still masked"):
        session_manager_module._reject_unresolved_masked_acp_secrets({"acp": acp})


def test_write_agent_profile_rejects_unresolved_masked_mcp_secret_after_rename() -> None:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(_mcp_profile())
    manager = _profile_manager("WithMcp", agent_registry)

    with pytest.raises(AcpSessionError, match="still masked"):
        manager.write_agent_profile(
            {
                "name": "WithMcp",
                "tools": {
                    "mcp": [
                        {
                            "name": "renamed",
                            "transport": "http",
                            "url": "https://example.test/mcp",
                            "headers": {"Authorization": "***"},
                        }
                    ]
                },
            }
        )


def test_write_agent_profile_rename_restores_masked_secrets_via_stable_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    original = _acp_profile()
    original.id = "acp-stable-id"
    agent_registry = AgentProfileRegistry()
    agent_registry.register(original)
    manager = _acp_manager(agent_registry)

    data = manager.read_agent_profile("WithAcp")
    assert data["id"] == "acp-stable-id"
    assert data["acp"]["args"] == ["***", "***", "***"]
    data["name"] = "RenamedAcp"

    manager.write_agent_profile(data)

    renamed = agent_registry.get("RenamedAcp")
    assert renamed is not None
    assert renamed.acp is not None
    # ``id`` is the identity that survives renames: the all-masked
    # round-trip restores the stored secrets instead of persisting "***".
    assert renamed.acp.args == ["--token", "real-token", "***"]
    assert renamed.acp.env == {"API_KEY": "real-secret", "EMPTY": ""}
    assert renamed.acp.config_options == {"channel": "private", "telemetry": False}


def test_write_agent_profile_rename_rejects_partially_masked_acp_args() -> None:
    original = _acp_profile()
    original.id = "acp-stable-id"
    agent_registry = AgentProfileRegistry()
    agent_registry.register(original)
    manager = _acp_manager(agent_registry)

    data = manager.read_agent_profile("WithAcp")
    data["name"] = "RenamedAcp"
    # Positional identity was lost when the masked list was edited — a
    # leftover mask is a placeholder even across a rename, and accepting it
    # would persist literal "***" into the renamed profile.
    data["acp"]["args"] = ["***", "***", "--verbose"]

    with pytest.raises(AcpSessionError, match="complete unmasked argument list"):
        manager.write_agent_profile(data)
    assert agent_registry.get("RenamedAcp") is None


def test_write_agent_profile_duplicate_id_twins_fail_closed_on_masked_args() -> None:
    twin_a = _acp_profile()
    twin_a.id = "twin-id"
    twin_b = _acp_profile()
    twin_b.name = "WithAcpCopy"
    twin_b.id = "twin-id"
    agent_registry = AgentProfileRegistry()
    agent_registry.register(twin_a)
    agent_registry.register(twin_b)
    manager = _acp_manager(agent_registry)

    # Two stored profiles share the id, so there is no single source of
    # truth for restoration — leftover masks must fail closed, not guess.
    payload = {
        "name": "RenamedAgain",
        "id": "twin-id",
        "acp": {"command": "remote-agent", "args": ["***", "--verbose"], "env": {}, "config_options": {}},
    }

    with pytest.raises(AcpSessionError, match="complete unmasked argument list"):
        manager.write_agent_profile(payload)
    assert agent_registry.get("RenamedAgain") is None


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda manager: manager.write_agent_profile({"name": "../escape"}), id="write_agent_profile-parent-dir"
        ),
        pytest.param(lambda manager: manager.delete_agent_profile("../escape"), id="delete_agent_profile-parent-dir"),
        pytest.param(lambda manager: manager.reset_agent_profile("../escape"), id="reset_agent_profile-parent-dir"),
        pytest.param(
            lambda manager: manager.write_model_profile({"id": "../escape", "name": "Bad"}),
            id="write_model_profile-parent-dir",
        ),
        pytest.param(lambda manager: manager.delete_model_profile("../escape"), id="delete_model_profile-parent-dir"),
        # Windows-reserved names share the loader predicate.
        pytest.param(
            lambda manager: manager.write_agent_profile({"name": "CON"}), id="write_agent_profile-reserved-con"
        ),
        pytest.param(lambda manager: manager.delete_agent_profile("foo:bar"), id="delete_agent_profile-drive-colon"),
        pytest.param(lambda manager: manager.reset_agent_profile("nul.txt"), id="reset_agent_profile-reserved-nul"),
        pytest.param(
            lambda manager: manager.write_model_profile({"id": "aux", "name": "Bad"}),
            id="write_model_profile-reserved-aux",
        ),
    ],
)
def test_profile_write_and_delete_reject_path_values(call: Callable[[AcpSessionManager], object]) -> None:
    manager = _manager(None, _StaticListStore([]))

    with pytest.raises(AcpSessionError, match="not a path"):
        call(manager)


def test_write_model_profile_rejects_masked_api_key_for_new_profile() -> None:
    manager = _manager(None, _StaticListStore([]))

    # Read-copy-write with a new id: no existing profile to restore the secret from,
    # so a still-masked api_key must be rejected rather than persisted literally.
    with pytest.raises(AcpSessionError, match="still masked"):
        manager.write_model_profile({"id": "copy-of-model", "name": "Copy", "api_key": "***"})


def test_write_model_profile_clears_emptied_fields_but_keeps_api_key(monkeypatch) -> None:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    model_registry = ModelProfileRegistry()
    model_registry.register(
        ModelProfile(id="model", name="Mock", api_key="real-secret", base_url="https://old.example/v1")
    )
    manager = _profile_manager("Code", agent_registry, model_registry)
    saved: dict[str, ModelProfile] = {}

    def _fake_save(profile: ModelProfile):
        saved["profile"] = profile
        return "/tmp/fake.yaml"

    monkeypatch.setattr(session_manager_module, "save_model_profile", _fake_save)

    manager.write_model_profile({"id": "model", "name": "Mock", "base_url": "", "api_key": "***"})

    # Explicit "" clears base_url; masked api_key preserves the stored secret.
    assert saved["profile"].base_url == ""
    assert saved["profile"].api_key == "real-secret"


def _capture_model_profile_saves(monkeypatch: pytest.MonkeyPatch) -> list[ModelProfile]:
    saved: list[ModelProfile] = []

    def _fake_save(profile: ModelProfile) -> str:
        saved.append(profile)
        return "/tmp/fake.yaml"

    monkeypatch.setattr(session_manager_module, "save_model_profile", _fake_save)
    return saved


@pytest.mark.parametrize(
    ("stream_field", "expected"),
    [({}, True), ({"stream": None}, True), ({"stream": False}, False)],
    ids=["missing", "null", "false"],
)
def test_write_model_profile_new_profile_streams_unless_stream_is_false(
    monkeypatch: pytest.MonkeyPatch, stream_field: dict[str, object], expected: bool
) -> None:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    manager = _profile_manager("Code", agent_registry, ModelProfileRegistry())
    saved = _capture_model_profile_saves(monkeypatch)

    manager.write_model_profile({"id": "new-model", "name": "New", **stream_field})

    assert [profile.stream for profile in saved] == [expected]


@pytest.mark.parametrize("stream_field", [{}, {"stream": None}], ids=["missing", "null"])
def test_write_model_profile_update_without_stream_keeps_the_stored_value(
    monkeypatch: pytest.MonkeyPatch, stream_field: dict[str, object]
) -> None:
    agent_registry = AgentProfileRegistry()
    agent_registry.register(AgentProfile(name="Code"))
    model_registry = ModelProfileRegistry()
    model_registry.register(ModelProfile(id="model", name="Mock", stream=False))
    manager = _profile_manager("Code", agent_registry, model_registry)
    saved = _capture_model_profile_saves(monkeypatch)

    manager.write_model_profile({"id": "model", "name": "Renamed", **stream_field})

    assert [(profile.name, profile.stream) for profile in saved] == [("Renamed", False)]


@pytest.mark.parametrize("spelling", ["code", "CODE", "cOdE"])
@pytest.mark.parametrize("has_shadow", [False, True])
def test_delete_agent_profile_rejects_case_variant_of_builtin_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spelling: str, has_shadow: bool
) -> None:
    """A case-variant spelling of a built-in name must hit the reset-pointer guard.

    ``Code.yaml``/``code.yaml`` can alias on macOS/Windows, so deleting a variant
    spelling must reject the built-in with the reset pointer — never silently
    report ``deleted=False`` and never unlink the built-in's only shadow file.
    """
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    source_registry = AgentProfileRegistry()
    source_registry.load_builtins()
    customized = source_registry.get_builtin_template("Code")
    assert customized is not None
    customized.instructions = "custom shadow"
    if has_shadow:
        save_agent_profile(customized, target_dir=user_dir)
    agent_registry = AgentProfileRegistry()
    agent_registry.load_all(user_dir=user_dir)
    manager = _acp_manager(agent_registry)
    shadow = user_dir / "Code.yaml"
    shadow_before = shadow.read_bytes() if has_shadow else None

    with pytest.raises(AcpSessionError, match="cannot be deleted; reset"):
        manager.delete_agent_profile(spelling)

    assert shadow.exists() is has_shadow
    if has_shadow:
        assert shadow.read_bytes() == shadow_before
    assert agent_registry.get("Code") is not None
    assert agent_registry.get("Code") == source_registry.get_builtin_template("Code")
    if has_shadow:
        assert agent_registry.get("Code-Modified").instructions == customized.instructions


@pytest.mark.parametrize("spelling", ["code", "CODE", "cOdE"])
@pytest.mark.parametrize("request_name", ["Code", "code", "CODE", "cOdE"])
@pytest.mark.parametrize("sub_agent_only", [False, True])
def test_delete_agent_profile_distinguishes_registered_twin_from_builtin_aliases(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spelling: str, request_name: str, sub_agent_only: bool
) -> None:
    """Only the user's exact registered spelling may delete its file."""
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    custom = AgentProfile(
        name=spelling, id="ca5e00000001", instructions="custom instructions", sub_agent_only=sub_agent_only
    )
    custom.skills.paths = ["private-skills"]
    custom.tools.mcp = [MCPServerConfig(name="private", transport="http", url="https://example.test")]
    custom.memory.files = ["private.md"]
    path = save_agent_profile(custom, target_dir=user_dir)
    before = path.read_bytes()
    unrelated = save_agent_profile(AgentProfile(name="Unrelated", id="unrelated-id"), target_dir=user_dir)
    unrelated_before = unrelated.read_bytes()
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    manager = _acp_manager(registry)
    template = registry.get_builtin_template("Code")
    assert registry.get(spelling) is not None
    assert not registry.is_builtin(spelling)

    if request_name == spelling:
        assert manager.delete_agent_profile(request_name) == {"name": spelling, "deleted": True}
        assert not path.exists()
        assert registry.get(spelling) is None
    else:
        if request_name != "Code":
            assert registry.get(request_name) is None
        with pytest.raises(AcpSessionError, match="cannot be deleted; reset 'Code' instead"):
            manager.delete_agent_profile(request_name)
        assert path.read_bytes() == before

    assert unrelated.read_bytes() == unrelated_before
    assert registry.get("Code") == template
    with pytest.raises(AcpSessionError, match="Built-in agent profile not found"):
        manager.reset_agent_profile(spelling)

    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    assert fresh.get("Code") == template
    assert fresh.get(spelling) == (None if request_name == spelling else custom)


@pytest.mark.parametrize("spelling", ["code", "CODE", "cOdE"])
@pytest.mark.parametrize("sub_agent_only", [False, True])
@pytest.mark.parametrize("modified_builtin", [False, True])
def test_reset_agent_profile_preserves_distinct_registered_twin(
    monkeypatch: pytest.MonkeyPatch, tmp_path, spelling: str, sub_agent_only: bool, modified_builtin: bool
) -> None:
    """Resetting Code must never unlink a user's case-variant profile through an alias."""
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    custom = AgentProfile(
        name=spelling, id="ca5e00000001", instructions="custom instructions", sub_agent_only=sub_agent_only
    )
    custom.skills.paths = ["private-skills"]
    custom.tools.mcp = [MCPServerConfig(name="private", transport="http", url="https://example.test")]
    custom.memory.files = ["private.md"]
    path = save_agent_profile(custom, target_dir=user_dir)
    before = path.read_bytes()
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    current = registry.get("Code")
    assert current is not None
    if modified_builtin:
        current.instructions = "in-memory customization"
    manager = _acp_manager(registry)
    # Neither a missing shadow nor a file belonging to the twin may be unlinked.
    delete = create_autospec(
        session_manager_module.delete_agent_profile, side_effect=session_manager_module.delete_agent_profile
    )
    monkeypatch.setattr(session_manager_module, "delete_agent_profile", delete)

    result = manager.reset_agent_profile("Code")

    delete.assert_not_called()
    assert result["changed"] is modified_builtin
    assert result["profile"]["builtin"] is True
    assert registry.get("Code") == registry.get_builtin_template("Code")
    assert registry.get(spelling) == custom
    assert path.read_bytes() == before
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    loaded = fresh.get(spelling)
    assert loaded is not None
    assert loaded.id == custom.id
    assert loaded.instructions == custom.instructions
    assert loaded.skills == custom.skills
    assert loaded.tools.mcp == custom.tools.mcp
    assert loaded.memory == custom.memory
    assert not fresh.is_builtin(spelling)


@pytest.mark.parametrize("spelling", ["code", "CODE", "cOdE"])
@pytest.mark.parametrize("sub_agent_only", [False, True])
def test_reset_deletes_builtin_shadow_written_over_registered_twin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spelling: str, sub_agent_only: bool
) -> None:
    """A file replaced by an older writer has its new owner despite stale registry entries.

    Caseless hosts exercise the real overwrite; case-sensitive hosts retain
    two separate files, and the separate twin must survive reset unchanged.
    """
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    manager = _acp_manager(registry)
    manager.write_agent_profile(
        {
            "name": spelling,
            "id": "ca5e00000001",
            "instructions": "custom twin",
            "sub_agent_only": sub_agent_only,
        }
    )
    twin = registry.get(spelling)
    assert twin is not None
    twin_path = user_dir / f"{spelling}.yaml"
    twin_before = twin_path.read_bytes()

    template = registry.get_builtin_template("Code")
    assert template is not None
    # Recreate an old-version/external overwrite; the ACP write API now rejects it.
    template.instructions = "persisted customization"
    save_agent_profile(template, target_dir=user_dir)
    registry.register(template)
    shadow = user_dir / "Code.yaml"
    stored = load_profile_from_yaml(shadow)
    assert stored.name == "Code"
    assert stored.id == template.id
    assert stored.instructions == "persisted customization"
    assert registry.get(spelling) is twin
    aliases = twin_path.samefile(shadow)
    result = manager.reset_agent_profile("Code")

    assert result["changed"] is True
    assert not shadow.exists()
    if not aliases:
        assert twin_path.read_bytes() == twin_before
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    assert fresh.get("Code") == fresh.get_builtin_template("Code")
    assert fresh.get(spelling) == (None if aliases else twin)


@pytest.mark.parametrize("owner_name", ["Code", "Foo"])
def test_delete_agent_profile_checks_stored_owner_after_legacy_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner_name: str
) -> None:
    """Old-version or external writes may leave stale entries for a caseless twin."""
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    stale_name = owner_name.lower()
    stale = AgentProfile(name=stale_name, id="stale-id", instructions="original twin")
    stale_path = save_agent_profile(stale, target_dir=user_dir)
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    manager = _acp_manager(registry)
    owner = registry.get_builtin_template(owner_name) or AgentProfile(name=owner_name, id="owner-id")
    owner.instructions = "persisted customization"
    save_agent_profile(owner, target_dir=user_dir)
    registry.register(owner)
    owner_path = user_dir / f"{owner_name}.yaml"
    owner_before = owner_path.read_bytes()
    aliases = stale_path.samefile(owner_path)
    assert registry.get(stale_name) == stale
    assert load_profile_from_yaml(owner_path).name == owner_name

    if aliases:
        with pytest.raises(AcpSessionError, match=f"belongs to {owner_name!r}"):
            manager.delete_agent_profile(stale_name)
        assert registry.get(stale_name) == stale
    else:
        assert manager.delete_agent_profile(stale_name) == {"name": stale_name, "deleted": True}
        assert not stale_path.exists()
    assert owner_path.read_bytes() == owner_before
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    assert fresh.get(owner_name) == owner
    owner_after_reload = owner_path.read_bytes()

    if registry.is_builtin(owner_name):
        with pytest.raises(AcpSessionError, match="cannot be deleted; reset 'Code' instead"):
            manager.delete_agent_profile(owner_name)
        assert owner_path.read_bytes() == owner_after_reload
        assert manager.reset_agent_profile(owner_name)["changed"] is True
    else:
        assert manager.delete_agent_profile(owner_name) == {"name": owner_name, "deleted": True}
        assert registry.get(owner_name) is None
    assert not owner_path.exists()


@pytest.mark.parametrize("owner_name", ["Code", "Foo"])
def test_delete_agent_profile_refuses_externally_replaced_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner_name: str
) -> None:
    """External replacements must also be protected, on every filesystem."""
    fake_platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: fake_platform)
    user_dir = tmp_path / "agents"
    stale_name = owner_name.lower()
    stale = AgentProfile(name=stale_name, id="stale-id")
    path = save_agent_profile(stale, target_dir=user_dir)
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    manager = _acp_manager(registry)
    path.write_text(f"name: {owner_name}\nid: owner-id\ninstructions: external replacement\n", encoding="utf-8")
    before = path.read_bytes()
    if owner_name == "Foo":
        assert registry.get(owner_name) is None

    with pytest.raises(AcpSessionError, match=f"belongs to {owner_name!r}"):
        manager.delete_agent_profile(stale_name)

    assert path.read_bytes() == before
    assert registry.get(stale_name) == stale


@pytest.mark.parametrize("configured", [False, True])
def test_web_profile_read_write_preserves_patch_absence_and_values(monkeypatch, tmp_path, configured) -> None:
    from chrys.service.tools.builtins.web.config import WebFetchConfigPatch, WebSearchConfigPatch

    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform_with_config_dir(tmp_path))
    original = AgentProfile(name="Web", id="web-round-trip")
    if configured:
        original.tools.web_search = WebSearchConfigPatch(mode="off", fallback_chain=[])
        original.tools.web_fetch = WebFetchConfigPatch(mode="on")
    registry = AgentProfileRegistry()
    registry.register(original)
    manager = _profile_manager("Web", registry)
    data = manager.read_agent_profile("Web")
    manager.write_agent_profile(data)
    restored = registry.get("Web")
    assert restored is not None
    assert restored.tools.web_search == original.tools.web_search
    assert restored.tools.web_fetch == original.tools.web_fetch
