# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""``browser_action_status`` - look up a recorded action result by action_id."""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest
from ..journal import describe
from ..types import ToolBlock, text_block
from .actionability import lookup_action

NAME = "browser_action_status"

DESCRIPTION = (
    "Look up a previous click, fill, upload, press_key, select_option, dialog or evaluate result by "
    "action_id. Use it after an uncertain result or a reconnect to check whether the action "
    "was recorded, instead of blindly repeating the action. Records are per browser: pass "
    "the browser_id whose journal recorded the action. It never retries anything."
)

PARAMS: dict[str, Any] = {
    "action_id": {"type": "string", "required": True},
}


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    if session is None:
        raise BadRequest("browser_action_status 需要一个 browser_id 才能查询动作记录")
    record = lookup_action(session, str(arguments["action_id"]))
    if record is None:
        return [
            text_block("status=unknown: no such action_id for this browser; check the browser_id or snapshot again")
        ]
    return [text_block(describe(record))]
