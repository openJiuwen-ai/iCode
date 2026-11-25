# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_evaluate`` - run page-context JS (only when explicitly enabled)."""

from __future__ import annotations

import contextlib
import json
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed
from ..types import ToolBlock, text_block
from ._shared import send_watching_dialog, truncate_chars
from .actionability import amark_dispatched, aprepare_action, arecord_action, evidence_line, new_action_id

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


#: Bounds an object result on the page side so a huge value never crosses the
#: socket whole. Runs on the result object only: the agent's expression itself
#: goes to ``Runtime.evaluate`` verbatim and never through page-visible globals
#: such as ``window.eval``, which a page can replace to see or spoof it.
_BOUNDED_OBJECT_FN = (
    "function(cap){ let s; try { s = JSON.stringify(this); } catch (_) { return {v: this}; }"
    " if (typeof s === 'string' && s.length > cap) return {cut: s.slice(0, cap), n: s.length};"
    " return {v: this}; }"
)


async def _object_text(client: Any, session: Any, object_id: str, cap: int, fallback: str) -> str:
    """Serialize an object result, truncated in the page to *cap* characters."""
    try:
        response = await client.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": _BOUNDED_OBJECT_FN,
                "arguments": [{"value": cap}],
                "returnByValue": True,
            },
            session_id=session.session_id,
            timeout=session.cfg.evaluate_timeout,
        )
    except (CDPTimeout, BrowserError):
        return fallback
    if response.get("exceptionDetails") is not None:
        return fallback
    value = (response.get("result") or {}).get("value")
    if not isinstance(value, dict):
        return fallback
    if isinstance(value.get("cut"), str):
        return f"{value['cut']}… (+{int(value.get('n') or 0) - cap} more chars)"
    return _json_value(value.get("v"))


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    if not session.cfg.evaluate_enabled:
        raise BadRequest("browser_evaluate 未启用（ICODE_BROWSER_EVALUATE=allow 才注册）")

    expression = arguments["expression"]
    await_promise = arguments["await_promise"]
    budget = int(getattr(session.cfg, "snapshot_char_budget", 8000) or 8000)

    client = session.require_client()
    action_id = new_action_id()
    # Evidence must never carry the expression itself: it can hold tokens or typed text.
    note = f"prepared evaluate (await_promise={bool(await_promise)})"
    await aprepare_action(session, action_id, "browser_evaluate", note)
    dialog_seq = getattr(session, "dialog_seq", 0)
    recorded = False

    object_group = f"chrys-eval-{action_id}"

    async def _send_eval() -> dict[str, Any]:
        return await client.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": bool(await_promise),
                "returnByValue": False,
                "objectGroup": object_group,
                "allowUnsafeEvalBlockedByCSP": True,
            },
            session_id=session.session_id,
            timeout=session.cfg.evaluate_timeout,
        )

    def _mark_dirty() -> None:
        mark = getattr(session, "mark_dirty", None)
        if callable(mark):
            mark()

    try:
        try:
            if callable(getattr(session, "dialog_since", None)):
                response, dialog = await send_watching_dialog(session, dialog_seq, _send_eval)
            else:
                response, dialog = await _send_eval(), None
        except (ConnectionClosed, CDPTimeout, BrowserError):
            _mark_dirty()
            await arecord_action(
                session,
                action_id,
                "browser_evaluate",
                "uncertain",
                f"status=uncertain retryable=false reason=dispatch_failed；{evidence_line(session, action_id)}",
            )
            recorded = True
            raise
        _mark_dirty()
        if dialog is not None:
            await amark_dispatched(
                session,
                action_id,
                "browser_evaluate",
                f"evaluate in flight; dialog opened；{evidence_line(session, action_id)}",
            )
            text = "\n".join(
                [
                    "已发送 evaluate，页面弹出对话框（执行结果未确认）",
                    dialog.fields(),
                    "status=blocked retryable=false",
                    evidence_line(session, action_id),
                ]
            )
            await arecord_action(session, action_id, "browser_evaluate", "blocked", text)
            recorded = True
            return [text_block(text)]
        await amark_dispatched(
            session, action_id, "browser_evaluate", f"dispatched evaluate；{evidence_line(session, action_id)}"
        )
        response = response or {}

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
        elif result.get("objectId"):
            fallback = str(result.get("description") or result.get("type") or "")
            # Leave room for the "(+N more chars)" suffix inside the budget.
            cap = max(1, budget - 64)
            text = await _object_text(client, session, str(result["objectId"]), cap, fallback)
            outcome = "succeeded"
            reason = ""
        else:
            text = str(result.get("unserializableValue") or result.get("description") or result.get("type") or "")
            outcome = "succeeded"
            reason = ""
        if result.get("objectId") or ((exception or {}).get("exception") or {}).get("objectId"):
            # Only after a result: while a dialog holds the page, this would block too.
            with contextlib.suppress(Exception):
                await client.send(
                    "Runtime.releaseObjectGroup",
                    {"objectGroup": object_group},
                    session_id=session.session_id,
                    timeout=session.cfg.evaluate_timeout,
                )

        text = truncate_chars(text, budget)
        evidence = f"status={outcome} retryable=false"
        if reason:
            evidence += f" reason={reason}"
        await arecord_action(
            session, action_id, "browser_evaluate", outcome, f"{evidence}；{evidence_line(session, action_id)}"
        )
        recorded = True
        session.note_success(session.target_id)
        return [text_block(text)]
    finally:
        if not recorded:
            await arecord_action(
                session,
                action_id,
                "browser_evaluate",
                "uncertain",
                f"status=uncertain retryable=false reason=aborted；{evidence_line(session, action_id)}",
            )
