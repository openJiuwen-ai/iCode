# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fork-owned snapshot types for the shared AIxCoding configuration."""

from __future__ import annotations

from dataclasses import dataclass

from chrys.kernel.aixcoding.diagnostics.config import DiagnosticsConfig


@dataclass(frozen=True)
class ModelCatalogConfig:
    """``[model_catalog]`` section: optional service base URL."""

    base_url: str = ""


@dataclass(frozen=True)
class AixcodingConfig:
    """One process-wide configuration snapshot."""

    environment: str
    diagnostics: DiagnosticsConfig
    model_catalog: ModelCatalogConfig
