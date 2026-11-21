# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``browser_close`` - close the browser and release its process."""

from __future__ import annotations

from typing import Any

from ..types import ToolBlock, text_block

NAME = "browser_close"

DESCRIPTION = "Close the browser and release its process."

PARAMS: dict[str, Any] = {}


async def run(session: Any, _arguments: dict[str, Any]) -> list[ToolBlock]:
    await session.close_current()
    return [text_block("browser closed")]
