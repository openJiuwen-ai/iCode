# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Builtin prompt ownership and interruption-safe migration of user overrides."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy

import pytest

from chrys.service.profiles.agents import prompt_migration
from chrys.service.profiles.agents.loader import load_profile_from_yaml
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.schema import AgentProfile, SubAgentRef
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.profiles.file_scan import read_profile_mapping
from chrys.service.profiles.reload import ProfileReload


def builtin_registry(directory):
    registry = AgentProfileRegistry()
    registry.load_all(directory)
    return registry


@pytest.mark.parametrize("name", ["Code", "QA", "Explore", "General"])
def test_builtin_prompts_follow_package_and_legacy_profile_survives(tmp_path, name):
    registry = builtin_registry(tmp_path)
    template = registry.get_builtin_template(name)
    old = deepcopy(template)
    old.instructions = "My original prompt"
    old.compaction.last_words_template = "My original memory prompt"
    old.compaction.last_words_max_output_tokens = 7000
    old.model.profile_id = "my-model"
    old.memory.files = ["mine.md"]
    save_profile(old, target_dir=tmp_path)

    registry = builtin_registry(tmp_path)
    current = registry.get(name)
    assert current.instructions == template.instructions
    assert current.compaction.last_words_template == template.compaction.last_words_template
    assert current.model == old.model and current.memory == old.memory
    assert current.compaction.last_words_max_output_tokens == 7000
    backup = registry.get(f"{name}-Modified")
    assert backup.instructions == old.instructions
    assert backup.compaction == old.compaction and backup.model == old.model
    assert backup.memory == old.memory and backup.id != old.id
    assert registry.get_by_id(backup.id) is backup
    assert not registry.is_builtin(backup.name)
    data = read_profile_mapping((tmp_path / f"{name}.yaml").read_text())
    assert "instructions" not in data and "last_words_template" not in data["compaction"]

    registry._builtin_profiles[name].instructions = "New package prompt"
    registry._builtin_profiles[name].compaction.last_words_template = ""
    current.instructions = "Do not persist this"
    current.compaction.last_words_template = "Do not persist this either"
    registry.save(current)
    registry.load_user_profiles(tmp_path)
    assert registry.get(name).instructions == "New package prompt"
    assert registry.get(name).compaction.last_words_template == ""
    assert registry.get(backup.name).instructions == old.instructions
    assert sorted(p.name for p in tmp_path.glob("*-Modified*.yaml")) == [f"{name}-Modified.yaml"]


@pytest.mark.parametrize("description", ["User's own investigation agent", ""])
def test_backups_retarget_only_backed_up_children_and_keep_tool_names(tmp_path, description):
    registry = builtin_registry(tmp_path)
    code = registry.get_builtin_template("Code")
    code.sub_agents.agents = [SubAgentRef("Explore"), SubAgentRef("General", tool_name="general_agent")]
    explore = registry.get_builtin_template("Explore")
    explore.description = description
    save_profile(code, target_dir=tmp_path)
    save_profile(explore, target_dir=tmp_path)
    registry = builtin_registry(tmp_path)
    refs = registry.get("Code-Modified").sub_agents.agents
    assert [(r.profile, r.tool_name) for r in refs] == [("Explore-Modified", "Explore"), ("General", "general_agent")]
    assert registry.get("Explore-Modified").description == explore.description
    assert refs[0].tool_description == (description or f"Delegate task to {explore.display_name}")
    assert registry.get("Code").sub_agents.agents[0].profile == "Explore"
    assert registry.get("General-Modified") is None


def test_migration_precedes_missing_id_and_plan_rewrites(tmp_path):
    path = tmp_path / "Code.yaml"
    path.write_text("name: Code\ninstructions: old prompt\nsub_agents:\n  agents: [{profile: Plan}]\n")
    registry = builtin_registry(tmp_path)
    assert registry.get("Code-Modified").instructions == "old prompt"
    assert registry.get("Code").id == registry.get_builtin_template("Code").id
    assert not registry.get("Code").sub_agents.agents
    assert "instructions" not in read_profile_mapping(path.read_text())


