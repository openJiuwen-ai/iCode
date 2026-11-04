# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for AgentProfileRegistry."""

from __future__ import annotations

import os
import unicodedata
from copy import deepcopy
from pathlib import Path as _Path
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import (
    AgentProfile,
    MCPServerConfig,
    MemoryConfig,
    SkillsConfig,
    SubAgentRef,
    SubAgentsConfig,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_register_and_get() -> None:
    reg = AgentProfileRegistry()
    p = AgentProfile(name="test")
    reg.register(p)
    assert reg.get("test") is p
    assert reg.get("unknown") is None


def test_resolve_selector_accepts_id_name_and_display_name() -> None:
    reg = AgentProfileRegistry()
    profile = AgentProfile(name="Code", id="agent-id", display_name="Human Code")
    reg.register(profile)

    assert reg.resolve_selector("agent-id") is profile
    assert reg.resolve_selector("Code") is profile
    assert reg.resolve_selector("human code") is profile


def test_get_by_id_does_not_fall_back_to_matching_name() -> None:
    reg = AgentProfileRegistry()
    profile = AgentProfile(name="former-id", id="current-id")
    reg.register(profile)

    assert reg.get_by_id("current-id") is profile
    assert reg.get_by_id("former-id") is None
    assert reg.get_by_id("missing-id", disambiguating_name="former-id") is None


def test_get_by_id_uses_exact_name_only_to_disambiguate_duplicate_ids() -> None:
    reg = AgentProfileRegistry()
    first = AgentProfile(name="First", id="shared-id")
    second = AgentProfile(name="Second", id="shared-id")
    reg.register(first)
    reg.register(second)

    assert reg.get_by_id("shared-id", disambiguating_name="Second") is second
    assert reg.get_by_id("shared-id", disambiguating_name="Missing") is None


def test_duplicate_profile_ids_warn_and_fail_closed(caplog: pytest.LogCaptureFixture) -> None:
    reg = AgentProfileRegistry()
    first = AgentProfile(name="First", id="shared-id")
    second = AgentProfile(name="Second", id="shared-id")

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.register(first)
        reg.register(second)
        assert reg.get_by_id("shared-id") is None
        assert reg.resolve_selector("shared-id") is None

    assert "Duplicate agent profile id 'shared-id'" in caplog.text
    assert caplog.text.count("ambiguous across profiles 'First', 'Second'") == 2


def test_resolve_selector_rejects_ambiguous_display_name() -> None:
    reg = AgentProfileRegistry()
    reg.register(AgentProfile(name="a", id="a-id", display_name="Shared"))
    reg.register(AgentProfile(name="b", id="b-id", display_name="Shared"))

    assert reg.resolve_selector("Shared") is None


def test_list_profiles() -> None:
    reg = AgentProfileRegistry()
    reg.register(AgentProfile(name="a"))
    reg.register(AgentProfile(name="b"))
    assert reg.list_names() == ["a", "b"]
    assert len(reg.list_profiles()) == 2


def test_register_overwrites() -> None:
    reg = AgentProfileRegistry()
    reg.register(AgentProfile(name="x", description="old"))
    reg.register(AgentProfile(name="x", description="new"))
    assert reg.get("x").description == "new"
    assert len(reg.list_profiles()) == 1


@pytest.mark.parametrize("name", ["Code", "Mine"])
def test_stdio_ignores_invalid_headers_without_dropping_user_profile(tmp_path: Path, name: str) -> None:
    (tmp_path / f"{name}.yaml").write_text(
        f"name: {name}\ninstructions: Keep my custom instructions\ntools:\n  mcp:\n"
        "    - name: server\n      transport: stdio\n      command: my-server\n      headers: []\n",
        encoding="utf-8",
    )
    registry = AgentProfileRegistry()

    registry.load_all(user_dir=tmp_path)

    profile = registry.get(name)
    assert profile is not None
    assert profile.instructions == "Keep my custom instructions"
    assert len(profile.tools.mcp) == 1
    assert profile.tools.mcp[0].command == "my-server"
    assert profile.tools.mcp[0].headers == {}


def test_remove_can_skip_sub_agent_ref_cascade(monkeypatch) -> None:
    reg = AgentProfileRegistry()
    reg.register(AgentProfile(name="child"))
    reg.register(AgentProfile(name="parent", sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="child")])))
    saved: list[str] = []

    def record_save(profile, *, target_dir=None):
        saved.append(profile.name)

    monkeypatch.setattr("chrys.service.profiles.agents.serializer.save_profile", record_save)

    assert reg.remove("child", cascade=False) is True

    parent = reg.get("parent")
    assert parent is not None
    assert [ref.profile for ref in parent.sub_agents.agents] == ["child"]
    assert saved == []


