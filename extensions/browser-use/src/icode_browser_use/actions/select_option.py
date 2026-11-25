# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
#
"""``browser_select_option`` - choose options on a native ``<select>``."""

from __future__ import annotations

import asyncio
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed
from ..types import ToolBlock, text_block
from ._shared import call_value, dialog_since, resolve_target, send_watching_dialog, truncate_chars
from .actionability import (
    evidence_line,
    mark_dispatched,
    new_action_id,
    prepare_action,
    record_action,
    target_disabled,
    target_facts,
)

NAME = "browser_select_option"

DESCRIPTION = (
    "Select one or more options on a native <select> element (single or "
    "multiple). values is the complete wanted set - options not listed are "
    "deselected - and each value is matched against the option value first and "
    "its visible label second; the selection is read back and reported. Custom "
    "dropdown widgets are not supported - drive them with browser_click and "
    "browser_snapshot instead."
)

PARAMS: dict[str, Any] = {
    "target": {"type": "string", "required": True},
    "values": {
        "type": "array",
        "items": {"type": "string", "maxLength": 256},
        "minItems": 1,
        "maxItems": 20,
        "required": True,
    },
}

_SELECT_STATE_FN = (
    "function(){"
    " const tag=(this.tagName||'').toLowerCase();"
    " if(tag!=='select') return {ok:false, reason:'not_select', tag};"
    " const elDisabled=(el)=>el.disabled===true || (typeof el.matches==='function' && el.matches(':disabled'));"
    " const optDisabled=(o)=>{"
    "   if(o.disabled===true) return true;"
    "   let p=o.parentElement;"
    "   while(p && p!==this){"
    "     if((p.tagName||'').toLowerCase()==='optgroup' && p.disabled===true) return true;"
    "     p=p.parentElement;"
    "   }"
    "   return false;"
    " };"
    " const options=[...this.options].map((o, index)=>({"
    "   index, value:String(o.value), label:String(o.label||o.text||'').trim(),"
    "   disabled:optDisabled(o), selected:o.selected===true}));"
    " return {ok:true, tag, multiple:this.multiple===true, disabled:elDisabled(this),"
    "   options};"
    "}"
)

_APPLY_FN = (
    "function(values, indices){"
    " const optDisabled=(o)=>{"
    "   if(o.disabled===true) return true;"
    "   let p=o.parentElement;"
    "   while(p && p!==this){"
    "     if((p.tagName||'').toLowerCase()==='optgroup' && p.disabled===true) return true;"
    "     p=p.parentElement;"
    "   }"
    "   return false;"
    " };"
    " const useIndices=Array.isArray(indices);"
    " const wantedIdx=useIndices ? new Set(indices) : null;"
    " const wanted=new Set(values);"
    " if(!this.multiple){"
    "   let target=-1;"
    "   for(let i=0; i<this.options.length; i++){"
    "     const option=this.options[i];"
    "     const matched=useIndices ? wantedIdx.has(i) : wanted.has(String(option.value));"
    "     if(matched && !optDisabled(option)){ target=i; break; }"
    "   }"
    "   if(target>=0) this.selectedIndex=target;"
    " } else {"
    "   for(let i=0; i<this.options.length; i++){"
    "     const option=this.options[i];"
    "     const matched=useIndices ? wantedIdx.has(i) : wanted.has(String(option.value));"
    "     option.selected=matched && !optDisabled(option);"
    "   }"
    " }"
    " this.dispatchEvent(new Event('input',{bubbles:true}));"
    " this.dispatchEvent(new Event('change',{bubbles:true}));"
    " return {"
    "   selected:[...this.options].filter(o=>o.selected).map(o=>String(o.value)),"
    "   selectedIndices:[...this.options].map((o, i)=>o.selected ? i : -1).filter(i=>i>=0),"
    " };"
    "}"
)

_sleep = asyncio.sleep
_READBACK_POLL = 0.075
_READBACK_POLLS = 2


def _selected_options(state: dict[str, Any]) -> list[dict[str, Any]]:
    options = [option for option in state.get("options", []) if isinstance(option, dict)]
    return [option for option in options if option.get("selected")]


def _selected(state: dict[str, Any]) -> list[str]:
    return [str(option.get("value")) for option in _selected_options(state)]


