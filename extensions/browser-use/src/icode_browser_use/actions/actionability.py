# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared actionability facts used by click, fill and other actions.

The action modules still own their verification policy; this module only reads
page/target facts and classifies the observable effect, so they do not each keep
a private copy of the same DOM probes.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from ..errors import BrowserError, ConnectionClosed, StaleRef
from ..journal import DISPATCHED, PREPARED, RESULT
from ..types import ToolBlock
from ._shared import call_value, eval_value

JsonCall = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class WriteOutcome:
    """Structured dispatch and verification outcome of a write action.

    Post-write flows (such as ``expect_*``) branch on these fields rather than
    scanning rendered prose lines, so wording adjustments never alter control flow.
    """

    status: str
    dispatched: bool
    blocks: list[ToolBlock]
    effect: str = ""
    action_id: str = ""

    @property
    def can_run_expect(self) -> bool:
        return self.dispatched and self.status not in {"blocked", "failed"}


PAGE_STATE_EXPR = (
    "(()=>({"
    " href:location.href,"
    " mutations:window.__chrys_mutations||0,"
    " expanded:document.querySelectorAll('[aria-expanded=true]').length,"
    " dialogs:document.querySelectorAll('dialog[open],[role=dialog]').length,"
    " active:(document.activeElement&&(document.activeElement.tagName||'').toLowerCase())||'',"
    " toasts:[...document.querySelectorAll('[role=alert],[role=status],[aria-live]')]"
    "   .map(e=>(e.innerText||'').trim().slice(0,200)).filter(Boolean).slice(-3)"
    "}))()"
)

_DETACHED_RE = re.compile(r"could not find (?:node|object)|no node with given id|node .*detached", re.IGNORECASE)

TARGET_FACTS_FN = (
    "function(){"
    " if(!this.isConnected) return {connected:false};"
    " const tag=(this.tagName||'').toLowerCase();"
    " const cls=String(this.className||'');"
    " const name=(this.getAttribute('aria-label')||this.getAttribute('title')"
    "   ||this.innerText||this.value||'').trim().slice(0,80);"
    " const root=(this.getRootNode && this.getRootNode())||document;"
    " const inShadow=root!==document;"
    " const container=this.closest('form,dialog,[role=dialog],article')||(inShadow?root:this);"
    " const ctxText=String((container&&container.innerText)||(container&&container.textContent)||'');"
    " const contextLength=ctxText.length;"
    " let rootTextHash=0;"
    " if(inShadow){"
    "   const rt=String(root.textContent||'');"
    "   let h=2166136261;"
    "   for(let i=0;i<rt.length;i++){ h^=rt.charCodeAt(i); h=Math.imul(h,16777619); }"
    "   rootTextHash=h>>>0;"
    " }"
    " const rootChildCount=inShadow&&root.querySelectorAll?root.querySelectorAll('*').length:0;"
    " return {connected:true, tag, role:this.getAttribute('role')||'', name,"
    "   type:this.type||this.getAttribute('type')||'',"
    "   inForm:!!this.form,"
    "   inShadow, rootChildCount, rootTextHash,"
    "   readOnly:this.readOnly===true,"
    "   checked:this.checked===true,"
    "   selected:this.selected===true,"
    "   value:this.value===undefined?'':String(this.value),"
    "   expanded:this.getAttribute('aria-expanded'),"
    "   ariaPressed:this.getAttribute('aria-pressed'),"
    "   ariaChecked:this.getAttribute('aria-checked'),"
    "   ariaLabel:(this.getAttribute('aria-label')||'').trim().slice(0,80),"
    "   contextLength,"
    "   disabled:this.disabled===true || (typeof this.matches==='function' && this.matches(':disabled')),"
    "   ariaDisabled:this.getAttribute('aria-disabled')==='true',"
    "   classDisabled:/\\bdisabled\\b/.test(cls)}; }"
)

