# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sandbox settings values: defaults, layering, invalid policy, and reload."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from textwrap import dedent

import pytest

import chrys.foundation.platform as platform_mod
from chrys.foundation.config.coercion import CoerceReason
from chrys.foundation.config.process_env import freeze_process_env
from chrys.foundation.config.settings import Settings
from chrys.foundation.config.settings_store import load_settings
from chrys.foundation.config.spec import Apply, InvalidPolicy, Source, specs_by_field


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = dataclasses.replace(
        platform_mod.get_platform(),
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
    )
    monkeypatch.setattr(platform_mod, "get_platform", lambda: fake)
    return fake.config_dir


@pytest.fixture(autouse=True)
def _clean_chrys_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The developer's own ``CHRYS_*`` exports must not reach these loads."""
    for name in list(os.environ):
        if name.startswith("CHRYS_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _write_user_yaml(config_dir: Path, text: str) -> Path:
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _write_project_yaml(root: Path, text: str) -> Path:
    (root / ".chrys").mkdir(parents=True, exist_ok=True)
    path = root / ".chrys" / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _enable_gate(config_dir: Path) -> Path:
    return _write_user_yaml(config_dir, "project:\n  config_enabled: true\n")


def test_a_fresh_load_leaves_the_sandbox_off_with_blank_url_and_token(config_dir: Path) -> None:
    """No user file and no environment: the loader returns the built-in defaults."""
    freeze_process_env()
    assert not (config_dir / "settings.yaml").is_file()

    loaded = load_settings()

    assert loaded.settings.sandbox_enabled is False
    assert loaded.settings.sandbox_enabled_was_rejected() is False
    assert loaded.settings.sandbox_url == ""
    assert loaded.settings.sandbox_token == ""
    assert loaded.source_for("sandbox.enabled").layer is Source.DEFAULT
    assert loaded.source_for("sandbox.url").layer is Source.DEFAULT
    assert loaded.source_for("sandbox.token").layer is Source.DEFAULT
    assert loaded.warnings == ()
    assert loaded.sealed_keys == frozenset()


def test_user_yaml_sets_sandbox_enabled_url_and_token(config_dir: Path) -> None:
    freeze_process_env()
    path = _write_user_yaml(
        config_dir,
        dedent(
            """\
            sandbox:
              enabled: true
              url: "http://127.0.0.1:8321"
              token: "user-token"
            """
        ),
    )

    loaded = load_settings()

    assert loaded.settings.sandbox_enabled is True
    assert loaded.settings.sandbox_enabled_was_rejected() is False
    assert loaded.settings.sandbox_url == "http://127.0.0.1:8321"
    assert loaded.settings.sandbox_token == "user-token"
    for key in ("sandbox.enabled", "sandbox.url", "sandbox.token"):
        origin = loaded.source_for(key)
        assert origin.layer is Source.USER
        assert origin.path == path
    assert loaded.warnings == ()


def test_process_env_overrides_user_yaml_for_sandbox_settings(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Layering is the built-in default, then the user document, then the process environment."""
    monkeypatch.setenv("CHRYS_SANDBOX_ENABLED", "true")
    monkeypatch.setenv("CHRYS_SANDBOX_URL", "http://from-env.example")
    monkeypatch.setenv("CHRYS_SANDBOX_TOKEN", "from-env")
    freeze_process_env()
    _write_user_yaml(
        config_dir,
        dedent(
            """\
            sandbox:
              enabled: false
              url: "http://from-user.example"
              token: "from-user"
            """
        ),
    )

    loaded = load_settings()

    assert loaded.settings.sandbox_enabled is True
    assert loaded.settings.sandbox_enabled_was_rejected() is False
    assert loaded.settings.sandbox_url == "http://from-env.example"
    assert loaded.settings.sandbox_token == "from-env"
    for key in ("sandbox.enabled", "sandbox.url", "sandbox.token"):
        assert loaded.source_for(key).layer is Source.ENV
    assert loaded.warnings == ()


def test_an_invalid_user_yaml_sandbox_enabled_falls_back_to_false(config_dir: Path) -> None:
    """``sandbox.enabled`` declares ``InvalidPolicy.SAFE_DEFAULT``."""
    assert specs_by_field(Settings)["sandbox_enabled"].invalid_policy is InvalidPolicy.SAFE_DEFAULT
    freeze_process_env()
    path = _write_user_yaml(config_dir, "sandbox:\n  enabled: banana\n")

    loaded = load_settings()

    assert loaded.settings.sandbox_enabled is False
    assert loaded.settings.sandbox_enabled_was_rejected() is True
    assert "sandbox.enabled" in loaded.sealed_keys
    assert loaded.source_for("sandbox.enabled").layer is Source.DEFAULT
    (warning,) = loaded.warnings
    assert warning.key == "sandbox.enabled"
    assert warning.origin.layer is Source.USER
    assert warning.origin.path == path
    assert warning.outcome.reason is CoerceReason.EXPECTED_BOOL


def test_an_invalid_env_sandbox_enabled_does_not_fall_through_to_user_yaml(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejected environment value stops the walk; the saved ``true`` does not come back."""
    monkeypatch.setenv("CHRYS_SANDBOX_ENABLED", "banana")
    freeze_process_env()
    _write_user_yaml(config_dir, "sandbox:\n  enabled: true\n")

    loaded = load_settings()

    assert loaded.settings.sandbox_enabled is False
    assert loaded.settings.sandbox_enabled_was_rejected() is True
    assert "sandbox.enabled" in loaded.sealed_keys
    assert loaded.source_for("sandbox.enabled").layer is Source.DEFAULT
    (warning,) = loaded.warnings
    assert warning.key == "sandbox.enabled"
    assert warning.origin.layer is Source.ENV
    assert warning.outcome.reason is CoerceReason.EXPECTED_BOOL


def test_an_invalid_sandbox_url_seals_and_an_invalid_token_falls_through(config_dir: Path) -> None:
    """``sandbox.url`` is ``SAFE_DEFAULT``; ``sandbox.token`` keeps the default policy.

    ``text_coercer`` rejects a non-string and accepts any non-empty string, so
    the invalid value here is a number — the shape the coercer names — not a
    URL the launcher would refuse.
    """
    specs = specs_by_field(Settings)
    assert specs["sandbox_url"].invalid_policy is InvalidPolicy.SAFE_DEFAULT
    assert specs["sandbox_token"].invalid_policy is InvalidPolicy.FALL_THROUGH
    freeze_process_env()
    path = _write_user_yaml(config_dir, "sandbox:\n  url: 7\n  token: 7\n")

    loaded = load_settings()

    assert loaded.settings.sandbox_url == ""
    assert loaded.settings.sandbox_token == ""
    assert "sandbox.url" in loaded.sealed_keys
    assert "sandbox.token" not in loaded.sealed_keys
    assert loaded.source_for("sandbox.url").layer is Source.DEFAULT
    assert loaded.source_for("sandbox.token").layer is Source.DEFAULT
    warnings = {warning.key: warning for warning in loaded.warnings}
    assert set(warnings) == {"sandbox.url", "sandbox.token"}
    for key in ("sandbox.url", "sandbox.token"):
        assert warnings[key].origin.layer is Source.USER
        assert warnings[key].origin.path == path
        assert warnings[key].outcome.reason is CoerceReason.EXPECTED_TEXT


def test_a_project_cannot_set_sandbox_url_or_token(config_dir: Path, project_root: Path) -> None:
    """``sandbox.enabled`` is already covered by the project-layer test.

    A repository must not name the address the shell connects to, or a token
    the process would send.
    """
    freeze_process_env()
    _enable_gate(config_dir)
    path = _write_project_yaml(
        project_root,
        dedent(
            """\
            sandbox:
              url: "http://from-project.example"
              token: "from-project"
            """
        ),
    )

    loaded = load_settings(project_root=project_root)

    assert loaded.settings.sandbox_url == ""
    assert loaded.settings.sandbox_token == ""
    assert loaded.source_for("sandbox.url").layer is Source.DEFAULT
    assert loaded.source_for("sandbox.token").layer is Source.DEFAULT
    warnings = {warning.key: warning for warning in loaded.warnings}
    assert set(warnings) == {"sandbox.url", "sandbox.token"}
    for key in ("sandbox.url", "sandbox.token"):
        assert warnings[key].origin.layer is Source.PROJECT
        assert warnings[key].origin.path == path
        assert warnings[key].outcome.reason is CoerceReason.NOT_ALLOWED_IN_PROJECT


def test_a_loaded_sandbox_enabled_does_not_reread_the_environment(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``Apply.RELOAD``: the object already loaded keeps the value it was built with.

    A later ``load_settings`` reads the environment again. This load does not
    freeze a bootstrap snapshot, so the second load sees the live change.
    The first object does not.
    """
    specs = specs_by_field(Settings)
    assert specs["sandbox_enabled"].apply is Apply.RELOAD
    assert specs["sandbox_url"].apply is Apply.RELOAD
    assert specs["sandbox_token"].apply is Apply.RELOAD
    assert not (config_dir / "settings.yaml").is_file()
    monkeypatch.setenv("CHRYS_SANDBOX_ENABLED", "true")

    loaded = load_settings()
    held = loaded.settings

    assert held.sandbox_enabled is True
    assert loaded.source_for("sandbox.enabled").layer is Source.ENV

    monkeypatch.setenv("CHRYS_SANDBOX_ENABLED", "false")

    assert held.sandbox_enabled is True
    assert loaded.settings is held
    again = load_settings()
    assert again.settings.sandbox_enabled is False
    assert again.source_for("sandbox.enabled").layer is Source.ENV
    assert held.sandbox_enabled is True
