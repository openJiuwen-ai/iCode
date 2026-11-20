# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_forward`` - step forward through the navigation history."""

from __future__ import annotations

from typing import Any

from ..types import ToolBlock
from .back import step_history

NAME = "browser_forward"

DESCRIPTION = (
    "Go forward one entry in the page's navigation history (the inverse of "
    "browser_back, via Page.navigateToHistoryEntry). At the history end it returns a "
    "plain notice and does NOT error. On success it waits for the navigation and "
    "returns a fresh snapshot with new @eN refs. Not auto-retried (it advances history)."
)

PARAMS: dict[str, Any] = {}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    return await step_history(session, 1, "已经在历史末尾，未前进")