def test_snapshot_restore_restores_registry_metadata(tmp_path: Path) -> None:
    reg = AgentProfileRegistry()
    reg.load_all(user_dir=tmp_path)
    original_template = reg.get_builtin_template("Code")
    assert original_template is not None
    snapshot = reg.snapshot()
    assert snapshot.user_dir == tmp_path

    reg.register(AgentProfile(name="temp"))
    reg._builtin_profiles["Code"].instructions = "polluted"

    reg.restore(snapshot)

    assert reg.get("temp") is None
    assert reg.is_builtin("Code") is True
    assert reg.get_builtin_template("Code") == original_template
    assert reg.snapshot().user_dir == tmp_path


@pytest.mark.parametrize("name", ["Code", "Explore", "General", "QA"])
def test_builtin_filename_guard_does_not_change_profile_identity(name: str) -> None:
    reg = AgentProfileRegistry()
    reg.load_builtins()
    custom = AgentProfile(name=name.lower())
    reg.register(custom)

    assert reg.resolve_builtin_filename(name) == name
    assert reg.resolve_builtin_filename(name.lower()) == name
    assert reg.resolve_builtin_filename(name.upper()) == name
    assert reg.resolve_builtin_filename(f"{name}Helper") is None
    assert reg.is_builtin(name)
    assert not reg.is_builtin(custom.name)
    assert reg.get_builtin_template(custom.name) is None
    assert reg.get(custom.name) is custom


def test_build_builtin_reset_preserves_only_skills_mcp_and_memory() -> None:
    reg = AgentProfileRegistry()
    reg.load_builtins()
    template = reg.get_builtin_template("Code")
    assert template is not None
    current = deepcopy(template)
    current.display_name = "Customized"
    current.instructions = "custom instructions"
    current.tools.builtins = []
    current.skills = SkillsConfig(paths=["custom-skills"])
    current.tools.mcp = [MCPServerConfig(name="private", transport="http", url="https://example.test")]
    current.memory = MemoryConfig(files=["private.md"])

    reset = reg.build_builtin_reset("Code", preserve_from=current)

    assert reset.name == template.name
    assert reset.id == template.id
    assert reset.display_name == template.display_name
    assert reset.instructions == template.instructions
    assert reset.tools.builtins == template.tools.builtins
    assert reset.skills == current.skills
    assert reset.tools.mcp == current.tools.mcp
    assert reset.memory == current.memory
    reset.skills.paths.append("mutated")
    assert current.skills.paths == ["custom-skills"]


def test_load_builtins() -> None:
    reg = AgentProfileRegistry()
    count = reg.load_builtins()
    assert count == 4
    assert reg.get("Code") is not None
    assert reg.get("Explore") is not None
    assert reg.get("General") is not None
    assert reg.get("Plan") is None
    assert reg.get("QA") is not None


def test_builtin_code_and_qa_enable_todo_with_skip_approval() -> None:
    """Code/QA ship the todo category default-on, with an EXPLICIT
    ``todo: skip`` override — the loader replaces (not merges) the schema
    default, so omitting it from their YAML would silently drop the skip."""
    reg = AgentProfileRegistry()
    reg.load_builtins()
    for name in ("Code", "QA"):
        profile = reg.get(name)
        assert profile is not None, f"missing builtin {name!r}"
        assert "todo" in profile.tools.builtins, name
        assert profile.approval.overrides.get("todo") == "skip", name


def test_builtin_executable_skill_profiles_require_script_approval() -> None:
    """Builtins state the script boundary explicitly in addition to the policy floor."""
    reg = AgentProfileRegistry()
    reg.load_builtins()
    for name in ("Code", "General", "QA"):
        profile = reg.get(name)
        assert profile is not None, f"missing builtin {name!r}"
        assert profile.approval.overrides.get("skill.run_skill_script") == "require", name


def test_load_user_profiles(tmp_path: Path) -> None:
    (tmp_path / "custom.yaml").write_text("name: my-custom\ndescription: user profile\n", encoding="utf-8")
    reg = AgentProfileRegistry()
    count = reg.load_user_profiles(tmp_path)
    assert count == 1
    assert reg.get("my-custom") is not None


