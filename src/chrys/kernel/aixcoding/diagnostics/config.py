# ruff: noqa: T201
# (stderr notes are the observable-degradation channel, DEVELOPMENT.md §9)
"""Diagnostics configuration types and platform-directory resolution.

The shared TOML reader lives in ``chrys.service.aixcoding.config``; this kernel
module deliberately has no file-loading or service-tier dependency.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LEVEL = "INFO"
DEFAULT_PANEL = False
VALID_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


@dataclass(frozen=True)
class LoggingConfig:
    """``[logging]`` section: file level and F6-panel bubble switch."""

    level: int
    panel: bool


@dataclass(frozen=True)
class DiagnosticsConfig:
    """Parsed diagnostics config; ``source`` names the file actually read."""

    logging: LoggingConfig
    source: Path | None


def resolve_config_dir(config_dir: Path | None) -> Path | None:
    """Use the caller's dir; else the upstream platform dir, lazily."""
    if config_dir is not None:
        return config_dir
    try:  # lazy + fully degraded: logging is optional (R9: foundation tier)
        from chrys.foundation.platform import get_platform

        return get_platform().config_dir
    except Exception:
        print("aixcoding.config: platform config_dir unavailable, using ~/.chrys", file=sys.stderr)
        return Path.home() / ".chrys"
