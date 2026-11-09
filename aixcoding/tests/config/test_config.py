# (tests — asserts are pytest idiom, Chinese prose is the data)
"""Tests for chrys.service.aixcoding.config."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from chrys.service.aixcoding.config import AixcodingConfig, ModelCatalogConfig, load_config
from chrys.service.aixcoding.config import loader as config_loader

OLD_ENV_NAMES = (
    "AIXCODING_AUTH_ENVIRONMENT",
    "CHRYS_AUTH_ENVIRONMENT",
    "CHRYS_MODEL_CATALOG_BASE_URL",
    "CHRYS_MODEL_CATALOG_URL",
    "CHRYS_MODEL_CATALOG_TOKEN",
    "CHRYS_MODEL_CATALOG_SYNC_INTERVAL",
    "CHRYS_REMOTE_MODEL_CONFIG_URL",
)


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch: pytest.MonkeyPatch):
    load_config.cache_clear()
    monkeypatch.delenv(config_loader.ENVIRONMENT_VARIABLE, raising=False)
    for name in OLD_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    yield
    load_config.cache_clear()


def write_toml(config_dir: Path, text: str) -> Path:
    path = config_dir / "aixcoding.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_defaults_when_file_missing(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert isinstance(cfg, AixcodingConfig)
    assert cfg.environment == "prod"
    assert cfg.diagnostics.logging.level == logging.INFO
    assert cfg.diagnostics.logging.panel is False
    assert cfg.diagnostics.source is None
    assert cfg.model_catalog == ModelCatalogConfig()


def test_parses_complete_toml(tmp_path: Path):
    path = write_toml(
        tmp_path,
        'environment = " LOCAL "\n\n[logging]\nlevel = "DEBUG"\npanel = true\n\n'
        '[model_catalog]\nbase_url = " http://catalog.example/csas "\n',
    )
    cfg = load_config(tmp_path)
    assert cfg.environment == "local"
    assert cfg.diagnostics.logging.level == logging.DEBUG
    assert cfg.diagnostics.logging.panel is True
    assert cfg.diagnostics.source == path
    assert cfg.model_catalog.base_url == "http://catalog.example/csas"


def test_environment_precedence_env_toml_prod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert load_config(tmp_path).environment == "prod"
    write_toml(tmp_path, 'environment = "dev"\n')
    load_config.cache_clear()
    assert load_config(tmp_path).environment == "dev"
    monkeypatch.setenv(config_loader.ENVIRONMENT_VARIABLE, " LOCAL ")
    load_config.cache_clear()
    assert load_config(tmp_path).environment == "local"


@pytest.mark.parametrize("source", ["env", "toml"])
@pytest.mark.parametrize("raw", ["", "staging"])
def test_invalid_environment_warns_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    raw: str,
    source: str,
):
    if source == "env":
        write_toml(tmp_path, 'environment = "local"\n')
        monkeypatch.setenv(config_loader.ENVIRONMENT_VARIABLE, raw)
    else:
        write_toml(tmp_path, f'environment = "{raw}"\n')
    assert load_config(tmp_path).environment == "prod"
    assert "aixcoding.config: invalid environment" in capsys.readouterr().err


def test_invalid_toml_preserves_environment_variable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
):
    write_toml(tmp_path, "not [ valid toml")
    monkeypatch.setenv(config_loader.ENVIRONMENT_VARIABLE, "local")
    cfg = load_config(tmp_path)
    assert cfg.environment == "local"
    assert cfg.diagnostics.logging.level == logging.INFO
    assert cfg.model_catalog.base_url == ""
    assert "cannot parse" in capsys.readouterr().err


def test_bad_level_name_falls_back(tmp_path: Path):
    write_toml(tmp_path, '[logging]\nlevel = "NOISY"\n')
    cfg = load_config(tmp_path)
    assert cfg.diagnostics.logging.level == logging.INFO


def test_bad_types_fall_back_safely(tmp_path: Path, capsys):
    write_toml(tmp_path, 'environment = 1\n[logging]\npanel = "yes"\n[model_catalog]\nbase_url = 42\n')
    cfg = load_config(tmp_path)
    assert cfg.environment == "prod"
    assert cfg.diagnostics.logging.panel is False
    assert cfg.model_catalog.base_url == ""
    err = capsys.readouterr().err
    assert "invalid environment" in err
    assert "invalid model_catalog.base_url" in err


def test_cache_returns_one_snapshot(tmp_path: Path):
    write_toml(tmp_path, 'environment = "local"\n')
    first = load_config(tmp_path)
    write_toml(tmp_path, 'environment = "prod"\n')
    assert load_config(tmp_path) is first
    assert load_config(tmp_path).environment == "local"
    load_config.cache_clear()
    assert load_config(tmp_path).environment == "prod"


def test_public_reexport_and_cache_clear():
    assert load_config is config_loader.load_config
    assert hasattr(load_config, "cache_clear")


def test_settings_yaml_is_not_read(tmp_path: Path):
    (tmp_path / "settings.yaml").write_text("model:\n  catalog:\n    base_url: http://old.example\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.model_catalog.base_url == ""


@pytest.mark.parametrize("name", OLD_ENV_NAMES)
def test_old_environment_names_are_ignored(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    monkeypatch.setenv(name, "local")
    cfg = load_config(tmp_path)
    assert cfg.environment == "prod"
    assert capsys.readouterr().err == ""
