# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Preparing settings overlays cannot partially replace the live handle."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from chrys.foundation.config import settings_store
from chrys.foundation.config.coercion import CoerceReason, invalid
from chrys.foundation.config.runtime_pointer import (
    MODEL_POINTER_ENV,
    MODEL_POINTER_KEY,
    SessionDefaultModel,
    StaleModelFileChange,
    get_model_pointer,
    model_selection_revision,
    remember_file_model,
    set_model_pointer,
)
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import LoadedSettings, SettingsHandle, SettingsWarning, load_settings
from chrys.foundation.config.spec import SettingOrigin, Source


@pytest.mark.parametrize("operation", ["prepare", "install"])
def test_failed_overlay_preserves_both_installed_settings_references(operation, monkeypatch) -> None:
    base = LoadedSettings(settings=Settings(), provenance={})
    handle = SettingsHandle(base)
    handle.override(theme="chrys-legacy")
    effective = handle.loaded
    candidate = LoadedSettings(settings=Settings(locale="zh-Hans"), provenance={})

    def fail_overlay(*args, **kwargs):
        raise ValueError("overlay rejected")

    monkeypatch.setattr(LoadedSettings, "overlay", fail_overlay)
    with pytest.raises(ValueError, match="overlay rejected"):
        if operation == "prepare":
            handle.prepare(candidate)
        else:
            handle.install(candidate)
    assert handle._base is base
    assert handle.loaded is effective


def test_install_composition_matches_prepared_install_without_recomputing_overlay(monkeypatch) -> None:
    base = LoadedSettings(settings=Settings(), provenance={})
    candidate = LoadedSettings(settings=Settings(locale="zh-Hans"), provenance={})
    combined, split = SettingsHandle(base), SettingsHandle(base)
    for handle in [combined, split]:
        handle.override(theme="chrys-legacy")
    old = split.loaded
    prepared = split.prepare(candidate)
    assert split._base is base
    assert split.loaded is old
    assert prepared.base is candidate
    combined.install(candidate)

    def fail_overlay(*args, **kwargs):
        raise AssertionError("installation recomputed the overlay")

    monkeypatch.setattr(LoadedSettings, "overlay", fail_overlay)
    split.install_prepared(prepared)
    assert split._base is combined._base is candidate
    assert split.loaded is prepared.effective
    assert split.loaded == combined.loaded
    assert split.settings.theme == "chrys-legacy"
    assert split.settings.locale == "zh-Hans"


def _load(handle: SettingsHandle, *, theme: str = "file-theme", in_file: bool = True, **extra) -> LoadedSettings:
    """What a manual reload's read of the files hands over, the confirmations taken before it."""
    return LoadedSettings(
        settings=Settings(theme=theme),
        provenance={},
        file_values={"theme": theme} if in_file else {},
        confirmed_overrides=handle.confirmed_overrides(),
        **extra,
    )


def _source(handle: SettingsHandle, key: str = "ui.theme") -> Source:
    return handle.loaded.source_for(key).layer


def _saved_choice(theme: str = "picked") -> SettingsHandle:
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
    handle.override(theme=theme)
    handle.confirm_written({"ui.theme": theme})
    return handle


def test_a_confirmed_choice_ends_when_the_file_moved_past_it_and_the_load_is_installed() -> None:
    handle = _saved_choice()
    prepared = handle.prepare(_load(handle))
    assert prepared.effective.settings.theme == "file-theme"
    assert handle.settings.theme == "picked"

    handle.install_prepared(prepared)

    assert handle.settings.theme == "file-theme"
    assert _source(handle) is not Source.RUNTIME
    assert handle.confirmed_overrides() == {}
    assert handle.base.confirmed_overrides == {}
    assert handle.loaded.confirmed_overrides == {}
    # Gone for good: a later ordinary load does not bring it back.
    handle.install(LoadedSettings(settings=Settings(theme="later"), provenance={}))
    assert handle.settings.theme == "later"


def test_a_key_removed_from_the_file_ends_the_choice_too() -> None:
    handle = _saved_choice()
    handle.install_prepared(handle.prepare(_load(handle, theme="chrys", in_file=False)))
    assert handle.settings.theme == "chrys"


