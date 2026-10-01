# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-approval value snapshots, deliberately separate from reusable DAA keys."""

from __future__ import annotations

from enum import Enum
from pathlib import PurePath

from chrys.kernel.exceptions import ModelVisibleToolError


def argument_snapshot(value: object) -> object:
    """Freeze typed values without erasing types, aliasing mutable data or comparing NaNs."""
    try:
        return _freeze(value)
    except (ValueError, RecursionError):
        # Unknown host objects must not compare equal just because neither has
        # a JSON identity. Do not loop forever asking about an unverifiable call.
        raise ModelVisibleToolError("Tool arguments cannot be safely compared for approval.") from None


def _freeze(value: object) -> tuple:
    kind = type(value)
    if isinstance(value, Enum):
        # Pydantic's Python-mode dump retains Enum instances. Their class and
        # frozen value must remain distinct from strings/integers and other enums.
        return kind, value.name, _freeze(value.value)
    if kind in (str, bool, int, bytes, type(None)):
        return kind, value
    if type(value) is float:
        return kind, value.hex()
    if type(value) is bytearray:
        return kind, bytes(value)
    if isinstance(value, PurePath):
        return kind, str(value)
    if type(value) is dict:
        return kind, frozenset((_freeze(key), _freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)) and kind in (list, tuple):
        return kind, tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)) and kind in (set, frozenset):
        return kind, frozenset(_freeze(item) for item in value)
    raise ValueError("Unsupported approval snapshot value")
