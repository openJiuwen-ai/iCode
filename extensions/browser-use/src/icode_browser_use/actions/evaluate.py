# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_evaluate`` - run page-context JS (only when explicitly enabled)."""

from __future__ import annotations

import json
from typing import Any

from ..errors import BadRequest, CDPTimeout, ConnectionClosed
from ..types import ToolBlock, text_block
from ._shared import truncate_chars
from .actionability import evidence_line, mark_dispatched, new_action_id, prepare_action, record_action

NAME = "browser_evaluate"

DESCRIPTION = (
    "Last resort: evaluate a JavaScript expression in the page context and return its value. "
    "Prefer the dedicated tools first — browser_find/browser_snapshot to locate, browser_extract "
    "to read text, browser_click/browser_fill to act, browser_wait_for to wait. "
    "Registered only when the plugin is started with ICODE_BROWSER_EVALUATE=allow; "
    "otherwise this tool is absent from tools/list and a stray call fails with "
    "browser_bad_request. A single 10s timeout applies; network is still limited to "
    "the allowlist. A page-side exception is reported as result text (not an error)."
)

PARAMS: dict[str, Any] = {
    "expression": {"type": "string", "required": True},
    "await_promise": {"type": "boolean", "default": False},
}


def _json_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _describe_exception(exception: dict[str, Any], result: dict[str, Any]) -> str:
    parts: list[str] = []
    description = result.get("description") or exception.get("text")
    if description:
        parts.append(str(description))
    inner = exception.get("exception") or {}
    if inner.get("description") and str(inner["description"]) not in parts:
        parts.append(str(inner["description"]))
    if exception.get("stackTrace"):
        parts.append("(stack trace omitted)")
    return "\n".join(parts) if parts else "未知页面异常"


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    if not session.cfg.evaluate_enabled:
        raise BadRequest("browser_evaluate 未启用（ICODE_BROWSER_EVALUATE=allow 才注册）")

    expression = arguments["expression"]
    await_promise = arguments["await_promise"]

    client = session.require_client()
    action_id = new_action_id()
    # Evidence must never carry the expression itself: it can hold tokens or typed text.
    note = f"prepared evaluate (await_promise={bool(await_promise)})"
    prepare_action(session, action_id, "browser_evaluate", note)
    try:
        response = await client.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": bool(await_promise),
                "returnByValue": True,
            },
            session_id=session.session_id,
            timeout=session.cfg.evaluate_timeout,
        )
    except (ConnectionClosed, CDPTimeout):
        record_action(
            session,
            action_id,
            "browser_evaluate",
            "uncertain",
            f"status=uncertain retryable=false reason=dispatch_failed；{evidence_line(session, action_id)}",
        )
        raise
    mark_dispatched(session, action_id, "browser_evaluate", f"dispatched evaluate；{evidence_line(session, action_id)}")

    exception = response.get("exceptionDetails")
    result = response.get("result") or {}
    if exception is not None:
        text = "页面异常（未抛错，按结果文本呈现）：\n" + _describe_exception(exception, result)
        outcome = "uncertain"
        reason = "page_exception"
    elif "value" in result:
        text = _json_value(result["value"])
        outcome = "succeeded"
        reason = ""
    else:
        text = str(result.get("description") or result.get("type") or "")
        outcome = "succeeded"
        reason = ""

    text = truncate_chars(text, session.cfg.snapshot_char_budget)
    evidence = f"status={outcome} retryable=false"
    if reason:
        evidence += f" reason={reason}"
    record_action(session, action_id, "browser_evaluate", outcome, f"{evidence}；{evidence_line(session, action_id)}")
    session.note_success(session.target_id)
    return [text_block(text)]
