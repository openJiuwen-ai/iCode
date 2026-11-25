# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_fill`` - type into a field via real input events with a fallback."""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed
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
    poll_submit_outcome,
    prepare_action,
    record_action,
    select_all_target,
    submit_verdict,
    target_focused,
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


def _normalize_native_value(value: Any) -> str:
    """Preserve exact spaces on native ``<input>``/``<textarea>`` while normalizing CRLF to LF."""
    return str(value or "").replace("\r\n", "\n")


def _splice_utf16(text: str, start: int, end: int, insert: str) -> str | None:
    """Splice *insert* into *text* using DOM UTF-16 code-unit selection offsets."""
    raw = text.encode("utf-16-le")
    total_units = len(raw) // 2
    if not (0 <= start <= end <= total_units):
        return None
    prefix = raw[: start * 2].decode("utf-16-le", errors="surrogatepass")
    suffix = raw[end * 2 :].decode("utf-16-le", errors="surrogatepass")
    return prefix + insert + suffix


def _replacement_candidates(before: str, selected: str, replacement: str) -> set[str]:
    """All exact strings reachable by replacing one occurrence (including overlapping) of *selected* in *before*."""
    if not selected:
        return set()
    candidates: set[str] = set()
    start = 0
    step = len(selected)
    while True:
        idx = before.find(selected, start)
        if idx == -1:
            break
        candidates.add(before[:idx] + replacement + before[idx + step :])
        start = idx + 1
    return candidates


def _values_match(
    value: Any,
    expected: str,
    *,
    clear: bool = True,
    before_value: str | None = None,
    selection: tuple[int, int] | None = None,
    selection_text: str = "",
    selection_prefix: str | None = None,
    selection_suffix: str | None = None,
    raw_value: str | None = None,
    native_value: bool = False,
) -> bool:
    if native_value:
        actual_raw = _normalize_native_value(value)
        wanted_raw = _normalize_native_value(expected)
        if not wanted_raw:
            return actual_raw == "" if clear else True
        if clear:
            return actual_raw == wanted_raw
        if before_value is not None:
            before_raw = _normalize_native_value(before_value)
            if selection is not None:
                combined = _splice_utf16(before_raw, selection[0], selection[1], wanted_raw)
                if combined is not None:
                    return actual_raw == combined
            if selection_text:
                sel_raw = _normalize_native_value(selection_text)
                candidates = _replacement_candidates(before_raw, sel_raw, wanted_raw)
                if candidates:
                    return actual_raw in candidates
                return False
            if actual_raw == before_raw:
                return False
            return wanted_raw in actual_raw and len(actual_raw) >= len(before_raw) + len(wanted_raw)
        return wanted_raw in actual_raw

    actual = _normalize_text(value)
    wanted = _normalize_text(expected)
    if not wanted:
        return actual == "" if clear else True
    if not actual:
        return False
    if clear:
        return actual == wanted
    if before_value is not None:
        if selection is not None:
            combined = _splice_utf16(before_value, selection[0], selection[1], expected)
            if combined is not None:
                return actual == _normalize_text(combined)
        before_norm = _normalize_text(before_value)
        if selection_prefix is not None and selection_suffix is not None:
            if raw_value is not None:
                # Range.toString() and textContent share the exact same DOM text representation
                # (including multi-line <div>/<p> blocks), so compare in that representation directly.
                return _normalize_text(raw_value) == _normalize_text(selection_prefix + expected + selection_suffix)
            reconstructed_before = _normalize_text(selection_prefix + selection_text + selection_suffix)
            if reconstructed_before == before_norm:
                return actual == _normalize_text(selection_prefix + expected + selection_suffix)
            return False
        if selection_text:
            sel_norm = _normalize_text(selection_text)
            candidates = {_normalize_text(c) for c in _replacement_candidates(before_norm, sel_norm, wanted)}
            return actual in candidates
        if actual == before_norm:
            return False
        return wanted in actual and len(actual) >= len(before_norm) + len(wanted)
    return wanted in actual


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