HIT_TEST_FN = (
    "function(x,y){"
    " const isClipper=(el,target)=>!!el && !!(el.contains && el.contains(target))"
    "   && (el.scrollHeight>el.clientHeight || el.scrollWidth>el.clientWidth);"
    " const slotMatches=(el,target)=>{"
    "   let s=el && el.assignedSlot;"
    "   while(s){ if(s===target||(target.contains&&target.contains(s))) return true; s=s.assignedSlot; }"
    "   return false;"
    " };"
    " const hosts=[];"
    " let curRoot=this.getRootNode?this.getRootNode():document;"
    " while(curRoot && curRoot!==document && curRoot.host){"
    "   hosts.push({root:curRoot, host:curRoot.host});"
    "   curRoot=curRoot.host.getRootNode?curRoot.host.getRootNode():document;"
    " }"
    " const top=document.elementFromPoint(x,y);"
    " if(!hosts.length){"
    "   const inside=!!top && (top===this || (this.contains && this.contains(top)) || slotMatches(top,this));"
    "   const clipper=!!top && !inside && isClipper(top,this);"
    "   return {inside, clipper, shadowRetargeted:false, tag:(top&&top.tagName)||'', id:(top&&top.id)||''};"
    " }"
    " const outerHost=hosts[hosts.length-1].host;"
    " if(!top){ return {inside:false, clipper:false, shadowRetargeted:false, tag:'', id:''}; }"
    " const outerOk=(top===outerHost || (outerHost.contains && outerHost.contains(top)));"
    " if(!outerOk){"
    "   return {inside:false, clipper:isClipper(top,outerHost), shadowRetargeted:false,"
    "     tag:(top&&top.tagName)||'', id:(top&&top.id)||''};"
    " }"
    " for(let i=hosts.length-1; i>=0; i--){"
    "   const {root, host}=hosts[i];"
    "   const expected=i>0 ? hosts[i-1].host : this;"
    "   const inner=typeof root.elementFromPoint==='function' ? root.elementFromPoint(x,y) : null;"
    "   if(!inner){"
    "     return {inside:false, clipper:false, shadowRetargeted:true, tag:(host&&host.tagName)||'', id:(host&&host.id)||''};"
    "   }"
    "   if(inner===expected || (expected.contains && expected.contains(inner)) || slotMatches(inner,expected)){"
    "     if(i===0){"
    "       return {inside:true, clipper:false, shadowRetargeted:false, tag:(inner&&inner.tagName)||'', id:(inner&&inner.id)||''};"
    "     }"
    "     continue;"
    "   }"
    "   if(inner===host){"
    "     return {inside:false, clipper:false, shadowRetargeted:true, tag:(host&&host.tagName)||'', id:(host&&host.id)||''};"
    "   }"
    "   return {inside:false, clipper:isClipper(inner,expected), shadowRetargeted:false,"
    "     tag:(inner&&inner.tagName)||'', id:(inner&&inner.id)||''};"
    " }"
    " return {inside:false, clipper:false, shadowRetargeted:true, tag:(outerHost&&outerHost.tagName)||'', id:(outerHost&&outerHost.id)||''};"
    "}"
)

FOCUS_FN = (
    "function(){"
    " this.focus();"
    " const root=(this.getRootNode && this.getRootNode())||document;"
    " if (this.isContentEditable) {"
    "  const range=document.createRange(); range.selectNodeContents(this); range.collapse(false);"
    "  const selection=(root && typeof root.getSelection==='function' ? root.getSelection() : null) || window.getSelection();"
    "  if (selection) { selection.removeAllRanges(); selection.addRange(range); }"
    " }"
    " let active=(root && root.activeElement) || document.activeElement;"
    " while (active && active.shadowRoot && active.shadowRoot.activeElement) { active=active.shadowRoot.activeElement; }"
    " const rect=this.getBoundingClientRect ? this.getBoundingClientRect() : {width:0,height:0};"
    " return {tag:(this.tagName||'').toLowerCase(),"
    "   type:(this.type||'').toLowerCase(),"
    "   editable:this.isContentEditable===true,"
    "   readOnly:this.readOnly===true,"
    "   disabled:this.disabled===true || (typeof this.matches==='function' && this.matches(':disabled')),"
    "   inShadow:root!==document,"
    "   hasLayout:rect.width>0 && rect.height>0,"
    "   tabIndex:typeof this.tabIndex==='number'?this.tabIndex:-1,"
    "   focused:active===this || (!!active && !!(this.contains && this.contains(active)))};"
    "}"
)

