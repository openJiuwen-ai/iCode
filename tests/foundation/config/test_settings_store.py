# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the user file layers and :func:`persist`."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml

import chrys.foundation.platform as platform_mod
from chrys.foundation.config.coercion import CoerceReason, CoerceStatus
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.runtime_pointer import set_model_pointer
from chrys.foundation.config.settings import MAX_TRANSIENT_RETRIES_LIMIT, Settings
from chrys.foundation.config.settings_store import load_settings, persist
from chrys.foundation.config.spec import Apply, Kind, SettingOrigin, Source, specs_by_field
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.text.images import MAX_IMAGE_BYTES


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = dataclasses.replace(
        platform_mod.get_platform(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    return fake.config_dir


def _write_user_yaml(config_dir: Path, text: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_search_ignore_setting_defaults_and_round_trips(config_dir: Path) -> None:
    key = "tools.search.respect_gitignore"
    freeze_process_env()
    assert load_settings(env={}).settings.search_respect_gitignore is True
    result = persist({key: False})
    assert result.written == {key: False}
    loaded = load_settings()
    assert loaded.settings.search_respect_gitignore is False
    assert loaded.source_for(key).layer is Source.USER
    assert yaml.safe_load(user_settings_path().read_text())["tools"]["search"]["respect_gitignore"] is False
    persist({key: True})
    assert load_settings().settings.search_respect_gitignore is True


def test_image_send_limit_defaults_to_the_compression_target_clamps_and_round_trips(config_dir: Path) -> None:
    key = "tools.image.send_limit_kb"
    freeze_process_env()
    assert load_settings(env={}).settings.image_send_limit_bytes() == MAX_IMAGE_BYTES
    assert load_settings(env={"CHRYS_IMAGE_SEND_LIMIT_KB": "8"}).settings.image_send_limit_kb == 32
    assert load_settings(env={"CHRYS_IMAGE_SEND_LIMIT_KB": "99999"}).settings.image_send_limit_kb == 3072
    persist({key: 128})
    loaded = load_settings()
    assert loaded.settings.image_send_limit_bytes() == 128 * 1024
    assert loaded.source_for(key).layer is Source.USER
    assert yaml.safe_load(user_settings_path().read_text())["tools"]["image"]["send_limit_kb"] == 128


def test_the_approval_dialog_waits_for_auto_review_by_default_and_round_trips(config_dir: Path) -> None:
    key = "ui.approval.defer_while_judging"
    entry = specs_by_field(Settings)["approval_defer_while_judging"]
    assert (entry.key, entry.apply, entry.kind) == (key, Apply.LIVE, Kind.BOOL)
    freeze_process_env()
    assert load_settings(env={}).settings.approval_defer_while_judging is True

    assert persist({key: False}).written == {key: False}

    loaded = load_settings()
    assert loaded.settings.approval_defer_while_judging is False
    assert loaded.source_for(key).layer is Source.USER
    assert yaml.safe_load(user_settings_path().read_text())["ui"]["approval"]["defer_while_judging"] is False


def _write_user_env(config_dir: Path, text: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_auto_review_defaults_and_persists(config_dir: Path) -> None:
    key = "approval.auto_review"
    freeze_process_env()
    assert load_settings(env={}).settings.approval_auto_review == "direct"
    assert persist({key: "formal"}).written == {key: "formal"}
    assert load_settings().settings.approval_auto_review == "formal"
    assert load_settings(env={"CHRYS_APPROVAL_AUTO_REVIEW": "invalid"}).settings.approval_auto_review == "direct"
    entry = specs_by_field(Settings)["approval_auto_review"]
    assert (entry.key, entry.apply, entry.kind, entry.choices) == (key, Apply.RELOAD, Kind.ENUM, ("direct", "formal"))


def _clear_theme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CHRYS_THEME", raising=False)


# ── the user file layers ─────────────────────────────────────────────


def test_a_user_yaml_value_loads_with_the_file_as_its_origin(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_theme(monkeypatch)
    freeze_process_env()
    path = _write_user_yaml(config_dir, "ui:\n  theme: solar\n")

    loaded = load_settings()

    assert loaded.settings.theme == "solar"
    origin = loaded.source_for("ui.theme")
    assert origin.layer is Source.USER
    assert origin.path == path


def test_the_real_environment_outranks_user_settings(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHRYS_THEME", "from-shell")
    freeze_process_env()
    _write_user_yaml(config_dir, "ui:\n  theme: from-yaml\n")

    loaded = load_settings()

    assert loaded.settings.theme == "from-shell"
    assert loaded.source_for("ui.theme").layer is Source.ENV


def test_files_stay_unread_until_bootstrap_freezes_the_environment(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_theme(monkeypatch)
    _write_user_yaml(config_dir, "ui:\n  theme: poison\nstray: 1\n")

    loaded = load_settings()

    assert loaded.settings.theme != "poison"
    assert loaded.source_for("ui.theme").layer is Source.DEFAULT
    assert loaded.unknown_keys == ()


def test_an_injected_environment_turns_the_file_layers_off(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_process_env()
    _write_user_yaml(config_dir, "ui:\n  theme: poison\nstray: 1\n")

    loaded = load_settings(env={})

    assert loaded.source_for("ui.theme").layer is Source.DEFAULT
    assert loaded.unknown_keys == ()


def test_an_invalid_user_yaml_value_warns_and_names_the_file(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_theme(monkeypatch)
    freeze_process_env()
    path = _write_user_yaml(config_dir, "ui:\n  theme: 5\n")

    loaded = load_settings()

    assert loaded.source_for("ui.theme").layer is Source.DEFAULT
    warning = next(w for w in loaded.warnings if w.key == "ui.theme")
    assert warning.origin.layer is Source.USER
    assert warning.origin.path == path
    assert warning.outcome.status is CoerceStatus.INVALID
    assert warning.outcome.reason is CoerceReason.EXPECTED_TEXT


def test_unknown_keys_are_reported_and_survive_a_persist(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_theme(monkeypatch)
    freeze_process_env()
    path = _write_user_yaml(config_dir, "stray: 1\nui:\n  theem: typo\n")

    loaded = load_settings()
    assert loaded.unknown_keys == ("stray", "ui.theem")

    result = persist({"ui.theme": "solar"})
    assert result.ok

    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["stray"] == 1
    assert doc["ui"]["theem"] == "typo"
    assert doc["ui"]["theme"] == "solar"
    assert load_settings().unknown_keys == ("stray", "ui.theem")


def test_a_runtime_only_key_in_the_user_yaml_does_not_load(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    freeze_process_env()
    _write_user_yaml(config_dir, "model:\n  profile:\n    override: sneaky\n")

    loaded = load_settings()

    assert loaded.settings.model_profile_override == ""
    assert loaded.source_for("model.profile.override").layer is Source.DEFAULT
    # Known key, so it is not "unknown" either — just not loadable from files.
    assert loaded.unknown_keys == ()


def test_an_unparseable_user_yaml_loads_as_empty(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_theme(monkeypatch)
    freeze_process_env()
    _write_user_yaml(config_dir, "ui: [unclosed\n")

    loaded = load_settings()

    assert loaded.source_for("ui.theme").layer is Source.DEFAULT
    assert loaded.unknown_keys == ()


# ── persist ──────────────────────────────────────────────────────────


def test_persist_round_trips_through_the_loader(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_theme(monkeypatch)
    monkeypatch.delenv("CHRYS_SESSION_TITLE_AUTO", raising=False)
    freeze_process_env()

    result = persist({"ui.theme": "solar", "session.title.auto": False})

    assert result.ok
    assert result.written == {"ui.theme": "solar", "session.title.auto": False}
    loaded = load_settings()
    assert loaded.settings.theme == "solar"
    assert loaded.settings.session_title_auto is False
    assert loaded.source_for("session.title.auto").layer is Source.USER


def test_persist_normalizes_before_writing(config_dir: Path) -> None:
    result = persist({"approval.default_mode": "  AUTO  "})
    assert result.ok
    assert result.written == {"approval.default_mode": "auto"}


def test_persist_rejects_the_whole_batch_on_one_bad_value(config_dir: Path) -> None:
    result = persist({"ui.theme": "fine", "session.title.auto": "nonsense"})

    assert not result.ok
    assert result.written == {}
    assert set(result.rejected) == {"session.title.auto"}
    assert result.rejected["session.title.auto"].reason is CoerceReason.EXPECTED_BOOL
    assert not user_settings_path().exists()


def test_persist_stores_the_clamped_canonical(config_dir: Path) -> None:
    result = persist({"llm.retry.max_transient": 999})
    assert result.ok
    assert result.written == {"llm.retry.max_transient": MAX_TRANSIENT_RETRIES_LIMIT}


def test_persist_takes_the_unset_spellings_verbatim_except_for_enums(config_dir: Path) -> None:
    blank_text = persist({"model.profile.active": ""})
    assert blank_text.ok
    assert blank_text.written == {"model.profile.active": ""}

    blank_enum = persist({"approval.default_mode": ""})
    assert not blank_enum.ok
    assert blank_enum.rejected["approval.default_mode"].reason is CoerceReason.NOT_A_CHOICE


def test_persist_rejects_a_value_of_the_wrong_type_with_the_kind_it_expected(config_dir: Path) -> None:
    result = persist({"session.title.auto": None})
    assert not result.ok
    assert result.rejected["session.title.auto"].reason is CoerceReason.EXPECTED_BOOL


def test_persist_downgrades_bypass_to_auto(config_dir: Path) -> None:
    result = persist({"approval.default_mode": "bypass"})
    assert result.ok
    assert result.written == {"approval.default_mode": "auto"}


def test_persist_refuses_unknown_and_runtime_only_keys(config_dir: Path) -> None:
    with pytest.raises(TypeError, match="Unknown settings keys"):
        persist({"no.such.key": 1})
    with pytest.raises(TypeError, match="Runtime-only"):
        persist({"model.profile.override": "x"})
    with pytest.raises(TypeError, match="Runtime-only"):
        persist({}, remove=("llm.retry.frontend_default",))
    assert not user_settings_path().exists()


def test_persist_remove_deletes_the_key_and_prunes_its_parents(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_theme(monkeypatch)
    freeze_process_env()
    persist({"ui.theme": "solar", "session.title.auto": False})

    result = persist({}, remove=("ui.theme",))
    assert result.ok

    doc = yaml.safe_load(user_settings_path().read_text(encoding="utf-8"))
    assert "ui" not in doc
    assert doc["session"] == {"title": {"auto": False}}
    assert load_settings().source_for("ui.theme").layer is Source.DEFAULT


def test_persist_of_nothing_writes_nothing(config_dir: Path) -> None:
    result = persist({})
    assert result.ok
    assert not user_settings_path().exists()


def test_a_cleared_optional_int_round_trips_through_the_document(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``None`` is the field's real value — "no timeout" — not a blank, and the
    # document is the only place it can be said now that the legacy env line is
    # migrated away. A load that read it back as silence would make the setting
    # unstorable: written, then ignored, then reported as the default.
    monkeypatch.delenv("CHRYS_ASK_USER_TIMEOUT_SECONDS", raising=False)
    freeze_process_env()

    result = persist({"tools.ask_user.timeout_seconds": None})

    assert result.ok
    doc = yaml.safe_load(user_settings_path().read_text(encoding="utf-8"))
    assert doc["tools"]["ask_user"]["timeout_seconds"] is None
    loaded = load_settings()
    assert loaded.settings.ask_user_timeout_seconds is None
    assert loaded.source_for("tools.ask_user.timeout_seconds").layer is Source.USER


def test_the_environment_layer_is_the_one_bootstrap_froze(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_theme(monkeypatch)
    _write_user_yaml(config_dir, "ui:\n  theme: from-yaml\n")
    freeze_process_env()
    monkeypatch.setenv("CHRYS_THEME", "written-after-bootstrap")

    loaded = load_settings()

    assert loaded.settings.theme == "from-yaml"
    assert loaded.source_for("ui.theme").layer is Source.USER


def test_the_model_pointer_is_still_read_live_after_bootstrap(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The one environment name that is not a bootstrap fact: activating a
    # profile writes it mid-session and the reload right behind it has to see
    # the new value, with the writer's own origin rather than ``ENV``.
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    freeze_process_env()
    set_model_pointer("activated-mid-session", origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))

    loaded = load_settings()

    assert loaded.settings.model_profile == "activated-mid-session"
    assert loaded.source_for("model.profile.active").layer is Source.PROCESS_RUNTIME


def test_approval_timeout_defaults_and_round_trips(config_dir: Path) -> None:
    key = "approval.timeout_seconds"
    entry = specs_by_field(Settings)["approval_timeout_seconds"]
    assert (entry.key, entry.apply, entry.kind) == (key, Apply.RESTART, Kind.INT)
    freeze_process_env()
    assert load_settings(env={}).settings.approval_timeout_seconds == 0
    assert persist({key: 45}).written == {key: 45}
    loaded = load_settings()
    assert loaded.settings.approval_timeout_seconds == 45
    assert loaded.source_for(key).layer is Source.USER
    assert load_settings(env={"CHRYS_APPROVAL_TIMEOUT_SECONDS": "90"}).settings.approval_timeout_seconds == 90


@pytest.mark.parametrize("raw, expected", [("-1", 0), ("bad", 0), ("1.5", 0)])
def test_approval_timeout_validates_seconds(raw: str, expected: int) -> None:
    loaded = load_settings(env={"CHRYS_APPROVAL_TIMEOUT_SECONDS": raw})
    assert loaded.settings.approval_timeout_seconds == expected
    assert loaded.warnings


@pytest.mark.parametrize("seconds", [0, 600])
def test_approval_timeout_preserves_explicit_zero_and_positive_values(config_dir: Path, seconds: int) -> None:
    freeze_process_env()
    assert persist({"approval.timeout_seconds": seconds}).written == {"approval.timeout_seconds": seconds}
    assert load_settings().settings.approval_timeout_seconds == seconds
    loaded = load_settings(env={"CHRYS_APPROVAL_TIMEOUT_SECONDS": str(seconds)})
    assert loaded.settings.approval_timeout_seconds == seconds
    assert not loaded.warnings


@pytest.mark.parametrize("yaml_theme", [None, "chrys-legacy"])
def test_settings_reload_ignores_legacy_files(config_dir, tmp_path, monkeypatch, yaml_theme) -> None:
    _clear_theme(monkeypatch)
    freeze_process_env()
    project = tmp_path / "workspace"
    project.mkdir()
    if yaml_theme is not None:
        _write_user_yaml(config_dir, f"ui:\n  theme: {yaml_theme}\n")
    user_env = _write_user_env(config_dir, "CHRYS_THEME=solar\n")
    project_env = project / ".env"
    project_env.write_text("CHRYS_THEME=solar\n", encoding="utf-8")

    for changed in ("solar", "midnight"):
        for path in (user_env, project_env):
            path.write_text(f"CHRYS_THEME={changed}\n", encoding="utf-8")
        loaded = load_settings(project_root=project)
        assert loaded.settings.theme == (yaml_theme or Settings().theme)
        assert loaded.source_for("ui.theme").layer is (Source.USER if yaml_theme else Source.DEFAULT)
        assert loaded.warnings == ()
        for path in (user_env, project_env):
            assert path.read_text(encoding="utf-8") == f"CHRYS_THEME={changed}\n"


@pytest.mark.parametrize(
    ("launch", "select", "file_default", "effective"),
    [
        ("cli", True, "Y", "Y"),
        ("env", True, "Y", "Y"),
        ("", False, "G", "P"),
    ],
)
def test_manual_reload_only_adopts_external_model_edits(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, launch: str, select: bool, file_default: str, effective: str
) -> None:
    from chrys.foundation.config.runtime_pointer import MODEL_POINTER_KEY, get_model_pointer
    from chrys.foundation.config.settings_store import SettingsHandle
    from chrys.orchestration.startup import bootstrap_runtime
    from chrys.service.profiles.models.env_bridge import activate_model_profile, set_global_default_profile_id

    path = _write_user_yaml(config_dir, "model.profile.active: P\n")
    monkeypatch.setenv("CHRYS_MODEL_PROFILE", "")
    if launch == "env":
        monkeypatch.setenv("CHRYS_MODEL_PROFILE", "E")
    handle = SettingsHandle(bootstrap_runtime(apply_patches=False, setup_telemetry=False).loaded)
    if launch == "cli":
        set_model_pointer("X", origin=SettingOrigin(layer=Source.CLI))
    if select:
        activate_model_profile("Y")
    else:
        set_global_default_profile_id("G")
    assert load_settings(manual_reload=True).file_model == file_default
    assert load_settings(manual_reload=True).settings.model_profile == effective
    path.write_text("model.profile.active: B\n", encoding="utf-8")
    candidate = load_settings(manual_reload=True)
    assert candidate.model_edit is not None
    expected = {"cli": "X", "env": "E", "": "B"}[launch]
    assert candidate.settings.model_profile == expected
    handle.install(candidate)
    assert get_model_pointer()[0] == expected
    assert load_settings().settings.model_profile == expected
    assert load_settings(manual_reload=True).model_edit is None
    if not launch:
        path.unlink()
        handle.install(load_settings(manual_reload=True))
        assert not get_model_pointer()[0]
        assert not load_settings().settings.model_profile
        assert handle.loaded.source_for(MODEL_POINTER_KEY).layer is Source.DEFAULT


def test_bad_edit_keeps_last_document_across_readers_and_refuses_writes(config_dir: Path) -> None:
    from chrys.foundation.config.settings import persist_theme
    from chrys.foundation.config.yaml_store import ConfigDocumentError, backup_path_for
    from chrys.service.profiles.models.env_bridge import get_global_default_profile_id

    freeze_process_env()
    path = _write_user_yaml(config_dir, "tools.ask_user.timeout_seconds: 65\nmodel.profile.active: A\n")
    backup_path_for(path).write_text("tools.ask_user.timeout_seconds: 50\n", encoding="utf-8")
    assert load_settings().settings.ask_user_timeout_seconds == 65
    poison = b"tools: [broken: 70"
    path.write_bytes(poison)
    with pytest.raises(ConfigDocumentError, match="line 1"):
        load_settings(manual_reload=True)
    assert get_global_default_profile_id() == "A"
    loaded = load_settings(project_root=config_dir.parent)
    assert loaded.document_error
    assert loaded.settings.ask_user_timeout_seconds == 65
    assert isinstance(persist_theme("chrys"), ConfigDocumentError)
    assert path.read_bytes() == poison
    assert backup_path_for(path).read_text(encoding="utf-8") == "tools.ask_user.timeout_seconds: 50\n"
