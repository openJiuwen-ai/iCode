# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``browser_screenshot`` - capture a PNG (auto-downgraded to JPEG when oversized)."""

from __future__ import annotations

import re
from typing import Any

from ..cdp.websocket import FrameTooLarge
from ..config import MAX_IMAGE_BYTES
from ..errors import BadRequest, BrowserError, CDPTimeout, ConnectionClosed, Unavailable
from ..types import ToolBlock, image_block
from ._shared import call_value, resolve_oopif_offset, resolve_target

NAME = "browser_screenshot"

DESCRIPTION = (
    "Capture a screenshot as image content. full_page=true captures beyond the "
    "viewport; target (an @eN ref or free text) clips to that element's box. PNG "
    "is used unless it exceeds the 2.5MB cap, in which case the tool automatically "
    "re-captures as JPEG (quality 80); an image still above the cap fails, which "
    "full_page captures (the whole document) are the likeliest to hit, so prefer a "
    "target clip or a smaller viewport for very long pages. NOTE: annotate is "
    "accepted but currently a placeholder - the plugin has no third-party drawing "
    "dependency, so the raw screenshot is returned unannotated."
)

PARAMS: dict[str, Any] = {
    "full_page": {"type": "boolean", "default": False},
    "target": {"type": "string"},
    "annotate": {"type": "boolean", "default": True},
}

_TARGET_PAD_PX = 8.0
_FULL_PAGE_MAX_HEIGHT = 16384
_FULL_PAGE_MAX_PIXELS = 25_000_000
_NO_LAYOUT_RE = re.compile(
    r"does not have a layout object|could not compute (?:content quads|box model)", re.IGNORECASE
)
_TARGET_RECT_FN = (
    "function(){"
    " const r=this.getBoundingClientRect();"
    " let x=r.left, y=r.top;"
    " let win=this.ownerDocument && this.ownerDocument.defaultView;"
    " while(win && win.frameElement){"
    "   const fe=win.frameElement;"
    "   const fr=fe.getBoundingClientRect();"
    "   const pw=win.parent;"
    "   const cs=(pw && pw.getComputedStyle) ? pw.getComputedStyle(fe) : null;"
    "   x += fr.left + (cs ? (parseFloat(cs.borderLeftWidth)||0)+(parseFloat(cs.paddingLeft)||0) : 0);"
    "   y += fr.top + (cs ? (parseFloat(cs.borderTopWidth)||0)+(parseFloat(cs.paddingTop)||0) : 0);"
    "   win = pw;"
    " }"
    " const topWin=win || window;"
    " return {x, y, width:r.width, height:r.height,"
    "   scrollX:topWin.scrollX||0, scrollY:topWin.scrollY||0};"
    "}"
)


def _decoded_size(data: str) -> int:
    """Approximate the decoded byte length of a base64 payload."""
    return len(data) * 3 // 4


def _padded_clip(
    x: float, y: float, width: float, height: float, *, pad: float = _TARGET_PAD_PX
) -> dict[str, Any] | None:
    if width <= 0 or height <= 0:
        return None
    left = max(0.0, x - pad)
    top = max(0.0, y - pad)
    right = x + width + pad
    bottom = y + height + pad
    clip_w = right - left
    clip_h = bottom - top
    if clip_w <= 0 or clip_h <= 0:
        return None
    return {
        "x": left,
        "y": top,
        "width": clip_w,
        "height": clip_h,
        "scale": 1,
    }


def _clip_from_model(model: dict[str, Any]) -> dict[str, Any] | None:
    """Build a ``Page.captureScreenshot`` clip from a ``DOM.getBoxModel`` result.

    The box-model quads are CSS-pixel page coordinates; the clip uses the quad's
    bounding rectangle at scale 1.
    """
    quad = model.get("content") or model.get("border") or model.get("padding")
    if not quad:
        return None
    xs = [float(v) for v in quad[0::2]]
    ys = [float(v) for v in quad[1::2]]
    x, y = min(xs), min(ys)
    width, height = max(xs) - x, max(ys) - y
    return _padded_clip(x, y, width, height)