def test_load_user_profiles_migrates_missing_id(tmp_path: Path) -> None:
    """Legacy YAML without `id` gets one assigned and the file is rewritten."""
    yaml_path = tmp_path / "legacy-agent.yaml"  # save_profile writes to {name}.yaml
    yaml_path.write_text("name: legacy-agent\ndescription: pre-id era\n", encoding="utf-8")
    reg = AgentProfileRegistry()
    reg.load_user_profiles(tmp_path)

    profile = reg.get("legacy-agent")
    assert profile is not None
    assert profile.id  # uuid-hex assigned
    assert len(profile.id) == 12

    # File on disk now carries the id
    assert "id:" in yaml_path.read_text(encoding="utf-8")
    # And reloading sees the same id (sticks across runs)
    reg2 = AgentProfileRegistry()
    reg2.load_user_profiles(tmp_path)
    assert reg2.get("legacy-agent").id == profile.id


def test_load_user_profiles_keeps_legacy_id_empty_when_migration_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    yaml_path = tmp_path / "legacy-agent.yaml"
    original = "name: legacy-agent\ndescription: pre-id era\n"
    yaml_path.write_text(original, encoding="utf-8")

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only agents directory")

    with monkeypatch.context() as migration_patch:
        migration_patch.setattr("chrys.service.profiles.agents.serializer.save_profile", fail_save)
        reg = AgentProfileRegistry()

        with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
            reg.load_user_profiles(tmp_path)

    profile = reg.get("legacy-agent")
    assert profile is not None
    assert profile.id == ""
    assert yaml_path.read_text(encoding="utf-8") == original
    assert "Could not persist migrated id" in caplog.text

    restarted = AgentProfileRegistry()
    restarted.load_user_profiles(tmp_path)
    restarted_profile = restarted.get("legacy-agent")
    assert restarted_profile is not None
    assert restarted_profile.id
    assert "id:" in yaml_path.read_text(encoding="utf-8")


def test_load_user_profiles_keeps_existing_id(tmp_path: Path) -> None:
    """YAML that already has an id is left untouched."""
    yaml_path = tmp_path / "ok-agent.yaml"
    yaml_path.write_text("name: ok-agent\nid: deadbeefcafe\n", encoding="utf-8")
    original = yaml_path.read_text(encoding="utf-8")
    reg = AgentProfileRegistry()
    reg.load_user_profiles(tmp_path)
    assert reg.get("ok-agent").id == "deadbeefcafe"
    assert yaml_path.read_text(encoding="utf-8") == original  # not rewritten


_PLAN_REF_YAML = """\
  - profile: Plan
    tool_name: plan_agent
    max_concurrency: 2
    tool_description: Read-only planning.
"""
_EXPLORE_REF_YAML = "  - profile: Explore\n    tool_name: explore_agent\n"
_EXPLORE_REF = SubAgentRef(profile="Explore", tool_name="explore_agent")


def _write_profile_with_sub_agents(directory: Path, name: str, profile_id: str, refs_yaml: str) -> Path:
    path = directory / f"{name}.yaml"
    path.write_text(
        f"name: {name}\nid: {profile_id}\nsub_agents:\n  max_total_concurrency: 4\n  agents:\n{refs_yaml}",
        encoding="utf-8",
    )
    return path


def _sub_agent_profiles(profile: AgentProfile | None) -> list[str]:
    assert profile is not None
    return [ref.profile for ref in profile.sub_agents.agents]


def test_load_user_profiles_drops_retired_plan_sub_agent_refs(tmp_path: Path) -> None:
    """A customized Code shadow saved while Plan shipped loses that reference and nothing else."""
    general_yaml = "  - profile: General\n    tool_name: general_agent\n"
    code = _write_profile_with_sub_agents(
        tmp_path, "Code", "b011c0de0001", _EXPLORE_REF_YAML + _PLAN_REF_YAML + general_yaml
    )
    only_plan = _write_profile_with_sub_agents(tmp_path, "Planner", "c0ffee000001", _PLAN_REF_YAML)
    expected = [_EXPLORE_REF, SubAgentRef(profile="General", tool_name="general_agent")]

    reg = AgentProfileRegistry()
    reg.load_all(user_dir=tmp_path)

    code_profile = reg.get("Code")
    assert code_profile is not None
    assert code_profile.sub_agents.agents == expected
    assert load_profile_from_yaml(code).sub_agents == SubAgentsConfig(max_total_concurrency=4, agents=expected)
    assert _sub_agent_profiles(reg.get("Planner")) == []
    assert load_profile_from_yaml(only_plan).sub_agents == SubAgentsConfig(max_total_concurrency=4)
    migrated = {path: path.read_bytes() for path in (code, only_plan)}

    restarted = AgentProfileRegistry()
    restarted.load_all(user_dir=tmp_path)
    assert {path: path.read_bytes() for path in migrated} == migrated


