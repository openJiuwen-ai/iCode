# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_drag`` - trusted CDP mouse & HTML5 drag-and-drop between two elements."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed, StaleRef
from ..types import ToolBlock, text_block
from ._shared import (
    clear_pending_input_releases,
    eval_value,
    flush_pending_input_releases,
    note_pending_input_release,
    resolve_oopif_offset,
    resolve_target,
    send_watching_dialog,
)
from .actionability import (
    WriteOutcome,
    amark_dispatched,
    aprepare_action,
    arecord_action,
    classify_effect,
    evidence_line,
    new_action_id,
    page_state,
    target_changed,
    target_disabled,
    target_facts,
)
from .click import (
    _CORNER_INSET,
    _NO_LAYOUT_RE,
    _confirmed,
    _hit_point,
    _largest_quad,
    _locate_hit,
    _quad_center,
    _quad_points,
    _safe_hit_test,
)
from .wait_for import EXPECT_PARAMS, prepare_expect_baseline, run_post_write_expect

NAME = "browser_drag"

DESCRIPTION = (
    "Drag source element onto target element using trusted CDP pointer and HTML5 "
    "drag events (mousePressed -> stepped mouseMoved -> HTML5 dragEnter/dragOver/drop "
    'when intercepted -> mouseReleased). Works for both native HTML5 draggable="true" '
    "boards and pointer-based sortable/kanban/slider widgets. Recorded in the action "
    "journal and never auto-retried."
)

PARAMS: dict[str, Any] = {
    "source": {"type": "string", "required": True},
    "target": {"type": "string", "required": True},
    "steps": {"type": "integer", "min": 1, "max": 30, "default": 8},
    **EXPECT_PARAMS,
}

_STEP_DELAY = 0.015
_SETTLE_POLL = 0.075
_SETTLE_POLLS = 3
_sleep = asyncio.sleep


async def _page_state(session: Any, cdp_session_id: str | None) -> dict[str, Any]:
    return await page_state(session, cdp_session_id, evaluate=eval_value)


async def _target_facts(session: Any, object_id: str, cdp_session_id: str | None) -> dict[str, Any]:
    return await target_facts(session, object_id, cdp_session_id)


async def _viewport_size(session: Any) -> tuple[float, float] | None:
    """Read the main frame's current viewport size; None on minimal test doubles."""
    try:
        val = await eval_value(session, "({w: window.innerWidth, h: window.innerHeight})")
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError):
        return None
    if isinstance(val, dict):
        w = float(val.get("w") or 0.0)
        h = float(val.get("h") or 0.0)
        if w > 0.0 and h > 0.0:
            return w, h
    return None


def _in_viewport(x: float, y: float, viewport: tuple[float, float] | None) -> bool:
    if viewport is None:
        return True
    return 0.0 <= x < viewport[0] and 0.0 <= y < viewport[1]


async def _current_quad_with_offset(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
) -> tuple[Any | None, tuple[float, float]]:
    """Read the node's live content quad and OOPIF offset WITHOUT scrolling the viewport."""
    client = session.require_client()
    session_id = cdp_session_id or session.session_id
    offset = await resolve_oopif_offset(session, cdp_session_id) if cdp_session_id else (0.0, 0.0)
    try:
        quads_response = await client.send(
            "DOM.getContentQuads",
            {"objectId": object_id},
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        )
        quad = _largest_quad(quads_response.get("quads") or []) if isinstance(quads_response, dict) else None
        if quad is None:
            model_response = await client.send(
                "DOM.getBoxModel",
                {"objectId": object_id},
                session_id=session_id,
                timeout=session.cfg.cdp_timeout,
            )
            model = (model_response.get("model") or {}) if isinstance(model_response, dict) else {}
            quad = model.get("content") or model.get("border") or model.get("padding")
        return quad, offset
    except BrowserError as exc:
        if _NO_LAYOUT_RE.search(str(exc)):
            raise BadRequest(
                "目标节点无可见布局盒模型（可能隐藏、未渲染或为无尺寸包装节点）；"
                "请用 browser_find 查看带 [in-view] 或 [editable] 标记的候选元素"
            ) from exc
        raise