async def _target_clip(session: Any, entry: Any, object_id: str) -> tuple[dict[str, Any] | None, bool]:
    """Scroll *entry* into view and return ``(clip, beyond_viewport)`` in page coordinates."""
    client = session.require_client()
    cdp_session_id = getattr(entry, "cdp_session_id", None)
    session_id = cdp_session_id or session.session_id
    try:
        await client.send(
            "DOM.scrollIntoViewIfNeeded",
            {"objectId": object_id},
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        )
    except ConnectionClosed:
        raise
    except BrowserError as exc:
        if _NO_LAYOUT_RE.search(str(exc)):
            raise BadRequest(
                "目标节点无可见布局盒模型（可能隐藏或未渲染），无法裁剪截图；请改用视口截图或先 browser_find 确认可见节点"
            ) from exc
    # Read the OOPIF's live main-frame viewport origin *after* scrolling so
    # main-document scroll changes are not double-counted against a cached offset.
    offset = await resolve_oopif_offset(session, cdp_session_id) if cdp_session_id else (0.0, 0.0)
    rect_val: Any = None
    try:
        res = await call_value(session, object_id, _TARGET_RECT_FN, cdp_session_id=cdp_session_id)
        rect_val = res.get("value") if isinstance(res, dict) else None
    except ConnectionClosed:
        raise
    except BrowserError:
        rect_val = None
    if isinstance(rect_val, dict) and float(rect_val.get("width") or 0) > 0 and float(rect_val.get("height") or 0) > 0:
        if cdp_session_id:
            # For OOPIFs, offset is already in main-frame viewport coordinates and
            # rect_val.(x, y) is in the child frame's viewport coordinates. Add main-frame
            # scroll (not the child frame's internal scroll) for document coordinates.
            main_scroll_x, main_scroll_y = 0.0, 0.0
            try:
                scroll_res = await client.send(
                    "Runtime.evaluate",
                    {"expression": "({x: window.scrollX || 0, y: window.scrollY || 0})", "returnByValue": True},
                    session_id=session.session_id,
                    timeout=session.cfg.evaluate_timeout,
                )
                sv = ((scroll_res or {}).get("result") or {}).get("value")
                if isinstance(sv, dict):
                    main_scroll_x = float(sv.get("x") or 0)
                    main_scroll_y = float(sv.get("y") or 0)
            except ConnectionClosed:
                raise
            except BrowserError:
                pass
            page_x = float(rect_val.get("x") or 0) + offset[0] + main_scroll_x
            page_y = float(rect_val.get("y") or 0) + offset[1] + main_scroll_y
        else:
            page_x = float(rect_val.get("x") or 0) + float(rect_val.get("scrollX") or 0) + offset[0]
            page_y = float(rect_val.get("y") or 0) + float(rect_val.get("scrollY") or 0) + offset[1]
        return _padded_clip(page_x, page_y, float(rect_val["width"]), float(rect_val["height"])), True
    try:
        model_response = await client.send(
            "DOM.getBoxModel",
            {"objectId": object_id},
            session_id=session_id,
            timeout=session.cfg.cdp_timeout,
        )
    except ConnectionClosed:
        raise
    except BrowserError as exc:
        if _NO_LAYOUT_RE.search(str(exc)):
            raise BadRequest(
                "目标节点无可见布局盒模型（可能隐藏或未渲染），无法裁剪截图；请改用视口截图或先 browser_find 确认可见节点"
            ) from exc
        raise
    clip = _clip_from_model(model_response.get("model") or {})
    if clip and (offset[0] or offset[1]):
        clip = _padded_clip(
            clip["x"] + offset[0] + _TARGET_PAD_PX,
            clip["y"] + offset[1] + _TARGET_PAD_PX,
            max(0.0, clip["width"] - 2 * _TARGET_PAD_PX),
            max(0.0, clip["height"] - 2 * _TARGET_PAD_PX),
        )
    # DOM.getBoxModel returns viewport-relative coordinates, so keep captureBeyondViewport=False.
    return clip, False