def test_load_user_profiles_keeps_plan_refs_when_the_user_owns_a_plan_profile(tmp_path: Path) -> None:
    (tmp_path / "Plan.yaml").write_text("name: Plan\nid: b011b1ad0003\nsub_agent_only: true\n", encoding="utf-8")
    path = _write_profile_with_sub_agents(tmp_path, "Custom", "c0ffee000001", _PLAN_REF_YAML)
    original = path.read_bytes()

    reg = AgentProfileRegistry()
    reg.load_all(user_dir=tmp_path)

    assert reg.get("Plan") is not None
    assert _sub_agent_profiles(reg.get("Custom")) == ["Plan"]
    assert path.read_bytes() == original


def test_load_user_profiles_keeps_plan_refs_while_the_users_plan_cannot_be_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Plan stored off its canonical path that cannot move there is still the user's own."""
    (tmp_path / "my-plan.yaml").write_text("name: Plan\nid: b011b1ad0003\n", encoding="utf-8")
    path = _write_profile_with_sub_agents(tmp_path, "Custom", "c0ffee000001", _PLAN_REF_YAML)
    original = path.read_bytes()

    def fail_rename(self: _Path, target: _Path) -> None:
        raise PermissionError("read-only agents dir")

    monkeypatch.setattr(_Path, "rename", fail_rename)
    reg = AgentProfileRegistry()
    reg.load_all(user_dir=tmp_path)

    assert reg.get("Plan") is None
    assert _sub_agent_profiles(reg.get("Custom")) == ["Plan"]
    assert path.read_bytes() == original


