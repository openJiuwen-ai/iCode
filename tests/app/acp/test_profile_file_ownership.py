# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ACP profile mutations verify disk ownership independently of registry freshness."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import create_autospec

import pytest

from chrys.app.acp.session_manager import AcpSessionError
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, SubAgentRef, SubAgentsConfig
from chrys.service.profiles.agents.serializer import profile_to_dict, save_profile
from tests.app.acp._session_manager_fakes import _acp_manager
from tests.support.platform_fakes import platform_with_config_dir


@pytest.mark.parametrize("owner_name,request_name", [("Code", "cOdE"), ("Foo", "foo"), ("code", "Code")])
@pytest.mark.parametrize("registered", [False, True])
@pytest.mark.parametrize("same_id", [False, True])
def test_write_refuses_another_file_owner_even_with_stale_registry_or_shared_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    owner_name: str,
    request_name: str,
    registered: bool,
    same_id: bool,
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    owner = AgentProfile(name=owner_name, id="owner-id", instructions="persisted customization")
    owner.skills.paths = ["private-skills"]
    owner.memory.files = ["private.md"]
    owner_path = save_profile(owner, target_dir=user_dir)
    registry.register(owner)
    requested = AgentProfile(name=request_name, id=owner.id if same_id else "new-id", instructions="overwrite")
    if registered:
        registry.register(requested)
    target = user_dir / f"{request_name}.yaml"
    if not target.exists():
        # A case-sensitive host can exercise the same ownership decision with
        # a noncanonical file. No production reader or writer is replaced.
        target.write_bytes(owner_path.read_bytes())
    before = target.read_bytes()
    snapshot = registry.snapshot()

    with pytest.raises(AcpSessionError, match="belongs to"):
        _acp_manager(registry).write_agent_profile(profile_to_dict(requested))

    assert target.read_bytes() == before
    assert owner_path.read_bytes() == before
    assert registry.snapshot() == snapshot


@pytest.mark.parametrize("operation", ["write", "delete", "reset"])
@pytest.mark.parametrize(
    "fault",
    [
        "corrupt",
        "nameless",
        "invalid_id",
        "invalid_date",
        "unreadable",
        "removed",
        "removed_after_stat",
        "stat_error",
        "directory",
    ],
)
def test_mutations_report_unverifiable_ownership_without_changing_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str, fault: str
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    name = "Code" if operation == "reset" else "Custom"
    owner = registry.get_builtin_template(name) or AgentProfile(name=name, id="owner-id")
    owner.instructions = "persisted customization"
    path = save_profile(owner, target_dir=user_dir)
    registry.register(owner)
    if fault == "corrupt":
        path.write_text("name: [", encoding="utf-8")
    elif fault == "nameless":
        path.write_text("instructions: no name\n", encoding="utf-8")
    elif fault == "invalid_id":
        path.write_text(f"name: {name}\nid: 42\n", encoding="utf-8")
    elif fault == "invalid_date":
        path.write_text(f"name: {name}\nid: 2026-13-01\n", encoding="utf-8")
    before = path.read_bytes()
    if fault == "directory":
        path.unlink()
        path.mkdir()
    snapshot = registry.snapshot()
    real_read_bytes = Path.read_bytes
    real_lstat = Path.lstat

    def read_after_edit(target: Path) -> bytes:
        if target == path:
            if fault == "unreadable":
                raise PermissionError("cannot read profile")
            if fault == "removed":
                target.unlink()
        return real_read_bytes(target)

    def inspect_profile(target: Path):
        if target == path and fault == "stat_error":
            raise PermissionError("cannot inspect profile")
        result = real_lstat(target)
        if target == path and fault == "removed_after_stat":
            target.unlink()
        return result

    manager = _acp_manager(registry)
    with monkeypatch.context() as filesystem:
        filesystem.setattr(Path, "read_bytes", create_autospec(Path.read_bytes, side_effect=read_after_edit))
        filesystem.setattr(Path, "lstat", create_autospec(Path.lstat, side_effect=inspect_profile))
        with pytest.raises(AcpSessionError, match="Cannot verify ownership") as error:
            if operation == "write":
                manager.write_agent_profile({"name": name, "instructions": "replacement"})
            elif operation == "delete":
                manager.delete_agent_profile(name)
            else:
                manager.reset_agent_profile(name)

    assert str(path) in str(error.value)
    assert registry.snapshot() == snapshot
    if fault in {"removed", "removed_after_stat"}:
        assert not path.exists()
    elif fault == "directory":
        assert path.is_dir()
    else:
        assert path.read_bytes() == before