FIELD_STATE_FN = (
    "function(){"
    " const tag=(this.tagName||'').toLowerCase();"
    " let text=null;"
    " if (tag==='input'||tag==='textarea') text=this.value;"
    " else if (this.isContentEditable) text=this.innerText;"
    " const root=(this.getRootNode && this.getRootNode())||document;"
    " let active=(root && root.activeElement) || document.activeElement;"
    " while (active && active.shadowRoot && active.shadowRoot.activeElement) { active=active.shadowRoot.activeElement; }"
    " return {tag, editable:this.isContentEditable===true,"
    "   focused:active===this || (!!active && !!(this.contains && this.contains(active))),"
    "   text:text===null?'':String(text)};"
    "}"
)

FOCUS_CHECK_FN = (
    "function(){"
    " if(!this.isConnected) return {focused:false};"
    " const root=(this.getRootNode && this.getRootNode())||document;"
    " let active=(root && root.activeElement) || document.activeElement;"
    " while (active && active.shadowRoot && active.shadowRoot.activeElement) { active=active.shadowRoot.activeElement; }"
    " return {focused:active===this || (!!active && !!(this.contains && this.contains(active)))};"
    "}"
)

SELECT_ALL_FN = (
    "function(){"
    " if(!this.isConnected) return {focused:false, selected:false, empty:false};"
    " const root=(this.getRootNode && this.getRootNode())||document;"
    " let active=(root && root.activeElement) || document.activeElement;"
    " while (active && active.shadowRoot && active.shadowRoot.activeElement) { active=active.shadowRoot.activeElement; }"
    " const focused=active===this || (!!active && !!(this.contains && this.contains(active)));"
    " if (!focused) return {focused:false, selected:false, empty:false};"
    " const tag=(this.tagName||'').toLowerCase();"
    " const norm=s=>String(s||'').replace(/\\s+/g,' ').trim();"
    " if (tag==='input' || tag==='textarea') {"
    "  const val=String(this.value||'');"
    "  if (!val) return {focused:true, selected:true, empty:true};"
    "  if (typeof this.select==='function') this.select();"
    "  let selected=true;"
    "  try {"
    "   if (typeof this.selectionStart==='number' && typeof this.selectionEnd==='number') {"
    "    selected=(this.selectionStart===0 && this.selectionEnd===val.length);"
    "   }"
    "  } catch (_) {}"
    "  return {focused:true, selected, empty:false};"
    " }"
    " if (this.isContentEditable) {"
    "  const expected=norm(this.innerText);"
    "  if (!expected) return {focused:true, selected:true, empty:true};"
    "  const range=document.createRange();"
    "  range.selectNodeContents(this);"
    "  const sel=(root && typeof root.getSelection==='function' ? root.getSelection() : null) || window.getSelection();"
    "  if (!sel) return {focused:true, selected:false, empty:false};"
    "  sel.removeAllRanges();"
    "  sel.addRange(range);"
    "  if (sel.rangeCount < 1) return {focused:true, selected:false, empty:false};"
    "  const r=sel.getRangeAt(0);"
    "  const inTarget=n=>!!n && (n===this || !!(this.contains && this.contains(n)));"
    "  const selText=norm(sel.toString()) || norm(r.toString());"
    "  const selected=!r.collapsed && inTarget(r.startContainer) && inTarget(r.endContainer) && selText===expected;"
    "  return {focused:true, selected, empty:false};"
    " }"
    " return {focused:true, selected:false, empty:false};"
    "}"
)