def test_load_user_profiles_defers_plan_ref_removal_while_a_profile_file_fails_to_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A user Plan.yaml with a syntax error must not cost other profiles their Plan references."""
    plan = tmp_path / "Plan.yaml"
    plan.write_text("name: Plan\nsub_agent_only: [unclosed\n", encoding="utf-8")
    path = _write_profile_with_sub_agents(tmp_path, "Custom", "c0ffee000001", _PLAN_REF_YAML)
    original = path.read_bytes()

    reg = AgentProfileRegistry()
    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    assert reg.get("Plan") is None
    assert _sub_agent_profiles(reg.get("Custom")) == ["Plan"]
    assert path.read_bytes() == original
    assert "Plan.yaml could not be loaded" in caplog.text

    plan.write_text("name: Plan\nid: b011b1ad0003\nsub_agent_only: true\n", encoding="utf-8")
    fixed = AgentProfileRegistry()
    fixed.load_all(user_dir=tmp_path)

    assert fixed.get("Plan") is not None
    assert _sub_agent_profiles(fixed.get("Custom")) == ["Plan"]
    assert path.read_bytes() == original


def test_load_user_profiles_drops_plan_refs_in_memory_when_the_rewrite_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _write_profile_with_sub_agents(tmp_path, "Custom", "c0ffee000001", _EXPLORE_REF_YAML + _PLAN_REF_YAML)
    original = path.read_bytes()

    def fail_save(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("read-only agents directory")

    with monkeypatch.context() as migration_patch:
        migration_patch.setattr("chrys.service.profiles.agents.serializer.save_profile", fail_save)
        reg = AgentProfileRegistry()

        with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
            reg.load_all(user_dir=tmp_path)

    assert _sub_agent_profiles(reg.get("Custom")) == ["Explore"]
    assert path.read_bytes() == original
    assert "could not rewrite its file" in caplog.text

    restarted = AgentProfileRegistry()
    restarted.load_all(user_dir=tmp_path)
    assert load_profile_from_yaml(path).sub_agents.agents == [_EXPLORE_REF]


def test_load_user_profiles_detects_duplicate_ids(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / "First.yaml").write_text("name: First\nid: shared-id\n", encoding="utf-8")
    (tmp_path / "Second.yaml").write_text("name: Second\nid: shared-id\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_user_profiles(tmp_path)

    assert reg.get("First") is not None
    assert reg.get("Second") is not None
    assert reg.get_by_id("shared-id") is None
    assert "Duplicate agent profile id 'shared-id'" in caplog.text


def test_load_builtins_have_hardcoded_ids() -> None:
    """All four built-in profiles register with their stable hardcoded ids."""
    reg = AgentProfileRegistry()
    reg.load_builtins()
    assert reg.get("Code").id == "b011c0de0001"
    assert reg.get("Explore").id == "b011e80e0002"
    assert reg.get("General").id == "b0119e4e0004"
    assert reg.get("QA").id == "b0119a000005"


def test_legacy_migration_inherits_builtin_id_for_shadow_profiles(tmp_path: Path) -> None:
    """A legacy user ``Code.yaml`` shadows the builtin and must inherit its id."""
    yaml_path = tmp_path / "Code.yaml"
    yaml_path.write_text("name: Code\ndescription: my customized code agent\n", encoding="utf-8")
    reg = AgentProfileRegistry()
    reg.load_user_profiles(tmp_path)

    assert reg.get("Code").id == "b011c0de0001"
    assert "id: b011c0de0001" in yaml_path.read_text(encoding="utf-8")


def test_builtin_ids_map_matches_yaml_ids() -> None:
    """The hardcoded ``_BUILTIN_IDS`` map must stay in sync with the YAMLs."""
    from chrys.service.profiles.agents.registry import _BUILTIN_IDS

    reg = AgentProfileRegistry()
    reg.load_builtins()
    for name, expected_id in _BUILTIN_IDS.items():
        profile = reg.get(name)
        assert profile is not None, f"missing builtin {name!r}"
        assert profile.id == expected_id, f"{name}: yaml has {profile.id}, map has {expected_id}"


def test_load_all(tmp_path: Path) -> None:
    (tmp_path / "extra.yaml").write_text("name: extra\n", encoding="utf-8")
    hidden_metadata = tmp_path / ".hidden.yaml"
    hidden_metadata.write_text("hidden:\n- Code\n", encoding="utf-8")
    reg = AgentProfileRegistry()
    total = reg.load_all(user_dir=tmp_path)
    assert total == 5  # 4 builtins + 1 user
    assert reg.get("extra") is not None
    assert reg.get("Code") is not None
    assert not hidden_metadata.exists()


def test_load_all_continues_when_hidden_metadata_cannot_be_deleted(tmp_path: Path, monkeypatch) -> None:
    from pathlib import Path

    hidden_metadata = tmp_path / ".hidden.yaml"
    hidden_metadata.write_text("hidden:\n- Code\n", encoding="utf-8")
    original_unlink = Path.unlink

    def fail_hidden_unlink(path: Path, *args, **kwargs):
        if path == hidden_metadata:
            raise PermissionError("read-only metadata")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_hidden_unlink)
    reg = AgentProfileRegistry()

    total = reg.load_all(user_dir=tmp_path)

    assert total == 4
    assert reg.get("Code") is not None
    assert hidden_metadata.exists()


def test_load_user_profiles_moves_noncanonical_shadow_to_name_yaml(tmp_path: Path, caplog) -> None:
    source = tmp_path / "my-code.yaml"
    source.write_text("name: Code\nid: b011c0de0001\ninstructions: custom shadow\n", encoding="utf-8")
    original_bytes = source.read_bytes()
    reg = AgentProfileRegistry()

    with caplog.at_level("INFO", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    canonical = tmp_path / "Code.yaml"
    assert not source.exists()
    assert canonical.read_bytes() == original_bytes
    code = reg.get("Code")
    assert code is not None
    assert code.instructions == "custom shadow"
    assert "canonical path" in caplog.text


def test_load_user_profiles_moves_yml_shadow_and_migrates_id_into_canonical_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    numeric_id = "721719210458"
    monkeypatch.setattr(
        "chrys.service.profiles.agents.registry.uuid.uuid4",
        lambda: UUID(hex=f"{numeric_id}{'0' * 20}"),
    )
    source = tmp_path / "custom.yml"
    source.write_text("name: Custom\ninstructions: hello\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    reg.load_all(user_dir=tmp_path)

    assert not source.exists()
    assert sorted(p.name for p in tmp_path.glob("*.y*ml")) == ["Custom.yaml"]
    custom = reg.get("Custom")
    assert custom is not None
    assert custom.id == numeric_id
    assert load_profile_from_yaml(tmp_path / "Custom.yaml").id == numeric_id


def test_load_user_profiles_quarantines_conflicting_source_and_never_rewrites_canonical_file(
    tmp_path: Path, caplog
) -> None:
    """The canonical file wins; a conflicting source is moved out of the scan, byte-for-byte intact."""
    canonical = tmp_path / "Code.yaml"
    canonical.write_text("name: Code\nid: b011c0de0001\ninstructions: first\n", encoding="utf-8")
    canonical_bytes = canonical.read_bytes()
    other = tmp_path / "my-code.yaml"
    # No ``id`` on purpose: the legacy-id migration must not write this content to Code.yaml.
    other.write_text("name: Code\ninstructions: second\n", encoding="utf-8")
    other_bytes = other.read_bytes()
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        total = reg.load_all(user_dir=tmp_path)

    assert total == 5  # 4 builtins + the canonical Code shadow; the conflicting source is not counted
    assert canonical.read_bytes() == canonical_bytes
    assert not other.exists()
    quarantined = tmp_path / "my-code.yaml.conflict"
    assert quarantined.read_bytes() == other_bytes
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Code.yaml", "my-code.yaml.conflict"]
    assert "moved it to my-code.yaml.conflict" in caplog.text
    assert "Duplicate agent profile name" not in caplog.text
    code = reg.get("Code")
    assert code is not None
    assert code.instructions == "first"


def test_quarantined_conflict_does_not_resurrect_after_canonical_file_is_deleted(tmp_path: Path) -> None:
    """Both files carry ids (the state an old legacy-id migration produced): Delete/Reset must stick."""
    canonical = tmp_path / "Code.yaml"
    canonical.write_text("name: Code\nid: b011c0de0001\ninstructions: edited via screen\n", encoding="utf-8")
    (tmp_path / "my-code.yaml").write_text("name: Code\nid: b011c0de0001\ninstructions: stale\n", encoding="utf-8")

    reg = AgentProfileRegistry()
    reg.load_all(user_dir=tmp_path)
    assert reg.get("Code").instructions == "edited via screen"

    canonical.unlink()  # what Delete or an exact-template Reset does
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=tmp_path)

    assert "stale" not in fresh.get("Code").instructions
    assert not canonical.exists()
    assert (tmp_path / "my-code.yaml.conflict").exists()


def test_quarantine_picks_a_free_suffix(tmp_path: Path) -> None:
    (tmp_path / "Code.yaml").write_text("name: Code\nid: b011c0de0001\n", encoding="utf-8")
    (tmp_path / "my-code.yaml").write_text("name: Code\nid: b011c0de0001\ninstructions: dup\n", encoding="utf-8")
    (tmp_path / "my-code.yaml.conflict").write_text("older quarantine\n", encoding="utf-8")

    AgentProfileRegistry().load_all(user_dir=tmp_path)

    assert (tmp_path / "my-code.yaml.conflict").read_text(encoding="utf-8") == "older quarantine\n"
    assert "instructions: dup" in (tmp_path / "my-code.yaml.conflict-2").read_text(encoding="utf-8")


def test_load_user_profiles_swaps_files_whose_names_point_at_each_other(tmp_path: Path, caplog) -> None:
    """A.yaml defining B while B.yaml defines A is a pending rename pair, not a conflict."""
    (tmp_path / "A.yaml").write_text("name: B\nid: b011c0de000b\ninstructions: i am b\n", encoding="utf-8")
    (tmp_path / "B.yaml").write_text("name: A\nid: b011c0de000a\ninstructions: i am a\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        total = reg.load_all(user_dir=tmp_path)

    assert total == 6
    assert sorted(p.name for p in tmp_path.iterdir()) == ["A.yaml", "B.yaml"]
    assert "name: A" in (tmp_path / "A.yaml").read_text(encoding="utf-8")
    assert "name: B" in (tmp_path / "B.yaml").read_text(encoding="utf-8")
    assert reg.get("A").instructions == "i am a"
    assert reg.get("B").instructions == "i am b"
    assert "conflict" not in caplog.text


def test_load_user_profiles_moves_a_chain_of_pending_renames_before_judging_conflicts(tmp_path: Path) -> None:
    """X.yaml → Code.yaml is blocked only until Code.yaml (defining Foo) has moved to Foo.yaml."""
    (tmp_path / "X.yaml").write_text("name: Code\nid: b011c0de0001\ninstructions: shadow\n", encoding="utf-8")
    (tmp_path / "Code.yaml").write_text("name: Foo\nid: b011c0de0002\ninstructions: foo\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    reg.load_all(user_dir=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Code.yaml", "Foo.yaml"]
    assert reg.get("Code").instructions == "shadow"
    assert reg.get("Foo").instructions == "foo"


def test_load_user_profiles_quarantines_pending_source_whose_target_is_kept_by_a_stayer(tmp_path: Path) -> None:
    """Code.yaml (defining Foo) still yields to Foo.yaml when Foo.yaml really defines Foo."""
    (tmp_path / "X.yaml").write_text("name: Code\nid: b011c0de0001\ninstructions: shadow\n", encoding="utf-8")
    (tmp_path / "Code.yaml").write_text("name: Foo\nid: b011c0de0002\ninstructions: dup foo\n", encoding="utf-8")
    (tmp_path / "Foo.yaml").write_text("name: Foo\nid: b011c0de0003\ninstructions: real foo\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    reg.load_all(user_dir=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Code.yaml", "Code.yaml.conflict", "Foo.yaml"]
    assert reg.get("Code").instructions == "shadow"
    assert reg.get("Foo").instructions == "real foo"
    assert "dup foo" in (tmp_path / "Code.yaml.conflict").read_text(encoding="utf-8")


_REAL_RENAME = _Path.rename


def _fail_rename_to(*blocked_names: str):
    """Build a ``Path.rename`` replacement that refuses to move anything to the given file names."""

    def rename(self, target):
        if _Path(target).name in blocked_names:
            raise PermissionError(f"cannot write {target}")
        return _REAL_RENAME(self, target)

    return rename


def test_load_user_profiles_leaves_dependents_alone_when_their_blocker_cannot_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """X.yaml waits on Code.yaml; when Code.yaml fails to move away, X.yaml is skipped, not quarantined."""
    (tmp_path / "X.yaml").write_text("name: Code\nid: b011c0de0001\ninstructions: shadow\n", encoding="utf-8")
    (tmp_path / "Code.yaml").write_text("name: Foo\nid: b011c0de0002\ninstructions: foo\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(_Path, "rename", _fail_rename_to("Foo.yaml"))
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert reg.get("Code").instructions != "shadow"
    assert reg.get("Foo") is None
    assert "could not be moved to Foo.yaml" in caplog.text
    assert "Code.yaml is still occupied" in caplog.text
    assert "conflict" not in caplog.text

    monkeypatch.setattr(_Path, "rename", _REAL_RENAME)  # the blocker is "fixed" for the next load
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Code.yaml", "Foo.yaml"]
    assert fresh.get("Code").instructions == "shadow"
    assert fresh.get("Foo").instructions == "foo"


def test_load_user_profiles_leaves_cycle_alone_when_parking_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    (tmp_path / "A.yaml").write_text("name: B\nid: b011c0de000b\ninstructions: i am b\n", encoding="utf-8")
    (tmp_path / "B.yaml").write_text("name: A\nid: b011c0de000a\ninstructions: i am a\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(_Path, "rename", _fail_rename_to("A.moving.yaml", "B.moving.yaml"))
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert reg.get("A") is None
    assert reg.get("B") is None
    assert "conflict" not in caplog.text


def test_load_user_profiles_leaves_dependent_alone_when_duplicate_cannot_be_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """Y.yaml waits on X.yaml; X.yaml is a duplicate of Code that fails to quarantine, so Y.yaml stays."""
    (tmp_path / "Code.yaml").write_text("name: Code\nid: b011c0de0001\ninstructions: real\n", encoding="utf-8")
    (tmp_path / "X.yaml").write_text("name: Code\nid: b011c0de0002\ninstructions: dup\n", encoding="utf-8")
    (tmp_path / "Y.yaml").write_text("name: X\nid: b011c0de0003\ninstructions: x\n", encoding="utf-8")
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    monkeypatch.setattr(_Path, "rename", _fail_rename_to("X.yaml.conflict"))
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == before
    assert reg.get("Code").instructions == "real"
    assert reg.get("X") is None
    assert "X.yaml is still occupied" in caplog.text


def test_load_user_profiles_keeps_profile_whose_filename_differs_only_in_normalization(tmp_path: Path, caplog) -> None:
    """An NFD ``Café.yaml`` holding NFC ``name: Café`` is the canonical file, not a conflict."""
    nfc_name = unicodedata.normalize("NFC", "Café")
    nfd_file = tmp_path / unicodedata.normalize("NFD", f"{nfc_name}.yaml")
    nfd_file.write_text(f"name: {nfc_name}\nid: b011c0de00ca\ninstructions: coffee\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    # macOS resolves both spellings to this file and it stays put; elsewhere it
    # is moved to the NFC name.  Either way it is registered and never quarantined.
    listed = [unicodedata.normalize("NFC", p.name) for p in tmp_path.iterdir()]
    assert listed == [f"{nfc_name}.yaml"]
    assert reg.get(nfc_name).instructions == "coffee"
    assert "conflict" not in caplog.text


def test_load_user_profiles_keeps_profile_whose_filename_differs_in_case_and_normalization(
    tmp_path: Path, caplog
) -> None:
    """``Ϊ́.yaml`` (U+0399 U+0308 U+0301) for ``name: ΐ`` (U+0390): the two case-folds are only canonically equal."""
    # Spelled as escapes on purpose: an editor normalizing the combining marks would erase the case.
    name = "\u0390"
    stored = tmp_path / "\u0399\u0308\u0301.yaml"
    stored.write_text(f"name: {name}\nid: b011c0de0390\ninstructions: iota\n", encoding="utf-8")
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        reg.load_all(user_dir=tmp_path)

    # macOS keeps the stored spelling (it resolves both); elsewhere the file is
    # moved to ``<name>.yaml``.  Either way there is one file and it is the canonical one.
    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].samefile(tmp_path / f"{name}.yaml")
    assert reg.get(name).instructions == "iota"
    assert "conflict" not in caplog.text


def test_load_user_profiles_quarantines_hard_link_alias_of_canonical_file(tmp_path: Path) -> None:
    """samefile() is also true for a hard link under another name; only case-only aliases stay."""
    canonical = tmp_path / "Code.yaml"
    canonical.write_text("name: Code\nid: b011c0de0001\ninstructions: shadow\n", encoding="utf-8")
    alias = tmp_path / "my-code.yaml"
    try:
        os.link(canonical, alias)
    except OSError as exc:  # pragma: no cover - filesystem without hard links
        pytest.skip(f"hard links unsupported here: {exc}")
    reg = AgentProfileRegistry()

    reg.load_all(user_dir=tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["Code.yaml", "my-code.yaml.conflict"]
    canonical.unlink()  # what Delete or an exact-template Reset does
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=tmp_path)
    assert not canonical.exists()
    assert fresh.get("Code").instructions != "shadow"


def test_load_user_profiles_rejects_unsafe_names_without_touching_disk(tmp_path: Path, caplog) -> None:
    source = tmp_path / "escape.yaml"
    source.write_text("name: ../escape\ninstructions: nope\n", encoding="utf-8")
    source_bytes = source.read_bytes()
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.loader"):
        total = reg.load_all(user_dir=tmp_path)

    assert total == 4
    assert reg.get("../escape") is None
    assert source.read_bytes() == source_bytes
    assert not (tmp_path.parent / "escape.yaml").exists()
    assert "not filename-safe" in caplog.text


def test_load_user_profiles_skips_source_that_cannot_be_moved(tmp_path: Path, monkeypatch, caplog) -> None:
    from pathlib import Path as _Path

    source = tmp_path / "my-custom.yaml"
    source.write_text("name: Custom\ninstructions: custom\n", encoding="utf-8")
    source_bytes = source.read_bytes()

    def fail_rename(self, target):
        raise PermissionError("read-only agents dir")

    monkeypatch.setattr(_Path, "rename", fail_rename)
    reg = AgentProfileRegistry()

    with caplog.at_level("WARNING", logger="chrys.service.profiles.agents.registry"):
        total = reg.load_all(user_dir=tmp_path)

    assert total == 4
    assert reg.get("Custom") is None
    assert sorted(p.name for p in tmp_path.iterdir()) == ["my-custom.yaml"]
    assert source.read_bytes() == source_bytes
    assert "could not be moved to Custom.yaml" in caplog.text