def test_reset_refuses_overwriting_a_twin_when_integrations_require_a_shadow(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    builtin = registry.get("Code")
    assert builtin is not None
    builtin.instructions = "custom instructions"
    builtin.skills.paths = ["preserved-skills"]
    target = user_dir / "Code.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text("name: code\nid: twin-id\ninstructions: private twin\n", encoding="utf-8")
    before = target.read_bytes()
    snapshot = registry.snapshot()

    with pytest.raises(AcpSessionError, match="belongs to"):
        _acp_manager(registry).reset_agent_profile("Code")

    assert target.read_bytes() == before
    assert registry.snapshot() == snapshot


@pytest.mark.parametrize("name", ["Code", "Custom"])
def test_create_update_and_remove_owned_files_remain_persistent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    profile = registry.get_builtin_template(name) or AgentProfile(name=name, id="custom-id")
    manager = _acp_manager(registry)
    payload = profile_to_dict(profile)
    payload["instructions"] = "first customization"
    manager.write_agent_profile(payload)
    payload["instructions"] = "updated customization"
    manager.write_agent_profile(payload)
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    persisted = fresh.get(name)
    assert persisted is not None
    expected_prompt = (
        registry.get_builtin_template(name).instructions if registry.is_builtin(name) else "updated customization"
    )
    assert persisted.instructions == expected_prompt
    assert persisted.id == profile.id

    if name == "Code":
        assert manager.reset_agent_profile(name)["changed"] is True
    else:
        assert manager.delete_agent_profile(name) == {"name": name, "deleted": True}

    assert not (user_dir / f"{name}.yaml").exists()
    reloaded = AgentProfileRegistry()
    reloaded.load_all(user_dir=user_dir)
    assert reloaded.get(name) == reloaded.get_builtin_template(name)


@pytest.mark.parametrize("owner_name,request_name", [("Code", "cOdE"), ("Foo", "foo")])
def test_case_variant_creation_respects_the_actual_filesystem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, owner_name: str, request_name: str
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    manager = _acp_manager(registry)
    owner = registry.get_builtin_template(owner_name) or AgentProfile(name=owner_name, id="owner-id")
    owner.instructions = "persisted customization"
    manager.write_agent_profile(profile_to_dict(owner))
    target = user_dir / f"{request_name}.yaml"
    aliases = target.exists()
    owner_path = user_dir / f"{owner_name}.yaml"
    before = owner_path.read_bytes()
    payload = {"name": request_name, "id": "new-id", "instructions": "new profile"}

    if aliases:
        with pytest.raises(AcpSessionError, match="belongs to"):
            manager.write_agent_profile(payload)
        assert registry.get(request_name) is None
    else:
        manager.write_agent_profile(payload)
        assert registry.get(request_name).instructions == "new profile"
    assert owner_path.read_bytes() == before
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    registry.enforce_builtin_prompts(owner)
    assert fresh.get(owner_name) == owner
    assert (fresh.get(request_name) is None) is aliases


def test_reset_preserves_a_known_foreign_owner_even_when_not_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    target = user_dir / "Code.yaml"
    target.parent.mkdir(exist_ok=True)
    target.write_text("name: code\nid: twin-id\ninstructions: private twin\n", encoding="utf-8")
    before = target.read_bytes()
    assert registry.get("code") is None

    result = _acp_manager(registry).reset_agent_profile("Code")

    assert result["changed"] is False
    assert target.read_bytes() == before


@pytest.mark.parametrize("fault", ["foreign", "corrupt", "invalid_id", "removed"])
def test_delete_preflights_all_cascade_files_before_any_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    child = AgentProfile(name="Child", id="child-id")
    safe_parent = AgentProfile(
        name="Safe", id="safe-id", sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Child")])
    )
    stale_parent = AgentProfile(
        name="foo",
        id="stale-id",
        instructions="stale parent",
        sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Child")]),
    )
    for profile in (child, safe_parent, stale_parent):
        save_profile(profile, target_dir=user_dir)
        registry.register(profile)
    target = user_dir / "foo.yaml"
    if fault == "foreign":
        target.write_text("name: Foo\nid: owner-id\ninstructions: protected owner\n", encoding="utf-8")
    elif fault == "corrupt":
        target.write_text("name: [", encoding="utf-8")
    elif fault == "invalid_id":
        target.write_text("name: foo\nid: 42\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in user_dir.glob("*.yaml")}
    snapshot = registry.snapshot()
    real_read_bytes = Path.read_bytes

    def read_after_removal(path: Path) -> bytes:
        if path == target and fault == "removed":
            path.unlink()
        return real_read_bytes(path)

    with monkeypatch.context() as filesystem:
        filesystem.setattr(Path, "read_bytes", create_autospec(Path.read_bytes, side_effect=read_after_removal))
        with pytest.raises(AcpSessionError, match=r"belongs to|Cannot verify ownership"):
            _acp_manager(registry).delete_agent_profile("Child")

    assert registry.snapshot() == snapshot
    for path, content in before.items():
        if path == target and fault == "removed":
            assert not path.exists()
        else:
            assert path.read_bytes() == content


def test_delete_persists_owned_cascade_without_saving_builtin_or_deleted_self_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    for name in ("Child", "Parent"):
        profile = AgentProfile(
            name=name, id=f"{name}-id", sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Child")])
        )
        save_profile(profile, target_dir=user_dir)
        registry.register(profile)
    builtin = registry.get("Code")
    assert builtin is not None
    builtin.sub_agents.agents.append(SubAgentRef(profile="Child"))
    builtin_path = user_dir / "Code.yaml"
    builtin_path.write_text("name: code\nid: foreign-id\n", encoding="utf-8")
    before = builtin_path.read_bytes()

    assert _acp_manager(registry).delete_agent_profile("Child") == {"name": "Child", "deleted": True}

    assert not (user_dir / "Child.yaml").exists()
    assert not registry.get("Parent").sub_agents.agents
    assert "Child" not in [ref.profile for ref in builtin.sub_agents.agents]
    assert builtin_path.read_bytes() == before
    fresh = AgentProfileRegistry()
    fresh.load_all(user_dir=user_dir)
    assert fresh.get("Child") is None
    assert not fresh.get("Parent").sub_agents.agents


@pytest.mark.parametrize("parent_names", [("Parent",), ("foo", "Foo")])
def test_delete_refuses_to_recreate_missing_cascade_parents(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, parent_names: tuple[str, ...]
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    user_dir = tmp_path / "agents"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=user_dir)
    child = AgentProfile(name="Child", id="child-id")
    child_path = save_profile(child)
    registry.register(child)
    for name in parent_names:
        registry.register(
            AgentProfile(
                name=name,
                id=f"{name}-id",
                sub_agents=SubAgentsConfig(agents=[SubAgentRef(profile="Child")]),
            )
        )
    before = child_path.read_bytes()
    snapshot = registry.snapshot()

    with pytest.raises(AcpSessionError, match=r"Parent agent profile file .* is missing"):
        _acp_manager(registry).delete_agent_profile("Child")

    assert registry.snapshot() == snapshot
    assert child_path.read_bytes() == before
    assert list(user_dir.glob("*.yaml")) == [child_path]


@pytest.mark.parametrize("operation", ["write", "delete", "reset"])
@pytest.mark.parametrize("owner", ["foreign", "corrupt", "matching"])
def test_mutations_check_the_actual_storage_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, operation: str, owner: str
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    loaded_dir = tmp_path / "loaded"
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=loaded_dir)
    name = "Code" if operation == "reset" else "Custom"
    profile = registry.get_builtin_template(name) or AgentProfile(name=name, id="custom-id")
    profile.instructions = "persisted customization"
    profile.model.profile_id = "resettable-model"
    # Reset must retain a shadow, exercising its save branch.
    profile.skills.paths = ["preserved-skills"]
    registry.register(profile)
    target = save_profile(profile)
    if owner == "foreign":
        target.write_text("name: Other\nid: other-id\ninstructions: protected\n", encoding="utf-8")
    elif owner == "corrupt":
        target.write_text("name: [", encoding="utf-8")
    else:
        # A conflicting file in the registry's source directory is irrelevant
        # to mutations in the serializer's destination directory.
        loaded_dir.mkdir(exist_ok=True)
        (loaded_dir / f"{name}.yaml").write_text("name: Other\nid: other-id\n", encoding="utf-8")
    before = target.read_bytes()
    snapshot = registry.snapshot()
    manager = _acp_manager(registry)

    def mutate() -> dict[str, object]:
        if operation == "write":
            return manager.write_agent_profile({"name": name, "instructions": "replacement"})
        if operation == "delete":
            return manager.delete_agent_profile(name)
        return manager.reset_agent_profile(name)

    if owner == "matching":
        mutate()
        if operation == "delete":
            assert not target.exists()
        else:
            assert target.read_bytes() != before
    else:
        with pytest.raises(AcpSessionError, match=r"belongs to|Cannot verify ownership"):
            mutate()
        assert registry.snapshot() == snapshot
        assert target.read_bytes() == before


def test_reset_preserves_a_foreign_file_in_the_actual_storage_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    platform = platform_with_config_dir(tmp_path)
    monkeypatch.setattr("chrys.foundation.platform.get_platform", lambda: platform)
    registry = AgentProfileRegistry()
    registry.load_all(user_dir=tmp_path / "loaded")
    builtin = registry.get("Code")
    assert builtin is not None
    builtin.instructions = "customized in memory"
    target = save_profile(builtin)
    target.write_text("name: code\nid: other-id\ninstructions: protected\n", encoding="utf-8")
    before = target.read_bytes()

    assert _acp_manager(registry).reset_agent_profile("Code")["changed"] is True

    assert registry.get("Code") == registry.get_builtin_template("Code")
    assert target.read_bytes() == before
