# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_click`` - trusted CDP click without a second JS click."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed, StaleRef
from ..journal import RESULT
from ..prompts import CLICK_HARD_RULE
from ..types import ToolBlock, text_block
from ._shared import (
    call_value,
    clear_pending_input_releases,
    eval_value,
    flush_pending_input_releases,
    note_pending_input_release,
    resolve_target,
    scroll_into_view_and_offset,
    send_watching_dialog,
)
from .actionability import (
    HIT_TEST_FN,
    WriteOutcome,
    amark_dispatched,
    aprepare_action,
    arecord_action,
    cdp_hit_test,
    classify_effect,
    evidence_line,
    new_action_id,
    page_state,
    target_changed,
    target_disabled,
    target_facts,
)
from .wait_for import EXPECT_PARAMS, prepare_expect_baseline, run_post_write_expect

NAME = "browser_click"

DESCRIPTION = (
    "Click an element identified by target (an @eN ref, or free text resolved by "
    "scoring). The tool scrolls the element into view, re-reads its geometry and "
    "sends a trusted CDP mouse event sequence (mouseMoved -> mousePressed -> "
    "mouseReleased); when the centre is covered it tries points near the corners "
    f"before giving up. {CLICK_HARD_RULE} dispatch=dom calls the element's own "
    "click() instead of mouse events: allowed only for a button/link/input whose "
    "mouse click on the same ref was already reported all-occluded or, on a "
    "non-submit target, effect=none, so it is never a first attempt and never a "
    "retry of a click that may have landed."
)

PARAMS: dict[str, Any] = {
    "target": {"type": "string", "required": True},
    "button": {"enum": ["left", "middle", "right"], "default": "left"},
    "click_count": {"type": "integer", "min": 1, "max": 3, "default": 1},
    "dispatch": {"enum": ["mouse", "dom"], "default": "mouse"},
    **EXPECT_PARAMS,
}

_BUTTON_MASK = {"left": 1, "middle": 4, "right": 2}
_DOM_CLICK_FN = (
    "function(){ if (typeof this.click !== 'function') throw new TypeError('target has no click()'); "
    "this.click(); return true; }"
)
_DOM_CLICK_TAGS = {"button", "a", "input"}
#: How far from the centre towards each corner the alternate hit points sit (0 = centre, 1 = corner).
_CORNER_INSET = 0.7
#: Marker of a mouse click that found no clear point; the only occlusion wording that unlocks dispatch=dom
#: (the corner-fallback note of a click that landed also mentions the covered centre).
_ALL_OCCLUDED = "目标中心与四角均被其他元素遮挡"
#: Marker of a mouse click whose hit tests did not all answer: nothing was sent, nothing is proven.
_HIT_TEST_FAILED = "命中测试失败"
#: Prefix of every dom refusal; such rows say nothing about the page and never move the gate.
_DOM_REFUSED = "dispatch=dom 未执行"
#: Label fragment of every dispatched dom click (prepared, dispatched or result); one per ref and document.
_DOM_USED = ", dispatch=dom"

_SUBMIT_WORDS = re.compile(
    r"评论|发布|发送|提交|回复|确认|保存|删除|购买|下单|支付|下一步|"
    r"关注|收藏|点赞|转发|报名|预约|同意|继续|结算|加入购物车|发表|更新|"
    r"post|send|comment|reply|submit|save|delete|remove|buy|pay|confirm|publish|share|"
    r"follow|like|subscribe|checkout|order|place\s+order|add|cart|bag|sign\s+in|register|vote|"
    r"accept|allow|agree|continue|next|apply",
    re.IGNORECASE,
)
_CLICK_POLL = 0.075
_CLICK_POLLS = 3
_CONTROL_SETTLE_SECONDS = 1.2
_CONTROL_POLL = 0.15
_CONTROL_MAX_POLLS = max(1, int(_CONTROL_SETTLE_SECONDS / _CONTROL_POLL))
_CONTROL_STATES = ("ariaPressed", "ariaChecked", "expanded")
_LABEL_COUNT = re.compile(r"\d+(?:[.,\u00a0\u202f]\d+)*(?:[kmb]\b)?", re.IGNORECASE)
_sleep = asyncio.sleep


def _target_disabled(facts: dict[str, Any]) -> bool:
    return target_disabled(facts)


