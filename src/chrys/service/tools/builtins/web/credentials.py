# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build-scoped web secrets from the process environment frozen at bootstrap.

Destination grants are checked by the caller before requesting any values here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from chrys.foundation.config.process_env import canonical_env_name, process_env_snapshot


@dataclass(frozen=True)
class ResolvedSearchCredentials:
    """Secrets excluded from repr and never passed to profile serialization."""

    values: dict[str, str] = field(repr=False)


def resolve_search_credentials(names: set[str]) -> ResolvedSearchCredentials:
    snapshot = process_env_snapshot()
    process = snapshot.values if snapshot is not None else os.environ
    result: dict[str, str] = {}
    for name in names:
        key = canonical_env_name(name)
        value = process.get(key, "")
        if not value:
            raise ValueError(f"Missing web search credential {name}")
        result[name] = value
    return ResolvedSearchCredentials(result)