async def target_facts(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    *,
    call: JsonCall = call_value,
) -> dict[str, Any]:
    try:
        response = await call(session, object_id, TARGET_FACTS_FN, cdp_session_id=cdp_session_id)
    except ConnectionClosed:
        raise
    except StaleRef:
        return {"connected": False}
    except BrowserError as exc:
        if _DETACHED_RE.search(str(exc)):
            return {"connected": False}
        return {}
    value = response.get("value") if isinstance(response, dict) else None
    return value if isinstance(value, dict) else {}


async def page_state(
    session: Any,
    cdp_session_id: str | None,
    *,
    evaluate: JsonCall = eval_value,
) -> dict[str, Any]:
    value = await evaluate(session, PAGE_STATE_EXPR, cdp_session_id=cdp_session_id)
    return value if isinstance(value, dict) else {}


async def focus_target(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    *,
    call: JsonCall = call_value,
) -> dict[str, Any]:
    response = await call(session, object_id, FOCUS_FN, cdp_session_id=cdp_session_id)
    value = response.get("value") if isinstance(response, dict) else None
    return value if isinstance(value, dict) else {}


async def field_state(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    *,
    call: JsonCall = call_value,
) -> dict[str, Any]:
    response = await call(session, object_id, FIELD_STATE_FN, cdp_session_id=cdp_session_id)
    value = response.get("value") if isinstance(response, dict) else None
    return value if isinstance(value, dict) else {}


async def target_focused(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    *,
    call: JsonCall = call_value,
) -> bool | None:
    """Return whether *object_id* still holds focus, or ``None`` when the probe omits it."""
    response = await call(session, object_id, FOCUS_CHECK_FN, cdp_session_id=cdp_session_id)
    value = response.get("value") if isinstance(response, dict) else None
    if isinstance(value, dict) and "focused" in value:
        return bool(value["focused"])
    return None


async def select_all_target(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    *,
    call: JsonCall = call_value,
) -> dict[str, Any]:
    """Select all contents inside *object_id* after verifying focus; return ``{focused, selected, empty}``."""
    response = await call(session, object_id, SELECT_ALL_FN, cdp_session_id=cdp_session_id)
    value = response.get("value") if isinstance(response, dict) else None
    return value if isinstance(value, dict) else {}


async def hit_test(
    session: Any,
    object_id: str,
    x: float,
    y: float,
    cdp_session_id: str | None,
    *,
    entry: Any = None,
    call: JsonCall = call_value,
) -> dict[str, Any] | None:
    response = await call(
        session,
        object_id,
        HIT_TEST_FN,
        cdp_session_id=cdp_session_id,
        arguments=[{"value": x}, {"value": y}],
    )
    value = response.get("value") if isinstance(response, dict) else None
    if not isinstance(value, dict):
        return None
    if value.get("shadowRetargeted") and entry is not None:
        resolved = await cdp_hit_test(session, entry, x, y, cdp_session_id, target_object_id=object_id)
        if resolved is not None:
            return resolved
    return value


