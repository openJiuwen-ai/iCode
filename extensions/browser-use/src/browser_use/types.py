# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small shared data types: tool content blocks and tool definitions.

Kept free of any transport import so that ``actions`` can build tool definitions
and results without importing the session or the extension adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A single tool content block, e.g. ``{"type": "text", "text": ...}``.
ToolBlock = dict[str, Any]


@dataclass(frozen=True)
class ToolDef:
    """A tool as advertised by ``tools/list``.

    Carries only the static contract - name, description and the raw parameter
    spec. Execution always goes through ``actions.dispatch``; there is no
    per-tool callback field.
    """

    name: str
    description: str
    params: dict[str, Any]
    schema: dict[str, Any]


def text_block(text: str) -> ToolBlock:
    """Build a model-visible text content block."""
    return {"type": "text", "text": text}


def image_block(data: str, mime_type: str) -> ToolBlock:
    """Build a model-visible image content block (base64 payload)."""
    return {"type": "image", "data": data, "mimeType": mime_type}