async def _current_target_point(
    session: Any,
    object_id: str,
    cdp_session_id: str | None,
) -> tuple[float, float] | None:
    """Return the node's current viewport centre WITHOUT scrolling, or None if unavailable."""
    quad, live_offset = await _current_quad_with_offset(session, object_id, cdp_session_id)
    if quad is None:
        return None
    cx, cy = _quad_center(quad)
    return cx + live_offset[0], cy + live_offset[1]


async def _locate_in_current_viewport(
    session: Any,
    object_id: str,
    entry: Any,
    cdp_session_id: str | None,
    viewport: tuple[float, float] | None,
) -> tuple[float, float, str, dict[str, Any] | None] | None:
    """Re-locate and hit-test *object_id* in the CURRENT viewport without scrolling.

    Preserves a clickable corner when the centre is occluded, and returns
    ``where='offscreen'`` when neither the centre nor any corner lies inside
    *viewport*. Returns ``None`` only for minimal test doubles without quads.
    """
    quad, live_offset = await _current_quad_with_offset(session, object_id, cdp_session_id)
    if quad is None:
        return None
    cx, cy = _quad_center(quad)
    centre_x, centre_y = cx + live_offset[0], cy + live_offset[1]
    candidates: list[tuple[str, float, float]] = [("centre", centre_x, centre_y)]
    points = _quad_points(quad)
    if len(points) >= 3:
        for px, py in points:
            candidates.append(
                (
                    "corner",
                    cx + (px - cx) * _CORNER_INSET + live_offset[0],
                    cy + (py - cy) * _CORNER_INSET + live_offset[1],
                )
            )
    in_view = [(kind, x, y) for kind, x, y in candidates if _in_viewport(x, y, viewport)]
    if not in_view:
        return centre_x, centre_y, "offscreen", None
    first_hit: dict[str, Any] | None = None
    unanswered = False
    for kind, x, y in in_view:
        hit = await _safe_hit_test(session, object_id, x, y, cdp_session_id, live_offset, entry=entry)
        if _confirmed(hit):
            return x, y, kind, hit
        if first_hit is None and isinstance(hit, dict):
            first_hit = hit
        if hit is None:
            unanswered = True
    return centre_x, centre_y, ("unverified" if unanswered else "occluded"), first_hit


async def _blocked_not_sent(
    session: Any,
    source_entry: Any,
    target_entry: Any,
    action_id: str,
    *,
    status: str,
    reason: str,
    detail: str,
) -> WriteOutcome:
    text = (
        f"未拖拽 {source_entry.token} -> {target_entry.token}：{detail}，未发送任何事件\n"
        f"dispatch=not_sent\nstatus={status} retryable=false\n"
        f"{evidence_line(session, action_id)}"
    )
    await arecord_action(
        session,
        action_id,
        "browser_drag",
        status,
        text,
        target_ref=source_entry.token,
        dispatch="not_sent",
        reason=reason,
    )
    return WriteOutcome(status=status, dispatched=False, blocks=[text_block(text)], action_id=action_id)