async def cdp_hit_test(
    session: Any,
    entry: Any,
    local_x: float,
    local_y: float,
    cdp_session_id: str | None,
    *,
    target_object_id: str | None = None,
) -> dict[str, Any] | None:
    """Resolve a shadow-retargeted hit point through Blink's native hit test."""
    target_backend = getattr(entry, "backend_node_id", None)
    if not target_backend:
        return None
    client = session.require_client()
    eff_session = cdp_session_id or session.session_id
    try:
        response = await client.send(
            "DOM.getNodeForLocation",
            {
                "x": int(round(local_x)),
                "y": int(round(local_y)),
                "includeUserAgentShadowDOM": False,
                "ignorePointerEventsNone": False,
            },
            session_id=eff_session,
            timeout=session.cfg.evaluate_timeout,
        )
    except ConnectionClosed:
        raise
    except BrowserError:
        return None
    hit_backend = int((response or {}).get("backendNodeId") or 0)
    if not hit_backend:
        return None
    if hit_backend == target_backend:
        return {"inside": True, "clipper": False, "shadowRetargeted": False, "tag": "", "id": ""}
    frame_id = getattr(entry, "frame_id", None)
    nodes = getattr(getattr(session, "snapshot", None), "nodes", None) or ()
    hit_node = next(
        (
            node
            for node in nodes
            if getattr(node, "backend_node_id", None) == hit_backend
            and (frame_id is None or getattr(node, "frame_id", None) == frame_id)
        ),
        None,
    )
    if hit_node is not None and target_backend in (getattr(hit_node, "ancestor_ids", ()) or ()):
        return {
            "inside": True,
            "clipper": False,
            "shadowRetargeted": False,
            "tag": str(getattr(hit_node, "tag", "") or "").upper(),
            "id": "",
        }
    if target_object_id:
        # Handle newly inserted children, #text nodes, or slotted light-DOM children
        # (whose snapshot ancestor_ids point to the shadow host rather than the inner shadow target)
        # by resolving the hit node and walking its composed parent/slot/host chain.
        try:
            resolved = await client.send(
                "DOM.resolveNode",
                {"backendNodeId": hit_backend},
                session_id=eff_session,
                timeout=session.cfg.evaluate_timeout,
            )
            hit_obj = ((resolved or {}).get("object") or {}).get("objectId")
            if hit_obj:
                check = await client.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": hit_obj,
                        "functionDeclaration": (
                            "function(target){"
                            " let cur=this;"
                            " for(let i=0;i<64 && cur;i++){"
                            "  if(cur===target) return true;"
                            "  cur=cur.assignedSlot || cur.parentNode || (cur.host||null);"
                            " }"
                            " return false;"
                            "}"
                        ),
                        "arguments": [{"objectId": target_object_id}],
                        "returnByValue": True,
                    },
                    session_id=eff_session,
                    timeout=session.cfg.evaluate_timeout,
                )
                if ((check or {}).get("result") or {}).get("value") is True:
                    return {"inside": True, "clipper": False, "shadowRetargeted": False, "tag": "", "id": ""}
        except ConnectionClosed:
            raise
        except BrowserError:
            pass
    return {
        "inside": False,
        "clipper": False,
        "shadowRetargeted": False,
        "tag": str(getattr(hit_node, "tag", "") or "").upper() if hit_node is not None else "",
        "id": "",
    }


def target_disabled(facts: dict[str, Any]) -> bool:
    return bool(facts.get("disabled") or facts.get("ariaDisabled"))


def new_action_id() -> str:
    return secrets.token_hex(6)


def evidence_line(session: Any, action_id: str) -> str:
    return f"action_id={action_id} document_epoch={getattr(session, 'page_epoch', 0)}"


def _write(
    session: Any,
    action_id: str,
    tool: str,
    phase: str,
    status: str,
    evidence: str,
    *,
    target_ref: str = "",
    dispatch: str = "",
    reason: str = "",
    effect: str = "",
) -> None:
    kwargs: dict[str, Any] = {
        "action_id": action_id,
        "tool": tool,
        "phase": phase,
        "status": status,
        "evidence": evidence,
        "document_epoch": int(getattr(session, "page_epoch", 0)),
    }
    if target_ref or dispatch or reason or effect:
        kwargs.update(target_ref=target_ref, dispatch=dispatch, reason=reason, effect=effect)
    session.journal.record(**kwargs)


def prepare_action(
    session: Any,
    action_id: str,
    tool: str,
    evidence: str,
    *,
    target_ref: str = "",
    dispatch: str = "",
    reason: str = "",
    effect: str = "",
) -> None:
    """Record ``prepared`` before anything is sent, so a crash is distinguishable."""
    _write(
        session,
        action_id,
        tool,
        PREPARED,
        "prepared",
        evidence,
        target_ref=target_ref,
        dispatch=dispatch,
        reason=reason,
        effect=effect,
    )