@pytest.mark.parametrize("stage", ["backup", "cleanup"])
def test_interrupted_migration_reuses_planned_backups(tmp_path, monkeypatch, stage):
    registry = builtin_registry(tmp_path)
    for name in ("Code", "Explore"):
        p = registry.get_builtin_template(name)
        p.instructions = f"old {name}"
        save_profile(p, target_dir=tmp_path)
    write = prompt_migration.atomic_write_owner_only_text

    def fail(path, text, **kwargs):
        if (stage == "backup" and path.name == "Explore-Modified.yaml") or (
            stage == "cleanup" and path.name == "Explore.yaml"
        ):
            raise OSError("simulated interruption")
        return write(path, text, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(prompt_migration, "atomic_write_owner_only_text", fail)
        with pytest.raises(OSError, match="interruption"):
            registry.migrate_builtin_prompts()
    assert (tmp_path / "Code-Modified.yaml").exists()
    registry = builtin_registry(tmp_path)
    assert registry.get("Code-Modified").instructions == "old Code"
    assert registry.get("Explore-Modified").instructions == "old Explore"
    assert registry.get("Code-Modified").sub_agents.agents[0].profile == "Explore-Modified"
    assert not list(tmp_path.glob("*-Modified-2.yaml"))


@pytest.mark.parametrize("change", ["source", "backup"])
def test_retry_preserves_user_edits_and_allocates_new_backup(tmp_path, monkeypatch, change):
    registry = builtin_registry(tmp_path)
    profile = registry.get_builtin_template("Code")
    profile.instructions = "first version"
    save_profile(profile, target_dir=tmp_path)
    write = prompt_migration.atomic_write_owner_only_text

    def fail(path, text, **kwargs):
        if path.name == "Code.yaml":
            raise OSError("cannot clean up")
        return write(path, text, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(prompt_migration, "atomic_write_owner_only_text", fail)
        with pytest.raises(OSError):
            registry.migrate_builtin_prompts()
    target = tmp_path / ("Code.yaml" if change == "source" else "Code-Modified.yaml")
    edited = load_profile_from_yaml(target)
    edited.instructions = "user's newer version"
    save_profile(edited, target_dir=tmp_path)
    registry = builtin_registry(tmp_path)
    assert registry.get("Code-Modified").instructions == (
        "first version" if change == "source" else "user's newer version"
    )
    assert registry.get("Code-Modified-2").instructions == (
        "user's newer version" if change == "source" else "first version"
    )


def test_backup_names_respect_file_and_display_collisions(tmp_path):
    registry = builtin_registry(tmp_path)
    save_profile(registry.get_builtin_template("Code"), target_dir=tmp_path)
    save_profile(AgentProfile(name="code-modified", instructions="keep this"), target_dir=tmp_path)
    save_profile(AgentProfile(name="Other", display_name="Code Agent (Modified 2)"), target_dir=tmp_path)
    registry = builtin_registry(tmp_path)
    assert registry.get("code-modified").instructions == "keep this"
    assert registry.get("Code-Modified-3") is not None


def test_unrelated_invalid_file_does_not_block_save_or_lose_filename_ownership(tmp_path):
    registry = builtin_registry(tmp_path)
    profile = registry.get_builtin_template("Code")
    profile.instructions = "unbacked user prompt"
    path = save_profile(profile, target_dir=tmp_path)
    broken = tmp_path / "Code-Modified.yaml"
    broken.write_text("name: [broken")
    registry.load_user_profiles(tmp_path)
    updated = registry.get("Code")
    updated.memory.files = ["notes.md"]
    registry.save(updated)
    assert broken.read_text() == "name: [broken"
    assert load_profile_from_yaml(tmp_path / "Code-Modified-2.yaml").instructions == "unbacked user prompt"
    assert load_profile_from_yaml(path).memory.files == ["notes.md"]


def test_unreadable_target_blocks_save_with_clear_error(tmp_path):
    registry = builtin_registry(tmp_path)
    source = tmp_path / "Code.yaml"
    source.write_text("name: [broken")
    with pytest.raises(OSError, match=r"Cannot read target agent profile 'Code'.*original file was preserved"):
        registry.save(registry.get("Code"))
    assert source.read_text() == "name: [broken"


def test_directory_scan_failure_blocks_save_even_without_visible_sources(tmp_path, monkeypatch):
    from chrys.service.profiles.file_scan import ProfileDirScan

    registry = builtin_registry(tmp_path)
    source = save_profile(registry.get_builtin_template("Code"), target_dir=tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(
        prompt_migration, "scan_profile_dir", lambda _: ProfileDirScan(directory_error=OSError("cannot list directory"))
    )
    with pytest.raises(OSError, match="Cannot read the agent profile directory"):
        registry.save(registry.get("Code"))
    assert source.read_bytes() == before


@pytest.mark.parametrize("legacy_completed", [False, True])
def test_recovery_removes_journal_with_credentials_after_all_sources_cleaned(tmp_path, monkeypatch, legacy_completed):
    from chrys.service.profiles.agents.schema import MCPServerConfig

    registry = builtin_registry(tmp_path)
    profile = registry.get_builtin_template("Code")
    profile.tools.mcp = [
        MCPServerConfig(
            name="test", transport="http", url="https://example.invalid", headers={"Authorization": "dummy-secret"}
        )
    ]
    save_profile(profile, target_dir=tmp_path)
    journal = tmp_path / ".builtin-prompt-migration.json"

    def interrupted(_path):
        raise OSError("interrupted before journal removal")

    with monkeypatch.context() as patch:
        patch.setattr(prompt_migration, "_discard_plan", interrupted)
        with pytest.raises(OSError, match="before journal removal"):
            registry.migrate_builtin_prompts()
    assert "dummy-secret" in journal.read_text()
    assert not prompt_migration.needs_prompt_migration((tmp_path / "Code.yaml").read_text())
    if legacy_completed:
        data = json.loads(journal.read_text())
        data["completed"] = True
        journal.write_text(json.dumps(data))
    builtin_registry(tmp_path)
    assert not journal.exists()
    assert [p.name for p in tmp_path.glob("*-Modified*.yaml")] == ["Code-Modified.yaml"]


def test_interrupted_plan_reuses_old_identity_when_new_source_appears(tmp_path, monkeypatch):
    registry = builtin_registry(tmp_path)
    save_profile(registry.get_builtin_template("Code"), target_dir=tmp_path)
    write = prompt_migration.atomic_write_owner_only_text

    def fail_cleanup(path, text):
        if path == tmp_path / "Code.yaml":
            raise OSError("interrupted cleanup")
        write(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(prompt_migration, "atomic_write_owner_only_text", fail_cleanup)
        with pytest.raises(OSError, match="interrupted cleanup"):
            registry.migrate_builtin_prompts()
    original_backup = (tmp_path / "Code-Modified.yaml").read_bytes()
    qa = registry.get_builtin_template("QA")
    qa.sub_agents.agents = [SubAgentRef("Code")]
    save_profile(qa, target_dir=tmp_path)
    registry = builtin_registry(tmp_path)
    assert (tmp_path / "Code-Modified.yaml").read_bytes() == original_backup
    assert sorted(p.name for p in tmp_path.glob("*-Modified*.yaml")) == ["Code-Modified.yaml", "QA-Modified.yaml"]
    ref = registry.get("QA-Modified").sub_agents.agents[0]
    assert ref.profile == "Code-Modified" and ref.tool_name == "Code"
    assert ref.tool_description == registry.get_builtin_template("Code").description
    assert not (tmp_path / ".builtin-prompt-migration.json").exists()


@pytest.mark.parametrize(
    "prompts,expected",
    [
        ("", False),
        ('instructions: ""\n', False),
        ('instructions: " \\t"\ncompaction: {last_words_template: "  "}\n', False),
        ('instructions: ["", "  "]\n', False),
        ('instructions: ["", "custom"]\n', True),
        ("compaction: {last_words_template: custom}\n", True),
        ("_builtin_prompts_managed: true\ninstructions: custom\n", True),
    ],
)
def test_only_nonempty_prompts_create_backups(tmp_path, prompts, expected):
    source = tmp_path / "Code.yaml"
    text = "name: Code\nid: b011c0de0001\nmemory: {files: [notes.md]}\n" + prompts
    source.write_text(text)
    registry = builtin_registry(tmp_path)
    assert (registry.get("Code-Modified") is not None) is expected
    assert registry.get("Code").memory.files == ["notes.md"]
    if not expected:
        assert source.read_text() == text
        assert not registry.prompt_backups
        assert not (tmp_path / ".builtin-prompt-migration.json").exists()


def test_clean_readonly_load_does_not_acquire_write_lock(tmp_path, monkeypatch):
    from chrys.service.profiles.agents import serializer

    registry = builtin_registry(tmp_path)
    registry.save(registry.get("Code"))

    def forbidden(*args, **kwargs):
        pytest.fail("readonly load acquired a write lock")

    monkeypatch.setattr(serializer, "profile_directory_lock", forbidden)
    assert builtin_registry(tmp_path).get("Code").instructions


def test_clone_reserves_drafts_and_preserves_self_reference_interface(tmp_path):
    registry = builtin_registry(tmp_path)
    source = AgentProfile(name="Custom", display_name="My Agent", instructions="prompt")
    source.sub_agents.agents = [SubAgentRef("Custom"), SubAgentRef("Code", tool_name="coder")]
    first = registry.clone(source)
    second = registry.clone(source, reserved_profiles=[first])
    assert first.name == "Custom-copy" and second.name == "Custom-copy-2"
    assert len({source.id, first.id, second.id}) == 3
    assert first.sub_agents.agents[0].profile == first.name
    assert second.sub_agents.agents[0].profile == second.name
    assert second.sub_agents.agents[0].tool_name == "Custom"
    assert second.sub_agents.agents[0].tool_description == "Delegate task to My Agent"
    assert second.sub_agents.agents[1] == source.sub_agents.agents[1]


def test_restore_and_reload_enforce_builtin_prompts(tmp_path):
    registry = builtin_registry(tmp_path)
    expected = registry.get_builtin_template("Code")
    snapshot = registry.snapshot()
    snapshot.profiles["Code"].instructions = "snapshot override"
    snapshot.profiles["Code"].compaction.last_words_template = "snapshot override"
    registry.restore(snapshot)
    assert registry.get("Code").instructions == expected.instructions
    view = registry.reload_base()
    view._profiles["Code"].instructions = "reload override"
    registry.install_reload(view)
    assert registry.get("Code").instructions == expected.instructions
    assert registry.get("Code").compaction.last_words_template == expected.compaction.last_words_template


def test_hot_reload_backs_up_before_staged_rewrites(tmp_path):
    registry = builtin_registry(tmp_path)
    (tmp_path / "Code.yaml").write_text(
        "name: Code\ninstructions: old hot reload prompt\nsub_agents:\n  agents: [{profile: Plan}]\n"
    )
    reload = ProfileReload(registry, None)
    reload.scan()
    assert not (tmp_path / "Code-Modified.yaml").exists()
    reload.normalize()
    reload.install()
    assert registry.get("Code-Modified").instructions == "old hot reload prompt"
    assert registry.get("Code").instructions == registry.get_builtin_template("Code").instructions
    assert not registry.get("Code").sub_agents.agents


def test_hot_reload_preserves_rewrites_of_already_managed_builtins(tmp_path):
    registry = builtin_registry(tmp_path)
    (tmp_path / "Code.yaml").write_text("name: Code\n_builtin_prompts_managed: true\n", encoding="utf-8")
    (tmp_path / "Explore.yaml").write_text("name: Explore\ninstructions: old explore\n", encoding="utf-8")
    reload = ProfileReload(registry, None)
    reload.scan()
    reload.normalize()
    reload.install()
    assert load_profile_from_yaml(tmp_path / "Code.yaml").id == registry.get_builtin_template("Code").id
    assert registry.get("Code-Modified") is None
    assert registry.get("Explore-Modified").instructions == "old explore"


def test_readonly_profile_directory_still_loads_without_losing_old_prompts(tmp_path, monkeypatch):
    from chrys.foundation.util.lock import FileLock

    source = tmp_path / "Code.yaml"
    source.write_text(
        "name: Code\ninstructions: old readonly prompt\nmodel: {profile_id: my-model}\n", encoding="utf-8"
    )
    before = source.read_bytes()

    def denied(_lock):
        raise PermissionError("readonly directory")

    monkeypatch.setattr(FileLock, "acquire", denied)
    registry = builtin_registry(tmp_path)
    assert registry.get("Code").model.profile_id == "my-model"
    assert registry.get("Code").instructions == registry.get_builtin_template("Code").instructions
    assert source.read_bytes() == before
    assert not list(tmp_path.glob("*-Modified*.yaml"))
    with pytest.raises(PermissionError):
        registry.save(registry.get("Code"))


def test_full_snapshot_serialization_preserves_builtin_named_candidate(tmp_path):
    registry = builtin_registry(tmp_path)
    candidate = registry.get_builtin_template("Code")
    candidate.instructions = "experimental candidate"
    path = save_profile(candidate, target_dir=tmp_path / "experiment")
    assert load_profile_from_yaml(path).instructions == "experimental candidate"
    clone = registry.clone(candidate)
    registry.register(clone)
    assert clone.id != candidate.id and clone.name != candidate.name
    assert registry.get_by_id(clone.id).instructions == "experimental candidate"


def test_migration_fingerprints_original_encoding(tmp_path):
    (tmp_path / "Code.yaml").write_text("name: Code\ninstructions: 保留旧提示词\n", encoding="utf-16")
    registry = builtin_registry(tmp_path)
    assert registry.get("Code-Modified").instructions == "保留旧提示词"
    assert registry.get("Code").instructions == registry.get_builtin_template("Code").instructions


def test_migration_preserves_ignored_reference_mappings_without_misaligning_children(tmp_path):
    (tmp_path / "Code.yaml").write_text(
        "name: Code\ninstructions: old prompt\nsub_agents:\n  agents:\n"
        "    - {tool_name: ignored-without-profile}\n    - {profile: Explore}\n"
    )
    (tmp_path / "Explore.yaml").write_text("name: Explore\ninstructions: old explore\n")
    registry = builtin_registry(tmp_path)
    refs = read_profile_mapping((tmp_path / "Code-Modified.yaml").read_text())["sub_agents"]["agents"]
    assert refs[0] == {"tool_name": "ignored-without-profile"}
    assert refs[1]["profile"] == "Explore-Modified"
    assert registry.get("Code-Modified").sub_agents.agents[0].tool_name == "Explore"


def test_another_process_cannot_migrate_while_editor_owns_directory(tmp_path):
    from chrys.foundation.platform.process import windows_hidden_subprocess_kwargs
    from chrys.service.profiles.agents.serializer import profile_directory_lock
    from tests.support.waiting import ENGINE_TURN_TIMEOUT

    source = tmp_path / "Code.yaml"
    source.write_text("name: Code\ninstructions: original prompt\n", encoding="utf-8")
    before = source.read_bytes()
    script = """
import sys
from pathlib import Path
from chrys.service.profiles.agents.registry import AgentProfileRegistry
try:
    registry = AgentProfileRegistry()
    registry.load_all(Path(sys.argv[1]))
    if len(sys.argv) > 2:
        registry.save(registry.get("Code"))
    print('loaded')
except TimeoutError:
    print('busy')
"""
    env = {**os.environ, "HOME": str(tmp_path), "USERPROFILE": str(tmp_path), "APPDATA": str(tmp_path)}

    def run(*args):
        return subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), *args],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=ENGINE_TURN_TIMEOUT,
            check=True,
            **windows_hidden_subprocess_kwargs(),
        ).stdout.strip()

    with profile_directory_lock(tmp_path):
        assert run() == "loaded"
        assert run("save") == "busy"
        assert source.read_bytes() == before
        assert not list(tmp_path.glob("*-Modified*.yaml"))
    assert run() == "loaded"
    assert run() == "loaded"
    assert [p.name for p in tmp_path.glob("*-Modified*.yaml")] == ["Code-Modified.yaml"]


@pytest.mark.parametrize("payload", [b'{"version":1,"entries":[', b"\xff", b"[]", b'{"version":1,"entries":null}'])
@pytest.mark.parametrize("has_prompts", [False, True])
def test_corrupt_journal_is_quarantined_without_blocking_save_or_replacing_backups(tmp_path, payload, has_prompts):
    registry = builtin_registry(tmp_path)
    source = registry.get_builtin_template("Code")
    source.instructions = "preserve this prompt"
    if has_prompts:
        save_profile(source, target_dir=tmp_path)
    else:
        registry.save(source)
    existing = AgentProfile(name="Code-Modified", id="existing-copy", instructions="existing backup")
    backup_path = save_profile(existing, target_dir=tmp_path)
    before = backup_path.read_bytes()
    journal = tmp_path / ".builtin-prompt-migration.json"
    journal.write_bytes(payload)
    archive = journal.with_name(journal.name + ".corrupt")
    archive.write_bytes(b"previous archive")
    updated = registry.get("Code")
    updated.description = "updated description"
    registry.save(updated)
    assert load_profile_from_yaml(tmp_path / "Code.yaml").description == "updated description"
    assert backup_path.read_bytes() == before
    if has_prompts:
        assert load_profile_from_yaml(tmp_path / "Code-Modified-2.yaml").instructions == "preserve this prompt"
    else:
        assert not (tmp_path / "Code-Modified-2.yaml").exists()
    assert archive.read_bytes() == b"previous archive"
    assert journal.with_name(journal.name + ".corrupt-2").read_bytes() == payload
    assert not journal.exists()


def _leave_interrupted_plan(registry, directory, monkeypatch):
    save_profile(registry.get_builtin_template("Code"), target_dir=directory)
    write = prompt_migration.atomic_write_owner_only_text

    def fail_cleanup(path, text):
        if path == directory / "Code.yaml":
            raise OSError("interrupted cleanup")
        write(path, text)

    with monkeypatch.context() as patch:
        patch.setattr(prompt_migration, "atomic_write_owner_only_text", fail_cleanup)
        with pytest.raises(OSError, match="interrupted cleanup"):
            registry.migrate_builtin_prompts()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_text", "name: [broken"),
        ("backup_text", "name: Wrong\nid: wrong\ninstructions: backup\n"),
        ("cleaned_text", "name: QA\ninstructions: must not overwrite source\n"),
    ],
)
def test_journal_with_invalid_embedded_profile_recovers_on_load(tmp_path, monkeypatch, field, value):
    registry = builtin_registry(tmp_path)
    _leave_interrupted_plan(registry, tmp_path, monkeypatch)
    backup = (tmp_path / "Code-Modified.yaml").read_bytes()
    journal = tmp_path / ".builtin-prompt-migration.json"
    data = json.loads(journal.read_text())
    data["entries"][0][field] = value
    payload = json.dumps(data)
    journal.write_text(payload)
    fresh = builtin_registry(tmp_path)
    assert fresh.get("Code").instructions == fresh.get_builtin_template("Code").instructions
    assert fresh.get("Code-Modified-2").instructions == fresh.get_builtin_template("Code").instructions
    assert (tmp_path / "Code-Modified.yaml").read_bytes() == backup
    assert journal.with_name(journal.name + ".corrupt").read_text() == payload
    assert not journal.exists()


