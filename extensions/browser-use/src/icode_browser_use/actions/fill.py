# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_fill`` - type into a field via real input events with a fallback."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed, StaleRef
from ..types import ToolBlock, text_block
from ._shared import call_value, dialog_since, eval_value, resolve_target, send_watching_dialog
from .actionability import (
    WriteOutcome,
    evidence_line,
    field_state,
    focus_target,
    mark_dispatched,
    new_action_id,
    normalize_text,
    page_state,
    prepare_action,
    record_action,
    submit_verdict,
)
from .wait_for import EXPECT_PARAMS, prepare_expect_baseline, run_post_write_expect

NAME = "browser_fill"

DESCRIPTION = (
    "Focus target (an @eN ref or free text) and type text using Input.insertText, "
    "which produces real input events compatible with React/controlled components. "
    "clear=true (default) empties the field first (trusted Ctrl+A / Backspace; a "
    "contenteditable is never given a JS `value` expando). submit=true sends a real "
    "Enter and then verifies the outcome; it reports submitted only when the editor "
    "cleared or the page navigated, otherwise it says the submission is unconfirmed. "
    "For chat-style SPAs (composer + send button) prefer submit=true over a separate "
    "browser_press_key or a click on the send button. Non-idempotent: not auto-retried."
)

PARAMS: dict[str, Any] = {
    "target": {"type": "string", "required": True},
    "text": {"type": "string", "required": True},
    "clear": {"type": "boolean", "default": True},
    "submit": {"type": "boolean", "default": False},
    "keypress_fallback": {"type": "boolean", "default": False},
    **EXPECT_PARAMS,
}

_CLEAR_FN = (
    "function(){"
    " const tag=(this.tagName||'').toLowerCase();"
    " const type=(this.type||'').toLowerCase();"
    " const textLike=tag==='textarea' || (tag==='input' &&"
    "   ['text','search','email','url','tel','password','number',''].includes(type));"
    " if (textLike) {"
    "  this.value=''; this.dispatchEvent(new Event('input',{bubbles:true}));"
    " }"
    " return true;"
    "}"
)

_SUBMIT_POLL = 0.15
_SUBMIT_BUDGET = 6
_sleep = asyncio.sleep
_normalize_text = normalize_text
_submit_verdict = submit_verdict


def _values_match(value: Any, expected: str, *, clear: bool = True) -> bool:
    actual = _normalize_text(value)
    wanted = _normalize_text(expected)
    if not wanted:
        return actual == "" if clear else True
    if not actual:
        return False
    return actual == wanted if clear else wanted in actual


async def _dispatch_key(session: Any, event_type: str, *, cdp_session_id: str | None = None, **fields: Any) -> None:
    client = session.require_client()
    params: dict[str, Any] = {"type": event_type}
    params.update({key: value for key, value in fields.items() if value is not None})
    await client.send(
        "Input.dispatchKeyEvent",
        params,
        session_id=cdp_session_id or session.session_id,
        timeout=session.cfg.cdp_timeout,
    )


async def _press_key(
    session: Any,
    key: str,
    code: str,
    virtual_key: int,
    *,
    text: str | None = None,
    cdp_session_id: str | None = None,
) -> None:
    await _dispatch_key(
        session,
        "keyDown",
        cdp_session_id=cdp_session_id,
        key=key,
        code=code,
        text=text,
        unmodifiedText=text,
        windowsVirtualKeyCode=virtual_key,
        nativeVirtualKeyCode=virtual_key,
    )
    await _dispatch_key(
        session,
        "keyUp",
        cdp_session_id=cdp_session_id,
        key=key,
        code=code,
        windowsVirtualKeyCode=virtual_key,
        nativeVirtualKeyCode=virtual_key,
    )


async def _select_and_delete(session: Any, cdp_session_id: str | None) -> None:
    """Select-all (Cmd+A on macOS, Ctrl+A elsewhere) then Backspace, after verifying focus."""
    select_mod = 4 if sys.platform == "darwin" else 2
    await _dispatch_key(
        session,
        "keyDown",
        cdp_session_id=cdp_session_id,
        key="a",
        code="KeyA",
        windowsVirtualKeyCode=65,
        modifiers=select_mod,
    )
    await _dispatch_key(
        session,
        "keyUp",
        cdp_session_id=cdp_session_id,
        key="a",
        code="KeyA",
        windowsVirtualKeyCode=65,
        modifiers=select_mod,
    )
    await _press_key(session, "Backspace", "Backspace", 8, cdp_session_id=cdp_session_id)


async def _type_characters(session: Any, text: str, cdp_session_id: str | None) -> None:
    """Type raw key events; only `char` carries the text, so it is inserted once."""
    for char in text:
        await _dispatch_key(session, "keyDown", cdp_session_id=cdp_session_id, key=char)
        await _dispatch_key(session, "char", cdp_session_id=cdp_session_id, key=char, text=char, unmodifiedText=char)
        await _dispatch_key(session, "keyUp", cdp_session_id=cdp_session_id, key=char)