_BG_SCREENSHOT_TIMEOUT = 2.5


async def _capture(session: Any, params: dict[str, Any], fmt: str) -> tuple[str, str]:
    client = session.require_client()
    probe_fn = getattr(session, "current_page_foreground", None)
    pre_foreground = await probe_fn() if callable(probe_fn) else None
    full_timeout = float(getattr(session.cfg, "cdp_timeout", 20.0))
    timeout = min(_BG_SCREENSHOT_TIMEOUT, full_timeout) if pre_foreground is False else full_timeout
    try:
        response = await client.send(
            "Page.captureScreenshot",
            params,
            session_id=session.session_id,
            timeout=timeout,
        )
    except CDPTimeout as exc:
        foreground = pre_foreground
        if foreground is not False and callable(probe_fn):
            foreground = await probe_fn()
        if foreground is False:
            raise Unavailable(
                "截图超时（reason=background_tab_throttled）：当前标签页处于后台，Chromium 渲染节流导致无法出图；"
                "请优先使用 browser_snapshot/browser_extract，或在需要视觉确认时先 browser_tab_switch 切到前台标签后重试"
            ) from exc
        raise
    data = str(response.get("data") or "")
    if not data:
        raise Unavailable("截图失败：Chrome 未返回图像数据")
    return data, ("image/png" if fmt == "png" else "image/jpeg")


async def _precheck_full_page_size(session: Any) -> None:
    """Reject enormous full-page dimensions before Chromium allocates a huge bitmap/frame."""
    client = session.require_client()
    try:
        metrics = await client.send(
            "Page.getLayoutMetrics",
            session_id=session.session_id,
            timeout=min(5.0, float(getattr(session.cfg, "cdp_timeout", 20.0))),
        )
    except ConnectionClosed:
        raise
    except BrowserError:
        return
    if not isinstance(metrics, dict):
        return
    size = metrics.get("cssContentSize") or metrics.get("contentSize")
    if not isinstance(size, dict):
        return
    width = float(size.get("width") or 0)
    height = float(size.get("height") or 0)
    if height > _FULL_PAGE_MAX_HEIGHT or (width > 0 and height > 0 and width * height > _FULL_PAGE_MAX_PIXELS):
        raise Unavailable(
            f"整页尺寸过大（{int(width)}×{int(height)} px），full_page 截图易超限或超时；"
            "请关闭 full_page 或指定 target 裁剪截图"
        )


async def run(session: Any, arguments: dict[str, Any]) -> list[ToolBlock]:
    full_page = arguments["full_page"]
    target = (arguments.get("target") or "").strip()

    params: dict[str, Any] = {"format": "png", "captureBeyondViewport": bool(full_page)}
    if target:
        entry = await resolve_target(session, target)
        object_id = await session.resolve_object_id(entry)
        clip, beyond = await _target_clip(session, entry, object_id)
        if clip:
            params["clip"] = clip
            params["captureBeyondViewport"] = bool(beyond)
    elif full_page:
        await _precheck_full_page_size(session)

    try:
        data, mime_type = await _capture(session, params, "png")
        oversized = _decoded_size(data) > MAX_IMAGE_BYTES
    except FrameTooLarge:
        data, mime_type = "", "image/png"
        oversized = True
    if oversized:
        params["format"] = "jpeg"
        params["quality"] = 80
        try:
            data, mime_type = await _capture(session, params, "jpeg")
        except FrameTooLarge as exc:
            raise Unavailable("截图超出 2.5MB 上限，请用 target 裁剪、缩小视口或关闭 full_page") from exc
        if _decoded_size(data) > MAX_IMAGE_BYTES:
            raise Unavailable("截图超出 2.5MB 上限，请用 target 裁剪、缩小视口或关闭 full_page")

    session.note_success(session.target_id)
    return [image_block(data, mime_type)]
