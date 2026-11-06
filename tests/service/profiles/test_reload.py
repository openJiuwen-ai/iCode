# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reconcile disk profile definitions without publishing an unbuilt candidate."""

from __future__ import annotations

from pathlib import Path

import pytest

from chrys.foundation.config.errors import ConfigError
from chrys.service.profiles.agents.registry import AgentProfileRegistry
from chrys.service.profiles.agents.serializer import save_profile
from chrys.service.profiles.models.registry import ModelProfileRegistry
from chrys.service.profiles.reload import ProfileReload


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def documents(tmp_path):
    agents, models = AgentProfileRegistry(), ModelProfileRegistry()
    agents.load_all(tmp_path / "agents")
    models.load_all(tmp_path / "models")
    return agents, models


def test_scan_prepares_agent_rename_missing_id_and_plan_cleanup_without_writing(tmp_path):
    agents, models = documents(tmp_path)
    old = agents.directory / "Old.yaml"
    raw = "name: New\nsub_agents:\n  agents: [{profile: Plan}, {profile: Explore}]\n"
    write(old, raw)
    candidate = ProfileReload(agents, models)
    candidate.scan()
    assert old.read_text(encoding="utf-8") == raw
    assert agents.get("New") is None
    profile = candidate.agents.get("New")
    assert profile.id
    assert [item.profile for item in profile.sub_agents.agents] == ["Explore"]
    candidate.normalize()
    candidate.check_versions()
    candidate.install()
    assert not old.exists()
    assert agents.get("New").id == profile.id
    again = AgentProfileRegistry()
    again.load_all(agents.directory)
    assert again.get("New") == profile
    save_profile(profile, target_dir=agents.directory)
    assert {p.name for p in agents.directory.glob("*.yaml")} == {"New.yaml"}


def test_last_valid_model_is_kept_by_source_even_for_duplicate_ids(tmp_path):
    agents, models = documents(tmp_path)
    a, b = models.directory / "a.yaml", models.directory / "b.yaml"
    write(a, "id: X\nname: First\nmodel_id: first")
    write(b, "id: X\nname: Winner\nmodel_id: second")
    models.load_all(models.directory)
    write(b, "name: [broken")
    reload = ProfileReload(agents, models)
    reload.scan()
    assert reload.models.get("X").name == "Winner"
    reload.normalize()
    reload.install()
    assert b.read_text(encoding="utf-8") == "name: [broken"
    b.unlink()
    write(a, "max_context_tokens: .inf\nname: First\nid: X")
    next_reload = ProfileReload(agents, models)
    next_reload.scan()
    assert next_reload.models.get("X").name == "First"
    assert next_reload.report[0].definition.key == "profiles.reload_kept"


def test_corrupt_agent_keeps_definition_but_deleted_override_reveals_pristine_builtin(tmp_path):
    agents, models = documents(tmp_path)
    path = agents.directory / "Code.yaml"
    write(path, "name: Code\ninstructions: custom body")
    agents.load_all(agents.directory)
    old = agents.get("Code")
    write(path, "ui: [bad")
    candidate = ProfileReload(agents, models)
    candidate.scan()
    candidate.normalize()
    candidate.install()
    assert agents.get("Code") == old
    assert path.read_text(encoding="utf-8") == "ui: [bad"
    path.unlink()
    candidate = ProfileReload(agents, models)
    candidate.scan()
    candidate.normalize()
    candidate.install()
    assert agents.get("Code").instructions != "custom body"
    assert agents.get("Code") == agents.get_builtin_template("Code")
    agents.get("Code").instructions = "live modification"
    assert agents.get_builtin_template("Code").instructions != "live modification"


def test_incomplete_directory_scan_cannot_remove_live_entries(tmp_path, monkeypatch):
    agents, models = documents(tmp_path)
    write(models.directory / "m.yaml", "name: model\nmodel_id: first")
    models.load_all(models.directory)
    iterdir = Path.iterdir

    def incomplete(path):
        if path == models.directory:
            yield path / "m.yaml"
            raise PermissionError("listing interrupted")
        yield from iterdir(path)

    monkeypatch.setattr(Path, "iterdir", incomplete)
    with pytest.raises(ConfigError, match="Cannot list"):
        ProfileReload(agents, models).scan()
    assert models.list_ids() == ["m"]


def test_stale_file_before_id_write_is_not_overwritten(tmp_path, monkeypatch):
    agents, models = documents(tmp_path)
    path = agents.directory / "Old.yaml"
    target = agents.directory / "New.yaml"
    write(path, "name: New")
    candidate = ProfileReload(agents, models)
    candidate.scan()
    canonicalize = candidate.agents.canonicalize_profile_files

    def edit_after_move(*args, **kwargs):
        ready = canonicalize(*args, **kwargs)
        write(target, "name: New\ninstructions: user edit")
        return ready

    monkeypatch.setattr(candidate.agents, "canonicalize_profile_files", edit_after_move)
    with pytest.raises(ConfigError, match=r"New\.yaml") as error:
        candidate.normalize()
    from chrys.foundation.i18n.formatting import format_message

    assert "Old.yaml → New.yaml" in format_message(error.value.display_hint)
    assert target.read_text(encoding="utf-8") == "name: New\ninstructions: user edit"
    assert agents.get("New") is None