async def _read_state(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    return await field_state(session, object_id, cdp_session_id, call=call_value)


async def _page_state(session: Any, cdp_session_id: str | None) -> dict[str, Any]:
    return await page_state(session, cdp_session_id, evaluate=eval_value)


async def _focus(session: Any, object_id: str, cdp_session_id: str | None) -> None:
    result = await focus_target(session, object_id, cdp_session_id, call=call_value)
    if not result.get("focused"):
        details: list[str] = []
        if result.get("tag"):
            details.append(f"tag={result['tag']}")
        if "editable" in result:
            details.append(f"editable={bool(result['editable'])}")
        if "hasLayout" in result and not result["hasLayout"]:
            details.append("no-layout")
        if result.get("inShadow"):
            details.append("in-shadow")
        if result.get("readOnly"):
            details.append("readonly")
        if result.get("disabled"):
            details.append("disabled")
        suffix = f" [{' '.join(details)}]" if details else ""
        raise BadRequest(f"无法聚焦目标元素{suffix}（可能是非编辑包装层、只读、被遮挡或跨 frame）；未执行清空/输入")
    tag = str(result.get("tag") or "").lower()
    type_ = str(result.get("type") or "").lower()
    editable = bool(result.get("editable"))
    text_input = tag == "input" and type_ in {"text", "search", "email", "url", "tel", "password", "number"}
    if not (editable or tag == "textarea" or text_input):
        raise BadRequest("目标不是可编辑文本字段（tag/type）；未执行清空/输入")
    if result.get("readOnly") or result.get("disabled"):
        raise BadRequest("目标字段为只读或 disabled；未执行清空/输入")


async def _clear(session: Any, object_id: str, cdp_session_id: str | None) -> None:
    await call_value(session, object_id, _CLEAR_FN, cdp_session_id=cdp_session_id)
    await _select_and_delete(session, cdp_session_id)
    state = await _read_state(session, object_id, cdp_session_id)
    if _normalize_text(state.get("text")):
        await _select_and_delete(session, cdp_session_id)
        state = await _read_state(session, object_id, cdp_session_id)
    if _normalize_text(state.get("text")):
        raise BadRequest("无法清空目标字段（清空后仍有内容）；未继续输入")


def _uncertain_fill(session: Any, entry: Any, reason: str, action_id: str, *, dispatched: bool = True) -> WriteOutcome:
    text = (
        f"已填入 {entry.token}\nstatus=uncertain retryable=false\nreason={reason}；不要重复输入\n"
        f"{evidence_line(session, action_id)}"
    )
    record_action(session, action_id, "browser_fill", "uncertain", text)
    return WriteOutcome(status="uncertain", dispatched=dispatched, blocks=[text_block(text)], action_id=action_id)


def _blocked_dialog_fill(session: Any, entry: Any, info: Any, action_id: str) -> WriteOutcome:
    result = "\n".join(
        [
            f"已填入 {entry.token}",
            info.fields(),
            "对话框出现：页面脚本暂停，输入是否生效未确认（不要重复输入）",
            "status=blocked retryable=false",
            "请先调用 browser_dialog（action=accept 或 dismiss），再 browser_snapshot 复核",
            evidence_line(session, action_id),
        ]
    )
    record_action(session, action_id, "browser_fill", "blocked", result)
    return WriteOutcome(
        status="blocked", dispatched=True, blocks=[text_block(result)], effect="dialog", action_id=action_id
    )


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    baseline = await prepare_expect_baseline(session, arguments)
    outcome = await _run_fill(session, arguments)
    return await run_post_write_expect(session, arguments, baseline=baseline, action_blocks=outcome)


async def _run_fill(session: Any, arguments: dict[str, Any]) -> WriteOutcome:
    target = arguments["target"]
    text = arguments["text"]
    clear = arguments["clear"]
    submit = arguments["submit"]
    keypress_fallback = arguments["keypress_fallback"]

    entry = await resolve_target(session, target)
    action_id = new_action_id()
    object_id = await session.resolve_object_id(entry)
    cdp_session_id = entry.cdp_session_id

    await _focus(session, object_id, cdp_session_id)
    if clear:
        try:
            await _clear(session, object_id, cdp_session_id)
        except ConnectionClosed:
            raise
        except (CDPTimeout, BrowserError) as exc:
            return _uncertain_fill(session, entry, f"clear_failed:{type(exc).__name__}", action_id, dispatched=False)

    client = session.require_client()
    prepare_action(session, action_id, "browser_fill", f"prepared fill of {entry.token}")
    dialog_seq = session.dialog_seq
    try:
        _, dialog = await send_watching_dialog(
            session,
            dialog_seq,
            lambda: client.send(
                "Input.insertText",
                {"text": text},
                session_id=cdp_session_id or session.session_id,
                timeout=session.cfg.cdp_timeout,
            ),
        )
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        return _uncertain_fill(session, entry, f"input_send_failed:{type(exc).__name__}", action_id)
    if dialog is not None:
        mark_dispatched(session, action_id, "browser_fill", f"fill of {entry.token} is in flight; a dialog opened")
        return _blocked_dialog_fill(session, entry, dialog, action_id)
    mark_dispatched(session, action_id, "browser_fill", f"dispatched fill of {entry.token}")
    await _sleep(0)
    dialog = await dialog_since(session, dialog_seq)
    if dialog is not None:
        return _blocked_dialog_fill(session, entry, dialog, action_id)
    try:
        state = await _read_state(session, object_id, cdp_session_id)
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        return _uncertain_fill(session, entry, f"readback_failed:{type(exc).__name__}", action_id)

    used_fallback = False
    mismatch = False
    try:
        if keypress_fallback:
            used_fallback = True
            if _normalize_text(state.get("text")):
                await _select_and_delete(session, cdp_session_id)
            await _type_characters(session, text, cdp_session_id)
            await _sleep(0)
            state = await _read_state(session, object_id, cdp_session_id)
        elif not _values_match(state.get("text"), text, clear=clear):
            if not _normalize_text(state.get("text")):
                used_fallback = True
                await _type_characters(session, text, cdp_session_id)
                await _sleep(0)
                state = await _read_state(session, object_id, cdp_session_id)
            else:
                mismatch = True
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        return _uncertain_fill(session, entry, f"input_fallback_failed:{type(exc).__name__}", action_id)

    if used_fallback and not _values_match(state.get("text"), text, clear=clear):
        mismatch = True

    submit_line = ""
    submitted = False
    if submit and mismatch:
        submit_line = "未提交：读回与输入不一致，先核对字段状态；不要按 Enter"
    elif submit:
        before_field = state
        try:
            before_page = await _page_state(session, cdp_session_id)
        except ConnectionClosed:
            raise
        except (CDPTimeout, BrowserError) as exc:
            submit_line = f"提交状态未知（页面回读失败：{type(exc).__name__}），不要按 Enter"
        else:
            try:
                _, dialog_seen = await send_watching_dialog(
                    session,
                    dialog_seq,
                    lambda: _press_key(session, "Enter", "Enter", 13, text="\r", cdp_session_id=cdp_session_id),
                )
            except ConnectionClosed:
                raise
            except (CDPTimeout, BrowserError) as exc:
                submit_line = f"提交可能已发出（Enter 发送失败：{type(exc).__name__}），不要重复按 Enter"
            else:
                replaced = False
                after_field = before_field
                after_page = before_page
                if dialog_seen is not None:
                    submit_line = (
                        f"提交结果未确认：{dialog_seen.fields()}；"
                        "请先调用 browser_dialog，再 browser_snapshot 复核，不要重复按 Enter"
                    )
                else:
                    for _ in range(_SUBMIT_BUDGET):
                        await _sleep(_SUBMIT_POLL)
                        dialog_seen = session.dialog_since(dialog_seq)
                        if dialog_seen is not None:
                            submit_line = (
                                f"提交结果未确认：{dialog_seen.fields()}；"
                                "请先调用 browser_dialog，再 browser_snapshot 复核，不要重复按 Enter"
                            )
                            break
                        try:
                            after_field = await _read_state(session, object_id, cdp_session_id)
                        except ConnectionClosed:
                            raise
                        except StaleRef:
                            replaced = True
                            break
                        except (CDPTimeout, BrowserError):
                            after_field = {}
                            break
                        try:
                            after_page = await _page_state(session, cdp_session_id)
                        except ConnectionClosed:
                            raise
                        except (CDPTimeout, BrowserError):
                            after_page = {}
                            break
                        if not _normalize_text(after_field.get("text")) or before_page.get("href") != after_page.get(
                            "href"
                        ):
                            break
                    if dialog_seen is None:
                        submit_line, submitted = _submit_verdict(
                            before_field, after_field, before_page, after_page, replaced=replaced
                        )

    session.mark_dirty()
    if not mismatch and (not submit or submitted):
        session.note_success(session.target_id)

    lines = [f"已填入 {entry.token}"]
    lines.append(evidence_line(session, action_id))
    if used_fallback:
        lines.append("insertText 回读未生效，已逐字符 dispatchKeyEvent 兜底")
    if mismatch:
        lines.append("读回与输入不一致，未重复输入；请 browser_snapshot 核对字段状态")
    if submit:
        lines.append(submit_line)
        if submitted:
            status = "succeeded"
        elif submit_line.startswith("提交被拒"):
            status = "failed"
        else:
            status = "uncertain"
        lines.append("status=" + status + ("" if status == "succeeded" else " retryable=false"))
    elif mismatch:
        status = "uncertain"
        lines.append("status=uncertain retryable=false")
    else:
        status = "succeeded"
        lines.append("status=succeeded")
    dialog = session.dialog_since(dialog_seq)
    if dialog is not None and status == "succeeded":
        status = "uncertain"
        lines[-1] = "status=uncertain retryable=false"
        lines.append(f"{dialog.fields()}；对话框出现后输入结果未确认，请先 browser_dialog 再复核")
    text = "\n".join(lines)
    record_action(session, action_id, "browser_fill", status, text)
    return WriteOutcome(status=status, dispatched=True, blocks=[text_block(text)], action_id=action_id)
