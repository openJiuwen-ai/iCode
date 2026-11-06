# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Turning rejected settings into warnings a user can act on."""

from __future__ import annotations

from pathlib import Path

from chrys.foundation.config.coercion import CoerceReason, invalid
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import (
    DormantProjectConfig,
    DormantProjectSource,
    LoadedSettings,
    SettingsWarning,
    load_settings,
)
from chrys.foundation.config.spec import SettingOrigin, Source
from chrys.foundation.config.user_settings import user_settings_path
from chrys.foundation.config.warnings import _REASON_MESSAGES, settings_warning_events


def test_a_rejected_env_value_is_reported_under_its_variable_name() -> None:
    """The dotted key is the panel's identifier; the user set a variable."""
    loaded = load_settings(env={"CHRYS_SESSION_TITLE_AUTO": "nonsense"})

    (event,) = settings_warning_events(loaded)

    assert event.code == "setting_rejected"
    assert "CHRYS_SESSION_TITLE_AUTO" in event.message
    assert "nonsense" in event.message
    assert "session.title.auto" not in event.message


def test_a_clamped_value_reports_both_what_was_written_and_what_is_in_force() -> None:
    loaded = load_settings(env={"CHRYS_MAX_TRANSIENT_RETRIES": "999"})

    (event,) = settings_warning_events(loaded)

    assert event.code == "setting_clamped"
    assert "999" in event.message
    assert "50" in event.message


def test_a_file_layer_warning_names_the_file() -> None:
    """A repository with several roots has had more than one project file."""
    project_file = Path("/repo/.chrys/settings.yaml")
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        warnings=(
            SettingsWarning(
                key="ui.theme",
                origin=SettingOrigin(layer=Source.PROJECT, path=project_file),
                outcome=invalid("mauve", CoerceReason.NOT_A_CHOICE, choices=("chrys", "chrys-legacy")),
            ),
        ),
    )

    (event,) = settings_warning_events(loaded)

    assert "ui.theme" in event.message
    assert str(project_file) in event.message
    assert "chrys, chrys-legacy" in event.message


def test_any_yaml_shaped_raw_value_composes_without_raising() -> None:
    """A document can put a bool, float, list or mapping where a scalar
    belongs; every verdict constructor renders the raw to a string, so the
    message layer (which accepts exactly ``str | int``) can always bind it."""
    origin = SettingOrigin(layer=Source.PROJECT, path=Path("/repo/.chrys/settings.yaml"))
    for raw in (False, 1.5, [1, 2], {"a": 1}, None, 10**5000):
        loaded = LoadedSettings(
            settings=Settings(),
            provenance={},
            warnings=(
                SettingsWarning(
                    key="tools.result.ceiling_tokens",
                    origin=origin,
                    outcome=invalid(raw, CoerceReason.EXPECTED_NON_NEGATIVE_INT),
                ),
            ),
        )

        (event,) = settings_warning_events(loaded)

        assert event.code == "setting_rejected"
        assert "tools.result.ceiling_tokens" in event.message


def test_every_reason_a_coercer_can_report_has_a_sentence() -> None:
    """A reason with no message would be a load that warns about nothing."""
    missing = [reason.name for reason in CoerceReason if reason not in _REASON_MESSAGES]

    assert missing == []


def test_a_warning_the_caller_reports_itself_is_skipped() -> None:
    loaded = load_settings(
        env={"CHRYS_MAX_TRANSIENT_RETRIES": "999", "CHRYS_SESSION_TITLE_AUTO": "nonsense"},
    )

    events = settings_warning_events(loaded, skip=lambda warning: warning.key == "llm.retry.max_transient")

    assert [event.code for event in events] == ["setting_rejected"]


def test_a_clean_load_produces_nothing() -> None:
    assert settings_warning_events(load_settings(env={})) == []


def test_unknown_user_keys_produce_one_batched_warning() -> None:
    """A whole section of misindented YAML is one mistake, not twelve."""
    loaded = LoadedSettings(settings=Settings(), provenance={}, unknown_keys=("stray", "ui.theem"))

    (event,) = settings_warning_events(loaded)

    assert event.code == "setting_unknown_keys"
    assert "stray" in event.message
    assert "ui.theem" in event.message
    assert str(user_settings_path()) in event.message


def test_a_denied_project_key_report_does_not_echo_the_value() -> None:
    """The key is the problem; repeating what the repository tried to set
    would only lend it column inches."""
    project_file = Path("/repo/.chrys/settings.yaml")
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        warnings=(
            SettingsWarning(
                key="agent.default_profile",
                origin=SettingOrigin(layer=Source.PROJECT, path=project_file),
                outcome=invalid("evil-profile", CoerceReason.NOT_ALLOWED_IN_PROJECT),
            ),
        ),
    )

    (event,) = settings_warning_events(loaded)

    assert "agent.default_profile" in event.message
    assert str(project_file) in event.message
    assert "evil-profile" not in event.message


def test_a_loosening_project_value_reports_what_was_written() -> None:
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        warnings=(
            SettingsWarning(
                key="llm.retry.max_transient",
                origin=SettingOrigin(layer=Source.PROJECT, path=Path("/repo/.chrys/settings.yaml")),
                outcome=invalid("30", CoerceReason.LOOSENS_USER_BASELINE),
            ),
        ),
    )

    (event,) = settings_warning_events(loaded)

    assert event.code == "setting_rejected"
    assert "30" in event.message
    assert "tighten" in event.message


def test_dormant_project_config_reports_once_per_file_with_its_keys() -> None:
    """One decision — enable the gate or not — so one warning, keys riding along."""
    project_file = Path("/repo/.chrys/settings.yaml")
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        dormant_project=(
            DormantProjectConfig(
                path=project_file,
                keys=("session.title.auto", "llm.retry.max_transient"),
            ),
        ),
    )

    (event,) = settings_warning_events(loaded)

    assert event.code == "project_config_dormant"
    assert str(project_file) in event.message
    assert "session.title.auto" in event.message
    assert "llm.retry.max_transient" in event.message
    assert "project.config_enabled" in event.message


def test_dormant_project_hooks_and_skills_each_name_the_setting_to_turn_on() -> None:
    hooks_file = Path("/repo/.chrys/hooks/hooks.yaml")
    skills_dir = Path("/repo/.agents/skills")
    loaded = LoadedSettings(
        settings=Settings(),
        provenance={},
        dormant_project_sources=(
            DormantProjectSource(key="project.hooks_enabled", path=hooks_file),
            DormantProjectSource(key="project.skills_enabled", path=skills_dir),
        ),
    )

    hooks, skills = settings_warning_events(loaded)

    assert hooks.code == "project_hooks_dormant"
    assert str(hooks_file) in hooks.message
    assert "Load project hooks" in hooks.message
    assert "project.hooks_enabled" in hooks.message
    assert skills.code == "project_skills_dormant"
    assert str(skills_dir) in skills.message
    assert "Load project skills" in skills.message
    assert "project.skills_enabled" in skills.message