async def _dispatch_drag(
    session: Any,
    sx: float,
    sy: float,
    tx: float,
    ty: float,
    steps: int,
) -> str:
    """Execute trusted pointer drag; seamlessly hand off to CDP drag events if HTML5 DnD intercepts."""
    await flush_pending_input_releases(session)
    client = session.require_client()
    timeout = session.cfg.cdp_timeout
    sid = session.session_id

    intercepted_data: list[dict[str, Any]] = []
    intercept_enabled = False
    has_events = callable(getattr(client, "on", None))

    async def _on_drag_intercepted(params: dict[str, Any], event_sid: str | None) -> None:
        if event_sid is not None and sid is not None and event_sid != sid:
            return
        data = params.get("data")
        if isinstance(data, dict):
            intercepted_data.append(data)

    if has_events:
        client.on("Input.dragIntercepted", _on_drag_intercepted)
        with contextlib.suppress(BrowserError, CDPTimeout):
            await client.send("Input.setInterceptDrags", {"enabled": True}, session_id=sid, timeout=min(5.0, timeout))
            intercept_enabled = True

    release_params = {
        "type": "mouseReleased",
        "x": tx,
        "y": ty,
        "button": "left",
        "buttons": 0,
        "clickCount": 0,
        "pointerType": "mouse",
    }
    mode = "pointer"
    try:
        await client.send(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": sx, "y": sy, "button": "none", "buttons": 0, "pointerType": "mouse"},
            session_id=sid,
            timeout=timeout,
        )
        note_pending_input_release(session, "Input.dispatchMouseEvent", release_params, sid)
        await client.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mousePressed",
                "x": sx,
                "y": sy,
                "button": "left",
                "buttons": 1,
                "clickCount": 1,
                "pointerType": "mouse",
            },
            session_id=sid,
            timeout=timeout,
        )
        total_steps = max(1, int(steps))
        for step in range(1, total_steps + 1):
            frac = step / total_steps
            mx = sx + (tx - sx) * frac
            my = sy + (ty - sy) * frac
            await client.send(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseMoved",
                    "x": mx,
                    "y": my,
                    "button": "left",
                    "buttons": 1,
                    "clickCount": 0,
                    "pointerType": "mouse",
                },
                session_id=sid,
                timeout=timeout,
            )
            if _STEP_DELAY > 0:
                await _sleep(_STEP_DELAY)
            if intercepted_data:
                break

        if intercepted_data:
            mode = "html5"
            drag_data = intercepted_data[-1]
            for event_type in ("dragEnter", "dragOver", "drop"):
                await client.send(
                    "Input.dispatchDragEvent",
                    {"type": event_type, "x": tx, "y": ty, "data": drag_data},
                    session_id=sid,
                    timeout=timeout,
                )
                if _STEP_DELAY > 0:
                    await _sleep(_STEP_DELAY)

        await client.send(
            "Input.dispatchMouseEvent",
            {
                "type": "mouseReleased",
                "x": tx,
                "y": ty,
                "button": "left",
                "buttons": 0,
                "clickCount": 1,
                "pointerType": "mouse",
            },
            session_id=sid,
            timeout=timeout,
        )
        clear_pending_input_releases(session)
        return mode
    finally:
        if intercept_enabled:
            with contextlib.suppress(Exception):
                await client.send(
                    "Input.setInterceptDrags", {"enabled": False}, session_id=sid, timeout=min(3.0, timeout)
                )
        off = getattr(client, "off", None)
        if callable(off):
            off("Input.dragIntercepted", _on_drag_intercepted)


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    baseline = await prepare_expect_baseline(session, arguments)
    outcome = await _run_drag(session, arguments)
    return await run_post_write_expect(session, arguments, baseline=baseline, outcome=outcome)


