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
from ._shared import call_value, eval_value, resolve_target, send_watching_dialog
from .actionability import (
    HIT_TEST_FN,
    cdp_hit_test,
    classify_effect,
    evidence_line,
    mark_dispatched,
    new_action_id,
    page_state,
    prepare_action,
    record_action,
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
_DOM_CLICK_FN = "function(){ this.click(); return true; }"
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


async def _settled_effect(
    session: Any, cdp_session_id: str | None, before: dict[str, Any], dialog_seq: int
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
    for _ in range(_CLICK_POLLS):
        if effect != "none":
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


async def _visible_quad(session: Any, object_id: str, cdp_session_id: str | None) -> Any | None:
    """Scroll the node into view and return its largest content quad (frame-local coordinates)."""
    client = session.require_client()
    session_id = cdp_session_id or session.session_id
    try:
        await client.send(
            "DOM.scrollIntoViewIfNeeded",
            {"objectId": object_id},
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        )
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
        return quad
    except BrowserError as exc:
        if _NO_LAYOUT_RE.search(str(exc)):
            raise BadRequest(
                "目标节点无可见布局盒模型（可能隐藏、未渲染或为无尺寸包装节点）；"
                "请用 browser_find 查看带 [in-view] 或 [editable] 标记的候选元素"
            ) from exc
        raise


async def _hit_point(
    session: Any,
    object_id: str,
    entry: Any,
    cdp_session_id: str | None,
    offset: tuple[float, float],
) -> tuple[float, float]:
    """Scroll the node into view then read its box; return a parent-viewport point.

    ``DOM.getContentQuads`` returns coordinates in the frame that owns the node;
    for an OOPIF that is its local viewport, so the iframe offset is added.
    The snapshot bbox is already in parent coordinates and is used as-is.
    """
    quad = await _visible_quad(session, object_id, cdp_session_id)
    if quad is not None:
        x, y = _quad_center(quad)
        return x + offset[0], y + offset[1]
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
    quad = await _visible_quad(session, object_id, cdp_session_id)
    points = _quad_points(quad) if quad is not None else []
    if len(points) < 3:
        return []
    cx, cy = _quad_center(quad)
    return [
        (cx + (px - cx) * _CORNER_INSET + offset[0], cy + (py - cy) * _CORNER_INSET + offset[1]) for px, py in points
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
    x, y = await _hit_point(session, object_id, entry, cdp_session_id, offset)
    hit = await _safe_hit_test(session, object_id, x, y, cdp_session_id, offset, entry=entry)
    if hit is None:
        return x, y, "unverified", None
    if _confirmed(hit):
        return x, y, "centre", hit
    centre_hit = hit
    unanswered = False
    for attempt in range(2):
        if attempt:
            x, y = await _hit_point(session, object_id, entry, cdp_session_id, offset)
            hit = await _safe_hit_test(session, object_id, x, y, cdp_session_id, offset, entry=entry)
            if _confirmed(hit):
                return x, y, "centre", hit
            unanswered = unanswered or hit is None
        try:
            corners = await _corner_points(session, object_id, cdp_session_id, offset)
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

    A dom click is spent permanently per ref in this document: any record whose
    evidence carries the dom label (prepared, in flight or finished) refuses a
    second one, and the plugin's own dom-refusal rows are skipped when finding the
    newest page record. That record must be a finished all-occluded refusal, or an
    ``effect=none`` mouse click on a target that is not submit-like. A record still
    in flight (no result row), a blocked record that never sent, or a mouse click
    that may have landed keeps the gate shut. Matching anchors on the plugin's own
    leading text, so page text quoted in a record (a dialog message) cannot open
    the gate.
    """
    tag = str(facts.get("tag") or "").lower()
    role = str(facts.get("role") or "").lower()
    if tag not in _DOM_CLICK_TAGS and role != "button":
        return f"目标是 {tag or 'unknown'}（role={role or '-'}），dispatch=dom 只允许 button/a/input 或 role=button"
    token_re = re.compile(re.escape(entry.token) + r"(?!\d)")
    rows = session.journal.rows_for("browser_click", session.page_epoch)
    for row in rows:
        evidence = str(row.get("evidence") or "")
        if _DOM_USED in evidence and token_re.search(evidence):
            return f"dispatch=dom 对 {entry.token} 已用过一次，不要重复；请 snapshot 核对"
    newest: dict[str, Any] | None = None
    for row in rows:
        evidence = str(row.get("evidence") or "")
        if token_re.search(evidence) and not evidence.startswith(f"未点击 {entry.token}：{_DOM_REFUSED}"):
            newest = row
            break
    if newest is None:
        return f"本文档内尚无 {entry.token} 的 mouse 点击记录；请先用默认 dispatch=mouse 点击"
    evidence = str(newest.get("evidence") or "")
    status = str(newest.get("status") or "")
    phase = str(newest.get("phase") or "")
    if phase != RESULT:
        return f"{entry.token} 上一次点击尚无结果记录（phase={phase}，可能已生效），不要再点击；请 snapshot 核对"
    if status == "blocked" and evidence.startswith(f"未点击 {entry.token}：{_ALL_OCCLUDED}"):
        return None
    if status == "blocked" and evidence.startswith(f"未点击 {entry.token}："):
        first = evidence.splitlines()[0]
        return f"{entry.token} 最近一次点击未发送（{first}），dispatch=dom 不能绕过；请先按提示处理"
    if status == "uncertain" and evidence.startswith(f"未点击 {entry.token}：{_HIT_TEST_FAILED}"):
        return f"{entry.token} 上一次命中测试失败，未发送任何事件；请重新 snapshot 后用默认 dispatch=mouse 点击"
    if status == "uncertain" and "\neffect=none\n" in evidence:
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
    client = session.require_client()
    common = {"x": x, "y": y, "button": button, "clickCount": count, "pointerType": "mouse"}
    await client.send(
        "Input.dispatchMouseEvent",
        {**common, "type": "mouseMoved", "buttons": 0},
        session_id=session.session_id,
        timeout=session.cfg.cdp_timeout,
    )
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


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    baseline = await prepare_expect_baseline(session, arguments)
    blocks = await _run_click(session, arguments)
    return await run_post_write_expect(session, arguments, baseline=baseline, action_blocks=blocks)


async def _run_click(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
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
        record_action(session, action_id, "browser_click", "blocked", text)
        return [text_block(text)]

    submit_like = _is_submit_like(facts)
    dispatch = arguments.get("dispatch", "mouse")
    if dispatch == "dom":
        refusal = _dom_click_allowed(session, entry, facts, submit_like)
        if refusal is not None:
            text = (
                f"未点击 {entry.token}：{_DOM_REFUSED}：{refusal}\n"
                f"dispatch=not_sent\nstatus=blocked retryable=false\n{evidence_line(session, action_id)}"
            )
            record_action(session, action_id, "browser_click", "blocked", text)
            return [text_block(text)]
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
            record_action(session, action_id, "browser_click", "uncertain", text)
            return [text_block(text)]
        if where == "occluded":
            text = (
                f"未点击 {entry.token}：{_ALL_OCCLUDED}"
                f"（命中 {(hit or {}).get('tag') or 'unknown'}），未发送任何事件\n"
                "dispatch=not_sent\nstatus=blocked retryable=false\n"
                "请先关闭遮挡层或重新 snapshot；若目标是 button/a/input 或 role=button，"
                "可对同一 ref 用 dispatch=dom 重试一次\n"
                f"{evidence_line(session, action_id)}"
            )
            record_action(session, action_id, "browser_click", "blocked", text)
            return [text_block(text)]
    baseline_ok = True
    try:
        before = await _page_state(session, cdp_session_id)
    except ConnectionClosed:
        raise
    except (StaleRef, BrowserError):
        before = {}
        baseline_ok = False
    label = f"button={button}, click_count={count}" + (", dispatch=dom" if dispatch == "dom" else "")
    prepare_action(
        session,
        action_id,
        "browser_click",
        f"prepared click on {entry.token} ({label})",
    )
    dialog_seq = session.dialog_seq
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
        record_action(session, action_id, "browser_click", "uncertain", text)
        return [text_block(text)]
    if dialog is not None:
        mark_dispatched(
            session,
            action_id,
            "browser_click",
            f"click on {entry.token} is in flight; a dialog opened before it settled",
        )
        text = _dialog_blocked(session, entry, label, dialog, action_id)
        record_action(session, action_id, "browser_click", "blocked", text)
        return [text_block(text)]
    mark_dispatched(
        session,
        action_id,
        "browser_click",
        f"dispatched click on {entry.token} ({label})",
    )
    effect, _ = await _settled_effect(session, cdp_session_id, before, dialog_seq)
    if effect == "dialog":
        text = _dialog_blocked(session, entry, label, session.dialog_since(dialog_seq), action_id)
        record_action(session, action_id, "browser_click", "blocked", text)
        return [text_block(text)]
    verified_control = False
    if not baseline_ok:
        effect = "unknown"
    elif effect not in ("navigation", "unknown") and _has_control_state(facts):
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
                    record_action(session, action_id, "browser_click", "blocked", text)
                    return [text_block(text)]
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
        record_action(session, action_id, "browser_click", "blocked", text)
        return [text_block(text)]

    if effect in ("navigation", "dom", "target_detached"):
        session.note_success(session.target_id)

    status = "succeeded" if effect in ("navigation", "dom", "target_detached") else "uncertain"
    lines = [
        f"已点击 {entry.token}（{label}）",
        "dispatch=sent",
        f"effect={effect}",
        f"status={status} retryable=false",
        evidence_line(session, action_id),
    ]
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
    record_action(session, action_id, "browser_click", status, text)
    return [text_block(text)]
