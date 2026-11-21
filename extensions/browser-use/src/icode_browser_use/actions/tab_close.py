# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_tab_close`` - close one tab this handle owns."""

from __future__ import annotations

from typing import Any

from ..types import ToolBlock, text_block
from . import tabs

NAME = "browser_tab_close"

DESCRIPTION = (
    "Close one tab listed by browser_tabs (tab_id). Closing the current tab makes its "
    "opener (or the first remaining tab) current, and its refs become stale: snapshot "
    "before acting again. The handle's last tab cannot be closed this way (use "
    "browser_close), and tabs not created by this handle are never closed."
)

PARAMS: dict[str, Any] = {
    "tab_id": {"type": "string", "required": True},
}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    tab_id = str(arguments["tab_id"])
    was_current = tab_id == session.target_id
    await session.close_page(tab_id)
    lines = [f"已关闭标签页 {tab_id}"]
    if was_current:
        lines.append("当前标签页已切换，旧 ref 全部失效；继续操作前请 browser_snapshot")
    listing = await tabs.run(session, {})
    lines.extend(str(block.get("text", "")) for block in listing)
    return [text_block("\n".join(lines))]