def mark_dispatched(
    session: Any,
    action_id: str,
    tool: str,
    evidence: str,
    *,
    target_ref: str = "",
    dispatch: str = "",
    reason: str = "",
    effect: str = "",
) -> None:
    """Record ``dispatched`` once the send boundary returned: the action may have taken effect."""
    _write(
        session,
        action_id,
        tool,
        DISPATCHED,
        "dispatched",
        evidence,
        target_ref=target_ref,
        dispatch=dispatch,
        reason=reason,
        effect=effect,
    )


def record_action(
    session: Any,
    action_id: str,
    tool: str,
    status: str,
    evidence: str,
    *,
    target_ref: str = "",
    dispatch: str = "",
    reason: str = "",
    effect: str = "",
) -> None:
    """Record the verified outcome (``succeeded`` / ``blocked`` / ``uncertain``)."""
    _write(
        session,
        action_id,
        tool,
        RESULT,
        status,
        evidence,
        target_ref=target_ref,
        dispatch=dispatch,
        reason=reason,
        effect=effect,
    )


def lookup_action(session: Any, action_id: str) -> dict[str, Any] | None:
    return session.journal.lookup(action_id)


def target_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not before or not after:
        return False
    return any(
        before.get(key) != after.get(key)
        for key in (
            "contextLength",
            "rootChildCount",
            "rootTextHash",
            "checked",
            "selected",
            "expanded",
            "value",
            "disabled",
        )
    )


def classify_effect(before: dict[str, Any], after: dict[str, Any]) -> str:
    if not before or not after:
        return "none"
    if before.get("href") != after.get("href"):
        return "navigation"
    if before.get("mutations") != after.get("mutations"):
        return "dom"
    if before.get("expanded") != after.get("expanded") or before.get("dialogs") != after.get("dialogs"):
        return "dom"
    if before.get("active") != after.get("active"):
        return "focus"
    return "none"


REJECT_WORDS = (
    "失败",
    "错误",
    "频繁",
    "请先登录",
    "未登录",
    "需要登录",
    "权限",
    "禁止",
    "不能",
    "超出限制",
    "failed",
    "error",
    "denied",
    "rate limit",
    "limit exceeded",
)

_BENIGN_TOAST_RE = re.compile(
    r"(?:\b(?:0|zero|no)\s+errors?\b|登录成功|login\s+succeeded|signed\s+in\s+successfully)",
    re.IGNORECASE,
)

_REJECT_TOAST_RE = re.compile(
    r"(?:失败|错误|频繁|请先登录|未登录|需要登录|登录已过期|无权限|没有权限|权限不足|禁止|不能|超出限制|"
    r"\bfailed\b|\berror\b|\bdenied\b|\bforbidden\b|\bunauthorized\b|"
    r"\brate\s+limit\b|\blimit\s+exceeded\b|\btoo\s+many\s+requests\b|\blogin\s+required\b|\bsign\s+in\s+required\b)",
    re.IGNORECASE,
)


def _is_rejection_toast(toast: str) -> bool:
    cleaned = _BENIGN_TOAST_RE.sub("", toast)
    return bool(_REJECT_TOAST_RE.search(cleaned))


def normalize_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def is_text_field(tag: Any, type_: Any, editable: Any) -> bool:
    """Whether a focused element takes typed text (textarea, contenteditable or a text-like input)."""
    tag_name = str(tag or "").lower()
    kind = str(type_ or "").lower()
    return (
        bool(editable)
        or tag_name == "textarea"
        or (tag_name == "input" and kind in {"text", "search", "email", "url", "tel", "password", "number", ""})
    )