def test_changed_registry_version_refuses_commit(tmp_path):
    agents, models = documents(tmp_path)
    candidate = ProfileReload(agents, models)
    candidate.scan()
    agents.remove("Explore", cascade=False)
    with pytest.raises(ConfigError, match="changed while reloading"):
        candidate.check_versions()


@pytest.mark.parametrize(
    "yaml_text", ["name: Writer\n", "name: Writer\nid: writer\nsub_agents:\n  agents: [{profile: Plan}]\n"]
)
def test_failed_id_or_plan_rewrite_does_not_register_candidate(tmp_path, monkeypatch, yaml_text):
    from chrys.service.profiles import reload as module

    agents, models = documents(tmp_path)
    path = agents.directory / "Writer.yaml"
    write(path, yaml_text)
    candidate = ProfileReload(agents, models)
    candidate.scan()

    def fail(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(module, "save_profile", fail)
    with pytest.raises(ConfigError, match=r"Writer\.yaml.*permission denied"):
        candidate.normalize()
    assert agents.get("Writer") is None and path.read_text(encoding="utf-8") == yaml_text


def test_conflicting_filename_is_quarantined_and_reported_without_registering_it(tmp_path):
    from chrys.foundation.i18n.formatting import format_message

    agents, models = documents(tmp_path)
    path = agents.directory / "Other.yaml"
    write(path, "name: Writer\ninstructions: conflicting source")
    write(agents.directory / "Writer.yaml", "name: Writer\nid: winner")
    candidate = ProfileReload(agents, models)
    candidate.scan()
    with pytest.raises(ConfigError, match=r"Other\.yaml") as failure:
        candidate.normalize()
    assert "Other.yaml.conflict" in format_message(failure.value.display_hint)
    assert (agents.directory / "Other.yaml.conflict").read_text(encoding="utf-8").endswith("conflicting source")
    assert agents.get("Writer") is None
    retry = ProfileReload(agents, models)
    retry.scan()
    retry.normalize()
    retry.install()
    assert agents.get("Writer").id == "winner"


def test_casefolded_agent_filename_keeps_last_registered_definition_on_corruption(tmp_path):
    agents, models = documents(tmp_path)
    path = agents.directory / "EDITOR.yaml"
    write(path, "name: Editor\nid: writer")
    if not (agents.directory / "Editor.yaml").exists():
        pytest.skip("filesystem has case-sensitive names")
    agents.load_all(agents.directory)
    write(path, "name: [invalid")
    candidate = ProfileReload(agents, models)
    candidate.scan()
    candidate.normalize()
    candidate.install()
    assert agents.get("Editor").id == "writer"
    assert agents.sources[path] == "Editor"


def test_atomic_editor_save_while_serializing_a_missing_id_survives(tmp_path, monkeypatch):
    from chrys.service.profiles import reload as module

    agents, models = documents(tmp_path)
    path = agents.directory / "Writer.yaml"
    write(path, "name: Writer")
    candidate = ProfileReload(agents, models)
    candidate.scan()
    serialize = module.save_profile

    def concurrent_save(*args, **kwargs):
        editor_file = path.with_suffix(".tmp")
        write(editor_file, "name: Writer\ninstructions: latest edit")
        editor_file.replace(path)
        return serialize(*args, **kwargs)

    monkeypatch.setattr(module, "save_profile", concurrent_save)
    with pytest.raises(ConfigError, match=r"Writer\.yaml"):
        candidate.normalize()
    assert path.read_text(encoding="utf-8").endswith("latest edit")
    assert agents.get("Writer") is None


@pytest.mark.parametrize("operation", ["rename", "fill_id"])
def test_editor_recreating_source_during_publication_is_never_overwritten_or_unlinked(tmp_path, monkeypatch, operation):
    from chrys.foundation.i18n.formatting import format_message
    from chrys.service.profiles import reload as module

    agents, models = documents(tmp_path)
    source = agents.directory / "Writer.yaml"
    renamed = agents.directory / "Renamed.yaml"
    original = "name: Renamed\nid: writer" if operation == "rename" else "name: Writer"
    write(source, original)
    candidate = ProfileReload(agents, models)
    candidate.scan()
    publish = module.publish_file_no_replace
    edited = False

    def editor_races_publication(staged, target):
        nonlocal edited
        if not edited:
            edited = True
            assert not source.exists()
            write(source, "name: Writer\ninstructions: external save")
        publish(staged, target)

    monkeypatch.setattr(module, "publish_file_no_replace", editor_races_publication)
    with pytest.raises(ConfigError) as failure:
        candidate.normalize()
    assert source.read_text(encoding="utf-8").endswith("external save")
    assert agents.get("Renamed") is None and agents.get("Writer") is None
    if operation == "fill_id":
        (saved,) = agents.directory.glob("*.reload")
        assert saved.read_text(encoding="utf-8") == original
        assert str(saved) in format_message(failure.value.display_hint)
    else:
        assert renamed.read_text(encoding="utf-8") == original


def test_malformed_acp_arguments_are_not_logged_with_credentials(tmp_path, caplog):
    agents, models = documents(tmp_path)
    path = agents.directory / "Remote.yaml"
    write(path, 'name: Remote\nacp:\n  command: cli\n  args: "--api-key private-test-value"')
    candidate = ProfileReload(agents, models)
    candidate.scan()
    assert str(path) in caplog.text and "private-test-value" not in caplog.text
    assert candidate.agents.get("Remote") is None


@pytest.mark.parametrize(("target_name", "temporary_file"), [("New", "mkstemp"), ("Old", "mkdtemp")])
def test_read_only_directory_reports_source_and_leaves_it_intact(tmp_path, monkeypatch, target_name, temporary_file):
    import errno
    import tempfile

    agents, models = documents(tmp_path)
    source = agents.directory / "Old.yaml"
    write(source, f"name: {target_name}")
    candidate = ProfileReload(agents, models)
    candidate.scan()

    def denied(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(source.parent))

    monkeypatch.setattr(tempfile, temporary_file, denied)
    with pytest.raises(ConfigError, match=r"Old\.yaml.*Permission denied"):
        candidate.normalize()
    assert source.read_text(encoding="utf-8") == f"name: {target_name}" and agents.get(target_name) is None


def test_directory_order_and_retained_invalid_files_do_not_report_definition_changes(tmp_path):
    from chrys.service.profiles.models.loader import load_profile_from_yaml

    agents, models = documents(tmp_path)

    for name in ("B", "A"):
        path = models.directory / f"{name}.yaml"
        write(path, f"name: {name}\nmodel_id: current")
        models.register(load_profile_from_yaml(path))
    for name in ("Alpha", "Beta"):
        write(agents.directory / f"{name}.yaml", f"name: {name}\nid: {name}")
    agents.load_user_profiles(agents.directory)
    for broken in (False, True):
        if broken:
            write(agents.directory / "Alpha.yaml", "name: [")
        candidate = ProfileReload(agents, models)
        candidate.scan()
        keys = [line.definition.key for line in candidate.report]
        assert "profiles.reload_changed" not in keys
        assert ("profiles.reload_kept" in keys) == broken
        candidate.normalize()
        candidate.install()


@pytest.mark.parametrize(
    ("directory", "raw", "locator"),
    [
        ("models", "name: Example\nmodel_id: [private-marker]", "model_id"),
        ("models", "name: Example\nhttp_max_retries: private-marker", "http_max_retries"),
        ("agents", "name: Example\nacp: {args: private-marker}", "acp.args"),
        ("agents", "name: Example\ntools: [private-marker]", "tools"),
        ("agents", "name: Example\nmodel: {profile_id: 5}", "model.profile_id"),
        ("models", "name: [private-marker\n", "2:1"),
        ("models", "# comment\n- private-marker", "2:1"),
        ("agents", "name: Example\nid: 2026-99-40 # private-marker", "2:5"),
    ],
)
@pytest.mark.parametrize("known", [True, False])
def test_reload_diagnostics_locate_invalid_files_without_printing_values(
    tmp_path, caplog, directory, raw, locator, known
):
    from chrys.foundation.i18n.formatting import format_message

    agents, models = documents(tmp_path)
    registry = models if directory == "models" else agents
    path = registry.directory / "Example.yaml"
    if known:
        write(path, "name: Example\nid: example")
        registry.load_all(registry.directory)
    write(path, raw)
    candidate = ProfileReload(agents, models)
    candidate.scan()
    key = "profiles.reload_kept" if known else "profiles.reload_invalid"
    (line,) = [format_message(line) for line in candidate.report if line.definition.key == key]
    assert str(path) in line and locator in line
    assert str(path) in caplog.text
    if ":" in locator:
        row, column = locator.split(":")
        assert f"line {row}, column {column}" in caplog.text
    else:
        assert locator in caplog.text
    assert "private-marker" not in line and "private-marker" not in caplog.text


def test_idless_agent_retained_at_startup_follows_its_source_through_corruption_and_deletion(tmp_path, monkeypatch):
    from chrys.service.profiles.agents import serializer

    agents, models = documents(tmp_path)
    path = agents.directory / "Writer.yaml"
    write(path, "name: Writer\ninstructions: original")

    def cannot_migrate(*args, **kwargs):
        raise PermissionError("read-only agent directory")

    with monkeypatch.context() as patch:
        patch.setattr(serializer, "save_profile", cannot_migrate)
        agents.load_user_profiles(agents.directory)
    legacy = agents.get("Writer")
    assert legacy.id == ""
    write(path, "name: [invalid")
    retained = ProfileReload(agents, models)
    retained.scan()
    assert retained.updated_agent(legacy) is legacy
    retained.normalize()
    retained.install()
    path.unlink()
    write(agents.directory / "Different.yaml", "name: Writer\nid: unrelated")
    removed = ProfileReload(agents, models)
    removed.scan()
    removed.normalize()
    assert removed.updated_agent(legacy) is None
    assert removed.updated_launch_agent("Writer", "Code") == "Code"