def _is_submit_like(facts: dict[str, Any]) -> bool:
    if "submitLike" in facts:
        if bool(facts.get("submitLike")):
            return True
        return bool(_SUBMIT_WORDS.search(str(facts.get("name") or "")))
    tag = str(facts.get("tag") or "").lower()
    kind = str(facts.get("type") or "").lower()
    if tag == "input" and kind in {"submit", "image"}:
        return True
    if tag == "button" and (kind == "submit" or (facts.get("inForm") and not kind)):
        return True
    return bool(_SUBMIT_WORDS.search(str(facts.get("name") or "")))


def _target_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return target_changed(before, after)


def _classify_effect(before: dict[str, Any], after: dict[str, Any]) -> str:
    return classify_effect(before, after)


async def _target_facts(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    return await target_facts(session, object_id, cdp_session_id, call=call_value)


async def _page_state(session: Any, cdp_session_id: str | None) -> dict[str, Any]:
    return await page_state(session, cdp_session_id, evaluate=eval_value)


def _owned_page_ids(session: Any) -> set[str]:
    pages_fn = getattr(session, "pages", None)
    if not callable(pages_fn):
        return set()
    return {str(page.target_id) for page in pages_fn() if getattr(page, "target_id", None)}


def _new_tab_since(session: Any, before_ids: set[str]) -> str | None:
    pages_fn = getattr(session, "pages", None)
    if not callable(pages_fn):
        return None
    for page in pages_fn():
        tid = str(getattr(page, "target_id", "") or "")
        if tid and tid not in before_ids:
            return tid
    return None


async def _settled_effect(
    session: Any,
    cdp_session_id: str | None,
    before: dict[str, Any],
    dialog_seq: int,
    *,
    pages_before: set[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    try:
        after = await _page_state(session, cdp_session_id)
    except ConnectionClosed:
        raise
    except (StaleRef, BrowserError):
        return ("dialog" if session.dialog_since(dialog_seq) is not None else "unknown"), {}
    if session.dialog_since(dialog_seq) is not None:
        return "dialog", {}
    effect = _classify_effect(before, after)
    if pages_before is not None and effect != "navigation" and _new_tab_since(session, pages_before):
        return "new_tab", after
    for _ in range(_CLICK_POLLS):
        if effect not in ("none", "focus"):
            break
        await _sleep(_CLICK_POLL)
        try:
            after = await _page_state(session, cdp_session_id)
        except ConnectionClosed:
            raise
        except (StaleRef, BrowserError):
            return ("dialog" if session.dialog_since(dialog_seq) is not None else "unknown"), {}
        if session.dialog_since(dialog_seq) is not None:
            return "dialog", {}
        effect = _classify_effect(before, after)
        if pages_before is not None and effect != "navigation" and _new_tab_since(session, pages_before):
            return "new_tab", after
    return effect, after


def _has_control_state(facts: dict[str, Any]) -> bool:
    if any(facts.get(key) is not None for key in _CONTROL_STATES):
        return True
    tag = str(facts.get("tag") or "").lower()
    role = str(facts.get("role") or "").lower()
    kind = str(facts.get("type") or "").lower()
    if bool(facts.get("ariaLabel")) and (
        tag == "button" or role == "button" or (tag == "input" and kind in {"submit", "button"})
    ):
        return True
    return tag == "input" and kind in {"submit", "button"} and bool(facts.get("value"))


def _control_changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if not after:
        return False
    if any(after.get(key) in {"true", "false", "mixed"} and before.get(key) != after[key] for key in _CONTROL_STATES):
        return True
    if any(key in before and key in after and before[key] != after[key] for key in ("checked", "selected")):
        return True
    old_val = " ".join(_LABEL_COUNT.sub("", str(before.get("value") or "")).casefold().split())
    new_val = " ".join(_LABEL_COUNT.sub("", str(after.get("value") or "")).casefold().split())
    if old_val and new_val and old_val != new_val:
        return True
    old_label = " ".join(_LABEL_COUNT.sub("", str(before.get("ariaLabel") or "")).casefold().split())
    new_label = " ".join(_LABEL_COUNT.sub("", str(after.get("ariaLabel") or "")).casefold().split())
    return bool(old_label and new_label and old_label != new_label)


async def _wait_for_control_change(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    facts: dict[str, Any],
    before: dict[str, Any],
    dialog_seq: int,
) -> str:
    """Observe the same control after dispatch, without clicking or retargeting it."""
    for _ in range(_CONTROL_MAX_POLLS):
        if session.dialog_since(dialog_seq) is not None:
            return "dialog"
        page = await _page_state(session, cdp_session_id)
        if _classify_effect(before, page) == "navigation":
            return "navigation"
        after = await _target_facts(session, object_id, cdp_session_id)
        if after.get("connected") is False:
            return "target_detached"
        if _control_changed(facts, after):
            return "control_state"
        await _sleep(_CONTROL_POLL)
    return "dialog" if session.dialog_since(dialog_seq) is not None else ""


def _dialog_blocked(session: Any, entry: Any, label: str, info: Any, action_id: str) -> str:
    fields = info.fields() if info is not None else "dialog=unknown"
    return "\n".join(
        [
            f"已发送点击 {entry.token}（{label}）",
            fields,
            "对话框出现：页面脚本暂停，本次点击是否生效未确认（不要重复点击）",
            "status=blocked retryable=false",
            "请先调用 browser_dialog（action=accept 或 dismiss），再 browser_snapshot 复核",
            evidence_line(session, action_id),
        ]
    )


def _quad_points(quad: Any) -> list[tuple[float, float]]:
    """Normalise a CDP quad (8 numbers) or ``{x,y}`` list to point tuples."""
    if isinstance(quad, dict):
        return [(float(quad.get("x", 0.0)), float(quad.get("y", 0.0)))]
    if not isinstance(quad, (list, tuple)):
        return []
    if quad and isinstance(quad[0], dict):
        return [(float(p.get("x", 0.0)), float(p.get("y", 0.0))) for p in quad]
    numbers = [float(value) for value in quad]
    return [(numbers[i], numbers[i + 1]) for i in range(0, len(numbers) - 1, 2)]


def _shoelace(points: list[tuple[float, float]]) -> float:
    total = 0.0
    count = len(points)
    for i in range(count):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % count]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def _quad_center(quad: Any) -> tuple[float, float]:
    points = _quad_points(quad)
    if not points:
        raise BadRequest("无法确定元素命中坐标（空 quad）")
    x = sum(p[0] for p in points) / len(points)
    y = sum(p[1] for p in points) / len(points)
    return x, y


def _largest_quad(quads: list[Any]) -> Any | None:
    """Return the quad with the largest area (avoids the gap between inline rects)."""
    best: Any | None = None
    best_area = -1.0
    for quad in quads:
        points = _quad_points(quad)
        if len(points) < 3:
            continue
        area = _shoelace(points)
        if area > best_area:
            best_area = area
            best = quad
    return best


_NO_LAYOUT_RE = re.compile(
    r"does not have a layout object|could not compute (?:content quads|box model)", re.IGNORECASE
)


async def _visible_quad_with_offset(
    session: Any, object_id: str, cdp_session_id: str | None
) -> tuple[Any | None, tuple[float, float]]:
    """Scroll the node into view and return ``(quad, live_oopif_offset)``."""
    client = session.require_client()
    session_id = cdp_session_id or session.session_id
    try:
        offset = await scroll_into_view_and_offset(session, object_id, cdp_session_id)
        quads_response = await client.send(
            "DOM.getContentQuads",
            {"objectId": object_id},
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        )
        quad = _largest_quad(quads_response.get("quads") or [])
        if quad is None:
            model_response = await client.send(
                "DOM.getBoxModel",
                {"objectId": object_id},
                session_id=session_id,
                timeout=session.cfg.cdp_timeout,
            )
            model = model_response.get("model") or {}
            quad = model.get("content") or model.get("border") or model.get("padding")
        return quad, offset
    except BrowserError as exc:
        if _NO_LAYOUT_RE.search(str(exc)):
            raise BadRequest(
                "目标节点无可见布局盒模型（可能隐藏、未渲染或为无尺寸包装节点）；"
                "请用 browser_find 查看带 [in-view] 或 [editable] 标记的候选元素"
            ) from exc
        raise


async def _visible_quad(session: Any, object_id: str, cdp_session_id: str | None) -> Any | None:
    """Scroll the node into view and return its largest content quad (frame-local coordinates)."""
    quad, _offset = await _visible_quad_with_offset(session, object_id, cdp_session_id)
    return quad


async def _hit_point(
    session: Any,
    object_id: str,
    entry: Any,
    cdp_session_id: str | None,
    offset: tuple[float, float],
) -> tuple[float, float]:
    """Scroll the node into view then read its box; return a parent-viewport point.

    ``DOM.getContentQuads`` returns coordinates in the frame that owns the node;
    for an OOPIF that is its local viewport, so the post-scroll iframe offset is
    added. The snapshot bbox is already in parent coordinates and is used as-is.
    """
    quad, live_offset = await _visible_quad_with_offset(session, object_id, cdp_session_id)
    if quad is not None:
        active_offset = live_offset if cdp_session_id else offset
        x, y = _quad_center(quad)
        return x + active_offset[0], y + active_offset[1]
    bbox = getattr(entry, "bbox", None)
    if bbox and len(bbox) == 4:
        x, y, width, height = (float(v) for v in bbox)
        return x + width / 2.0, y + height / 2.0
    raise BadRequest("无法确定元素命中坐标（元素无可见盒模型）")


async def _corner_points(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
    offset: tuple[float, float],
) -> list[tuple[float, float]]:
    """Parent-viewport points ``_CORNER_INSET`` of the way from the centre to each quad corner.

    Empty when the node has no quad (the snapshot bbox gives no corners worth
    trying); a CDP failure propagates so the caller does not mistake it for
    "no corners".
    """
    quad, live_offset = await _visible_quad_with_offset(session, object_id, cdp_session_id)
    points = _quad_points(quad) if quad is not None else []
    if len(points) < 3:
        return []
    active_offset = live_offset if cdp_session_id else offset
    cx, cy = _quad_center(quad)
    return [
        (cx + (px - cx) * _CORNER_INSET + active_offset[0], cy + (py - cy) * _CORNER_INSET + active_offset[1])
        for px, py in points
    ]


def _confirmed(hit: dict[str, Any] | None) -> bool:
    """The hit test answered and did not land on another element (an unanswered test is no confirmation)."""
    return isinstance(hit, dict) and hit.get("inside") is not False


async def _safe_hit_test(
    session: Any,
    object_id: str,
    x: float,
    y: float,
    cdp_session_id: str | None,
    offset: tuple[float, float],
    *,
    entry: Any = None,
) -> dict[str, Any] | None:
    try:
        hit = await _hit_test(session, object_id, x, y, cdp_session_id, offset)
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError):
        return None
    if isinstance(hit, dict) and hit.get("shadowRetargeted") and entry is not None:
        local_x = x - offset[0] if cdp_session_id else x
        local_y = y - offset[1] if cdp_session_id else y
        resolved = await cdp_hit_test(session, entry, local_x, local_y, cdp_session_id, target_object_id=object_id)
        if resolved is not None:
            return resolved
    return hit


async def _locate_hit(
    session: Any,
    object_id: str,
    entry: Any,
    cdp_session_id: str | None,
    offset: tuple[float, float],
    *,
    hit_point: Any = None,
) -> tuple[float, float, str, dict[str, Any] | None]:
    """Find a point on the target that hit-tests to it: centre, then corners.

    When the covering element is one of the target's own scroll containers the
    scroll may not have settled when the quads were read, so the geometry is
    re-read once and every point tried again. Returns ``(x, y, where, hit)``
    where ``where`` is ``centre`` / ``corner`` / ``occluded`` (``hit`` is then
    the centre's hit for the occluded message; every point was answered and
    none reached the target) / ``unverified`` (some hit test did not answer,
    so no point is known to reach the target and occlusion is not established).
    """
    resolve_point = hit_point or _hit_point
    x, y = await resolve_point(session, object_id, entry, cdp_session_id, offset)
    if cdp_session_id:
        offset = session.oopif_offset(cdp_session_id)
    hit = await _safe_hit_test(session, object_id, x, y, cdp_session_id, offset, entry=entry)
    if hit is None:
        return x, y, "unverified", None
    if _confirmed(hit):
        return x, y, "centre", hit
    centre_hit = hit
    unanswered = False
    for attempt in range(2):
        if attempt:
            x, y = await resolve_point(session, object_id, entry, cdp_session_id, offset)
            if cdp_session_id:
                offset = session.oopif_offset(cdp_session_id)
            hit = await _safe_hit_test(session, object_id, x, y, cdp_session_id, offset, entry=entry)
            if _confirmed(hit):
                return x, y, "centre", hit
            unanswered = unanswered or hit is None
        try:
            corners = await _corner_points(session, object_id, cdp_session_id, offset)
            if cdp_session_id:
                offset = session.oopif_offset(cdp_session_id)
        except ConnectionClosed:
            raise
        except (CDPTimeout, BrowserError):
            unanswered = True
            corners = []
        for cx, cy in corners:
            candidate = await _safe_hit_test(session, object_id, cx, cy, cdp_session_id, offset, entry=entry)
            if _confirmed(candidate):
                return cx, cy, "corner", candidate
            unanswered = unanswered or candidate is None
        if not (isinstance(hit, dict) and hit.get("clipper")):
            break
    if unanswered:
        return x, y, "unverified", centre_hit
    return x, y, "occluded", centre_hit


def _dom_click_allowed(session: Any, entry: Any, facts: dict[str, Any], submit_like: bool) -> str | None:
    """Reason ``dispatch=dom`` is refused for this target, or None when it may proceed.

    Decisions read only structured journal fields (``target_ref``, ``dispatch``,
    ``reason``, ``effect``, ``phase``, ``status``) so rewording display prose never
    changes retry policy. Legacy rows lacking structured fields are treated
    conservatively (never open the gate).
    """
    tag = str(facts.get("tag") or "").lower()
    role = str(facts.get("role") or "").lower()
    if facts.get("clickable") is False:
        return f"目标是 {tag or 'unknown'}（role={role or '-'}），不支持原生 click()，无法执行 dispatch=dom"
    if tag not in _DOM_CLICK_TAGS and role != "button":
        return f"目标是 {tag or 'unknown'}（role={role or '-'}），dispatch=dom 只允许 button/a/input 或 role=button"
    rows = session.journal.rows_for("browser_click", session.page_epoch)
    target_rows = [row for row in rows if str(row.get("target_ref") or "") == entry.token]
    for row in target_rows:
        if str(row.get("dispatch") or "") == "dom":
            return f"dispatch=dom 对 {entry.token} 已用过一次，不要重复；请 snapshot 核对"
    newest = next((row for row in target_rows if str(row.get("reason") or "") != "dom_refused"), None)
    if newest is None:
        return f"本文档内尚无 {entry.token} 的 mouse 点击记录；请先用默认 dispatch=mouse 点击"
    status = str(newest.get("status") or "")
    phase = str(newest.get("phase") or "")
    reason = str(newest.get("reason") or "")
    dispatch_mode = str(newest.get("dispatch") or "")
    effect = str(newest.get("effect") or "")
    if phase != RESULT:
        return f"{entry.token} 上一次点击尚无结果记录（phase={phase}，可能已生效），不要再点击；请 snapshot 核对"
    if status == "blocked" and reason == "all_occluded":
        return None
    if status == "blocked" and dispatch_mode == "not_sent":
        detail = reason or "blocked"
        return f"{entry.token} 最近一次点击未发送（{detail}），dispatch=dom 不能绕过；请先按提示处理"
    if status == "uncertain" and reason == "hit_test_failed":
        return f"{entry.token} 上一次命中测试失败，未发送任何事件；请重新 snapshot 后用默认 dispatch=mouse 点击"
    if status == "uncertain" and dispatch_mode == "mouse" and effect == "none":
        if submit_like:
            return f"{entry.token} 是提交类目标，mouse 点击 effect=none 不能证明未提交，不放行 dispatch=dom；请 snapshot 核对"
        return None
    return f"{entry.token} 最近一次 mouse 点击结果为 {status}（可能已生效），不要再点击；请 snapshot 核对"


async def _dom_click(session: Any, object_id: str, cdp_session_id: str | None) -> None:
    await call_value(session, object_id, _DOM_CLICK_FN, cdp_session_id=cdp_session_id)


async def _hit_test(
    session: Any,
    object_id: str,
    x: float,
    y: float,
    cdp_session_id: str | None,
    offset: tuple[float, float],
) -> dict[str, Any] | None:
    client = session.require_client()
    local_x = x - offset[0] if cdp_session_id else x
    local_y = y - offset[1] if cdp_session_id else y
    response = await client.send(
        "Runtime.callFunctionOn",
        {
            "objectId": object_id,
            "functionDeclaration": HIT_TEST_FN,
            "arguments": [{"value": local_x}, {"value": local_y}],
            "returnByValue": True,
        },
        session_id=cdp_session_id or session.session_id,
        timeout=session.cfg.evaluate_timeout,
    )
    value = response.get("result", {}).get("value")
    return value if isinstance(value, dict) else None


async def _dispatch_click(session: Any, x: float, y: float, button: str, count: int) -> None:
    """Emit the mouseMoved / mousePressed / mouseReleased triple at (x, y)."""
    await flush_pending_input_releases(session)
    client = session.require_client()
    common = {"x": x, "y": y, "button": button, "clickCount": count, "pointerType": "mouse"}
    release_params = {**common, "clickCount": 0, "type": "mouseReleased", "buttons": 0}
    await client.send(
        "Input.dispatchMouseEvent",
        {**common, "type": "mouseMoved", "buttons": 0},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    note_pending_input_release(session, "Input.dispatchMouseEvent", release_params, session.session_id)
    await client.send(
        "Input.dispatchMouseEvent",
        {**common, "type": "mousePressed", "buttons": _BUTTON_MASK[button]},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    await client.send(
        "Input.dispatchMouseEvent",
        {**common, "type": "mouseReleased", "buttons": 0},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
    clear_pending_input_releases(session)


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    baseline = await prepare_expect_baseline(session, arguments)
    outcome = await _run_click(session, arguments)
    return await run_post_write_expect(session, arguments, baseline=baseline, outcome=outcome)


async def _run_click(session: Any, arguments: dict[str, Any]) -> WriteOutcome:
    target = arguments["target"]
    button = arguments["button"]
    count = arguments["click_count"]

    entry = await resolve_target(session, target)
    action_id = new_action_id()
    object_id = await session.resolve_object_id(entry)
    cdp_session_id = entry.cdp_session_id
    offset = session.oopif_offset(cdp_session_id)

    facts = await _target_facts(session, object_id, cdp_session_id)
    if _target_disabled(facts):
        text = (
            f"未点击 {entry.token}：目标处于 disabled 状态"
            "（原生/aria），不会产生提交；请先检查输入框内容、登录或权限，"
            "不要对同一 ref 重试\ndispatch=not_sent\nstatus=blocked retryable=false\n"
            f"{evidence_line(session, action_id)}"
        )
        await arecord_action(
            session,
            action_id,
            "browser_click",
            "blocked",
            text,
            target_ref=entry.token,
            dispatch="not_sent",
            reason="disabled",
        )
        return WriteOutcome(status="blocked", dispatched=False, blocks=[text_block(text)], action_id=action_id)

    submit_like = _is_submit_like(facts)
    dispatch = arguments.get("dispatch", "mouse")
    if dispatch == "dom":
        refusal = _dom_click_allowed(session, entry, facts, submit_like)
        if refusal is not None:
            text = (
                f"未点击 {entry.token}：{_DOM_REFUSED}：{refusal}\n"
                f"dispatch=not_sent\nstatus=blocked retryable=false\n{evidence_line(session, action_id)}"
            )
            await arecord_action(
                session,
                action_id,
                "browser_click",
                "blocked",
                text,
                target_ref=entry.token,
                dispatch="not_sent",
                reason="dom_refused",
            )
            return WriteOutcome(status="blocked", dispatched=False, blocks=[text_block(text)], action_id=action_id)
        x = y = 0.0
        where = "dom"
    else:
        x, y, where, hit = await _locate_hit(session, object_id, entry, cdp_session_id, offset)
        if where == "unverified":
            text = (
                f"未点击 {entry.token}：{_HIT_TEST_FAILED}，无法确认落点，未发送任何事件\n"
                "dispatch=not_sent\nstatus=uncertain retryable=false\n请重新 snapshot 后再试\n"
                f"{evidence_line(session, action_id)}"
            )
            await arecord_action(
                session,
                action_id,
                "browser_click",
                "uncertain",
                text,
                target_ref=entry.token,
                dispatch="not_sent",
                reason="hit_test_failed",
            )
            return WriteOutcome(status="uncertain", dispatched=False, blocks=[text_block(text)], action_id=action_id)
        if where == "occluded":
            text = (
                f"未点击 {entry.token}：{_ALL_OCCLUDED}"
                f"（命中 {(hit or {}).get('tag') or 'unknown'}），未发送任何事件\n"
                "dispatch=not_sent\nstatus=blocked retryable=false\n"
                "请先关闭遮挡层或重新 snapshot；若目标是 button/a/input 或 role=button，"
                "可对同一 ref 用 dispatch=dom 重试一次\n"
                f"{evidence_line(session, action_id)}"
            )
            await arecord_action(
                session,
                action_id,
                "browser_click",
                "blocked",
                text,
                target_ref=entry.token,
                dispatch="not_sent",
                reason="all_occluded",
            )
            return WriteOutcome(status="blocked", dispatched=False, blocks=[text_block(text)], action_id=action_id)
    baseline_ok = True
    try:
        before = await _page_state(session, cdp_session_id)
    except ConnectionClosed:
        raise
    except (StaleRef, BrowserError):
        before = {}
        baseline_ok = False
    label = f"button={button}, click_count={count}" + (", dispatch=dom" if dispatch == "dom" else "")
    await aprepare_action(
        session,
        action_id,
        "browser_click",
        f"prepared click on {entry.token} ({label})",
        target_ref=entry.token,
        dispatch=dispatch,
    )
    dialog_seq = session.dialog_seq
    pages_before = _owned_page_ids(session)
    if dispatch == "dom":

        async def send_click() -> None:
            await _dom_click(session, object_id, cdp_session_id)

    else:

        async def send_click() -> None:
            await _dispatch_click(session, x, y, button, count)

    session.mark_dirty()
    try:
        _, dialog = await send_watching_dialog(session, dialog_seq, send_click)
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        text = (
            f"已发送点击 {entry.token}（{label}）\n"
            "dispatch=unknown\neffect=unknown\nstatus=uncertain retryable=false\n"
            f"reason=dispatch_failed:{type(exc).__name__}；不要重复点击\n"
            f"{evidence_line(session, action_id)}"
        )
        await arecord_action(
            session,
            action_id,
            "browser_click",
            "uncertain",
            text,
            target_ref=entry.token,
            dispatch=dispatch,
            reason="dispatch_failed",
            effect="unknown",
        )
        return WriteOutcome(
            status="uncertain", dispatched=True, blocks=[text_block(text)], effect="unknown", action_id=action_id
        )
    if dialog is not None:
        await amark_dispatched(
            session,
            action_id,
            "browser_click",
            f"click on {entry.token} is in flight; a dialog opened before it settled",
            target_ref=entry.token,
            dispatch=dispatch,
        )
        text = _dialog_blocked(session, entry, label, dialog, action_id)
        await arecord_action(
            session,
            action_id,
            "browser_click",
            "blocked",
            text,
            target_ref=entry.token,
            dispatch=dispatch,
            reason="dialog",
            effect="dialog",
        )
        return WriteOutcome(
            status="blocked", dispatched=True, blocks=[text_block(text)], effect="dialog", action_id=action_id
        )
    await amark_dispatched(
        session,
        action_id,
        "browser_click",
        f"dispatched click on {entry.token} ({label})",
        target_ref=entry.token,
        dispatch=dispatch,
    )
    effect, _ = await _settled_effect(session, cdp_session_id, before, dialog_seq, pages_before=pages_before)
    if effect == "dialog":
        text = _dialog_blocked(session, entry, label, session.dialog_since(dialog_seq), action_id)
        await arecord_action(
            session,
            action_id,
            "browser_click",
            "blocked",
            text,
            target_ref=entry.token,
            dispatch=dispatch,
            reason="dialog",
            effect="dialog",
        )
        return WriteOutcome(
            status="blocked", dispatched=True, blocks=[text_block(text)], effect="dialog", action_id=action_id
        )
    new_tab_id = _new_tab_since(session, pages_before)
    if new_tab_id is not None and effect != "navigation":
        effect = "new_tab"
    verified_control = False
    if not baseline_ok and effect != "new_tab":
        effect = "unknown"
    elif effect not in ("navigation", "new_tab", "unknown") and _has_control_state(facts):
        orig_effect = effect
        if effect == "dom":
            effect = "dom_elsewhere"
        try:
            observed = await asyncio.wait_for(
                _wait_for_control_change(session, object_id, cdp_session_id, facts, before, dialog_seq),
                timeout=_CONTROL_SETTLE_SECONDS,
            )
        except ConnectionClosed:
            raise
        except (asyncio.TimeoutError, BrowserError):
            pass
        else:
            if observed == "control_state":
                effect, verified_control = "dom", True
            elif observed == "target_detached":
                effect = "target_detached"
            elif observed == "navigation":
                effect = "navigation"
            elif observed == "dialog":
                dialog = session.dialog_since(dialog_seq)
                if dialog is not None:
                    text = _dialog_blocked(session, entry, label, dialog, action_id)
                    await arecord_action(
                        session,
                        action_id,
                        "browser_click",
                        "blocked",
                        text,
                        target_ref=entry.token,
                        dispatch=dispatch,
                        reason="dialog",
                        effect="dialog",
                    )
                    return WriteOutcome(
                        status="blocked",
                        dispatched=True,
                        blocks=[text_block(text)],
                        effect="dialog",
                        action_id=action_id,
                    )
        if not verified_control and effect not in ("navigation", "target_detached"):
            after_facts = await _target_facts(session, object_id, cdp_session_id)
            if after_facts.get("connected") is False:
                effect = "target_detached"
            elif facts.get("inShadow") and _target_changed(facts, after_facts):
                effect = "dom"
            elif (
                orig_effect == "dom"
                and str(facts.get("tag") or "").lower() == "input"
                and (after_facts.get("disabled") != facts.get("disabled") or _target_changed(facts, after_facts))
            ):
                effect = "dom"
    elif effect == "dom":
        after_facts = await _target_facts(session, object_id, cdp_session_id)
        if after_facts.get("connected") is False:
            effect = "target_detached"
        elif not facts or not after_facts:
            effect = "unknown"
        elif not _target_changed(facts, after_facts):
            effect = "dom_elsewhere"
    elif effect in ("none", "focus") and facts.get("inShadow"):
        # Light-DOM MutationObserver does not cross shadow boundaries; re-read
        # shadow-local target/root facts so shadow-internal changes are observed.
        after_facts = await _target_facts(session, object_id, cdp_session_id)
        if after_facts.get("connected") is False:
            effect = "target_detached"
        elif _target_changed(facts, after_facts):
            effect = "dom"

    dialog = session.dialog_since(dialog_seq)
    if dialog is not None:
        text = _dialog_blocked(session, entry, label, dialog, action_id)
        await arecord_action(
            session,
            action_id,
            "browser_click",
            "blocked",
            text,
            target_ref=entry.token,
            dispatch=dispatch,
            reason="dialog",
            effect="dialog",
        )
        return WriteOutcome(
            status="blocked", dispatched=True, blocks=[text_block(text)], effect="dialog", action_id=action_id
        )

    if effect in ("navigation", "new_tab", "dom", "target_detached"):
        session.note_success(session.target_id)

    status = "succeeded" if effect in ("navigation", "new_tab", "dom", "target_detached") else "uncertain"
    lines = [
        f"已点击 {entry.token}（{label}）",
        "dispatch=sent",
        f"effect={effect}",
        f"status={status} retryable=false",
        evidence_line(session, action_id),
    ]
    if effect == "new_tab" and new_tab_id is not None:
        lines.append(f"tab_id={new_tab_id}")
        toolset = str(getattr(getattr(session, "cfg", None), "toolset", "full") or "full").strip().lower()
        if toolset == "compact":
            new_url = ""
            pages_fn = getattr(session, "pages", None)
            if callable(pages_fn):
                for page in pages_fn():
                    if str(getattr(page, "target_id", "") or "") == new_tab_id:
                        new_url = str(getattr(page, "url", "") or "")
                        break
            url_hint = f"，url={new_url}" if new_url else ""
            lines.append(
                f"已在新标签页打开（tab_id={new_tab_id}{url_hint}）；"
                "当前 compact 工具集不包含标签切换工具，请勿调用 browser_tab_switch"
                "（如需查看新页面可直接 browser_navigate 到目标 URL，或改用 full 工具集）"
            )
        else:
            lines.append(f"已在新标签页打开（tab_id={new_tab_id}）；可用 browser_tab_switch 切换查看")
    if verified_control:
        lines.append("verified=control_state")
    if where == "corner":
        lines.append("hit_point=corner（中心被遮挡，已改用靠近角落的点）")
    if effect == "target_detached":
        lines.append(
            "目标已从页面移除（常见于弹窗关闭或控件切换）；请用 browser_find(fresh=true) 核对业务结果，不要重复点击"
        )
    elif effect == "focus":
        lines.append("仅观察到焦点变化；如果目标是提交按钮，提交未确认，请 snapshot 复核")
    elif effect == "dom_elsewhere":
        lines.append("页面发生变化，目标本身未确认；请用 browser_find(fresh=true) 或 snapshot 核对，不要重复点击")
    elif effect == "unknown":
        lines.append("结果不可判定（探针失败/连接异常）；不要重复点击，请 snapshot 复核")
    elif effect == "none":
        if submit_like or dispatch == "dom":
            lines.append("页面未变化（submit-like 目标未自动兜底，避免重复提交）；请检查 disabled/权限或先 snapshot")
        else:
            lines.append(
                "页面未变化；请 browser_snapshot 复核。若目标是 button/a/input 或 role=button 且确认未生效，"
                "可对同一 ref 用 dispatch=dom 重试一次"
            )
    text = "\n".join(lines)
    await arecord_action(
        session,
        action_id,
        "browser_click",
        status,
        text,
        target_ref=entry.token,
        dispatch=dispatch,
        effect=effect,
    )
    return WriteOutcome(status=status, dispatched=True, blocks=[text_block(text)], effect=effect, action_id=action_id)