def test_a_file_that_still_holds_the_choice_changes_nothing() -> None:
    handle = _saved_choice()
    prepared = handle.prepare(_load(handle, theme="picked"))
    assert prepared.released == {}
    handle.install_prepared(prepared)
    assert handle.settings.theme == "picked"
    assert _source(handle) is Source.RUNTIME
    assert "theme" in handle.confirmed_overrides()


def test_only_the_confirmed_key_of_a_batch_ends() -> None:
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
    handle.override(notifications_sound=False, notifications_desktop=False)
    handle.confirm_written({"notifications.delivery.sound": False, "notifications.delivery.desktop": True})
    assert set(handle.confirmed_overrides()) == {"notifications_sound"}

    candidate = LoadedSettings(
        settings=Settings(notifications_sound=True, notifications_desktop=True),
        provenance={},
        file_values={"notifications_sound": True, "notifications_desktop": True},
        confirmed_overrides=handle.confirmed_overrides(),
    )
    handle.install_prepared(handle.prepare(candidate))

    assert handle.settings.notifications_sound is True
    assert handle.settings.notifications_desktop is False
    assert _source(handle, "notifications.delivery.desktop") is Source.RUNTIME


def test_a_write_the_user_has_since_overtaken_confirms_nothing() -> None:
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
    handle.override(theme="newer")
    handle.confirm_written({"ui.theme": "older"})
    assert handle.confirmed_overrides() == {}


def test_choosing_again_withdraws_the_confirmation() -> None:
    handle = _saved_choice()
    handle.override(theme="newer")
    assert handle.confirmed_overrides() == {}


def test_a_write_with_no_choice_behind_it_or_outside_the_live_tier_confirms_nothing() -> None:
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
    handle.confirm_written({"ui.theme": "nord"})
    handle.override(default_approval_mode="auto")
    handle.confirm_written({"approval.default_mode": "auto", "unknown.key": 1})
    assert handle.confirmed_overrides() == {}


def test_a_choice_made_after_the_load_was_prepared_stays_and_the_others_end() -> None:
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
    handle.override(theme="picked", locale="zh-Hans")
    handle.confirm_written({"ui.theme": "picked", "ui.locale": "zh-Hans"})
    candidate = LoadedSettings(
        settings=Settings(theme="file-theme", locale="en"),
        provenance={},
        file_values={"theme": "file-theme", "locale": "en"},
        confirmed_overrides=handle.confirmed_overrides(),
    )
    prepared = handle.prepare(candidate)
    assert set(prepared.released) == {"theme", "locale"}

    handle.override(theme="while-building")
    handle.install_prepared(prepared)

    assert handle.settings.theme == "while-building"
    assert _source(handle) is Source.RUNTIME
    assert handle.settings.locale == "en"
    assert _source(handle, "ui.locale") is not Source.RUNTIME


def test_a_choice_made_between_the_read_and_preparing_is_not_released() -> None:
    handle = _saved_choice()
    candidate = _load(handle)
    handle.override(theme="newer")
    prepared = handle.prepare(candidate)
    assert prepared.released == {}
    assert prepared.effective.settings.theme == "newer"


def test_an_ordinary_load_releases_nothing() -> None:
    handle = _saved_choice()
    handle.install(LoadedSettings(settings=Settings(theme="file-theme"), provenance={}, file_values={"theme": "x"}))
    assert handle.settings.theme == "picked"
    assert _source(handle) is Source.RUNTIME


def test_a_load_that_is_never_installed_ends_nothing() -> None:
    handle = _saved_choice()
    handle.prepare(_load(handle))
    assert handle.settings.theme == "picked"
    assert "theme" in handle.confirmed_overrides()


def test_a_value_the_users_file_refused_keeps_the_choice_but_another_layers_refusal_does_not() -> None:
    outcome = invalid("nope", CoerceReason.EXPECTED_TEXT)
    file = Path("settings.yaml")
    in_file = SettingsWarning("ui.theme", SettingOrigin(layer=Source.USER, path=file), outcome)
    in_project = SettingsWarning("ui.theme", SettingOrigin(layer=Source.PROJECT, path=file), outcome)
    in_env = SettingsWarning("ui.theme", SettingOrigin(layer=Source.ENV), outcome)

    handle = _saved_choice()
    assert handle.prepare(_load(handle, in_file=False, warnings=(in_file,))).released == {}
    assert set(handle.prepare(_load(handle, warnings=(in_project,))).released) == {"theme"}
    assert set(handle.prepare(_load(handle, in_file=False, warnings=(in_env,))).released) == {"theme"}


