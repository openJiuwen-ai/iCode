# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_hover`` - move the mouse over an element without clicking."""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import BrowserError, ConnectionClosed
from ..types import ToolBlock, text_block
from ._shared import resolve_target
from .actionability import evidence_line, hit_test, new_action_id, record_action
from .click import _hit_point

NAME = "browser_hover"

DESCRIPTION = (
    "Move the mouse over target (an @eN ref or free text) without clicking. "
    "Use this for controls that appear only on hover (for example a hidden "
    "delete button), then call browser_snapshot to read the newly visible elements."
)

PARAMS: dict[str, Any] = {
    "target": {"type": "string", "required": True},
}

_HOVER_SETTLE = 0.15
_sleep = asyncio.sleep


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    target = arguments["target"]
    entry = await resolve_target(session, target)
    action_id = new_action_id()
    object_id = await session.resolve_object_id(entry)
    cdp_session_id = entry.cdp_session_id
    offset = session.oopif_offset(cdp_session_id)

    x, y = await _hit_point(session, object_id, entry, cdp_session_id, offset)
    try:
        hit = await hit_test(
            session,
            object_id,
            x - offset[0] if cdp_session_id else x,
            y - offset[1] if cdp_session_id else y,
            cdp_session_id,
            entry=entry,
        )
    except ConnectionClosed:
        raise
    except BrowserError:
        hit = None
    if hit is None or (isinstance(hit, dict) and hit.get("inside") is False):
        reason = (
            f"目标中心被其他元素遮挡（命中 {hit.get('tag') or 'unknown'}）"
            if isinstance(hit, dict)
            else "命中检测失败，无法确认目标未被遮挡"
        )
        text = (
            f"未悬停 {entry.token}：{reason}；status=uncertain retryable=false；"
            "请先关闭遮挡层或重新 snapshot\n"
            f"{evidence_line(session, action_id)}"
        )
        record_action(session, action_id, "browser_hover", "uncertain", text)
        return [text_block(text)]
    client = session.require_client()
    await client.send(
        "Input.dispatchMouseEvent",
        {"type": "mouseMoved", "x": x, "y": y, "button": "none", "buttons": 0},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    await _sleep(_HOVER_SETTLE)

    session.mark_dirty()
    session.note_success(session.target_id)
    text = (
        f"已悬停 {entry.token}；请调用 browser_snapshot 查看悬停后才出现的控件"
        "（本工具不会点击）\n"
        f"{evidence_line(session, action_id)}"
    )
    record_action(session, action_id, "browser_hover", "succeeded", text)
    return [text_block(text)]