@pytest.mark.parametrize("completed", [False, True])
def test_newer_journal_version_is_preserved_and_explained(tmp_path, completed):
    registry = builtin_registry(tmp_path)
    source = save_profile(registry.get_builtin_template("Code"), target_dir=tmp_path)
    before = source.read_bytes()
    journal = tmp_path / ".builtin-prompt-migration.json"
    payload = json.dumps({"version": 2, "completed": completed, "entries": []})
    journal.write_text(payload)
    with pytest.raises(OSError, match="version 2 requires a newer iCode version"):
        registry.save(registry.get("Code"))
    assert source.read_bytes() == before
    assert journal.read_text() == payload
    assert not list(tmp_path.glob("*.corrupt*"))


def test_retry_does_not_duplicate_id_of_user_renamed_backup(tmp_path, monkeypatch):
    registry = builtin_registry(tmp_path)
    _leave_interrupted_plan(registry, tmp_path, monkeypatch)
    original_path = tmp_path / "Code-Modified.yaml"
    renamed = load_profile_from_yaml(original_path)
    renamed.name = "Renamed"
    renamed_path = save_profile(renamed, target_dir=tmp_path)
    before = renamed_path.read_bytes()
    original_path.unlink()
    registry = builtin_registry(tmp_path)
    recovered = registry.get("Code-Modified-2")
    assert recovered is not None and recovered.id != renamed.id
    assert registry.get_by_id(renamed.id).name == "Renamed"
    assert registry.get_by_id(recovered.id) is recovered
    assert renamed_path.read_bytes() == before
    assert not (tmp_path / ".builtin-prompt-migration.json").exists()


def test_profile_lock_preserves_transaction_timeout_and_releases_lock(tmp_path):
    from chrys.foundation.util.lock import FileLock
    from chrys.service.profiles.agents.serializer import profile_directory_lock

    failure = TimeoutError("actual write timeout")
    with pytest.raises(TimeoutError, match="actual write timeout") as caught, profile_directory_lock(tmp_path):
        raise failure
    assert caught.value is failure
    with FileLock(tmp_path / ".profiles.lock", timeout=0):
        pass