def test_a_rebuild_prepared_before_the_release_does_not_bring_the_choice_back() -> None:
    handle = _saved_choice()
    earlier = handle.prepare(LoadedSettings(settings=Settings(), provenance={}))
    handle.install_prepared(handle.prepare(_load(handle)))

    handle.install_prepared(earlier)

    assert handle.settings.theme == Settings().theme
    assert _source(handle) is not Source.RUNTIME


@pytest.fixture(autouse=True)
def _pointer_unwinds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Registered with monkeypatch even when absent, so a pointer a test writes does not outlive it."""
    monkeypatch.setenv(MODEL_POINTER_ENV, "")
    monkeypatch.delenv(MODEL_POINTER_ENV)


def _session_scoped(model: str, origin: SettingOrigin) -> LoadedSettings:
    return LoadedSettings(
        settings=Settings(model_profile=model),
        provenance={MODEL_POINTER_KEY: origin},
        manual_model_revision=model_selection_revision(),
        session_scoped_model=True,
    )


def test_a_session_scoped_load_settles_on_the_session_and_never_touches_or_races_the_pointer() -> None:
    origin = SettingOrigin(layer=Source.USER, path=Path("settings.yaml"))
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}))
    pointer = get_model_pointer()
    prepared = handle.prepare(_session_scoped("from-file", origin))

    remember_file_model("another-sessions-write")
    handle.install_prepared(prepared)

    assert handle.base.session_default_model == SessionDefaultModel("from-file", origin, model_selection_revision())
    assert not handle.base.session_scoped_model
    assert handle.base.manual_model_revision is None
    assert get_model_pointer() == pointer


def test_a_session_scoped_load_decided_by_a_pin_keeps_what_the_session_settled_on() -> None:
    settled = SessionDefaultModel("from-file", SettingOrigin(layer=Source.DEFAULT), model_selection_revision())
    handle = SettingsHandle(LoadedSettings(settings=Settings(), provenance={}, session_default_model=settled))
    pinned = replace(
        _session_scoped("pinned", SettingOrigin(layer=Source.SESSION)),
        session_default_model=settled,
    )

    handle.install_prepared(handle.prepare(pinned))

    assert handle.base.session_default_model is settled


def test_a_load_carries_what_the_session_settled_on_past_the_pointer_and_a_pin_past_both() -> None:
    origin = SettingOrigin(layer=Source.USER, path=Path("settings.yaml"))
    set_model_pointer("revoked", origin=origin)
    settled = SessionDefaultModel("", SettingOrigin(layer=Source.DEFAULT), model_selection_revision())

    loaded = load_settings(session_default_model=settled)
    assert loaded.settings.model_profile == ""
    assert loaded.source_for(MODEL_POINTER_KEY).layer is Source.DEFAULT
    assert loaded.session_default_model is settled

    pinned = load_settings(session_default_model=settled, model_profile="pinned")
    assert pinned.settings.model_profile == "pinned"
    assert pinned.source_for(MODEL_POINTER_KEY).layer is Source.SESSION
    assert pinned.session_default_model is settled

    set_model_pointer("chosen", origin=SettingOrigin(layer=Source.PROCESS_RUNTIME))
    after_a_choice = load_settings(session_default_model=settled)
    assert after_a_choice.settings.model_profile == "chosen"
    assert after_a_choice.session_default_model is None


def test_only_a_process_wide_manual_read_races_the_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    real_read = settings_store._read_file_layers

    def read_during_a_write(*args, **kwargs):
        remember_file_model("written-meanwhile")
        return real_read(*args, **kwargs)

    monkeypatch.setattr(settings_store, "_read_file_layers", read_during_a_write)

    assert load_settings(manual_reload=True, session_scoped_model=True).session_scoped_model
    with pytest.raises(StaleModelFileChange):
        load_settings(manual_reload=True)
