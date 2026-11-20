# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_dialog`` - accept or dismiss the JavaScript dialog blocking the page."""

from __future__ import annotations

from typing import Any

from ..errors import BadRequest, BrowserError, ConnectionClosed
from ..types import ToolBlock, text_block
from .actionability import evidence_line, mark_dispatched, new_action_id, prepare_action, record_action

NAME = "browser_dialog"

DESCRIPTION = (
    "Accept or dismiss the JavaScript dialog (alert/confirm/prompt) that is currently "
    "open. While a dialog is open every other page action fails with browser_dialog_open "
    "because the page's script is paused; a dialog is dismissed automatically after "
    "ICODE_BROWSER_DIALOG_TIMEOUT seconds and the reason is reported. Accepting a prompt "
    "requires text: the page receives exactly that answer (an empty answer is refused, "
    "never submitted implicitly). Nothing is clicked or re-sent. Call browser_snapshot "
    "afterwards: an action that ran while the dialog was open is not proof."
)

PARAMS: dict[str, Any] = {
    "action": {"type": "string", "enum": ["accept", "dismiss"], "required": True},
    "text": {"type": "string", "default": "", "maxLength": 1000},
}


def _last_line(session: Any) -> str:
    last = session.last_dialog()
    return f"last={last.fields()}" if last is not None else "last=none（本会话还没有对话框记录）"


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    action = arguments["action"]
    text = arguments.get("text") or ""
    action_id = new_action_id()
    info = session.pending_dialog()
    if info is None:
        result = "\n".join(
            [
                "当前没有打开的 JavaScript 对话框",
                "status=blocked retryable=false",
                _last_line(session),
                evidence_line(session, action_id),
            ]
        )
        record_action(session, action_id, NAME, "blocked", result)
        return [text_block(result)]

    prepare_action(session, action_id, NAME, f"prepared {action} of {info.kind} dialog seq={info.seq}")
    try:
        closed, sent = await session.handle_dialog(accept=action == "accept", text=text)
    except ConnectionClosed:
        raise
    except BadRequest as exc:
        result = "\n".join(
            [
                f"未{('接受' if action == 'accept' else '拒绝')} {info.kind} 对话框：{exc.message}",
                "对话框仍在等待处理",
                "status=blocked retryable=true",
                evidence_line(session, action_id),
            ]
        )
        record_action(session, action_id, NAME, "blocked", result)
        return [text_block(result)]
    except BrowserError as exc:
        result = "\n".join(
            [
                f"未{('接受' if action == 'accept' else '拒绝')} {info.kind} 对话框：{exc.message}",
                "status=uncertain retryable=false",
                _last_line(session),
                evidence_line(session, action_id),
            ]
        )
        record_action(session, action_id, NAME, "uncertain", result)
        return [text_block(result)]

    mark_dispatched(session, action_id, NAME, f"dispatched {action} of {info.kind} dialog seq={info.seq}")
    handled = sent and closed.reason == "handled by browser_dialog"
    status = "succeeded" if handled else "uncertain"
    lines = [
        f"已{('接受' if action == 'accept' else '拒绝')} {info.kind} 对话框",
        closed.fields(),
        f"result={closed.closed} reason={closed.reason}",
    ]
    if handled and info.kind == "prompt":
        lines.append(f"prompt_text_chars={len(text)}")
    lines.append(f"status={status}" + ("" if handled else " retryable=false"))
    lines.append(evidence_line(session, action_id))
    if handled:
        lines.append("提示：对话框打开期间的动作结果不可判定，请 browser_snapshot 复核，不要重复点击/提交")
    result = "\n".join(lines)
    record_action(session, action_id, NAME, status, result)
    return [text_block(result)]
