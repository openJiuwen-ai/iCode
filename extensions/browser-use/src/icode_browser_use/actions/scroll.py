# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_scroll`` - scroll the window or an element's inner scroller."""

from __future__ import annotations

import json
from typing import Any

from ..types import ToolBlock, text_block
from ._shared import call_value, eval_value, resolve_target
from .actionability import evidence_line, new_action_id, record_action

NAME = "browser_scroll"

DESCRIPTION = (
    "Scroll the window, or a specific element's inner scroll container. direction "
    "is up/down/left/right; amount is the number of viewport-heights (or widths) to "
    "move. Give target to scroll that element's own scroller (use this when "
    "'scrolling the window does nothing' because content lives in a scrollable "
    "div). Give to_text to locate an element by text and bring it into view; when "
    "both are given target chooses the container and to_text is an extra bring-"
    "into-view. Returns the new scroll position and whether the bottom was reached."
)

PARAMS: dict[str, Any] = {
    "direction": {"enum": ["up", "down", "left", "right"], "default": "down"},
    "amount": {"type": "integer", "min": 1, "max": 10, "default": 3},
    "target": {"type": "string"},
    "to_text": {"type": "string"},
}

_DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}

_WINDOW_POS_EXPR = (
    "JSON.stringify({x: window.scrollX, y: window.scrollY,"
    " innerHeight: window.innerHeight,"
    " scrollHeight: document.documentElement ? document.documentElement.scrollHeight : 0})"
)
_ELEMENT_POS_FN = (
    "function(){ return JSON.stringify({x: this.scrollLeft, y: this.scrollTop,"
    " innerHeight: this.clientHeight, scrollHeight: this.scrollHeight}); }"
)


def _parse_position(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _element_position(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    result = await call_value(session, object_id, _ELEMENT_POS_FN, cdp_session_id=cdp_session_id)
    return _parse_position(result.get("value"))


async def _window_position(session: Any) -> dict[str, Any]:
    return _parse_position(await eval_value(session, _WINDOW_POS_EXPR))


def _at_bottom(position: dict[str, Any]) -> bool:
    offset = float(position.get("y", 0) or 0)
    inner = float(position.get("innerHeight", 0) or 0)
    scroll_height = float(position.get("scrollHeight", 0) or 0)
    return scroll_height > 0 and offset + inner >= scroll_height - 1.0


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    direction = arguments["direction"]
    amount = arguments["amount"]
    action_id = new_action_id()
    target_spec = (arguments.get("target") or "").strip()
    to_text = (arguments.get("to_text") or "").strip()

    dir_x, dir_y = _DIRECTIONS[direction]
    target_entry = await resolve_target(session, target_spec) if target_spec else None
    cdp_session_id = target_entry.cdp_session_id if target_entry is not None else None
    frame = await session.read_frame_metrics(cdp_session_id=cdp_session_id)
    viewport_w, viewport_h = frame.viewport
    step_x = dir_x * amount * viewport_w
    step_y = dir_y * amount * viewport_h

    client = session.require_client()
    scrolled_element = False
    notes: list[str] = []

    if target_entry is not None:
        object_id = await session.resolve_object_id(target_entry)
        function = (
            f"function(){{ this.scrollBy({{left: {step_x}, top: {step_y}, behavior: 'instant'}}); return true; }}"
        )
        await call_value(session, object_id, function, cdp_session_id=cdp_session_id)
        scrolled_element = True
        if to_text and to_text != target_spec:
            # Extra positioning: target selects the container, to_text brings a node
            # into view inside it.
            extra = await resolve_target(session, to_text)
            extra_id = await session.resolve_object_id(extra)
            await client.send(
                "DOM.scrollIntoViewIfNeeded",
                {"objectId": extra_id},
                session_id=extra.cdp_session_id or session.session_id,
                timeout=session.cfg.cdp_timeout,
            )
            notes.append(f"并已定位文本目标：{to_text}")
        position = await _element_position(session, object_id, cdp_session_id)
    else:
        expression = f"window.scrollBy({{left: {step_x}, top: {step_y}, behavior: 'instant'}})"
        await client.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            session_id=session.session_id,
            timeout=session.cfg.evaluate_timeout,
        )
        if to_text:
            entry = await resolve_target(session, to_text)
            object_id = await session.resolve_object_id(entry)
            await client.send(
                "DOM.scrollIntoViewIfNeeded",
                {"objectId": object_id},
                session_id=entry.cdp_session_id or session.session_id,
                timeout=session.cfg.cdp_timeout,
            )
            notes.append(f"并已定位文本目标：{to_text}")
        position = await _window_position(session)

    scope = "元素内滚动容器" if scrolled_element else "window"
    lines = [f"已滚动 {direction} ×{amount}（{scope}）"]
    lines.append(evidence_line(session, action_id))
    lines.append(
        f"新位置 x={position.get('x', 0)} y={position.get('y', 0)}；到底={'是' if _at_bottom(position) else '否'}"
    )
    lines.extend(notes)
    session.mark_dirty()
    session.note_success(session.target_id)
    text = "\n".join(lines)
    record_action(session, action_id, "browser_scroll", "succeeded", text)
    return [text_block(text)]