async def _run_drag(session: Any, arguments: dict[str, Any]) -> WriteOutcome:
    source_spec = arguments["source"]
    target_spec = arguments["target"]
    steps = int(arguments.get("steps", 8))

    source_entry = await resolve_target(session, source_spec)
    target_entry = await resolve_target(session, target_spec)
    if (
        source_entry.frame_id == target_entry.frame_id
        and source_entry.backend_node_id == target_entry.backend_node_id
        and source_entry.cdp_session_id == target_entry.cdp_session_id
    ):
        raise BadRequest("source 与 target 指向同一个元素，无法执行拖拽")

    action_id = new_action_id()
    source_oid = await session.resolve_object_id(source_entry)
    target_oid = await session.resolve_object_id(target_entry)
    source_cdp = source_entry.cdp_session_id
    target_cdp = target_entry.cdp_session_id

    source_facts = await _target_facts(session, source_oid, source_cdp)
    if target_disabled(source_facts):
        text = (
            f"未拖拽 {source_entry.token} -> {target_entry.token}：源元素处于 disabled 状态，未发送任何事件\n"
            "dispatch=not_sent\nstatus=blocked retryable=false\n"
            f"{evidence_line(session, action_id)}"
        )
        await arecord_action(
            session,
            action_id,
            "browser_drag",
            "blocked",
            text,
            target_ref=source_entry.token,
            dispatch="not_sent",
            reason="disabled",
        )
        return WriteOutcome(status="blocked", dispatched=False, blocks=[text_block(text)], action_id=action_id)

    sx, sy, s_where, s_hit = await _locate_hit(
        session, source_oid, source_entry, source_cdp, session.oopif_offset(source_cdp), hit_point=_hit_point
    )
    if s_where not in ("centre", "corner"):
        reason = "all_occluded" if s_where == "occluded" else "hit_test_failed"
        detail = (
            f"源元素中心与四角均被遮挡（命中 {(s_hit or {}).get('tag') or 'unknown'}）"
            if s_where == "occluded"
            else "源元素命中测试失败，无法确认落点"
        )
        status = "blocked" if s_where == "occluded" else "uncertain"
        return await _blocked_not_sent(
            session, source_entry, target_entry, action_id, status=status, reason=reason, detail=detail
        )

    # Check target in the current viewport WITHOUT scrolling first so we never clobber
    # source's verified corner point or silently scroll source off-screen.
    viewport = await _viewport_size(session)
    scrolled_for_target = False
    live_target = await _current_target_point(session, target_oid, target_cdp)
    if live_target is not None:
        tx, ty = live_target
        if not _in_viewport(tx, ty, viewport):
            # If both fit within one viewport, scroll to frame both together first.
            if viewport is not None and abs(tx - sx) < viewport[0] - 24 and abs(ty - sy) < viewport[1] - 24:
                dx = ((min(sx, tx) + max(sx, tx)) / 2.0) - (viewport[0] / 2.0)
                dy = ((min(sy, ty) + max(sy, ty)) / 2.0) - (viewport[1] / 2.0)
                with contextlib.suppress(BrowserError, CDPTimeout):
                    await eval_value(session, f"window.scrollBy({dx!r}, {dy!r})")
                scrolled_for_target = True
                refreshed_target = await _current_target_point(session, target_oid, target_cdp)
                if refreshed_target is not None:
                    tx, ty = refreshed_target
            if not _in_viewport(tx, ty, viewport):
                tx, ty = await _hit_point(
                    session, target_oid, target_entry, target_cdp, session.oopif_offset(target_cdp)
                )
                scrolled_for_target = True
    else:
        # Minimal test double without DOM.getContentQuads: resolve target only and preserve (sx, sy).
        tx, ty = await _hit_point(session, target_oid, target_entry, target_cdp, session.oopif_offset(target_cdp))

    # Re-verify BOTH endpoints in the current viewport WITHOUT scrolling right before pointerdown.
    if scrolled_for_target:
        viewport = await _viewport_size(session) or viewport
        refreshed_target = await _current_target_point(session, target_oid, target_cdp)
        if refreshed_target is not None:
            tx, ty = refreshed_target

    re_source = await _locate_in_current_viewport(session, source_oid, source_entry, source_cdp, viewport)
    if re_source is not None:
        sx, sy, s_where, s_hit = re_source
        if s_where not in ("centre", "corner"):
            if s_where == "offscreen":
                return await _blocked_not_sent(
                    session,
                    source_entry,
                    target_entry,
                    action_id,
                    status="blocked",
                    reason="not_simultaneously_visible",
                    detail="源元素与目标元素无法同时位于当前视口内（跨屏拖拽暂不支持拖动中滚动）",
                )
            reason = "all_occluded" if s_where == "occluded" else "hit_test_failed"
            detail = (
                f"派发前复核发现源元素中心与四角均被遮挡（命中 {(s_hit or {}).get('tag') or 'unknown'}）"
                if s_where == "occluded"
                else "派发前重新验证源元素命中测试失败，无法确认落点"
            )
            status = "blocked" if s_where == "occluded" else "uncertain"
            return await _blocked_not_sent(
                session, source_entry, target_entry, action_id, status=status, reason=reason, detail=detail
            )

    if viewport is not None and (not _in_viewport(sx, sy, viewport) or not _in_viewport(tx, ty, viewport)):
        return await _blocked_not_sent(
            session,
            source_entry,
            target_entry,
            action_id,
            status="blocked",
            reason="not_simultaneously_visible",
            detail="源元素与目标元素无法同时位于当前视口内（跨屏拖拽暂不支持拖动中滚动）",
        )

    baseline_ok = True
    try:
        before = await _page_state(session, target_cdp or source_cdp)
    except ConnectionClosed:
        raise
    except (StaleRef, BrowserError):
        before = {}
        baseline_ok = False

    await aprepare_action(
        session,
        action_id,
        "browser_drag",
        f"prepared drag {source_entry.token} -> {target_entry.token} (steps={steps})",
        target_ref=source_entry.token,
        dispatch="mouse",
    )
    dialog_seq = session.dialog_seq
    session.mark_dirty()
    try:
        drag_mode, dialog = await send_watching_dialog(
            session, dialog_seq, lambda: _dispatch_drag(session, sx, sy, tx, ty, steps)
        )
    except ConnectionClosed:
        raise
    except (CDPTimeout, BrowserError) as exc:
        text = (
            f"已发送拖拽 {source_entry.token} -> {target_entry.token}\n"
            "dispatch=unknown\neffect=unknown\nstatus=uncertain retryable=false\n"
            f"reason=dispatch_failed:{type(exc).__name__}；不要盲目重试，请 snapshot 复核\n"
            f"{evidence_line(session, action_id)}"
        )
        await arecord_action(
            session,
            action_id,
            "browser_drag",
            "uncertain",
            text,
            target_ref=source_entry.token,
            dispatch="mouse",
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
            "browser_drag",
            f"drag {source_entry.token} -> {target_entry.token} interrupted by dialog",
            target_ref=source_entry.token,
            dispatch="mouse",
        )
        text = "\n".join(
            [
                f"已发送拖拽 {source_entry.token} -> {target_entry.token}",
                dialog.fields(),
                "对话框出现：拖拽结果未确认，请先调用 browser_dialog 再 browser_snapshot 复核",
                "status=blocked retryable=false",
                evidence_line(session, action_id),
            ]
        )
        await arecord_action(
            session,
            action_id,
            "browser_drag",
            "blocked",
            text,
            target_ref=source_entry.token,
            dispatch="mouse",
            reason="dialog",
            effect="dialog",
        )
        return WriteOutcome(
            status="blocked", dispatched=True, blocks=[text_block(text)], effect="dialog", action_id=action_id
        )

    await amark_dispatched(
        session,
        action_id,
        "browser_drag",
        f"dispatched drag {source_entry.token} -> {target_entry.token} (mode={drag_mode})",
        target_ref=source_entry.token,
        dispatch=drag_mode,
    )

    effect = "none"
    if not baseline_ok:
        effect = "unknown"
    else:
        for _ in range(_SETTLE_POLLS):
            await _sleep(_SETTLE_POLL)
            if session.dialog_since(dialog_seq) is not None:
                effect = "dialog"
                break
            try:
                after = await _page_state(session, target_cdp or source_cdp)
            except ConnectionClosed:
                raise
            except (CDPTimeout, BrowserError):
                effect = "unknown"
                break
            effect = classify_effect(before, after)
            if effect in ("navigation", "dom"):
                break
        if effect in ("none", "focus"):
            try:
                after_facts = await _target_facts(session, source_oid, source_cdp)
                if after_facts.get("connected") is False:
                    effect = "target_detached"
                elif target_changed(source_facts, after_facts):
                    effect = "dom"
            except (CDPTimeout, BrowserError):
                pass

    if effect in ("navigation", "dom", "target_detached"):
        session.note_success(session.target_id)
    status = "succeeded" if effect in ("navigation", "dom", "target_detached") else "uncertain"
    lines = [
        f"已拖拽 {source_entry.token} -> {target_entry.token}（mode={drag_mode}, steps={steps}）",
        "dispatch=sent",
        f"effect={effect}",
        f"status={status} retryable=false",
        evidence_line(session, action_id),
    ]
    if status != "succeeded":
        lines.append("拖拽未观察到明确 DOM 变更；请调用 browser_snapshot 核对页面状态")
    text = "\n".join(lines)
    await arecord_action(
        session,
        action_id,
        "browser_drag",
        status,
        text,
        target_ref=source_entry.token,
        dispatch=drag_mode,
        effect=effect,
    )
    return WriteOutcome(status=status, dispatched=True, blocks=[text_block(text)], effect=effect, action_id=action_id)