def submit_verdict(
    before_field: dict[str, Any],
    after_field: dict[str, Any],
    before_page: dict[str, Any],
    after_page: dict[str, Any],
    *,
    replaced: bool = False,
) -> tuple[str, bool]:
    """Judge whether an Enter in a text field submitted, from field and page state read around it.

    Only an emptied field or a navigation count as submitted; a page update alone is reported
    as unconfirmed, and a new toast carrying a rejection word is reported as rejected.
    Page navigation is evaluated independently of field readback because a full-page form
    submission destroys the old input's execution context.
    """
    href_before = before_page.get("href")
    href_after = after_page.get("href")
    if isinstance(href_before, str) and isinstance(href_after, str) and href_before != href_after:
        return "已提交（页面已跳转）", True
    if "text" not in after_field or "text" not in before_field:
        return "提交状态未知（字段回读失败），请 browser_snapshot 复核", False
    before_text = normalize_text(before_field.get("text"))
    after_text = normalize_text(after_field.get("text"))
    new_toasts = [
        toast
        for toast in after_page.get("toasts", [])
        if toast not in before_page.get("toasts", []) and normalize_text(toast) != before_text
    ]
    rejection = next((toast for toast in new_toasts if _is_rejection_toast(toast)), None)
    if replaced:
        return "提交可能成功（编辑器已被替换），请 browser_snapshot 复核", False
    if before_text and not after_text:
        if rejection:
            return (
                f"提交结果未确认（输入框已清空，但页面出现拒绝提示：{rejection}）；请 browser_snapshot 复核，不要重复提交",
                False,
            )
        return "已提交（输入框已清空）", True
    if rejection:
        return f"提交被拒：{rejection}", False
    if after_page and before_page.get("mutations") != after_page.get("mutations"):
        return "提交可能已发出（页面有更新），请 browser_snapshot 复核", False
    if not after_page:
        return (
            "提交未确认：页面状态未知（回读失败），输入框仍有内容；请用 browser_snapshot 核对，不要重复按 Enter",
            False,
        )
    return (
        "提交未确认：输入框仍有内容且页面无变化；请用 browser_snapshot 找到提交按钮，不要重复按 Enter",
        False,
    )


async def poll_submit_outcome(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    before_field: dict[str, Any],
    before_page: dict[str, Any],
    dialog_seq: int,
    *,
    budget: int,
    poll_interval: float,
    sleep: Callable[[float], Awaitable[Any]],
    read_field: Callable[[], Awaitable[dict[str, Any]]],
    read_page: Callable[[], Awaitable[dict[str, Any]]],
) -> tuple[dict[str, Any], dict[str, Any], bool, Any]:
    """Poll field and page state independently after a submit Enter.

    Returns ``(after_field, after_page, replaced, dialog_seen)``. A destroyed or
    stale field never prevents reading ``after_page`` so cross-document form
    navigations are still observed as independent proof of submission.
    """
    from ..errors import CDPTimeout

    replaced = False
    after_field = before_field
    after_page = before_page
    before_href = before_page.get("href")
    for _ in range(budget):
        await sleep(poll_interval)
        dialog_seen = session.dialog_since(dialog_seq)
        if dialog_seen is not None:
            return after_field, after_page, replaced, dialog_seen
        field_done = False
        if not replaced:
            try:
                after_field = await read_field()
            except ConnectionClosed:
                raise
            except StaleRef:
                replaced = True
                field_done = True
            except (CDPTimeout, BrowserError):
                after_field = {}
                field_done = True
        try:
            after_page = await read_page()
        except ConnectionClosed:
            raise
        except (CDPTimeout, BrowserError):
            snap_url = str(getattr(getattr(session, "snapshot", None), "url", "") or "")
            if isinstance(before_href, str) and snap_url and snap_url != before_href:
                after_page = {"href": snap_url}
                break
            after_page = {}
            if field_done or not normalize_text(after_field.get("text")):
                break
            continue
        if isinstance(before_href, str) and isinstance(after_page.get("href"), str) and before_href != after_page["href"]:
            break
        if not replaced and "text" in after_field and not normalize_text(after_field.get("text")):
            break
        if field_done:
            break
    return after_field, after_page, replaced, session.dialog_since(dialog_seq)