def _readback_matches(state: dict[str, Any], resolved_values: list[str], target_indices: list[int]) -> bool:
    if not state.get("ok"):
        return False
    selected_opts = _selected_options(state)
    if not selected_opts:
        return False
    if any(option.get("disabled") for option in selected_opts):
        return False
    if all(isinstance(option.get("index"), int) for option in selected_opts):
        actual_pairs = {(int(option["index"]), str(option.get("value"))) for option in selected_opts}
        expected_pairs = set(zip(target_indices, resolved_values, strict=True))
        return actual_pairs == expected_pairs
    return {str(option.get("value")) for option in selected_opts} == set(resolved_values)


async def _select_state(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    response = await call_value(session, object_id, _SELECT_STATE_FN, cdp_session_id=cdp_session_id)
    value = response.get("value") if isinstance(response, dict) else None
    return value if isinstance(value, dict) else {}


def _resolve_option_targets(
    state: dict[str, Any], values: list[str]
) -> tuple[list[int], list[str], list[str], list[str]]:
    """Map requested values/labels to concrete enabled option indices and values.

    Returns ``(indices, resolved_values, missing, unavailable)``: *missing*
    never matched an option, *unavailable* matched only effectively disabled
    options (including options inside a disabled ``<optgroup>``). A value shared
    by one disabled and one enabled option resolves strictly to the enabled
    option's index so duplicate-value siblings are never modified together.
    """
    options = [option for option in state.get("options", []) if isinstance(option, dict)]
    indexed = [
        (int(option["index"]) if isinstance(option.get("index"), int) else pos, option)
        for pos, option in enumerate(options)
    ]
    indices: list[int] = []
    resolved: list[str] = []
    missing: list[str] = []
    unavailable: list[str] = []
    for requested in values:
        matches = [(idx, option) for idx, option in indexed if option.get("value") == requested]
        if not matches:
            matches = [(idx, option) for idx, option in indexed if option.get("label") == requested]
        if not matches:
            missing.append(requested)
            continue
        enabled = next(((idx, option) for idx, option in matches if not option.get("disabled")), None)
        if enabled is None:
            unavailable.append(requested)
        else:
            idx, option = enabled
            if idx not in indices:
                indices.append(idx)
                resolved.append(str(option.get("value")))
    return indices, resolved, missing, unavailable


def _resolve_options(state: dict[str, Any], values: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Map requested values/labels to option values (compat wrapper)."""
    _indices, resolved, missing, unavailable = _resolve_option_targets(state, values)
    return resolved, missing, unavailable


def _listed(items: list[str]) -> str:
    shown = ", ".join(truncate_chars(str(item), 64) for item in items[:20])
    return shown + ("" if len(items) <= 20 else f" …(+{len(items) - 20} more)")


def _available(state: dict[str, Any]) -> str:
    options = [option for option in state.get("options", []) if isinstance(option, dict)]
    labels = [
        f"{truncate_chars(str(option.get('value')), 48)}={truncate_chars(str(option.get('label') or ''), 64)}"
        + (" [disabled]" if option.get("disabled") else "")
        for option in options[:12]
    ]
    more = "" if len(options) <= 12 else f" …(+{len(options) - 12} more)"
    return ", ".join(labels) + more


def _blocked(session: Any, action_id: str, text: str) -> list[ToolBlock]:
    record_action(session, action_id, NAME, "blocked", text)
    return [text_block(text)]


def _blocked_dialog(session: Any, action_id: str, head: str, info: Any) -> list[ToolBlock]:
    text = "\n".join(
        [
            head,
            info.fields(),
            "对话框出现：页面脚本暂停，选择是否生效未确认（不要重复选择）",
            "status=blocked retryable=false",
            "请先调用 browser_dialog（action=accept 或 dismiss），再 browser_snapshot 复核",
            evidence_line(session, action_id),
        ]
    )
    return _blocked(session, action_id, text)


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    target = arguments["target"]
    values = list(arguments["values"])
    if not values:
        raise BadRequest("values 至少要包含一项")
    entry = await resolve_target(session, target)
    action_id = new_action_id()
    object_id = await session.resolve_object_id(entry)
    cdp_session_id = entry.cdp_session_id

    facts = await target_facts(session, object_id, cdp_session_id, call=call_value)
    if not facts:
        raise BadRequest("无法读取目标状态（可能连接异常或元素已失效）；请 browser_snapshot 复核")
    state = await _select_state(session, object_id, cdp_session_id)
    if not state:
        raise BadRequest("无法读取 <select> 状态（可能连接异常或元素已失效）；请 browser_snapshot 复核")
    if not state.get("ok"):
        text = (
            f"未选择 {entry.token}：目标不是原生 <select>（tag={state.get('tag') or facts.get('tag')}）；"
            "自定义下拉请用 browser_click + browser_snapshot\n"
            f"status=blocked retryable=false\n{evidence_line(session, action_id)}"
        )
        return _blocked(session, action_id, text)
    if state.get("disabled") or target_disabled(facts):
        text = (
            f"未选择 {entry.token}：目标处于 disabled 状态；status=blocked retryable=false\n"
            f"{evidence_line(session, action_id)}"
        )
        return _blocked(session, action_id, text)

    target_indices, resolved, missing, unavailable = _resolve_option_targets(state, values)
    if missing or unavailable:
        reasons = []
        if missing:
            reasons.append(f"选项不存在 {_listed(missing)}")
        if unavailable:
            reasons.append(f"选项被禁用 {_listed(unavailable)}")
        text = (
            f"未选择 {entry.token}：{'；'.join(reasons)}；可用选项：{_available(state)}\n"
            f"status=blocked retryable=false\n{evidence_line(session, action_id)}"
        )
        return _blocked(session, action_id, text)
    if not state.get("multiple") and len(target_indices) > 1:
        text = (
            f"未选择 {entry.token}：这是单选 <select>，一次只能选一个值（请求了 {values}）；"
            "请重新调用并只给一个值\n"
            f"status=blocked retryable=false\n{evidence_line(session, action_id)}"
        )
        return _blocked(session, action_id, text)

    prepare_action(session, action_id, NAME, f"prepared select {resolved} on {entry.token}")
    dialog_seq = session.dialog_seq
    try:
        response, dialog = await send_watching_dialog(
            session,
            dialog_seq,
            lambda: call_value(
                session,
                object_id,
                _APPLY_FN,
                cdp_session_id=cdp_session_id,
                arguments=[{"value": resolved}, {"value": target_indices}],
            ),
        )
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        text = (
            f"已发送选择 {resolved} 到 {entry.token}\nstatus=uncertain retryable=false\n"
            f"reason=dispatch_failed:{type(exc).__name__}；不要重复选择\n"
            f"{evidence_line(session, action_id)}"
        )
        record_action(session, action_id, NAME, "uncertain", text)
        return [text_block(text)]
    if dialog is not None:
        mark_dispatched(session, action_id, NAME, f"select {resolved} on {entry.token} is in flight; a dialog opened")
        return _blocked_dialog(session, action_id, f"已发送选择 {resolved} 到 {entry.token}", dialog)
    mark_dispatched(session, action_id, NAME, f"dispatched select {resolved} on {entry.token}")

    dialog = await dialog_since(session, dialog_seq)
    after: dict[str, Any] = {}
    selected: list[str] = []
    matched = False
    if dialog is None:
        for attempt in range(_READBACK_POLLS + 1):
            if attempt:
                await _sleep(_READBACK_POLL)
            try:
                after = await _select_state(session, object_id, cdp_session_id)
            except ConnectionClosed:
                raise
            except (CDPTimeout, BrowserError):
                after = {}
                break
            dialog = session.dialog_since(dialog_seq)
            if dialog is not None:
                break
            selected = _selected(after) if after.get("ok") else []
            matched = _readback_matches(after, resolved, target_indices)
            if matched:
                break
    dialog = session.dialog_since(dialog_seq)
    if dialog is not None:
        return _blocked_dialog(session, action_id, f"已发送选择 {resolved} 到 {entry.token}", dialog)
    applied = response.get("value") if isinstance(response, dict) else None
    reported = [str(value) for value in applied.get("selected", [])] if isinstance(applied, dict) else []
    session.mark_dirty()

    lines = [f"已在 {entry.token} 选择 {resolved}"]
    if matched:
        status = "succeeded"
        lines.append(f"selected={selected}")
        lines.append("status=succeeded")
    elif not after:
        # The readback channel failed: the apply echo is not proof, so stay honest.
        status = "uncertain"
        lines.append(f"apply_echo={reported or '无'}")
        lines.append("status=uncertain retryable=false")
        lines.append("页面未回读（连接或探测失败）；请 browser_snapshot 核对，不要重复选择")
    else:
        status = "uncertain"
        lines.append(f"selected={selected or '读回为空'}")
        lines.append("status=uncertain retryable=true")
        lines.append("读回与请求不一致；选择是幂等的，可在 snapshot 核对后重试同一 values")
    if status == "succeeded":
        session.note_success(session.target_id)
    lines.append(evidence_line(session, action_id))
    text = "\n".join(lines)
    record_action(session, action_id, NAME, status, text)
    return [text_block(text)]
