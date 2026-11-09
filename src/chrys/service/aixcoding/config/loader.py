# ruff: noqa: T201
# (stderr notes are the observable-degradation channel, DEVELOPMENT.md §9)
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read and cache ``~/.chrys/aixcoding.toml`` for AIxCoding features."""

from __future__ import annotations

import logging
import os
import sys
import tomllib
from functools import cache
from pathlib import Path

from chrys.kernel.aixcoding.diagnostics.config import (
    DEFAULT_LEVEL,
    DEFAULT_PANEL,
    DiagnosticsConfig,
    LoggingConfig,
)

from .types import AixcodingConfig, ModelCatalogConfig

CONFIG_FILE_NAME = "aixcoding.toml"
ENVIRONMENT_VARIABLE = "AIXCODING_ENVIRONMENT"
VALID_ENVIRONMENTS = ("local", "dev", "prod")
DEFAULT_ENVIRONMENT = "prod"

_LEVEL_BY_NAME = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _warn(message: str) -> None:
    print(f"aixcoding.config: {message}", file=sys.stderr)


def _resolve_config_dir(config_dir: Path | None) -> Path:
    if config_dir is not None:
        return config_dir
    try:
        from chrys.foundation.platform import get_platform

        return get_platform().config_dir
    except Exception:
        _warn("platform config_dir unavailable, using ~/.chrys")
        return Path.home() / ".chrys"


def _resolve_environment(data: dict, source: Path | None) -> str:
    if ENVIRONMENT_VARIABLE in os.environ:
        raw: object = os.environ[ENVIRONMENT_VARIABLE]
        origin = ENVIRONMENT_VARIABLE
    elif "environment" in data:
        raw = data["environment"]
        origin = str(source) if source is not None else "aixcoding.toml"
    else:
        return DEFAULT_ENVIRONMENT
    if not isinstance(raw, str):
        _warn(f"invalid environment from {origin}; using {DEFAULT_ENVIRONMENT}")
        return DEFAULT_ENVIRONMENT
    value = raw.strip().lower()
    if value not in VALID_ENVIRONMENTS:
        _warn(f"invalid environment from {origin}; using {DEFAULT_ENVIRONMENT}")
        return DEFAULT_ENVIRONMENT
    return value


def _coerce_level(value: object) -> int:
    if isinstance(value, str):
        return _LEVEL_BY_NAME.get(value.strip().upper(), logging.INFO)
    return logging.INFO


def _load_diagnostics(data: dict, source: Path | None) -> DiagnosticsConfig:
    section = data.get("logging")
    if not isinstance(section, dict):
        section = {}
    panel_value = section.get("panel", DEFAULT_PANEL)
    return DiagnosticsConfig(
        logging=LoggingConfig(
            level=_coerce_level(section.get("level", DEFAULT_LEVEL)),
            panel=panel_value if isinstance(panel_value, bool) else DEFAULT_PANEL,
        ),
        source=source,
    )


def _load_model_catalog(data: dict) -> ModelCatalogConfig:
    section = data.get("model_catalog", {})
    if not isinstance(section, dict):
        _warn("invalid [model_catalog] section; using the deployment tier base")
        return ModelCatalogConfig()
    raw = section.get("base_url", "")
    if not isinstance(raw, str):
        _warn("invalid model_catalog.base_url; using the deployment tier base")
        return ModelCatalogConfig()
    return ModelCatalogConfig(base_url=raw.strip())


@cache
def load_config(config_dir: Path | None = None) -> AixcodingConfig:
    """Return one fail-safe snapshot; environment variables override TOML."""
    path = _resolve_config_dir(config_dir) / CONFIG_FILE_NAME
    source: Path | None = path
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
        source = None
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        _warn(f"cannot parse {path} ({exc}); using defaults")
        data = {}
    return AixcodingConfig(
        environment=_resolve_environment(data, source),
        diagnostics=_load_diagnostics(data, source),
        model_catalog=_load_model_catalog(data),
    )
