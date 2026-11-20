# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Normalize the saved source kind shared by workflows and hooks."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Final

type BackupOriginal = Callable[[Path, bytes], None]
type ReportReadError = Callable[[Path, Exception], None]

SOURCE_KIND_WORKDIR: Final = "workdir"


def source_edit(record: object, field: str = "source_kind") -> bool:
    """Replace a retired source kind in a decoded record."""
    if not isinstance(record, dict) or record.get(field) != "project":
        return False
    record[field] = SOURCE_KIND_WORKDIR
    return True
