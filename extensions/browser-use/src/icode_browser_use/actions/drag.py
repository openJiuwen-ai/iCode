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
from .click import _hit_point, _locate_hit
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

    tx, ty = await _hit_point(session, target_oid, target_entry, target_cdp, session.oopif_offset(target_cdp))
    # Target scroll may have shifted the viewport; re-read source point without scrolling away if needed.
    # If both fit in viewport simultaneously, use the updated source coords cleanly.
    try:
        sx, sy = await _hit_point(session, source_oid, source_entry, source_cdp, session.oopif_offset(source_cdp))
        tx, ty = await _hit_point(session, target_oid, target_entry, target_cdp, session.oopif_offset(target_cdp))
    except BrowserError:
        pass

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
