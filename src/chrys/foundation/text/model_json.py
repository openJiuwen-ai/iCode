# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JSON text written for a model to read."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from chrys.foundation.platform.files import surrogate_safe_text

# JSON already escapes every other line boundary recognized by str.splitlines().
_JSON_LINE_BREAK_ESCAPES = str.maketrans({"\u0085": r"\u0085", "\u2028": r"\u2028", "\u2029": r"\u2029"})


def model_json(value: Any, *, default: Callable[[Any], Any] | None = None, indent: int | None = None) -> str:
    """*value* as JSON with non-ASCII text left as written.

    ``ensure_ascii`` turns each CJK character or emoji into a six-character
    escape that the model reads less easily and that costs more tokens. A lone
    surrogate keeps its ``\\uXXXX`` escape, so the request still encodes as
    UTF-8 and the JSON reads back to the same value.

    Persisted, signed and identity JSON keeps ``ensure_ascii``: its bytes are
    compared.
    """
    return surrogate_safe_text(json.dumps(value, default=default, indent=indent, ensure_ascii=False))


def model_path(path: str) -> str:
    """Render *path* as a single-line, round-trippable JSON string for the model."""
    return model_json(path).translate(_JSON_LINE_BREAK_ESCAPES)