class _FocusLost(Exception):
    """Internal signal: the target element lost focus before a keyboard step."""


async def _ensure_focused(session: Any, object_id: str, cdp_session_id: str | None) -> None:
    focused = await target_focused(session, object_id, cdp_session_id, call=call_value)
    if focused is False:
        raise _FocusLost("target lost focus")


async def _select_and_delete(session: Any, object_id: str, cdp_session_id: str | None) -> None:
    """Verify focus, select all inside the target editor and verify the selection, then Backspace."""
    result = await select_all_target(session, object_id, cdp_session_id, call=call_value)
    if result.get("focused") is False:
        raise _FocusLost("target lost focus before select-all")
    if result.get("empty") is True:
        return
    if result.get("selected") is False:
        raise BadRequest("无法在目标编辑器内选中全部内容；未执行删除/输入")
    await _press_key(session, "Backspace", "Backspace", 8, cdp_session_id=cdp_session_id)


async def _type_characters(session: Any, object_id: str, text: str, cdp_session_id: str | None) -> None:
    """Type raw key events; re-verify focus before each character so focus shifts never write to another field."""
    for char in text:
        await _ensure_focused(session, object_id, cdp_session_id)
        await _dispatch_key(session, "keyDown", cdp_session_id=cdp_session_id, key=char)
        await _dispatch_key(session, "char", cdp_session_id=cdp_session_id, key=char, text=char, unmodifiedText=char)
        await _dispatch_key(session, "keyUp", cdp_session_id=cdp_session_id, key=char)


