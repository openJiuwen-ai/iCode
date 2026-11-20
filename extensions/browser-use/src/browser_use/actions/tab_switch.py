# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_tab_switch`` - make another owned tab current."""

from __future__ import annotations

from typing import Any

from ..types import ToolBlock, text_block
from ._shared import display_url

NAME = "browser_tab_switch"

DESCRIPTION = (
    "Switch the current tab to one listed by browser_tabs (tab_id). Refs from the "
    "old tab become stale, later write actions target the new tab, the page is "
    "brought to the front when possible, and a fresh snapshot of it is returned. "
    "Tabs not created by this handle cannot be selected."
)

PARAMS: dict[str, Any] = {
    "tab_id": {"type": "string", "required": True},
}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    info = await session.switch_page(str(arguments["tab_id"]))
    from . import snapshot as snapshot_action

    blocks = await snapshot_action.run(
        session,
        {"scope": "viewport", "interactive_only": False, "since": 0},
    )
    snapshot_text = "\n".join(str(block.get("text", "")) for block in blocks if block.get("type") == "text")
    prefix = f"已切换到 #{info.index} {display_url(info.url)}"
    return [text_block(prefix + ("\n" + snapshot_text if snapshot_text else ""))]
