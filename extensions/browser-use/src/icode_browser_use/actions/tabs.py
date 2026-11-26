# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_tabs`` - list the tabs this browser handle owns."""

from __future__ import annotations

from typing import Any

from ..policy import sanitize_display_title
from ..types import ToolBlock, text_block
from ._shared import display_url, truncate_chars

NAME = "browser_tabs"

DESCRIPTION = (
    "List the tabs this browser handle owns: the tab it created plus popups that "
    "tab opened. Each row has an index, tab_id, url, title, opener and a current "
    "marker. Tabs opened by the user or another tool are not listed and are never "
    "driven or closed. Use browser_tab_switch with a tab_id to change the current tab."
)

PARAMS: dict[str, Any] = {}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    pages = await session.list_pages()
    if not pages:
        return [text_block("标签页：0（尚未启动；调用 browser_navigate 或 browser_snapshot 启动）")]
    current = next((info.index for info in pages if info.current), None)
    marker = f"#{current}" if current is not None else "未知"
    lines = [f"标签页 {len(pages)} 个（当前 {marker}）"]
    for info in pages:
        marker = "*" if info.current else "-"
        opener = info.opener_id or "-"
        active = getattr(info, "active", None)
        vis = " [foreground]" if active is True else (" [background]" if active is False else "")
        lines.append(
            f"#{info.index} {marker} tab_id={info.target_id} opener={opener} "
            f"url={display_url(info.url)} title={truncate_chars(sanitize_display_title(info.title, info.url), 120)}{vis}"
        )
    return [text_block("\n".join(lines))]