async def _read_state(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    return await field_state(session, object_id, cdp_session_id, call=call_value)


async def _page_state(session: Any, cdp_session_id: str | None) -> dict[str, Any]:
    return await page_state(session, cdp_session_id, evaluate=eval_value)


async def _focus(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
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
    return result


async def _clear(session: Any, object_id: str, cdp_session_id: str | None) -> None:
    await call_value(session, object_id, _CLEAR_FN, cdp_session_id=cdp_session_id)
    state = await _read_state(session, object_id, cdp_session_id)
    if state.get("focused") is False:
        raise _FocusLost("target lost focus during clear")
    if _normalize_text(state.get("text")):
        await _select_and_delete(session, object_id, cdp_session_id)
        state = await _read_state(session, object_id, cdp_session_id)
        if state.get("focused") is False:
            raise _FocusLost("target lost focus after clear")
    if _normalize_text(state.get("text")):
        await _select_and_delete(session, object_id, cdp_session_id)
        state = await _read_state(session, object_id, cdp_session_id)
        if state.get("focused") is False:
            raise _FocusLost("target lost focus after clear")
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
    return await run_post_write_expect(session, arguments, baseline=baseline, outcome=outcome)


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

    focus_info = await _focus(session, object_id, cdp_session_id)
    native_value = str(focus_info.get("tag") or "").lower() in {"input", "textarea"}
    before_text: str | None = None
    before_selection: tuple[int, int] | None = None
    before_selection_text = ""
    before_selection_prefix: str | None = None
    before_selection_suffix: str | None = None
    if not clear:
        raw_before = focus_info.get("text")
        if isinstance(raw_before, str):
            before_text = raw_before
        sel_start = focus_info.get("selectionStart")
        sel_end = focus_info.get("selectionEnd")
        if isinstance(sel_start, int) and isinstance(sel_end, int):
            before_selection = (sel_start, sel_end)
        raw_sel_text = focus_info.get("selectionText")
        if isinstance(raw_sel_text, str):
            before_selection_text = raw_sel_text
        raw_prefix = focus_info.get("selectionPrefix")
        raw_suffix = focus_info.get("selectionSuffix")
        if isinstance(raw_prefix, str) and isinstance(raw_suffix, str):
            before_selection_prefix = raw_prefix
            before_selection_suffix = raw_suffix
    if clear:
        try:
            await _clear(session, object_id, cdp_session_id)
        except ConnectionClosed:
            raise
        except _FocusLost as exc:
            raise BadRequest("目标在清空过程中失去焦点；已停止输入，未向其他字段写入") from exc
        except BadRequest:
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
    matched = _values_match(
        state.get("text"),
        text,
        clear=clear,
        before_value=before_text,
        selection=before_selection,
        selection_text=before_selection_text,
        selection_prefix=before_selection_prefix,
        selection_suffix=before_selection_suffix,
        raw_value=state.get("rawText") if isinstance(state.get("rawText"), str) else None,
        native_value=native_value,
    )
    try:
        if not matched:
            unchanged_append = (
                not clear
                and before_text is not None
                and (
                    _normalize_native_value(state.get("text")) == _normalize_native_value(before_text)
                    if native_value
                    else _normalize_text(state.get("text")) == _normalize_text(before_text)
                )
            )
            if keypress_fallback:
                if state.get("focused") is False:
                    raise _FocusLost("target lost focus before keypress fallback")
                if clear:
                    used_fallback = True
                    if _normalize_text(state.get("text")):
                        await _select_and_delete(session, object_id, cdp_session_id)
                    await _type_characters(session, object_id, text, cdp_session_id)
                    await _sleep(0)
                    state = await _read_state(session, object_id, cdp_session_id)
                elif unchanged_append or before_text is None:
                    # Never select-and-delete when clear=False: preserve existing text.
                    used_fallback = True
                    await _type_characters(session, object_id, text, cdp_session_id)
                    await _sleep(0)
                    state = await _read_state(session, object_id, cdp_session_id)
                else:
                    mismatch = True
            elif not _normalize_text(state.get("text")) or unchanged_append:
                if state.get("focused") is False:
                    raise _FocusLost("target lost focus before fallback typing")
                used_fallback = True
                await _type_characters(session, object_id, text, cdp_session_id)
                await _sleep(0)
                state = await _read_state(session, object_id, cdp_session_id)
            else:
                mismatch = True
    except ConnectionClosed:
        raise
    except _FocusLost:
        return _uncertain_fill(session, entry, "focus_lost", action_id)
    except (CDPTimeout, BrowserError) as exc:
        return _uncertain_fill(session, entry, f"input_fallback_failed:{type(exc).__name__}", action_id)

    if used_fallback and not _values_match(
        state.get("text"),
        text,
        clear=clear,
        before_value=before_text,
        selection=before_selection,
        selection_text=before_selection_text,
        selection_prefix=before_selection_prefix,
        selection_suffix=before_selection_suffix,
        raw_value=state.get("rawText") if isinstance(state.get("rawText"), str) else None,
        native_value=native_value,
    ):
        mismatch = True

    submit_line = ""
    submitted = False
    if submit and mismatch:
        submit_line = "未提交：读回与输入不一致，先核对字段状态；不要按 Enter"
    elif submit and state.get("focused") is False:
        submit_line = "未提交：输入后目标已失去焦点，未按 Enter；请 browser_snapshot 核对"
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
                if dialog_seen is not None:
                    submit_line = (
                        f"提交结果未确认：{dialog_seen.fields()}；"
                        "请先调用 browser_dialog，再 browser_snapshot 复核，不要重复按 Enter"
                    )
                else:
                    after_field, after_page, replaced, dialog_seen = await poll_submit_outcome(
                        session,
                        object_id,
                        cdp_session_id,
                        before_field,
                        before_page,
                        dialog_seq,
                        budget=_SUBMIT_BUDGET,
                        poll_interval=_SUBMIT_POLL,
                        sleep=_sleep,
                        read_field=lambda: _read_state(session, object_id, cdp_session_id),
                        read_page=lambda: _page_state(session, cdp_session_id),
                    )
                    if dialog_seen is not None:
                        submit_line = (
                            f"提交结果未确认：{dialog_seen.fields()}；"
                            "请先调用 browser_dialog，再 browser_snapshot 复核，不要重复按 Enter"
                        )
                    else:
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
