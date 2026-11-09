# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Public entry for the shared AIxCoding configuration snapshot."""

from .loader import load_config
from .types import AixcodingConfig, ModelCatalogConfig

__all__ = ["AixcodingConfig", "ModelCatalogConfig", "load_config"]
